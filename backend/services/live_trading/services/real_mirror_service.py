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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

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
    """总开关：急停 > Redis 热开关 > env。"""
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


def whitelist_allows(
    redis: Any, *, tenant_id: str, user_id: str, strategy_id: str = ""
) -> bool:
    """白名单匹配：``*`` / tenant / tenant:user / tenant:user:strategy。空集合=全否。"""
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
        (e.decode() if isinstance(e, (bytes, bytearray)) else str(e)).strip()
        for e in entries
    }
    tenant = str(tenant_id or "default").strip() or "default"
    user = str(user_id or "").strip()
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
    """写入 Redis 热开关。"""
    client = _redis_client(redis)
    if client is None:
        raise RuntimeError("Redis 不可用")
    if enabled:
        client.set(_ENABLED_KEY, "1")
    else:
        client.delete(_ENABLED_KEY)


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
        "trading_time": is_trading_time(),
        "broker_selected": selected,
        "real_trading_ready": ready,
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
        symbol = str(item.get("symbol") or "").upper()
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


async def _reference_price(symbol: str, fallback: float) -> float:
    """参考价：QuantDB 最近收盘价优先，取不到用虚拟成交价。"""
    try:
        closes = await asyncio.to_thread(batch_quantdb_last_close, [symbol])
        price = float(closes.get(symbol) or 0)
        if price > 0:
            return price
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 参考价取数失败 symbol=%s: %s", symbol, exc)
    return float(fallback or 0)


# --------------------------------------------------------------------------
# 限额
# --------------------------------------------------------------------------
def _reserve_quota(
    redis: Any, cfg: MirrorConfig, *, symbol: str, value: float
) -> tuple[bool, str, dict[str, float]]:
    """原子预留当日额度。

    返回 ``(是否通过, 原因, 快照)``；快照含 ``daily_value`` / ``daily_orders`` /
    ``daily_symbols`` / ``new_symbol``（本笔是否当日新标的，回滚时用）。
    """
    client = _redis_client(redis)
    if client is None:
        return False, "redis_unavailable", {}
    try:
        result = client.eval(
            _RESERVE_LUA,
            3,
            _daily_key("value"),
            _daily_key("orders"),
            _daily_key("symbols"),
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
    }
    return _as_int(result[0]) == 1, _as_text(result[1]), snapshot


def _release_quota(
    redis: Any, cfg: MirrorConfig, *, symbol: str, value: float, was_new_symbol: bool
) -> None:
    """提交失败时回滚预留（尽力而为，失败只告警）。"""
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.incrbyfloat(_daily_key("value"), -float(value))
        client.decr(_daily_key("orders"))
        if was_new_symbol:
            client.srem(_daily_key("symbols"), symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Mirror] 额度回滚失败 symbol=%s value=%.2f: %s", symbol, value, exc
        )


def _record_reject(redis: Any, cfg: MirrorConfig, *, reason: str) -> None:
    """连续拒单熔断：达到阈值自动急停 + 通知。"""
    client = _redis_client(redis)
    if client is None:
        return
    try:
        count = int(client.incr(_REJECTS_KEY))
        client.expire(_REJECTS_KEY, _DAILY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 拒单计数失败: %s", exc)
        return
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


def _record_success(redis: Any) -> None:
    client = _redis_client(redis)
    if client is None:
        return
    try:
        client.delete(_REJECTS_KEY)
    except Exception:  # noqa: BLE001
        pass


def notify(*, title: str, content: str, level: str = "warning") -> None:
    """站内通知（尽力而为，不阻断交易路径）。旁路任务（账户同步等）也可复用。"""
    try:
        from backend.shared.notification_publisher import publish_notification_async

        asyncio.create_task(
            publish_notification_async(
                user_id=os.getenv("MIRROR_NOTIFY_USER_ID", "00000001"),
                tenant_id="default",
                title=title,
                content=content,
                type="trading",
                level=level,
            )
        )
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
    try:
        from backend.services.trade_shared.trade_config import settings

        if not getattr(settings, "ENABLE_REAL_TRADING", False):
            return False, "real_trading_disabled"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Mirror] 读取实盘开关失败，按未就绪处理: %s", exc)
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
) -> dict[str, Any]:
    """虚拟成交 → 真单镜像。**永不抛异常**，返回结构化决策结果。

    ``db`` 缺省时自建独立会话（调用方持有未提交事务时用，避免真单写入
    提前提交调用方的事务）。

    返回 ``status``：``skipped``（风控/未启用）、``queued``（非交易时段入队）、
    ``submitted``（已提交真单）、``failed``（提交失败）、``error``（内部异常）。
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
        return {"status": "skipped", "reason": reason, "symbol": symbol}

    if side not in {"BUY", "SELL"}:
        return _skip("invalid_side")
    if quantity <= 0 or price <= 0:
        return _skip("invalid_quantity_or_price")

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
    from backend.shared.database_manager_v2 import get_db_manager

    async with get_db_manager().session() as session:
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
        return {"status": "skipped", "reason": reason, "symbol": symbol}

    ref_price = await _reference_price(symbol, float(payload.get("price") or 0))
    if ref_price <= 0:
        return _skip("no_reference_price")
    drift = abs(ref_price - float(payload.get("price") or 0)) / ref_price
    if cfg.max_slippage_pct > 0 and drift > cfg.max_slippage_pct:
        # 参考价与虚拟成交价偏离过大（除权/停牌/数据滞后）→ 不下真单
        logger.warning(
            "[Mirror] 价格偏离过大 symbol=%s 虚拟=%.3f 参考=%.3f 偏离=%.2f%%",
            symbol,
            float(payload.get("price") or 0),
            ref_price,
            drift * 100,
        )
        return _skip("price_drift")

    if side == "BUY":
        limit_price = round(ref_price * (1 + cfg.max_slippage_pct), 2)
    else:
        limit_price = round(ref_price * (1 - cfg.max_slippage_pct), 2)
    if limit_price <= 0:
        return _skip("invalid_limit_price")
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
        available = float((account.get("available_volume") or {}).get(symbol) or 0)
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
            },
            user_id=user_id,
            tenant_id=tenant,
            redis=redis,
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 - HTTPException 等
        _release_quota(
            redis, cfg, symbol=symbol, value=order_value, was_new_symbol=was_new_symbol
        )
        _record_reject(redis, cfg, reason=str(exc))
        logger.error(
            "[Mirror] 真单提交失败 cid=%s symbol=%s: %s", mirror_cid, symbol, exc
        )
        return {"status": "failed", "reason": str(exc), "symbol": symbol}

    status = str(result.get("status") or "")
    if status not in {"success"}:
        _release_quota(
            redis, cfg, symbol=symbol, value=order_value, was_new_symbol=was_new_symbol
        )
        reason = status or "unknown"
        _record_reject(redis, cfg, reason=reason)
        logger.warning(
            "[Mirror] 真单未成功 cid=%s symbol=%s status=%s detail=%s",
            mirror_cid,
            symbol,
            status,
            result.get("result") or result.get("violations"),
        )
        return {
            "status": "failed",
            "reason": reason,
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
            f"模拟盘成交触发真单镜像。\n标的：{symbol}\n方向：{side}\n"
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


async def _drain_with_session(
    redis: Any, client: Any, cfg: MirrorConfig, db: Any, limit: int
) -> dict[str, Any]:
    submitted = 0
    failed = 0
    requeued = 0
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
        result = await _submit_payload(db=db, redis=redis, cfg=cfg, payload=payload)
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
