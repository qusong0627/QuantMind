"""实盘持仓快照（PG ``real_account_snapshots``）读取口径（**唯一实现**）。

为什么单列共享模块：自选池统一视图（api 服务）显示「我实盘有的票」、持仓哨兵
（trade 服务）判定「该不该提醒/能不能一键卖」，两处必须看到**同一个持仓集合**。
各写一份 SQL 的后果不是报错，而是「列表里没有的票收到卖出提醒」或反过来
「实盘 8 只票在自选里显示 0 只」——两边都看起来正常，没人能一眼看出错。

口径（实测，2026-09-20）：

1. **多券商账户取并集**，不是「取最新一条」：同一 (tenant, user) 下 ``qmt_exec``
   与 ``tdx_bridge`` 是两个互不相交的真实账户（实测 50 只 / 8 只），两条流每 30s
   交错写库，``ORDER BY snapshot_at DESC LIMIT 1`` 等于掷硬币。
2. **停更源不并入**：某源最新快照落后「全源最新」超过 ``_REAL_SOURCE_STALE_MINUTES``
   视为该源已停更——陈旧源会把早已卖出的持仓一直留在表上（假持仓比漏持仓更危险，
   它会让哨兵对着空仓发卖出提醒）。停更事实在 ``meta.sources[src].stale`` 如实报出。
3. **同票两源都有**时取活跃券商（``broker:selected:CN`` → ``REAL_BROKER_TYPE``）的量，
   并把两个出处都记进 ``sources``（Phase 4 一键卖出按出处路由券商）。选定的键在
   **交易库（db2）**、用与写入方同一个 ``RedisClient`` 读（见
   :func:`active_broker_type`——读错库/读错客户端会让运维在页面上切了券商而这里
   毫无察觉，历史实现正是如此）。
4. ``payload_json`` 实测是**双层编码**（JSON 字符串套 JSON），两种形态都容忍；
   解不开就在日志里说清楚，不静默当空仓。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from backend.shared.logging_config import get_logger
from backend.shared.signal_scores import normalize_position_symbol

logger = get_logger(__name__)

#: 相对新鲜度窗口（分钟）：某源落后全源最新超过此值 → 判停更、不并入。
_REAL_SOURCE_STALE_MINUTES = 60

#: 「券商接入」页选定券商的键。**在交易库（db2）**——写入方是 trade 服务的
#: ``broker_config.py``（``PUT /broker-config/selected/{market}``，走
#: ``trade_shared`` 的 RedisClient，db=``REDIS_DB_TRADE``）。
_SELECTED_BROKER_KEY = "broker:selected:CN"

_SNAPSHOT_SQL = (
    "SELECT DISTINCT ON (source) source, snapshot_at, payload_json "
    "FROM real_account_snapshots "
    "WHERE tenant_id = :tid AND user_id = :uid "
    "ORDER BY source, snapshot_at DESC"
)

#: 交易库连接：惰性单例 + 失败冷却。本函数在订单与盯盘热路径上，Redis 不可达时
#: 每次调用都重连一遍要各等一个 connect 超时（默认 5s）——那是把「读不到券商」
#: 升级成「下单路径卡死」。
_BROKER_REDIS_RETRY_S = 30.0
_broker_redis: Any = None
_broker_redis_last_try = 0.0
_broker_redis_lock = threading.Lock()

#: 「回退到 env 默认」告警的冷却（秒）：回退是热路径事件（每单/每轮盯盘一次），
#: 但它必须留痕——见 :func:`_warn_fallback_once`。
_FALLBACK_WARN_S = 30.0
_fallback_warned_at = 0.0


def _broker_redis_client() -> Any:
    """交易库的 ``redis.Redis``（连不上 → ``None``，冷却期内不再重试）。

    用 ``trade_shared.redis_client.RedisClient``：它与**写入方**是同一实现——同库
    （``REDIS_DB_TRADE``）、同哨兵/单机口径。读侧另换一个客户端就等于换个库：键在
    db2 有值、读的人在 db0 看不到，而后果见 :func:`active_broker_type` 的注释。
    """
    global _broker_redis, _broker_redis_last_try
    with _broker_redis_lock:
        client = getattr(_broker_redis, "client", None)
        if client is not None:
            return client
        now = time.monotonic()
        if (
            _broker_redis_last_try
            and now - _broker_redis_last_try < _BROKER_REDIS_RETRY_S
        ):
            return None
        _broker_redis_last_try = now
        from backend.services.trade_shared.redis_client import RedisClient

        rc = RedisClient()
        rc.connect()  # 失败自己记 error 日志并把 .client 置 None
        _broker_redis = rc
        return getattr(rc, "client", None)


def _read_selected_broker() -> str | None:
    """``broker:selected:CN`` 的原始值；``None`` = **键不存在**。读不到就抛。"""
    client = _broker_redis_client()
    if client is None:
        raise RuntimeError("交易库 Redis 不可用（RedisClient 未连上）")
    raw = client.get(_SELECTED_BROKER_KEY)
    if not raw:
        return None
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


def _warn_fallback_once(reason: str, broker: str) -> None:
    """读失败→回退 env 默认：**必须留痕**，但不许刷屏（热路径，冷却 30s）。"""
    global _fallback_warned_at
    now = time.monotonic()
    if _fallback_warned_at and now - _fallback_warned_at < _FALLBACK_WARN_S:
        return
    _fallback_warned_at = now
    logger.warning(
        "读不到 broker:selected:CN（%s）：回退 REAL_BROKER_TYPE=%r。"
        "若「券商接入」页选的是另一家，这里看到的持仓/风控就是另一座账户",
        reason,
        broker,
    )


class BrokerSelectionUnreadable(RuntimeError):
    """``broker:selected:CN`` 读不到——**与「没设置」是两回事**。

    读失败的语义是「不知道有没有人选过、选的是谁」；而回退 ``REAL_BROKER_TYPE`` 的
    语义是「没人显式选过，用部署默认」。同一 ``(tenant, user)`` 下 ``tdx_bridge`` 与
    ``qmt_exec`` 是两座互不相交的真实账户（实测规模差 ~25 倍），把后者当前者用等于
    掷硬币——所以「读不到又非要知道」的调用点（决策轮取资金面）必须**不做**，
    由它自己 abort 本轮（见 ``decision_round_io.load_account_numbers``）。
    """


def active_broker_type(*, strict: bool = False) -> str | None:
    """当前实盘券商：交易库 ``broker:selected:CN``（券商接入页选定）→ settings 回退。

    **读的是交易库（db2）**，与写入方 ``broker_config`` 同库同客户端。这里曾经用共享
    哨兵客户端（db0）读、且读的是它并不存在的 ``.client`` 属性 ⇒ 每次都以
    ``AttributeError`` 收场、被 ``except`` 吞成一行 debug ⇒ **永远回退
    ``REAL_BROKER_TYPE``**：运维在券商接入页切了券商，本函数照旧返回旧的那家（订单
    路由、镜像服务读的是选定值，于是出现「下单走 A 账户、持仓与风控看 B 账户」），
    且日志里没有任何一级信号。``test_real_positions`` 里有一条源码守卫钉着这条路。

    ``strict=True``：读失败**不回退**，抛 :class:`BrokerSelectionUnreadable`。键
    **不存在**不是失败——那是「没人显式选过」，settings 兜底正是设计语义。
    """
    reason = ""
    try:
        selected = _read_selected_broker()
    except Exception as exc:  # noqa: BLE001 - 容忍方（列表/并集兜底）不该被读失败打断
        reason = f"{type(exc).__name__}: {exc}"
        selected = None
        if strict:
            raise BrokerSelectionUnreadable(reason) from exc
    if selected:
        return selected
    try:
        from backend.services.trade_shared.trade_config import settings

        fallback = str(getattr(settings, "REAL_BROKER_TYPE", "") or "")
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise BrokerSelectionUnreadable(
                f"settings.REAL_BROKER_TYPE 也不可读：{type(exc).__name__}: {exc}"
            ) from exc
        return None
    if reason:
        _warn_fallback_once(reason, fallback)
    return fallback


def selected_broker_is_explicit() -> bool:
    """``broker:selected:CN`` 是否**被显式设置**（区别于 ``REAL_BROKER_TYPE`` 兜底）。

    页面「当前交易券商」必须说清是「你选的」还是「环境变量兜底」——两者行为不同，
    后者随时可能随部署变化，用户以为是自己选的就错了。读不到 = 未显式设置，
    不阻断页面（容忍口径与 :func:`active_broker_type` 的默认一致）。
    """
    try:
        return bool(_read_selected_broker())
    except Exception as exc:  # noqa: BLE001 - 读不到 = 未显式设置，不阻断页面
        logger.debug("读取 broker:selected:CN 失败: %s", exc)
        return False


def snapshot_source_for_broker(broker_type: str | None) -> str | None:
    """券商类型 → 快照 source（与 risk_gate_service 同一映射）。"""
    b = str(broker_type or "").lower()
    if b.startswith("tdx"):
        return "tdx_bridge"
    if b.startswith("qmt"):
        return "qmt_exec"
    return None


#: 快照 source → CN 券商键（``PUT /broker-config/selected/CN`` 的取值口径）。
#: 「按源看」的视图源要能反查回可选券商，否则前端只能自己猜映射、猜错就切错券商。
#: 与 ``broker_config.MARKET_BROKERS["CN"]`` 的一致性由测试锁定（反查表口径同源）。
_CN_SOURCE_TO_BROKER: dict[str, str] = {
    "qmt_exec": "qmt_exec",
    "tdx_bridge": "tdx",
}


def broker_for_snapshot_source(source: str | None) -> str | None:
    """快照 source → CN 券商键；非 CN 实盘源（手工录入等）返回 None（=不可选为交易券商）。"""
    return _CN_SOURCE_TO_BROKER.get(str(source or "").strip().lower())


def merge_real_sources(
    rows: list[tuple[str, object, object]],
    *,
    active_source: str | None = None,
) -> tuple[dict[str, dict], dict]:
    """快照行 → ``(prefix 键持仓表, meta)``（纯函数，便于盯口径）。

    ``rows`` = ``[(source, snapshot_at, payload_json)]``，每源一行（各源最新一条）。
    """
    if not rows:
        return {}, {"sources": {}, "snapshot_at": None, "active_broker": active_source}

    def _ts(v: object) -> str | None:
        return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)

    # 相对新鲜度：以全源最新快照为基准（免疫容器/DB 时钟偏移）
    newest = max((r[1] for r in rows if r[1] is not None), default=None)
    active_src = snapshot_source_for_broker(active_source)

    out: dict[str, dict] = {}
    src_meta: dict[str, object] = {}
    for source, snap_at, payload in rows:
        age_min = None
        if newest is not None and snap_at is not None:
            age_min = round((newest - snap_at).total_seconds() / 60.0, 1)
        stale = age_min is not None and age_min > _REAL_SOURCE_STALE_MINUTES
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                logger.warning(
                    "real snapshot payload_json 二次解码失败（source=%s）", source
                )
                payload = {}
        items = [
            p for p in (payload or {}).get("positions") or [] if isinstance(p, dict)
        ]
        src_meta[str(source)] = {
            "snapshot_at": _ts(snap_at),
            "positions": len(items),
            "stale": stale,
            "lag_min": age_min,
            "active_broker": str(source) == active_src,
        }
        if stale:
            continue
        for item in items:
            sym = normalize_position_symbol(item.get("symbol"))
            if not sym:
                continue
            row = dict(item)
            row["source"] = str(source)
            prev = out.get(sym)
            # 同一只票出现在两个账户：优先活跃券商，否则取量大者（并记 sources）
            if prev is not None:
                prev_src = str(prev.get("source") or "")
                prefer_new = (str(source) == active_src and prev_src != active_src) or (
                    (str(source) == active_src) == (prev_src == active_src)
                    and float(row.get("volume") or 0) > float(prev.get("volume") or 0)
                )
                merged_sources = sorted({*prev.get("sources", [prev_src]), str(source)})
                if prefer_new:
                    row["sources"] = merged_sources
                    out[sym] = row
                else:
                    prev["sources"] = merged_sources
                continue
            row["sources"] = [str(source)]
            out[sym] = row

    return out, {
        "sources": src_meta,
        "snapshot_at": _ts(newest),
        "active_broker": active_src,
    }


async def load_real_positions(
    tenant_id: str, user_id: str
) -> tuple[dict[str, dict], dict]:
    """实盘持仓明细（prefix 键）+ 各源快照元信息。

    每个持仓带 ``source`` 出处与 ``sources`` 并集（Phase 4 一键卖出按出处路由券商）。
    查询失败抛异常——由调用方决定是「如实标 ok=False」还是跳过本轮，本模块不吞。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(_SNAPSHOT_SQL), {"tid": tenant_id, "uid": user_id}
            )
        ).fetchall()
    return merge_real_sources(
        [(str(r[0]), r[1], r[2]) for r in rows], active_source=active_broker_type()
    )
