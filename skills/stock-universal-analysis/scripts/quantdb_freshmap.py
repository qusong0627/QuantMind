#!/usr/bin/env python3
"""QuantDB 数据新鲜度地图（agent 自查工具）。

用法：
  quantdb_freshmap.py                 # 全数据集：文件数 + 最新文件日期
  quantdb_freshmap.py 601138.SH       # 指定代码：各数据集里该股最新数据到哪天
输出 JSON：{dataset: {files, latest_file, latest_dt, note}}；agent 据此判断
"这个库最新到哪天、能不能当今日数据用"。在 quantmind 容器内跑（duckdb）。
"""
import glob
import json
import os
import sys

CODE = sys.argv[1] if len(sys.argv) > 1 and "." in sys.argv[1] else ""
SYM = CODE.split(".")[0] if CODE else ""

ROOTS = (
    os.path.expanduser("~/projects/quantmind/data/quantdb"),
    "/data/quantdb",
    "/quantmind/data/quantdb",
)
DATA = next((p for p in ROOTS if os.path.isdir(p)), None)

DATASETS = [
    "1_kline_data/daily_forward", "1_kline_data/daily_backward",
    "1_kline_data/daily_unadjusted",
    "2_base_sector/instrument_detail", "2_base_sector/index_weights",
    "3_financial_data/balance", "3_financial_data/income",
    "3_financial_data/cashflow", "3_financial_data/dividend_factors",
    "3_financial_data/holder_num", "3_financial_data/capital",
    "5_technical_derived/valuation", "5_technical_derived/technical_indicators",
    "5_technical_derived/market_sentiment",
    "6_ml_datasets/features_daily", "6_ml_datasets/l1_factors", "6_ml_datasets/l2_factors",
]

# 各数据集的日期列候选（实测优先级）
DATE_COLS = ["trade_time", "trade_date", "end_date", "report_date", "date",
             "time", "dt", "TradingDate", "HqDate"]


def latest_dt_of_parquet(path: str) -> str | None:
    try:
        import duckdb

        cols = [r[0] for r in duckdb.connect().execute(
            f"SELECT column_name FROM parquet_schema('{path}')").fetchall()]
        # 列名来源 parquet_schema 字段名在 name 列
        cols = [r[1] for r in duckdb.connect().execute(
            f"SELECT file_name, name FROM parquet_schema('{path}')").fetchall()]
        col = next((c for c in DATE_COLS if c in cols), None)
        if not col:
            return None
        row = duckdb.connect().execute(
            f"SELECT max({col}) FROM read_parquet('{path}')").fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    out = {"data_root": DATA, "code": CODE or None, "datasets": {}}
    if not DATA:
        out["error"] = "未找到 quantdb 数据目录"
        print(json.dumps(out, ensure_ascii=False))
        return 1
    for ds in DATASETS:
        d = os.path.join(DATA, ds)
        if not os.path.isdir(d):
            continue
        files = sorted(glob.glob(f"{d}/**/*.parquet", recursive=True))
        if not files:
            continue
        info = {"files": len(files),
                "latest_file": os.path.basename(files[-1])}
        # Hive 分区目录 dt=YYYYMMDD（路径即新鲜度）
        import re as _re

        parts = sorted({m.group(1) for f2 in files
                        if (m := _re.search(r"dt=(\d{8})", f2))})
        if parts:
            info["latest_dt"] = parts[-1]
            if CODE:
                # 最近 3 个分区过滤 symbol 求该股最大日期
                import duckdb

                latest_sym = None
                pdirs = sorted(
                    {f2.rsplit("/", 1)[0] for f2 in files
                     if any(f"dt={p2}" in f2 for p2 in parts)},
                    reverse=True)[:3]
                for pdir in pdirs:
                    pfiles = sorted(glob.glob(f"{pdir}/*.parquet"))
                    for pf in pfiles:
                        try:
                            row = duckdb.connect().execute(
                                f"SELECT max(trade_time) FROM read_parquet('{pf}') "
                                f"WHERE symbol = '{CODE}'").fetchone()
                            if row and row[0]:
                                latest_sym = str(row[0])
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if latest_sym:
                        break
                info["symbol_latest_dt"] = latest_sym or "无数据"
        else:
            # 按股票分文件 / 合并表
            if CODE:
                mine = [f for f in files if SYM in os.path.basename(f)] or \
                       [f for f in files if f.endswith(f"{CODE}.parquet")]
                if mine:
                    info["latest_dt"] = latest_dt_of_parquet(mine[-1])
                    info["symbol_file"] = os.path.basename(mine[-1])
                else:
                    info["latest_dt"] = latest_dt_of_parquet(files[-1])
                    info["note"] = "无该代码独立文件"
            else:
                info["latest_dt"] = latest_dt_of_parquet(files[-1])
        out["datasets"][ds] = info
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())