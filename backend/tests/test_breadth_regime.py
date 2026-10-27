"""backend/scripts/breadth_regime.py 单元测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.breadth_regime import (
    bh_fdr,
    cluster_count,
    composite_scores,
    episode_count,
    forward_path,
)


class TestEpisodeAndCluster:
    def test_episode_count(self):
        m = np.array([False, True, True, False, True, False, False, True])
        assert episode_count(m) == 3

    def test_episode_count_empty(self):
        assert episode_count(np.array([], dtype=bool)) == 0

    def test_cluster_count_merges_overlapping(self):
        m = np.zeros(60, dtype=bool)
        m[[0, 1, 2, 10, 30, 31, 55]] = True
        # gap=20：0 与 10 合并；10 与 30 间隔 20 → 独立；30 与 55 间隔 25 → 独立
        assert cluster_count(m, 20) == 3

    def test_cluster_count_exact_gap_boundary(self):
        m = np.zeros(30, dtype=bool)
        m[[0, 20]] = True
        assert cluster_count(m, 20) == 2   # 间隔正好 20 天 → 算两次独立
        assert cluster_count(m, 21) == 1   # 间隔 20 < 21 → 合并
        m2 = np.zeros(30, dtype=bool)
        m2[[0, 19]] = True
        assert cluster_count(m2, 20) == 1


class TestBhFdr:
    def test_known_values(self):
        p = np.array([0.001, 0.01, 0.04, 0.2])
        q = bh_fdr(p)
        assert q == pytest.approx([0.004, 0.02, 0.0533333, 0.2], rel=1e-5)

    def test_monotone_in_p(self):
        p = np.linspace(0.001, 0.9, 50)
        q = bh_fdr(p)
        assert np.all(np.diff(q) >= -1e-12)

    def test_nan_passthrough(self):
        q = bh_fdr(np.array([0.01, np.nan, 0.5]))
        assert np.isnan(q[1])


class TestForwardPath:
    def test_simple_path(self):
        close = pd.Series([10.0, 11.0, 9.0, 12.0, 13.0])
        ret, low, high = forward_path(close, 2)
        assert ret[0] == pytest.approx(9.0 / 10.0 - 1)
        assert low[0] == pytest.approx(9.0 / 10.0 - 1)
        assert high[0] == pytest.approx(11.0 / 10.0 - 1)
        assert np.isnan(ret[-1]) and np.isnan(ret[-2])

    def test_no_lookahead_first_point(self):
        close = pd.Series([10.0, 10.5, 9.8, 20.0])
        ret, _, _ = forward_path(close, 2)
        # 第三个点（20.0）不能影响前两天的窗口
        assert ret[0] == pytest.approx(9.8 / 10.0 - 1)
        assert ret[1] == pytest.approx(20.0 / 10.5 - 1)


class TestCompositeScores:
    def test_neutral_percentiles_give_50(self):
        idx = pd.RangeIndex(3)
        feats = pd.DataFrame(
            {c: [0.5, 0.5, 0.5] for c in
             ["breadth20", "breadth60", "adl_slope", "nh_nl", "mcclellan", "trin_ema10"]},
            index=idx,
        )
        sc = composite_scores(feats)
        assert sc["stress"].tolist() == pytest.approx([50.0, 50.0, 50.0])

    def test_stress_direction(self):
        idx = pd.RangeIndex(1)
        stressed = pd.DataFrame(
            {c: [0.05] for c in
             ["breadth20", "breadth60", "adl_slope", "nh_nl", "mcclellan"]}
            | {"trin_ema10": [0.95]},
            index=idx,
        )
        sc = composite_scores(stressed)
        assert sc["stress"].iloc[0] == pytest.approx(95.0)

    def test_overheated_direction(self):
        idx = pd.RangeIndex(1)
        hot = pd.DataFrame(
            {c: [0.95] for c in
             ["breadth20", "breadth60", "adl_slope", "nh_nl", "mcclellan"]}
            | {"trin_ema10": [0.05]},
            index=idx,
        )
        sc = composite_scores(hot)
        assert sc["stress"].iloc[0] == pytest.approx(5.0)
