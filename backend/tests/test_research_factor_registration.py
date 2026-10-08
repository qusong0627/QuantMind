"""因子研究 → 训练目录注册：解析层契约（纯函数，无 IO）。

链路背景（2026-10-07）：因子研究页多选因子后要能一键注册进训练目录
（``qm_training_factor_mapping`` 的草稿）。此前这条线**完全不存在**——
页面里的「已写入训练目录」是一句假文案（``FactorPortfolioModal`` 后端调
``build_portfolio(persist=False)``，从不碰映射表）。

本文件锁的是解析层（code → 映射行）的不变量，DB 写入由端点层测：

1. ``code`` 必须能在该数据集的目录里定位到来源库 ``l2``；定位不到就**明确
   跳过并给原因**，绝不猜一个库塞进去（猜错会把 A 库的列名挂到 B 库下，
   训练时读出来全是 NaN，而没有任何一层会报错）；
2. 标签/泄漏库（``EXCLUDED_FROM_TRAINING``）必须被挡在门外——这是安全边界，
   注册等同于把该列提升为训练特征，放进来就是标签穿越；
3. 重复 code 去重，且**保持传入顺序**（前端要按用户勾选顺序回报结果）；
4. 注册进来的因子 ``enabled=True`` 但 ``default_selected=False``：用户选的是
   「允许它参与训练」，不是「往后每次训练都默认勾上」。
"""

from __future__ import annotations

import pytest

from backend.services.api.routers.admin.research_factor_registration import (
    resolve_registrable,
)
from backend.services.engine.data_platform.quantdb_factor_reader import (
    EXCLUDED_FROM_TRAINING,
)


def _factor(code: str, lib: str, **extra) -> dict:
    return {"code": code, "l2": lib, "display_name": code, **extra}


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_resolves_source_dataset_and_column_from_factor_entry():
    """l2 → source_dataset，code → source_column/feature_key。"""
    factors = [_factor("mom_ret_5d", "l1_factors")]

    candidates, skipped = resolve_registrable(["mom_ret_5d"], factors)

    assert skipped == []
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source_dataset == "l1_factors"
    assert c.source_column == "mom_ret_5d"
    assert c.feature_key == "mom_ret_5d"
    assert c.enabled is True
    assert c.default_selected is False


def test_preserves_requested_order_across_libraries():
    """跨库多选：结果顺序跟着用户勾选顺序，不按库重排。"""
    factors = [
        _factor("b_col", "l2_factors"),
        _factor("a_col", "l1_factors"),
        _factor("c_col", "l2_factors"),
    ]

    candidates, _ = resolve_registrable(["b_col", "a_col", "c_col"], factors)

    assert [c.source_column for c in candidates] == ["b_col", "a_col", "c_col"]
    assert [c.source_dataset for c in candidates] == [
        "l2_factors",
        "l1_factors",
        "l2_factors",
    ]


def test_duplicate_codes_registered_once():
    """同一个 code 传两次只产生一条映射（否则会撞唯一索引）。"""
    factors = [_factor("dup", "l1_factors")]

    candidates, _ = resolve_registrable(["dup", "dup", "dup"], factors)

    assert len(candidates) == 1


def test_whitespace_and_blank_codes_ignored():
    """前后空白容忍；空串直接丢弃，不产生「找不到」噪音。"""
    factors = [_factor("x", "l1_factors")]

    candidates, skipped = resolve_registrable(["  x  ", "", "   "], factors)

    assert [c.source_column for c in candidates] == ["x"]
    assert skipped == []


# ---------------------------------------------------------------------------
# 拒绝路径
# ---------------------------------------------------------------------------
def test_unknown_code_is_skipped_with_reason():
    """不在该数据集里的 code → 跳过并说明，不是静默丢弃。"""
    candidates, skipped = resolve_registrable(["nope"], [_factor("yes", "l1_factors")])

    assert candidates == []
    assert len(skipped) == 1
    assert skipped[0].code == "nope"
    assert skipped[0].reason


def test_entry_without_source_library_is_skipped():
    """目录项缺 l2 → 跳过。绝不回退到默认源（会把列挂错库）。"""
    factors = [{"code": "no_lib"}]

    candidates, skipped = resolve_registrable(["no_lib"], factors)

    assert candidates == []
    assert skipped and skipped[0].code == "no_lib"


@pytest.mark.parametrize("leaky", sorted(EXCLUDED_FROM_TRAINING))
def test_leaky_library_is_refused(leaky):
    """标签/泄漏库必须被拒——注册即提升为训练特征，放进来就是标签穿越。"""
    factors = [_factor(f"col_from_{leaky}", leaky)]

    candidates, skipped = resolve_registrable([f"col_from_{leaky}"], factors)

    assert candidates == [], f"{leaky} 是泄漏库，不得注册为训练特征"
    assert skipped and leaky in skipped[0].reason


def test_illegal_library_name_is_refused():
    """库名不是合法标识符（注入风险）→ 拒。"""
    factors = [_factor("c", "l1_factors; DROP TABLE x")]

    candidates, skipped = resolve_registrable(["c"], factors)

    assert candidates == []
    assert skipped


def test_key_and_ohlcv_columns_are_refused():
    """命中主键/OHLCV 的列不得作为 feature_key——回写会覆盖行情列。"""
    factors = [
        _factor("symbol", "l1_factors"),
        _factor("close", "l1_factors"),
        _factor("trade_date", "l1_factors"),
    ]

    candidates, skipped = resolve_registrable(
        ["symbol", "close", "trade_date"], factors
    )

    assert [c.source_column for c in candidates] == []
    assert {s.code for s in skipped} == {"symbol", "close", "trade_date"}


# ---------------------------------------------------------------------------
# 混合：一次请求里成功与失败并存
# ---------------------------------------------------------------------------
def test_mixed_batch_reports_both_sides():
    """部分成功不是全盘失败：能注册的照常注册，不能的逐条给原因。"""
    factors = [
        _factor("good", "l1_factors"),
        _factor("leak", "features_daily"),
    ]

    candidates, skipped = resolve_registrable(["good", "leak", "ghost"], factors)

    assert [c.source_column for c in candidates] == ["good"]
    assert {s.code for s in skipped} == {"leak", "ghost"}


def test_empty_input_is_not_an_error():
    candidates, skipped = resolve_registrable([], [])

    assert candidates == [] and skipped == []
