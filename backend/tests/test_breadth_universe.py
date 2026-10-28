"""分市场广度与全 A 合成 K 线的口径测试（2026-10-28）。

钉住四件事：
1. 板别掩码与排名代理都用 **PIT**（排名只看前一交易日市值）；
2. 七档涨跌分布互斥、涨停只算一次；
3. 合成 K 线的等权链式口径（月内 buy-and-hold、月初重置）；
4. 同一函数换宇宙必须只改样本、不改公式。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.breadth_regime import (
    INDICATORS,
    build_features,
    composite_scores,
    stress_from_raw,
)
from backend.scripts.breadth_universe import (
    Universe,
    board_mask,
    build_universe_panel,
    distribution_counts,
    equal_weight_aggregate,
    rank_mask,
)
from backend.scripts.trin_breadth import PriceMatrices


def _px(
    dates: list[str],
    symbols: list[str],
    close: np.ndarray,
    amount: np.ndarray | None = None,
    open_: np.ndarray | None = None,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
) -> PriceMatrices:
    n = close.shape
    amt = np.ones(n) if amount is None else amount
    return PriceMatrices(
        dates=pd.DatetimeIndex(dates),
        symbols=np.array(symbols),
        close=close,
        volume=amt.copy(),
        amount=amt,
        open=close.copy() if open_ is None else open_,
        high=close.copy() if high is None else high,
        low=close.copy() if low is None else low,
    )


class TestBoardMask:
    def test_suffix_and_prefix(self):
        syms = np.array(
            ["600000.SH", "000001.SZ", "300001.SZ", "688001.SH", "920001.BJ"]
        )
        assert board_mask(syms, ".SH", None).tolist() == [
            True,
            False,
            False,
            True,
            False,
        ]
        assert board_mask(syms, ".SZ", None).tolist() == [
            False,
            True,
            True,
            False,
            False,
        ]
        assert board_mask(syms, None, ("300", "301", "302")).tolist() == [
            False,
            False,
            True,
            False,
            False,
        ]
        assert board_mask(syms, None, None).all()


class TestRankMask:
    def test_uses_previous_day_market_cap(self):
        """当日暴涨把市值顶上去的票，不能当天就进宇宙（用昨收市值排名）。"""
        fmv = np.array([[100.0, 200.0], [1000.0, 200.0]])
        valid = np.array([[False, False], [True, True]])
        static = np.ones(2, dtype=bool)
        mask = rank_mask(fmv, valid, static, top=1)
        # day1 用 day0 市值：B(200) 胜出；A 当日翻倍不算数
        assert mask[1].tolist() == [False, True]
        assert mask[0].sum() == 0

    def test_band_selection(self):
        fmv = np.array([[400.0, 300.0, 200.0, 100.0], [400.0, 300.0, 200.0, 100.0]])
        valid = np.array([[False] * 4, [True] * 4])
        mask = rank_mask(fmv, valid, np.ones(4, dtype=bool), start=1, end=3)
        # 排名：0,1,2,3 → 取 [1,3) = 名次 2、3
        assert mask[1].tolist() == [False, True, True, False]


class TestDistribution:
    def test_seven_exclusive_buckets(self):
        ret = np.array([[-0.10, -0.06, -0.01, 0.0, 0.01, 0.06, 0.10]])
        base = np.ones((1, 7), dtype=bool)
        thr = np.full((1, 7), 0.095)
        d = distribution_counts(ret, thr, base)
        assert {k: int(v[0]) for k, v in d.items()} == {
            "dist_limit_up": 1,
            "dist_up_big": 1,
            "dist_up_small": 1,
            "dist_flat": 1,
            "dist_down_small": 1,
            "dist_down_big": 1,
            "dist_limit_down": 1,
        }

    def test_limit_up_not_double_counted_as_big_up(self):
        ret = np.array([[0.20, 0.0]])
        thr = np.full((1, 2), 0.195)
        d = distribution_counts(ret, thr, np.ones((1, 2), dtype=bool))
        assert int(d["dist_limit_up"][0]) == 1
        assert int(d["dist_up_big"][0]) == 0


class TestEqualWeightAggregate:
    def test_within_month_is_buy_and_hold_equal_weight(self):
        """月内不复位：两只票各 +10% → 等权指数 +10%（= 累计收益均值）。"""
        close = np.array([[10.0, 10.0], [11.0, 10.0], [11.0, 11.0]])
        px = _px(
            ["2026-01-05", "2026-01-06", "2026-01-07"],
            ["600000.SH", "600001.SH"],
            close,
        )
        agg = equal_weight_aggregate(
            px, np.ones((3, 2), dtype=bool), np.full((3, 2), 100.0)
        )
        assert agg["ew_close"].iloc[0] == pytest.approx(1000.0)
        assert agg["ew_close"].iloc[1] == pytest.approx(1050.0)
        assert agg["ew_close"].iloc[2] == pytest.approx(1100.0)

    def test_month_start_resets_weights(self):
        """跨月复位：1 月底权重已漂到 52/48，2 月首日必须回到 50/50。"""
        close = np.array(
            [
                [10.0, 10.0],
                [11.0, 10.0],
                [11.0, 11.0],
                [9.9, 11.0],  # 2 月首日：A -10%，B 0
            ]
        )
        px = _px(
            ["2026-01-28", "2026-01-29", "2026-01-30", "2026-02-02"],
            ["600000.SH", "600001.SH"],
            close,
        )
        agg = equal_weight_aggregate(
            px, np.ones((4, 2), dtype=bool), np.full((4, 2), 100.0)
        )
        # 复位后 -5%（若不复位，权重 0.5238/0.4762 → -5.24%）
        assert agg["ew_close"].iloc[3] == pytest.approx(1100.0 * 0.95, rel=1e-9)

    def test_ohlc_invariants(self):
        open_ = np.array([[10.0, 10.0], [10.5, 9.5]])
        high = np.array([[10.2, 10.2], [11.0, 10.5]])
        low = np.array([[9.8, 9.8], [9.8, 9.0]])
        close = np.array([[10.0, 10.0], [10.6, 9.6]])
        px = _px(
            ["2026-01-05", "2026-01-06"],
            ["600000.SH", "600001.SH"],
            close,
            open_=open_,
            high=high,
            low=low,
        )
        agg = equal_weight_aggregate(
            px, np.ones((2, 2), dtype=bool), np.full((2, 2), 100.0)
        )
        row = agg.iloc[1]
        assert row["ew_high"] >= max(row["ew_open"], row["ew_close"])
        assert row["ew_low"] <= min(row["ew_open"], row["ew_close"])

    def test_turnover_uses_previous_float_cap(self):
        close = np.array([[10.0, 10.0], [10.0, 10.0]])
        amt = np.array([[0.0, 0.0], [100.0, 100.0]])  # 万元
        fmv = np.array([[5e9, 5e9], [5e9, 5e9]])  # 元
        px = _px(
            ["2026-01-05", "2026-01-06"], ["600000.SH", "600001.SH"], close, amount=amt
        )
        agg = equal_weight_aggregate(px, np.ones((2, 2), dtype=bool), fmv)
        # 200 万元 / 100 亿元 = 0.02%
        assert agg["ew_turnover"].iloc[1] == pytest.approx(2e-4)


class TestStressConsistency:
    """分市场只能换样本、不能换公式（用户 2026-10-28 的硬要求）。"""

    @staticmethod
    def _frame(n: int = 90, seed: int = 7) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        cols = {ind.column: np.cumsum(rng.normal(size=n)) for ind in INDICATORS}
        return pd.DataFrame(
            cols, index=pd.date_range("2020-01-01", periods=n, freq="B")
        )

    def test_stress_from_raw_matches_feature_pipeline(self):
        """预计算用的 stress_from_raw 与 build_features+composite_scores 逐位一致。"""
        df = self._frame()
        want = composite_scores(build_features(df, min_periods=10))["stress"].to_numpy()
        got = stress_from_raw(df, min_periods=10).to_numpy()
        np.testing.assert_allclose(got, want, equal_nan=True)

    def test_panel_stress_equals_raw_recompute(self):
        """面板里落盘的 stress 列 = 用同一宇宙原始列现场重算的结果。"""
        n = 600
        rng = np.random.default_rng(3)
        close = 10.0 + np.cumsum(rng.normal(0.0, 0.05, size=(n, 2)), axis=0)
        dates = [
            d.strftime("%Y-%m-%d") for d in pd.bdate_range("2022-01-03", periods=n)
        ]
        px = _px(
            dates, ["600000.SH", "600001.SH"], close, amount=np.full((n, 2), 100.0)
        )
        panel = build_universe_panel(
            px,
            np.zeros((n, 2), dtype=bool),
            np.full((n, 2), 1e9),
            universes=(Universe("all", "全A", "board"),),
        ).set_index("date")
        got = panel["stress"].to_numpy()
        want = stress_from_raw(panel, min_periods=500).to_numpy()
        assert np.isfinite(got[-1]) and np.isfinite(want[-1])
        np.testing.assert_allclose(got, want, equal_nan=True)


class TestBuildUniversePanel:
    def test_universes_only_change_sample_not_formula(self):
        # day1：沪 A +5%（额 100）、沪 B −1%（额 300）、深 C +5%（额 1000）
        close = np.array(
            [
                [10.0, 10.0, 10.0],
                [10.5, 9.9, 10.5],
            ]
        )
        amt = np.array([[1.0, 1.0, 1.0], [100.0, 300.0, 1000.0]])
        px = _px(
            ["2026-01-05", "2026-01-06"],
            ["600000.SH", "600001.SH", "000001.SZ"],
            close,
            amount=amt,
        )
        st = np.zeros((2, 3), dtype=bool)
        fmv = np.full((2, 3), 100.0)
        panel = build_universe_panel(
            px,
            st,
            fmv,
            universes=(
                Universe("all", "全A", "board"),
                Universe("sh", "沪市", "board", suffix=".SH"),
            ),
        )
        all_row = panel[
            (panel.universe == "all") & (panel.date == pd.Timestamp("2026-01-06"))
        ].iloc[0]
        sh_row = panel[
            (panel.universe == "sh") & (panel.date == pd.Timestamp("2026-01-06"))
        ].iloc[0]
        assert all_row["n_total"] == 3 and sh_row["n_total"] == 2
        # 同一公式：全A 2 涨 1 跌（额 1100/300）→ 0.545；沪市 1 涨 1 跌（100/300）→ 3
        assert all_row["trin_amt"] == pytest.approx((2 / 1) / (1100 / 300))
        assert sh_row["trin_amt"] == pytest.approx((1 / 1) / (100 / 300))
        # 等权收益：全A (5-1+5)/3；沪市 (5-1)/2
        assert all_row["ret_equal_weight"] == pytest.approx(3.0)
        assert sh_row["ret_equal_weight"] == pytest.approx(2.0)
        # 合成 K 线同日起点相同、日收益不同
        assert all_row["ew_close"] == pytest.approx(1030.0)
        assert sh_row["ew_close"] == pytest.approx(1020.0)
