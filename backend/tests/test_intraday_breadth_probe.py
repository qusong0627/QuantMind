"""backend/scripts/intraday_breadth_probe.py 单元测试（纯函数部分）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.intraday_breadth_probe import (
    _bucket,
    _seg,
    composite_table,
    incremental_intraday,
    state_table,
)


def _panel(n: int = 120, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {
            "up_share": rng.uniform(5, 95, n),
            "trin_amt": rng.uniform(0.05, 3.0, n),
            "amt_gap_pp": rng.uniform(-30, 40, n),
            "mcl_chg5": rng.normal(0, 100, n),
            "ret_rest_ew": rng.normal(0, 0.5, n),
            "ret_rest_vw": rng.normal(0, 0.5, n),
            "ret_next_ew": rng.normal(0, 0.5, n),
            "sh_above_ma250": rng.random(n) > 0.4,
        },
        index=idx,
    )


class TestBucket:
    def test_edges_are_left_closed(self):
        s = pd.Series([29, 30, 40, 60, 70])
        b = _bucket(
            s, [30, 40, 50, 60, 70], ["<30", "30-40", "40-50", "50-60", "60-70", ">=70"]
        )
        assert list(b.astype(str)) == ["<30", "30-40", "40-50", "60-70", ">=70"]

    def test_missing_values_stay_missing(self):
        b = _bucket(pd.Series([np.nan]), [30], ["<30", ">=30"])
        assert b.isna().all()


class TestSeg:
    def test_counts_mean_and_win(self):
        d = _panel(10)
        d["ret_rest_ew"] = [1.0, 2.0, 3.0, 4.0, 5.0, -1.0, -2.0, -3.0, -4.0, -5.0]
        d["ret_next_ew"] = 0.0
        out = _seg(d, d.index < d.index[5], "first5")
        assert out["n"] == 5
        assert out["rest_mean"] == pytest.approx(3.0)
        assert out["rest_win"] == 100.0

    def test_empty_segment_does_not_raise(self):
        d = _panel(10)
        out = _seg(d, pd.Series(False, index=d.index), "empty")
        assert out["n"] == 0
        assert np.isnan(out["rest_mean"])


class TestStateTable:
    def test_returns_breadth_trin_grid(self):
        out = state_table(_panel(300))
        assert {"breadth", "trin", "n", "rest_ew", "next_ew"}.issubset(out.columns)
        assert len(out) > 0
        assert out["n"].sum() <= 300


class TestIncremental:
    def test_segments_are_labelled(self):
        out = incremental_intraday(_panel())
        labels = out["segment"].tolist()
        assert "广度<30" in labels
        assert "广度>60" in labels
        assert "MCL 扩" in labels
        assert "MCL 缩" in labels
        assert any("额价差>20" in s for s in labels)

    def test_composite_has_six_cells(self):
        out = composite_table(_panel())
        assert len(out) == 6
        assert out["segment"].str.startswith(("MA250上方", "MA250下方")).all()
