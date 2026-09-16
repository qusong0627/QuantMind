"""UTC 瞬时时间的唯一口径，避免 timestamptz / naive 来回改把成交写炸。

约定（sim_trades.executed_at 及同类瞬时列）：
- 库列：TIMESTAMPTZ
- Python：必须是 timezone-aware UTC
- JSON：ISO-8601 且以 Z 结尾
- 无时区输入一律视为 UTC，禁止当成 Asia/Shanghai 再减 8 小时
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.types import DateTime, TypeDecorator

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime:
    """把任意 datetime 收成 aware UTC。None 或非法值回落到 utc_now。"""
    if value is None:
        return utc_now()
    try:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    except Exception:
        return utc_now()


def to_utc_iso(value: datetime | None) -> str | None:
    """JSON 序列化：始终 UTC，后缀 Z。naive 按 UTC 解释。None 保持 None。"""
    if value is None:
        return None
    return as_utc(value).isoformat().replace("+00:00", "Z")


class UtcDateTime(TypeDecorator):
    """SQLAlchemy 列类型：绑定/读取都是 aware UTC，对应 PG timestamptz。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return as_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return as_utc(value)
