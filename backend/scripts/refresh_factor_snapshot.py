#!/usr/bin/env python3
"""刷新非 CN 因子研究工作台的个股快照（不重建因子面板）。

只重算 ``factor_research_private/stock_snapshot.parquet`` 的名称/行业/市值列：

- HK：akshare_financial 已发行股本 × 最新收盘价（缺值回退总市值）、
  akshare_profile 恒生行业；
- US：最新 valuation 分区的总市值/PE/PB；
- FUTURES：收盘 × 合约乘数 × 持仓量（20 日均值）；无持仓的外盘/现货退
  20 日成交额；品种 → 板块。

用法（容器内）::

    python backend/scripts/refresh_factor_snapshot.py --markets HK,US,FUTURES
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.scripts.build_factor_panel_private import (  # noqa: E402
    _out_dir,
    _stock_snapshot_non_cn,
)
from backend.services.engine.factor_research import data as frdata  # noqa: E402
from backend.services.engine.factor_research import windows as frwindows  # noqa: E402

SNAPSHOT_COLS = [
    "symbol",
    "name",
    "industry",
    "total_mv_yi",
    "pe_ttm",
    "pb",
    "avg_amount_yi",
]


def _daily_fields(market: str) -> tuple[str, ...]:
    fields = ("open", "high", "low", "close", "volume", "amount")
    if market == "FUTURES":
        fields = (*fields, "position")
    return fields


def refresh(market: str, days: int) -> dict:
    out = _out_dir(market)
    path = out / "stock_snapshot.parquet"
    old = pd.read_parquet(path) if path.exists() else None
    start = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y%m%d")
    daily = frdata.load_daily_panel(
        start, "20991231", fields=_daily_fields(market), market=market
    )
    new = _stock_snapshot_non_cn(market, daily)
    if old is not None and "symbol" in old.columns:
        base = old[["symbol"]].copy()
        fresh = new.drop_duplicates("symbol").set_index("symbol")
        merged = base.merge(
            fresh.reset_index(), on="symbol", how="left", suffixes=("", "_new")
        )
        # 名称/行业/成交额：新快照优先，缺值保留旧值（不清空既有信息）
        for col in ("name", "industry", "total_mv_yi", "pe_ttm", "pb", "avg_amount_yi"):
            if col not in merged.columns:
                merged[col] = np.nan
        old_idx = old.drop_duplicates("symbol").set_index("symbol")
        for col in ("name", "industry", "avg_amount_yi"):
            if col in merged.columns and col in old_idx.columns:
                fallback = old_idx[col].reindex(merged["symbol"]).to_numpy()
                merged[col] = merged[col].where(merged[col].notna(), fallback)
        # 未在最近行情里出现的旧标的也要有行业（HK profile / FUTURES 品种表覆盖全代码）
        if market == "FUTURES":
            vmap = frdata.futures_variety_sector_map()
            fallback = [
                vmap.get(frdata.futures_variety_code(sym), "其他")
                for sym in merged["symbol"]
            ]
            merged["industry"] = merged["industry"].where(
                merged["industry"].notna(), pd.Series(fallback, index=merged.index)
            )
        else:
            sect = frdata.load_sector_map(market)
            if sect is not None and "industry" in sect.columns:
                smap = sect.drop_duplicates("symbol").set_index("symbol")["industry"]
                fallback = smap.reindex(merged["symbol"]).to_numpy()
                merged["industry"] = merged["industry"].where(
                    merged["industry"].notna(), fallback
                )
    else:
        merged = new
    merged = merged.reindex(columns=SNAPSHOT_COLS)
    frwindows.atomic_write_parquet(merged, path)
    return {
        "market": market,
        "path": str(path),
        "rows": len(merged),
        "mv_nonnull": int(merged["total_mv_yi"].notna().sum()),
        "industry_nonnull": int(merged["industry"].notna().sum()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="HK,US,FUTURES")
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()
    for market in [m.strip().upper() for m in args.markets.split(",") if m.strip()]:
        info = refresh(market, args.days)
        print(
            f"{info['market']}: {info['rows']} rows · 市值 {info['mv_nonnull']} · "
            f"行业 {info['industry_nonnull']} → {info['path']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
