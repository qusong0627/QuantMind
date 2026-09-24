import json
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services import corporate_action_service
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)
from backend.shared.trade_redis_keys import normalize_trade_user_id


class _FakeRedisClient:
    def __init__(self):
        self.store: dict[str, str] = {}

    @property
    def client(self):
        return self

    def set(self, key, value):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)


@pytest.mark.asyncio
async def test_dividend_apply_writes_redis_cache(monkeypatch):
    fake_redis = _FakeRedisClient()
    monkeypatch.setattr(corporate_action_service, "redis_client", fake_redis)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(SimulationAccount.__table__.create)
        await conn.run_sync(SimulationCashLedger.__table__.create)
        await conn.run_sync(SimulationPositionLot.__table__.create)
        await conn.run_sync(SimulationCorporateAction.__table__.create)
        await conn.execute(
            text(
                "CREATE TABLE stock_daily_latest ("
                "symbol TEXT, trade_date TEXT, close FLOAT, adj_factor FLOAT)"
            )
        )

    async with session_factory() as session:
        session.add(
            SimulationAccount(
                account_id="sim:default:08030005",
                tenant_id="default",
                user_id="08030005",
                cash=0.0,
                available_cash=0.0,
                long_market_value=1000.0,
                total_asset=1000.0,
                equity=1000.0,
                initial_equity=1000.0,
            )
        )
        session.add(
            SimulationPositionLot(
                account_id="sim:default:08030005",
                tenant_id="default",
                user_id="08030005",
                # 台账 lots 真实为后缀式（公司行为表为前缀式）：此前测试误用
                # 前缀式，掩盖了 _apply_action 按前缀查空、分红永不入账的 bug。
                symbol="600519.SH",
                position_side="long",
                quantity_open=100.0,
                quantity_remaining=100.0,
                cost_price=10.0,
                cost_amount=1000.0,
                status="open",
            )
        )
        session.add(
            SimulationCorporateAction(
                id=1,
                symbol="SH600519",
                action_type="dividend",
                cash_dividend_per_share=0.5,
                status="pending",
            )
        )
        await session.execute(
            text(
                "INSERT INTO stock_daily_latest(symbol, trade_date, close, adj_factor) "
                "VALUES ('SH600519', '2026-06-13', 9.5, 1.0)"
            )
        )
        await session.flush()

        await SimulationCorporateActionService._apply_action(
            session=session,
            action=await session.get(SimulationCorporateAction, 1),
            applied_at=datetime(2026, 6, 13, 9, 0, 0),
        )
        await session.commit()

    await engine.dispose()

    sim_key = "simulation:account:default:08030005"
    trade_user = normalize_trade_user_id("08030005") or "08030005"
    trade_key = f"trade:account:default:{trade_user}"

    assert sim_key in fake_redis.store, "sim cache key not written"
    sim_payload = json.loads(fake_redis.store[sim_key])
    assert float(sim_payload["cash"]) == pytest.approx(50.0)
    assert sim_payload["rebuild_source"] == "corporate_action_apply"

    assert trade_key in fake_redis.store, "trade account cache key not written"
    trade_payload = json.loads(fake_redis.store[trade_key])
    assert float(trade_payload["cash"]) == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_bonus_share_apply_writes_redis_with_updated_quantity(monkeypatch):
    fake_redis = _FakeRedisClient()
    monkeypatch.setattr(corporate_action_service, "redis_client", fake_redis)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(SimulationAccount.__table__.create)
        await conn.run_sync(SimulationCashLedger.__table__.create)
        await conn.run_sync(SimulationPositionLot.__table__.create)
        await conn.run_sync(SimulationCorporateAction.__table__.create)
        await conn.execute(
            text(
                "CREATE TABLE stock_daily_latest ("
                "symbol TEXT, trade_date TEXT, close FLOAT, adj_factor FLOAT)"
            )
        )

    async with session_factory() as session:
        session.add(
            SimulationAccount(
                account_id="sim:default:08030005",
                tenant_id="default",
                user_id="08030005",
                cash=1000.0,
                available_cash=1000.0,
                long_market_value=1000.0,
                total_asset=2000.0,
                equity=2000.0,
                initial_equity=2000.0,
            )
        )
        session.add(
            SimulationPositionLot(
                account_id="sim:default:08030005",
                tenant_id="default",
                user_id="08030005",
                # 台账 lots 真实为后缀式（公司行为表为前缀式）：此前测试误用
                # 前缀式，掩盖了 _apply_action 按前缀查空、分红永不入账的 bug。
                symbol="600519.SH",
                position_side="long",
                quantity_open=100.0,
                quantity_remaining=100.0,
                cost_price=10.0,
                cost_amount=1000.0,
                status="open",
            )
        )
        session.add(
            SimulationCorporateAction(
                id=2,
                symbol="SH600519",
                action_type="bonus_share",
                share_ratio=0.3,
                status="pending",
            )
        )
        await session.execute(
            text(
                "INSERT INTO stock_daily_latest(symbol, trade_date, close, adj_factor) "
                "VALUES ('SH600519', '2026-06-13', 10.0, 1.0)"
            )
        )
        await session.flush()

        await SimulationCorporateActionService._apply_action(
            session=session,
            action=await session.get(SimulationCorporateAction, 2),
            applied_at=datetime(2026, 6, 13, 9, 0, 0),
        )

        ledger_rows = (
            await session.execute(
                select(SimulationCashLedger).where(
                    SimulationCashLedger.ref_type == "corporate_action"
                )
            )
        ).scalars().all()
        await session.commit()

    await engine.dispose()

    sim_key = "simulation:account:default:08030005"
    assert sim_key in fake_redis.store
    sim_payload = json.loads(fake_redis.store[sim_key])
    positions = sim_payload.get("positions") or {}
    pos = next(iter(positions.values())) if positions else {}
    assert float(pos.get("volume", 0)) == pytest.approx(130.0)

    # bonus_share apply 应写一条 BONUS_SHARE_VALUE cash_ledger,记录市值增量
    # 增量 = 100 股 × 0.3 ratio × 10 现价 = 300
    bonus_ledgers = [r for r in ledger_rows if r.event_type == "BONUS_SHARE_VALUE"]
    assert len(bonus_ledgers) == 1
    assert float(bonus_ledgers[0].amount) == pytest.approx(300.0)


def test_persist_projection_cache_noop_when_redis_unavailable(monkeypatch):
    class _NoRedis:
        @property
        def client(self):
            return None

    monkeypatch.setattr(corporate_action_service, "redis_client", _NoRedis())
    captured = {}

    class _Account:
        tenant_id = "default"
        user_id = "08030005"

    SimulationCorporateActionService._persist_projection_cache(
        account=_Account(),
        positions={},
        tenant_id="default",
        user_id="08030005",
    )
    assert captured == {}
