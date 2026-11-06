"""orders.broker 记账字段：送单前把执行券商钉在订单上。

背景（2026-11-06 记账地基）：没有这个字段就无法按券商对账、也无法回答
「这笔单走的哪条通道」。写入点必须赶在 **SUBMITTED 那次 commit 之前**——
超时单停在 submitted，之后再写就永远落不了库。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.services.live_trading.services import trading_engine as te
from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.trade_shared.models.enums import OrderStatus, TradingMode


class FakeBroker:
    def __init__(self, broker_type: str = "tdx") -> None:
        self.broker_type = broker_type


def _engine(broker: FakeBroker) -> TradingEngine:
    engine = TradingEngine(db=MagicMock(), redis=MagicMock())
    engine._get_stock_broker = MagicMock(return_value=broker)  # type: ignore[method-assign]
    engine.order_service.transition_order_status = AsyncMock()  # type: ignore[method-assign]
    engine._execute_via_broker = AsyncMock(return_value=None)  # type: ignore[method-assign]
    engine._sync_account_to_redis = AsyncMock()  # type: ignore[method-assign]
    return engine


def _order() -> SimpleNamespace:
    return SimpleNamespace(
        order_id="11111111-2222-3333-4444-555555555555",
        user_id="10000001",
        tenant_id="default",
        symbol="SH600519",
        side=SimpleNamespace(value="buy"),
        quantity=100,
        price=1500.0,
        order_type=SimpleNamespace(value="limit"),
        trading_mode=TradingMode.REAL,
        status=OrderStatus.PENDING,
        broker=None,
    )


@pytest.mark.asyncio
async def test_submit_order_stamps_broker_before_submitted_commit():
    engine = _engine(FakeBroker("tdx"))
    order = _order()

    result = await engine.submit_order(order)

    assert result["success"] is True
    assert order.broker == "tdx"


@pytest.mark.asyncio
async def test_submit_order_does_not_override_existing_broker():
    engine = _engine(FakeBroker("qmt_exec"))
    order = _order()
    order.broker = "tdx"  # 来源单已指定通道（多账户/镜像跟随）

    await engine.submit_order(order)

    assert order.broker == "tdx"


@pytest.mark.asyncio
async def test_get_broker_stamps_type_on_instance(monkeypatch):
    created = FakeBroker("__unset__")

    monkeypatch.setattr(te, "create_broker", lambda **kwargs: created)
    monkeypatch.setattr(te.settings, "ENABLE_REAL_TRADING", True, raising=False)
    monkeypatch.setattr(te.settings, "REAL_BROKER_TYPE", "qmt_exec", raising=False)

    engine = TradingEngine.__new__(TradingEngine)
    engine._broker_cache = {}
    engine.redis = None

    broker = engine._get_broker(TradingMode.REAL, "SH600519")

    assert broker is created
    assert broker.broker_type == "qmt_exec"


@pytest.mark.asyncio
async def test_get_broker_marks_paper_for_simulation(monkeypatch):
    created = FakeBroker("__unset__")
    monkeypatch.setattr(te, "create_broker", lambda **kwargs: created)

    engine = TradingEngine.__new__(TradingEngine)
    engine._broker_cache = {}
    engine.redis = None

    broker = engine._get_broker(TradingMode.SIMULATION, "SH600519")

    assert broker.broker_type == "paper"
