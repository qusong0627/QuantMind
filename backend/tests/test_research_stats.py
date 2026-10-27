"""backend/scripts/research_stats.py 单元测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.research_stats import (
    block_bootstrap_corr,
    expanding_pct_rank,
    forward_return,
    newyork_west_tstat,
    overlap_deflated_t,
    spearman_ic,
)


class TestExpandingPctRank:
    def test_leading_nan_not_diluting(self):
        """热身期 NaN 不得计入分母 —— 曾把 95% 分位稀释成 74%。"""
        s = pd.Series([np.nan] * 5 + [1.0, 2.0, 3.0])
        r = expanding_pct_rank(s, min_periods=3)
        assert r.iloc[-1] == pytest.approx(2 / 3)

    def test_all_nan_window_returns_nan(self):
        s = pd.Series([np.nan] * 10 + [1.0])
        r = expanding_pct_rank(s, min_periods=10)
        assert np.isnan(r.iloc[-1])

    def test_no_lookahead(self):
        base = pd.Series(np.arange(300.0))
        extended = pd.Series(np.r_[np.arange(300.0), 10_000.0])
        a = expanding_pct_rank(base, min_periods=250)
        b = expanding_pct_rank(extended, min_periods=250)
        pd.testing.assert_series_equal(a, b.iloc[:300])

    def test_bounds(self):
        r = expanding_pct_rank(pd.Series(np.arange(300.0)), min_periods=250).dropna()
        assert r.min() >= 0.0 and r.max() < 1.0

    def test_current_value_nan_returns_nan(self):
        s = pd.Series(np.r_[np.arange(300.0), np.nan])
        r = expanding_pct_rank(s, min_periods=250)
        assert np.isnan(r.iloc[-1])


class TestSignificanceHelpers:
    def test_overlap_deflation_shrinks_t(self):
        t_raw = overlap_deflated_t(0.10, 1000, 1)
        t_20 = overlap_deflated_t(0.10, 1000, 20)
        assert abs(t_raw) > abs(t_20)

    def test_newyork_west_recovers_slope_sign(self):
        rng = np.random.default_rng(7)
        x = np.linspace(0, 1, 500)
        y = 2.0 * x + rng.normal(0, 0.1, 500)
        assert newyork_west_tstat(y, x, lags=5) > 10

    def test_spearman_ic_direction(self):
        x = pd.Series(np.arange(200.0))
        y = pd.Series(np.arange(200.0))
        ic, n = spearman_ic(x, y)
        assert ic == pytest.approx(1.0)
        assert n == 200

    def test_block_bootstrap_ci_contains_point(self):
        rng = np.random.default_rng(3)
        x = rng.normal(size=400)
        y = x * 0.5 + rng.normal(size=400)
        point, lo, hi = block_bootstrap_corr(x, y, block=10, n_boot=300)
        assert lo <= point <= hi


class TestForwardReturn:
    def test_basic(self):
        s = pd.Series([10.0, 11.0, 12.1])
        r = forward_return(s, 1)
        assert r.iloc[0] == pytest.approx(10.0)
        assert r.iloc[1] == pytest.approx(10.0)
        assert np.isnan(r.iloc[2])
