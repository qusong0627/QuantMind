"""
QMT Agent Binding Model
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String

from .base import Base


def _utcnow() -> datetime:
    # timestamptz 列必须用 aware UTC；naive utcnow 会被会话时区重解释（-8h BUG）。
    return datetime.now(timezone.utc)


class QMTAgentBinding(Base):
    """QMT Agent 设备绑定表：记录 access_key 与交易账号/设备指纹的绑定关系"""

    __tablename__ = "qmt_agent_bindings"

    # 线上历史表为 varchar 主键（UUID 字符串），与 QMTAgentSession.binding_id 对应
    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 多租户隔离
    tenant_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)

    # 关联 API Key
    api_key_id = Column(Integer, nullable=False, index=True)

    # Agent 类型（固定为 "qmt"）
    agent_type = Column(String(32), nullable=False, default="qmt")

    # 绑定的交易账号与客户端指纹
    account_id = Column(String(64), nullable=False, index=True)
    client_fingerprint = Column(String(255), nullable=False)
    hostname = Column(String(255), nullable=True)
    client_version = Column(String(64), nullable=True)

    # 状态：active / inactive
    status = Column(String(32), nullable=False, default="active", index=True)

    # 网络信息
    last_ip = Column(String(64), nullable=True)

    # 时间戳
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    bound_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        Index(
            "idx_qmt_binding_tenant_account_status", "tenant_id", "account_id", "status"
        ),
        Index("idx_qmt_binding_api_key", "api_key_id"),
    )

    def __repr__(self):
        return (
            f"<QMTAgentBinding(id={self.id}, account_id={self.account_id}, "
            f"status={self.status}, fingerprint={self.client_fingerprint})>"
        )
