"""
性能监控相关模型
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PerformanceMetric(BaseModel):
    """性能指标"""

    name: str = Field(..., description="指标名称")
    value: float = Field(..., description="指标值")
    unit: str = Field(..., description="单位")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="时间戳")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")


class ProviderPerformance(BaseModel):
    """Provider性能统计"""

    provider_name: str = Field(..., description="Provider名称")
    model_name: str = Field(..., description="模型名称")
    total_requests: int = Field(..., description="总请求数")
    successful_requests: int = Field(..., description="成功请求数")
    failed_requests: int = Field(..., description="失败请求数")
    avg_response_time: float = Field(..., description="平均响应时间(ms)")
    min_response_time: float = Field(..., description="最小响应时间(ms)")
    max_response_time: float = Field(..., description="最大响应时间(ms)")
    success_rate: float = Field(..., description="成功率")
    error_rate: float = Field(..., description="错误率")
    last_request_time: datetime = Field(..., description="最后请求时间")
    status: str = Field("active", description="状态")

    @property
    def is_healthy(self) -> bool:
        """是否健康"""
        return self.success_rate >= 0.95 and self.avg_response_time < 30000


class SystemPerformance(BaseModel):
    """系统性能统计"""

    total_requests: int = Field(..., description="总请求数")
    successful_requests: int = Field(..., description="成功请求数")
    failed_requests: int = Field(..., description="失败请求数")
    avg_response_time: float = Field(..., description="平均响应时间(ms)")
    active_providers: int = Field(..., description="活跃Provider数量")
    memory_usage: float | None = Field(None, description="内存使用率")
    cpu_usage: float | None = Field(None, description="CPU使用率")
    uptime: int = Field(..., description="运行时间(秒)")
    last_updated: datetime = Field(
        default_factory=datetime.utcnow, description="最后更新时间"
    )

    @property
    def overall_success_rate(self) -> float:
        """总体成功率"""
        return (
            (self.successful_requests / self.total_requests * 100)
            if self.total_requests > 0
            else 0
        )

    @property
    def overall_error_rate(self) -> float:
        """总体错误率"""
        return (
            (self.failed_requests / self.total_requests * 100)
            if self.total_requests > 0
            else 0
        )


class PerformanceHistory(BaseModel):
    """性能历史数据"""

    provider_name: str = Field(..., description="Provider名称")
    time_range: str = Field(..., description="时间范围")
    data_points: list[PerformanceMetric] = Field(..., description="数据点")
    summary: dict[str, float] = Field(..., description="汇总统计")


class PerformanceAlert(BaseModel):
    """性能告警"""

    id: str = Field(..., description="告警ID")
    type: str = Field(..., description="告警类型")
    severity: str = Field(..., description="严重程度")
    message: str = Field(..., description="告警消息")
    provider_name: str = Field(..., description="Provider名称")
    metric_name: str = Field(..., description="指标名称")
    current_value: float = Field(..., description="当前值")
    threshold: float = Field(..., description="阈值")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="创建时间"
    )
    resolved_at: datetime | None = Field(None, description="解决时间")
    status: str = Field("active", description="状态")


class PerformanceQuery(BaseModel):
    """性能查询参数"""

    provider_name: str | None = Field(None, description="Provider名称")
    time_range: str = Field("1h", description="时间范围")
    metric_types: list[str] = Field(default_factory=list, description="指标类型")
    start_time: datetime | None = Field(None, description="开始时间")
    end_time: datetime | None = Field(None, description="结束时间")
    limit: int = Field(100, description="数据点数量限制")
