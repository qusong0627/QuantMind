"""因子报告支持的数据集注册表（构建脚本与 API 服务共用，避免两处口径漂移）。

每个数据集声明：
  dir_parts    —— 相对 QuantDB 数据根的目录（经 quantdb_paths 解析，禁硬编码绝对路径）
  label_mode   —— labels_table：独立标签表（含 fwd_ret_* 列）
                  close_fwd   ：用本表 close 在 T+k 分区的收盘价算 (close_{T+k}/close_T - 1)
                                与训练侧 features_daily.return_Nd 同口径
  label_parts  —— labels_table 模式的标签目录
  meta_cols    —— 非因子列（标识 + OHLCV + 血缘元数据）；构建时再按 dtype 兜一层
  library_rule —— 因子库归属规则：alpha_prefix（按 a101/a158/gtja 前缀）|
                  fixed:L1 / fixed:L2 | l2_membership（L1+L2 表按是否也在 L2 表中判定）
"""

from __future__ import annotations

from pathlib import Path

from backend.shared.quantdb_paths import resolve_quantdb_subdir

_ID_COLS = ("symbol", "date", "time", "dt")
_OHLCV_COLS = ("open", "high", "low", "close", "volume", "amount")

DATASETS: dict[str, dict] = {
    "alpha_library": {
        "label": "Alpha 库（Alpha101 / GTJA191 / Alpha158）",
        "dir_parts": ("6_ml_datasets", "alpha_library"),
        "label_mode": "labels_table",
        "label_parts": ("6_ml_datasets", "alpha_library_labels"),
        "meta_cols": _ID_COLS,
        "library_rule": "alpha_prefix",
        "universe": "A股全市场 · Alpha 库（Alpha101/GTJA191/Alpha158）",
    },
    # 因子清单库：只含**日频可复现**的因子。
    # ⚠️ 分钟族、财务 PIT 族，以及需要 point-in-time 指数成分股的，都不在内
    # （各自的缺口原因不同，补齐所需的数据依赖也不同）。
    # 具体的纳入/剔除清单见 <数据集>/MANIFEST.json。
    # 标签复用 alpha_library_labels：同一份 load_kline 网格、且标签只依赖 close。
    "factor_defs": {
        "label": "因子清单库（日频可复现部分）",
        "dir_parts": ("6_ml_datasets", "factor_defs"),
        "label_mode": "labels_table",
        "label_parts": ("6_ml_datasets", "alpha_library_labels"),
        "meta_cols": _ID_COLS,
        # 暂无子库切分：源清单的「分类」字段 46% 是「其他/未分类」，硬切反而误导；
        # 单库直出排行表，要按家族筛再看是否需要规则化。
        "library_rule": "fixed:factor_defs",
        "universe": "A股全市场 · 因子清单库（日频可复现因子）",
    },
    "l1_factors": {
        "label": "L1 因子（量价/换手/波动等基础因子）",
        "dir_parts": ("6_ml_datasets", "l1_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:L1",
        "universe": "A股全市场 · L1 因子（量价/换手/波动等基础因子）",
    },
    "l2_factors": {
        "label": "L2 因子（逐笔微观结构，覆盖期短于 L1）",
        "dir_parts": ("6_ml_datasets", "l2_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:L2",
        "universe": "A股全市场 · L2 因子（逐笔微观结构）",
    },
    "l1_l2_factors": {
        "label": "L1 + L2 合并因子",
        "dir_parts": ("6_ml_datasets", "l1_l2_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "l2_membership",
        "universe": "A股全市场 · L1+L2 合并因子",
    },
    "tdxgs": {
        "label": "TDXGS 通达信技术指标（88）",
        "dir_parts": ("6_ml_datasets", "tdxgs"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:tdxgs",
        "universe": "A股全市场 · 通达信/同花顺技术指标（MyTT 口径）",
    },
    "jq110": {
        "label": "JQ110 聚宽因子（109）",
        "dir_parts": ("6_ml_datasets", "jq110"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:jq110",
        "universe": "A股全市场 · 聚宽策略因子（动量/情绪/技术/风险/风格）",
    },
    "alpha360": {
        "label": "Alpha360 原始量价回溯（360）",
        "dir_parts": ("6_ml_datasets", "alpha360"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:alpha360",
        "universe": "A股全市场 · 60 日原始量价序列（DL 用）",
    },
    # GAP_MINED_MARK: 空档挖掘因子库（2026-09 从 QuantDB 未开采矿脉挖出，69 因子）
    "gap_mined": {
        "label": "空档挖掘因子（股东机构/财务PIT/板块概念/两融/L2二阶/新闻情绪，69）",
        "dir_parts": ("6_ml_datasets", "gap_mined"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "gap_family",
        "universe": "A股全市场 · 空档挖掘因子（2020-01 起，L2二阶自 2022-01）",
    },
}

DEFAULT_DATASET = "alpha_library"


def dataset_names() -> list[str]:
    return list(DATASETS)


def dataset_dir(dataset: str) -> Path:
    cfg = DATASETS.get(dataset) or DATASETS[DEFAULT_DATASET]
    return resolve_quantdb_subdir(*cfg["dir_parts"])


def label_dir(dataset: str) -> Path | None:
    cfg = DATASETS.get(dataset) or DATASETS[DEFAULT_DATASET]
    parts = cfg.get("label_parts")
    return resolve_quantdb_subdir(*parts) if parts else None


def report_dir(dataset: str) -> Path:
    return dataset_dir(dataset) / "report"


def l2_columns() -> set[str]:
    """l2_factors 的列集合（用于 L1+L2 表的归属判定）。"""
    root = dataset_dir("l2_factors")
    parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    if not parts:
        return set()
    try:
        import pyarrow.parquet as pq

        return set(pq.ParquetFile(f"{parts[-1]}/data.parquet").schema_arrow.names)
    except Exception:  # noqa: BLE001
        return set()


def library_of(dataset: str, column: str, l2_cols: set[str] | None = None) -> str:
    cfg = DATASETS.get(dataset) or DATASETS[DEFAULT_DATASET]
    rule = str(cfg["library_rule"])
    if rule.startswith("fixed:"):
        return rule.split(":", 1)[1]
    if rule == "alpha_prefix":
        if column.startswith("a158"):
            return "alpha158"
        if column.startswith("a101"):
            return "alpha101"
        return "gtja191"
    if rule == "l2_membership":
        cols = l2_cols if l2_cols is not None else l2_columns()
        return "L2" if column in cols else "L1"
    if rule == "gap_family":
        # GAP_MINED_MARK: 按因子名前缀判子库（因子名自带家族语义）
        if column.startswith("HD_"):
            return "股东机构筹码"
        if column.startswith("FIN_"):
            return "财务基本面PIT"
        if column.startswith("MG2_"):
            return "融资融券"
        if column.startswith("N2_"):
            return "北向持股"
        if column.startswith(("IND_", "CONCEPT_")):
            return "板块概念"
        if column.startswith("NEWS_"):
            return "新闻情绪"
        return "L2二阶微观结构"
    return "unknown"
