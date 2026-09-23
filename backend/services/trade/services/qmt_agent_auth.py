"""
QMT Agent auth helpers.

- API Key / Binding management stays in trade service.
- Bridge session token lifecycle is provided by backend.shared.qmt_bridge_auth.
"""

from __future__ import annotations

from typing import Optional

from passlib.context import CryptContext
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.api.user_app.models.api_key import ApiKey
from backend.services.trade_shared.models.qmt_agent_binding import QMTAgentBinding
from backend.shared.api_key_checks import api_key_rejection_reason
from backend.shared.qmt_bridge_auth import (
    BridgeSessionContext,
    SESSION_REFRESH_THRESHOLD_SECONDS,
    SESSION_TTL_SECONDS,
    create_bridge_session,
    generate_bridge_token,
    hash_bridge_token,
    refresh_bridge_session,
    revoke_session,
    utcnow,
    verify_bridge_session_token,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALLOWED_AGENT_TYPE = "qmt"


async def resolve_api_key(session: AsyncSession, access_key: str) -> ApiKey | None:
    result = await session.execute(select(ApiKey).where(ApiKey.access_key == access_key))
    return result.scalar_one_or_none()


def validate_api_key_secret(key: ApiKey | None, secret_key: str) -> str | None:
    # 可用性判定（存在/启用/未过期）走 shared 的唯一实现，不再就地手写。
    #
    # 2026-09-23 修：这里原本自己写了一遍到期判断，用的是
    # `key.expires_at < datetime.now()` —— 左边是 TIMESTAMPTZ（asyncpg 回
    # **aware**），右边是 **naive** 本地墙钟，比较直接抛
    # `TypeError: can't compare offset-naive and offset-aware datetimes`，
    # 整条 QMT agent 鉴权 500（不是「偏 8 小时」这种温和走样）。
    # 之所以一直没暴露：现网 api_keys 的 expires_at 全是 NULL（复核时实测 0/2），
    # 短路让这行**从未被执行过**——直到有人签发一枚带有效期的凭据。
    # 判定逻辑收进 `shared/api_key_checks` 之后，四份拷贝不会再各自漂移。
    reason = api_key_rejection_reason(key)
    if reason is not None:
        return reason
    if not secret_key or not pwd_context.verify(secret_key, key.secret_hash):
        return "secret_key_invalid"
    return None


async def get_active_binding(
    session: AsyncSession,
    tenant_id: str,
    account_id: str,
) -> QMTAgentBinding | None:
    result = await session.execute(
        select(QMTAgentBinding)
        .where(
            and_(
                QMTAgentBinding.tenant_id == tenant_id,
                QMTAgentBinding.account_id == account_id,
                QMTAgentBinding.status == "active",
            )
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_or_create_binding(
    session: AsyncSession,
    key: ApiKey,
    account_id: str,
    client_fingerprint: str,
    hostname: str | None,
    client_version: str | None,
    last_ip: str | None,
    force_rebind: bool = False,
) -> tuple[QMTAgentBinding, bool]:
    result = await session.execute(
        select(QMTAgentBinding)
        .where(QMTAgentBinding.api_key_id == key.id)
        .order_by(QMTAgentBinding.created_at.desc())
        .limit(1)
    )
    binding = result.scalar_one_or_none()

    # 规范化 user_id 为 8 位补零格式
    normalized_user_id = str(key.user_id).zfill(8)

    if binding is None:
        binding = QMTAgentBinding(
            tenant_id=key.tenant_id,
            user_id=normalized_user_id,
            api_key_id=key.id,
            agent_type=ALLOWED_AGENT_TYPE,
            account_id=account_id,
            client_fingerprint=client_fingerprint,
            hostname=hostname,
            client_version=client_version,
            status="active",
            last_ip=last_ip,
            last_seen_at=utcnow(),
            bound_at=utcnow(),
        )
        session.add(binding)
        await session.flush()
        return binding, True

    if binding.account_id != account_id or binding.client_fingerprint != client_fingerprint:
        if not force_rebind:
            raise ValueError("binding_conflict")
        # force_rebind=True：覆盖旧设备绑定信息
        binding.account_id = account_id
        binding.client_fingerprint = client_fingerprint

    binding.user_id = normalized_user_id  # 确保更新
    binding.hostname = hostname or binding.hostname
    binding.client_version = client_version or binding.client_version
    binding.last_ip = last_ip or binding.last_ip
    binding.status = "active"
    binding.last_seen_at = utcnow()
    await session.flush()
    return binding, False


async def reset_binding(
    session: AsyncSession,
    key: ApiKey,
) -> bool:
    """将该 API Key 的最新绑定设为 inactive，下次连接将创建新绑定。"""
    result = await session.execute(
        select(QMTAgentBinding)
        .where(QMTAgentBinding.api_key_id == key.id)
        .order_by(QMTAgentBinding.created_at.desc())
        .limit(1)
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        return False
    binding.status = "inactive"
    await session.flush()
    return True


__all__ = [
    "ALLOWED_AGENT_TYPE",
    "BridgeSessionContext",
    "SESSION_REFRESH_THRESHOLD_SECONDS",
    "SESSION_TTL_SECONDS",
    "create_bridge_session",
    "generate_bridge_token",
    "get_active_binding",
    "get_or_create_binding",
    "hash_bridge_token",
    "refresh_bridge_session",
    "reset_binding",
    "resolve_api_key",
    "revoke_session",
    "utcnow",
    "validate_api_key_secret",
    "verify_bridge_session_token",
]
