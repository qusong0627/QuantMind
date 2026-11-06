import logging

from fastapi import APIRouter

from .real_trading_preflight import router as preflight_router
from .real_trading_ledger import router as ledger_router
from .manual_executions import router as manual_executions_router
from .real_trading_lifecycle import router as lifecycle_router
from .broker_records import router as broker_records_router
from .agent_statement import router as agent_statement_router
from .daily_settlements import router as daily_settlements_router

router = APIRouter()
logger = logging.getLogger(__name__)

router.include_router(preflight_router)
router.include_router(ledger_router)
router.include_router(manual_executions_router)
router.include_router(lifecycle_router)
router.include_router(broker_records_router)
router.include_router(agent_statement_router)
router.include_router(daily_settlements_router)
