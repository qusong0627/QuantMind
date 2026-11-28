from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.shared.mcp_tool_catalog import ALL_API_PERMISSIONS

# ============ Request Models ============


def _validate_permissions(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    unknown = sorted(set(value) - ALL_API_PERMISSIONS)
    if unknown:
        raise ValueError(f"未知权限码：{', '.join(unknown)}")
    return list(dict.fromkeys(value))


class ApiKeyCreate(BaseModel):
    """Create API Key Request"""

    name: str = Field(..., min_length=1, max_length=100, description="Key Name/Label")
    permissions: list[str] = Field(default=[], description="List of permissions (scopes)")
    expires_at: datetime | None = Field(None, description="Expiration time")

    @field_validator("permissions")
    @classmethod
    def _permissions(cls, value: list[str]) -> list[str]:
        return _validate_permissions(value) or []


class ApiKeyUpdate(BaseModel):
    """Update API Key Request"""

    name: str | None = Field(None, min_length=1, max_length=100)
    permissions: list[str] | None = Field(None)
    is_active: bool | None = Field(None)

    @field_validator("permissions")
    @classmethod
    def _permissions(cls, value: list[str] | None) -> list[str] | None:
        return _validate_permissions(value)


# ============ Response Models ============


class ApiKeyResponse(BaseModel):
    """API Key Response (showing secret only once)"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    access_key: str
    secret_key: str = Field(..., description="Secret Key (only returned on creation)")
    name: str
    permissions: list[str]
    is_active: bool
    created_at: datetime
    expires_at: datetime | None


class ApiKeyInfo(BaseModel):
    """API Key Info (no secret)"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    access_key: str
    name: str
    permissions: list[str]
    is_active: bool
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class ApiKeyListResponse(BaseModel):
    """List of API Keys"""

    items: list[ApiKeyInfo]
    count: int


class ApiKeyBootstrapResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    access_key: str
    name: str
    permissions: list[str]
    is_active: bool
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    secret_key: str | None = None
    just_created: bool = False


class ApiKeyRotateSecretResponse(BaseModel):
    access_key: str
    secret_key: str
