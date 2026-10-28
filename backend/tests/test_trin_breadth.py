"""backend/scripts/trin_breadth.py 单元测试。

覆盖三类：
1. 纯函数数学正确性（TRIN / EMA / McClellan / 扩张分位无前视）
2. 板别阈值与 ``local_market_data.limit_pct`` 一致（20%/30%/ST 5% 都要对）
3. 小样本端到端：手算 3 只票 3 天，核对家数、成交额与 TRIN
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.trin_breadth import (
    CAT_BSE,
    CAT_GROWTH,
    CAT_MAIN,
    CAT_STAR,
    BreadthConfig,
    PriceMatrices,
    build_breadth_panel,
    ema,
    expanding_pct_rank,
    limit_threshold_matrix,
    mcclellan_net,
    safe_div,
    symbol_category,
    trin,
)


class TestTrinMath:
    def test_classic_ratio(self):
        # (10/5) / (300/100) = 2/3
        v = trin(
            pd.Series([10.0]), pd.Series([5.0]), pd.Series([300.0]), pd.Series([100.0])
        )
        assert v.iloc[0] == pytest.approx(2 / 3)

    def test_equivalent_expanded_form(self):
        # 用户写的 (上涨家数 × 下跌成交额)/(下跌家数 × 上涨成交额) 必须等价
        up_c, dn_c, up_s, dn_s = 30.0, 12.0, 8_000.0, 5_000.0
        direct = (up_c * dn_s) / (dn_c * up_s)
        v = trin(
            pd.Series([up_c]), pd.Series([dn_c]), pd.Series([up_s]), pd.Series([dn_s])
        )
        assert v.iloc[0] == pytest.approx(direct)

    def test_zero_down_count_is_nan_not_inf(self):
        v = trin(
            pd.Series([10.0]), pd.Series([0.0]), pd.Series([100.0]), pd.Series([50.0])
        )
        assert np.isnan(v.iloc[0])

    def test_zero_down_size_is_nan(self):
        v = trin(
            pd.Series([10.0]), pd.Series([5.0]), pd.Series([100.0]), pd.Series([0.0])
        )
        assert np.isnan(v.iloc[0])

    def test_series_alignment_preserved(self):
        idx = pd.Index([7, 8, 9])
        v = trin(
            pd.Series([2.0, 4.0, 6.0], index=idx),
            pd.Series([1.0, 1.0, 1.0], index=idx),
            pd.Series([10.0, 10.0, 10.0], index=idx),
            pd.Series([10.0, 10.0, 10.0], index=idx),
        )
        assert list(v.index) == [7, 8, 9]


class TestSafeDiv:
    def test_no_inf(self):
        assert np.isnan(safe_div(1.0, 0.0))
        assert np.isnan(safe_div(0.0, 0.0))
        assert safe_div(6.0, 2.0) == 3.0


class TestEma:
    def test_recursion_matches_manual(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        got = ema(s, span=3).dropna()
        alpha = 2 / (3 + 1)
        manual = [1.0]
        for x in [2.0, 3.0, 4.0, 5.0]:
            manual.append(manual[-1] + alpha * (x - manual[-1]))
        # min_periods=span=3 → 前两个为 NaN，剩三个可比
        assert list(got) == pytest.approx(manual[2:])

    def test_warmup_is_nan(self):
        s = pd.Series(np.arange(10.0))
        assert ema(s, span=5).iloc[:4].isna().all()


class TestMcClellan:
    def test_positive_when_breadth_expanding(self):
        net = pd.Series(np.r_[np.zeros(20), np.full(20, 100.0), np.full(20, 200.0)])
        osc = mcclellan_net(net).dropna()
        assert (osc > 0).all()

    def test_negative_when_breadth_collapsing(self):
        net = pd.Series(np.r_[np.zeros(20), np.full(20, -100.0), np.full(20, -200.0)])
        osc = mcclellan_net(net).dropna()
        assert (osc < 0).all()


class TestExpandingPctRank:
    def test_no_lookahead(self):
        """尾部追加数据不得改变历史分位 —— 这是防前视的关键性质。"""
        base = pd.Series(np.arange(300.0))
        extended = pd.Series(np.r_[np.arange(300.0), 10_000.0])
        a = expanding_pct_rank(base, min_periods=250)
        b = expanding_pct_rank(extended, min_periods=250)
        pd.testing.assert_series_equal(a, b.iloc[:300])

    def test_rank_bounds(self):
        s = pd.Series(np.arange(300.0))
        r = expanding_pct_rank(s, min_periods=250).dropna()
        assert r.min() >= 0.0 and r.max() < 1.0

    def test_matches_reference_implementation_with_nans(self):
        """bisect 实现必须与逐行参考实现逐位一致（含热身期 NaN）。"""
        rng = np.random.default_rng(20261028)
        vals = rng.normal(size=400)
        vals[::17] = np.nan
        s = pd.Series(vals)
        got = expanding_pct_rank(s, min_periods=50)
        ref = []
        for i in range(len(s)):
            w = s.iloc[: i + 1].to_numpy()
            cur = w[-1]
            if not np.isfinite(cur) or np.isfinite(w).sum() < 50:
                ref.append(np.nan)
            else:
                v = w[np.isfinite(w)]
                ref.append(float((v < cur).mean()))
        np.testing.assert_allclose(got.to_numpy(), np.array(ref), equal_nan=True)


class TestSymbolCategory:
    def test_boards(self):
        syms = np.array(
            ["600000.SH", "000001.SZ", "300001.SZ", "688001.SH", "920001.BJ"]
        )
        cats = symbol_category(syms)
        assert list(cats) == [CAT_MAIN, CAT_MAIN, CAT_GROWTH, CAT_STAR, CAT_BSE]

    def test_chinext_302_treated_as_growth(self):
        cats = symbol_category(np.array(["302001.SZ"]))
        assert cats[0] == CAT_GROWTH


class TestLimitThreshold:
    def test_board_specific_bands(self):
        dates = pd.DatetimeIndex(["2026-08-14"])
        syms = np.array(["600000.SH", "300001.SZ", "688001.SH", "920001.BJ"])
        cats = symbol_category(syms)
        st = np.zeros((1, len(syms)), dtype=bool)
        thr = limit_threshold_matrix(dates, cats, st)
        assert thr[0, 0] == pytest.approx(0.10 - 0.005)
        assert thr[0, 1] == pytest.approx(0.20 - 0.005)
        assert thr[0, 2] == pytest.approx(0.20 - 0.005)
        assert thr[0, 3] == pytest.approx(0.30 - 0.01)

    def test_st_main_board_5pct(self):
        dates = pd.DatetimeIndex(["2024-01-02"])
        syms = np.array(["600000.SH"])
        cats = symbol_category(syms)
        st = np.ones((1, 1), dtype=bool)
        thr = limit_threshold_matrix(dates, cats, st)
        assert thr[0, 0] == pytest.approx(0.05 - 0.005)

    def test_chinext_before_2020_reform_is_10pct(self):
        dates = pd.DatetimeIndex(["2019-06-03"])
        syms = np.array(["300001.SZ"])
        cats = symbol_category(syms)
        st = np.zeros((1, 1), dtype=bool)
        thr = limit_threshold_matrix(dates, cats, st)
        assert thr[0, 0] == pytest.approx(0.10 - 0.005)

    def test_chinext_after_2020_reform_is_20pct(self):
        dates = pd.DatetimeIndex(["2020-08-24"])
        syms = np.array(["300001.SZ"])
        cats = symbol_category(syms)
        st = np.zeros((1, 1), dtype=bool)
        thr = limit_threshold_matrix(dates, cats, st)
        assert thr[0, 0] == pytest.approx(0.20 - 0.005)


def _synthetic_prices() -> PriceMatrices:
    """3 只票、4 天：手算得到的 TRIN。

    day2: A +10%(额 100), B -5%(额 300), C 0%(额 50)
          up=1 down=1, up_amt=100 down_amt=300 → TRIN = 1/(1/3) = 3.0
    day3: A +2%(额 200), B -1%(额 100), C +3%(额 100)
          up=2 down=1, up_amt=300 down_amt=100 → TRIN = 2/3
    """
    dates = pd.DatetimeIndex(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    symbols = np.array(["600000.SH", "600001.SH", "600002.SH"])
    close = np.array(
        [
            [10.0, 20.0, 30.0],
            [11.0, 19.0, 30.0],
            [11.22, 18.81, 30.9],
            [11.22, 18.81, 30.9],
        ]
    )
    amount = np.array(
        [
            [100.0, 100.0, 100.0],
            [100.0, 300.0, 50.0],
            [200.0, 100.0, 100.0],
            [10.0, 10.0, 10.0],
        ]
    )
    volume = amount.copy()
    return PriceMatrices(
        dates=dates, symbols=symbols, close=close, volume=volume, amount=amount
    )


class TestBuildBreadthPanel:
    def test_counts_and_trin(self):
        px = _synthetic_prices()
        st = np.zeros((4, 3), dtype=bool)
        panel = build_breadth_panel(px, st)

        d2 = panel.iloc[2]
        assert d2["n_up"] == 2
        assert d2["n_down"] == 1
        assert d2["n_flat"] == 0
        assert d2["amt_up"] == pytest.approx(300.0)
        assert d2["amt_down"] == pytest.approx(100.0)
        assert d2["trin_amt"] == pytest.approx(2 / 3)

        d1 = panel.iloc[1]
        assert d1["n_up"] == 1 and d1["n_down"] == 1 and d1["n_flat"] == 1
        assert d1["trin_amt"] == pytest.approx(3.0)

    def test_first_day_excluded(self):
        """首日没有前收盘，不能计入涨跌家数。"""
        px = _synthetic_prices()
        panel = build_breadth_panel(px, np.zeros((4, 3), dtype=bool))
        assert panel.iloc[0]["n_total"] == 0
        assert np.isnan(panel.iloc[0]["trin_amt"])

    def test_flat_amount_not_in_trin(self):
        px = _synthetic_prices()
        panel = build_breadth_panel(px, np.zeros((4, 3), dtype=bool))
        # day1: A 平盘 100 万，只应出现在 amt_total，不进 amt_up/amt_down
        assert panel.iloc[1]["amt_total"] == pytest.approx(450.0)
        assert panel.iloc[1]["amt_up"] == pytest.approx(100.0)
        assert panel.iloc[1]["amt_down"] == pytest.approx(300.0)

    def test_new_listing_excluded_from_clean_variant(self):
        """次新剔除必须真的改变结果，而不是恒等于主口径。"""
        px = _synthetic_prices()
        panel = build_breadth_panel(
            px, np.zeros((4, 3), dtype=bool), config=BreadthConfig(new_listing_days=2)
        )
        # 第 3 天（iloc[2]）三只票 age 都 >= 2，干净口径与主口径同宇宙
        assert panel.iloc[2]["trin_amt_shsz"] == pytest.approx(
            panel.iloc[2]["trin_amt"]
        )

        # 把门槛抬到 3 天：第 3 天三只票全是次新 → 干净口径无样本
        strict = build_breadth_panel(
            px, np.zeros((4, 3), dtype=bool), config=BreadthConfig(new_listing_days=3)
        )
        assert np.isnan(strict.iloc[2]["trin_amt_shsz"])
        assert np.isfinite(strict.iloc[2]["trin_amt"])

    def test_equal_weight_return_not_all_nan(self):
        """等权收益必须按有效标的求均值 —— 曾经用 np.mean 导致整列 NaN。"""
        px = _synthetic_prices()
        panel = build_breadth_panel(px, np.zeros((4, 3), dtype=bool))
        # day1: (+10% -5% +0%) / 3 = +1.6667%
        assert panel.iloc[1]["ret_equal_weight"] == pytest.approx(5 / 3, rel=1e-9)
        assert panel["ret_equal_weight"].notna().sum() == 3  # 首日无收益

    def test_limit_up_detected_by_board_band(self):
        dates = pd.DatetimeIndex(["2026-01-05", "2026-01-06"])
        symbols = np.array(["600000.SH", "300001.SZ"])
        # 主板 +10% 封板；创业板 +10% 不是涨停（20% 板）
        close = np.array([[10.0, 10.0], [11.0, 11.0]])
        amount = np.array([[1.0, 1.0], [1.0, 1.0]])
        px = PriceMatrices(dates, symbols, close, amount.copy(), amount.copy())
        panel = build_breadth_panel(px, np.zeros((2, 2), dtype=bool))
        assert panel.iloc[1]["n_limit_up"] == 1
