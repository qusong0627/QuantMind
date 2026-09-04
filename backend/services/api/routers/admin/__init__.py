from fastapi import APIRouter

from .dashboard import router as dashboard_router
from .data_platform import router as data_platform_router
from .quantdb_console import router as quantdb_console_router
from .quantus_console import router as quantus_console_router
from .quanthk_console import router as quanthk_console_router
from .quantbc_console import router as quantbc_console_router
from .quantfutures_console import router as quantfutures_console_router
from .model_management import router as model_management_router
from .model_management_ops import router as model_management_ops_router
from .admin_training import router as admin_training_router
from .strategy_templates import router as strategy_templates_router
from .users import router as users_router
from .alpha_factor_pipeline import router as alpha_factor_pipeline_router
from .trading_agents import router as trading_agents_router
from .sync_schedule import router as sync_schedule_router
from .quantdb_factor_catalog import router as quantdb_factor_catalog_router
from .qlib_console import router as qlib_console_router
from .system_update import router as system_update_router
from .node_history import router as node_history_router
from .system_events import router as system_events_router

admin_router = APIRouter()
admin_router.include_router(
    dashboard_router, prefix="/dashboard", tags=["Admin-Dashboard"]
)
admin_router.include_router(
    admin_training_router, prefix="/models", tags=["Admin-ModelTraining"]
)
admin_router.include_router(
    model_management_router, prefix="/models", tags=["Admin-ModelManagement"]
)
admin_router.include_router(
    model_management_ops_router, prefix="/data", tags=["Admin-DataManagement"]
)
admin_router.include_router(
    users_router, prefix="/users", tags=["Admin-Users"]
)
admin_router.include_router(
    strategy_templates_router, prefix="/strategy-templates", tags=["Admin-StrategyTemplates"]
)
admin_router.include_router(
    data_platform_router, prefix="/data-platform", tags=["Admin-DataPlatform"]
)
admin_router.include_router(
    quantdb_console_router, prefix="/data-platform/quantdb", tags=["Admin-QuantDB"]
)
admin_router.include_router(
    quantus_console_router, prefix="/data-platform/quantus", tags=["Admin-QuantUS"]
)
admin_router.include_router(
    quanthk_console_router, prefix="/data-platform/quanthk", tags=["Admin-QuantHK"]
)
admin_router.include_router(
    quantbc_console_router, prefix="/data-platform/quantbc", tags=["Admin-QuantBC"]
)
admin_router.include_router(
    quantfutures_console_router, prefix="/data-platform/quantfutures", tags=["Admin-QuantFutures"]
)
admin_router.include_router(
    alpha_factor_pipeline_router, prefix="/alpha-factors", tags=["Admin-AlphaFactorPipeline"]
)
admin_router.include_router(
    trading_agents_router, prefix="/trading-agents", tags=["Admin-TradingAgents"]
)
admin_router.include_router(
    sync_schedule_router, prefix="/data-platform", tags=["Admin-SyncSchedule"]
)
admin_router.include_router(
    quantdb_factor_catalog_router, prefix="/training-data", tags=["Admin-TrainingData"]
)
admin_router.include_router(
    qlib_console_router, prefix="/data-platform/qlib", tags=["Admin-Qlib"]
)
admin_router.include_router(
    system_update_router, prefix="/system", tags=["Admin-SystemUpdate"]
)
admin_router.include_router(
    node_history_router, prefix="/dashboard", tags=["Admin-NodeHistory"]
)
admin_router.include_router(
    system_events_router, prefix="/system-events", tags=["Admin-SystemEvents"]
)
