from datetime import datetime
from typing import List, Optional
from uuid import UUID

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.deps import (
    AuthContext,
    get_auth_context,
    get_db,
    get_redis,
)
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.models.order import OrderStatus, TradingMode
from backend.services.simulation.schemas.order import (
    SimOrderCancelRequest,
    SimOrderCreate,
    SimOrderResponse,
)
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.order_service import SimOrderService
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
    require_sim_user_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_user_id(raw_user_id: str, tenant_id: str = "default") -> int:
    """兼容别名，统一走 require_sim_user_id（OSS admin 归保留账户 0）。"""
    return require_sim_user_id(raw_user_id, tenant_id=tenant_id)


@router.post(
    "/orders", response_model=SimOrderResponse, status_code=status.HTTP_201_CREATED
)
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

    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    # P0-1：同用户撮合临界区串行化，防止并发下单双花/快照恢复覆盖。
    # 拿不到锁直接429由前端重试，不静默放行。
    try:
        async with SimulationAccountManager.locked_execution(user_id, auth.tenant_id):
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
    except RuntimeError as exc:
        if str(exc) == "SIM_EXEC_LOCK_BUSY":
            raise HTTPException(
                status_code=429, detail="账户撮合繁忙，请稍后重试"
            ) from exc
        raise


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
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimOrderService(db)
    orders = await service.list_orders(
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
    # 批量 enrich 名称，避免前端 N+1，外观与 真实订单/持仓 口径一致
    try:
        from backend.services.trade_shared.utils.stock_lookup import lookup_symbol_name

        enriched = []
        for o in orders:
            try:
                name = (
                    lookup_symbol_name(o.symbol) if getattr(o, "symbol", None) else None
                )
            except Exception:
                name = None
            # Pydantic from_attributes 读不到 DB 列时，用运行时属性补齐
            try:
                o.symbol_name = name  # type: ignore[attr-defined]
            except Exception:
                pass
            enriched.append(o)
        return enriched
    except Exception:
        return orders


@router.get("/orders/{order_id}", response_model=SimOrderResponse)
async def get_order(
    order_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
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
    user_id = _require_user_id(auth.user_id, auth.tenant_id)
    service = SimOrderService(db)
    order = await service.get_order(auth.tenant_id, user_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Simulation order not found")

    try:
        return await service.cancel_order(order, request.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
