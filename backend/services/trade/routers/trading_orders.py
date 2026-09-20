"""
Order API Routes
"""

import logging
from typing import Optional
from collections.abc import Callable
from uuid import UUID
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.shared.stock_name_mapper import resolve_name
from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db, get_redis
from backend.services.trade_shared.models.order import OrderStatus, TradingMode
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.trade_shared.schemas.order import (
    OrderCancelRequest,
    OrderCreate,
    OrderListQuery,
    OrderResponse,
    OrderUpdate,
)
from backend.services.trade_shared.services.order_service import OrderService
from backend.services.live_trading.services.trading_engine import TradingEngine

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_user_id(raw_user_id: str) -> str:
    """获取用户ID (字符串类型，兼容 'admin' 等非数字ID)"""
    if not raw_user_id:
        raise HTTPException(status_code=400, detail="Invalid user_id in token")
    return raw_user_id


def fill_order_names(
    orders: list[OrderResponse],
    resolver: Callable[[str], str] | None = None,
) -> list[OrderResponse]:
    """给缺失名称的委托补上股票名（就地补在**响应对象**上，不碰 ORM 实例）。

    为什么不碰 ORM：`orders.symbol_name` 是真实映射列，在 GET 里给持久化对象赋值
    会把会话标脏 → 收尾 flush 时读出 UPDATE（只读副本直接报错）。补在已序列化的
    `OrderResponse` 上则是一次纯内存加工。

    只补空值：库里写好的名字优先（人工修正不得被覆盖）。查不到保持 None，
    由前端回落显示代码——不写空串、更不把代码塞进名称列，否则「有名字」与
    「没名字」在界面上就分不出来了。
    """
    lookup = resolver or resolve_name
    for order in orders:
        if order.symbol_name:
            continue
        name = lookup(order.symbol) or ""
        if name:
            order.symbol_name = name
    return orders


@router.post("/", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    order_data: OrderCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """Create a new order"""
    try:
        user_id = _require_user_id(auth.user_id)
        if (order_data.trading_mode or TradingMode.SIMULATION) == TradingMode.SIMULATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Simulation orders are handled by simulation-service",
            )

        order_service = OrderService(db, redis)
        trading_engine = TradingEngine(db, redis)

        # Create order
        order = await order_service.create_order(user_id, auth.tenant_id, order_data)

        # Check risk
        risk_check = await trading_engine.check_order_risk(user_id, order)
        if not risk_check["passed"]:
            # Reject order
            await order_service.cancel_order(order.order_id, reason=f"Risk check failed: {risk_check['violations']}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "Risk check failed",
                    "violations": risk_check["violations"],
                },
            )

        # Submit order for execution
        await trading_engine.submit_order(order, tenant_id=auth.tenant_id)

        # Refresh order to get latest status
        await db.refresh(order)

        return order

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create order: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create order: {str(e)}",
        )


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """Get order by ID"""
    user_id = _require_user_id(auth.user_id)
    order_service = OrderService(db, redis)
    order = await order_service.get_order(order_id, tenant_id=auth.tenant_id, user_id=user_id)

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order {order_id} not found")

    return order


@router.put("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: UUID,
    update_data: OrderUpdate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """Update order (only pending orders)"""
    try:
        user_id = _require_user_id(auth.user_id)
        order_service = OrderService(db, redis)
        existing = await order_service.get_order(order_id, tenant_id=auth.tenant_id, user_id=user_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Order {order_id} not found",
            )
        order = await order_service.update_order(order_id, update_data, tenant_id=auth.tenant_id, user_id=user_id)

        return order

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update order: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update order: {str(e)}",
        )


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: UUID,
    cancel_request: OrderCancelRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """Cancel order — 先向 Broker 发撤单指令，再更新本地状态。"""
    try:
        user_id = _require_user_id(auth.user_id)
        order_service = OrderService(db, redis)
        existing = await order_service.get_order(order_id, tenant_id=auth.tenant_id, user_id=user_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Order {order_id} not found",
            )

        # 终态订单无需撤单
        terminal = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
        if existing.status in terminal:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Order is already in terminal state: {existing.status.value}",
            )

        # 向 Broker 发出撤单指令（fire-and-forget，失败不阻止本地状态更新）
        try:
            engine = TradingEngine(db, redis)
            broker = engine._get_broker(existing.trading_mode, existing.symbol)
            exchange_order_id = str(existing.exchange_order_id or "").strip()
            await broker.cancel_order(
                exchange_order_id or str(existing.order_id),
                user_id=str(user_id),
                tenant_id=auth.tenant_id,
                account_id=None,
                client_order_id=str(existing.client_order_id or ""),
                symbol=str(existing.symbol or ""),
                side=str(getattr(existing.side, "value", existing.side) or ""),
            )
        except Exception as broker_exc:
            logger.warning("broker cancel_order failed (continuing local cancel): %s", broker_exc)

        order = await order_service.cancel_order(
            order_id,
            cancel_request.reason,
            tenant_id=auth.tenant_id,
            user_id=user_id,
        )

        return order

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to cancel order: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel order: {str(e)}",
        )


@router.get("/", response_model=list[OrderResponse])
async def list_orders(
    portfolio_id: int = None,
    symbol: str = None,
    status: str = None,
    trading_mode: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """List orders with filters"""
    user_id = _require_user_id(auth.user_id)
    normalized_trading_mode = None
    if trading_mode is not None:
        try:
            normalized_trading_mode = TradingMode(str(trading_mode).upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid trading_mode: {trading_mode}",
            ) from exc

    normalized_status = None
    if status:
        try:
            normalized_status = OrderStatus(str(status).upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid status: {status}",
            ) from exc

    query = OrderListQuery(
        tenant_id=auth.tenant_id,
        user_id=user_id,
        portfolio_id=portfolio_id,
        symbol=symbol,
        status=normalized_status,
        trading_mode=normalized_trading_mode,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )

    order_service = OrderService(db, redis)
    orders = await order_service.list_orders(query)

    # 桥接写入路径不写 symbol_name（199 行里 114 行 NULL），前端「股票」列只剩代码。
    # 列表是所有交易记录界面的取数口（策略管理/交易记录页签），补在最里层一次到位。
    return fill_order_names(
        [OrderResponse.model_validate(o) for o in orders]
    )


@router.get("/stats/summary")
async def get_order_statistics(
    portfolio_id: int = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """Get order statistics"""
    user_id = _require_user_id(auth.user_id)
    order_service = OrderService(db, redis)
    stats = await order_service.get_order_statistics(auth.tenant_id, user_id, portfolio_id)

    return stats
