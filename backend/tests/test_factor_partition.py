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
    resolve_features,
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


# ── schema 增长陷阱：union_by_name 的两副面孔 ──────────────────────────────
# 实测背景（2026-09-20 生产）：features_daily 自 20260914 起由 50 列变为 78 列
# （新增 is_st / industry_name / in_hs300 / list_date / pb_mrq ...），且是**永久**变更；
# l1_factors 自 20260826 起丢掉 published_at / release_id。两者都跨 schema 边界。
#
# 于是同一段 glob 有两种读法，且**两种都危险**：
#   不带 union_by_name → 静默只给首个文件的列（新列凭空消失，行数却是全量）
#   带   union_by_name → 新列在老分区全为 NULL ⇒ 完美的「年代指示器」，喂给模型即泄露
# 所以「补上 union_by_name」不是修复。真正安全的是 read_partition / 显式列清单。


def _make_growing_schema_library(root):
    """老分区 2 列、新分区多一列（模拟 features_daily 的 50→78）。"""
    _make_library(root, {"20240102": {"symbol": ["A"], "f_old": [1.0]}})
    _make_library(root, {"20240103": {"symbol": ["A"], "f_old": [2.0]}})
    _make_library(
        root, {"20240104": {"symbol": ["A"], "f_old": [3.0], "f_new": [30.0]}}
    )


def test_duckdb_glob_without_union_by_name_silently_drops_later_schema_columns(
    tmp_path,
):
    """不带 union_by_name：行数是全量，**列却只有首个文件的** —— 静默丢列。

    这就是「跨 schema glob」的真实形态：不报错、行数正常、新列凭空消失。
    """
    duckdb = pytest.importorskip("duckdb")
    # Arrange
    lib = tmp_path / "features_daily"
    _make_growing_schema_library(lib)
    files = [
        str(lib / f"dt={dt}" / "data.parquet")
        for dt in ("20240102", "20240103", "20240104")
    ]
    con = duckdb.connect()
    # Act
    cols = [
        r[0]
        for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet({files!r})"
        ).fetchall()
    ]
    n = con.execute(f"SELECT count(*) FROM read_parquet({files!r})").fetchone()[0]
    # Assert：全量 3 行，但只有首文件的两列（dt 是 hive 分区列，自动识别）
    assert n == 3
    assert "f_new" not in cols
    assert sorted(cols) == ["dt", "f_old", "symbol"]


def test_duckdb_glob_with_union_by_name_turns_new_columns_into_era_separator(tmp_path):
    """补 union_by_name：新列在老分区**全 NULL**、在新分区全非 NULL ⇒ 年代泄露。

    这不是「修复」而是把静默丢列换成静默泄露 —— 任何树模型都会拿它做一刀切。
    """
    duckdb = pytest.importorskip("duckdb")
    # Arrange
    lib = tmp_path / "features_daily"
    _make_growing_schema_library(lib)
    files = [
        str(lib / f"dt={dt}" / "data.parquet")
        for dt in ("20240102", "20240103", "20240104")
    ]
    con = duckdb.connect()
    # Act
    rows = con.execute(
        f"SELECT dt, f_new FROM read_parquet({files!r}, union_by_name=true, "
        "hive_partitioning=true) ORDER BY dt"
    ).fetchall()
    # Assert：新的两行 NULL/非 NULL 与日期完全共线
    assert [r[1] for r in rows] == [None, None, 30.0]
    assert [r[0] for r in rows] == [20240102, 20240103, 20240104]  # dt 为整数


def test_read_partition_keeps_named_columns_across_a_schema_growth(tmp_path):
    """契约读法：跨 schema 边界按名取列，缺列即抛，不会 NULL 填充。"""
    # Arrange
    lib = tmp_path / "features_daily"
    _make_growing_schema_library(lib)
    old = lib / "dt=20240102" / "data.parquet"
    new = lib / "dt=20240104" / "data.parquet"
    # Act
    df_old = read_partition(old)
    df_new = read_partition(new)
    # Assert：两日都读到各自真实存在的列，无凭空 NULL 列
    # （特征按名排序 ⇒ f_new 在 f_old 之前，与物理序无关）
    assert list(df_old.columns) == ["symbol", "f_old"]
    assert list(df_new.columns) == ["symbol", "f_new", "f_old"]
    # 跨 schema 累积应由 ColumnContract 拦住（而非静默补 NULL）
    contract = ColumnContract("features_daily")
    contract.check(df_old.columns, context="20240102")
    with pytest.raises(RuntimeError, match="列集合与首日不一致"):
        contract.check(df_new.columns, context="20240104")


# ── 开工前定清单：训练/回测按名读取永不报错 ────────────────────────────────


def test_resolve_features_then_read_partition_never_raises_across_schema_break(
    tmp_path,
):
    """这就是「训练不报错、回测不报错」的正解，端到端钉住。

    跨 schema 断点（这里是 3 列→4 列），先 resolve_features 取交集，
    再拿同一份 columns 逐日 read_partition —— 全程零异常。
    """
    # Arrange
    lib = tmp_path / "features_daily"
    _make_library(lib, {"20240102": {"symbol": ["A"], "f_a": [1.0], "f_b": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["A"], "f_a": [3.0], "f_b": [4.0]}})
    _make_library(
        lib,
        {"20240104": {"symbol": ["A"], "f_a": [5.0], "f_b": [6.0], "f_new": [70.0]}},
    )
    wanted = ["f_a", "f_b", "f_new"]  # f_new 只在最后一天有
    paths = sorted(iter_partitions(lib))

    # Act
    resolved = resolve_features(paths, wanted, library="features_daily")
    frames = [read_partition(p, columns=resolved.read_columns) for p in paths]

    # Assert：f_new 被剔除，剩下的逐日都在，维度恒定
    assert resolved.columns == ("f_a", "f_b")
    # read_columns 必须带上 symbol，否则没法对齐标签
    assert resolved.key_columns == ("symbol",)
    assert [tuple(f.columns) for f in frames] == [("symbol", "f_a", "f_b")] * 3
    assert [f["f_a"].iloc[0] for f in frames] == [1.0, 3.0, 5.0]


def test_resolve_features_drops_column_only_present_in_some_partitions(tmp_path):
    # Arrange
    lib = tmp_path / "features_daily"
    _make_growing_schema_library(lib)
    # Act
    r = resolve_features(
        iter_partitions(lib), ["f_old", "f_new"], library="features_daily"
    )
    # Assert
    assert r.columns == ("f_old",)
    assert [c for c, _ in r.dropped] == ["f_new"]
    assert "缺" in dict(r.dropped)["f_new"]


def test_resolve_features_keeps_everything_when_schema_is_uniform(tmp_path):
    # Arrange
    lib = tmp_path / "l1_factors"
    _make_library(lib, {"20240102": {"symbol": ["A"], "f_b": [1.0], "f_a": [2.0]}})
    _make_library(lib, {"20240103": {"symbol": ["A"], "f_a": [3.0], "f_b": [4.0]}})
    # Act：列序不同不算缺失
    r = resolve_features(iter_partitions(lib), ["f_a", "f_b"], library="l1_factors")
    # Assert
    assert r.columns == ("f_a", "f_b")
    assert r.dropped == ()
    assert r.n_partitions == 2
    assert r.span == ("20240102", "20240103")


def test_resolve_features_raise_mode_reports_which_columns_and_why(tmp_path):
    # Arrange
    lib = tmp_path / "features_daily"
    _make_growing_schema_library(lib)
    # Act / Assert：严格模式必须点名是哪一列、缺在哪
    with pytest.raises(RuntimeError, match="f_new"):
        resolve_features(iter_partitions(lib), ["f_old", "f_new"], on_missing="raise")


def test_resolve_features_missing_reason_carries_the_span(tmp_path):
    """缺失说明要给出区间，调用方才能判断该「截断窗口」还是「去掉该列」。"""
    # Arrange
    lib = tmp_path / "lib"
    _make_library(lib, {"20240102": {"symbol": ["A"], "f_old": [1.0]}})
    _make_library(
        lib,
        {"20240103": {"symbol": ["A"], "f_old": [2.0], "f_side": [9.0]}},
    )
    _make_library(
        lib,
        {"20240104": {"symbol": ["A"], "f_old": [3.0], "f_side": [9.0]}},
    )
    # Act
    r = resolve_features(iter_partitions(lib), ["f_old", "f_side"])
    # Assert：f_side 只从 20240103 起才有
    assert dict(r.dropped)["f_side"] == "仅 20240102 缺"


def test_resolve_features_rejects_nan_fill_policy(tmp_path):
    """刻意不提供「填 NaN」：那是泄露不是容错（老分区全 NULL = 年代指示器）。"""
    lib = tmp_path / "lib"
    _make_growing_schema_library(lib)
    with pytest.raises(ValueError, match="intersect/raise"):
        resolve_features(iter_partitions(lib), ["f_old"], on_missing="nan")


def test_resolve_features_rejects_empty_inputs(tmp_path):
    lib = tmp_path / "lib"
    _make_growing_schema_library(lib)
    with pytest.raises(ValueError, match="至少一个分区"):
        resolve_features([], ["f_old"])
    with pytest.raises(ValueError, match="非空 wanted"):
        resolve_features(iter_partitions(lib), [])


def test_resolve_features_raises_when_intersection_is_empty(tmp_path):
    lib = tmp_path / "lib"
    _make_growing_schema_library(lib)
    with pytest.raises(ValueError, match="交集为空"):
        resolve_features(iter_partitions(lib), ["f_new"])
