from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, JSON, String, UniqueConstraint

from backend.services.trade.models.base import Base


class PreflightSnapshot(Base):
    __tablename__ = "real_trading_preflight_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "trading_mode",
            "snapshot_date",
            name="uq_preflight_snapshot_daily",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    tenant_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    trading_mode = Column(String(16), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)

    ready = Column(Boolean, nullable=False, default=False)
    total_checks = Column(Integer, nullable=False, default=0)
    passed_checks = Column(Integer, nullable=False, default=0)
    required_failed_count = Column(Integer, nullable=False, default=0)
    run_count = Column(Integer, nullable=False, default=0)

    failed_required_keys = Column(JSON, nullable=False, default=list)
    checks = Column(JSON, nullable=False, default=list)
    source = Column(String(32), nullable=False, default="preflight_api")
    last_checked_at = Column(DateTime, nullable=False, default=datetime.now)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
