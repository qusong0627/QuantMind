import uuid
import sys
import types
from types import SimpleNamespace

import pytest

if "slowapi" not in sys.modules:
    slowapi_module = types.ModuleType("slowapi")
    slowapi_util_module = types.ModuleType("slowapi.util")

    class _Limiter:
        def __init__(self, *args, **kwargs):
            pass

        def limit(self, *args, **kwargs):
            def _decorator(func):
                return func

            return _decorator

    def _get_remote_address(*args, **kwargs):
        return "test-client"

    slowapi_module.Limiter = _Limiter
    slowapi_util_module.get_remote_address = _get_remote_address
    sys.modules["slowapi"] = slowapi_module
    sys.modules["slowapi.util"] = slowapi_util_module

from backend.services.trade.routers import internal_strategy_lifecycle
from backend.services.live_trading.services import internal_strategy_dispatcher
from backend.services.trade_shared.models.enums import PositionSide, TradeAction


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value

    def all(self):
        if self._value is None:
            return []
        if isinstance(self._value, list):
            return list(self._value)
        return [self._value]

    def scalar_one_or_none(self):
        return self._value


class _SequenceDb:
    def __init__(self, values):
        self._values = list(values)

    async def execute(self, *_args, **_kwargs):
        if not self._values:
            raise AssertionError("unexpected execute call")
        return _ScalarResult(self._values.pop(0))

    async def commit(self):
        return None


class _DummyRedis:
    pass


async def _fake_portfolio_snapshot(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_internal_strategy_order_sell_to_open_success(monkeypatch):
    captured = {}

    class _FakeOrderService:
        def __init__(self, _db, _redis):
            pass

        async def create_order(self, user_id, tenant_id, order_data):
            captured["order_data"] = order_data
            return SimpleNamespace(
                order_id=uuid.uuid4(),
                trade_action=order_data.trade_action,
                position_side=order_data.position_side,
                is_margin_trade=order_data.is_margin_trade,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        async def transition_order_status(self, *_args, **_kwargs):
            return None

    class _FakeEngine:
        def __init__(self, _db, _redis):
            pass

        async def check_order_risk(self, _uid, _order):
            return {"passed": True, "violations": []}

        async def submit_order(self, order, tenant_id="default"):
            return {
                "success": True,
                "order_id": str(order.order_id),
                "status": "submitted",
                "message": f"submitted:{tenant_id}",
            }

    monkeypatch.setattr(internal_strategy_dispatcher, "OrderService", _FakeOrderService)
    monkeypatch.setattr(internal_strategy_dispatcher, "TradingEngine", _FakeEngine)
    monkeypatch.setattr(internal_strategy_dispatcher, "_fetch_active_portfolio_snapshot", _fake_portfolio_snapshot)

    db = _SequenceDb([12345, None])
    res = await internal_strategy_lifecycle.strategy_order(
        {
            "trading_mode": "REAL",
            "symbol": "600000.SH",
            "side": "SELL",
            "quantity": 200,
            "price": 10.1,
            "order_type": "LIMIT",
            "trade_action": "sell_to_open",
            "position_side": "short",
            "is_margin_trade": True,
            "client_order_id": "cid-chain-001",
        },
        x_user_id="1001",
        x_tenant_id="default",
        redis=_DummyRedis(),
        db=db,
    )

    assert res["status"] == "success"
    assert res["execution"] == "direct"
    assert captured["order_data"].trade_action == TradeAction.SELL_TO_OPEN
    assert captured["order_data"].position_side == PositionSide.SHORT
    assert captured["order_data"].is_margin_trade is True


@pytest.mark.asyncio
async def test_internal_strategy_order_sell_to_open_rejected_by_risk(monkeypatch):
    class _FakeOrderService:
        def __init__(self, _db, _redis):
            self.rejected = False

        async def create_order(self, user_id, tenant_id, order_data):
            return SimpleNamespace(
                order_id=uuid.uuid4(),
                trade_action=order_data.trade_action,
                position_side=order_data.position_side,
                is_margin_trade=order_data.is_margin_trade,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        async def transition_order_status(self, order, status, remarks=None):
            order.status = status
            order.remarks = remarks
            return order

    class _FakeEngine:
        def __init__(self, _db, _redis):
            pass

        async def check_order_risk(self, _uid, _order):
            return {
                "passed": False,
                "violations": [{"rule": "SHORT_QUOTA_INSUFFICIENT", "message": "quota not enough"}],
            }

        async def submit_order(self, *_args, **_kwargs):
            raise AssertionError("risk blocked should not submit")

    monkeypatch.setattr(internal_strategy_dispatcher, "OrderService", _FakeOrderService)
    monkeypatch.setattr(internal_strategy_dispatcher, "TradingEngine", _FakeEngine)
    monkeypatch.setattr(internal_strategy_dispatcher, "_fetch_active_portfolio_snapshot", _fake_portfolio_snapshot)

    db = _SequenceDb([12345, None])
    res = await internal_strategy_lifecycle.strategy_order(
        {
            "trading_mode": "REAL",
            "symbol": "600000.SH",
            "side": "SELL",
            "quantity": 200,
            "price": 10.1,
            "order_type": "LIMIT",
            "trade_action": "sell_to_open",
            "position_side": "short",
            "is_margin_trade": True,
            "client_order_id": "cid-chain-002",
        },
        x_user_id="1001",
        x_tenant_id="default",
        redis=_DummyRedis(),
        db=db,
    )

    assert res["status"] == "rejected"
    assert res["execution"] == "risk_blocked"
    assert res["violations"][0]["rule"] == "SHORT_QUOTA_INSUFFICIENT"


@pytest.mark.asyncio
async def test_internal_strategy_order_buy_to_close_success(monkeypatch):
    captured = {}

    class _FakeOrderService:
        def __init__(self, _db, _redis):
            pass

        async def create_order(self, user_id, tenant_id, order_data):
            captured["order_data"] = order_data
            return SimpleNamespace(
                order_id=uuid.uuid4(),
                trade_action=order_data.trade_action,
                position_side=order_data.position_side,
                is_margin_trade=order_data.is_margin_trade,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        async def transition_order_status(self, *_args, **_kwargs):
            return None

    class _FakeEngine:
        def __init__(self, _db, _redis):
            pass

        async def check_order_risk(self, _uid, _order):
            return {"passed": True, "violations": []}

        async def submit_order(self, order, tenant_id="default"):
            return {
                "success": True,
                "order_id": str(order.order_id),
                "status": "submitted",
                "message": f"submitted:{tenant_id}",
            }

    monkeypatch.setattr(internal_strategy_dispatcher, "OrderService", _FakeOrderService)
    monkeypatch.setattr(internal_strategy_dispatcher, "TradingEngine", _FakeEngine)
    monkeypatch.setattr(internal_strategy_dispatcher, "_fetch_active_portfolio_snapshot", _fake_portfolio_snapshot)

    db = _SequenceDb([12345, None])
    res = await internal_strategy_lifecycle.strategy_order(
        {
            "trading_mode": "REAL",
            "symbol": "600000.SH",
            "side": "BUY",
            "quantity": 100,
            "price": 9.8,
            "order_type": "LIMIT",
            "trade_action": "buy_to_close",
            "position_side": "short",
            "is_margin_trade": True,
            "client_order_id": "cid-chain-003",
        },
        x_user_id="1001",
        x_tenant_id="default",
        redis=_DummyRedis(),
        db=db,
    )

    assert res["status"] == "success"
    assert captured["order_data"].trade_action == TradeAction.BUY_TO_CLOSE
    assert captured["order_data"].position_side == PositionSide.SHORT
    assert captured["order_data"].is_margin_trade is True
