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


# ── 单一读取实现：两个读者必须对同一原始值一致 ──────────────────────────

# 真实运维会写出来的形态：正常、大小写、前后空白/制表符、CRLF 残留、空串，
# 以及**不得**被当成 true 的写法（1/yes/on）。
_RAW_FLAG_VALUES = [
    "true",
    "TRUE",
    "True",
    " true ",
    "\ttrue",
    "true\r",
    "true\n",
    "false",
    "FALSE",
    "false ",
    "",
    " ",
    "0",
    "1",
    "yes",
    "on",
    "no",
]

# 只有这些（strip + 小写后）算 true。其余一律 false —— 词表放宽 = 更多写法能打开实盘。
_TRUE_AFTER_NORMALIZE = {"true"}


@pytest.mark.parametrize("raw", _RAW_FLAG_VALUES)
def test_real_trading_flag_readers_agree(monkeypatch, raw: str) -> None:
    """`ENABLE_REAL_TRADING` 的两个读者，对同一个原始值必须给出一致判定。

    两者读同一个变量：`shared/live_trading_gate.is_real_trading_enabled()`（端点咽喉）
    与 `trade_shared/trade_config.settings.ENABLE_REAL_TRADING`（引擎据此选券商，
    进而决定 `create_broker(enable_real=...)`）。本模块 docstring 把这写成
    「读的是同一个环境变量」，但**两份读取实现**已经在 `strip` 上分叉：
    闸门是 `.strip().lower()`，settings 只有 `.lower()`（实测：`" true "` → 闸门 True、
    settings False）。后果不是「少个功能」而是**静默降级**：闸门放行 REAL 请求，
    `_get_broker` 却因 settings 判关回落到 `PaperTradingBroker`，于是一笔实盘单
    被纸面成交并返回 `success=True` —— 与 `internal_strategy_dispatcher` 修过的
    事故同形（那次是 REAL 走 else 分支，这次是同名变量的两种读法）。

    这类「两个读者、一份变量」的分叉没有症状，只能靠**对同一批输入断言一致**来防。
    """
    monkeypatch.setenv(gate.ENV_KEY, raw)

    from backend.services.trade_shared.trade_config import Settings

    gate_says = gate.is_real_trading_enabled()
    engine_says = Settings().ENABLE_REAL_TRADING

    assert gate_says == engine_says, (
        f"两个读者对 {gate.ENV_KEY}={raw!r} 判定不一致："
        f"闸门={gate_says} 引擎={engine_says}"
    )


@pytest.mark.parametrize("raw", _RAW_FLAG_VALUES)
def test_real_trading_flag_vocabulary_is_true_only(monkeypatch, raw: str) -> None:
    """词表**只有 `true`**：`1`/`yes`/`on` 不得打开实盘。

    这是默认关闭的合规闸门，接受词越宽 = 越容易误开。若哪天有人「顺手」把词表
    扩成 `{"1","true","yes","on"}`，本用例会红 —— 那是要人复核的决定，不是重构。
    """
    monkeypatch.setenv(gate.ENV_KEY, raw)

    expected = raw.strip().lower() in _TRUE_AFTER_NORMALIZE
    assert gate.is_real_trading_enabled() is expected, (
        f"{gate.ENV_KEY}={raw!r} 的判定与词表不符（应 {expected}）"
    )


def test_real_trading_flag_readers_agree_when_unset(monkeypatch) -> None:
    """变量缺席时两侧都判关（默认关闭是合规底线）。

    传 `_env_file=None` 关掉 `.env` 文件源：本项目还有**第三种结构差异** ——
    闸门只读进程环境（`os.getenv`），settings 额外读 `.env`；`.env` 存在但没被
    export 时，闸门判关、settings 判开。这个方向的差**是安全的**（中间件 403 在前，
    不会出现「放行但纸面成交」），且让闸门读 `.env` 就得把 stdlib 的它绑上 dotenv，
    故**有意不修**，只记录。本用例钉的是「两边读同一份输入时答案必须一致」，
    所以把文件源关掉，只留进程环境这一份。
    """
    monkeypatch.delenv(gate.ENV_KEY, raising=False)

    from backend.services.trade_shared.trade_config import Settings

    assert gate.is_real_trading_enabled() is False
    assert Settings(_env_file=None).ENABLE_REAL_TRADING is False


def test_no_second_env_reader_of_real_trading_flag() -> None:
    """源断言：全仓不得再有第二处**直接读 env** 的 `ENABLE_REAL_TRADING`。

    上面的对拍用例能证明「今天这两处一致」，证明不了「明天不会多出第三处」——
    而分叉的成因恰恰是「再写一份也无处可挡」。故把纪律钉在源上（同
    `test_rule_parity.test_source_single_implementation` 的做法）：判实盘开关
    只能走 `shared/env_flags`，读 `trade_config.settings` 的消费者不受影响
    （`trading_engine` 走 `getattr(settings, ...)`、`real_mirror_service` 走闸门谓词）。
    """
    import re
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    # 直接读 env 的各种写法；`ENV_KEY = "ENABLE_REAL_TRADING"` 这种常量声明不算。
    pattern = re.compile(
        r"""os\.getenv\(\s*["']ENABLE_REAL_TRADING["']"""
        r"""|os\.environ(?:\.get)?[\(\[]\s*["']ENABLE_REAL_TRADING["']"""
    )
    offenders = []
    for path in backend.rglob("*.py"):
        if path.name == "env_flags.py":
            continue  # 唯一实现本身
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(str(path.relative_to(backend)))

    assert not offenders, (
        "实盘开关只能经 shared.env_flags 读，禁止就地 os.getenv："
        f"{offenders}（同名变量两种读法已实测分叉过，见 env_flags 模块 docstring）"
    )
