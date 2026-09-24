"""模拟盘 → 大 QMT 真单镜像（双轨并行）。

模拟盘（虚拟撮合）成交后，按配置**额外**向大 QMT 提交一笔真实委托；虚拟账本
行为完全不变（双轨），两边靠 ``client_order_id`` 关联，供对账视图比对滑点/费用。

调用点（两处，均只调本模块一个函数，禁止各写一份逻辑）：
  * ``simulation/engine.py::SimulationEngine._execute_order`` —— 调度器自动调仓
  * ``internal_strategy_dispatcher.py`` SIMULATION/SHADOW 分支 —— 内部/托管任务

生产控制（缺一不可，全部 fail-closed：读不到配置/异常一律不下单）
  * 开关：``SIMULATION_MIRROR_TO_REAL``（env，默认 false）+ Redis ``mirror:enabled`` 热开关
  * 急停：Redis ``mirror:kill`` 置 1 → 立即停单（最高优先级）
  * 白名单：Redis SET ``mirror:whitelist``，元素 ``*`` / ``tenant`` / ``tenant:user`` /
    ``tenant:user:strategy``；**空集合 = 不镜像任何策略**
  * 黑名单：Redis SET ``mirror:blacklist``（symbol，前缀式如 ``SH600519``）
  * 限额（小额试水口径）：单笔 ≤1 万、单日 ≤5 万、单日 ≤5 只标的、单日 ≤20 笔
  * 资金/持仓闸门：买入校验 QMT 可用资金，卖出校验 QMT 可用持仓
  * 熔断：连续拒单 ≥ N（默认 3）自动置 ``mirror:kill`` 并推送通知
  * 时段：非交易时段入队（Redis ``mirror:queue``），由 ``run_mirror_queue_drainer``
    在下一交易时段开盘提交，滑点在对账中标注

限额计数用 Lua 脚本**原子**预留，提交失败会回滚预留，避免并发下的超额。

限额口径只覆盖**镜像路径**：绕开镜像直接走 REAL 下单（``broker:selected:CN=qmt_exec``
+ ``trading_mode=REAL``）不受本模块的名单/限额约束，那属于既有的实盘通道口径。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from backend.services.live_trading.services import lot_rules
from backend.services.live_trading.services.qmt_account_sync_task import (
    batch_quantdb_last_close,
)
from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecError,
    get_qmt_exec_client,
)
from backend.services.live_trading.services.trading_session import (
    is_trading_time,
    trade_date_str,
)
from backend.shared.live_trading_gate import is_real_trading_enabled
from backend.shared.simulation_account_keys import (
    canonical_sim_user_suffix,
    resolve_db_account_user,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ENABLED_KEY = "mirror:enabled"
_KILL_KEY = "mirror:kill"
_WHITELIST_KEY = "mirror:whitelist"
_BLACKLIST_KEY = "mirror:blacklist"
_QUEUE_KEY = "mirror:queue"
_QUEUED_SET_KEY = "mirror:queued"
_REJECTS_KEY = "mirror:rejects"
_CONFIG_KEY = "mirror:config"
_DAILY_KEY = "mirror:daily:{date}:{field}"
_DAILY_TTL_SECONDS = 3 * 24 * 3600
_QUEUE_TTL_SECONDS = 7 * 24 * 3600
_MAX_CLIENT_ORDER_ID_LEN = 100

# 强平/止损单（bypass_price_gate）的 sanity 上界：偏离昨收超过该比例视为脏数据，
# 即使豁免 2% 偏离闸门也不下单（防把过期/错符号的报价当盘口价打出去）。
# 数值的**唯一出处**在 lot_rules（与逐笔限价的合理带上界同源），此处只是别名。
_SANITY_MAX_DRIFT = lot_rules.SANITY_MAX_DRIFT

# 镜像跳过记录：mirror:skipped:{YYYYMMDD} 哈希，field={symbol}:{reason} → 次数，
# 供当日双轨对账报表使用（TTL 7 天）。
_SKIP_HASH_PREFIX = "mirror:skipped:"
_SKIP_TTL_SECONDS = 7 * 24 * 3600

# 镜像**下单失败**台账：mirror:failed:{YYYYMMDD} 哈希，形状同跳过记录。
# 与跳过分开记账：跳过是「我们决定不发」，失败是「发了、没成」——一个看策略闸门，
# 一个看通道；混进同一个哈希，对账报表就只能靠 reason 前缀猜。
_FAIL_HASH_PREFIX = "mirror:failed:"
_FAIL_TTL_SECONDS = 7 * 24 * 3600

# 失败告警去重键：mirror:alert:{YYYYMMDD}:{kind}——**一类一天一条**。
_ALERT_KEY_FMT = "mirror:alert:{date}:{kind}"
_ALERT_TTL_SECONDS = 7 * 24 * 3600
ALERT_SUBMIT_FAILED = "submit_failed"

# 账户/行情缓存：避免每笔镜像都打一次 RPC
_ACCOUNT_CACHE_SECONDS = 10.0
_account_cache: dict[str, Any] = {"at": 0.0, "data": None}

# 原子限额预留：KEYS=[日金额, 日笔数, 日标的集合]
# ARGV=[本次金额, symbol, 单笔上限, 单日金额上限, 单日笔数上限, 单日标的上限, TTL]
# 返回 {是否通过, 原因, 当日金额, 当日笔数, 当日标的数, 本笔是否新标的}
_RESERVE_LUA = """
local dv = tonumber(redis.call('GET', KEYS[1]) or '0')
local orders = tonumber(redis.call('GET', KEYS[2]) or '0')
local symbols = redis.call('SCARD', KEYS[3])
local new_flag = 0
if redis.call('SISMEMBER', KEYS[3], ARGV[2]) == 0 then new_flag = 1 end
local value = tonumber(ARGV[1])
if value > tonumber(ARGV[3]) then
  return {0, 'max_order_value', tostring(dv), tostring(orders),
          tostring(symbols), tostring(new_flag)}
end
if dv + value > tonumber(ARGV[4]) then
  return {0, 'max_daily_value', tostring(dv), tostring(orders),
          tostring(symbols), tostring(new_flag)}
end
if orders + 1 > tonumber(ARGV[5]) then
  return {0, 'max_daily_orders', tostring(dv), tostring(orders),
          tostring(symbols), tostring(new_flag)}
end
if new_flag == 1 and symbols + 1 > tonumber(ARGV[6]) then
  return {0, 'max_daily_symbols', tostring(dv), tostring(orders),
          tostring(symbols), tostring(new_flag)}
end
redis.call('INCRBYFLOAT', KEYS[1], value)
redis.call('INCR', KEYS[2])
redis.call('SADD', KEYS[3], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[7])
redis.call('EXPIRE', KEYS[2], ARGV[7])
redis.call('EXPIRE', KEYS[3], ARGV[7])
return {1, 'ok', tostring(dv + value), tostring(orders + 1),
        tostring(symbols + new_flag), tostring(new_flag)}
"""


def _one(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE_VALUES


def _as_float(value: Any) -> float:
    """Redis 返回值（bytes/str/数值）→ float，非法值归 0。"""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="ignore")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    return int(_as_float(value))


def _as_text(value: Any) -> str:
    """Redis 返回值（bytes/str）→ str（避免 ``str(b'ok')`` 变成 ``"b'ok'"``）。"""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MirrorConfig:
    """镜像风控参数（env 基线 + Redis ``mirror:config`` 覆盖）。"""

    enabled: bool = False
    max_order_value: float = 10000.0
    max_daily_value: float = 50000.0
    max_daily_symbols: int = 5
    max_daily_orders: int = 20
    max_slippage_pct: float = 0.02
    max_consecutive_rejects: int = 3
    queue_outside_hours: bool = True
    markets: frozenset[str] = frozenset({"CN"})


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name, "")
        return float(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name, "")
        return int(float(raw)) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if not str(raw).strip():
        return default
    return _one(raw)


def _redis_client(redis: Any) -> Any:
    return getattr(redis, "client", None) if redis is not None else None


def _redis_get(redis: Any, key: str) -> str:
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")
    raw = client.get(key)
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="ignore")
    return str(raw or "")


def _read_config_overrides(redis: Any) -> dict[str, Any]:
    try:
        raw = _redis_get(redis, _CONFIG_KEY)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 读不到就用 env 基线
        return {}


def load_config(redis: Any = None) -> MirrorConfig:
    """加载生效配置：env 基线 → Redis ``mirror:config`` JSON 覆盖。"""
    cfg = MirrorConfig(
        enabled=_env_bool("SIMULATION_MIRROR_TO_REAL", False)
        or _env_bool("MIRROR_ENABLED", False),
        max_order_value=_env_float("MIRROR_MAX_ORDER_VALUE", 10000.0),
        max_daily_value=_env_float("MIRROR_MAX_DAILY_VALUE", 50000.0),
        max_daily_symbols=_env_int("MIRROR_MAX_DAILY_SYMBOLS", 5),
        max_daily_orders=_env_int("MIRROR_MAX_DAILY_ORDERS", 20),
        max_slippage_pct=_env_float("MIRROR_MAX_SLIPPAGE_PCT", 0.02),
        max_consecutive_rejects=_env_int("MIRROR_MAX_CONSECUTIVE_REJECTS", 3),
        queue_outside_hours=_env_bool("MIRROR_QUEUE_OUTSIDE_HOURS", True),
    )
    over = _read_config_overrides(redis)
    if not over:
        return cfg
    changes: dict[str, Any] = {}
    for field, caster in (
        ("max_order_value", float),
        ("max_daily_value", float),
        ("max_slippage_pct", float),
        ("max_daily_symbols", int),
        ("max_daily_orders", int),
        ("max_consecutive_rejects", int),
    ):
        if over.get(field) is None:
            continue
        try:
            changes[field] = caster(over[field])
        except (TypeError, ValueError):
            logger.warning("[Mirror] 忽略非法配置项 %s=%r", field, over[field])
    if over.get("enabled") is not None:
        changes["enabled"] = _one(over["enabled"])
    if over.get("queue_outside_hours") is not None:
        changes["queue_outside_hours"] = _one(over["queue_outside_hours"])
    if over.get("markets"):
        changes["markets"] = frozenset(
            str(m).upper() for m in over["markets"] if str(m).strip()
        )
    return replace(cfg, **changes) if changes else cfg


def kill_switch_on(redis: Any) -> bool:
    """急停开关。**读失败视为已急停**（真钱路径 fail-closed）。"""
    try:
        return _one(_redis_get(redis, _KILL_KEY))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 急停开关读取失败，按已急停处理: %s", exc)
        return True


def set_kill_switch(redis: Any, on: bool) -> None:
    """运维/熔断置位。``on=False`` 清除。"""
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")
    if on:
        client.set(_KILL_KEY, "1")
    else:
        client.delete(_KILL_KEY)


def mirror_enabled(redis: Any, cfg: MirrorConfig) -> bool:
    """总开关：实盘闸门 > 急停 > Redis 热开关 > env。

    闸门排最前：`ENABLE_REAL_TRADING=false` 时镜像**恒关**，不看 Redis 里留了什么。
    Redis 的 ``mirror:enabled`` 是运维热开关，可能来自之前开着实盘的会话——不清掉
    会让一个「本部署没有实盘」的实例在状态页上报「镜像已启用」。判定方向与
    ``_real_trading_ready`` 一致，两处不会互相打架。
    """
    if not is_real_trading_enabled():
        return False
    if kill_switch_on(redis):
        return False
    try:
        raw = _redis_get(redis, _ENABLED_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 热开关读取失败，按关闭处理: %s", exc)
        return False
    if not raw.strip():
        return cfg.enabled
    return _one(raw)


def _canonical_whitelist_entry(entry: str) -> str:
    """把白名单条目里的用户段归一到规范键形（管理员族一律 10000001）。

    条目形如 ``tenant`` / ``tenant:user`` / ``tenant:user:strategy``；只归第二段，
    tenant 与 strategy 原样保留（strategy_id 是 UUID，不存在别名族）。非管理员族的
    数字用户（``default:42``）经 ``canonical_sim_user_suffix`` 是恒等变换，不受影响。
    """
    text = str(entry or "").strip()
    if not text or text == "*" or ":" not in text:
        return text
    tenant, _, rest = text.partition(":")
    user, sep, strategy = rest.partition(":")
    canonical = canonical_sim_user_suffix(user)
    return f"{tenant}:{canonical}{sep}{strategy}" if sep else f"{tenant}:{canonical}"


def whitelist_allows(
    redis: Any, *, tenant_id: str, user_id: str, strategy_id: str = ""
) -> bool:
    """白名单匹配：``*`` / tenant / tenant:user / tenant:user:strategy。空集合=全否。

    **两侧都过键形归一**。存量白名单写的是历史别名（实测线上是
    ``{default:00000001, default:1}``），而调用方传的是规范账户 ``10000001``；
    精确串匹配下两者永不相等 → 真单被静默 ``skipped(whitelist)``，界面上只表现为
    「实盘通道没反应」。归一只在用户段做：tenant 原样、strategy 原样（UUID 无别名）。
    """
    client = _redis_client(redis)
    if client is None:
        return False
    try:
        entries = client.smembers(_WHITELIST_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 白名单读取失败，按不镜像处理: %s", exc)
        return False
    if not entries:
        return False
    normalized = {
        _canonical_whitelist_entry(
            e.decode() if isinstance(e, (bytes, bytearray)) else str(e)
        )
        for e in entries
    }
    tenant = str(tenant_id or "default").strip() or "default"
    user = canonical_sim_user_suffix(str(user_id or "").strip())
    strategy = str(strategy_id or "").strip()
    keys = {tenant, f"{tenant}:{user}"}
    if strategy:
        keys.add(f"{tenant}:{user}:{strategy}")
    return "*" in normalized or bool(normalized & keys)


def _is_blacklisted(redis: Any, symbol: str) -> bool:
    client = _redis_client(redis)
    if client is None:
        return True
    try:
        return bool(client.sismember(_BLACKLIST_KEY, str(symbol or "").upper()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 黑名单读取失败，按黑名单处理: %s", exc)
        return True


def _daily_key(field: str, date_str: str | None = None) -> str:
    return _DAILY_KEY.format(date=date_str or trade_date_str(), field=field)


def _record_ledger(
    prefix: str,
    redis: Any,
    *,
    symbol: str,
    reason: str,
    detail: dict[str, Any],
    ttl: int,
) -> None:
    """当日台账写一笔（哈希 ``field={symbol}:{reason}`` → 次数 + ``:detail``），只记日志。"""
    client = _redis_client(redis)
    if client is None:
        return
    key = f"{prefix}{trade_date_str()}"
    field = f"{symbol}:{reason}"
    payload = json.dumps(detail, ensure_ascii=False)
    try:
        pipe = client.pipeline()
        pipe.hincrby(key, field, 1)
        pipe.hset(key, f"{field}:detail", payload)
        pipe.expire(key, ttl)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Mirror] 记录台账失败 %s %s: %s", symbol, reason, exc)


def _load_ledger(
    prefix: str, redis: Any, date_str: str | None = None
) -> dict[str, int]:
    """读某日台账：``{"symbol:reason": count}``（``:detail`` 行不算计数）。"""
    client = _redis_client(redis)
    if client is None:
        return {}
    key = f"{prefix}{date_str or trade_date_str()}"
    try:
        raw = client.hgetall(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 读取台账失败: %s", exc)
        return {}
    counts: dict[str, int] = {}
    for field, value in (raw or {}).items():
        name = field.decode() if isinstance(field, bytes) else str(field)
        if name.endswith(":detail"):
            continue
        try:
            counts[name] = int(value)
        except (TypeError, ValueError):
            continue
    return counts


def _entry_detail(side: str, quantity: float, source: str) -> dict[str, Any]:
    return {
        "side": side,
        "quantity": quantity,
        "source": source,
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def record_skip(
    redis: Any,
    *,
    symbol: str,
    side: str,
    quantity: float,
    reason: str,
    source: str,
) -> None:
    """记录一次镜像跳过（供当日双轨对账报表），失败只记日志不打断下单流程。"""
    _record_ledger(
        _SKIP_HASH_PREFIX,
        redis,
        symbol=symbol,
        reason=reason,
        detail=_entry_detail(side, quantity, source),
        ttl=_SKIP_TTL_SECONDS,
    )


def load_skips(redis: Any, date_str: str | None = None) -> dict[str, int]:
    """读取某日的跳过计数：{ "symbol:reason": count }（对账报表用）。"""
    return _load_ledger(_SKIP_HASH_PREFIX, redis, date_str)


def record_failure(
    redis: Any,
    *,
    symbol: str,
    side: str,
    quantity: float,
    reason: str,
    source: str,
) -> None:
    """记录一次真单**提交失败**（异常 / 拒单 / 券商拒收），当日按标的与原因计数。

    与 :func:`record_skip` 分开记（跳过 = 决定不发，失败 = 发了没成）。也与
    ``mirror:rejects`` 是两回事：那是**连续**计数、成功一笔就清零，间歇性失败
    （失败一笔、成功一笔、再失败一笔）在它上面永远到不了熔断阈值，于是既没有急停、
    也没有通知、状态快照还读 0 —— 当日台账补的就是这一格。
    """
    _record_ledger(
        _FAIL_HASH_PREFIX,
        redis,
        symbol=symbol,
        reason=reason,
        detail=_entry_detail(side, quantity, source),
        ttl=_FAIL_TTL_SECONDS,
    )


def load_failures(redis: Any, date_str: str | None = None) -> dict[str, int]:
    """读取某日的失败计数：{ "symbol:reason": count }（状态快照 / 对账用）。"""
    return _load_ledger(_FAIL_HASH_PREFIX, redis, date_str)


# --------------------------------------------------------------------------
# 运维接口（控制面：路由层只用这些，不直接碰 Redis 键）
# --------------------------------------------------------------------------
def env_enabled() -> bool:
    """env 基线开关（页面显示用；真正是否下单还要看急停/热开关/白名单）。"""
    return _env_bool("SIMULATION_MIRROR_TO_REAL", False) or _env_bool(
        "MIRROR_ENABLED", False
    )


def real_trading_ready(redis: Any, market: str = "CN") -> tuple[bool, str]:
    """公开包装：实盘通道是否就绪（``ENABLE_REAL_TRADING`` + 市场选定 qmt_exec）。"""
    return _real_trading_ready(redis, market)


def set_enabled(redis: Any, enabled: bool) -> None:
    """写入 Redis 热开关。

    ``False`` 写显式 ``"0"`` 而不是删键：删键会回落到 env 基线
    ``SIMULATION_MIRROR_TO_REAL``，env 为 true 时「关闭」等于没关（fail-open）。
    """
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")
    client.set(_ENABLED_KEY, "1" if enabled else "0")


def enforce_disabled_on_startup(redis: Any) -> bool:
    """启动期自检：实盘闸门关闭时，把遗留的 ``mirror:enabled`` 热开关复位为 ``"0"``。

    返回是否发生过复位。**先 WARNING 再写**——不复位则每次进程重启都会带着一个
    「镜像已启用」的状态活在 Redis 里；静默复位则运维看不到它曾经存在过。

    只动这一个键：白名单/黑名单/限额计数是运维配置，不属于「实盘开关」的辖域，
    擅自清掉会让重新启用实盘时丢失白名单。
    """
    if is_real_trading_enabled():
        return False
    try:
        raw = _redis_get(redis, _ENABLED_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 启动自检读 %s 失败: %s", _ENABLED_KEY, exc)
        return False
    if not raw.strip() or not _one(raw):
        return False
    logger.warning(
        "[Mirror] 检测到遗留热开关 %s=%s，但 ENABLE_REAL_TRADING=false —— "
        "强制复位为 0（实盘关闭时镜像恒关，见 mirror_enabled）",
        _ENABLED_KEY,
        raw,
    )
    try:
        _redis_client(redis).set(_ENABLED_KEY, "0")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 复位 %s 失败: %s", _ENABLED_KEY, exc)
        return False
    return True


def read_config_overrides(redis: Any) -> dict[str, Any]:
    """当前 ``mirror:config`` 原始覆盖项。"""
    return _read_config_overrides(redis)


def write_config_overrides(redis: Any, updates: dict[str, Any]) -> dict[str, Any]:
    """合并写入 ``mirror:config``，返回合并后的完整覆盖项。"""
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")
    merged = {**read_config_overrides(redis), **updates}
    client.set(_CONFIG_KEY, json.dumps(merged, ensure_ascii=False))
    return merged


def set_lists(
    redis: Any,
    *,
    whitelist: list[str] | None = None,
    blacklist: list[str] | None = None,
) -> None:
    """整体替换白/黑名单（``None`` = 不动，``[]`` = 清空）。"""
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")

    def _replace(key: str, values: list[str]) -> None:
        client.delete(key)
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        if cleaned:
            client.sadd(key, *cleaned)

    if whitelist is not None:
        _replace(_WHITELIST_KEY, whitelist)
    if blacklist is not None:
        _replace(_BLACKLIST_KEY, blacklist)


def _decode_set(raw: Any) -> list[str]:
    return sorted(
        (
            e.decode("utf-8", errors="ignore")
            if isinstance(e, (bytes, bytearray))
            else str(e)
        )
        for e in (raw or set())
    )


def status_snapshot(redis: Any) -> dict[str, Any]:
    """控制面总览：开关、急停、名单、限额、当日用量、队列、阻塞原因。"""
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")
    cfg = load_config(redis)
    kill = kill_switch_on(redis)
    enabled = mirror_enabled(redis, cfg)
    ready, reason = _real_trading_ready(redis, "CN")
    selected = _redis_get(redis, "broker:selected:CN").strip().lower()
    today = trade_date_str()
    return {
        "enabled": enabled,
        "env_enabled": env_enabled(),
        "kill_switch": kill,
        "whitelist": _decode_set(client.smembers(_WHITELIST_KEY)),
        "blacklist": _decode_set(client.smembers(_BLACKLIST_KEY)),
        "config": {
            "max_order_value": cfg.max_order_value,
            "max_daily_value": cfg.max_daily_value,
            "max_daily_symbols": cfg.max_daily_symbols,
            "max_daily_orders": cfg.max_daily_orders,
            "max_slippage_pct": cfg.max_slippage_pct,
            "max_consecutive_rejects": cfg.max_consecutive_rejects,
            "queue_outside_hours": cfg.queue_outside_hours,
            "markets": sorted(cfg.markets),
        },
        "quota": {
            "date": today,
            "daily_value": _as_float(client.get(_daily_key("value", today))),
            "daily_orders": _as_int(client.get(_daily_key("orders", today))),
            "daily_symbols": _as_int(client.scard(_daily_key("symbols", today))),
        },
        "queue_length": _as_int(client.llen(_QUEUE_KEY)),
        "consecutive_rejects": _as_int(client.get(_REJECTS_KEY)),
        # 连续计数**成功一笔就清零**，间歇性失败在它上面读不出「今天出过事」——
        # 当日台账是那一格（按标的与原因，供值班决定补哪一笔）。
        "daily_failures": {
            "date": today,
            "count": _as_int(client.get(_daily_key("failed", today))),
            "ledger": load_failures(redis, today),
        },
        "trading_time": is_trading_time(),
        "broker_selected": selected,
        "real_trading_ready": ready,
        # ``blocked_reason`` 只回答「镜像为什么没开」；通道就绪是另一个问题，
        # 且**镜像开着也可能不就绪**（ENABLE_REAL_TRADING 未开 / 券商不是 qmt_exec）。
        # 分开报：推送确认面板要并列展示这两态，合成一个字段会让「就绪=false 但 reason 为空」
        # 变成一句没法行动的空话。``blocked_reason`` 语义保持不变（存量消费方在用）。
        "not_ready_reason": "" if ready else (reason or "unknown"),
        "blocked_reason": ""
        if enabled
        else ("kill_switch" if kill else reason or "disabled"),
    }


# --------------------------------------------------------------------------
# 账户/行情
# --------------------------------------------------------------------------
async def _account_snapshot(force: bool = False) -> dict[str, Any]:
    """QMT 账户（资金 + 持仓），10s 进程内缓存。"""
    now = asyncio.get_running_loop().time()
    cached = _account_cache.get("data")
    if (
        not force
        and cached is not None
        and now - float(_account_cache.get("at") or 0) < _ACCOUNT_CACHE_SECONDS
    ):
        return cached
    client = get_qmt_exec_client()
    asset = await client.get_asset()
    positions = await client.get_positions()
    available: dict[str, float] = {}
    for item in positions:
        # 桥返回的 symbol 是前缀式（SH600371），而限额闸门/Signal 链路一律用
        # 后缀式（600371.SH）查；不归一化会让卖出恒判「可用持仓 0」被跳过。
        symbol = StockCodeUtil.to_suffix(
            str(item.get("symbol") or item.get("stock_code") or "")
        )
        if symbol:
            available[symbol] = float(item.get("can_use_volume") or 0)
    data = {
        "cash": float(asset.get("cash") or 0),
        "total_asset": float(asset.get("total_asset") or 0),
        "available_volume": available,
    }
    _account_cache["data"] = data
    _account_cache["at"] = now
    return data


def _invalidate_account_cache() -> None:
    _account_cache["data"] = None
    _account_cache["at"] = 0.0


async def _reference_price(symbol: str) -> float:
    """独立参考价：QuantDB 最近收盘价。取不到返回 0（调用方跳过，fail-closed）。

    不能用虚拟成交价兜底——那样价格偏离闸门恒等于 0，停牌/除权/数据滞后
    这些最该拦住真单的场景会全部放行。
    """
    try:
        closes = await asyncio.to_thread(batch_quantdb_last_close, [symbol])
        price = float(closes.get(symbol) or 0)
        if price > 0:
            return price
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 参考价取数失败 symbol=%s: %s", symbol, exc)
    return 0.0


# --------------------------------------------------------------------------
# 限额
# --------------------------------------------------------------------------
def _reserve_quota(
    redis: Any, cfg: MirrorConfig, *, symbol: str, value: float
) -> tuple[bool, str, dict[str, Any]]:
    """原子预留当日额度。

    返回 ``(是否通过, 原因, 快照)``；快照含 ``daily_value`` / ``daily_orders`` /
    ``daily_symbols`` / ``new_symbol``（本笔是否当日新标的，回滚时用）与
    ``date``（**预留时**的日期键，回滚必须原样带回去，见 ``_release_quota``）。
    """
    client = _redis_client(redis)
    if client is None:
        return False, "redis_unavailable", {}
    # 三个键必须属于同一天：分别调 trade_date_str() 会在跨零点的瞬间把金额写进
    # 昨天、笔数写进今天（两套账，谁都拦不住）。
    date_str = trade_date_str()
    try:
        result = client.eval(
            _RESERVE_LUA,
            3,
            _daily_key("value", date_str),
            _daily_key("orders", date_str),
            _daily_key("symbols", date_str),
            value,
            symbol,
            cfg.max_order_value,
            cfg.max_daily_value,
            cfg.max_daily_orders,
            cfg.max_daily_symbols,
            _DAILY_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[Mirror] 限额预留失败，按拒绝处理: %s", exc)
        return False, "quota_error", {}
    snapshot = {
        "daily_value": _as_float(result[2]),
        "daily_orders": _as_float(result[3]),
        "daily_symbols": _as_float(result[4]),
        "new_symbol": float(_as_int(result[5])),
        "date": date_str,
    }
    return _as_int(result[0]) == 1, _as_text(result[1]), snapshot


def _release_quota(
    redis: Any,
    cfg: MirrorConfig,
    *,
    symbol: str,
    value: float,
    was_new_symbol: bool,
    date_str: str = "",
) -> None:
    """提交失败时回滚预留（尽力而为，失败只告警）。

    ``date_str`` 必须是**预留时**返回的日期键：回滚时重新取 ``trade_date_str()``
    会在跨零点时扣到新的一天（次日金额被减成负数 → 限额形同放开）。
    当日键已过期（TTL）时直接跳过，避免写出永不读取的负数垃圾键。
    """
    client = _redis_client(redis)
    if client is None:
        return
    day = date_str or trade_date_str()
    value_key = _daily_key("value", day)
    try:
        if not client.exists(value_key):
            logger.warning(
                "[Mirror] 额度键已过期，跳过回滚 symbol=%s date=%s", symbol, day
            )
            return
        client.incrbyfloat(value_key, -float(value))
        client.decr(_daily_key("orders", day))
        if was_new_symbol:
            client.srem(_daily_key("symbols", day), symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Mirror] 额度回滚失败 symbol=%s value=%.2f: %s", symbol, value, exc
        )


def _incr_daily(client: Any, field: str) -> int:
    """当日计数 +1；读不到返回 0（台账/告警缺一个数，不该打断真单路径）。"""
    try:
        key = _daily_key(field)
        value = int(client.incr(key))
        client.expire(key, _DAILY_TTL_SECONDS)
        return value
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Mirror] 当日计数失败 %s: %s", field, exc)
        return 0


def _alert_key(kind: str, date_str: str | None = None) -> str:
    return _ALERT_KEY_FMT.format(date=date_str or trade_date_str(), kind=kind)


def _release_mark(client: Any, mark_key: str) -> None:
    """放掉去重占位键：推送没送达时用它把「今天推过」撤回。"""
    if client is None:
        return
    try:
        client.delete(mark_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Mirror] 告警去重键释放失败 %s: %s", mark_key, exc)


def _deliver(
    client: Any, mark_key: str, *, title: str, content: str, level: str
) -> None:
    """旁路推送（**不 await**：真单路径不许等一次写库）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文（无事件循环，如脚本）：这条推不出去 ⇒ 立即放掉占位键，
        # 好让之后的失败真能推出来。日志要如实说「没送出去」，不许装成已推。
        logger.warning(
            "[Mirror] 下单失败告警未推送：当前没有事件循环（key=%s）", mark_key
        )
        _release_mark(client, mark_key)
        return
    asyncio.create_task(_deliver_then_verify(client, mark_key, title, content, level))


async def _deliver_then_verify(
    client: Any, mark_key: str, title: str, content: str, level: str
) -> None:
    try:
        delivered = await _publish(title=title, content=content, level=level)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 下单失败告警推送异常: %s", exc)
        delivered = False
    if delivered:
        return
    logger.warning(
        "[Mirror] 下单失败告警未送达，放掉去重键（下次失败再推）: %s", mark_key
    )
    _release_mark(client, mark_key)


def _push_failure_once(
    redis: Any, *, symbol: str, reason: str, failed_today: int
) -> bool:
    """当日**首笔**下单失败推一条（同类当天只此一条）。返回有没有推。

    去重键**先占位、推失败再放掉**：占位是同步的，能挡住同一秒里并发拒单各推一条；
    放掉则保证「没送达 ⇒ 不烧键」——重复推优于沉默（同止损失败/决策轮告警的纪律，
    差别只在那两条路能 await 送达，真单路径不能，所以拆成走/验两步）。
    """
    client = _redis_client(redis)
    key = _alert_key(ALERT_SUBMIT_FAILED)
    if client is not None:
        try:
            if client.get(key):
                return False
            client.set(key, "1", ex=_ALERT_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 读不到按没推过办（重复优于沉默）
            logger.warning("[Mirror] 失败告警去重键不可用: %s", exc)
    where = f"{symbol} " if symbol else ""
    _deliver(
        client,
        key,
        title="真单镜像有下单失败",
        content=(
            f"{where}{reason}\n"
            f"今日第 {failed_today or 1} 笔。本笔**不会自动重发**——请先核对 orders 里的"
            "实际委托与柜台状态，再决定是否补单。"
        ),
        level="error",
    )
    return True


def _record_reject(
    redis: Any,
    cfg: MirrorConfig,
    *,
    reason: str,
    symbol: str = "",
    side: str = "",
    quantity: float = 0.0,
    source: str = "",
) -> None:
    """一笔真单没发成：**当日台账 + 首笔告警 + 连续拒单熔断**（三件事各自独立）。

    台账先写、计数后写：计数键坏了不该让这一笔在系统里消失（此前那条 ``return`` 会
    让「计数失败」与「什么都没发生」长得一模一样）。
    """
    client = _redis_client(redis)
    if client is None:
        return
    record_failure(
        redis, symbol=symbol, side=side, quantity=quantity, reason=reason, source=source
    )
    failed_today = _incr_daily(client, "failed")
    try:
        count = int(client.incr(_REJECTS_KEY))
        client.expire(_REJECTS_KEY, _DAILY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 拒单计数失败: %s", exc)
        count = 0
    if cfg.max_consecutive_rejects > 0 and count >= cfg.max_consecutive_rejects:
        try:
            set_kill_switch(redis, True)
        except Exception as exc:  # noqa: BLE001
            logger.error("[Mirror] 熔断置位失败: %s", exc)
        logger.error(
            "[Mirror] 连续拒单 %d 次（>= %d），已自动急停 mirror:kill=1，原因=%s",
            count,
            cfg.max_consecutive_rejects,
            reason,
        )
        notify(
            title="真单镜像已自动急停",
            content=(
                f"连续 {count} 次下单失败（最近原因：{reason}），"
                f"已置 mirror:kill=1 停止镜像。请检查大 QMT 状态后在页面重新开启。"
            ),
            level="error",
        )
        return
    # 阈值之下：至少让今天的第一笔被看见。熔断那条已经说明了一切（含次数与最近原因），
    # 不再叠一条——两条同秒到达只会稀释「已停单」这条。
    _push_failure_once(redis, symbol=symbol, reason=reason, failed_today=failed_today)


def _record_success(redis: Any) -> None:
    """一笔成功 ⇒ 连续计数清零。**当日台账与当日失败计数不动**（那是已经发生的事）。"""
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.delete(_REJECTS_KEY)
    except Exception:  # noqa: BLE001
        pass


async def _publish(*, title: str, content: str, level: str) -> bool:
    """站内通知（落库 → 前端通知中心）。返回是否送达。"""
    from backend.shared.notification_publisher import publish_notification_async

    return bool(
        await publish_notification_async(
            user_id=resolve_db_account_user("MIRROR_NOTIFY_USER_ID"),
            tenant_id="default",
            title=title,
            content=content,
            type="trading",
            level=level,
        )
    )


def notify(*, title: str, content: str, level: str = "warning") -> None:
    """站内通知（尽力而为，不阻断交易路径）。旁路任务（账户同步等）也可复用。"""
    try:
        asyncio.create_task(_publish(title=title, content=content, level=level))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 通知推送失败: %s", exc)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def build_mirror_client_order_id(
    *,
    client_order_id: str = "",
    sim_order_id: str = "",
    run_id: str = "",
    symbol: str = "",
    side: str = "",
) -> str:
    """镜像单的 client_order_id（确定性 → 幂等；截断到 orders 列宽）。"""
    base = (
        str(client_order_id or "").strip()
        or str(sim_order_id or "").strip()
        or f"{run_id}-{symbol}-{side}".strip("-")
    )
    return f"mir-{base}"[:_MAX_CLIENT_ORDER_ID_LEN]


def _real_trading_ready(redis: Any, market: str) -> tuple[bool, str]:
    """实盘通道就绪：ENABLE_REAL_TRADING 且该市场选定 qmt_exec。"""
    # 走共享闸门读 env（**调用时读**，不是 import 时冻结），与中间件、
    # 与 `/real-trading/preflight`、与 `mirror_enabled` 的判定同一处。
    # 原来这里读 `trade_config.settings`，那份是 import 期快照——运维改了
    # env 重启后两份开关会在运行期分叉。
    if not is_real_trading_enabled():
        return False, "real_trading_disabled"
    selected = ""
    try:
        selected = _redis_get(redis, f"broker:selected:{market}").strip().lower()
    except Exception:  # noqa: BLE001
        selected = ""
    if not selected:
        try:
            from backend.services.trade_shared.trade_config import settings

            selected = str(getattr(settings, "REAL_BROKER_TYPE", "") or "").lower()
        except Exception:  # noqa: BLE001
            selected = ""
    if selected != "qmt_exec":
        return False, f"broker_not_qmt_exec:{selected or 'unset'}"
    return True, ""


def _infer_market(symbol: str) -> str:
    try:
        from backend.services.simulation.services.market_rules import infer_market

        return str(infer_market(symbol or "").value).upper()
    except Exception:  # noqa: BLE001
        return "CN"


async def mirror_virtual_fill(
    *,
    db: Any = None,
    redis: Any,
    tenant_id: str,
    user_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    client_order_id: str = "",
    sim_order_id: str = "",
    run_id: str = "",
    strategy_id: str = "",
    market: str = "",
    source: str = "",
    bypass_price_gate: bool = False,
    trigger: str = "",
    limit_price: float | None = None,
    agent: str = "",
) -> dict[str, Any]:
    """虚拟成交 → 真单镜像。**永不抛异常**，返回结构化决策结果。

    ``agent``：这条腿属于哪家模型（P2.7 分账）。随载荷一路带到 ``orders.agent``——
    成交回报回来时只认订单，届时才知道该记进哪本分账。非 LLM 腿留空。

    ``trigger``：成交通知里那句「这笔真单为什么会有」的原因是**可覆盖**的。默认句是
    「模拟盘成交触发真单镜像」，但实盘独有持仓直卖（``SOURCE_REAL_DIRECT``）没有模拟腿，
    照原样说出去就是假话 —— 真钱通知的第一句必须是真的。

    ``limit_price``：调用方（LLM 决策 / 交易台）**逐笔指定**的限价。``None`` 时按
    参考价 ± ``max_slippage_pct`` 派生（历史行为）。给了值就进队列载荷，由
    :func:`_submit_payload` 用 :func:`lot_rules.resolve_limit_price` 复核——
    预检放行的价在此**再核一遍**：队列里的载荷是跨进程数据，不能当可信输入。

    ``db`` 缺省时自建独立会话（调用方持有未提交事务时用，避免真单写入
    提前提交调用方的事务）。

    ``bypass_price_gate=True``：强平/止损类订单（一定要成交），跳过 2% 偏离闸门，
    限价改用**盘口价**（虚拟成交价）为基准，昨收只做 sanity 上界。普通策略信号
    不要用它——那正是该闸门要拦的场景。

    返回 ``status``：``skipped``（风控/未启用）、``queued``（非交易时段入队）、
    ``submitted``（已提交真单）、``duplicate``（该 client_order_id 已有真单，
    幂等跳过）、``failed``（提交失败）、``error``（内部异常）。
    """
    symbol = str(symbol or "").strip().upper()
    side = str(side or "").strip().upper()
    try:
        return await _mirror_virtual_fill(
            db=db,
            redis=redis,
            tenant_id=tenant_id,
            user_id=user_id,
            symbol=symbol,
            side=side,
            quantity=float(quantity or 0),
            price=float(price or 0),
            client_order_id=str(client_order_id or ""),
            sim_order_id=str(sim_order_id or ""),
            run_id=str(run_id or ""),
            strategy_id=str(strategy_id or ""),
            market=str(market or ""),
            source=str(source or ""),
            bypass_price_gate=bool(bypass_price_gate),
            trigger=str(trigger or ""),
            limit_price=None if limit_price is None else float(limit_price),
            agent=str(agent or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 镜像失败绝不影响虚拟账本
        logger.error(
            "[Mirror] 镜像异常 source=%s symbol=%s side=%s: %s",
            source,
            symbol,
            side,
            exc,
            exc_info=True,
        )
        return {"status": "error", "reason": str(exc), "symbol": symbol}


async def _mirror_virtual_fill(
    *,
    db: Any,
    redis: Any,
    tenant_id: str,
    user_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    client_order_id: str,
    sim_order_id: str,
    run_id: str,
    strategy_id: str,
    market: str,
    source: str,
    bypass_price_gate: bool = False,
    trigger: str = "",
    limit_price: float | None = None,
    agent: str = "",
) -> dict[str, Any]:
    def _skip(reason: str) -> dict[str, Any]:
        logger.info(
            "[Mirror] 跳过 source=%s %s %s qty=%s reason=%s",
            source,
            symbol,
            side,
            quantity,
            reason,
        )
        record_skip(
            redis,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reason=reason,
            source=source,
        )
        return {"status": "skipped", "reason": reason, "symbol": symbol}

    if side not in {"BUY", "SELL"}:
        return _skip("invalid_side")
    if quantity <= 0 or price <= 0:
        return _skip("invalid_quantity_or_price")
    if limit_price is not None and (not math.isfinite(limit_price) or limit_price <= 0):
        # 脏价不进队列：``NaN`` 经 json.dumps 会写成非标准的 ``NaN`` 字面量
        return _skip("invalid_limit_price")

    cfg = load_config(redis)
    if not mirror_enabled(redis, cfg):
        return _skip("mirror_disabled")
    market_key = str(market or "").upper() or _infer_market(symbol)
    if market_key not in cfg.markets:
        return _skip(f"market_not_supported:{market_key}")
    if not whitelist_allows(
        redis, tenant_id=tenant_id, user_id=user_id, strategy_id=strategy_id
    ):
        return _skip("whitelist")
    if _is_blacklisted(redis, symbol):
        return _skip("blacklist")

    ready, reason = _real_trading_ready(redis, market_key)
    if not ready:
        return _skip(reason)

    mirror_cid = build_mirror_client_order_id(
        client_order_id=client_order_id,
        sim_order_id=sim_order_id,
        run_id=run_id,
        symbol=symbol,
        side=side,
    )
    payload = {
        "client_order_id": mirror_cid,
        "tenant_id": str(tenant_id or "default"),
        "user_id": str(user_id or ""),
        "strategy_id": str(strategy_id or ""),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "market": market_key,
        "source": source,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    if str(agent or "").strip():
        # 只在有归属时入载荷（与 trigger/limit_price 同规矩）：空串入队会让
        # 「这条单没有归属」与「归属是空串」在队列里长得一样。
        payload["agent"] = str(agent).strip()
    if bypass_price_gate:
        payload["bypass_price_gate"] = True
    if trigger:
        # 只在被覆盖时入载荷：队列里的这一笔开盘后仍要说同一句真话
        payload["trigger"] = str(trigger)
    if limit_price is not None:
        # 只在给出时入载荷：``None`` 走「参考价 ± max_slippage_pct」派生（历史行为）
        payload["limit_price"] = float(limit_price)
    if not is_trading_time():
        if not cfg.queue_outside_hours:
            return _skip("outside_trading_hours")
        if _enqueue(redis, payload):
            logger.info(
                "[Mirror] 非交易时段入队 source=%s %s %s qty=%s cid=%s",
                source,
                symbol,
                side,
                quantity,
                mirror_cid,
            )
            return {"status": "queued", "reason": "outside_trading_hours", **payload}
        return _skip("queue_duplicate")

    if db is not None:
        return await _submit_payload(db=db, redis=redis, cfg=cfg, payload=payload)
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        return await _submit_payload(db=session, redis=redis, cfg=cfg, payload=payload)


def _enqueue(redis: Any, payload: dict[str, Any]) -> bool:
    """入队（同一 client_order_id 只入队一次）。"""
    client = _redis_client(redis)
    if client is None:
        return False
    cid = str(payload.get("client_order_id") or "")
    try:
        if cid and not client.sadd(_QUEUED_SET_KEY, cid):
            return False
        if cid:
            client.expire(_QUEUED_SET_KEY, _QUEUE_TTL_SECONDS)
        client.rpush(_QUEUE_KEY, json.dumps(payload, ensure_ascii=False))
        client.expire(_QUEUE_KEY, _QUEUE_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        # 半失败要回滚去重标记，否则该单永远卡在「已入队」而实际不在队列里
        if cid:
            try:
                client.srem(_QUEUED_SET_KEY, cid)
            except Exception:  # noqa: BLE001 - 回滚失败只能靠 TTL 过期
                pass
        logger.error("[Mirror] 入队失败 cid=%s: %s", cid, exc)
        return False


async def _submit_payload(
    *, db: Any, redis: Any, cfg: MirrorConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    """真单提交核心：价格/资金/持仓闸门 → 限额预留 → 复用 REAL 下单链路。"""
    symbol = str(payload.get("symbol") or "")
    side = str(payload.get("side") or "")
    quantity = float(payload.get("quantity") or 0)
    mirror_cid = str(payload.get("client_order_id") or "")
    tenant = str(payload.get("tenant_id") or "default")
    user_id = str(payload.get("user_id") or "")

    def _skip(reason: str) -> dict[str, Any]:
        logger.info(
            "[Mirror] 提交前跳过 %s %s qty=%s cid=%s reason=%s",
            symbol,
            side,
            quantity,
            mirror_cid,
            reason,
        )
        record_skip(
            redis,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reason=reason,
            source=str(payload.get("source") or "submit"),
        )
        return {"status": "skipped", "reason": reason, "symbol": symbol}

    ref_price = await _reference_price(symbol)
    if ref_price <= 0:
        return _skip("no_reference_price")
    live_price = float(payload.get("price") or 0)
    drift = abs(ref_price - live_price) / ref_price
    bypass = bool(payload.get("bypass_price_gate"))
    if bypass:
        # 强平/止损来源：偏离闸门豁免（急跌日止损恰恰是偏离最大的时刻），
        # 但仍做异常数据上界——偏离过大视为脏数据，fail-closed 不下单。
        if drift > _SANITY_MAX_DRIFT:
            logger.warning(
                "[Mirror] 强平单价格异常 symbol=%s 委托=%.3f 昨收=%.3f 偏离=%.2f%% > %.0f%%",
                symbol,
                live_price,
                ref_price,
                drift * 100,
                _SANITY_MAX_DRIFT * 100,
            )
            return _skip("price_sanity")
    elif cfg.max_slippage_pct > 0 and drift > cfg.max_slippage_pct:
        # 参考价与虚拟成交价偏离过大（除权/停牌/数据滞后）→ 不下真单
        logger.warning(
            "[Mirror] 价格偏离过大 symbol=%s 虚拟=%.3f 参考=%.3f 偏离=%.2f%%",
            symbol,
            live_price,
            ref_price,
            drift * 100,
        )
        return _skip("price_drift")

    # 限价基准：普通信号用昨收（虚拟成交价与实时盘口可能脱钩，见计划 §2.3 开放问题）；
    # 强平/止损单用盘口价（payload price = 当时盘口），否则跌停保护价会被昨收口径算歪。
    base_price = live_price if bypass else ref_price
    if base_price <= 0:
        return _skip("no_reference_price")
    # TCA 基准价（P1.6）= 决策腿当时的价 = ``live_price``（载荷里那句 price：模拟腿的
    # 虚拟成交价，或强平单的当时盘口）。**刻意不用上面那个昨收 ``ref_price``**：它是
    # 偏离闸门的锚，拿它当基准会把隔夜跳空算成"执行损耗"。``live_price`` 非正时留空，
    # 报告按"不可定价"计数——编一个价会让滑点变成自证式的假读数。
    tca_ref_price = live_price if live_price > 0 else None
    # 限价：调用方逐笔给了就用它的（单边带内才放行），没给就按参考价 ± 2% 派生。
    # 预检（push_orders）用同一个函数算过一遍，这里是**真金白银的最终边界**——
    # 队列里的载荷可能来自另一个进程/更早的版本，一律重核，不信任输入。
    limit_price, problem = lot_rules.resolve_limit_price(
        side,
        base_price,
        requested=payload.get("limit_price"),
        max_slip=cfg.max_slippage_pct,
    )
    if limit_price is None:
        logger.warning(
            "[Mirror] 限价被拒 %s %s cid=%s requested=%s base=%.3f 原因=%s",
            symbol,
            side,
            mirror_cid,
            payload.get("limit_price"),
            base_price,
            problem,
        )
        return _skip(
            "invalid_limit_price" if problem == "limit_price_not_positive" else problem
        )
    order_value = round(limit_price * quantity, 2)
    if order_value <= 0:
        return _skip("invalid_order_value")

    # 资金 / 持仓闸门（真实账户口径）
    try:
        account = await _account_snapshot()
    except QmtExecError as exc:
        logger.warning("[Mirror] 账户查询失败 code=%s: %s", exc.code, exc)
        return _skip(f"account_unavailable:{exc.code}")
    if side == "BUY":
        need = order_value * 1.002  # 预留手续费
        if float(account.get("cash") or 0) < need:
            logger.warning(
                "[Mirror] 可用资金不足 symbol=%s 需要=%.2f 可用=%.2f",
                symbol,
                need,
                float(account.get("cash") or 0),
            )
            return _skip("insufficient_cash")
    else:
        # 账户快照键为后缀式（QuantDB 口径），payload 的 symbol 可能带市场后缀差异，
        # 查前统一归一化，避免口径不符导致的假「持仓不足」。
        available = float(
            (account.get("available_volume") or {}).get(StockCodeUtil.to_suffix(symbol))
            or 0
        )
        if available < quantity:
            logger.warning(
                "[Mirror] 可用持仓不足 symbol=%s 需要=%s 可用=%s",
                symbol,
                quantity,
                available,
            )
            return _skip("insufficient_position")

    was_new_symbol = False
    try:
        ok, reason, snapshot = _reserve_quota(
            redis, cfg, symbol=symbol, value=order_value
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[Mirror] 限额校验异常，按拒绝处理: %s", exc)
        return _skip("quota_error")
    if not ok:
        logger.warning(
            "[Mirror] 触发限额 %s symbol=%s 本笔=%.2f 当日金额=%.2f 笔数=%.0f 标的=%.0f",
            reason,
            symbol,
            order_value,
            snapshot.get("daily_value", 0),
            snapshot.get("daily_orders", 0),
            snapshot.get("daily_symbols", 0),
        )
        return {"status": "skipped", "reason": reason, "symbol": symbol, **snapshot}
    was_new_symbol = _as_int(snapshot.get("new_symbol")) == 1
    quota_date = str(snapshot.get("date") or "")

    # 下发前复检（急停/开关/名单/通道）：首检到此处已过参考价 + 账户快照两次 RPC，
    # 期间运维的任何停单动作都必须拦住这一笔（额度先回滚再返回）。
    blocked = _dispatch_gate_blocked(redis, cfg, payload)
    if blocked:
        _release_quota(
            redis,
            cfg,
            symbol=symbol,
            value=order_value,
            was_new_symbol=was_new_symbol,
            date_str=quota_date,
        )
        logger.warning(
            "[Mirror] 下发前复检未通过，已撤销本笔 cid=%s symbol=%s reason=%s",
            mirror_cid,
            symbol,
            blocked,
        )
        return {"status": "skipped", "reason": blocked, "symbol": symbol, **snapshot}

    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )

    try:
        result = await dispatch_internal_strategy_order(
            order_data={
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": limit_price,
                "order_type": "LIMIT",
                "trading_mode": "REAL",
                "strategy_id": payload.get("strategy_id") or None,
                "client_order_id": mirror_cid,
                "remarks": f"mirror:{payload.get('source') or 'sim'}",
                # P2.7：归属随真单落 orders.agent（队列载荷是跨进程数据，缺了就空）
                "agent": payload.get("agent") or None,
                # P1.6 TCA 基准价 = **决策腿当时看到的价**（载荷 price，即模拟虚拟
                # 成交价 / 强平盘口价），不是上面那个昨收 `ref_price`——那个是偏离闸门
                # 的锚，用它当基准会把隔夜跳空算进滑点里，读出"执行很差"的假象。
                "ref_price": tca_ref_price,
            },
            user_id=user_id,
            tenant_id=tenant,
            redis=redis,
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 - HTTPException 等
        _release_quota(
            redis,
            cfg,
            symbol=symbol,
            value=order_value,
            was_new_symbol=was_new_symbol,
            date_str=quota_date,
        )
        _record_reject(
            redis,
            cfg,
            reason=str(exc),
            symbol=symbol,
            side=side,
            quantity=quantity,
            source=str(payload.get("source") or ""),
        )
        logger.error(
            "[Mirror] 真单提交失败 cid=%s symbol=%s: %s", mirror_cid, symbol, exc
        )
        return {"status": "failed", "reason": str(exc), "symbol": symbol}

    status = str(result.get("status") or "")
    execution = str(result.get("execution") or "")
    detail = result.get("result") if isinstance(result.get("result"), dict) else {}
    # 复用 REAL 尾段时「券商拒单」不抛异常：TradingEngine.submit_order 仍返回
    # success=True，只是把订单置为 REJECTED。只看 status 会把拒单当成功
    # （扣额度、清熔断计数、推「真单已提交」），必须同时看执行结果。
    broker_status = str(detail.get("status") or "").upper()
    if execution == "duplicate_skipped":
        # 幂等命中：该 client_order_id 早已下单，本笔额度退回，不计拒单。
        _release_quota(
            redis,
            cfg,
            symbol=symbol,
            value=order_value,
            was_new_symbol=was_new_symbol,
            date_str=quota_date,
        )
        logger.info(
            "[Mirror] 真单已存在（幂等跳过）cid=%s symbol=%s 真实订单=%s",
            mirror_cid,
            symbol,
            result.get("order_id"),
        )
        return {
            "status": "duplicate",
            "reason": "duplicate_skipped",
            "symbol": symbol,
            "client_order_id": mirror_cid,
            "order_id": result.get("order_id"),
            "detail": result,
        }
    failure = ""
    if status != "success":
        failure = status or "unknown"
    elif detail.get("success") is False:
        failure = str(detail.get("message") or "submit_failed")
    elif broker_status in {"REJECTED", "EXPIRED"}:
        failure = f"broker_{broker_status.lower()}:{detail.get('message') or ''}"
    if failure:
        _release_quota(
            redis,
            cfg,
            symbol=symbol,
            value=order_value,
            was_new_symbol=was_new_symbol,
            date_str=quota_date,
        )
        _record_reject(
            redis,
            cfg,
            reason=failure,
            symbol=symbol,
            side=side,
            quantity=quantity,
            source=str(payload.get("source") or ""),
        )
        logger.warning(
            "[Mirror] 真单未成功 cid=%s symbol=%s status=%s execution=%s detail=%s",
            mirror_cid,
            symbol,
            status,
            execution,
            result.get("result") or result.get("violations"),
        )
        return {
            "status": "failed",
            "reason": failure,
            "symbol": symbol,
            "detail": result,
        }

    _record_success(redis)
    _invalidate_account_cache()
    logger.info(
        "[Mirror] 真单已提交 cid=%s 真实订单=%s %s %s qty=%s 限价=%.2f 金额=%.2f",
        mirror_cid,
        result.get("order_id"),
        symbol,
        side,
        quantity,
        limit_price,
        order_value,
    )
    notify(
        title=f"真单已提交：{side} {symbol}",
        content=(
            f"{str(payload.get('trigger') or '模拟盘成交触发真单镜像').rstrip('。')}。\n"
            f"标的：{symbol}\n方向：{side}\n"
            f"数量：{quantity}\n限价：{limit_price:.2f}\n"
            f"金额：{order_value:.2f}\n镜像单号：{mirror_cid}"
        ),
        level="info",
    )
    return {
        "status": "submitted",
        "symbol": symbol,
        "client_order_id": mirror_cid,
        "order_id": result.get("order_id"),
        "limit_price": limit_price,
        "order_value": order_value,
        "detail": result,
    }


# --------------------------------------------------------------------------
# 队列排空（非交易时段入队的镜像单，开盘后补交）
# --------------------------------------------------------------------------
async def drain_mirror_queue(
    redis: Any, *, db: Any = None, limit: int = 20
) -> dict[str, Any]:
    """交易时段内把队列里的镜像单逐笔提交。"""
    if not is_trading_time():
        return {"status": "outside_trading_hours", "drained": 0}
    client = _redis_client(redis)
    if client is None:
        return {"status": "redis_unavailable", "drained": 0}
    cfg = load_config(redis)
    if not mirror_enabled(redis, cfg):
        return {"status": "disabled", "drained": 0}
    if db is not None:
        return await _drain_with_session(redis, client, cfg, db, limit)
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        return await _drain_with_session(redis, client, cfg, session, limit)


def _queued_entry_blocked(
    redis: Any, cfg: MirrorConfig, payload: dict[str, Any]
) -> str:
    """补交前的复核（市场/白/黑名单 + 通道就绪）。返回空串=允许，否则阻塞原因。

    入队时过了闸门不代表补交时还成立：市场开关、名单、券商选择、实盘开关都可能在
    隔夜被改，必须在真正下单前重新判一遍（市场判定与首检 ``_mirror_virtual_fill``
    同口径，否则把 CN 从 mirror:config.markets 移除后，隔夜队列里的 CN 单仍会补交）。
    """
    symbol = str(payload.get("symbol") or "")
    market_key = str(payload.get("market") or "").upper() or _infer_market(symbol)
    if market_key not in cfg.markets:
        return f"market_not_supported:{market_key}"
    if not whitelist_allows(
        redis,
        tenant_id=str(payload.get("tenant_id") or ""),
        user_id=str(payload.get("user_id") or ""),
        strategy_id=str(payload.get("strategy_id") or ""),
    ):
        return "whitelist"
    if _is_blacklisted(redis, symbol):
        return "blacklist"
    ready, reason = _real_trading_ready(redis, market_key)
    return "" if ready else reason


def _dispatch_gate_blocked(
    redis: Any, cfg: MirrorConfig, payload: dict[str, Any]
) -> str:
    """**真正调用 RPC 之前**的最后一道复检：急停 → 开关 → 市场/名单 → 通道就绪。

    首检（``_mirror_virtual_fill``）到实际下发之间隔着参考价查询、账户快照
    （RPC 各 10s 超时）与额度预留，期间运维置急停/关开关/改名单/切券商都必须
    拦住；否则「急停立即生效」不成立。返回空串=放行。
    """
    if kill_switch_on(redis):
        return "kill_switch"
    if not mirror_enabled(redis, cfg):
        return "mirror_disabled"
    return _queued_entry_blocked(redis, cfg, payload)


async def _drain_with_session(
    redis: Any, client: Any, cfg: MirrorConfig, db: Any, limit: int
) -> dict[str, Any]:
    submitted = 0
    failed = 0
    requeued = 0
    dropped = 0
    for _ in range(max(1, int(limit))):
        raw = client.lpop(_QUEUE_KEY)
        if not raw:
            break
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[Mirror] 队列条目非法，丢弃: %r", raw)
            continue
        if not isinstance(payload, dict):
            continue
        if not is_trading_time():
            # 排空跨过收盘/午休边界：本条放回队列，本轮到此外止
            client.rpush(_QUEUE_KEY, raw)
            requeued += 1
            break
        blocked = _queued_entry_blocked(redis, cfg, payload)
        if blocked:
            dropped += 1
            logger.warning(
                "[Mirror] 队列条目复核未通过，丢弃 cid=%s symbol=%s reason=%s",
                payload.get("client_order_id"),
                payload.get("symbol"),
                blocked,
            )
            continue
        try:
            result = await _submit_payload(db=db, redis=redis, cfg=cfg, payload=payload)
        except Exception as exc:  # noqa: BLE001 - 已出队，异常必须回队
            # 条目已 lpop 出队：这里抛出去只会被常驻任务的兜底 except 记一条日志，
            # 队列单静默消失（额度可能已预留）。放回队尾 + 本轮到此为止。
            logger.error(
                "[Mirror] 队列条目提交异常，放回队列 cid=%s symbol=%s: %s",
                payload.get("client_order_id"),
                payload.get("symbol"),
                exc,
                exc_info=True,
            )
            client.rpush(_QUEUE_KEY, raw)
            requeued += 1
            break
        status = str(result.get("status") or "")
        if status == "submitted":
            submitted += 1
        elif status == "queued":
            # 又落到非交易时段（午休边界）：放回队列下次再试
            client.rpush(_QUEUE_KEY, raw)
            requeued += 1
            break
        elif status in {"failed", "error"}:
            failed += 1
        elif status == "skipped":
            # 下发前被闸门拦住（急停/开关/名单/通道/价格/资金）→ 条目作废，
            # 但要计入 dropped，否则日志里看不出来单子去哪了。
            dropped += 1
        logger.info(
            "[Mirror] 队列补交 cid=%s status=%s reason=%s",
            payload.get("client_order_id"),
            status,
            result.get("reason"),
        )
    return {
        "status": "ok",
        "submitted": submitted,
        "failed": failed,
        "requeued": requeued,
        "dropped": dropped,
    }


async def run_mirror_queue_drainer(interval_seconds: float = 30.0) -> None:
    """常驻任务：交易时段排空镜像队列（trade 服务 lifespan 注册）。"""
    from backend.services.trade_shared.redis_client import RedisClient

    interval = max(5.0, float(interval_seconds))
    redis = RedisClient()
    redis.connect()
    logger.info("[Mirror] 镜像队列排空任务启动 interval=%.0fs", interval)
    while True:
        try:
            if is_trading_time():
                result = await drain_mirror_queue(redis)
                if result.get("submitted") or result.get("failed"):
                    logger.info("[Mirror] 队列排空结果 %s", result)
        except asyncio.CancelledError:
            logger.info("[Mirror] 队列排空任务退出")
            raise
        except Exception as exc:  # noqa: BLE001 - 常驻任务不能退出
            logger.warning("[Mirror] 队列排空失败: %s", exc, exc_info=True)
        await asyncio.sleep(interval)
