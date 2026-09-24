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
async def test_the_trade_row_carries_the_full_fee_breakdown(monkeypatch):
    """四个费用列都要写。

    此前只写 ``commission=0.0``、另三列吃默认 0 ⇒ ``get_trade_statistics`` 求和
    恒为 0 ⇒ UI 上「总佣金 ¥0.00」——真单的成本口径不能是 0（复盘/风控/披露都读它）。

    金额取 10 万（高于最低佣金）才有区分力：真单按券商实收的估计（万2.5）记，
    撮合默认是万3，两者只在最低佣金之上才分得开。
    """
    from backend.services.simulation.services.market_rules import CN_RULES

    order = _filled_order(quantity=1000.0)
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

    await consumer._handle_order_filled({**_filled_event(), "filled_qty": "1000", "filled_price": "100"})

    trade = session.added[0]
    commission, stamp, transfer = CN_RULES.compute_real_order_breakdown(
        1000, 100, OrderSide.BUY
    )
    assert (trade.commission, trade.stamp_duty, trade.transfer_fee) == (
        commission,
        stamp,
        transfer,
    ), "费用分项必须来自 market_rules（费率单实现）"
    assert trade.total_fee == round(commission + stamp + transfer, 2)
    assert trade.total_fee == 26.0, "10 万买入 = 25（万2.5）+ 1 过户；31.0 即取了撮合口径"


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


def _scoped_params(stmt) -> dict:
    """ORM select 的绑定值，按去掉序号后缀的列名索引（``tenant_id_1`` → ``tenant_id``）。"""
    return {key.rsplit("_", 1)[0]: value for key, value in stmt.compile().params.items()}


class _ScopedTradeSession:
    """按 ``(租户, 用户, 成交号)`` 决定「这一笔记过没有」的替身。

    ——全库去重与账户内去重的分野就在这里：另一账户的同号成交**不该**吞掉这一笔。
    查询用的三个维度也一并记下来，供断言「去重查询确实带上了租户与用户」。
    """

    def __init__(self, *, order, trades_by_scope=None, commit_error=None):
        self.order = order
        self.trades = dict(trades_by_scope or {})
        self.commit_error = commit_error
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.queried_scopes = []

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM trades" in sql:
            p = _scoped_params(stmt)
            scope = (p.get("tenant_id"), p.get("user_id"), p.get("exchange_trade_id"))
            self.queried_scopes.append(scope)
            return _ScalarResult(self.trades.get(scope))
        if "FROM orders" in sql:
            return _ScalarResult(self.order)
        raise AssertionError(f"unexpected sql: {sql}")

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_order_filled_dedupe_is_scoped_to_the_account(monkeypatch):
    """另一个账户的**同号成交**不再吞掉这一笔（此前全库去重 → 静默丢单）。

    券商成交号只在一个账户内唯一：两个账户（多租户，或两个券商各自从 1 开始编号）
    撞出同一个号时，全库 SELECT 会把这一笔当重投丢掉——不落成交行、不更新订单、
    不记分账，而任何地方都不报错。
    """
    from unittest.mock import AsyncMock

    import backend.services.trade.services.execution_stream_consumer as mod

    order = _filled_order(agent="deepseek-v4-pro")
    session = _ScopedTradeSession(
        order=order,
        trades_by_scope={
            ("default", 2002, "tid-009"): SimpleNamespace(trade_id="t-2"),
        },
    )
    consumer = ExecutionStreamConsumer()
    monkeypatch.setattr(mod, "get_session", lambda: _FakeSessionContext(session))
    stub = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "post_fill_for_order", stub)

    async def _noop_notification(**_kwargs):
        return None

    monkeypatch.setattr(mod, "publish_notification_async", _noop_notification)

    await consumer._handle_order_filled(_filled_event())

    assert session.queried_scopes == [("default", 1001, "tid-009")], (
        "去重查询必须带上租户与用户（成交行的租户/用户取自订单）"
    )
    assert session.committed is True
    assert len(session.added) == 1, "本账户的这笔必须落库"
    assert stub.await_count == 1, "分账也要记——此前整笔成交被静默丢掉"


@pytest.mark.asyncio
async def test_order_filled_same_scope_replay_is_suppressed(monkeypatch):
    """同账户同成交号才是重投：不插行、不落账本。"""
    from unittest.mock import AsyncMock

    import backend.services.trade.services.execution_stream_consumer as mod

    order = _filled_order(agent="deepseek-v4-pro")
    session = _ScopedTradeSession(
        order=order,
        trades_by_scope={
            ("default", 1001, "tid-009"): SimpleNamespace(trade_id="t-1"),
        },
    )
    consumer = ExecutionStreamConsumer()
    monkeypatch.setattr(mod, "get_session", lambda: _FakeSessionContext(session))
    stub = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "post_fill_for_order", stub)

    await consumer._handle_order_filled(_filled_event())

    assert session.added == []
    assert stub.await_count == 0
    assert session.committed is False


@pytest.mark.asyncio
async def test_order_filled_integrity_error_means_already_recorded(monkeypatch):
    """并发双写撞成交唯一键：回滚、不抛（事件照常 ack）——重投的语义就是「已记过」。

    分账落账与本事务同生共死：这条路径回滚时账本也一起回滚，由先提交的那一方记。
    """
    from sqlalchemy.exc import IntegrityError
    from unittest.mock import AsyncMock

    import backend.services.trade.services.execution_stream_consumer as mod

    order = _filled_order(agent="deepseek-v4-pro")
    session = _ScopedTradeSession(
        order=order,
        commit_error=IntegrityError("INSERT INTO trades", {}, Exception("dup")),
    )
    consumer = ExecutionStreamConsumer()
    monkeypatch.setattr(mod, "get_session", lambda: _FakeSessionContext(session))
    stub = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "post_fill_for_order", stub)

    await consumer._handle_order_filled(_filled_event())  # 不抛

    assert stub.await_count == 1
    assert session.rolled_back is True
    assert session.committed is False
    assert len(session.added) == 1


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
