"""
Order Model
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, TimestampMixin
from .enums import (  # noqa: F401 (re-exported)
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TradeAction,
    TradingMode,
)


class Order(Base, TimestampMixin):
    """Order table"""

    __tablename__ = "orders"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )

    # Foreign keys
    tenant_id = Column(String(64), nullable=False, default="default", index=True)
    user_id = Column(String(32), nullable=False, index=True)
    portfolio_id = Column(Integer, nullable=False, index=True)
    strategy_id = Column(Integer, nullable=True, index=True)

    # Order info
    symbol = Column(String(20), nullable=False, index=True)
    symbol_name = Column(String(50), nullable=True)
    side = Column(
        Enum(OrderSide, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    trade_action = Column(
        Enum(TradeAction, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
        index=True,
    )
    position_side = Column(
        Enum(PositionSide, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=PositionSide.LONG,
        index=True,
    )
    is_margin_trade = Column(Boolean, nullable=False, default=False)
    order_type = Column(
        Enum(OrderType, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    trading_mode = Column(
        Enum(TradingMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TradingMode.SIMULATION,
        index=True,
    )
    status = Column(
        Enum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderStatus.PENDING,
        index=True,
    )

    # Quantity and price
    quantity = Column(Float, nullable=False)
    filled_quantity = Column(Float, nullable=False, default=0.0)
    price = Column(Float, nullable=True)  # For limit orders
    stop_price = Column(Float, nullable=True)  # For stop orders
    average_price = Column(Float, nullable=True)  # Average fill price

    # Amounts
    order_value = Column(Float, nullable=False)  # quantity * price
    filled_value = Column(Float, nullable=False, default=0.0)
    commission = Column(Float, nullable=False, default=0.0)

    # Timestamps
    submitted_at = Column(DateTime, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    expired_at = Column(DateTime, nullable=True)

    # Additional info
    client_order_id = Column(String(100), nullable=True, unique=True)
    exchange_order_id = Column(String(100), nullable=True)
    remarks = Column(String(500), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    # Indexes
    __table_args__ = (
        Index("idx_order_tenant_user_status", "tenant_id", "user_id", "status"),
        Index("idx_order_user_status", "user_id", "status"),
        Index("idx_order_portfolio_symbol", "portfolio_id", "symbol"),
        Index("idx_order_created", "created_at"),
    )

    def __repr__(self):
        return (
            f"<Order(id={self.id}, order_id={self.order_id}, "
            f"symbol={self.symbol}, side={self.side}, "
            f"trade_action={self.trade_action}, position_side={self.position_side}, "
            f"quantity={self.quantity}, status={self.status})>"
        )
