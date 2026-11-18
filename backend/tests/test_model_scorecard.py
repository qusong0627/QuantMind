"""模型推理成绩单的纯函数测试（口径护栏）。

回归点（2026-11-18 用户要求）：
* 胜率按 **Top-K 推理票** 算，跌的照算（不是只数涨的）；
* 收益与基准同窗口（T+1 开盘 → T+1+N 收盘）；
* 综合分与权重公式固定，不允许事后调参；
* 滚动窗口按**已成熟批次**聚合（22 个交易日 ≈ 1 个月）。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backend.services.engine.inference.model_scorecard import (
    cohort_metrics,
    composite_score,
    normalize_weights,
    rank_ic,
    rolling_stats,
)


def test_rank_ic_perfect_monotonic() -> None:
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    r = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10])
    assert rank_ic(s, r) == pytest.approx(1.0)


def test_cohort_metrics_counts_losers_in_win_rate() -> None:
    scores = pd.DataFrame(
        {"symbol": ["A", "B", "C", "D", "E", "F"], "score": [6, 5, 4, 3, 2, 1]}
    )
    returns = pd.Series(
        {"A": 0.10, "B": -0.05, "C": 0.03, "D": -0.02, "E": 0.01, "F": -0.04}
    )

    m = cohort_metrics(scores, returns, benchmark_return=0.01, top_k=3)

    assert m["n_signals"] == 6
    assert m["n_matured"] == 6
    # top3 = A(+10%) B(-5%) C(+3%) → 2/3 上涨；跌幅计入均值
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["avg_return"] == pytest.approx((0.10 - 0.05 + 0.03) / 3)
    assert m["median_return"] == pytest.approx(0.03)
    assert m["excess_return"] == pytest.approx((0.10 - 0.05 + 0.03) / 3 - 0.01)
    # 截面 IC 要求 ≥10 个成熟样本；6 只时如实返回 NaN，不伪造
    assert math.isnan(m["rank_ic"])
    # 十分位多空需要 ≥10 个样本（每侧至少 1 只）；6 只时如实留空
    assert m["long_short"] is None


def test_cohort_metrics_skips_unmatured_symbols() -> None:
    scores = pd.DataFrame({"symbol": ["A", "B"], "score": [2, 1]})
    returns = pd.Series({"A": 0.05})  # B 停牌/无价 → 不成熟

    m = cohort_metrics(scores, returns, benchmark_return=0.0, top_k=2)

    assert m["n_matured"] == 1
    assert m["win_rate"] == pytest.approx(1.0)


def test_composite_score_bounds() -> None:
    assert composite_score(0.65, 0.05, 1.0, 0.9) == pytest.approx(100.0)
    assert composite_score(0.35, -0.10, -0.5, 0.2) == pytest.approx(0.0)


def test_normalize_weights_positive_part_and_equal_fallback() -> None:
    w = normalize_weights(
        [
            {"model_id": "a", "score": 90},
            {"model_id": "b", "score": 70},
            {"model_id": "c", "score": 40},
        ]
    )
    assert w["c"] == 0.0
    assert w["a"] == pytest.approx(40 / 60)
    assert w["b"] == pytest.approx(20 / 60)
    assert sum(w.values()) == pytest.approx(1.0)

    # 全体低于 50 分（熊市月）→ softmax 保留相对区分度，不退化成等权
    soft = normalize_weights(
        [{"model_id": "a", "score": 30}, {"model_id": "b", "score": 20}]
    )
    assert sum(soft.values()) == pytest.approx(1.0)
    assert soft["a"] > soft["b"] > 0


def test_rolling_stats_aggregates_matured_cohorts() -> None:
    cohorts = pd.DataFrame(
        {
            "win_rate": [0.6, 0.4, 0.5],
            "avg_return": [0.02, -0.01, 0.03],
            "median_return": [0.01, -0.01, 0.02],
            "excess_return": [0.01, -0.02, 0.02],
            "rank_ic": [0.1, -0.05, 0.15],
        }
    )

    s = rolling_stats(cohorts)

    assert s["cohorts"] == 3
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["avg_return"] == pytest.approx((0.02 - 0.01 + 0.03) / 3)
    assert s["cohort_hit_rate"] == pytest.approx(2 / 3)
    assert s["rank_ic"] == pytest.approx((0.1 - 0.05 + 0.15) / 3)
    assert s["t_stat"] is not None and not math.isnan(s["t_stat"])
    assert 0 <= s["score"] <= 100


def test_rolling_stats_empty_is_honest() -> None:
    s = rolling_stats(pd.DataFrame())
    assert s["cohorts"] == 0
    assert s["score"] is None
    assert s["win_rate"] is None
