"""
Trading Service Configuration
"""

import os
from typing import Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.services.simulation.services.market_rules import CN_RULES
from backend.shared.env_flags import normalize_env_flag

# A 股费率默认值 —— 唯一来源派生（`CN_RULES` 是费用分项唯一实现）。
# 提到模块级常量而非内联在 `os.getenv` 默认参里，是为了让「默认值」可被测试直接断言：
# `settings.*` 会被 env 覆盖（覆盖是有意的），断言它会因部署设了 env 而假红。
CN_COMMISSION_DEFAULT: float = CN_RULES.commission_rate
CN_COMMISSION_MIN_DEFAULT: float = CN_RULES.commission_min
CN_STAMP_DUTY_DEFAULT: float = CN_RULES.stamp_duty_rate
CN_SELL_ALLIN_DEFAULT: float = round(
    CN_RULES.commission_rate + CN_RULES.stamp_duty_rate + CN_RULES.transfer_fee_rate,
    6,
)


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

    # AI / LLM
    DASHSCOPE_API_KEY: str = os.getenv("DASHSCOPE_API_KEY", "")

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
    #
    # 实盘总开关。**与 `shared/live_trading_gate` 共用一份读取实现**（`shared/env_flags`），
    # 见 `test_real_trading_flag_readers_agree`。
    #
    # 光把默认值换成 `env_flag(...)` 是不够的：本类 `env_file=".env"` + pydantic-settings
    # 会**按字段名自己去读同名环境变量**，读到的原始串直接进 pydantic 的 bool 解析器 ——
    # 于是 `" true "` / `"true\r"` / `""` 在此抛 `ValidationError`（模块级
    # `settings = Settings()` 在 import 时就炸，整个 trade 栈起不来），而 `"1"`/`"yes"`/`"on"`
    # 解析成 `True`，与闸门的词表和空白容忍度都不一致。故原始串先经 before 校验器
    # 按**闸门口径**归一。
    #
    # 词表收窄（`1`/`yes`/`on` 由 True 变 False）是有意的：那三个值在闸门侧今天就是关，
    # 端点在中间件已被 403，收窄只是把 settings 拉回同一个答案，不改可达行为。
    ENABLE_REAL_TRADING: bool = False

    @field_validator("ENABLE_REAL_TRADING", mode="before")
    @classmethod
    def _normalize_real_trading_flag(cls, raw: object) -> object:
        """原始串（env / .env 来源）先按闸门口径归一，再交给 pydantic 的 bool 解析。"""
        return normalize_env_flag(raw) if isinstance(raw, str) else raw

    REAL_BROKER_TYPE: str = os.getenv("REAL_BROKER_TYPE", "tdx")
    ORDER_TIMEOUT: int = 30  # seconds
    MAX_ORDER_SIZE: float = 1000000.0  # max order value
    MIN_ORDER_SIZE: float = 100.0  # min order value

    # 通达信 (TDX) 交易桥配置
    TDX_BRIDGE_URL: str = os.getenv("TDX_BRIDGE_URL", "http://192.168.31.31:8550")
    TDX_BRIDGE_TOKEN: str = os.getenv("TDX_BRIDGE_TOKEN", "")
    TDX_ACCOUNT: str = os.getenv("TDX_ACCOUNT", "")
    TDX_ACCOUNT_TYPE: str = os.getenv("TDX_ACCOUNT_TYPE", "stock")

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
    # 以下三项是喂给 `CN_RULES.compute_fee_breakdown` 的**覆盖值**（同为 env 可调），
    # 默认值一律取 `CN_RULES` 自身 —— 与上面同一条单源纪律：抄写同值副本今天不错，
    # 但费率一变就有第二处要改，而这一族的教训正是「不在网里就没人比」。
    SIMULATION_COMMISSION_RATE: float = float(
        os.getenv("SIMULATION_COMMISSION_RATE", str(CN_COMMISSION_DEFAULT))
    )
    # 模拟盘最低佣金（元），对齐真实券商，单笔 < 最低佣金按最低收取
    SIMULATION_COMMISSION_MIN: float = float(
        os.getenv("SIMULATION_COMMISSION_MIN", str(CN_COMMISSION_MIN_DEFAULT))
    )
    # 证券交易印花税（卖出单边费率）。A 股当前 0.05%，仅 CN 卖出收取。
    SIMULATION_STAMP_DUTY_RATE: float = float(
        os.getenv("SIMULATION_STAMP_DUTY_RATE", str(CN_STAMP_DUTY_DEFAULT))
    )

    # Commission rates for risk purchasing-power check
    # 默认值**派生自 `CN_RULES`**（费用分项唯一实现），不在此手写数字：
    # 此处曾写死 0.0003 / 0.0013，后者是「0.03% 佣金 + 0.1% 印花税 + 0.001% 过户费」
    # 的旧合计。印花税 2023-08-28 减半到 0.05% 后这个合计没跟着动，且卖出侧全仓
    # **零消费者**（唯一读它的 risk_service 只取 BUY），两处失真都没症状。
    # 费率变了这里跟着变；`test_trade_config_commission_derives_from_single_source` 钉双向。
    COMMISSION_RATE_BUY: float = float(
        os.getenv("COMMISSION_RATE_BUY", str(CN_COMMISSION_DEFAULT))
    )
    COMMISSION_RATE_SELL: float = float(
        os.getenv("COMMISSION_RATE_SELL", str(CN_SELL_ALLIN_DEFAULT))
    )


settings = Settings()
