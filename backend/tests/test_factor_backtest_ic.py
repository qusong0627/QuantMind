"""T-FB-03 单测：日度 IC 序列与组合曲线（口径钉死）。

矩阵/报告的数字必须与**挖掘阶段**和**现行 CN 回测**可比——IC 口径已在
``alpha_agent._vectorized_daily_spearman_ic`` 钉过一次（挖掘侧同源），
本模块重写为「同时产出日度序列」，因此必须逐值复现旧实现的 5 元组：
1e-12 容差断言，改一边两侧都红。
"""

import json
import math

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_backtest import ic as I

pytestmark = pytest.mark.unit


def _mk_panel(n_days=250, n_inst=40, seed=7, corr=0.0, with_nan=False):
    """构造 (datetime, instrument) 面板：f 随机、r = corr*f + 噪声。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    insts = [f"SH{600000 + i}" for i in range(n_inst)]
    idx = pd.MultiIndex.from_product([dates, insts], names=["datetime", "instrument"])
    f = pd.Series(rng.normal(size=len(idx)), index=idx)
    r = pd.Series(
        corr * f.values + (1.0 - abs(corr)) * rng.normal(size=len(idx)), index=idx
    )
    if with_nan:
        mask = rng.random(len(idx)) < 0.02
        f[mask] = np.nan
        r[mask] = np.nan
    return f, r


# ── 与现行实现逐值同口径 ─────────────────────────────────────────────


def test_ic_stats_matches_alpha_agent_vectorized():
    """5 元组 (ic, rank_ic, icir, rank_icir, n_obs) 与现行实现 1e-12 相等。"""
    # Arrange
    from backend.services.engine.routers.alpha_agent import (
        _vectorized_daily_spearman_ic,
    )

    f, r = _mk_panel(corr=0.3, with_nan=True)
    # Act
    legacy = _vectorized_daily_spearman_ic(f, r)
    mine = I.daily_ic_stats(f, r)
    # Assert
    assert math.isclose(mine["ic"], legacy[0], abs_tol=1e-12)
    assert math.isclose(mine["rank_ic"], legacy[1], abs_tol=1e-12)
    assert math.isclose(mine["icir"], legacy[2], abs_tol=1e-12)
    assert math.isclose(mine["rank_icir"], legacy[3], abs_tol=1e-12)
    assert mine["n_obs"] == legacy[4]


def test_daily_ic_series_matches_manual_spearman_per_day():
    """日度序列 = 每日秩相关（对逐日手工计算逐值断言）。"""
    # Arrange
    f, r = _mk_panel(n_days=30, n_inst=25, corr=0.5)
    df = pd.DataFrame(
        {"f": f.values, "r": r.values, "date": f.index.get_level_values("datetime")}
    )
    expected = df.groupby("date").apply(
        lambda g: g["f"].corr(g["r"], method="spearman"), include_groups=False
    )
    # Act
    got = I.daily_ic_series(f, r)
    # Assert
    assert len(got) == len(expected)
    for day in expected.index:
        assert math.isclose(
            float(got.loc[day]), float(expected.loc[day]), abs_tol=1e-10
        )


def test_perfect_factor_has_unit_ic_every_day():
    """f 与 r 完全同序：每日 IC = 1。"""
    # Arrange
    f, _ = _mk_panel(n_days=20, n_inst=30)
    # Act
    s = I.daily_ic_series(f, f)
    stats = I.daily_ic_stats(f, f)
    # Assert
    assert np.allclose(s.values, 1.0)
    assert math.isclose(stats["ic"], 1.0, abs_tol=1e-12)


def test_single_instrument_day_is_dropped():
    """某日只有 1 只标的：当日相关系数无定义 → 从序列剔除，不产假数。"""
    # Arrange
    f, r = _mk_panel(n_days=2, n_inst=5)
    day0 = f.index.get_level_values("datetime")[0]
    drop_mask = f.index.get_level_values("datetime") == day0
    keep = ~drop_mask | (
        f.index.get_level_values("instrument")
        == f.index.get_level_values("instrument")[0]
    )
    f2, r2 = f[keep], r[keep]
    f2.loc[(day0, f2.index.get_level_values("instrument")[0])] = 1.0  # 该日仅 1 行
    # Act
    s = I.daily_ic_series(f2, r2)
    # Assert
    assert day0 not in s.index


# ── 组合曲线 ─────────────────────────────────────────────────────────


def test_turnover_zero_when_membership_stable():
    """标的排名恒定：头部长仓成员不变 → 日交易比例（成本口径）恒 0。"""
    # Arrange
    dates = pd.bdate_range("2024-01-01", periods=30)
    insts = [f"SH{600000 + i}" for i in range(20)]
    idx = pd.MultiIndex.from_product([dates, insts], names=["datetime", "instrument"])
    level = pd.Series({inst: float(i) for i, inst in enumerate(insts)})
    f = pd.Series([level[i] for _, i in idx], index=idx)
    r = pd.Series(np.linspace(-0.01, 0.01, len(idx)), index=idx)
    # Act
    curves = I.portfolio_curves(f, r, top_pct=0.3)
    # Assert
    assert (curves["traded"].dropna() == 0.0).all()


def test_curves_have_long_short_buckets_and_coverage():
    """曲线含多头/空头/多空/分位桶/覆盖数，索引为交易日。"""
    # Arrange
    f, r = _mk_panel(n_days=40, n_inst=50, corr=0.4)
    # Act
    curves = I.portfolio_curves(f, r, top_pct=0.3, n_buckets=5)
    # Assert
    for col in ("ret_long", "ret_short", "ret_ls", "traded", "coverage", "q1", "q5"):
        assert col in curves.columns
    assert len(curves) == 40
    # 单调因子（f 与 r 完全同序）时 q5 累计应高于 q1
    curves_mono = I.portfolio_curves(f, f, top_pct=0.3, n_buckets=5)
    q5_cum = (1 + curves_mono["q5"]).prod()
    q1_cum = (1 + curves_mono["q1"]).prod()
    assert q5_cum > q1_cum


def test_perf_metrics_gross_matches_legacy_sharpe_formula():
    """夏普/年化与现行实现同公式：mean*252 / mean/std*sqrt(252)。"""
    # Arrange
    f, r = _mk_panel(n_days=120, n_inst=40, corr=0.2)
    curves = I.portfolio_curves(f, r)
    ret = curves["ret_long"].dropna()
    # Act
    perf = I.perf_metrics(curves["ret_long"], curves["traded"], cost_bps=0.0)
    # Assert
    assert math.isclose(perf["ann_return"], float(ret.mean() * 252), rel_tol=1e-12)
    legacy_sharpe = float(ret.mean() / (ret.std(ddof=1) + 1e-8) * np.sqrt(252))
    assert math.isclose(perf["sharpe"], legacy_sharpe, rel_tol=1e-9)


def test_perf_metrics_cost_reduces_net_return():
    """扣费只动净值口径：费率越高净收益越低，0 bp 时净=毛。"""
    # Arrange
    f, r = _mk_panel(n_days=120, n_inst=40, corr=0.3)
    curves = I.portfolio_curves(f, r)
    # Act
    gross = I.perf_metrics(curves["ret_long"], curves["traded"], cost_bps=0.0)
    net20 = I.perf_metrics(curves["ret_long"], curves["traded"], cost_bps=20)
    net50 = I.perf_metrics(curves["ret_long"], curves["traded"], cost_bps=50)
    # Assert
    assert math.isclose(gross["ann_return_net"], gross["ann_return"], rel_tol=1e-12)
    assert net20["ann_return_net"] < gross["ann_return_net"]
    assert net50["ann_return_net"] < net20["ann_return_net"]


# ── 序列载荷（JSON 安全）─────────────────────────────────────────────


def test_series_payload_is_json_safe_and_none_for_nan():
    """载荷必须可 json.dumps（NaN 一律 None——JSON 无 NaN，前端显示「—」）。"""
    # Arrange
    f, r = _mk_panel(n_days=60, n_inst=30, corr=0.2, with_nan=True)
    curves = I.portfolio_curves(f, r)
    ic_series = I.daily_ic_series(f, r)
    # Act
    payload = I.build_series_payload(curves, ic_series, cost_bps=20)
    dumped = json.dumps(payload)
    # Assert
    assert len(payload["dates"]) == len(curves)
    assert len(payload["nav_long"]) == len(curves)
    assert set(payload["q_curves"]) == {"q1", "q2", "q3", "q4", "q5"}
    assert payload["meta"]["cost_bps"] == 20
    assert "NaN" not in dumped and "Infinity" not in dumped
    assert payload["bench"] == "equal_weight"


def test_ic_cum_is_cumulative_sum_over_daily_ic():
    """累计 IC = 日度 IC 的累计和（缺失日跳过）。"""
    # Arrange
    f, r = _mk_panel(n_days=50, n_inst=30, corr=0.2)
    curves = I.portfolio_curves(f, r)
    ic_series = I.daily_ic_series(f, r)
    # Act
    payload = I.build_series_payload(curves, ic_series, cost_bps=20)
    # Assert
    ic_vals = [v for v in payload["ic"] if v is not None]
    expected_cum = np.cumsum(ic_vals)
    got_cum = [v for v in payload["ic_cum"] if v is not None]
    assert np.allclose(got_cum, expected_cum, atol=1e-12)
