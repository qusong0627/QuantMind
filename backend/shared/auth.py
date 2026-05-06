"""统一认证中间件"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from config.settings import settings

from .logging_config import get_logger

logger = get_logger(__name__)
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
DEFAULT_INTERNAL_CALL_SECRET = "dev-internal-call-secret"


def get_internal_call_secret() -> str:
    """Shared internal secret for service-to-service calls."""
    import os

    return str(os.getenv("INTERNAL_CALL_SECRET", DEFAULT_INTERNAL_CALL_SECRET)).strip()


class AuthManager:
    """认证管理器"""

    def __init__(self):
        self.secret_key = settings.security.secret_key
        self.algorithm = settings.security.jwt_algorithm
        self.expire_minutes = settings.security.jwt_expire_minutes

    def create_access_token(self, data: dict[str, Any]) -> str:
        """创建访问令牌

        Args:
            data: 要编码的数据

        Returns:
            JWT令牌字符串
        """
        to_encode = data.copy()
        expire = datetime.utcnow() + timedelta(minutes=self.expire_minutes)
        to_encode.update({"exp": expire})

        encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)

        logger.info(f"Access token created for user: {data.get('sub', 'unknown')}")
        return encoded_jwt

    def verify_token(self, token: str) -> dict[str, Any]:
        """验证令牌

        Args:
            token: JWT令牌

        Returns:
            解码后的数据

        Raises:
            HTTPException: 令牌无效时抛出
        """
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("Token has expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.JWTError as e:
            logger.warning(f"Invalid token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def hash_password(self, password: str) -> str:
        """哈希密码

        Args:
            password: 明文密码

        Returns:
            哈希后的密码
        """
        return pwd_context.hash(password)

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """验证密码

        Args:
            plain_password: 明文密码
            hashed_password: 哈希密码

        Returns:
            密码是否匹配
        """
        return pwd_context.verify(plain_password, hashed_password)


# 全局认证管理器实例
auth_manager = AuthManager()


from fastapi import Request
from fastapi.security import HTTPBearer


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
) -> dict[str, Any]:
    """获取当前用户信息的依赖注入函数（支持内部 Secret 和 JWT）"""
    # 1. 内部调用校验
    internal_secret = request.headers.get("X-Internal-Call")
    if internal_secret:
        expected = get_internal_call_secret()
        if internal_secret == expected:
            user_id = request.headers.get("X-User-Id", "0")
            return {
                "sub": user_id,
                "user_id": user_id,
                "username": "internal",
                "roles": ["admin"],
            }

    # 2. 常规 JWT 校验
    token = None
    if credentials:
        token = credentials.credentials
    else:
        # 兼容 SSE (EventSource) 不支持 Header 的情况，尝试从 Query 参数获取
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = auth_manager.verify_token(token)

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


def require_roles(*required_roles: str):
    """角色权限装饰器

    Args:
        required_roles: 需要的角色列表

    Returns:
        权限检查函数
    """

    def role_checker(current_user: dict[str, Any] = Depends(get_current_user)):
        user_roles = current_user.get("roles", [])

        if not any(role in user_roles for role in required_roles):
            logger.warning(
                f"User {current_user.get('sub')} attempted to access resource requiring roles {required_roles}, "
                f"but only has roles {user_roles}"
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

        return current_user

    return role_checker


def optional_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any] | None:
    """可选认证依赖注入函数

    Args:
        credentials: HTTP认证凭据（可选）

    Returns:
        用户信息字典或None
    """
    if credentials is None:
        return None

    try:
        token = credentials.credentials
        payload = auth_manager.verify_token(token)
        return payload
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# 微服务通用 JWT 解码工具（从环境变量读取密钥，供各服务统一使用）
# ---------------------------------------------------------------------------


def decode_jwt_token(token: str) -> dict:
    """解码并验证 JWT Token，返回 payload 字典。

    从环境变量动态读取密钥（支持统一配置注入）：
    - SECRET_KEY / JWT_SECRET_KEY / JWT_SECRET
    - ALGORITHM / JWT_ALGORITHM

    Raises:
        HTTPException 401: token 无效或已过期。
    """
    import os

    try:
        from jose import JWTError
        from jose import jwt as jose_jwt
    except ImportError:  # pragma: no cover
        raise RuntimeError("python-jose 未安装，请执行: pip install python-jose[cryptography]")

    secret_key = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET_KEY") or os.getenv("JWT_SECRET") or "dev-secret"
    algorithm = os.getenv("ALGORITHM") or os.getenv("JWT_ALGORITHM") or "HS256"

    try:
        return jose_jwt.decode(token, secret_key, algorithms=[algorithm])
    except JWTError as exc:
        logger.warning(f"JWT decode failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
