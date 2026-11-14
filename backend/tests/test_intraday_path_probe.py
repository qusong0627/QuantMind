"""backend/scripts/intraday_path_probe.py 单元测试（路径交叉与统计）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.intraday_path_probe import _first_cross, _stat

TIMES = ["10:10", "10:30", "11:00", "11:30", "14:00", "15:00"]


class TestFirstCross:
    def test_cross_above_after_start(self):
        row = pd.Series(
            {
                "10:10": 20,
                "10:30": 35,
                "11:00": 45,
                "11:30": 50,
                "14:00": 55,
                "15:00": 60,
            }
        )
        assert _first_cross(row, "10:10", 40, True, TIMES) == "11:00"

    def test_ignores_before_start(self):
        row = pd.Series(
            {
                "10:10": 20,
                "10:30": 45,
                "11:00": 30,
                "11:30": 30,
                "14:00": 30,
                "15:00": 30,
            }
        )
        assert _first_cross(row, "10:10", 40, True, TIMES) == "10:30"

    def test_cross_below(self):
        row = pd.Series(
            {
                "10:10": 80,
                "10:30": 70,
                "11:00": 55,
                "11:30": 50,
                "14:00": 45,
                "15:00": 40,
            }
        )
        assert _first_cross(row, "10:10", 60, False, TIMES) == "11:00"

    def test_no_cross_returns_none(self):
        row = pd.Series(dict.fromkeys(TIMES, 45))
        assert _first_cross(row, "10:10", 60, True, TIMES) is None


class TestStat:
    def test_mean_win_t(self):
        x = pd.Series([0.01, 0.02, -0.01, 0.03])
        out = _stat(x)
        assert out["n"] == 4
        assert out["mean_%"] == 1.25
        assert out["win_%"] == 75.0
        assert out["t"] > 0

    def test_too_few_returns_nan(self):
        out = _stat(pd.Series([0.01]))
        assert np.isnan(out["mean_%"])
