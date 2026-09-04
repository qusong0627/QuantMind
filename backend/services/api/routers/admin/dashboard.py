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

router = APIRouter(
    dependencies=[Depends(require_admin)],  # 路由器级认证兜底
)
logger = logging.getLogger(__name__)


# ---------- Schemas (内联定义，避免额外文件) ----------


class ApiResponse(BaseModel):
    success: bool
    code: int
    message: str
    data: dict[str, Any] | None = None


# 核心服务健康检查（HTTP）
CORE_SERVICE_HEALTH_URLS = {
    "api": os.getenv("ADMIN_DASHBOARD_API_HEALTH_URL", "http://127.0.0.1:8000/health"),
    "trade": os.getenv("ADMIN_DASHBOARD_TRADE_HEALTH_URL", "http://127.0.0.1:8002/health"),
    "engine": os.getenv("ADMIN_DASHBOARD_ENGINE_HEALTH_URL", "http://127.0.0.1:8001/health"),
    "stream": os.getenv("ADMIN_DASHBOARD_STREAM_HEALTH_URL", "http://127.0.0.1:8003/health"),
}

# 基础设施服务（TCP 端口探测；容器内通过 docker 主机名访问独立容器）
INFRA_SERVICES = [
    {
        "service": "postgres",
        "host": os.getenv("ADMIN_DASHBOARD_DB_HOST", "quantmind-db"),
        "port": int(os.getenv("ADMIN_DASHBOARD_DB_PORT", "5432")),
        "desc": "PostgreSQL 数据库",
    },
    {
        "service": "redis",
        "host": os.getenv("ADMIN_DASHBOARD_REDIS_HOST", "quantmind-redis"),
        "port": int(os.getenv("ADMIN_DASHBOARD_REDIS_PORT", "6379")),
        "desc": "Redis 缓存/队列",
    },
    {
        "service": "data_gateway",
        "host": os.getenv("ADMIN_DASHBOARD_DATA_GATEWAY_HOST", "quantmind-data-gateway"),
        "port": int(os.getenv("ADMIN_DASHBOARD_DATA_GATEWAY_PORT", "8004")),
        "desc": "数据网关 (8004)",
    },
    {
        "service": "web",
        "host": os.getenv("ADMIN_DASHBOARD_WEB_HOST", "quantmind-web"),
        "port": int(os.getenv("ADMIN_DASHBOARD_WEB_PORT", "80")),
        "desc": "Nginx 前端 (80/3080)",
    },
    {
        "service": "qwenpaw",
        "host": os.getenv("ADMIN_DASHBOARD_QWENPAW_HOST", "qwenpaw"),
        "port": int(os.getenv("ADMIN_DASHBOARD_QWENPAW_PORT", "8088")),
        "desc": "QwenPaw AI 助手 (8088)",
    },
    {
        "service": "rsshub",
        "host": os.getenv("ADMIN_DASHBOARD_RSSHUB_HOST", "quantmind-rsshub"),
        "port": int(os.getenv("ADMIN_DASHBOARD_RSSHUB_PORT", "1200")),
        "desc": "RSSHub 订阅源 (1200)",
    },
    {
        "service": "huntly",
        "host": os.getenv("ADMIN_DASHBOARD_HUNTLY_HOST", "quantmind-huntly"),
        "port": int(os.getenv("ADMIN_DASHBOARD_HUNTLY_PORT", "80")),
        "desc": "Huntly RSS 阅读器 (8090)",
    },
]


def _get_host_workload() -> dict[str, Any]:
    """采集宿主机/容器真实 CPU、内存与磁盘负载。"""
    try:
        import psutil

        cpu_percent = round(float(psutil.cpu_percent(interval=None)), 1)
        cpu_count = int(psutil.cpu_count(logical=True) or 1)

        mem = psutil.virtual_memory()
        mem_percent = round(float(mem.percent), 1)
        mem_used_gb = round(float((mem.total - mem.available) / (1024**3)), 2)
        mem_total_gb = round(float(mem.total / (1024**3)), 2)

        try:
            disk = psutil.disk_usage(os.getcwd())
            disk_percent = round(float(disk.percent), 1)
            disk_used_gb = round(float(disk.used / (1024**3)), 2)
            disk_total_gb = round(float(disk.total / (1024**3)), 2)
        except Exception:
            disk_percent = 0.0
            disk_used_gb = 0.0
            disk_total_gb = 0.0

        return {
            "cpu_percent": cpu_percent,
            "cpu_count": cpu_count,
            "memory_percent": mem_percent,
            "memory_used_gb": mem_used_gb,
            "memory_total_gb": mem_total_gb,
            "disk_percent": disk_percent,
            "disk_used_gb": disk_used_gb,
            "disk_total_gb": disk_total_gb,
        }
    except Exception as exc:
        logger.warning("Failed to collect host workload via psutil: %s", exc)
        return {
            "cpu_percent": 0.0,
            "cpu_count": 1,
            "memory_percent": 0.0,
            "memory_used_gb": 0.0,
            "memory_total_gb": 0.0,
            "disk_percent": 0.0,
            "disk_used_gb": 0.0,
            "disk_total_gb": 0.0,
        }


def _build_system_metrics(
    health_score: int,
    uptime_days: int | None,
    services: list[dict[str, Any]],
) -> dict[str, Any]:
    """构造系统指标，基于真实健康检查结果与物理负载。"""
    overall_status = "healthy" if services and all(service.get("status") == "healthy" for service in services) else "degraded"
    if not services:
        overall_status = "degraded"

    workload = _get_host_workload()
    healthy_count = sum(1 for s in services if s.get("healthy"))
    total_count = len(services)

    return {
        "health_score": health_score,
        "uptime_days": uptime_days,
        "status": overall_status,
        "services": services,
        "workload": workload,
        "services_summary": {
            "healthy": healthy_count,
            "total": total_count,
        },
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


async def _fetch_tcp_service_health(service: dict[str, Any]) -> dict[str, Any]:
    """TCP 端口探测基础设施服务（DB/Redis/Web 等）。"""
    import socket

    host = service.get("host", "127.0.0.1")
    port = int(service.get("port", 0))
    name = service.get("service", "unknown")
    try:
        loop = asyncio.get_running_loop()
        conn = await asyncio.wait_for(
            loop.run_in_executor(None, socket.create_connection, (host, port), 1.5),
            timeout=2.0,
        )
        conn.close()
        return {
            "service": name,
            "host": host,
            "port": port,
            "status": "healthy",
            "score": 100,
            "healthy": True,
            "desc": service.get("desc", ""),
        }
    except Exception as exc:
        logger.warning("Admin dashboard TCP probe failed for %s:%s: %s", host, port, exc)
        return {
            "service": name,
            "host": host,
            "port": port,
            "status": "unreachable",
            "score": 0,
            "healthy": False,
            "desc": service.get("desc", ""),
            "error": str(exc),
        }


async def _fetch_celery_health(service_name: str, redis_db: int = 3) -> dict[str, Any]:
    """通过 Redis 中 celery 的 pidbox 键判断 worker/beat 是否活跃。

    celery 容器与 quantmind 共享网络、无独立监听端口，故以 Redis 注册信息为准。
    """
    try:
        import redis as _redis

        client = _redis.from_url(
            os.getenv("REDIS_URL", "redis://quantmind-redis:6379"),
            socket_timeout=2,
            db=redis_db,
        )
        # pidbox 存在说明 celery worker 已注册并活跃
        keys = client.keys("*pidbox*")
        client.close()
        alive = any(b"pidbox" in k if isinstance(k, bytes) else "pidbox" in str(k) for k in keys)
        return {
            "service": service_name,
            "status": "healthy" if alive else "degraded",
            "score": 100 if alive else 60,
            "healthy": alive,
            "desc": "Celery Worker 异步任务" if service_name == "celery" else "Celery Beat 定时调度",
        }
    except Exception as exc:
        logger.warning("Admin dashboard celery probe failed for %s: %s", service_name, exc)
        return {
            "service": service_name,
            "status": "unreachable",
            "score": 0,
            "healthy": False,
            "desc": "Celery Worker 异步任务" if service_name == "celery" else "Celery Beat 定时调度",
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
        http_services = await asyncio.gather(*probes)

    infra_services = await asyncio.gather(
        *[_fetch_tcp_service_health(service) for service in INFRA_SERVICES]
    )

    celery_services = await asyncio.gather(
        _fetch_celery_health("celery"),
        _fetch_celery_health("celery_beat"),
    )
    services = list(http_services) + list(infra_services) + list(celery_services)

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

            # 4. 模型统计 (用户训练产出的模型)
            model_row = await _safe_fetch_one(
                session,
                """
                SELECT COUNT(*) as total FROM qm_user_models
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
                "models": {
                    "total": model_row.get("total") or 0,
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
                "models": {"total": 0},
                "system": _build_system_metrics(health_score, uptime_days, services),
            },
        )


@router.get("/system-load", response_model=ApiResponse)
async def get_system_load(
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """获取轻量级系统实时物理负载与服务概要（供侧边栏等低延迟高频轮询）"""
    _ = current_user
    workload = _get_host_workload()
    uptime_days = _get_uptime_days(request)

    health_score, services = await _collect_system_health()
    healthy_count = sum(1 for s in services if s.get("healthy"))
    total_count = len(services)

    return ApiResponse(
        success=True,
        code=200,
        message="获取系统负载成功",
        data={
            "workload": workload,
            "uptime_days": uptime_days,
            "health_score": health_score,
            "services_summary": {
                "healthy": healthy_count,
                "total": total_count,
            },
        },
    )

