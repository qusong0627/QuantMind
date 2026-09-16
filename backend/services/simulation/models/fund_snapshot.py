from datetime import datetime

from sqlalchemy import JSON, Column, Date, DateTime, Float, Integer, String, UniqueConstraint

from backend.services.trade_shared.models.base import Base


class SimulationFundSnapshot(Base):
    __tablename__ = "simulation_fund_snapshots"
    __table_args__ = (
        # T-P1-07：市场维度 —— 'ALL'=跨市场合并行，其余为单市场行
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "snapshot_date",
            "market",
            name="uq_sim_fund_snapshot_scope_date_market",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(50), nullable=False, index=True)
    user_id = Column(String(50), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    market = Column(String(16), nullable=False, default="ALL")
    total_asset = Column(Float, nullable=False, default=0.0)
    available_balance = Column(Float, nullable=False, default=0.0)
    frozen_balance = Column(Float, nullable=False, default=0.0)
    market_value = Column(Float, nullable=False, default=0.0)
    initial_capital = Column(Float, nullable=False, default=0.0)
    total_pnl = Column(Float, nullable=False, default=0.0)
    today_pnl = Column(Float, nullable=False, default=0.0)
    source = Column(String(64), nullable=False, default="redis_simulation_account")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def total_assets(self) -> float:
        return float(self.total_asset or 0.0)

    @property
    def cash(self) -> float:
        return float(self.available_balance or 0.0)

    @property
    def data(self) -> dict[str, object]:
        return {
            "initial_capital": float(self.initial_capital or 0.0),
            "total_pnl": float(self.total_pnl or 0.0),
            "today_pnl": float(self.today_pnl or 0.0),
            "source": self.source,
        }
