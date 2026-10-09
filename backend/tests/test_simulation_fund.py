"""``SimulationAccountManager.fund_execution_cash`` 与注资 Lua 的测试。

两层（同 ``test_simulation_t1_lua.py`` 纪律）：

1. **调用面**（假 redis）：金额守卫、键/参数形状、payload 解析、异常映射——
   全部不碰网络；
2. **脚本本体**（真 Redis，不可达则整体 skip）：从源文件抽取 ``_fund_cash_lua``
   实际执行——cjson 对 nil 字段的退化、字符串数字转换只有真跑才暴露得出来。

背景（2026-10-09 实盘事故）：决策轮买单被 Lua 现金守卫拒 ``INSUFFICIENT_CASH``
——守卫只读执行户 ``cash``，而 sizing 用桥户/分账口径，量级差 ~200 倍。注资是
「执行前把执行户补到本轮地板」的唯一原子原语。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT))

MANAGER_SOURCE = (
    PROJECT_ROOT / "backend" / "services" / "trade_shared" / "simulation_manager.py"
)

_CANDIDATE_HOSTS = (
    os.environ.get("QM_TEST_REDIS_HOST"),
    "redis",
    "quantmind-redis",
    "localhost",
)


# ── 1. 调用面（假 redis）─────────────────────────────────────────────


class _EvalRecorder:
    """记录 eval 调用形状的假客户端；``get`` 恒 None（读缓存全部未命中）。"""

    def __init__(self, result) -> None:
        self.calls: list[tuple] = []
        self._result = result

    def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, *args))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def get(self, key):
        return None


def _manager(result):
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    client = _EvalRecorder(result)
    return SimulationAccountManager(SimpleNamespace(client=client)), client


@pytest.mark.asyncio
async def test_fund_rejects_nonpositive_or_nonfinite_amounts_without_eval() -> None:
    """金额 ≤0 / NaN / Inf 直接拒（不 eval）：入金原语绝不猜符号。"""
    for bad in (0.0, -5.0, float("nan"), float("inf")):
        manager, client = _manager('{"success": true}')
        res = await manager.fund_execution_cash(10000001, bad)
        assert res == {"success": False, "reason": "INVALID_AMOUNT"}, bad
        assert client.calls == []


@pytest.mark.asyncio
async def test_fund_evals_the_lua_with_canonical_key_and_string_amount() -> None:
    from backend.shared.simulation_account_keys import (
        account_key,
        canonical_sim_user_suffix,
    )

    manager, client = _manager('{"success": true, "cash": 123.45}')
    res = await manager.fund_execution_cash(10000001, 250.0, market="CN")
    assert res == {"success": True, "cash": 123.45}
    script, numkeys, key, amount = client.calls[0]
    assert script is manager._fund_cash_lua
    assert numkeys == 1
    assert key == account_key("default", canonical_sim_user_suffix(10000001), "CN")
    assert amount == "250.0"


@pytest.mark.asyncio
async def test_fund_passes_through_the_lua_rejection_payload() -> None:
    """账户不存在等拒因**原样透出**（调用方要按 reason 分流，不是笼统失败）。"""
    manager, _ = _manager('{"success": false, "reason": "ACCOUNT_NOT_FOUND"}')
    res = await manager.fund_execution_cash(10000001, 10.0)
    assert res == {"success": False, "reason": "ACCOUNT_NOT_FOUND"}


@pytest.mark.asyncio
async def test_fund_maps_an_eval_exception_to_fund_failed() -> None:
    manager, _ = _manager(RuntimeError("connection reset"))
    res = await manager.fund_execution_cash(10000001, 10.0)
    assert res == {"success": False, "reason": "FUND_FAILED"}


@pytest.mark.asyncio
async def test_fund_reports_redis_unavailable_without_a_client(monkeypatch) -> None:
    import backend.services.trade_shared.simulation_manager as sm

    monkeypatch.setattr(sm, "_shared_redis", lambda: None)
    manager = sm.SimulationAccountManager(SimpleNamespace(client=None))
    res = await manager.fund_execution_cash(10000001, 10.0)
    assert res == {"success": False, "reason": "REDIS_UNAVAILABLE"}


# ── 1b. ensure_cash_floor：读账 → 算缺口 → 入金（锁纪律在 manager 内）───


def _floor_manager(monkeypatch, *, snapshot, result='{"success": true}'):
    """锁降级透传（``_shared_redis`` 无连接 → token ""）+ 账户缓存恒命中/恒缺。"""
    import backend.services.trade_shared.simulation_manager as sm

    monkeypatch.setattr(sm, "_shared_redis", lambda: None)
    manager, client = _manager(result)
    monkeypatch.setattr(manager, "_pick_cached_account", lambda *a, **k: snapshot)
    return manager, client


@pytest.mark.asyncio
async def test_ensure_floor_funds_exactly_the_deficit(monkeypatch) -> None:
    """缺口 = 地板 − 现现金（锁内现读）：只补差额，不多打钱。"""
    manager, client = _floor_manager(
        monkeypatch,
        snapshot={"cash": 100.0},
        result='{"success": true, "cash": 1000.0}',
    )
    res = await manager.ensure_cash_floor(10000001, 1000.0)
    assert res["success"] is True
    assert res["funded"] == pytest.approx(900.0)
    assert res["cash_before"] == pytest.approx(100.0)
    assert res["snapshot"] == {"cash": 100.0}
    _script, _n, _key, amount = client.calls[0]
    assert amount == "900.0"


@pytest.mark.asyncio
async def test_ensure_floor_skips_when_cash_already_covers(monkeypatch) -> None:
    """现金已够地板：不 eval（宁可留着上轮余量，也不做无意义入金）。"""
    manager, client = _floor_manager(monkeypatch, snapshot={"cash": 2000.0})
    res = await manager.ensure_cash_floor(10000001, 1000.0)
    assert res["success"] is True
    assert res["funded"] == 0.0
    assert res["cash"] == pytest.approx(2000.0)
    assert res["snapshot"] == {"cash": 2000.0}
    assert client.calls == []


@pytest.mark.asyncio
async def test_ensure_floor_respects_min_deficit(monkeypatch) -> None:
    """缺口 ≤ min_deficit 视为已够：分钱级缺口不值得一条台账行。"""
    manager, client = _floor_manager(monkeypatch, snapshot={"cash": 999.5})
    res = await manager.ensure_cash_floor(10000001, 1000.0, min_deficit=1.0)
    assert res["success"] is True
    assert res["funded"] == 0.0
    assert client.calls == []


@pytest.mark.asyncio
async def test_ensure_floor_reports_a_missing_account_without_funding(
    monkeypatch,
) -> None:
    """账户不存在 → 拒且不 eval（账都不在，入金只会掩盖建账断链）。"""
    manager, client = _floor_manager(monkeypatch, snapshot=None)

    async def _no_rebuild(*a, **k):
        return None

    monkeypatch.setattr(manager, "_rebuild_from_ledger", _no_rebuild)
    res = await manager.ensure_cash_floor(10000001, 1000.0)
    assert res == {"success": False, "reason": "ACCOUNT_NOT_FOUND"}
    assert client.calls == []


# ── 2. 脚本本体（真 Redis）──────────────────────────────────────────


def _extract_lua(attr: str) -> str:
    source = MANAGER_SOURCE.read_text()
    match = re.search(rf'self\.{attr} = """(.*?)"""', source, re.S)
    assert match, f"未能从 simulation_manager.py 抽取 {attr}"
    return match.group(1)


@pytest.fixture(scope="module")
def redis_client():
    redis = pytest.importorskip("redis", reason="需要 redis-py 才能执行 Lua 脚本")
    last_error: Exception | None = None
    for host in _CANDIDATE_HOSTS:
        if not host:
            continue
        try:
            client = redis.Redis(host=host, port=6379, socket_connect_timeout=2)
            if client.ping():
                return client
        except Exception as exc:  # noqa: BLE001 - 逐个候选主机试探
            last_error = exc
    pytest.skip(f"无可用 Redis，跳过注资 Lua 测试: {last_error}")


@pytest.fixture(scope="module")
def fund_lua() -> str:
    return _extract_lua("_fund_cash_lua")


@pytest.fixture
def account_key(redis_client):
    key = f"simulation:account:pytest:{uuid.uuid4().hex[:8]}"
    yield key
    redis_client.delete(key)


def _run(redis_client, script: str, key: str, *argv: str) -> dict:
    raw = redis_client.eval(script, 1, key, *argv)
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


def _seed(redis_client, key: str, account: dict) -> None:
    redis_client.set(key, json.dumps(account))


def _read(redis_client, key: str) -> dict:
    raw = redis_client.get(key)
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


@pytest.mark.unit
@pytest.mark.message_broker
def test_fund_lua_adds_cash_and_keeps_positions(redis_client, fund_lua, account_key):
    """入金：cash/available_cash 同增、total_asset 重算、持仓与市值原样。"""
    _seed(
        redis_client,
        account_key,
        {
            "cash": 100.0,
            "available_cash": 100.0,
            "total_asset": 1100.0,
            "market_value": 1000.0,
            "positions": {
                "600519.SH": {
                    "volume": 100,
                    "available_volume": 100,
                    "cost": 10.0,
                    "price": 10.0,
                    "market_value": 1000.0,
                }
            },
        },
    )

    result = _run(redis_client, fund_lua, account_key, "900")

    assert result["success"] is True
    assert result["cash"] == pytest.approx(1000.0)
    account = _read(redis_client, account_key)
    assert account["cash"] == pytest.approx(1000.0)
    assert account["available_cash"] == pytest.approx(1000.0)
    assert account["market_value"] == pytest.approx(1000.0)
    assert account["total_asset"] == pytest.approx(2000.0)
    assert account["positions"]["600519.SH"]["volume"] == 100


@pytest.mark.unit
@pytest.mark.message_broker
def test_fund_lua_does_not_create_a_missing_account(redis_client, fund_lua):
    """账户不存在 → 拒且**不建键**：给一条不存在的账注资会把建账断链掩盖成「钱到位」。"""
    key = f"simulation:account:pytest:absent-{uuid.uuid4().hex[:8]}"
    result = _run(redis_client, fund_lua, key, "500")
    assert result == {"success": False, "reason": "ACCOUNT_NOT_FOUND"}
    assert redis_client.exists(key) == 0


@pytest.mark.unit
@pytest.mark.message_broker
def test_fund_lua_rejects_nonpositive_amounts(redis_client, fund_lua, account_key):
    """Lua 层守卫（Python 层之外的第二道）：0/负金额不落账。"""
    _seed(
        redis_client,
        account_key,
        {"cash": 100.0, "total_asset": 100.0, "market_value": 0.0, "positions": {}},
    )
    for bad in ("0", "-5"):
        result = _run(redis_client, fund_lua, account_key, bad)
        assert result == {"success": False, "reason": "INVALID_AMOUNT"}, bad
    assert _read(redis_client, account_key)["cash"] == pytest.approx(100.0)


@pytest.mark.unit
@pytest.mark.message_broker
def test_fund_lua_does_not_invent_available_cash_when_absent(
    redis_client, fund_lua, account_key
):
    """旧形态账户没有 ``available_cash`` 字段 → 注入后仍不凭空造（字段缺席≠0）。"""
    _seed(
        redis_client,
        account_key,
        {"cash": 100.0, "total_asset": 1100.0, "market_value": 1000.0, "positions": {}},
    )

    result = _run(redis_client, fund_lua, account_key, "900")

    assert result["success"] is True
    account = _read(redis_client, account_key)
    assert "available_cash" not in account
    assert account["cash"] == pytest.approx(1000.0)
    assert account["total_asset"] == pytest.approx(2000.0)
