"""backend/scripts/intraday_stock_breadth_probe.py 单元测试（聚合纯函数）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.intraday_stock_breadth_probe import (
    breadth_bucket_table,
    rs_table,
    video_setup,
)


def _daily(rows: list[dict]) -> pd.DataFrame:
    out = pd.DataFrame(rows)
    base = {
        "share_up_rest": 50.0,
        "share_down_rest": 50.0,
        "median_rest": 0.0,
        "mean_rest": 0.0,
        "median_morning": 0.0,
        "capw_morning": 0.0,
        "mcl_chg5": 0.0,
        "n": 5000,
    }
    for k, v in base.items():
        if k not in out:
            out[k] = v
    return out


def _stock(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestBreadthBucket:
    def test_six_buckets(self):
        d = _daily(
            [
                {"d": pd.Timestamp("2024-01-01"), "up_share_1010": b}
                for b in (10, 35, 45, 55, 65, 75)
            ]
        )
        out = breadth_bucket_table(d)
        assert out["bucket"].astype(str).tolist() == [
            "<30",
            "30-40",
            "40-50",
            "50-60",
            "60-70",
            ">=70",
        ]
        assert out["days"].sum() == 6


class TestRsTable:
    def test_groups_by_bucket_and_quintile(self):
        d = _stock(
            [
                {"up_share": 75, "rs_q": "Q1最弱", "ret_r": 0.01},
                {"up_share": 75, "rs_q": "Q1最弱", "ret_r": 0.03},
                {"up_share": 75, "rs_q": "Q5最强", "ret_r": -0.01},
                {"up_share": 20, "rs_q": "Q1最弱", "ret_r": -0.02},
            ]
        )
        out = rs_table(d)
        row = out[(out["bucket"].astype(str) == ">=70") & (out["rs_q"] == "Q1最弱")]
        assert len(row) == 1
        assert row["p_up"].iloc[0] == 100.0
        assert row["mean_rest"].iloc[0] == 2.0

    def test_morning_filter_drops_extremes(self):
        d = _stock(
            [
                {"up_share": 75, "rs_q": "Q1最弱", "ret_r": 0.01, "ret_m": 0.02},
                {"up_share": 75, "rs_q": "Q5最强", "ret_r": 0.02, "ret_m": 0.15},
            ]
        )
        out = rs_table(d, max_abs_morning=0.09)
        assert out["n"].sum() == 1


class TestVideoSetup:
    def test_counts_days_by_state(self):
        d = _daily(
            [
                {
                    "d": pd.Timestamp("2024-01-01"),
                    "up_share_1010": 35,
                    "capw_morning": 0.5,
                    "median_morning": -0.2,
                },
                {
                    "d": pd.Timestamp("2024-01-02"),
                    "up_share_1010": 35,
                    "capw_morning": 0.4,
                    "median_morning": -0.3,
                },
                {
                    "d": pd.Timestamp("2024-01-03"),
                    "up_share_1010": 35,
                    "capw_morning": -0.5,
                    "median_morning": -0.6,
                },
                {
                    "d": pd.Timestamp("2024-01-04"),
                    "up_share_1010": 75,
                    "capw_morning": 1.0,
                    "median_morning": 0.8,
                },
            ]
        )
        out = video_setup(d).set_index("状态")
        assert out.loc["广度<40 ∧ 指数红(成交额加权)", "days"] == 2
        assert out.loc["广度<40 ∧ 指数绿", "days"] == 1
        assert out.loc["广度>=60", "days"] == 1

    def test_empty_state_returns_nan(self):
        d = _daily(
            [
                {
                    "d": pd.Timestamp("2024-01-01"),
                    "up_share_1010": 75,
                    "capw_morning": 1.0,
                    "median_morning": 0.8,
                }
            ]
        )
        out = video_setup(d).set_index("状态")
        assert np.isnan(out.loc["广度<40 ∧ 指数红(成交额加权)", "收盘上涨占比_%"])
