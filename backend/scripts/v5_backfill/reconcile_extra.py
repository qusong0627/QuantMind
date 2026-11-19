"""第二批（gtja/tech/style/risk）对账：计算 vs 官方 V5 重叠日。"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from backend.scripts.v5_backfill.extra_formulas import compute_extra
from backend.scripts.v5_backfill.formulas import winsorize_cross_section
from backend.scripts.v5_backfill.panel import (QDB, load_bars, load_index_return_dates)


def one(target: str) -> dict:
    v5 = pd.read_parquet(QDB / "6_ml_datasets" / "factor_dictionary" / f"dt={target}" / "data.parquet").set_index("symbol")
    bars = load_bars(target, window=800)
    dates = sorted(bars["dt"].unique())
    mret = load_index_return_dates(dates)
    extra = compute_extra(bars, mret)
    cross = extra[extra["dt"] == target].set_index("symbol")
    cross = winsorize_cross_section(cross).reindex(v5.index)
    rows = []
    for col in cross.columns:
        if col not in v5.columns:
            continue
        ref = pd.to_numeric(v5[col], errors="coerce")
        a = pd.to_numeric(cross[col], errors="coerce")
        m = a.notna() & ref.notna()
        if m.sum() < 100:
            rows.append({"column": col, "n": int(m.sum()), "status": "NO_DATA"})
            continue
        rel = (a[m] - ref[m]).abs() / ref[m].abs().clip(lower=1e-12)
        lo_r, hi_r = ref[m].min(), ref[m].max()
        lo_c, hi_c = a[m].quantile(0.01), a[m].quantile(0.99)
        body = (ref[m] > lo_r) & (ref[m] < hi_r) & (a[m] > lo_c) & (a[m] < hi_c)
        eq_body = float((rel[body] < 1e-5).mean()) if body.any() else float("nan")
        rows.append({
            "column": col, "n": int(m.sum()), "eq_1e-5": round(float((rel < 1e-5).mean()), 6),
            "eq_body": round(eq_body, 6) if eq_body == eq_body else None,
            "p50": float(rel.median()), "p99": float(rel.quantile(0.99)), "max": float(rel.max()),
            "status": "PASS" if eq_body >= 0.9999 else ("PARTIAL" if (rel < 1e-5).mean() >= 0.95 else "FAIL"),
        })
    return {"target": target, "columns": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="20180629,20200630")
    ap.add_argument("--out", default="data/tmp/qc_2010_2015/v5_reconcile_extra.json")
    a = ap.parse_args()
    rep = []
    for d in [x.strip() for x in a.dates.split(",") if x.strip()]:
        r = one(d)
        rep.append(r)
        print(f"\n===== {d} =====")
        print(pd.DataFrame(r["columns"]).sort_values(["status", "column"]).to_string(index=False))
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print("\n报告:", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
