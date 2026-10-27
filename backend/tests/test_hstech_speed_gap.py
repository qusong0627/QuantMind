"""`hstech_speed_gap` 三重对齐与量价工具的单元测试（合成数据）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.hstech_speed_gap import (
    _vol_ratio,
    _zscore,
    build_triple_aligned,
)
from backend.scripts.hstech_speed_gap_evidence import wilson


def _frames(a_dates: list[str], hk_dates: list[str], us_dates: list[str]):
    a_idx = pd.DatetimeIndex(a_dates)
    hk_idx = pd.DatetimeIndex(hk_dates)
    us_idx = pd.DatetimeIndex(us_dates)

    hk = pd.DataFrame(
        {
            "HSTECH.HK|open": np.linspace(100, 110, len(hk_idx)),
            "HSTECH.HK|close": np.linspace(101, 111, len(hk_idx)),
            "HSTECH.HK|volume": 1_000_000.0,
            "HSTECH.HK|amount": 1_000_000_000.0,
        },
        index=hk_idx,
    )
    a = pd.DataFrame(index=a_idx)
    for sym in (
        "000016.SH",
        "000300.SH",
        "399006.SZ",
        "000001.SH",
        "399001.SZ",
        "000852.SH",
    ):
        a[f"{sym}|open"] = np.linspace(100, 120, len(a_idx))
        a[f"{sym}|close"] = np.linspace(101, 121, len(a_idx))
        a[f"{sym}|volume"] = 1_000_000.0
        a[f"{sym}|amount"] = 1_000_000.0
    us = pd.DataFrame(
        {"IXIC.US|close": np.linspace(100, 110, len(us_idx))}, index=us_idx
    )
    return hk, a, us


def test_gap_uses_previous_a_close_and_skips_lookahead():
    # Arrange: 连续三个交易日；美股多给一天，保证 1/1 的收益可算
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    hk, a, us = _frames(dates, dates, ["2023-12-29", *dates])
    a["000300.SH|open"] = [100.0, 105.0, 115.0]
    a["000300.SH|close"] = [100.0, 110.0, 121.0]

    # Act
    al = build_triple_aligned(hk, a, us, ("000300.SH",))

    # Assert: 1/3 的跳空 = log(115/110)，不是 log(115/121)
    assert al.loc[pd.Timestamp("2024-01-03"), "gap_000300.SH"] == pytest.approx(
        np.log(115.0 / 110.0)
    )
    assert al.loc[pd.Timestamp("2024-01-03"), "signal_date"] == pd.Timestamp(
        "2024-01-02"
    )


def test_excludes_a_holiday_when_hk_traded_between():
    # Arrange: A 股 1/3 休市，港股 1/3 照常交易
    a_dates = ["2024-01-01", "2024-01-02", "2024-01-04"]
    hk_dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    us_dates = ["2023-12-29", "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    hk, a, us = _frames(a_dates, hk_dates, us_dates)

    # Act
    al = build_triple_aligned(hk, a, us, ("000300.SH",))

    # Assert: 1/4 的上一 A 股日是 1/2，但港股 1/3 已交易 → 不是干净的隔夜信号
    assert pd.Timestamp("2024-01-04") not in al.index
    assert pd.Timestamp("2024-01-02") in al.index


def test_us_uses_last_session_before_a_open():
    # Arrange: A 股 1/2 开盘前，只有美股 1/1 的收盘可知
    a_dates = ["2024-01-01", "2024-01-02"]
    hk_dates = a_dates
    us_dates = ["2023-12-29", "2024-01-01", "2024-01-02"]
    hk, a, us = _frames(a_dates, hk_dates, us_dates)
    us["IXIC.US|close"] = [100.0, 100.0, 200.0]

    # Act
    al = build_triple_aligned(hk, a, us, ("000300.SH",))

    # Assert: 1/2 用 1/1 的美股收益（0%），不能用 1/2 当天（+100%）
    assert al.loc[pd.Timestamp("2024-01-02"), "us"] == pytest.approx(0.0)


def test_vol_ratio_uses_prior_median_not_current_value():
    # Arrange
    amount = pd.Series([10.0, 10.0, 10.0, 50.0])

    # Act
    ratio = _vol_ratio(amount, window=2)

    # Assert: 最后一日 = 50 / median(前两日=10)，不是 50 / median(10,10,50)
    assert ratio.iloc[-1] == pytest.approx(5.0)


def test_zscore_clips_extremes_for_volatility_estimate():
    # Arrange: 前 30 天 +1%，第 31 天 +50%，之后 +1%；最后一天的波动率窗口包含极端日
    x = pd.Series([0.01] * 30 + [0.5] + [0.01] * 29)
    clipped = x.clip(-0.10, 0.10)

    # Act
    z = _zscore(x, window=60)

    # Assert: z 的分子用原始 0.01，分母用缩尾后的标准差（0.5 被压到 0.1）
    expected_sd = clipped.iloc[:-1].std()
    raw_sd = x.iloc[:-1].std()
    assert z.iloc[-1] == pytest.approx(0.01 / expected_sd)
    assert expected_sd < raw_sd


def test_wilson_interval_contains_point_estimate():
    # Arrange: 50 次里 41 次命中 = 82%
    lo, hi = wilson(41, 50)

    # Assert: 区间包含 82%，且明显宽于 0（n=50 的抽样误差）
    assert lo < 0.82 < hi
    assert 0.65 < lo < 0.75
    assert hi < 0.95
