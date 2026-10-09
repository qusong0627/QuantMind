"""非 SOTA 因子清理判据（P3）——纯函数：只建议不删，每条判据必须带数字证据。

行为钉死（判据透明是这块的核心价值，改判据必须过这里）：

- ``duplicate``：与池内因子 |ρ|≥``corr_dup``（默认 0.9）**且对方更强** →
  被支配。强弱比较 ICIR 优先（质量证据），ICIR 双方缺失时退到池评分；
  双方无可比指标、或平手 → **不下裁决**（宁可漏报，不可误报）。
- ``weak_icir``：ICIR ≤ 池内后 ``weak_icir_quantile`` 分位（默认 20%）；
  有效样本 < ``min_icir_sample`` 不判（分位无意义）；全员同值不算弱
  （无分化）；ICIR 缺失 ≠ 弱（缺失是「不知道」，绝不按 0 参与）。
- ``no_diversity``：留一贡献 ≤ 0；None（未算过）≠ 0。
- severity：``duplicate`` 单独即 high（信息已被更强副本覆盖）；两条软判据
  同时出现 → high；单条软判据 → medium。
- 确定性：输出按 (severity 高在前, factor_id 升序)，同输入同输出。
"""

from __future__ import annotations

import pytest

from backend.services.engine.mining_plugins.pool_cleanup import (
    CleanupCandidate,
    CleanupCriteria,
    evaluate_cleanup,
)


def _c(fid: str, **kw) -> CleanupCandidate:
    return CleanupCandidate(factor_id=fid, **kw)


def _by_id(report) -> dict[str, object]:
    return {s.candidate.factor_id: s for s in report.suggestions}


def _codes(suggestion) -> set[str]:
    return {r.code for r in suggestion.reasons}


# ── duplicate：被更强副本支配 ─────────────────────────────────────────


class TestDuplicate:
    def test_weaker_side_flagged_stronger_not(self):
        a = _c("a", icir=0.2, max_pool_corr=0.93, max_pool_corr_with="b")
        b = _c("b", icir=0.9, max_pool_corr=0.93, max_pool_corr_with="a")
        report = evaluate_cleanup([a, b])
        flagged = _by_id(report)
        assert set(flagged) == {"a"}, "只有被支配的一侧该被标记"
        reason = next(r for r in flagged["a"].reasons if r.code == "duplicate")
        assert "0.93" in reason.detail and "b" in reason.detail
        assert flagged["a"].severity == "high"

    def test_abs_corr_is_idempotent(self):
        a = _c("a", icir=0.2, max_pool_corr=-0.93, max_pool_corr_with="b")
        b = _c("b", icir=0.9, max_pool_corr=0.93, max_pool_corr_with="a")
        assert "a" in _by_id(evaluate_cleanup([a, b]))

    def test_below_threshold_not_flagged(self):
        a = _c("a", icir=0.2, max_pool_corr=0.85, max_pool_corr_with="b")
        b = _c("b", icir=0.9, max_pool_corr=0.85, max_pool_corr_with="a")
        assert evaluate_cleanup([a, b]).suggestions == ()

    def test_counterpart_missing_from_scope_not_flagged(self):
        a = _c("a", icir=0.2, max_pool_corr=0.95, max_pool_corr_with="ghost")
        assert evaluate_cleanup([a]).suggestions == ()

    def test_tie_not_flagged(self):
        a = _c("a", icir=0.5, max_pool_corr=0.92, max_pool_corr_with="b")
        b = _c("b", icir=0.5, max_pool_corr=0.92, max_pool_corr_with="a")
        assert evaluate_cleanup([a, b]).suggestions == ()

    def test_falls_back_to_pool_score_when_icir_missing(self):
        a = _c("a", pool_score=0.1, max_pool_corr=0.91, max_pool_corr_with="b")
        b = _c("b", pool_score=0.8, max_pool_corr=0.91, max_pool_corr_with="a")
        flagged = _by_id(evaluate_cleanup([a, b]))
        assert set(flagged) == {"a"}
        detail = next(r for r in flagged["a"].reasons if r.code == "duplicate").detail
        assert "池评分" in detail

    def test_no_comparable_metric_not_flagged(self):
        a = _c("a", max_pool_corr=0.95, max_pool_corr_with="b")
        b = _c("b", max_pool_corr=0.95, max_pool_corr_with="a")
        assert evaluate_cleanup([a, b]).suggestions == ()


# ── weak_icir：预测力垫底 ────────────────────────────────────────────


class TestWeakIcir:
    def _pool(self, values, **kw):
        return [_c(f"f{i}", icir=v, **kw) for i, v in enumerate(values)]

    def test_bottom_quantile_flagged_with_threshold(self):
        # 10 个值：后 20% 分位 = 第 2 小 = 0.02
        values = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.05, 0.02]
        report = evaluate_cleanup(self._pool(values))
        assert report.icir_sample_size == 10
        assert report.weak_icir_threshold == pytest.approx(0.05)
        flagged = _by_id(report)
        assert set(flagged) == {"f8", "f9"}
        detail = next(r for r in flagged["f9"].reasons if r.code == "weak_icir").detail
        assert "0.050" in detail and "10" in detail  # 阈值与样本数都进判据

    def test_sample_too_small_no_verdict(self):
        report = evaluate_cleanup(self._pool([0.9, 0.5, 0.3, 0.1]))
        assert report.weak_icir_threshold is None
        assert report.suggestions == ()

    def test_uniform_values_not_weak(self):
        report = evaluate_cleanup(self._pool([0.5] * 6))
        assert report.suggestions == (), "全员同值没有分化，不该判弱"
        assert report.weak_icir_threshold is None

    def test_missing_icir_not_weak(self):
        rows = self._pool([0.9, 0.8, 0.7, 0.6, 0.5]) + [_c("no_icir")]
        report = evaluate_cleanup(rows)
        assert "no_icir" not in _by_id(report)
        assert report.icir_sample_size == 5

    def test_garbage_criteria_do_not_crash(self):
        rows = self._pool([0.9, 0.8, 0.7, 0.6, 0.5])
        report = evaluate_cleanup(
            rows, CleanupCriteria(weak_icir_quantile=0.0, min_icir_sample=0)
        )
        assert report.weak_icir_threshold is not None


# ── no_diversity：零/负多样性贡献 ─────────────────────────────────────


class TestNoDiversity:
    def test_zero_and_negative_flagged_positive_and_missing_not(self):
        rows = [
            _c("zero", diversity_contrib=0.0),
            _c("neg", diversity_contrib=-0.2),
            _c("pos", diversity_contrib=0.4),
            _c("missing"),
        ]
        flagged = _by_id(evaluate_cleanup(rows))
        assert set(flagged) == {"zero", "neg"}
        detail = next(
            r for r in flagged["neg"].reasons if r.code == "no_diversity"
        ).detail
        assert "-0.200" in detail


# ── severity 与确定性 ────────────────────────────────────────────────


class TestSeverityAndOrder:
    def test_duplicate_alone_is_high(self):
        a = _c("a", icir=0.2, max_pool_corr=0.95, max_pool_corr_with="b")
        b = _c("b", icir=0.9, max_pool_corr=0.95, max_pool_corr_with="a")
        assert _by_id(evaluate_cleanup([a, b]))["a"].severity == "high"

    def test_two_soft_reasons_are_high(self):
        values = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.01]
        rows = [_c(f"f{i}", icir=v) for i, v in enumerate(values)]
        rows.append(_c("weak_both", icir=0.005, diversity_contrib=-0.1))
        flagged = _by_id(evaluate_cleanup(rows))
        assert flagged["weak_both"].severity == "high"
        assert _codes(flagged["weak_both"]) == {"weak_icir", "no_diversity"}

    def test_single_soft_reason_is_medium(self):
        rows = [_c("lonely", diversity_contrib=0.0)]
        assert _by_id(evaluate_cleanup(rows))["lonely"].severity == "medium"

    def test_deterministic_order_high_first_then_id(self):
        a = _c("z_soft", diversity_contrib=0.0)  # medium
        b = _c("b_dup", icir=0.1, max_pool_corr=0.95, max_pool_corr_with="c_strong")
        c = _c("c_strong", icir=0.9, max_pool_corr=0.95, max_pool_corr_with="b_dup")
        d = _c("a_soft", diversity_contrib=-0.1)  # medium
        report = evaluate_cleanup([a, b, c, d])
        ids = [s.candidate.factor_id for s in report.suggestions]
        assert ids == ["b_dup", "a_soft", "z_soft"]


def test_empty_pool_yields_empty_report():
    report = evaluate_cleanup([])
    assert report.suggestions == ()
    assert report.weak_icir_threshold is None
    assert report.icir_sample_size == 0
