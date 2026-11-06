"""每日日结查询与补跑（智能体台账口径）。

* ``GET  /daily-settlements``       —— 区间日结（默认最近 30 天）
* ``POST /daily-settlements/run``   —— 手动补跑（单日或区间，幂等）

日结本身由 ``backend.services.trade.services.daily_settlement`` 计算：
分账台账（qm_agent_ledger_*）+ 执行事实（orders/trades）+ 真实资金锚
（real_account_snapshots）。金额只认成交，不认委托。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.live_trading.routers.real_trading_utils import (
    normalize_db_user_id,
)
from backend.services.trade.services import daily_settlement as settlement
from backend.services.trade_shared.deps import (
    AuthContext,
    get_auth_context,
    get_db,
    get_redis,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_DAYS = 30
_MAX_BACKFILL_DAYS = 400


def _parse_day(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} 格式应为 YYYY-MM-DD：{value}"
        ) from exc


@router.get("/daily-settlements")
async def list_settlements(
    start: str | None = Query(None, description="YYYY-MM-DD（含）"),
    end: str | None = Query(None, description="YYYY-MM-DD（含）"),
    auth: AuthContext = Depends(get_auth_context),
    db: Any = Depends(get_db),
):
    """区间日结单（按交易日倒序）。"""
    uid = normalize_db_user_id(str(auth.user_id))
    tenant = str(auth.tenant_id or "default")
    end_day = _parse_day(end, "end") or datetime.now().date()
    start_day = _parse_day(start, "start") or (end_day - timedelta(days=_DEFAULT_DAYS))
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="开始日期晚于结束日期")

    try:
        await settlement.ensure_table_async(db)
        rows = await settlement.list_days(
            db, tenant_id=tenant, user_id=str(uid), start=start_day, end=end_day
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[DailySettlement] 查询失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"日结查询失败：{exc}") from exc

    return {
        "success": True,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "count": len(rows),
        "rows": rows,
        "note": "口径=智能体台账（成交/平仓）+ 执行事实（orders/trades）；金额只认成交",
    }


@router.post("/daily-settlements/run")
async def run_settlements(
    start: str = Query(..., description="YYYY-MM-DD（含）"),
    end: str | None = Query(None, description="YYYY-MM-DD（含）；留空=同 start"),
    auth: AuthContext = Depends(get_auth_context),
    db: Any = Depends(get_db),
    redis: Any = Depends(get_redis),
):
    """补跑日结（幂等 upsert；非交易日自动跳过）。"""
    uid = normalize_db_user_id(str(auth.user_id))
    tenant = str(auth.tenant_id or "default")
    start_day = _parse_day(start, "start")
    end_day = _parse_day(end, "end") or start_day
    if start_day is None or end_day is None:
        raise HTTPException(status_code=400, detail="start 必填")
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="开始日期晚于结束日期")
    if (end_day - start_day).days > _MAX_BACKFILL_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"补跑区间最长 {_MAX_BACKFILL_DAYS} 天",
        )

    anchor_source = await settlement.resolve_anchor_source(redis)
    days = await settlement.trading_days_between(start_day, end_day)
    done: list[str] = []
    skipped: list[str] = []
    errors: dict[str, str] = {}
    for day in days:
        try:
            payload = await settlement.run_day(
                db,
                tenant_id=tenant,
                user_id=str(uid),
                day=day,
                anchor_source=anchor_source,
            )
            if payload.get("has_activity") or payload.get("status") == "closed":
                done.append(day.isoformat())
            else:
                skipped.append(day.isoformat())
        except Exception as exc:  # noqa: BLE001 - 单日失败隔离
            logger.error("[DailySettlement] %s 日结失败: %s", day, exc)
            errors[day.isoformat()] = f"{type(exc).__name__}: {exc}"

    return {
        "success": not errors,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "done": done,
        "skipped": skipped,
        "errors": errors,
    }
