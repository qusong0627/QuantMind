"""Shared identity and session checks for private engine WebSocket routes."""
# OSS port: 与企业版逐字一致。依赖的 decode_jwt_token / verify_access_token /
# ensure_session_live 在 OSS 版 shared 侧同名同参可用。
# 注：qlib_app/api/websocket.py 当前为内联实现，未改接本模块；本文件补齐
# import 面，供后续收敛与沙箱 manifest 对齐。

from __future__ import annotations

import asyncio

from fastapi import WebSocket

from backend.shared.auth import decode_jwt_token
from backend.shared.session_validation import (
    VerifiedIdentity,
    ensure_session_live,
    verify_access_token,
)

SESSION_RECHECK_SECONDS = 30


async def authenticate_websocket(
    websocket: WebSocket,
    *,
    token: str | None,
    user_id: str | None,
    tenant_id: str | None,
    source: str,
) -> VerifiedIdentity:
    """Authenticate a WS token and reject query-string identity overrides."""
    raw_auth_header = websocket.headers.get("authorization", "")
    header_token = (
        raw_auth_header.split(" ", 1)[1].strip()
        if raw_auth_header.lower().startswith("bearer ")
        else ""
    )
    candidate_token = (token or header_token or "").strip()
    if not candidate_token:
        raise ValueError("missing websocket auth token")

    identity = await verify_access_token(
        candidate_token, source=source, decoder=decode_jwt_token
    )
    if user_id and str(user_id).strip() != identity.user_id:
        raise ValueError("websocket identity mismatch")
    if tenant_id and str(tenant_id).strip() != identity.tenant_id:
        raise ValueError("websocket tenant mismatch")
    return identity


async def receive_with_session_recheck(websocket: WebSocket, identity: VerifiedIdentity):
    """Receive one frame, revalidating idle sockets at a bounded interval."""
    try:
        data = await asyncio.wait_for(
            websocket.receive_text(), timeout=SESSION_RECHECK_SECONDS
        )
    except asyncio.TimeoutError:
        data = None
    await ensure_session_live(identity.tenant_id, identity.user_id, identity.jti)
    return data
