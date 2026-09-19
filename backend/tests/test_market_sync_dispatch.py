"""市场定时同步派发的回归测试。

锁的是**标记与派发的先后顺序**：last_run 标记同时承担「去重」职责，
先写标记再派发，等于把「打算跑」当成「已经跑」，一次 broker 抖动就能
让当天同步永久失效（2026-09-18 Redis stop-writes 事故即此路径）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from backend.services.engine.tasks import market_sync_scheduler as sched


class _FakeCelery:
    """只记下发过什么，不碰 broker。"""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.fail_for = fail_for or set()

    def send_task(self, name: str, args: list[Any], queue: str) -> None:
        market = args[0]
        if market in self.fail_for:
            raise ConnectionError("broker down")
        self.sent.append(market)


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把调度器的配置与标记替换成可控对象。

    配置里的 `time` 取**当前真实分钟**，这样 `dispatch_due_syncs` 内部的
    `datetime.now()` 无需冻结也必然「到点」——比 mock 掉 datetime 少一层耦合。
    """
    marks: set[str] = set()
    state: dict[str, Any] = {"marks": marks}

    def _fake_get_schedule(_market: str) -> dict[str, Any]:
        return {
            "enabled": True,
            "time": datetime.now().strftime("%H:%M"),
            "days": 5,
            "datasets": [],
            "with_qlib": False,
        }

    monkeypatch.setattr(sched, "get_schedule", _fake_get_schedule)
    monkeypatch.setattr(
        sched, "_last_run_today", lambda market, date_str: (market, date_str) in marks
    )
    monkeypatch.setattr(sched, "_mark_run", lambda market, date_str: marks.add((market, date_str)))
    return state


def _install_celery(monkeypatch: pytest.MonkeyPatch, fake: _FakeCelery) -> None:
    import backend.services.engine.qlib_app.celery_config as cfg

    monkeypatch.setattr(cfg, "celery_app", fake, raising=True)


def test_marks_run_after_successful_dispatch(monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})

    result = sched.dispatch_due_syncs()

    assert fake.sent == ["US"]
    assert result["dispatched"] == ["US"]
    assert {market for market, _date in env["marks"]} == {"US"}, "派发成功后必须写标记"


def test_dispatch_failure_leaves_no_marker_so_next_minute_retries(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """核心回归：派发抛错时，绝不能留下「今天已跑」的假记录。"""
    fake = _FakeCelery(fail_for={"US"})
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})

    result = sched.dispatch_due_syncs()

    assert result["dispatched"] == []
    assert env["marks"] == set(), "派发失败不得写 last_run 标记，否则当天永不重试"


def test_marker_prevents_second_dispatch_same_day(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})

    sched.dispatch_due_syncs()
    second = sched.dispatch_due_syncs()

    assert second["dispatched"] == []
    assert fake.sent == ["US"], "同日不得重复派发"


def test_one_market_failure_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    fake = _FakeCelery(fail_for={"HK"})
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"HK": "QuantHK 港股", "US": "QuantUS 美股"})

    result = sched.dispatch_due_syncs()

    assert result["dispatched"] == ["US"]
    assert {market for market, _date in env["marks"]} == {"US"}, "失败的市场不得被标记"
