"""regime_universe 纯函数单元测试（宿主即可运行，不需要 fastapi）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.services.api.market_analysis.regime_universe import _roll_pct


def test_roll_pct_matches_rolling_apply_with_nans():
    """向量化分位必须与原 rolling.apply 逐值一致（含 NaN 窗口）。"""
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(size=800))
    s.iloc[100:120] = np.nan
    expected = s.rolling(300, min_periods=200).apply(
        lambda x: float((x.iloc[-1] >= x).mean()), raw=False
    )
    got = _roll_pct(s, window=300, min_periods=200)
    assert np.allclose(expected, got, equal_nan=True)


def test_roll_pct_requires_min_periods():
    s = pd.Series([1.0, 2.0, 3.0])
    out = _roll_pct(s, window=3, min_periods=2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == 1.0
    assert out.iloc[2] == 1.0
