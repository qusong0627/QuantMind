from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)


def _order(symbol: str = "SH600000"):
    return SimpleNamespace(symbol=symbol, trading_session_date=None)


@pytest.mark.asyncio
async def test_cn_session_queues_outside_market_hours():
    engine = SimulationExecutionEngine(None, None)
    before_open = datetime(2026, 9, 15, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    decision = await engine.assess_execution_window(_order(), now=before_open)

    assert decision.can_execute is False
    assert decision.retryable is True
    assert decision.target_trade_date == date(2026, 9, 15)


@pytest.mark.asyncio
async def test_cn_session_accepts_afternoon_market_hours():
    engine = SimulationExecutionEngine(None, None)
    market_time = datetime(
        2026, 9, 15, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    )

    decision = await engine.assess_execution_window(_order(), now=market_time)

    assert decision.can_execute is True
    assert decision.retryable is False


@pytest.mark.asyncio
async def test_cn_session_queues_during_lunch_break():
    engine = SimulationExecutionEngine(None, None)
    lunch = datetime(2026, 9, 15, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    decision = await engine.assess_execution_window(_order(), now=lunch)

    assert decision.can_execute is False
    assert decision.target_trade_date == date(2026, 9, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hour", "minute"),
    [(11, 45), (15, 30)],
)
async def test_hk_session_accepts_exchange_specific_hours(hour, minute):
    engine = SimulationExecutionEngine(None, None)
    market_time = datetime(
        2026, 9, 15, hour, minute, tzinfo=ZoneInfo("Asia/Hong_Kong")
    )

    decision = await engine.assess_execution_window(
        _order("0700.HK"), now=market_time
    )

    assert decision.can_execute is True
    assert decision.target_trade_date == date(2026, 9, 15)


@pytest.mark.asyncio
async def test_hk_session_queues_after_16_to_next_session():
    engine = SimulationExecutionEngine(None, None)
    after_close = datetime(
        2026, 9, 15, 16, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")
    )

    decision = await engine.assess_execution_window(
        _order("0700.HK"), now=after_close
    )

    assert decision.can_execute is False
    assert decision.target_trade_date == date(2026, 9, 16)


def test_t1_uses_shanghai_trade_date_for_naive_utc_lot():
    # 2026-09-14 16:30 UTC is 2026-09-15 00:30 in Shanghai.
    lot = SimpleNamespace(
        quantity_remaining=100,
        position_side="long",
        open_date=datetime(2026, 9, 14, 16, 30),
    )

    assert (
        SimulationProjectionService._lot_available_quantity(
            lot, as_of_date=date(2026, 9, 15)
        )
        == 0
    )
    assert (
        SimulationProjectionService._lot_available_quantity(
            lot, as_of_date=date(2026, 9, 16)
        )
        == 100
    )


def test_quote_age_requires_timestamp_and_rejects_old_quote():
    now = datetime.now(timezone.utc).timestamp()
    assert SimulationExecutionEngine._quote_age_seconds({}) is None
    age = SimulationExecutionEngine._quote_age_seconds({"timestamp": now - 10})
    assert age is not None
    assert 9 <= age <= 12
