"""分数级中性化纯函数测试（金样 + 独立复算 + 降级边界）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.shared.neutralization.core import (
    NeutralizationConfig,
    NeutralizationInputError,
    STATUS_DEGRADED,
    STATUS_OK,
    neutralize_scores,
)


def _fixture(n: int = 400, seed: int = 42):
    rng = np.random.RandomState(seed)
    symbols = [f"SH{600000 + i:06d}" for i in range(n)]
    log_mv = rng.normal(22, 1.2, n)
    beta60 = rng.normal(1.0, 0.3, n)
    mom20 = rng.normal(0.0, 0.15, n)
    scores = 0.8 * log_mv + 0.5 * beta60 + 0.3 * mom20 + rng.normal(0, 0.5, n)
    score_s = pd.Series(scores, index=symbols)
    controls = pd.DataFrame(
        {"log_mv": log_mv, "beta60": beta60, "mom20": mom20}, index=symbols
    )
    return score_s, controls


def _config(**kwargs) -> NeutralizationConfig:
    return NeutralizationConfig(**kwargs)


def test_golden_first_values():
    scores, controls = _fixture()
    result = neutralize_scores(scores, controls, _config())
    assert result.status == STATUS_OK
    expected = [0.4425, 0.3075, 0.5350, 0.6700, 0.5650, 0.4925, 0.4575, 0.1650]
    assert result.neutral_score.iloc[:8].round(6).tolist() == pytest.approx(
        expected, abs=1e-6
    )


def test_exposure_below_threshold_and_monotone_range():
    scores, controls = _fixture()
    result = neutralize_scores(scores, controls, _config())
    assert result.status == STATUS_OK
    for value in result.diagnostics["exposure"].values():
        assert value is not None and abs(value) < 0.05
    neutral = result.neutral_score.dropna()
    assert neutral.min() > 0.0
    assert neutral.max() <= 1.0
    assert neutral.index.is_unique


def test_scaling_invariance():
    scores, controls = _fixture()
    base = neutralize_scores(scores, controls, _config())
    scaled = neutralize_scores(scores * 7.5 + 123.0, controls, _config())
    pd.testing.assert_series_equal(
        base.neutral_score, scaled.neutral_score, check_names=False
    )


def test_missing_controls_excluded_and_reported():
    scores, controls = _fixture()
    controls = controls.copy()
    controls.iloc[:10, 0] = np.nan
    result = neutralize_scores(scores, controls, _config())
    assert result.status == STATUS_OK
    assert result.neutral_score.iloc[:10].isna().all()
    assert result.diagnostics["n_symbols"] == len(scores) - 10
    assert result.diagnostics["coverage"] < 1.0


def test_insufficient_sample_degrades():
    scores, controls = _fixture(n=100)
    result = neutralize_scores(scores, controls, _config())
    assert result.status == STATUS_DEGRADED
    assert "样本不足" in (result.reason or "")
    assert result.neutral_score.isna().all()


def test_constant_control_degrades():
    scores, controls = _fixture()
    controls = controls.copy()
    controls["beta60"] = 0.5
    result = neutralize_scores(scores, controls, _config())
    assert result.status == STATUS_DEGRADED
    assert "病态" in (result.reason or "")


def test_duplicate_index_raises():
    scores, controls = _fixture(n=400)
    bad = pd.concat([scores, scores.iloc[:1]])
    with pytest.raises(NeutralizationInputError):
        neutralize_scores(bad, controls, _config())


def test_missing_control_column_raises():
    scores, controls = _fixture()
    with pytest.raises(NeutralizationInputError):
        neutralize_scores(scores, controls.drop(columns=["mom20"]), _config())


def test_config_version_stable_and_sensitive():
    a = NeutralizationConfig()
    b = NeutralizationConfig()
    assert a.config_version == b.config_version
    c = NeutralizationConfig(controls=("log_mv", "beta60"))
    assert c.config_version != a.config_version


def test_matches_independent_normal_equations():
    """独立实现（正规方程 solve）复算两遍残差，与被测 lstsq 路径一致。"""
    scores, controls = _fixture()
    cfg = NeutralizationConfig()
    result = neutralize_scores(scores, controls, cfg)

    valid = controls.notna().all(axis=1)
    x = controls.loc[valid]
    y = scores.loc[valid].rank(pct=True, method="average")

    def rank_pct(s: pd.Series) -> pd.Series:
        return s.rank(pct=True, method="average")

    def winsor(s: pd.Series, pct: float) -> pd.Series:
        lo, hi = s.quantile(pct), s.quantile(1 - pct)
        return s.clip(lower=lo, upper=hi)

    xr = pd.DataFrame(
        {c: rank_pct(winsor(x[c], cfg.winsor_pct)) for c in cfg.controls}, index=x.index
    )
    design = np.column_stack(
        [np.ones(len(xr))] + [xr[c].to_numpy() for c in cfg.controls]
    )
    target = y.to_numpy()

    beta = np.linalg.solve(design.T @ design, design.T @ target)
    resid1 = target - design @ beta
    resid1_rank = pd.Series(resid1, index=x.index).rank(pct=True, method="average")
    beta2 = np.linalg.solve(design.T @ design, design.T @ resid1_rank.to_numpy())
    resid2 = resid1_rank.to_numpy() - design @ beta2

    got = result.residual.loc[x.index].to_numpy()
    assert np.allclose(got, resid2, atol=1e-10)
