"""MCP 服务运行配置（全部来自环境变量，无配置文件）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    api_base: str = _env("QM_API_BASE", "http://quantmind:8000").rstrip("/")
    host: str = _env("MCP_HOST", "0.0.0.0")
    port: int = int(_env("MCP_PORT", "8765"))
    service_name: str = _env("MCP_SERVICE_NAME", "quantmind-mcp")
    contract_version: str = "1.0"
    request_timeout: float = float(_env("MCP_REQUEST_TIMEOUT", "15"))
    config_ttl_seconds: float = float(_env("MCP_CONFIG_TTL", "30"))
    redis_host: str = _env("REDIS_HOST", "redis")
    redis_port: int = int(_env("REDIS_PORT", "6379"))
    redis_password: str = os.getenv("REDIS_PASSWORD", "").strip()
    redis_db: int = int(_env("REDIS_MCP_DB", "0"))
    stdio_access_key: str = os.getenv("QM_ACCESS_KEY", "").strip()
    stdio_secret_key: str = os.getenv("QM_SECRET_KEY", "").strip()


settings = Settings()
