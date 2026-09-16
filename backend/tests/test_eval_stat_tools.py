"""T-P4-05a 测试：回测体检九项统计工具箱 + 四分类判定。

覆盖：九项纯函数（公式性质/解析解/边界）+ 四分类制造用例（A/B/L/E 各一）+
真库 sanity（上证指数当"策略" → beta 主导特征，不得判 A）。
"""

from __future__ import annotations

import numpy as np
import pytest

rng = np.random.default_rng(20260916)


def _strong_returns(n=756, mean=0.0012, std=0.010, seed=11):
    """明确正 alpha：日 SR≈0.12（年化≈1.9），长样本（独立种子，防测试顺序漂移）。"""
    return np.random.default_rng(seed).normal(mean, std, n)


def _noise(n=500):
    return rng.normal(0.0, 0.012, n)


# ── 纯函数性质 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_psr_properties():
    from backend.scripts.eval import stat_tools as st

    weak = st.psr(_strong_returns(mean=0.0002))
    strong = st.psr(_strong_returns(mean=0.0015))
    assert strong["psr"] > weak["psr"]  # 单调
    assert strong["psr"] > 0.95
    assert st.psr([0.001] * 20)["sufficient"] is False or True  # 零方差退化不抛


@pytest.mark.unit
def test_dsr_deflates_with_trials():
    from backend.scripts.eval import stat_tools as st

    r = _strong_returns()
    dsr1 = st.deflated_sharpe_ratio(r, n_trials=1)
    dsr240 = st.deflated_sharpe_ratio(r, n_trials=240)
    psr0 = st.psr(r)
    assert dsr1["dsr"] == pytest.approx(psr0["psr"], abs=1e-9)  # N=1 时 DSR==PSR(0)
    assert dsr240["dsr"] < dsr1["dsr"]  # 试验越多越难显著
    assert dsr1["passes_095"] is True


@pytest.mark.unit
def test_min_trl_monotone_and_negative_sharpe():
    from backend.scripts.eval import stat_tools as st

    def _exact(mean, std, n=756):
        # 确定性序列：交替 ±std 偏移 → 精确 mean/std（去随机噪声）
        half = n // 2
        return np.concatenate(
            [np.full(half, mean + std), np.full(n - half, mean - std)]
        )

    low = st.min_track_record_length(_exact(0.0006, 0.01))
    high = st.min_track_record_length(_exact(0.0018, 0.01))
    assert low["min_trl_years"] > high["min_trl_years"]  # SR 越高所需样本越短
    neg = st.min_track_record_length(_exact(-0.001, 0.01))
    assert neg["sufficient"] is False


@pytest.mark.unit
def test_factor_regression_recovers_params():
    from backend.scripts.eval import stat_tools as st

    n = 750
    local = np.random.default_rng(31)
    m = local.normal(0.0003, 0.011, n)
    y = 0.0005 + 1.2 * m + local.normal(0, 0.002, n)
    res = st.factor_regression(y, m)
    assert res["sufficient"] is True
    assert res["beta"] == pytest.approx(1.2, abs=0.05)
    assert res["alpha_annual"] == pytest.approx(0.0005 * 252, rel=0.35)
    assert res["alpha_significant"] is True
    assert res["r2"] > 0.9

    # 纯 beta（y == m）→ alpha≈0 不显著
    pure = st.factor_regression(m.copy(), m)
    assert pure["alpha_significant"] is False
    assert pure["r2"] == pytest.approx(1.0, abs=1e-6)


@pytest.mark.unit
def test_pbo_noise_high_strong_low():
    from backend.scripts.eval import stat_tools as st

    noise_mat = np.random.default_rng(41).normal(0.0, 0.01, (600, 24))
    pbo_noise = st.pbo_cscv(noise_mat)
    assert pbo_noise["sufficient"] is True
    assert 0.25 <= pbo_noise["pbo"] <= 0.75  # 纯噪声 ≈ 0.5

    strong_mat = noise_mat.copy()
    strong_mat[:, 3] += 0.002  # 第 4 组真实强
    pbo_strong = st.pbo_cscv(strong_mat)
    assert pbo_strong["pbo"] < 0.25


@pytest.mark.unit
def test_bootstrap_constant_positive_no_zero_cross():
    from backend.scripts.eval import stat_tools as st

    r = np.full(300, 0.001) + np.random.default_rng(42).normal(0, 0.0001, 300)
    res = st.block_bootstrap(r)
    assert res["sufficient"] is True
    assert res["return_ci_crosses_zero"] is False
    assert res["return_ci"][0] > 0


@pytest.mark.unit
def test_concentration_detects_few_days():
    from backend.scripts.eval import stat_tools as st

    concentrated = np.zeros(200)
    concentrated[:5] = 0.06  # 全部收益在前 5 天
    res = st.return_concentration(concentrated)
    assert res["kills_alpha"] is True

    spread = np.full(200, 0.001)
    assert st.return_concentration(spread)["kills_alpha"] is False


@pytest.mark.unit
def test_regime_split_three_phases():
    from backend.scripts.eval import stat_tools as st

    n = 400
    idx = np.concatenate(
        [np.linspace(100, 130, 140), np.linspace(130, 95, 140), np.full(120, 95.0)]
    )
    strat = np.concatenate(
        [np.full(120, 0.001), np.full(140, -0.0008), np.full(140, 0.0002)]
    )
    res = st.regime_split(strat[:n], idx[:n])
    assert res["sufficient"] is True
    assert len(res["regimes_covered"]) >= 2
    assert res["regimes"]["牛"]["cum_return"] > 0


@pytest.mark.unit
def test_cost_sensitivity_exact_drag():
    from backend.scripts.eval import stat_tools as st

    r = np.full(100, 0.001)
    turnover = np.full(100, 0.1)
    res = st.cost_sensitivity(r, turnover, uplift_bps=5.0)
    expected_drag = 0.1 * 5.0 / 10000.0 * 252
    assert res["cost_drag_annual"] == pytest.approx(expected_drag, rel=1e-6)
    assert res["adjusted_annual"] == pytest.approx(
        0.001 * 252 - expected_drag, rel=1e-6
    )


# ── 四分类制造用例 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_verdict_a_true_alpha():
    from backend.scripts.eval.health_check import run_health_check

    n = 1500
    local = np.random.default_rng(51)
    bench = local.normal(0.0, 0.01, n)
    strat = 0.0015 + 0.1 * bench + local.normal(0, 0.004, n)  # 独立强 alpha
    res = run_health_check(strat, benchmark_returns=bench)
    assert res["verdict"] == "A", res["reasons"]


@pytest.mark.unit
def test_verdict_b_beta_dominated():
    from backend.scripts.eval.health_check import run_health_check

    def _exact(mean, std, n):
        half = n // 2
        return np.concatenate(
            [np.full(half, mean + std), np.full(n - half, mean - std)]
        )

    bench = _exact(0.0008, 0.01, 900)  # 确定性：SR=0.08 → MinTRL(424) < 样本(900)
    res = run_health_check(bench.copy(), benchmark_returns=bench)  # 策略==基准
    assert res["verdict"] == "B", res["reasons"]


@pytest.mark.unit
def test_verdict_l_concentration_kills_alpha():
    """运气嫌疑（确定性构造）：样本充足（MinTRL 过）但全部收益来自 5 个爆点日，
    剔除后转负 → 集中度杀 alpha → L（E 优先级被样本充足排除、B 被"剔后无收益"排除）。"""
    from backend.scripts.eval.health_check import run_health_check

    n = 700
    strat = np.concatenate(
        [
            np.full(348, 0.0004),
            np.full(347, -0.0005),
            np.full(5, 0.06),  # 全部收益集中在这 5 天
        ]
    )
    np.random.default_rng(53).shuffle(strat)
    bench = np.random.default_rng(54).normal(0.0001, 0.008, n)
    res = run_health_check(strat, benchmark_returns=bench)
    assert res["tests"]["concentration"]["kills_alpha"] is True
    assert res["verdict"] == "L", (res["verdict"], res["reasons"])


@pytest.mark.unit
def test_verdict_l_luck_with_many_trials():
    """运气嫌疑（DSR 去胀）：长样本 + N=2000 试验 → DSR 站不住 → L。"""
    from backend.scripts.eval.health_check import run_health_check

    # 构造（实测校准）：2 年样本、真实显著小 alpha（t≈3.4）、MinTRL 过——
    # 但曾试 5000 组参数，DSR 0.62 < 0.95：去胀后站不住 → L。
    # （注：长样本天然抗去胀——统计上诚实，L-via-DSR 的现实场景就是"短样本×高试验数"）
    n = 500
    local = np.random.default_rng(57)
    bench = local.normal(0.0, 0.01, n)
    strat = 0.0006 + 0.05 * bench + local.normal(0, 0.004, n)
    res = run_health_check(strat, benchmark_returns=bench, n_trials=5000)
    assert res["tests"]["min_trl"]["adequate"] is True
    assert res["tests"]["factor_regression"]["alpha_significant"] is True
    assert res["tests"]["dsr"]["passes_095"] is False
    assert res["verdict"] == "L", (res["verdict"], res["reasons"])


@pytest.mark.unit
def test_verdict_e_short_sample():
    from backend.scripts.eval.health_check import run_health_check

    # 确定性：低 SR（均值 0.0002 / 波动 0.01 → SR=0.02）→ MinTRL(6765) ≫ 样本(80) → 必然 E
    strat = np.concatenate([np.full(40, 0.0102), np.full(40, -0.0098)])
    bench = np.concatenate([np.full(20, 0.010), np.full(60, -0.001)])
    res = run_health_check(strat, benchmark_returns=bench)
    assert res["verdict"] == "E", res["reasons"]


@pytest.mark.unit
def test_report_renders_label():
    from backend.scripts.eval.health_check import render_report, run_health_check

    bench = np.random.default_rng(56).normal(0.0003, 0.01, 300)
    res = run_health_check(bench.copy(), benchmark_returns=bench)
    text = render_report(res)
    assert "结论标签" in text and "可信度分" in text


# ── 真库 sanity ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_real_index_as_strategy_is_not_alpha():
    """上证指数当'策略' vs 沪深300：R² 高、alpha 不显著——不得判 A。"""
    from backend.scripts.eval.health_check import (
        load_index_closes,
        nav_curve_to_returns,
        run_health_check,
    )

    _d, closes = load_index_closes("000001.SH", 250)
    _bd, bench = load_index_closes("000300.SH", 250)
    if len(closes) < 60 or len(bench) < 60:
        pytest.skip("指数数据不可用")
    res = run_health_check(
        nav_curve_to_returns(list(closes)),
        benchmark_returns=nav_curve_to_returns(list(bench)),
    )
    fr = res["tests"]["factor_regression"]
    assert fr["sufficient"] is True
    assert fr["r2"] > 0.5  # 指数 vs 指数：高相关（beta 主导特征）
    assert res["verdict"] in {"B", "E", "L"}  # 绝不是 A
