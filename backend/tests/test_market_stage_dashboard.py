"""backend/scripts/market_stage_dashboard.py 单元测试（阶段判定纯函数）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.market_stage_dashboard import (
    bottom_stage,
    long_stage,
    medium_stage,
    short_stage,
)


def _breadth(rows: list[float], **kw) -> pd.DataFrame:
    n = len(rows)
    d = pd.DataFrame(
        {
            "up_ratio": rows,
            "pct_above_ma20": kw.get("above20", 40.0),
            "pct_above_ma60": kw.get("above60", 40.0),
            "adl_slope20": kw.get("slope", 0),
            "nh_nl": 0.0,
            "adl": np.arange(n, dtype=float),
            "trin_vol": kw.get("trin_vol", 1.0),
        },
        index=pd.bdate_range("2020-01-01", periods=n),
    )
    return d


class TestLongStage:
    def test_rising_index_is_bull(self):
        idx = pd.Series(
            np.linspace(100, 200, 300), index=pd.bdate_range("2020-01-01", periods=300)
        )
        assert long_stage(idx)["阶段"] == "多头趋势"

    def test_falling_index_is_bear(self):
        idx = pd.Series(
            np.linspace(200, 100, 300), index=pd.bdate_range("2020-01-01", periods=300)
        )
        assert long_stage(idx)["阶段"] == "空头趋势"


class TestMediumStage:
    def test_washout_wins_over_trend(self):
        d = _breadth(
            [50, 50, 50, 50, 50, 10, 10, 10, 10, 10], above20=10, above60=10, slope=-100
        )
        assert medium_stage(d)["阶段"] == "超卖/投降段"

    def test_downtrend_continuation(self):
        d = _breadth([45] * 10, above20=25, above60=15, slope=-100)
        assert medium_stage(d)["阶段"] == "下跌中继"

    def test_repair(self):
        d = _breadth([60] * 10, above20=60, above60=55, slope=100)
        assert medium_stage(d)["阶段"] == "上行修复"

    def test_middle_is_neutral(self):
        d = _breadth([45] * 10, above20=40, above60=35, slope=0)
        assert medium_stage(d)["阶段"] == "震荡/待确认"


class TestShortStage:
    def test_oversold(self):
        assert short_stage(_breadth([40] * 9 + [10]))["阶段"] == "超卖（反抽候选）"

    def test_strong(self):
        assert short_stage(_breadth([40] * 9 + [90]))["阶段"] == "强势延续"

    def test_middle(self):
        assert short_stage(_breadth([40] * 10))["阶段"] == "中性"


def _val(pct: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bp_capw": 0.7,
            "bp_capw_pct": pct,
            "ep_capw": 0.05,
            "med_pe": 30.0,
        },
        index=pd.bdate_range("2020-01-01", periods=1),
    )


class TestBottomStage:
    def test_cheap_plus_washout_is_bottom_signal(self):
        b = _breadth([40] * 5 + [10] * 5)  # 5日均 = 10 < 25
        assert bottom_stage(b, _val(0.9))["阶段"] == "底部信号（便宜+投降）"

    def test_cheap_without_washout_waits(self):
        b = _breadth([50] * 10)
        assert bottom_stage(b, _val(0.9))["阶段"] == "估值便宜但未投降（等）"

    def test_expensive_is_not_bottom(self):
        b = _breadth([10] * 10)
        assert bottom_stage(b, _val(0.4))["阶段"] == "估值不便宜（底部信号不成立）"
