"""SimulationAccountManager 的 T+1 可卖量 Lua 脚本测试。

这些用例直接对真实 Redis 执行脚本本体（从源文件抽取），而不是复写一份 Python
等价实现——Lua 里的 nil 语义、cjson 空表退化等问题只有真实执行才暴露得出来。

需要一个可达的 Redis，否则整体 skip。可用 QM_TEST_REDIS_HOST 指定主机。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT))

MANAGER_SOURCE = (
    PROJECT_ROOT
    / "backend"
    / "services"
    / "trade_shared"
    / "simulation_manager.py"
)

_CANDIDATE_HOSTS = (
    os.environ.get("QM_TEST_REDIS_HOST"),
    "redis",
    "quantmind-redis",
    "localhost",
)


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
    pytest.skip(f"无可用 Redis，跳过 T+1 Lua 测试: {last_error}")


@pytest.fixture(scope="module")
def update_lua() -> str:
    return _extract_lua("_update_balance_lua")


@pytest.fixture(scope="module")
def unlock_lua() -> str:
    return _extract_lua("_unlock_t1_lua")


@pytest.fixture
def account_key(redis_client):
    import uuid

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


def _position(redis_client, key: str, symbol: str) -> dict:
    return _read(redis_client, key)["positions"][symbol]


@pytest.mark.unit
@pytest.mark.message_broker
def test_legacy_position_without_available_volume_is_sellable(
    redis_client, update_lua, account_key
):
    """T+1 上线前写入的持仓没有 available_volume，必须视为全部可卖。

    这是本次改动最危险的回归点：缺少 nil 回退会让存量用户的持仓永久无法卖出，
    且不会抛任何错误。
    """
    _seed(
        redis_client,
        account_key,
        {
            "cash": 1000.0,
            "total_asset": 11000.0,
            "market_value": 10000.0,
            "positions": {
                # 故意不写 available_volume，模拟存量数据
                "600519.SH": {
                    "volume": 1000,
                    "cost": 10.0,
                    "price": 10.0,
                    "market_value": 10000.0,
                }
            },
        },
    )

    result = _run(redis_client, update_lua, account_key, "600519.SH", "5000", "-500", "10")

    assert result["success"] is True
    position = _position(redis_client, account_key, "600519.SH")
    assert position["volume"] == 500
    assert position["available_volume"] == 500


@pytest.mark.unit
@pytest.mark.message_broker
def test_same_day_buy_is_not_sellable(redis_client, update_lua, account_key):
    _seed(
        redis_client,
        account_key,
        {"cash": 100000.0, "total_asset": 100000.0, "market_value": 0.0, "positions": {}},
    )

    buy = _run(redis_client, update_lua, account_key, "600519.SH", "-10000", "1000", "10")
    assert buy["success"] is True
    assert _position(redis_client, account_key, "600519.SH")["available_volume"] == 0

    sell = _run(redis_client, update_lua, account_key, "600519.SH", "5000", "-500", "10")
    assert sell["success"] is False
    assert sell["reason"] == "INSUFFICIENT_AVAILABLE_VOLUME"


@pytest.mark.unit
@pytest.mark.message_broker
def test_unlock_t1_makes_holdings_sellable(
    redis_client, update_lua, unlock_lua, account_key
):
    _seed(
        redis_client,
        account_key,
        {"cash": 100000.0, "total_asset": 100000.0, "market_value": 0.0, "positions": {}},
    )
    _run(redis_client, update_lua, account_key, "600519.SH", "-10000", "1000", "10")

    unlocked = _run(redis_client, unlock_lua, account_key)
    assert unlocked["success"] is True
    assert unlocked["unlocked"] == 1
    assert _position(redis_client, account_key, "600519.SH")["available_volume"] == 1000

    sell = _run(redis_client, update_lua, account_key, "600519.SH", "5000", "-500", "10")
    assert sell["success"] is True


@pytest.mark.unit
@pytest.mark.message_broker
def test_only_unlocked_portion_is_sellable(
    redis_client, update_lua, unlock_lua, account_key
):
    """跨日累积：第一天买入解锁后，第二天新买入的部分仍受 T+1 约束。"""
    _seed(
        redis_client,
        account_key,
        {"cash": 100000.0, "total_asset": 100000.0, "market_value": 0.0, "positions": {}},
    )
    _run(redis_client, update_lua, account_key, "600519.SH", "-10000", "1000", "10")
    _run(redis_client, unlock_lua, account_key)
    _run(redis_client, update_lua, account_key, "600519.SH", "-5000", "500", "10")

    position = _position(redis_client, account_key, "600519.SH")
    assert position["volume"] == 1500
    assert position["available_volume"] == 1000

    ok = _run(redis_client, update_lua, account_key, "600519.SH", "10000", "-1000", "10")
    assert ok["success"] is True

    blocked = _run(redis_client, update_lua, account_key, "600519.SH", "10", "-1", "10")
    assert blocked["success"] is False
    assert blocked["reason"] == "INSUFFICIENT_AVAILABLE_VOLUME"


@pytest.mark.unit
@pytest.mark.message_broker
def test_oversell_beyond_holdings_is_rejected(redis_client, update_lua, account_key):
    _seed(
        redis_client,
        account_key,
        {
            "cash": 0.0,
            "total_asset": 10000.0,
            "market_value": 10000.0,
            "positions": {
                "600519.SH": {
                    "volume": 1000,
                    "available_volume": 1000,
                    "cost": 10.0,
                    "price": 10.0,
                    "market_value": 10000.0,
                }
            },
        },
    )

    result = _run(redis_client, update_lua, account_key, "600519.SH", "99990", "-9999", "10")
    assert result["success"] is False
    assert result["reason"] == "INSUFFICIENT_HOLDINGS"


@pytest.mark.unit
@pytest.mark.message_broker
def test_insufficient_cash_is_rejected(redis_client, update_lua, account_key):
    _seed(
        redis_client,
        account_key,
        {"cash": 100.0, "total_asset": 100.0, "market_value": 0.0, "positions": {}},
    )

    result = _run(redis_client, update_lua, account_key, "600519.SH", "-10000", "1000", "10")
    assert result["success"] is False
    assert result["reason"] == "INSUFFICIENT_CASH"
