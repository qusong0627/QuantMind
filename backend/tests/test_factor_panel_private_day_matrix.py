"""重算快照时按「最新分区的列清单」读历史分区，必须容忍 schema 漂移。

**回归背景（2026-10-07）**

`discovery.load_auto` 只读每个库的**最新**分区来决定「这个库有哪些因子」，而
`_day_matrix` 拿这份列清单去读**每一个**采样日分区。列清单会随新列上线而变，
历史分区却没有那一列：

    features_daily 的 return_*d 在 2026-09-21 改名成 future_return_*d

于是用新列名读 2016 年的分区时，pyarrow 抛
`ArrowInvalid: No match for FieldRef.Name(future_return_1d)`。修之前这条路径从
「因子研究 → 扫描 → 重算快照」全程可达：新增列表里正好是 features_daily 那 8 列，
用户点重算，跑完前 6 个库（约 5 分钟）后死在第 7 个库上，而**所有写盘都在库循环
之后**——白等一次，快照一个字节没变。

本文件钉住修好之后的形状：

1. **缺列不抛异常**，该行整行 NaN（列值只在它真正存在的那段有数据）；
2. **行序恒等于传入的 `names`**，不是分区里的物理列序——错位是静默的数据损坏，
   比崩掉更糟；
3. **schema 一致时结果与朴素读法逐值相同**——修好后不能偷偷改变本来就对的路径。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_factor_panel_private.py"
)
_spec = importlib.util.spec_from_file_location("_bfpp", _SCRIPT)
assert _spec and _spec.loader, f"无法加载 {_SCRIPT}"
bfpp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bfpp)


def _write_partition(tmp_path: Path, cols: dict[str, list[float]]) -> Path:
    """写一个含 symbol 的单日分区；cols 的**插入顺序**即 parquet 的物理列序。"""
    d = tmp_path / "dt=20160104"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"symbol": ["000001.SZ", "600000.SH"], **cols}), d / "data.parquet"
    )
    return d / "data.parquet"


SYMBOLS = pd.Index(["000001.SZ", "600000.SH"])


def test_missing_column_yields_nan_row_instead_of_raising(tmp_path: Path) -> None:
    """历史分区没有新列时必须留 NaN，而不是 ArrowInvalid。"""
    f = _write_partition(tmp_path, {"ma5": [1.0, 2.0]})

    out = bfpp._day_matrix(f, ["ma5", "future_return_1d"], SYMBOLS)

    assert out.shape == (2, 2), "行数必须等于 len(names)，否则调用方按位置写入会错位"
    np.testing.assert_allclose(out[0], [1.0, 2.0])
    assert np.isnan(out[1]).all(), "分区里不存在的列应整行 NaN"


def test_row_order_follows_names_not_physical_column_order(tmp_path: Path) -> None:
    """行序按 `names`，不按 parquet 物理列序——错位是静默的数据损坏。"""
    # 物理列序故意与 names 相反
    f = _write_partition(tmp_path, {"f2": [20.0, 21.0], "f1": [10.0, 11.0]})

    out = bfpp._day_matrix(f, ["f1", "f2"], SYMBOLS)

    np.testing.assert_allclose(out[0], [10.0, 11.0], err_msg="f1 应是第 0 行")
    np.testing.assert_allclose(out[1], [20.0, 21.0], err_msg="f2 应是第 1 行")


def test_matches_naive_read_when_schema_is_intact(tmp_path: Path) -> None:
    """schema 齐全时与朴素读法逐值相同——修好后不能改变本来正确的路径。"""
    f = _write_partition(tmp_path, {"f1": [1.5, 2.5], "f2": [3.5, 4.5]})

    naive = (
        pd.read_parquet(f, columns=["symbol", "f1", "f2"])
        .set_index("symbol")
        .reindex(SYMBOLS)[["f1", "f2"]]
        .to_numpy(dtype=np.float32)
        .T
    )

    np.testing.assert_array_equal(bfpp._day_matrix(f, ["f1", "f2"], SYMBOLS), naive)


def test_symbols_are_aligned_and_absent_symbol_is_nan(tmp_path: Path) -> None:
    """按 symbols 对齐：请求了但分区里没有的股票，该行对应位置留 NaN。"""
    f = _write_partition(tmp_path, {"f1": [1.0, 2.0]})

    out = bfpp._day_matrix(f, ["f1"], pd.Index(["600000.SH", "999999.BJ"]))

    np.testing.assert_allclose(out[0], [2.0, np.nan], equal_nan=True)


def test_all_columns_missing_returns_all_nan(tmp_path: Path) -> None:
    """整批列都对不上时也不能崩——返回全 NaN，由下游按 NaN 处理。"""
    f = _write_partition(tmp_path, {"other": [1.0, 2.0]})

    out = bfpp._day_matrix(f, ["f1", "f2"], SYMBOLS)

    assert out.shape == (2, 2)
    assert np.isnan(out).all()
