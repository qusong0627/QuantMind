"""backend/scripts/breadth_divergence_probe.py 单元测试。

覆盖三类：
1. 恒等式：1/TRIN ≡ 人均成交额比；额价差可由 (涨家占比, TRIN) 重构
2. 事件计数与统计：连续触发折叠成段、HAC t 的方向性
3. 特征构造：250 日新高/熊市状态判定不吃未来数据
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.breadth_divergence_probe import (
    _episodes,
    add_events,
    build_features,
    event_stats,
    newyork_west_tstat,
)


def _panel(days: int = 320, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n_total = 1000
    n_up = rng.integers(100, 900, days)
    n_down = n_total - n_up
    amt_up = rng.uniform(2e7, 9e7, days)
    amt_down = rng.uniform(2e7, 9e7, days)
    out = pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=days),
        "n_total": n_total,
        "n_up": n_up,
        "n_down": n_down,
        "amt_up": amt_up,
        "amt_down": amt_down,
        "amt_total": amt_up + amt_down,  # 平盘股成交额为 0，便于精确重构
        "trin_amt": (n_up / n_down) / (amt_up / amt_down),
        "mcclellan": rng.normal(0, 100, days),
    })
    for sym, drift in (("000001.SH", 0.0003), ("000300.SH", 0.0002),
                       ("000852.SH", 0.0001)):
        ret = rng.normal(drift, 0.01, days)
        out[f"close_{sym}"] = 100 * np.exp(np.cumsum(ret))
    return out


class TestIdentities:
    def test_per_capita_ratio_is_inverse_trin(self):
        df = build_features(_panel())
        assert np.allclose(df["per_capita_ratio"] * df["trin_amt"], 1.0)

    def test_amt_gap_reconstruction(self):
        df = build_features(_panel())
        # amt_total = amt_up + amt_down 时，额价差应被 涨家占比+TRIN 精确重构
        assert np.allclose(df["amt_gap_pp"], df["amt_gap_recon_pp"], atol=1e-9)

    def test_rank_correlation_of_redundant_series_is_near_one(self):
        df = build_features(_panel())
        rho = df["per_capita_ratio"].rank().corr(df["trin_amt"].rank())
        assert rho == pytest.approx(-1.0)


class TestEpisodes:
    def test_counts_rising_edges(self):
        mask = pd.Series([False, True, True, False, True, False, False, True])
        assert _episodes(mask) == 3

    def test_all_false_is_zero(self):
        assert _episodes(pd.Series([False] * 10)) == 0

    def test_leading_true_is_one_episode(self):
        assert _episodes(pd.Series([True, True, False])) == 1


class TestEventStats:
    def test_detects_negative_forward_returns(self):
        days = 400
        ret = np.full(days, 0.001)
        mask = np.zeros(days, dtype=bool)
        for i in range(200, days - 6, 20):
            mask[i] = True
            ret[i + 1 : i + 6] = -0.01
        close = 100 * np.cumprod(1 + ret)
        df = pd.DataFrame({
            "date": pd.bdate_range("2020-01-01", periods=days),
            "close_000001.SH": close,
        })
        s = event_stats(df, pd.Series(mask), "000001.SH", 5)
        assert s.n_days == int(mask.sum())
        assert s.delta < 0
        assert s.mean < 0


class TestHacTstat:
    def test_white_noise_is_insignificant(self):
        rng = np.random.default_rng(11)
        x = (rng.random(2000) > 0.5).astype(float)
        y = rng.normal(0, 1, 2000)
        assert abs(newyork_west_tstat(y, x, lags=5)) < 3

    def test_strong_signal_is_detected(self):
        rng = np.random.default_rng(12)
        x = np.zeros(2000)
        x[::50] = 1.0
        y = rng.normal(0, 1, 2000) - 2.0 * x
        assert newyork_west_tstat(y, x, lags=5) < -5


class TestNoLookahead:
    def test_high250_false_before_full_window(self):
        df = build_features(_panel(days=200))
        assert not df["high250_000001.SH"].iloc[:249].any()

    def test_monotone_rise_makes_new_high(self):
        p = _panel(days=300)
        p["close_000001.SH"] = np.arange(300, dtype=float) + 100
        df = build_features(p)
        assert bool(df["high250_000001.SH"].iloc[-1])

    def test_bear_flag_is_below_ma250(self):
        p = _panel(days=300)
        p["close_000001.SH"] = np.r_[
            np.linspace(200, 100, 260), np.linspace(100, 90, 40)
        ]
        df = build_features(p)
        assert bool(df["bear_000001.SH"].iloc[-1])


class TestEventsUseOnlyCurrentDay:
    def test_weak_breadth_flag_matches_threshold(self):
        df = add_events(build_features(_panel()))
        expected = df["up_share"] < 40.0
        assert (df["ev_weak_breadth"] == expected).all()
