"""对外 API 的**凭据范围**（`api_keys.permissions`）。

这个文件保护的东西一句话说清：**一枚只读凭据不能自己变成能下单的凭据。**

在批次 4 之前，`api_keys.permissions` 全仓没有任何读取点——它的实际语义是
「装饰」：`["trade.read"]` 的凭据与 `["trade.write"]` 的凭据在服务端完全等价。
对外面把「把 key 交给外部节点」这件事变成了常态，装饰性的权限字段就从
「无伤大雅的展示」变成了「一个会让人误判自己权限的字段」。

钉住四件事：

1. **判定是精确相等**，没有通配、没有蕴含。`trade.write` 不蕴含 `trade.read`
   （本仓在闸门放行表上已经吃过「前缀匹配顺带放行」的亏，见
   `live_trading_gate` 里那两段注释）。
2. **空列表 = 没有权限**，不是「全部」。把 NULL/[] 当全权，会让「漏填」与
   「故意收窄」变成同一件事，而它们的失败后果正好相反。
3. **403 而不是 401**。凭据是有效的，只是没被授权；报 401 会让调用方去重新
   握手换令牌，而换一百次令牌权限也不会变。
4. **名单与实际路由对得上**：`GATED_ENDPOINTS` 里不能有幽灵条目（指向不存在
   的端点），也不能有漏网之鱼（交易面新加一个端点却没挂码）。
   后半条是这整个文件存在的**主要理由**——其它几条都是它的推论。
"""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers.external import auth as ext_auth
from backend.services.api.routers.external import permissions as perms
from backend.services.api.routers.external import router as router_module
from backend.services.api.routers.external import trading as trading_plane
from backend.shared import live_trading_gate as gate

SECRET = "permission-test-external-secret-9f2a"
ACCESS_KEY = "qm_live_permtest0001"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: SECRET)


# ---------------------------------------------------------------------------
# 假的库接缝：`permissions` 从 api_keys 行的这个字段来（每请求查库）
# ---------------------------------------------------------------------------


class _FakeKey:
    def __init__(self, permissions: list[str]):
        self.user_id = "10000001"
        self.tenant_id = "default"
        self.permissions = permissions
        self.is_active = True
        self.expires_at = None
        self.secret_hash = "$2b$12$" + "x" * 53  # 占位，本文件不走握手


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
        self.session = _FakeSession(key)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(gate.ENV_KEY, "false")
    app = FastAPI()
    app.include_router(router_module.router, prefix=gate.EXT_API)
    gate.install_live_trading_gate_middleware(app, "test")
    return TestClient(app)


def _as_credential(
    monkeypatch: pytest.MonkeyPatch, permissions: list[str]
) -> dict[str, str]:
    """把「这枚凭据有哪些权限」装进假的库接缝，返回一个可用的 Authorization 头。"""
    import backend.shared.database_manager_v2 as db

    monkeypatch.setattr(db, "get_session", lambda **_kw: _FakeCtx(_FakeKey(permissions)))
    token, _ = ext_auth.mint_external_token(ACCESS_KEY)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游一律不许被真的调到。

    这条是**安全属性**而不是省事：下面每个「有权限」的用例都应该停在
    「已知会去调上游」这一步之前或正好那一步。若哪天有人在权限依赖**之前**
    加了别的分支（比如先读一次上游再判权限），这个 fixture 会让它变成
    一个显式的失败，而不是一次真的打到 trade 的请求。
    """

    async def _boom(*_a, **_kw):
        raise AssertionError("本文件不该真的调用上游")

    monkeypatch.setattr(trading_plane, "fetch_json", _boom)


# ---------------------------------------------------------------------------
# 1. 判定语义（纯函数）
# ---------------------------------------------------------------------------


def _principal(permissions: tuple[str, ...]) -> ext_auth.ExternalPrincipal:
    return ext_auth.ExternalPrincipal(
        access_key=ACCESS_KEY,
        user_id="10000001",
        tenant_id="default",
        permissions=permissions,
        session_expires_at=0,
    )


def test_empty_permission_list_grants_nothing() -> None:
    """空列表 = 没有权限。**不是**「全部」——见模块 docstring 第 2 条。"""
    p = _principal(())
    assert perms.has_permission(p, perms.PERMISSION_TRADE_READ) is False
    assert perms.has_permission(p, perms.PERMISSION_TRADE_WRITE) is False


@pytest.mark.parametrize(
    "granted,asked",
    [
        # 没有蕴含关系（两个方向都不蕴含）
        ("trade.read", "trade.write"),
        ("trade.write", "trade.read"),
        # 没有通配
        ("*", "trade.write"),
        ("trade.*", "trade.write"),
        ("trade", "trade.read"),
        # 没有前缀/子串匹配
        ("trade.readonly", "trade.read"),
        ("trade.read", "trade"),
        # 大小写敏感（不是 `.lower()` 之后比）
        ("TRADE.READ", "trade.read"),
        # 带空格就是另一个字符串
        (" trade.read", "trade.read"),
    ],
)
def test_matching_is_exact_equality(granted: str, asked: str) -> None:
    """**精确相等**。上面每一对都「看起来像」应该通过——它们都不能通过。

    这条不是在挑字眼：`permissions` 是签发时人手工填的字符串数组，
    任何「宽松一点更好用」的匹配规则都会让一枚本意只读的凭据悄悄多出写权限。
    """
    assert perms.has_permission(_principal((granted,)), asked) is False


def test_exact_match_does_grant() -> None:
    """反面：真正该过的要过（免得上面的严格断言靠「恒为 False」蒙混过关）。"""
    assert perms.has_permission(_principal(("trade.read",)), "trade.read") is True


# ---------------------------------------------------------------------------
# 2. 端点行为：403 而不是 401，且不带码就过不去
# ---------------------------------------------------------------------------


_GATED_GET = ("/api/ext/v1/trading/sim/account", "/api/ext/v1/trading/sim/trades")
_GATED_POST = ("/api/ext/v1/trading/sim/orders",)


@pytest.mark.parametrize("path", _GATED_GET)
def test_read_endpoint_needs_trade_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    headers = _as_credential(monkeypatch, [])
    resp = client.get(path, headers=headers)
    assert resp.status_code == 403, f"{path} 在无权限时没有拒绝：{resp.text}"
    assert resp.json()["detail"] == "permission_denied"


def test_write_endpoint_needs_trade_write(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持 `trade.read` 的凭据下不了单——这正是本文件的核心命题。"""
    headers = _as_credential(monkeypatch, [perms.PERMISSION_TRADE_READ])
    resp = client.post(
        "/api/ext/v1/trading/sim/orders",
        headers=headers,
        json={
            "symbol": "600036",
            "side": "buy",
            "order_type": "limit",
            "quantity": 100,
            "price": 10.5,
            "client_order_id": "probe-1",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "permission_denied"


def test_403_is_not_401_so_the_client_does_not_rehandshake(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**403 而不是 401**（模块 docstring 第 3 条）。

    401 的语义是「你的令牌不认」，调用方据此会去重新握手——而权限与令牌无关，
    换一百次令牌也换不来 `trade.write`。方向反了会把排查引向凭据。
    """
    headers = _as_credential(monkeypatch, [])
    resp = client.get("/api/ext/v1/trading/sim/orders", headers=headers)
    assert resp.status_code == 403
    assert resp.status_code != 401
    assert "WWW-Authenticate" not in resp.headers


def test_ungated_endpoint_ignores_permissions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有挂码的端点不受影响：一枚空权限凭据照样能读能力、读数据索引。

    `permissions.py` 的 docstring 说明了为什么现在只关交易面（给数据面新造
    `data.read` 会让现网凭据**立刻**失去数据面，而没有任何界面能加回来）。
    """
    headers = _as_credential(monkeypatch, [])
    resp = client.get("/api/ext/v1/capabilities", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["principal"]["permissions"] == []


def test_principal_permissions_come_from_the_credential_row_not_the_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """权限每请求从库里的**行**读，不是签进令牌里。

    这是「吊销即时生效」的同一条性质：凭据的权限被收窄后，它已签发的、
    尚未过期的令牌立刻按新权限判定。若权限被烘进令牌，就得等 TTL 过期。
    """
    headers = _as_credential(monkeypatch, ["trade.read"])
    body = client.get("/api/ext/v1/capabilities", headers=headers).json()
    assert body["principal"]["permissions"] == ["trade.read"]


@pytest.mark.parametrize(
    "permissions,path",
    [
        (["trade.read"], "/api/ext/v1/trading/sim/orders"),
        (["trade.read"], "/api/ext/v1/trading/sim/account"),
    ],
)
def test_correct_permission_actually_gets_through(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    permissions: list[str],
    path: str,
) -> None:
    """**反面**：有码就要真的放行。

    没有这一条，上面所有 `assert 403` 都可以被一个「把端点删了」的实现满足
    ——404 不是 403，那些断言会红，但一个「权限依赖写反了、恒拒绝」的实现
    会让这个文件变成在测试一个坏掉的东西。这里放一个良性的上游替身，
    确认请求真的走到了 handler。
    """
    headers = _as_credential(monkeypatch, permissions)

    async def _fake_fetch(*_a, **_kw):
        if path.endswith("/orders"):
            return []
        return {
            "success": True,
            "data": {
                "cash": 1000.0,
                "total_asset": 1000.0,
                "market_value": 0.0,
                "positions": {},
                "account_not_initialized": False,
            },
        }

    monkeypatch.setattr(trading_plane, "fetch_json", _fake_fetch)

    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, f"{path} 在有权限时仍然被拒：{resp.text}"


# ---------------------------------------------------------------------------
# 3. 名单 vs 实际路由（本文件的主要理由）
# ---------------------------------------------------------------------------


def _ext_paths() -> set[str]:
    """实际路由集。走 `openapi()`——本仓的 `include_router` 包成了
    `_IncludedRouter`，`r.path` 上根本取不到（见 gate coverage 的说明）。"""
    probe = FastAPI()
    probe.include_router(router_module.router, prefix=gate.EXT_API)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return {p for p in probe.openapi().get("paths", {}) if p.startswith(gate.EXT_API)}


def test_every_gated_entry_is_a_real_route() -> None:
    """`GATED_ENDPOINTS` 里不许有幽灵条目。

    曾经有一条 `GET /api/ext/v1/trading/risk`：规划时写下的，而风控四级状态机
    至今**没有 HTTP 端点**。幽灵条目的危害是它会「验证通过」——测试拿它去请求
    得到 404，而 404 里没有 `permission_denied`……或者更糟，有人为了让测试变绿
    去把断言放宽。两种情况都让这份名单彻底失去意义。
    """
    real = _ext_paths()
    phantoms = [
        f"{method} {path}"
        for method, path in perms.GATED_ENDPOINTS
        if path not in real
    ]
    assert not phantoms, (
        f"权限名单里这些端点在实际路由里不存在：{phantoms}。"
        "要么删掉名单条目，要么把端点建出来——不要留一条指向 404 的权限声明。"
    )


def test_every_gated_entry_has_a_requirement() -> None:
    """两张表必须一一对应：有端点没码 = 那个端点其实没关。"""
    assert set(perms.GATED_ENDPOINTS) == set(perms.GATED_REQUIREMENTS), (
        "GATED_ENDPOINTS 与 GATED_REQUIREMENTS 对不上："
        f"{set(perms.GATED_ENDPOINTS) ^ set(perms.GATED_REQUIREMENTS)}"
    )


def test_every_requirement_is_a_declared_constant() -> None:
    """码值只能取自本模块声明的常量。

    裸字面量（`"trade.write"` 手写一遍）会在改名时漏掉一处，而漏掉的那处
    表现是「这枚凭据永远 403」——一个查起来很费劲、看起来像缓存的问题。
    """
    declared = {perms.PERMISSION_TRADE_READ, perms.PERMISSION_TRADE_WRITE}
    assert set(perms.GATED_REQUIREMENTS.values()) <= declared


def test_no_trading_route_is_left_ungated() -> None:
    """**交易面不许有没挂码的端点。** 这条是名单的兜底方向。

    上面那条测的是「名单里没有假的」，这条测的是「没有真的漏了」。
    将来有人给交易面加一个 `GET /trading/sim/positions`，只改 `trading.py`
    而忘了来 `permissions.py` 挂码，这条会红——否则那个端点会**默认可达**，
    而所有测试全绿。
    """
    real = _ext_paths()
    trading_routes = {p for p in real if p.startswith(f"{gate.EXT_API}/trading/")}
    assert trading_routes, "前提失效：对外交易面一条路由都没有，本断言会空转"

    gated_paths = {path for _method, path in perms.GATED_ENDPOINTS}
    ungated = sorted(trading_routes - gated_paths)
    assert not ungated, (
        f"这些对外交易面端点没有挂权限码，任何有效凭据都能调：{ungated}。"
        "请在 permissions.py 的 GATED_ENDPOINTS/GATED_REQUIREMENTS 各加一行，"
        "并想清楚它该要 trade.read 还是 trade.write。"
    )


def test_gated_endpoints_declare_a_real_http_method() -> None:
    """方法名写错（`GETT`）会让这条权限**永不生效**，而且是静默的。"""
    probe = FastAPI()
    probe.include_router(router_module.router, prefix=gate.EXT_API)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        paths = probe.openapi().get("paths", {})

    for method, path in perms.GATED_ENDPOINTS:
        assert method in {"GET", "POST"}, f"没见过的 HTTP 方法：{method}"
        declared = {m.upper() for m in paths.get(path, {})}
        assert method in declared, (
            f"{method} {path} 不在该路径声明的方法里（实际：{sorted(declared)}）"
        )
