"""模拟盘撮合拒单测试：涨跌停、停牌、无行情、T+1 可卖量、费用。

适配当前 SimulationExecutionEngine 接口：行情经 `_latest_price` 返回
`MarketSnapshot`，成交量/资金经 `SimulationAccountManager.update_balance`
（这里用 mock 模拟，含可卖量不足的失败分支）。错误以文本消息返回。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.simulation.models.order import OrderSide, OrderType, SimOrder
from backend.services.simulation.services.execution_engine import (
    MarketSnapshot,
    SimulationExecutionEngine,
)


class _FakeDb:
    async def execute(self, *args, **kwargs):
        raise AssertionError("db.execute should not be called in these tests")


def _manager(balance_result: dict | None = None) -> SimpleNamespace:
    """账户替身：默认成交成功；传入 balance_result 可模拟资金/可卖量不足。"""
    m = SimpleNamespace()
    m.update_balance = AsyncMock(
        return_value={"success": True} if balance_result is None else balance_result
    )
    m.get_account = AsyncMock(return_value={"cash": 1_000_000.0})
    m.redis = None
    return m


def _snapshot(
    *,
    price: float = 10.0,
    source: str = "market_data_service",
    limit_up: bool = False,
    limit_down: bool = False,
    suspended: bool = False,
    recent_volume: float | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        price=price,
        price_source=source,
        limit_up=limit_up,
        limit_down=limit_down,
        suspended=suspended,
        recent_volume=recent_volume,
    )


def _engine(
    snapshot: MarketSnapshot | None,
    manager: SimpleNamespace | None = None,
) -> SimulationExecutionEngine:
    mgr = manager or _manager()
    eng = SimulationExecutionEngine(db=_FakeDb(), manager=mgr)
    eng._latest_price = AsyncMock(return_value=snapshot)
    return eng, mgr


def _make_order(side: OrderSide, quantity: float = 100.0) -> SimOrder:
    return SimOrder(
        tenant_id="default",
        user_id=1001,
        portfolio_id=0,
        symbol="SH600000",
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
    )


@pytest.mark.asyncio
async def test_execute_order_blocks_buy_when_limit_up():
    engine, manager = _engine(_snapshot(price=11.0, limit_up=True))

    result = await engine.execute_order(_make_order(OrderSide.BUY))

    assert result.success is False
    assert "Limit-up locked" in result.message
    assert manager.update_balance.called is False


@pytest.mark.asyncio
async def test_execute_order_blocks_sell_when_limit_down():
    engine, manager = _engine(_snapshot(price=9.0, limit_down=True))

    result = await engine.execute_order(_make_order(OrderSide.SELL))

    assert result.success is False
    assert "Limit-down locked" in result.message
    assert manager.update_balance.called is False


@pytest.mark.asyncio
async def test_execute_order_blocks_when_suspended():
    engine, manager = _engine(_snapshot(suspended=True))

    result = await engine.execute_order(_make_order(OrderSide.BUY))

    assert result.success is False
    assert "suspended" in result.message.lower()
    assert manager.update_balance.called is False


@pytest.mark.asyncio
async def test_execute_order_blocks_when_no_market_data():
    """无行情（实时+DB 兜底都失败）必须拒单，绝不虚构价格成交。"""
    engine, manager = _engine(_snapshot(price=0.0, source="unavailable"))

    result = await engine.execute_order(_make_order(OrderSide.BUY))

    assert result.success is False
    assert "无法获取" in result.message
    assert manager.update_balance.called is False


@pytest.mark.asyncio
async def test_execute_order_blocks_sell_exceeding_available_volume():
    """T+1：可卖量不足时，账户层拒绝，撮合不落地成交。"""
    engine, manager = _engine(
        _snapshot(),
        _manager(balance_result={"success": False, "reason": "INSUFFICIENT_AVAILABLE_VOLUME"}),
    )

    result = await engine.execute_order(_make_order(OrderSide.SELL, quantity=300.0))

    assert result.success is False
    assert "INSUFFICIENT_AVAILABLE_VOLUME" in result.message
    assert manager.update_balance.called is True


@pytest.mark.asyncio
async def test_execute_order_fills_buy_and_charges_min_commission_no_stamp():
    engine, manager = _engine(_snapshot(price=10.0))

    result = await engine.execute_order(_make_order(OrderSide.BUY))

    assert result.success is True
    assert result.quantity == 100
    assert result.commission >= 5.0  # 最低佣金（默认 5 元）
    assert result.stamp_duty == 0.0  # 买入不收印花税
    assert result.price == pytest.approx(10.0, abs=0.01)
    assert manager.update_balance.called is True
    _, kwargs = manager.update_balance.call_args
    assert kwargs["delta_volume"] == 100
    assert kwargs["delta_cash"] < 0


@pytest.mark.asyncio
async def test_execute_order_fills_sell_and_charges_stamp_duty():
    engine, manager = _engine(_snapshot(price=10.0))

    result = await engine.execute_order(_make_order(OrderSide.SELL))

    assert result.success is True
    assert result.stamp_duty > 0  # A 股卖出征收印花税
    assert manager.update_balance.called is True
    _, kwargs = manager.update_balance.call_args
    assert kwargs["delta_volume"] == -100


@pytest.mark.asyncio
async def test_execute_order_partially_fills_at_participation_cap(monkeypatch):
    monkeypatch.setenv("SIMULATION_MAX_PARTICIPATION_RATE", "0.10")
    engine, manager = _engine(_snapshot(price=10.0, recent_volume=5_000))

    result = await engine.execute_order(
        _make_order(OrderSide.BUY, quantity=1_000)
    )

    assert result.success is True
    assert result.quantity == 500
    assert result.requested_quantity == 1_000
    assert result.message == "partially_filled"
    _, kwargs = manager.update_balance.call_args
    assert kwargs["delta_volume"] == 500
