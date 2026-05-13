from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.deps import AuthContext, get_auth_context, get_db, get_redis
from backend.services.trade.redis_client import RedisClient
from backend.services.trade.simulation.models.order import OrderStatus, TradingMode
from backend.services.trade.simulation.schemas.order import (
    SimOrderCancelRequest,
    SimOrderCreate,
    SimOrderResponse,
)
from backend.services.trade.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.trade.simulation.services.order_service import SimOrderService
from backend.services.trade.simulation.services.simulation_manager import (
    SimulationAccountManager,
)

router = APIRouter()


def _require_int_user_id(raw_user_id: str) -> int:
    """获取用户ID（模拟盘订单模型要求 int）"""
    if not raw_user_id:
        raise HTTPException(status_code=400, detail="Invalid user_id in token")
    try:
        return int(raw_user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid user_id in token")


@router.post("/orders", response_model=SimOrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    data: SimOrderCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    if data.trading_mode != TradingMode.SIMULATION:
        raise HTTPException(
            status_code=400,
            detail="Simulation service only accepts trading_mode=simulation",
        )

    order_service = SimOrderService(db)
    manager = SimulationAccountManager(redis)
    engine = SimulationExecutionEngine(db, manager)

    user_id = _require_int_user_id(auth.user_id)
    order = await order_service.create_order(auth.tenant_id, user_id, data)
    order.status = OrderStatus.SUBMITTED
    await db.commit()
    await db.refresh(order)

    result = await engine.execute_order(order)
    if not result.success:
        await engine.mark_rejected(order, result.message)
        await db.refresh(order)
        return order

    await engine.apply_filled(order, result)
    await db.refresh(order)
    return order


@router.get("/orders", response_model=list[SimOrderResponse])
async def list_orders(
    portfolio_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_int_user_id(auth.user_id)
    service = SimOrderService(db)
    return await service.list_orders(
        auth.tenant_id,
        user_id,
        portfolio_id=portfolio_id,
        status=status,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/{order_id}", response_model=SimOrderResponse)
async def get_order(
    order_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_int_user_id(auth.user_id)
    service = SimOrderService(db)
    order = await service.get_order(auth.tenant_id, user_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Simulation order not found")
    return order


@router.post("/orders/{order_id}/cancel", response_model=SimOrderResponse)
async def cancel_order(
    order_id: UUID,
    request: SimOrderCancelRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_int_user_id(auth.user_id)
    service = SimOrderService(db)
    order = await service.get_order(auth.tenant_id, user_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Simulation order not found")

    try:
        return await service.cancel_order(order, request.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
