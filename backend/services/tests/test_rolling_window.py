"""滚动重训窗口计算单测（纯函数，无 DB/网络依赖）。"""

from datetime import date, timedelta

import pytest

from backend.shared.rolling_window import (
    RollingWindowError,
    add_months,
    compute_rolled_split,
    months_between,
)


def _trading_days():
    """2016-01-04 ~ 2026-09-30 的全部工作日（模拟探针交易日序列）。"""
    days = []
    d = date(2016, 1, 4)
    end = date(2026, 9, 30)
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


ORIG = {
    "train_start": "2016-01-04",
    "train_end": "2024-12-31",
    "valid_start": "2025-01-02",
    "valid_end": "2025-12-31",
    "test_start": "2026-01-05",
    "test_end": "2026-08-31",
}
KEYS = (
    "train_start",
    "train_end",
    "valid_start",
    "valid_end",
    "test_start",
    "test_end",
)


def test_add_months_clamps_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 8, 31), 1) == date(2026, 9, 30)
    assert months_between(date(2026, 8, 31), date(2026, 9, 11)) == 1


def test_auto_shift_whole_window_by_latest_month():
    # 数据末端 2026-09-11：auto 步长 1 个月，六边界整体平移
    new_window, shift, warnings = compute_rolled_split(
        ORIG, _trading_days(), "2026-09-11"
    )
    assert shift == 1
    # 例：原 2016-01 ~ 2026-08 → 新 2016-02 ~ 2026-09
    assert new_window["train_start"] == "2016-02-04"  # 周四，本身是交易日
    assert new_window["train_end"] == "2025-01-31"  # 周五，本身是交易日
    assert new_window["test_start"] == "2026-02-05"  # 周四，本身是交易日
    assert new_window["test_end"] == "2026-09-30"  # 2026-08-31+1月，吸附
    # 2025-02-02 是周日 → 吸附到周一并告警（节假日吸附路径）
    assert new_window["valid_start"] == "2025-02-03"
    assert any("valid_start" in w for w in warnings)
    order = [new_window[k] for k in KEYS]
    assert order[0] <= order[1] < order[2] <= order[3] < order[4] <= order[5]


def test_explicit_shift_two_months():
    new_window, shift, _ = compute_rolled_split(
        ORIG, _trading_days(), "2026-09-11", shift_months=2
    )
    assert shift == 2
    assert new_window["train_start"].startswith("2016-03")


def test_up_to_date_returns_409():
    with pytest.raises(RollingWindowError) as exc_info:
        compute_rolled_split(ORIG, _trading_days(), "2026-08-31")
    assert exc_info.value.http_status == 409


def test_bad_input_rejected():
    with pytest.raises(RollingWindowError):
        compute_rolled_split(ORIG, _trading_days(), "2026-09-11", shift_months=0)
    with pytest.raises(RollingWindowError):
        compute_rolled_split(
            {**ORIG, "test_end": "不是日期"}, _trading_days(), "2026-09-11"
        )
    with pytest.raises(RollingWindowError):
        compute_rolled_split(ORIG, [], "2026-09-11")
