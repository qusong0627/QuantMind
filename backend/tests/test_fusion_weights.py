"""机构级融合权重引擎 —— 纯函数契约测试（TDD 先行）。

口径要点（见 docs/机构级模型融合_设计方案.md §3）：
1. 默认策略 icir_shrunk = 「滚动 ICIR 倾斜 × 多样性惩罚」向成员平均分收缩，
   收缩强度由每成员自己的有效样本量决定（n/(n+K)）——样本少的成员自动退回平均，
   绝不拿 6 天 IC 当 120 天的证据。
2. 单成员上限（cap）用「水位填充」再分配；有效上限 = max(配置值, 1/存活成员数)
   ——两个成员时 0.40 在数学上不可行（0.4+0.4<1），必须放宽。
3. 阈值清理在封顶之前按收缩预权判定：预权 < 0.02 的成员先剔除再重归一，
   否则会被上限再分配「救活」。
4. 近似重复（相关 > 0.98）剔除的只是该成员的 ICIR 倾斜项（raw=0），
   它仍保留向平均收缩的份额（相关性一旦破裂它还是分散器）。
5. 全部输出：权重非负、和恒为 1、对成员顺序不敏感（确定性）。
"""

from __future__ import annotations

import math

import pytest

from backend.shared.fusion_weights import (
    FusionWeightConfig,
    compute_fusion_weights,
)


def _series(n: int, value: float, alternation: float = 0.0) -> list[float]:
    """构造 n 天 IC 序列：均值=value；alternation>0 时交替 ± 制造 std。

    注意：ddof=1 的样本 std 会把所有成员的 ICIR 同乘一个公共因子
    （sqrt(n/(n-1))），而归一化后权重对该公共因子免疫，故精确值断言不受影响。
    """
    if alternation <= 0:
        return [value] * n
    return [value + (alternation if i % 2 == 0 else -alternation) for i in range(n)]


def _assert_valid(fw) -> None:
    total = sum(fw.weights.values())
    assert math.isclose(total, 1.0, abs_tol=1e-9), f"权重和={total}"
    assert all(w >= 0 for w in fw.weights.values()), fw.weights


class TestBasicContract:
    def test_rejects_fewer_than_two_members(self):
        with pytest.raises(ValueError):
            compute_fusion_weights({"a": _series(60, 0.05, 0.02)})

    def test_rejects_unknown_strategy(self):
        with pytest.raises(ValueError):
            compute_fusion_weights(
                {"a": _series(60, 0.05, 0.02), "b": _series(60, 0.02, 0.01)},
                config=FusionWeightConfig(strategy="magic"),
            )

    def test_recent_ic_alias_maps_to_icir_shrunk(self):
        """旧 recent_ic 策略（其每日刷新任务已删）直接映射到机构级默认策略。"""
        fw = compute_fusion_weights(
            {"a": _series(60, 0.05, 0.02), "b": _series(60, 0.02, 0.01)},
            config=FusionWeightConfig(strategy="recent_ic"),
        )
        assert fw.strategy == "icir_shrunk"

    def test_equal_strategy_is_uniform_and_order_insensitive(self):
        fw = compute_fusion_weights(
            {"a": _series(60, 0.05), "b": _series(60, 0.02), "c": _series(60, 0.01)},
            config=FusionWeightConfig(strategy="equal"),
        )
        assert fw.weights == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
        _assert_valid(fw)
        fw2 = compute_fusion_weights(
            {"c": _series(60, 0.01), "a": _series(60, 0.05), "b": _series(60, 0.02)},
            config=FusionWeightConfig(strategy="equal"),
        )
        assert fw.weights == fw2.weights

    def test_manual_strategy_normalizes_and_requires_all_members(self):
        fw = compute_fusion_weights(
            {"a": _series(60, 0.05, 0.02), "b": _series(60, 0.02, 0.01)},
            config=FusionWeightConfig(strategy="manual", manual_weights={"a": 2.0, "b": 1.0}),
        )
        assert fw.weights["a"] == pytest.approx(2 / 3)
        assert fw.weights["b"] == pytest.approx(1 / 3)
        _assert_valid(fw)
        with pytest.raises(ValueError):
            compute_fusion_weights(
                {"a": _series(60, 0.05), "b": _series(60, 0.02)},
                config=FusionWeightConfig(strategy="manual", manual_weights={"a": 1.0}),
            )

    def test_nan_and_none_are_skipped_in_stats(self):
        s = _series(60, 0.05, 0.02)
        s[0] = float("nan")
        s[1] = None  # type: ignore[assignment]
        fw = compute_fusion_weights(
            {"a": s, "b": _series(60, 0.02, 0.01)},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        diag = {d.member_id: d for d in fw.diagnostics}
        assert diag["a"].n_days == 58
        _assert_valid(fw)


class TestIcirShrunk:
    def test_shrinkage_then_cap_water_filling_exact(self):
        """3 成员各 60 天（λ=0.5）：ICIR 2/1/0 → 预权 1/2,1/3,1/6；cap=0.4 后 0.4/0.4/0.2。"""
        fw = compute_fusion_weights(
            {
                "a": _series(60, 0.10, 0.05),   # icir = 2
                "b": _series(60, 0.05, 0.05),   # icir = 1
                "c": _series(60, 0.0, 0.05),    # icir = 0
            },
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        assert fw.weights["a"] == pytest.approx(0.4, abs=1e-9)
        assert fw.weights["b"] == pytest.approx(0.4, abs=1e-9)
        assert fw.weights["c"] == pytest.approx(0.2, abs=1e-9)
        _assert_valid(fw)

    def test_insufficient_days_member_gets_no_tilt(self):
        """5 天样本的成员即使 ICIR 再高也不倾斜（raw=0），只保留向平均收缩的份额。"""
        fw = compute_fusion_weights(
            {
                "a": _series(60, 0.10, 0.05),
                "b": _series(60, 0.05, 0.05),
                "c": _series(5, 0.50, 0.10),    # 5 天、ICIR 极高 → 不得倾斜
            },
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        diag = {d.member_id: d for d in fw.diagnostics}
        assert diag["c"].raw == 0.0
        assert diag["c"].reason == "insufficient_days"
        assert fw.weights["c"] < 1 / 3  # 少于等权份额
        assert fw.weights["a"] > fw.weights["b"] > fw.weights["c"]
        _assert_valid(fw)

    def test_all_nonpositive_icir_falls_back_to_equal_with_warning(self):
        fw = compute_fusion_weights(
            {
                "a": _series(60, -0.10, 0.05),
                "b": _series(60, -0.02, 0.01),
                "c": _series(60, 0.0, 0.05),
            },
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        for w in fw.weights.values():
            assert w == pytest.approx(1 / 3)
        assert "nonpositive" in fw.warning
        _assert_valid(fw)

    def test_duplicate_member_keeps_prior_share_but_loses_tilt(self):
        """a、b 相关 0.99：ICIR 低者（b）raw 清零、标注 duplicate，只保留收缩份额。

        cap 放宽到 1.0 隔离水位填充的干扰，精确断言收缩后份额 (1-λ)/m。
        """
        fw = compute_fusion_weights(
            {
                "a": _series(60, 0.01, 0.05),   # icir = 0.2
                "b": _series(60, 0.005, 0.05),  # icir = 0.1
                "c": _series(60, 0.03, 0.05),   # icir = 0.6
            },
            corr={"a": {"b": 0.99, "c": 0.1}, "b": {"a": 0.99, "c": 0.1}, "c": {"a": 0.1, "b": 0.1}},
            config=FusionWeightConfig(strategy="icir_shrunk", max_weight=1.0),
        )
        diag = {d.member_id: d for d in fw.diagnostics}
        assert diag["b"].raw == 0.0
        assert diag["b"].reason == "duplicate"
        assert diag["a"].reason == ""
        # λ=0.5 全等 → b 只拿 (1-λ)/m = 1/6
        assert fw.weights["b"] == pytest.approx((1 - 60 / 120) / 3, abs=1e-9)
        _assert_valid(fw)

    def test_correlation_penalty_downweights_crowded_member(self):
        """c 与 a、b 平均相关 0.9 → 惩罚后 raw 降低 → 权重低于无相关情形。"""
        base = compute_fusion_weights(
            {"a": _series(60, 0.10, 0.05), "b": _series(60, 0.08, 0.05), "c": _series(60, 0.10, 0.05)},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        crowded = compute_fusion_weights(
            {"a": _series(60, 0.10, 0.05), "b": _series(60, 0.08, 0.05), "c": _series(60, 0.10, 0.05)},
            corr={"a": {"b": 0.1, "c": 0.9}, "b": {"a": 0.1, "c": 0.9}, "c": {"a": 0.9, "b": 0.9}},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        assert crowded.weights["c"] < base.weights["c"]

    def test_tiny_weight_is_dropped_before_cap_renormalized(self):
        """长历史 + 非正 ICIR 的成员收缩份额 <0.02 → 先剔除再重归一。

        剔除必须在封顶之前：否则上限再分配会把它从 0.0146 抬到 0.5，永远剔不掉。
        """
        fw = compute_fusion_weights(
            {
                "a": _series(2000, 0.10, 0.05),  # icir = 2，λ=2000/2060
                "b": _series(2000, 0.0, 0.05),   # icir = 0 → 无倾斜
            },
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        diag = {d.member_id: d for d in fw.diagnostics}
        assert diag["b"].dropped is True
        assert diag["b"].reason == "nonpositive_icir"  # 保留剔除根因，dropped 单独标注
        assert fw.weights["b"] == 0.0
        assert fw.weights["a"] == pytest.approx(1.0)
        _assert_valid(fw)

    def test_all_insufficient_days_warns_root_cause_not_nonpositive(self):
        """新融合模型的常见态：全员样本不足 → 等权，警告须指向 insufficient_days。

        2026-10-09 真实冒烟（nativft×lightgbm 各 12/10 天 IC）暴露：原先一律报
        "nonpositive"，而两成员 ICIR 其实为正（0.62/0.54）——警告与根因不符。
        """
        fw = compute_fusion_weights(
            {"a": _series(12, 0.05, 0.02), "b": _series(10, 0.10, 0.05)},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        assert fw.weights == pytest.approx({"a": 0.5, "b": 0.5})
        assert fw.warning == "all_insufficient_days_fallback_equal"
        _assert_valid(fw)

    def test_deterministic_across_member_order(self):
        fw1 = compute_fusion_weights(
            {"a": _series(60, 0.10, 0.05), "b": _series(60, 0.05, 0.05)},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        fw2 = compute_fusion_weights(
            {"b": _series(60, 0.05, 0.05), "a": _series(60, 0.10, 0.05)},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        assert fw1.weights == fw2.weights

    def test_two_member_cap_relaxes_to_feasible_share(self):
        """m=2 时 0.40 上限不可行（0.4+0.4<1），有效上限=max(0.4, 1/2)=0.5。"""
        fw = compute_fusion_weights(
            {"a": _series(60, 0.10, 0.05), "b": _series(20, 0.001, 0.05)},
            config=FusionWeightConfig(strategy="icir_shrunk", min_days=10),
        )
        assert fw.weights["a"] == pytest.approx(0.5, abs=1e-9)
        assert fw.weights["b"] == pytest.approx(0.5, abs=1e-9)
        _assert_valid(fw)

    def test_diagnostics_carry_ic_stats(self):
        fw = compute_fusion_weights(
            {"a": _series(60, 0.10, 0.05), "b": _series(60, 0.05, 0.05)},
            config=FusionWeightConfig(strategy="icir_shrunk"),
        )
        diag = {d.member_id: d for d in fw.diagnostics}
        assert diag["a"].ic_mean == pytest.approx(0.10, abs=1e-9)
        assert diag["a"].icir == pytest.approx(2.0, rel=0.02)  # ddof=1：2*sqrt(60/59)
        assert diag["a"].n_days == 60
        assert diag["a"].weight == pytest.approx(fw.weights["a"])
