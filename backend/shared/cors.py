"""Shared CORS helpers for QuantMind services."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

DEFAULT_DEV_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3001",
    "http://localhost:3001",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]


def _normalize(origins: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for origin in origins:
        value = str(origin).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def resolve_cors_origins(
    *,
    logger: logging.Logger | None = None,
    env_var_names: tuple[str, ...] = ("CORS_ALLOWED_ORIGINS", "CORS_ORIGINS"),
    default_dev_origins: list[str] | None = None,
) -> list[str]:
    """Resolve service CORS origins with production-safe defaults.

    Production/staging reject wildcard values and require explicit origins.
    Development/testing fall back to local frontend origins.
    """

    runtime = str(os.getenv("ENVIRONMENT", "development")).strip().lower()
    prod_like = runtime in {"production", "staging"}

    configured_raw = ""
    for name in env_var_names:
        value = str(os.getenv(name, "")).strip()
        if value:
            configured_raw = value
            break

    if configured_raw:
        # Check if it looks like a JSON array
        if configured_raw.startswith("[") and configured_raw.endswith("]"):
            import json

            try:
                origins = _normalize(json.loads(configured_raw))
            except Exception:
                # Fallback to split if JSON parsing fails
                origins = _normalize(configured_raw.split(","))
        else:
            origins = _normalize(configured_raw.split(","))
        if "*" in origins:
            if prod_like:
                if logger:
                    logger.warning(
                        "Wildcard CORS is forbidden in %s; requests from browsers will be denied until explicit origins are configured.",
                        runtime,
                    )
                return []
            # 在开发模式下，如果配置了 *，则返回 ["*"] 允许所有来源（包括 Electron 的 file://）
            return ["*"]
        return origins

    if prod_like:
        if logger:
            logger.warning(
                "No CORS origins configured for %s; browser cross-origin requests will be denied.",
                runtime,
            )
        return []

    return _normalize(default_dev_origins or DEFAULT_DEV_ORIGINS)
