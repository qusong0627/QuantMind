import logging
from dataclasses import dataclass
from typing import Optional
from collections.abc import Generator

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
from backend.shared.auth import decode_jwt_token

logger = logging.getLogger(__name__)
security = HTTPBearer()


@dataclass
class AuthContext:
    user_id: str
    tenant_id: str
    raw_sub: str
    roles: list[str]
    is_admin: bool = False


async def get_auth_context(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    x_tenant_id: str | None = Header(None),
) -> AuthContext:
    payload = decode_jwt_token(credentials.credentials)

    # 兼容不同服务的字段习惯 (sub vs user_id)
    sub = str(payload.get("sub") or payload.get("user_id") or "").strip()
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing subject (sub)",
        )

    tenant_id = str(payload.get("tenant_id") or x_tenant_id or "default").strip()
    roles = payload.get("roles", ["user"])
    # 与 API 服务 middleware/auth.py 同口径：is_admin 字段优先，其次看 roles
    is_admin = bool(payload.get("is_admin", "admin" in roles))

    return AuthContext(
        user_id=sub,
        tenant_id=tenant_id,
        raw_sub=sub,
        roles=roles,
        is_admin=is_admin,
    )


async def require_admin(
    auth: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    """管理员门（trade 侧控制面端点用）。

    仅依据 JWT 的 ``is_admin`` / ``roles`` 判定（与 API 服务同口径），
    不在 trade 服务里回查 RBAC——那是 api 侧依赖，跨服务引入会拖慢每次请求。
    """
    if not (auth.is_admin or "admin" in (auth.roles or [])):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return auth


def get_redis():
    """获取 Redis 客户端"""
    client = get_trade_redis()
    if getattr(client, "client", None) is None:
        client.connect()
    if getattr(client, "client", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable",
        )
    return client


async def get_db() -> Generator:
    """获取数据库 Session (Async)"""
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as session:
        yield session


async def get_read_db() -> Generator:
    """获取只读数据库 Session"""
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        yield session
