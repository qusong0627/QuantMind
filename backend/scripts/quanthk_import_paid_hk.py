#!/usr/bin/env python3
"""付费 HK 历史日线 → QuantHK 导入脚本。

从付费数据目录（GBK CSV，每只股票一个文件）读取港股全市场历史日线，
按 QuantDB 的 Hive 分区格式落盘到 quanthk 本地 parquet，作为 yahoo 日线的
历史补充（替换旧的 stock.duckdb 数据）。

数据源结构（预测者网格式，CSV GBK 编码）:
  股票代码(hk00001), 交易日期, 开盘价, 最高价, 最低价, 收盘价,
  昨收盘, 涨跌幅, 成交量, 成交额

落盘格式:
  {quanthk}/1_kline_data/daily_forward/dt=YYYYMMDD/data.parquet
  stock_code 5位（00001）→ 4位+.HK（0001.HK），与 quanthk 现有对齐。
  存原始价（不复权），与现有 daily_forward 口径一致。

用法:
  python backend/scripts/quanthk_import_paid_hk.py --dir /path/to/hk_data
  python backend/scripts/quanthk_import_paid_hk.py --dir ... --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.kline_canonical import canonicalize  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quanthk_import_paid_hk")

QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk"))
)
REL_DIR = "1_kline_data/daily_forward"

# 输出列（与 quanthk 日线对齐）
OUT_COLS = [
    "symbol", "time", "open", "high", "low", "close",
    "volume", "amount", "release_id", "published_at",
]

# 源 CSV 列（GBK 编码中文列名）
SRC_COLS = {
    "股票代码": "raw_code",
    "交易日期": "time",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "成交额": "amount",
}

DEFAULT_SRC_DIR = "/media/zbox/sata/A_H/hk_data_20260508/hk_data"


def _quanthk_root() -> Path:
    env_val = os.getenv("QM_QUANTHK_DATA_DIR", "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if Path("/data/quanthk").is_dir():
        return Path("/data/quanthk")
    QUANTHK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return QUANTHK_DATA_DIR


def _to_qhk_symbol(raw_code: str) -> str:
    """hk00001 → 0001.HK，00001 → 0001.HK。"""
    code = raw_code.strip()
    if code.lower().startswith("hk"):
        code = code[2:]
    code = code.zfill(5)
    if code.startswith("0") and len(code) == 5:
        code = code[1:]
    return f"{code}.HK"


def _load_stock_files(src_dir: Path) -> list[Path]:
    """列出所有股票 CSV 文件。"""
    files = sorted(src_dir.glob("*.csv"))
    return [f for f in files if f.name[:5].isdigit()]


def _read_stock_csv(path: Path) -> pd.DataFrame:
    """读单个股票 CSV（GBK 编码），标准化。"""
    # 尝试多种编码
    df = None
    for enc in ("gbk", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.rename(columns={k: v for k, v in SRC_COLS.items() if k in df.columns})
    # 从文件名取代码（00001.csv），文件可能含中文名，用文件名前5位
    code_from_file = path.stem[:5]
    df["raw_code"] = code_from_file
    df["symbol"] = df["raw_code"].map(_to_qhk_symbol)

    # 时间
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    df["time"] = df["time"].dt.date

    # 数值转换
    for c in ("open", "high", "low", "close", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    else:
        df["volume"] = 0

    # 去重（同一天可能重复）
    df = df.drop_duplicates(subset=["symbol", "time"], keep="last")

    # 缺失列补 0
    for c in ("open", "high", "low", "close", "amount"):
        if c not in df.columns:
            df[c] = 0.0

    df["release_id"] = "paid_hk"
    df["published_at"] = datetime.now().isoformat(timespec="seconds")
    df = df[OUT_COLS].dropna(subset=["close"])
    return df


def _write_partition(root: Path, date_str: str, chunk: pd.DataFrame) -> None:
    """写单个 Hive 分区 dt=YYYYMMDD/data.parquet。"""
    dt_dir = root / f"dt={date_str}"
    dt_dir.mkdir(parents=True, exist_ok=True)
    target = dt_dir / "data.parquet"
    if target.exists():
        old = pd.read_parquet(target)
        combined = pd.concat([old, chunk], ignore_index=True)
        # 一个股票日只留一行：paid_hk 历史优先于 akshare（见 kline_canonical）
        combined = canonicalize(combined)
        combined.to_parquet(target, index=False)
    else:
        chunk.to_parquet(target, index=False)


def import_paid_hk(src_dir: str | None = None, *, dry_run: bool = False) -> dict:
    """全量导入付费 HK 历史日线到 quanthk。"""
    src = Path(src_dir or os.getenv("QM_PAID_HK_DATA_DIR", DEFAULT_SRC_DIR))
    if not src.is_dir():
        raise FileNotFoundError(f"数据目录不存在: {src}")

    root = _quanthk_root()
    target_dir = root / REL_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    files = _load_stock_files(src)
    log.info("待导入股票文件: %d 个 (%s)", len(files), src)

    if dry_run:
        # 预览一个文件的规模
        sample = _read_stock_csv(files[0])
        return {
            "dir": str(src),
            "stocks": len(files),
            "dry_run": True,
            "sample_rows": len(sample),
            "target_dir": str(target_dir),
        }

    # 分批读取，避免内存爆
    all_chunks: list[pd.DataFrame] = []
    total_rows = 0
    errors = 0
    for i, f in enumerate(files):
        try:
            df = _read_stock_csv(f)
            if not df.empty:
                all_chunks.append(df)
                total_rows += len(df)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 %s 失败: %s", f.name, exc)
            errors += 1
        if (i + 1) % 500 == 0:
            log.info("  进度 %d/%d，累计 %d 行", i + 1, len(files), total_rows)

    if not all_chunks:
        return {"dir": str(src), "stocks": len(files), "rows": 0, "errors": errors}

    all_df = pd.concat(all_chunks, ignore_index=True)
    log.info("读取完成: %d 行，按交易日分区写入...", len(all_df))

    # 按交易日分组落盘
    grouped = {ts.strftime("%Y%m%d"): g for ts, g in all_df.groupby(all_df["time"])}
    written = 0
    for date_str, chunk in sorted(grouped.items()):
        _write_partition(target_dir, date_str, chunk)
        written += 1

    return {
        "dir": str(src),
        "stocks": len(files),
        "rows": int(len(all_df)),
        "partitions": written,
        "start_date": min(grouped),
        "end_date": max(grouped),
        "errors": errors,
        "target_dir": str(target_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="付费HK历史日线 → QuantHK")
    parser.add_argument("--dir", default=None, help="数据目录（默认 /media/zbox/sata/A_H/hk_data_20260508/hk_data）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写盘")
    args = parser.parse_args()

    try:
        result = import_paid_hk(args.dir, dry_run=args.dry_run)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("导入失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
