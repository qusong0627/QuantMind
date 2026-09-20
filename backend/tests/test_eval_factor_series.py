"""因子长序列载荷（`scripts/eval/factor_series.py`，设计 §1.6 / 阶段 2 详情图）。

与模型不同，因子面板自带多周期列（`ic_1/2/5/10/20`）→ **IC 衰减真能算**，
不能学着模型报「不可算」。盯：
- 图上数与卡上分同源（只摊平不重算）；
- 全 NaN 的列给 None，不给 0（0 会被读成「真的一点效果都没有」）；
- 分年 IC 按真实日期聚合（面板日期是 int32 YYYYMMDD，不是 ISO 字符串）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.eval.factor_series import factor_series_payload


def _panel() -> pd.DataFrame:
    """3 天 × 10 分位列的迷你面板（ic_1..ic_20 各给一个常数便于断言）。"""
    return pd.DataFrame(
        {
            "date": [20250102, 20250103, 20260105],
            "ic": [0.03, -0.01, 0.05],
            "turnover": [0.2, 0.2, 0.2],
            "coverage": [0.8, 0.8, 0.8],
            **{f"q{i}": [0.004 - i * 0.001] * 3 for i in range(1, 11)},
            "ic_1": [0.10] * 3,
            "ic_2": [0.08] * 3,
            "ic_5": [0.06] * 3,
            "ic_10": [0.04] * 3,
            "ic_20": [0.02] * 3,
        }
    )


# ── 摊平 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_daily_ic_dates_are_iso_and_ascending():
    """面板日期是 int32 YYYYMMDD → 必须转 ISO（前端按字符串比日期）。"""
    payload = factor_series_payload(_panel())

    assert [p["date"] for p in payload["series"]["daily_ic"]] == [
        "2025-01-02",
        "2025-01-03",
        "2026-01-05",
    ]
    assert payload["series"]["daily_ic"][0]["value"] == pytest.approx(0.03)


@pytest.mark.unit
def test_ic_decay_is_computed_for_factors_unlike_models():
    """因子面板自带多周期列 → 衰减真能算；报了「不可算」就是漏掉一整个能力。"""
    payload = factor_series_payload(_panel())

    assert payload["series"]["ic_decay"] == [
        {"horizon": 1, "value": pytest.approx(0.10)},
        {"horizon": 2, "value": pytest.approx(0.08)},
        {"horizon": 5, "value": pytest.approx(0.06)},
        {"horizon": 10, "value": pytest.approx(0.04)},
        {"horizon": 20, "value": pytest.approx(0.02)},
    ]
    assert "ic_decay" not in payload["notes"]


@pytest.mark.unit
def test_decile_buckets_are_one_based_and_ordered():
    payload = factor_series_payload(_panel())

    deciles = payload["series"]["decile_mean"]
    assert [d["bucket"] for d in deciles] == list(range(1, 11))
    assert deciles[0]["value"] == pytest.approx(0.003)


@pytest.mark.unit
def test_segment_ic_groups_by_calendar_year():
    payload = factor_series_payload(_panel())

    segments = payload["series"]["segment_ic"]
    assert [s["label"] for s in segments] == ["2025", "2026"]
    assert segments[0]["value"] == pytest.approx(0.01)  # (0.03 + -0.01) / 2
    assert segments[0]["n_days"] == 2
    assert segments[1]["n_days"] == 1


@pytest.mark.unit
def test_scalars_carry_ic_ir_and_counts():
    payload = factor_series_payload(_panel())

    assert payload["scalars"]["ic_mean"] == pytest.approx((0.03 - 0.01 + 0.05) / 3)
    assert payload["scalars"]["n_days"] == 3
    assert payload["scalars"]["ic_ir"] is not None


# ── 退化输入 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_all_nan_column_yields_none_not_zero():
    """全 NaN → None（0 会被读成「真的一点效果都没有」，是伪造证据）。"""
    panel = _panel()
    panel["turnover"] = np.nan

    payload = factor_series_payload(panel)

    assert payload["scalars"]["turnover_mean"] is None


@pytest.mark.unit
def test_missing_horizon_columns_are_reported_not_silently_empty():
    panel = _panel().drop(columns=["ic_1", "ic_2", "ic_5", "ic_10", "ic_20"])

    payload = factor_series_payload(panel)

    assert payload["series"]["ic_decay"] == []
    assert "ic_1/2/5/10/20" in payload["notes"]["ic_decay"]


@pytest.mark.unit
def test_nan_ic_days_are_dropped_from_the_curve():
    panel = _panel()
    panel.loc[1, "ic"] = np.nan

    payload = factor_series_payload(panel)

    assert [p["date"] for p in payload["series"]["daily_ic"]] == [
        "2025-01-02",
        "2026-01-05",
    ]
