"""
User Data Models
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from backend.services.api.models.base import Base

# Base is imported above


class User(Base):
    """用户基础信息表"""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "phone_number", name="uq_users_tenant_phone"),
    )

    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    user_id = Column(
        String(64), unique=True, nullable=False, index=True, comment="用户唯一标识"
    )
    tenant_id = Column(String(64), nullable=False, index=True, comment="租户ID")

    # 认证信息
    username = Column(String(128), nullable=False, index=True, comment="用户名")
    # Allow nullable if phone registration is supported
    email = Column(String(255), nullable=True, index=True, comment="邮箱")
    # 手机号按租户唯一（由 uq_users_tenant_phone 约束保证），不要使用全局 unique。
    phone_number = Column(String(32), nullable=True, index=True, comment="手机号")
    password_hash = Column(String(255), nullable=False, comment="密码哈希")

    # 状态
    is_active = Column(Boolean, default=True, comment="是否激活")
    is_verified = Column(Boolean, default=False, comment="是否验证邮箱")
    is_admin = Column(Boolean, default=False, comment="是否为管理员")
    is_locked = Column(Boolean, default=False, comment="是否锁定")

    # 登录信息
    last_login_at = Column(DateTime(timezone=True), comment="最后登录时间")
    last_login_ip = Column(String(64), comment="最后登录IP")
    login_count = Column(Integer, default=0, comment="登录次数")

    # 审计字段
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    updated_at = Column(
        DateTime(timezone=True), onupdate=func.now(), comment="更新时间"
    )

    # 软删除
    is_deleted = Column(Boolean, default=False, index=True, comment="是否删除")
    deleted_at = Column(DateTime(timezone=True), comment="删除时间")

    def __repr__(self):
        return f"<User(user_id={self.user_id}, username={self.username})>"


class UserProfile(Base):
    """用户档案表"""

    __tablename__ = "user_profiles"

    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    user_id = Column(
        String(64), unique=True, nullable=False, index=True, comment="用户ID"
    )
    tenant_id = Column(String(64), nullable=False, index=True, comment="租户ID")

    # 个人信息
    display_name = Column(String(128), comment="显示名称")
    avatar_url = Column(String(512), comment="头像URL")
    bio = Column(Text, comment="个人简介")
    location = Column(String(128), comment="地理位置")
    website = Column(String(255), comment="个人网站")
    phone = Column(String(32), comment="手机号")

    # 交易偏好
    trading_experience = Column(
        String(32),
        default="intermediate",
        comment="交易经验: beginner/intermediate/advanced",
    )
    risk_tolerance = Column(
        String(32), default="medium", comment="风险承受能力: low/medium/high"
    )
    investment_goal = Column(String(128), comment="投资目标")

    # 社交信息
    github_url = Column(String(255), comment="GitHub")
    twitter_handle = Column(String(128), comment="Twitter")
    linkedin_url = Column(String(255), comment="LinkedIn")

    # 配置信息
    preferences = Column(JSON, default={}, comment="用户偏好设置(JSON)")
    notification_settings = Column(JSON, default={}, comment="通知设置(JSON)")
    api_key = Column(Text, comment="API Key（用户级，全系统通用）")

    # 审计字段
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    updated_at = Column(
        DateTime(timezone=True), onupdate=func.now(), comment="更新时间"
    )

    def __repr__(self):
        return (
            f"<UserProfile(user_id={self.user_id}, display_name={self.display_name})>"
        )


class UserSession(Base):
    """用户会话表"""

    __tablename__ = "user_sessions"

    # 主键 (session_id 是实际主键)
    id = Column(String(64), primary_key=True, default=lambda: __import__("uuid").uuid4().hex)
    session_id = Column(
        String(64), unique=True, nullable=False, primary_key=True, comment="会话ID"
    )
    user_id = Column(String(64), nullable=False, index=True, comment="用户ID")
    tenant_id = Column(String(64), nullable=False, index=True, comment="租户ID")

    # 会话信息
    token_jti = Column(String(64), index=True, comment="JWT ID")
    refresh_token = Column(String(1024), comment="刷新Token")
    ip_address = Column(String(64), comment="IP地址")
    user_agent = Column(String(255), comment="User Agent")

    # 时间信息
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="创建时间"
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, comment="过期时间")
    last_active_at = Column(DateTime(timezone=True), comment="最后活跃时间")

    # 状态
    is_active = Column(Boolean, default=True, comment="是否活跃")
    is_revoked = Column(Boolean, default=False, comment="是否撤销")

    def __repr__(self):
        return f"<UserSession(user_id={self.user_id}, is_active={self.is_active})>"
