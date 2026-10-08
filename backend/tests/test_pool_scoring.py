"""池检索打分纯函数（AlphaPROBE 启发）——质量×疲劳衰减×冗余惩罚×新鲜度。

口径（全部系数可配，缺省见 ScoringParams）：
  q_norm     = 池内 ICIR 平均秩分位（缺失 → 不进召回，绝不按 0 参与）
  fatigue    = 1 / (1 + w·times_retrieved)
  redundancy = 1 - λ·max(0, max_pool_corr)     （None = 未知 → 不惩罚）
  freshness  = 0.5 ** (age_days / halflife)    （None = 未知 → 1.0 中性）
  score      = q_norm × fatigue × redundancy × freshness

本文件钉死的是**行为方向**与**确定性**（同分按 factor_id 升序），不是浮点尾数。
显示纪律：摘要里缺失指标一律「—」——写到 prompt 里的「0.0000」会教坏 LLM。
"""

from __future__ import annotations

import pytest

from backend.services.engine.mining_plugins.pool_scoring import (
    DigestEntry,
    PoolCandidate,
    ScoringParams,
    freshness_score,
    icir_percentiles,
    rank_candidates,
    render_digest,
    retrieval_score,
    select_topk,
)


class TestIcirPercentiles:
    def test_highest_gets_one_lowest_gets_low(self):
        pcts = icir_percentiles([0.1, 0.5, 0.9])
        assert pcts == pytest.approx([1 / 3, 2 / 3, 1.0])

    def test_missing_values_stay_none(self):
        assert icir_percentiles([0.5, None, 0.9]) == pytest.approx([0.5, None, 1.0])

    def test_ties_share_average_rank(self):
        pcts = icir_percentiles([0.5, 0.5, 0.9])
        # 前两个并列占秩 1,2 → 平均 1.5 / 3
        assert pcts[0] == pytest.approx(pcts[1])
        assert pcts[0] == pytest.approx(1.5 / 3)

    def test_single_present_value_is_one(self):
        assert icir_percentiles([0.42]) == [1.0]

    def test_all_missing_is_all_none(self):
        assert icir_percentiles([None, None]) == [None, None]


class TestFreshness:
    def test_halflife_decays_to_half(self):
        assert freshness_score(90.0, 90.0) == pytest.approx(0.5)
        assert freshness_score(180.0, 90.0) == pytest.approx(0.25)

    def test_zero_age_is_one(self):
        assert freshness_score(0.0, 90.0) == pytest.approx(1.0)

    def test_negative_age_clamps_to_one(self):
        """时钟偏移造成的负年龄不奖励——上限就是 1.0。"""
        assert freshness_score(-3.0, 90.0) == pytest.approx(1.0)

    def test_unknown_age_is_neutral(self):
        assert freshness_score(None, 90.0) == pytest.approx(1.0)


class TestRetrievalScore:
    def test_composition_matches_formula(self):
        cand = PoolCandidate(
            factor_id="f", icir=0.5, times_retrieved=2, max_pool_corr=0.8, age_days=90.0
        )
        params = ScoringParams(
            fatigue_weight=0.5, redundancy_lambda=0.5, freshness_halflife_days=90.0
        )
        expected = 0.75 * (1 / (1 + 0.5 * 2)) * (1 - 0.5 * 0.8) * 0.5
        assert retrieval_score(0.75, cand, params) == pytest.approx(expected)

    def test_fatigue_lowers_score_monotonically(self):
        params = ScoringParams()
        scores = [
            retrieval_score(
                0.8, PoolCandidate(factor_id="f", icir=0.8, times_retrieved=n), params
            )
            for n in (0, 1, 5)
        ]
        assert scores[0] > scores[1] > scores[2]

    def test_redundancy_none_is_no_penalty(self):
        params = ScoringParams()
        s_none = retrieval_score(
            0.8, PoolCandidate(factor_id="f", max_pool_corr=None), params
        )
        s_zero = retrieval_score(
            0.8, PoolCandidate(factor_id="f", max_pool_corr=0.0), params
        )
        assert s_none == pytest.approx(s_zero)

    def test_negative_corr_does_not_bonus(self):
        """max(0, ·) 截断：负相关不额外加分（-0.9 与 0 同罚则）。"""
        params = ScoringParams()
        s_neg = retrieval_score(
            0.8, PoolCandidate(factor_id="f", max_pool_corr=-0.9), params
        )
        s_zero = retrieval_score(
            0.8, PoolCandidate(factor_id="f", max_pool_corr=0.0), params
        )
        assert s_neg == pytest.approx(s_zero)


class TestRankAndSelect:
    def _pool(self):
        return [
            PoolCandidate(factor_id="a", icir=0.9, times_retrieved=0),
            PoolCandidate(factor_id="b", icir=0.5, times_retrieved=0),
            PoolCandidate(factor_id="c", icir=0.2, times_retrieved=0),
            PoolCandidate(factor_id="d", icir=None, times_retrieved=0),  # 无 ICIR
        ]

    def test_rank_orders_by_score_desc(self):
        ranked = rank_candidates(self._pool(), ScoringParams())
        assert [s.candidate.factor_id for s in ranked] == ["a", "b", "c"]

    def test_missing_icir_excluded_from_recall(self):
        ranked = rank_candidates(self._pool(), ScoringParams())
        assert "d" not in [s.candidate.factor_id for s in ranked]

    def test_select_topk_truncates_and_respects_exclude(self):
        picked = select_topk(self._pool(), k=2, exclude={"a"}, params=ScoringParams())
        assert [s.candidate.factor_id for s in picked] == ["b", "c"]

    def test_deterministic_tie_break_by_factor_id(self):
        pool = [
            PoolCandidate(factor_id="z", icir=0.5),
            PoolCandidate(factor_id="m", icir=0.5),
            PoolCandidate(factor_id="a", icir=0.5),
        ]
        ranked = rank_candidates(pool, ScoringParams())
        assert [s.candidate.factor_id for s in ranked] == ["a", "m", "z"]

    def test_empty_pool_selects_nothing(self):
        assert select_topk([], k=5) == []

    def test_k_zero_or_negative_selects_nothing(self):
        assert select_topk(self._pool(), k=0) == []
        assert select_topk(self._pool(), k=-1) == []


class TestRenderDigest:
    def _entries(self, n=3):
        return [
            DigestEntry(
                factor_name=f"factor_{i}",
                formula=f"close/mean(close,{i + 5})",
                ic=0.01 * (i + 1),
                icir=0.5 - 0.1 * i,
                pfs=0.9,
                round_label=f"第{i + 1}轮",
            )
            for i in range(n)
        ]

    def test_renders_name_formula_and_metrics(self):
        text = render_digest(self._entries(1))
        assert "factor_0" in text
        assert "close/mean(close,5)" in text
        assert "0.5000" in text  # ICIR
        assert "第1轮" in text

    def test_missing_metric_shows_dash_never_zero(self):
        entries = [
            DigestEntry(factor_name="f", formula="x", ic=None, icir=None, pfs=None)
        ]
        text = render_digest(entries)
        assert "—" in text
        assert "0.0000" not in text

    def test_max_chars_drops_lowest_first_and_renumbers(self):
        entries = self._entries(6)
        full = render_digest(entries, max_chars=100000)
        short = render_digest(entries, max_chars=len(full) // 2)
        assert len(short) <= len(full) // 2
        assert "factor_0" in short  # 最高分保留
        assert "factor_5" not in short  # 最低分被砍
        assert "1. `factor_0`" in short  # 重新编号不留空洞

    def test_empty_entries_returns_empty_string(self):
        assert render_digest([]) == ""
