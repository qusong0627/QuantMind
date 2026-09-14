"""自定义市场数据集构建脚本的纯函数测试。

覆盖增量重建的守卫部件：筛选指纹（顺序无关、内容敏感）、缺失分区规划、
板别涨跌停阈值。完整 rebuild() 依赖真实 QuantDB 目录，不在单测覆盖。
"""

from __future__ import annotations

from pathlib import Path

from backend.scripts.build_factor_custom_dataset import (
    _limit_threshold,
    _missing_dates,
    _selection_fingerprint,
)


def test_limit_threshold_by_board():
    # 主板 9.8% / 创业板与科创板 19.8% / 北交所 29.8%（复权价近似口径）
    assert _limit_threshold("600036.SH") == 0.098
    assert _limit_threshold("000001.SZ") == 0.098
    assert _limit_threshold("300750.SZ") == 0.198
    assert _limit_threshold("688981.SH") == 0.198
    assert _limit_threshold("830799.BJ") == 0.298
    assert _limit_threshold("430047.BJ") == 0.298


def test_selection_fingerprint_is_order_insensitive():
    # Arrange：同一筛选集，列顺序不同
    a = {"l1_factors": ["turn_1", "vol_std_5"], "alpha360": ["a360_x"]}
    b = {"alpha360": ["a360_x"], "l1_factors": ["vol_std_5", "turn_1"]}

    # Act / Assert：指纹一致（列清单按内容比较）
    assert _selection_fingerprint(a) == _selection_fingerprint(b)


def test_selection_fingerprint_changes_with_content():
    base = {"l1_factors": ["turn_1"]}

    # Act：增删列或换库都应改变指纹，触发全量重建
    added = {"l1_factors": ["turn_1", "vol_std_5"]}
    moved = {"alpha360": ["turn_1"]}

    # Assert
    assert _selection_fingerprint(base) != _selection_fingerprint(added)
    assert _selection_fingerprint(base) != _selection_fingerprint(moved)


def test_missing_dates_returns_only_unbuilt_partitions(tmp_path: Path):
    # Arrange：两个已建分区（其一仅有目录、缺 data.parquet）
    (tmp_path / "dt=20260910").mkdir()
    (tmp_path / "dt=20260910" / "data.parquet").write_bytes(b"x")
    (tmp_path / "dt=20260911").mkdir()  # 目录在但文件缺失 → 仍需重建
    dates = ["20260910", "20260911", "20260912"]

    # Act
    missing = _missing_dates(dates, tmp_path)

    # Assert
    assert missing == ["20260911", "20260912"]
