"""Agent 交易对账单：按 agent 汇总 REAL 委托/成交（PG 唯一真源）。

口径
----
* 只统计 ``trading_mode=REAL``；模拟/影子属于模拟盘，不进实盘对账单；
* 金额取 ``orders.filled_value``、手续费取 ``orders.commission``（均由成交回报落库）；
* ``agent`` 为空 = 人工/系统兜底单，归入「(人工)」；
* 日期区间 ``start``/``end``（YYYY-MM-DD，含端点），默认近 30 天；
* 券商侧同日对账走 ``/real-trading/broker-records?date=...``
  （QMT 支持历史，TDX 只有当日——柜台接口边界）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from backend.services.live_trading.routers.real_trading_utils import (
    normalize_db_user_id,
)
from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db
from backend.services.trade_shared.models.enums import OrderStatus, TradingMode
from backend.services.trade_shared.models.order import Order

router = APIRouter()
logger = logging.getLogger(__name__)

_DEFAULT_DAYS = 30


def _parse_day(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"日期格式应为 YYYY-MM-DD：{value}"
        ) from exc
    return parsed.replace(hour=23, minute=59, second=59) if end_of_day else parsed


@router.get("/agent-statement")
async def agent_statement(
    start: str | None = Query(None, description="YYYY-MM-DD（含）"),
    end: str | None = Query(None, description="YYYY-MM-DD（含）"),
    auth: AuthContext = Depends(get_auth_context),
    db: Any = Depends(get_db),
):
    """按 agent 汇总实盘委托/成交（对账单）。"""
    uid = normalize_db_user_id(str(auth.user_id))
    tenant = str(auth.tenant_id or "default")
    end_dt = _parse_day(end, end_of_day=True) or datetime.now()
    start_dt = _parse_day(start) or (end_dt - timedelta(days=_DEFAULT_DAYS))

    stmt = (
        select(
            Order.agent,
            func.count().label("orders"),
            func.count()
            .filter(Order.status == OrderStatus.FILLED)
            .label("filled_orders"),
            func.coalesce(func.sum(Order.filled_value), 0.0).label("filled_value"),
            func.coalesce(func.sum(Order.commission), 0.0).label("commission"),
        )
        .where(
            Order.user_id == uid,
            Order.tenant_id == tenant,
            Order.trading_mode == TradingMode.REAL,
            Order.created_at >= start_dt,
            Order.created_at <= end_dt,
        )
        .group_by(Order.agent)
    )
    try:
        rows = (await db.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001
        logger.error("[AgentStatement] 查询失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"对账单查询失败：{exc}") from exc

    items = [
        {
            "agent": str(row.agent or "").strip() or "(人工)",
            "orders": int(row.orders or 0),
            "filled_orders": int(row.filled_orders or 0),
            "filled_value": round(float(row.filled_value or 0), 2),
            "commission": round(float(row.commission or 0), 2),
        }
        for row in rows
    ]
    items.sort(key=lambda item: item["filled_value"], reverse=True)
    total = {
        "orders": sum(i["orders"] for i in items),
        "filled_orders": sum(i["filled_orders"] for i in items),
        "filled_value": round(sum(i["filled_value"] for i in items), 2),
        "commission": round(sum(i["commission"] for i in items), 2),
    }
    return {
        "success": True,
        "start": start_dt.strftime("%Y-%m-%d"),
        "end": end_dt.strftime("%Y-%m-%d"),
        "total": total,
        "items": items,
        "note": "金额=成交额（filled_value），手续费=orders.commission；券商侧对账用 broker-records",
    }
