"""
SQLAlchemy base model for simulation.
"""

from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.shared.utc_datetime import UtcDateTime, utc_now


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )
