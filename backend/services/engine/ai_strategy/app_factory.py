#!/usr/bin/env python3
"""
AI策略服务应用工厂模块
负责创建和配置FastAPI应用
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api import (
    RequestLoggingMiddleware,
    chat_stream_handler,
    health_check_handler,
    root_handler,
)
from .services import close_strategy_service

logger = logging.getLogger(__name__)

# 添加路径以便导入共享模块
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/shared")
sys.path.insert(0, "/app/backend/shared")

# 导入共享模块
try:
    from backend.shared.config import settings
    from backend.shared.database import engine as _shared_engine
    from backend.shared.observability.logging import init_service_logging
    from backend.shared.observability.tracing import init_tracing
except ImportError as e:
    print(f"Warning: Could not import shared modules: {e}")
    settings = None
    _shared_engine = None
    init_tracing = None
    init_service_logging = None


def _get_app_settings():
    if not settings:
        return None
    return getattr(settings, "ai_strategy_app", None) or getattr(settings, "app", None)


def create_app(logger: logging.Logger | None = None) -> FastAPI:
    """
    创建FastAPI应用

    Args:
        logger: 日志记录器实例

    Returns:
        配置好的FastAPI应用实例
    """
    # 创建FastAPI应用
    app = FastAPI(
        title="QuantMind AI Strategy Service",
        description="AI策略生成服务 - 阿里云千问(Qwen)集成版",
        version="3.1.0",
    )

    # 初始化日志系统
    if logger is None:
        app_settings = _get_app_settings()
        if init_service_logging and settings:
            logger = init_service_logging(
                service_name="ai-strategy",
                service_version="3.1.0",
                log_level=getattr(app_settings, "log_level", "INFO"),
            )
        else:
            logging.basicConfig(level=logging.INFO)
            logger = logging.getLogger(__name__)

    # 初始化数据库连接池
    try:
        from backend.shared.database_pool import init_default_databases

        init_default_databases()
        logger.info("Default databases initialized")
    except Exception as e:
        logger.error(f"Failed to initialize default databases: {e}")

    # 注册路由
    _register_routes(app)

    # 配置中间件
    _configure_middleware(app, logger)

    # 初始化分布式追踪
    _initialize_tracing(app)

    # 注册应用事件
    _register_events(app)

    return app


def _register_routes(app: FastAPI):
    """注册应用路由"""

    # 基础路由
    app.get("/")(root_handler)
    app.get("/health")(health_check_handler)

    # 优先使用 v1 路由（功能更完整）
    v1_registered = False
    try:
        from .api.v1.routes import router as v1_router

        app.include_router(v1_router, prefix="/api/v1")
        logger.info("V1 routes registered successfully")
        v1_registered = True
    except ImportError as e:
        logger.warning(f"Could not import v1 routes: {e}")
        # 尝试绝对路径导入
        try:
            from backend.services.engine.ai_strategy.api.v1.routes import router as v1_router

            app.include_router(v1_router, prefix="/api/v1")
            logger.info("V1 routes registered successfully (absolute)")
            v1_registered = True
        except ImportError as e2:
            logger.warning(f"Could not import v1 routes via absolute path: {e2}")

    # Wizard 路由统一在应用工厂挂载，避免 main.py/engine.py 二次挂载造成入口分叉。
    try:
        from .api.v1.wizard import router as wizard_router

        app.include_router(wizard_router, prefix="/api/v1")
        logger.info("Wizard routes registered successfully")
    except ImportError as e:
        logger.warning(f"Could not import wizard routes: {e}")
        try:
            from backend.services.engine.ai_strategy.api.v1.wizard import router as wizard_router

            app.include_router(wizard_router, prefix="/api/v1")
            logger.info("Wizard routes registered successfully (absolute)")
        except ImportError as e2:
            logger.warning(f"Could not import wizard routes via absolute path: {e2}")

    # 如果 v1 路由不可用，注册最小功能以保证可用性
    if not v1_registered:
        app.post("/api/v1/chat/stream")(chat_stream_handler)


def _configure_middleware(app: FastAPI, logger: logging.Logger):
    """配置中间件"""

    # CORS中间件
    if settings:
        cors_origins = settings.get_cors_origins()
    else:
        cors_origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def gateway_context_middleware(request: Request, call_next):
        """从网关标准头注入用户上下文，统一 request.state 字段。"""
        trace_id = (
            request.headers.get("X-Trace-Id")
            or request.headers.get("X-Request-Id")
            or request.headers.get("traceparent")
        )
        if trace_id and not getattr(request.state, "trace_id", None):
            request.state.trace_id = trace_id

        # 若上游鉴权中间件已设置 request.state.user，则不覆盖。
        if not getattr(request.state, "user", None):
            user_id = request.headers.get("X-User-Id")
            tenant_id = request.headers.get("X-Tenant-Id")
            if user_id or tenant_id:
                request.state.user = {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                }

        response = await call_next(request)
        if trace_id:
            response.headers["X-Trace-Id"] = trace_id
        return response

    # 请求日志中间件
    request_logging_middleware = RequestLoggingMiddleware(logger)
    app.middleware("http")(request_logging_middleware)


def _initialize_tracing(app: FastAPI):
    """初始化分布式追踪"""
    if init_tracing:
        try:
            enabled = init_tracing(app, sqlalchemy_engine=_shared_engine)
            if enabled:
                logger.info("Tracing enabled for AI strategy service")
            else:
                logger.info("Tracing not enabled (dependency missing or flag off)")
        except Exception as e:
            logger.warning(f"Tracing init failed for AI strategy service: {e}")
    else:
        logger.info("Tracing module not available")


def _register_events(app: FastAPI):
    """注册应用事件"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期事件"""
        # 0. 统一配置加载 (2026-02-14 统一管理方案)
        try:
            from backend.shared.config_manager import init_unified_config

            await init_unified_config(service_name="ai-strategy")
        except Exception as e:
            logger.warning(f"Unified config load failed for ai-strategy: {e}")

        # 启动事件
        logger.info("AI Strategy Service starting up...")

        # 从统一配置获取服务端口
        if settings:
            port = settings.get_service_port("ai_strategy")
            logger.info(f"Service configured to run on port {port}")

        # 检查Qwen可用性
        from .provider_registry import get_provider

        try:
            get_provider()
            logger.info("Qwen LLM provider initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Qwen provider: {e}")

        # 检查并预热向量解析/字段检索（2026-05-03：暂时关闭强制预热）
        warmup_enabled = os.getenv("AI_STRATEGY_WARMUP", "false").strip().lower() not in ("0", "false", "no", "off")
        if warmup_enabled:
            try:
                from .services.startup_health import run_startup_health_checks
                await run_startup_health_checks()
            except Exception as e:
                logger.critical(f"FATAL: AI Strategy mandatory warmup failed: {e}")
                # 在生产环境下建议直接抛出异常阻断启动
                if os.getenv("ENV") == "prod":
                    raise
        else:
            logger.info("Warmup disabled by env: AI_STRATEGY_WARMUP=false")

        yield

        # 关闭事件
        logger.info("AI Strategy Service shutting down...")

        # 关闭策略服务
        await close_strategy_service()

        logger.info("AI Strategy Service shutdown complete")

    # 应用生命周期管理器
    app.router.lifespan_context = lifespan


def get_service_port() -> int:
    """获取服务端口"""
    if settings:
        return settings.get_service_port("ai_strategy")
    return int(os.getenv("AI_STRATEGY_PORT", 8008))


def get_port() -> int:
    """别名，供 main.py 使用"""
    return get_service_port()


def get_host() -> str:
    """获取服务主机地址"""
    app_settings = _get_app_settings()
    if app_settings:
        return getattr(app_settings, "api_host", "0.0.0.0")
    # 2026-02-14 默认 0.0.0.0 允许 Docker 容器访问
    return os.getenv("API_HOST", "0.0.0.0")


def get_log_level() -> str:
    """获取日志级别"""
    app_settings = _get_app_settings()
    if app_settings:
        return str(getattr(app_settings, "log_level", "info")).lower()
    return os.getenv("LOG_LEVEL", "info").lower()
