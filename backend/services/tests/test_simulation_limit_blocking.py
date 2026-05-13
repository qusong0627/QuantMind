import pytest

from backend.services.trade.simulation.models.order import OrderSide, OrderType, SimOrder
from backend.services.trade.simulation.services.execution_engine import (
    MarketSnapshot,
    SimulationExecutionEngine,
)


class _FakeManager:
    def __init__(self):
        self.called = False

    async def update_balance(self, **kwargs):
        self.called = True
        return {"success": True}


class _FakeDb:
    async def execute(self, *args, **kwargs):
        raise AssertionError("db.execute should not be called in these tests")


def _make_order(side: OrderSide) -> SimOrder:
    return SimOrder(
        tenant_id="default",
        user_id=1001,
        portfolio_id=0,
        symbol="SH600000",
        side=side,
        order_type=OrderType.MARKET,
        quantity=100.0,
    )


def test_limit_price_detection_ignores_non_price_flags():
    assert SimulationExecutionEngine._is_price_near(price=10.0, limit_price=1.0) is False
    assert SimulationExecutionEngine._is_price_near(price=10.0, limit_price=2.0) is False


def test_limit_price_detection_uses_nearby_limit_price():
    assert SimulationExecutionEngine._is_price_near(price=10.0, limit_price=10.0) is True
    assert SimulationExecutionEngine._is_price_near(price=10.0, limit_price=10.01) is True
    assert SimulationExecutionEngine._is_price_near(price=10.0, limit_price=10.2) is False


@pytest.mark.asyncio
async def test_execute_order_blocks_buy_when_limit_up(monkeypatch):
    manager = _FakeManager()
    engine = SimulationExecutionEngine(_FakeDb(), manager)
    order = _make_order(OrderSide.BUY)

    async def _fake_latest_price(_symbol: str, **_kwargs) -> MarketSnapshot:
        return MarketSnapshot(price=10.0, price_source="test", limit_up=True)

    monkeypatch.setattr(engine, "_latest_price", _fake_latest_price)
    result = await engine.execute_order(order)

    assert result.success is False
    assert "Limit-up" in result.message
    assert manager.called is False


@pytest.mark.asyncio
async def test_execute_order_blocks_sell_when_limit_down(monkeypatch):
    manager = _FakeManager()
    engine = SimulationExecutionEngine(_FakeDb(), manager)
    order = _make_order(OrderSide.SELL)

    async def _fake_latest_price(_symbol: str, **_kwargs) -> MarketSnapshot:
        return MarketSnapshot(price=10.0, price_source="test", limit_down=True)

    monkeypatch.setattr(engine, "_latest_price", _fake_latest_price)
    result = await engine.execute_order(order)

    assert result.success is False
    assert "Limit-down" in result.message
    assert manager.called is False


@pytest.mark.asyncio
async def test_execute_order_blocks_when_suspended(monkeypatch):
    manager = _FakeManager()
    engine = SimulationExecutionEngine(_FakeDb(), manager)
    order = _make_order(OrderSide.BUY)

    async def _fake_latest_price(_symbol: str, **_kwargs) -> MarketSnapshot:
        return MarketSnapshot(price=10.0, price_source="test", suspended=True)

    monkeypatch.setattr(engine, "_latest_price", _fake_latest_price)
    result = await engine.execute_order(order)

    assert result.success is False
    assert "suspended" in result.message.lower()
    assert manager.called is False
