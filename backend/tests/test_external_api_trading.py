"""对外交易面（`external/trading.py`）与它背后的委托令牌（`upstream.py`）。

这个文件钉的是**钱**。三组断言，每组对应一个已经踩过或差点踩到的坑：

1. **提权边界**：`upstream.py` 的模块 docstring 承诺「委托令牌默认不带 admin」，
   并说这里有一对断言。那一对就是
   `test_delegated_token_is_not_admin_by_default` 与
   `test_admin_delegated_token_is_opt_in_and_only_for_the_admin_call`。
   含义：一枚对外凭据能下单、能看自己的模拟账户，但拿不到 trade 的**管理面**。
   `sub` 仍是 `10000001`（管理员族的用户 id），所以模拟盘照常落在
   10000001 这个账户上——**「进得了模拟盘」与「进不了控制面」不矛盾**，
   它们由两件不同的事决定（`sub` 的值 vs JWT 里的 admin 标志）。

2. **幂等**：对外契约比上游更严——幂等键**必需**。没有它，一次超时重试就是
   一张重复成交。这里同时钉住「头与报文冲突要 400」和「键最终以
   `client_order_id` 发到上游」。

3. **账户信封**：上游**两条返回路径的形状不一样**（已初始化时 market 在
   `data` 里，未初始化时 market 在**顶层**）。这两条都要能解析。反面同样重要：
   `data` 缺失时必须 502，**不许**退化成「空账户」——把一次契约漂移显示成
   「你账户里没钱」是最坏的一种错。

上游一律用替身。本文件不产生任何真实委托。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers.external import auth as ext_auth
from backend.services.api.routers.external import permissions as perms
from backend.services.api.routers.external import router as router_module
from backend.services.api.routers.external import trading as trading_plane
from backend.services.api.routers.external import upstream
from backend.shared import live_trading_gate as gate

SECRET = "trading-test-external-secret-7d3e"
ACCESS_KEY = "qm_live_tradetest001"

ORDER_ID = "0b6d2f4a-7c31-4e58-9a02-1f8b3c5d7e90"

#: 上游 `SimOrderResponse` 的最小合法实例（必填字段一个不少）。
_ORDER_ROW = {
    "order_id": ORDER_ID,
    "symbol": "600036",
    "side": "buy",
    "order_type": "limit",
    "quantity": 100.0,
    "price": 10.5,
    "status": "submitted",
    "filled_quantity": 0.0,
    "average_price": None,
    "order_value": 1050.0,
    "filled_value": 0.0,
    "commission": 0.0,
    "created_at": "2026-09-23T06:00:00Z",
    "updated_at": "2026-09-23T06:00:00Z",
}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: SECRET)


# ---------------------------------------------------------------------------
# 鉴权与权限接缝
# ---------------------------------------------------------------------------


class _FakeKey:
    def __init__(self, permissions: list[str]):
        self.user_id = "10000001"
        self.tenant_id = "default"
        self.permissions = permissions
        self.is_active = True
        self.expires_at = None
        self.secret_hash = "$2b$12$" + "x" * 53


class _FakeResult:
    def __init__(self, key):
        self._key = key

    def scalar_one_or_none(self):
        return self._key


class _FakeSession:
    def __init__(self, key):
        self._key = key

    async def execute(self, *_a, **_kw):
        return _FakeResult(self._key)

    async def commit(self):
        pass


class _FakeCtx:
    def __init__(self, key):
        self.key = key

    async def __aenter__(self):
        return _FakeSession(self.key)

    async def __aexit__(self, *_exc):
        return False


FULL_PERMISSIONS = [perms.PERMISSION_TRADE_READ, perms.PERMISSION_TRADE_WRITE]


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import backend.shared.database_manager_v2 as db

    monkeypatch.setattr(
        db, "get_session", lambda **_kw: _FakeCtx(_FakeKey(FULL_PERMISSIONS))
    )
    monkeypatch.setenv(gate.ENV_KEY, "false")
    app = FastAPI()
    app.include_router(router_module.router, prefix=gate.EXT_API)
    gate.install_live_trading_gate_middleware(app, "test")
    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    token, _ = ext_auth.mint_external_token(ACCESS_KEY)
    return {"Authorization": f"Bearer {token}"}


def _principal() -> ext_auth.ExternalPrincipal:
    return ext_auth.ExternalPrincipal(
        access_key=ACCESS_KEY,
        user_id="10000001",
        tenant_id="default",
        permissions=tuple(FULL_PERMISSIONS),
        session_expires_at=0,
    )


class UpstreamRecorder:
    """记录调用并按需返回预设体；`model=`/`list_model=` 与真实 `fetch_json` 同语义。"""

    def __init__(self, responses: dict[tuple[str, str], object] | None = None):
        self.responses = responses or {}
        self.calls: list[dict] = []

    async def __call__(self, service: str, method: str, path: str, **kw):
        self.calls.append({"service": service, "method": method, "path": path, **kw})
        payload = self.responses.get((method, path))
        if payload is None:
            raise AssertionError(f"本用例没有为 {method} {path} 准备响应")
        if isinstance(payload, Exception):
            # 真实 `fetch_json` 对上游 4xx 就是抛 HTTPException——替身照做，
            # 否则「透传」这条测的就不是透传。
            raise payload
        model = kw.get("model")
        if model is not None:
            return model.model_validate(payload)
        list_model = kw.get("list_model")
        if list_model is not None:
            return [list_model.model_validate(item) for item in payload]
        return payload

    def only_call(self) -> dict:
        assert len(self.calls) == 1, f"期望恰好一次上游调用，实际 {len(self.calls)}"
        return self.calls[0]


def _install(monkeypatch: pytest.MonkeyPatch, responses: dict | None = None):
    rec = UpstreamRecorder(responses)
    monkeypatch.setattr(trading_plane, "fetch_json", rec)
    return rec


# ---------------------------------------------------------------------------
# 1. 提权边界
# ---------------------------------------------------------------------------


def test_delegated_token_is_not_admin_by_default() -> None:
    """**默认不带 admin。**（`upstream.py` 承诺的那一对的另一半见下一条。）

    即使这枚对外凭据挂在 `10000001`（现网的管理员族用户）名下也一样：
    凭据的授权模型是 `api_keys.permissions` 里的字符串，而不是「这个人是谁」。
    让一枚 `["trade.read"]` 的凭据顺带拿到 trade 管理面的通行证，
    等于权限模型被绕过。
    """
    from backend.shared.auth import decode_jwt_token

    token = upstream._delegated_token(_principal())
    claims = decode_jwt_token(token)

    assert claims["sub"] == "10000001", "委托身份必须是被绑定的那个用户"
    assert claims["roles"] == ["user"]
    assert "is_admin" not in claims, "默认路径不许带 admin 标志"
    assert claims.get("is_admin") is not True
    assert "admin" not in claims["roles"]


def test_admin_delegated_token_is_opt_in_and_only_for_the_admin_call() -> None:
    """**能进模拟盘**（sub 不变）**但拿不到 trade 控制面**（默认无 admin）。

    这一条与上一条合起来就是模块 docstring 承诺的那对断言：
    两个事实不矛盾，因为它们由两件不同的事决定——
    * `sub == "10000001"` 决定**模拟账户是哪一个**（`require_sim_user_id`，
      与人工 UI 同一个账户）；
    * JWT 里的 admin 标志决定**能不能进管理面**（`require_admin`）。
    """
    from backend.shared.auth import decode_jwt_token

    default_claims = decode_jwt_token(upstream._delegated_token(_principal()))
    admin_claims = decode_jwt_token(upstream._delegated_token(_principal(), admin=True))

    # 提权只加 admin，**不动身份**——模拟盘不会因此换账户
    assert default_claims["sub"] == admin_claims["sub"] == "10000001"
    # 默认那枚进不了管理面
    assert "is_admin" not in default_claims
    # 显式那枚进得了，且两个服务认的形状都给了
    assert admin_claims["is_admin"] is True
    assert admin_claims["roles"] == ["admin"]


def test_privileged_field_names_are_not_representable_in_the_order_body() -> None:
    """报文模型**接不住**任何看起来像提权的字段（`extra="forbid"` → 422）。"""
    from pydantic import ValidationError

    base = {
        "symbol": "600036",
        "side": "buy",
        "order_type": "limit",
        "quantity": 100,
        "price": 10.5,
        "client_order_id": "p1",
    }
    for probe in ("admin", "is_admin", "roles", "role", "permissions", "user_id",
                  "tenant_id", "trading_mode"):
        with pytest.raises(ValidationError) as excinfo:
            trading_plane.SimOrderRequest.model_validate({**base, probe: True})
        assert any(
            err["type"] == "extra_forbidden" and err["loc"] == (probe,)
            for err in excinfo.value.errors()
        ), f"{probe} 应当以 extra_forbidden 被拒，实际：{excinfo.value.errors()}"


def test_admin_is_a_keyword_only_module_argument_not_a_derived_value() -> None:
    """`admin` 三个入口**都**是 keyword-only 且默认 `False`。

    这条是「外部无从提权」的结构性版本：`admin` 不在任何报文模型里（上一条），
    也不在任何一个函数的**位置参数**里——调用点必须显式写出 `admin=True`，
    代码评审时一眼看得见。若哪天有人把它改成位置参数或给了非 False 的默认值，
    这里立刻红。
    """
    import inspect

    for fn in (upstream._delegated_token, upstream.build_headers, upstream.fetch_json):
        param = inspect.signature(fn).parameters["admin"]
        assert param.default is False, f"{fn.__name__} 的 admin 默认值必须是 False"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{fn.__name__} 的 admin 必须是 keyword-only"
        )


# ---------------------------------------------------------------------------
# 请求头：从零构造（与 `trade_proxy` 的减法相反）
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTPClient:
    """替掉 `httpx.AsyncClient`，把**真正发出去**的请求原样记下来。

    ⚠️ 走的是 `GET /sim/orders/{id}` 而不是 `/sim/account`：账户端点调
    `fetch_json` 时**不传 model**（它自己展开 `{success,data}` 信封），
    而 `fetch_json` 在「调用方只要成了」这条路上直接返回 None、连 `.json()`
    都不调。委托详情端点带着 `model=`，会真的解析响应——整条链路都在。
    """

    def __init__(self, payload):
        self._payload = payload
        self.requests: list[dict] = []

    async def request(self, **kwargs):
        self.requests.append(kwargs)
        return _FakeHTTPResponse(self._payload)


def test_client_supplied_headers_never_reach_upstream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """客户端塞进来的 `X-User-Id` / `X-Internal-Call` / `Cookie` **一个都到不了上游**。

    这是本面与 `trade_proxy` 最本质的区别，也是整条对外链路最该钉住的一条：
    那边是「转发一个我不完全信任的请求」（减法，靠剥离名单），这边是「我自己
    造一个请求」（加法）。这里不查 `build_headers` 的返回值（那是它自己的事），
    而是走完整条 HTTP 路由、把**真正发给上游的那些头**抓下来看。
    """
    fake = _FakeHTTPClient(_ORDER_ROW)
    monkeypatch.setattr(upstream, "_get_client", lambda: fake)

    resp = client.get(
        f"/api/ext/v1/trading/sim/orders/{ORDER_ID}",
        headers={
            **auth_headers,
            "X-User-Id": "99999999",          # 冒充另一个用户
            "X-Tenant-Id": "other-tenant",
            "X-Internal-Call": "attacker-supplied",
            "Cookie": "session=stolen",
            "X-Forwarded-For": "10.0.0.1",
        },
    )
    assert resp.status_code == 200, resp.text

    sent = fake.requests[0]["headers"]
    assert sent["X-User-Id"] == "10000001", "身份必须来自凭据绑定的用户"
    assert sent["X-Tenant-Id"] == "default"
    assert sent["Authorization"].startswith("Bearer ")
    assert "Cookie" not in sent
    assert "X-Forwarded-For" not in sent
    # X-Internal-Call 只可能来自服务端自己的配置，绝不等于客户端塞的那个值
    assert sent.get("X-Internal-Call") != "attacker-supplied"


def test_delegated_token_is_never_returned_to_the_caller(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """委托令牌**只活在这一次 httpx 请求里**，不回吐给调用方。

    `upstream.py` 的例外条款（`admin=True` 不削弱默认决定）整个建立在这一点上：
    调用方拿不到那枚提权令牌，也就无从重放。所以这里断言的是**它没被回吐**——
    响应体里不该出现任何令牌形状的东西。
    """
    fake = _FakeHTTPClient(_ORDER_ROW)
    monkeypatch.setattr(upstream, "_get_client", lambda: fake)

    resp = client.get(
        f"/api/ext/v1/trading/sim/orders/{ORDER_ID}", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert "Bearer" not in resp.text
    assert "qmx1." not in resp.text, "会话令牌也不该被回吐"
    # 反过来：令牌确实发出去了（否则上面两条断言是空的）
    sent_auth = fake.requests[0]["headers"]["Authorization"]
    assert sent_auth.startswith("Bearer ")
    assert sent_auth != auth_headers["Authorization"], "发出去的是委托令牌，不是会话令牌"


# ---------------------------------------------------------------------------
# 2. 幂等
# ---------------------------------------------------------------------------


def test_missing_idempotency_key_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """**幂等键必需。** 缺了 400，且**不碰上游**。"""
    rec = _install(monkeypatch, {})
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers=auth_headers,
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100,
            "price": 10.5,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "idempotency_key_required"
    assert rec.calls == [], "被拒的请求不该打到上游"


@pytest.mark.parametrize("where", ["header", "body", "both"])
def test_idempotency_key_from_either_place(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
    where: str,
) -> None:
    """键放头、放报文、或两处都给（且一致）都要能用。"""
    rec = _install(monkeypatch, {("POST", "/api/v1/simulation/orders"): _ORDER_ROW})
    headers = dict(auth_headers)
    body: dict = {
        "symbol": "600036",
        "side": "buy",
        "order_type": "limit",
        "quantity": 100,
        "price": 10.5,
    }
    if where in ("header", "both"):
        headers["Idempotency-Key"] = "probe-key-1"
    if where in ("body", "both"):
        body["client_order_id"] = "probe-key-1"

    resp = client.post("/api/ext/v1/trading/sim/orders", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    assert rec.only_call()["json_body"]["client_order_id"] == "probe-key-1"


def test_conflicting_idempotency_keys_are_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """头与报文都给了但**不一致** → 400。

    放行的话，调用方下次重试可能只带其中一个，于是同一笔意图拿到两个键、
    下出两张单——正是幂等要防的那件事。
    """
    rec = _install(monkeypatch, {})
    headers = dict(auth_headers)
    headers["Idempotency-Key"] = "key-from-header"
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers=headers,
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100,
            "price": 10.5,
            "client_order_id": "key-from-body",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "idempotency_key_conflict"
    assert rec.calls == []


def test_matching_keys_on_both_places_is_fine() -> None:
    """纯函数层：两处一致 → 取该值（不是报错）。"""
    assert trading_plane.resolve_idempotency_key("k", "k") == "k"
    assert trading_plane.resolve_idempotency_key(" k ", "k") == "k"
    assert trading_plane.resolve_idempotency_key(None, "k") == "k"
    assert trading_plane.resolve_idempotency_key("k", None) == "k"
    assert trading_plane.resolve_idempotency_key(None, None) is None
    assert trading_plane.resolve_idempotency_key("", "  ") is None


def test_trading_mode_is_pinned_server_side(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """`trading_mode` **由服务端钉死**。

    这条不只是「检查后拒绝」，是**表达不出来**：报文模型里根本没有这个字段
    （`extra="forbid"`），而发给上游的 body 里它恒为 `SIMULATION`。
    """
    rec = _install(monkeypatch, {("POST", "/api/v1/simulation/orders"): _ORDER_ROW})
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers={**auth_headers, "Idempotency-Key": "k1"},
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100,
            "price": 10.5,
        },
    )
    assert resp.status_code == 201
    assert rec.only_call()["json_body"]["trading_mode"] == "SIMULATION"


def test_client_cannot_specify_trading_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """调用方**写不出** `trading_mode=REAL`：报文模型 extra=forbid → 422。"""
    rec = _install(monkeypatch, {})
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers={**auth_headers, "Idempotency-Key": "k1"},
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100,
            "price": 10.5,
            "trading_mode": "REAL",
        },
    )
    assert resp.status_code == 422, resp.text
    assert rec.calls == []


def test_typo_in_field_name_is_rejected_not_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """`quantitiy` 这类拼错必须 422。

    静默忽略的后果是「下单成功但数量是默认值」——一笔真实成交跑在错误的
    数量上，而调用方以为它传对了。
    """
    rec = _install(monkeypatch, {})
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers={**auth_headers, "Idempotency-Key": "k1"},
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantitiy": 100,
            "price": 10.5,
        },
    )
    assert resp.status_code == 422, resp.text
    assert rec.calls == []


def test_market_order_omits_price(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """市价单不能带 `price`：上游对市价单收到 price 的行为与限价单不同，
    未指定时**不发这个键**（而不是发一个 null）。"""
    rec = _install(
        monkeypatch,
        {
            ("POST", "/api/v1/simulation/orders"): {
                **_ORDER_ROW,
                "order_type": "market",
                "price": None,
            }
        },
    )
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers={**auth_headers, "Idempotency-Key": "k1"},
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
    )
    assert resp.status_code == 201, resp.text
    assert "price" not in rec.only_call()["json_body"]


# ---------------------------------------------------------------------------
# 3. 账户信封的两条形状
# ---------------------------------------------------------------------------


def test_account_initialized_path_market_inside_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """已初始化：`market` 在 `data` **里面**。"""
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/account"): {
                "success": True,
                "data": {
                    "market": "CN",
                    "cash": 123456.78,
                    "total_asset": 200000.0,
                    "market_value": 76543.22,
                    "positions": {
                        "SH600036": {
                            "volume": 100.0,
                            "available_volume": 0.0,
                            "cost": 35.5,
                            "market_value": 3600.0,
                            "price": 36.0,
                        }
                    },
                    "position_count": 1,
                    "initial_equity": 200000.0,
                    "total_pnl": 0.0,
                    "today_pnl": -120.0,
                    "monthly_pnl": 340.0,
                    # 上游 dict 里还有十几个我们没承诺的键，extra=ignore 要吃掉它们
                    "maintenance_margin_ratio": 0.0,
                    "liabilities": 0.0,
                },
            }
        },
    )
    body = client.get("/api/ext/v1/trading/sim/account", headers=auth_headers).json()
    assert body["market"] == "CN"
    assert body["cash"] == 123456.78
    assert body["positions"]["SH600036"]["volume"] == 100.0
    assert body["position_count"] == 1
    assert "liabilities" not in body, "未承诺的键不该透出去"


def test_account_not_initialized_path_market_at_top_level(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """未初始化：`market` 在**顶层**，`data` 里没有它。

    这是上游两条路径形状不同之处。本面不看上游给哪一份，直接用请求里那个
    `market`（它已是规范字面量）——所以上游调整信封层级时这里不会跟着错。
    """
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/account"): {
                "success": True,
                "market": "CN",
                "data": {
                    "cash": 0.0,
                    "total_asset": 0.0,
                    "market_value": 0.0,
                    "positions": {},
                    "account_not_initialized": True,
                },
            }
        },
    )
    body = client.get(
        "/api/ext/v1/trading/sim/account?market=CN", headers=auth_headers
    ).json()
    assert body["market"] == "CN", "顶层那个 market 也要能被盖回来"
    assert body["account_not_initialized"] is True
    assert body["cash"] == 0.0


def test_account_market_echoes_the_request_not_the_upstream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """上游回一个**不同**的 market 时，以**请求**为准。

    这防止的是「上游按自己的默认值回，而调用方以为查的是自己请求的那个市场」。
    """
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/account"): {
                "success": True,
                "data": {
                    "market": "CN",  # 上游说是 CN
                    "cash": 1.0,
                    "total_asset": 1.0,
                    "market_value": 0.0,
                },
            }
        },
    )
    body = client.get(
        "/api/ext/v1/trading/sim/account?market=HK", headers=auth_headers
    ).json()
    assert body["market"] == "HK"


def test_account_contract_drift_is_502_not_an_empty_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """`data` 信封缺失 → **502**，不是「空账户」。

    把一次契约漂移显示成「你账户里没钱」是最坏的一种错：调用方会据此
    认为资产归零并可能触发风控/告警，而真实情况是接口坏了。
    """
    for bad in ({"success": True}, {"success": True, "data": None}, {"success": True, "data": []}):
        _install(monkeypatch, {("GET", "/api/v1/simulation/account"): bad})
        resp = client.get("/api/ext/v1/trading/sim/account", headers=auth_headers)
        assert resp.status_code == 502, f"{bad} → {resp.status_code}"
        assert resp.json()["detail"] == "upstream_contract_changed"


def test_account_missing_required_field_is_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """必填字段（`cash`/`total_asset`/`market_value`）缺失 → 502。

    这三个是**契约**：上游一改名，这里立刻硬失败，而不是让那个字段恒为 null。
    可空字段改名则降级成 null——分层是刻意的。
    """
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/account"): {
                "success": True,
                "data": {"total_asset": 1.0, "market_value": 0.0},  # 少 cash
            }
        },
    )
    resp = client.get("/api/ext/v1/trading/sim/account", headers=auth_headers)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream_contract_changed"


def test_account_optional_field_missing_is_null_not_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """可空字段缺失 → **null**，不是 0。

    本仓在市值零值上踩过：把「取不到」显示成 0，下游会当成「确实是 0」。
    注意 `position_count` 与它们不同——**0 是它的真值**，不是缺失。
    """
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/account"): {
                "success": True,
                "data": {
                    "cash": 1.0,
                    "total_asset": 1.0,
                    "market_value": 0.0,
                    "accounts": None,  # 上游多给的键，被 ignore
                },
            }
        },
    )
    body = client.get("/api/ext/v1/trading/sim/account", headers=auth_headers).json()
    assert body["initial_equity"] is None
    assert body["total_pnl"] is None
    assert body["today_pnl"] is None
    assert body["monthly_pnl"] is None
    assert body["position_count"] is None
    # 但与「确实是 0」区分得开：market_value 是真值 0
    assert body["market_value"] == 0.0


def test_account_market_is_an_enum_not_a_free_string(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """拼错的 market 要 422。

    上游只是 `market.upper()`，`market=CHINA` 不会报错，只会造出一个空的、
    永远没数据的账户——外部节点会以为「账户是空的」而不是「我参数写错了」。
    """
    rec = _install(monkeypatch, {})
    resp = client.get(
        "/api/ext/v1/trading/sim/account?market=CHINA", headers=auth_headers
    )
    assert resp.status_code == 422
    assert rec.calls == []


# ---------------------------------------------------------------------------
# 4. 委托路径与时间序列化
# ---------------------------------------------------------------------------


def test_order_path_param_is_a_uuid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """路径参数是 **UUID**（`sim_orders.order_id`），不是自增 id。

    上游两者都有；本模块刻意只暴露 UUID——自增 id 是跨租户唯一的实现细节。
    一个非 UUID 的路径**到不了**路由函数：闸门的放行表把这一段的形状写死成
    `[0-9a-fA-F-]{36}`，先一步 403。所以这里分两层各测各的：

    * 闸门层（纯函数）：非 UUID 形状判为 blocked；
    * 路由层（**不带闸门**地挂路由）：真给到路由函数时是 422。

    两层都测是有意的——闸门的形状表是「碰巧」与 UUID 同宽的，不能当成
    「参数校验」的替代；反过来路由的 422 也不该被当成「外部到得了一个非法路径」。
    """
    # 闸门层
    assert gate.is_blocked("GET", f"/api/ext/v1/trading/sim/orders/{ORDER_ID}") is False
    assert gate.is_blocked("GET", "/api/ext/v1/trading/sim/orders/12345") is True

    # 路由层：把闸门摘掉，直接看路由自己怎么答
    import backend.shared.database_manager_v2 as db

    monkeypatch.setattr(
        db, "get_session", lambda **_kw: _FakeCtx(_FakeKey(FULL_PERMISSIONS))
    )
    bare = FastAPI()
    bare.include_router(router_module.router, prefix=gate.EXT_API)
    bare_client = TestClient(bare)

    rec = _install(
        monkeypatch, {("GET", f"/api/v1/simulation/orders/{ORDER_ID}"): _ORDER_ROW}
    )
    ok = bare_client.get(
        f"/api/ext/v1/trading/sim/orders/{ORDER_ID}", headers=auth_headers
    )
    assert ok.status_code == 200, ok.text

    bad = bare_client.get("/api/ext/v1/trading/sim/orders/12345", headers=auth_headers)
    assert bad.status_code == 422, "自增 id 在路由层不该被接受"
    assert len(rec.calls) == 1, "被拒的请求不该打到上游"


def test_timestamps_are_serialized_with_z(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """瞬时列一律带 `Z`（CLAUDE.md「瞬时时间」）。

    上游发 `Z`、这里发 `+00:00` 的话两处都是 UTC，但客户端做字符串比对时
    会判成不同。
    """
    _install(
        monkeypatch, {("GET", f"/api/v1/simulation/orders/{ORDER_ID}"): _ORDER_ROW}
    )
    body = client.get(
        f"/api/ext/v1/trading/sim/orders/{ORDER_ID}", headers=auth_headers
    ).json()
    assert body["created_at"].endswith("Z")
    assert "+00:00" not in body["created_at"]


def test_trades_endpoint_is_the_reconciliation_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """成交列表能解析（对账以它为准，不是拿委托列表推算）。"""
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/trades"): [
                {
                    "trade_id": ORDER_ID,
                    "order_id": ORDER_ID,
                    "symbol": "600036",
                    "side": "buy",
                    "quantity": 100.0,
                    "price": 10.5,
                    "trade_value": 1050.0,
                    "commission": 5.25,
                    "executed_at": "2026-09-23T06:01:00Z",
                }
            ]
        },
    )
    body = client.get("/api/ext/v1/trading/sim/trades", headers=auth_headers).json()
    assert len(body) == 1
    assert body[0]["commission"] == 5.25
    assert body[0]["executed_at"].endswith("Z")


def test_upstream_4xx_is_passed_through_not_masked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """上游 4xx **原样透传**（那是调用方的问题：余额不足、标的不对）。

    5xx 才会被翻译成 502——分界的意义是让调用方的重试/告警逻辑能按状态码
    分类：把上游 500 原样吐出去会让它以为该退避重试，而上游 500 常需要人去看。
    """
    from fastapi import HTTPException

    _install(
        monkeypatch,
        {
            ("POST", "/api/v1/simulation/orders"): HTTPException(
                status_code=400, detail="可用资金不足"
            )
        },
    )
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers={**auth_headers, "Idempotency-Key": "k1"},
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100000,
            "price": 10.5,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "可用资金不足"


def test_simulation_endpoints_stay_reachable_when_real_trading_is_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """实盘**开着**时模拟盘同样可达（闸门不该影响它）。"""
    monkeypatch.setenv(gate.ENV_KEY, "true")
    _install(
        monkeypatch,
        {
            ("GET", "/api/v1/simulation/account"): {
                "success": True,
                "data": {"cash": 1.0, "total_asset": 1.0, "market_value": 0.0},
            }
        },
    )
    assert (
        client.get("/api/ext/v1/trading/sim/account", headers=auth_headers).status_code
        == 200
    )
