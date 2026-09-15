"""
Simulation position lot model.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.simulation.models import Base, TimestampMixin


class SimulationPositionLot(Base, TimestampMixin):
    __tablename__ = "simulation_position_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", index=True
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # T-P1-04 Ledger 契约：市场维度（历史行 NULL，读取按 COALESCE(market,'CN') 口径）
    market: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    position_side: Mapped[str] = mapped_column(
        String(16), nullable=False, default="long", index=True
    )
    open_fill_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    open_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quantity_open: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity_remaining: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "idx_sim_position_lots_owner_symbol_side",
            "tenant_id",
            "user_id",
            "symbol",
            "position_side",
        ),
    )
