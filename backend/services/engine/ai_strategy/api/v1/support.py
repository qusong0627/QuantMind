"""AI Strategy V1 支撑路由

模板、验证、健康检查、性能监控、文件管理。
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from ...models import (
    BatchValidationRequest,
    CodeValidationRequest,
    ParameterValidationRequest,
    TemplateMatchRequest,
    TemplateMatchResponse,
    TemplateSearchFilter,
    TemplateValidationRequest,
    search_templates,
)
from ...provider_registry import REGISTRY, get_provider_name
from ...services.template_matcher import template_matcher
from ...services.validator import unified_validator
from ...storage.database import (
    get_file_stats,
    get_strategy_statistics,
)

try:  # 部署模式优先 shared
    from shared.errors import ErrorCode  # type: ignore
    from shared.response import error, success  # type: ignore
except Exception:  # pragma: no cover
    from backend.shared.errors import ErrorCode  # type: ignore
    from backend.shared.response import error, success  # type: ignore

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/templates")
async def list_templates(
    category: str | None = None,
    risk_level: str | None = None,
    market: str | None = None,
    complexity: str | None = None,
    page: int = 1,
    page_size: int = 20,
    query: str | None = None,
):
    """获取策略模板列表"""
    try:
        filters = TemplateSearchFilter(
            category=category,
            risk_level=risk_level,
            market=market,
            complexity=complexity,
        )
        result = search_templates(
            query=query,
            category=filters.category,
            risk_level=filters.risk_level,
            market=filters.market,
            complexity=filters.complexity,
            page=page,
            page_size=page_size,
        )

        return success(
            {
                "templates": [template.model_dump() for template in result.templates],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
                "total_pages": result.total_pages,
                "filters": filters.model_dump(),
                "search_time": result.search_time,
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取模板列表失败: {exc}")


@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    """获取策略模板详情"""
    try:
        from ...models.template import get_template_by_id

        template = get_template_by_id(template_id)
        if not template:
            return error(ErrorCode.NOT_FOUND, f"模板 {template_id} 不存在")
        return success(template.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取模板详情失败: {exc}")


@router.post("/templates/match")
async def match_templates(request: TemplateMatchRequest):
    """智能匹配策略模板"""
    try:
        matches = template_matcher.match_templates(request)
        response = TemplateMatchResponse(
            success=True,
            matches=matches,
            total_matches=len(matches),
            processing_time=0,
            suggestions=[
                "建议根据匹配度最高的模板生成策略",
                "可以查看适配建议来优化参数配置",
            ],
        )
        return success(response.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"模板匹配失败: {exc}")


@router.get("/templates/categories")
async def get_template_categories():
    """获取所有策略类别"""
    try:
        categories = [
            {"value": "trend", "label": "趋势策略", "description": "基于市场趋势的策略"},
            {"value": "mean_reversion", "label": "均值回归", "description": "基于价格回归的策略"},
            {"value": "momentum", "label": "动量策略", "description": "基于价格动量的策略"},
            {"value": "breakout", "label": "突破策略", "description": "基于价格突破的策略"},
            {"value": "arbitrage", "label": "套利策略", "description": "基于价格差异的策略"},
        ]
        return success({"categories": categories})
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取策略类别失败: {exc}")


@router.get("/templates/stats")
async def get_template_stats():
    """获取模板统计信息"""
    try:
        from ...models.template import BUILTIN_TEMPLATES

        category_stats = {}
        complexity_stats = {"low": 0, "medium": 0, "high": 0}
        for template in BUILTIN_TEMPLATES:
            category = template.category
            category_stats[category] = category_stats.get(category, 0) + 1
            complexity = template.metadata.complexity
            complexity_stats[complexity] = complexity_stats.get(complexity, 0) + 1

        stats = {
            "total_templates": len(BUILTIN_TEMPLATES),
            "category_distribution": category_stats,
            "complexity_distribution": complexity_stats,
            "avg_min_capital": sum(t.min_capital for t in BUILTIN_TEMPLATES) // len(BUILTIN_TEMPLATES),
            "avg_max_symbols": sum(t.max_symbols for t in BUILTIN_TEMPLATES) // len(BUILTIN_TEMPLATES),
        }
        return success(stats)
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取模板统计失败: {exc}")


@router.post("/validate/parameters")
async def validate_parameters(request: ParameterValidationRequest):
    """验证策略参数"""
    try:
        result = unified_validator.parameter_validator.validate_parameters(request)
        return success(result.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"参数验证失败: {exc}")


@router.post("/validate/code")
async def validate_code(request: CodeValidationRequest):
    """验证策略代码"""
    try:
        result = unified_validator.code_validator.validate_code(request)
        return success(result.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"代码验证失败: {exc}")


@router.post("/validate/template")
async def validate_template(request: TemplateValidationRequest):
    """验证模板兼容性"""
    try:
        result = unified_validator.template_validator.validate_template_compatibility(request)
        return success(result.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"模板验证失败: {exc}")


@router.post("/validate/batch")
async def validate_batch(request: BatchValidationRequest):
    """批量验证"""
    try:
        result = unified_validator.validate_batch(request)
        return success(result.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"批量验证失败: {exc}")


@router.get("/validate/rules")
async def get_validation_rules():
    """获取验证规则信息"""
    try:
        from ...services.validator import STRATEGY_PARAMETER_RULES

        rules_info = []
        for rule in STRATEGY_PARAMETER_RULES:
            rules_info.append(
                {
                    "field": rule.field,
                    "rule": rule.rule,
                    "message": rule.message,
                    "severity": rule.severity,
                }
            )

        return success(
            {
                "rules": rules_info,
                "total_rules": len(rules_info),
                "categories": {
                    "required": len([r for r in rules_info if r["rule"] == "required"]),
                    "enum": len([r for r in rules_info if r["rule"].startswith("enum")]),
                    "range": len([r for r in rules_info if r["rule"].startswith("range")]),
                },
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取验证规则失败: {exc}")


@router.get("/validate/metrics")
async def get_validation_metrics():
    """获取验证质量指标"""
    try:
        from ...services.validator import CODE_QUALITY_METRICS

        return success(
            {
                "code_quality_metrics": CODE_QUALITY_METRICS,
                "thresholds": {
                    "excellent_score": 90,
                    "good_score": 70,
                    "acceptable_score": 50,
                    "poor_score": 30,
                },
                "recommendations": {
                    "syntax_errors": "必须修复才能继续",
                    "logic_errors": "建议修复以确保策略正确性",
                    "warnings": "建议优化以提升代码质量",
                    "score_below_50": "建议优化策略参数和代码结构",
                },
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取验证指标失败: {exc}")


@router.post("/validate/realtime")
async def validate_realtime(
    parameters: dict[str, object] | None = None,
    code: str | None = None,
    template_id: str | None = None,
    strict_mode: bool = False,
):
    """实时验证（简化的批量验证）"""
    try:
        validation_types = []
        if parameters:
            validation_types.append("parameters")
        if code:
            validation_types.append("code")
        if template_id:
            validation_types.append("template")

        if not validation_types:
            return error(
                ErrorCode.PARAM_REQUIRED,
                "至少需要提供一种验证类型（parameters, code, template）",
            )

        request = BatchValidationRequest(
            parameters=parameters,
            code=code,
            template_id=template_id,
            validation_types=validation_types,
            strict_mode=strict_mode,
        )
        result = unified_validator.validate_batch(request)
        simplified_result = {
            "success": result.success,
            "overall_score": result.overall_score,
            "is_ready_for_generation": result.is_ready_for_generation,
            "next_steps": result.next_steps,
            "processing_time": result.processing_time,
            "summary": {
                "parameter_validation": (
                    {
                        "is_valid": (result.parameter_validation.is_valid if result.parameter_validation else None),
                        "score": (result.parameter_validation.score if result.parameter_validation else None),
                        "error_count": (len(result.parameter_validation.errors) if result.parameter_validation else 0),
                        "warning_count": (
                            len(result.parameter_validation.warnings) if result.parameter_validation else 0
                        ),
                    }
                    if result.parameter_validation
                    else None
                ),
                "code_validation": (
                    {
                        "is_valid": (result.code_validation.is_valid if result.code_validation else None),
                        "quality_score": (result.code_validation.quality_score if result.code_validation else None),
                        "syntax_error_count": (
                            len(result.code_validation.syntax_errors) if result.code_validation else 0
                        ),
                        "logic_error_count": (
                            len(result.code_validation.logic_errors) if result.code_validation else 0
                        ),
                        "complexity": (result.code_validation.complexity if result.code_validation else None),
                    }
                    if result.code_validation
                    else None
                ),
                "template_validation": (
                    {
                        "is_valid": (result.template_validation.is_valid if result.template_validation else None),
                        "compatibility_score": (
                            result.template_validation.compatibility_score if result.template_validation else None
                        ),
                        "error_count": (len(result.template_validation.errors) if result.template_validation else 0),
                    }
                    if result.template_validation
                    else None
                ),
            },
        }
        return success(simplified_result)
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"实时验证失败: {exc}")


@router.get("/health")
async def api_health_with_validation():
    """API健康检查（包含验证服务状态）"""
    from ...services.startup_health import get_startup_health_state

    active = get_provider_name()
    providers = {name: {"is_healthy": True, "active": name == active} for name in REGISTRY.keys()}
    validation_status = {
        "parameter_validator": True,
        "code_validator": True,
        "template_validator": True,
        "unified_validator": True,
    }
    startup_state = get_startup_health_state().to_dict()

    return success(
        {
            "service": "ai-strategy",
            "status": startup_state["status"],
            "models": providers,
            "validation_services": validation_status,
            "startup_health": startup_state,
            "features": {
                "template_matching": True,
                "parameter_validation": True,
                "code_validation": True,
                "realtime_validation": True,
                "batch_validation": True,
                "performance_monitoring": True,
                "file_management": True,
            },
        }
    )


@router.get("/performance/system")
async def get_system_performance():
    """获取系统性能统计"""
    try:
        from ...services.performance_monitor import performance_monitor

        perf = performance_monitor.get_system_performance()
        return success(perf.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取系统性能失败: {exc}")


@router.get("/performance/providers/{provider_name}")
async def get_provider_performance(provider_name: str):
    """获取Provider性能统计"""
    try:
        from ...services.performance_monitor import performance_monitor

        perf = performance_monitor.get_provider_performance(provider_name)
        return success(perf.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取Provider性能失败: {exc}")


@router.get("/performance")
async def get_performance_overview():
    """兼容前端调用，返回系统与provider性能汇总"""
    try:
        from ...services.performance_monitor import performance_monitor

        system_perf = performance_monitor.get_system_performance()
        providers_perf = performance_monitor.get_all_providers_performance()
        return success(
            {
                "system": system_perf.model_dump(),
                "providers": [perf.model_dump() for perf in providers_perf],
                "provider_count": len(providers_perf),
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取性能指标失败: {exc}")


@router.get("/performance/providers")
async def get_all_providers_performance():
    """获取所有Provider性能统计"""
    try:
        from ...services.performance_monitor import performance_monitor

        performances = performance_monitor.get_all_providers_performance()
        return success(
            {
                "providers": [perf.model_dump() for perf in performances],
                "total_providers": len(performances),
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取Provider性能列表失败: {exc}")


@router.post("/performance/history")
async def get_performance_history(request):
    """获取性能历史数据"""
    try:
        from ...models.performance import PerformanceQuery
        from ...services.performance_monitor import performance_monitor

        query = PerformanceQuery(**request)
        history = performance_monitor.get_performance_history(query)
        return success(history.model_dump())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取性能历史失败: {exc}")


@router.get("/performance/alerts")
async def get_active_alerts():
    """获取活跃告警"""
    try:
        from ...services.performance_monitor import performance_monitor

        alerts = performance_monitor.get_active_alerts()
        return success(
            {
                "alerts": [alert.model_dump() for alert in alerts],
                "total_alerts": len(alerts),
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取告警列表失败: {exc}")


@router.post("/performance/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str):
    """解决告警"""
    try:
        from ...services.performance_monitor import performance_monitor

        performance_monitor.resolve_alert(alert_id)
        return success({"message": f"告警 {alert_id} 已解决"})
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"解决告警失败: {exc}")


@router.post("/performance/reset")
async def reset_performance_stats(provider_name: str = None):
    """重置性能统计"""
    try:
        from ...services.performance_monitor import performance_monitor

        performance_monitor.reset_stats(provider_name)
        return success({"message": "性能统计已重置"})
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"重置性能统计失败: {exc}")


@router.post("/files/upload")
async def upload_file(
    file_content,
    filename: str,
    user_id: str,
    category: str = "auto",
    description: str = "",
    tags: list = [],
):
    """上传文件"""
    try:
        from ...services.file_manager import file_manager

        if hasattr(file_content, "file"):
            file_obj = file_content.file
        else:
            file_obj = file_content

        file_info = file_manager.upload_file(file_obj, filename, user_id, category, description, tags)
        return success(file_info.to_dict())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"文件上传失败: {exc}")


@router.get("/files/{file_id}")
async def get_file_info(file_id: str):
    """获取文件信息"""
    try:
        from ...services.file_manager import file_manager

        file_info = file_manager.get_file(file_id)
        if not file_info:
            return error(ErrorCode.NOT_FOUND, f"文件 {file_id} 不存在")
        return success(file_info.to_dict())
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取文件信息失败: {exc}")


@router.get("/files/{file_id}/content")
async def get_file_content(file_id: str):
    """获取文件内容"""
    try:
        from ...services.file_manager import file_manager

        content = file_manager.get_file_content(file_id)
        if not content:
            return error(ErrorCode.NOT_FOUND, f"文件 {file_id} 不存在或无法读取")

        file_info = file_manager.get_file(file_id)
        return StreamingResponse(
            iter([content]),
            media_type=file_info.content_type,
            headers={"Content-Disposition": f"attachment; filename={file_info.filename}"},
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取文件内容失败: {exc}")


@router.get("/files/{file_id}/download")
async def download_file(file_id: str):
    """下载文件"""
    try:
        from ...services.file_manager import file_manager

        file_path = file_manager.download_file(file_id)
        if not file_path:
            return error(ErrorCode.NOT_FOUND, f"文件 {file_id} 不存在")
        return success({"file_path": file_path})
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"下载文件失败: {exc}")


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, user_id: str):
    """删除文件"""
    try:
        from ...services.file_manager import file_manager

        deleted = file_manager.delete_file(file_id, user_id)
        if not deleted:
            return error(ErrorCode.NOT_FOUND, f"文件 {file_id} 不存在或无权限删除")
        return success({"message": f"文件 {file_id} 已删除"})
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"删除文件失败: {exc}")


@router.get("/files")
async def list_user_files(user_id: str, category: str = None, limit: int = 100, offset: int = 0):
    """列出用户文件"""
    try:
        from ...services.file_manager import file_manager

        result = file_manager.list_user_files(user_id, category, limit, offset)
        return success(result)
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取文件列表失败: {exc}")


@router.get("/files/category/{category}")
async def list_files_by_category(category: str, limit: int = 100, offset: int = 0):
    """按类别列出文件"""
    try:
        from ...services.file_manager import file_manager

        result = file_manager.list_files_by_category(category, limit, offset)
        return success(result)
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取类别文件失败: {exc}")


@router.get("/files/search")
async def search_files(query: str, user_id: str = None, category: str = None, limit: int = 100):
    """搜索文件"""
    try:
        from ...services.file_manager import file_manager

        files = file_manager.search_files(query, user_id, category, limit)
        return success(
            {
                "files": [file_info.to_dict() for file_info in files],
                "total": len(files),
                "query": query,
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"搜索文件失败: {exc}")


@router.put("/files/{file_id}/metadata")
async def update_file_metadata(file_id: str, user_id: str, description: str = None, tags: list = None):
    """更新文件元数据"""
    try:
        from ...services.file_manager import file_manager

        updated = file_manager.update_file_metadata(file_id, user_id, description, tags)
        if not updated:
            return error(ErrorCode.NOT_FOUND, f"文件 {file_id} 不存在或无权限修改")
        return success({"message": f"文件 {file_id} 元数据已更新"})
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"更新文件元数据失败: {exc}")


@router.post("/files/cleanup")
async def cleanup_temp_files(max_age_days: int = 7):
    """清理临时文件"""
    try:
        from ...services.file_manager import file_manager

        deleted_count = file_manager.cleanup_temp_files(max_age_days)
        return success(
            {
                "message": f"已清理 {deleted_count} 个临时文件",
                "deleted_count": deleted_count,
            }
        )
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"清理临时文件失败: {exc}")


@router.get("/files/stats")
async def get_file_stats_endpoint():
    """获取文件存储统计"""
    try:
        stats = get_file_stats()
        return success(stats)
    except Exception as exc:
        return error(ErrorCode.INTERNAL_ERROR, f"获取文件统计失败: {exc}")
