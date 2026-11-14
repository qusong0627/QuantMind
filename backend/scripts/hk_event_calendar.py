#!/usr/bin/env python3
"""港股 PIT 事件日历：南向首次重仓、拆合股、分红除净。

用途：给事件研究/DiD 提供统一的 (date, stock, event_type) 表。
不含 alpha 逻辑；只做时间戳与来源归档。

输出：
  data/quanthk/2_base_sector/event_calendar/data.parquet
  data/quanthk/2_base_sector/event_calendar/summary.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

ROOT = Path(
    os.getenv(
        "QM_QUANTHK_DATA_DIR",
        "/data/quanthk" if Path("/data/quanthk").is_dir() else str(PROJECT_ROOT / "data" / "quanthk"),
    )
)
OUT = ROOT / "2_base_sector" / "event_calendar"
START = pd.Timestamp("2024-11-22")


def _south_events() -> pd.DataFrame:
    frames = []
    for f in sorted((ROOT / "2_base_sector" / "hsgt_south").glob("dt=*/data.parquet")):
        d = pd.read_parquet(f, columns=["symbol", "holding_percentage", "query_date"])
        if d.empty:
            continue
        d["date"] = pd.to_datetime(f.parent.name[3:], format="%Y%m%d")
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    first_available = d["date"].min()
    d = d.loc[d["holding_percentage"] >= 0.01]
    first = d.groupby("symbol", as_index=False)["date"].min()
    out = pd.DataFrame(
        {
            "date": first["date"],
            "stock_code": first["symbol"],
            "event_type": "south_init_1pct",
            "source": "hsgt_south",
            "value": 0.01,
            "meta": "首次南向持股>=1%",
            "censored": first["date"] <= first_available,
        }
    )
    return out.loc[out["date"] >= START]


def _split_events() -> pd.DataFrame:
    rows = []
    for f in (ROOT / "3_financial_data" / "splits").glob("*.parquet"):
        try:
            d = pd.read_parquet(f, columns=["trade_date", "split_ratio", "symbol", "source"])
        except Exception:  # noqa: BLE001
            continue
        d = d.dropna(subset=["trade_date"])
        d["date"] = pd.to_datetime(d["trade_date"], errors="coerce")
        for r in d.itertuples(index=False):
            if pd.isna(r.date) or r.date < START:
                continue
            rows.append(
                {
                    "date": r.date,
                    "stock_code": r.symbol,
                    "event_type": "split",
                    "source": r.source,
                    "value": float(r.split_ratio) if pd.notna(r.split_ratio) else None,
                    "meta": "拆合股比例",
                    "censored": False,
                }
            )
    return pd.DataFrame(rows)


def _dividend_events() -> pd.DataFrame:
    rows = []
    for f in (ROOT / "3_financial_data" / "dividend").glob("*.parquet"):
        try:
            d = pd.read_parquet(f)
        except Exception:  # noqa: BLE001
            continue
        if d.empty:
            continue
        col = "ex_date" if "ex_date" in d.columns else "trade_date"
        d = d.dropna(subset=[col])
        d["date"] = pd.to_datetime(d[col], errors="coerce")
        if col == "ex_date" and "trade_date" in d.columns:
            d["date"] = d["date"].fillna(pd.to_datetime(d["trade_date"], errors="coerce"))
        d = d.dropna(subset=["date"])
        for r in d.itertuples(index=False):
            if pd.isna(r.date) or r.date < START:
                continue
            div = getattr(r, "dividend", None)
            rows.append(
                {
                    "date": r.date,
                    "stock_code": r.symbol,
                    "event_type": "dividend",
                    "source": getattr(r, "source", ""),
                    "value": float(div) if pd.notna(div) else None,
                    "meta": "除净日",
                    "censored": False,
                }
            )
    return pd.DataFrame(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    frames = [d for d in (_south_events(), _split_events(), _dividend_events()) if not d.empty]
    if not frames:
        raise SystemExit("no events")
    all_events = pd.concat(frames, ignore_index=True)
    all_events = all_events.dropna(subset=["date", "stock_code"]).sort_values(["date", "event_type"])
    all_events.to_parquet(OUT / "data.parquet", index=False)
    summary = {
        "rows": int(len(all_events)),
        "by_type": all_events["event_type"].value_counts().to_dict(),
        "censored_by_type": (
            all_events.loc[all_events["censored"], "event_type"].value_counts().to_dict()
        ),
        "usable_by_type": (
            all_events.loc[~all_events["censored"], "event_type"].value_counts().to_dict()
        ),
        "by_year": {
            str(y): int(n)
            for y, n in all_events.groupby(all_events["date"].dt.year).size().items()
        },
        "date_range": [str(all_events["date"].min().date()), str(all_events["date"].max().date())],
        "note": "south_init_1pct 是首次持有代理，不是官方纳入日；split/dividend 来自 Yahoo/akshare。",
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
