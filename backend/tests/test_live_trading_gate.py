"""实盘闸门（`backend/shared/live_trading_gate.py`）的行为测试。

这个闸门有**两个方向**都会出事，所以两个方向都要钉住：

**拦少了** —— 生产部署里留一条能下真单的路。所以拒绝表逐条断言，
    且每个被拒路径都要带上机器可读的 ``detail == real_trading_disabled``
    （前端与探针靠它识别，不是靠 403 这个码本身）。

**拦多了** —— 把模拟盘或行情打死。两处最容易误伤：

    1. ``/api/v1/tdx/*`` 同一前缀下混着行情（``quote-feed``）与交易
       （``push-signals``），按前缀拦会断掉盘中行情主源；
    2. ``/api/v1/real-trading/*`` 名字像实盘，实际是**两种模式共用的运行台**
       —— 模拟盘部署一样要调 ``/preflight``、``/trading-precheck``、``/start``、
       ``/manual-executions``。第一版闸门就是按前缀拦了这个 router，
       会让模拟盘点「启动」直接 403；下面
       ``test_shared_runtime_not_prefix_blocked`` 是那条的回归。

**本文件证明什么、不证明什么**：用真实的 FastAPI 应用 + 真实的中间件跑 ASGI 栈，
所以「挂上去了没有」「403 的包体长什么样」是真的。但路由是桩（catch-all），
不做 DB/Redis —— 它不证明真实 router 的注册前缀与拒绝表不冲突。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.shared import live_trading_gate as gate

API = "/api/v1"

#: 实盘专有端点：开关关闭时必须 403（方法, 路径）
BLOCKED_CASES: tuple[tuple[str, str], ...] = (
    # 实盘委托（模拟盘走 /api/v1/simulation/orders）
    ("POST", f"{API}/orders"),
    ("GET", f"{API}/orders"),
    ("DELETE", f"{API}/orders/12345"),
    ("POST", f"{API}/orders/12345/cancel"),
    # 大 QMT 真单镜像 / 止损止盈
    ("GET", f"{API}/qmt-mirror/status"),
    ("POST", f"{API}/qmt-mirror/enabled"),
    ("POST", f"{API}/qmt-mirror/kill"),
    ("GET", f"{API}/qmt-sltp/config"),
    ("PUT", f"{API}/qmt-sltp/config"),
    ("POST", f"{API}/qmt-sltp/reset"),
    # 券商接入与凭证
    ("GET", f"{API}/broker-config-status"),
    ("PUT", f"{API}/broker-config/selected/CN"),
    ("POST", f"{API}/broker-config/qmt_exec/test"),
    ("GET", f"{API}/broker-config/qmt_exec"),
    # 信号下发券商 / 自动交易配置
    ("POST", f"{API}/tdx/push-signals"),
    ("POST", f"{API}/tdx/rolling-signals"),
    ("GET", f"{API}/tdx/rolling-config"),
    ("PUT", f"{API}/tdx/rolling-config"),
    ("GET", f"{API}/tdx/sltp-config"),
    ("PUT", f"{API}/tdx/sltp-config"),
    # 券商侧委托查询与撤单
    ("GET", f"{API}/tdx/orders"),
    ("POST", f"{API}/tdx/orders/cancel"),
    ("GET", f"{API}/tdx/inflight"),
    # 实盘流水 / 账户参数 / 撤单（精确拒绝）
    ("GET", f"{API}/real-trading/orders"),
    ("GET", f"{API}/real-trading/history"),
    ("PUT", f"{API}/real-trading/account/settings"),
    ("POST", f"{API}/risk/cancel-all"),
)

#: 行情面：**任何方法**都必须放行。误杀 = 盘中行情主源断流。
MARKET_DATA_CASES: tuple[str, ...] = (
    f"{API}/tdx/config",
    f"{API}/tdx/overview",
    f"{API}/tdx/quote-feed/status",
    f"{API}/tdx/quote-tick-sessions",
    f"{API}/tdx/quote-ticks",
    f"{API}/tdx/l2/realtime",
    f"{API}/tdx/l2/status",
    f"{API}/tdx/l2-config",
    f"{API}/market/snapshot",
    f"{API}/market/kline",
    f"{API}/ws/market",
)

#: 双模式共用端点（模拟盘也在用）：必须放行，模式由 handler 内判定。
SHARED_RUNTIME_CASES: tuple[str, ...] = (
    f"{API}/real-trading/preflight",
    f"{API}/real-trading/trading-precheck",
    f"{API}/real-trading/start",
    f"{API}/real-trading/stop",
    f"{API}/real-trading/status",
    f"{API}/real-trading/logs",
    f"{API}/real-trading/risk-status",
    f"{API}/real-trading/runtime-config",
    f"{API}/real-trading/account",
    f"{API}/real-trading/account/sources",
    f"{API}/real-trading/account/ledger/daily",
    f"{API}/real-trading/manual-executions",
    f"{API}/real-trading/manual-executions/preview",
    f"{API}/real-trading/manual-executions/abc-123/logs",
)


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """默认跑在「实盘关闭」态 —— 这是生产默认值，也是本闸门存在的理由。"""
    monkeypatch.setenv(gate.ENV_KEY, "false")


# ---------------------------------------------------------------------------
# 纯函数层：拒绝表本身
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), BLOCKED_CASES)
def test_blocked_when_disabled(method: str, path: str) -> None:
    assert gate.is_blocked(method, path) is True


@pytest.mark.parametrize("path", MARKET_DATA_CASES)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_market_data_never_blocked(method: str, path: str) -> None:
    """行情端点**不分方法**一律放行。

    故意对所有方法断言，而不是只测 GET：`/tdx/config` 是 POST 写的，
    它是行情桥与交易桥共用的地址/token，拦掉会让行情桥也配不了。
    """
    assert gate.is_blocked(method, path) is False


@pytest.mark.parametrize("path", SHARED_RUNTIME_CASES)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_shared_runtime_not_prefix_blocked(method: str, path: str) -> None:
    """`/real-trading/*` 是双模式共用运行台，中间件**不得**按前缀拦。

    这是第一版闸门的回归：当时拒绝表里有 `f"{API}/real-trading"`，
    模拟盘部署调 `/preflight` 与 `/start` 会直接 403。
    """
    assert gate.is_blocked(method, path) is False


@pytest.mark.parametrize(
    "path",
    [
        f"{API}/real-trading/account/settings",  # GET 放行（只读）
        f"{API}/risk/status",
        f"{API}/risk/config",  # 风控对模拟盘同样生效
    ],
)
def test_adjacent_reads_allowed(path: str) -> None:
    assert gate.is_blocked("GET", path) is False


@pytest.mark.parametrize(
    "path",
    [f"{API}/orders-x", f"{API}/qmt-mirroring", f"{API}/broker-config-status-x"],
)
def test_prefix_boundary_is_slash(path: str) -> None:
    """前缀边界必须落在 `/` 上，不能吞掉同前缀开头的别的端点。"""
    assert gate.is_blocked("GET", path) is False


@pytest.mark.parametrize("path", [f"{API}/orders", f"{API}/qmt-mirror/status"])
def test_trailing_slash_normalized(path: str) -> None:
    """带尾斜杠与不带必须同判，否则前端拼串差异就是一条旁路。"""
    assert gate.is_blocked("GET", path + "/") is True


def test_enabled_flag_makes_is_blocked_irrelevant(monkeypatch) -> None:
    """开关打开后，闸门整体失效 —— 恢复实盘不改一行代码，这是可逆性的证明。

    ⚠️ 注意断言的是**中间件放行**而不是 ``is_blocked() is False``：
    ``is_blocked`` 是规则表，纯函数、**不看 env**（这是有意的分层——规则可以单独
    审计与单测，不必为了测它去动环境变量）。env 的判定在
    ``install_live_trading_gate_middleware`` 的第一行。所以开关打开时
    ``is_blocked`` 照样返回 True，只是没人问它了。
    """
    monkeypatch.setenv(gate.ENV_KEY, "true")
    assert gate.is_real_trading_enabled() is True
    # 规则表原样不变（不是「清空了规则」，是「不看规则」）
    assert gate.is_blocked("POST", f"{API}/orders") is True
    # 但每一个被拒路径都真的通得过
    client = TestClient(_build_app())
    for method, path in BLOCKED_CASES:
        assert client.request(method, path).status_code == 200, f"{method} {path}"


# ---------------------------------------------------------------------------
# 模式判定：双模式共用端点靠它
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["SIMULATION", "simulation", " sim ", "sim", "paper", "模拟盘", "模拟"]
)
def test_simulation_modes_allowed(raw: str) -> None:
    gate.ensure_real_trading_allowed(raw)  # 不抛即通过


@pytest.mark.parametrize(
    "raw",
    [
        "REAL",
        "real",
        " Real ",
        "SHADOW",  # 不下真单但要拉 k8s runner，同属实盘运维面
        "LIVE",  # 认不出的写法一律实盘侧（fail-closed）
        "",
        None,
        "simulation-x",  # 不是白名单里的整词，不能靠前缀蒙混
    ],
)
def test_real_side_modes_rejected(raw: object) -> None:
    with pytest.raises(HTTPException) as exc:
        gate.ensure_real_trading_allowed(raw)
    assert exc.value.status_code == 403
    assert exc.value.detail == gate.DISABLED_DETAIL


@pytest.mark.parametrize("raw", ["REAL", "SHADOW", "", None])
def test_mode_guard_inert_when_enabled(monkeypatch, raw: object) -> None:
    monkeypatch.setenv(gate.ENV_KEY, "true")
    gate.ensure_real_trading_allowed(raw)


# ---------------------------------------------------------------------------
# ASGI 层：中间件真的挂上去了，403 的包体真的长这样
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """最小应用：真实中间件 + catch-all 桩路由。

    桩路由的作用是把「放行」变成一个可断言的状态码（200），
    而不是「没有异常」——后者分不清放行与中间件根本没生效。
    """
    app = FastAPI()

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def _echo(full_path: str):  # noqa: ARG001 - 桩，路径无关
        return {"ok": True, "reached": True}

    gate.install_live_trading_gate_middleware(app, service_name="test")
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app())


@pytest.mark.parametrize(("method", "path"), BLOCKED_CASES)
def test_middleware_returns_403_envelope(
    client: TestClient, method: str, path: str
) -> None:
    resp = client.request(method, path)
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"] == gate.DISABLED_DETAIL
    assert body["success"] is False
    assert body["error"]["code"] == gate.DISABLED_DETAIL
    # 人读的那句话必须同时点名后端 env 与前端构建变量——只开一半是最常见的误配
    assert gate.ENV_KEY in body["message"]
    assert "VITE_ENABLE_REAL_TRADING" in body["message"]


@pytest.mark.parametrize("path", MARKET_DATA_CASES)
def test_middleware_lets_market_data_through(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json()["reached"] is True


@pytest.mark.parametrize("path", SHARED_RUNTIME_CASES)
def test_middleware_lets_shared_runtime_through(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_middleware_respects_root_path() -> None:
    """反代前缀下判定用去掉 root_path 的路径。

    不这么做的话，挂在 `/qm` 下的部署会把 `/qm/api/v1/orders` 当成未知路径放行，
    闸门静默失效。
    """
    app = _build_app()
    client = TestClient(app, root_path="/qm")
    assert client.post(f"{API}/orders").status_code == 403
