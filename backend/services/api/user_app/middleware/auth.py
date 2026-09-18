"""
Authentication Middleware
增强的认证中间件，支持RBAC权限控制
"""

import logging
from typing import List, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.services.api.user_app.config import settings
from backend.services.api.user_app.database import get_db
from backend.services.api.user_app.services.auth_service import AuthService
from backend.services.api.user_app.services.rbac_service import RBACService

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def _get_internal_user_from_headers(request: Request) -> dict | None:
    """
    仅在内部调用 secret 匹配时，信任网关注入的身份头。
    注意：网关中间件会剥离外部传入的这些头，避免客户端伪造。

    密钥校验走 shared.auth 唯一读取点（runtime.env 权威、公开默认值一律视为未配置、
    fail-closed；C1 治理）。此前直接比 settings.INTERNAL_CALL_SECRET——compose 注入的
    公开默认值会遮蔽轮换后的密钥（2026-09-18 实测内部调用 401），且拿公开默认值即可
    通过该校验（隐患）。
    """
    from hmac import compare_digest

    from backend.shared.auth import get_internal_call_secret

    secret = request.headers.get("X-Internal-Call")
    if not secret:
        return None
    expected = get_internal_call_secret()
    if not expected or not compare_digest(str(secret), expected):
        return None

    user_id = request.headers.get("X-User-Id")
    tenant_id = request.headers.get("X-Tenant-Id") or "default"
    if not user_id:
        return None

    return {
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "username": "internal",
        "email": "",
        "jti": None,
    }


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """
    获取当前用户（依赖注入）

    从JWT Token中解析用户信息
    """
    internal_user = _get_internal_user_from_headers(request)
    if internal_user:
        return internal_user

    if not credentials:
        print(f"DEBUG: get_current_user Missing authentication token for {request.url.path}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    auth_service = AuthService()
    payload = await auth_service.verify_token(token)

    if not payload:
        print(f"DEBUG: get_current_user Invalid token for {request.url.path}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的Token或Token已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token缺少租户信息",
            headers={"WWW-Authenticate": "Bearer"},
        )

    roles = payload.get("roles", [])
    is_admin = bool(payload.get("is_admin", "admin" in roles))

    # 存量登录态兼容：user_id 规范化（admin / 00000001 → 10000001）
    # 之后，改名之前签发的 token 其 sub 仍是旧值，直接使用会查不到自己名下的模型/
    # 策略/数据（用户表现：「注册的都没了」）。这里映射到规范 ID，令旧会话平滑过渡。
    from backend.shared.admin_identity import is_admin_user_id, normalize_admin_user_id

    user_id = payload.get("sub")
    if is_admin_user_id(user_id):
        user_id = normalize_admin_user_id(user_id)

    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "username": payload.get("username"),
        "email": payload.get("email"),
        "is_admin": is_admin,
        "roles": roles,
        "jti": payload.get("jti"),
    }


async def get_current_active_user(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    获取当前活跃用户

    验证用户是否被禁用
    """
    # 这里可以添加额外的验证逻辑
    # 例如检查用户是否被禁用、是否需要重新认证等

    return current_user


async def require_admin(current_user: dict = Depends(get_current_user), db=Depends(get_db)) -> dict:
    """
    要求管理员权限
    """
    # 1. 优先检查 Token 中解析出的 is_admin 标识
    if current_user.get("is_admin"):
        return current_user

    # 2. 回退到 RBAC 角色查询
    rbac_service = RBACService(db)
    has_admin = await rbac_service.has_role(current_user["user_id"], "admin")

    if not has_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")

    return current_user


def require_permission(permission_code: str):
    """
    权限检查装饰器

    用法:
    @router.post("/orders")
    @require_permission("order.create")
    async def create_order(...):
        ...
    """

    async def permission_checker(current_user: dict = Depends(get_current_user), db=Depends(get_db)) -> dict:
        rbac_service = RBACService(db)

        # 检查权限
        has_perm = await rbac_service.has_permission(current_user["user_id"], permission_code)

        if not has_perm:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要权限: {permission_code}",
            )

        return current_user

    return permission_checker


def require_any_permission(permission_codes: list[str]):
    """
    要求任意一个权限

    用法:
    @router.get("/data")
    @require_any_permission(["data.read", "data.admin"])
    async def get_data(...):
        ...
    """

    async def permission_checker(current_user: dict = Depends(get_current_user), db=Depends(get_db)) -> dict:
        rbac_service = RBACService(db)

        has_perm = await rbac_service.has_any_permission(current_user["user_id"], permission_codes)

        if not has_perm:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要以下任意权限: {', '.join(permission_codes)}",
            )

        return current_user

    return permission_checker


def require_role(role_code: str):
    """
    角色检查装饰器

    用法:
    @router.get("/admin/users")
    @require_role("admin")
    async def list_all_users(...):
        ...
    """

    async def role_checker(current_user: dict = Depends(get_current_user), db=Depends(get_db)) -> dict:
        rbac_service = RBACService(db)

        has_role = await rbac_service.has_role(current_user["user_id"], role_code)

        if not has_role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"需要角色: {role_code}")

        return current_user

    return role_checker


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
) -> dict | None:
    """
    获取可选的当前用户

    支持从 Authorization Header 或 URL 参数 (access_token) 中获取
    如果没有Token也不报错，返回None
    """
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        token = request.query_params.get("access_token")

    if not token:
        return None

    auth_service = AuthService()
    payload = await auth_service.verify_token(token)

    if not payload:
        return None

    return {
        "user_id": payload.get("sub"),
        "tenant_id": payload.get("tenant_id"),
        "username": payload.get("username"),
        "email": payload.get("email"),
        "jti": payload.get("jti"),
    }


def check_user_permission(user_id: str, resource_user_id: str) -> bool:
    """
    检查用户权限

    用户只能访问自己的资源，除非是管理员
    """
    # 简化版本：用户只能访问自己的资源
    return user_id == resource_user_id


async def verify_user_access(resource_user_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    """
    验证用户访问权限

    确保用户只能访问自己的资源
    """
    if not check_user_permission(current_user["user_id"], resource_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限访问该资源")

    return current_user
