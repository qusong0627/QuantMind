"""QuantDB 特征源统一访问层。

旧入口 ``db/feature_snapshots/model_features_*.parquet`` 已被 QuantDB 直读取代，
所有读取训练/推理/回测/因子分析特征的代码都应经由本模块，禁止散落硬编码
parquet 路径。

- 规范数据源：QuantDB ``6_ml_datasets/<source>/dt=YYYYMMDD/data.parquet``
- 读取口径：symbol 统一转前缀式（SH600036），日期列统一为 ``trade_date``
- 遗留兼容：``QM_LEGACY_FEATURE_SNAPSHOTS`` 显式开启时旧快照仍可读写，
  仅用于存量旧模型；新链路一律走 QuantDB。

目录/市场映射复用 ``quantdb_factor_reader``，避免重复维护。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pandas as pd

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

LEGACY_FEATURE_SNAPSHOTS_ENV = "QM_LEGACY_FEATURE_SNAPSHOTS"
_TRUE = {"1", "true", "yes", "on"}
_DEFAULT_SOURCE = "l1_factors"
_CODE6_RE = re.compile(r"(\d{6})$")


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def legacy_feature_snapshots_enabled() -> bool:
    """是否允许读写旧的 ``model_features_*.parquet`` 快照。"""
    return os.getenv(LEGACY_FEATURE_SNAPSHOTS_ENV, "").strip().lower() in _TRUE


def warn_legacy_feature_snapshots(where: str) -> None:
    logger.warning(
        "[legacy-feature-snapshots] %s 仍引用 model_features_*.parquet 旧入口；"
        "请迁移到 QuantDB 因子分区 6_ml_datasets/*（可设 %s=0 阻断写入）。",
        where,
        LEGACY_FEATURE_SNAPSHOTS_ENV,
    )


def legacy_feature_snapshot_dir() -> Path:
    return project_root() / "db" / "feature_snapshots"


# ── QuantDB 因子源（唯一规范入口）────────────────────────────────────────────


def market_data_dir(market: str = "CN") -> Path:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        market_data_dir as _market_data_dir,
    )

    return _market_data_dir(market)


def factor_source_dir(source: str = _DEFAULT_SOURCE, market: str = "CN") -> Path:
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    reader = QuantDBFactorReader(market=market)
    return reader.source_path(source)


def user_dataset_dir(dataset: str = _DEFAULT_SOURCE) -> Path:
    """用户自定义数据集目录。

    宿主 ``./data/quantcustom/6_ml_datasets/<dataset>/dt=YYYYMMDD/data.parquet``，
    容器内 ``/data/quantcustom/6_ml_datasets/<dataset>``（QM_QUANTCUSTOM_DATA_DIR）。
    因子挖掘、历史补全等用户产出写这里，经后台「字段发现 + 草稿发布」后可用于训练。
    """
    return factor_source_dir(dataset, market="CUSTOM")


def list_factor_partitions(
    source: str = _DEFAULT_SOURCE, market: str = "CN"
) -> list[Path]:
    """返回已发布分区文件 ``dt=YYYYMMDD/data.parquet``（已排序）。"""
    root = factor_source_dir(source, market)
    return sorted(root.glob("dt=*/data.parquet")) if root.is_dir() else []


def _partition_dt(path: Path) -> str:
    try:
        return path.parent.name.split("=", 1)[1]
    except IndexError:  # pragma: no cover - 目录结构异常
        return ""


def _code6(value: object) -> str:
    prefix = StockCodeUtil.to_prefix(str(value or ""))
    match = _CODE6_RE.search(prefix)
    return match.group(1) if match else prefix


def _schema_columns(path: Path) -> list[str]:
    import pyarrow.parquet as pq

    return list(pq.ParquetFile(str(path)).schema_arrow.names)


def read_factor_source(
    source: str = _DEFAULT_SOURCE,
    market: str = "CN",
    *,
    columns: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    normalize_symbol: bool = True,
) -> pd.DataFrame:
    """把 QuantDB 因子分区读入内存，返回含 ``symbol`` / ``trade_date`` 的 DataFrame。

    ``columns`` 为期望列名（缺失列自动忽略）；缺省读取全部分区列。
    symbol 默认归一化为前缀式，与训练/推理/API 层口径一致。
    """
    partitions = list_factor_partitions(source, market)
    if not partitions:
        raise FileNotFoundError(
            f"QuantDB 因子源无分区: {factor_source_dir(source, market)}"
        )
    start_s = str(start).replace("-", "")[:8] if start else None
    end_s = str(end).replace("-", "")[:8] if end else None
    if start_s or end_s:
        partitions = [
            p
            for p in partitions
            if (not start_s or _partition_dt(p) >= start_s)
            and (not end_s or _partition_dt(p) <= end_s)
        ]
    if not partitions:
        raise FileNotFoundError(
            f"QuantDB 因子源在 [{start}, {end}] 无分区: {factor_source_dir(source, market)}"
        )

    available = _schema_columns(partitions[0])
    read_cols = None
    if columns is not None:
        read_cols = []
        missing = []
        for column in columns:
            if column in available:
                read_cols.append(column)
                continue
            # 分区列是 date 或 dt，对外统一别名 trade_date
            if column == "trade_date":
                source_col = next(
                    (c for c in ("date", "dt") if c in available), None
                )
                if source_col and source_col not in read_cols:
                    read_cols.append(source_col)
                    continue
            missing.append(column)
        if missing:
            logger.warning("QuantDB 因子源 %s 缺失列（忽略）: %s", source, missing)

    frames: list[pd.DataFrame] = []
    for p in partitions:
        try:
            frames.append(pd.read_parquet(p, columns=read_cols, engine="pyarrow"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("跳过分区 %s: %s", p, exc)
    if not frames:
        raise RuntimeError(f"QuantDB 因子源读取失败: {factor_source_dir(source, market)}")

    df = pd.concat(frames, ignore_index=True)
    if "trade_date" not in df.columns:
        if "date" in df.columns:
            df = df.rename(columns={"date": "trade_date"})
        elif "dt" in df.columns:
            df = df.rename(columns={"dt": "trade_date"})
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    if normalize_symbol and "symbol" in df.columns:
        df["symbol"] = df["symbol"].map(
            lambda v: StockCodeUtil.to_prefix(str(v))
        )
    return df


def merge_factor_into_source(
    factor_df: pd.DataFrame,
    feature_name: str,
    source: str = _DEFAULT_SOURCE,
    market: str = "CN",
    *,
    create_missing: bool = False,
) -> int:
    """把单列因子按交易日合并写回 QuantDB 因子分区（原子替换），返回非空值数。

    仅触碰因子列覆盖到的 ``dt=`` 分区；symbol 以 6 位代码对齐，
    因此不受 Qlib（sh600036）/QuantDB（600036.SH）口径差异影响。

    因子挖掘/历史补全等用户产出应写用户自定义数据集（``market="CUSTOM"``，
    即 ``QM_QUANTCUSTOM_DATA_DIR/6_ml_datasets/<source>``），不要写官方库。
    ``create_missing=True`` 时分区不存在会新建（仅含 symbol/date/因子列）。
    """
    if factor_df is None or factor_df.empty:
        return 0

    df = factor_df.copy()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index()
    if "instrument" in df.columns and "datetime" in df.columns:
        df = df.rename(columns={"instrument": "symbol", "datetime": "trade_date"})
    elif "symbol" not in df.columns or "trade_date" not in df.columns:
        if len(df.columns) >= 3:
            cols = list(df.columns)
            df = df.rename(columns={cols[0]: "symbol", cols[1]: "trade_date"})
    if "symbol" not in df.columns or "trade_date" not in df.columns:
        raise ValueError(f"无法识别因子 DataFrame 列: {df.columns.tolist()}")

    df["_dt"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y%m%d")
    df["_code6"] = df["symbol"].map(_code6)
    df = df.dropna(subset=["_dt", "_code6", feature_name])

    root = factor_source_dir(source, market)
    total = 0
    for dt_str, group in df.groupby("_dt"):
        part = root / f"dt={dt_str}" / "data.parquet"
        pair = group[["symbol", "_code6", feature_name]].drop_duplicates("_code6")
        if part.is_file():
            base = pd.read_parquet(part, engine="pyarrow")
            base["_code6"] = base["symbol"].map(_code6)
            base = base.drop(columns=[feature_name], errors="ignore")
            merged = base.merge(
                pair[["_code6", feature_name]], on="_code6", how="left"
            ).drop(columns=["_code6"])
        elif create_missing:
            part.parent.mkdir(parents=True, exist_ok=True)
            merged = pd.DataFrame(
                {
                    "symbol": pair["symbol"].map(
                        lambda v: StockCodeUtil.to_suffix(str(v))
                    ),
                    "date": pd.to_datetime(dt_str),
                    feature_name: pair[feature_name].to_numpy(),
                }
            ).drop_duplicates("symbol")
        else:
            logger.warning("分区不存在，跳过合并: %s", part)
            continue
        # 临时名不得以 .parquet 结尾：dt=*/*.parquet 通配符会把它当分区读，
        # DuckDB 撞上半写文件直接抛错（同上游 rd_mined 物化器的同名改名）
        tmp = part.parent / f".{part.name}.tmp"
        try:
            merged.to_parquet(tmp, index=False, engine="pyarrow")
            tmp.replace(part)
        finally:
            tmp.unlink(missing_ok=True)
        total += int(merged[feature_name].notna().sum())
    logger.info(
        "因子 %s 已合并回 QuantDB[%s] %s: %d 个非空值",
        feature_name,
        market,
        source,
        total,
    )
    return total
