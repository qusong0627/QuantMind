"""策略监控推送源（trade 侧 producer）纯函数单测。

只测不依赖 Redis / DB 的部分：开关、间隔、变化指纹。
"""

from __future__ import annotations

import pytest

from backend.services.trade.services import strategy_monitor_pusher as smp


def test_push_enabled_default_true(monkeypatch):
    monkeypatch.delenv("SIM_STRATEGY_PUSH_ENABLED", raising=False)
    assert smp.push_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", "Off"])
def test_push_enabled_false_values(monkeypatch, raw):
    monkeypatch.setenv("SIM_STRATEGY_PUSH_ENABLED", raw)
    assert smp.push_enabled() is False


def test_push_interval_clamped_and_default(monkeypatch):
    monkeypatch.setenv("SIM_STRATEGY_PUSH_INTERVAL_SECONDS", "1")
    assert smp.push_interval_seconds() == 5
    monkeypatch.setenv("SIM_STRATEGY_PUSH_INTERVAL_SECONDS", "abc")
    assert smp.push_interval_seconds() == 30
    monkeypatch.delenv("SIM_STRATEGY_PUSH_INTERVAL_SECONDS", raising=False)
    assert smp.push_interval_seconds() == 30


def test_fingerprint_ignores_non_display_fields():
    base = {
        "tenant_id": "default",
        "sim_user_id": "0",
        "market": "CN",
        "total_asset": 991403.31,
        "today_pnl": -171.0,
        "today_return": -0.0172,
        "total_pnl": -8596.69,
        "total_return": -0.8597,
    }
    # 非展示字段变化不应触发推送
    assert smp._fingerprint(base) == smp._fingerprint(dict(base, market="HK"))
    # 展示字段变化必须触发推送
    assert smp._fingerprint(base) != smp._fingerprint(dict(base, today_pnl=-172.0))
    assert smp._fingerprint(base) != smp._fingerprint(dict(base, total_asset=1.0))


def test_pusher_interval_is_clamped():
    pusher = smp.StrategyMonitorPusher(redis=None, interval_seconds=1)
    assert pusher.interval_seconds == 5
