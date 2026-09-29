"""私人因子库 `auto` 扫描的来源选择规则。

回归背景（2026-09-19）：`6_ml_datasets/` 下多出一个**下划线前缀的试跑目录**后，
`auto` 扫描把它当正式因子库收进来，随后 `LIB_LABELS[lib]` 抛 `KeyError`，
整个重建在跑完 [2/6] 各库之后崩掉 —— 几分钟算力白费，且报错点在 200 行之外，
只给一个裸 `KeyError: '_pilot_xxx'`，看不出该改哪里。

同时暴露的第二个口子：`LIB_LABELS` 是**白名单**，但凡有个库没登记就崩，
而 `auto` 的语义是「扫描 `6_ml_datasets` 全部因子数据集」。新增因子库必须改代码
才能被扫到，与「自动扫描」的承诺不符。

这里锁两条规则：
1. `_` 前缀目录 = 临时/试跑产物，不纳入（试跑子集窗口短，还会靠字母序抢在正式库
   前面赢下去重优先级，把正式库的长窗口数据换成短窗口）；
2. 未登记标签的库**不崩**，回退用目录名当标签 —— 既不静默丢弃，也不用为新库改代码。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_factor_panel_private.py"
)
_spec = importlib.util.spec_from_file_location("_bfpp", _SCRIPT)
assert _spec and _spec.loader, f"无法加载 {_SCRIPT}"
bfpp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bfpp)


def _write_ds(
    root: Path, name: str, factor_cols: list[str], day: str = "20240102"
) -> Path:
    """造一个最小可扫的数据集目录：dt=<day>/data.parquet，含 symbol + 若干数值因子列。"""
    d = root / name / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "symbol": ["000001.SZ", "600000.SH"],
            **{c: [1.0, 2.0] for c in factor_cols},
        }
    )
    pq.write_table(table, d / "data.parquet")
    return d


@pytest.fixture()
def qroot(tmp_path: Path) -> Path:
    root = tmp_path / "quantdb" / "6_ml_datasets"
    root.mkdir(parents=True)
    return tmp_path / "quantdb"


def test_scan_includes_registered_library(qroot: Path):
    """已登记标签的库正常纳入（同时作为「扫描非空」的对照，防假通过）。"""
    _write_ds(qroot / "6_ml_datasets", "alpha_library", ["f1", "f2", "f3", "f4", "f5"])

    external, fr = bfpp._load_auto(qroot)

    assert "alpha_library" in external, f"已登记的库未被扫到：{sorted(external)}"
    assert [e["name"] for e in external["alpha_library"]] == [
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
    ]
    assert fr == []


def test_scan_skips_underscore_prefixed_scratch_dirs(qroot: Path):
    """`_` 前缀 = 试跑目录，不纳入；正式库不受影响。"""
    root = qroot / "6_ml_datasets"
    _write_ds(root, "alpha_library", ["f1", "f2", "f3", "f4", "f5"])
    _write_ds(root, "_pilot_trial", ["p1", "p2", "p3", "p4", "p5"])

    external, _ = bfpp._load_auto(qroot)

    assert "_pilot_trial" not in external, "下划线前缀的试跑目录不应纳入私人库"
    assert "alpha_library" in external, "对照库丢了，说明扫描整段没跑"


def test_scan_scratch_dir_does_not_win_dedup_over_real_library(qroot: Path):
    """试跑目录即便靠字母序排前，也不能把正式库的同名因子顶掉。

    `_`(0x5F) 的字典序在字母之前；只有排在 `AUTO_PRIORITY` 里的库才不受影响，
    优先序之外的库（靠字母序追加的那批）会被试跑版先占住名字，同名列随之被
    `seen` 过滤掉 —— 因子还在，但用的是短窗口那份数据。故此处用优先序外的库名。
    """
    root = qroot / "6_ml_datasets"
    _write_ds(root, "zzz_real_lib", ["shared", "only_real", "f3", "f4", "f5"])
    _write_ds(root, "_pilot_trial", ["shared", "only_pilot", "p3", "p4", "p5"])

    external, _ = bfpp._load_auto(qroot)

    assert sorted(external) == ["zzz_real_lib"], (
        f"来源应为且仅为正式库，实为 {sorted(external)}"
    )
    shared = [e["name"] for e in external["zzz_real_lib"] if e["name"] == "shared"]
    assert shared == ["shared"], "同名因子应来自正式库"


def test_lib_label_falls_back_to_dirname_for_unregistered_library():
    """未登记标签的库取标签不崩：回退成目录名。

    崩溃点不在扫描（`_load_auto` 不碰 LIB_LABELS），而在 `main()` 落盘前组装
    meta 时。**带默认值的取值是唯一安全写法**：`LIB_LABELS[x]` 会对任何新库抛
    `KeyError`，且报错点在几百行之外、跑完各库之后才炸。
    """
    assert bfpp._lib_label("alpha_library") == "Alpha 因子库"
    assert bfpp._lib_label("brand_new_lib") == "brand_new_lib"


def test_main_has_no_bare_lib_labels_subscript():
    """静态守卫：`main()` 不得用 `LIB_LABELS[` 直接下标（唯一的 KeyError 来源）。

    这条防的是「一处 .get、一处 []」的双写法回归 —— 扫描侧用 `.get` 看着没事，
    崩溃在落盘前那处 `[]`，看不出关联。
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "LIB_LABELS[" not in src, "请统一走 _lib_label()，不要直接下标 LIB_LABELS"


def test_scan_skips_dirs_below_factor_column_threshold(qroot: Path):
    """数值因子列 < 5 的目录视为记录/元数据目录，跳过。"""
    root = qroot / "6_ml_datasets"
    _write_ds(root, "alpha_library", ["f1", "f2", "f3", "f4", "f5"])
    _write_ds(root, "meta_only", ["a", "b"])

    external, _ = bfpp._load_auto(qroot)

    assert "meta_only" not in external
    assert "alpha_library" in external


def _wide_part(cols: dict[str, list[float]]) -> pd.DataFrame:
    """造一块宽表分片：MultiIndex(trade_date, symbol) + 若干 float64 因子列。"""
    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2024-01-02", "2024-02-01"]), pd.Index(["a", "b"])],
        names=["trade_date", "symbol"],
    )
    return pd.DataFrame(
        {c: np.asarray(v, dtype=np.float64) for c, v in cols.items()}, index=idx
    )


def test_write_wide_scores_roundtrip_and_float32(tmp_path: Path):
    """宽表落盘：内容不变、因子列压成 float32（体积减半），两个索引列在位。"""

    parts = [
        _wide_part({"f1": [1.5, 2.5, 3.5, 4.5]}),
        _wide_part({"g1": [-1.0, -2.0, -3.0, -4.0]}),
    ]
    p = tmp_path / "wide.parquet"

    n_cols = bfpp._write_wide_scores(parts, p)

    assert n_cols == 4, f"应为 2 索引列 + 2 因子列，实得 {n_cols}"
    back = pd.read_parquet(p)
    assert list(back.columns) == ["trade_date", "symbol", "f1", "g1"]
    assert back["f1"].dtype == np.float32, (
        "因子列必须压成 float32（内存/体积修复的契约）"
    )
    assert back["g1"].dtype == np.float32
    # 日期列只锁「是 datetime 且顺序/值原样」：分辨率随 pandas 默认走
    # （pandas 3 写 us、pandas 2 写 ns）。读侧 scores_for 是 dtype 无关的
    # melt，钉死 ns 会让同一份代码在两种环境下必红。
    assert pd.api.types.is_datetime64_any_dtype(back["trade_date"])
    assert list(back["trade_date"].dt.strftime("%Y-%m-%d")) == [
        "2024-01-02",
        "2024-01-02",
        "2024-02-01",
        "2024-02-01",
    ]
    assert len(back) == 4, "行数必须完整"
    np.testing.assert_allclose(back["f1"].to_numpy(), [1.5, 2.5, 3.5, 4.5], rtol=1e-6)
    np.testing.assert_allclose(
        back["g1"].to_numpy(), [-1.0, -2.0, -3.0, -4.0], rtol=1e-6
    )


def test_write_wide_scores_rejects_duplicate_columns(tmp_path: Path):
    """跨分片重名列必须报错。

    扫描阶段已按优先级去重，这里是兜底：arrow 建表允许重名列，一旦漏网，
    下游 `read_parquet(columns=[...])` 取到哪一份就说不清了 —— 静默取错。
    """

    parts = [_wide_part({"f1": [1.0, 2.0, 3.0, 4.0]}), _wide_part({"f1": [9.0] * 4})]

    with pytest.raises(ValueError, match="重复"):
        bfpp._write_wide_scores(parts, tmp_path / "dup.parquet")


def test_write_wide_scores_preserves_categorical_symbol(tmp_path: Path):
    """`symbol` 是分类列时保持分类（与既有快照的 dictionary 编码一致，读侧口径不变）。"""

    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2024-01-02"]), pd.Categorical(["a", "b"])],
        names=["trade_date", "symbol"],
    )
    parts = [pd.DataFrame({"f1": pd.array([1.0, 2.0], dtype=np.float64)}, index=idx)]

    p = tmp_path / "cat.parquet"
    bfpp._write_wide_scores(parts, p)

    assert pd.read_parquet(p)["symbol"].dtype.name == "category"


def test_scan_raises_when_dataset_root_missing(tmp_path: Path):
    """6_ml_datasets 都不存在时明确报错，而不是返回空表让下游静默空转。"""
    with pytest.raises(FileNotFoundError, match="6_ml_datasets"):
        bfpp._load_auto(tmp_path / "nope")
