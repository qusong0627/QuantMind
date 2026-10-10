"""WS 握手身份解析契约（2026-10-10 审计 H16 回归）。

旧实现两处叠加成洞：
1. 请求头/查询参数里的 ``x-tenant-id``/``x-user-id`` **优先于** JWT 声明；
2. ``authenticated = bool(user_id and token)``——token 只查非空，伪造/过期 token
   配上自称头照样算「已登录」。
后果：任意客户端可越权订阅他人 ``intel.{tenant}.*`` / ``notification.{uid}``
（authorize_intel_topic 与 notification 门只看 metadata）。

修复后的契约：身份只信校验通过的 JWT 声明；自称值未登录时仅作展示，
authenticated 只在 JWT 有效时为真。本文件用假 websocket 直接测纯函数。
"""

from __future__ import annotations

import pytest

from backend.services.stream.ws_core.server import _extract_ws_auth_metadata


class _FakeWS:
    """只满足 _extract_ws_auth_metadata 需要的两个属性。"""

    def __init__(self, headers: dict | None = None, params: dict | None = None):
        self.headers = headers or {}
        self.query_params = params or {}


def _mint_token(user_id: str = "10000001", tenant_id: str = "default") -> str:
    from backend.shared.auth import auth_manager

    return auth_manager.create_access_token({"sub": user_id, "tenant_id": tenant_id})


@pytest.mark.asyncio
async def test_jwt_claims_win_over_self_claimed_headers():
    """自称头不能颠覆有效 JWT 的身份（跨租户越权订阅的根）。"""
    token = _mint_token("10000001", "default")
    ws = _FakeWS(
        headers={"x-tenant-id": "victim-tenant", "x-user-id": "99999999"},
        params={"token": token},
    )
    meta = await _extract_ws_auth_metadata(ws)
    assert meta["authenticated"] is True
    assert meta["tenant_id"] == "default", "tenant 必须取 JWT 声明而非自称头"
    assert meta["user_id"] == "10000001", "user 必须取 JWT 声明而非自称头"


@pytest.mark.asyncio
async def test_forged_token_with_claimed_identity_is_anonymous():
    """伪造 token + 自称 tenant/user 一律匿名（旧实现此处 authenticated=True）。"""
    ws = _FakeWS(
        headers={"x-tenant-id": "victim-tenant", "x-user-id": "99999999"},
        params={"token": "garbage-not-a-jwt"},
    )
    meta = await _extract_ws_auth_metadata(ws)
    assert meta["authenticated"] is False
    assert meta["user_id"] == "anonymous"
    assert meta["auth_source"] == "anonymous"


@pytest.mark.asyncio
async def test_no_token_is_anonymous_even_with_query_identity():
    """无 token：查询参数自称身份不参与鉴权，tenant 仅作展示兜底。"""
    ws = _FakeWS(params={"tenant_id": "default", "user_id": "10000001"})
    meta = await _extract_ws_auth_metadata(ws)
    assert meta["authenticated"] is False
    assert meta["user_id"] == "anonymous"
    assert meta["tenant_id"] == "default"


@pytest.mark.asyncio
async def test_token_missing_tenant_claim_is_not_authenticated():
    """JWT 缺 tenant 声明 → 不算已登录（intel tenant 段匹配无从谈起）。"""
    from backend.shared.auth import auth_manager

    token = auth_manager.create_access_token({"sub": "10000001"})
    ws = _FakeWS(params={"token": token})
    meta = await _extract_ws_auth_metadata(ws)
    assert meta["authenticated"] is False
    assert meta["user_id"] == "anonymous"


@pytest.mark.asyncio
async def test_bearer_header_token_accepted():
    """Authorization: Bearer 头与 ?token= 等效。"""
    token = _mint_token()
    ws = _FakeWS(headers={"authorization": f"Bearer {token}"})
    meta = await _extract_ws_auth_metadata(ws)
    assert meta["authenticated"] is True
    assert meta["user_id"] == "10000001"


@pytest.mark.asyncio
async def test_private_topics_require_self(monkeypatch):
    """trade.updates.* / strategy.* 仅限本人订阅（H16 同族门的回归）。"""
    from backend.services.stream.ws_core import server as ws_server

    sent: list[dict] = []

    async def _capture(connection_id, payload, use_queue=True):
        sent.append(payload)
        return True

    monkeypatch.setattr(ws_server.manager, "send_message", _capture)
    monkeypatch.setattr(
        ws_server.manager,
        "connection_metadata",
        {
            "cid-1": {
                "authenticated": True,
                "tenant_id": "default",
                "user_id": "10000001",
            }
        },
        raising=False,
    )
    monkeypatch.setattr(
        ws_server.manager, "active_connections", {"cid-1": object()}, raising=False
    )

    # 越权：订阅他人成交回报 / 他人策略 topic
    await ws_server.handle_message(
        "cid-1", {"type": "subscribe", "topic": "trade.updates.99999999"}
    )
    assert sent and sent[-1].get("error_code") == "SUBSCRIPTION_FORBIDDEN"
    sent.clear()
    await ws_server.handle_message(
        "cid-1", {"type": "subscribe", "topic": "strategy.99999999"}
    )
    assert sent and sent[-1].get("error_code") == "SUBSCRIPTION_FORBIDDEN"

    # 本人 topic 放行
    sent.clear()
    await ws_server.handle_message(
        "cid-1", {"type": "subscribe", "topic": "trade.updates.10000001"}
    )
    assert sent and sent[-1].get("type") == "subscribed"
    await ws_server.manager.unsubscribe("cid-1", "trade.updates.10000001")
