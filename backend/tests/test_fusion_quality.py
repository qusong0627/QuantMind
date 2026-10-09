"""融合质量适配层（fusion_quality）—— 纯函数契约测试。

背景（2026-10-09 取证）：`qm_model_inference_quality` 最大 trade_date=09-23、
近 12 天零行（回填链路只按 qm_model_inference_runs 定位、且天然滞后 H 天），
而成员分数桶天天在长 → 权重引擎的 IC 必须**现场从分数桶 + 已实现收益回算**。
本层数学与生产融合模板（inference_ensemble_src）逐位对齐：
L1 截面 pct 秩（pandas rank pct=True）+ L3 权重线性合成（回放不含 L4 共识调节）。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backend.services.engine.inference.fusion_quality import (
    daily_rank_ic,
    extract_horizon,
    forward_returns_from_closes,
    fuse_rank_frames,
    pairwise_corr,
    replay_fusion,
)


def _closes(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["trade_date", "symbol", "close"])


class TestForwardReturns:
    def test_basic_ratio_and_tail_dropped(self):
        closes = _closes(
            [
                ("2026-01-05", "SH600000", 10.0),
                ("2026-01-06", "SH600000", 11.0),
                ("2026-01-07", "SH600000", 12.1),
            ]
        )
        out = forward_returns_from_closes(closes, horizon=1)
        got = {(r.trade_date, r.symbol): r.label for r in out.itertuples()}
        assert got[("2026-01-05", "SH600000")] == pytest.approx(0.1)   # 10→11
        assert got[("2026-01-06", "SH600000")] == pytest.approx(0.1)   # 11→12.1
        # 尾日无未来价 → 必须整体剔除（不得拿别的 symbol 的下一行充数）
        assert ("2026-01-07", "SH600000") not in got

    def test_no_cross_symbol_bleed(self):
        """A 的尾行绝不能取到 B 的下一日 —— shift 必须在 symbol 分组内（2026-10-08 已修同类 bug）。"""
        closes = _closes(
            [
                ("2026-01-05", "A", 10.0),
                ("2026-01-06", "A", 99.0),
                ("2026-01-05", "B", 20.0),
                ("2026-01-06", "B", 21.0),
            ]
        )
        out = forward_returns_from_closes(closes, horizon=1)
        got = {(r.trade_date, r.symbol): r.label for r in out.itertuples()}
        assert got[("2026-01-05", "A")] == pytest.approx(8.9)  # 10→99，组内
        assert ("2026-01-06", "A") not in got
        assert got[("2026-01-05", "B")] == pytest.approx(0.05)
        assert ("2026-01-06", "B") not in got

    def test_unordered_rows_are_sorted_by_date(self):
        closes = _closes(
            [
                ("2026-01-07", "X", 12.1),
                ("2026-01-05", "X", 10.0),
                ("2026-01-06", "X", 11.0),
            ]
        )
        out = forward_returns_from_closes(closes, horizon=1)
        got = {(r.trade_date, r.symbol): r.label for r in out.itertuples()}
        assert got[("2026-01-05", "X")] == pytest.approx(0.1)


class TestDailyRankIc:
    def _labels(self, day_count: int = 40):
        rows = []
        for i in range(day_count):
            rows.append(("2026-02-02", f"S{i:03d}", float(i)))
        return pd.DataFrame(rows, columns=["trade_date", "symbol", "label"])

    def test_perfect_and_inverted_ic(self):
        labels = self._labels()
        good = labels.assign(score=labels["label"])
        bad = labels.assign(score=-labels["label"])
        assert daily_rank_ic(good, labels)["2026-02-02"] == pytest.approx(1.0)
        assert daily_rank_ic(bad, labels)["2026-02-02"] == pytest.approx(-1.0)

    def test_min_symbols_gate_and_date_normalization(self):
        labels = self._labels(40)
        labels["trade_date"] = pd.to_datetime(labels["trade_date"])  # datetime 入参要归一
        small = labels.head(10).assign(score=lambda d: d["label"])
        assert daily_rank_ic(small, labels) == {}  # 10 < 30 → 整日剔除

    def test_nan_labels_dropped_before_count(self):
        labels = self._labels(40)
        labels.loc[0, "label"] = float("nan")
        scores = labels.assign(score=labels["label"] * -1.0)
        out = daily_rank_ic(scores, labels)
        # 39 个有效符号 ≥ 30，仍出 -1
        assert out["2026-02-02"] == pytest.approx(-1.0)


class TestPairwiseCorr:
    def _panel(self, day: str, values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "trade_date": [day] * len(values),
                "symbol": [f"S{i:03d}" for i in range(len(values))],
                "score": values,
            }
        )

    def test_identical_and_inverted_panels(self):
        a = self._panel("2026-02-02", [float(i) for i in range(40)])
        b = self._panel("2026-02-02", [float(i) for i in range(40)])
        c = self._panel("2026-02-02", [-float(i) for i in range(40)])
        corr = pairwise_corr({"a": a, "b": b, "c": c}, min_symbols=30, min_days=1)
        assert corr["a"]["b"] == pytest.approx(1.0)
        assert corr["a"]["c"] == pytest.approx(-1.0)
        assert corr["c"]["a"] == pytest.approx(-1.0)  # 对称

    def test_insufficient_common_days_absent(self):
        a = self._panel("2026-02-02", [float(i) for i in range(40)])
        b = self._panel("2026-02-03", [float(i) for i in range(40)])
        corr = pairwise_corr({"a": a, "b": b}, min_symbols=30, min_days=2)
        assert "b" not in corr.get("a", {})  # 无共同日 → 无信息（引擎侧按 pen=1）


class TestReplayFusion:
    def _panel(self, day: str, values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "trade_date": [day] * len(values),
                "symbol": [f"S{i:03d}" for i in range(len(values))],
                "score": values,
            }
        )

    def _labels(self, day: str, values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "trade_date": [day] * len(values),
                "symbol": [f"S{i:03d}" for i in range(len(values))],
                "label": values,
            }
        )

    def test_single_effective_member_reproduces_member_ic(self):
        """权重 {A:1, B:0}：融合 IC == A 当日成员 IC（同数学：A 的 pct 秩 vs 标签）。"""
        labels = self._labels("2026-02-02", [float(i) for i in range(40)])
        a = self._panel("2026-02-02", [float(i) + (0.5 if i % 2 else 0.0) for i in range(40)])
        b = self._panel("2026-02-02", [-float(i) for i in range(40)])
        rep = replay_fusion({"a": a, "b": b}, {"a": 1.0, "b": 0.0}, labels)
        assert rep.fused_ic["2026-02-02"] == pytest.approx(1.0, abs=1e-9)
        assert rep.member_ic["a"]["2026-02-02"] == pytest.approx(1.0, abs=1e-9)

    def test_weights_flip_fused_direction(self):
        """权重完全决定方向：A 正序、B 反序，w={A:1,B:0} → +1；w={A:0,B:1} → −1。

        0 权路径在浮点上是精确的（0·x + 1·y = y），不受 ulp 抖动影响。
        """
        labels = self._labels("2026-02-02", [float(i) for i in range(40)])
        a = self._panel("2026-02-02", [float(i) for i in range(40)])
        b = self._panel("2026-02-02", [float(39 - i) for i in range(40)])
        rep_a = replay_fusion({"a": a, "b": b}, {"a": 1.0, "b": 0.0}, labels)
        rep_b = replay_fusion({"a": a, "b": b}, {"a": 0.0, "b": 1.0}, labels)
        assert rep_a.fused_ic["2026-02-02"] == pytest.approx(1.0, abs=1e-9)
        assert rep_b.fused_ic["2026-02-02"] == pytest.approx(-1.0, abs=1e-9)

    def test_member_missing_symbols_uses_available_only(self):
        """B 当日只覆盖一半标的：融合对该半区按 Σw 重新归一（与模板 1/n 缺省逻辑不同仅在权重缺省）。"""
        labels = self._labels("2026-02-02", [float(i) for i in range(40)])
        a = self._panel("2026-02-02", [float(i) for i in range(40)])
        b = self._panel("2026-02-02", [-float(i) for i in range(20)])  # 仅 S000..S019
        rep = replay_fusion({"a": a, "b": b}, {"a": 1.0, "b": 1.0}, labels)
        # S000..S019 两成员平均≈0.5；S020..S039 仅 a → 与标签同序，整体仍正 IC
        assert rep.fused_ic["2026-02-02"] > 0

    def test_summary_carries_means_and_counts(self):
        labels = self._labels("2026-02-02", [float(i) for i in range(40)])
        a = self._panel("2026-02-02", [float(i) for i in range(40)])
        rep = replay_fusion({"a": a, "a2": a.copy()}, {"a": 1.0, "a2": 0.0}, labels)
        assert rep.summary["fused"]["ic_mean"] == pytest.approx(1.0)
        assert rep.summary["fused"]["n_days"] == 1
        assert rep.summary["a"]["ic_mean"] == pytest.approx(1.0)


class TestFuseRankFrames:
    """生产模板 L1+L3 的合成数学（直接被 replay 与对账复用）。"""

    def test_weighted_mean_of_pct_ranks(self):
        a = pd.Series({"s1": 1.0, "s2": 0.0})
        b = pd.Series({"s1": 0.0, "s2": 1.0})
        fused = fuse_rank_frames({"a": a, "b": b}, {"a": 3.0, "b": 1.0})
        assert fused["s1"] == pytest.approx(0.75)
        assert fused["s2"] == pytest.approx(0.25)

    def test_missing_member_renormalizes_by_present_weights(self):
        a = pd.Series({"s1": 1.0, "s2": 0.5})
        b = pd.Series({"s2": 0.0})  # s1 缺 B
        fused = fuse_rank_frames({"a": a, "b": b}, {"a": 1.0, "b": 1.0})
        assert fused["s1"] == pytest.approx(1.0)   # 仅 a，权重归一后 = a
        assert fused["s2"] == pytest.approx(0.25)  # (0.5+0)/2

    def test_absent_member_gets_uniform_default(self):
        a = pd.Series({"s1": 1.0})
        b = pd.Series({"s1": 0.0})
        fused = fuse_rank_frames({"a": a, "b": b}, {})  # 权重缺省 → 1/n
        assert fused["s1"] == pytest.approx(0.5)


class TestExtractHorizon:
    def test_config_label_horizon_wins(self):
        assert extract_horizon({"label": {"target_horizon_days": 3}}, {"horizon_days": 10}) == 3

    def test_metadata_fallback(self):
        assert extract_horizon({}, {"horizon_days": 10}) == 10
        assert extract_horizon(None, {"prediction_horizon": 7}) == 7

    def test_default_and_junk(self):
        assert extract_horizon({}, {"horizon_days": None}) == 5
        assert extract_horizon({"label": {"target_horizon_days": "abc"}}, None) == 5
        assert extract_horizon({"label": {"target_horizon_days": -2}}, None) == 5
