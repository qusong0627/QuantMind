"""机构择时 v2：长历史市场面板（广度/估值/权重裂口/两融/趋势）。

本模块只读 QuantDB 与市场分析快照，不写生产表；结果缓存到
``${QM_MARKET_TIMING_V2_DIR:-/data/market_timing_v2}/cache``。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DATA_ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
STATE_DIR = Path(os.environ.get("QM_MARKET_TIMING_V2_DIR", "/data/market_timing_v2"))
CACHE_DIR = STATE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

START = pd.Timestamp("2015-06-01")
INDEX_SYMBOLS = {
    "hs300": "000300.SH",
    "sse": "000001.SH",
    "szse": "399001.SZ",
    "zz500": "000905.SH",
    "zz1000": "000852.SH",
}

FEATURES = [
    "ret1",
    "ret5",
    "ret20",
    "ret60",
    "ret120",
    "vol5",
    "vol20",
    "vol60",
    "ma_gap20",
    "ma_gap60",
    "ma_gap120",
    "dd20",
    "dd60",
    "breadth",
    "log_adr",
    "limit_up_ratio",
    "limit_down_ratio",
    "limit_net",
    "pct_above_ma20",
    "pct_above_ma60",
    "mcclellan",
    "high_low_net",
    "net_adv_pct",
    "trin_amt",
    "trin_vol",
    "amt_ratio20",
    "bp_capw_pct",
    "ep_capw_pct",
    "med_pe_pct",
    "div_yield_pct",
    "bp_chg20",
    "ep_chg20",
    "pe_chg20",
    "gap_ie",
    "gap_ie_5",
    "gap_ie_20",
    "gap_hz",
    "gap_hz_5",
    "gap_hz_20",
    "gap_ze",
    "gap_ze_5",
    "gap_ze_20",
    "margin_bal_pct20",
    "margin_net_5",
]
TARGET = "fwd5"


def _partition_files(root: Path, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    lo, hi = int(start.strftime("%Y%m%d")), int(end.strftime("%Y%m%d"))
    files: list[str] = []
    if not root.exists():
        return files
    for name in sorted(os.listdir(root)):
        if not name.startswith("dt="):
            continue
        ds = name[3:]
        if len(ds) != 8 or not ds.isdigit():
            continue
        if lo <= int(ds) <= hi:
            p = root / name / "data.parquet"
            if p.exists():
                files.append(str(p))
    return files


def _read_files(
    files: list[str], columns: str, where: str = "", group: str = ""
) -> pd.DataFrame:
    con = duckdb.connect()
    files_sql = "[" + ",".join("'" + f + "'" for f in files) + "]"
    q = f"SELECT {columns} FROM read_parquet({files_sql}, union_by_name=true) {where} {group}"
    try:
        return con.execute(q).df()
    finally:
        con.close()


def _load_index(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    files = _partition_files(DATA_ROOT / "1_kline_data" / "index_daily", start, end)
    syms = ",".join(f"'{s}'" for s in INDEX_SYMBOLS.values())
    df = _read_files(
        files,
        "symbol, time, open, close",
        f"WHERE symbol IN ({syms}) AND open > 0 AND close > 0",
    )
    df["date"] = pd.to_datetime(df["time"]).dt.normalize()
    return df.drop(columns=["time"]).sort_values(["symbol", "date"])


def _load_margin(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    cache = CACHE_DIR / "margin_daily.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        df["date"] = pd.to_datetime(df["date"])
        # 两融分区可能晚到 1-2 天；缓存末日晚于 end-2 天才复用
        if not df.empty and df["date"].max() >= end - pd.Timedelta(days=2):
            return df
    files = _partition_files(DATA_ROOT / "2_base_sector" / "margin_trading", start, end)
    if not files:
        return pd.DataFrame(
            columns=[
                "date",
                "finance_balance",
                "finance_net",
                "finance_buy",
                "finance_repay",
            ]
        )
    df = _read_files(
        files,
        "CAST(substr(regexp_replace(CAST(time AS VARCHAR), '[^0-9]', '', 'g'), 1, 8) AS INTEGER) AS dt_key, "
        "sum(finance_balance) AS finance_balance, sum(finance_net) AS finance_net, "
        "sum(finance_buy) AS finance_buy, sum(finance_repay) AS finance_repay",
        group="GROUP BY 1",
    )
    df["date"] = pd.to_datetime(df["dt_key"].astype(str), format="%Y%m%d")
    df = df.drop(columns=["dt_key"]).sort_values("date")
    for c in ["finance_balance", "finance_net", "finance_buy", "finance_repay"]:
        df[c] = pd.to_numeric(df[c], errors="coerce") / 1e4
    df.to_parquet(cache, index=False)
    return df


def build_market_history(*, force: bool = False) -> pd.DataFrame:
    cache = CACHE_DIR / "market_history.parquet"
    if cache.exists() and not force:
        df = pd.read_parquet(cache)
        df["date"] = pd.to_datetime(df["date"])
        return df

    today = pd.Timestamp.today().normalize()
    idx = _load_index(START, today + pd.Timedelta(days=1))
    closes = idx.pivot_table(
        index="date", columns="symbol", values="close", aggfunc="last"
    )
    opens = idx.pivot_table(
        index="date", columns="symbol", values="open", aggfunc="last"
    )
    cal = closes.index.sort_values()
    closes, opens = closes.reindex(cal), opens.reindex(cal)

    trin = pd.read_parquet(
        DATA_ROOT / "5_technical_derived/market_breadth/trin_daily.parquet"
    )
    trin["date"] = pd.to_datetime(trin["date"]).dt.normalize()
    trin = trin.set_index("date").reindex(cal)

    val = pd.read_parquet(
        DATA_ROOT / "5_technical_derived/market_breadth/valuation_daily.parquet"
    )
    if "date" not in val.columns:
        val = val.reset_index()
    val["date"] = pd.to_datetime(val["date"]).dt.normalize()
    val = val.set_index("date").reindex(cal).ffill()

    margin = _load_margin(START, today).set_index("date").reindex(cal)

    hs = closes[INDEX_SYMBOLS["hs300"]]
    hs_ret = hs.pct_change()
    zz_ret = closes[INDEX_SYMBOLS["zz1000"]].pct_change()
    ew_ret = pd.to_numeric(trin["ret_equal_weight"], errors="coerce") / 100.0

    df = pd.DataFrame(index=cal)
    df["ret_hs300"] = hs_ret
    df["ret_zz1000"] = zz_ret
    df["ret_ew"] = ew_ret
    for n in (1, 5, 20, 60, 120):
        df[f"ret{n}"] = hs / hs.shift(n) - 1
    for n in (5, 20, 60):
        df[f"vol{n}"] = hs_ret.rolling(n).std()
    for n in (20, 60, 120):
        df[f"ma_gap{n}"] = hs / hs.rolling(n).mean() - 1
        df[f"dd{n}"] = hs / hs.rolling(n).max() - 1

    for c in [
        "n_total",
        "n_up",
        "n_down",
        "n_limit_up",
        "n_limit_down",
        "ret_disp",
        "adr",
        "mcclellan",
        "pct_above_ma20",
        "pct_above_ma60",
        "n_high_52w",
        "n_low_52w",
        "trin_amt",
        "trin_vol",
        "net_advances",
        "amt_total",
    ]:
        df[c] = pd.to_numeric(trin[c], errors="coerce")
    df["breadth"] = df["n_up"] / (df["n_up"] + df["n_down"])
    df["log_adr"] = np.log1p(df["adr"].clip(lower=0))
    df["limit_up_ratio"] = df["n_limit_up"] / df["n_total"]
    df["limit_down_ratio"] = df["n_limit_down"] / df["n_total"]
    df["limit_net"] = df["limit_up_ratio"] - df["limit_down_ratio"]
    df["high_low_net"] = (df["n_high_52w"] - df["n_low_52w"]) / df["n_total"]
    df["net_adv_pct"] = df["net_advances"] / df["n_total"]
    df["amt_ratio20"] = df["amt_total"] / df["amt_total"].rolling(20).mean()

    for c in [
        "bp_capw_pct",
        "ep_capw_pct",
        "med_pe_pct",
        "div_yield_pct",
        "med_pe_cheap_pct",
    ]:
        df[c] = pd.to_numeric(val[c], errors="coerce")
    df["bp_chg20"] = df["bp_capw_pct"].diff(20)
    df["ep_chg20"] = df["ep_capw_pct"].diff(20)
    df["pe_chg20"] = df["med_pe_pct"].diff(20)

    df["gap_ie"] = df["ret_hs300"] - df["ret_ew"]
    df["gap_hz"] = df["ret_hs300"] - df["ret_zz1000"]
    df["gap_ze"] = df["ret_zz1000"] - df["ret_ew"]
    for c in ["gap_ie", "gap_hz", "gap_ze"]:
        for n in (5, 20):
            df[f"{c}_{n}"] = df[c].rolling(n).sum()

    df["margin_bal"] = margin["finance_balance"]
    df["margin_net"] = margin["finance_net"]
    df["margin_bal_pct20"] = df["margin_bal"] / df["margin_bal"].rolling(20).mean() - 1
    df["margin_net_5"] = df["margin_net"].rolling(5).sum()

    o = opens[INDEX_SYMBOLS["hs300"]]
    for h in (1, 3, 5, 10, 20):
        vals = []
        for i in range(len(cal)):
            if i + 1 + h >= len(cal):
                vals.append(np.nan)
            else:
                entry = o.iloc[i + 1]
                exit_ = o.iloc[i + 1 + h]
                vals.append(
                    float(exit_ / entry - 1)
                    if entry > 0 and np.isfinite(exit_)
                    else np.nan
                )
        df[f"fwd{h}"] = vals
    df["dir5"] = (df["fwd5"] > 0).astype(float)

    out = df.reset_index()
    if "index" in out.columns:
        out = out.rename(columns={"index": "date"})
    out.to_parquet(cache, index=False)
    return out


def load_market_history() -> pd.DataFrame:
    df = build_market_history()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)
