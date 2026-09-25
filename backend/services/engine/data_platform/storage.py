"""
存储层：Parquet 写出 + Qlib bin 导出。

目录布局：
    /opt/quantmind/db/parquet/{source}/{market}/{field}/{year}/{symbol}.parquet

Qlib 导出（规范目录，容器内挂载路径；与 backend/shared/qlib_paths.py 口径一致）：
    /data/qlib/cn_data/features/{symbol}/{field}.day.bin
    /data/qlib/cn_data/calendars/day.txt
    /data/qlib/cn_data/instruments/all.txt

写出策略：
- Parquet 用 Zstd 压缩，单标的单年单文件，方便增量与并行。
- Qlib bin 由 dump_bin.py 或 qlib.contrib.data.dump 完成；本模块仅做 Parquet -> CSV
  中间层（dump_bin 期望 CSV）以及最终落盘到 cn_data 目录。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 默认路径，允许 ENV 覆盖
PARQUET_ROOT = Path(os.getenv("QM_PARQUET_ROOT", "/opt/quantmind/db/parquet"))
QLIB_BIN_ROOT = Path(os.getenv("QM_QLIB_BIN_ROOT", "/data/qlib/cn_data"))


class ParquetWriter:
    """单标的单年增量写出器。

    用法：
        writer = ParquetWriter()
        writer.write_daily(df, source="baostock", market="A")
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        compression: str = "zstd",
    ) -> None:
        self.root = Path(root) if root else PARQUET_ROOT
        self.compression = compression
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(
        self,
        *,
        source: str,
        market: str,
        field: str,
        year: int,
        symbol: str,
    ) -> Path:
        safe_symbol = symbol.replace("/", "_")
        return (
            self.root
            / source
            / market
            / field
            / str(year)
            / f"{safe_symbol}.parquet"
        )

    def write_daily(
        self,
        df: pd.DataFrame,
        *,
        source: str,
        market: str,
        field: str = "daily_kline",
    ) -> list[Path]:
        if df is None or df.empty:
            return []

        if "trade_date" not in df.columns or "symbol" not in df.columns:
            raise ValueError("DataFrame 需包含 symbol + trade_date 列")

        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["_year"] = pd.to_datetime(df["trade_date"]).dt.year

        written: list[Path] = []
        for (symbol, year), part in df.groupby(["symbol", "_year"]):
            path = self._path_for(
                source=source, market=market, field=field, year=int(year), symbol=symbol
            )
            path.parent.mkdir(parents=True, exist_ok=True)

            payload = part.drop(columns=["_year"])
            if path.exists():
                # 增量：合并并去重（按 trade_date 保留较新一行）
                try:
                    existing = pd.read_parquet(path)
                    payload = (
                        pd.concat([existing, payload], ignore_index=True)
                        .drop_duplicates(subset=["symbol", "trade_date"], keep="last")
                        .sort_values("trade_date")
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("read existing parquet failed (%s): %s", path, exc)

            payload.to_parquet(path, compression=self.compression, index=False)
            written.append(path)

        logger.info(
            "ParquetWriter wrote %d files for source=%s market=%s field=%s",
            len(written), source, market, field,
        )
        return written


class QlibBinExporter:
    """[DEPRECATED] 从 Parquet 中间层导出到 Qlib bin 目录。

    已被 QlibDataBuilder (从 QuantDB parquet 直接生成) 替代。
    保留仅用于向后兼容，新代码请使用 backend.services.engine.qlib_data_builder。

    简化实现：将 (symbol, field) -> Parquet 集合合并成 CSV，再交给
    qlib.contrib.data.dump.DumpDataAll 写出 bin。
    """

    def __init__(
        self,
        bin_root: Path | None = None,
        parquet_root: Path | None = None,
    ) -> None:
        self.bin_root = Path(bin_root) if bin_root else QLIB_BIN_ROOT
        self.parquet_root = Path(parquet_root) if parquet_root else PARQUET_ROOT
        self.bin_root.mkdir(parents=True, exist_ok=True)

    def collect_symbol(
        self,
        symbol: str,
        *,
        source: str,
        market: str,
        field: str = "daily_kline",
    ) -> pd.DataFrame:
        base = self.parquet_root / source / market / field
        if not base.exists():
            return pd.DataFrame()

        frames = []
        for year_dir in sorted(base.iterdir()):
            if not year_dir.is_dir():
                continue
            f = year_dir / f"{symbol}.parquet"
            if f.exists():
                frames.append(pd.read_parquet(f))
        if not frames:
            return pd.DataFrame()

        return (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["symbol", "trade_date"], keep="last")
            .sort_values("trade_date")
            .reset_index(drop=True)
        )

    def export_csv_for_dump(
        self,
        out_dir: Path,
        *,
        source: str,
        market: str,
        field: str = "daily_kline",
    ) -> int:
        """把所有 symbol 平铺成 CSV，供 dump_bin.py 使用。返回写出文件数。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        base = self.parquet_root / source / market / field
        if not base.exists():
            logger.warning("Parquet base not found: %s", base)
            return 0

        symbols: set[str] = set()
        for year_dir in base.iterdir():
            if not year_dir.is_dir():
                continue
            for f in year_dir.glob("*.parquet"):
                symbols.add(f.stem)

        n = 0
        for sym in sorted(symbols):
            df = self.collect_symbol(sym, source=source, market=market, field=field)
            if df.empty:
                continue
            df.to_csv(out_dir / f"{sym}.csv", index=False)
            n += 1
        logger.info("QlibBinExporter exported %d CSV files to %s", n, out_dir)
        return n
