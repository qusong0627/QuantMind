"""对外 API（`/api/ext/v1`）的机器身份：令牌铸造/校验与失败方向。

**这套东西要保护什么**
外部节点（Windows 上的交易系统 + 智能体）拿的是一枚长期凭据，换一枚短期
会话令牌来调 API。它**必须**满足：

* **不回落到公开默认密钥** —— C1 事故的成因就是 compose 里
  ``${INTERNAL_CALL_SECRET:-changeme-internal-secret}`` 这种回退值被烘进镜像。
  这里未配置就是「不可用」，不是「用一个众所周知的密钥」。
* **令牌不能被误认成用户 JWT** —— 用户 JWT 与机器令牌共用 ``Authorization:
  Bearer``。若机器令牌是同一套 JWT 密钥签的，一个签发处的 bug 就能让机器令牌
  当用户令牌使（或反过来）。这里用独立密钥 + 独立格式，两者互不认。
* **失败信息不泄露细节** —— 过期/签名错/密钥不存在对外都是同一个 401，
  否则等于给攻击者一个探测 oracle。
* **撤销即时生效** —— 节点凭据被停用后，未过期的令牌也必须立刻失效。

本文件只测**纯函数与依赖**，不连库：库里那一层由 `load_active_key` 挡在
可替换的接缝后面。
"""

from __future__ import annotations

import time

import pytest

from backend.services.api.routers.external import auth as ext_auth

SECRET = "unit-test-external-secret-9f3a"


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都显式给密钥——本模块的行为**依赖**于它被正确配置。"""
    monkeypatch.setenv(ext_auth.SECRET_ENV_KEY, SECRET)
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: SECRET)


# ---------------------------------------------------------------------------
# 密钥读取：未配置 = 不可用（不是「用默认值」）
# ---------------------------------------------------------------------------


def test_missing_secret_makes_secret_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: "")
    assert ext_auth.get_external_api_secret() == ""


def test_mint_fails_closed_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置密钥时**铸造必须失败**，而不是拿空串去签。"""
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: "")
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.mint_external_token("qm_live_abc")


@pytest.mark.parametrize(
    "public_default",
    ["changeme", "changeme-external-secret", "dev-external-secret", "secret", "test"],
)
def test_public_default_literals_are_treated_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch, public_default: str
) -> None:
    """公开默认值一律视为未配置——C1 同款防线。"""
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: public_default)
    assert ext_auth.get_external_api_secret() == ""


# ---------------------------------------------------------------------------
# 令牌往返与防篡改
# ---------------------------------------------------------------------------


def test_round_trip() -> None:
    token, expires_at = ext_auth.mint_external_token("qm_live_abc", ttl_seconds=600)
    payload = ext_auth.parse_external_token(token)
    assert payload["ak"] == "qm_live_abc"
    assert payload["exp"] == expires_at


def test_token_has_distinct_prefix() -> None:
    """独立前缀让「这是机器令牌」一眼可辨，也让用户 JWT 校验必然拒绝它。"""
    token, _ = ext_auth.mint_external_token("qm_live_abc")
    assert token.startswith("qmx1.")


def test_two_tokens_for_same_key_differ() -> None:
    """带 jti：同一凭据两次铸造的令牌不应相同（便于审计与选择性吊销）。"""
    a, _ = ext_auth.mint_external_token("qm_live_abc")
    b, _ = ext_auth.mint_external_token("qm_live_abc")
    assert a != b


def test_tampered_payload_is_rejected() -> None:
    token, _ = ext_auth.mint_external_token("qm_live_abc")
    head, payload, sig = token.split(".")
    forged = f"{head}.{payload[:-2]}XY.{sig}"
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.parse_external_token(forged)


def test_swapped_signature_is_rejected() -> None:
    a, _ = ext_auth.mint_external_token("qm_live_aaa")
    b, _ = ext_auth.mint_external_token("qm_live_bbb")
    a_head, a_payload, _ = a.split(".")
    _, _, b_sig = b.split(".")
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.parse_external_token(f"{a_head}.{a_payload}.{b_sig}")


def test_token_signed_with_other_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token, _ = ext_auth.mint_external_token("qm_live_abc")
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: "a-completely-different-secret")
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.parse_external_token(token)


def test_expired_token_is_rejected() -> None:
    token, _ = ext_auth.mint_external_token("qm_live_abc", ttl_seconds=1)
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.parse_external_token(token, now=int(time.time()) + 5)


@pytest.mark.parametrize(
    "garbage",
    ["", "not-a-token", "qmx1.", "qmx1.only-two", "qmx1.a.b.c", "Bearer x", ".."],
)
def test_malformed_tokens_are_rejected(garbage: str) -> None:
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.parse_external_token(garbage)


def test_user_jwt_is_not_accepted_as_machine_token() -> None:
    """用户 JWT（三段 base64url）必须被机器令牌解析器拒绝。

    两者的 header 都是 base64url JSON，所以「长得像」——这条钉住格式差异
    （前缀不同）真的起作用。
    """
    import jwt

    from backend.shared.auth import auth_manager

    user_jwt = auth_manager.create_access_token({"sub": "1", "username": "u"})
    # 用户 JWT 是三段、无 qmx1 前缀
    assert not user_jwt.startswith("qmx1.")
    with pytest.raises(ext_auth.ExternalAuthError):
        ext_auth.parse_external_token(user_jwt)
    assert jwt is not None  # 保持 import 有意义（jwt 由 auth_manager 内部使用）


def test_machine_token_is_not_accepted_as_user_jwt() -> None:
    """反方向同样要挡住：机器令牌不得通过用户 JWT 校验。"""
    from fastapi import HTTPException

    from backend.shared.auth import auth_manager

    token, _ = ext_auth.mint_external_token("qm_live_abc")
    with pytest.raises(HTTPException) as exc:
        auth_manager.verify_token(token)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 依赖：失败方向与撤销
# ---------------------------------------------------------------------------

def _creds(scheme: str, token: str):
    """构造 HTTPBearer 注入的凭据对象。"""
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


def _bearer(token: str):
    return _creds("Bearer", token)




class _Key:
    def __init__(self, *, is_active: bool = True, user_id: str = "7", tenant_id: str = "t1"):
        self.access_key = "qm_live_abc"
        self.is_active = is_active
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.permissions = ["read"]


@pytest.mark.anyio
async def test_dependency_rejects_without_bearer() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await ext_auth.require_external_principal(None)
    assert exc.value.status_code == 401
    assert exc.value.detail == ext_auth.AUTH_FAILED_DETAIL


@pytest.mark.anyio
async def test_dependency_rejects_malformed_bearer() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await ext_auth.require_external_principal(_creds("Basic", "abc"))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_dependency_accepts_active_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_loader(access_key: str):
        return _Key() if access_key == "qm_live_abc" else None

    monkeypatch.setattr(ext_auth, "load_active_key", _fake_loader)
    token, _ = ext_auth.mint_external_token("qm_live_abc")
    principal = await ext_auth.require_external_principal(_bearer(token))
    assert principal.access_key == "qm_live_abc"
    assert principal.user_id == "7"
    assert principal.tenant_id == "t1"


@pytest.mark.anyio
async def test_dependency_rejects_unknown_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    async def _fake_loader(access_key: str):
        return None

    monkeypatch.setattr(ext_auth, "load_active_key", _fake_loader)
    token, _ = ext_auth.mint_external_token("qm_live_abc")
    with pytest.raises(HTTPException) as exc:
        await ext_auth.require_external_principal(_bearer(token))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_revoked_key_kills_live_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """**撤销即时生效**：凭据被停用后，未过期的令牌立刻失效。

    这是「短期令牌 + 每请求查库」相较「纯自包含令牌」换来的性质——
    后者在令牌过期前无法吊销。
    """
    from fastapi import HTTPException

    async def _revoked_loader(access_key: str):
        return _Key(is_active=False)

    monkeypatch.setattr(ext_auth, "load_active_key", _revoked_loader)
    token, _ = ext_auth.mint_external_token("qm_live_abc")
    with pytest.raises(HTTPException) as exc:
        await ext_auth.require_external_principal(_bearer(token))
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_failure_detail_is_uniform(monkeypatch: pytest.MonkeyPatch) -> None:
    """过期 / 签名错 / 密钥不存在 —— 对外必须是同一个 detail，不给探测 oracle。"""
    from fastapi import HTTPException

    async def _none(access_key: str):
        return None

    monkeypatch.setattr(ext_auth, "load_active_key", _none)

    details = set()

    # (a) 未知密钥
    t1, _ = ext_auth.mint_external_token("qm_live_abc")
    with pytest.raises(HTTPException) as e1:
        await ext_auth.require_external_principal(_bearer(t1))
    details.add(e1.value.detail)

    # (b) 过期
    t2, _ = ext_auth.mint_external_token("qm_live_abc", ttl_seconds=-1)
    with pytest.raises(HTTPException) as e2:
        await ext_auth.require_external_principal(_bearer(t2))
    details.add(e2.value.detail)

    # (c) 畸形
    with pytest.raises(HTTPException) as e3:
        await ext_auth.require_external_principal(_bearer("qmx1.a.b"))
    details.add(e3.value.detail)

    assert details == {ext_auth.AUTH_FAILED_DETAIL}, f"失败信息可区分，构成探测 oracle：{details}"


# ---------------------------------------------------------------------------
# `load_active_key` 本体（前面所有用例都把它 monkeypatch 掉了）
# ---------------------------------------------------------------------------
#
# ⚠️ 上面 20 多条用例全都 `monkeypatch.setattr(ext_auth, "load_active_key", ...)`。
# 那对被测的**依赖**是对的（不连库），但对 `load_active_key` **自己**等于零覆盖
# ——而它现在被两个调用点共用（每请求鉴权依赖 + 握手），且刚刚被重写过。
# 下面把这一层补上：只桩掉 `get_session`，判定逻辑真跑。

from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Any  # noqa: E402

UTC = timezone.utc


class _Key:
    def __init__(self, *, is_active: bool = True, expires_at: Any = None) -> None:
        self.is_active = is_active
        self.expires_at = expires_at
        self.user_id = "7"
        self.tenant_id = "t1"
        self.permissions = ["read"]
        self.secret_hash = "$2b$12$ignored"


class _FakeSession:
    """最小 async context manager + execute。"""

    def __init__(self, key: Any, error: Exception | None) -> None:
        self._key, self._error = key, error

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, _stmt: Any) -> Any:
        if self._error:
            raise self._error
        return _FakeResult(self._key)


class _FakeResult:
    def __init__(self, key: Any) -> None:
        self._key = key

    def scalar_one_or_none(self) -> Any:
        return self._key


def _patch_session(monkeypatch: pytest.MonkeyPatch, key: Any, error: Exception | None = None) -> None:
    """桩在**源头模块**上：`load_active_key` 是函数内 import，patch 它自己的
    模块属性没有意义（这是本仓踩过的坑，见 `test_public_sync_hardening.py`）。"""
    import backend.shared.database_manager_v2 as db_mod

    def _get_session(*_a: Any, **_kw: Any) -> _FakeSession:
        return _FakeSession(key, error)

    monkeypatch.setattr(db_mod, "get_session", _get_session)


@pytest.mark.anyio
async def test_load_active_key_returns_a_valid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _Key(expires_at=datetime.now(UTC) + timedelta(days=1))
    _patch_session(monkeypatch, key)
    assert await ext_auth.load_active_key("qm_live_abc") is key


@pytest.mark.anyio
async def test_load_active_key_rejects_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _Key(is_active=False))
    assert await ext_auth.load_active_key("qm_live_abc") is None


@pytest.mark.anyio
async def test_load_active_key_rejects_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """过期凭据返回 None——**这条以前会 500**（aware 与 naive 相比抛 TypeError）。"""
    _patch_session(monkeypatch, _Key(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert await ext_auth.load_active_key("qm_live_abc") is None


@pytest.mark.anyio
async def test_load_active_key_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, None)
    assert await ext_auth.load_active_key("qm_live_abc") is None


@pytest.mark.anyio
async def test_load_active_key_fails_closed_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认（每请求鉴权路径）：查库失败 → None → 401。

    数据库抖一下不该让请求带着**未经核验**的身份通过。
    """
    _patch_session(monkeypatch, None, error=RuntimeError("db down"))
    assert await ext_auth.load_active_key("qm_live_abc") is None


@pytest.mark.anyio
async def test_load_active_key_raises_for_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """`on_backend_error="raise"`（握手路径）：503，不是 401。

    把「我们的库挂了」报成「你的凭据不对」会把运维引向客户端，方向反了。
    """
    _patch_session(monkeypatch, None, error=RuntimeError("db down"))
    with pytest.raises(ext_auth.AuthBackendUnavailable):
        await ext_auth.load_active_key("qm_live_abc", on_backend_error="raise")


@pytest.mark.anyio
async def test_both_error_modes_differ_only_in_who_gets_blamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个故障，两种模式必须给出**不同**的结果——否则那个参数是装饰品。"""
    _patch_session(monkeypatch, None, error=RuntimeError("db down"))
    assert await ext_auth.load_active_key("ak") is None
    with pytest.raises(ext_auth.AuthBackendUnavailable):
        await ext_auth.load_active_key("ak", on_backend_error="raise")
