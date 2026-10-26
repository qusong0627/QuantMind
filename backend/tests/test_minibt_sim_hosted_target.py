# -*- coding: utf-8 -*-
"""托管调度器：minibt 目标读取 / 请求 / 重试上限（假 Redis，无网络）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.services import simulation_hosted_scheduler as sched
from backend.services.simulation.services.simulation_hosted_scheduler import (
    SimulationHostedScheduler,
)
from backend.shared.minibt_sim_keys import (
    attempts_key,
    request_key,
    target_key,
)

_SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 26, 14, 50, tzinfo=_SH)
TRADE_DATE = "2026-10-26"
TENANT, USER, STRATEGY, RUN_ID = "default", "10000001", "minibt_dual_ma", "run_x"


@pytest.fixture(autouse=True)
def _silence_runtime_log(monkeypatch):
    """运行日志流写真实 Redis（db2 生产键），测试必须静音。"""
    monkeypatch.setattr(sched, "hosted_sim_log", lambda **kwargs: None)


class _FakeClient:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex:
            self.ttl[key] = int(ex)
        return True

    def incr(self, key):
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = str(value)
        return value

    def expire(self, key, seconds):
        self.ttl[key] = int(seconds)
        return True


class _FakeRedis:
    def __init__(self):
        self.client = _FakeClient()


def _scheduler():
    return SimulationHostedScheduler(_FakeRedis())


def _call(scheduler):
    return scheduler._resolve_minibt_target(
        tenant_id=TENANT,
        user_id=USER,
        strategy_id=STRATEGY,
        run_id=RUN_ID,
        trade_date=TRADE_DATE,
        phase="BUY",
        now=NOW,
    )


def _target(**overrides):
    payload = {
        "status": "ok",
        "trade_date": TRADE_DATE,
        "computed_at": NOW.timestamp() - 5,
        "symbol": "600036.SH",
        "target_shares": 1000,
        "request_id": "req_1",
    }
    payload.update(overrides)
    return payload


def test_无目标时发请求并显式跳过() -> None:
    scheduler = _scheduler()
    target, reason = _call(scheduler)
    assert target is None
    assert reason == "minibt_target_pending"
    client = scheduler.redis.client
    assert request_key(TENANT, USER, STRATEGY) in client.store
    request = json.loads(client.store[request_key(TENANT, USER, STRATEGY)])
    assert request["trade_date"] == TRADE_DATE
    assert request["run_id"] == RUN_ID
    assert int(client.store[attempts_key(TENANT, USER, STRATEGY, TRADE_DATE)]) == 1


def test_目标新鲜则直接返回() -> None:
    scheduler = _scheduler()
    scheduler.redis.client.store[target_key(TENANT, USER, STRATEGY)] = json.dumps(
        _target()
    )
    target, reason = _call(scheduler)
    assert reason == "ok"
    assert target and target["target_shares"] == 1000


def test_目标过期与错误分别给出原因() -> None:
    scheduler = _scheduler()
    stale = _target(computed_at=NOW.timestamp() - 999)
    scheduler.redis.client.store[target_key(TENANT, USER, STRATEGY)] = json.dumps(stale)
    _, reason = _call(scheduler)
    assert reason == "minibt_target_stale"

    scheduler = _scheduler()
    error = _target(status="error", error="snapshot_stale:400s")
    scheduler.redis.client.store[target_key(TENANT, USER, STRATEGY)] = json.dumps(error)
    _, reason = _call(scheduler)
    assert reason == "minibt_target_error"


def test_重试三次后不再请求() -> None:
    scheduler = _scheduler()
    client = scheduler.redis.client
    client.store[attempts_key(TENANT, USER, STRATEGY, TRADE_DATE)] = "3"
    target, reason = _call(scheduler)
    assert target is None
    assert reason == "minibt_target_unavailable"
    assert request_key(TENANT, USER, STRATEGY) not in client.store


def test_请求键已存在时不重复覆盖() -> None:
    scheduler = _scheduler()
    client = scheduler.redis.client
    first, _ = _call(scheduler)
    assert first is None
    request_before = client.store[request_key(TENANT, USER, STRATEGY)]
    _call(scheduler)  # 第二拍：请求仍在，NX 失败但原因仍是 pending
    request_after = client.store[request_key(TENANT, USER, STRATEGY)]
    assert request_after == request_before
    assert int(client.store[attempts_key(TENANT, USER, STRATEGY, TRADE_DATE)]) == 2


def test_交易日期与调度日不一致视为不可用() -> None:
    scheduler = _scheduler()
    old_day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    scheduler.redis.client.store[target_key(TENANT, USER, STRATEGY)] = json.dumps(
        _target(trade_date=old_day)
    )
    _, reason = _call(scheduler)
    assert reason == "minibt_target_pending"  # 日期不符 → 重新请求


def test_买入前预计算窗() -> None:
    cfg = {"buy_time": "14:50", "sell_time": "14:45"}
    assert (
        sched._minibt_precompute_due(
            datetime(2026, 10, 26, 14, 48, 31, tzinfo=_SH), cfg
        )
        is True
    )
    assert (
        sched._minibt_precompute_due(datetime(2026, 10, 26, 14, 47, 0, tzinfo=_SH), cfg)
        is False
    )
    assert (
        sched._minibt_precompute_due(datetime(2026, 10, 26, 14, 50, 1, tzinfo=_SH), cfg)
        is False
    )
