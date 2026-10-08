"""因子研究「扫描」—— 扫描结果与快照目录的差异（新增/消失）。

为什么要单独造这条链：用户挖到新因子写进 quantdb 后，界面上看不见它们。
原因是目录（``factors.json``）是一份**快照产物**，不是每次读盘现算的。
重算一次私人库要 5~15 分钟并重写 5.5GB 宽表，所以需要一个**秒级**的
「先看看有什么新的」入口，由用户决定要不要付这次重算。

这里锁的是**差异口径**，不是 UI：

1. 差异必须来自**与重算同一次扫描**（同一份 ``discovery``），否则扫描说
   「没有新因子」而重算却算出别的结果 —— 扫描就成了骗人的前置提示；
2. 扫描**只读**：绝不能顺手写 factors.json 或触发构建（这是个 GET）；
3. classic 数据集没有「目录快照」这个概念（目录来自静态 catalog.py），
   必须明确拒绝，而不是返回一份看起来合理的空差异。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

_ROOT = Path(__file__).resolve().parents[2]

# discovery 模块：本次改动把「扫描」从构建脚本里提出来，供构建与接口共用
_DISCOVERY = (
    _ROOT / "backend" / "services" / "engine" / "factor_research" / "discovery.py"
)
_spec = importlib.util.spec_from_file_location("_discovery", _DISCOVERY)
assert _spec and _spec.loader, f"无法加载 {_DISCOVERY}"
discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discovery)


def _write_ds(root: Path, name: str, cols: list[str], day: str = "20240102") -> Path:
    """最小可扫数据集：dt=<day>/data.parquet，含 symbol + 若干数值因子列。"""
    d = root / name / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {"symbol": ["000001.SZ", "600000.SH"], **{c: [1.0, 2.0] for c in cols}}
        ),
        d / "data.parquet",
    )
    return d


@pytest.fixture()
def qroot(tmp_path: Path) -> Path:
    root = tmp_path / "quantdb"
    (root / "6_ml_datasets").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# 纯差异：扫描结果 × 目录
# ---------------------------------------------------------------------------
def test_diff_reports_new_and_missing():
    """三条集合：新增（盘上有、目录没有）、消失（目录有、盘上没了）、其余算未变。"""
    discovered = {"alpha_library": ["a1", "a2", "brand_new"]}
    catalog = {"a1", "a2", "removed_one"}

    out = discovery.diff_catalog(discovered, catalog)

    assert [x["code"] for x in out["new"]] == ["brand_new"]
    assert out["new"][0]["library"] == "alpha_library"
    assert [x["code"] for x in out["missing"]] == ["removed_one"]
    assert out["unchanged_count"] == 2
    assert out["discovered_count"] == 3
    assert out["catalog_count"] == 3


def test_diff_attaches_library_label_and_falls_back_to_dirname():
    """新增项要带库的中文标签；**没登记过的库回退成目录名**（新库必须能露面）。"""
    out = discovery.diff_catalog({"brand_new_lib": ["x1"]}, set())

    assert out["new"][0]["library"] == "brand_new_lib"
    assert out["new"][0]["library_label"] == "brand_new_lib"
    assert out["new_by_library"] == {"brand_new_lib": 1}

    known = discovery.diff_catalog({"alpha_library": ["x1"]}, set())
    assert known["new"][0]["library_label"] == "Alpha 因子库"


def test_diff_is_empty_when_nothing_changed():
    """无变化时三块都空 —— 前端据此显示「已是最新」，不能靠计数猜。"""
    out = discovery.diff_catalog({"l1_factors": ["f1"]}, {"f1"})

    assert out["new"] == []
    assert out["missing"] == []
    assert out["unchanged_count"] == 1


def test_diff_missing_keeps_snapshot_library_when_known():
    """消失项优先用调用方给的「目录里记的库」，退回扫描结果里的库。

    快照记着它当初来自哪个库；扫描已经没有它了，拿扫描的库只能是空的。
    """
    out = discovery.diff_catalog(
        {"l1_factors": ["f1"]}, {"gone1"}, catalog_library={"gone1": "l2_factors"}
    )

    assert out["missing"][0]["library"] == "l2_factors"


# ---------------------------------------------------------------------------
# 扫描侧：与重算同源
# ---------------------------------------------------------------------------
def test_scan_libraries_matches_build_scan(qroot: Path):
    """接口用的扫描与构建脚本用的是**同一个函数**，逐库因子名必须一模一样。

    这条是本次改动的核心不变量：两边一旦分叉，扫描就会给出与重算不符的承诺。
    """
    root = qroot / "6_ml_datasets"
    _write_ds(root, "alpha_library", ["f1", "f2", "f3", "f4", "f5"])

    build_spec = importlib.util.spec_from_file_location(
        "_bfpp", _ROOT / "backend" / "scripts" / "build_factor_panel_private.py"
    )
    assert build_spec and build_spec.loader
    bfpp = importlib.util.module_from_spec(build_spec)
    build_spec.loader.exec_module(bfpp)

    from_api = discovery.scan_libraries(qroot)
    from_build, _ = bfpp._load_auto(qroot)

    assert from_api == {
        lib: [it["name"] for it in items] for lib, items in from_build.items()
    }


def test_scan_libraries_excludes_scratch_dirs(qroot: Path):
    """`_` 前缀的试跑目录不露面 —— 否则「新增 N 个」会把试跑因子算进来。"""
    root = qroot / "6_ml_datasets"
    _write_ds(root, "alpha_library", ["f1", "f2", "f3", "f4", "f5"])
    _write_ds(root, "_pilot_trial", ["p1", "p2", "p3", "p4", "p5"])

    libs = discovery.scan_libraries(qroot)

    assert "_pilot_trial" not in libs
    assert "alpha_library" in libs


def test_scan_libraries_dedups_across_libraries_by_priority(qroot: Path):
    """跨库重名按优先序取首库 —— 与重算的去重口径一致，否则差异数是假的。"""
    root = qroot / "6_ml_datasets"
    _write_ds(root, "l1_factors", ["shared", "only_l1", "a", "b", "c"])
    # 次库给 6 列，被拿走 "shared" 后仍剩 5 —— 见下面那条「降级到阈值以下会整库消失」
    _write_ds(root, "zzz_other", ["shared", "only_other", "d", "e", "f", "g"])

    libs = discovery.scan_libraries(qroot)

    assert libs["l1_factors"] == ["shared", "only_l1", "a", "b", "c"]
    assert libs["zzz_other"] == ["only_other", "d", "e", "f", "g"]
    assert "shared" not in libs["zzz_other"], "重名列必须只出现在优先级更高的库里"


def test_library_dropped_when_dedup_leaves_below_threshold(qroot: Path):
    """去重后不足 ``MIN_FACTOR_COLUMNS`` 列的库**整库不纳入**（既有语义，勿改）。

    口径是「去重之后再数」而不是「去重之前数」：次库的列被高优先级库拿走之后
    可能掉到阈值以下，此时整个库消失 —— 连它没重名的那些因子也不进了。

    这正是扫描与重算**必须同源**的理由：若扫描按去重前的列数判断，它会把这些
    因子报成「新增」，而重算根本不会纳入它们，用户会一直等一个不会出现的结果。
    """
    root = qroot / "6_ml_datasets"
    _write_ds(root, "l1_factors", ["shared", "only_l1", "a", "b", "c"])
    # 5 列；被高优先级库拿走 "shared" 后剩 4 列 < MIN_FACTOR_COLUMNS → 整库跳过
    _write_ds(root, "zzz_other", ["shared", "only_other", "d", "e", "f"])

    libs = discovery.scan_libraries(qroot)

    assert "zzz_other" not in libs, "去重后仅 4 列，应整库跳过"
    assert libs["l1_factors"] == ["shared", "only_l1", "a", "b", "c"]


def test_scan_libraries_raises_when_root_missing(tmp_path: Path):
    """6_ml_datasets 不存在时明确报错，不返回空表（空表会被读成「没有新因子」）。"""
    with pytest.raises(FileNotFoundError, match="6_ml_datasets"):
        discovery.scan_libraries(tmp_path / "nope")
