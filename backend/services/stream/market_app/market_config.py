import json
import logging
import os
from pathlib import Path
from typing import List

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

ROOT_ENV = Path(__file__).resolve().parents[4] / ".env"

# ============================================================
# 行情 Redis 配置 (quantmind-redis 访客只读用户)
#
# 口令**不再内置**（2026-09-23 移除）：原先把 Fernet 密钥与密文一起写在本文件里，
# 而本仓是公开仓——密钥写进公开仓就不叫密钥，谁 clone 谁就能解出那个口令。
# 现在按下面的优先级取，取不到就空口令 + 告警（与当前 docker-compose 一致：
# 它给 stream 传的就是空的 `REDIS_PASSWORD=`，该实例按免密访问）。
# ============================================================
_ENV_MARKET_REDIS_PASSWORD = "MARKET_REDIS_PASSWORD"
_ENV_FERNET_KEY = "QM_MARKET_REDIS_FERNET_KEY"
_ENV_FERNET_CIPHERTEXT = "QM_MARKET_REDIS_CT"


def _resolve_market_redis_password() -> str:
    """行情 Redis 口令：明文环境变量优先，其次「Fernet 密钥 + 密文」两个环境变量。

    容错放在这里而不是让调用方去猜：任一来源缺失/解密失败都只降级为空口令并告警，
    不抛异常——stream 服务起不来比口令少一层鉴权严重得多（且 Redis 只在容器网内可达）。
    """
    plain = os.getenv(_ENV_MARKET_REDIS_PASSWORD, "").strip()
    if plain:
        return plain

    key = os.getenv(_ENV_FERNET_KEY, "").strip()
    ciphertext = os.getenv(_ENV_FERNET_CIPHERTEXT, "").strip()
    if key and ciphertext:
        try:
            return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "行情 Redis 口令解密失败（%s/%s 不匹配）：%s；按空口令继续",
                _ENV_FERNET_KEY, _ENV_FERNET_CIPHERTEXT, exc,
            )
            return ""

    logger.warning(
        "未配置行情 Redis 口令（%s 或 %s+%s 皆缺）——按空口令访问；"
        "该实例若开了 requirepass 会连接失败，请在 .env 里补上",
        _ENV_MARKET_REDIS_PASSWORD, _ENV_FERNET_KEY, _ENV_FERNET_CIPHERTEXT,
    )
    return ""


MARKET_REDIS_HOST = "quantmind-redis"
MARKET_REDIS_PORT = 6379
MARKET_REDIS_PASSWORD = _resolve_market_redis_password()
MARKET_REDIS_DB = 3


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(
        env_file=(str(ROOT_ENV), ".env"),
        case_sensitive=True,
        extra="ignore",
    )

    # Service
    SERVICE_NAME: str = "market-data-service"
    MARKET_DATA_HOST: str = "0.0.0.0"
    MARKET_DATA_PORT: int = 8003
    DEBUG: bool = False

    # 兼容旧代码引用的 HOST/PORT
    @property
    def HOST(self) -> str:
        return self.MARKET_DATA_HOST

    @property
    def PORT(self) -> int:
        return self.MARKET_DATA_PORT

    # Database (Unified Configuration)
    MARKET_DATA_DB_URL: str = Field(
        default="postgresql+psycopg2://postgres:@localhost:5432/quantmind",
        validation_alias=AliasChoices("MARKET_DATA_DB_URL", "DATABASE_URL"),
    )
    DB_DRIVER: str = "asyncpg"
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "quantmind"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    @property
    def DATABASE_URL(self) -> str:
        return self.MARKET_DATA_DB_URL

    @model_validator(mode="after")
    def _ensure_database_url(self):
        # 若 DATABASE_URL 未显式配置，使用 DB_* 拼接，确保与根 .env 一致。
        current = (self.MARKET_DATA_DB_URL or "").strip()
        if current and "localhost:5432/quantmind" not in current:
            return self

        driver = (self.DB_DRIVER or "asyncpg").strip()
        user = (self.DB_USER or "postgres").strip()
        password = self.DB_PASSWORD or ""
        host = (self.DB_HOST or "localhost").strip()
        port = int(self.DB_PORT or 5432)
        db = (self.DB_NAME or "quantmind").strip()
        self.MARKET_DATA_DB_URL = (
            f"postgresql+{driver}://{user}:{password}@{host}:{port}/{db}"
        )
        return self

    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30
    DB_ECHO: bool = False

    # Redis (Unified - OSS Edition)
    # 行情 Redis 已硬编码为 quantmind-redis 访客只读用户
    REDIS_HOST: str = Field(default=MARKET_REDIS_HOST)
    REDIS_PORT: int = Field(default=MARKET_REDIS_PORT)
    REDIS_USE_SENTINEL: bool = Field(default=False)
    REDIS_SENTINELS_RAW: str = Field(default="localhost:26379")
    REDIS_MASTER_NAME: str = "quantmind-master"
    REDIS_PASSWORD: str = Field(default=MARKET_REDIS_PASSWORD)
    REDIS_DB: int = Field(
        default=MARKET_REDIS_DB, validation_alias=AliasChoices("REDIS_DB", "REDIS_DB_MARKET")
    )

    # 远程行情快照 Redis (OSS: 使用统一 Redis)
    REMOTE_QUOTE_REDIS_HOST: str = Field(default=MARKET_REDIS_HOST)
    REMOTE_QUOTE_REDIS_PORT: int = Field(default=MARKET_REDIS_PORT)
    REMOTE_QUOTE_REDIS_PASSWORD: str = Field(default=MARKET_REDIS_PASSWORD)

    # 无订阅时的保活拉取标的，确保时序/落库链路持续有数据
    STREAM_WARMUP_SYMBOLS: str = "SZ000001,SH600000"

    # Cache TTL (seconds)
    CACHE_TTL_QUOTE: int = 1
    CACHE_TTL_KLINE: int = 60
    CACHE_TTL_SNAPSHOT: int = 5

    # WebSocket
    WS_HEARTBEAT_INTERVAL: int = 30
    WS_MAX_CONNECTIONS: int = 1000

    # Data Sources
    # A 股行情统一走 QuantDB 本地 parquet；remote_redis 保留给后续实时快照接入。
    DATA_SOURCES: list[str] = ["quantdb", "remote_redis"]
    DEFAULT_SOURCE: str = "quantdb"

    @field_validator("DEFAULT_SOURCE")
    @classmethod
    def _validate_default_source(cls, v: str, info):  # type: ignore[override]
        data_sources = (
            (info.data.get("DATA_SOURCES") or []) if hasattr(info, "data") else []
        )
        if v and data_sources and v not in data_sources:
            raise ValueError(f"DEFAULT_SOURCE={v} 不在 DATA_SOURCES={data_sources} 中")
        return v

    @field_validator("REMOTE_QUOTE_REDIS_PORT", mode="before")
    @classmethod
    def _empty_remote_quote_port_to_default(cls, v):
        # compose 远端行情块以「留空 = 用共享配置默认值」注入该变量（空串），
        # 空串不能进 int 解析——回落到本字段声明的默认端口（否则 stream 启动即崩）。
        if v is None or (isinstance(v, str) and not v.strip()):
            return MARKET_REDIS_PORT
        return v

    @property
    def REDIS_SENTINELS(self) -> list[tuple]:
        text = (self.REDIS_SENTINELS_RAW or "").strip()
        if not text:
            return [("localhost", 26379)]
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        out.append((str(item[0]), int(item[1])))
                if out:
                    return out
        except Exception:
            pass
        pairs = []
        for item in text.split(","):
            seg = item.strip()
            if not seg:
                continue
            if ":" in seg:
                host, port = seg.split(":", 1)
                pairs.append((host.strip(), int(port.strip())))
            else:
                pairs.append((seg, 26379))
        return pairs or [("localhost", 26379)]

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 100

    # Monitoring
    METRICS_ENABLED: bool = True


settings = Settings()
