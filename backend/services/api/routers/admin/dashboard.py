"""
管理员仪表板路由 - 从 admin_service 迁移
提供系统级统计指标（用户数、策略数、内容数、系统健康度）
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.services.api.user_app.middleware.auth import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------- Schemas (内联定义，避免额外文件) ----------


class ApiResponse(BaseModel):
    success: bool
    code: int
    message: str
    data: dict[str, Any] | None = None


CORE_SERVICE_HEALTH_URLS = {
    "api": os.getenv("ADMIN_DASHBOARD_API_HEALTH_URL", "http://127.0.0.1:8000/health"),
    "trade": os.getenv("ADMIN_DASHBOARD_TRADE_HEALTH_URL", "http://127.0.0.1:8002/health"),
    "engine": os.getenv("ADMIN_DASHBOARD_ENGINE_HEALTH_URL", "http://127.0.0.1:8001/health"),
    "stream": os.getenv("ADMIN_DASHBOARD_STREAM_HEALTH_URL", "http://127.0.0.1:8003/health"),
}


def _build_system_metrics(
    health_score: int,
    uptime_days: int | None,
    services: list[dict[str, Any]],
) -> dict[str, Any]:
    """构造系统指标，基于真实健康检查结果。"""
    overall_status = "healthy" if services and all(service.get("status") == "healthy" for service in services) else "degraded"
    if not services:
        overall_status = "degraded"

    return {
        "health_score": health_score,
        "uptime_days": uptime_days,
        "status": overall_status,
        "services": services,
    }


async def _fetch_service_health(
    client: httpx.AsyncClient,
    service_name: str,
    health_url: str,
) -> dict[str, Any]:
    """请求单个服务的真实健康状态。"""
    try:
        response = await client.get(health_url)
        response.raise_for_status()
        payload = response.json()
        status = str(payload.get("status") or "degraded")
        healthy = status == "healthy"
        score = 100 if healthy else 60
        return {
            "service": service_name,
            "url": health_url,
            "status": status,
            "score": score,
            "healthy": healthy,
            "details": payload,
        }
    except Exception as exc:
        logger.warning("Admin dashboard health probe failed for %s: %s", service_name, exc)
        return {
            "service": service_name,
            "url": health_url,
            "status": "unreachable",
            "score": 0,
            "healthy": False,
            "error": str(exc),
        }


async def _collect_system_health() -> tuple[int, list[dict[str, Any]]]:
    """聚合核心服务健康状态为一个 0-100 分值。"""
    timeout_raw = os.getenv("ADMIN_DASHBOARD_HEALTH_TIMEOUT_SECONDS", "2.5").strip()
    try:
        timeout_seconds = max(0.5, float(timeout_raw))
    except ValueError:
        timeout_seconds = 2.5

    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        probes = [
            _fetch_service_health(client, service_name, health_url)
            for service_name, health_url in CORE_SERVICE_HEALTH_URLS.items()
        ]
        services = await asyncio.gather(*probes)

    score = round(sum(service.get("score", 0) for service in services) / max(len(services), 1))
    return score, services


def _get_uptime_days(request: Request) -> int | None:
    started_at = getattr(request.app.state, "started_at", None)
    if not isinstance(started_at, datetime):
        return None

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    elapsed_days = int((datetime.now(timezone.utc) - started_at).total_seconds() // 86400)
    return max(elapsed_days, 0)


# ---------- Endpoints ----------


@router.get("/metrics", response_model=ApiResponse)
async def get_dashboard_metrics(
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """获取仪表盘统计指标（管理员权限）"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async def _safe_fetch_one(session, sql: str) -> dict[str, Any]:
        """
        安全查询单行统计数据。
        在空库或缺表场景下返回空字典，避免管理页 500。
        """
        try:
            rows = await session.execute(text(sql))
            return dict(rows.mappings().first() or {})
        except Exception as exc:
            logger.warning("Admin dashboard metrics query skipped: %s", exc)
            # 失败后清理事务状态，避免后续查询被 InFailedSQLTransactionError 连锁影响。
            try:
                await session.rollback()
            except Exception:
                pass
            return {}

    try:
        async with get_session(read_only=True) as session:
            # 1. 用户统计
            user_row = await _safe_fetch_one(
                session,
                """
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE is_active = true AND is_deleted = false) as active,
                    COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE) as new_today
                FROM users
            """,
            )

            # 2. 策略统计
            strategy_row = await _safe_fetch_one(
                session,
                """
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'ACTIVE') as live
                FROM strategies
            """,
            )

            # 3. 回测统计
            backtest_row = await _safe_fetch_one(
                session,
                """
                SELECT COUNT(*) as backtesting 
                FROM qlib_backtest_runs 
                WHERE status IN ('running', 'pending')
            """,
            )

            # 4. 内容统计 (社区)
            content_row = await _safe_fetch_one(
                session,
                """
                SELECT 
                    (SELECT COUNT(*) FROM community_posts) as posts,
                    (SELECT COUNT(*) FROM community_comments) as comments
            """,
            )

            health_score, services = await _collect_system_health()
            uptime_days = _get_uptime_days(request)

            data = {
                "users": {
                    "total": user_row.get("total") or 0,
                    "active": user_row.get("active") or 0,
                    "new_today": user_row.get("new_today") or 0,
                },
                "strategies": {
                    "total": strategy_row.get("total") or 0,
                    "live": strategy_row.get("live") or 0,
                    "backtesting": backtest_row.get("backtesting") or 0,
                },
                "content": {
                    "posts": content_row.get("posts") or 0,
                    "comments": content_row.get("comments") or 0,
                },
                "system": _build_system_metrics(health_score, uptime_days, services),
            }
        return ApiResponse(success=True, code=200, message="获取成功", data=data)
    except Exception as e:
        logger.error(f"仪表盘指标加载失败: {e}", exc_info=True)
        # 兜底返回空指标，避免前端管理页因单点错误不可用。
        health_score = 0
        services: list[dict[str, Any]] = []
        uptime_days = _get_uptime_days(request)
        return ApiResponse(
            success=True,
            code=200,
            message="指标降级返回",
            data={
                "users": {"total": 0, "active": 0, "new_today": 0},
                "strategies": {"total": 0, "live": 0, "backtesting": 0},
                "content": {"posts": 0, "comments": 0},
                "system": _build_system_metrics(health_score, uptime_days, services),
            },
        )


@router.get("/market-sources", response_model=ApiResponse)
async def get_market_sources_status(
    current_user: dict = Depends(require_admin),
):
    """
    获取行情数据源状态 (在线库 106 + 离线库 139)
    """
    from sqlalchemy import text
    from backend.shared.database_manager_v2 import get_session
    from backend.services.api.routers.admin.model_management_ops import get_data_status
    
    online_status = {
        "status": "checking",
        "latest_date": None,
        "row_count": 0,
        "error": None
    }
    
    try:
        # Check Online Source (106 PostgreSQL)
        async with get_session(read_only=True) as session:
            # Check stock_daily_latest
            sql_max = text("SELECT MAX(trade_date) FROM stock_daily_latest")
            res_max = await session.execute(sql_max)
            max_date = res_max.scalar()
            
            if max_date:
                sql_count = text("SELECT COUNT(*) FROM stock_daily_latest WHERE trade_date = :d")
                res_count = await session.execute(sql_count, {"d": max_date})
                row_count = res_count.scalar() or 0
                
                online_status["latest_date"] = max_date.isoformat() if hasattr(max_date, 'isoformat') else str(max_date)
                online_status["row_count"] = row_count
                online_status["status"] = "healthy" if row_count > 4000 else "degraded"
            else:
                online_status["status"] = "empty"
    except Exception as e:
        logger.error(f"106 DB connection failed: {e}")
        online_status["status"] = "unreachable"
        online_status["error"] = str(e)
        
    try:
        # Check Offline Source (139 Sync Parquet & Qlib)
        offline_status_raw = await get_data_status(refresh=False, current_user=current_user)
    except Exception as e:
        logger.error(f"139 Offline status check failed: {e}")
        offline_status_raw = {"error": str(e), "message": "Failed to check offline status"}

    data = {
        "online_source": online_status,
        "offline_source": offline_status_raw
    }

    return ApiResponse(success=True, code=200, message="获取成功", data=data)

