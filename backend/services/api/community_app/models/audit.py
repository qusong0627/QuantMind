"""Audit Log Model."""
from sqlalchemy import JSON, BigInteger, Column, DateTime, String
from sqlalchemy.sql import func

from backend.services.api.models.base import Base


class AuditLogRecord(Base):
    __tablename__ = "community_audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(64))
    ip = Column(String(64))
    user_agent = Column(String(256))
    meta = Column(JSON)
    created_at = Column(DateTime, default=func.now())
