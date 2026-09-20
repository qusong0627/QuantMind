"""模型长序列载荷（`scripts/eval/model_series.py`，设计 §1.6 / 阶段 2 详情图）。

详情图（逐日 IC 曲线、分档柱、分段 IC、换手）必须与**卡上分数**同源——
所以这份载荷只做「把 `_pred_stats` 已经算好的统计摊平成长序列」，
不重算、不近似。

盯三件事：
- 缺的序列要有 **note**（模型 pred 只有单一持有期标签 → IC 衰减本就不可算，
  如实标注而不是画一条平的假衰减）；
- 长序列要**截断留尾**并写明截了多少（千点曲线画不下，但截了不许不说）；
- 退化输入给空序列 + 原因，不给 None（前端拿 None 会崩，拿空数组只会没图）。
"""

from __future__ import annotations

import pytest

from backend.scripts.eval.model_series import (
    MAX_SERIES_POINTS,
    series_from_stats,
    truncate_series,
)

STATS = {
    "daily_ic": {"2026-09-01": 0.031, "2026-09-02": -0.012, "2026-09-03": 0.045},
    "strat": {
        "sufficient": True,
        "mean_returns": [0.01, 0.004, 0.002, -0.001, -0.006],
        "monotonicity": 0.9,
        "ls_mean": 0.008,
        "ls_ir": 0.42,
        "n_days": 3,
        "n_groups": 5,
        "median_stocks_per_day": 300,
    },
    "robust": {
        "sufficient": True,
        "by_split": {
            "2025": {"ic_mean": 0.02, "n_days": 240, "ic_ir": 0.4},
            "2026": {"ic_mean": -0.03, "n_days": 120, "ic_ir": -0.5},
        },
        "min_segment": "2026",
    },
    "health": {
        "sufficient": True,
        "ic_mean_20": 0.02,
        "ic_mean_60": 0.01,
        "icir": 0.3,
        "drift": 0.01,
        "n_days": 3,
    },
    "cost": {
        "sufficient": True,
        "turnover_mean": 0.25,
        "turnover_series": [0.2, 0.3],
        "cost_drag_annual": 0.126,
        "round_trip_cost": 0.002,
        "top_k": 50,
    },
}


# ── 摊平 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_daily_ic_series_is_date_ascending_points():
    payload = series_from_stats(STATS)

    assert payload["series"]["daily_ic"] == [
        {"date": "2026-09-01", "value": pytest.approx(0.031)},
        {"date": "2026-09-02", "value": pytest.approx(-0.012)},
        {"date": "2026-09-03", "value": pytest.approx(0.045)},
    ]


@pytest.mark.unit
def test_decile_series_keeps_bucket_order_and_labels_the_low_bucket():
    """分档序＝预测值从小到大；档位下标要带出来（前端靠它定红绿与轴序）。"""
    payload = series_from_stats(STATS)

    deciles = payload["series"]["decile_mean"]
    assert [d["bucket"] for d in deciles] == [1, 2, 3, 4, 5]
    assert deciles[0]["value"] == pytest.approx(0.01)
    assert payload["notes"]["bucket_order"].startswith("bucket 1 = 预测值最低档")


@pytest.mark.unit
def test_segment_series_is_sorted_by_label():
    payload = series_from_stats(STATS)

    assert [s["label"] for s in payload["series"]["segment_ic"]] == ["2025", "2026"]
    assert payload["series"]["segment_ic"][1]["value"] == pytest.approx(-0.03)
    assert payload["series"]["segment_ic"][1]["n_days"] == 120


@pytest.mark.unit
def test_scalars_are_the_same_numbers_the_card_scored_on():
    """图上标题的标量必须与评分用的那组数一致（同源，不重算）。"""
    payload = series_from_stats(STATS)

    assert payload["scalars"] == {
        "ic_mean_20": pytest.approx(0.02),
        "ic_mean_60": pytest.approx(0.01),
        "icir": pytest.approx(0.3),
        "ic_mean": None,
        "ls_mean": pytest.approx(0.008),
        "ls_ir": pytest.approx(0.42),
        "monotonicity": pytest.approx(0.9),
        "turnover_mean": pytest.approx(0.25),
        "cost_drag_annual": pytest.approx(0.126),
        "round_trip_cost": pytest.approx(0.002),
        "n_days_ic": 3,
        "n_days_strat": 3,
    }


@pytest.mark.unit
def test_ic_decay_is_declared_unavailable_with_the_real_reason():
    """pred.parquet 只有单一持有期标签 → IC 衰减不可算；如实标注，不画假曲线。"""
    payload = series_from_stats(STATS)

    assert "ic_decay" not in payload["series"]
    assert "持有期" in payload["notes"]["ic_decay"]
    assert "不可算" in payload["notes"]["ic_decay"]


# ── 退化输入 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_insufficient_stats_yield_empty_series_with_reason():
    """统计缺省（IC 天数不足等）→ 空数组 + 原因，不留 None 让前端崩。"""
    payload = series_from_stats(
        {
            "daily_ic": {},
            "strat": {"sufficient": False, "reason": "有效日不足"},
            "robust": {},
            "health": {"sufficient": False, "reason": "IC 序列天数不足（3 < 20）"},
            "cost": {},
        },
    )

    assert payload["series"]["daily_ic"] == []
    assert payload["series"]["decile_mean"] == []
    assert "IC 序列天数不足" in payload["notes"]["daily_ic"]
    assert "有效日不足" in payload["notes"]["decile_mean"]


@pytest.mark.unit
def test_none_values_inside_series_are_dropped_not_kept_as_null():
    """序列里的 None 点要剔除（JSON null 会让折线断在半空）。"""
    stats = {**STATS, "daily_ic": {"2026-09-01": 0.03, "2026-09-02": None}}

    payload = series_from_stats(stats)

    assert payload["series"]["daily_ic"] == [
        {"date": "2026-09-01", "value": pytest.approx(0.03)}
    ]


# ── 截断 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_truncate_series_keeps_the_tail_and_reports_the_loss():
    """超长序列留**最近**的点（近端才是决策依据），并写明截了多少。"""
    points = list(range(MAX_SERIES_POINTS + 37))

    kept, note = truncate_series(points)

    assert len(kept) == MAX_SERIES_POINTS
    assert kept[-1] == len(points) - 1
    assert "37" in note


@pytest.mark.unit
def test_truncate_series_leaves_short_series_untouched():
    points = [1, 2, 3]

    kept, note = truncate_series(points)

    assert kept == points
    assert note == ""
