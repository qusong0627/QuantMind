"""Payment transaction model for subscription payments."""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.services.api.models.base import Base


class PaymentTransaction(Base):
    """Payment transaction record for subscription purchases."""

    __tablename__ = "payment_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending, succeeded, failed, refunded
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="alipay"
    )
    transaction_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_info: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
