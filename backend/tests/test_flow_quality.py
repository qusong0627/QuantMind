"""资金流质量分的口径测试（与线上/研究共用同一实现）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.shared.flow_quality import (
    COMPONENT_COLUMNS,
    COMPONENTS,
    component_percentiles,
    quality_score,
    rank_pct,
)


def _wide(values: dict[str, list[float]]) -> dict[str, pd.DataFrame]:
    n = len(next(iter(values.values())))
    idx = pd.Index(["2026-10-27"])
    cols = [f"s{i}" for i in range(n)]
    return {k: pd.DataFrame([v], index=idx, columns=cols) for k, v in values.items()}


class TestRankPct:
    def test_rank_inside_mask_only(self):
        frame = pd.DataFrame(
            {"a": [1.0], "b": [2.0], "c": [999.0], "d": [3.0]},
            index=["2026-10-27"],
        )
        mask = pd.DataFrame(
            {"a": [True], "b": [True], "c": [False], "d": [True]},
            index=frame.index,
        )
        r = rank_pct(frame, mask)
        assert r.loc["2026-10-27", "a"] == pytest.approx(0.0)
        assert r.loc["2026-10-27", "d"] == pytest.approx(1.0)
        assert np.isnan(r.loc["2026-10-27", "c"])


class TestQualityScore:
    def test_monotone_when_components_ordered(self):
        n = 5
        values = {
            c.column: (list(range(n)) if c.sign > 0 else list(range(n, 0, -1)))
            for c in COMPONENTS
        }
        score = quality_score(_wide(values)).iloc[0].to_numpy()
        # 第 0 列正向最低、负向最高 → 分数最低；依次升高
        assert np.all(np.diff(score) > 0)
        assert score[0] == pytest.approx(0.0)
        assert score[-1] == pytest.approx(100.0)

    def test_missing_component_makes_score_nan(self):
        n = 3
        values = {
            c.column: list(range(n)) if c.sign > 0 else list(range(n, 0, -1))
            for c in COMPONENTS
        }
        values[COMPONENT_COLUMNS[0]][1] = np.nan
        score = quality_score(_wide(values)).iloc[0].to_numpy()
        assert np.isnan(score[1])
        assert np.isfinite(score[0]) and np.isfinite(score[2])

    def test_missing_column_raises(self):
        values = {
            c.column: [1.0, 2.0]
            for c in COMPONENTS
            if c.column != COMPONENT_COLUMNS[-1]
        }
        with pytest.raises(KeyError):
            quality_score(_wide(values))

    def test_higher_sell_pressure_lowers_score(self):
        n = 4
        base = {c.column: [1.0] * n for c in COMPONENTS}
        # 唯一变化：卖压第 3 列最高 → 分数应更低
        base["micro_trade_sell_pressure"] = [1.0, 2.0, 3.0, 4.0]
        score = quality_score(_wide(base)).iloc[0].to_numpy()
        assert score[3] < score[0]

    def test_percentiles_are_cross_sectional(self):
        n = 4
        values = {
            c.column: list(range(n)) if c.sign > 0 else list(range(n, 0, -1))
            for c in COMPONENTS
        }
        pct = component_percentiles(_wide(values))
        for c in COMPONENTS:
            series = pct[c.column].iloc[0].to_numpy()
            assert series.min() == pytest.approx(0.0)
            assert series.max() == pytest.approx(1.0)
