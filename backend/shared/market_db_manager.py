import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

# ============================================================
# 行情专用 PostgreSQL 硬编码配置 (106.53.100.144 只读用户)
# 密码使用 Fernet 对称加密存储，运行时解密
# ============================================================
_MARKET_DB_FERNET_KEY = b"-cP0xGlpx1dyqj58DZhuUShHH3cvkgG71ee-9xCpR6c="
_MARKET_DB_ENCRYPTED_PASSWORD = (
    b"gAAAAABqEGoR4G_jR-ldndJ7dGP8g_WoCRztHJxkbs9kveFdAwbyxSfYuVfe7BlBTTB1vbchc5_QhWyBxJ9dVkARszQSqrHdi2hs79wxtL4HVdQT7CHcRO0="
)


def _decrypt_market_db_password() -> str:
    """解密行情数据库密码"""
    return Fernet(_MARKET_DB_FERNET_KEY).decrypt(_MARKET_DB_ENCRYPTED_PASSWORD).decode()


MARKET_DB_HOST = "106.53.100.144"
MARKET_DB_PORT = 5432
MARKET_DB_USER = "quantmind_market"
MARKET_DB_PASSWORD = _decrypt_market_db_password()
MARKET_DB_NAME = "quantmind"

MARKET_DB_URL = f"postgresql+asyncpg://{MARKET_DB_USER}:{MARKET_DB_PASSWORD}@{MARKET_DB_HOST}:{MARKET_DB_PORT}/{MARKET_DB_NAME}"

_market_engine: AsyncEngine | None = None
_market_session_factory: sessionmaker | None = None


def get_market_engine() -> AsyncEngine:
    """获取外部行情数据库的异步引擎"""
    global _market_engine, _market_session_factory
    if _market_engine is None:
        logger.info(f"Initializing external market DB engine connected to {MARKET_DB_HOST}:{MARKET_DB_PORT}")
        _market_engine = create_async_engine(
            MARKET_DB_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            echo=False,
        )
        _market_session_factory = sessionmaker(
            _market_engine, class_=AsyncSession, expire_on_commit=False
        )
    return _market_engine


@asynccontextmanager
async def get_market_session() -> AsyncGenerator[AsyncSession, None]:
    """获取外部行情数据库会话"""
    get_market_engine()  # Ensure engine is initialized
    async with _market_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
