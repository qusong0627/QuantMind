"""内部信任链的**身份透传**：`X-Internal-Call` + `X-User-Id` 走代理也不丢。

**这套东西要保护什么**

同容器里的服务回环调用（QuantBot → `/api/v1/alpha-agent/*`）走的是
`127.0.0.1:8000`（API 网关）→ engine 代理 → engine 服务。网关代理的身份来源是
`get_optional_user`——它**只**认 `Authorization: Bearer`，于是握着内部密钥、
却没有 JWT 的调用方在这里被判成匿名：代理转发时不带 `X-User-Id`，engine 中间件
（`X-Internal-Call` 匹配才采信身份头）随后以「需要登录」401。

2026-09-23 实测事故就是这么来的：QuantBot 因子查询/回测/解读全线 401，而修复
前的绕法是「把管理员口令放进源码去 /auth/login 换 JWT」——公开仓里等于公开口令，
且口令一改就静默 401。

本文件锁死两条：
1. `get_optional_user` 采信内部信任头（与 `get_current_user` 同口径）；
2. **失败方向**：密钥不匹配 / 服务端未配置密钥 / 缺 `X-User-Id` 一律匿名，
   绝不因为在场一个 `X-User-Id` 就放行。
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from backend.services.api.routers import engine_proxy
from backend.services.api.user_app.middleware import auth as api_auth

SECRET = "unit-test-internal-secret-7c41"


def _request(headers: dict[str, str], body: bytes = b"") -> Request:
    """最小可用的 Starlette 请求（headers + query_params + 空 body 通道）。"""

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/alpha-agent/factors",
            "query_string": b"",
            "headers": raw,
        },
        receive,
    )


@pytest.fixture(autouse=True)
def _fixed_internal_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端密钥显式给值——本模块的行为**依赖**于它被正确配置。

    `engine_proxy` 是 `from ... import get_internal_call_secret`（模块级绑定），
    只补丁源头的话代理仍在用真实 runtime 密钥——两处都要补，否则「伪造头」那条
    用例会因为拿到真密钥而假绿。
    """
    monkeypatch.setattr("backend.shared.auth.get_internal_call_secret", lambda: SECRET)
    monkeypatch.setattr(engine_proxy, "get_internal_call_secret", lambda: SECRET)


# ---------------------------------------------------------------------------
# 正例：内部身份经 get_optional_user 透出
# ---------------------------------------------------------------------------


def test_internal_headers_yield_identity_without_jwt() -> None:
    user = asyncio.run(
        api_auth.get_optional_user(
            _request(
                {
                    "X-Internal-Call": SECRET,
                    "X-User-Id": "10000001",
                    "X-Tenant-Id": "default",
                }
            ),
            None,
        )
    )
    assert user is not None, "内部密钥匹配 + X-User-Id 必须给出身份（否则回环调用 401）"
    assert user["user_id"] == "10000001"
    assert user["tenant_id"] == "default"


def test_tenant_defaults_when_header_absent() -> None:
    user = asyncio.run(
        api_auth.get_optional_user(
            _request({"X-Internal-Call": SECRET, "X-User-Id": "10000001"}), None
        )
    )
    assert user is not None
    assert user["tenant_id"] == "default"


# ---------------------------------------------------------------------------
# 反例：失败方向一律匿名
# ---------------------------------------------------------------------------


def test_wrong_secret_does_not_grant_identity() -> None:
    """密钥不匹配时 `X-User-Id` 就是一枚普通请求头——绝不能冒充用户。"""
    user = asyncio.run(
        api_auth.get_optional_user(
            _request({"X-Internal-Call": "not-the-secret", "X-User-Id": "10000001"}),
            None,
        )
    )
    assert user is None


def test_missing_user_header_is_anonymous() -> None:
    user = asyncio.run(
        api_auth.get_optional_user(_request({"X-Internal-Call": SECRET}), None)
    )
    assert user is None


def test_unconfigured_server_secret_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务端密钥读不出来（""）= 内部信任链整体失效，不是「用默认值」。"""
    monkeypatch.setattr("backend.shared.auth.get_internal_call_secret", lambda: "")
    user = asyncio.run(
        api_auth.get_optional_user(
            _request({"X-Internal-Call": SECRET, "X-User-Id": "10000001"}), None
        )
    )
    assert user is None


def test_no_credentials_still_anonymous() -> None:
    """回归护栏：无头无令牌仍是 None（代理据此放行匿名只读路径）。"""
    assert asyncio.run(api_auth.get_optional_user(_request({}), None)) is None


# ---------------------------------------------------------------------------
# 端到端（单进程内）：依赖解出的身份必须真的进了转发头
# ---------------------------------------------------------------------------


class _FakeResponse:
    content = b"{}"
    status_code = 200
    headers = {"content-type": "application/json"}


class _FakeAsyncClient:
    """替掉 httpx，只为把转发头截下来看。"""

    captured: dict = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def request(self, **kwargs: object) -> _FakeResponse:
        type(self).captured = dict(kwargs)
        return _FakeResponse()


def test_engine_proxy_forwards_internal_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回环调用 → 代理 → engine：`X-Internal-Call` 与 `X-User-Id` 都要带上。

    这正是 2026-09-23 事故的接缝：`get_optional_user` 认不出内部身份 ⇒ 代理不转发
    ⇒ engine 401。两端各自正确、中间断链。
    """
    monkeypatch.setattr(engine_proxy.httpx, "AsyncClient", _FakeAsyncClient)
    req = _request(
        {"X-Internal-Call": SECRET, "X-User-Id": "10000001", "X-Tenant-Id": "default"}
    )
    user = asyncio.run(api_auth.get_optional_user(req, None))
    resp = asyncio.run(engine_proxy._proxy(req, user))
    assert resp.status_code == 200

    headers = {k.lower(): v for k, v in _FakeAsyncClient.captured["headers"].items()}
    assert headers.get("x-internal-call") == SECRET
    assert headers.get("x-user-id") == "10000001"
    assert headers.get("x-tenant-id") == "default"


def test_engine_proxy_does_not_forward_forged_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """伪造头（密钥不符）→ 匿名透出，`X-User-Id` 绝不落到转发头里。"""
    monkeypatch.setattr(engine_proxy.httpx, "AsyncClient", _FakeAsyncClient)
    req = _request({"X-Internal-Call": "forged", "X-User-Id": "10000001"})
    user = asyncio.run(api_auth.get_optional_user(req, None))
    assert user is None
    asyncio.run(engine_proxy._proxy(req, user))

    headers = {k.lower(): v for k, v in _FakeAsyncClient.captured["headers"].items()}
    assert headers.get("x-user-id") is None
    assert headers.get("x-internal-call") == SECRET  # 代理自建的那枚，非客户端所发
