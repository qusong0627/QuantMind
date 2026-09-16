"""
Simulation account ledger root model.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.simulation.models import Base, TimestampMixin


class SimulationAccount(Base, TimestampMixin):
    __tablename__ = "simulation_accounts"

    account_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    # 市场化账户（2026-09-16）：id 带市场段（CN 无后缀）；本列供 SQL 过滤/对账
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="CN")
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_currency: Mapped[str] = mapped_column(
        String(16), nullable=False, default="CNY"
    )
    account_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="cash"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    initial_equity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    available_cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    frozen_cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    long_market_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    short_market_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_asset: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    liabilities: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    equity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    maintenance_margin_ratio: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_projected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "idx_simulation_accounts_tenant_user_market",
            "tenant_id",
            "user_id",
            "market",
            unique=True,
        ),
    )
