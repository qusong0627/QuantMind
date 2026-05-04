"""
验证相关模型
统一前后端验证逻辑
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 导入共享枚举
try:
    from shared.enums import (
        BacktestPeriod,
        MarketType,
        RiskLevel,
        StrategyLength,
        StrategyStyle,
        Timeframe,
        ValidationErrorType,
        ValidationSeverity,
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
    ValidationErrorType = Literal["syntax", "logic", "dependency", "parameter"]
    ValidationSeverity = Literal["error", "warning", "info"]


class ValidationError(BaseModel):
    """验证错误"""

    line: int | None = Field(None, description="错误行号")
    column: int | None = Field(None, description="错误列号")
    message: str = Field(..., description="错误消息")
    type: ValidationErrorType = Field(..., description="错误类型")
    severity: ValidationSeverity = Field(..., description="严重程度")
    code: str | None = Field(None, description="错误代码")
    suggestion: str | None = Field(None, description="修复建议")


class ParameterValidationError(BaseModel):
    """参数验证错误"""

    field: str = Field(..., description="字段名")
    message: str = Field(..., description="错误消息")
    severity: ValidationSeverity = Field(..., description="严重程度")
    current_value: Any = Field(None, description="当前值")
    suggested_value: Any = Field(None, description="建议值")
    rule: str = Field(..., description="验证规则")


class ValidationResult(BaseModel):
    """验证结果"""

    is_valid: bool = Field(..., description="是否有效")
    errors: list[ValidationError] = Field(
        default_factory=list, description="错误列表")
    warnings: list[ValidationError] = Field(
        default_factory=list, description="警告列表"
    )
    suggestions: list[str] = Field(default_factory=list, description="建议列表")
    processing_time: int = Field(..., description="处理时间(ms)")
    score: float = Field(..., ge=0, le=100, description="验证得分")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")


class ParameterValidationRequest(BaseModel):
    """参数验证请求"""

    parameters: dict[str, Any] = Field(..., description="参数字典")
    strict_mode: bool = Field(False, description="严格模式")
    include_suggestions: bool = Field(True, description="包含建议")

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, v):
        if not isinstance(v, dict):
            raise ValueError("参数必须是字典类型")
        return v


class ParameterValidationResponse(BaseModel):
    """参数验证响应"""

    success: bool = Field(..., description="是否成功")
    is_valid: bool = Field(..., description="参数是否有效")
    errors: list[ParameterValidationError] = Field(
        default_factory=list, description="错误列表"
    )
    warnings: list[ParameterValidationError] = Field(
        default_factory=list, description="警告列表"
    )
    suggestions: list[str] = Field(default_factory=list, description="建议列表")
    adjusted_parameters: dict[str, Any] | None = Field(
        None, description="调整后的参数"
    )
    score: float = Field(..., ge=0, le=100, description="验证得分")
    processing_time: int = Field(..., description="处理时间(ms)")


class CodeValidationRequest(BaseModel):
    """代码验证请求"""

    code: str = Field(..., description="代码内容")
    language: str = Field("python", description="编程语言")
    parameters: dict[str, Any] | None = Field(None, description="策略参数")
    stage: str = Field("generation", description="验证阶段")
    strict_mode: bool = Field(False, description="严格模式")

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        if not v or not v.strip():
            raise ValueError("代码内容不能为空")
        return v.strip()


class CodeValidationResponse(BaseModel):
    """代码验证响应"""

    success: bool = Field(..., description="是否成功")
    is_valid: bool = Field(..., description="代码是否有效")
    syntax_errors: list[ValidationError] = Field(
        default_factory=list, description="语法错误"
    )
    logic_errors: list[ValidationError] = Field(
        default_factory=list, description="逻辑错误"
    )
    warnings: list[ValidationError] = Field(
        default_factory=list, description="警告列表"
    )
    suggestions: list[str] = Field(default_factory=list, description="建议列表")
    quality_score: float = Field(..., ge=0, le=100, description="代码质量得分")
    complexity: int = Field(..., description="复杂度")
    processing_time: int = Field(..., description="处理时间(ms)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")


class TemplateValidationRequest(BaseModel):
    """模板验证请求"""

    template_id: str = Field(..., description="模板ID")
    parameters: dict[str, Any] = Field(..., description="参数字典")
    strict_mode: bool = Field(False, description="严格模式")


class TemplateValidationResponse(BaseModel):
    """模板验证响应"""

    success: bool = Field(..., description="是否成功")
    is_valid: bool = Field(..., description="模板是否有效")
    template_id: str = Field(..., description="模板ID")
    compatibility_score: float = Field(..., ge=0, le=1, description="兼容性得分")
    errors: list[ParameterValidationError] = Field(
        default_factory=list, description="错误列表"
    )
    warnings: list[ParameterValidationError] = Field(
        default_factory=list, description="警告列表"
    )
    suggestions: list[str] = Field(default_factory=list, description="建议列表")
    adaptations: list[str] = Field(default_factory=list, description="适配建议")
    processing_time: int = Field(..., description="处理时间(ms)")


class BatchValidationRequest(BaseModel):
    """批量验证请求"""

    parameters: dict[str, Any] | None = Field(None, description="参数字典")
    code: str | None = Field(None, description="代码内容")
    template_id: str | None = Field(None, description="模板ID")
    validation_types: list[str] = Field(
        default_factory=list, description="验证类型")
    strict_mode: bool = Field(False, description="严格模式")


class BatchValidationResponse(BaseModel):
    """批量验证响应"""

    success: bool = Field(..., description="是否成功")
    parameter_validation: ParameterValidationResponse | None = Field(
        None, description="参数验证结果"
    )
    code_validation: CodeValidationResponse | None = Field(
        None, description="代码验证结果"
    )
    template_validation: TemplateValidationResponse | None = Field(
        None, description="模板验证结果"
    )
    overall_score: float = Field(..., ge=0, le=100, description="综合得分")
    is_ready_for_generation: bool = Field(..., description="是否准备好生成")
    next_steps: list[str] = Field(default_factory=list, description="下一步建议")
    processing_time: int = Field(..., description="处理时间(ms)")

    # 验证规则定义


class ValidationRule:
    """验证规则基类"""

    def __init__(
        self,
        field: str,
        rule: str,
        message: str,
        severity: ValidationSeverity = "error",
    ):
        self.field = field
        self.rule = rule
        self.message = message
        self.severity = severity

    def validate(self, value: Any) -> bool:
        """验证值"""
        raise NotImplementedError("子类必须实现validate方法")


class RangeRule(ValidationRule):
    """范围验证规则"""

    def __init__(
        self,
        field: str,
        min_val: float = None,
        max_val: float = None,
        message: str = None,
    ):
        if message is None:
            if min_val is not None and max_val is not None:
                message = f"{field}必须在{min_val}-{max_val}之间"
            elif min_val is not None:
                message = f"{field}必须大于等于{min_val}"
            elif max_val is not None:
                message = f"{field}必须小于等于{max_val}"
            else:
                message = f"{field}值无效"

        super().__init__(field, f"range:{min_val}:{max_val}", message)
        self.min_val = min_val
        self.max_val = max_val

    def validate(self, value: Any) -> bool:
        try:
            num_val = float(value)
            if self.min_val is not None and num_val < self.min_val:
                return False
            if self.max_val is not None and num_val > self.max_val:
                return False
            return True
        except (ValueError, TypeError):
            return False


class EnumRule(ValidationRule):
    """枚举验证规则"""

    def __init__(self, field: str, valid_values: list[Any], message: str = None):
        if message is None:
            message = f"{field}必须是以下值之一: {', '.join(map(str, valid_values))}"
        super().__init__(field, f"enum:{valid_values}", message)
        self.valid_values = valid_values

    def validate(self, value: Any) -> bool:
        return value in self.valid_values


class RequiredRule(ValidationRule):
    """必需字段验证规则"""

    def __init__(self, field: str, message: str = None):
        if message is None:
            message = f"{field}是必需的"
        super().__init__(field, "required", message)

    def validate(self, value: Any) -> bool:
        return value is not None and value != ""

        # 预定义验证规则


STRATEGY_PARAMETER_RULES = [
    # 必需字段
    RequiredRule("description", "策略描述不能为空"),
    RequiredRule("market", "市场不能为空"),
    RequiredRule("risk_level", "风险等级不能为空"),
    # 枚举字段
    EnumRule("market", ["CN", "US", "HK", "GLOBAL"]),
    EnumRule("risk_level", ["low", "medium", "high"]),
    EnumRule("timeframe", ["1m", "5m", "15m",
             "30m", "1h", "4h", "1d", "1w", "1M"]),
    EnumRule(
        "strategy_length", ["short_term",
                            "medium_term", "long_term", "unlimited"]
    ),
    EnumRule(
        "backtest_period",
        ["3months", "6months", "1year", "2years", "5years", "unlimited"],
    ),
    # 数值范围
    RangeRule("initial_capital", min_val=10000, message="初始资金至少需要10,000元"),
    RangeRule(
        "position_size", min_val=1, max_val=100, message="仓位大小必须在1-100%之间"
    ),
    RangeRule(
        "max_positions", min_val=1, max_val=50, message="最大持仓数量必须在1-50之间"
    ),
    RangeRule("stop_loss", min_val=0, max_val=50, message="止损必须在0-50%之间"),
    RangeRule("take_profit", min_val=0, max_val=200, message="止盈必须在0-200%之间"),
    # 高级参数
    RangeRule(
        "max_drawdown", min_val=0, max_val=100, message="最大回撤必须在0-100%之间"
    ),
    RangeRule(
        "commission_rate", min_val=0, max_val=0.01, message="手续费率必须在0-1%之间"
    ),
    RangeRule("slippage", min_val=0, max_val=0.01, message="滑点必须在0-1%之间"),
]

# 代码质量评估规则
CODE_QUALITY_METRICS = {
    "complexity_threshold": 50,  # 复杂度阈值
    "max_line_length": 120,  # 最大行长度
    "max_function_length": 100,  # 最大函数长度
    "min_comment_ratio": 0.1,  # 最小注释比例
    "required_functions": [  # 必需函数列表
        "initialize",
        "generate_signals",
        "execute_trade",
        "calculate_positions",
    ],
}
