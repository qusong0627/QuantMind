"""AI 策略向导 - 策略生成相关 Schema 定义"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .stock_pool import Condition
from .strategy_params import BuyRule, RiskConfig, SellRule


class GenerateRequest(BaseModel):
    conditions: Condition
    buyRules: list[BuyRule]
    sellRules: list[SellRule]
    risk: RiskConfig


class GenerateResponse(BaseModel):
    code: str
    doc: str
    hints: dict[str, Any] = {}


class GenerateQlibRequest(BaseModel):
    user_id: str
    conditions: dict[str, Any]
    pool_content: str | None = None  # 股票池内容（直接传入股票代码，每行一个）
    pool_file_key: str | None = None  # 兼容旧字段：COS key
    qlib_params: dict[str, Any] | None = None  # {strategy_type, topk, n_drop, rebalance_period}
    # 以下字段已废弃，保留向后兼容
    position_config: dict[str, Any] | None = None
    style: str | None = None
    risk_config: dict[str, Any] | None = None
    custom_notes: str | None = None


class GenerateQlibResponse(BaseModel):
    success: bool
    code: str | None = None
    error: str | None = None


class GenerateQlibTaskSubmitResponse(BaseModel):
    success: bool
    task_id: str
    status: Literal["pending", "running"]


class GenerateQlibTaskStatusResponse(BaseModel):
    success: bool
    task_id: str
    status: Literal["pending", "running", "completed", "failed", "not_found"]
    result: GenerateQlibResponse | None = None
    error: str | None = None


class SaveToCloudRequest(BaseModel):
    """保存策略到云端请求"""

    user_id: str = Field(..., description="用户ID")
    strategy_name: str = Field(..., description="策略名称")
    code: str = Field(..., description="策略代码")
    metadata: dict[str, Any] = Field(default_factory=dict, description="策略元数据（条件、池子、风险配置等）")


class SaveToCloudResponse(BaseModel):
    """保存策略到云端响应"""

    success: bool
    strategy_id: str | None = None
    cloud_url: str | None = None  # 已废弃，保留兼容
    cos_url: str | None = None  # 预签名 COS URL（私读，TTL=3600s）
    cos_key: str | None = None  # COS 对象键
    access_path: str | None = None
    file_size: int | None = None
    code_hash: str | None = None
    error: str | None = None


class ValidateQlibRequest(BaseModel):
    """Qlib代码验证请求"""

    code: str = Field(..., description="待验证的Qlib策略代码")
    context: dict[str, Any] | None = Field(default=None, description="验证上下文（股票池、回测参数等）")
    mode: Literal["full", "syntax_only"] | None = "full"


class ValidationCheckResponse(BaseModel):
    """单项验证结果"""

    type: str
    passed: bool
    message: str
    details: str | None = None


class ValidateQlibResponse(BaseModel):
    """Qlib代码验证响应"""

    success: bool
    valid: bool
    checks: list[ValidationCheckResponse]
    warnings: list[str] = []
    execution_preview: dict[str, Any] | None = None
    error: str | None = None


class RepairQlibRequest(BaseModel):
    """请求 LLM 修复 QLib 策略代码（主要用于语法/结构修复）"""

    code: str = Field(..., description="待修复的策略代码")
    error: str | None = Field(default=None, description="前端汇总的错误信息（可选）")
    max_rounds: int = Field(default=3, ge=1, le=6, description="最多修复轮次（防止无限循环）")


class RepairQlibResponse(BaseModel):
    """LLM 修复结果"""

    success: bool
    valid: bool
    code: str
    attempts: int = 0
    checks: list[ValidationCheckResponse] = []
    warnings: list[str] = []
    error: str | None = None
