"""副驾驶 API（T-P6-16）：实时上下文（QuantBot 工具化查询）/ 建议卡 / 一键执行 / 交易台面板。

- `GET  /api/v1/copilot/context`  实时上下文（持仓 + 当日信号 Top + 近期告警 + 热集/regime +
  数据可得性），供 QuantBot 以**工具化查询**注入实时上下文（非 prompt 拼接）；
- `POST /api/v1/copilot/advice`   创建建议卡（QuantBot/人工；带 context_refs 依据下钻）；
- `GET  /api/v1/copilot/advice`   建议卡列表（交易台副驾驶面板数据源）；
- `POST /api/v1/copilot/advice/{id}/reject`  拒绝（理由留痕）；
- `POST /api/v1/copilot/advice/{id}/execute` **一键执行**：逐动作走 OrderRouter 唯一入口
  （source=co_pilot，幂等键 cop-{advice}-{sym}-{side}，同用户撮合临界区串行化），
  执行结果逐条留痕到建议卡（execution JSONB）；
- `GET  /api/v1/copilot/panel`    交易台面板聚合（总线事件流 + 时延 + 预算 + 误报率摘要）。

纪律：数据块逐块带 `source`；不可用如实标注（available=false + reason），绝不 mock。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)
_CST = timezone(timedelta(hours=8))

router = APIRouter(prefix="/api/v1/copilot", tags=["Copilot"])

_ALLOWED_SIDES = {"buy", "sell"}
_ALLOWED_ORDER_TYPES = {"market", "limit"}
_MAX_ACTIONS = 20


class AdviceAction(BaseModel):
    symbol: str
    side: str
    quantity: float = Field(..., gt=0)
    order_type: str = "market"
    price: float | None = None


class AdviceCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    rationale: str = ""
    # actions 允许为空 = 纯建议卡（观察/纪律类，如"不追热点"）；执行端点对空动作 400
    actions: list[AdviceAction] = Field(default_factory=list, max_length=_MAX_ACTIONS)
    context_refs: dict[str, Any] = Field(default_factory=dict)
    source: str = "quantbot"


class RejectRequest(BaseModel):
    reason: str = ""


_CN_SUFFIX_RE = __import__("re").compile(r"^\d{6}\.(SH|SZ|BJ)$")


def validate_actions(actions: list[AdviceAction]) -> list[dict[str, Any]]:
    """动作规范化+校验（纯函数）：符号归一为 CN 后缀式（v1 仅支持 A 股）；
    非法即 400（资金相关不静默降级）。"""
    from backend.shared.stock_utils import StockCodeUtil

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        side = str(action.side or "").strip().lower()
        if side not in _ALLOWED_SIDES:
            raise ValueError(f"side 非法: {action.side!r}（允许 {sorted(_ALLOWED_SIDES)}）")
        order_type = str(action.order_type or "market").strip().lower()
        if order_type not in _ALLOWED_ORDER_TYPES:
            raise ValueError(f"order_type 非法: {action.order_type!r}")
        symbol = StockCodeUtil.to_suffix(str(action.symbol or "").strip()) or ""
        if not _CN_SUFFIX_RE.match(symbol):
            raise ValueError(f"symbol 非法（v1 仅支持 A 股后缀式）: {action.symbol!r}")
        if order_type == "limit" and (action.price is None or float(action.price) <= 0):
            raise ValueError(f"{symbol} 限价单必须带正价格")
        key = (symbol, side)
        if key in seen:
            raise ValueError(f"重复动作: {symbol} {side}")
        seen.add(key)
        out.append({"symbol": symbol, "side": side, "quantity": float(action.quantity),
                    "order_type": order_type,
                    "price": float(action.price) if action.price is not None else None})
    return out


async def _collect_context(tenant_id: str, raw_user: str) -> dict[str, Any]:
    """实时上下文聚合（QuantBot 工具数据源；块级 source/可用性如实标注）。"""
    now = datetime.now(_CST)
    blocks: dict[str, Any] = {
        "as_of": now.isoformat(),
        "tenant_id": tenant_id,
    }
    # 持仓（模拟账户）
    blocks["positions"] = {"available": False, "reason": "未读取", "source": "redis:trade:simulation:account"}
    try:
        from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

        client = get_trade_redis()
        if getattr(client, "client", None) is None:
            client.connect()
        raw = client.client.get(f"simulation:account:{tenant_id}:{raw_user}")
        payload = json.loads(raw) if raw else {}
        positions = []
        for sym, pos in (payload.get("positions") or {}).items():
            try:
                volume = float((pos or {}).get("volume") or 0)
                price = float((pos or {}).get("price") or 0)
            except (TypeError, ValueError):
                continue
            if volume > 0:
                positions.append({"symbol": str(sym), "volume": volume, "price": price,
                                  "market_value": round(volume * price, 2)})
        blocks["positions"] = {
            "available": True,
            "cash": payload.get("cash"),
            "total_asset": payload.get("total_asset"),
            "items": sorted(positions, key=lambda p: -p["market_value"])[:30],
            "source": "redis:trade:simulation:account",
        }
    except Exception as exc:  # noqa: BLE001
        blocks["positions"] = {"available": False, "reason": str(exc)[:200],
                               "source": "redis:trade:simulation:account"}

    # 当日信号（engine_signal_scores 最新日）
    blocks["signals"] = {"available": False, "reason": "未读取", "source": "db:engine_signal_scores"}
    try:
        async with get_session(read_only=True) as session:
            latest = (await session.execute(
                text("SELECT max(trade_date) FROM engine_signal_scores WHERE COALESCE(market,'CN')='CN'")
            )).scalar()
            if latest is None:
                blocks["signals"] = {"available": False, "reason": "无信号数据",
                                     "source": "db:engine_signal_scores"}
            else:
                rows = (await session.execute(
                    text(
                        "SELECT DISTINCT ON (symbol) symbol, fusion_score, signal_side "
                        "FROM engine_signal_scores WHERE trade_date = :d AND COALESCE(market,'CN')='CN' "
                        "ORDER BY symbol, fusion_score DESC"
                    ),
                    {"d": latest},
                )).fetchall()
                top = sorted(rows, key=lambda r: -(r[1] or 0))[:20]
                sides: dict[str, int] = {}
                for _s, _f, side in rows:
                    sides[str(side or "HOLD")] = sides.get(str(side or "HOLD"), 0) + 1
                blocks["signals"] = {
                    "available": True, "trade_date": str(latest),
                    "side_counts": sides,
                    "top": [{"symbol": r[0], "fusion_score": r[1], "signal_side": r[2]} for r in top],
                    "source": "db:engine_signal_scores",
                }
    except Exception as exc:  # noqa: BLE001
        blocks["signals"] = {"available": False, "reason": str(exc)[:200],
                             "source": "db:engine_signal_scores"}

    # 近期告警（哨兵留痕）
    blocks["alerts"] = {"available": False, "reason": "未读取", "source": "db:sentinel_alerts"}
    try:
        async with get_session(read_only=True) as session:
            rows = (await session.execute(
                text(
                    "SELECT alert_type, severity, symbol, title, hit, annotation, ts::text "
                    "FROM sentinel_alerts WHERE ts > NOW() - INTERVAL '24 hours' "
                    "ORDER BY ts DESC LIMIT 20"
                )
            )).fetchall()
        blocks["alerts"] = {
            "available": True,
            "items": [
                {"alert_type": r[0], "severity": r[1], "symbol": r[2], "title": r[3],
                 "hit": r[4], "annotation": r[5], "ts": r[6]}
                for r in rows
            ],
            "source": "db:sentinel_alerts",
        }
    except Exception as exc:  # noqa: BLE001
        blocks["alerts"] = {"available": False, "reason": str(exc)[:200],
                            "source": "db:sentinel_alerts"}

    # 热集 + regime（Redis）
    try:
        import os

        import redis as _redis

        client = _redis.Redis(host=os.getenv("REDIS_HOST") or "redis",
                              port=int(os.getenv("REDIS_PORT", "6379")),
                              password=os.getenv("REDIS_PASSWORD") or None,
                              db=int(os.getenv("REDIS_DB_GENERAL", "0")),
                              decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
        try:
            hot_n = client.scard("qm:hot_set:symbols") if client.exists("qm:hot_set:symbols") else 0
            regime = client.hgetall("qm:regime:intraday") or {}
        finally:
            client.close()
        blocks["market_state"] = {
            "available": bool(regime) or hot_n > 0,
            "hot_set_size": int(hot_n or 0),
            "regime": regime or None,
            "source": "redis:qm:hot_set:symbols, qm:regime:intraday",
        }
    except Exception as exc:  # noqa: BLE001
        blocks["market_state"] = {"available": False, "reason": str(exc)[:200],
                                  "source": "redis"}
    return blocks


@router.get("/context")
async def copilot_context(current_user: dict = Depends(get_current_user)):
    """实时上下文（QuantBot 工具化查询入口）。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    raw_user = str(current_user.get("user_id") or "")
    return {"success": True, "data": await _collect_context(tenant_id, raw_user)}


@router.post("/advice")
async def create_advice(payload: AdviceCreate, current_user: dict = Depends(get_current_user)):
    """创建建议卡（QuantBot/人工）；actions 非法即 400。"""
    from backend.shared.copilot_contract import ensure_copilot_advice_table

    try:
        actions = validate_actions(payload.actions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ensure_copilot_advice_table():
        raise HTTPException(status_code=503, detail="建议卡存储不可用")
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "0")
    async with get_session(read_only=False) as session:
        row = (await session.execute(
            text(
                "INSERT INTO copilot_advice (tenant_id, user_id, source, title, rationale, "
                "actions, context_refs, status) "
                "VALUES (:t, :u, :src, :ti, :ra, CAST(:ac AS JSONB), CAST(:cr AS JSONB), 'pending') "
                "RETURNING advice_id::text"
            ),
            {"t": tenant_id, "u": int(user_id) if user_id.isdigit() else 0,
             "src": payload.source[:32], "ti": payload.title[:256], "ra": payload.rationale[:4000],
             "ac": json.dumps(actions, ensure_ascii=False),
             "cr": json.dumps(payload.context_refs, ensure_ascii=False, default=str)},
        )).scalar()
        await session.commit()
    return {"success": True, "data": {"advice_id": row, "status": "pending", "actions": actions}}


@router.get("/advice")
async def list_advice(
    status: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """建议卡列表（交易台面板）。"""
    from backend.shared.copilot_contract import ensure_copilot_advice_table

    if not ensure_copilot_advice_table():
        return {"success": True, "data": {"items": [], "available": False}}
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "0")
    params: dict[str, Any] = {"t": tenant_id,
                              "u": int(user_id) if user_id.isdigit() else 0, "lim": limit}
    # user_id=0 = QuantBot/系统共享建议（内部调用身份非数字 → 0），租户内全员可见
    where = "tenant_id = :t AND (user_id = :u OR user_id = 0)"
    if status.strip():
        where += " AND status = :st"
        params["st"] = status.strip()
    async with get_session(read_only=True) as session:
        rows = (await session.execute(
            text(
                "SELECT advice_id::text, source, title, rationale, actions, context_refs, status, "
                "       created_at::text, decided_at::text, reject_reason, executed_at::text, execution, "
                "       outcome, outcome_status "
                f"FROM copilot_advice WHERE {where} ORDER BY created_at DESC LIMIT :lim"
            ),
            params,
        )).fetchall()
    return {"success": True, "data": {"items": [
        {"advice_id": r[0], "source": r[1], "title": r[2], "rationale": r[3], "actions": r[4],
         "context_refs": r[5], "status": r[6], "created_at": r[7], "decided_at": r[8],
         "reject_reason": r[9], "executed_at": r[10], "execution": r[11],
         "outcome": r[12], "outcome_status": r[13]}
        for r in rows
    ], "available": True}}


@router.get("/advice/stats")
async def advice_stats(
    days: int = Query(90, ge=7, le=365),
    current_user: dict = Depends(get_current_user),
):
    """建议卡成功率统计（近 days 天，租户视图）：采纳率 + T+1/T+3/T+5 胜率与平均超额。

    口径（与回调服务同源）：决策日收盘→第 h 交易日收盘，卖出取规避收益，超额=个股−沪深300；
    拒绝的卡同样兑现（反事实），统计上可分列。
    """
    from backend.shared.copilot_contract import ensure_copilot_advice_table

    if not ensure_copilot_advice_table():
        return {"success": True, "data": {"available": False}}
    tenant_id = str(current_user.get("tenant_id") or "default")
    async with get_session(read_only=True) as session:
        rows = (await session.execute(
            text(
                "SELECT status, outcome FROM copilot_advice "
                "WHERE tenant_id = :t AND created_at > NOW() - make_interval(days => :d)"
            ),
            {"t": tenant_id, "d": int(days)},
        )).fetchall()
    total = len(rows)
    executed = sum(1 for s, _ in rows if s in ("executed", "partial", "failed"))
    rejected = sum(1 for s, _ in rows if s == "rejected")
    decided = executed + rejected
    scored = 0
    hz: dict[str, dict[str, float]] = {
        h: {"n": 0.0, "hits": 0.0, "excess_sum": 0.0, "excess_n": 0.0} for h in ("1", "3", "5")
    }
    for _status, outcome in rows:
        doc = outcome if isinstance(outcome, dict) else None
        if not doc:
            continue
        scored += 1
        for h, s in (doc.get("summary") or {}).items():
            if h not in hz or not isinstance(s, dict):
                continue
            n = float(s.get("n") or 0)
            hz[h]["n"] += n
            hz[h]["hits"] += float(s.get("hits") or 0)
            if s.get("avg_excess") is not None and n > 0:
                hz[h]["excess_sum"] += float(s["avg_excess"]) * n
                hz[h]["excess_n"] += n
    by_horizon = {
        h: {
            "n": int(v["n"]),
            "hits": int(v["hits"]),
            "hit_rate": round(v["hits"] / v["n"], 4) if v["n"] else None,
            "avg_excess": round(v["excess_sum"] / v["excess_n"], 6) if v["excess_n"] else None,
        }
        for h, v in hz.items()
    }
    return {"success": True, "data": {
        "available": True,
        "days": int(days),
        "total": total,
        "decided": decided,
        "executed": executed,
        "rejected": rejected,
        "decide_rate": round(decided / total, 4) if total else None,
        "scored": scored,
        "by_horizon": by_horizon,
        "source": "db:copilot_advice（决策日收盘→T+h 收盘，超额 vs 沪深300；拒绝同样兑现）",
    }}


async def _load_pending_advice(session, advice_id: str, tenant_id: str, user_id: int):
    return (await session.execute(
        text(
            "SELECT advice_id::text, title, actions, status FROM copilot_advice "
            "WHERE advice_id = CAST(:aid AS UUID) AND tenant_id = :t "
            "AND (user_id = :u OR user_id = 0)  -- 0=QuantBot 共享建议，可被用户决策"
        ),
        {"aid": advice_id, "t": tenant_id, "u": user_id},
    )).fetchone()


@router.post("/advice/{advice_id}/reject")
async def reject_advice(
    advice_id: str,
    payload: RejectRequest,
    current_user: dict = Depends(get_current_user),
):
    """拒绝建议（理由留痕；仅 pending 可拒）。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "0")
    uid = int(user_id) if user_id.isdigit() else 0
    async with get_session(read_only=False) as session:
        row = await _load_pending_advice(session, advice_id, tenant_id, uid)
        if row is None:
            raise HTTPException(status_code=404, detail="建议卡不存在")
        if row[3] != "pending":
            raise HTTPException(status_code=409, detail=f"建议卡已定局（{row[3]}），不可重复决定")
        await session.execute(
            text(
                "UPDATE copilot_advice SET status='rejected', decided_at=NOW(), decided_by=:u, "
                "reject_reason=:r WHERE advice_id = CAST(:aid AS UUID)"
            ),
            {"u": uid, "r": payload.reason[:500], "aid": advice_id},
        )
        await session.commit()
    return {"success": True, "data": {"advice_id": advice_id, "status": "rejected"}}


@router.post("/advice/{advice_id}/execute")
async def execute_advice(advice_id: str, current_user: dict = Depends(get_current_user)):
    """一键执行（接受）：逐动作走 OrderRouter（来源 co_pilot，幂等键 cop-*，同用户临界区）。"""
    from backend.services.simulation.services.order_router import (
        OrderRequest,
        submit_order,
    )
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
        require_sim_user_id,
    )
    from backend.shared.copilot_contract import ensure_copilot_advice_table
    from backend.shared.order_contract import SOURCE_CO_PILOT, build_copilot_client_order_id

    if not ensure_copilot_advice_table():
        raise HTTPException(status_code=503, detail="建议卡存储不可用")
    tenant_id = str(current_user.get("tenant_id") or "default")
    raw_user = str(current_user.get("user_id") or "")
    uid = require_sim_user_id(raw_user, tenant_id=tenant_id)

    async with get_session(read_only=False) as session:
        row = await _load_pending_advice(session, advice_id, tenant_id, uid)
        if row is None:
            raise HTTPException(status_code=404, detail="建议卡不存在")
        if row[3] != "pending":
            raise HTTPException(status_code=409, detail=f"建议卡已定局（{row[3]}）")
        title, actions = row[1], row[2] or []
        if not actions:
            raise HTTPException(
                status_code=400, detail="纯建议卡（无动作）不可执行——接受/拒绝即可"
            )

    redis = get_trade_redis()
    if getattr(redis, "client", None) is None:
        redis.connect()

    results: list[dict[str, Any]] = []
    # 注：OrderRouter.submit_order 内部已持「同用户撮合临界区」锁（唯一入口自带串行化），
    # 调用方**不得**再包 locked_execution——否则同用户重入死锁（实测「账户撮合繁忙」）。
    async with get_session(read_only=False) as session:
        for action in actions:
            symbol = str(action.get("symbol") or "")
            side = str(action.get("side") or "").lower()
            try:
                outcome = await submit_order(
                    session, redis,
                    OrderRequest(
                        tenant_id=tenant_id, user_id=uid, symbol=symbol, side=side,
                        quantity=float(action.get("quantity") or 0),
                        order_type=str(action.get("order_type") or "market"),
                        price=action.get("price"),
                        source=SOURCE_CO_PILOT,
                        client_order_id=build_copilot_client_order_id(advice_id, symbol, side),
                        remarks=f"co_pilot:{title[:80]}",
                    ),
                )
                results.append({
                    "symbol": symbol, "side": side, "success": bool(outcome.success),
                    "order_id": outcome.order_id, "trade_id": outcome.trade_id,
                    "fill_price": outcome.fill_price, "filled_quantity": outcome.filled_quantity,
                    "message": outcome.message, "duplicate": bool(outcome.duplicate),
                })
            except Exception as exc:  # noqa: BLE001 - 单动作失败不阻断其余
                logger.warning("[copilot] 执行失败 %s %s: %s", symbol, side, exc)
                results.append({"symbol": symbol, "side": side, "success": False,
                                "message": str(exc)[:200]})

    ok = sum(1 for r in results if r.get("success"))
    status = "executed" if ok == len(results) and results else (
        "failed" if ok == 0 else "partial")
    async with get_session(read_only=False) as session:
        await session.execute(
            text(
                "UPDATE copilot_advice SET status=:st, decided_at=NOW(), decided_by=:u, "
                "executed_at=NOW(), execution=CAST(:ex AS JSONB) WHERE advice_id = CAST(:aid AS UUID)"
            ),
            {"st": status, "u": uid, "ex": json.dumps(results, ensure_ascii=False, default=str),
             "aid": advice_id},
        )
        await session.commit()
    return {"success": True, "data": {"advice_id": advice_id, "status": status,
                                      "executed": ok, "total": len(results), "results": results}}


@router.get("/panel")
async def copilot_panel(
    hours: int = Query(24, ge=1, le=168),
    current_user: dict = Depends(get_current_user),
):
    """交易台副驾驶面板聚合：总线事件流 + 时延 + 推理预算 + 误报率摘要（无 mock，缺失如实）。"""
    panel: dict[str, Any] = {"as_of": datetime.now(_CST).isoformat()}
    # 事件流（哨兵留痕 = 总线全量落表）
    try:
        async with get_session(read_only=True) as session:
            rows = (await session.execute(
                text(
                    "SELECT alert_id::text, ts::text, alert_type, severity, market, symbol, title, "
                    "       targets, pushed, outcome_status, hit, annotation "
                    "FROM sentinel_alerts WHERE ts > NOW() - make_interval(hours => :h) "
                    "ORDER BY ts DESC LIMIT 50"
                ),
                {"h": hours},
            )).fetchall()
        panel["events"] = {
            "available": True,
            "items": [
                {"alert_id": r[0], "ts": r[1], "alert_type": r[2], "severity": r[3],
                 "market": r[4], "symbol": r[5], "title": r[6], "targets": r[7],
                 "pushed": bool(r[8]), "outcome_status": r[9], "hit": r[10], "annotation": r[11]}
                for r in rows
            ],
            "source": "db:sentinel_alerts",
        }
    except Exception as exc:  # noqa: BLE001
        panel["events"] = {"available": False, "reason": str(exc)[:200], "items": []}
    # 时延（T-P6-05 双通道口径：展示用 _fresh 档——全量档含停牌/夜盘陈旧重放帧，
    # 不是传输时延；2026-09-18 面板曾因读全量档显示 15.3min 误报）
    try:
        from backend.shared.latency_metrics import read_latency

        bridge_fresh = read_latency("market_snapshot_bridge_fresh")
        subscriber_fresh = read_latency("market_snapshot_fresh")
        display, display_stage = (
            (bridge_fresh, "market_snapshot_bridge_fresh")
            if bridge_fresh
            else (subscriber_fresh, "market_snapshot_fresh")
        )
        panel["latency"] = {
            "available": display is not None,
            "display": display,
            "display_stage": display_stage,
            "market_snapshot_bridge_fresh": bridge_fresh,
            "market_snapshot_fresh": subscriber_fresh,
            "note": "展示口径=_fresh 档（行情到达时延）；全量档含陈旧重放帧，不用于展示",
            "source": "redis:intel:latency",
        }
    except Exception as exc:  # noqa: BLE001
        panel["latency"] = {"available": False, "reason": str(exc)[:200]}
    # 资源预算（T-P6-10）：复用验收器的资源汇总（tdx worker 群 + 引擎 + 容器内存）
    try:
        from backend.scripts.p6_acceptance_report import collect_resources

        resources = collect_resources()
        panel["budget"] = {
            "available": resources is not None,
            "detail": resources,
            "source": "psutil+cgroup（与验收器 G 项同源）",
        }
    except Exception as exc:  # noqa: BLE001
        panel["budget"] = {"available": False, "reason": str(exc)[:200]}
    # 误报率摘要
    try:
        from backend.services.api.routers.sentinel import build_report

        async with get_session(read_only=True) as session:
            rows = (await session.execute(
                text(
                    "SELECT alert_type, severity, pushed, outcome_status, hit, annotation "
                    "FROM sentinel_alerts WHERE trade_date >= CURRENT_DATE - 30"
                )
            )).fetchall()
        report = build_report(
            [{"alert_type": r[0], "severity": r[1], "pushed": bool(r[2]),
              "outcome_status": r[3], "hit": r[4], "annotation": r[5]} for r in rows],
            days=30,
        )
        panel["miss_rate"] = {"available": True, **report["overall"],
                              "window_days": 30, "source": "db:sentinel_alerts"}
    except Exception as exc:  # noqa: BLE001
        panel["miss_rate"] = {"available": False, "reason": str(exc)[:200]}
    return {"success": True, "data": panel}
