"""
Order Service - Order management business logic
"""

import logging
import json
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from backend.services.trade_shared.utils.stock_lookup import lookup_symbol_name
from backend.shared.order_contract import normalize_agent

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.order import (
    Order,
    OrderStatus,
    PositionSide,
    TradeAction,
    TradingMode,
)
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.trade_shared.schemas.order import (
    OrderCreate,
    OrderListQuery,
    OrderUpdate,
)
from backend.services.trade_shared.trade_config import settings

logger = logging.getLogger(__name__)


class OrderService:
    """Order management service"""

    def __init__(self, db: AsyncSession, redis: RedisClient):
        self.db = db
        self.redis = redis

    def _validate_transition(self, current_status: OrderStatus, new_status: OrderStatus) -> bool:
        if current_status == new_status:
            return True

        allowed = {
            OrderStatus.PENDING: [
                OrderStatus.SUBMITTED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ],
            OrderStatus.SUBMITTED: [
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ],
            OrderStatus.PARTIALLY_FILLED: [
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
            ],
            OrderStatus.FILLED: [],
            OrderStatus.CANCELLED: [],
            OrderStatus.REJECTED: [],
            OrderStatus.EXPIRED: [],
        }

        return new_status in allowed.get(current_status, [])

    @staticmethod
    def _resolve_trade_action(order_data: OrderCreate) -> TradeAction:
        if order_data.trade_action is not None:
            return order_data.trade_action
        if order_data.position_side == PositionSide.SHORT:
            return TradeAction.SELL_TO_OPEN if order_data.side.value == "sell" else TradeAction.BUY_TO_CLOSE
        return TradeAction.BUY_TO_OPEN if order_data.side.value == "buy" else TradeAction.SELL_TO_CLOSE

    @staticmethod
    def _normalize_query_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def _get_order_cache_key(self, order_id: str | UUID) -> str:
        return f"order:detail:{order_id}"

    def _get_order_list_cache_key(self, query: OrderListQuery) -> str:
        key_parts = [
            f"user:{query.user_id}",
            f"portfolio:{query.portfolio_id or 'all'}",
            f"mode:{query.trading_mode.value if query.trading_mode else 'all'}",
            f"symbol:{query.symbol or 'all'}",
            f"status:{query.status.value if query.status else 'all'}",
            f"side:{query.side.value if query.side else 'all'}",
        ]
        if query.start_date:
            key_parts.append(f"start:{query.start_date.isoformat()}")
        if query.end_date:
            key_parts.append(f"end:{query.end_date.isoformat()}")
        key_parts.append(f"limit:{query.limit}")
        key_parts.append(f"offset:{query.offset}")
        key_str = ":".join(key_parts)
        # 前缀带 user，便于 delete_pattern("order:list:user:{user_id}:*") 命中，避免永不失效
        return f"order:list:user:{query.user_id}:{hashlib.md5(key_str.encode()).hexdigest()}"

    async def _invalidate_order_cache(self, user_id: int, portfolio_id: int | None = None):
        """Invalidate order list caches"""
        patterns = [
            f"order:list:user:{user_id}:*",
        ]
        for pattern in patterns:
            self.redis.delete_pattern(pattern)
        logger.debug(f"Invalidated order caches for user {user_id}")

    def _serialize_order(self, order: Order) -> dict:
        """Serialize order for Redis cache"""
        data = {}
        for c in order.__table__.columns:
            v = getattr(order, c.name)
            if isinstance(v, datetime):
                data[c.name] = v.isoformat() if v else None
            elif isinstance(v, UUID):
                data[c.name] = str(v)
            elif isinstance(v, Decimal):
                data[c.name] = str(v)
            elif hasattr(v, 'value'):
                data[c.name] = v.value
            else:
                data[c.name] = v
        return data

    def _deserialize_orders(self, data: list[dict]) -> list[Order]:
        """Deserialize orders from Redis cache"""
        orders = []
        for item in data:
            order = Order(**item)
            orders.append(order)
        return orders

    async def get_order(
        self,
        order_id: UUID,
        tenant_id: str | None = None,
        user_id: int | None = None,
    ) -> Order | None:
        conditions = [Order.order_id == order_id]
        if tenant_id is not None:
            conditions.append(Order.tenant_id == tenant_id)
        if user_id is not None:
            conditions.append(Order.user_id == user_id)

        result = await self.db.execute(select(Order).where(and_(*conditions)))
        return result.scalar_one_or_none()

    async def list_orders(self, query: OrderListQuery) -> list[Order]:
        """List orders with Redis caching"""
        cache_key = self._get_order_list_cache_key(query)

        cached = self.redis.get(cache_key)
        if cached:
            logger.debug(f"Cache hit for order list: {cache_key}")
            return self._deserialize_orders(cached)

        conditions = []
        start_date = self._normalize_query_datetime(query.start_date)
        end_date = self._normalize_query_datetime(query.end_date)

        if query.tenant_id:
            conditions.append(Order.tenant_id == query.tenant_id)
        if query.user_id:
            conditions.append(Order.user_id == query.user_id)
        if query.portfolio_id:
            conditions.append(Order.portfolio_id == query.portfolio_id)
        if query.symbol:
            conditions.append(Order.symbol == query.symbol.upper())
        if query.status:
            conditions.append(Order.status == query.status)
        if query.side:
            conditions.append(Order.side == query.side)
        if query.trading_mode:
            conditions.append(Order.trading_mode == query.trading_mode)
        if start_date:
            conditions.append(Order.created_at >= start_date)
        if end_date:
            conditions.append(Order.created_at <= end_date)

        stmt = select(Order).where(and_(*conditions)) if conditions else select(Order)
        stmt = stmt.order_by(Order.created_at.desc())
        stmt = stmt.limit(query.limit).offset(query.offset)

        result = await self.db.execute(stmt)
        orders = list(result.scalars().all())

        if orders:
            serialized = [self._serialize_order(o) for o in orders]
            self.redis.set(cache_key, serialized, ttl=settings.CACHE_TTL_ORDER)
            logger.debug(f"Cached order list: {cache_key}")

        return orders

    async def transition_order_status(
        self, order: Order, new_status: OrderStatus, remarks: str | None = None
    ) -> Order:
        if not self._validate_transition(order.status, new_status):
            logger.warning(
                f"Invalid status transition attempted: {order.status} -> {new_status} for order {order.order_id}"
            )
            raise ValueError(f"Invalid status transition: {order.status} -> {new_status}")

        old_status = order.status
        order.status = new_status

        if new_status == OrderStatus.SUBMITTED:
            order.submitted_at = datetime.now()
        elif new_status == OrderStatus.FILLED:
            order.filled_at = datetime.now()
        elif new_status == OrderStatus.CANCELLED:
            order.cancelled_at = datetime.now()
        elif new_status == OrderStatus.EXPIRED:
            order.expired_at = datetime.now()

        if remarks:
            order.remarks = f"{order.remarks or ''} [{new_status.value.upper()}: {remarks}]"

        await self.db.commit()
        await self.db.refresh(order)

        logger.info(f"Order {order.order_id} transitioned: {old_status} -> {new_status}")
        await self._invalidate_order_cache(order.user_id, order.portfolio_id)
        self.redis.delete(self._get_order_cache_key(order.order_id))

        return order

    async def create_order(self, user_id: int, tenant_id: str, order_data: OrderCreate) -> Order:
        if order_data.order_type.value in ["market"]:
            order_value = order_data.quantity * 0
        else:
            order_value = order_data.quantity * (order_data.price or 0)

        order = Order(
            tenant_id=tenant_id,
            user_id=user_id,
            portfolio_id=order_data.portfolio_id,
            strategy_id=order_data.strategy_id,
            symbol=order_data.symbol.upper(),
            symbol_name=order_data.symbol_name or lookup_symbol_name(order_data.symbol.upper()),
            side=order_data.side,
            trade_action=self._resolve_trade_action(order_data),
            position_side=order_data.position_side,
            is_margin_trade=order_data.is_margin_trade,
            order_type=order_data.order_type,
            trading_mode=order_data.trading_mode or TradingMode.SIMULATION,
            quantity=order_data.quantity,
            price=order_data.price,
            stop_price=order_data.stop_price,
            order_value=order_value,
            client_order_id=order_data.client_order_id,
            # P2.7 分账：归属随订单落库（成交回报只带订单，不带决策上下文）。
            # 截断口径只此一处（``normalize_agent``）：与契约列同宽。
            agent=normalize_agent(order_data.agent) or None,
            # P1.6 TCA 基准价：决策时点的参考价（与 price 是两回事，见契约列注释）。
            # 缺省 None 是**如实缺省**：老腿/前端直单没有这个价，报告按"不可定价"计。
            ref_price=order_data.ref_price,
            remarks=order_data.remarks,
            status=OrderStatus.PENDING,
        )

        self.db.add(order)
        await self.db.commit()
        await self.db.refresh(order)

        logger.info(f"Order created: {order.order_id}")
        await self._invalidate_order_cache(user_id, order_data.portfolio_id)

        return order

    async def update_order(
        self,
        order_id: UUID,
        update_data: OrderUpdate,
        tenant_id: str | None = None,
        user_id: int | None = None,
    ) -> Order | None:
        order = await self.get_order(order_id, tenant_id=tenant_id, user_id=user_id)
        if not order:
            return None

        if order.status != OrderStatus.PENDING:
            raise ValueError(f"Cannot update order in status: {order.status}")

        if update_data.quantity is not None:
            order.quantity = update_data.quantity
        if update_data.price is not None:
            order.price = update_data.price
        if update_data.stop_price is not None:
            order.stop_price = update_data.stop_price
        if update_data.remarks is not None:
            order.remarks = update_data.remarks

        if order.order_type.value != "market" and order.price:
            order.order_value = order.quantity * order.price

        await self.db.commit()
        await self.db.refresh(order)

        logger.info(f"Order updated: {order_id}")
        await self._invalidate_order_cache(order.user_id, order.portfolio_id)
        self.redis.delete(self._get_order_cache_key(order_id))

        return order

    async def cancel_order(
        self,
        order_id: UUID,
        reason: str | None = None,
        tenant_id: str | None = None,
        user_id: int | None = None,
    ) -> Order | None:
        order = await self.get_order(order_id, tenant_id=tenant_id, user_id=user_id)
        if not order:
            return None

        return await self.transition_order_status(order, OrderStatus.CANCELLED, reason)

    async def get_order_statistics(self, tenant_id: str, user_id: int, portfolio_id: int | None = None) -> dict:
        # orders.user_id 为 VARCHAR，避免 int 参数类型不匹配（should be str comparison）
        conditions = [Order.tenant_id == tenant_id, Order.user_id == str(user_id)]
        if portfolio_id:
            conditions.append(Order.portfolio_id == portfolio_id)

        result = await self.db.execute(select(Order).where(and_(*conditions)))
        orders = list(result.scalars().all())

        stats = {
            "total_orders": len(orders),
            "pending": sum(1 for o in orders if o.status == OrderStatus.PENDING),
            "filled": sum(1 for o in orders if o.status == OrderStatus.FILLED),
            "cancelled": sum(1 for o in orders if o.status == OrderStatus.CANCELLED),
            "rejected": sum(1 for o in orders if o.status == OrderStatus.REJECTED),
        }

        return stats
