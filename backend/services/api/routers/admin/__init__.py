from fastapi import APIRouter

from .dashboard import router as dashboard_router
from .model_management import router as model_management_router
from .model_management_ops import router as model_management_ops_router
from .admin_training import router as admin_training_router
from .strategy_templates import router as strategy_templates_router
from .users import router as users_router

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
