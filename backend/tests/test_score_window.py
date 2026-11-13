"""T+N 交易窗口口径金样（``backend/shared/score_window.py``）。

口径唯一真源：D 日分数 → D+lag 执行 → D+lag+N 目标（全部为交易日）。
这些断言就是后端与前端（`electron/src/features/shared/scoreWindow.ts`）的共同金样；
改口径必须先改这里。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.shared.score_window import (
    CalendarUnavailable,
    label_targets_bulk,
    label_window,
    label_window_dates,
    next_trading_days,
    normalize_market,
)


def test_label_window_t5_crosses_national_day_holiday() -> None:
    # 2026-09-30 的下一个交易日是 2026-10-08（国庆休市 10-01..10-07）。
    w = label_window("CN", date(2026, 9, 30), 5)
    assert w.entry_date == date(2026, 10, 8)
    assert w.target_date == date(2026, 10, 15)
    assert len(w.window_dates) == 6
    assert w.window_dates[0] == date(2026, 10, 8)
    assert w.window_dates[-1] == date(2026, 10, 15)


def test_label_window_t20_matches_training_label_contract() -> None:
    # D=2026-11-12（周四）→ 执行 11-13，T+20 目标 12-11（周五），共 21 个日期。
    w = label_window("CN", date(2026, 11, 12), 20)
    assert w.entry_date == date(2026, 11, 13)
    assert w.target_date == date(2026, 12, 11)
    assert len(w.window_dates) == 21
    assert w.data_date == date(2026, 11, 12)


def test_label_window_t1_is_two_dates() -> None:
    w = label_window("CN", date(2026, 11, 12), 1)
    assert w.entry_date == date(2026, 11, 13)
    assert w.target_date == date(2026, 11, 16)  # 跨周末
    assert label_window_dates("CN", date(2026, 11, 12), 1) == [
        date(2026, 11, 13),
        date(2026, 11, 16),
    ]


def test_label_window_never_returns_weekend() -> None:
    for horizon in (1, 5, 20):
        w = label_window("CN", date(2026, 11, 12), horizon)
        for d in w.window_dates:
            assert d.weekday() < 5, f"窗口含周末: {d}"
            assert d > date(2026, 11, 12)


def test_long_horizon_within_calendar_coverage() -> None:
    # 2026-06-01 的 T+60 落在日历覆盖范围内；同时验证 61 个日期全为交易日。
    w = label_window("CN", date(2026, 6, 1), 60)
    assert len(w.window_dates) == 61
    assert all(d.weekday() < 5 for d in w.window_dates)


def test_label_targets_bulk_skips_non_trading_day() -> None:
    out = label_targets_bulk(
        "CN",
        [date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 14)],
        5,
    )
    assert out[date(2026, 11, 12)] == date(2026, 11, 20)
    assert out[date(2026, 11, 13)] == date(2026, 11, 23)
    # 11-14 是周六：不是数据日，目标日返回 None，不猜下一个工作日。
    assert out[date(2026, 11, 14)] is None


def test_label_window_is_market_aware() -> None:
    cn = label_window("CN", date(2026, 11, 12), 5)
    a = label_window("A", date(2026, 11, 12), 5)
    us = label_window("US", date(2026, 11, 12), 5)
    hk = label_window("HK", date(2026, 11, 12), 5)
    assert a == cn
    assert us.target_date != cn.target_date or us.target_date > date(2026, 11, 12)
    assert hk.target_date > date(2026, 11, 12)


def test_next_trading_days_count_and_order() -> None:
    days = next_trading_days("CN", date(2026, 11, 12), 10)
    assert len(days) == 10
    assert days == sorted(days)
    assert days[0] == date(2026, 11, 13)


def test_normalize_market_aliases() -> None:
    assert normalize_market("A") == "CN"
    assert normalize_market("CN") == "CN"
    assert normalize_market("HK") == "HK"
    assert normalize_market("US") == "US"
    assert normalize_market("FUTURES") == "FUTURES"
    with pytest.raises(ValueError):
        normalize_market("MARS")


def test_calendar_unavailable_outside_coverage() -> None:
    with pytest.raises(CalendarUnavailable):
        label_window("CN", date(2099, 1, 1), 5)
