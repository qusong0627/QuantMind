"""Kronos A股零样本测试：截面指标测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.kronos_ashare.metrics import (
    baseline_score,
    cross_section_metrics,
    summarize,
)


def _frame(n: int = 30) -> tuple[pd.Series, pd.Series]:
    symbols = [f"sh{600000 + i}" for i in range(n)]
    scores = pd.Series(np.arange(n, dtype=float), index=symbols)
    fwd = pd.Series(np.arange(n, dtype=float) / 1000.0, index=symbols)
    return scores, fwd


def test_cross_section_metrics_perfect_rank():
    scores, fwd = _frame()

    m = cross_section_metrics(scores, fwd, topk=5)

    assert m["rank_ic"] == pytest.approx(1.0)
    assert m["ic"] == pytest.approx(1.0)
    assert m["topk_excess"] > 0
    assert m["topk_hit"] == 1.0


def test_cross_section_metrics_reverse_rank():
    scores, fwd = _frame()

    m = cross_section_metrics(scores * -1.0, fwd, topk=5)

    assert m["rank_ic"] == pytest.approx(-1.0)
    assert m["topk_excess"] < 0


def test_cross_section_metrics_respects_tradable_mask():
    scores, fwd = _frame()
    tradable = pd.Series(True, index=scores.index)
    tradable.iloc[-1] = False  # 去掉最高分，TopK 超额应下降

    base = cross_section_metrics(scores, fwd, topk=5)
    masked = cross_section_metrics(scores, fwd, topk=5, tradable=tradable)

    assert masked["topk_excess"] < base["topk_excess"]
    assert masked["n"] == base["n"] - 1


def test_cross_section_metrics_returns_nan_when_too_few_rows():
    scores = pd.Series([1.0, 2.0], index=["a", "b"])
    fwd = pd.Series([0.1, 0.2], index=["a", "b"])

    m = cross_section_metrics(scores, fwd, topk=1)

    assert np.isnan(m["rank_ic"])
    assert m["n"] == 0


def test_summarize_computes_mean_and_icir():
    rows = [{"ic": 0.5, "rank_ic": 0.5, "topk_excess": 0.01, "topk_hit": 1.0}] * 4

    s = summarize(rows)

    assert s["ic_mean"] == pytest.approx(0.5)
    assert s["ic_std"] == pytest.approx(0.0)
    assert np.isnan(s["ic_icir"])  # std=0 时不伪造 ICIR


def test_baseline_score_momentum_and_reversal():
    calendar = pd.bdate_range("2025-01-01", periods=30)
    close = pd.Series(100.0 + np.arange(30, dtype=float), index=calendar)
    panel = pd.DataFrame({"close": close})

    mom = baseline_score(panel, calendar[25], lookback=20, kind="momentum")
    rev = baseline_score(panel, calendar[25], lookback=20, kind="reversal")

    assert mom > 0
    assert rev == pytest.approx(-mom)
