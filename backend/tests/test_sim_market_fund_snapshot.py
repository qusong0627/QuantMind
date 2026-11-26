"""A simulation reset starts a new chart and daily return baseline."""

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.services.simulation.services import fund_snapshot_service as snapshots

Fund = snapshots.SimulationFundSnapshot
MarketFund = snapshots.SimulationMarketFundSnapshot
Service = snapshots.SimulationFundSnapshotService
BEFORE_RESET = date(2026, 11, 25)
RESET_DAY = date(2026, 11, 26)
NEXT_DAY = date(2026, 11, 27)


@pytest.fixture
def snapshot_db(monkeypatch):
    engine = create_engine("sqlite://")
    for model in (Fund, MarketFund):
        model.__table__.create(engine)

    with Session(engine) as session:

        class AsyncSession:
            async def execute(self, stmt):
                return session.execute(stmt)

        @asynccontextmanager
        async def get_session(*, read_only):
            yield AsyncSession()

        monkeypatch.setattr(snapshots, "get_session", get_session)
        yield session, AsyncSession()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_market", [False, True])
async def test_chart_ignores_pre_reset_snapshot(snapshot_db, multi_market):
    session, async_session = snapshot_db
    owner = {"tenant_id": "default", "user_id": "42"}
    session.add_all(
        [
            MarketFund(
                **owner, market="CN", snapshot_date=BEFORE_RESET, total_asset=884401.91
            ),
            MarketFund(
                **owner,
                market="CN",
                snapshot_date=RESET_DAY,
                total_asset=976143.63,
                initial_capital=1_000_000,
                today_pnl=91741.72,
            ),
            Fund(
                **owner,
                snapshot_date=RESET_DAY,
                total_asset=977296.63,
                today_pnl=-22703.37,
                source="eod_settlement",
            ),
        ]
    )
    if multi_market:
        session.add(MarketFund(**owner, market="HK", snapshot_date=RESET_DAY))
    session.flush()

    assert (
        await Service._market_day_open_equity(
            async_session, "default", "42", "CN", RESET_DAY
        )
        is None
    )

    rows = await Service.list_market_daily("default", "42", "CN", days=1)
    assert [row.snapshot_date for row in rows] == [RESET_DAY]
    assert len(await Service.list_market_daily("default", "42", "CN")) == 1
    expected = Decimal("976143.63" if multi_market else "977296.63")
    assert Decimal(str(rows[0].total_asset)) == expected
    assert (
        await Service._market_day_open_equity(
            async_session, "default", "42", "CN", NEXT_DAY
        )
        == expected
    )
    assert rows[0].today_pnl == (-23856.37 if multi_market else -22703.37)


@pytest.mark.asyncio
async def test_clear_user_daily_clears_both_tables_for_requested_owner(snapshot_db):
    session, async_session = snapshot_db
    for model in (Fund, MarketFund):
        for tenant, user in (
            ("default", "42"),
            ("default", "00000042"),
            ("other", "42"),
        ):
            session.add(
                model(tenant_id=tenant, user_id=user, snapshot_date=BEFORE_RESET)
            )
    session.flush()

    await Service.clear_user_daily(async_session, "default", {"42", "00000042"})

    for model in (Fund, MarketFund):
        assert [(row.tenant_id, row.user_id) for row in session.query(model)] == [
            ("other", "42")
        ]
