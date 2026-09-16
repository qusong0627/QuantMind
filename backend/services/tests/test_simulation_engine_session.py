from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.simulation.engine import SimulationEngine
from backend.services.simulation.services.signal_loader import _normalize_signal_symbol


@pytest.mark.asyncio
async def test_run_cycle_uses_get_session_not_db_manager_session():
    engine = SimulationEngine()
    engine.signal_loader = MagicMock()
    engine.signal_loader.load_latest_signals = AsyncMock(return_value=[])

    fake_db = MagicMock()

    @asynccontextmanager
    async def fake_get_session(*_args, **_kwargs):
        yield fake_db

    with patch("backend.services.simulation.engine.get_session", fake_get_session):
        report = await engine.run_cycle(
            tenant_id="default",
            user_id="00000001",
            strategy_id="2",
        )

    assert report.error == "无可用信号"
    engine.signal_loader.load_latest_signals.assert_awaited_once()
    assert engine.signal_loader.load_latest_signals.await_args.kwargs["db"] is fake_db
    assert engine.signal_loader.load_latest_signals.await_args.kwargs["run_id"] is None


@pytest.mark.asyncio
async def test_run_cycle_does_not_treat_task_id_as_signal_batch():
    engine = SimulationEngine()
    engine.signal_loader = MagicMock()
    engine.signal_loader.load_latest_signals = AsyncMock(return_value=[])

    fake_db = MagicMock()

    @asynccontextmanager
    async def fake_get_session(*_args, **_kwargs):
        yield fake_db

    with patch("backend.services.simulation.engine.get_session", fake_get_session):
        report = await engine.run_cycle(
            tenant_id="default",
            user_id="00000001",
            strategy_id="2",
            run_id="bootstrap_run_1789367236_2",
        )

    assert report.error == "无可用信号"
    assert engine.signal_loader.load_latest_signals.await_args.kwargs["run_id"] is None


def test_signal_loader_normalizes_bare_code_to_suffix():
    assert _normalize_signal_symbol("600928") == "600928.SH"
    assert _normalize_signal_symbol("000419") == "000419.SZ"
    assert _normalize_signal_symbol("SH600928") == "600928.SH"
    assert _normalize_signal_symbol("600928.SH") == "600928.SH"


def test_ensure_redis_attaches_connected_trade_client():
    engine = SimulationEngine()
    assert getattr(engine.redis, "client", None) is None

    fake = MagicMock()
    fake.client = object()
    with patch("backend.services.trade_shared.redis_client.get_redis", return_value=fake):
        engine._ensure_redis()

    assert engine.redis is fake
    assert engine.account_manager.redis is fake
