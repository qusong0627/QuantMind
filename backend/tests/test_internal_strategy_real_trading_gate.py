"""内部策略调度：实盘闸门（REAL 分支必须受 ENABLE_REAL_TRADING 约束）。

背景（2026-09-21 审计）：``dispatch_internal_strategy_order`` 的 REAL 分支从不读
``ENABLE_REAL_TRADING``，而 ``order_data["trading_mode"]`` **缺省即 "REAL"**。
中间件也拦不到这条路径：``internal_strategy.router`` 是挂在**根路径**上的
（``/order``、``/hosted-executions``、``/sync-account``），而 ``_BLOCKED_PREFIXES``
里的条目全是 ``/api/v1/...``，``_matches("/order", "/api/v1/orders")`` 为假。
``/api/v1/orders`` 那道闸门拦的是**另一个**端点。

后果不是「报错」而是**静默降级**：``TradingEngine._get_broker`` 在 enable_real=False 时
走 else 分支回落到 ``PaperTradingBroker``，于是一个 REAL 请求会
① 落一行真单 ``orders``、② 过一次风控、③ 拿一个纸面成交、
④ 返回 ``{"success": true, "status": "FILLED"}`` —— 调用方以为自己下了真单。

契约（与 ``manual_executions`` / ``push_orders`` 同一套既有策略，非新政策）：
实盘侧模式（REAL/SHADOW）+ ``ENABLE_REAL_TRADING=false`` → **403 且副作用为零**；
SIMULATION 照常放行（不许误杀）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from backend.services.live_trading.services import internal_strategy_dispatcher as d
from backend.services.live_trading.services.manual_execution_service import (
    manual_execution_service,
)
from backend.services.trade.routers.internal_strategy_lifecycle import (
    HostedExecutionCreateRequest,
    create_hosted_execution,
    strategy_order,
)
from backend.services.trade_shared.models.enums import TradingMode
from backend.shared.live_trading_gate import (
    DISABLED_DETAIL,
    ENV_KEY,
    is_blocked,
    is_real_side_mode,
)


class FakeResult:
    def __init__(self, value: Any = None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    """记录每次 execute：用来断言「拒绝发生在任何落库动作之前」。"""

    def __init__(self, results: list[Any] | None = None):
        self.statements: list[Any] = []
        self._results = list(results or [])

    async def execute(self, stmt):
        self.statements.append(stmt)
        return FakeResult(self._results.pop(0) if self._results else None)

    def add(self, obj):  # pragma: no cover - 假体空转
        return None

    async def commit(self):  # pragma: no cover - 假体空转
        return None

    async def refresh(self, obj):  # pragma: no cover - 假体空转
        return None


class RecordingOrderService:
    """捕获 create_order 调用——本文件里它必须**一次都不被调用**。"""

    instances: list[RecordingOrderService] = []

    def __init__(self, db, redis):
        self.calls: list[dict[str, Any]] = []
        RecordingOrderService.instances.append(self)

    async def create_order(self, *, user_id, tenant_id, order_data):
        self.calls.append({"user_id": user_id, "order_data": order_data})
        return SimpleNamespace(order_id="should-not-exist")


class RecordingEngine:
    instances: list[RecordingEngine] = []

    def __init__(self, db, redis):
        self.risk_calls: list[Any] = []
        self.submit_calls: list[Any] = []
        RecordingEngine.instances.append(self)

    async def check_order_risk(self, user_id, order):
        self.risk_calls.append(user_id)
        return {"passed": True}

    async def submit_order(self, order, tenant_id=None):
        self.submit_calls.append(order)
        return {"success": True}


@pytest.fixture(autouse=True)
def _reset_recorders():
    RecordingOrderService.instances = []
    RecordingEngine.instances = []
    yield
    RecordingOrderService.instances = []
    RecordingEngine.instances = []


@pytest.fixture
def real_trading_off(monkeypatch):
    """显式清掉 env：不依赖跑测试的 shell 恰好没设过它。"""
    monkeypatch.delenv(ENV_KEY, raising=False)
    yield


@pytest.fixture
def real_trading_on(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "true")
    yield


def _order_data(**overrides) -> dict[str, Any]:
    base = {
        "symbol": "600036.SH",
        "side": "BUY",
        "quantity": 100,
        "price": 41.0,
        "order_type": "LIMIT",
        "client_order_id": "gate-test-1",
    }
    base.update(overrides)
    return base


def _dispatch(order_data: dict[str, Any]):
    db = FakeDb(results=[None, None])
    with (
        patch.object(
            d, "_fetch_active_portfolio_snapshot", AsyncMock(return_value=None)
        ),
        patch.object(d, "OrderService", RecordingOrderService),
        patch.object(d, "TradingEngine", RecordingEngine),
    ):
        return asyncio.run(
            d.dispatch_internal_strategy_order(
                order_data=order_data,
                user_id="1",
                tenant_id="default",
                redis=SimpleNamespace(),
                db=db,
            )
        )


# ---------------------------------------------------------------------------
# 拒绝侧：实盘关闭时，实盘侧模式一律 403 且不留任何痕迹
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["REAL", "real", "SHADOW", "shadow"])
def test_real_side_modes_refused_when_disabled(real_trading_off, mode):
    """REAL/SHADOW + 实盘关闭 → 403，且不建单、不过风控、不提交。"""
    with pytest.raises(HTTPException) as exc:
        _dispatch(_order_data(trading_mode=mode))

    assert exc.value.status_code == 403
    assert exc.value.detail == DISABLED_DETAIL

    # 副作用为零：既没构造 OrderService/TradingEngine，也没写过库
    assert RecordingOrderService.instances == []
    assert RecordingEngine.instances == []


def test_omitted_trading_mode_is_refused_when_disabled(real_trading_off):
    """**缺省即 REAL** —— 这才是生产上的真实触发方式，必须一并被拦。

    调用方不写 trading_mode 时旧行为是直接下真单；本用例锁死它现在会被拒。
    """
    with pytest.raises(HTTPException) as exc:
        _dispatch(_order_data())  # 刻意不带 trading_mode

    assert exc.value.status_code == 403
    assert exc.value.detail == DISABLED_DETAIL
    assert RecordingOrderService.instances == []


def test_refusal_happens_before_any_db_write(real_trading_off):
    """拒绝必须发生在**任何落库动作之前**：orders 表里不能留下半行。"""
    db = FakeDb(results=[None, None])
    with patch.object(
        d, "_fetch_active_portfolio_snapshot", AsyncMock(return_value=None)
    ):
        with pytest.raises(HTTPException):
            asyncio.run(
                d.dispatch_internal_strategy_order(
                    order_data=_order_data(trading_mode="REAL"),
                    user_id="1",
                    tenant_id="default",
                    redis=SimpleNamespace(),
                    db=db,
                )
            )
    assert db.statements == []


# ---------------------------------------------------------------------------
# 放行侧：不许误杀
# ---------------------------------------------------------------------------


def test_simulation_mode_passes_gate_when_disabled(real_trading_off):
    """SIMULATION 是模拟盘链路，实盘关闭时**必须照常**走到虚拟成交。"""
    from backend.services.simulation.services.order_submission_service import (
        SimulationSubmissionOutcome,
    )

    captured: dict[str, Any] = {}

    async def _run():
        db = FakeDb(results=[None])
        submission = SimpleNamespace(
            submit_and_fill=AsyncMock(
                return_value=SimulationSubmissionOutcome(
                    success=True,
                    order_id="ord-sim-1",
                    fill_price=10.5,
                    filled_quantity=100,
                    commission=1.2,
                    price_source="quote",
                    message="filled",
                )
            )
        )
        with (
            patch(
                "backend.services.simulation.services.order_submission_service."
                "SimulationOrderSubmissionService",
                return_value=submission,
            ),
            patch.object(d, "mirror_virtual_fill", new_callable=AsyncMock),
        ):
            captured["result"] = await d.dispatch_internal_strategy_order(
                order_data=_order_data(trading_mode="SIMULATION"),
                user_id="1",
                tenant_id="default",
                redis=SimpleNamespace(),
                db=db,
            )

    asyncio.run(_run())
    assert captured["result"]["execution"] == "virtual"


def test_real_mode_allowed_when_enabled(real_trading_on):
    """实盘启用时 REAL 分支照常——闸门不能把正当用法一起拦掉。"""
    result = _dispatch(_order_data(trading_mode="REAL"))

    assert result["status"] == "success"
    assert result["execution"] == "direct"
    assert len(RecordingOrderService.instances) == 1
    assert len(RecordingOrderService.instances[0].calls) == 1


def test_gate_compares_plain_string_not_enum_repr():
    """``TradingMode`` 是 ``(str, Enum)`` 混入：``str(成员)`` 得到 ``"TradingMode.SIMULATION"``
    而不是 ``"simulation"``，且 ``isinstance(成员, str)`` 恒真、看不出任何毛病。

    于是 ``is_real_side_mode`` 会把枚举当成**无法识别的写法**判到实盘侧，
    把整个模拟盘一起 403 —— 调度器里必须传 ``.value``。

    这条断言锁住那个反直觉的事实本身（它是 ``.value`` 存在的唯一理由），
    免得后人「顺手」把它改回传枚举。
    """
    assert str(TradingMode.SIMULATION) == "TradingMode.SIMULATION"
    assert TradingMode.SIMULATION.value == "SIMULATION"

    assert is_real_side_mode(TradingMode.SIMULATION) is True  # 传枚举 → 误判为实盘侧
    assert is_real_side_mode(TradingMode.SIMULATION.value) is False  # 传 .value → 正确


# ---------------------------------------------------------------------------
# /hosted-executions：任务记录落库在真正下单之前，闸门必须早于它
# ---------------------------------------------------------------------------


def test_hosted_execution_refused_before_task_is_created(real_trading_off):
    """默认 trading_mode="REAL" 的托管任务在实盘关闭时必须被拒，
    且**不能先落一条任务记录**（否则留下永远下不出单的孤儿任务）。"""
    payload = HostedExecutionCreateRequest(strategy_id="7", run_id="r1")

    create = AsyncMock(return_value={"task_id": "t1"})
    with patch.object(manual_execution_service, "create_hosted_task", create):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                create_hosted_execution(
                    payload=payload, x_user_id="1", x_tenant_id="default"
                )
            )

    assert exc.value.status_code == 403
    assert exc.value.detail == DISABLED_DETAIL
    create.assert_not_awaited()


def test_hosted_execution_simulation_passes_gate(real_trading_off):
    """托管任务的 SIMULATION 模式照常（不许误杀）。"""
    payload = HostedExecutionCreateRequest(
        strategy_id="7", run_id="r1", trading_mode="SIMULATION"
    )

    create = AsyncMock(return_value={"task_id": "t1"})
    with patch.object(manual_execution_service, "create_hosted_task", create):
        result = asyncio.run(
            create_hosted_execution(
                payload=payload, x_user_id="1", x_tenant_id="default"
            )
        )

    create.assert_awaited_once()
    assert result["task_id"] == "t1"


# ---------------------------------------------------------------------------
# 路由级接线：走真实的 /order handler（不是直调调度器）
# ---------------------------------------------------------------------------


def test_order_route_refuses_when_disabled(real_trading_off):
    """``POST /order`` 的真实 handler 也必须拒——证明闸门挂在**路由实际走的**那条链上，
    而不只是调度器被直调时才生效。"""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            strategy_order(
                order_data=_order_data(trading_mode="REAL"),
                x_user_id="1",
                x_tenant_id="default",
                redis=SimpleNamespace(),
                db=FakeDb(),
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == DISABLED_DETAIL


def test_middleware_cannot_cover_this_endpoint():
    """**架构约束的钉子**：中间件挡不住 ``/order``，所以闸门只能放在调度器里。

    ``internal_strategy.router`` 挂在根路径，而 ``_BLOCKED_PREFIXES`` 全是
    ``/api/v1/...`` —— ``is_blocked("POST", "/order")`` 恒为 False。

    这条不是「记录漏洞」，而是钉住**为什么不能靠中间件**：``/order`` 是双模式共用端点
    （SIMULATION 也从这里进），中间件按路径拦是模式盲的，加进拒绝表就会误杀模拟盘；
    而模式在 body 里，中间件读不到。所以正确位置是调度器里按模式判。
    若后人想「顺手把它加进 _BLOCKED_PREFIXES」，这条会红。
    """
    assert is_blocked("POST", "/order") is False
    assert is_blocked("POST", "/hosted-executions") is False
    # 同时确认中间件**确实**在保护那个名字很像的端点，别把两者搞混
    assert is_blocked("POST", "/api/v1/orders") is True


def test_simulation_mode_unaffected_when_enabled(real_trading_on):
    """实盘启用时 SIMULATION 也不受影响（闸门只作用于实盘侧）。"""
    from backend.services.simulation.services.order_submission_service import (
        SimulationSubmissionOutcome,
    )

    async def _run():
        db = FakeDb(results=[None])
        submission = SimpleNamespace(
            submit_and_fill=AsyncMock(
                return_value=SimulationSubmissionOutcome(
                    success=True,
                    order_id="ord-sim-2",
                    fill_price=10.5,
                    filled_quantity=100,
                    commission=1.2,
                    price_source="quote",
                    message="filled",
                )
            )
        )
        with patch(
            "backend.services.simulation.services.order_submission_service."
            "SimulationOrderSubmissionService",
            return_value=submission,
        ):
            return await d.dispatch_internal_strategy_order(
                order_data=_order_data(trading_mode="SIMULATION"),
                user_id="1",
                tenant_id="default",
                redis=SimpleNamespace(),
                db=db,
            )

    assert asyncio.run(_run())["execution"] == "virtual"
