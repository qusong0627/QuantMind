"""
SQLAlchemy base model for simulation.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    """时区BUG修复：DateTime(timezone=True) 列必须配 aware UTC 默认值。

    此前用 naive datetime.utcnow，经 Asia/Shanghai 会话存成 -8h 的错误
    instant（如 14:50 的成交存成 06:50），交易记录时间少 8 小时。
    """
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
