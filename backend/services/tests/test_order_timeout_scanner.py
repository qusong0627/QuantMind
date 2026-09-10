from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.services.trade_shared.models.enums import OrderSide, OrderStatus, TradingMode
from backend.services.trade.services import order_timeout_scanner


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self, orders):
        self.orders = list(orders)
        self.committed = False

    async def execute(self, _stmt):
        return _ScalarResult(self.orders)

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
async def test_bridge_ack_timeout_marks_pending_review_without_rejecting(monkeypatch):
    order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id=1001,
        symbol="600000.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(seconds=300),
        exchange_order_id=None,
        remarks="[AWAITING_BRIDGE_ACK]",
    )
    session = _FakeSession([order])
    notifications = []

    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )

    async def _fake_notification(**kwargs):
        notifications.append(kwargs)
        return None

    monkeypatch.setattr(order_timeout_scanner, "publish_notification_async", _fake_notification)
    monkeypatch.setattr(order_timeout_scanner, "_BRIDGE_ACK_TIMEOUT_SECONDS", 120)

    count = await order_timeout_scanner._scan_bridge_ack_timeout_once()

    assert count == 1
    assert session.committed is True
    assert order.status == OrderStatus.SUBMITTED
    assert "[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]" in (order.remarks or "")
    assert notifications[0]["title"] == "桥接回报超时待核查"


@pytest.mark.asyncio
async def test_bridge_ack_timeout_skips_already_flagged_order(monkeypatch):
    order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id=1001,
        symbol="600000.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(seconds=300),
        exchange_order_id=None,
        remarks="[AWAITING_BRIDGE_ACK] [BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]",
    )
    session = _FakeSession([])
    notifications = []

    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )

    async def _fake_notification(**kwargs):
        notifications.append(kwargs)
        return None

    monkeypatch.setattr(order_timeout_scanner, "publish_notification_async", _fake_notification)
    monkeypatch.setattr(order_timeout_scanner, "_BRIDGE_ACK_TIMEOUT_SECONDS", 120)

    count = await order_timeout_scanner._scan_bridge_ack_timeout_once()

    assert count == 0
    assert notifications == []


class _FilterSession(_FakeSession):
    """模拟 SQL 的托管订单排除条件（通达信桥/mirror 前缀的行不返回）。"""

    async def execute(self, stmt):
        kept = [
            o
            for o in self.orders
            if (o.remarks is None or "通达信桥委托" not in (o.remarks or ""))
            and not (o.remarks or "").startswith("mirror:")
        ]
        return _ScalarResult(kept)


@pytest.mark.asyncio
async def test_scan_once_skips_bridge_managed_orders(monkeypatch):
    # 桥管理的委托（成交回报由桥每 30s 同步）不得被本地超时启发式过期
    bridge_order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id=1001,
        symbol="SH600206",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(minutes=40),
        exchange_order_id="160356",
        remarks="通达信桥委托",
    )
    normal_order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id=1001,
        symbol="SH600000",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(minutes=40),
        exchange_order_id=None,
        remarks=None,
    )
    session = _FilterSession([bridge_order, normal_order])

    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )
    monkeypatch.setattr(order_timeout_scanner, "_TIMEOUT_MINUTES", 30)

    count = await order_timeout_scanner._scan_once()

    assert count == 1
    assert bridge_order.status == OrderStatus.SUBMITTED
    assert normal_order.status == OrderStatus.EXPIRED


@pytest.mark.asyncio
async def test_scan_once_skips_mirror_real_orders(monkeypatch):
    # QMT 镜像真单：柜台可能仍挂着（随时成交），本地判死会造成状态错位
    mirror_order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id="00000001",
        symbol="600036.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(minutes=40),
        exchange_order_id="9351",
        remarks="mirror:internal_dispatcher:SIMULATION",
    )
    normal_order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id="00000001",
        symbol="600000.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(minutes=40),
        exchange_order_id=None,
        remarks="manual order",
    )
    session = _FilterSession([mirror_order, normal_order])

    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )
    monkeypatch.setattr(order_timeout_scanner, "_TIMEOUT_MINUTES", 30)

    count = await order_timeout_scanner._scan_once()

    assert count == 1
    assert mirror_order.status == OrderStatus.SUBMITTED
    assert normal_order.status == OrderStatus.EXPIRED


@pytest.mark.asyncio
async def test_scan_once_query_excludes_bridge_remarks(monkeypatch):
    """SQL 层面必须带排除条件，防止真实执行把桥委托误标过期。"""
    captured = {}

    class _CaptureSession(_FakeSession):
        async def execute(self, stmt):
            captured["stmt"] = stmt
            return _ScalarResult([])

    session = _CaptureSession([])
    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )

    await order_timeout_scanner._scan_once()

    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "通达信桥委托" in sql
    assert "LIKE" in sql or "like" in sql


@pytest.mark.asyncio
async def test_scan_once_query_excludes_mirror_remarks(monkeypatch):
    """SQL 层面排除 QMT 镜像单（remarks='mirror:...'）。"""
    captured = {}

    class _CaptureSession(_FakeSession):
        async def execute(self, stmt):
            captured["stmt"] = stmt
            return _ScalarResult([])

    session = _CaptureSession([])
    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )

    await order_timeout_scanner._scan_once()

    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "mirror:%" in sql


class _StaleFilterSession(_FakeSession):
    """模拟 _flag_stale_broker_managed_once 的过滤：只返回托管且未标记的行。"""

    async def execute(self, stmt):
        kept = [
            o
            for o in self.orders
            if (o.remarks or "").startswith("mirror:")
            and "[STALE_PENDING_REVIEW]" not in (o.remarks or "")
        ]
        return _ScalarResult(kept)


@pytest.mark.asyncio
async def test_flag_stale_broker_managed_marks_without_expiring(monkeypatch):
    order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id="00000001",
        symbol="600036.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(minutes=40),
        remarks="mirror:internal_dispatcher:SIMULATION",
    )
    session = _StaleFilterSession([order])
    notifications = []

    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )

    async def _fake_notification(**kwargs):
        notifications.append(kwargs)
        return None

    monkeypatch.setattr(order_timeout_scanner, "publish_notification_async", _fake_notification)
    monkeypatch.setattr(order_timeout_scanner, "_TIMEOUT_MINUTES", 30)

    count = await order_timeout_scanner._flag_stale_broker_managed_once()

    assert count == 1
    assert session.committed is True
    assert order.status == OrderStatus.SUBMITTED  # 状态不动，柜台才是权威
    assert "[STALE_PENDING_REVIEW]" in (order.remarks or "")
    assert notifications[0]["title"] == "委托长时间未成交"


@pytest.mark.asyncio
async def test_flag_stale_broker_managed_skips_already_flagged(monkeypatch):
    order = SimpleNamespace(
        order_id=uuid4(),
        tenant_id="default",
        user_id="00000001",
        symbol="600036.SH",
        side=OrderSide.BUY,
        trading_mode=TradingMode.REAL,
        status=OrderStatus.SUBMITTED,
        submitted_at=datetime.now() - timedelta(minutes=40),
        remarks="mirror:sim [STALE_PENDING_REVIEW] [pending_over=30m]",
    )
    session = _StaleFilterSession([order])
    notifications = []

    monkeypatch.setattr(
        "backend.services.trade.services.order_timeout_scanner.get_session",
        lambda: _FakeSessionContext(session),
    )

    async def _fake_notification(**kwargs):
        notifications.append(kwargs)
        return None

    monkeypatch.setattr(order_timeout_scanner, "publish_notification_async", _fake_notification)

    count = await order_timeout_scanner._flag_stale_broker_managed_once()

    assert count == 0
    assert notifications == []
