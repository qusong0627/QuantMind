"""
Simulation order model.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Enum, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.trade_shared.models.enums import _CaseInsensitiveEnum
from backend.services.simulation.models import Base, TimestampMixin
from backend.shared.utc_datetime import UtcDateTime


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


class TradingMode(_CaseInsensitiveEnum):
    SIMULATION = "SIMULATION"


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
    strategy_id: Mapped[int | None] = mapped_column(
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
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_price: Mapped[float | None
                          ] = mapped_column(Float, nullable=True)

    order_value: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    filled_value: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)
    commission: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0)

    submitted_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True)

    execution_model: Mapped[str] = mapped_column(
        String(32), nullable=False, default="synthetic_price"
    )
    price_source: Mapped[str | None] = mapped_column(
        String(64), nullable=True)
    # T-P1-03 Order 契约列：client_order_id 落台账（此前只写投影，投影为空时幂等断链）；
    # source = rebalance/manual/internal/mirror/sltp（来源分类，供对账与下钻）
    client_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # P2.7 分账契约列：这条腿是哪家模型下的。模拟台账是**意图的源头**——真单是它的
    # 镜像，镜像单的 agent 从这条行上抄（见 real_mirror_service 的 payload）。
    # 人点/风控/托管单恒 NULL。不建索引（同 trade_shared 侧口径：取值域个位数）。
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (
        Index("idx_sim_order_tenant_user_status",
              "tenant_id", "user_id", "status"),
        Index(
            "idx_sim_order_tenant_user_created", "tenant_id", "user_id", "created_at"
        ),
    )
