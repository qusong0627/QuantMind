import logging
import time
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class StartupHealthState:
    """启动健康状态单例"""
    def __init__(self):
        self.start_time = time.monotonic()
        self.warmup_completed = False
        self.warmup_duration = 0.0
        self.errors = []
        self.initialized = False

    def mark_completed(self):
        self.warmup_completed = True
        self.warmup_duration = time.monotonic() - self.start_time
        self.initialized = True

    def add_error(self, error: str):
        self.errors.append(error)
        self.initialized = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "healthy" if self.warmup_completed and not self.errors else "degraded",
            "warmup_completed": self.warmup_completed,
            "warmup_duration": round(self.warmup_duration, 2),
            "errors": self.errors,
            "uptime": round(time.monotonic() - self.start_time, 2)
        }

_state = StartupHealthState()

def get_startup_health_state() -> StartupHealthState:
    return _state

async def run_startup_health_checks():
    """执行启动健康检查与强制预热"""
    state = get_startup_health_state()
    logger.info("🚀 Starting AI Strategy startup health checks and mandatory warmup...")
    
    try:
        from .selection.vector_parser import get_strategy_vector_parser
        from .selection.schema_retriever import get_schema_retriever
        from ..provider_registry import get_provider

        # 1. 检查 LLM Provider
        get_provider()
        logger.info("✅ LLM Provider check passed")

        # 2. 强制预热向量解析器
        await get_strategy_vector_parser()
        logger.info("✅ Strategy vector parser warmup passed")

        # 3. 强制预热 Schema 检索器
        await get_schema_retriever()
        logger.info("✅ Schema retriever warmup passed")

        state.mark_completed()
        logger.info("✨ AI Strategy startup health checks completed successfully")
        
    except Exception as e:
        error_msg = f"Startup health check failed: {str(e)}"
        logger.error(f"❌ {error_msg}")
        state.add_error(error_msg)
        # 强制抛出异常以阻断启动
        raise RuntimeError(error_msg) from e
