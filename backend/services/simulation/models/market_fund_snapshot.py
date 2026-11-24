from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, Integer, String, UniqueConstraint

from backend.services.trade_shared.models.base import Base


class SimulationMarketFundSnapshot(Base):
    """分市场模拟资金日快照（智能图表按市场取每日收益率曲线）。

    与用户级 ``simulation_fund_snapshots`` 分开存：后者是跨市场合并口径，
    基线/EOD 都依赖它，加市场维度会破坏既有语义。本表只做展示数据源。
    """

    __tablename__ = "simulation_market_fund_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "market",
            "snapshot_date",
            name="uq_sim_market_fund_scope_date",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    market = Column(String(16), nullable=False, default="CN", index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    total_asset = Column(Float, nullable=False, default=0.0)
    available_balance = Column(Float, nullable=False, default=0.0)
    frozen_balance = Column(Float, nullable=False, default=0.0)
    market_value = Column(Float, nullable=False, default=0.0)
    initial_capital = Column(Float, nullable=False, default=0.0)
    total_pnl = Column(Float, nullable=False, default=0.0)
    today_pnl = Column(Float, nullable=False, default=0.0)
    source = Column(String(64), nullable=False, default="redis_simulation_account")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
