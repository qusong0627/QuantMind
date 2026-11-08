"""Kronos A股零样本测试：窗口纯函数测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.kronos_ashare.windows import (
    build_window,
    score_prediction,
    select_signal_dates,
)


def _panel(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    close = 100.0 + np.arange(len(calendar), dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(calendar), 1_000_000.0),
            "amount": np.full(len(calendar), 1e8),
            "change": pd.Series(close).pct_change().fillna(0.0).to_numpy(),
        },
        index=calendar,
    )


def test_build_window_returns_expected_prices_and_returns():
    # Arrange
    calendar = pd.bdate_range("2025-01-01", periods=40)
    panel = _panel(calendar)
    signal_date = calendar[30]

    # Act
    win = build_window(panel, calendar, "sh600000", signal_date, lookback=10, pred_len=5)

    # Assert
    assert win is not None
    assert len(win.x_df) == 10
    assert win.x_df["close"].iloc[0] == pytest.approx(121.0)
    assert win.last_close == pytest.approx(130.0)
    assert win.entry_date == calendar[31]
    assert win.entry_price == pytest.approx(131.0)
    assert win.fwd_ret_model == pytest.approx(135.0 / 130.0 - 1.0)
    assert win.fwd_ret_exec == pytest.approx(135.0 / 131.0 - 1.0)


def test_build_window_returns_none_when_history_is_too_short():
    calendar = pd.bdate_range("2025-01-01", periods=20)
    panel = _panel(calendar)

    assert build_window(panel, calendar, "sh600000", calendar[5], lookback=10, pred_len=5) is None


def test_build_window_returns_none_when_future_is_too_short():
    calendar = pd.bdate_range("2025-01-01", periods=20)
    panel = _panel(calendar)

    assert build_window(panel, calendar, "sh600000", calendar[17], lookback=10, pred_len=5) is None


def test_build_window_rejects_missing_columns():
    calendar = pd.bdate_range("2025-01-01", periods=40)
    panel = _panel(calendar).drop(columns=["amount"])

    with pytest.raises(ValueError, match="missing columns"):
        build_window(panel, calendar, "sh600000", calendar[30], lookback=10, pred_len=5)


def test_select_signal_dates_steps_and_caps():
    calendar = pd.bdate_range("2025-01-01", periods=40)

    dates = select_signal_dates(calendar, calendar[0], calendar[35], every=10, max_dates=2)

    assert len(dates) == 2
    assert dates[0] == calendar[0]
    assert dates[-1] == calendar[30]


def test_score_prediction_modes():
    pred = pd.DataFrame({"close": [130.0, 140.0]})

    assert score_prediction(pred, 130.0, "mean") == pytest.approx(135.0 / 130.0 - 1.0)
    assert score_prediction(pred, 130.0, "end") == pytest.approx(140.0 / 130.0 - 1.0)
    assert score_prediction(pred, 130.0, "max") == pytest.approx(140.0 / 130.0 - 1.0)
    assert score_prediction(pred, 130.0, "min") == pytest.approx(130.0 / 130.0 - 1.0)
