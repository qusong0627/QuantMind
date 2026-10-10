"""T-MV-08：残差正交引擎（对池内强因子回归取残差 → 增量 IC 可测）。

验收（框架 §4）：「残差因子与父集合最大相关 < 阈值；增量 IC 可测」。
口径纪律：
1. **数学**：残差 = 候选因子对强父本逐日截面 OLS（z 空间、含截距）的残差；
   父本集合 = 池内 ICIR 前 K（``max_parents``），ICIR 缺失者不参与、同分按
   id 稳定排序。
2. **相关统计单源**：与池内两两相关同一实现（``pool_panels.daily_rank_corr``，
   逐日秩相关再对日均值），正交阈值默认 0.3（远小于池去重线 0.9）。
3. **诚实降级**：无父本 / 父本覆盖不足 / 联合样本不足一律显式 status，
   绝不拿缺样本算出的数字当结论；面板无 fret 时增量 IC 记 None + note。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.mining_plugins import orthogonalize, pool_panels
from backend.services.engine.mining_plugins.orthogonalize import (
    OrthogonalCriteria,
    OrthogonalReport,
    build_traces,
    evaluate_orthogonality,
    select_strong_parents,
)

DAYS = [f"2026-01-{i:02d}" for i in range(1, 31)]
SYMS = [f"s{j:03d}" for j in range(60)]


def _frame(
    values: np.ndarray, days: list[str], symbols: list[str], *, fret=None
) -> pd.DataFrame:
    """合成面板帧：days × symbols 网格（引擎契约列 trade_date/symbol/zscore[/fret]）。"""
    nd, ns = values.shape
    frame = pd.DataFrame(
        {
            "trade_date": np.repeat(days, ns),
            "symbol": np.tile(symbols, nd),
            "zscore": values.reshape(-1),
        }
    )
    if fret is not None:
        frame["fret"] = np.asarray(fret).reshape(-1)
    return frame


def _synth(seed: int = 7):
    """父本 p、独立噪声 n、候选 0.7p+0.7n、前瞻收益 ≈ 噪声（残差里还有货）。"""
    rng = np.random.default_rng(seed)
    p = rng.normal(size=(len(DAYS), len(SYMS)))
    n = rng.normal(size=(len(DAYS), len(SYMS)))
    cand = 0.7 * p + 0.7 * n
    fret = n + 0.3 * rng.normal(size=(len(DAYS), len(SYMS)))
    return p, n, cand, fret


class TestSelectStrongParents:
    def test_icir_desc_none_out_tie_by_id(self):
        rows = [("a", 0.9), ("c", 0.9), ("b", None), ("d", 0.2), ("e", "bad")]
        assert select_strong_parents(rows, k=3) == ["a", "c", "d"]
        assert select_strong_parents(rows, exclude="a", k=3) == ["c", "d"]
        assert select_strong_parents([("x", float("nan"))], k=5) == []
        assert select_strong_parents([], k=5) == []


class TestOrthogonalProperty:
    def test_threshold_semantics_and_unknown(self):
        """阈值语义：< 阈值 = 正交；无相关样本 = None（未知≠通过）。

        逐日截面 OLS 的样本内残差天然与父本值空间正交（最小二乘性质），
        日秩相关只在 0 附近小量抖动——False 分支主要作为实现回归的守卫，
        这里直测属性本身。
        """
        assert OrthogonalReport(
            status="ok", max_parent_abs_corr=0.2, threshold=0.3
        ).orthogonal is True
        assert OrthogonalReport(
            status="ok", max_parent_abs_corr=0.4, threshold=0.3
        ).orthogonal is False
        assert OrthogonalReport(status="no_parents").orthogonal is None
        assert OrthogonalReport(
            status="ok", max_parent_abs_corr=None
        ).orthogonal is None


class TestEvaluateOrthogonality:
    def test_residual_keeps_incremental_signal(self):
        p, _, cand, fret = _synth()
        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS, fret=fret),
            {"p1": _frame(p, DAYS, SYMS)},
        )

        assert report.status == "ok"
        assert report.orthogonal is True
        assert report.max_parent_abs_corr < 0.15, "残差与父本日秩相关应≈0"
        assert report.residual_ic is not None and report.residual_ic > 0.3
        assert report.candidate_ic is not None and report.candidate_ic > 0.3
        assert report.n_days == len(DAYS)
        assert report.n_obs == len(DAYS) * len(SYMS)
        assert abs(report.parent_corrs["p1"]) == pytest.approx(
            report.max_parent_abs_corr
        )

    def test_duplicate_candidate_residual_is_noise(self):
        p, _, _, _ = _synth(seed=11)
        rng = np.random.default_rng(12)
        cand = p + 1e-3 * rng.normal(size=p.shape)  # 与父本几乎同值

        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS), {"p1": _frame(p, DAYS, SYMS)}
        )

        assert report.status == "ok"
        assert report.orthogonal is True
        assert report.max_parent_abs_corr < 0.2

    def test_no_parents_status(self):
        _, _, cand, _ = _synth()
        report = evaluate_orthogonality(_frame(cand, DAYS, SYMS), {})
        assert report.status == "no_parents"
        assert report.orthogonal is None

    def test_parent_below_min_stocks_not_qualified(self):
        p, _, cand, _ = _synth()
        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS), {"thin": _frame(p[:, :5], DAYS, SYMS[:5])}
        )
        assert report.status == "no_qualified_parents"

    def test_parent_below_min_sample_days_not_qualified(self):
        p, _, cand, _ = _synth()
        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS),
            {"short": _frame(p[:5], DAYS[:5], SYMS)},
            criteria=OrthogonalCriteria(min_sample_days=10),
        )
        assert report.status == "no_qualified_parents"

    def test_qualified_parents_but_joint_days_insufficient(self):
        p, _, cand, _ = _synth(seed=21)
        rng = np.random.default_rng(22)
        p2 = rng.normal(size=p.shape)
        parents = {
            "d1": _frame(p[:20], DAYS[:20], SYMS),
            "d2": _frame(p2[10:], DAYS[10:], SYMS),
        }

        report = evaluate_orthogonality(_frame(cand, DAYS, SYMS), parents)

        assert report.status == "insufficient_days"
        assert report.n_days < 20, "联合 complete-case 只剩 10 天，不得当结论"

    def test_days_sampling_restricts_window(self):
        p, _, cand, _ = _synth(seed=31)
        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS),
            {"p1": _frame(p, DAYS, SYMS)},
            days=set(DAYS[:6]),
            criteria=OrthogonalCriteria(min_sample_days=3),
        )
        assert report.status == "ok"
        assert report.n_days == 6

    def test_fret_missing_note(self):
        p, _, cand, _ = _synth()
        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS), {"p1": _frame(p, DAYS, SYMS)}
        )
        assert report.status == "ok"
        assert report.residual_ic is None and report.candidate_ic is None
        assert report.note == "fret_missing"
        assert report.orthogonal is True, "缺 fret 不影响正交判定"

    def test_bad_candidate_panel_status(self):
        p, _, _, _ = _synth()
        bad = _frame(np.zeros((3, 4)), DAYS[:3], SYMS[:4]).drop(columns=["zscore"])
        report = evaluate_orthogonality(bad, {"p1": _frame(p, DAYS, SYMS)})
        assert report.status == "bad_panel"

    def test_to_trace_json_serializable(self):
        p, _, cand, fret = _synth()
        report = evaluate_orthogonality(
            _frame(cand, DAYS, SYMS, fret=fret), {"p1": _frame(p, DAYS, SYMS)}
        )
        trace = report.to_trace()
        trace["evaluated_at"] = "2026-10-10T00:00:00Z"

        text = json.dumps(trace)  # 必须可入 metadata_json

        assert '"status": "ok"' in text
        assert trace["parents"][0]["factor_id"] == "p1"
        assert trace["criteria"]["min_sample_days"] == 20
        assert trace["threshold"] == orthogonalize.DEFAULT_MAX_PARENT_ABS_CORR


class TestBuildTraces:
    def _factors(self):
        return [
            {"factor_id": "f0", "factor_name": "F0", "icir": 0.9},
            {"factor_id": "f1", "factor_name": "F1", "icir": 0.5},
            {"factor_id": "f2", "factor_name": "F2", "icir": 0.1},
            {"factor_id": "f3", "factor_name": "F3", "icir": 0.7},  # 无面板
        ]

    def _independent_frames(self, seed: int = 41):
        rng = np.random.default_rng(seed)
        shape = (len(DAYS), len(SYMS))
        return {
            fid: _frame(
                rng.normal(size=shape), DAYS, SYMS, fret=rng.normal(size=shape)
            )
            for fid in ("f0", "f1", "f2")
        }

    def test_parent_selection_excludes_self_and_stats(self):
        traces, stats = build_traces(
            self._factors(), self._independent_frames(), criteria=OrthogonalCriteria(max_parents=1)
        )

        assert set(traces) == {"f0", "f1", "f2", "f3"}
        assert traces["f3"]["status"] == "no_panel"
        assert [p["factor_id"] for p in traces["f0"]["parents"]] == ["f1"]
        assert [p["factor_id"] for p in traces["f1"]["parents"]] == ["f0"]
        assert [p["factor_id"] for p in traces["f2"]["parents"]] == ["f0"]
        assert traces["f0"]["parents"][0]["name"] == "F1"
        assert stats["evaluated"] == 3
        assert stats["orthogonal"] == 3, stats
        assert stats["not_orthogonal"] == 0
        assert stats["unknown"] == 0
        assert stats["no_panel"] == 1
        assert stats["by_status"]["ok"] == 3

    def test_no_frames_all_no_panel(self):
        traces, stats = build_traces(self._factors(), {})

        assert all(t["status"] == "no_panel" for t in traces.values())
        assert stats["evaluated"] == 0 and stats["no_panel"] == 4

    def test_single_panel_factor_gets_no_parents(self):
        frames = {"f0": _frame(np.zeros((len(DAYS), len(SYMS))), DAYS, SYMS)}
        traces, stats = build_traces(self._factors(), frames)

        assert traces["f0"]["status"] == "no_parents"
        assert stats["evaluated"] == 0
        assert stats["by_status"]["no_parents"] == 1


class TestPoolPanelsSingleSource:
    def test_pair_corr_delegates_to_daily_rank_corr(self):
        rng = np.random.default_rng(51)
        base = _frame(rng.normal(size=(8, len(SYMS))), DAYS[:8], SYMS)
        ranked = base.assign(
            rank_pct=base.groupby("trade_date")["zscore"].rank(pct=True)
        )

        first = pool_panels.pair_corr(ranked, ranked)
        second = pool_panels.daily_rank_corr(ranked, ranked)

        assert first == second, "pair_corr 必须委托同一实现（单源重构）"
        assert first[0] == pytest.approx(1.0)
        assert first[1] == 8
