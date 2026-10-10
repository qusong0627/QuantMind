"""到点判据（审计 H4）的单测。

语义：`now >= 配置时刻` 即到点；迟到分钟/小时仍执行（当日去重由各调度器
的 last_run 日键负责，见各自派发测试）。反向守卫：早于配置时刻不得提前跑。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.services.engine.tasks import schedule_gate as gate
from backend.services.engine.tasks.schedule_gate import no_data_window, not_due_yet


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 10, hour, minute, 30)


def test_before_the_configured_minute_is_not_due() -> None:
    assert not_due_yet(_at(4, 29), "04:30") is True


def test_the_exact_minute_is_due() -> None:
    assert not_due_yet(_at(4, 30), "04:30") is False


def test_late_by_minutes_is_still_due() -> None:
    """H4 核心：worker 忙过 60s 迟到 5 分钟，仍要到点（旧 `==` 判据整天静默跳发）。"""
    assert not_due_yet(_at(4, 35), "04:30") is False


def test_late_by_hours_is_still_due_for_same_day_catch_up() -> None:
    """重启补齐：过点数小时仍是「今天没跑过」的当日首次到点。"""
    assert not_due_yet(_at(9, 10), "04:30") is False


def test_non_padded_time_is_compared_by_value_not_by_string() -> None:
    """非补零写法 "4:30" 是 strptime 合法且 _normalize 原样保留的配置：

    字符串比较下 ``"04:35" < "4:30"`` 为真（'0' < '4'），会把已过点的
    04:35 误判成「未到点」；元组比较按数值判断。
    """
    assert not_due_yet(_at(4, 29), "4:30") is True
    assert not_due_yet(_at(4, 35), "4:30") is False


def test_garbage_time_fails_closed() -> None:
    """解析失败宁可不跑：跑一个看不懂的时刻可能把凌晨任务按白天数据执行。"""
    assert not_due_yet(_at(4, 35), "") is True
    assert not_due_yet(_at(4, 35), "abc") is True
    assert not_due_yet(_at(4, 35), "04-30") is True


# ── 交易日历门（P2-3 / 审计 M7）──────────────────────────────────────────


def _stub_calendar(monkeypatch, table: dict[str, bool | None]):
    """按日期表换掉日历探针；表外日期探不到（None）。返回调用记录供断言。"""
    calls: list[tuple[str, str]] = []

    def fake(calendar_market: str, day) -> bool | None:
        calls.append((calendar_market, day.isoformat()))
        return table.get(day.isoformat())

    monkeypatch.setattr(gate, "_probe_trading_day", fake)
    return calls


def test_sunday_skips_when_saturday_is_also_closed(monkeypatch) -> None:
    """2026-10-11 是周日：昨天（周六）与今天都不是交易日 → 无新数据可同步。"""
    _stub_calendar(monkeypatch, {"2026-10-10": False, "2026-10-11": False})
    reason = no_data_window("A", datetime(2026, 10, 11, 1, 0, 30))
    assert reason is not None
    assert "2026-10-10" in reason
    assert "2026-10-11" in reason


def test_saturday_morning_still_fetches_fridays_close(monkeypatch) -> None:
    """过夜同步取上一交易日数据：周六凌晨补周五收盘（昨日是交易日）绝不跳。"""
    _stub_calendar(monkeypatch, {"2026-10-09": True, "2026-10-10": False})
    assert no_data_window("A", datetime(2026, 10, 10, 1, 0, 30)) is None


def test_holiday_interior_day_skips(monkeypatch) -> None:
    """长假内部（前日/今日都非交易日）→ 跳。"""
    _stub_calendar(monkeypatch, {"2026-10-05": False, "2026-10-06": False})
    reason = no_data_window("A", datetime(2026, 10, 6, 3, 0, 30))
    assert reason is not None


def test_first_day_back_from_holiday_runs(monkeypatch) -> None:
    """节后首日（今日是交易日）→ 放行（幂等增量，空跑代价远小于误拦）。"""
    _stub_calendar(monkeypatch, {"2026-10-07": False, "2026-10-08": True})
    assert no_data_window("A", datetime(2026, 10, 8, 1, 0, 30)) is None


@pytest.mark.parametrize(
    "table",
    [
        {"2026-10-10": None, "2026-10-11": None},  # 日历整体答不了
        {"2026-10-10": False, "2026-10-11": None},  # 一侧答不了
        {"2026-10-10": None, "2026-10-11": False},
    ],
)
def test_unanswerable_calendar_fails_open(monkeypatch, table) -> None:
    """日历答不了 = 放行：此闸只许省掉注定为空的空跑，绝不许拦掉可能有新数据的班次。"""
    _stub_calendar(monkeypatch, table)
    assert no_data_window("A", datetime(2026, 10, 11, 1, 0, 30)) is None


@pytest.mark.parametrize(
    ("market", "calendar_market"),
    [
        ("A", "CN"),
        ("CUSTOM", "CN"),  # CUSTOM 重建的是 A 股派生数据集，同 A 股口径
        ("FUTURES", "CFFEX"),  # 国内期货与 A 股同一套法定节假日
        ("HK", "HK"),
        ("US", "US"),
        ("us", "US"),  # 大小写不敏感
    ],
)
def test_market_is_probed_with_its_calendar(
    monkeypatch, market: str, calendar_market: str
) -> None:
    calls = _stub_calendar(monkeypatch, {"2026-10-09": True, "2026-10-10": False})
    assert no_data_window(market, datetime(2026, 10, 10, 4, 0, 30)) is None
    assert {m for m, _ in calls} == {calendar_market}
    assert {d for _, d in calls} == {"2026-10-09", "2026-10-10"}


def test_always_on_market_has_no_gate(monkeypatch) -> None:
    """BC（加密货币全天候）不设日历门，探针一次都不调。"""
    calls = _stub_calendar(monkeypatch, {})
    assert no_data_window("BC", datetime(2026, 10, 11, 4, 15, 30)) is None
    assert calls == []


def test_unknown_market_fails_open(monkeypatch) -> None:
    calls = _stub_calendar(monkeypatch, {})
    assert no_data_window("XYZ", datetime(2026, 10, 11, 4, 15, 30)) is None
    assert calls == []
