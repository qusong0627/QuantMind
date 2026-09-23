"""对外 API 握手与能力查询（`/api/ext/v1/auth/session`、`/capabilities`）。

不连库：`get_session` 在端点内是**延迟 import** 的，这里把它换成假的，
于是端点逻辑（校验顺序、失败方向、状态码选择）全部真跑。

钉住三件事：

1. **握手失败不可枚举** —— 「凭据不存在」与「密钥错的」在状态码、detail、
   以及**耗时**上都一样。耗时那半靠一次占位 bcrypt 抹平，而占位哈希本身
   必须不触发 passlib 警告（它在 passlib 2.0 下会变成异常，那时枚举防线
   会变成 500——这类防线的失效方式必须被钉住）。
2. **能力查询如实反映实盘开关** —— 外部节点靠它决定敢不敢下真单。
   报错了比没有这个端点更糟。
3. **失败方向** —— 平台侧没配密钥是**部署问题**（503），不是调用方的凭据
   问题（401）。给 401 会让运维去查客户端凭证，方向反了。
"""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers.external import auth as ext_auth
from backend.services.api.routers.external import router as router_module
from backend.shared import live_trading_gate as gate

SECRET = "handshake-test-external-secret-4c1e"
ACCESS_KEY = "qm_live_handshake001"
GOOD_SECRET_KEY = "sk_" + "a1b2c3d4e5" * 3 + "a1b2"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: SECRET)


# ---------------------------------------------------------------------------
# 假的库接缝
# ---------------------------------------------------------------------------


def _good_hash() -> str:
    """真实 bcrypt 哈希（模块级算一次，bcrypt cost 12 不便宜）。"""
    from backend.services.api.user_app.services.api_key_service import pwd_context

    return pwd_context.hash(GOOD_SECRET_KEY)


_GOOD_HASH = _good_hash()


class _FakeKey:
    """最小替身。字段名与 `ApiKey` 模型一致——端点读的属性这里都得有，
    否则测试会以 AttributeError 的形式漏掉真实代码路径。"""

    def __init__(self, *, is_active: bool = True, expires_at=None, secret_hash: str = _GOOD_HASH):
        self.user_id = "42"
        self.tenant_id = "acme"
        self.permissions = ["market.read"]
        self.is_active = is_active
        self.expires_at = expires_at
        self.secret_hash = secret_hash


class _FakeResult:
    def __init__(self, key):
        self._key = key

    def scalar_one_or_none(self):
        return self._key


class _FakeSession:
    def __init__(self, key):
        self._key = key
        self.committed = False

    async def execute(self, *_a, **_kw):
        return _FakeResult(self._key)

    async def commit(self):
        self.committed = True


class _FakeCtx:
    def __init__(self, key):
        self.session = _FakeSession(key)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc):
        return False


def _install_fake_db(monkeypatch: pytest.MonkeyPatch, key) -> None:
    """把端点内的 `get_session` 换成假的（端点是延迟 import 的，patch 模块属性即可）。"""
    import backend.shared.database_manager_v2 as db

    monkeypatch.setattr(db, "get_session", lambda **_kw: _FakeCtx(key))


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(gate.ENV_KEY, "false")
    app = FastAPI()
    app.include_router(router_module.router, prefix=gate.EXT_API)
    gate.install_live_trading_gate_middleware(app, "test")
    return TestClient(app)


def _bearer(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    token, _ = ext_auth.mint_external_token(ACCESS_KEY)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. 占位哈希：不许警告，不许抛
# ---------------------------------------------------------------------------


def test_dummy_hash_is_clean_and_returns_false() -> None:
    """占位哈希必须「干净地返回 False」。

    手搓的 `"$2b$12$" + "X"*53` 能返回 False，但 passlib 会警告 padding bits
    并在 2.0 里把它变成异常——那时枚举防线会变成 500。这条钉住那个边界：
    **任何警告都算失败**。
    """
    from backend.services.api.user_app.services.api_key_service import pwd_context

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert pwd_context.verify("任何东西", router_module._DUMMY_BCRYPT_HASH) is False


def test_dummy_hash_is_not_a_real_credential() -> None:
    """占位哈希的明文不可知——它不该是任何真实凭据或空串。"""
    assert router_module._DUMMY_BCRYPT_HASH.startswith("$2b$")
    from backend.services.api.user_app.services.api_key_service import pwd_context

    for guess in ("", "sk_", "secret", "changeme"):
        assert pwd_context.verify(guess, router_module._DUMMY_BCRYPT_HASH) is False


# ---------------------------------------------------------------------------
# 2. 握手成功路径
# ---------------------------------------------------------------------------


def test_handshake_issues_a_working_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "_check_throttle", _noop_throttle)
    _install_fake_db(monkeypatch, _FakeKey())
    resp = client.post(
        f"{gate.EXT_API}/auth/session",
        json={"access_key": ACCESS_KEY, "secret_key": GOOD_SECRET_KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"].startswith("qmx1.")
    assert body["user_id"] == "42"
    assert body["tenant_id"] == "acme"
    assert body["permissions"] == ["market.read"]
    # 续期提示必须早于到期，否则节点会等到 401 才换
    assert body["renew_after"] < body["expires_at"]

    # 这枚令牌立刻可用
    caps = client.get(f"{gate.EXT_API}/capabilities", headers={"Authorization": f"Bearer {body['token']}"})
    assert caps.status_code == 200, caps.text
    assert caps.json()["principal"]["access_key"] == ACCESS_KEY


async def _noop_throttle(*_a, **_kw) -> None:
    return None


# ---------------------------------------------------------------------------
# 3. 握手失败：不可枚举
# ---------------------------------------------------------------------------


def _handshake(client: TestClient, secret: str):
    return client.post(
        f"{gate.EXT_API}/auth/session",
        json={"access_key": ACCESS_KEY, "secret_key": secret},
    )


def test_unknown_key_and_wrong_secret_are_indistinguishable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两种失败必须同状态码、同 detail——否则就是可用 access_key 的枚举器。"""
    monkeypatch.setattr(router_module, "_check_throttle", _noop_throttle)

    _install_fake_db(monkeypatch, _FakeKey())  # 凭据存在
    wrong = _handshake(client, "sk_totally-wrong-secret")

    _install_fake_db(monkeypatch, None)  # 凭据不存在
    missing = _handshake(client, "sk_totally-wrong-secret")

    assert wrong.status_code == missing.status_code == 401, (
        f"状态码不同：存在={wrong.status_code} 不存在={missing.status_code}"
    )
    assert wrong.json() == missing.json(), "响应体不同，可据此枚举出有效 access_key"


def test_inactive_key_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """停用的凭据不能换到令牌。"""
    monkeypatch.setattr(router_module, "_check_throttle", _noop_throttle)
    _install_fake_db(monkeypatch, _FakeKey(is_active=False))
    resp = _handshake(client, GOOD_SECRET_KEY)
    assert resp.status_code == 401


def test_expired_credential_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据自身的 expires_at 到期后不能换令牌（与令牌 TTL 是两回事）。"""
    from datetime import timedelta

    from backend.shared.utc_datetime import utc_now

    monkeypatch.setattr(router_module, "_check_throttle", _noop_throttle)
    _install_fake_db(monkeypatch, _FakeKey(expires_at=utc_now() - timedelta(days=1)))
    resp = _handshake(client, GOOD_SECRET_KEY)
    assert resp.status_code == 401


def test_unconfigured_platform_secret_is_503_not_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台没配密钥是**部署问题**（503），不是调用方凭据问题（401）。

    给 401 会让运维去查客户端凭证，方向反了。
    """
    monkeypatch.setattr(router_module, "_check_throttle", _noop_throttle)
    _install_fake_db(monkeypatch, _FakeKey())
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: "")  # 平台侧未配置

    resp = _handshake(client, GOOD_SECRET_KEY)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "external_api_not_configured"


# ---------------------------------------------------------------------------
# 4. 能力查询
# ---------------------------------------------------------------------------


def test_capabilities_requires_auth(client: TestClient) -> None:
    resp = client.get(f"{gate.EXT_API}/capabilities")
    assert resp.status_code == 401
    assert resp.json()["detail"] == ext_auth.AUTH_FAILED_DETAIL


def test_capabilities_rejects_user_jwt(client: TestClient) -> None:
    """用户 JWT 不能当机器令牌用——这条路径上两者会在同一个头里相遇。"""
    from backend.shared.auth import auth_manager

    user_jwt = auth_manager.create_access_token({"sub": "1", "username": "u"})
    resp = client.get(f"{gate.EXT_API}/capabilities", headers={"Authorization": f"Bearer {user_jwt}"})
    assert resp.status_code == 401


def test_capabilities_reports_real_trading_state_honestly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**能力查询必须如实反映实盘开关**——外部节点靠它决定敢不敢下真单。

    报错了比没有这个端点更糟：节点会以为自己能下单。
    """
    _install_fake_db(monkeypatch, _FakeKey())

    monkeypatch.setenv(gate.ENV_KEY, "false")
    off = client.get(f"{gate.EXT_API}/capabilities", headers=_bearer(monkeypatch))
    assert off.status_code == 200, off.text
    assert off.json()["trading"]["real_trading_enabled"] is False, (
        "实盘关闭却报 true——外部节点会去下单，然后收 403"
    )

    monkeypatch.setenv(gate.ENV_KEY, "true")
    on = client.get(f"{gate.EXT_API}/capabilities", headers=_bearer(monkeypatch))
    assert on.json()["trading"]["real_trading_enabled"] is True


#: 每个面当前的真实状态。**批次落地时改这里**——改完下面那条断言会把
#: 「声明为可用」与「真的有路由」对齐检查一遍。
EXPECTED_PLANE_AVAILABILITY = {
    "control": False,  # 批次 4
    "task": False,  # 批次 4
    "data": True,  # 批次 3
    "stream": False,  # 批次 4
    "trading": False,  # 批次 4
}


def test_capabilities_marks_unbuilt_planes_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个面的 `available` 必须如实——这是外部节点的**接入前提**。

    `available=False` 的面，节点不该去试；`true` 的面，节点会直接按它写代码。
    所以两个方向都要钉：没实现却报 true（节点撞 404）、实现了却报 false
    （功能白做）都是故障，只是后者安静一些。
    """
    _install_fake_db(monkeypatch, _FakeKey())
    body = client.get(f"{gate.EXT_API}/capabilities", headers=_bearer(monkeypatch)).json()
    planes = {p["plane"]: p for p in body["planes"]}
    assert set(planes) == set(EXPECTED_PLANE_AVAILABILITY)
    actual = {name: p["available"] for name, p in planes.items()}
    assert actual == EXPECTED_PLANE_AVAILABILITY, (
        f"面的可用性与实现不符：{actual}。"
        "批次落地时请同步更新 EXPECTED_PLANE_AVAILABILITY。"
    )


def test_planes_marked_available_have_real_routes(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """**`available=True` 必须真的有路由**，否则节点按它去调只会收 404。

    这条把「自我描述」与「实际挂载」钉在一起：上面那条测的是声明，
    这条测的是声明与网关的实际形状一致。反过来（有路由却报 false）
    只损失一个功能，不致错——所以只单向断言。
    """
    import warnings

    from backend.services.api.main import app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        served = {p for p in app.openapi().get("paths", {}) if p.startswith(gate.EXT_API)}

    for plane, available in EXPECTED_PLANE_AVAILABILITY.items():
        if not available:
            continue
        prefix = f"{gate.EXT_API}/{plane}/"
        assert [p for p in served if p.startswith(prefix)], (
            f"面 {plane} 被标成 available=true，但网关下没有 {prefix}* 的路由——"
            "外部节点照它写代码会全部 404"
        )


def test_capabilities_carries_server_time(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个响应带服务器时间：外部节点据此判断自己的时钟漂移。"""
    _install_fake_db(monkeypatch, _FakeKey())
    body = client.get(f"{gate.EXT_API}/capabilities", headers=_bearer(monkeypatch)).json()
    assert isinstance(body["server_time"], (int, float))
    assert body["server_time"] > 1_700_000_000


# ---------------------------------------------------------------------------
# 5. 节流桶键：不可由请求头操纵
# ---------------------------------------------------------------------------


def _req(host: str, headers: dict[str, str] | None = None):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "client": (host, 12345), "headers": raw, "method": "POST", "path": "/"})


def test_throttle_key_ignores_client_supplied_ip_headers() -> None:
    """**节流桶不得由请求头决定**。

    第一版实现读了 `X-Real-IP`。那两个头是客户端可控的——攻击者每试一次换一个
    值就换一个桶，节流等于没做。这条钉住「只认 TCP 对端」。
    """
    base = router_module._throttle_key(_req("10.0.0.9"), ACCESS_KEY)
    spoofed = [
        {"X-Real-IP": "1.2.3.4"},
        {"X-Forwarded-For": "5.6.7.8"},
        {"X-Real-IP": "9.9.9.9", "X-Forwarded-For": "9.9.9.9, 8.8.8.8"},
    ]
    for hdrs in spoofed:
        got = router_module._throttle_key(_req("10.0.0.9", hdrs), ACCESS_KEY)
        assert got == base, f"请求头 {hdrs} 改变了节流桶——可据此绕过限流"


def test_throttle_key_separates_peers_and_credentials() -> None:
    """不同对端、不同凭据必须落在不同桶；同一组合必须稳定（键不能带随机量）。"""
    a = router_module._throttle_key(_req("10.0.0.1"), "qm_live_aaa")
    b = router_module._throttle_key(_req("10.0.0.2"), "qm_live_aaa")
    c = router_module._throttle_key(_req("10.0.0.1"), "qm_live_bbb")
    assert len({a, b, c}) == 3, "对端或凭据不同却共用一个桶——限流会互相误伤"
    assert a == router_module._throttle_key(_req("10.0.0.1"), "qm_live_aaa")


def test_throttle_key_does_not_leak_the_access_key() -> None:
    """明文 access_key 不得出现在桶键里（键名会进 MONITOR/慢日志/备份）。"""
    key = router_module._throttle_key(_req("10.0.0.1"), "qm_live_supersecret")
    assert "qm_live_supersecret" not in key
