from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from ..utils.metadata import normalize_text_list, sanitize_note

# 导入共享枚举
try:
    from shared.enums import (
        BacktestPeriod,
        MarketType,
        RiskLevel,
        StrategyCategory,
        StrategyLength,
        StrategyStyle,
        Timeframe,
    )
except ImportError:
    # 如果共享模块不可用，使用本地定义
    RiskLevel = Literal["low", "medium", "high"]
    MarketType = Literal["CN", "US", "HK", "GLOBAL"]
    Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
    StrategyStyle = Literal["conservative", "balanced", "aggressive", "custom"]
    StrategyLength = Literal["short_term",
                             "medium_term", "long_term", "unlimited"]
    BacktestPeriod = Literal[
        "3months", "6months", "1year", "2years", "5years", "unlimited"
    ]
    StrategyCategory = Literal[
        "trend", "mean_reversion", "momentum", "breakout", "arbitrage"
    ]


class ChatRequest(BaseModel):
    """聊天对话请求模型"""

    message: str = Field(..., description="用户消息内容")
    user_id: str | None = Field(None, description="用户ID")
    provider: str | None = Field(None, description="指定使用的provider")


class StrategyGenerationRequest(BaseModel):
    """
    策略生成请求类，用于定义生成策略所需的参数

    继承自BaseModel，使用Pydantic进行数据验证
    """

    description: str = Field(
        ..., description="自然语言策略描述"
    )  # 必填参数，用于描述策略的核心逻辑
    market: MarketType = Field("CN", description="市场标识")  # 可选参数，默认为中国市场
    risk_level: RiskLevel = Field(
        "medium", description="风险等级"
    )  # 风险等级，低、中、高三种可选，默认为中等
    user_id: str | None = Field(None, description="用户ID")  # 可选参数，用于标识用户
    provider: str | None = Field(
        None, description="指定provider"
    )  # 可选参数，用于指定策略提供者

    # 新增参数
    symbols: list[str] | None = Field(
        default_factory=list, description="股票池代码列表"
    )  # 可选参数，用于指定策略关注的股票列表
    timeframe: Timeframe = Field("1d", description="时间周期")  # 可选参数，默认为日线
    style: StrategyStyle | None = Field(
        None, description="策略风格"
    )  # 可选参数，用于描述策略的风格特征
    initial_capital: float | None = Field(
        100000, description="初始资金"
    )  # 可选参数，默认10万
    position_size: float | None = Field(
        10, description="单次仓位百分比"
    )  # 可选参数，默认10%
    max_positions: int | None = Field(
        5, description="最大持仓数量"
    )  # 可选参数，默认最多5个持仓
    stop_loss: float | None = Field(
        5, description="止损百分比"
    )  # 可选参数，默认5%止损
    take_profit: float | None = Field(
        20, description="止盈百分比"
    )  # 可选参数，默认20%止盈
    strategy_length: StrategyLength = Field(
        "unlimited", description="策略适用时间长度"
    )  # 可选参数，默认无限期
    backtest_period: BacktestPeriod = Field(
        "1year", description="回测时间范围"
    )  # 可选参数，默认1年回测期

    # 用于多轮对话和示例学习
    examples: list[str] | None = Field(
        None, description="示例代码列表"
    )  # 可选参数，用于提供示例代码
    strategy_id: str | None = Field(
        None, description="策略ID，用于多轮对话"
    )  # 可选参数，用于标识策略以便进行多轮对话

    # 新增高级参数（前端模板系统需要）
    max_drawdown: float | None = Field(
        None, ge=0, le=100, description="最大回撤百分比"
    )  # 新增：最大回撤限制
    commission_rate: float | None = Field(
        None, ge=0, le=1, description="手续费率"
    )  # 新增：手续费率（0-1）
    slippage: float | None = Field(
        None, ge=0, le=1, description="滑点"
    )  # 新增：滑点（0-1）
    benchmark: str | None = Field(None, description="基准指数")  # 新增：基准指数
    template_id: str | None = Field(
        None, description="策略模板ID"
    )  # 新增：使用的模板ID
    use_template: bool = Field(
        False, description="是否使用模板生成"
    )  # 新增：是否使用模板


class StrategyCodeArtifact(BaseModel):
    filename: str
    language: str = "python"
    code: str


class StrategyMetadata(BaseModel):
    factors: list[str] = Field(default_factory=list)
    risk_controls: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("factors", "risk_controls", "assumptions", mode="before")
    @classmethod
    def _sanitize_list_fields(cls, value):
        return normalize_text_list(value)

    @field_validator("notes", mode="before")
    @classmethod
    def _sanitize_notes(cls, value):
        return sanitize_note(value)

    @field_validator("notes")
    @classmethod
    def _truncate_notes(cls, value):
        if value and len(value) > 2000:
            return value[:2000].rstrip()
        return value


class StrategyRefineRequest(BaseModel):
    """策略完善请求模型"""

    strategy_id: str = Field(..., description="要完善的策略ID")
    feedback: str = Field(..., description="用户的完善建议")
    current_code: str = Field(..., description="当前策略代码")
    user_id: str | None = Field("desktop-user", description="用户ID")
    provider: str | None = Field(None, description="指定provider")
    # 可选的策略参数更新
    updated_params: StrategyGenerationRequest | None = Field(
        None, description="更新后的策略参数"
    )


class StrategyAnalysisRequest(BaseModel):
    """策略分析请求模型"""

    strategy_id: str = Field(..., description="要分析的策略ID")
    analysis_type: Literal["backtest", "risk", "performance", "optimization"] = Field(
        "performance", description="分析类型"
    )
    user_id: str | None = Field("desktop-user", description="用户ID")


class StrategyExecutionRequest(BaseModel):
    """策略执行请求模型"""

    strategy_id: str = Field(..., description="要执行的策略ID")
    execution_config: dict | None = Field(None, description="执行配置参数")
    user_id: str | None = Field("desktop-user", description="用户ID")


class StrategyConversionRequest(BaseModel):
    """策略转换请求模型"""

    source_code: str = Field(..., description="原始策略代码")
    source_platform: str = Field(..., description="原始平台名称")
    target_platform: str = Field("qlib", description="目标平台名称")
    user_requirements: str | None = Field(None, description="用户额外要求")
    user_id: str | None = Field("desktop-user", description="用户ID")
    provider: str | None = Field(None, description="指定使用的provider")


class PlatformDifference(BaseModel):
    """平台差异说明"""

    category: str
    source_feature: str
    target_equivalent: str
    notes: str
    manual_review_required: bool = False


class StrategyConversionResponse(BaseModel):
    """策略转换响应模型"""

    success: bool
    converted_code: str
    conversion_notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    platform_differences: list[PlatformDifference] = Field(
        default_factory=list)
    estimated_compatibility: int = 0


class StrategyGenerationResult(BaseModel):
    strategy_name: str
    rationale: str
    artifacts: list[StrategyCodeArtifact]
    metadata: StrategyMetadata = Field(default_factory=StrategyMetadata)
    provider: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# 类型别名
StrategyGenReq = StrategyGenerationRequest
StrategyGenRes = StrategyGenerationResult
