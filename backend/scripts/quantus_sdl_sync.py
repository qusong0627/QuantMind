#!/usr/bin/env python3
"""美股 stock_daily_latest_us 每日同步：quantus parquet → PG 最新交易日快照。

个股预测/研究服务的市场专属表（research_service._get_sdl_table US → stock_daily_latest_us）
一直缺失，导致 US 单股推理第一步就报「relation stock_daily_latest_us does not exist」。
本脚本建表并灌入最近 90 个交易日全量（与 stock_daily_latest_hk 同列集，共 53 列）。

用法:
  python backend/scripts/quantus_sdl_sync.py            # 灌到最新交易日
  python backend/scripts/quantus_sdl_sync.py --date 20260910

口径（务必看，与 CN/HK 不同处）:
- 价格：daily_forward 为**未复权原始价**（QuantUS 无复权因子，l1_factors.adj_factor 恒 1.0），
  故 adj_factor 列恒写 1.0，close 即真实成交价，下游 _to_nominal_price 不做二次还原。
- amount：**美元原始成交额**（≈ close×volume 量级），不是 A 股/港股的「万元」。
- vol_std_5/20/60：**小数**（0.0147 = 1.47%），CN/HK 聚合表为百分数（2.78 = 2.78%）；
  research_service.predict_single_stock 按市场分派这两套口径。
- 财务列：QuantUS 的 l1_factors 里 pe_ttm/pb/roe/bp/ep_ttm/float_mv/total_mv/turnover_rate
  实测恒 0，不落假 0；改用 f10 快照的 pe_ratio/pb_ratio/market_cap（**仅最新交易日行**填充，
  历史行留 NULL，避免把「今天的估值」回填到历史行造成前视）。
- roe/bp/ep_ttm/float_mv/turnover_rate/main_flow/listed_days/ma10/ma_gap_10/return_60d
  之外美股缺的列一律 NULL，见 _MISSING_COLUMNS。
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
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quantus_sdl_sync")

from backend.services.engine.data_platform.quantus_hub import _resolve_quantus_data_dir

import psycopg2
import psycopg2.extras

# 与 stock_daily_latest_hk 完全同列集：下游 SQL（research_service._load_sdl_pg_map /
# stock_query_app / ai_strategy）按这套列名写死，列集必须对齐。
_TABLE = "stock_daily_latest_us"
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

# 落库窗口（交易日）：K 线/分数曲线要画窗口，只灌最新 1 天前端画不出曲线
_WINDOW_TRADING_DAYS = 90

# 快照列（写入顺序），与 _COLUMNS 列集一致
_SNAPSHOT_COLUMNS = [
    "trade_date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "stock_name",
    "industry",
    "pe_ttm",
    "pb",
    "roe",
    "total_mv",
    "float_mv",
    "turnover_rate",
    "pct_change",
    "is_st",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma_gap_5",
    "ma_gap_10",
    "ma_gap_20",
    "return_1d",
    "return_3d",
    "return_5d",
    "return_10d",
    "return_20d",
    "return_60d",
    "vol_std_5",
    "vol_std_20",
    "vol_std_60",
    "vol_atr_14",
    "rsi_14",
    "rsi_6",
    "macd_hist",
    "volume_ratio_5",
    "volume_ratio_20",
    "main_flow",
    "flow_net_amount",
    "listed_days",
    "bp",
    "ep_ttm",
    "ln_mv_total",
    "kdj_k",
    "beta_20",
    "volume_ma_5",
    "amount_ma_5",
    "listing_market",
    "name",
]

# 美股数据源里没有的列（落 NULL，不伪造）：恒 0 的假值比空值更容易骗过下游筛选。
_MISSING_COLUMNS = (
    "roe(pg无净资产收益率)",
    "float_mv/total_mv(改由 f10.market_cap 填 total_mv)",
    "turnover_rate",
    "main_flow",
    "listed_days",
    "ma10/ma_gap_10",
    "bp/ep_ttm(l1 恒 0)",
)


def _pg():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", os.getenv("DB_HOST", "db")),
        port=int(os.getenv("POSTGRES_PORT", os.getenv("DB_PORT", "5432"))),
        user=os.getenv("POSTGRES_USER", os.getenv("DB_USER", "quantmind")),
        password=os.getenv(
            "POSTGRES_PASSWORD", os.getenv("DB_PASSWORD", "quantmind2026")
        ),
        dbname=os.getenv("POSTGRES_DB", os.getenv("DB_NAME", "quantmind")),
    )


def _ensure_table() -> None:
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} ({_COLUMNS})")
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_sym_date ON {_TABLE}(symbol, trade_date)"
            )
            conn.commit()
    finally:
        conn.close()


def _trading_days(qdir: Path, end: str | None) -> list[str]:
    """daily_forward 已有分区（≤ end 的最近 _WINDOW_TRADING_DAYS 个），升序。"""
    parts = sorted(
        p.name[3:] for p in (qdir / "1_kline_data" / "daily_forward").glob("dt=*")
    )
    if end:
        parts = [d for d in parts if d <= end]
    return parts[-_WINDOW_TRADING_DAYS:]


def _read_static(con: duckdb.DuckDBPyConnection, qdir: Path) -> pd.DataFrame:
    """静态元数据（名称/行业/估值快照）。f10 估值仅对最新交易日行生效（见模块注释）。"""
    names = con.execute(
        "SELECT symbol, cn_name FROM read_parquet("
        f"'{qdir}/2_base_sector/security_master/*.parquet', union_by_name=true)"
    ).fetchdf()
    names["stock_name"] = names["cn_name"].fillna("").astype(str).str.strip()
    # 同代码多行（实测 SBNY：空名行在前、退市静态名在后），空名排序在前、非空胜出
    names = names.sort_values(["symbol", "stock_name"]).drop_duplicates(
        "symbol", keep="last"
    )
    names = names[["symbol", "stock_name"]]

    sector = con.execute(
        "SELECT symbol, industry FROM read_parquet("
        f"'{qdir}/2_base_sector/sector/*.parquet', union_by_name=true)"
    ).fetchdf()
    sector["industry"] = sector["industry"].fillna("").astype(str)
    sector = sector.drop_duplicates("symbol", keep="last")[["symbol", "industry"]]

    f10 = con.execute(
        "SELECT symbol, pe_ratio, pb_ratio, market_cap FROM read_parquet("
        f"'{qdir}/2_base_sector/f10/*.parquet', union_by_name=true)"
    ).fetchdf()
    f10 = f10.drop_duplicates("symbol", keep="last")

    meta = names.merge(sector, on="symbol", how="left").merge(
        f10, on="symbol", how="left"
    )
    return meta


def _build_day(
    con: duckdb.DuckDBPyConnection, qdir: Path, day: str
) -> pd.DataFrame | None:
    """单日全市场行（OHLCV + L1 因子）；无分区返回 None。"""
    kline_path = qdir / "1_kline_data" / "daily_forward" / f"dt={day}" / "data.parquet"
    if not kline_path.is_file():
        return None
    k = con.execute(
        "SELECT symbol, time AS trade_date, open, high, low, close, volume, amount "
        f"FROM read_parquet('{kline_path}')"
    ).fetchdf()
    if k.empty:
        return None
    l1_path = qdir / "6_ml_datasets" / "l1_factors" / f"dt={day}" / "data.parquet"
    if l1_path.is_file():
        # return_3d/10d/60d 与 mom_ret_* 实测逐值相等（同族口径），直接映射；
        # 精确同名的 return_1d/5d/20d 用原名列。
        l1 = con.execute(
            "SELECT symbol, pctchange AS pct_change, return_1d, return_5d, return_20d, "
            "mom_ret_3d AS return_3d, mom_ret_10d AS return_10d, mom_ret_60d AS return_60d, "
            "ma5, ma20, ma60, ma_gap_5, ma_gap_20, rsi_14, rsi_6, macd_hist, kdj_k, "
            "beta_20, vol_std_5, vol_std_20, vol_std_60, vol_atr_14, volume_ratio_5, "
            "volume_ratio_20, volume_ma_5, amount_ma_5, flow_net_amount, ln_mv_total "
            f"FROM read_parquet('{l1_path}')"
        ).fetchdf()
        k = k.merge(l1, on="symbol", how="left")
    k["trade_date"] = pd.to_datetime(k["trade_date"]).dt.date
    k["adj_factor"] = 1.0  # QuantUS 无复权因子，价格即未复权原始价
    k["is_st"] = False
    k["listing_market"] = "US"
    return k


def sync_day(date_str: str | None = None) -> dict:
    """灌最近 90 个交易日快照（≤ date_str）。返回 {date, rows, days}。"""
    _ensure_table()
    qdir = _resolve_quantus_data_dir()
    days = _trading_days(qdir, date_str)
    if not days:
        raise SystemExit("无可用的 daily_forward 分区")
    latest_day = days[-1]

    con = duckdb.connect()
    try:
        meta = _read_static(con, qdir)
        frames = [f for f in (_build_day(con, qdir, d) for d in days) if f is not None]
    finally:
        con.close()
    if not frames:
        raise SystemExit(f"最近 {len(days)} 个分区均无数据")

    df = pd.concat(frames, ignore_index=True)
    df = df.merge(meta, on="symbol", how="left")
    # f10 估值快照只落在最新交易日行：历史行保持 NULL，防止「今日估值」被当成当日估值使用
    for src, dst in (
        ("pe_ratio", "pe_ttm"),
        ("pb_ratio", "pb"),
        ("market_cap", "total_mv"),
    ):
        df[dst] = df[src].where(df["trade_date"] == pd.Timestamp(latest_day).date())
    df["name"] = df["stock_name"]
    df = df.drop_duplicates(["symbol", "trade_date"], keep="last")

    keep = _SNAPSHOT_COLUMNS
    out = df.reindex(columns=keep)
    # astype(object) 把 numpy.int64/bool 还原成 Python 标量（psycopg2 不认 numpy 类型），
    # where(notna) 把 NaN/NaT 统一成 NULL（否则 float('nan') 会落成 'NaN'::float8）
    out = out.astype(object).where(pd.notna(out), None)
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE}")  # 全量重灌窗口快照（幂等）
            rows = [tuple(r) for r in out.itertuples(index=False, name=None)]
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {_TABLE} ({','.join(keep)}) VALUES %s",
                rows,
                page_size=2000,
            )
            conn.commit()
    finally:
        conn.close()
    log.info(
        "stock_daily_latest_us %s 灌入 %d 行（%d 个交易日）",
        latest_day,
        len(out),
        len(days),
    )
    return {"date": latest_day, "rows": int(len(out)), "days": len(days)}


def main() -> int:
    parser = argparse.ArgumentParser(description="美股 stock_daily_latest_us 每日同步")
    parser.add_argument(
        "--date", type=str, default=None, help="YYYYMMDD，缺省最新交易日"
    )
    args = parser.parse_args()
    started = datetime.now()
    result = sync_day(args.date)
    result["elapsed"] = round((datetime.now() - started).total_seconds(), 1)
    result["missing_columns"] = _MISSING_COLUMNS
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
