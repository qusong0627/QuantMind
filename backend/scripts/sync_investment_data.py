#!/usr/bin/env python3
"""从 chenditc/investment_data GitHub 仓库同步最新 Qlib 数据

流程：
  1. 从 GitHub Releases 下载最新的 qlib_bin.tar.gz
  2. 解压并更新规范 Qlib 目录 data/qlib/cn_data/ (calendars, instruments, features)
  3. 用 Qlib API 读取 OHLCV 数据，写入 PostgreSQL stock_daily_latest
  4. 可选：用 Qlib API 生成 parquet 快照到 db/feature_snapshots/

用法：
  # 同步最新版本
  python backend/scripts/sync_investment_data.py

  # 同步指定版本
  python backend/scripts/sync_investment_data.py --version 2026-05-22

  # 仅更新 Qlib 二进制数据，不写数据库
  python backend/scripts/sync_investment_data.py --skip-db

  # 跳过 Qlib 数据更新（仅从 Qlib 读取写入 PG）
  python backend/scripts/sync_investment_data.py --skip-qlib
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, date
from pathlib import Path
from typing import Any

import asyncpg
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# ── 配置 ──────────────────────────────────────────────────────────────────────
# A 股 Qlib 规范目录（与 qlib_paths / QlibDataBuilder / full-deploy 口径一致）：
# 容器内为 /data/qlib/cn_data，宿主机直跑回落 PROJECT_ROOT/data/qlib/cn_data。
# 禁止再写历史遗留的 db/qlib_data，避免「夜间同步写 A 目录、回测读 B 目录」。
QLIB_DATA_DIR = (
    Path("/data/qlib/cn_data")
    if Path("/data").is_dir()
    else PROJECT_ROOT / "data" / "qlib" / "cn_data"
)
FEATURE_SNAPSHOTS_DIR = PROJECT_ROOT / "db" / "feature_snapshots"
DOWNLOAD_DIR = PROJECT_ROOT / "tmp" / "investment_data"
REPO = "chenditc/investment_data"
ASSET_NAME = "qlib_bin.tar.gz"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "quantmind")
DB_USER = os.getenv("DB_USER", "quantmind")
DB_PASS = os.getenv("DB_PASSWORD", "quantmind2026")


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def _get_latest_version() -> str:
    """获取 GitHub 最新 release tag（不依赖 API，用 HEAD 请求）"""
    try:
        req = urllib.request.Request(
            f"https://github.com/{REPO}/releases/latest",
            method="HEAD",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            location = resp.url
            # URL 形如 .../releases/tag/2026-05-22
            tag = location.rstrip("/").rsplit("/", 1)[-1]
            return tag
    except Exception as e:
        raise RuntimeError(f"无法获取最新版本: {e}")


def _download_asset(version: str, target: Path) -> None:
    """从 GitHub Releases 下载指定版本的资产"""
    url = f"https://github.com/{REPO}/releases/download/{version}/{ASSET_NAME}"
    _log(f"Downloading {ASSET_NAME} from {url} ...")
    target.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = resp.length or 0
        downloaded = 0
        with open(target, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded / total * 100
                    mb = downloaded / (1024 * 1024)
                    total_mb = total / (1024 * 1024)
                    print(f"\r  {pct:.0f}% ({mb:.0f}MB / {total_mb:.0f}MB)", end="", flush=True)
    print()
    _log(f"Downloaded to {target} ({target.stat().st_size / 1024 / 1024:.0f}MB)")


def _extract_qlib_data(archive: Path) -> None:
    """解压 qlib_bin.tar.gz 到规范 Qlib 目录并同步到 investment_data 目录"""
    _log(f"Extracting {archive} to {QLIB_DATA_DIR} ...")
    tmp_dir = DOWNLOAD_DIR / "extract"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    cmd = ["tar", "-xzf", str(archive), "-C", str(tmp_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"解压失败: {proc.stderr or proc.stdout}")

    # 找到解压后的根目录（通常是单层目录 qlib_bin/ 或直接是 calendars/ features/）
    extracted_root = tmp_dir
    subdirs = [d for d in tmp_dir.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        # 单目录包装，进入内部
        candidate = subdirs[0]
        if (candidate / "calendars").exists() or (candidate / "features").exists():
            extracted_root = candidate

    # 同步各子目录到 QLIB_DATA_DIR
    for subdir_name in ("calendars", "instruments", "features"):
        src = extracted_root / subdir_name
        dst = QLIB_DATA_DIR / subdir_name
        if src.exists():
            _log(f"Syncing {subdir_name}/ ({sum(1 for _ in src.rglob('*'))} files)")
            dst.mkdir(parents=True, exist_ok=True)
            for file in src.rglob("*"):
                if file.is_file():
                    rel = file.relative_to(src)
                    out = dst / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(file), str(out))

    # 同步到 investment_data/qlib_bin/（确保 qlib_bin 路径也保持最新）
    inv_qlib = Path(os.getenv("QM_INVESTMENT_DATA_DIR", "/data/third_party/investment_data")) / "qlib_bin"
    if inv_qlib != QLIB_DATA_DIR:
        for subdir_name in ("calendars", "instruments", "features"):
            src = extracted_root / subdir_name
            dst = inv_qlib / subdir_name
            if src.exists():
                dst.mkdir(parents=True, exist_ok=True)
                for file in src.rglob("*"):
                    if file.is_file():
                        rel = file.relative_to(src)
                        out = dst / rel
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(file), str(out))
        _log(f"Investment data synced to {inv_qlib}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    _log(f"Qlib data updated to {QLIB_DATA_DIR}")

    # 清除 daily_data_sync 的 qlib root 缓存，确保后续读取使用最新路径
    try:
        import backend.scripts.daily_data_sync as dds
        dds._QLIB_ROOT_CACHE = None
        dds._CALENDAR_CACHE = None
    except Exception:
        pass


def _read_qlib_calendar() -> list[date]:
    """读取交易日历"""
    cal_path = QLIB_DATA_DIR / "calendars" / "day.txt"
    if not cal_path.exists():
        return []
    dates = []
    for line in cal_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                dates.append(date.fromisoformat(line))
            except ValueError:
                continue
    return dates


def _read_qlib_instruments() -> list[str]:
    """读取所有股票代码，过滤掉指数（qlib instruments 把指数混入了个股列表）。

    指数代码：000300/000852/000905/000906（沪）/ 399300（深）/ 880xxx（申万行业）。
    这些若写进 stock_daily_latest 会污染横截面排序（return_5d 等极端值）。
    """
    inst_path = QLIB_DATA_DIR / "instruments" / "all.txt"
    if not inst_path.exists():
        return []
    # 指数/行业代码前缀（小写比较）
    index_prefixes = ("sh000300", "sh000852", "sh000905", "sh000906",
                      "sz399300", "sz000852", "sz000905", "sz000906",
                      "sh880", "sz880")  # 申万行业指数
    symbols = set()
    for line in inst_path.read_text().splitlines():
        parts = line.strip().split("\t")
        if parts:
            sym = parts[0].lower()
            if sym in index_prefixes:
                continue
            symbols.add(sym)
    return sorted(symbols)


_QLIB_INITIALIZED = False


def _init_qlib_once() -> None:
    """初始化 Qlib（只调用一次）"""
    global _QLIB_INITIALIZED
    if _QLIB_INITIALIZED:
        return
    try:
        import qlib
        qlib.init(provider_uri=str(QLIB_DATA_DIR), region="cn")
        _QLIB_INITIALIZED = True
        _log(f"Qlib initialized: {QLIB_DATA_DIR}")
    except Exception as e:
        _log(f"Qlib init warning: {e}")


def _read_qlib_ohlcv_for_symbol(symbol: str, start_time: str | None = None, end_time: str | None = None) -> pd.DataFrame | None:
    """用 Qlib API 读取单只股票的 OHLCV 数据。

    start_time/end_time: 可选，只读该日期范围（含）的数据，用于增量补缺口或限定范围。
    """
    _init_qlib_once()
    try:
        from qlib.data import D

        # 读取 OHLCV
        df = D.features(
            [symbol],
            ["$open", "$high", "$low", "$close", "$volume", "$factor"],
            start_time=start_time,
            end_time=end_time,
        )
        if df is None or df.empty:
            return None

        df = df.reset_index()
        # reset_index 后列顺序是 [instrument, datetime, $open, ...]（qlib MultiIndex 顺序）
        df.columns = ["symbol", "datetime", "open", "high", "low", "close", "volume", "factor"]
        df["trade_date"] = pd.to_datetime(df["datetime"]).dt.date
        df["symbol"] = df["symbol"].str.upper()

        # 复权价处理
        df["adj_factor"] = df["factor"].astype(float)
        df = df.drop(columns=["datetime", "factor"])

        return df
    except Exception as e:
        _log(f"  Warning: failed to read Qlib data for {symbol}: {e}")
        return None


async def _upsert_ohlcv_to_pg(dfs: list[pd.DataFrame], batch_size: int = 500) -> int:
    """将 OHLCV 数据批量写入 stock_daily_latest"""
    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )
    try:
        # 获取目标表列
        rows = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='stock_daily_latest'
            ORDER BY ordinal_position
        """)
        table_cols = {r["column_name"] for r in rows}

        # 合并所有 DataFrame
        combined = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]
        combined = combined.drop_duplicates(subset=["trade_date", "symbol"])

        # 只保留表中存在的列
        use_cols = [c for c in ["trade_date", "symbol", "open", "high", "low", "close", "volume", "adj_factor"]
                    if c in table_cols and c in combined.columns]
        data = combined[use_cols].fillna(0)

        # 替换无穷值
        data = data.replace([float("inf"), float("-inf")], 0)

        records = [tuple(row) for row in data.itertuples(index=False, name=None)]
        if not records:
            _log("No new records to insert")
            return 0

        _log(f"Upserting {len(records):,} records to stock_daily_latest...")

        # 临时表 ON COMMIT DROP 需要显式事务包裹，否则每个 execute 自动提交后临时表即丢失
        async with conn.transaction():
            # 创建临时表
            await conn.execute(f"""
                CREATE TEMP TABLE tmp_ohlcv_import ON COMMIT DROP
                AS SELECT * FROM stock_daily_latest WITH NO DATA
            """)
            await conn.copy_records_to_table("tmp_ohlcv_import", records=records, columns=use_cols)

            # UPSERT
            non_pk = [c for c in use_cols if c not in ("trade_date", "symbol")]
            if non_pk:
                update_set = ", ".join([f"{c}=EXCLUDED.{c}" for c in non_pk])
                sql = (
                    f"INSERT INTO stock_daily_latest ({', '.join(use_cols)}) "
                    f"SELECT {', '.join(use_cols)} FROM tmp_ohlcv_import "
                    f"ON CONFLICT (trade_date, symbol) DO UPDATE SET {update_set}"
                )
            else:
                sql = (
                    f"INSERT INTO stock_daily_latest ({', '.join(use_cols)}) "
                    f"SELECT {', '.join(use_cols)} FROM tmp_ohlcv_import "
                    "ON CONFLICT (trade_date, symbol) DO NOTHING"
                )
            await conn.execute(sql)

        _log(f"Upserted {len(records):,} records successfully")
        return len(records)
    finally:
        await conn.close()


def _generate_parquet_snapshots(symbols: list[str], years: list[int] | None = None) -> int:
    """用 Qlib API 按年生成 parquet 快照"""
    if years is None:
        years = list(range(2016, date.today().year + 2))

    _init_qlib_once()
    try:
        from qlib.data import D
    except Exception as e:
        _log(f"Failed to initialize Qlib: {e}")
        return 0

    FEATURE_COLS = ["$open", "$high", "$low", "$close", "$volume", "$factor", "$amount", "$pctchange"]
    total_records = 0

    for year in years:
        _log(f"Generating parquet for {year}...")
        try:
            start_dt = f"{year}-01-01"
            end_dt = f"{year}-12-31"

            df = D.features(symbols, FEATURE_COLS, start_time=start_dt, end_time=end_dt)
            if df is None or df.empty:
                continue

            df = df.reset_index()
            df.columns = ["symbol", "datetime"] + [c.lstrip("$") for c in FEATURE_COLS]
            df["trade_date"] = pd.to_datetime(df["datetime"]).dt.date
            df["symbol"] = df["symbol"].str.upper()
            df["adj_factor"] = df["factor"].astype(float)
            df = df.drop(columns=["datetime", "factor"])

            out_path = FEATURE_SNAPSHOTS_DIR / f"model_features_{year}.parquet"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(str(out_path), index=False)
            total_records += len(df)
            _log(f"  {year}: {len(df):,} records -> {out_path}")

        except Exception as e:
            _log(f"  Failed to generate parquet for {year}: {e}")

    return total_records


def main() -> int:
    parser = argparse.ArgumentParser(description="从 chenditc/investment_data 同步最新 Qlib 数据")
    parser.add_argument("--version", default="", help="指定版本号 (默认最新版)")
    parser.add_argument("--skip-db", action="store_true", help="跳过数据库写入")
    parser.add_argument("--skip-qlib", action="store_true", help="跳过 Qlib 数据更新（仅从现有数据读取写入 PG）")
    parser.add_argument("--skip-parquet", action="store_true", help="跳过 parquet 快照生成")
    parser.add_argument("--symbols", default="", help="指定股票代码列表（逗号分隔，默认为全部）")
    parser.add_argument("--years", default="", help="指定年份范围（逗号分隔，用于 parquet 生成）")
    parser.add_argument("--db-start", default="", help="只写该日期（含）之后的 PG 数据，用于增量补缺口（如 2026-06-23）")
    parser.add_argument("--db-end", default="", help="只写该日期（含）之前的 PG 数据，用于限定范围（如 2026-06-26）")
    args = parser.parse_args()

    _log("=" * 60)
    _log("chenditc/investment_data 数据同步")
    _log("=" * 60)

    # 1. 确定版本
    version = args.version.strip()
    if not version:
        version = _get_latest_version()
    _log(f"Target version: {version}")

    # 2. 下载
    archive_path = DOWNLOAD_DIR / version / ASSET_NAME
    if not archive_path.exists():
        _log(f"Downloading version {version}...")
        _download_asset(version, archive_path)
    else:
        _log(f"Archive already exists: {archive_path}")

    # 3. 解压更新 Qlib 数据
    if not args.skip_qlib:
        _log("\n--- Step 1: Update Qlib binary data ---")
        _extract_qlib_data(archive_path)

    # 4. 读取股票代码
    symbols = []
    if args.symbols.strip():
        symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _read_qlib_instruments()
        _log(f"\n--- Step 2: Read {len(symbols)} symbols from instruments ---")

    calendar = _read_qlib_calendar()
    if calendar:
        _log(f"Calendar: {calendar[0]} to {calendar[-1]} ({len(calendar)} trading days)")

    # 5. 写入数据库
    if not args.skip_db and symbols:
        _log("\n--- Step 3: Read OHLCV from Qlib and write to PostgreSQL ---")

        # 分批读取并写入，避免内存溢出
        batch_dfs = []
        total_symbols = len(symbols)
        inserted = 0

        for i, sym in enumerate(symbols):
            df = _read_qlib_ohlcv_for_symbol(sym, start_time=args.db_start or None, end_time=args.db_end or None)
            if df is not None and not df.empty:
                batch_dfs.append(df)

            # 每 200 只股票批量写入一次
            if len(batch_dfs) >= 200 or i == total_symbols - 1:
                if batch_dfs:
                    inserted += asyncio.run(_upsert_ohlcv_to_pg(batch_dfs))
                    batch_dfs = []

            if (i + 1) % 1000 == 0:
                _log(f"  Progress: {i + 1}/{total_symbols} symbols processed")

        _log(f"Total records upserted: {inserted:,}")

    # 6. 生成 parquet 快照
    if not args.skip_parquet and symbols:
        _log("\n--- Step 4: Generate parquet snapshots ---")
        years = []
        if args.years.strip():
            years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
        else:
            years = None

        total_records = _generate_parquet_snapshots(symbols, years)
        _log(f"Total records in parquet: {total_records:,}")

    # 7. 完成
    _log("\n" + "=" * 60)
    _log("Sync completed!")
    _log("=" * 60)

    # 打印最终状态
    if calendar:
        _log(f"Qlib calendar: {calendar[0]} to {calendar[-1]}")
    _log(f"Qlib symbols: {len(symbols)}")
    _log(f"Qlib features: {sum(1 for _ in (QLIB_DATA_DIR / 'features').rglob('*')) if (QLIB_DATA_DIR / 'features').exists() else 0} files")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
