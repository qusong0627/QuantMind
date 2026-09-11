from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.live_trading.services.risk_service import RiskService


class _FakeRedisClient:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get(self, key):
        return self.store.get(key)


class _FakeRedis:
    def __init__(self, store=None):
        self.client = _FakeRedisClient(store=store)


@pytest.mark.asyncio
async def test_risk_service_rejects_sell_to_open_when_long_short_not_enabled(monkeypatch):
    redis = _FakeRedis()
    svc = RiskService(db=None, redis=redis)

    async def _no_rules(_user_id):
        return []

    monkeypatch.setattr(svc, "get_applicable_rules", _no_rules)
    monkeypatch.setattr(
        "backend.services.live_trading.services.risk_service.get_margin_stock_pool_service",
        lambda _path: SimpleNamespace(is_margin_eligible=lambda _symbol: True),
    )
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.ENABLE_MARGIN_TRADING", True)
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.ENABLE_LONG_SHORT_REAL", False)
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.LONG_SHORT_WHITELIST_USERS", "1001")

    order = SimpleNamespace(
        symbol="600000.SH",
        order_value=1000.0,
        side=SimpleNamespace(value="sell"),
        trade_action=SimpleNamespace(value="sell_to_open"),
        trading_mode=SimpleNamespace(value="REAL"),
        is_margin_trade=True,
        tenant_id="default",
    )
    result = await svc.check_order_risk(user_id=1001, order=order, portfolio_value=100000.0, available_cash=50000.0)

    assert result["passed"] is False
    assert any(v["rule"] == "LONG_SHORT_NOT_ENABLED" for v in result["violations"])


@pytest.mark.asyncio
async def test_risk_service_rejects_sell_to_open_when_credit_snapshot_unavailable(monkeypatch):
    redis = _FakeRedis()
    svc = RiskService(db=object(), redis=redis)

    async def _no_rules(_user_id):
        return []

    monkeypatch.setattr(svc, "get_applicable_rules", _no_rules)
    monkeypatch.setattr(
        "backend.services.live_trading.services.risk_service.get_margin_stock_pool_service",
        lambda _path: SimpleNamespace(is_margin_eligible=lambda _symbol: True),
    )
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.ENABLE_MARGIN_TRADING", True)
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.ENABLE_LONG_SHORT_REAL", True)
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.LONG_SHORT_WHITELIST_USERS", "1001")
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.SHORT_ADMISSION_STRICT", True)
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils._fetch_latest_real_account_snapshot",
        AsyncMock(return_value=None),
    )

    order = SimpleNamespace(
        symbol="600000.SH",
        order_value=1000.0,
        side=SimpleNamespace(value="sell"),
        trade_action=SimpleNamespace(value="sell_to_open"),
        trading_mode=SimpleNamespace(value="REAL"),
        is_margin_trade=True,
        tenant_id="default",
    )
    result = await svc.check_order_risk(user_id=1001, order=order, portfolio_value=100000.0, available_cash=50000.0)

    assert result["passed"] is False
    assert any(v["rule"] == "CREDIT_ACCOUNT_UNAVAILABLE" for v in result["violations"])


@pytest.mark.asyncio
async def test_risk_service_passes_sell_to_open_when_all_checks_ready(monkeypatch):
    snapshot = {
        "payload_json": {
            "credit_enabled": True,
            "shortable_symbols_count": 120,
            "last_short_check_at": 1700000000,
        }
    }
    redis = _FakeRedis()
    svc = RiskService(db=object(), redis=redis)

    async def _no_rules(_user_id):
        return []

    monkeypatch.setattr(svc, "get_applicable_rules", _no_rules)
    monkeypatch.setattr(
        "backend.services.live_trading.services.risk_service.get_margin_stock_pool_service",
        lambda _path: SimpleNamespace(is_margin_eligible=lambda _symbol: True),
    )
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.ENABLE_MARGIN_TRADING", True)
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.ENABLE_LONG_SHORT_REAL", True)
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.LONG_SHORT_WHITELIST_USERS", "1001")
    monkeypatch.setattr("backend.services.live_trading.services.risk_service.settings.SHORT_ADMISSION_STRICT", True)
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils._fetch_latest_real_account_snapshot",
        AsyncMock(return_value=snapshot),
    )

    order = SimpleNamespace(
        symbol="600000.SH",
        order_value=1000.0,
        side=SimpleNamespace(value="sell"),
        trade_action=SimpleNamespace(value="sell_to_open"),
        trading_mode=SimpleNamespace(value="REAL"),
        is_margin_trade=True,
        tenant_id="default",
    )
    result = await svc.check_order_risk(user_id=1001, order=order, portfolio_value=100000.0, available_cash=50000.0)

    assert result["passed"] is True
    assert result["violations"] == []


@pytest.mark.asyncio
async def test_risk_service_rejects_star_board_buy_lot_less_than_200(monkeypatch):
    redis = _FakeRedis()
    svc = RiskService(db=None, redis=redis)

    async def _no_rules(_user_id):
        return []

    monkeypatch.setattr(svc, "get_applicable_rules", _no_rules)

    order = SimpleNamespace(
        symbol="688031.SH",
        quantity=100,
        order_value=14400.0,
        side=SimpleNamespace(value="buy"),
        trade_action=None,
        trading_mode=SimpleNamespace(value="REAL"),
        is_margin_trade=False,
        tenant_id="default",
    )
    result = await svc.check_order_risk(user_id=1001, order=order, portfolio_value=100000.0, available_cash=50000.0)
    assert result["passed"] is False
    assert any(v["rule"] == "min_lot_size" for v in result["violations"])
