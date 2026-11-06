"""A01：跨服务统一会话身份校验（撤销 / 禁用 / 周期复核）。

背景（2026-10-03 多用户改造接力计划 A01）：

- api 的 ``AuthService.verify_token`` 是**唯一**检查 ``user_sessions`` 的实现，且它的
  Redis 正向缓存 TTL 是令牌全寿命（48h），缓存命中时不看撤销状态；
- engine / trade / stream 只验 JWT 签名与过期，撤销与禁用完全不可见；
- ``users.is_active / is_locked / is_deleted`` 在令牌校验路径上从未被检查过。

本模块把「会话行 + 用户行状态」的判定收敛到一处，供四个服务共用：

1. 正向缓存 ``auth:session_ok:{tenant}:{jti}`` 的 TTL 硬上限为 60 秒
   （``MAX_REVALIDATE_SECONDS``），保证撤销/禁用最迟 60 秒内生效；
2. 撤销走 :func:`invalidate_session` / :func:`invalidate_user_sessions`：删除缓存键并在
   ``auth:invalidate`` 频道广播，事件驱动地立即生效（stream 据此关闭连接）；
3. 依赖不可用时 fail-closed：Redis 不可用回源 PG；PG 也不可用则拒绝。

设计约束：本模块**不做 JWT 解码**。各服务既有的解码口径（PyJWT + settings，或
jose + 环境变量）已经互通且被回归锁住，这里只接受已解码的 payload 做状态判定，
避免引入第二套密钥来源。调用方拿到 payload 后必须调用本模块，否则身份不算已验证。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.shared.logging_config import get_logger
from backend.shared.utc_datetime import utc_now

logger = get_logger(__name__)

# 正向缓存的硬上限：任何实现都不得把「已验证」缓存超过 60 秒，否则撤销/禁用
# 无法在约定窗口内生效（验收 A-01 / A-06）。
MAX_REVALIDATE_SECONDS = 60
DEFAULT_REVALIDATE_SECONDS = 60

INVALIDATION_CHANNEL = "auth:invalidate"

# 每用户 jti 索引的兜底 TTL：撤销「某用户全部会话」时用来精确删除缓存键。
_USER_INDEX_MAX_TTL = 7 * 24 * 3600


class AuthRejected(Exception):
    """身份校验失败（fail-closed）。``reason`` 是稳定机器码，供日志与测试断言。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class VerifiedIdentity:
    """通过会话与用户状态双重校验的身份。"""

    user_id: str
    tenant_id: str
    jti: str
    username: str | None = None
    email: str | None = None
    roles: tuple[str, ...] = ()
    is_admin: bool = False
    source: str = ""

    def as_user_dict(self) -> dict[str, Any]:
        """转成各服务 ``request.state.user`` / 依赖注入沿用的字典口径。"""
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "username": self.username,
            "email": self.email,
            "roles": list(self.roles),
            "is_admin": self.is_admin,
            "jti": self.jti,
        }


# ---------------------------------------------------------------------------
# 键与配置
# ---------------------------------------------------------------------------


def revalidate_seconds() -> int:
    """正向缓存 TTL，取值被硬性限制在 (0, 60]。"""
    raw = os.getenv("AUTH_SESSION_REVALIDATE_SECONDS")
    try:
        value = int(raw) if raw else DEFAULT_REVALIDATE_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_REVALIDATE_SECONDS
    if value <= 0:
        value = DEFAULT_REVALIDATE_SECONDS
    return min(value, MAX_REVALIDATE_SECONDS)


def session_ok_key(tenant_id: str, jti: str) -> str:
    return f"auth:session_ok:{tenant_id}:{jti}"


def user_index_key(tenant_id: str, user_id: str) -> str:
    return f"auth:user_jti:{tenant_id}:{user_id}"


def legacy_session_key(tenant_id: str, jti: str) -> str:
    """旧版 48h 缓存键；保留读写仅为滚动发布期间兼容，不再作为信任依据。"""
    return f"session:{tenant_id}:{jti}"


# ---------------------------------------------------------------------------
# 依赖访问（函数内 import，便于测试 monkeypatch）
# ---------------------------------------------------------------------------


def _redis_or_none():
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        return get_redis_sentinel_client()
    except Exception as exc:  # noqa: BLE001 - 依赖不可用由调用方 fail-closed
        logger.warning("session_validation: redis unavailable: %s", exc)
        return None


def _cache_get(tenant_id: str, jti: str) -> str | None:
    client = _redis_or_none()
    if client is None:
        return None
    try:
        # 认证缓存由撤销流程即时删除，必须读主库，避免从库复制延迟继续接受旧会话。
        cached = client.get(session_ok_key(tenant_id, jti), use_slave=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_validation: cache read failed: %s", exc)
        return None
    if cached is None:
        return None
    if isinstance(cached, bytes):
        return cached.decode("utf-8", errors="replace")
    return str(cached)


def _cache_hit(tenant_id: str, jti: str, user_id: str) -> bool | None:
    """命中返回该会话当前的管理员状态；未命中或不可信返回 None（调用方回源）。

    缓存值形如 ``<user_id>|<is_admin>``。**旧格式（只有 user_id）一律当未命中**：
    滚动发布期间新旧实例共存，旧值不带管理员位，按命中处理就等于把管理员状态
    交给令牌声明（正是 A01 遗留 3 要收掉的口径）。
    """
    cached = _cache_get(tenant_id, jti)
    if cached is None:
        return None
    subject, sep, admin = cached.partition("|")
    if not sep:
        return None
    if subject != user_id:
        raise AuthRejected(
            "session_cache_mismatch", "cached session owner mismatch"
        )
    return admin == "1"


def _cache_set(
    tenant_id: str, jti: str, user_id: str, payload: dict[str, Any], is_admin: bool
) -> None:
    client = _redis_or_none()
    if client is None:
        return
    try:
        client.setex(
            session_ok_key(tenant_id, jti),
            revalidate_seconds(),
            f"{user_id}|{1 if is_admin else 0}".encode(),
        )
        index_key = user_index_key(tenant_id, user_id)
        client.hset(index_key, jti, "1")
        client.expire(index_key, _index_ttl(payload))
    except Exception as exc:  # noqa: BLE001 - 缓存写失败只影响性能，不影响正确性
        logger.warning("session_validation: cache write failed: %s", exc)


def _index_ttl(payload: dict[str, Any]) -> int:
    exp = payload.get("exp")
    try:
        remaining = int(float(exp) - datetime.now(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return _USER_INDEX_MAX_TTL
    if remaining <= 0:
        return _USER_INDEX_MAX_TTL
    return max(revalidate_seconds(), min(remaining, _USER_INDEX_MAX_TTL))


async def _load_session_row(tenant_id: str, jti: str) -> dict[str, Any] | None:
    """读取会话行与用户状态。数据库不可用时抛 dependency_unavailable。

    这里**故意不查 is_admin**：列不存在（schema 漂移、旧库）会让整条 SELECT 变成
    ProgrammingError，被下面统一吞成 dependency_unavailable，等于把所有普通用户一起
    锁在门外。管理员状态由 `_load_is_admin` 单独取，取不到只影响权限、不影响登录。
    """
    from backend.shared import database_manager_v2

    sql = text(
        """
        SELECT s.user_id            AS user_id,
               s.is_active          AS session_active,
               s.is_revoked         AS session_revoked,
               s.expires_at         AS expires_at,
               u.is_active          AS user_active,
               u.is_locked          AS user_locked,
               u.is_deleted         AS user_deleted
        FROM user_sessions s
        LEFT JOIN users u
               ON u.user_id = s.user_id AND u.tenant_id = s.tenant_id
        WHERE s.token_jti = :jti AND s.tenant_id = :tenant_id
        ORDER BY s.created_at DESC NULLS LAST
        LIMIT 1
        """
    )
    try:
        async with database_manager_v2.get_session(read_only=True) as session:
            result = await session.execute(sql, {"jti": jti, "tenant_id": tenant_id})
            row = result.mappings().first()
    except Exception as exc:  # noqa: BLE001
        raise AuthRejected(
            "dependency_unavailable", f"session store unavailable: {type(exc).__name__}"
        ) from exc
    return dict(row) if row is not None else None


async def _load_is_admin(tenant_id: str, user_id: str) -> bool:
    """取当前管理员状态；查不到一律 False（权限判定 fail-closed）。"""
    from backend.shared import database_manager_v2

    sql = text(
        "SELECT is_admin FROM users WHERE user_id = :user_id AND tenant_id = :tenant_id "
        "AND is_deleted = false"
    )
    try:
        async with database_manager_v2.get_session(read_only=True) as session:
            result = await session.execute(sql, {"user_id": user_id, "tenant_id": tenant_id})
            row = result.mappings().first()
    except Exception as exc:  # noqa: BLE001 - 不能因为权限列就拒绝正常登录
        logger.warning("session_validation: 管理员状态读取失败，按非管理员处理: %s", exc)
        return False
    return bool(row["is_admin"]) if row is not None else False


# ---------------------------------------------------------------------------
# 核心校验
# ---------------------------------------------------------------------------


def _identity_from(
    payload: dict[str, Any],
    user_id: str,
    tenant_id: str,
    jti: str,
    source: str,
    is_admin: bool,
) -> VerifiedIdentity:
    roles = payload.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    try:
        role_tuple = tuple(str(role) for role in roles)
    except TypeError:
        role_tuple = ()
    return VerifiedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        jti=jti,
        username=payload.get("username"),
        email=payload.get("email"),
        roles=role_tuple,
        is_admin=bool(is_admin),
        source=source,
    )


def _assert_row_usable(row: dict[str, Any], user_id: str) -> None:
    if str(row.get("user_id") or "") != user_id:
        raise AuthRejected("session_owner_mismatch", "session does not belong to token subject")
    if row.get("session_revoked"):
        raise AuthRejected("session_revoked")
    if not row.get("session_active"):
        raise AuthRejected("session_inactive")
    expires_at = row.get("expires_at")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            # 历史数据可能是 naive，按 UTC 解释，避免比较异常。
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utc_now():
            raise AuthRejected("session_expired")
    if row.get("user_active") is None:
        raise AuthRejected("user_not_found")
    if not row.get("user_active"):
        raise AuthRejected("user_inactive")
    if row.get("user_locked"):
        raise AuthRejected("user_locked")
    if row.get("user_deleted"):
        raise AuthRejected("user_deleted")


async def enforce_session_identity(
    payload: dict[str, Any], *, source: str = ""
) -> VerifiedIdentity:
    """对已解码的 access token payload 做会话/用户状态判定。

    Raises:
        AuthRejected: 任一条件不满足，或依赖不可用且无有效缓存。
    """
    if not isinstance(payload, dict):
        raise AuthRejected("malformed_payload", "token payload is not an object")

    user_id = str(payload.get("sub") or payload.get("user_id") or "").strip()
    tenant_id = str(payload.get("tenant_id") or "").strip()
    jti = str(payload.get("jti") or "").strip()
    token_type = str(payload.get("type") or "access")

    if not user_id:
        raise AuthRejected("missing_subject")
    if not tenant_id:
        raise AuthRejected("missing_tenant")
    if token_type != "access":
        raise AuthRejected("wrong_token_type", f"unexpected token type: {token_type}")
    if not jti:
        # 与 api 现行口径一致：没有 jti 就无法判定撤销状态，一律拒绝。
        raise AuthRejected("missing_jti")

    cached_admin = _cache_hit(tenant_id, jti, user_id)
    if cached_admin is not None:
        return _identity_from(
            payload, user_id, tenant_id, jti, source, is_admin=cached_admin
        )

    row = await _load_session_row(tenant_id, jti)
    if row is None:
        raise AuthRejected("session_not_found")
    _assert_row_usable(row, user_id)
    # 管理员状态以库为准：令牌里的 is_admin 只是签发那一刻的快照，降权之后
    # 旧令牌仍带管理员声明（require_admin 第一眼看的就是它），最长可持续 48 小时。
    is_admin = await _load_is_admin(tenant_id, user_id)
    _cache_set(tenant_id, jti, user_id, payload, is_admin)
    return _identity_from(payload, user_id, tenant_id, jti, source, is_admin=is_admin)


async def ensure_session_live(tenant_id: str, user_id: str, jti: str) -> None:
    """长连接建立后的周期复查入口，判定口径与 ``enforce_session_identity`` 完全一致。

    SSE/WebSocket 在握手成功之后就拿到了身份，撤销与禁用必须还能把流收掉；
    否则一个已撤销的令牌可以让一条流式响应无限跑下去。
    """
    if _cache_hit(tenant_id, jti, user_id) is not None:
        return
    row = await _load_session_row(tenant_id, jti)
    if row is None:
        raise AuthRejected("session_not_found")
    _assert_row_usable(row, user_id)


async def verify_access_token(
    token: str, *, source: str = "", decoder=None
) -> VerifiedIdentity:
    """解码并校验 access token 的便捷入口。

    ``decoder`` 缺省使用共享的 PyJWT 口径（``AuthManager``）。trade 侧沿用 jose
    口径，可传入 ``backend.shared.auth.decode_jwt_token`` 保持既有互通用例。
    """
    from fastapi import HTTPException

    if decoder is None:
        from backend.shared.auth import auth_manager

        def decoder(raw: str) -> dict[str, Any]:  # type: ignore[misc]
            return auth_manager.verify_token(raw)

    try:
        payload = decoder(token)
    except HTTPException as exc:
        raise AuthRejected("invalid_token", str(exc.detail)) from exc
    except AuthRejected:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AuthRejected("invalid_token", type(exc).__name__) from exc
    return await enforce_session_identity(payload, source=source)


# ---------------------------------------------------------------------------
# 事件驱动失效
# ---------------------------------------------------------------------------


def _publish(client, message: dict[str, Any]) -> None:
    try:
        client.publish(INVALIDATION_CHANNEL, json.dumps(message))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_validation: publish failed: %s", exc)


def invalidate_session(tenant_id: str, jti: str, *, reason: str = "revoked") -> None:
    """撤销单个会话：删除正向缓存并广播，立即对所有服务生效。"""
    tenant_id = str(tenant_id or "").strip()
    jti = str(jti or "").strip()
    if not tenant_id or not jti:
        return
    client = _redis_or_none()
    if client is None:
        return
    try:
        client.delete(session_ok_key(tenant_id, jti))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_validation: invalidate delete failed: %s", exc)
    _publish(client, {"tenant_id": tenant_id, "jti": jti, "reason": reason})


def invalidate_user_sessions(tenant_id: str, user_id: str, *, reason: str = "disabled") -> int:
    """撤销某用户全部已知会话，返回删除的缓存键数量。

    索引里只包含「部署后至少校验过一次」的 jti；更早的会话由 60 秒周期复核兜底。
    """
    tenant_id = str(tenant_id or "").strip()
    user_id = str(user_id or "").strip()
    if not tenant_id or not user_id:
        return 0
    client = _redis_or_none()
    if client is None:
        return 0

    index_key = user_index_key(tenant_id, user_id)
    removed = 0
    try:
        mapping = client.hgetall(index_key, use_slave=False) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_validation: user index read failed: %s", exc)
        mapping = {}
    for field in list(mapping):
        jti = field.decode("utf-8", errors="replace") if isinstance(field, bytes) else str(field)
        if not jti:
            continue
        try:
            client.delete(session_ok_key(tenant_id, jti))
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("session_validation: invalidate user key failed: %s", exc)
    try:
        client.delete(index_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_validation: user index delete failed: %s", exc)

    _publish(client, {"tenant_id": tenant_id, "user_id": user_id, "reason": reason})
    logger.info(
        "session_validation: invalidated %s session(s) for user=%s tenant=%s reason=%s",
        removed,
        user_id,
        tenant_id,
        reason,
    )
    return removed
