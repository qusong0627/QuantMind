"""backend/shared/market_breadth.py 单元测试（涨跌停/广度/分布/板块聚合）。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend.shared.market_breadth import (
    TOL_BJ,
    TOL_SHSZ,
    breadth_distribution,
    classify_by_pct,
    classify_price,
    is_corp_action_pct,
    is_ex_div,
    market_breadth,
    sector_aggregate,
    streak_from_tail,
    wan_to_yi,
    volume_ratio_5,
)

D = date(2026, 8, 14)
PRE_RELAX = date(2026, 7, 1)


class TestLimitClassification:
    def test_price_limit_up_exact(self):
        assert classify_price(72.81, 72.81, 72.81, 59.57) == "limit_up"

    def test_price_broke_up(self):
        assert classify_price(70.5, 72.9, 72.81, 59.57) == "broke_up"
        assert classify_price(70.5, 72.0, 72.81, 59.57) == "normal"

    def test_price_limit_down(self):
        assert classify_price(59.57, 60.5, 72.81, 59.57) == "limit_down"

    def test_price_no_limit_new_stock(self):
        assert classify_price(30.0, 31.0, 0.0, 0.0) == "normal"

    def test_corp_action_pct(self):
        assert is_corp_action_pct(-74.3, 0.20) is True
        assert is_corp_action_pct(-42.16, 0.10) is True
        assert is_corp_action_pct(-10.0, 0.10) is False

    def test_pct_main_board(self):
        assert classify_by_pct(10.0, "601138.SH", False, D) == "limit_up"
        assert classify_by_pct(9.4, "601138.SH", False, D) == "up"
        assert classify_by_pct(-10.0, "601138.SH", False, D) == "limit_down"

    def test_pct_st_pre_relax(self):
        assert classify_by_pct(5.0, "600301.SH", True, PRE_RELAX) == "limit_up"
        assert classify_by_pct(9.9, "600301.SH", True, D) == "limit_up"  # fidelity: allow-limit-threshold — 用例入参：涨幅百分比（判定在被测 classify_by_pct 内）

    def test_pct_growth_and_bse(self):
        assert classify_by_pct(20.0, "300750.SZ", False, D) == "limit_up"
        assert classify_by_pct(12.0, "688981.SH", False, D) == "up"
        assert classify_by_pct(30.0, "832000.BJ", False, D) == "limit_up"


class TestStreak:
    def test_streak_count(self):
        assert streak_from_tail([-3.0, 10.0, 10.0, 9.99], 9.5) == 3  # fidelity: allow-limit-threshold — 入参：阈值是 streak_from_tail 的必填参数（判定逻辑必须在被测函数内）

    def test_streak_broken(self):
        assert streak_from_tail([10.0, -2.0, 10.0], 9.5) == 1  # fidelity: allow-limit-threshold — 入参：阈值是 streak_from_tail 的必填参数（判定逻辑必须在被测函数内）


class TestBreadth:
    def test_buckets(self):
        pct = pd.Series(
            [10.1, 7.5, 5.0, 3.2, 1.5, 0.5, 0.0, -0.4, -1.2, -2.5, -4.5, -7.1, -10.2, 4.0]
        )
        dist = breadth_distribution(pct)
        assert dist["涨停"] == 1
        assert dist[">7"] == 1
        assert dist["3~5"] == 2
        assert dist["平盘"] == 1
        assert dist["跌停"] == 1
        assert sum(dist.values()) == 14

    def test_market_breadth(self):
        r = market_breadth(pd.Series([1.0, -1.0, 0.0, 2.0]))
        assert r["up_count"] == 2
        assert r["down_count"] == 1
        assert r["up_down_ratio"] == pytest.approx(2.0)


class TestSector:
    def _members(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "SectorCode": ["BK1", "BK1", "BK2"],
                "SectorName": ["行业A", "行业A", "行业B"],
                "SectorType": ["行业板块(一级)"] * 3,
                "Symbol": ["600000.SH", "600001.SH", "600002.SH"],
            }
        )

    def test_equal_weight(self):
        pct = pd.Series({"600000.SH": 5.0, "600001.SH": 3.0, "600002.SH": -1.0})
        out = sector_aggregate(self._members(), pct, None)
        a = out[out["SectorName"] == "行业A"].iloc[0]
        assert a["n"] == 2
        assert a["avg_pct"] == pytest.approx(4.0)
        assert a["mv_weighted_pct"] is None

    def test_mv_weighted(self):
        pct = pd.Series({"600000.SH": 10.0, "600001.SH": 0.0})
        mv = pd.Series({"600000.SH": 3e11, "600001.SH": 1e11})
        out = sector_aggregate(self._members(), pct, mv)
        a = out[out["SectorName"] == "行业A"].iloc[0]
        assert a["mv_weighted_pct"] == pytest.approx(7.5)


class TestUnits:
    def test_wan_to_yi(self):
        assert wan_to_yi(703474.0) == pytest.approx(70.35, abs=0.01)

    def test_volume_ratio(self):
        prior = [100.0, 110.0, 90.0, 120.0, 130.0]
        assert volume_ratio_5(300.0, prior) == pytest.approx(2.73, abs=0.01)
        assert volume_ratio_5(300.0, []) is None


class TestExDiv:
    def test_normal_day(self):
        assert is_ex_div(1.4717, 66.19, 65.23) is False

    def test_ex_div_day(self):
        assert is_ex_div(1.5, 10.0, 10.5) is True
