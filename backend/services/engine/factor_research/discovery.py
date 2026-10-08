"""因子来源扫描 —— 从 ``<quantdb>/6_ml_datasets`` 发现可用因子库。

**为什么单独成模块**：这套扫描有两个消费者，它们的口径必须**逐字一致**：

- ``backend/scripts/build_factor_panel_private.py``：重算快照时按它决定纳入哪些因子；
- ``GET /factor-research/scan``：扫描按钮按它回答「有什么新的」。

两边一旦分叉，扫描给出的就是**与重算不符的承诺** —— 按钮说「没有新因子」，
重算却算出别的东西（或反过来），而这个不一致没有任何一层会报错。故此处是
唯一实现，构建脚本只做转调。

扫描语义（``auto``）：
- 遍历 ``6_ml_datasets`` 下**含 ``dt=*/data.parquet`` 分区**的目录；
- 跳过 ``AUTO_SKIP``（标签集 / 冗余并集）与 ``.``/``_`` 前缀目录
  （``_`` 是试跑目录的约定，且它的字典序在字母前，会抢在正式库前面赢下去重
  优先级，把正式库的长窗口数据换成短窗口 —— 见 test_factor_panel_private_scan）；
- 取每个库**最新分区**的数值列（只读 parquet schema，不读数据，故秒级）；
- 跨库重名按 ``AUTO_PRIORITY`` 先到先得。

本模块只读，绝不写盘、绝不触发计算。
"""

from __future__ import annotations

import json
from pathlib import Path

# 来源库（auto = 扫描 6_ml_datasets；l1l2 = L1/L2 因子数据；kept = 多库筛选保留清单）
L1L2_SOURCES = ("l1_factors", "l2_factors")
KEPT_LIB_SOURCES = ("alpha_library", "alpha360", "jq110", "tdxgs")
# auto 模式的优先序（重名列先到先得）；未列出的目录按字母序追加
AUTO_PRIORITY = (
    "l1_factors",
    "l2_factors",
    "alpha_library",
    "alpha360",
    "jq110",
    "tdxgs",
    "features_daily",
)
# auto 模式跳过的目录：标签集（非因子）与 L1∪L2 冗余并集
AUTO_SKIP = {"alpha_library_labels", "l1_l2_factors"}
LIB_LABELS = {
    "l1_factors": "L1 因子",
    "l2_factors": "L2 因子",
    "alpha_library": "Alpha 因子库",
    "alpha360": "Alpha360 量价",
    "jq110": "聚宽 JQ110",
    "tdxgs": "通达信指标",
    "features_daily": "每日特征（技术+估值）",
    "factor_research": "经典因子（demo 复刻）",
    "gap_mined": "空档挖掘因子",  # GAP_MINED_MARK
}

# 数值因子列少于这个数的目录视为记录/元数据目录，跳过（不是因子库）
MIN_FACTOR_COLUMNS = 5

# 目录条目里记着「来源库目录名」的字段前缀（factors.json 的 wind_source）。
# 「消失」项要在扫描结果里找不到自己了，唯一还能查到库名的地方就是这个字段；
# 构建侧也必须用它拼（见 build_factor_panel_private），否则两边字符串对不上，
# 差异里的来源库标签会集体变空 —— 不报错，只是悄悄少一列信息。
SOURCE_PREFIX = "QuantDB 6_ml_datasets/"

# 非因子列（标识与行情列不入库；date 为分区日期的冗余列）
DROP_COLS = {
    "symbol",
    "time",
    "dt",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
}


def lib_label(lib: str) -> str:
    """库的中文标签；未登记的库回退成目录名。

    `auto` 的语义是「扫描 6_ml_datasets 全部因子数据集」，所以必须容忍没登记过的
    库：直接下标取字典的话，新增一个因子库就会在**跑完各库、落盘前**抛裸
    `KeyError`，几分钟算力白费且报错点离原因几百行。全仓只此一处取标签。
    """
    return LIB_LABELS.get(lib, lib)


def source_of(wind_source: str) -> str:
    """目录条目的 ``wind_source`` → 来源库目录名；非 6_ml_datasets 来源返回 ``""``。"""
    ws = wind_source or ""
    return ws[len(SOURCE_PREFIX) :] if ws.startswith(SOURCE_PREFIX) else ""


def numeric_factor_names(path: Path) -> list[str]:
    """分区内的数值型因子列（排除标识/行情列与字符串列）。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = []
    for field in pq.ParquetFile(path).schema_arrow:
        if field.name in DROP_COLS:
            continue
        if pa.types.is_integer(field.type) or pa.types.is_floating(field.type):
            out.append(field.name)
    return out


def load_kept() -> tuple[dict[str, list[dict]], list[dict]]:
    """五库联合筛选保留清单 → (外部库分组, factor_research 组)。"""
    from backend.shared.quantdb_paths import resolve_quantdb_dir

    p = (
        resolve_quantdb_dir()
        / "factor_research"
        / "screening"
        / "factor_selection.json"
    )
    if not p.exists():
        raise FileNotFoundError(f"筛选清单缺失（先跑 screen_factors.py）: {p}")
    sel = json.loads(p.read_text(encoding="utf-8"))
    external: dict[str, list[dict]] = {}
    fr_kept: list[dict] = []
    for k in sel.get("kept", []):
        if not k.get("name"):
            continue
        if k["library"] in KEPT_LIB_SOURCES:
            external.setdefault(k["library"], []).append(k)
        elif k["library"] == "factor_research":
            fr_kept.append(k)
    return external, fr_kept


def load_l1l2(qroot: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    """本地 L1/L2 因子数据的因子列清单 → (L1/L2 分组, 空)。"""
    external: dict[str, list[dict]] = {}
    for ds in L1L2_SOURCES:
        root = qroot / "6_ml_datasets" / ds
        parts = sorted(root.glob("dt=*/data.parquet"))
        if not parts:
            raise FileNotFoundError(
                f"{ds} 数据集缺失（先运行特征管线生成 {ds}）: {root}"
            )
        names = numeric_factor_names(parts[-1])
        external[ds] = [{"name": c, "display_name": c} for c in names]
    return external, []


def load_auto(qroot: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    """自动扫描 6_ml_datasets：全部含分区的因子数据集（按优先序去重）。"""
    root = qroot / "6_ml_datasets"
    if not root.exists():
        raise FileNotFoundError(f"6_ml_datasets 目录缺失: {root}")
    found = set()
    for d_ in root.iterdir():
        # 跳过：约定名、点目录、以及**下划线前缀**（临时/试跑目录的约定）。
        # 下划线必须排除，不只是为了整洁：`_`(0x5F) 字典序在字母前，试跑目录会排在
        # 正式库前面先占住列名，正式库的同名列随即被 seen 过滤掉 —— 因子还在，但
        # 用的是试跑那份（通常窗口短得多）的数据，且全程不报错。
        if not d_.is_dir() or d_.name in AUTO_SKIP or d_.name.startswith((".", "_")):
            continue
        parts = sorted(d_.glob("dt=*/data.parquet"))
        if parts:
            found.add(d_.name)
    order = [x for x in AUTO_PRIORITY if x in found] + sorted(
        found - set(AUTO_PRIORITY)
    )
    external: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for ds in order:
        parts = sorted((root / ds).glob("dt=*/data.parquet"))
        names = [c for c in numeric_factor_names(parts[-1]) if c not in seen]
        if len(names) < MIN_FACTOR_COLUMNS:  # 非因子目录（记录/元数据）跳过
            continue
        seen.update(names)
        external[ds] = [{"name": c, "display_name": c} for c in names]
    return external, []


def load_sources(source: str, qroot: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    if source == "kept":
        return load_kept()
    if source == "l1l2":
        return load_l1l2(qroot)
    return load_auto(qroot)


def scan_libraries(qroot: Path | None = None) -> dict[str, list[str]]:
    """``auto`` 扫描 → 有序 ``{库: [因子名]}``（已跨库去重）。

    只读 parquet schema（每库仅取最新一个分区），秒级返回。
    """
    if qroot is None:
        from backend.shared.quantdb_paths import resolve_quantdb_dir

        qroot = resolve_quantdb_dir()
    external, _ = load_auto(qroot)
    return {lib: [it["name"] for it in items] for lib, items in external.items()}


def diff_catalog(
    discovered: dict[str, list[str]],
    catalog: set[str],
    *,
    catalog_library: dict[str, str] | None = None,
) -> dict:
    """扫描结果 × 目录 → 新增 / 消失 / 未变。

    纯函数：不读盘、不写盘，便于锁口径。``catalog_library`` 是目录侧记的来源库
    （用于给「消失」项标注它当初来自哪个库 —— 扫描里已经没有它了）。
    """
    catalog_library = catalog_library or {}
    seen: dict[str, str] = {}
    for lib, names in discovered.items():
        for n in names:
            seen.setdefault(n, lib)

    new_items = [
        {
            "code": code,
            "library": seen[code],
            "library_label": lib_label(seen[code]),
        }
        for code in sorted(set(seen) - catalog)
    ]
    missing_items = [
        {
            "code": code,
            "library": catalog_library.get(code, ""),
            "library_label": lib_label(catalog_library.get(code, "")),
        }
        for code in sorted(catalog - set(seen))
    ]

    new_by_library: dict[str, int] = {}
    for it in new_items:
        new_by_library[it["library"]] = new_by_library.get(it["library"], 0) + 1

    return {
        "new": new_items,
        "missing": missing_items,
        "new_by_library": new_by_library,
        "unchanged_count": len(catalog & set(seen)),
        "discovered_count": len(seen),
        "catalog_count": len(catalog),
    }
