"""
Simulation order model.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.trade.simulation.models import Base, TimestampMixin


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TradingMode(str, enum.Enum):
    SIMULATION = "simulation"


class SimOrder(Base, TimestampMixin):
    __tablename__ = "sim_orders"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True
    )

    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    portfolio_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, default=0
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )

    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    trading_mode: Mapped[TradingMode] = mapped_column(
        Enum(TradingMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TradingMode.SIMULATION,
        index=True,
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderStatus.PENDING,
        index=True,
    )

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    average_price: Mapped[Optional[float]
                          ] = mapped_column(Float, nullable=True)

    order_value: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    filled_value: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    commission: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)

    submitted_at: Mapped[Optional[datetime]
                         ] = mapped_column(DateTime, nullable=True)
    filled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]
                         ] = mapped_column(DateTime, nullable=True)

    execution_model: Mapped[str] = mapped_column(
        String(32), nullable=False, default="synthetic_price"
    )
    price_source: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (
        Index("idx_sim_order_tenant_user_status",
              "tenant_id", "user_id", "status"),
        Index(
            "idx_sim_order_tenant_user_created", "tenant_id", "user_id", "created_at"
        ),
    )
