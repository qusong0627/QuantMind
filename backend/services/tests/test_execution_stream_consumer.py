from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.services.trade_shared.models.enums import OrderSide, OrderStatus, TradingMode
from backend.services.trade.services.execution_stream_consumer import ExecutionStreamConsumer


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, *, order=None, trade=None):
        self.order = order
        self.trade = trade
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM trades" in sql:
            return _ScalarResult(self.trade)
        if "FROM orders" in sql:
            return _ScalarResult(self.order)
        raise AssertionError(f"unexpected sql: {sql}")

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class _FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_order_filled_matches_by_client_order_id_and_uses_exchange_trade_id(monkeypatch):
    order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id=1001,
        portfolio_id=11,
        symbol="600000.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        filled_quantity=0.0,
        filled_value=0.0,
        average_price=None,
        quantity=100.0,
        exchange_order_id="oid-1",
        remarks=None,
    )
    session = _FakeSession(order=order, trade=None)
    consumer = ExecutionStreamConsumer()

    monkeypatch.setattr(
        "backend.services.trade.services.execution_stream_consumer.get_session",
        lambda: _FakeSessionContext(session),
    )
    async def _noop_notification(**_kwargs):
        return None
    monkeypatch.setattr(
        "backend.services.trade.services.execution_stream_consumer.publish_notification_async",
        _noop_notification,
    )

    await consumer._handle_order_filled(
        {
            "tenant_id": "default",
            "user_id": "1001",
            "client_order_id": "cid-001",
            "exchange_order_id": "oid-1",
            "exchange_trade_id": "tid-001",
            "broker_order_id": "not-a-uuid",
            "filled_qty": "100",
            "filled_price": "10.5",
        }
    )

    assert session.committed is True
    assert len(session.added) == 1
    assert session.added[0].exchange_trade_id == "tid-001"
    assert order.status == OrderStatus.FILLED
    assert order.average_price == 10.5


@pytest.mark.asyncio
async def test_order_submitted_matches_by_exchange_order_id_when_broker_order_id_is_not_uuid(monkeypatch):
    order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id=1001,
        portfolio_id=11,
        symbol="600000.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.PENDING,
        exchange_order_id="oid-2",
        remarks=None,
    )
    session = _FakeSession(order=order, trade=None)
    consumer = ExecutionStreamConsumer()

    monkeypatch.setattr(
        "backend.services.trade.services.execution_stream_consumer.get_session",
        lambda: _FakeSessionContext(session),
    )

    await consumer._handle_order_submitted(
        {
            "tenant_id": "default",
            "user_id": "1001",
            "exchange_order_id": "oid-2",
            "broker_order_id": "broker-generated-id",
            "event_id": "evt-1",
        }
    )

    assert order.status == OrderStatus.SUBMITTED
    assert "STREAM_SUBMITTED" in (order.remarks or "")


def _filled_order(**over):
    base = {
        "order_id": uuid4(),
        "tenant_id": "default",
        "user_id": 1001,
        "portfolio_id": 11,
        "symbol": "600000.SH",
        "side": OrderSide.BUY,
        "trading_mode": TradingMode.REAL,
        "status": OrderStatus.SUBMITTED,
        "filled_quantity": 0.0,
        "filled_value": 0.0,
        "average_price": None,
        "quantity": 100.0,
        "exchange_order_id": "oid-9",
        "remarks": None,
        # P2.7 分账归属：非 LLM 腿恒 None（既有用例都属于这一类）
        "agent": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _filled_event():
    return {
        "tenant_id": "default",
        "user_id": "1001",
        "client_order_id": "cid-009",
        "exchange_order_id": "oid-9",
        "exchange_trade_id": "tid-009",
        "broker_order_id": "not-a-uuid",
        "filled_qty": "100",
        "filled_price": "10.5",
    }


@pytest.mark.asyncio
async def test_order_filled_posts_the_agent_fill_once(monkeypatch):
    """P2.7：成交落库时把归属（``orders.agent``）记进分账账本，一次成交一次落账。"""
    from unittest.mock import AsyncMock

    import backend.services.trade.services.execution_stream_consumer as mod

    order = _filled_order(agent="deepseek-v4-pro")
    session = _FakeSession(order=order, trade=None)
    consumer = ExecutionStreamConsumer()
    monkeypatch.setattr(mod, "get_session", lambda: _FakeSessionContext(session))
    stub = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "post_fill_for_order", stub)

    async def _noop_notification(**_kwargs):
        return None

    monkeypatch.setattr(mod, "publish_notification_async", _noop_notification)

    await consumer._handle_order_filled(_filled_event())

    assert session.committed is True
    assert stub.await_count == 1
    assert stub.await_args.args[0] is session, "必须写进调用方的事务"
    kw = stub.await_args.kwargs
    assert kw["order"] is order
    assert kw["fill_key"] == "tid-009"
    assert (kw["quantity"], kw["price"]) == (100.0, 10.5)


@pytest.mark.asyncio
async def test_order_filled_replay_never_posts(monkeypatch):
    """同成交号重投（已有成交行）：既不插行也不落账本——否则额度被同一笔扣两次。"""
    from unittest.mock import AsyncMock

    import backend.services.trade.services.execution_stream_consumer as mod

    order = _filled_order(agent="deepseek-v4-pro")
    session = _FakeSession(order=order, trade=SimpleNamespace(trade_id="t-1"))
    consumer = ExecutionStreamConsumer()
    monkeypatch.setattr(mod, "get_session", lambda: _FakeSessionContext(session))
    stub = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "post_fill_for_order", stub)

    await consumer._handle_order_filled(_filled_event())

    assert session.added == []
    assert stub.await_count == 0
