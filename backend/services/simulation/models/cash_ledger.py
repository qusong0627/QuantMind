"""
Simulation cash ledger model.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.simulation.models import Base, TimestampMixin


class SimulationCashLedger(Base, TimestampMixin):
    __tablename__ = "simulation_cash_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # T-P1-04 Ledger 契约：市场维度（历史行 NULL，按市场过滤读取时 COALESCE(market,'CN')）
    market: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ref_type: Mapped[str] = mapped_column(String(32), nullable=False, default="trade")
    ref_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    balance_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "idx_sim_cash_ledger_owner_time",
            "tenant_id",
            "user_id",
            "occurred_at",
        ),
    )
