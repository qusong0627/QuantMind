"""CCASS 百分比口径修复的契约测试。

锁三件事（2026-11-13 审计发现的实际坑）：
1. 2024-11 ~ 2025-12 的「同日混用小数/百分点」必须修成统一小数；
2. 纯百分点日（整段百分比都是 36.33 这种）必须整体 /100；
3. HKEX 缺百分比列（4 列布局）产生的 0 / 数量÷100 必须变 NaN 或参考值，
   **绝不能**被当成真实持仓占比。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.shared.ccass_top50 import (
    canonical_group,
    classify_participant,
    repair_percentages,
)

ISSUED = 3_835_000_000.0


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "stock_code": r.get("stock_code", "0001.HK"),
                "participant_id": r.get("participant_id", f"P{i}"),
                "participant_name": r.get("participant_name", f"P{i}"),
                "holding_quantity": r["holding_quantity"],
                "holding_percentage": r["holding_percentage"],
            }
            for i, r in enumerate(rows)
        ]
    )


def test_mixed_units_within_one_day_are_normalised():
    """2025-12-12 之前：大持仓存小数、小持仓存百分点，同日混用。"""
    df = _frame(
        [
            {"holding_quantity": 1_393_732_677, "holding_percentage": 0.3633},
            {"holding_quantity": 362_140_283, "holding_percentage": 0.0945},
            {"holding_quantity": 36_000_000, "holding_percentage": 0.94},  # 0.94%
            {"holding_quantity": 3_500_000, "holding_percentage": 0.09},   # 0.09%
        ]
    )
    out = repair_percentages(df)
    pct = out["holding_percentage"].to_numpy(dtype="float64")
    assert pct[0] == pytest.approx(0.3633)
    assert pct[1] == pytest.approx(0.0945)
    assert pct[2] == pytest.approx(0.0094, rel=1e-3)
    assert pct[3] == pytest.approx(0.0009, rel=1e-3)
    assert out["holding_percentage"].sum() == pytest.approx(0.4681, rel=1e-3)


def test_pure_percent_point_day_is_divided_by_100():
    """整段百分比都存成百分点（36.33 而非 0.3633）时整体 /100。"""
    df = _frame(
        [
            {"holding_quantity": 1_393_732_677, "holding_percentage": 36.33},
            {"holding_quantity": 36_000_000, "holding_percentage": 0.94},
            {"holding_quantity": 3_500_000, "holding_percentage": 0.09},
        ]
    )
    out = repair_percentages(df)
    pct = out["holding_percentage"].to_numpy(dtype="float64")
    assert pct[0] == pytest.approx(0.3633)
    assert pct[1] == pytest.approx(0.0094, rel=1e-3)
    assert pct[2] == pytest.approx(0.0009, rel=1e-3)


def test_missing_percentage_uses_reference_never_zero():
    """4 列布局缺百分比：有参考股本时按数量折算；没有时必须是 NaN。"""
    df = _frame([{"holding_quantity": 1_000_000_000, "holding_percentage": np.nan}])
    out = repair_percentages(df)
    assert np.isnan(out["holding_percentage"].iloc[0])

    out_ref = repair_percentages(df, reference_issued={"0001.HK": ISSUED})
    assert out_ref["holding_percentage"].iloc[0] == pytest.approx(
        1_000_000_000 / ISSUED
    )


def test_qty_over_100_garbage_is_not_treated_as_percentage():
    """旧解析把「990 股」写成 9.9%：必须识别为异常，不能留在数据里。"""
    df = _frame(
        [
            {"holding_quantity": 1_393_732_677, "holding_percentage": 0.3633},
            {"holding_quantity": 990, "holding_percentage": 9.9},
        ]
    )
    out = repair_percentages(df, reference_issued={"0001.HK": ISSUED})
    pct = out["holding_percentage"].to_numpy(dtype="float64")
    assert pct[1] < 1e-5
    assert pct[1] == pytest.approx(990 / ISSUED)


def test_zero_percentage_with_positive_quantity_becomes_nan():
    df = _frame(
        [
            {"holding_quantity": 1_393_732_677, "holding_percentage": 0.3633},
            {"holding_quantity": 4_352_644_533, "holding_percentage": 0.0},
        ]
    )
    out = repair_percentages(df)
    assert np.isnan(out["holding_percentage"].iloc[1])


def test_reference_smaller_than_max_holding_is_rejected():
    """参考股本 < 最大持仓 ⇒ 参考表已过期/损坏，必须保持 NaN（防 13 倍占比）。"""
    df = _frame([{"holding_quantity": 1_359_796_530, "holding_percentage": np.nan}])
    out = repair_percentages(df, reference_issued={"0001.HK": 90_000_000})
    assert np.isnan(out["holding_percentage"].iloc[0])


def test_inconsistent_stock_day_percentages_are_nulled():
    """修复后合计仍 >105% 的股票日：百分比整体置空（数量仍保留）。"""
    df = _frame(
        [
            {"holding_quantity": 600_000, "holding_percentage": 0.60},
            {"holding_quantity": 600_000, "holding_percentage": 0.60},
            # 同一行里混入百分点（98 而非 0.0098），防止「纯百分点日整体 /100」
            {"holding_quantity": 980_000, "holding_percentage": 98.0},
        ]
    )
    out = repair_percentages(df)
    assert out["holding_percentage"].isna().all()
    assert out["holding_quantity"].tolist() == [600_000, 600_000, 980_000]


def test_repair_is_idempotent():
    df = _frame(
        [
            {"holding_quantity": 1_393_732_677, "holding_percentage": 0.3633},
            {"holding_quantity": 36_000_000, "holding_percentage": 0.94},
        ]
    )
    once = repair_percentages(df)
    twice = repair_percentages(once)
    pd.testing.assert_series_equal(
        once["holding_percentage"], twice["holding_percentage"]
    )


def test_participant_grouping_merges_multi_entity_names():
    assert classify_participant("C00019", "香港上海匯豐銀行有限公司") == "cust"
    assert classify_participant("B01955", "富途證券國際(香港)有限公司") == "brok"
    assert classify_participant("A00003", "中國証券登記結算有限責任公司") == "csc"
    assert canonical_group("C00019", "香港上海匯豐銀行有限公司")[0] == canonical_group(
        "B01089", "匯豐金融證券(亞洲)有限公司"
    )[0]
    assert canonical_group("A00003", "中國証券登記結算有限責任公司")[0] == "SOUTH"
