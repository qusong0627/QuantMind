"""
Trade Model
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, TimestampMixin
from .order import OrderSide, PositionSide, TradeAction, TradingMode


class Trade(Base, TimestampMixin):
    """Trade (execution) table"""

    __tablename__ = "trades"

    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )

    # Foreign keys
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    tenant_id = Column(String(64), nullable=False, default="default", index=True)
    user_id = Column(String(32), nullable=False, index=True)
    portfolio_id = Column(Integer, nullable=False, index=True)

    # Trade info
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
    trading_mode = Column(
        Enum(TradingMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TradingMode.SIMULATION,
        index=True,
    )

    # Execution details
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    trade_value = Column(Float, nullable=False)  # quantity * price
    commission = Column(Float, nullable=False, default=0.0)
    stamp_duty = Column(Float, nullable=False, default=0.0)
    transfer_fee = Column(Float, nullable=False, default=0.0)
    total_fee = Column(Float, nullable=False, default=0.0)

    # Timestamps
    executed_at = Column(DateTime, nullable=False, default=datetime.now)

    # Exchange info
    exchange_trade_id = Column(String(100), nullable=True)
    exchange_name = Column(String(50), nullable=True)

    # Remarks
    remarks = Column(String(500), nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_trade_order", "order_id"),
        Index("idx_trade_tenant_user_symbol", "tenant_id", "user_id", "symbol"),
        Index("idx_trade_user_symbol", "user_id", "symbol"),
        Index("idx_trade_portfolio", "portfolio_id", "executed_at"),
        Index("idx_trade_executed", "executed_at"),
    )

    def __repr__(self):
        return (
            f"<Trade(id={self.id}, trade_id={self.trade_id}, "
            f"symbol={self.symbol}, side={self.side}, "
            f"trade_action={self.trade_action}, position_side={self.position_side}, "
            f"quantity={self.quantity}, price={self.price})>"
        )
