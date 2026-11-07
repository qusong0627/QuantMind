"""股票研究档案库：把每次研究涉及的股票信息持久化，方便下次直接查。

产物（``/data/stock_classifier/research/``）：
- ``stock_registry.parquet``：每只股票的汇总（首次/最近出现、被标最差/最好的天数、
  最近预测值、排名分位、名称/行业/ST/HS300）；
- ``stock_registry.csv``：同上的 CSV（方便直接打开）；
- ``stock_notes.json``：人工/研究备注（按代码累积，追加式）。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.research_registry build
  cd /app && python3 -m backend.scripts.stock_classifier.research_registry show SZ000001
  cd /app && python3 -m backend.scripts.stock_classifier.research_registry list --flag bottom --min-share 0.3
  cd /app && python3 -m backend.scripts.stock_classifier.research_registry note SZ000001 "排雷命中：10日跌超10%"
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

QUANTDB = Path("/data/quantdb")
OUT = Path("/data/stock_classifier/research")
PRED = Path(
    "/app/models/users/default/10000001/custom/"
    "mdl_cust_train_20261102081916_9553667e_1613e123/pred.parquet"
)
INSTRUMENT = QUANTDB / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"


def _suffix(symbol: str) -> str:
    s = str(symbol)
    if s[:2] in ("SH", "SZ", "BJ") and len(s) == 8:
        return f"{s[2:]}.{s[:2]}"
    return s


def _prefix(symbol: str) -> str:
    s = str(symbol)
    if "." in s:
        code, mkt = s.split(".", 1)
        return f"{mkt}{code}"
    return s


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pred = pd.read_parquet(PRED, columns=["symbol", "trade_date", "pred"])
    pred["trade_date"] = pd.to_datetime(pred["trade_date"])
    pred = pred[pred["trade_date"] >= "2025-01-01"].dropna(subset=["pred"])
    pred["sym"] = pred["symbol"].map(_suffix)

    # 每日排名分位与是否落在最差/最好 10%
    g = pred.groupby("trade_date")["pred"]
    pred["rank_pct"] = g.rank(pct=True)
    pred["is_bottom10"] = pred["rank_pct"] <= 0.1
    pred["is_top10"] = pred["rank_pct"] >= 0.9

    agg = pred.groupby("sym").agg(
        first_date=("trade_date", "min"),
        last_date=("trade_date", "max"),
        n_days=("pred", "size"),
        bottom10_days=("is_bottom10", "sum"),
        top10_days=("is_top10", "sum"),
        mean_pred=("pred", "mean"),
        last_pred=("pred", "last"),
        last_rank_pct=("rank_pct", "last"),
    )
    agg["bottom10_share"] = (agg["bottom10_days"] / agg["n_days"]).round(3)
    agg["top10_share"] = (agg["top10_days"] / agg["n_days"]).round(3)

    inst = pq.read_table(INSTRUMENT, columns=["Symbol", "Name", "rs_hyname", "IsSTGP", "BelongHS300"]).to_pandas()
    inst["sym"] = inst["Symbol"].astype(str)
    merged = agg.reset_index().merge(inst, on="sym", how="left")
    merged["symbol_prefix"] = merged["sym"].map(_prefix)
    merged = merged.rename(
        columns={"Name": "name", "rs_hyname": "industry", "IsSTGP": "is_st", "BelongHS300": "in_hs300"}
    )
    cols = [
        "sym", "symbol_prefix", "name", "industry", "is_st", "in_hs300",
        "first_date", "last_date", "n_days", "bottom10_days", "bottom10_share",
        "top10_days", "top10_share", "mean_pred", "last_pred", "last_rank_pct",
    ]
    merged = merged[cols].sort_values(["bottom10_share", "n_days"], ascending=[False, False])
    merged.to_parquet(OUT / "stock_registry.parquet", index=False)
    merged.to_csv(OUT / "stock_registry.csv", index=False, encoding="utf-8-sig")
    notes_path = OUT / "stock_notes.json"
    if not notes_path.exists():
        notes_path.write_text("{}", encoding="utf-8")
    print(f"档案库：{len(merged)} 只股票 → {OUT / 'stock_registry.parquet'}")
    print(f"其中最差10%占比≥30%的 {int((merged.bottom10_share >= 0.3).sum())} 只，"
          f"≥50% 的 {int((merged.bottom10_share >= 0.5).sum())} 只")


def show(symbol: str) -> None:
    reg = pd.read_parquet(OUT / "stock_registry.parquet")
    sym = _suffix(symbol) if symbol[:2] in ("SH", "SZ", "BJ") else symbol
    row = reg[reg.sym == sym]
    if row.empty:
        print(f"未找到 {symbol}")
        return
    print(json.dumps(row.iloc[0].to_dict(), ensure_ascii=False, indent=2, default=str))
    notes = json.loads((OUT / "stock_notes.json").read_text("utf-8"))
    if sym in notes:
        print("\n研究备注：")
        for n in notes[sym]:
            print(f"  [{n['date']}] {n['text']}")


def list_items(flag: str, min_share: float, limit: int) -> None:
    reg = pd.read_parquet(OUT / "stock_registry.parquet")
    if flag == "bottom":
        out = reg[(reg.bottom10_share >= min_share) & (reg.n_days >= 100)]
        out = out.sort_values("bottom10_share", ascending=False)
    else:
        out = reg[(reg.top10_share >= min_share) & (reg.n_days >= 100)]
        out = out.sort_values("top10_share", ascending=False)
    print(
        out[["sym", "name", "industry", "n_days", "bottom10_share", "top10_share",
             "last_pred", "last_rank_pct", "last_date"]]
        .head(limit)
        .to_string(index=False)
    )


def note(symbol: str, text: str) -> None:
    notes_path = OUT / "stock_notes.json"
    notes = json.loads(notes_path.read_text("utf-8")) if notes_path.exists() else {}
    sym = _suffix(symbol) if symbol[:2] in ("SH", "SZ", "BJ") else symbol
    notes.setdefault(sym, []).append(
        {"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text}
    )
    notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{sym}：已记录（累计 {len(notes[sym])} 条）")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    s = sub.add_parser("show")
    s.add_argument("symbol")
    li = sub.add_parser("list")
    li.add_argument("--flag", choices=("bottom", "top"), default="bottom")
    li.add_argument("--min-share", type=float, default=0.3)
    li.add_argument("--limit", type=int, default=30)
    n = sub.add_parser("note")
    n.add_argument("symbol")
    n.add_argument("text")
    args = ap.parse_args()
    if args.cmd == "build":
        build()
    elif args.cmd == "show":
        show(args.symbol)
    elif args.cmd == "list":
        list_items(args.flag, args.min_share, args.limit)
    else:
        note(args.symbol, args.text)


if __name__ == "__main__":
    main()
