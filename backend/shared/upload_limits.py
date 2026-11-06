"""Configured upload payload ceilings shared by storage and ASGI guards."""

from __future__ import annotations

import os

DEFAULT_AI_STRATEGY_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def ai_strategy_max_upload_bytes() -> int:
    raw = os.getenv(
        "AI_STRATEGY_MAX_UPLOAD_BYTES",
        str(DEFAULT_AI_STRATEGY_MAX_UPLOAD_BYTES),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_AI_STRATEGY_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_AI_STRATEGY_MAX_UPLOAD_BYTES
