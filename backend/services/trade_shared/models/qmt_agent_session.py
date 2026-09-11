"""
QMT Agent Session Model
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, String

from .base import Base


def _utcnow() -> datetime:
    # timestamptz 列必须用 aware UTC；naive utcnow 会被会话时区重解释（-8h BUG）。
    return datetime.now(timezone.utc)


class QMTAgentSession(Base):
    """QMT Agent Bridge Session 表：每次 POST /bridge/session 生成一个有时效的 token"""

    __tablename__ = "qmt_agent_sessions"

    # 线上历史表为 varchar 主键（UUID 字符串）
    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 关联绑定记录
    binding_id = Column(String(64), nullable=False, index=True)

    # 多租户隔离（冗余，避免多表 JOIN）
    tenant_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)

    # Token：存储 SHA256 哈希，不存明文
    token_hash = Column(String(64), nullable=False, unique=True, index=True)

    # 有效期与撤销
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    # 时间戳
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        Index("idx_qmt_session_binding", "binding_id"),
        Index("idx_qmt_session_tenant_user", "tenant_id", "user_id"),
    )

    def __repr__(self):
        return (
            f"<QMTAgentSession(id={self.id}, binding_id={self.binding_id}, "
            f"expires_at={self.expires_at}, revoked={self.revoked_at is not None})>"
        )
