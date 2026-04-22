"""
Notification Models
通知模型
"""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from backend.services.api.models.base import Base


class Notification(Base):
    """用户通知表"""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="ID")
    user_id = Column(
        String(64),
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
        comment="用户ID",
    )
    tenant_id = Column(String(64), nullable=False, index=True, comment="租户ID")

    # 内容
    title = Column(String(128), nullable=False, comment="标题")
    content = Column(Text, nullable=False, comment="内容")
    type = Column(
        "notification_type",  # 映射到数据库中的 notification_type 列
        String(32),
        default="system",
        index=True,
        comment="类型: system/trading/market/strategy",
    )
    level = Column(
        String(16), default="info", comment="级别: info/warning/error/success"
    )

    # 链接
    action_url = Column(String(512), comment="跳转链接")

    # 状态
    is_read = Column(Boolean, default=False, index=True, comment="是否已读")
    read_at = Column(DateTime(timezone=True), comment="读取时间")

    # 时间
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
        comment="创建时间",
    )
    expires_at = Column(DateTime(timezone=True), comment="过期时间")

    def __repr__(self):
        return (
            f"<Notification(id={self.id}, title={self.title}, is_read={self.is_read})>"
        )
