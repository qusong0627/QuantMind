"""兜底不变量：**实盘单不得被纸面成交**（钉在引擎取券商处）。

缺陷形态（2026-09-21 核实）：`TradingEngine._get_broker` 在
`settings.ENABLE_REAL_TRADING=False` 时，**不论订单模式**都回落
`create_broker(enable_real=False)` → `PaperTradingBroker`。于是「一笔 REAL 单 +
实盘关闭」不是报错，而是：落一行真 `orders`、过一次风控、拿一个纸面成交、
返回 `success=True` —— 调用方以为自己下了真单。

各条已枚举的入口今天都有闸：
  * HTTP 中间件（`/api/v1/orders` 等前缀）
  * `internal_strategy_dispatcher`（内部策略派发与**止损执行器**共用，3d410ab7）
  * `/manual-executions`、`/real-trading/*` 的 handler 级 `ensure_real_trading_allowed`
  * `ENABLE_REAL_TRADING` 的两种读法已收敛（a9da6780）

**但这些闸全在调用方**，而「取券商」这个决定点是共用的、且它自己的行为是静默的。
于是不变量钉在决定点上：REAL/SHADOW 模式下**取不到真券商就抛**，绝不回落纸面。
这样任何新增调用方（或今天还没被枚举到的后台循环）都不会重演——闸可以漏，
兜底不会。

同时也钉住**不误伤**：SIMULATION 在实盘关闭时照旧走纸面撮合（模拟盘的全部
行为都建立在这条上）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.live_trading.services import trading_engine as te
from backend.services.live_trading.services.broker_client import (
    PaperTradingBroker,
    create_broker,
)
from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.trade_shared.models.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from backend.services.trade_shared.trade_config import settings


def _engine(*, real_enabled: bool, redis=None) -> TradingEngine:
    """按开关态造引擎。

    `ENABLE_REAL_TRADING` 是 pydantic 单例（import 期读 env），改 env 对已导入的
    进程无效，故直接改单例属性 —— 生产里也是同一个单例被 `_get_broker` 读。
    """
    settings.ENABLE_REAL_TRADING = real_enabled  # type: ignore[misc]
    return TradingEngine(db=MagicMock(), redis=redis)


@pytest.fixture(autouse=True)
def _restore_flag():
    original = settings.ENABLE_REAL_TRADING
    yield
    settings.ENABLE_REAL_TRADING = original  # type: ignore[misc]


def _order(**over) -> SimpleNamespace:
    base = {
        "order_id": "11111111-2222-3333-4444-555555555555",
        "user_id": "1",
        "tenant_id": "default",
        "client_order_id": "cid-1",
        "symbol": "SH600036",
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": 100,
        "price": 10.5,
        "trading_mode": TradingMode.REAL,
        "status": OrderStatus.PENDING,
    }
    base.update(over)
    return SimpleNamespace(**base)


# ── 1. 取券商：REAL/SHADOW 无真券商时抛，不回落纸面 ───────────────────


@pytest.mark.parametrize("mode", [TradingMode.REAL, TradingMode.SHADOW])
@pytest.mark.unit
def test_real_side_mode_with_flag_off_raises_instead_of_paper(mode) -> None:
    """实盘关闭时，REAL/SHADOW 取券商必须**抛**，不能返回纸面券商。

    SHADOW 同判不是顺手加的：闸门的 `is_real_side_mode` 就把 REAL/SHADOW 归实盘侧
    （「不下真单但要拉 k8s runner，同属实盘运维面」），两处口径必须一致——
    否则「影子」在本函数里是纸面、在闸门里是实盘，又是一份变量两个答案。
    """
    engine = _engine(real_enabled=False, redis=None)

    with pytest.raises(RuntimeError) as exc:
        engine._get_broker(mode, "SH600036")  # noqa: SLF001

    message = str(exc.value)
    assert "实盘" in message, f"错误信息须能自查，实际: {message}"
    assert mode.value in message, f"错误信息须带模式: {message}"


@pytest.mark.unit
def test_simulation_mode_with_flag_off_still_gets_paper() -> None:
    """不误伤：模拟盘在实盘关闭时照旧纸面撮合（模拟盘全部行为建立在这条上）。

    redis 用 MagicMock 而非 None：工厂对纸面券商**要求** redis
    （`if not redis_client: raise ValueError`），传 None 会让本用例红在
    「模拟盘拿不到券商」上，而那与实盘兜底无关。真实调用方也总有 redis。
    """
    engine = _engine(real_enabled=False, redis=MagicMock())

    broker = engine._get_broker(TradingMode.SIMULATION, "SH600036")  # noqa: SLF001

    assert isinstance(broker, PaperTradingBroker)


@pytest.mark.unit
def test_real_side_mode_with_flag_on_is_not_paper() -> None:
    """开关打开时 REAL 拿到的是真券商（这是「恢复实盘」的可逆性）。"""
    engine = _engine(real_enabled=True, redis=None)

    broker = engine._get_broker(TradingMode.REAL, "SH600036")  # noqa: SLF001

    assert not isinstance(broker, PaperTradingBroker)


# ── 2. 工厂层：enable_real=True 不得静默给纸面 ───────────────────────


@pytest.mark.parametrize(
    "broker_type",
    [
        "bridge",
        "redis",
        "qmt",
        "tdx",
        "qmt_exec",
        "tiger",
        "futu",
        "ib",
        "not-a-broker",
    ],
)
@pytest.mark.unit
def test_create_broker_real_never_returns_paper(broker_type: str) -> None:
    """`create_broker(enable_real=True, ...)` 对**任何** broker_type 都不得回落到纸面。

    两类合法结局：造出真券商，或抛（未知类型）。**不合法的是第三种** ——
    悄悄给一个 `PaperTradingBroker`。海外券商/桥的构造可能需要 SDK 或凭据而抛，
    这没关系：抛是响的，降级是哑的。

    不断言「必须造出实例」：那要求本用例连上券商 SDK，且把「构造失败」误判成缺陷。
    不变量只有一条 —— 永远不是纸面。
    """
    try:
        broker = create_broker(enable_real=True, broker_type=broker_type)
    except Exception as exc:  # noqa: BLE001 - 抛是允许的结局
        assert not isinstance(exc, AssertionError)
        return

    assert not isinstance(broker, PaperTradingBroker), (
        f"broker_type={broker_type!r} 在 enable_real=True 下给回了纸面券商"
    )


# ── 3. 端到端：submit_order 如实上报，且**纸面券商从未被构造** ────────


@pytest.mark.unit
def test_submit_order_reports_failure_and_never_constructs_paper() -> None:
    """REAL 单 + 实盘关闭：如实返回失败、留拒单备注，且不构造纸面券商。

    直接钉「纸面券商没被构造」而不是「返回 success=False」：后者只说明这次
    没成交，前者说明**没有第二条悄悄成交的路**。用 spy 包住工厂，非纸面调用
    照常透传（本用例里不会发生）。
    """
    engine = _engine(real_enabled=False, redis=None)
    engine.order_service.transition_order_status = AsyncMock()  # type: ignore[method-assign]

    calls: list[dict] = []
    real_factory = te.create_broker

    def _spy(**kwargs):
        calls.append(kwargs)
        return real_factory(**kwargs)

    order = _order()
    with patch.object(te, "create_broker", _spy):
        result = __import__("asyncio").run(
            engine.submit_order(order, tenant_id="default")
        )

    assert result["success"] is False, "实盘关闭时 REAL 单不得上报成功"
    # 工厂一次都不该被惊动（无论 enable_real 取值）——本用例里 `calls` 为空是
    # **结论**而不是空转：真正的判据在下面那条「拒因带实盘字样」，它证明引擎
    # 是**在取券商时**就拒了，而不是绕去别处成交。
    assert calls == [], f"实盘关闭时仍去构造券商: {calls}"
    rejected = [
        c
        for c in engine.order_service.transition_order_status.await_args_list
        if len(c.args) >= 2 and c.args[1] == OrderStatus.REJECTED
    ]
    assert rejected, "拒单未留痕（无 REJECTED 状态迁移）"
    assert "实盘" in str(rejected[-1].kwargs.get("remarks", "")), (
        f"拒因须可自查，实际: {rejected[-1].kwargs.get('remarks')}"
    )
