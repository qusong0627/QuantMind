"""
Trade Service - Trade (execution) management business logic
"""

import logging
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.models.order import Order, OrderStatus, TradingMode
from backend.services.trade.models.trade import Trade
from backend.services.trade.redis_client import RedisClient
from backend.services.trade.schemas.trade import TradeListQuery
from backend.services.trade.services.remote_service import remote_service
from backend.services.trade.trade_config import settings

logger = logging.getLogger(__name__)


class TradeService:
    """Trade management service with Redis caching"""

    def __init__(self, db: AsyncSession, redis: RedisClient):
        self.db = db
        self.redis = redis
        from backend.services.trade.services.order_service import OrderService

        self.order_service = OrderService(db, redis)

    def _get_trade_list_cache_key(self, user_id: int, portfolio_id: int | None = None, trading_mode: TradingMode | None = None) -> str:
        mode_str = trading_mode.value if trading_mode else "all"
        port_str = f"port:{portfolio_id}" if portfolio_id else "all_portfolios"
        return f"trade:list:user:{user_id}:{port_str}:mode:{mode_str}"

    async def _invalidate_trade_cache(self, user_id: int, portfolio_id: int | None = None):
        """Invalidate trade-related caches for all combinations"""
        # Pattern match delete for this user
        pattern = f"trade:list:user:{user_id}:*"
        await self.redis.delete_pattern(pattern)
        logger.debug(f"Invalidated all trade caches for user {user_id}")

    async def list_trades(self, query: TradeListQuery) -> list[Trade]:
        """List trades with aggressive Redis caching"""
        # Try cache for standard list queries (no specific symbol/date filters)
        cache_key = None
        if not (query.start_date or query.end_date or query.symbol):
            # We always have user_id from AuthContext
            cache_key = self._get_trade_list_cache_key(query.user_id, query.portfolio_id, query.trading_mode)
            # Add paging info to key
            cache_key += f":limit:{query.limit}:offset:{query.offset}"

            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.info(f"Cache hit for trade list: {cache_key}")
                return cached_data

        # Build query (filters and stmt remain the same...)
        filters = [
            Trade.tenant_id == query.tenant_id,
            Trade.user_id == query.user_id if query.user_id else True,
            Trade.portfolio_id == query.portfolio_id if query.portfolio_id else True,
            Trade.symbol == query.symbol if query.symbol else True,
            Trade.executed_at >= query.start_date if query.start_date else True,
            Trade.executed_at <= query.end_date if query.end_date else True,
        ]

        if query.trading_mode:
            filters.append(Trade.trading_mode == query.trading_mode)

        stmt = select(Trade).where(and_(*filters)).order_by(Trade.executed_at.desc())

        if query.limit:
            stmt = stmt.limit(query.limit)
        if query.offset:
            stmt = stmt.offset(query.offset)

        result = await self.db.execute(stmt)
        trades = result.scalars().all()

        # Update cache if applicable
        if cache_key and trades:
            trade_dicts = []
            for t in trades:
                d = {c.name: getattr(t, c.name) for c in t.__table__.columns}
                for k, v in d.items():
                    if isinstance(v, (datetime, UUID, Decimal)):
                        d[k] = str(v)
                trade_dicts.append(d)

            await self.redis.set(cache_key, json.dumps(trade_dicts), expire=settings.CACHE_TTL_TRADE)

        return trades
    async def create_trade(
        self,
        order: Order,
        quantity: float,
        price: float,
        commission: float = 0.0,
        stamp_duty: float = 0.0,
        transfer_fee: float = 0.0,
    ) -> Trade:
        """Create a trade (execution record) with optimistic locking and fee breakdown"""
        trade_value = quantity * price
        total_fee = commission + stamp_duty + transfer_fee

        trade = Trade(
            order_id=order.order_id,
            tenant_id=order.tenant_id,
            user_id=order.user_id,
            portfolio_id=order.portfolio_id,
            symbol=order.symbol,
            symbol_name=getattr(order, "symbol_name", None),
            side=order.side,
            trade_action=order.trade_action,
            position_side=order.position_side,
            is_margin_trade=bool(order.is_margin_trade),
            trading_mode=order.trading_mode,
            quantity=quantity,
            price=price,
            trade_value=trade_value,
            commission=commission,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
            total_fee=total_fee,
            executed_at=datetime.now(),
        )

        # Optimistic Locking check for Order updates
        stmt = (
            select(Order)
            .where(and_(Order.id == order.id, Order.version == order.version))
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        locked_order = result.scalar_one_or_none()

        if not locked_order:
            logger.error(f"Concurrency conflict: Order {order.order_id} modified by another process")
            raise RuntimeError("Order version conflict")

        # Update order amounts
        locked_order.filled_quantity += quantity
        locked_order.filled_value += trade_value
        locked_order.commission += total_fee
        locked_order.version += 1  # Increment version

        # Calculate average price
        if locked_order.filled_quantity > 0:
            locked_order.average_price = locked_order.filled_value / locked_order.filled_quantity

        # Update order status via State Machine
        new_status = locked_order.status
        if locked_order.filled_quantity >= locked_order.quantity:
            new_status = OrderStatus.FILLED
        elif locked_order.filled_quantity > 0:
            new_status = OrderStatus.PARTIALLY_FILLED

        if new_status != locked_order.status:
            await self.order_service.transition_order_status(locked_order, new_status)

        self.db.add(trade)
        await self.db.commit()
        await self.db.refresh(trade)

        # Immediate Position Sync
        try:
            from backend.services.trade.portfolio.services.position_service import PositionService
            sync_data = TradeSync(
                portfolio_id=trade.portfolio_id,
                symbol=trade.symbol,
                side=trade.side.value if hasattr(trade.side, 'value') else trade.side,
                quantity=trade.quantity,
                price=trade.price,
                commission=trade.commission,
                stamp_duty=trade.stamp_duty,
                transfer_fee=trade.transfer_fee,
                total_fee=trade.total_fee,
                position_side=trade.position_side,
                trade_action=trade.trade_action,
                trade_id=str(trade.trade_id)
            )
            # Re-fetch session context if needed or use existing (Note: sync_trade_update needs its own commit if successful)
            await PositionService.sync_trade_update(self.db, sync_data)
            await self.db.commit()
        except Exception as e:
            logger.error(f"Failed to sync position for trade {trade.trade_id}: {str(e)}")
            # In production, this might trigger a background retry job

        logger.info(f"Trade created and position synced: {trade.trade_id}")

        # Cache Invalidation & Update
        await self._invalidate_trade_cache(order.user_id, order.portfolio_id)
        # Clear specific order cache
        await self.redis.delete(self._get_order_cache_key(order.order_id))

        # Publish Event (Event-Driven Communication)
        event_data = {
            "event_type": "TRADE_CREATED",
            "trade_id": str(trade.trade_id),
            "order_id": str(order.order_id),
            "portfolio_id": order.portfolio_id,
            "user_id": order.user_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "trade_action": order.trade_action.value if order.trade_action else None,
            "position_side": order.position_side.value if order.position_side else None,
            "is_margin_trade": bool(order.is_margin_trade),
            "quantity": quantity,
            "price": price,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "transfer_fee": transfer_fee,
            "total_fee": total_fee,
            "timestamp": datetime.now().isoformat(),
        }
        self.redis.publish_event("trading_events", event_data)
        return trade

        # Invalidate cache
        self.redis.delete(f"order:{order.order_id}")
        self._invalidate_trade_cache(order.user_id, order.portfolio_id)

        # Legacy Sync to Portfolio Service (Keeping it for compatibility for now)
        try:
            await remote_service.sync_trade_to_portfolio(trade)
        except Exception as e:
            logger.warning(f"Legacy sync failed (event already published): {e}")

        return trade

    async def get_trade(
        self,
        trade_id: UUID,
        tenant_id: str | None = None,
        user_id: int | None = None,
    ) -> Trade | None:
        """Get trade by ID with optional tenant/user scope"""
        use_cache = tenant_id is None and user_id is None

        # Only use global cache for unscoped lookup.
        cache_key = f"trade:{trade_id}"
        if use_cache:
            cached = self.redis.get(cache_key)
            if cached:
                return Trade(**{k: v for k, v in cached.items() if k != "_sa_instance_state"})

        conditions = [Trade.trade_id == trade_id]
        if tenant_id is not None:
            conditions.append(Trade.tenant_id == tenant_id)
        if user_id is not None:
            conditions.append(Trade.user_id == user_id)

        result = await self.db.execute(select(Trade).where(and_(*conditions)))
        trade = result.scalar_one_or_none()

        if trade and use_cache:
            # Cache it
            trade_dict = {k: v for k, v in trade.__dict__.items() if k != "_sa_instance_state"}
            self.redis.set(cache_key, trade_dict, ttl=settings.CACHE_TTL_TRADE)

        return trade

    @staticmethod
    def _normalize_query_datetime(value: datetime | None) -> datetime | None:
        """Convert timezone-aware datetime to naive UTC for TIMESTAMP WITHOUT TIME ZONE columns."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    async def list_trades(self, query: TradeListQuery) -> list[Trade]:
        """List trades with filters"""
        conditions = []
        start_date = self._normalize_query_datetime(query.start_date)
        end_date = self._normalize_query_datetime(query.end_date)

        if query.tenant_id:
            conditions.append(Trade.tenant_id == query.tenant_id)
        if query.user_id:
            conditions.append(Trade.user_id == query.user_id)
        if query.portfolio_id:
            conditions.append(Trade.portfolio_id == query.portfolio_id)
        if query.order_id:
            conditions.append(Trade.order_id == query.order_id)
        if query.symbol:
            conditions.append(Trade.symbol == query.symbol.upper())
        if query.side:
            conditions.append(Trade.side == query.side)
        if query.trading_mode:
            conditions.append(Trade.trading_mode == query.trading_mode)
        if start_date:
            conditions.append(Trade.executed_at >= start_date)
        if end_date:
            conditions.append(Trade.executed_at <= end_date)

        stmt = select(Trade).where(and_(*conditions)) if conditions else select(Trade)
        stmt = stmt.order_by(Trade.executed_at.desc())
        stmt = stmt.limit(query.limit).offset(query.offset)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_trades_by_order(self, tenant_id: str, user_id: int, order_id: UUID) -> list[Trade]:
        """Get all trades for an order"""
        result = await self.db.execute(
            select(Trade)
            .where(
                and_(
                    Trade.order_id == order_id,
                    Trade.tenant_id == tenant_id,
                    Trade.user_id == user_id,
                )
            )
            .order_by(Trade.executed_at)
        )
        return list(result.scalars().all())

    async def get_trade_statistics(self, tenant_id: str, user_id: int, portfolio_id: int | None = None) -> dict:
        """Get trade statistics"""
        conditions = [Trade.tenant_id == tenant_id, Trade.user_id == user_id]
        if portfolio_id:
            conditions.append(Trade.portfolio_id == portfolio_id)

        summary_stmt = select(
            func.count(Trade.id).label("total_trades"),
            func.coalesce(func.sum(Trade.trade_value), 0.0).label("total_value"),
            func.coalesce(func.sum(Trade.commission), 0.0).label("total_commission"),
            func.coalesce(func.sum(case((Trade.side == "buy", 1), else_=0)), 0).label("buy_trades"),
            func.coalesce(func.sum(case((Trade.side == "sell", 1), else_=0)), 0).label("sell_trades"),
        ).where(and_(*conditions))
        summary_row = (await self.db.execute(summary_stmt)).one()

        day_bucket = func.date(Trade.executed_at)
        daily_stmt = (
            select(day_bucket.label("trade_day"), func.count(Trade.id).label("trade_count"))
            .where(and_(*conditions))
            .group_by(day_bucket)
            .order_by(day_bucket.asc())
        )
        daily_rows = (await self.db.execute(daily_stmt)).all()
        daily_counts = []
        for row in daily_rows:
            trade_day = row.trade_day
            if not trade_day:
                continue
            daily_counts.append(
                {
                    "timestamp": f"{trade_day.isoformat()}T00:00:00Z",
                    "value": int(row.trade_count or 0),
                    "label": "trade_count",
                }
            )

        return {
            "daily_counts": daily_counts,
            "total_trades": int(summary_row.total_trades or 0),
            "total_value": float(summary_row.total_value or 0.0),
            "total_commission": float(summary_row.total_commission or 0.0),
            "buy_trades": int(summary_row.buy_trades or 0),
            "sell_trades": int(summary_row.sell_trades or 0),
        }

    def _invalidate_trade_cache(self, user_id: int, portfolio_id: int):
        """Invalidate trade-related cache"""
        self.redis.delete(f"trades:user:{user_id}")
        self.redis.delete(f"trades:portfolio:{portfolio_id}")
