"""Database models for trade portfolio module."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

from backend.services.trade.models.enums import PositionSide, TradingMode

Base = declarative_base()


class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(64), nullable=False, default="default", index=True, comment="租户ID")
    user_id = Column(String(32), nullable=False, index=True, comment="用户ID")
    name = Column(String(100), nullable=False, comment="组合名称")
    description = Column(Text, nullable=True, comment="组合描述")

    initial_capital = Column(
        Numeric(20, 2), nullable=False, default=0, comment="初始资金"
    )
    current_capital = Column(
        Numeric(20, 2), nullable=False, default=0, comment="当前资金"
    )
    available_cash = Column(
        Numeric(20, 2), nullable=False, default=0, comment="可用现金"
    )
    frozen_cash = Column(Numeric(20, 2), nullable=False,
                         default=0, comment="冻结资金")

    total_value = Column(Numeric(20, 2), nullable=False,
                         default=0, comment="总市值")
    total_pnl = Column(Numeric(20, 2), nullable=False,
                       default=0, comment="总盈亏")
    total_return = Column(Numeric(10, 4), nullable=False,
                          default=0, comment="总收益率")
    daily_pnl = Column(Numeric(20, 2), nullable=False,
                       default=0, comment="日盈亏")
    daily_return = Column(Numeric(10, 4), nullable=False,
                          default=0, comment="日收益率")
    yesterday_total_value = Column(Numeric(20, 2), nullable=False,
                                   default=0, comment="昨日结算总资产")

    max_drawdown = Column(Numeric(10, 4), nullable=False,
                          default=0, comment="最大回撤")
    sharpe_ratio = Column(Numeric(10, 4), nullable=True, comment="夏普比率")
    volatility = Column(Numeric(10, 4), nullable=True, comment="波动率")

    status = Column(String(20), nullable=False, default="active", comment="状态")

    # Completed tasks:
    # - [x] 修复 `internal_strategy.py` 中的 `time` 模块遮蔽导致的 500 错误
    # - [x] 验证实盘交易指标数据的准确性
    trading_mode = Column(
        Enum(TradingMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TradingMode.SIMULATION,
        index=True,
        comment="交易模式：实盘 / 模拟盘",
    )

    broker_type = Column(String(32), nullable=True, comment="券商类型 (如 QMT/Paper)")
    broker_account_id = Column(String(64), nullable=True, comment="券商资金账号")
    broker_params = Column(JSON, nullable=True, default={}, comment="券商配置参数")

    strategy_id = Column(Integer, nullable=True, comment="关联策略ID")
    real_trading_id = Column(String(50), nullable=True, comment="实盘引擎部署ID")
    run_status = Column(
        String(20), nullable=False, default="stopped", comment="运行状态"
    )

    is_deleted = Column(Boolean, default=False, nullable=False, comment="是否删除")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    positions = relationship(
        "Position", back_populates="portfolio", cascade="all, delete-orphan"
    )
    snapshots = relationship(
        "PortfolioSnapshot", back_populates="portfolio", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_portfolio_tenant_user_status", "tenant_id", "user_id", "status"),
        Index("idx_portfolio_user_status", "user_id", "status"),
        Index("idx_portfolio_created_at", "created_at"),
        CheckConstraint("initial_capital >= 0",
                        name="check_initial_capital_positive"),
        CheckConstraint("available_cash >= 0",
                        name="check_available_cash_positive"),
    )


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(
        Integer, ForeignKey("portfolios.id"), nullable=False, index=True
    )

    symbol = Column(String(20), nullable=False, index=True, comment="证券代码")
    symbol_name = Column(String(100), nullable=True, comment="证券名称")
    exchange = Column(String(20), nullable=True, comment="交易所")
    side = Column(String(20), nullable=False, default=PositionSide.LONG.value, comment="持仓方向")

    quantity = Column(Integer, nullable=False, default=0, comment="持仓数量")
    available_quantity = Column(
        Integer, nullable=False, default=0, comment="可用数量")
    frozen_quantity = Column(Integer, nullable=False,
                             default=0, comment="冻结数量")

    avg_cost = Column(Numeric(20, 4), nullable=False,
                      default=0, comment="平均成本")
    total_cost = Column(Numeric(20, 2), nullable=False,
                        default=0, comment="总成本")

    current_price = Column(
        Numeric(20, 4), nullable=False, default=0, comment="当前价格"
    )
    market_value = Column(Numeric(20, 2), nullable=False,
                          default=0, comment="市值")

    unrealized_pnl = Column(
        Numeric(20, 2), nullable=False, default=0, comment="浮动盈亏"
    )
    unrealized_pnl_rate = Column(
        Numeric(10, 4), nullable=False, default=0, comment="浮动盈亏率"
    )
    realized_pnl = Column(
        Numeric(20, 2), nullable=False, default=0, comment="已实现盈亏"
    )

    weight = Column(Numeric(10, 4), nullable=False, default=0, comment="仓位权重")

    status = Column(String(20), nullable=False,
                    default="holding", comment="状态")

    opened_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, comment="开仓时间"
    )
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    closed_at = Column(DateTime, nullable=True, comment="平仓时间")

    portfolio = relationship("Portfolio", back_populates="positions")
    history = relationship(
        "PositionHistory", back_populates="position", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_portfolio_symbol", "portfolio_id", "symbol"),
        Index("idx_status", "status"),
        CheckConstraint("quantity >= 0", name="check_quantity_positive"),
        CheckConstraint(
            "available_quantity >= 0", name="check_available_quantity_positive"
        ),
    )


class PositionHistory(Base):
    __tablename__ = "position_history"

    id = Column(Integer, primary_key=True, index=True)
    position_id = Column(
        Integer, ForeignKey("positions.id"), nullable=False, index=True
    )

    action = Column(String(20), nullable=False, comment="操作")
    quantity_change = Column(Integer, nullable=False, comment="数量变化")
    price = Column(Numeric(20, 4), nullable=False, comment="价格")
    amount = Column(Numeric(20, 2), nullable=False, comment="金额")

    quantity_after = Column(Integer, nullable=False, comment="变更后数量")
    avg_cost_after = Column(Numeric(20, 4), nullable=False, comment="变更后均价")

    note = Column(Text, nullable=True, comment="备注")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    position = relationship("Position", back_populates="history")

    __table_args__ = (Index("idx_pos_history_created_at", "created_at"),)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(
        Integer, ForeignKey("portfolios.id"), nullable=False, index=True
    )

    snapshot_date = Column(DateTime, nullable=False, comment="快照日期")

    total_value = Column(Numeric(20, 2), nullable=False, comment="总市值")
    available_cash = Column(Numeric(20, 2), nullable=False, comment="可用现金")
    market_value = Column(Numeric(20, 2), nullable=False, comment="持仓市值")

    total_pnl = Column(Numeric(20, 2), nullable=False, comment="总盈亏")
    total_return = Column(Numeric(10, 4), nullable=False, comment="总收益率")
    daily_pnl = Column(Numeric(20, 2), nullable=False, comment="日盈亏")
    daily_return = Column(Numeric(10, 4), nullable=False, comment="日收益率")

    max_drawdown = Column(Numeric(10, 4), nullable=False, comment="最大回撤")
    sharpe_ratio = Column(Numeric(10, 4), nullable=True, comment="夏普比率")
    volatility = Column(Numeric(10, 4), nullable=True, comment="波动率")

    position_count = Column(Integer, nullable=False, default=0, comment="持仓数量")
    is_settlement = Column(Boolean, nullable=False, default=False, comment="是否为结算快照")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    portfolio = relationship("Portfolio", back_populates="snapshots")

    __table_args__ = (
        Index("idx_portfolio_date", "portfolio_id", "snapshot_date"),
        Index("idx_snapshot_date", "snapshot_date"),
    )


__all__ = [
    "Base",
    "Portfolio",
    "Position",
    "PositionHistory",
    "PortfolioSnapshot",
]
