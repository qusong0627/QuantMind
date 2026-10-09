"""MinerU 用户级 Token 配置端点（/api/v1/ai-ide/config/doc-parse）契约测试。

个人中心「其他设置 → AI 服务配置」经这对端点读写用户自带 MinerU Token。
三条红线：
  1. GET 只回掩码；`profile_readable=False` 必须与「没配」区分开——把
     「Profile 读不到」显示成「没配」会诱导用户覆盖掉自己的 Token。
  2. POST 三态：不传字段=400、空串=清除、有值=覆盖。
  3. 任何日志/错误回包都不许出现 Token 明文（下游 FastAPI 422 的
     detail[].input 会原样回显请求体）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.routers.ai_ide import config as config_mod  # noqa: E402

TOKEN = "abcdefgh-1234-5678-ijkl-token-xyz"  # len > 8，掩码 = abc****-xyz

# POST 用例会 monkeypatch 共享 httpx 模块的 AsyncClient（config_mod.httpx 就是
# httpx 本身），测试自身的客户端类必须提前取好别名，否则会被一起换掉。
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture()
def app() -> FastAPI:
    test_app = FastAPI()

    @test_app.middleware("http")
    async def inject_user(request, call_next):
        request.state.user = {
            "user_id": request.headers.get("x-test-user", "u-1"),
            "tenant_id": request.headers.get("x-test-tenant", "t-1"),
        }
        return await call_next(request)

    test_app.include_router(config_mod.router, prefix="/api/v1/ai-ide/config")
    return test_app


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    return monkeypatch


def _install_profile(monkeypatch, data, *, calls: list | None = None):
    async def fake_fetch(user_id, tenant_id, *, strict=False):
        if calls is not None:
            calls.append((user_id, tenant_id, strict))
        return data

    monkeypatch.setattr(config_mod, "fetch_profile_raw", fake_fetch)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _RecordingClient:
    """记录 PUT 的 URL/headers/json，按预设回包。"""

    def __init__(self, response=None, *, exc: BaseException | None = None):
        self.response = response
        self.exc = exc
        self.puts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def put(self, url, **kwargs):
        self.puts.append(
            {"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")}
        )
        if self.exc is not None:
            raise self.exc
        return self.response


def _install_put(monkeypatch, *, response=None, exc=None) -> _RecordingClient:
    recorder = _RecordingClient(response, exc=exc)
    monkeypatch.setattr(config_mod.httpx, "AsyncClient", lambda *a, **kw: recorder)
    return recorder


def _client(app: FastAPI) -> httpx.AsyncClient:
    return _REAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ── GET：掩码 / 兜底 / 可读性 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reports_user_token_masked(app, clean_env) -> None:
    _install_profile(clean_env, {"mineru_api_token": TOKEN})

    async with _client(app) as client:
        resp = await client.get("/api/v1/ai-ide/config/doc-parse")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["profile_readable"] is True
    assert body["has_user_token"] is True
    assert body["masked_token"] == f"{TOKEN[:3]}****{TOKEN[-4:]}"
    assert body["env_configured"] is False
    assert body["effective_source"] == "user"
    assert TOKEN not in resp.text, "全量 Token 绝不能出现在回包里"


@pytest.mark.asyncio
async def test_get_falls_back_to_env(app, clean_env) -> None:
    clean_env.setenv("MINERU_API_TOKEN", "env-tok-123456")
    _install_profile(clean_env, {})

    async with _client(app) as client:
        resp = await client.get("/api/v1/ai-ide/config/doc-parse")

    body = resp.json()
    assert body["has_user_token"] is False
    assert body["masked_token"] == ""
    assert body["env_configured"] is True
    assert body["effective_source"] == "env"


@pytest.mark.asyncio
async def test_get_none_when_nothing_configured(app, clean_env) -> None:
    _install_profile(clean_env, {})

    async with _client(app) as client:
        resp = await client.get("/api/v1/ai-ide/config/doc-parse")

    body = resp.json()
    assert body["effective_source"] == "none"
    assert body["env_configured"] is False


@pytest.mark.asyncio
async def test_get_unreadable_profile_is_not_shown_as_unconfigured(
    app, clean_env
) -> None:
    """网关读不到 → profile_readable=False；不许伪装成「没配」（会让用户以为要重填）。"""
    clean_env.setenv("MINERU_API_TOKEN", "env-tok-123456")
    _install_profile(clean_env, None)

    async with _client(app) as client:
        resp = await client.get("/api/v1/ai-ide/config/doc-parse")

    body = resp.json()
    assert body["profile_readable"] is False
    assert body["has_user_token"] is False
    assert body["effective_source"] == "env", "读不到用户级时按 env 报有效来源"


@pytest.mark.asyncio
async def test_get_short_token_never_partially_echoed(app, clean_env) -> None:
    """长度不足 8 的 Token 掩码会泄露大半，必须整个隐去。"""
    _install_profile(clean_env, {"mineru_api_token": "short12"})

    async with _client(app) as client:
        resp = await client.get("/api/v1/ai-ide/config/doc-parse")

    body = resp.json()
    assert body["has_user_token"] is True
    assert body["masked_token"] == ""
    assert "short12" not in resp.text


@pytest.mark.asyncio
async def test_get_passes_identity_to_gateway(app, clean_env) -> None:
    calls: list = []
    _install_profile(clean_env, {}, calls=calls)

    async with _client(app) as client:
        await client.get(
            "/api/v1/ai-ide/config/doc-parse",
            headers={"x-test-user": "u-9", "x-test-tenant": "t-9"},
        )

    assert calls == [("u-9", "t-9", False)]


# ── POST：三态语义 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_saves_stripped_token(app, clean_env) -> None:
    recorder = _install_put(clean_env, response=_FakeResponse(200, {"code": 200}))

    async with _client(app) as client:
        resp = await client.post(
            "/api/v1/ai-ide/config/doc-parse",
            json={"mineru_api_token": f"  {TOKEN}  "},
        )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "message": "已保存"}
    assert len(recorder.puts) == 1
    put = recorder.puts[0]
    assert put["json"] == {"mineru_api_token": TOKEN}, "前后空白必须剥掉"
    assert put["url"].endswith("/api/v1/profiles/u-1")
    assert put["headers"]["X-User-Id"] == "u-1"
    assert put["headers"]["X-Tenant-Id"] == "t-1"
    assert "X-Internal-Call" in put["headers"]


@pytest.mark.asyncio
async def test_post_empty_string_clears(app, clean_env) -> None:
    recorder = _install_put(clean_env, response=_FakeResponse(200, {"code": 200}))

    async with _client(app) as client:
        resp = await client.post(
            "/api/v1/ai-ide/config/doc-parse", json={"mineru_api_token": ""}
        )

    assert resp.json()["message"] == "已清除"
    assert recorder.puts[0]["json"] == {"mineru_api_token": ""}, (
        "空串是显式清除，必须送出"
    )


@pytest.mark.asyncio
async def test_post_missing_field_is_400_without_gateway_call(app, clean_env) -> None:
    recorder = _install_put(clean_env, response=_FakeResponse(200, {}))

    async with _client(app) as client:
        resp = await client.post("/api/v1/ai-ide/config/doc-parse", json={})

    assert resp.status_code == 400
    assert recorder.puts == [], "字段都没传，不许空 PUT 打网关"


# ── POST：失败路径不泄露 Token ──────────────────────────────────────


@pytest.mark.asyncio
async def test_post_422_logs_fields_not_values(app, clean_env, caplog) -> None:
    """下游 422 的 detail[].input 就是明文 Token——日志只能记字段路径。"""
    _install_put(
        clean_env,
        response=_FakeResponse(
            422,
            {"detail": [{"loc": ["body", "mineru_api_token"], "input": TOKEN}]},
        ),
    )

    with caplog.at_level("ERROR", logger=config_mod.logger.name):
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/ai-ide/config/doc-parse",
                json={"mineru_api_token": TOKEN},
            )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "同步到用户服务失败"
    assert TOKEN not in resp.text
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in logged, "Token 明文进了日志"
    assert "body.mineru_api_token" in logged, "应留下出错字段供排查"


@pytest.mark.asyncio
async def test_post_transport_error_is_500_fixed_message(app, clean_env) -> None:
    _install_put(clean_env, exc=httpx.ConnectError("refused"))

    async with _client(app) as client:
        resp = await client.post(
            "/api/v1/ai-ide/config/doc-parse",
            json={"mineru_api_token": TOKEN},
        )

    assert resp.status_code == 500
    assert resp.json()["detail"] == "保存失败，请稍后重试"
    assert TOKEN not in resp.text


if __name__ == "__main__":  # pragma: no cover
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
