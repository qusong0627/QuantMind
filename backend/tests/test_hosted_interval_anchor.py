"""托管调仓节奏的起算日锚定：非交易日（周末/节假日）启动必须锚到「下一个交易日」。

2026-09-28 修的 bug：``_session_index`` 对起算日也用 ``direction="previous"``，
周末/节假日启动的策略被折算成「上一个交易日启动」——首调仓从下一个交易日推迟到
第 N-1 个交易日（默认 N=3 时周一该跑的拖到周三），管理端「下次窗口」同步错位。

日历背景（XSHG）：2026-09-25（周五）为节假日，09-26/27 周末，下一交易日 = 09-28（周一）；
节后 10-01..10-07 长假，10-08 为假期后第一个交易日。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.services.simulation_hosted_scheduler import (
    _is_interval_rebalance_day,
    _next_scheduled_trigger,
    _normalize_live_trade_config,
    _session_index,
    _should_trigger,
)

TZ = ZoneInfo("Asia/Shanghai")

_NEXT_SESSION = date(2026, 9, 28)  # 周末 + 节假日后的第一个交易日（周一）
_WEEKEND_STARTED = date(2026, 9, 26)  # 周六启动


def _cfg(rebalance_days: int = 3) -> dict:
    return _normalize_live_trade_config(
        {
            "enabled_sessions": ["PM"],
            "sell_time": "14:30",
            "buy_time": "14:40",
            "schedule_type": "interval",
            "rebalance_days": rebalance_days,
            "sell_first": False,
        }
    )


@pytest.mark.unit
def test_session_index_next_maps_weekend_forward():
    """起算日折算方向：next 前推到下一个交易日；缺省（当前交易日语义）仍是 previous。"""
    assert _session_index(date(2026, 9, 27), direction="next") == _session_index(
        _NEXT_SESSION
    )
    assert _session_index(date(2026, 9, 27)) == _session_index(date(2026, 9, 25))


@pytest.mark.unit
def test_non_session_start_anchors_to_next_session():
    """周六/周日/节假日启动 → 起算日 = 下一个交易日，当天即第 0 档。"""
    for started in (date(2026, 9, 25), _WEEKEND_STARTED, date(2026, 9, 27)):
        assert _is_interval_rebalance_day(
            _NEXT_SESSION, started_day=started, rebalance_days=3
        ), f"started_day={started}：第一个交易日必须是第 0 档"


@pytest.mark.unit
def test_trading_day_start_same_day_unchanged():
    """交易日当天启动语义不变：当天第 0 档，次日不是。"""
    assert _is_interval_rebalance_day(
        _NEXT_SESSION, started_day=_NEXT_SESSION, rebalance_days=3
    )
    assert not _is_interval_rebalance_day(
        date(2026, 9, 29), started_day=_NEXT_SESSION, rebalance_days=3
    )


@pytest.mark.unit
def test_weekend_start_cadence_counts_sessions_after_anchor():
    """锚到 09-28 后按交易日计数：09-29/09-30 跳过，第 3 个交易日（10-08）触发。"""
    assert not _is_interval_rebalance_day(
        date(2026, 9, 29), started_day=_WEEKEND_STARTED, rebalance_days=3
    )
    assert not _is_interval_rebalance_day(
        date(2026, 9, 30), started_day=_WEEKEND_STARTED, rebalance_days=3
    )
    assert _is_interval_rebalance_day(
        date(2026, 10, 8), started_day=_WEEKEND_STARTED, rebalance_days=3
    )


@pytest.mark.unit
def test_should_trigger_monday_after_weekend_start():
    """行为面：周六保存的托管配置，周一 BUY 窗口应 matched（修复前是 interval_skip）。"""
    decision = _should_trigger(
        now=datetime(2026, 9, 28, 14, 40, 10, tzinfo=TZ),
        live_trade_config=_cfg(),
        started_day=_WEEKEND_STARTED,
    )
    assert decision.should_trigger is True
    assert decision.reason == "matched"
    assert decision.phase == "BUY"


@pytest.mark.unit
def test_next_window_from_weekend_is_next_trading_day():
    """管理端「下次窗口」：周六看应报 09-28（修复前报 09-30）。"""
    nxt = _next_scheduled_trigger(
        now=datetime(2026, 9, 26, 10, 0, tzinfo=TZ),
        live_trade_config=_cfg(),
        started_day=_WEEKEND_STARTED,
    )
    assert nxt is not None
    assert nxt.trade_date == "2026-09-28"


@pytest.mark.unit
def test_calendar_missing_fallback_rolls_weekend_forward(monkeypatch):
    """日历不可用的兜底分支同样不得回退：周末起算 → 下一个工作日即第 0 档。"""
    from backend.services.simulation.services import simulation_hosted_scheduler as mod

    monkeypatch.setattr(mod, "_session_index", lambda day, market="CN", **kw: None)
    assert mod._is_interval_rebalance_day(
        _NEXT_SESSION, started_day=_WEEKEND_STARTED, rebalance_days=3
    )
    assert not mod._is_interval_rebalance_day(
        date(2026, 9, 29), started_day=_WEEKEND_STARTED, rebalance_days=3
    )
