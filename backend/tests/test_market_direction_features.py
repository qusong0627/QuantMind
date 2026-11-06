"""大盘方向模型纯函数单测（截面聚合 / 滚动分位 / 标签 / 切分 / 指标）。

运行：``python3 -m pytest backend/tests/test_market_direction_features.py -q``
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.market_direction.features import (
    AggSpec,
    aggregate_partition,
    assign_split,
    auc_score,
    block_bootstrap_auc,
    compute_labels,
    long_flat_curve,
    purge_overlapping_labels,
    rolling_pct_rank,
    wilson_interval,
    winsorize,
)

pytestmark = pytest.mark.unit


def _frame(values: list[float], ranks: list[float], caps: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fun_mv_rank": ranks,
            "fun_total_mv": caps,
            "f": values,
        }
    )


def test_winsorize_clips_extremes_and_keeps_nan() -> None:
    x = np.array([-100.0, 0.0, 1.0, 2.0, 3.0, 100.0, np.nan])
    out = winsorize(x)
    assert np.isnan(out[-1])
    assert out[1] == 0.0 and out[4] == 3.0
    assert out[0] > -100.0 and out[-2] < 100.0
    assert np.nanmin(out) <= 0.0


def test_aggregate_partition_mean_std_and_weights() -> None:
    n = 200
    values = np.linspace(0.0, 1.0, n)
    ranks = np.linspace(0.0, 1.0, n)
    caps = np.log(np.linspace(1e9, 1e12, n))
    frame = _frame(values.tolist(), ranks.tolist(), caps.tolist())
    out = aggregate_partition(
        frame,
        [
            AggSpec("f", "mean"),
            AggSpec("f", "capw"),
            AggSpec("f", "std"),
            AggSpec("f", "ls"),
        ],
    )
    assert out["fa_n_stocks"] == float(n)
    assert out["fa_f_mean"] == pytest.approx(0.5, abs=0.02)
    assert out["fa_f_std"] == pytest.approx(values.std(), abs=0.02)
    # 市值加权应偏向高排名（值大）的一端
    assert out["fa_f_capw"] > out["fa_f_mean"]
    # 大小盘差：大市值端值更大 → 正值
    assert out["fa_f_ls"] > 0.2


def test_aggregate_partition_insufficient_universe_returns_nan() -> None:
    frame = _frame([1.0, 2.0], [0.9, 0.1], [21.0, 21.0])
    out = aggregate_partition(frame, [AggSpec("f", "mean")])
    assert np.isnan(out["fa_f_mean"])


def test_rolling_pct_rank_uses_trailing_window_only() -> None:
    x = np.arange(300, dtype=float)
    pct = rolling_pct_rank(x, window=50, min_periods=20)
    assert np.isnan(pct[10])
    # 单调上升序列的最新值位于窗口最高分位（含自身，mid-rank → (w-1+0.5)/w）
    assert pct[299] == pytest.approx(0.99)
    # 未来值变化不影响历史分位（PIT）
    y = x.copy()
    y[250:] = 0.0
    pct2 = rolling_pct_rank(y, window=50, min_periods=20)
    np.testing.assert_allclose(pct[:200], pct2[:200], equal_nan=True)


def test_compute_labels_forward_returns_and_no_lookahead() -> None:
    dates = pd.Series(pd.date_range("2024-01-01", periods=8, freq="B"))
    close = pd.Series([100.0, 101.0, 99.0, 102.0, 103.0, 101.0, 105.0, 108.0])
    open_ = pd.Series([99.0, 100.0, 101.0, 98.0, 102.0, 104.0, 100.0, 104.0])
    out = compute_labels(dates, close, open_, horizon=2)
    assert out["fwd_ret_2"].iloc[0] == pytest.approx(99.0 / 100.0 - 1.0)
    assert out["y_2"].iloc[0] == 0.0
    assert out["fwd_ret_2"].iloc[1] == pytest.approx(102.0 / 101.0 - 1.0)
    assert out["y_2"].iloc[1] == 1.0
    assert np.isnan(out["fwd_ret_2"].iloc[-2])
    assert np.isnan(out["y_2"].iloc[-1])
    # 次日开盘口径：close[t+2]/open[t+1]-1
    assert out["fwd_ret_open_2"].iloc[0] == pytest.approx(99.0 / 100.0 - 1.0)
    assert out["fwd_ret_open_2"].iloc[2] == pytest.approx(103.0 / 98.0 - 1.0)


def test_assign_split_and_purge_tail() -> None:
    dates = pd.Series(
        pd.to_datetime(
            ["2024-05-29", "2024-05-30", "2024-05-31", "2024-06-03", "2024-06-04"]
        )
    )
    labels = assign_split(
        dates,
        {"train": ("2024-01-01", "2024-05-31"), "valid": ("2024-06-01", "2024-06-30")},
    )
    assert labels.tolist() == ["train", "train", "train", "valid", "valid"]

    split = np.array(["train"] * 10 + ["valid"] * 10, dtype=object)
    keep = purge_overlapping_labels(split, horizon=5, embargo_extra=2)
    assert keep[:3].all()  # 10 - (5+2) = 3 保留
    assert not keep[3:10].any()
    assert keep[10:].all()  # 末段不剔除


def test_auc_score_matches_rank_formula_and_handles_ties() -> None:
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.4, 0.35, 0.8])
    assert auc_score(y, p) == pytest.approx(0.75)
    # 全部并列 → 0.5
    assert auc_score(y, np.full(4, 0.5)) == pytest.approx(0.5)
    assert np.isnan(auc_score(np.zeros(4), p))


def test_block_bootstrap_interval_brackets_estimate() -> None:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 400).astype(float)
    p = np.clip(0.5 + 0.2 * (y - 0.5) + rng.normal(0, 0.1, 400), 0.01, 0.99)
    point = auc_score(y, p)
    lo, hi = block_bootstrap_auc(y, p, block=5, n_boot=200)
    assert lo < point < hi


def test_wilson_interval_bounds() -> None:
    lo, hi = wilson_interval(60, 100)
    assert 0.5 < lo < 0.6 < hi < 0.7
    lo, hi = wilson_interval(0, 0)
    assert np.isnan(lo) and np.isnan(hi)


def test_long_flat_curve_respects_signal_and_costs() -> None:
    returns = np.array([0.01, 0.01, -0.01, 0.02])
    always = long_flat_curve(returns, np.ones(4), cost_per_switch=0.0)
    never = long_flat_curve(returns, np.zeros(4), cost_per_switch=0.0)
    assert always["total_return"] > 0
    assert never["total_return"] == pytest.approx(0.0)
    assert never["exposure"] == 0.0
    # 有切换成本时收益下降
    costly = long_flat_curve(returns, np.ones(4), cost_per_switch=0.001)
    assert costly["total_return"] < always["total_return"]
