"""
User Pydantic Schemas
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ============ 请求模型 ============


class UserRegister(BaseModel):
    """用户注册"""

    tenant_id: str = Field(..., min_length=1, max_length=64, description="租户ID")
    username: str = Field(..., min_length=3, max_length=128, description="用户名")
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(..., min_length=8, max_length=128, description="密码")
    full_name: str | None = Field(None, description="全名（可选）")

    @field_validator("username")
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        if not v.isalnum():
            raise ValueError("用户名只能包含字母和数字")
        return v


class UserLogin(BaseModel):
    """用户登录"""

    tenant_id: str = Field(..., min_length=1, max_length=64, description="租户ID")
    username: str = Field(..., description="用户名或邮箱")
    password: str = Field(..., description="密码")


class AdminLogin(BaseModel):
    """管理员专用登录"""

    username: str = Field(..., description="管理员用户名")
    password: str = Field(..., description="密码")
    admin_key: str = Field(..., description="管理员安全密钥")
    tenant_id: str = Field("default", description="租户ID")


class VerificationRequest(BaseModel):
    """验证码/回执请求"""

    tenant_id: str = Field(..., min_length=1, max_length=64, description="租户ID")
    identifier: str = Field(..., description="目标标识（邮箱/手机号）")
    type: Literal["email", "sms"] = Field("email", description="验证码类型")
    purpose: Literal["register", "reset_password"] = Field(
        "register", description="用途"
    )
    user_id: str | None = Field(None, description="相关用户ID（可选）")


class ForgotPasswordRequest(BaseModel):
    """忘记密码请求"""

    tenant_id: str = Field(..., min_length=1, max_length=64, description="租户ID")
    email: EmailStr = Field(..., description="注册邮箱")


class PasswordResetRequest(BaseModel):
    """密码重置请求"""

    token: str = Field(..., description="密码重置令牌")
    tenant_id: str = Field(..., min_length=1, max_length=64, description="租户ID")
    new_password: str = Field(..., min_length=8, description="新密码")


class PasswordChangeRequest(BaseModel):
    """修改密码请求"""

    old_password: str = Field(..., description="旧密码")
    new_password: str = Field(..., min_length=8, description="新密码")


class UserProfileUpdate(BaseModel):
    """更新用户档案"""

    display_name: str | None = Field(None, max_length=128)
    avatar_url: str | None = Field(None, max_length=512, description="头像URL")
    bio: str | None = Field(None, max_length=500)
    location: str | None = Field(None, max_length=128)
    website: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=32)
    trading_experience: str | None = Field(
        None, pattern="^(beginner|intermediate|advanced)$"
    )
    risk_tolerance: str | None = Field(None, pattern="^(low|medium|high)$")
    investment_goal: str | None = Field(None, max_length=128)
    ai_ide_api_key: str | None = Field(
        None, max_length=2048, description="AI-IDE API Key"
    )


# ============ 响应模型 ============


class UserResponse(BaseModel):
    """用户信息响应"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    tenant_id: str
    username: str
    email: str | None
    is_active: bool
    is_verified: bool
    is_admin: bool = False
    created_at: datetime
    last_login_at: datetime | None


class UserProfileResponse(BaseModel):
    """用户档案响应"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    tenant_id: str
    display_name: str | None
    username: str | None = Field(
        None, alias="username_at_runtime", description="用户名（来自User表）"
    )
    avatar_url: str | None = None
    bio: str | None
    location: str | None
    website: str | None
    phone: str | None = None
    trading_experience: str | None = None
    risk_tolerance: str | None = None
    investment_goal: str | None
    ai_ide_api_key: str | None = Field(None, description="AI-IDE API Key")
    created_at: datetime
    updated_at: datetime | None

    @field_validator("avatar_url", mode="before")
    @classmethod
    def default_avatar(cls, v: str | None) -> str:
        """如果没有头像，返回默认头像"""
        if v is None or v == "":
            return "/uploads/default_avatar.png"
        return v


class UserDetailResponse(BaseModel):
    """用户详细信息（包含档案）"""

    user: UserResponse
    profile: UserProfileResponse


class TokenResponse(BaseModel):
    """Token响应"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse | None = None


# ============ 统一响应格式 ============


class ResponseModel(BaseModel):
    """统一响应格式"""

    code: int = 200
    message: str = "success"
    data: dict | None = None
    meta: dict | None = None


class PaginatedResponse(BaseModel):
    """分页响应"""

    code: int = 200
    message: str = "success"
    data: dict = Field(..., description="包含items和pagination")
    meta: dict | None = None


class SmsSendRequest(BaseModel):
    phone: str
    tenant_id: str
    type: Literal["register", "login", "reset_password"]


class PhoneSendCodeRequest(BaseModel):
    """
    个人中心手机号绑定/换绑专用：必须已登录，由后端控制可发送的目的与频率。
    """

    purpose: Literal["bind_phone", "change_phone_old", "change_phone_new"] = Field(
        ..., description="发送验证码用途"
    )
    phone: str | None = Field(
        None,
        description="目标手机号（bind/change_new 必填；change_old 可不传，默认使用当前绑定手机号）",
    )


class PhoneBindRequest(BaseModel):
    phone: str
    code: str


class PhoneChangeRequest(BaseModel):
    old_code: str
    new_phone: str
    new_code: str


class PhoneRegisterRequest(BaseModel):
    phone: str
    code: str
    password: str
    tenant_id: str
    username: str | None = None


class PhoneLoginRequest(BaseModel):
    phone: str
    code: str
    tenant_id: str


class PhoneResetPasswordRequest(BaseModel):
    phone: str
    code: str
    new_password: str
    tenant_id: str


class CheckAvailabilityRequest(BaseModel):
    """检查用户名/手机号是否可用"""

    type: Literal["username", "phone", "email"]
    value: str
    tenant_id: str = "default"
