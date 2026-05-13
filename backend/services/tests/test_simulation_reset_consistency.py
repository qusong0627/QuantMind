import json

import pytest

from backend.services.trade.deps import AuthContext
from backend.services.trade.routers import simulation as simulation_router
from backend.services.trade.services.simulation_manager import SimulationAccountManager


class _FakeRedisClient:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def eval(self, script, numkeys, *args):
        key = args[0]
        symbol = args[1]
        delta_cash = float(args[2])
        delta_volume = float(args[3])
        price = float(args[4])
        account = json.loads(self.store[key])
        account["cash"] = float(account.get("cash") or 0.0) + delta_cash
        positions = dict(account.get("positions") or {})
        pos = dict(positions.get(symbol) or {"volume": 0, "cost": 0, "market_value": 0, "price": 0})
        pos["volume"] = float(pos.get("volume") or 0.0) + delta_volume
        pos["price"] = price
        pos["market_value"] = float(pos["volume"] or 0.0) * price
        positions[symbol] = pos
        account["positions"] = positions
        account["market_value"] = sum(float(p.get("volume") or 0.0) * float(p.get("price") or 0.0) for p in positions.values())
        account["total_asset"] = float(account.get("cash") or 0.0) + float(account.get("market_value") or 0.0)
        self.store[key] = json.dumps(account, ensure_ascii=False)
        return {"success": True}


class _FakeRedis:
    def __init__(self):
        self.client = _FakeRedisClient()


@pytest.mark.asyncio
async def test_reset_with_initial_cash_keeps_initial_equity_consistent():
    redis = _FakeRedis()
    auth = AuthContext(user_id="1001", tenant_id="default", raw_sub="1001", roles=["user"])
    manager = SimulationAccountManager(redis)

    await manager.set_initial_cash(user_id=1001, initial_cash=300_000, tenant_id="default")

    await simulation_router.reset_simulation_account(
        request=simulation_router.AccountResetRequest(initial_cash=500_000),
        auth=auth,
        redis=redis,
    )

    resp = await simulation_router.get_simulation_account(auth=auth, redis=redis)
    data = resp["data"]
    assert data["cash"] == 500_000
    assert data["total_asset"] == 500_000
    assert data["initial_equity"] == 500_000
    assert data["baseline"]["initial_equity"] == 500_000

    settings = await manager.get_settings(
        user_id=1001,
        tenant_id="default",
        default_initial_cash=1_000_000,
        cooldown_days=30,
    )
    assert settings["initial_cash"] == 500_000
