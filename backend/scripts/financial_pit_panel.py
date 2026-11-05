#!/usr/bin/env python3
"""PIT 财务面板：per-share 指标按公告日对齐，防止前视。

数据源：`3_financial_data/pershare_index/*.parquet`
  关键列：m_timetag(报告期), m_anntime/actual_ann_dt(公告日),
          s_fa_weighted_roe, inc_revenue_rate, inc_net_profit_rate,
          gross_margin, net_profit, s_fa_eps_basic

输出：
  `5_technical_derived/margin/financial_pit.parquet`
  `5_technical_derived/margin/financial_pit_quarterly.parquet`
"""

from __future__ import annotations

import glob
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
if not BASE.exists():
    BASE = PROJECT_ROOT / "data" / "quantdb"
FIN_DIR = BASE / "3_financial_data" / "pershare_index"
KLINE_DIR = BASE / "1_kline_data" / "daily_backward"
OUT_DIR = BASE / "5_technical_derived" / "margin"

COLS = [
    "Symbol",
    "m_timetag",
    "m_anntime",
    "actual_ann_dt",
    "s_fa_weighted_roe",
    "inc_revenue_rate",
    "inc_net_profit_rate",
    "gross_margin",
    "net_profit",
    "s_fa_eps_basic",
]
KEEP = {
    "s_fa_weighted_roe": "roe",
    "inc_revenue_rate": "rev_growth",
    "inc_net_profit_rate": "profit_growth",
    "gross_margin": "gross_margin",
    "net_profit": "net_profit",
    "s_fa_eps_basic": "eps",
}


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.astype(str), format="%Y%m%d", errors="coerce")


def load_pit() -> pd.DataFrame:
    rows = []
    for f in glob.glob(str(FIN_DIR / "*.parquet")):
        df = pq.read_table(f).to_pandas()
        if df.empty:
            continue
        for c in COLS:
            if c not in df.columns:
                df[c] = np.nan
        df = df[COLS]
        df = df.rename(columns={"Symbol": "symbol"})
        df["report_date"] = _date(df["m_timetag"])
        df["ann_date"] = _date(df["actual_ann_dt"])
        df["ann_date"] = df["ann_date"].fillna(_date(df["m_anntime"]))
        df = df.dropna(subset=["report_date", "ann_date"]).rename(columns=KEEP)
        rows.append(df[["symbol", "report_date", "ann_date", *KEEP.values()]])
    out = pd.concat(rows, ignore_index=True)
    out = out.sort_values(["symbol", "report_date", "ann_date"]).drop_duplicates(
        ["symbol", "report_date"], keep="last"
    )
    return out


def quarter_asofs() -> pd.DatetimeIndex:
    parts = sorted(KLINE_DIR.glob("dt=*"))
    td = pd.DatetimeIndex(
        [pd.Timestamp(p.name.split("=")[1]) for p in parts]
    ).sort_values()
    out = []
    for year in range(2016, 2027):
        for _q, md in ((1, "03-31"), (2, "06-30"), (3, "09-30"), (4, "12-31")):
            qe = pd.Timestamp(f"{year}-{md}")
            if qe < td.min() or qe > td.max():
                continue
            i = np.searchsorted(td.values, np.datetime64(qe), side="right") - 1
            out.append(td[min(i + 10, len(td) - 1)])
    return pd.DatetimeIndex(sorted(set(out)))


def build_quarterly(pit: pd.DataFrame, asofs: pd.DatetimeIndex) -> pd.DataFrame:
    # 日期列可能是字符串（load_pit 会转 Timestamp，外部调用方不一定）——统一归一，
    # 否则 merge_asof 会因 dtype 不一致直接抛 MergeError。
    pit = pit.copy()
    pit["report_date"] = pd.to_datetime(pit["report_date"], errors="coerce")
    pit["ann_date"] = pd.to_datetime(pit["ann_date"], errors="coerce")
    pit = pit.dropna(subset=["report_date", "ann_date"])
    # 外部调用方可能只带部分指标列；缺失列补 NaN，`*_cs` 排名照常生成。
    for column in KEEP.values():
        if column not in pit.columns:
            pit[column] = np.nan
    symbols = pit["symbol"].unique()
    grid = pd.DataFrame(
        {
            "symbol": np.repeat(symbols, len(asofs)),
            "asof": np.tile(asofs.values, len(symbols)),
        }
    ).sort_values(["symbol", "asof"])
    left = grid.sort_values(["symbol", "asof"])
    right = pit.sort_values(["symbol", "ann_date"])
    right_groups = {
        k: v.drop(columns=["symbol"]).sort_values("ann_date")
        for k, v in right.groupby("symbol")
    }
    outs = []
    for sym, g in left.groupby("symbol"):
        r = right_groups.get(sym)
        if r is None or r.empty:
            continue
        outs.append(
            pd.merge_asof(
                g.sort_values("asof"),
                r,
                left_on="asof",
                right_on="ann_date",
                direction="backward",
            )
        )
    out = pd.concat(outs, ignore_index=True).dropna(subset=["report_date"])
    for c in KEEP.values():
        out[f"{c}_cs"] = out.groupby("asof")[c].rank(pct=True)
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    pit = load_pit()
    asofs = quarter_asofs()
    q = build_quarterly(pit, asofs)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pit.to_parquet(OUT_DIR / "financial_pit.parquet", index=False)
    q.to_parquet(OUT_DIR / "financial_pit_quarterly.parquet", index=False)
    print("pit", len(pit), "quarterly", len(q), "asofs", len(asofs))
    print(
        q.tail(3)[
            ["symbol", "asof", "report_date", "ann_date", "roe", "rev_growth"]
        ].to_string(index=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
