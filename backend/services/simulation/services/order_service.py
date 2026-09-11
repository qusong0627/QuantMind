"""
Simulation order service.
"""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import String, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.order import OrderStatus, SimOrder
from backend.services.simulation.schemas.order import SimOrderCreate


class SimOrderService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_order(
        self, tenant_id: str, user_id: str, data: SimOrderCreate, **kwargs
    ) -> SimOrder:
        order_value = data.quantity * (data.price or 0)
        try:
            from backend.shared.stock_utils import StockCodeUtil

            symbol = StockCodeUtil.to_prefix(data.symbol) or data.symbol.upper()
        except Exception:
            symbol = data.symbol.upper()
        order = SimOrder(
            tenant_id=tenant_id,
            user_id=int(user_id) if str(user_id).isdigit() else user_id,
            portfolio_id=data.portfolio_id or 0,
            strategy_id=data.strategy_id,
            symbol=symbol,
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
        # V2链路会同步写simulation_orders投影；旧链路不需要，忽略trigger等kwargs
        try:
            await self.sync_order_projection(order)
        except Exception:
            pass
        return order

    async def get_order(
        self, tenant_id: str, user_id: str, order_id: UUID
    ) -> SimOrder | None:
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
        conditions = [
            SimOrder.tenant_id == tenant_id,
            cast(SimOrder.user_id, String) == str(user_id),
        ]
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
            select(SimOrder)
            .where(and_(*conditions))
            .order_by(SimOrder.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def cancel_order(
        self, order: SimOrder, reason: str | None = None
    ) -> SimOrder:
        if order.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        ]:
            raise ValueError(f"Cannot cancel order in status: {order.status.value}")
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now(timezone.utc)
        if reason:
            order.remarks = f"{order.remarks or ''} [Cancelled: {reason}]"
        await self.db.commit()
        await self.db.refresh(order)
        return order

    # ── V2投影链路兼容（margin/强平与pending worker调用） ──
    async def get_projection_order_by_client_order_id(
        self, *, tenant_id: str, user_id: int, client_order_id: str
    ):
        """按client_order_id幂等查单；无表/无记录返回None，不抛错。"""
        try:
            from backend.services.simulation.models.order_v2 import SimulationOrderV2

            result = await self.db.execute(
                select(SimulationOrderV2)
                .where(
                    SimulationOrderV2.tenant_id == str(tenant_id),
                    SimulationOrderV2.user_id == str(user_id),
                    SimulationOrderV2.client_order_id == str(client_order_id),
                )
                .order_by(SimulationOrderV2.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
        except Exception:
            return None

    async def sync_order_projection(self, order, rejected_reason: str | None = None):
        """把旧SimOrder同步到simulation_orders投影；失败只记日志，保证主链路可用。"""
        try:
            import logging

            from backend.services.simulation.models.order_v2 import SimulationOrderV2

            status = getattr(order, "status", None)
            status_str = getattr(status, "value", status)
            stmt = (
                select(SimulationOrderV2)
                .where(SimulationOrderV2.order_id == getattr(order, "order_id", None))
                .limit(1)
                if getattr(order, "order_id", None) is not None
                else None
            )
            existing = None
            if stmt is not None:
                existing = (await self.db.execute(stmt)).scalar_one_or_none()
            if existing is None:
                try:
                    proj = SimulationOrderV2(
                        order_id=getattr(order, "order_id", None),
                        tenant_id=str(getattr(order, "tenant_id", "default")),
                        user_id=str(getattr(order, "user_id", "")),
                        account_id=f"{getattr(order, 'tenant_id', 'default')}:{getattr(order, 'user_id', '')}",
                        symbol=str(getattr(order, "symbol", "")),
                        side=str(
                            getattr(
                                getattr(order, "side", ""),
                                "value",
                                getattr(order, "side", ""),
                            )
                        ),
                        order_type=str(
                            getattr(
                                getattr(order, "order_type", ""),
                                "value",
                                getattr(order, "order_type", ""),
                            )
                        ),
                        quantity=float(getattr(order, "quantity", 0) or 0),
                        price=getattr(order, "price", None),
                        status=str(status_str or "pending"),
                        rejected_reason=rejected_reason,
                    )
                    self.db.add(proj)
                    await self.db.commit()
                except Exception as exc:
                    logging.getLogger(__name__).debug(
                        "sync_order_projection insert skipped: %s", exc
                    )
                    try:
                        await self.db.rollback()
                    except Exception:
                        pass
            else:
                existing.status = str(status_str or existing.status)
                if rejected_reason is not None:
                    existing.rejected_reason = rejected_reason
                await self.db.commit()
        except Exception:
            pass

    async def queue_order(self, order, message: str = "", trading_session_date=None):
        """挂单排队兼容：更新投影状态为pending，不阻塞主流程。"""
        try:
            await self.sync_order_projection(
                order, rejected_reason=str(message or "")[:500]
            )
        except Exception:
            pass

    def _build_runtime_order(self, projection_order, remarks: str | None = None):
        """V2投影转旧SimOrder运行时对象（内存态，供worker复用execute_order）。"""
        order = SimOrder(
            tenant_id=getattr(projection_order, "tenant_id", "default"),
            user_id=int(getattr(projection_order, "user_id", 0) or 0),
            portfolio_id=int(getattr(projection_order, "portfolio_id", 0) or 0),
            symbol=str(getattr(projection_order, "symbol", "")),
            side=getattr(projection_order, "side", "buy"),
            order_type=getattr(projection_order, "order_type", "market"),
            quantity=float(getattr(projection_order, "quantity", 0) or 0),
            price=getattr(projection_order, "price", None),
            remarks=remarks,
            status=OrderStatus.PENDING,
        )
        try:
            order.order_id = getattr(projection_order, "order_id", order.order_id)
        except Exception:
            pass
        return order
