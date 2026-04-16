"""
Simulation trade model.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.trade.simulation.models import Base, TimestampMixin
from backend.services.trade.simulation.models.order import OrderSide, TradingMode


class SimTrade(Base, TimestampMixin):
    __tablename__ = "sim_trades"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sim_orders.order_id"),
        nullable=False,
        index=True,
    )

    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    portfolio_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, default=0
    )

    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    trading_mode: Mapped[TradingMode] = mapped_column(
        Enum(TradingMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TradingMode.SIMULATION,
        index=True,
    )

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    trade_value: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    stamp_duty: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    transfer_fee: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    total_fee: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    price_source: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True)

    __table_args__ = (
        Index("idx_sim_trade_tenant_user_symbol",
              "tenant_id", "user_id", "symbol"),
        Index("idx_sim_trade_order", "order_id"),
    )
