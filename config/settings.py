"""统一配置管理模块"""

import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json

from config.api_config import get_api_key


class DatabaseSettings(BaseSettings):
    """数据库配置"""

    model_config = SettingsConfigDict(extra="allow")

    # PostgreSQL配置
    postgres_host: str = os.getenv("DB_MASTER_HOST", "192.168.1.88")
    postgres_port: int = int(os.getenv("DB_MASTER_PORT", "6789"))
    postgres_user: str = os.getenv("DB_USER", "quantmind")
    postgres_password: str = os.getenv("DB_PASSWORD", "admin123")
    postgres_database: str = os.getenv("DB_NAME", "quantmind")

    # Redis配置
    redis_host: str = os.getenv("REDIS_HOST", "192.168.1.88")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_user: str | None = os.getenv("REDIS_USER")
    redis_password: str | None = os.getenv("REDIS_PASSWORD", "admin123")
    redis_db: int = int(os.getenv("REDIS_DB", "0"))
    redis_use_sentinel: bool = os.getenv("REDIS_USE_SENTINEL", "false").lower() == "true"
    redis_sentinels: str = os.getenv("REDIS_SENTINELS", "")
    redis_master_name: str = os.getenv("REDIS_MASTER_NAME", "quantmind-master")

    @property
    def redis_url(self) -> str:
        credentials = ""
        if self.redis_user:
            credentials = self.redis_user
            if self.redis_password:
                credentials += f":{self.redis_password}"
            credentials += "@"
        elif self.redis_password:
            credentials = f":{self.redis_password}@"
        return (
            f"redis://{credentials}{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )


class APISettings(BaseSettings):
    """API配置"""

    model_config = SettingsConfigDict(extra="allow")

    # 服务端口配置
    gateway_port: int = 8000
    user_service_port: int = 8002
    backtest_service_port: int = 8003
    data_service_port: int = 8012

    # API限制配置
    rate_limit_per_minute: int = 100
    request_timeout: int = 30

    # 外部API配置 - 通过API配置管理器获取
    @property
    def tsanghi_api_key(self) -> str | None:
        return get_api_key("tsanghi")

    @property
    def qwen_api_key(self) -> str | None:
        return get_api_key("qwen")

    @property
    def gemini_api_key(self) -> str | None:
        return get_api_key("gemini")

    @property
    def openai_api_key(self) -> str | None:
        return get_api_key("openai")

    @property
    def alpha_vantage_api_key(self) -> str | None:
        return get_api_key("alpha_vantage")

    @property
    def juhe_api_key(self) -> str | None:
        return get_api_key("juhe")

    @property
    def canghai_api_key(self) -> str | None:
        return get_api_key("canghai_ai")


class LoggingSettings(BaseSettings):
    """日志配置"""

    model_config = SettingsConfigDict(extra="allow")

    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_file: str | None = None
    max_log_size: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5


class SecuritySettings(BaseSettings):
    """安全配置"""

    model_config = SettingsConfigDict(extra="allow")

    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3001"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            if v.startswith("[") and v.endswith("]"):
                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    # 将 secret_key 改为标准字段，优先从环境变量 SECRET_KEY 读取
    secret_key: str = os.getenv("SECRET_KEY", "")

    def model_post_init(self, __context) -> None:
        """在初始化后校验并填充默认值"""
        if not self.secret_key:
            # 尝试从 API 配置管理器获取
            key = get_api_key("jwt")
            self.secret_key = key if key else "your-secret-key-here"


class Settings(BaseSettings):
    """主配置类"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="allow",
    )

    environment: str = os.getenv("APP_ENV", "production")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    edition: str = os.getenv("APP_EDITION", "oss").lower()

    @property
    def capabilities(self) -> dict:
        """根据版本返回功能开关"""
        is_oss = self.edition == "oss"
        is_enterprise = self.edition == "enterprise"

        return {
            "edition": self.edition,
            "features": {
                "community": not is_oss,
                "sms": not is_oss,
                "cos": not is_oss,
                "multi_strategy": is_enterprise,
                "advanced_factors": is_enterprise,
                "rbac_enhanced": is_enterprise,
                "audit_logs": is_enterprise or not is_oss,
                "local_storage": is_oss,
                "k8s_deployment": is_enterprise
            }
        }

    database: DatabaseSettings = DatabaseSettings()
    api: APISettings = APISettings()
    logging: LoggingSettings = LoggingSettings()
    security: SecuritySettings = SecuritySettings()

# 全局配置实例
settings = Settings()
