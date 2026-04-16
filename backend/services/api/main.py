import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.services.api.routers import (
    auth,
    inquiry,
    notifications,
    profiles,
    system,
    trading_calendar,
    users,
)
from backend.services.api.routers.asset import router as asset_router
from backend.services.api.routers.admin import admin_router
from backend.services.api.routers.ai_ide_proxy import router as ai_ide_proxy_router
from backend.services.api.routers.community.router import router as community_router
from backend.services.api.routers.copaw_proxy import router as copaw_proxy_router
from backend.services.api.routers.engine_proxy import router as engine_proxy_router
from backend.services.api.routers.files import router as files_router
from backend.services.api.routers.model_training import router as model_training_router
from backend.services.api.routers.stocks_search import router as stocks_search_router
from backend.services.api.routers.trade_proxy import router as trade_proxy_router
from backend.services.api.user_app.api.v1.api_keys import router as api_keys_router

# OSS 版本禁用订阅功能
# from backend.services.api.user_app.api.v1.subscriptions import (
#     router as subscriptions_router,
# )
from backend.shared.config_manager import init_unified_config
from backend.shared.cors import resolve_cors_origins
from backend.shared.database_pool import init_default_databases as init_sync_db_pool
from backend.shared.error_contract import install_error_contract_handlers
from backend.shared.logging_config import get_logger, setup_logging
from backend.shared.openapi_utils import quantmind_generate_unique_id
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.service_health_metrics import (
    build_metrics_response,
    set_service_health,
)

setup_logging(service_name="quantmind-api")
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.started_at = datetime.now(timezone.utc)
    app.state.startup_healthy = True
    try:
        await init_unified_config(service_name="quantmind-api")
        init_sync_db_pool(pool_size=20)

        from backend.shared.database_manager_v2 import init_database

        await init_database()

        from backend.services.api.routers.admin.model_management import (
            ensure_admin_tables,
        )

        await ensure_admin_tables()
        from backend.shared.model_registry import model_registry_service

        await model_registry_service.ensure_tables()
        from backend.services.engine.services.model_inference_persistence import (
            model_inference_persistence,
        )

        await model_inference_persistence.ensure_tables()

        logger.info("✅ QuantMind API initialized")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ API initialization failed: {e}", exc_info=True)

    set_service_health("quantmind-api", bool(app.state.startup_healthy))
    yield
    logger.info("🔚 QuantMind API shutdown complete")


app = FastAPI(
    title="QuantMind Consolidated API",
    version="2.0.0",
    description="用户、认证、交易、引擎统一网关服务",
    lifespan=lifespan,
    generate_unique_id_function=quantmind_generate_unique_id,
)

# 1. 中间件
install_request_id_middleware(app)
install_error_contract_handlers(app)
install_access_log_middleware(app, service_name="quantmind-api")

# 2. 注册具体业务路由 (高优先级)
os.makedirs("data/uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="data/uploads"), name="uploads")
app.include_router(auth.router, prefix="/api/v1", tags=["Auth"])
app.include_router(system.router)

# OSS 版本禁用社区功能
# if settings.edition != "oss":
#     app.include_router(community_router)

app.include_router(users.router, prefix="/api/v1/users", tags=["Users"])
app.include_router(profiles.router, prefix="/api/v1/profiles", tags=["Profiles"])
app.include_router(notifications.router, prefix="/api/v1", tags=["Notifications"])
app.include_router(inquiry.router, prefix="/api/v1", tags=["Inquiry"])
app.include_router(files_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1/admin")
app.include_router(
    model_training_router, prefix="/api/v1/models", tags=["ModelTraining"]
)
app.include_router(stocks_search_router)
app.include_router(trading_calendar.router)
app.include_router(api_keys_router, prefix="/api/v1")
app.include_router(asset_router, prefix="/api/v1/asset", tags=["Asset"])
# OSS 版本禁用订阅功能
# app.include_router(
#     subscriptions_router, prefix="/api/v1/subscription", tags=["Subscriptions"]
# )

# 3. 注册代理路由 (低优先级，兜底捕获)
app.include_router(engine_proxy_router)
app.include_router(trade_proxy_router)
app.include_router(ai_ide_proxy_router)
app.include_router(copaw_proxy_router)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    startup_healthy = bool(getattr(app.state, "startup_healthy", True))
    set_service_health("quantmind-api", startup_healthy)
    return {
        "status": "healthy" if startup_healthy else "degraded",
        "service": "quantmind-api",
    }


@app.get("/")
async def root():
    return {"message": "QuantMind API Service V2 is running"}


@app.get("/metrics")
async def metrics():
    return build_metrics_response()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
