from sqlalchemy import JSON, Column, DateTime, Float, Integer, String

from backend.services.trade.models.base import Base


class SimulationFundSnapshot(Base):
    __tablename__ = "simulation_fund_snapshots"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(50), index=True)
    user_id = Column(String(50), index=True)
    account_id = Column(String(50), index=True)
    snapshot_date = Column(DateTime)
    total_assets = Column(Float, default=0.0)
    cash = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    data = Column(JSON)
