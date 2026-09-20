"""自选池统一视图：手工自选 ∪ 模拟持仓 ∪ 实盘持仓 ∪ 正分候选。

**读时并集，不写库。** 把持仓/候选写进 `qm_user_watchlist` 会分不清手工与自动：
用户手动移出一只持仓票，下一次同步它又回来了；候选有六百多只，写进去会
把用户自己的池子淹掉。所以 `qm_user_watchlist` 仍是「手工自选」的唯一真身，
本模块只负责在读取时把四路来源合成一张表，卖出/清仓后持仓源自然消失
（即产品口径里的「自动移出」）。

四路来源与口径：

- ``manual``          —— ``qm_user_watchlist``（prefix 键形，如 ``SH600036``）
- ``position_sim``    —— trade 服务 ``GET /api/v1/simulation/account``（含可卖量/T+1）
- ``position_real``   —— PG ``real_account_snapshots`` **多券商账户并集**（同一 user 下
  ``qmt_exec`` 与 ``tdx_bridge`` 是两个互不相交的真实账户，取最新一条等于掷硬币）
- ``candidate``       —— ``engine_signal_scores`` 最近覆盖充分日、``signal_side='BUY'``
  且 ``fusion_score > 0``（与候选信号页同源同口径），按分数降序截断

诚实纪律（与 `stock_terminal_exclusions` 一致）：**取不到 ≠ 没有**。
模拟/实盘/候选任一源失败时在 ``meta.sources`` 里显式标 ``ok=False`` + 原因，
绝不把「没取到」渲染成「空仓/无候选」。名单未导入同样透传 ``imported=False``。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx

from backend.shared.logging_config import get_logger
from backend.shared.stock_utils import StockCodeUtil

logger = get_logger(__name__)

#: 与 `/stock-terminal/list` 同源的「信号日覆盖充分」判据（该处 _MIN_SIGNAL_COVERAGE）。
#: 复制常量而不是 import：两边分属 router 层与 service 层，反向依赖会造成循环导入；
#: 改口径时两处必须一起改（同一条 SQL 的 HAVING 子句）。
MIN_SIGNAL_COVERAGE = 1000

#: 候选通道默认截断只数。全市场正分候选约 650 只，全量返回会把自选表撑爆；
#: 截断数在 counts 里如实给出（candidate_total vs candidate_shown）。
DEFAULT_CANDIDATE_CAP = 200

#: 单次响应默认上限（持仓与手工自选不参与截断——它们天然有限，且有操作价值）
DEFAULT_LIMIT = 300

_CN_PREFIX_RE = r"^(SH|SZ|BJ)\d{6}$"


def _norm_symbol(raw: Any) -> str | None:
    """任意键形 → prefix 规范形（``SH600036``）；非 A 股返回 None。

    持仓键可能带侧标（``SH600036::long``，两融），先切掉再归一。
    """
    base = str(raw or "").split("::", 1)[0].strip()
    if not base:
        return None
    prefix = StockCodeUtil.to_prefix(base)
    return prefix if re.match(_CN_PREFIX_RE, prefix) else None


def _position_payload(row: dict[str, Any]) -> dict[str, Any]:
    """持仓行 → 前端载荷（camelCase，缺值一律 None 不填 0）。"""

    def _f(key: str) -> float | None:
        v = row.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    payload = {
        "volume": _f("volume"),
        "availableVolume": _f("available_volume"),
        "cost": _f("cost_price") if row.get("cost_price") is not None else _f("cost"),
        "marketValue": _f("market_value"),
        "price": _f("last_price") if row.get("last_price") is not None else _f("price"),
        "side": row.get("side") or "long",
    }
    # 实盘出处（qmt_exec / tdx_bridge）：Phase 4 一键卖出必须按出处路由券商
    if row.get("source"):
        payload["source"] = row["source"]
    if row.get("sources"):
        payload["sources"] = list(row["sources"])
    return payload


def merge_sources(
    *,
    manual: dict[str, dict[str, Any]],
    sim: dict[str, dict[str, Any]],
    real: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """四路来源合成一张表（纯函数）。

    ``manual``/``sim``/``real`` 以 prefix 代码为键；``candidates`` 是
    ``[{symbol(prefix), score, side, freq, asOf}]``。返回 ``(items, counts)``。

    排序：有持仓的在前（按持仓市值降序）→ 手工自选 → 候选（按分数降序）。
    持仓与手工自选**不参与截断**；只有候选会被 ``candidate_cap`` 与 ``limit``
    以「先到先得」的方式削减（``counts.total`` 始终是并集全量，不受二者影响）。
    """
    items: dict[str, dict[str, Any]] = {}

    def _touch(sym: str, source: str) -> dict[str, Any]:
        it = items.get(sym)
        if it is None:
            it = {
                "symbol": sym,
                "stockName": None,
                "sources": [],
                "position": {},
                "score": None,
            }
            items[sym] = it
        if source not in it["sources"]:
            it["sources"].append(source)
        return it

    for sym, row in manual.items():
        it = _touch(sym, "manual")
        if row.get("stockName"):
            it["stockName"] = row["stockName"]
        it["addedAt"] = row.get("addedAt")

    for source, book in (("position_sim", sim), ("position_real", real)):
        for sym, row in book.items():
            it = _touch(sym, source)
            it["position"][source.removeprefix("position_")] = _position_payload(row)
            if row.get("name") and not it["stockName"]:
                it["stockName"] = row["name"]

    # 候选先全量并入（同票可能与持仓/手工行合并），cap 与 limit 都只在输出阶段做——
    # 否则 counts.total 会变成「截断后的行数」，与「并集共几行」不是一回事
    for c in candidates:
        sym = c.get("symbol")
        if not sym:
            continue
        it = _touch(sym, "candidate")
        # 同一只票在多个候选行时（理论上 DISTINCT ON 已去重）保留高分那条
        prev = it.get("score")
        if prev is None or float(c.get("score") or 0) > float(prev.get("value") or 0):
            it["score"] = {
                "value": c.get("score"),
                "side": c.get("side"),
                "freq": c.get("freq") or "daily",
                "asOf": c.get("asOf"),
            }

    def _sort_key(it: dict[str, Any]) -> tuple[int, float, str]:
        pos = it.get("position") or {}
        mv = sum(float((p or {}).get("marketValue") or 0.0) for p in pos.values())
        has_pos = 1 if pos else 0
        is_manual = 1 if "manual" in it["sources"] else 0
        score = float((it.get("score") or {}).get("value") or 0.0)
        # 持仓优先 → 手工 → 候选；组内：持仓按市值、其余按分数
        if has_pos:
            return (0, -mv, it["symbol"])
        if is_manual:
            return (1, -score, it["symbol"])
        return (2, -score, it["symbol"])

    ordered = sorted(items.values(), key=_sort_key)
    # _sort_key 已把持仓(0)/手工(1)排在候选(2)前面，优先级组就是前缀；
    # 候选展示数同时受 candidate_cap 与 limit 余量约束
    n_priority = sum(
        1 for it in ordered if it.get("position") or "manual" in it["sources"]
    )
    budget = max(0, min(int(limit) - n_priority, int(candidate_cap)))
    shown = ordered[:n_priority] + ordered[n_priority:][:budget]

    counts = {
        "manual": len(manual),
        "position_sim": len(sim),
        "position_real": len(real),
        "candidate_total": len(candidates),
        "candidate_shown": sum(1 for it in shown if "candidate" in it["sources"]),
        "total": len(items),
        "shown": len(shown),
    }
    return shown, counts


# ── 取数（每路一个函数，失败如实上报不静默当空）──────────────────────


async def load_manual_watchlist(
    tenant_id: str, user_id: str
) -> dict[str, dict[str, Any]]:
    """手工自选（prefix 键）。查询失败抛异常，由调用方标 ok=False。"""
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text

    async with get_session(read_only=True) as session:
        res = await session.execute(
            text(
                "SELECT symbol, stock_name, added_at FROM qm_user_watchlist "
                "WHERE tenant_id = :tid AND user_id = :uid"
            ),
            {"tid": tenant_id, "uid": user_id},
        )
        out: dict[str, dict[str, Any]] = {}
        for r in res:
            sym = _norm_symbol(r[0])
            if not sym:
                continue
            out[sym] = {
                "stockName": r[1],
                "addedAt": r[2].isoformat() if hasattr(r[2], "isoformat") else r[2],
            }
        return out


async def load_sim_positions(
    authorization: str, tenant_id: str, user_id: str, trade_base_url: str
) -> dict[str, dict[str, Any]]:
    """模拟盘持仓明细（prefix 键）。

    ``/api/v1/simulation/account`` 的 positions 键是**后缀式**（``300649.SZ``），
    与自选表的 prefix 口径不同——这里统一归一到 prefix，避免同一只票在并集里
    出现两行。
    """
    headers = {
        "Authorization": authorization,
        "X-User-Id": str(user_id),
        "X-Tenant-Id": str(tenant_id),
    }
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(
            f"{trade_base_url}/api/v1/simulation/account", headers=headers
        )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, row in (data.get("positions") or {}).items():
        sym = _norm_symbol(key)
        if not sym or not isinstance(row, dict):
            continue
        out[sym] = row
    return out


#: 同一 user 下多个券商账户快照并存（实测 qmt_exec 50 只 / tdx_bridge 8 只，持仓集合
#: 完全不相交）。相对新鲜度窗口：某源的最新快照落后「全源最新」超过这个分钟数，
#: 视为该源已停更，不并入（陈旧源会把早已卖出的持仓一直留在表上）。
_REAL_SOURCE_STALE_MINUTES = 60


def _snapshot_source_for_broker(broker_type: str | None) -> str | None:
    """券商类型 → 快照 source（与 risk_gate_service 同一映射）。"""
    b = str(broker_type or "").lower()
    if b.startswith("tdx"):
        return "tdx_bridge"
    if b.startswith("qmt"):
        return "qmt_exec"
    return None


def _active_broker_type() -> str | None:
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


async def load_real_positions(
    tenant_id: str, user_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """实盘持仓明细（prefix 键）+ 各源快照元信息。

    **多券商账户取并集**，不是「取最新一条」：实测同一 (tenant, user) 下
    ``qmt_exec``（50 只）与 ``tdx_bridge``（8 只）是两个互不相交的真实账户，
    两条流每 30s 交错写库——``ORDER BY snapshot_at DESC LIMIT 1`` 等于掷硬币，
    一半概率把 221 万持仓显示成 8 只。

    每个持仓带 ``source`` 出处（Phase 4 一键卖出要按出处路由券商）；两源都报
    同一只票时取活跃券商的量（``broker:selected:CN`` → ``REAL_BROKER_TYPE``）。
    停更源（落后全源最新 > ``_REAL_SOURCE_STALE_MINUTES``）不并入但在 meta 里
    如实报出。``payload_json`` 实测是**双层编码**（JSON 字符串套 JSON），容忍两种形态。
    """
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (source) source, snapshot_at, payload_json "
                    "FROM real_account_snapshots "
                    "WHERE tenant_id = :tid AND user_id = :uid "
                    "ORDER BY source, snapshot_at DESC"
                ),
                {"tid": tenant_id, "uid": user_id},
            )
        ).fetchall()
    if not rows:
        return {}, {"sources": {}, "snapshot_at": None, "active_broker": None}

    def _ts(v: Any) -> str | None:
        return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)

    # 相对新鲜度：以全源最新快照为基准（免疫容器/DB 时钟偏移）
    newest = max((r[1] for r in rows if r[1] is not None), default=None)
    active_src = _snapshot_source_for_broker(_active_broker_type())

    out: dict[str, dict[str, Any]] = {}
    src_meta: dict[str, Any] = {}
    for source, snap_at, payload in rows:
        age_min = None
        if newest is not None and snap_at is not None:
            age_min = round((newest - snap_at).total_seconds() / 60.0, 1)
        stale = age_min is not None and age_min > _REAL_SOURCE_STALE_MINUTES
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                logger.warning("real snapshot payload_json 二次解码失败（source=%s）", source)
                payload = {}
        items = [p for p in (payload or {}).get("positions") or [] if isinstance(p, dict)]
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
            sym = _norm_symbol(item.get("symbol"))
            if not sym:
                continue
            row = dict(item)
            row["source"] = str(source)
            prev = out.get(sym)
            # 同一只票出现在两个账户：优先活跃券商，否则取量大者（并记 sources）
            if prev is not None:
                prev_src = str(prev.get("source") or "")
                prefer_new = (
                    str(source) == active_src and prev_src != active_src
                ) or (
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


async def load_signal_scores(
    tenant_id: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """信号日正分候选 + 全量分数映射。

    返回 ``(candidates, score_map, meta)``：

    - ``candidates``：最近覆盖充分日（``COUNT(DISTINCT symbol) >= MIN_SIGNAL_COVERAGE``，
      与候选列表同口径）``signal_side='BUY'`` 且 ``fusion_score > 0``，按分数降序**全量**
      （截断统一由 ``merge_sources`` 在输出端做，cap 只在一处生效）。
    - ``score_map``：该日**全部**标的的最新一条分数（同 symbol 多 run 时按
      ``created_at`` 取最新——混着取会让分数不确定），供持仓/自选行显示分数；
      ``source='realtime'`` 的行即盘中实时分（热集推理落库）。
    """
    from backend.shared.database_manager_v2 import get_session
    from sqlalchemy import text

    meta: dict[str, Any] = {
        "signal_date": None,
        "candidate_total": 0,
        "realtime_rows": 0,
    }
    async with get_session(read_only=True) as session:
        d0 = (
            await session.execute(
                text(
                    "SELECT trade_date FROM engine_signal_scores "
                    "WHERE tenant_id = :tid AND (market IS NULL OR market = 'CN') "
                    "GROUP BY trade_date HAVING COUNT(DISTINCT symbol) >= :min_cov "
                    "ORDER BY trade_date DESC LIMIT 1"
                ),
                {"tid": tenant_id, "min_cov": MIN_SIGNAL_COVERAGE},
            )
        ).scalar_one_or_none()
        if d0 is None:
            d0 = (
                await session.execute(
                    text(
                        "SELECT trade_date FROM engine_signal_scores "
                        "WHERE tenant_id = :tid AND (market IS NULL OR market = 'CN') "
                        "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar_one_or_none()
        if d0 is None:
            return [], {}, meta

        meta["signal_date"] = str(d0)[:10]
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (symbol) symbol, fusion_score, signal_side, source "
                    "FROM engine_signal_scores "
                    "WHERE tenant_id = :tid AND trade_date = :d AND (market IS NULL OR market = 'CN') "
                    "ORDER BY symbol, created_at DESC, id DESC"
                ),
                {"tid": tenant_id, "d": d0},
            )
        ).fetchall()

    as_of = meta["signal_date"]
    score_map: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for r in rows:
        sym = _norm_symbol(r[0])
        if not sym:
            continue
        score = float(r[1]) if r[1] is not None else None
        freq = "realtime" if str(r[3] or "") == "realtime" else "daily"
        if freq == "realtime":
            meta["realtime_rows"] += 1
        score_map[sym] = {"value": score, "side": r[2], "freq": freq, "asOf": as_of}
        if r[2] == "BUY" and score is not None and score > 0:
            candidates.append(
                {
                    "symbol": sym,
                    "score": score,
                    "side": "BUY",
                    "freq": freq,
                    "asOf": as_of,
                }
            )

    candidates.sort(key=lambda c: (-float(c["score"]), c["symbol"]))
    # 全量返回，截断由 merge_sources 在输出端统一做（cap/limit 一处生效）；
    # 全量数由 counts.candidate_total 如实给出，不在这里另设同名 meta 键
    return candidates, score_map, meta


# ── 编排 ─────────────────────────────────────────────────────────────


async def build_unified_watchlist(
    *,
    tenant_id: str,
    user_id: str,
    authorization: str,
    trade_base_url: str,
    limit: int = DEFAULT_LIMIT,
    candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    today: str | None = None,
) -> dict[str, Any]:
    """四路取数 → 合成 → 风险标注。任一路失败只标 ``meta.sources``，不阻断其余。"""
    from backend.services.api import stock_terminal_exclusions as excl

    sources: dict[str, Any] = {}

    async def _guard(name: str, coro: Any, default: Any) -> Any:
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断整表
            logger.warning("unified watchlist: %s 取数失败: %s", name, exc)
            sources[name] = {"ok": False, "reason": str(exc)[:200]}
            return default

    manual = await _guard("manual", load_manual_watchlist(tenant_id, user_id), {})
    sim = await _guard(
        "position_sim",
        load_sim_positions(authorization, tenant_id, user_id, trade_base_url),
        {},
    )

    real, real_meta = await _guard(
        "position_real",
        load_real_positions(tenant_id, user_id),
        ({}, {"sources": {}, "snapshot_at": None, "active_broker": None}),
    )
    candidates, score_map, sig_meta = await _guard(
        "candidate", load_signal_scores(tenant_id), ([], {}, {})
    )

    items, counts = merge_sources(
        manual=manual,
        sim=sim,
        real=real,
        candidates=candidates,
        candidate_cap=candidate_cap,
        limit=limit,
    )

    # 分数回填：候选行的 score 已在 merge 里带上；持仓/手工行在此补分数
    for it in items:
        if it.get("score") is None:
            it["score"] = score_map.get(it["symbol"])

    # 风险标注（名单 + 近 20 天新闻），与候选列表同源同口径
    ref_today = today or date.today().isoformat()
    lst, blocked, list_meta = excl.list_channel(today=ref_today)
    suffix_syms = [StockCodeUtil.to_suffix(it["symbol"]) for it in items]
    news_map = await excl.news_annotations(suffix_syms)
    _risk_syms, _news_meta = await excl.news_risk_channel()  # 缓存命中，只为拿 meta
    del _risk_syms
    annotated = 0
    for it, sfx in zip(items, suffix_syms, strict=True):
        risk = excl.row_risk(
            sfx, lst=lst, blocked=blocked, news=news_map.get(sfx), today=ref_today
        )
        if risk:
            it["risk"] = risk
            annotated += 1

    default_sources = {
        "manual": {},
        "position_sim": {},
        "position_real": {},
        "candidate": {},
    }
    for name in default_sources:
        sources.setdefault(name, {"ok": True})

    return {
        "items": items,
        "counts": {**counts, "risk_annotated": annotated},
        "meta": {
            "sources": sources,
            "signal_date": sig_meta.get("signal_date"),
            "realtime_rows": sig_meta.get("realtime_rows", 0),
            # 实盘是多账户并集：逐源报快照时间/只数/是否停更（前端要显示「来源」）
            "real_snapshot_at": real_meta.get("snapshot_at"),
            "real_sources": real_meta.get("sources") or {},
            "real_active_broker": real_meta.get("active_broker"),
            "exclusion": excl.exclusion_meta(
                list_meta=list_meta,
                news_meta=_news_meta,
                counts={},
                extra={"channel": "unified"},
            ),
        },
    }
