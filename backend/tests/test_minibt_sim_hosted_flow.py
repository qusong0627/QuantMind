# -*- coding: utf-8 -*-
"""托管调度器 minibt 分支：读目标 → 调 run_cycle（假 Redis + 假执行入口）。"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.services import simulation_hosted_scheduler as sched
from backend.services.simulation.services.rebalance_job_service import (
    SimulationRebalanceJobService,
)
from backend.shared.minibt_sim_keys import target_key

_SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 26, 14, 50, 10, tzinfo=_SH)
TENANT, USER, STRATEGY = "default", "10000001", "minibt_dual_ma"
KEY = f"trade:active_strategy:{TENANT}:{USER}"


class _FakeClient:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttl = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex:
            self.ttl[key] = ex
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    def incr(self, key):
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = str(value)
        return value

    def expire(self, key, seconds):
        self.ttl[key] = seconds
        return True


def _active_payload(runtime="minibt"):
    return {
        "strategy_id": STRATEGY,
        "strategy_name": "minibt·双均线",
        "run_id": "run_x",
        "mode": "SIMULATION",
        "runtime": runtime,
        "started_at": "2026-10-20T09:00:00+08:00",
        "execution_config": {},
        "live_trade_config": {
            "rebalance_days": 3,
            "schedule_type": "interval",
            "enabled_sessions": ["PM"],
            "sell_time": "14:45",
            "buy_time": "14:50",
            "trigger_window_seconds": 90,
            "max_orders_per_cycle": 20,
        },
    }


def _target():
    return {
        "status": "ok",
        "trade_date": "2026-10-26",
        "computed_at": NOW.timestamp() - 5,
        "symbol": "600036.SH",
        "target_shares": 1000,
        "request_id": "req_1",
        "run_id": "run_x",
    }


@pytest.mark.asyncio
async def test_minibt分支把目标传给执行入口并强制每日评估(monkeypatch) -> None:
    client = _FakeClient(
        {
            KEY: json.dumps(_active_payload()),
            target_key(TENANT, USER, STRATEGY): json.dumps(_target()),
        }
    )
    scheduler = sched.SimulationHostedScheduler(SimpleNamespace(client=client))
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "succeeded",
            "signal_count": 1,
            "order_count": 1,
            "filled_count": 1,
        }

    monkeypatch.setattr(sched, "run_simulation_cycle_for_active", fake_run)
    # 运行日志流写的是真实 Redis（db2，租户/用户与生产同键）——测试必须静音，
    # 否则会在用户的「运行日志」面板里留下假条目。
    monkeypatch.setattr(sched, "hosted_sim_log", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_skip_once", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_clear_skip_marker", lambda **kwargs: None)
    monkeypatch.setattr(
        SimulationRebalanceJobService, "ensure_job", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        SimulationRebalanceJobService, "mark_ready", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        SimulationRebalanceJobService, "mark_started", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        SimulationRebalanceJobService, "mark_finished", AsyncMock(return_value=None)
    )

    triggered = await sched.SimulationHostedScheduler._process_key(
        scheduler, KEY, now=NOW
    )
    assert triggered is True
    assert captured["minibt_target"]["target_shares"] == 1000
    # minibt 运行时按日评估：rebalance_days=3 被改成 1
    assert captured["live_trade_config"]["rebalance_days"] == 1


@pytest.mark.asyncio
async def test_目标缺失时不执行且写请求键(monkeypatch) -> None:
    client = _FakeClient({KEY: json.dumps(_active_payload())})
    scheduler = sched.SimulationHostedScheduler(SimpleNamespace(client=client))
    called = []

    async def fake_run(**kwargs):
        called.append(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(sched, "run_simulation_cycle_for_active", fake_run)
    monkeypatch.setattr(sched, "hosted_sim_log", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_skip_once", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_clear_skip_marker", lambda **kwargs: None)
    triggered = await sched.SimulationHostedScheduler._process_key(
        scheduler, KEY, now=NOW
    )
    assert triggered is False
    assert called == []
    assert any(k.startswith("qm:sim:minibt:req:") for k in client.store)


@pytest.mark.asyncio
async def test_买入前预计算只写请求不执行(monkeypatch) -> None:
    client = _FakeClient({KEY: json.dumps(_active_payload())})
    scheduler = sched.SimulationHostedScheduler(SimpleNamespace(client=client))
    called = []

    async def fake_run(**kwargs):
        called.append(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(sched, "run_simulation_cycle_for_active", fake_run)
    monkeypatch.setattr(sched, "hosted_sim_log", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_skip_once", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_clear_skip_marker", lambda **kwargs: None)
    triggered = await sched.SimulationHostedScheduler._process_key(
        scheduler, KEY, now=datetime(2026, 10, 26, 14, 48, 31, tzinfo=_SH)
    )
    assert triggered is False
    assert called == []
    assert any(k.startswith("qm:sim:minibt:req:") for k in client.store)
