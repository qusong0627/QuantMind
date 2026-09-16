import json

import pytest

from backend.services.trade_shared.simulation_manager import (
    SimulationAccountManager,
    canonical_sim_uid,
    require_sim_user_id,
)
from backend.shared.simulation_account_keys import (
    account_lookup_keys,
    canonical_sim_user_suffix,
    ledger_user_id_candidates,
)


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
        account["market_value"] = sum(
            float(p.get("volume") or 0.0) * float(p.get("price") or 0.0)
            for p in positions.values()
        )
        account["total_asset"] = float(account.get("cash") or 0.0) + float(
            account.get("market_value") or 0.0
        )
        self.store[key] = json.dumps(account, ensure_ascii=False)
        return {"success": True}


class _FakeRedis:
    def __init__(self):
        self.client = _FakeRedisClient()


@pytest.mark.asyncio
async def test_simulation_manager_writes_settings_and_account_json():
    redis = _FakeRedis()
    manager = SimulationAccountManager(redis)

    await manager.set_initial_cash(
        user_id=42,
        tenant_id="default",
        initial_cash=1_000_000,
    )
    settings = await manager.get_settings(
        user_id=42,
        tenant_id="default",
        default_initial_cash=500_000,
    )
    assert settings["initial_cash"] == 1_000_000
    assert json.loads(redis.client.get("simulation:settings:default:42"))["initial_cash"] == 1_000_000

    account = await manager.init_account(user_id=42, tenant_id="default", initial_cash=2_000_000)
    assert account["cash"] == 2_000_000
    assert json.loads(redis.client.get("simulation:account:default:42"))["cash"] == 2_000_000


@pytest.mark.asyncio
async def test_simulation_manager_account_update_uses_cache_helper():
    redis = _FakeRedis()
    manager = SimulationAccountManager(redis)

    await manager.init_account(user_id=42, tenant_id="default", initial_cash=1_000_000)
    result = await manager.update_balance(
        user_id=42,
        symbol="600000.SH",
        delta_cash=-1000,
        delta_volume=100,
        price=10.0,
        tenant_id="default",
    )

    assert result["success"] is True
    cached = json.loads(redis.client.get("simulation:account:default:42"))
    assert cached["cash"] == 999000
    assert cached["total_asset"] > 0


@pytest.mark.asyncio
async def test_get_account_promotes_legacy_admin_zero_key():
    redis = _FakeRedis()
    manager = SimulationAccountManager(redis)
    redis.client.set(
        "simulation:account:default:0",
        json.dumps({"cash": 1_000_000.0, "positions": {}, "total_asset": 1_000_000.0}),
    )

    account = await manager.get_account(1, tenant_id="default")
    assert account is not None
    assert account["cash"] == 1_000_000.0
    canonical = json.loads(redis.client.get("simulation:account:default:10000001"))
    assert canonical["cash"] == 1_000_000.0


@pytest.mark.asyncio
async def test_get_account_reads_zfill_alias_key():
    redis = _FakeRedis()
    manager = SimulationAccountManager(redis)
    redis.client.set(
        "simulation:account:default:00000001",
        json.dumps({"cash": 1_000_000.0, "positions": {}}),
    )

    account = await manager.get_account(10000001, tenant_id="default")
    assert account is not None
    assert account["cash"] == 1_000_000.0


@pytest.mark.asyncio
async def test_get_account_reconnects_detached_redis(monkeypatch):
    shared = _FakeRedis()
    await SimulationAccountManager(shared).init_account(
        user_id=42, tenant_id="default", initial_cash=1_000_000
    )

    class _Detached:
        client = None

    manager = SimulationAccountManager(_Detached())
    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager._shared_redis",
        lambda: shared.client,
    )
    account = await manager.get_account(42, tenant_id="default")
    assert account is not None
    assert account["cash"] == 1_000_000.0


def test_account_lookup_keys_covers_admin_aliases():
    keys = account_lookup_keys("default", "10000001")
    assert keys[0] == "simulation:account:default:10000001"
    assert "simulation:account:default:00000001" in keys
    assert "simulation:account:default:1" in keys
    assert "simulation:account:default:0" in keys
    assert keys == list(dict.fromkeys(keys))


def test_ledger_user_id_candidates_admin_family_includes_legacy_zero():
    for raw in ("10000001", "00000001", "1", "0", "admin"):
        candidates = ledger_user_id_candidates(raw)
        assert "10000001" in candidates
        assert "0" in candidates
        assert "1" in candidates


def test_ledger_user_id_candidates_does_not_mix_numeric_user_with_admin():
    candidates = ledger_user_id_candidates("42")
    assert candidates[0] == "42"
    assert "0" not in candidates
    assert "10000001" not in candidates


def test_canonical_sim_uid_collapses_admin_family():
    assert canonical_sim_uid("10000001") == 10000001
    assert canonical_sim_uid("00000001") == 10000001
    assert canonical_sim_uid("1") == 10000001
    assert canonical_sim_uid("admin") == 10000001
    assert canonical_sim_uid("0") == 10000001
    assert canonical_sim_uid("42") == 42
    assert canonical_sim_user_suffix(1) == "10000001"
    assert canonical_sim_user_suffix(42) == "42"


def test_require_sim_user_id_maps_admin_family(monkeypatch):
    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.record_sim_sub",
        lambda *args, **kwargs: None,
    )
    assert require_sim_user_id("00000001") == 10000001
    assert require_sim_user_id("admin") == 10000001
    assert require_sim_user_id("10000001") == 10000001
    assert require_sim_user_id("42") == 42
