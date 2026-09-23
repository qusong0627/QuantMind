"""对外 API（`/api/ext/v1`）的机器身份：访问凭据 → 短期会话令牌。

**为什么不复用用户 JWT，也不复用 `X-Internal-Call`**

* 用户 JWT 的持有者是人，令牌里写死 `sub`；机器令牌要能**按凭据撤销**
  （停机、换机、凭据外泄）。两者的生命周期模型不同。
* `X-Internal-Call` 是**服务间**信任链。外部节点一旦拿到它就能横向冒充任意
  内部服务——那是 C1 事故里被利用的同一类通道，不能再开一个出口给它。

于是：外部节点拿**长期访问凭据**（`api_keys` 表，bcrypt 存哈希）换一枚
**短期的、独立的**会话令牌。

令牌形状
--------
    qmx1.<base64url(payload)>.<base64url(hmac_sha256)>

签名覆盖 ``qmx1.<payload>``（含前缀），所以算法标识本身也被保护。
独立前缀有两个作用：一眼能认出「这是机器令牌」，且用户 JWT 校验器
（`auth_manager.verify_token`）必然拒绝它——两边密钥不同、格式不同，互不认。

失败方向
--------
* **密钥未配置 = 整体不可用**，不回落到任何默认值。C1 事故的成因正是 compose
  里 `${INTERNAL_CALL_SECRET:-changeme-internal-secret}` 这种公开回退值。
* 密钥是**每次调用实时读**的（`runtime_secrets` 语义），轮换后免重启生效。
* `exp` 之外**每个请求都查一次库**（`load_active_key`）——这是刻意用它换掉
  「纯自包含令牌」的部分性能，换取**撤销即时生效**：凭据停用后，已签发的
  未过期令牌立刻失效。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, NamedTuple

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

#: 密钥的配置键。runtime.env 权威（管理台可热换）→ 环境变量 → 未配置即拒。
SECRET_ENV_KEY = "EXTERNAL_API_SECRET"

#: 令牌版本前缀。改令牌格式时必须换它——旧前缀的令牌会立刻全部失效。
TOKEN_PREFIX = "qmx1"

#: 所有认证失败对外**统一**的 detail。过期/签名错/凭据不存在都回这一个：
#: 区分开等于给攻击者一个探测 oracle（能问出「这个 access_key 存在吗」）。
AUTH_FAILED_DETAIL = "external_auth_failed"

#: 会话令牌默认有效期。外部节点在其 2/3 处续期即可（见 `EXPECTED_REFRESH_HINT`）。
DEFAULT_TTL_SECONDS = 3600

#: 公开默认值 —— 一律视为未配置（C1 加固同款防线）。
#: 前两条是 C1 事故里被利用的字面量；其余是「开发者随手写」的高频词。
_PUBLIC_DEFAULTS = frozenset(
    {
        "changeme",
        "changeme-internal-secret",
        "changeme-external-secret",
        "dev-external-secret",
        "secret",
        "test",
    }
)


class ExternalAuthError(Exception):
    """认证失败。**对外只表现为统一 401**，内部细节只进日志。"""


class AuthBackendUnavailable(Exception):
    """凭据库查不动（连接失败/超时）。

    与 `ExternalAuthError` 分开是**刻意**的：前者是调用方的问题（401），
    这个是**我们的**问题（503）。混成同一个会把运维引向错误方向——
    去查客户端凭据，而真正坏的是数据库。
    """


class ExternalPrincipal(NamedTuple):
    """已认证的外部调用方。`access_key` 用于审计归因到具体凭据。"""

    access_key: str
    user_id: str
    tenant_id: str
    permissions: tuple[str, ...]
    session_expires_at: int


# ---------------------------------------------------------------------------
# 密钥读取
# ---------------------------------------------------------------------------


def _read_secret_raw() -> str:
    """读原始值（未做公开默认值过滤）。**测试的接缝**——不要绕过它直接读 env。"""
    from backend.shared.runtime_secrets import get_secret

    return (get_secret(SECRET_ENV_KEY, "") or os.getenv(SECRET_ENV_KEY, "")).strip()


def get_external_api_secret() -> str:
    """对外 API 签名密钥。**未配置返回空串**（不是默认值）。

    每次调用实时读——轮换后免重启生效。
    """
    raw = (_read_secret_raw() or "").strip()
    if raw in _PUBLIC_DEFAULTS:
        return ""
    return raw


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def access_key_fp(access_key: str) -> str:
    """凭据指纹——**日志与 Redis 键名一律用它，不用明文**。

    `access_key` 是匿名请求体里的自由字符串（可含换行 / ANSI 转义）：进日志
    等于给攻击者一个伪造日志行、污染终端回显的通道；进 Redis 键名则会出现在
    MONITOR / 慢日志 / 备份里。日志汇聚与工单附件比键名更常见，泄漏面更大。
    """
    return hashlib.sha256((access_key or "").encode("utf-8")).hexdigest()[:16]


def _sign(secret: str, signing_input: str) -> str:
    # 显式声明并守住 ASCII 前提：改 utf-8 会静默改变所有已签发令牌的签名。
    # 越界输入转成 ExternalAuthError，不让 UnicodeEncodeError 逃成 500。
    try:
        raw = signing_input.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ExternalAuthError("待签内容含非 ASCII 字节") from exc
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256)
    return _b64e(mac.digest())


def mint_external_token(
    access_key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> tuple[str, int]:
    """用一枚已核验的访问凭据铸造会话令牌。

    返回 ``(token, expires_at_unix)``。调用方**必须**先校验 secret_key
    （bcrypt 比对）——本函数只负责签，不负责证明调用者有权拿到令牌。

    密钥未配置时抛 `ExternalAuthError`：宁可整体不可用，也不用空密钥去签。
    """
    secret = get_external_api_secret()
    if not secret:
        raise ExternalAuthError(f"{SECRET_ENV_KEY} 未配置，对外 API 不可用")

    issued_at = int(time.time())
    expires_at = issued_at + int(ttl_seconds)
    payload = {
        "ak": access_key,
        "iat": issued_at,
        "exp": expires_at,
        # jti 让同一凭据签发的多枚令牌互不相同：审计能定位到「哪一枚」被用，
        # 将来要做单令牌吊销时也不必改格式。
        "jti": secrets.token_hex(8),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{TOKEN_PREFIX}.{body}"
    return f"{signing_input}.{_sign(secret, signing_input)}", expires_at


def parse_external_token(token: str, *, now: int | None = None) -> dict[str, Any]:
    """校验并解出令牌载荷。任何异常一律抛 `ExternalAuthError`（原因只进日志）。

    先验签再读 `exp`：未签名的字段一个都不许被信任。
    """
    secret = get_external_api_secret()
    if not secret:
        raise ExternalAuthError(f"{SECRET_ENV_KEY} 未配置，对外 API 不可用")

    parts = (token or "").split(".")
    if len(parts) != 3:
        raise ExternalAuthError("令牌格式非法")
    prefix, body, signature = parts

    # ASCII 白名单（边界校验）。令牌本就只由 base64url + `.` 组成，非 ASCII 一定是伪造。
    # 必须在验签**之前**挡：`_sign` 里是 `encode("ascii")`，`hmac.compare_digest` 也
    # 拒绝非 ASCII 字符串——两者都会抛 UnicodeEncodeError / TypeError 穿出去变成 500，
    # 破坏「过期/签名错/畸形一律同一个 401」这条反枚举不变式，还给匿名者一个
    # 可无限刷 traceback 的通道。HTTP 头按 latin-1 解码，`0xE9` 是合法字节，
    # curl / 裸 socket 发得出来（httpx 客户端发不出不代表真实客户端发不出）。
    if not token.isascii():
        raise ExternalAuthError("令牌含非 ASCII 字节")
    if prefix != TOKEN_PREFIX:
        raise ExternalAuthError("令牌前缀不匹配（不是对外 API 令牌）")

    expected = _sign(secret, f"{prefix}.{body}")
    if not hmac.compare_digest(signature, expected):
        raise ExternalAuthError("令牌签名不匹配")

    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ExternalAuthError("令牌载荷无法解析") from exc
    if not isinstance(payload, dict) or not payload.get("ak"):
        raise ExternalAuthError("令牌载荷缺字段")

    current = int(time.time()) if now is None else int(now)
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise ExternalAuthError("令牌缺 exp")
    if current >= exp:
        raise ExternalAuthError("令牌已过期")
    return payload


# ---------------------------------------------------------------------------
# 凭据核验（每请求一次只读查询）
# ---------------------------------------------------------------------------


async def load_active_key(access_key: str, *, on_backend_error: str = "none"):
    """按 access_key 取有效凭据；无效（不存在/停用/过期）返回 None。

    **只读、不提交**：这里在每个请求的路径上。平台原有的
    `ApiKeyService.validate_key` 每次命中都写 `last_used_at` 并 commit，
    高频外部调用下会把这个表变成写热点；`last_used_at` 改在**铸造令牌**时更新
    （每次会话一次，足够了）。

    `on_backend_error`：查库失败时怎么办——这是**调用方**的事，不是判定的事，
    所以做成参数而不是在这里替它决定：

    * ``"none"``（默认，供每请求的鉴权依赖用）：fail-closed 返回 None → 401。
      数据库抖一下不该让请求带着未经核验的身份通过。
    * ``"raise"``（供**握手**用）：抛 `AuthBackendUnavailable` → 503。
      握手失败时把「我们的库挂了」报成「你的凭据不对」会把运维引向客户端，
      方向反了——这与密钥未配置时给 503 而不是 401 是同一条理由。

    这是可替换的接缝：单测 monkeypatch 它即可覆盖全部失败分支，不必连库。
    """
    try:
        from sqlalchemy import select

        from backend.services.api.user_app.models.api_key import ApiKey
        from backend.shared.api_key_checks import api_key_rejection_reason
        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.access_key == access_key)
            )
            key = result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("对外凭据查询失败 ak_fp=%s: %s", access_key_fp(access_key), exc)
        if on_backend_error == "raise":
            raise AuthBackendUnavailable(str(exc)) from exc
        return None

    # 存在/启用/未过期的判定走 shared 的唯一实现（见该模块 docstring：
    # 这份判定曾在仓库里被手写四遍，其中一份把 aware 与 naive 比出了 500）。
    if api_key_rejection_reason(key) is not None:
        return None
    return key


async def touch_last_used(access_key: str) -> None:
    """更新 `last_used_at`。**只在铸造令牌时调用**（每次会话一次）。

    失败不抛：这是审计副产品，不该阻断握手。
    """
    from sqlalchemy import update

    from backend.services.api.user_app.models.api_key import ApiKey
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.utc_datetime import utc_now

    try:
        async with get_session() as session:
            await session.execute(
                update(ApiKey)
                .where(ApiKey.access_key == access_key)
                .values(last_used_at=utc_now())
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新 last_used_at 失败 ak_fp=%s: %s", access_key_fp(access_key), exc)


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------


def _reject(reason: str, access_key: str | None = None) -> HTTPException:
    """统一 401。真实原因只进日志，不回给调用方。"""
    logger.warning("[ExtAuth] 拒绝：%s（ak_fp=%s）", reason, access_key_fp(access_key) if access_key else "-")
    return HTTPException(
        status_code=401,
        detail=AUTH_FAILED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


#: `auto_error=False`：缺头时返回 None 而不是让 FastAPI 抛它自己的 403/401——
#: 失败形状要由 `_reject` 统一（同一个 detail），不能一半是框架的一半是我们的。
#:
#: ⚠️ 必须用 `HTTPBearer` 而不是 `Header()`。用 `Header()` 时 OpenAPI 里该端点的
#: `security` 是 null、Authorization 只表现为一个**可选普通请求头**——自动生成
#: 客户端的外部系统会**默认不带鉴权**。这是面向机器的接口，schema 就是它的说明书。
_bearer_scheme = HTTPBearer(auto_error=False)


async def require_external_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> ExternalPrincipal:
    """对外端点的鉴权依赖：`Depends(require_external_principal)`。

    三步都失败即 401，且对外**同一个 detail**：
    1. 令牌验签 + 未过期；
    2. 凭据仍存在且启用（每请求查库 → 撤销即时生效）；
    3. 凭据未过期。
    """
    if credentials is None:
        raise _reject("缺 Authorization 头")
    if credentials.scheme.lower() != "bearer":
        raise _reject("Authorization 不是 Bearer 形状")
    token = (credentials.credentials or "").strip()
    if not token:
        raise _reject("Authorization 不是 Bearer 形状")

    try:
        payload = parse_external_token(token)
    except ExternalAuthError as exc:
        raise _reject(str(exc)) from exc

    access_key = str(payload["ak"])
    key = await load_active_key(access_key)
    if key is None:
        raise _reject("凭据不存在/已停用/已过期", access_key)
    # 二次校验：`load_active_key` 的契约是「无效即返回 None」，这里**不盲信它**。
    # 认证路径上「已停用的凭据仍然可用」是最坏的失败模式，而重复判一次的成本
    # 是一次属性读取——将来谁把上游那个过滤条件重构掉了，这一层还兜得住。
    if not getattr(key, "is_active", False):
        raise _reject("凭据已停用（上游未过滤）", access_key)

    return ExternalPrincipal(
        access_key=access_key,
        user_id=str(key.user_id),
        tenant_id=str(key.tenant_id),
        permissions=tuple(key.permissions or ()),
        session_expires_at=int(payload["exp"]),
    )
