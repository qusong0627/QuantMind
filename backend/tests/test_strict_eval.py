"""严格评估器纯函数测试（WP2）。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from backend.services.engine.training.strict_eval import (
    block_bootstrap_ci,
    build_schedule,
    compound,
    hac_t,
    max_drawdown,
    pick_top_n,
    summarize_batches,
    to_suffix,
)


def test_to_suffix_keeps_suffix_and_converts_prefix():
    assert to_suffix(["SH600000", "000001.SZ", "BJ430047", "TSLA"]) == [
        "600000.SH",
        "000001.SZ",
        "430047.BJ",
        "TSLA",
    ]


def test_build_schedule_is_non_overlapping_and_respects_end():
    cal = [f"2026{m:02d}{d:02d}" for m in range(1, 3) for d in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
    assert build_schedule(cal, "20260101", "20260120", 10) == ["20260101"]
    assert build_schedule(cal, "20260101", "20260210", 10) == ["20260101", "20260201"]


def test_pick_top_n_skips_excluded():
    import pandas as pd

    scores = pd.Series({"a": 0.3, "b": 0.2, "c": 0.1})
    assert pick_top_n(scores, 2, {"b"}) == ["a", "c"]


def test_compound_and_mdd():
    assert compound([0.1, -0.1]) == pytest.approx(-0.01)
    assert max_drawdown([0.1, -0.2]) == pytest.approx(-0.2)


def test_hac_t_signs():
    rng = np.random.default_rng(0)
    positive = 0.01 + rng.normal(0, 0.001, 100)
    negative = -0.01 + rng.normal(0, 0.001, 100)
    assert hac_t(positive, lag=1) > 5
    assert hac_t(negative, lag=1) < -5
    assert math.isnan(hac_t([0.01], lag=1))


def test_block_bootstrap_ci_ordered():
    rng = np.random.default_rng(0)
    r = 0.01 + rng.normal(0, 0.01, 60)
    lo, hi = block_bootstrap_ci(r, block=3, draws=200)
    assert lo < hi
    assert lo < float(np.mean(r)) < hi


def test_summarize_batches_costs_monotonic():
    rows = [
        {
            "signal": "20260101",
            "gross": 0.02,
            "bench": 0.01,
            "bench_zz": 0.015,
            "overlap": 0.5,
            "month": "202601",
            "participation": [0.01, 0.02],
        },
        {
            "signal": "20260111",
            "gross": -0.01,
            "bench": -0.005,
            "bench_zz": 0.0,
            "overlap": 0.5,
            "month": "202601",
            "participation": [0.01],
        },
        {
            "signal": "20260121",
            "gross": 0.005,
            "bench": 0.0,
            "bench_zz": 0.001,
            "overlap": 0.5,
            "month": "202601",
            "participation": [0.005],
        },
    ]
    out = summarize_batches(rows, horizon=10, costs_bps=(15, 25))
    assert out["batches"] == 3
    assert out["cost_15bp"]["total"] > out["cost_25bp"]["total"]
    assert out["cost_15bp"]["mdd"] <= 0
    assert out["participation"]["median"] == pytest.approx(0.01)
    assert out["monthly_gross"][0]["month"] == "202601"
    assert math.isfinite(out["cost_15bp"]["psr"])
