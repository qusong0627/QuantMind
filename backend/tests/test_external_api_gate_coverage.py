"""对外命名空间（`/api/ext/v1`）必须被实盘闸门覆盖，且**默认拒绝**。

为什么单独一条闸门测试
----------------------
`live_trading_gate.py` 的两张表（`_BLOCKED_PREFIXES` / `_BLOCKED_EXACT`）里每一个
字面量都以 `/api/v1` 开头，`_matches` 的边界又落在 `/` 上。所以
`/api/ext/v1/orders` 这种路径：

* 不等于 `/api/v1/...`，也对不上任何前缀；
* 于是 `is_blocked()` 返回 `False`，**闸门放行**。

也就是说，闸门目前只守住了 `/api/v1` 这一个命名空间，而「新路由默认落在拒绝侧」
这个说法在**新命名空间**上是不成立的——对外命名空间是后来者，它落在拒绝表的
覆盖范围之外，默认可达。这与闸门模块 docstring 里承诺的失败方向相反。

本文件钉住修好之后的形状：

1. 对外命名空间**未登记即拒绝**（fail-closed），登记走显式放行/拒绝两张表；
2. 该策略对**运行时新加的路由**同样生效——不是只对当前这几个端点特判；
3. 放行表是**承重**的：把它清空，已登记端点必须变成拒绝
   （否则放行表与实现脱节，测试会变成空转）；
4. 闸门路由发现**不是空集**——零个端点参与时本文件必须失败，而不是全绿。
"""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.shared import live_trading_gate as gate

# ---------------------------------------------------------------------------
# 命名空间的唯一出处
# ---------------------------------------------------------------------------


def test_ext_namespace_constant_exists_and_is_distinct() -> None:
    """对外命名空间必须与 `/api/v1` 分开且不以它为前缀。

    若哪天有人把它改成 `/api/v1/ext`，闸门现有的拒绝表就会自动覆盖到它——
    那也行，但必须是有意的：这条会红，逼人改这里的断言并重新想一遍。
    """
    assert gate.EXT_API == "/api/ext/v1"
    assert not gate._matches(gate.EXT_API, gate._API), (
        "对外命名空间落在了 /api/v1 之下，闸门原有的拒绝表会连带生效，"
        "本文件的默认拒绝策略就不再是唯一防线了——请重新评估。"
    )


# ---------------------------------------------------------------------------
# 默认拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/ext/v1/orders",
        "/api/ext/v1/orders/123",
        "/api/ext/v1/trade/anything",
        "/api/ext/v1/something-nobody-registered-yet",
    ],
)
def test_unregistered_ext_paths_are_denied(path: str) -> None:
    """对外命名空间里**没登记过**的路径一律拒绝——新端点不会默认可达。"""
    assert gate.is_blocked("GET", path) is True
    assert gate.is_blocked("POST", path) is True


@pytest.mark.parametrize("path", ["/api/ext/v1/auth/session", "/api/ext/v1/capabilities"])
def test_registered_ext_paths_are_allowed(path: str) -> None:
    """已登记的非交易端点放行（否则对外接入连握手都做不了）。"""
    assert gate.is_blocked("POST", path) is False
    assert gate.is_blocked("GET", path) is False


def test_every_real_ext_route_is_covered_by_the_policy() -> None:
    """防漂移：从 router **实际**枚举路由，逐条断言已登记。

    上面那些 `is False` 用的是手写字面量路径。一旦有人给 router 换前缀或
    改端点名，那些字面量就变成在测一个不存在的路径，而新路径默默落进
    「未登记即拒绝」——功能没坏，但**外部节点会莫名其妙收 403**，
    而测试全绿。这条把两边钉在一起。
    """
    from backend.services.api.routers.external.router import router as ext_router

    real = {gate.EXT_API + r.path for r in ext_router.routes}
    assert real, "对外 router 一条路由都没有——下面的断言会空转"
    assert real == {
        "/api/ext/v1/auth/session",
        "/api/ext/v1/capabilities",
    }, f"对外路由集合变了：{sorted(real)}。请同步更新本文件与闸门登记表。"

    uncovered = [p for p in real if gate.is_blocked("GET", p)]
    assert not uncovered, (
        f"这些对外路由没在闸门里登记，实盘关闭的部署上会 403：{uncovered}。"
        "请在 live_trading_gate._ALLOWED_EXT_ENDPOINTS 或 _BLOCKED_EXT_PREFIXES 登记。"
    )


def test_real_app_serves_ext_at_the_expected_paths() -> None:
    """router 声明的前缀必须与 `main.py` 实际挂载的结果一致。

    上面几条测的是 router 对象（`EXT_API + r.path`）。若有人把 `main.py` 里的
    `prefix=EXT_API` 改成别的字面量，router 级断言全绿而**线上路径已经变了**——
    外部节点会全部 404，而闸门那边还在拦一个不存在的路径。

    ⚠️ 用 OpenAPI schema 而不是 `app.routes`：本仓库的 FastAPI 会把被 include 的
    router 包成 `_IncludedRouter` 节点而非摊平，`app.routes` 里根本看不到
    `/api/ext/...`（第一版探针就是这么被误导的）。

    ⚠️ **这里不设 skip**。第一版用 `try/except → pytest.skip` 兜装配失败，
    结果真出过一次：`main.py` 从一个已删掉的转出里 import `EXT_API`，网关
    **整个启动不了**，而这条测试只报了一句 skip、其余全绿。装配失败就是故障本身，
    不该被降级成一个安静的跳过。
    """
    from backend.services.api.main import app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 网关里已有若干 duplicate-operation-id 噪声
        paths = set(app.openapi().get("paths", {}))

    served = {p for p in paths if p.startswith(gate.EXT_API)}
    assert served == {
        "/api/ext/v1/auth/session",
        "/api/ext/v1/capabilities",
    }, (
        f"网关实际伺服的对外路径与预期不符：{sorted(served)}。"
        "如果确实改了，请同步更新 router 测试与闸门登记表。"
    )


def test_router_does_not_hardcode_the_namespace() -> None:
    """对外 router 里不得出现命名空间字面量——前缀只能有一个出处。

    第一版这条写的是 `assert "EXT_API" in src`，**那是个空转断言**：
    import 行里出现一次就绿了，跟 router 有没有真的用它对不上。改成
    查「不许有字面量」——这个性质才是有意义的那个。
    """
    import inspect
    import re

    from backend.services.api.routers.external import router as router_module

    src = inspect.getsource(router_module)
    hits = [
        f"L{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if re.search(r"""["']/api/ext""", line)
    ]
    assert not hits, (
        "对外 router 里硬写了命名空间字面量，前缀出现了第二个出处：\n" + "\n".join(hits)
    )


def test_ext_denies_are_not_confused_with_v1() -> None:
    """`/api/ext/v1/...` 的判定不得被 `/api/v1` 的规则污染，反之亦然。"""
    # ext 命名空间不该影响 v1 的既有结论
    assert gate.is_blocked("POST", "/api/v1/orders") is True
    assert gate.is_blocked("GET", "/api/v1/tdx/quote-feed") is False
    # v1 的行情白名单不该漏到 ext 上（`/api/ext/v1/market` 没登记 = 拒绝）
    assert gate.is_blocked("GET", "/api/ext/v1/market") is True


# ---------------------------------------------------------------------------
# 承重性：放行表真的在起作用（防「策略写了但没接线」）
# ---------------------------------------------------------------------------


def test_allowlist_entries_are_endpoint_scoped_not_namespace_scoped() -> None:
    """放行表必须登记到**端点**，不能登记到命名空间。

    `_matches` 是前缀匹配，所以登记 `f"{EXT_API}/auth"` 会把整段
    `/api/ext/v1/auth/*` 一起放行——将来加改密/换机/吊销端点时，它们**不会**
    触发「未登记即拒绝」，而这正是本文件与闸门 docstring 承诺的摩擦。
    写多深，放行就止于多深。
    """
    siblings = (
        "/api/ext/v1/auth/rotate",
        "/api/ext/v1/auth/revoke",
        "/api/ext/v1/auth/session/extra",
        "/api/ext/v1/authz",
    )
    leaked = [p for p in siblings if gate.is_blocked("POST", p) is False]
    assert not leaked, (
        f"这些路径落在放行表的命名空间前缀下、被自动放行了：{leaked}。"
        "放行表登记得太宽——请登记到端点（如 /auth/session）。"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/ext/v1/auth/session",
        "/api/ext/v1/auth/session/",  # 尾斜杠必须同判，否则外部节点莫名 403
        "/api/ext/v1/capabilities",
        "/api/ext/v1/capabilities/",
    ],
)
def test_exact_match_survives_trailing_slash(path: str) -> None:
    """放行改精确相等之后，尾斜杠仍须同判。

    `_normalize` 去尾斜杠，所以这两种写法落在同一个字符串上。若哪天有人把
    `_normalize` 去掉，这条会红——而那意味着「客户端多打一个 `/` 就 403」，
    对外部节点是极难排查的故障（网关的 307 重定向在中间件**之后**，
    根本轮不到它）。
    """
    assert gate.is_blocked("GET", path) is False
    assert gate.is_blocked("POST", path) is False


def test_allowlist_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空放行表后，已登记端点必须变成拒绝。

    若放行表根本没被 `is_blocked` 读取，这条会绿——那说明上面那些
    `is False` 的断言测的是别的东西。
    """
    monkeypatch.setattr(gate, "_ALLOWED_EXT_ENDPOINTS", ())
    assert gate.is_blocked("POST", "/api/ext/v1/auth/session") is True
    assert gate.is_blocked("GET", "/api/ext/v1/capabilities") is True


def test_default_deny_is_not_an_accident(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认拒绝必须来自「未登记」，不是来自「刚好没匹配上任何拒绝项」。

    这条与上一条互为反面：证明拒绝侧的判定**读的是登记表**。
    """
    monkeypatch.setattr(gate, "_ALLOWED_EXT_ENDPOINTS", ("/api/ext/v1/whatever",))
    assert gate.is_blocked("GET", "/api/ext/v1/whatever") is False, (
        "登记为放行后仍被拒——默认拒绝没有读放行表，策略是硬编码的"
    )
    monkeypatch.setattr(gate, "_BLOCKED_EXT_PREFIXES", ("/api/ext/v1/trade",))
    assert gate.is_blocked("POST", "/api/ext/v1/trade/x") is True, (
        "登记进拒绝表仍被放行——拒绝表没被读取"
    )


# ---------------------------------------------------------------------------
# 行为面：真装中间件跑一遍，并用「运行时新加的路由」证明默认拒绝
# ---------------------------------------------------------------------------


@pytest.fixture()
def _disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gate.ENV_KEY, "false")


def _app_with_gate() -> FastAPI:
    app = FastAPI()

    @app.post("/api/ext/v1/auth/session")
    async def _session() -> dict[str, str]:  # pragma: no cover - 桩
        return {"ok": "session"}

    @app.get("/api/ext/v1/capabilities")
    async def _caps() -> dict[str, str]:  # pragma: no cover - 桩
        return {"ok": "capabilities"}

    gate.install_live_trading_gate_middleware(app, "test")
    return app


@pytest.mark.usefixtures("_disabled")
def test_middleware_lets_registered_ext_routes_through() -> None:
    client = TestClient(_app_with_gate())
    assert client.post("/api/ext/v1/auth/session").status_code == 200
    assert client.get("/api/ext/v1/capabilities").status_code == 200


@pytest.mark.usefixtures("_disabled")
def test_middleware_blocks_a_route_added_at_runtime() -> None:
    """**核心断言**：闸门装好之后再挂一个对外交易路由，它必须立刻被 403。

    这条模拟的是真实风险——将来有人往对外命名空间加 `/orders`，
    只挂 router 不改闸门。默认拒绝策略下他什么都不用做就是安全的；
    策略没接的话，这个端点在实盘关闭的部署上直接可达。
    """
    app = _app_with_gate()

    @app.post("/api/ext/v1/orders")
    async def _orders() -> dict[str, str]:  # pragma: no cover - 桩
        return {"ok": "placed a real order"}

    client = TestClient(app)
    resp = client.post("/api/ext/v1/orders")
    assert resp.status_code == 403, (
        f"对外命名空间里后加的交易路由没被挡住（HTTP {resp.status_code}）——"
        "实盘关闭的部署上它是可达的。"
    )
    assert resp.json()["detail"] == gate.DISABLED_DETAIL


def test_enabled_deployment_does_not_block_ext(monkeypatch: pytest.MonkeyPatch) -> None:
    """实盘开启时闸门整体让路——对外命名空间也不例外（否则开关只开了一半）。"""
    monkeypatch.setenv(gate.ENV_KEY, "true")
    client = TestClient(_app_with_gate())
    assert client.post("/api/ext/v1/orders").status_code == 404  # 路由不存在，但没被 403
