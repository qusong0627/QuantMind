"""全市场多分类面板的纯函数单测。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.stock_classifier.panel import (
    assign_quantile_classes,
    class_mean_returns,
    daily_rank_ic,
    dedup_by_corr,
    is_limit_up,
    limit_up_threshold,
    select_by_ic,
)

pytestmark = pytest.mark.unit


def test_assign_quantile_classes_balanced_and_nan() -> None:
    y = np.arange(100, dtype=float)
    y[3] = np.nan
    classes = assign_quantile_classes(y, n_classes=5)
    assert classes[3] == -1
    valid = classes[classes >= 0]
    counts = np.bincount(valid, minlength=5)
    assert counts.sum() == 99
    assert counts.max() - counts.min() <= 1
    # 收益越大类别越高
    assert classes[0] == 0 and classes[-1] == 4


def test_assign_quantile_classes_handles_ties() -> None:
    y = np.array([1.0] * 50 + [2.0] * 50)
    classes = assign_quantile_classes(y, n_classes=2)
    assert (classes[:50] == 0).all()
    assert (classes[50:] == 1).all()


def test_daily_rank_ic_perfect_and_missing() -> None:
    y = np.arange(50, dtype=float)
    block = np.column_stack([y, -y, np.full(50, np.nan)])
    ic = daily_rank_ic(block, y)
    assert ic[0] == pytest.approx(1.0)
    assert ic[1] == pytest.approx(-1.0)
    assert np.isnan(ic[2])


def test_daily_rank_ic_requires_enough_samples() -> None:
    ic = daily_rank_ic(np.arange(10, dtype=float).reshape(-1, 1), np.arange(10.0))
    assert np.isnan(ic).all()


def test_select_by_ic_keeps_both_signs_and_filters() -> None:
    # 3 个因子：正 IC、负 IC、零 IC
    ic_matrix = np.column_stack(
        [
            np.full(100, 0.05),
            np.full(100, -0.04),
            np.zeros(100),
        ]
    )
    coverage = np.array([1.0, 1.0, 1.0])
    keep = select_by_ic(ic_matrix, coverage, min_abs_ic=0.01, min_abs_icir=0.1)
    assert 0 in keep and 1 in keep and 2 not in keep


def test_select_by_ic_coverage_gate() -> None:
    ic_matrix = np.column_stack([np.full(100, 0.05)])
    keep = select_by_ic(ic_matrix, np.array([0.5]), min_coverage=0.8)
    assert keep == []


def test_dedup_by_corr_greedy() -> None:
    corr = np.array(
        [
            [1.0, 0.95, 0.1],
            [0.95, 1.0, 0.2],
            [0.1, 0.2, 1.0],
        ]
    )
    selected = dedup_by_corr([0, 1, 2], corr, max_features=5, corr_threshold=0.9)
    assert selected == [0, 2]


def test_class_mean_returns() -> None:
    y = np.array([-0.03, -0.01, 0.0, 0.01, 0.03])
    classes = np.array([0, 0, 1, 2, 2])
    mu = class_mean_returns(y, classes, 3)
    assert mu[0] == pytest.approx(-0.02)
    assert mu[1] == pytest.approx(0.0)
    assert mu[2] == pytest.approx(0.02)


def test_limit_up_threshold_by_board() -> None:
    assert limit_up_threshold("600000.SH") == pytest.approx(0.10)
    assert limit_up_threshold("300750.SZ") == pytest.approx(0.20)
    assert limit_up_threshold("688981.SH") == pytest.approx(0.20)
    assert limit_up_threshold("830799.BJ") == pytest.approx(0.30)


def test_is_limit_up() -> None:
    symbols = pd.Series(["600000.SH", "300750.SZ", "000001.SZ"])
    returns = pd.Series([0.099, 0.15, 0.05])
    mask = is_limit_up(symbols, returns)
    assert mask.tolist() == [True, False, False]
