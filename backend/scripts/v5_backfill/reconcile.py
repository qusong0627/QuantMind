"""V5 逐列对账：计算值 vs 官方 V5（重叠日），输出 PASS/FAIL 报告。"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from backend.scripts.v5_backfill.formulas import compute_batch, winsorize_cross_section
from backend.scripts.v5_backfill.panel import QDB, load_bars, load_circ_daily

SCHEMA = QDB / "6_ml_datasets" / "factor_dictionary"


def reconcile_one(target: str, window: int = 320) -> dict:
    v5 = pd.read_parquet(SCHEMA / f"dt={target}" / "data.parquet").set_index("symbol")
    bars = load_bars(target, window=window)
    symbols = sorted(bars["symbol"].unique())
    start = bars["dt"].min()
    circ = load_circ_daily(symbols, start, target)
    calc = compute_batch(bars, circ, target)
    calc_w = winsorize_cross_section(calc)
    calc_w = calc_w.reindex(v5.index)

    rows = []
    for col in calc_w.columns:
        if col not in v5.columns:
            continue
        ref = pd.to_numeric(v5[col], errors="coerce")
        a = pd.to_numeric(calc_w[col], errors="coerce")
        m = a.notna() & ref.notna()
        if m.sum() < 100:
            rows.append({"column": col, "n": int(m.sum()), "status": "NO_DATA"})
            continue
        rel = (a[m] - ref[m]).abs() / ref[m].abs().clip(lower=1e-12)
        eq6 = float((rel < 1e-6).mean())
        eq5 = float((rel < 1e-5).mean())
        eq4 = float((rel < 1e-4).mean())
        # 主体 = 官方 V5 与自算值都未被缩尾的值（排除两侧 clip 段）
        lo_ref, hi_ref = ref[m].min(), ref[m].max()
        lo_calc, hi_calc = a[m].quantile(0.01), a[m].quantile(0.99)
        body = (ref[m] > lo_ref) & (ref[m] < hi_ref) & (a[m] > lo_calc) & (a[m] < hi_calc)
        eq_body = float((rel[body] < 1e-5).mean()) if body.any() else float("nan")
        rows.append({
            "column": col, "n": int(m.sum()),
            "eq1e-6": round(eq6, 6), "eq1e-5": round(eq5, 6), "eq1e-4": round(eq4, 6),
            "eq_body": round(eq_body, 6) if eq_body == eq_body else None,
            "p50": float(rel.median()), "p99": float(rel.quantile(0.99)),
            "max": float(rel.max()),
            "status": "PASS" if eq_body >= 0.9999 else ("PARTIAL" if eq5 >= 0.95 else "FAIL"),
        })
    return {"target": target, "columns": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="20180629,20200630")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    report = []
    for d in [x.strip() for x in args.dates.split(",") if x.strip()]:
        r = reconcile_one(d)
        report.append(r)
        print(f"\n===== {d} =====")
        df = pd.DataFrame(r["columns"]).sort_values(["status", "column"])
        print(df.to_string(index=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("\n报告:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
