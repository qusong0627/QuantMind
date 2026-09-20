"""因子库分区读取契约 + 列序漂移巡检/修复的回归测试。

核心回归目标是**列序漂移致静默串列**这一类 bug（实测 l1_factors 有 170 个漂移分区、
l1_l2_factors 170 个、features_daily 174 个）。测试用合成 parquet 复现：同一批因子名，
在不同交易日的文件里物理位置不同。

判别要点：**窗内自检永远通过**（同一窗口内列序稳定），只有跨窗才暴露 ——
所以测试必须构造「两个不同的物理序」，不能只在单日内断言。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.scripts.check_factor_partition_drift import (
    _apply_plan,
    _write_manifest,
    restore,
    scan_library,
)
from backend.shared.factor_partition import (
    ColumnContract,
    canonical_columns,
    drift_report,
    feature_columns,
    iter_partitions,
    partition_signature,
    read_partition,
    scan_order_groups,
)

META = ["symbol", "date"]


def _make_library(root, layout: dict[str, dict[str, list]]) -> None:
    """按 ``{dt: {列名: 值}}`` 落盘合成因子库（保持 dict 插入序＝物理列序）。"""
    for dt, columns in layout.items():
        d = root / f"dt={dt}"
        d.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns).to_parquet(d / "data.parquet", index=False)


# ── 定序契约 ──────────────────────────────────────────────────────────────


def test_canonical_columns_puts_meta_first_then_sorted_features():
    # Arrange
    physical = ["f_b", "close", "f_a", "symbol", "date", "open"]
    # Act
    result = canonical_columns(physical)
    # Assert
    assert result == ["symbol", "date", "open", "close", "f_a", "f_b"]


def test_canonical_columns_is_invariant_under_input_permutation():
    # Arrange
    import itertools

    cols = ["symbol", "date", "close", "f_a", "f_b", "f_c"]
    # Act
    orders = {tuple(canonical_columns(p)) for p in itertools.permutations(cols)}
    # Assert
    assert len(orders) == 1


def test_feature_columns_excludes_meta_columns():
    # Arrange / Act
    result = feature_columns(["symbol", "date", "open", "z_factor", "a_factor", "time"])
    # Assert
    assert result == ["a_factor", "z_factor"]


def test_canonical_columns_handles_empty_input():
    assert canonical_columns([]) == []


# ── 读取 ──────────────────────────────────────────────────────────────────


def test_read_partition_returns_canonical_order_regardless_of_physical_order(tmp_path):
    # Arrange：同一天，两个文件物理列序相反
    lib = tmp_path / "lib"
    _make_library(
        lib, {"20240102": {"symbol": ["600000.SH"], "f_a": [1.0], "f_b": [2.0]}}
    )
    _make_library(
        lib, {"20240103": {"symbol": ["600000.SH"], "f_b": [3.0], "f_a": [4.0]}}
    )
    # Act
    d1 = read_partition(lib / "dt=20240102" / "data.parquet")
    d2 = read_partition(lib / "dt=20240103" / "data.parquet")
    # Assert
    assert list(d1.columns) == list(d2.columns) == ["symbol", "f_a", "f_b"]


def test_read_partition_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_partition(tmp_path / "nope.parquet")


def test_read_partition_with_explicit_columns_reorders_them(tmp_path):
    # Arrange
    lib = tmp_path / "lib"
    _make_library(
        lib, {"20240102": {"symbol": ["600000.SH"], "f_a": [1.0], "f_b": [2.0]}}
    )
    # Act：显式列清单故意给错序
    df = read_partition(
        lib / "dt=20240102" / "data.parquet", columns=["f_b", "f_a", "symbol"]
    )
    # Assert：仍按规范序输出
    assert list(df.columns) == ["symbol", "f_a", "f_b"]
    assert df["f_a"].tolist() == [1.0]


# ── 回归主测试：位置读取者 vs 契约读取者 ────────────────────────────────────


def test_positional_reader_silently_misaligns_while_contract_reader_does_not(tmp_path):
    """这就是原始 bug 的最小复现：同一批因子、跨日物理列序不同。

    朴素读取者（``pd.read_parquet`` 后直接 ``.to_numpy()``）会把第 1 列在不同日子
    当成同一个因子 —— 静默串列；``read_partition`` 不受影响。
    """
    # Arrange
    lib = tmp_path / "lib"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [10.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_b": [20.0], "f_a": [2.0]}})
    p1, p2 = lib / "dt=20240102" / "data.parquet", lib / "dt=20240103" / "data.parquet"

    # Act — 朴素路径：按物理位置取「第一个特征列」
    naive_first_feature = []
    for p in (p1, p2):
        raw = pd.read_parquet(p)
        naive_first_feature.append(
            raw.drop(columns=[c for c in META if c in raw.columns]).to_numpy()[:, 0]
        )
    # Act — 契约路径：按名字取 f_a
    contract_f_a = [read_partition(p)["f_a"].iloc[0] for p in (p1, p2)]

    # Assert：朴素路径把 f_a 和 f_b 混为一谈（1.0 / 20.0），契约路径稳定（1.0 / 2.0）
    assert [float(v[0]) for v in naive_first_feature] == [1.0, 20.0]
    assert contract_f_a == [1.0, 2.0]


def test_drift_is_invisible_within_a_single_day_window(tmp_path):
    """漂移难发现的原因：窗内自检恒通过。这条测试把这个事实钉住。"""
    # Arrange：前 2 天一个序（同一窗口），第 3 天另一个序（跨窗）
    lib = tmp_path / "lib"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240104": {"symbol": ["X"], "f_b": [2.0], "f_a": [1.0]}})
    # Act
    sigs = [partition_signature(p) for p in sorted(iter_partitions(lib))]
    # Assert：窗内两种相同、跨窗才不同
    assert sigs[0] == sigs[1]
    assert sigs[1] != sigs[2]
    assert set(sigs[1]) == set(sigs[2])  # 列集合相同 ⇒ 纯位置漂移


# ── 跨日守卫 ──────────────────────────────────────────────────────────────


def test_column_contract_accepts_same_set_in_different_order():
    # Arrange
    c = ColumnContract("demo")
    # Act / Assert
    assert c.check(["symbol", "f_a", "f_b"]) == ["f_a", "f_b"]
    assert c.check(["symbol", "f_b", "f_a"]) == ["f_a", "f_b"]


def test_column_contract_raises_on_column_set_change():
    # Arrange
    c = ColumnContract("demo")
    c.check(["symbol", "f_a", "f_b"])
    # Act / Assert：少一列必须炸，不能静默跳过（否则跨日累积少算一天无人察觉）
    with pytest.raises(RuntimeError, match="列集合与首日不一致"):
        c.check(["symbol", "f_a"], context="dt=20240103")


def test_column_contract_raises_when_first_call_has_no_features():
    c = ColumnContract("demo")
    with pytest.raises(RuntimeError, match="无特征列"):
        c.check(["symbol", "date"])


# ── 巡检 ──────────────────────────────────────────────────────────────────


def test_scan_order_groups_groups_by_physical_order(tmp_path):
    # Arrange
    lib = tmp_path / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240104": {"symbol": ["X"], "f_b": [2.0], "f_a": [1.0]}})
    # Act
    groups = scan_order_groups(lib)
    # Assert：主序覆盖 2 天，次序覆盖 1 天
    assert [len(g.dates) for g in groups] == [2, 1]


def test_drift_report_counts_only_reorderable_partitions(tmp_path):
    # Arrange
    lib = tmp_path / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_b": [2.0], "f_a": [1.0]}})
    # Act
    rep = drift_report(lib)
    # Assert
    assert rep["partitions"] == 2
    assert rep["orders"] == 2
    assert rep["drifted"] == 1
    assert rep["schema_variants"] == []


def test_drift_report_empty_library(tmp_path):
    rep = drift_report(tmp_path / "nothing")
    assert rep["partitions"] == 0 and rep["drifted"] == 0


# ── 修复 ──────────────────────────────────────────────────────────────────


def test_scan_library_plans_only_drifted_partitions(tmp_path):
    # Arrange
    lib = tmp_path / "6_ml_datasets" / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_b": [2.0], "f_a": [1.0]}})
    # Act
    stat, det = scan_library("CN", lib)
    # Assert
    assert stat.partitions == 2 and stat.drifted == 1
    assert [f.parent.name for f, _ in det["plan"]] == ["dt=20240103"]
    assert det["plan"][0][1] == ["symbol", "f_a", "f_b"]


def test_scan_library_plans_nothing_when_column_sets_differ_but_order_matches(tmp_path):
    """列集合差异（上游删列）不是漂移 —— 相对次序没变就不该动。"""
    # Arrange
    lib = tmp_path / "6_ml_datasets" / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_a": [1.0]}})
    # Act
    stat, det = scan_library("CN", lib)
    # Assert
    assert stat.drifted == 0
    assert stat.schema_variants == 1
    assert det["plan"] == []


def test_apply_plan_rewrites_to_target_order_and_is_idempotent(tmp_path):
    # Arrange
    lib = tmp_path / "6_ml_datasets" / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_b": [20.0], "f_a": [10.0]}})
    _, det = scan_library("CN", lib)
    # Act
    done = _apply_plan(det["plan"])
    # Assert：物理列序归位，且**因子值没被改动**
    assert done == 1
    fixed = lib / "dt=20240103" / "data.parquet"
    assert partition_signature(fixed) == ("symbol", "f_a", "f_b")
    df = pd.read_parquet(fixed)
    assert df["f_a"].tolist() == [10.0] and df["f_b"].tolist() == [20.0]
    # 幂等：再扫一遍没有待修的
    stat2, det2 = scan_library("CN", lib)
    assert stat2.drifted == 0 and det2["plan"] == []


def test_apply_plan_leaves_no_temp_files_behind(tmp_path):
    # Arrange
    lib = tmp_path / "6_ml_datasets" / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_b": [2.0], "f_a": [1.0]}})
    _, det = scan_library("CN", lib)
    # Act
    _apply_plan(det["plan"])
    # Assert
    assert list(lib.rglob("*.tmp.parquet")) == []


# ── 回滚 ──────────────────────────────────────────────────────────────────


def test_manifest_roundtrip_restores_original_order_and_values(tmp_path):
    """修复 → 回滚 必须逐位还原，否则生产修复没有安全网。"""
    # Arrange
    lib = tmp_path / "6_ml_datasets" / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["X"], "f_b": [20.0], "f_a": [10.0]}})
    drifted = lib / "dt=20240103" / "data.parquet"
    original_order = partition_signature(drifted)
    _, det = scan_library("CN", lib)
    manifest = _write_manifest(det["plan"], tmp_path / "manifest.json")
    # Act：修好，再回滚
    _apply_plan(det["plan"])
    assert partition_signature(drifted) == ("symbol", "f_a", "f_b")
    restore(str(manifest))
    # Assert：物理列序与数值都回到原样
    assert partition_signature(drifted) == original_order
    df = pd.read_parquet(drifted)
    assert df["f_b"].tolist() == [20.0] and df["f_a"].tolist() == [10.0]


def test_restore_ignores_files_missing_from_disk(tmp_path, caplog):
    # Arrange：清单里指向一个已删除的文件，回滚应跳过而不是崩
    manifest = tmp_path / "m.json"
    manifest.write_text('{"%s": ["symbol", "f_a"]}' % (tmp_path / "gone.parquet"))
    # Act / Assert
    assert restore(str(manifest)) == 0
    assert not (tmp_path / "gone.parquet").exists()


# ── 巡检器自身的判据（防「绝对位置」误判回归） ──────────────────────────────


def test_schema_subset_is_not_reported_as_drift(tmp_path):
    """删掉一列会让后续列的**绝对位置**整体前移 —— 但相对次序没变，不算漂移。

    （曾用 ``ref.index(c) != order.index(c)`` 判过，把 quantus/quanthk 的
    「缺一列」误报成 173 列漂移。）
    """
    # Arrange
    lib = tmp_path / "6_ml_datasets" / "l1_factors"
    _make_library(
        lib, {"20240102": {"symbol": ["X"], "f_a": [1.0], "f_b": [2.0], "f_c": [3.0]}}
    )
    _make_library(
        lib, {"20240103": {"symbol": ["X"], "f_a": [1.0], "f_c": [3.0]}}
    )  # 缺 f_b
    # Act
    stat, det = scan_library("CN", lib)
    # Assert
    assert stat.drifted == 0
    assert det["plan"] == []
