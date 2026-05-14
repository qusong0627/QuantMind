"""
Simulation order service.
"""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import String, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.simulation.models.order import OrderStatus, SimOrder
from backend.services.trade.simulation.schemas.order import SimOrderCreate


class SimOrderService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_order(self, tenant_id: str, user_id: str, data: SimOrderCreate) -> SimOrder:
        order_value = data.quantity * (data.price or 0)
        order = SimOrder(
            tenant_id=tenant_id,
            user_id=int(user_id) if str(user_id).isdigit() else user_id,
            portfolio_id=data.portfolio_id or 0,
            strategy_id=data.strategy_id,
            symbol=data.symbol.upper(),
            side=data.side,
            order_type=data.order_type,
            quantity=data.quantity,
            price=data.price,
            order_value=order_value,
            remarks=data.remarks,
            status=OrderStatus.PENDING,
        )
        self.db.add(order)
        await self.db.commit()
        await self.db.refresh(order)
        return order

    async def get_order(self, tenant_id: str, user_id: str, order_id: UUID) -> SimOrder | None:
        result = await self.db.execute(
            select(SimOrder).where(
                and_(
                    SimOrder.tenant_id == tenant_id,
                    cast(SimOrder.user_id, String) == str(user_id),
                    SimOrder.order_id == order_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_orders(
        self,
        tenant_id: str,
        user_id: str,
        *,
        portfolio_id: int | None = None,
        status: str | None = None,
        symbol: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SimOrder]:
        conditions = [SimOrder.tenant_id == tenant_id, cast(SimOrder.user_id, String) == str(user_id)]
        if portfolio_id is not None:
            conditions.append(SimOrder.portfolio_id == portfolio_id)
        if status:
            conditions.append(SimOrder.status == status)
        if symbol:
            conditions.append(SimOrder.symbol == symbol.upper())
        if start_date:
            conditions.append(SimOrder.created_at >= start_date)
        if end_date:
            conditions.append(SimOrder.created_at <= end_date)

        stmt = (
            select(SimOrder).where(and_(*conditions)).order_by(SimOrder.created_at.desc()).limit(limit).offset(offset)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def cancel_order(self, order: SimOrder, reason: str | None = None) -> SimOrder:
        if order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
            raise ValueError(f"Cannot cancel order in status: {order.status.value}")
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now()
        if reason:
            order.remarks = f"{order.remarks or ''} [Cancelled: {reason}]"
        await self.db.commit()
        await self.db.refresh(order)
        return order
