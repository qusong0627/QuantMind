"""Qlib 回测请求和响应 Schema"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


GRID_SEARCH_MAX_COMBINATIONS = 100


def count_param_values(param_min: float, param_max: float, param_step: float) -> int:
    """计算参数范围内的取值数量，避免浮点数精度问题"""
    if param_step <= 0:
        return 0
    # 使用 round 避免浮点数误差，例如 (0.3-0.1)/0.1 = 1.9999999
    return int(round((param_max - param_min) / param_step)) + 1


class QlibStrategyParams(BaseModel):
    """Qlib 策略参数"""

    topk: int = Field(50, description="选股数量", ge=5, le=200)
    short_topk: int = Field(50, description="做空选股数量", ge=0, le=200)
    n_drop: int = Field(10, description="每期调仓数量", ge=0, le=200)
    signal: str = Field("<PRED>", description="信号列名或文件路径")
    min_score: float = Field(0.0, description="权重策略最小分数阈值", ge=0.0)
    max_weight: float = Field(1.0, description="权重策略单标的最大权重", ge=0.0, le=1.0)
    long_exposure: float = Field(1.0, description="多头敞口", ge=0.0, le=3.0)
    short_exposure: float = Field(1.0, description="空头敞口", ge=0.0, le=3.0)

    # 扩展参数
    momentum_period: int = Field(20, description="动量计算周期", ge=5, le=60)
    riskmodel_root: str | None = Field(None, description="风险模型根目录")
    market: str = Field("all", description="基准权重市场代码")
    topk_sectors: int = Field(5, description="行业轮动选择行业数量", ge=2, le=15)
    lookback_days: int = Field(20, description="回看周期", ge=5, le=252)
    vol_lookback: int = Field(20, description="波动率估计回看天数", ge=5, le=120)
    stop_loss: float = Field(-0.08, description="止损线", le=-0.01, ge=-0.5)
    take_profit: float = Field(0.15, description="止盈线", ge=0.01, le=1.0)
    rebalance_days: int = Field(3, description="调仓周期(天)", ge=1, le=60)
    enable_short_selling: bool = Field(False, description="是否启用双向交易/做空")
    margin_stock_pool: str | None = Field(None, description="固定融资融券股票池标识")
    financing_rate: float = Field(0.08, description="融资年化利率", ge=0.0, le=1.0)
    borrow_rate: float = Field(0.08, description="融券年化费率", ge=0.0, le=1.0)
    max_short_exposure: float = Field(1.0, description="最大空头敞口", ge=0.0, le=3.0)
    max_leverage: float = Field(1.0, description="最大总杠杆", ge=0.0, le=5.0)
    account_stop_loss: float = Field(
        0.2,
        description="账户爆仓止损线(净值低于初始资金该比例时强制平仓并停止交易)",
        ge=0.0,
        le=0.8,
    )


class QlibBacktestRequest(BaseModel):
    """Qlib 回测请求"""

    model_config = ConfigDict(populate_by_name=True)

    # 策略配置 (支持原生 ID 和前端模板 ID)
    strategy_type: str = Field(
        "TopkDropout",
        description="策略类型 (如 TopkDropout, standard_topk, deep_time_series 等)",
    )

    strategy_params: QlibStrategyParams = Field(
        default_factory=QlibStrategyParams, description="策略参数"
    )
    strategy_content: str | None = Field(
        None,
        description="策略代码（仅用于 CustomStrategy 模式）",
    )
    model_id: str | None = Field(
        None,
        description="可选显式模型ID；当 signal='<PRED>' 时优先使用该模型的 pred.pkl",
    )
    is_third_party: bool = Field(False, description="是否为第三方/外置策略")

    # 时间范围
    start_date: str = Field(
        ..., description="开始日期 YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    end_date: str = Field(
        ..., description="结束日期 YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )

    # 回测配置
    use_vectorized: bool = Field(False, description="是否使用向量化极速回测加速")
    initial_capital: float = Field(100_000_000, description="初始资金", gt=0)
    benchmark: str = Field("SH000300", description="基准指数", alias="benchmark_symbol")
    universe: str = Field("all", description="股票池")

    # 交易成本费率（A股标准）
    commission: float = Field(0.00025, description="券商佣金费率", ge=0)
    min_commission: float = Field(5.0, description="最低佣金(元)", ge=0)
    stamp_duty: float = Field(0.0005, description="印花税率(仅卖出)", ge=0)
    transfer_fee: float = Field(0.00001, description="过户费率(仅SH)", ge=0)
    min_transfer_fee: float = Field(0.01, description="最低过户费(元)", ge=0)
    impact_cost_coefficient: float = Field(
        0.0005, description="市场冲击成本系数(滑点模型)", ge=0
    )

    buy_cost: float | None = Field(None, description="买入综合费率", alias="open_cost")
    sell_cost: float | None = Field(
        None, description="卖出综合费率", alias="close_cost"
    )

    user_id: str = Field("default", description="用户ID")
    tenant_id: str = Field("default", description="租户ID")

    # 动态仓位
    dynamic_position: bool = Field(False, description="是否启用动态仓位")
    style: str | None = Field(None, description="策略风格")
    market_state_symbol: str | None = Field(None, description="市场状态参考指数代码")
    market_state_window: int = Field(
        20, description="市场状态滚动窗口（交易日）", ge=5, le=240
    )
    strategy_total_position: float = Field(
        1.0, description="策略总资金占比", ge=0.0, le=1.0
    )

    seed: int | None = Field(None, description="随机种子")
    # NOTE: deal_price="close" 会使用当日收盘价成交。
    # 若信号包含当日收盘价因子（如 $close），则存在前视偏差（Look-ahead Bias），
    # 会高估策略收益。生产环境建议改用 deal_price="open" 以避免偏差。
    deal_price: Literal["open", "close"] = Field("close", description="成交价格类型")
    signal_lag_days: int = Field(
        1,
        description="信号生效滞后交易日数；默认 T 日信号在 T+1 生效，避免同日收盘信号同日成交",
        ge=0,
        le=5,
    )
    allow_feature_signal_fallback: bool = Field(
        False,
        description="是否允许预测信号缺失时回退到行情特征信号；默认禁止静默回退到 $close",
    )

    # 无风险利率（年化），用于 Sharpe Ratio 和 CAPM Alpha 计算
    # 中国常用参考：银行一年期存款利率约 1.5%，货币基金约 2%
    risk_free_rate: float = Field(
        0.02, description="无风险利率（年化，用于Sharpe/Alpha计算）", ge=0.0, le=0.2
    )

    backtest_id: str | None = Field(
        None, description="回测任务ID（异步模式由服务端注入并透传）"
    )
    strategy_id: str | None = Field(
        None, description="关联的策略ID (用于持久化保存和AI修复覆盖)"
    )
    history_source: Literal["manual", "optimization"] = Field(
        "manual",
        description="历史来源标记：manual=普通回测，optimization=参数优化子任务",
    )


class QlibPortfolioMetrics(BaseModel):
    """Qlib Portfolio 指标"""

    final_value: float | None = None
    account: float | None = None
    position_value: float | None = None


class RebalanceInstruction(BaseModel):
    """调仓指令建议"""

    symbol: str
    action: Literal["buy", "sell", "hold"]
    current_weight: float
    target_weight: float
    weight_diff: float
    estimated_amount: float | None = None


class QlibBacktestResult(BaseModel):
    """Qlib 回测结果"""

    backtest_id: str
    user_id: str | None = None
    tenant_id: str = "default"
    status: str = "completed"
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    progress: float | None = None
    config: dict[str, Any] | None = None
    task_id: str | None = None

    annual_return: float | None = 0.0
    sharpe_ratio: float | None = 0.0
    max_drawdown: float | None = 0.0
    alpha: float | None = None

    total_return: float | None = None
    volatility: float | None = None
    beta: float | None = None
    information_ratio: float | None = None
    benchmark_return: float | None = None
    benchmark_symbol: str | None = None

    total_trades: int | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    avg_holding_period: float | None = None

    portfolio_metrics: QlibPortfolioMetrics | None = None
    equity_curve: list[dict[str, Any]] | None = None
    drawdown_curve: list[dict[str, Any]] | None = None
    trades: list[dict[str, Any]] | None = None
    positions: list[dict[str, Any]] | None = None

    factor_metrics: dict[str, Any] | None = None
    stratified_returns: list[dict[str, Any]] | None = None
    style_attribution: dict[str, Any] | None = None
    rebalance_suggestions: list[RebalanceInstruction] | None = None
    advanced_stats: dict[str, Any] | None = None

    execution_time: float | None = None
    error_message: str | None = None
    full_error: str | None = None


class HealthCheckResponse(BaseModel):
    """Qlib 健康检查响应"""

    status: str = Field(..., description="服务状态")
    qlib_initialized: bool = Field(False, description="Qlib 是否已初始化")
    version: str = Field("unknown", description="Qlib 版本")
    data_available: bool = Field(False, description="数据源是否可用")
    qlib_backend: str = Field("unknown", description="Qlib 后端(real/mock/error)")
    db_ok: bool = Field(False, description="数据库连通性")
    redis_ok: bool = Field(False, description="Redis 连通性")
    error: str | None = Field(None, description="异常信息")


class OptimizationTaskResponse(BaseModel):
    """异步优化任务提交响应"""

    optimization_id: str
    task_id: str
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.now)


class OptimizationParamRange(BaseModel):
    """优化参数搜索区间"""

    name: str
    min: float
    max: float
    step: float = Field(..., gt=0)


class OptimizationTaskResult(BaseModel):
    """单次参数组合回测结果"""

    params: dict[str, float]
    metrics: QlibBacktestResult


class QlibOptimizationRequest(BaseModel):
    """网格参数优化请求"""

    base_request: QlibBacktestRequest
    param_ranges: list[OptimizationParamRange]
    optimization_target: str = Field("sharpe_ratio")
    max_parallel: int = Field(4, ge=1, le=32)

    def total_combinations(self) -> int:
        total_combinations = 1
        for param in self.param_ranges:
            count = count_param_values(param.min, param.max, param.step)
            if count <= 0:
                raise ValueError(f"参数 {param.name} 未生成有效取值")
            total_combinations *= count
        return total_combinations

    @model_validator(mode="after")
    def validate_total_combinations(self) -> "QlibOptimizationRequest":
        total_combinations = self.total_combinations()
        if total_combinations > GRID_SEARCH_MAX_COMBINATIONS:
            raise ValueError(
                f"组合数过多 ({total_combinations})，参数优化最多允许{GRID_SEARCH_MAX_COMBINATIONS}组"
            )

        return self


class QlibOptimizationResult(BaseModel):
    """网格参数优化结果"""

    optimization_id: str
    best_params: dict[str, Any]
    all_results: list[OptimizationTaskResult]
    target_metric: str
    execution_time: float


class OptimizationProgressInfo(BaseModel):
    """优化任务运行中的进度信息"""

    optimization_id: str | None = None
    progress: float = 0.0
    status: str = "pending"
    message: str | None = None
    total_tasks: int = 0
    completed_count: int = 0
    failed_count: int = 0
    current_params: dict[str, Any] | None = None
    best_params: dict[str, Any] | None = None
    best_metric_value: float | None = None


class OptimizationHistoryItem(BaseModel):
    """优化历史摘要"""

    optimization_id: str
    task_id: str | None = None
    mode: str = "grid_search"
    user_id: str | None = None
    tenant_id: str = "default"
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    optimization_target: str | None = None
    total_tasks: int = 0
    completed_count: int = 0
    failed_count: int = 0
    current_params: dict[str, Any] | None = None
    best_params: dict[str, Any] | None = None
    best_metric_value: float | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    can_apply: bool = False


class OptimizationHistoryDetail(OptimizationHistoryItem):
    """优化历史详情"""

    base_request: dict[str, Any] = Field(default_factory=dict)
    param_ranges: list[dict[str, Any]] = Field(default_factory=list)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    all_results: list[dict[str, Any]] = Field(default_factory=list)


class GeneticHistoryRecord(BaseModel):
    """遗传算法代际统计"""

    generation: int
    max_fitness: float
    avg_fitness: float
    std_fitness: float


class QlibGeneticOptimizationRequest(BaseModel):
    """遗传算法优化请求"""

    optimization_id: str
    base_request: QlibBacktestRequest
    param_ranges: list[OptimizationParamRange]
    optimization_target: str = Field("sharpe_ratio")
    population_size: int = Field(20, ge=2, le=500)
    generations: int = Field(10, ge=1, le=500)
    mutation_rate: float = Field(0.2, ge=0.0, le=1.0)
    crossover_rate: float = Field(0.8, ge=0.0, le=1.0)
    tournament_size: int = Field(3, ge=2, le=32)
    max_parallel: int = Field(4, ge=1, le=32)


class QlibGeneticOptimizationResult(BaseModel):
    """遗传算法优化结果"""

    optimization_id: str
    best_params: dict[str, Any]
    best_fitness: float
    history: list[GeneticHistoryRecord]
    execution_time: float


class QlibAIFixRequest(BaseModel):
    """Qlib AI 策略修复请求"""

    backtest_id: str = Field(..., description="相关联的回测ID")
    error_message: str | None = Field(None, description="简短错误信息")
    full_error: str | None = Field(None, description="完整堆栈跟踪")


class QlibAIFixResponse(BaseModel):
    """Qlib AI 策略修复响应"""

    success: bool
    repaired_code: str | None = None
    strategy_id: str | None = None
    message: str = ""
