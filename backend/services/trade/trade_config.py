"""
Trading Service Configuration
"""

import os
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Trading Service settings"""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    # Service Info
    SERVICE_NAME: str = "trading-service"
    SERVICE_VERSION: str = "1.0.0"
    HOST: str = "0.0.0.0"
    PORT: int = 8002

    # Database (Unified Configuration)
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:@localhost:5432/quantmind",
    )
    DATABASE_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "20"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "30"))

    # Redis (Unified - OSS Edition)
    REDIS_HOST: str = Field(default="quantmind-redis")
    REDIS_PORT: int = Field(default=6379)
    REDIS_DB: int = int(os.getenv("REDIS_DB_TRADE", "2"))
    REDIS_PASSWORD: str | None = Field(default="")

    # Redis Sentinel (Disabled in OSS)
    REDIS_SENTINEL_ENABLED: bool = False
    REDIS_SENTINEL_HOSTS: str = "localhost:26379"
    REDIS_MASTER_NAME: str = "quantmind-master"

    # Cache TTL (seconds)
    CACHE_TTL_ORDER: int = 300  # 5 minutes
    CACHE_TTL_TRADE: int = 600  # 10 minutes
    CACHE_TTL_RISK: int = 1800  # 30 minutes

    # CORS
    CORS_ORIGINS: list = ["*"]

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # JWT (for internal service communication)
    JWT_SECRET: str = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    JWT_ALGORITHM: str = "HS256"

    # Trading Engine
    ENABLE_REAL_TRADING: bool = (
        os.getenv("ENABLE_REAL_TRADING", "false").lower() == "true"
    )
    ORDER_TIMEOUT: int = 30  # seconds
    MAX_ORDER_SIZE: float = 1000000.0  # max order value
    MIN_ORDER_SIZE: float = 100.0  # min order value

    # QMT Broker
    QMT_HOST: str = os.getenv("QMT_HOST", "127.0.0.1")
    QMT_PORT: int = int(os.getenv("QMT_PORT", "18080"))
    REAL_BROKER_TYPE: str = os.getenv("REAL_BROKER_TYPE", "bridge")
    # HMAC 指令签名密钥：用于对发布到 Redis 的下单指令进行签名，
    # 本地 Agent 侧通过相同密钥验证，防止伪造指令。
    # 生成方式：python -c "import secrets; print(secrets.token_hex(32))"
    QMT_CMD_HMAC_SECRET: str = os.getenv("QMT_CMD_HMAC_SECRET", "")

    # Trade Command Stream（指令通道，替换原 Pub/Sub）
    # key 格式：{TRADE_CMD_STREAM_PREFIX}:{platform_user_id}
    TRADE_CMD_STREAM_PREFIX: str = os.getenv(
        "TRADE_CMD_STREAM_PREFIX", "quantmind:trade:cmds"
    )
    TRADE_CMD_STREAM_MAXLEN: int = int(os.getenv("TRADE_CMD_STREAM_MAXLEN", "10000"))

    # Risk Control
    MAX_DAILY_TRADES: int = 100
    MAX_POSITION_SIZE: float = 0.3  # 30% of portfolio
    MAX_LEVERAGE: float = 3.0
    STOP_LOSS_PERCENTAGE: float = 0.05  # 5%
    MIN_LOT_MAIN_BOARD: int = int(os.getenv("MIN_LOT_MAIN_BOARD", "100"))
    MIN_LOT_GEM_BOARD: int = int(os.getenv("MIN_LOT_GEM_BOARD", "100"))
    MIN_LOT_STAR_BOARD: int = int(os.getenv("MIN_LOT_STAR_BOARD", "200"))
    MIN_LOT_BJ_BOARD: int = int(os.getenv("MIN_LOT_BJ_BOARD", "100"))
    ENABLE_MARGIN_TRADING: bool = (
        os.getenv("ENABLE_MARGIN_TRADING", "true").lower() == "true"
    )
    ENABLE_SHORT_SELLING_REAL: bool = (
        os.getenv("ENABLE_SHORT_SELLING_REAL", "false").lower() == "true"
    )
    ENABLE_LONG_SHORT_REAL: bool = (
        os.getenv("ENABLE_LONG_SHORT_REAL", "false").lower() == "true"
    )
    LONG_SHORT_WHITELIST_USERS: str = os.getenv("LONG_SHORT_WHITELIST_USERS", "")
    SHORT_ADMISSION_STRICT: bool = (
        os.getenv("SHORT_ADMISSION_STRICT", "true").lower() == "true"
    )
    MARGIN_STOCK_POOL_PATH: str = os.getenv(
        "MARGIN_STOCK_POOL_PATH",
        os.path.join(os.getenv("STORAGE_ROOT", "data"), "融资融券.json"),
    )
    MARGIN_SHORT_MARGIN_RATE: float = float(
        os.getenv("MARGIN_SHORT_MARGIN_RATE", "0.5")
    )
    MARGIN_WARNING_RATIO: float = float(os.getenv("MARGIN_WARNING_RATIO", "1.3"))
    MARGIN_CLOSEOUT_RATIO: float = float(os.getenv("MARGIN_CLOSEOUT_RATIO", "1.1"))
    DEFAULT_FINANCING_RATE: float = float(os.getenv("DEFAULT_FINANCING_RATE", "0.08"))
    DEFAULT_BORROW_RATE: float = float(os.getenv("DEFAULT_BORROW_RATE", "0.08"))

    # Service URLs (for inter-service communication)
    USER_SERVICE_URL: str = os.getenv("USER_SERVICE_URL", "http://localhost:8002")
    PORTFOLIO_SERVICE_URL: str = os.getenv(
        "PORTFOLIO_SERVICE_URL", "http://localhost:8002"
    )
    MARKET_DATA_SERVICE_URL: str = os.getenv(
        "MARKET_DATA_SERVICE_URL", "http://quantmind-stream:8003"
    )

    # Execution Stream Consumer
    ENABLE_EXEC_STREAM_CONSUMER: bool = (
        os.getenv("ENABLE_EXEC_STREAM_CONSUMER", "false").lower() == "true"
    )
    EXEC_STREAM_PREFIX: str = os.getenv("EXEC_STREAM_PREFIX", "qm:exec:stream")
    EXEC_STREAM_GROUP: str = os.getenv("EXEC_STREAM_GROUP", "exec-trade")
    EXEC_STREAM_CONSUMER_NAME: str = os.getenv(
        "EXEC_STREAM_CONSUMER_NAME", "trade-consumer-1"
    )
    EXEC_STREAM_BATCH_SIZE: int = int(os.getenv("EXEC_STREAM_BATCH_SIZE", "100"))
    EXEC_STREAM_BLOCK_MS: int = int(os.getenv("EXEC_STREAM_BLOCK_MS", "3000"))
    EXEC_STREAM_TENANTS: str = os.getenv("EXEC_STREAM_TENANTS", "default")
    EXEC_STREAM_MAX_RETRY: int = int(os.getenv("EXEC_STREAM_MAX_RETRY", "3"))
    EXEC_STREAM_DLQ_PREFIX: str = os.getenv("EXEC_STREAM_DLQ_PREFIX", "qm:exec:dlq")

    # Simulation
    SIMULATION_SLIPPAGE_BPS: float = float(os.getenv("SIMULATION_SLIPPAGE_BPS", "5"))
    SIMULATION_COMMISSION_RATE: float = float(
        os.getenv("SIMULATION_COMMISSION_RATE", "0.0003")
    )

    # Commission rates for risk purchasing-power check
    # A 股买入佣金约 0.03%（券商最低 5 元）；卖出另含印花税 0.1%+过户费 0.001%
    COMMISSION_RATE_BUY: float = float(os.getenv("COMMISSION_RATE_BUY", "0.0003"))
    COMMISSION_RATE_SELL: float = float(
        os.getenv("COMMISSION_RATE_SELL", "0.0013")
    )  # 0.03%+0.1%+0.001%


settings = Settings()
