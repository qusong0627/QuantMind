"""`hstech_pricing_power` 公式语义的单元测试（合成数据，不读真实行情）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.hstech_pricing_power import (
    A_SYMBOLS,
    _block,
    _episodes,
    build_features,
    build_signals,
    clean_mask,
    forward_frame,
)


def _make_frame(n: int = 80, a_rising: bool = True) -> pd.DataFrame:
    """构造一个 HK 与 6 个 A 股指数都齐全的最小宽表。"""
    idx = pd.bdate_range("2024-01-01", periods=n)
    data: dict[str, pd.Series] = {}
    hk_close = pd.Series(100.0 * np.power(1.01, np.arange(n)), index=idx)
    data["HSTECH.HK|close"] = hk_close
    data["HSTECH.HK|volume"] = pd.Series(
        1_000_000.0 * np.power(1.005, np.arange(n)), index=idx
    )
    data["HSTECH.HK|amount"] = pd.Series(1_000_000_000.0, index=idx)
    for i, sym in enumerate(A_SYMBOLS):
        step = 1.0 if a_rising else -1.0
        base = 3000.0 + i * 100
        data[f"{sym}|close"] = pd.Series(
            base * np.power(1.0 + step * 0.002, np.arange(n)), index=idx
        )
        data[f"{sym}|volume"] = pd.Series(5_000_000.0, index=idx)
    return pd.DataFrame(data, index=idx)


def test_formula_fires_on_persistent_hk_and_a_uptrend():
    # Arrange
    df = _make_frame(n=80, a_rising=True)

    # Act
    feat = build_features(df, span=5)
    sig = build_signals(feat)["formula"]

    # Assert: 热身期后应持续满足共振 + 趋势 + 拐头
    assert feat["res_up"].tail(20).all()
    assert feat["trend_norm"].tail(20).all()
    assert feat["turn_norm"].tail(20).all()
    assert sig.tail(20).all()
    assert not sig.head(19).any()


def test_formula_blocked_when_a_shares_fall():
    # Arrange: 恒生仍上涨，但 A 股全跌 —— 共振不成立
    df = _make_frame(n=80, a_rising=False)

    # Act
    feat = build_features(df, span=5)
    sig = build_signals(feat)["formula"]

    # Assert
    assert feat["trend_norm"].tail(20).all()
    assert not feat["res_up"].tail(20).any()
    assert not sig.any()


def test_clean_mask_flags_a_holiday_gap():
    # Arrange: 港股 1/1~1/5 都开市，A 股 1/2~1/3 休市
    hk_index = pd.bdate_range("2024-01-01", "2024-01-08")
    common = pd.DatetimeIndex(["2024-01-01", "2024-01-04", "2024-01-05", "2024-01-08"])

    # Act
    mask = clean_mask(hk_index, common)

    # Assert: 1/4 之前港股单独交易了 1/2、1/3 两天 → 不是干净日
    assert mask.loc["2024-01-01"] is False or not mask.loc["2024-01-01"]
    assert not mask.loc[pd.Timestamp("2024-01-04")]
    assert mask.loc[pd.Timestamp("2024-01-05")]
    assert mask.loc[pd.Timestamp("2024-01-08")]


def test_forward_frame_enters_at_next_open():
    # Arrange
    idx = pd.bdate_range("2024-01-01", periods=5)
    a = pd.DataFrame(
        {
            "000300.SH|open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "000300.SH|close": [10.5, 11.5, 12.5, 13.5, 14.5],
        },
        index=idx,
    )

    # Act
    fwd = forward_frame(a, "000300.SH")

    # Assert
    assert fwd.loc[idx[0], "gap1"] == pytest.approx(11.0 / 10.5 - 1)
    assert fwd.loc[idx[0], "open_n1"] == pytest.approx(11.5 / 11.0 - 1)
    assert fwd.loc[idx[0], "open_n3"] == pytest.approx(13.5 / 11.0 - 1)
    assert np.isnan(fwd.loc[idx[-1], "open_n1"])


def test_block_at_least_twice_horizon():
    assert _block(1) == 20
    assert _block(5) == 20
    assert _block(10) == 20
    assert _block(20) == 40


def test_episodes_counts_runs():
    idx = pd.bdate_range("2024-01-01", periods=8)
    mask = pd.Series([False, True, True, False, True, True, True, False], index=idx)

    n, avg, longest = _episodes(mask)

    assert n == 2
    assert avg == pytest.approx(2.5)
    assert longest == 3
