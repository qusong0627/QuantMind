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
   并把两个出处都记进 ``sources``（Phase 4 一键卖出按出处路由券商）。
4. ``payload_json`` 实测是**双层编码**（JSON 字符串套 JSON），两种形态都容忍；
   解不开就在日志里说清楚，不静默当空仓。
"""

from __future__ import annotations

import json

from backend.shared.logging_config import get_logger
from backend.shared.signal_scores import normalize_position_symbol

logger = get_logger(__name__)

#: 相对新鲜度窗口（分钟）：某源落后全源最新超过此值 → 判停更、不并入。
_REAL_SOURCE_STALE_MINUTES = 60

_SNAPSHOT_SQL = (
    "SELECT DISTINCT ON (source) source, snapshot_at, payload_json "
    "FROM real_account_snapshots "
    "WHERE tenant_id = :tid AND user_id = :uid "
    "ORDER BY source, snapshot_at DESC"
)


def snapshot_source_for_broker(broker_type: str | None) -> str | None:
    """券商类型 → 快照 source（与 risk_gate_service 同一映射）。"""
    b = str(broker_type or "").lower()
    if b.startswith("tdx"):
        return "tdx_bridge"
    if b.startswith("qmt"):
        return "qmt_exec"
    return None


def active_broker_type() -> str | None:
    """当前实盘券商：Redis ``broker:selected:CN``（券商接入页选定）→ settings 回退。"""
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        client = get_redis_sentinel_client()
        if client and client.client:
            raw = client.client.get("broker:selected:CN")
            if raw:
                return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception as exc:  # noqa: BLE001 - 选谁只是并列时的偏好，取不到不影响并集
        logger.debug("读取 broker:selected:CN 失败: %s", exc)
    try:
        from backend.services.trade_shared.trade_config import settings

        return str(getattr(settings, "REAL_BROKER_TYPE", "") or "")
    except Exception:  # noqa: BLE001
        return None


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
