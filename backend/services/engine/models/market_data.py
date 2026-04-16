from sqlalchemy import Column, Date, DateTime, Float, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.shared.database import Base


class MarketDataDaily(Base):
    """
    每日市场数据表 — v10 LightGBMDirect T+1 全市场增强模型所需字段。

    字段分组：
    - 基础行情 (10): open/high/low/close/volume/amount/vwap/returns_1d/turnover_rate/adj_factor
    - 微观结构 (15): a_value/num_n/vpin/rrv/rjv/rkurt/rskew/dretwd/change_ratio/tover_os/
                     tover_tl/lrg_trd_tolbuytims/lrg_trd_tolselltims/lrg_trd_tolbuynum/lrg_trd_tolsellnum
    - 增强字段 (8):  b_volume/s_volume/market_value/retindex/clsindex/rv/bv/risk_premium1
    - 兼容字段 (1):  features JSONB（保留，向后兼容旧数据管道）
    """
    __tablename__ = "market_data_daily"

    # 复合主键
    date = Column(Date, primary_key=True, nullable=False, comment="交易日期")
    symbol = Column(String(32), primary_key=True, nullable=False, comment="股票代码 (e.g. sh600519)")

    # ── 基础行情字段 ─────────────────────────────────────────────────────────
    open          = Column(Float, nullable=True, comment="开盘价")
    high          = Column(Float, nullable=True, comment="最高价")
    low           = Column(Float, nullable=True, comment="最低价")
    close         = Column(Float, nullable=True, comment="收盘价")
    volume        = Column(Float, nullable=True, comment="成交量")
    amount        = Column(Float, nullable=True, comment="成交额")
    vwap          = Column(Float, nullable=True, comment="成交均价 (VWAP)")
    returns_1d    = Column(Float, nullable=True, comment="日收益率")
    turnover_rate = Column(Float, nullable=True, comment="换手率")
    adj_factor    = Column(Float, nullable=True, comment="复权因子")

    # ── 微观结构字段（由 tick 数据管道预计算写入）────────────────────────────
    a_value             = Column(Float, nullable=True, comment="Amihud 非流动性因子")
    num_n               = Column(Float, nullable=True, comment="成交笔数")
    vpin                = Column(Float, nullable=True, comment="VPIN（知情交易概率）")
    rrv                 = Column(Float, nullable=True, comment="实现波动率")
    rjv                 = Column(Float, nullable=True, comment="实现跳跃波动率")
    rkurt               = Column(Float, nullable=True, comment="收益率超额峰度")
    rskew               = Column(Float, nullable=True, comment="收益率偏度")
    dretwd              = Column(Float, nullable=True, comment="下行收益率天数占比")
    change_ratio        = Column(Float, nullable=True, comment="换手率变化比率")
    tover_os            = Column(Float, nullable=True, comment="机构换手率")
    tover_tl            = Column(Float, nullable=True, comment="总换手率")
    lrg_trd_tolbuytims  = Column(Float, nullable=True, comment="大单买入次数")
    lrg_trd_tolselltims = Column(Float, nullable=True, comment="大单卖出次数")
    lrg_trd_tolbuynum   = Column(Float, nullable=True, comment="大单买入总量")
    lrg_trd_tolsellnum  = Column(Float, nullable=True, comment="大单卖出总量")

    # ── 增强字段（外部行情/指数数据）────────────────────────────────────────
    b_volume      = Column(Float, nullable=True, comment="买盘成交量")
    s_volume      = Column(Float, nullable=True, comment="卖盘成交量")
    market_value  = Column(Float, nullable=True, comment="流通市值")
    retindex      = Column(Float, nullable=True, comment="当日指数收益率（全市场同值）")
    clsindex      = Column(Float, nullable=True, comment="指数收盘价")
    rv            = Column(Float, nullable=True, comment="个股实现波动率")
    bv            = Column(Float, nullable=True, comment="基准实现波动率（指数）")
    risk_premium1 = Column(Float, nullable=True, comment="风险溢价因子")

    # ── 兼容字段（保留，向后兼容旧数据管道）─────────────────────────────────
    features = Column(JSONB, nullable=True, comment="扩展特征 JSON（向后兼容）")

    # 审计字段
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="最后更新时间")

    __table_args__ = (
        Index("idx_market_data_date", "date"),
    )

    def __repr__(self):
        return f"<MarketDataDaily(date={self.date}, symbol={self.symbol})>"
