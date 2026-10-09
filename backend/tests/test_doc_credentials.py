"""文档解析链凭据解析（doc_credentials / profile_gateway strict）单元测试。

口径唯一且顺序固定：用户 Profile Token > env ``MINERU_API_TOKEN`` > 未配置。
后台轮询用 strict 解析：「网关读不到」必须与「用户没配/清掉了」区分开——
前者重试（引擎重启期网关短暂不可用），后者才是定格失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent import (  # noqa: E402
    doc_credentials as cred_mod,
)
from backend.services.engine.alpha_agent import profile_gateway as pg  # noqa: E402


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    return monkeypatch


def _install_profile(monkeypatch, profile: dict | None, *, calls: list | None = None):
    async def fake_fetch(user_id, tenant_id, *, strict=False):
        if calls is not None:
            calls.append((user_id, tenant_id, strict))
        return profile

    monkeypatch.setattr(cred_mod, "fetch_profile_raw", fake_fetch)


# ── resolve_effective_mineru_token ──────────────────────────────────


@pytest.mark.asyncio
async def test_user_token_wins_over_env(clean_env) -> None:
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_profile(clean_env, {"mineru_api_token": "user-tok"})

    token, src = await cred_mod.resolve_effective_mineru_token("u1", "t1")
    assert (token, src) == ("user-tok", "user")


@pytest.mark.asyncio
async def test_env_fallback_when_user_token_missing(clean_env) -> None:
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_profile(clean_env, {})

    assert await cred_mod.resolve_effective_mineru_token("u1", "t1") == (
        "env-tok",
        "env",
    )


@pytest.mark.asyncio
async def test_none_when_nothing_configured(clean_env) -> None:
    _install_profile(clean_env, {})
    assert await cred_mod.resolve_effective_mineru_token("u1", "t1") == (None, "none")


@pytest.mark.asyncio
async def test_placeholder_user_token_is_not_a_credential(clean_env) -> None:
    """用户把示例文案当 Token 粘进来了：不认，回落 env（否则 503 永远不出现）。"""
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    _install_profile(clean_env, {"mineru_api_token": "your-deepseek-api-key"})

    assert await cred_mod.resolve_effective_mineru_token("u1", "t1") == (
        "env-tok",
        "env",
    )


@pytest.mark.asyncio
async def test_blank_user_token_is_ignored(clean_env) -> None:
    _install_profile(clean_env, {"mineru_api_token": "   "})
    assert await cred_mod.resolve_effective_mineru_token("u1", "t1") == (None, "none")


@pytest.mark.asyncio
async def test_missing_ids_skip_profile_read(clean_env) -> None:
    """没有 user_id/tenant_id（老行/内部调用）：不读 Profile（缺请求头参数，
    再试多少次都一样），直接走 env。"""
    clean_env.setenv("MINERU_API_TOKEN", "env-tok")
    calls: list = []
    _install_profile(clean_env, {"mineru_api_token": "user-tok"}, calls=calls)

    token, src = await cred_mod.resolve_effective_mineru_token(None, None)
    assert (token, src) == ("env-tok", "env")
    assert calls == [], "缺 id 不许打网关"


@pytest.mark.asyncio
async def test_strict_flag_is_forwarded(clean_env) -> None:
    calls: list = []
    _install_profile(clean_env, {}, calls=calls)

    await cred_mod.resolve_effective_mineru_token("u1", "t1", strict=True)
    assert calls == [("u1", "t1", True)]


@pytest.mark.asyncio
async def test_strict_gateway_error_propagates(clean_env) -> None:
    """strict 下「读不到」不许降级成「没配」——异常必须上抛给轮询层重试。"""

    async def boom(user_id, tenant_id, *, strict=False):
        raise pg.ProfileGatewayError("gateway down")

    clean_env.setattr(cred_mod, "fetch_profile_raw", boom)

    with pytest.raises(pg.ProfileGatewayError):
        await cred_mod.resolve_effective_mineru_token("u1", "t1", strict=True)


# ── profile_gateway.fetch_profile_raw 的 strict 语义 ────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _install_fake_httpx(monkeypatch, *, get):
    def _respond(spec):
        if isinstance(spec, BaseException):
            raise spec
        status, payload = spec
        return _FakeResponse(status, payload)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, **kwargs):
            return _respond(get)

    monkeypatch.setattr(pg.httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_fetch_profile_raw_strict_returns_none_on_404(monkeypatch) -> None:
    """404 = 账号确实没有这条 Profile（非瞬态）：strict 也不抛，返回 None。"""
    _install_fake_httpx(monkeypatch, get=(404, {"detail": "no"}))
    assert await pg.fetch_profile_raw("u1", "t1", strict=True) is None


@pytest.mark.asyncio
async def test_fetch_profile_raw_strict_raises_on_5xx(monkeypatch) -> None:
    _install_fake_httpx(monkeypatch, get=(500, {"detail": "boom"}))
    with pytest.raises(pg.ProfileGatewayError):
        await pg.fetch_profile_raw("u1", "t1", strict=True)


@pytest.mark.asyncio
async def test_fetch_profile_raw_strict_raises_on_transport_error(monkeypatch) -> None:
    _install_fake_httpx(monkeypatch, get=pg.httpx.ConnectError("refused"))
    with pytest.raises(pg.ProfileGatewayError):
        await pg.fetch_profile_raw("u1", "t1", strict=True)


@pytest.mark.asyncio
async def test_fetch_profile_raw_returns_data_on_200(monkeypatch) -> None:
    _install_fake_httpx(monkeypatch, get=(200, {"data": {"mineru_api_token": "tok"}}))
    data = await pg.fetch_profile_raw("u1", "t1", strict=True)
    assert data == {"mineru_api_token": "tok"}


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
