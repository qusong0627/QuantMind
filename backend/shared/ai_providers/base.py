#!/usr/bin/env python3
"""
AI提供商基类和工厂模块
提供统一的AI接口抽象和工厂模式实现
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class ModelType(Enum):
    """AI模型类型"""

    GPT_35 = "gpt-3.5-turbo"
    GPT_4 = "gpt-4"
    GPT_4_TURBO = "gpt-4-turbo"
    CLAUDE_3_SONNET = "claude-3-sonnet"
    CLAUDE_3_OPUS = "claude-3-opus"
    CLAUDE_3_HAIKU = "claude-3-haiku"
    DEEPSEEK_CHAT = "deepseek-chat"
    DEEPSEEK_CODER = "deepseek-coder"
    LOCAL_LLAMA = "llama"
    LOCAL_MISTRAL = "mistral"
    CUSTOM = "custom"


class StrategyType(Enum):
    """策略类型"""

    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    ARBITRAGE = "arbitrage"
    TREND_FOLLOWING = "trend_following"
    BREAKOUT = "breakout"
    CUSTOM = "custom"


class ComplexityLevel(Enum):
    """策略复杂度"""

    BASIC = "basic"  # 基础策略，单一指标
    INTERMEDIATE = "intermediate"  # 中等策略，多指标组合
    ADVANCED = "advanced"  # 高级策略，机器学习/复杂逻辑
    EXPERT = "expert"  # 专家级策略，多因子/深度学习


@dataclass
class StrategyRequest:
    """策略生成请求"""

    prompt: str
    strategy_type: StrategyType | None = None
    complexity_level: ComplexityLevel = ComplexityLevel.INTERMEDIATE
    target_assets: list[str] = field(default_factory=list)
    timeframe: str = "1d"
    risk_tolerance: str = "medium"  # low, medium, high
    max_positions: int = 10
    backtest_period: str = "1y"
    custom_requirements: list[str] = field(default_factory=list)
    context_data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "prompt": self.prompt,
            "strategy_type": self.strategy_type.value if self.strategy_type else None,
            "complexity_level": self.complexity_level.value,
            "target_assets": self.target_assets,
            "timeframe": self.timeframe,
            "risk_tolerance": self.risk_tolerance,
            "max_positions": self.max_positions,
            "backtest_period": self.backtest_period,
            "custom_requirements": self.custom_requirements,
            "context_data": self.context_data,
        }


@dataclass
class StrategyCode:
    """策略代码"""

    language: str  # python, javascript, etc.
    code: str
    dependencies: list[str] = field(default_factory=list)
    entry_point: str = "main"
    description: str | None = None


@dataclass
class StrategyParameter:
    """策略参数"""

    name: str
    type: str  # int, float, str, bool
    default_value: int | float | str | bool
    range: dict[str, int | float] | None = None
    description: str | None = None
    optimization_hints: list[str] | None = None


@dataclass
class StrategyResponse:
    """策略生成响应"""

    strategy_name: str
    description: str
    strategy_type: StrategyType
    complexity_level: ComplexityLevel
    code: StrategyCode
    parameters: list[StrategyParameter] = field(default_factory=list)
    risk_indicators: dict[str, Any] = field(default_factory=dict)
    expected_performance: dict[str, float] = field(default_factory=dict)
    reasoning: str | None = None
    confidence_score: float = 0.0
    generation_time: float = 0.0
    model_used: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        """计算生成时间"""
        if self.generation_time == 0.0:
            self.generation_time = time.time()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "strategy_name": self.strategy_name,
            "description": self.description,
            "strategy_type": self.strategy_type.value,
            "complexity_level": self.complexity_level.value,
            "code": {
                "language": self.code.language,
                "code": self.code.code,
                "dependencies": self.code.dependencies,
                "entry_point": self.code.entry_point,
                "description": self.code.description,
            },
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "default_value": p.default_value,
                    "range": p.range,
                    "description": p.description,
                    "optimization_hints": p.optimization_hints,
                }
                for p in self.parameters
            ],
            "risk_indicators": self.risk_indicators,
            "expected_performance": self.expected_performance,
            "reasoning": self.reasoning,
            "confidence_score": self.confidence_score,
            "generation_time": self.generation_time,
            "model_used": self.model_used,
            "token_usage": self.token_usage,
        }


@dataclass
class ModelConfig:
    """模型配置"""

    model_name: str
    model_type: ModelType
    api_key: str | None = None
    api_base: str | None = None
    max_tokens: int = 4000
    temperature: float = 0.7
    timeout: int = 60
    retry_attempts: int = 3
    custom_params: dict[str, Any] = field(default_factory=dict)


class BaseAIProvider(ABC):
    """AI Provider基础抽象类"""

    def __init__(self, config: ModelConfig):
        self.config = config
        self._is_initialized = False

    @abstractmethod
    async def initialize(self) -> bool:
        """初始化Provider"""

    @abstractmethod
    async def generate_strategy(self, request: StrategyRequest) -> StrategyResponse:
        """生成交易策略"""

    @abstractmethod
    async def optimize_strategy(
        self,
        strategy_code: str,
        performance_data: dict[str, Any],
        optimization_goals: list[str],
    ) -> StrategyResponse:
        """优化现有策略"""

    @abstractmethod
    async def analyze_strategy(self, strategy_code: str) -> dict[str, Any]:
        """分析策略代码"""

    @abstractmethod
    async def validate_strategy(self, strategy_code: str) -> dict[str, Any]:
        """验证策略代码"""

    @abstractmethod
    def get_supported_models(self) -> list[ModelType]:
        """获取支持的模型列表"""

    @abstractmethod
    def estimate_tokens(self, text: str) -> int:
        """估算token数量"""

    @property
    def is_initialized(self) -> bool:
        """检查是否已初始化"""
        return self._is_initialized

    async def health_check(self) -> dict[str, Any]:
        """健康检查"""
        return {
            "provider": self.__class__.__name__,
            "model": self.config.model_name,
            "status": "healthy" if self._is_initialized else "not_initialized",
            "timestamp": time.time(),
        }

    def _build_system_prompt(self, request: StrategyRequest) -> str:
        """构建系统提示词"""
        base_prompt = f"""
你是一个专业的量化交易策略开发专家。请根据用户需求生成高质量的交易策略。

策略要求：
- 策略类型: {request.strategy_type.value if request.strategy_type else "不限"}
- 复杂度级别: {request.complexity_level.value}
- 目标资产: {", ".join(request.target_assets) if request.target_assets else "不限"}
- 时间周期: {request.timeframe}
- 风险偏好: {request.risk_tolerance}
- 最大持仓数: {request.max_positions}
- 回测周期: {request.backtest_period}

请生成包含以下内容的完整策略：
1. 策略名称和描述
2. 完整的Python代码实现
3. 策略参数定义
4. 风险指标说明
5. 预期性能评估

代码要求：
- 包含清晰的注释
- 实现完整的买入/卖出逻辑
- 包含风险控制机制

QuantMind / Qlib 平台规范（必须遵守）：
1. 代码中必须提供 get_strategy_config() 或 STRATEGY_CONFIG 入口
2. 推荐基类（选股用 RedisTopkStrategy，权重用 RedisWeightStrategy）：
   导入路径: from backend.services.engine.qlib_app.utils.extended_strategies import RedisTopkStrategy, RedisWeightStrategy
3. signal 默认使用 "<PRED>"
4. 若重写 __init__，必须使用 kwargs.pop() 消费自定义参数，避免 unexpected keyword argument
5. 若重写 reset，必须兼容 def reset(self, *args, **kwargs)
6. generate_trade_decision 必须返回 TradeDecisionWO（不能返回 dict）
7. 禁止使用 os, sys, subprocess, requests, urllib, socket 等危险模块
"""

        if request.custom_requirements:
            base_prompt += "\n自定义要求:\n" + "\n".join(f"- {req}" for req in request.custom_requirements)

        return base_prompt.strip()

    def _parse_strategy_response(self, raw_response: str, request: StrategyRequest) -> StrategyResponse:
        """解析AI响应为StrategyResponse对象"""
        # 这是一个基础实现，具体的Provider可以重写此方法
        return StrategyResponse(
            strategy_name="Generated Strategy",
            description="AI生成的交易策略",
            strategy_type=request.strategy_type or StrategyType.CUSTOM,
            complexity_level=request.complexity_level,
            code=StrategyCode(language="python", code=raw_response, dependencies=["pandas", "numpy"]),
            model_used=self.config.model_name,
        )

    def _log_messages(self, provider_name: str, messages: list[dict[str, str]]):
        """记录消息历史"""
        formatted_messages = "\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])
        logger.debug(f"调用AI提供商: {provider_name}")
        logger.debug(f"消息历史: {formatted_messages}")
