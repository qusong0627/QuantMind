"""统一认证中间件"""

import secrets as _secrets
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

# 已知公开默认值（compose/代码兜底常量）——**一律视为未配置**（C1 安全加固，2026-09-17）。
# 背景：compose 的 `${INTERNAL_CALL_SECRET:-changeme-internal-secret}` 回退值令匿名者仅凭
# `X-Internal-Call: changeme-internal-secret` + `X-User-Id` 两枚请求头即可冒充任意用户
# （含 admin）并经内部网关直下真单（trading_mode 缺省 REAL）。轮换后应同步清理 .env 与
# compose 的默认回退值。
_PUBLIC_INTERNAL_DEFAULTS = frozenset(
    {"changeme-internal-secret", "dev-internal-call-secret", "quantmind-internal-secret"}
)

#: 用户 JWT 签名密钥的已知公开默认值——**一律视为未配置**。
#:
#: 与 `_PUBLIC_INTERNAL_DEFAULTS` 同一类问题（2026-09-23 复核）。此前该键没有任何过滤：
#: `config/settings.py` 兜底 `"dev-secret-key"`，compose 回退
#: `"changeme-generate-a-random-secret"`，`.env.example` 印 `CHANGE_ME_...`。
#: 三条都公开，而这是**签用户 JWT 的密钥**——拿仓库里读到的字面量自签一枚
#: `{"sub":"1","roles":["admin"]}` 即可通过标准鉴权路径冒充管理员（已实测复现，
#: 比 C1 更直接：C1 需要走内部请求头通道，这条走的是普通登录口）。
_PUBLIC_JWT_DEFAULTS = frozenset(
    {
        "dev-secret-key",  # config/settings.py 的 os.getenv 兜底
        "changeme-generate-a-random-secret",  # docker-compose.yml 的 :- 回退
        "CHANGE_ME_GENERATE_YOUR_OWN_SECRET_KEY",  # .env.example
        "CHANGE_ME_GENERATE_YOUR_OWN_JWT_SECRET",  # .env.example
        "dev-secret",  # decode_jwt_token 的历史兜底
    }
)

#: JWT 密钥候选键，按优先级。MRO 与历史行为一致（SECRET_KEY 优先）。
_JWT_SECRET_ENV_KEYS = ("SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET")


def get_internal_call_secret() -> str:
    """内部调用密钥（服务间信任链唯一读取点）。

    权威优先级（C1 加固后）：**runtime.env（管理台/运维可热换）** > 环境变量（非公开默认）
    > ""（空 = 无有效密钥，一切内部校验必须失败 = fail-closed）。

    与 `runtime_secrets.get_secret` 的不同：本键**文件优先**——compose 会给本键注入
    （可能过期的）环境变量，若按"env 优先"会把轮换后的新密钥遮蔽掉（2026-09-17 实测事故）。
    """
    import os

    try:
        from .runtime_secrets import _parse, runtime_env_path

        v = str(_parse(runtime_env_path()).get("INTERNAL_CALL_SECRET", "")).strip()
        if v and v not in _PUBLIC_INTERNAL_DEFAULTS:
            return v
    except Exception:  # noqa: BLE001 - 读取失败回落环境变量
        pass
    env_val = str(os.getenv("INTERNAL_CALL_SECRET", "")).strip()
    if env_val and env_val not in _PUBLIC_INTERNAL_DEFAULTS:
        return env_val
    return ""


def get_jwt_secret() -> str:
    """用户 JWT 签名密钥（**唯一读取点**）。

    权威优先级与 `get_internal_call_secret` 一致：**runtime.env（运维/管理台可热换）**
    > 环境变量（非公开默认）> ""（空 = 无有效密钥，签/验一律拒绝 = fail-closed）。

    同样**文件优先**：compose 会给 `SECRET_KEY`/`JWT_SECRET_KEY` 注入（可能过期的）
    环境变量，若按「env 优先」会把启动期自动生成并落盘的新密钥遮蔽掉，
    表现为**每次重启都轮换一次签名密钥**（同 2026-09-17 内部密钥的实测事故）。

    返回 "" 是「部署未配置」而非「密钥为空串」——调用方必须**拒绝服务**，
    绝不能把空串喂给 jwt.decode（那等于承认「用空密钥签的令牌」）。
    """
    import os

    env_file: dict[str, str] = {}
    try:
        from .runtime_secrets import _parse, runtime_env_path

        env_file = _parse(runtime_env_path())
    except Exception:  # noqa: BLE001 - 读取失败回落环境变量
        pass

    for source in (env_file, os.environ):
        for key in _JWT_SECRET_ENV_KEYS:
            v = str(source.get(key, "") or "").strip()
            if v and v not in _PUBLIC_JWT_DEFAULTS:
                return v
    return ""


class AuthManager:
    """认证管理器"""

    def __init__(self):
        self.algorithm = settings.security.jwt_algorithm
        self.expire_minutes = settings.security.jwt_expire_minutes

    @property
    def secret_key(self) -> str:
        """**每次实时解析**，不是启动期快照。

        启动期自动生成的新密钥要立刻生效（`main_oss.py` 生成时本对象已存在），
        运维在 runtime.env 里换密钥也不该要求重启。见 `get_jwt_secret`。
        """
        return get_jwt_secret()

    def _require_secret(self) -> str:
        """取签名密钥；未配置则拒绝服务。

        这里**不能**退回 `settings.security.secret_key`——那正是公开字面量
        `dev-secret-key` 的来源。未配置就是不可用（fail-closed）。
        """
        key = self.secret_key
        if not key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="jwt_secret_not_configured",
            )
        return key

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

        encoded_jwt = jwt.encode(to_encode, self._require_secret(), algorithm=self.algorithm)

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
        secret = self._require_secret()  # 未配置 → 503，绝不拿空串/公开字面量验签
        try:
            payload = jwt.decode(token, secret, algorithms=[self.algorithm])
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("Token has expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.PyJWTError as e:
            # 注意：这里不能写 `jwt.JWTError`。现装 PyJWT(2.13) 没有这个名字，
            # 只有 `PyJWTError`/`InvalidTokenError`。写过的话，解释器在求值
            # except 子句时自己抛 AttributeError，于是**任何格式非法的令牌
            # 都变成 500 而不是 401**（匿名可触发，还会刷 traceback 日志）。
            # 只有「已过期」那一支因为在前一个 except 里而幸免。
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
    # 1. 内部调用校验（C1 加固：空密钥必败；compare_digest 防时序侧信道）
    internal_secret = request.headers.get("X-Internal-Call")
    if internal_secret:
        expected = get_internal_call_secret()
        if expected and _secrets.compare_digest(str(internal_secret), expected):
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

    # 此前这里是自带的 `or "dev-secret"` 兜底——与 AuthManager 独立开来，
    # 修一处漏一处。统一走 get_jwt_secret()：它就是 JWT 密钥的唯一读取点。
    secret_key = get_jwt_secret()
    if not secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="jwt_secret_not_configured",
        )
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
