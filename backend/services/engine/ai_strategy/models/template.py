"""
策略模板模型
与前端模板系统保持一致
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# 导入共享枚举
try:
    from shared.enums import (
        ComponentType,
        MarketType,
        RiskLevel,
        StrategyCategory,
        Timeframe,
    )
except ImportError:
    # 如果共享模块不可用，使用本地定义
    StrategyCategory = Literal[
        "trend", "mean_reversion", "momentum", "breakout", "arbitrage"
    ]
    MarketType = Literal["CN", "US", "HK", "GLOBAL"]
    Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
    RiskLevel = Literal["low", "medium", "high"]
    ComponentType = Literal["DATA_HANDLING",
                            "LOGIC", "RISK_CONTROL", "OPTIMIZATION"]


class StrategyComponent(BaseModel):
    """策略组件"""

    type: ComponentType = Field(..., description="组件类型")
    name: str = Field(..., description="组件名称")
    description: str = Field(..., description="组件描述")
    required: bool = Field(True, description="是否必需")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="组件参数")


class ValidationRule(BaseModel):
    """验证规则"""

    field: str = Field(..., description="字段名")
    rule: str = Field(..., description="验证规则")
    message: str = Field(..., description="错误消息")


class TemplateMetadata(BaseModel):
    """模板元数据"""

    complexity: Literal["low", "medium",
                        "high"] = Field(..., description="复杂度")
    estimated_backtest_time: str = Field(..., description="预计回测时间")
    dependencies: list[str] = Field(default_factory=list, description="依赖项")
    performance: dict[str, str] = Field(..., description="性能指标")


class StrategyTemplate(BaseModel):
    """策略模板"""

    id: str = Field(..., description="模板ID")
    name: str = Field(..., description="模板名称")
    category: StrategyCategory = Field(..., description="策略类别")
    description: str = Field(..., description="模板描述")
    version: str = Field("1.0.0", description="模板版本")
    author: str = Field("QuantMind Team", description="作者")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="创建时间"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, description="更新时间"
    )

    # 模板特性
    tags: list[str] = Field(default_factory=list, description="标签")
    suitable_markets: list[MarketType] = Field(..., description="适用市场")
    suitable_timeframes: list[Timeframe] = Field(..., description="适用时间框架")
    suitable_risk_levels: list[RiskLevel] = Field(..., description="适用风险等级")
    min_capital: int = Field(..., description="最少资金")
    max_symbols: int = Field(..., description="最大股票数")

    # 必需组件
    required_components: list[StrategyComponent] = Field(
        ..., description="必需组件")

    # 默认参数
    default_parameters: dict[str, Any] = Field(
        default_factory=dict, description="默认参数"
    )

    # 验证规则
    validation_rules: list[ValidationRule] = Field(
        default_factory=list, description="验证规则"
    )

    # 模板元数据
    metadata: TemplateMetadata = Field(..., description="元数据")


class TemplateMatch(BaseModel):
    """模板匹配结果"""

    template: StrategyTemplate = Field(..., description="匹配的模板")
    confidence: float = Field(..., ge=0, le=1, description="匹配置信度")
    reason: str = Field(..., description="匹配原因")
    adaptations: list[str] = Field(default_factory=list, description="适配建议")
    score: float = Field(..., ge=0, le=1, description="匹配分数")
    match_factors: dict[str, float] = Field(
        default_factory=dict, description="匹配因子"
    )


class TemplateSearchFilter(BaseModel):
    """模板搜索过滤器"""

    category: StrategyCategory | None = None
    risk_level: RiskLevel | None = None
    market: MarketType | None = None
    timeframe: Timeframe | None = None
    tags: list[str] | None = None
    min_capital: int | None = None
    max_symbols: int | None = None
    complexity: Literal["low", "medium", "high"] | None = None


class TemplateSearchResult(BaseModel):
    """模板搜索结果"""

    templates: list[StrategyTemplate] = Field(..., description="模板列表")
    total: int = Field(..., description="总数")
    page: int = Field(..., description="页码")
    page_size: int = Field(..., description="页大小")
    total_pages: int = Field(..., description="总页数")
    filters: TemplateSearchFilter = Field(..., description="搜索过滤器")
    sort: str = Field(..., description="排序方式")
    search_time: int = Field(..., description="搜索耗时(ms)")


class TemplateMatchRequest(BaseModel):
    """模板匹配请求"""

    user_params: dict[str, Any] = Field(..., description="用户参数")
    user_description: str | None = Field(None, description="用户描述")
    max_results: int = Field(5, ge=1, le=20, description="最大结果数")
    min_confidence: float = Field(0.3, ge=0, le=1, description="最小置信度")


class TemplateMatchResponse(BaseModel):
    """模板匹配响应"""

    success: bool = Field(..., description="是否成功")
    matches: list[TemplateMatch] = Field(..., description="匹配结果")
    total_matches: int = Field(..., description="匹配总数")
    processing_time: int = Field(..., description="处理时间(ms)")
    suggestions: list[str] = Field(default_factory=list, description="建议")


# 预定义的模板库
BUILTIN_TEMPLATES = [
    # 双均线策略
    StrategyTemplate(
        id="dual_ma_crossover",
        name="双均线交叉策略",
        category="trend",
        description="基于快慢均线交叉的趋势跟踪策略",
        tags=["trend", "ma", "crossover", "beginner"],
        suitable_markets=["CN", "US"],
        suitable_timeframes=["1d", "1w"],
        suitable_risk_levels=["low", "medium"],
        min_capital=10000,
        max_symbols=5,
        required_components=[
            StrategyComponent(
                type="DATA_HANDLING",
                name="MarketDataProcessor",
                description="市场数据处理器",
                parameters={"markets": ["CN"],
                            "dataSources": ["YahooFinance"]},
            ),
            StrategyComponent(
                type="LOGIC",
                name="DualMABacktester",
                description="双均线回测逻辑",
                parameters={"fastPeriod": 10, "slowPeriod": 30},
            ),
            StrategyComponent(
                type="RISK_CONTROL",
                name="BasicRiskManager",
                description="基础风险管理器",
                parameters={"stopLoss": 5, "takeProfit": 15},
            ),
        ],
        default_parameters={
            "description": "双均线交叉策略",
            "market": "CN",
            "timeframe": "1d",
            "risk_level": "medium",
            "fast_ma": 10,
            "slow_ma": 30,
        },
        validation_rules=[
            ValidationRule(
                field="fast_ma",
                rule="required|min:5|max:50",
                message="快线周期必须在5-50之间",
            ),
            ValidationRule(
                field="slow_ma",
                rule="required|min:10|max:200",
                message="慢线周期必须在10-200之间",
            ),
        ],
        metadata=TemplateMetadata(
            complexity="low",
            estimated_backtest_time="5-10分钟",
            dependencies=["pandas", "talib", "numpy"],
            performance={
                "expectedReturn": "15-25%",
                "maxDrawdown": "10-20%",
                "sharpeRatio": "0.8-1.2",
            },
        ),
    ),
    # MACD策略
    StrategyTemplate(
        id="macd_strategy",
        name="MACD趋势策略",
        category="trend",
        description="基于MACD指标的趋势跟踪策略",
        tags=["trend", "macd", "momentum", "intermediate"],
        suitable_markets=["CN", "US", "HK"],
        suitable_timeframes=["1d", "4h"],
        suitable_risk_levels=["low", "medium"],
        min_capital=15000,
        max_symbols=8,
        required_components=[
            StrategyComponent(
                type="DATA_HANDLING",
                name="MarketDataProcessor",
                description="市场数据处理器",
                parameters={"markets": ["CN"],
                            "dataSources": ["YahooFinance"]},
            ),
            StrategyComponent(
                type="LOGIC",
                name="MACDBacktester",
                description="MACD回测逻辑",
                parameters={"fastPeriod": 12,
                            "slowPeriod": 26, "signalPeriod": 9},
            ),
            StrategyComponent(
                type="RISK_CONTROL",
                name="BasicRiskManager",
                description="基础风险管理器",
                parameters={"stopLoss": 8, "takeProfit": 20},
            ),
        ],
        default_parameters={
            "description": "MACD趋势策略",
            "market": "CN",
            "timeframe": "1d",
            "risk_level": "medium",
            "fast_period": 12,
            "slow_period": 26,
            "signal_period": 9,
        },
        validation_rules=[
            ValidationRule(
                field="fast_period",
                rule="required|min:5|max:50",
                message="快线周期必须在5-50之间",
            ),
            ValidationRule(
                field="slow_period",
                rule="required|min:10|max:200",
                message="慢线周期必须在10-200之间",
            ),
            ValidationRule(
                field="signal_period",
                rule="required|min:5|max:20",
                message="信号线周期必须在5-20之间",
            ),
        ],
        metadata=TemplateMetadata(
            complexity="medium",
            estimated_backtest_time="10-15分钟",
            dependencies=["pandas", "talib", "numpy", "scipy"],
            performance={
                "expectedReturn": "20-35%",
                "maxDrawdown": "15-25%",
                "sharpeRatio": "1.0-1.5",
            },
        ),
    ),
    # 布林带策略
    StrategyTemplate(
        id="bollinger_bands",
        name="布林带均值回归策略",
        category="mean_reversion",
        description="基于布林带的均值回归策略",
        tags=["mean_reversion", "volatility", "bollinger", "intermediate"],
        suitable_markets=["CN", "US"],
        suitable_timeframes=["1h", "4h", "1d"],
        suitable_risk_levels=["medium"],
        min_capital=20000,
        max_symbols=6,
        required_components=[
            StrategyComponent(
                type="DATA_HANDLING",
                name="MarketDataProcessor",
                description="市场数据处理器",
                parameters={"markets": ["CN"],
                            "dataSources": ["YahooFinance"]},
            ),
            StrategyComponent(
                type="LOGIC",
                name="BollingerBandsBacktester",
                description="布林带回测逻辑",
                parameters={"period": 20, "stdDev": 2},
            ),
            StrategyComponent(
                type="RISK_CONTROL",
                name="BasicRiskManager",
                description="基础风险管理器",
                parameters={"stopLoss": 8, "takeProfit": 12},
            ),
        ],
        default_parameters={
            "description": "布林带均值回归策略",
            "market": "CN",
            "timeframe": "4h",
            "risk_level": "medium",
            "period": 20,
            "std_dev": 2,
        },
        validation_rules=[
            ValidationRule(
                field="period",
                rule="required|min:10|max:100",
                message="周期必须在10-100之间",
            ),
            ValidationRule(
                field="std_dev",
                rule="required|min:1.5|max:3",
                message="标准差倍数必须在1.5-3之间",
            ),
        ],
        metadata=TemplateMetadata(
            complexity="medium",
            estimated_backtest_time="10-20分钟",
            dependencies=["pandas", "talib", "numpy", "scipy"],
            performance={
                "expectedReturn": "18-30%",
                "maxDrawdown": "12-20%",
                "sharpeRatio": "0.9-1.3",
            },
        ),
    ),
    # 激进版TopK策略
    StrategyTemplate(
        id="aggressive_topk_strategy",
        name="激进版截面TopK策略",
        category="momentum",
        description="基于机器学习预测分数的激进版TopK动量轮动策略，零容忍度每日极致换仓",
        tags=["momentum", "machine_learning", "high_frequency", "aggressive"],
        suitable_markets=["CN"],
        suitable_timeframes=["1d"],
        suitable_risk_levels=["high"],
        min_capital=1000000,
        max_symbols=50,
        required_components=[
            StrategyComponent(
                type="DATA_HANDLING",
                name="QlibDataProcessor",
                description="Qlib高级数据处理器",
                parameters={"markets": ["CN"], "dataSources": ["Qlib"]},
            ),
            StrategyComponent(
                type="LOGIC",
                name="TopkDropoutStrategy",
                description="TopK丢弃轮动逻辑",
                parameters={"topk": 50, "n_drop": 0},
            ),
            StrategyComponent(
                type="RISK_CONTROL",
                name="WeightStrategyManager",
                description="等权全仓风控管理器",
                parameters={"risk_degree": 0.95},
            ),
        ],
        default_parameters={
            "description": "激进版截面TopK策略",
            "market": "CN",
            "timeframe": "1d",
            "risk_level": "high",
            "topk": 50,
            "n_drop": 0,
        },
        validation_rules=[
            ValidationRule(
                field="topk",
                rule="required|min:10|max:100",
                message="持仓数量限制必须在10-100之间",
            ),
            ValidationRule(
                field="n_drop",
                rule="required|min:0|max:10",
                message="缓冲换仓数量必须在0-10之间，0表示激进换仓",
            ),
        ],
        metadata=TemplateMetadata(
            complexity="high",
            estimated_backtest_time="1-3分钟",
            dependencies=["qlib", "pandas", "numpy", "lightgbm"],
            performance={
                "expectedReturn": "70-160%",
                "maxDrawdown": "30-40%",
                "sharpeRatio": "2.0-3.5",
            },
        ),
    ),
]


# 模板查找函数
def get_template_by_id(template_id: str) -> StrategyTemplate | None:
    """根据ID获取模板"""
    for template in BUILTIN_TEMPLATES:
        if template.id == template_id:
            return template
    return None


def get_templates_by_category(category: StrategyCategory) -> list[StrategyTemplate]:
    """根据类别获取模板"""
    return [t for t in BUILTIN_TEMPLATES if t.category == category]


def get_templates_by_risk_level(risk_level: RiskLevel) -> list[StrategyTemplate]:
    """根据风险等级获取模板"""
    return [t for t in BUILTIN_TEMPLATES if risk_level in t.suitable_risk_levels]


def get_templates_by_market(market: MarketType) -> list[StrategyTemplate]:
    """根据市场获取模板"""
    return [t for t in BUILTIN_TEMPLATES if market in t.suitable_markets]


def search_templates(
    query: str | None = None,
    category: StrategyCategory | None = None,
    risk_level: RiskLevel | None = None,
    market: MarketType | None = None,
    complexity: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> TemplateSearchResult:
    """搜索模板"""
    templates = BUILTIN_TEMPLATES.copy()

    # 应用过滤器
    if category:
        templates = [t for t in templates if t.category == category]
    if risk_level:
        templates = [
            t for t in templates if risk_level in t.suitable_risk_levels]
    if market:
        templates = [t for t in templates if market in t.suitable_markets]
    if complexity:
        templates = [
            t for t in templates if t.metadata.complexity == complexity]

    # 文本搜索
    if query:
        query_lower = query.lower()
        templates = [
            t
            for t in templates
            if (
                query_lower in t.name.lower()
                or query_lower in t.description.lower()
                or any(query_lower in tag.lower() for tag in t.tags)
            )
        ]

    # 分页
    total = len(templates)
    total_pages = (total + page_size - 1) // page_size
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paged_templates = templates[start_idx:end_idx]

    return TemplateSearchResult(
        templates=paged_templates,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        filters=TemplateSearchFilter(
            category=category,
            risk_level=risk_level,
            market=market,
            complexity=complexity,
        ),
        sort="name",
        search_time=0,
    )
