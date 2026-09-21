"""实盘交易闸门（`ENABLE_REAL_TRADING`）：单一咽喉点，默认关闭。

为什么是两层，而不是一层
------------------------
实盘面在代码里有两种形态，用同一种手法拦不住：

1. **专有端点** —— 整个 router 只服务实盘（`/api/v1/orders`、`/qmt-mirror/*`、
   `/broker-config/*`、`/tdx/push-signals` …）。这类由**中间件**按 (方法, 路径)
   判定，新加的路由默认落在拒绝侧，漏挂依赖也不会漏拦。

2. **双模式共用端点** —— 同一个 URL 的 REAL 与 SIMULATION 走同一段代码，
   模式在 **query / body** 里（`/api/v1/real-trading/preflight`、`/trading-precheck`、
   `/start`、`/manual-executions`）。中间件读不到 body（读了会耗尽请求流），
   按前缀整段拦又会把**模拟盘启动链路**一起打死。
   这类由 `ensure_real_trading_allowed()` 在 handler 内按模式判定。

   ⚠️ 正因如此，`_BLOCKED_PREFIXES` 里**没有** `f"{_API}/real-trading"`。
   那个 router 名字叫 real-trading，实际是**两种模式共用的策略运行台**：
   模拟盘部署时前端一样要调 `/preflight`、`/trading-precheck`、`/start`、
   `/manual-executions`。按前缀拦掉它 = 模拟盘点「启动」直接 403。

只拦交易，绝不拦行情
--------------------
用户明确要求实时行情保留。`/api/v1/tdx/*` 同一前缀下混着行情与交易
（`/tdx/quote-feed` 与 `/tdx/push-signals` 是两回事），所以判定必须落到**端点**
而不是前缀——按前缀拦会把盘中行情主源（TDX 桥热集行情、`market:snapshot/series`
的写侧）一起打死。行情端点在 `_MARKET_DATA_PREFIXES` 里逐条列出并**优先于拒绝表**
判定，`backend/tests/test_live_trading_gate.py` 有对应的「不许误杀」断言。

与 `ENABLE_REAL_TRADING` 的关系
--------------------------------
与 `backend/services/trade_shared/trade_config.py::ENABLE_REAL_TRADING` 读的是
**同一个环境变量**（同 `ENABLE_CRYPTO` 在 `quantbc_hub` 与 `market_adapters` 各有
一份读取实现的既成做法）。前端另有构建期开关 `VITE_ENABLE_REAL_TRADING`
（`electron/src/config/tradingFlags.ts`）管「渲染不渲染」；本模块管「端点通不通」。
恢复实盘必须两端同时打开。
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

ENV_KEY = "ENABLE_REAL_TRADING"

#: 闸门拒绝时返回的机器可读原因（前端/探针据此断言，不要改字面量）
DISABLED_DETAIL = "real_trading_disabled"

_API = "/api/v1"

# ---------------------------------------------------------------------------
# 行情面：无条件放行（优先于下面所有拒绝规则判定）
# ---------------------------------------------------------------------------

#: 行情/数据面端点前缀。**盘中行情主源，误杀会直接断数据。**
#: - `/tdx/quote-*`：TDX 桥热集行情轮询与逐笔
#: - `/tdx/l2*`：L2 行情与 L2 通道配置（行情侧，非交易侧）
#: - `/tdx/config`：桥地址/token——行情通道与交易通道共用同一座桥，
#:   拦住它会让行情桥也配不了
_MARKET_DATA_PREFIXES: tuple[str, ...] = (
    f"{_API}/tdx/config",
    f"{_API}/tdx/overview",
    f"{_API}/tdx/quote-feed",
    f"{_API}/tdx/quote-tick",
    f"{_API}/tdx/quote-ticks",
    f"{_API}/tdx/l2",
    f"{_API}/tdx/l2-config",
    f"{_API}/market",
    f"{_API}/ws",
)

# ---------------------------------------------------------------------------
# 拒绝表（实盘关闭时）：专有端点，不分方法一律拒绝
# ---------------------------------------------------------------------------

_BLOCKED_PREFIXES: tuple[str, ...] = (
    # 实盘委托。该 router 直接依赖 live_trading.services.trading_engine；
    # 模拟盘委托走的是另一条路径 `/api/v1/simulation/orders`，不受影响。
    f"{_API}/orders",
    # 大 QMT 真单镜像与止损止盈
    f"{_API}/qmt-mirror",
    f"{_API}/qmt-sltp",
    # 券商接入：含凭证面。`/broker-config-status` 是独立路径，前缀匹配够不着，
    # 必须单列（`_matches` 的边界落在 `/` 上，不会把 `-status` 一起吞掉）
    f"{_API}/broker-config",
    f"{_API}/broker-config-status",
    # 信号下发券商 / 自动交易配置
    f"{_API}/tdx/push-signals",
    f"{_API}/tdx/rolling-signals",
    f"{_API}/tdx/rolling-config",
    f"{_API}/tdx/sltp-config",
    # 券商侧委托查询与撤单
    f"{_API}/tdx/orders",
    f"{_API}/tdx/inflight",
)

#: 精确 (方法, 路径) 拒绝。用于按前缀拦会误伤、但确实只服务实盘的端点。
#: 这几条在前端**没有任何调用方**（`grep -rn` 已核），拦掉不会伤模拟盘。
_BLOCKED_EXACT: frozenset[tuple[str, str]] = frozenset(
    {
        # 实盘券商委托流水与历史（模拟盘的成交查询走 `/api/v1/simulation/orders`
        # 与 `/api/v1/trades`，与本条无关）
        ("GET", f"{_API}/real-trading/orders"),
        ("GET", f"{_API}/real-trading/history"),
        # 实盘账户初始资金等参数，改了直接影响真钱账本
        ("PUT", f"{_API}/real-trading/account/settings"),
        # 实盘撤单。`GET /risk/status` 与 `POST /risk/config` **放行**——
        # 风控对模拟盘同样生效，且配置面属于页面骨架，拦掉只会让界面报错，
        # 不会多挡住任何一笔真单
        ("POST", f"{_API}/risk/cancel-all"),
    }
)

# ---------------------------------------------------------------------------
# 模式判定（双模式共用端点用）
# ---------------------------------------------------------------------------

#: **认得出**的模拟盘写法。除此之外一律算实盘侧。
_SIMULATION_TOKENS = frozenset(
    {"simulation", "sim", "paper", "模拟", "模拟盘", "虚拟盘"}
)


def is_real_trading_enabled() -> bool:
    """实盘是否启用。**调用时读 env**（不是 import 时冻结）——
    测试要能 monkeypatch，运维改 env 重启即生效，没有中间态。"""
    return os.getenv(ENV_KEY, "false").strip().lower() == "true"


def _normalize(path: str) -> str:
    """去尾斜杠。`/api/v1/orders` 与 `/api/v1/orders/` 必须同判。"""
    return path.rstrip("/") or "/"


def _matches(path: str, prefix: str) -> bool:
    """前缀匹配，但边界必须落在 `/` 上：`/api/v1/orders` 不该匹配 `/api/v1/orders-x`。"""
    return path == prefix or path.startswith(prefix + "/")


def is_blocked(method: str, path: str) -> bool:
    """该请求在实盘关闭时是否应被拒绝。纯函数，便于单测与审计。"""
    method = method.upper()
    path = _normalize(path)

    # 1) 行情先行：无论什么方法，行情端点一律放行
    if any(_matches(path, p) for p in _MARKET_DATA_PREFIXES):
        return False

    # 2) 精确拒绝
    if (method, path) in _BLOCKED_EXACT:
        return True

    # 3) 整段拒绝
    return any(_matches(path, p) for p in _BLOCKED_PREFIXES)


def is_real_side_mode(raw: object) -> bool:
    """该交易模式是否属于「实盘侧」（REAL / SHADOW / 无法识别的写法）。

    **只认得出模拟盘的写法才算模拟盘**，其余一律算实盘侧——与中间件「新路由默认
    落在拒绝侧」同一个失败方向。

    ⚠️ 这与前端 `normalizeTradingMode` 的归一方向**正好相反**，是有意的：
    前端未知判模拟，为的是「宁可少认一个实盘」（别把模拟部署显示成实盘）；
    这里未知判实盘，为的是「宁可多拦一个」（别让没见过的写法绕过闸门）。
    两者各守一端，都是安全的失败方向。

    SHADOW 归入实盘侧：它虽不下真单，但同样要拉起 k8s runner 容器
    （见 `real_trading_preflight.preflight_check` 的依赖说明），属于实盘运维面。
    """
    return str(raw or "").strip().lower() not in _SIMULATION_TOKENS


def ensure_real_trading_allowed(trading_mode: object) -> None:
    """实盘关闭时，显式请求实盘侧模式的调用一律 403。

    在 handler 里调（不是中间件）——模式在 query/body 里，中间件看不到。
    调用点见 `real_trading_lifecycle.start_trading`、
    `real_trading_preflight.{preflight_check,trading_precheck}`、
    `manual_executions.{preview_manual_execution,create_manual_execution}`。
    """
    if is_real_trading_enabled() or not is_real_side_mode(trading_mode):
        return
    raise HTTPException(status_code=403, detail=DISABLED_DETAIL)


def install_live_trading_gate_middleware(app: FastAPI, service_name: str) -> None:
    """安装实盘闸门。`ENABLE_REAL_TRADING=false`（默认）时拒绝实盘专有端点。

    拒绝用 403 而非 404：403 说得清「这东西存在但本部署没开」，
    404 会让运维去查路由注册，方向反了。
    """

    @app.middleware("http")
    async def _live_trading_gate(request: Request, call_next):
        if is_real_trading_enabled():
            return await call_next(request)

        path = request.url.path
        # 服务可能挂在 root_path 下（反代前缀），判定用去掉前缀后的路径
        root_path = request.scope.get("root_path") or ""
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]

        if not is_blocked(request.method, path):
            return await call_next(request)

        logger.warning(
            "[LiveTradingGate] 拒绝 %s %s（%s=false，service=%s）",
            request.method,
            request.url.path,
            ENV_KEY,
            service_name,
        )
        return JSONResponse(
            status_code=403,
            content={
                "detail": DISABLED_DETAIL,
                "message": (
                    f"本部署未启用实盘交易（{ENV_KEY}=false）。"
                    "如需启用，请在后端与服务端同时设置该环境变量与前端构建变量 "
                    "VITE_ENABLE_REAL_TRADING=true 后重启。"
                ),
                "success": False,
                "error": {
                    "code": DISABLED_DETAIL,
                    "message": "real trading is disabled on this deployment",
                },
            },
        )
