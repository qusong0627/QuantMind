#!/usr/bin/env python3
"""港股 stock_daily_latest_hk 每日同步：quanthk parquet → PG 最新交易日快照。

个股预测/研究服务的市场专属表（research_service._get_sdl_table HK → stock_daily_latest_hk）
一直缺失导致 HK 单股推理报「relation stock_daily_latest_hk does not exist」。
本脚本建表并灌入最新交易日全量（OHLCV + L1 因子字段 + 中文名/行业）。

用法:
  python backend/scripts/quanthk_sdl_sync.py            # 灌最新交易日
  python backend/scripts/quanthk_sdl_sync.py --date 20260831
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("quanthk_sdl_sync")

from backend.services.engine.data_platform.quanthk_hub import _resolve_quanthk_data_dir

import psycopg2
import psycopg2.extras


def _pg():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", os.getenv("DB_HOST", "db")),
        port=int(os.getenv("POSTGRES_PORT", os.getenv("DB_PORT", "5432"))),
        user=os.getenv("POSTGRES_USER", os.getenv("DB_USER", "quantmind")),
        password=os.getenv("POSTGRES_PASSWORD", os.getenv("DB_PASSWORD", "quantmind2026")),
        dbname=os.getenv("POSTGRES_DB", os.getenv("DB_NAME", "quantmind")),
    )

# 与 CN stock_daily_latest 对齐的列（HK 缺失列留 NULL；额外 name 列供 research_service 非 CN 分支）
_TABLE = "stock_daily_latest_hk"
_COLUMNS = """trade_date date, symbol varchar(32), open double precision, high double precision,
low double precision, close double precision, volume double precision, amount double precision,
adj_factor double precision, stock_name varchar(128), industry varchar(128), pe_ttm double precision,
pb double precision, roe double precision, total_mv double precision, float_mv double precision,
turnover_rate double precision, pct_change double precision, is_st boolean, ma5 double precision,
ma10 double precision, ma20 double precision, ma60 double precision, ma_gap_5 double precision,
ma_gap_10 double precision, ma_gap_20 double precision, return_1d double precision,
return_3d double precision, return_5d double precision, return_10d double precision,
return_20d double precision, return_60d double precision, vol_std_5 double precision,
vol_std_20 double precision, vol_std_60 double precision, vol_atr_14 double precision,
rsi_14 double precision, rsi_6 double precision, macd_hist double precision,
volume_ratio_5 double precision, volume_ratio_20 double precision, main_flow double precision,
flow_net_amount double precision, listed_days integer, bp double precision, ep_ttm double precision,
ln_mv_total double precision, kdj_k double precision, beta_20 double precision,
volume_ma_5 double precision, amount_ma_5 double precision, listing_market varchar(16),
name varchar(128)"""


def _ensure_table() -> None:
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} ({_COLUMNS})")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_sym_date ON {_TABLE}(symbol, trade_date)")
            conn.commit()
    finally:
        conn.close()


def _build_day(df_day: str) -> pd.DataFrame:
    qdir = _resolve_quanthk_data_dir()
    con = duckdb.connect()
    try:
        k = con.execute(
            "SELECT symbol, time, open, high, low, close, volume, amount FROM read_parquet("
            f"'{qdir}/1_kline_data/daily_forward/dt={df_day}/data.parquet')"
        ).fetchdf()
        if k.empty:
            raise SystemExit(f"{df_day} 无日线分区")
        k = k.rename(columns={"time": "trade_date"})
        sym_in = "(" + ",".join(f"'{s}'" for s in k["symbol"].tolist()) + ")"
        l1 = con.execute(
            f"SELECT symbol, turnover_rate, pctchange AS pct_change, return_1d, return_5d, return_20d, "
            f"ma5, ma20, ma60, rsi_14, rsi_6, macd_hist, kdj_k, beta_20, vol_std_5, vol_std_20, vol_std_60, "
            f"volume_ratio_5, volume_ratio_20, volume_ma_5, amount_ma_5, flow_net_amount, pe_ttm, pb, roe "
            f"FROM read_parquet('{qdir}/6_ml_datasets/l1_factors/dt={df_day}/data.parquet') "
            f"WHERE symbol IN {sym_in}"
        ).fetchdf()
        df = k.merge(l1, on="symbol", how="left")
        prof = con.execute(
            "SELECT symbol, 所属行业 FROM read_parquet("
            f"'{qdir}/2_base_sector/akshare_profile/*.parquet', union_by_name=true)"
        ).fetchdf()
        prof = prof.rename(columns={"所属行业": "industry"})
        df = df.merge(prof, on="symbol", how="left")
        names = con.execute(
            "SELECT symbol, cn_name FROM read_parquet("
            f"'{qdir}/2_base_sector/security_master/data.parquet')"
        ).fetchdf()
        df = df.merge(names.rename(columns={"cn_name": "cn_name"}), on="symbol", how="left")
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["adj_factor"] = 1.0
        df["is_st"] = False
        df["volume"] = df["volume"].fillna(0.0)
        df["symbol"] = df["symbol"].astype(str)
        return df
    finally:
        con.close()


def sync_day(date_str: str | None = None) -> dict:
    _ensure_table()
    if date_str is None:
        qdir = _resolve_quanthk_data_dir()
        parts = sorted(
            p.name[3:] for p in (qdir / "1_kline_data" / "daily_forward").glob("dt=*")
        )
        if not parts:
            raise SystemExit("无日线分区")
        date_str = parts[-1]
    # 拉近 90 个交易日（K 线展示需要窗口，仅 1 天前端画不出曲线）
    qdir = _resolve_quanthk_data_dir()
    parts = sorted(p.name[3:] for p in (qdir / "1_kline_data" / "daily_forward").glob("dt=*"))
    parts = [d for d in parts if d <= date_str][-90:]
    if not parts:
        raise SystemExit("无日线分区")
    frames = [_build_day(d) for d in parts]
    df = pd.concat(frames, ignore_index=True)
    cols = [
        "trade_date", "symbol", "open", "high", "low", "close", "volume", "amount",
        "adj_factor", "stock_name", "industry", "pe_ttm", "pb", "roe", "turnover_rate",
        "pct_change", "is_st", "ma5", "ma20", "ma60", "return_1d", "return_5d",
        "return_20d", "vol_std_5", "vol_std_20", "vol_std_60", "rsi_14", "rsi_6",
        "macd_hist", "volume_ratio_5", "volume_ratio_20", "flow_net_amount", "kdj_k",
        "beta_20", "volume_ma_5", "amount_ma_5", "name",
    ]
    df["stock_name"] = df["cn_name"]
    df["name"] = df["cn_name"]
    keep = [c for c in cols if c in df.columns]
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE}")  # 全量重灌近 90 交易日快照
            rows = [
                tuple(None if pd.isna(v) and not isinstance(v, bool) else v for v in r)
                for r in df[keep].itertuples(index=False, name=None)
            ]
            cols_sql = ",".join(keep)
            placeholders = ",".join(["%s"] * len(keep))
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {_TABLE} ({cols_sql}) VALUES %s",
                rows,
                page_size=2000,
            )
            conn.commit()
    finally:
        conn.close()
    cnt = len(df)
    log.info("stock_daily_latest_hk %s 灌入 %d 只", date_str, cnt)
    return {"date": date_str, "rows": cnt}


def main() -> int:
    parser = argparse.ArgumentParser(description="港股 stock_daily_latest_hk 每日同步")
    parser.add_argument("--date", type=str, default=None, help="YYYYMMDD，缺省最新交易日")
    args = parser.parse_args()
    result = sync_day(args.date)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
