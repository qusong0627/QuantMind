"""
高级分析相关的数据模型

定义请求和响应的Pydantic模型
"""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# ============================================================================
# 基础风险指标
# ============================================================================


class BasicRiskRequest(BaseModel):
    """基础风险分析请求"""

    backtest_id: str = Field(..., description="回测ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")


class BasicRiskMetrics(BaseModel):
    """基础风险指标"""

    # Qlib原生指标
    annualized_return: float = Field(..., description="年化收益率")
    total_return: float = Field(..., description="累计收益率")
    volatility: float = Field(..., description="年化波动率")
    max_drawdown: float = Field(..., description="最大回撤")

    # 风险调整收益指标
    sharpe_ratio: float = Field(..., description="夏普比率")
    calmar_ratio: float = Field(..., description="Calmar比率")
    sortino_ratio: float = Field(..., description="Sortino比率")

    # 高级风险指标
    var_95: float = Field(..., description="95%置信度VaR")
    cvar_95: float = Field(..., description="95%置信度CVaR（条件VaR）")
    skewness: float = Field(..., description="偏度（收益分布对称性）")
    kurtosis: float = Field(..., description="峰度（极端值频率）")
    omega_ratio: float = Field(..., description="Omega比率（收益/损失比）")

    # 统计指标
    positive_days_pct: float = Field(..., description="盈利天数占比")
    best_day_return: float = Field(..., description="最佳单日收益")
    worst_day_return: float = Field(..., description="最差单日收益")


class TimeSeriesData(BaseModel):
    """时间序列数据"""

    dates: list[str] = Field(..., description="日期列表")
    values: list[float] = Field(..., description="数值列表")


class HistogramData(BaseModel):
    """直方图数据"""

    bins: list[float] = Field(..., description="区间边界")
    counts: list[int] = Field(..., description="频数")


class BasicRiskResponse(BaseModel):
    """基础风险分析响应"""

    metrics: BasicRiskMetrics = Field(..., description="风险指标")

    # 时间序列
    daily_returns: TimeSeriesData = Field(..., description="每日收益率")
    cumulative_returns: TimeSeriesData = Field(..., description="累计收益曲线")
    drawdown: TimeSeriesData = Field(..., description="回撤曲线")

    # 分布数据
    returns_distribution: HistogramData = Field(..., description="收益率分布")

    # 元数据
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")
    data_points: int = Field(..., description="数据点数量")


# ============================================================================
# 绩效分析
# ============================================================================


class PerformanceRequest(BaseModel):
    """绩效分析请求"""

    backtest_id: str = Field(..., description="回测ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")
    rolling_window: int | None = Field(30, description="滚动窗口大小（天）")


class MonthlyReturn(BaseModel):
    """月度收益"""

    year: int
    month: int
    return_pct: float
    trading_days: int


class PercentileData(BaseModel):
    """分位数数据"""

    p01: float = Field(..., description="1%分位数")
    p05: float = Field(..., description="5%分位数")
    p25: float = Field(..., description="25%分位数")
    p50: float = Field(..., description="中位数")
    p75: float = Field(..., description="75%分位数")
    p95: float = Field(..., description="95%分位数")
    p99: float = Field(..., description="99%分位数")


class PerformanceResponse(BaseModel):
    """绩效分析响应"""

    # 时间维度分析
    monthly_returns: list[MonthlyReturn] = Field(..., description="月度收益")
    quarterly_returns: dict[str, float] = Field(..., description="季度收益")
    yearly_return: float = Field(..., description="年度收益")

    # 滚动指标
    rolling_sharpe: TimeSeriesData = Field(..., description="滚动夏普比率")
    rolling_volatility: TimeSeriesData = Field(..., description="滚动波动率")
    rolling_return: TimeSeriesData = Field(..., description="滚动收益")

    # 收益分布
    return_percentiles: PercentileData = Field(..., description="收益率分位数")

    # 元数据
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")
    rolling_window: int = Field(..., description="滚动窗口大小")


# ============================================================================
# 交易统计（待实现）
# ============================================================================


class TradeStatsRequest(BaseModel):
    """交易统计分析请求"""

    backtest_id: str = Field(..., description="回测ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")


class TradeStatsMetrics(BaseModel):
    """交易统计指标"""

    win_rate: float = Field(..., description="胜率")
    profit_loss_ratio: float = Field(..., description="盈亏比")
    profit_loss_days_ratio: float = Field(..., description="盈亏天数比（盈利交易日/亏损交易日）")
    avg_holding_days: float = Field(..., description="平均持仓天数")
    trade_frequency: float = Field(..., description="交易频率（每月）")
    total_trades: int = Field(..., description="总交易次数")


class TradeStatsResponse(BaseModel):
    """交易统计响应"""

    metrics: TradeStatsMetrics = Field(..., description="交易统计指标")
    pnl_distribution: HistogramData = Field(..., description="单笔盈亏分布")
    holding_days_distribution: HistogramData = Field(..., description="持仓天数分布")
    trade_frequency_series: TimeSeriesData = Field(..., description="交易频率序列")
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")


# ============================================================================
# 基准对比（待实现）
# ============================================================================


class BenchmarkComparisonRequest(BaseModel):
    """基准对比请求"""

    backtest_id: str = Field(..., description="回测ID")
    benchmark_id: str = Field(..., description="基准ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")


class BenchmarkMetrics(BaseModel):
    """基准对比指标"""

    excess_return: float = Field(..., description="超额收益")
    beta: float = Field(..., description="Beta系数")
    alpha: float | None = Field(None, description="Alpha")
    tracking_error: float = Field(..., description="跟踪误差")
    upside_capture: float = Field(..., description="上行捕获比")
    downside_capture: float = Field(..., description="下行捕获比")
    correlation: float = Field(..., description="相关系数")


class BenchmarkComparisonResponse(BaseModel):
    """基准对比响应"""

    metrics: BenchmarkMetrics = Field(..., description="基准对比指标")
    strategy_returns: TimeSeriesData = Field(..., description="策略累计收益")
    benchmark_returns: TimeSeriesData = Field(..., description="基准累计收益")
    excess_returns: TimeSeriesData = Field(..., description="超额收益")
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")
    benchmark_id: str = Field(..., description="基准ID")


# ============================================================================
# 持仓分析（待实现）
# ============================================================================


class PositionAnalysisRequest(BaseModel):
    """持仓分析请求"""

    backtest_id: str = Field(..., description="回测ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")


class PositionSummary(BaseModel):
    """持仓摘要"""

    symbol: str = Field(..., description="标的代码")
    name: str | None = Field(None, description="标的名称")
    weight: float = Field(..., description="持仓权重")
    sector: str | None = Field(None, description="行业")


class SectorAllocation(BaseModel):
    """行业配置"""

    sector: str = Field(..., description="行业")
    weight: float = Field(..., description="权重")
    contribution: float | None = Field(None, description="收益贡献")


class PositionAnalysisResponse(BaseModel):
    """持仓分析响应"""

    top_holdings: list[PositionSummary] = Field(..., description="Top持仓")
    sector_allocations: list[SectorAllocation] = Field(..., description="行业配置")
    concentration_hhi: float = Field(..., description="集中度HHI")
    holdings_count: int = Field(..., description="持仓数量")
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")


# ============================================================================
# 因子分析
# ============================================================================


class FactorAnalysisRequest(BaseModel):
    """因子/信号质量分析请求"""

    backtest_id: str = Field(..., description="回测ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")
    n_groups: int = Field(5, description="分层数量", ge=2, le=10)


class StratifiedGroupReturn(BaseModel):
    """分层收益数据"""

    group: int = Field(..., description="分组编号（1=最低分，n=最高分）")
    avg_return: float = Field(..., description="日均收益率")
    total_return: float = Field(..., description="累计收益率")
    volatility: float = Field(..., description="年化波动率")


class FactorAnalysisResponse(BaseModel):
    """因子分析响应"""

    rank_ic: float | None = Field(None, description="平均 Rank IC")
    rank_ic_std: float | None = Field(None, description="Rank IC 标准差")
    icir: float | None = Field(None, description="ICIR（IC 信息比率）")
    stratified_returns: list[StratifiedGroupReturn] = Field(default_factory=list, description="分层收益（多空分层）")
    data_available: bool = Field(..., description="是否有足够数据进行因子分析")
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")


# ============================================================================
# 风格归因
# ============================================================================


class StyleAttributionRequest(BaseModel):
    """风格归因分析请求"""

    backtest_id: str = Field(..., description="回测ID")
    user_id: str = Field(..., description="用户ID")
    tenant_id: str = Field("default", description="租户ID")
    benchmark: str = Field("SH000300", description="基准指数代码")


class StyleFactorExposure(BaseModel):
    """单个风格因子暴露"""

    factor: str = Field(..., description="因子名称")
    portfolio: float = Field(..., description="组合暴露（标准化）")
    benchmark: float = Field(..., description="基准暴露（标准化）")
    active: float = Field(..., description="主动暴露（组合-基准）")


class StyleAttributionResponse(BaseModel):
    """风格归因响应"""

    factors: list[StyleFactorExposure] = Field(default_factory=list, description="各风格因子暴露明细")
    analysis_date: str | None = Field(None, description="分析截面日期")
    data_available: bool = Field(..., description="是否有足够数据进行风格归因")
    analyzed_at: datetime = Field(default_factory=datetime.now, description="分析时间")
