import asyncio
import importlib
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.shared.auth import AuthManager, get_internal_call_secret
from backend.shared.config_manager import init_unified_config
from backend.shared.cors import resolve_cors_origins
from backend.shared.database_pool import init_default_databases as init_sync_db_pool
from backend.shared.error_contract import install_error_contract_handlers
from backend.shared.logging_config import get_logger
from backend.shared.openapi_utils import quantmind_generate_unique_id
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.service_health_metrics import (
    build_metrics_response,
    set_service_health,
)

logger = get_logger(__name__)


# 兼容 qlib_app 内部裸导入路径（from qlib_app.*）
if "qlib_app" not in sys.modules:
    sys.modules["qlib_app"] = importlib.import_module("backend.services.engine.qlib_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.startup_healthy = True
    try:
        await init_unified_config(service_name="quantmind-engine")
        logger.info("✅ QuantMind Engine Unified Config Loaded")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Unified config load failed: {e}")

    try:
        init_sync_db_pool(pool_size=20, max_overflow=10)
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Sync database pool init failed: {e}")

    try:
        await _bootstrap_qlib_runtime()
        logger.info("✅ Qlib runtime bootstrap completed")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Qlib runtime bootstrap failed: {e}")

    try:
        from backend.shared.model_registry import model_registry_service

        await model_registry_service.ensure_tables()
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Model registry table ensure failed: {e}")

    warmup_enabled = os.getenv("AI_STRATEGY_WARMUP", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if warmup_enabled:
        timeout_raw = os.getenv("AI_STRATEGY_WARMUP_TIMEOUT_SECONDS", "60").strip()
        try:
            warmup_timeout_seconds = max(1.0, float(timeout_raw))
        except ValueError:
            warmup_timeout_seconds = 60.0
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_run_ai_strategy_warmup_sync),
                timeout=warmup_timeout_seconds,
            )
            logger.info("✅ AI Strategy Warmup completed")
        except asyncio.TimeoutError:
            logger.warning(
                "⚠️ AI Strategy Warmup timeout after %.1fs; startup continues",
                warmup_timeout_seconds,
            )
            # Timeout is not critical - service can still function
        except Exception as e:
            logger.warning(f"⚠️ AI Strategy Warmup skipped: {e}")
            # Missing API key or other non-critical error - service can still function

    set_service_health("quantmind-engine", bool(getattr(app.state, "startup_healthy", True)))

    # 启动 VectorizedMatcher（受 ENABLE_VECTORIZED_MATCHER 环境变量控制）
    vm_task: asyncio.Task | None = None
    try:
        from backend.services.trade.services.vectorized_matcher import VectorizedMatcher
        _vm = VectorizedMatcher()
        vm_task = asyncio.create_task(_vm.start(), name="vectorized_matcher")
        app.state.vectorized_matcher = _vm
        logger.info("✅ VectorizedMatcher task created (active if ENABLE_VECTORIZED_MATCHER=true)")
    except Exception as e:
        logger.warning(f"⚠️ VectorizedMatcher startup skipped: {e}")

    yield

    if vm_task and not vm_task.done():
        try:
            app.state.vectorized_matcher.stop()
            await asyncio.wait_for(vm_task, timeout=5.0)
        except Exception:
            vm_task.cancel()
    logger.info("🔚 QuantMind Engine shutdown complete")


def _run_ai_strategy_warmup_sync() -> None:
    from backend.services.engine.ai_strategy.services.selection.schema_retriever import (
        get_schema_retriever,
    )
    from backend.services.engine.ai_strategy.services.selection.vector_parser import (
        get_strategy_vector_parser,
    )

    asyncio.run(get_strategy_vector_parser())
    asyncio.run(get_schema_retriever())


async def _bootstrap_qlib_runtime() -> None:
    """在 engine 生命周期内补齐 qlib 初始化与回测表检查。"""
    from backend.services.engine.qlib_app import get_qlib_service
    from backend.services.engine.qlib_app.services.backtest_persistence import (
        BacktestPersistence,
    )
    from backend.services.engine.qlib_app.services.optimization_persistence import (
        OptimizationPersistence,
    )

    qlib_service = get_qlib_service()
    await asyncio.to_thread(qlib_service.initialize)
    await BacktestPersistence().ensure_tables()
    await OptimizationPersistence().ensure_tables()


app = FastAPI(
    title="QuantMind Computational Engine",
    version="2.0.0",
    description="收敛后的计算引擎服务（整合了 AI 策略生成、模型推理、回测等模块）",
    lifespan=lifespan,
    generate_unique_id_function=quantmind_generate_unique_id,
)

install_request_id_middleware(app)
install_error_contract_handlers(app)
install_access_log_middleware(app, service_name="quantmind-engine")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    统一认证中间件：支持内部信任 Header (X-User-Id) 或直接通过 JWT 令牌 (Bearer) 校验。
    """
    path = request.url.path
    method = request.method.upper()
    internal_secret = request.headers.get("X-Internal-Call")
    expected_secret = get_internal_call_secret()

    # 1. 尝试从网关透传的信任 Header 获取
    user_id = request.headers.get("X-User-Id")
    tenant_id = request.headers.get("X-Tenant-Id", "default")

    # 2. 如果没有信任 Header，尝试直接校验 JWT (支持 Nginx 直接转发)
    auth_header = request.headers.get("Authorization")
    if not user_id and auth_header and auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ")[1]
            payload = AuthManager().verify_token(token)
            user_id = str(payload.get("sub") or payload.get("user_id") or "")
            tenant_id = str(payload.get("tenant_id") or "default")
        except Exception:
            # 校验失败不立即报错，留待具体的路由逻辑（或 internal_secret 检查）决定
            pass

    # 所有 /api/v1/* 业务路由必须通过内部密钥或有效的用户身份。
    # 只有在内部密钥不匹配且用户身份也缺失的情况下才报错（OPTIONS 放行）。
    if method != "OPTIONS" and path.startswith("/api/v1/"):
        if internal_secret != expected_secret and not user_id:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required (Invalid internal secret or missing user context)"},
            )

    if user_id:
        # 注入到 request.state
        request.state.user = {"user_id": user_id, "tenant_id": tenant_id, "sub": user_id}

    response = await call_next(request)
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. AI 策略生成
try:
    from backend.services.engine.ai_strategy.api.routes.strategy_backtest_loop import router as strategy_loop_router
    from backend.services.engine.ai_strategy.api.v1.routes import router as ai_strat_router
    from backend.services.engine.ai_strategy.api.v1.wizard import router as wizard_router

    app.include_router(ai_strat_router, prefix="/api/v1", tags=["AI Strategy"])
    app.include_router(wizard_router, prefix="/api/v1", tags=["Strategy Wizard"])
    app.include_router(strategy_loop_router, prefix="/api/v1", tags=["Strategy Loop"])
except ImportError as e:
    logger.error(f"❌ Failed to load AI Strategy routers: {e}")


# 2. 模型推理
try:
    from backend.services.engine.routers.inference import router as inference_router

    app.include_router(inference_router, prefix="/api/v1/inference", tags=["AI Inference"])
except ImportError as e:
    logger.error(f"❌ Failed to load AI Inference router: {e}")

# 3. 闭环编排
try:
    from backend.services.engine.routers.pipeline import router as pipeline_router

    app.include_router(pipeline_router, prefix="/api/v1/pipeline", tags=["Pipeline"])
except ImportError as e:
    logger.error(f"❌ Failed to load Pipeline router: {e}")

# 3.1 实盘E2E最小契约接口
try:
    from backend.services.engine.routers.realtime_contract import router as realtime_contract_router

    app.include_router(realtime_contract_router, prefix="/api/v1")
except ImportError as e:
    logger.error(f"❌ Failed to load Realtime Contract router: {e}")

# 4. 量化回测
try:
    from backend.services.engine.qlib_app.api.backtest import router as backtest_router

    app.include_router(backtest_router, prefix="/api/v1", tags=["Qlib Backtest"])
except ImportError as e:
    logger.error(f"❌ Failed to load Qlib Backtest router: {e}")

# 4.1 高级分析
try:
    from backend.services.engine.qlib_app.api.analysis import router as analysis_router

    # analysis_router 自带 /api/v1/analysis 前缀
    app.include_router(analysis_router, tags=["Qlib Analysis"])
except ImportError as e:
    logger.error(f"❌ Failed to load Qlib Analysis router: {e}")

# 5. 用户策略管理
try:
    from backend.services.engine.qlib_app.api.user_strategies import router as strategies_router

    app.include_router(strategies_router, prefix="/api/v1/strategies", tags=["Strategies"])
except ImportError as e:
    logger.error(f"❌ Failed to load Strategies router: {e}")

# 5.1 管理员策略模板管理
try:
    from backend.services.engine.qlib_app.api.admin_templates import router as admin_templates_router

    app.include_router(
        admin_templates_router,
        prefix="/api/v1/admin/strategy-templates",
        tags=["Admin-StrategyTemplates"],
    )
    logger.info("✅ Admin Strategy Templates router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Admin Strategy Templates router: {e}")

# 6. 股票查询与智能选股
try:
    from backend.services.engine.stock_query_app.routes import router as stock_router
    from backend.services.engine.stock_query_app.smart_screener_api import router as smart_screener_router

    app.include_router(stock_router)
    app.include_router(smart_screener_router)
    logger.info("✅ Stock Query & Smart Screener routers loaded")
except ImportError as e:
    logger.warning(f"⚠️ Stock Query router not available: {e}")


try:
    from backend.services.engine.routers.ai_ide.chat import router as ai_chat_router
    from backend.services.engine.routers.ai_ide.config import router as ai_config_router
    from backend.services.engine.routers.ai_ide.executor import router as ai_executor_router
    from backend.services.engine.routers.ai_ide.workspace import router as ai_workspace_router

    app.include_router(ai_chat_router, prefix="/api/v1/ai-ide/ai", tags=["Cloud IDE-AI"])
    app.include_router(ai_config_router, prefix="/api/v1/ai-ide/config", tags=["Cloud IDE-Config"])
    app.include_router(ai_executor_router, prefix="/api/v1/ai-ide/execute", tags=["Cloud IDE-Executor"])
    app.include_router(ai_workspace_router, prefix="/api/v1/ai-ide/files", tags=["Cloud IDE-Workspace"])
    logger.info("✅ Cloud AI-IDE routers loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Cloud AI-IDE routers: {e}")


@app.get("/health")
async def health_check():
    startup_healthy = bool(getattr(app.state, "startup_healthy", True))
    set_service_health("quantmind-engine", startup_healthy)
    return {
        "status": "healthy" if startup_healthy else "degraded",
        "service": "quantmind-engine",
    }


@app.get("/")
async def root():
    return {"message": "QuantMind Engine Core V2 is running"}


@app.get("/metrics")
async def metrics():
    return build_metrics_response()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, access_log=False)
