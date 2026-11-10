#!/usr/bin/env python3
"""因子研究主库重建金样对账（旧快照 vs 新快照）。

用途：把 2016–2026 主库重建结果与改造前的 2020–2026 快照在**重叠窗口**逐值比对。
经典库方向是目录静态定义，重叠期应逐值一致；私有库方向改为校准窗冻结后，
score/rank 会随 sign 变化（属预期的口径修正），金样只用于 classic。

用法（仓库根）：
    python3 backend/scripts/factor_research_golden_check.py \
        --old data/quantdb/_factor_research_backup_20261110/factor_research \
        --new data/quantdb/factor_research \
        --date 2020-01-23 [--tol 1e-5]

退出码：0=通过；1=有差异（打印差异摘要）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.services.engine.factor_research import windows as frwindows  # noqa: E402


def _panel_for_date(root: Path, date: pd.Timestamp) -> pd.DataFrame:
    parts = frwindows.year_files(root / frwindows.PANEL_DIR, str(date.date()), str(date.date()))
    if parts:
        df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    else:
        df = pd.read_parquet(root / "factor_panel.parquet")
    df = df[pd.to_datetime(df["trade_date"]) == date].copy()
    # 新旧快照的 categorical 类别集不同，比较前统一成字符串
    df["factor_code"] = df["factor_code"].astype(str)
    df["symbol"] = df["symbol"].astype(str)
    return df.sort_values(["factor_code", "rank"]).reset_index(drop=True)


def _next_sample(root: Path, date: pd.Timestamp) -> pd.Timestamp | None:
    """该快照里 date 的下一个采样日（fwd 的实际持有终点）。"""
    f = pd.read_parquet(root / "fwd_returns.parquet", columns=["trade_date"])
    ds = sorted(pd.to_datetime(f["trade_date"]).unique())
    later = [d for d in ds if pd.Timestamp(d) > date]
    return pd.Timestamp(later[0]) if later else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--tol", type=float, default=1e-5)
    args = ap.parse_args()
    old, new = Path(args.old), Path(args.new)
    date = pd.Timestamp(args.date)
    report: dict = {"date": str(date.date()), "ok": True}
    # 两个快照的末日不同 → 末月的持有终点不同（2026-10-08 vs 2026-10-30）。
    # 这种日期不参与比较；跨数据 vintage 的差异则照常报告。
    nxt_old, nxt_new = _next_sample(old, date), _next_sample(new, date)
    horizon_same = nxt_old == nxt_new
    report["horizon"] = {
        "old_next": str(nxt_old.date()) if nxt_old is not None else None,
        "new_next": str(nxt_new.date()) if nxt_new is not None else None,
        "comparable": horizon_same,
    }
    if not horizon_same:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    o = _panel_for_date(old, date)
    n = _panel_for_date(new, date)
    keys = ["factor_code", "rank"]
    m = o.merge(n, on=keys, suffixes=("_old", "_new"), how="inner")
    report["panel"] = {
        "old_rows": len(o),
        "new_rows": len(n),
        "common_rows": len(m),
        "symbol_mismatch": int((m["symbol_old"] != m["symbol_new"]).sum()),
    }
    # 数值列逐列比对（old/new 同列名不同后缀时手工处理）
    for c in ("score", "raw", "fwd_ret"):
        x = pd.to_numeric(m[f"{c}_old"], errors="coerce").to_numpy(dtype=float)
        y = pd.to_numeric(m[f"{c}_new"], errors="coerce").to_numpy(dtype=float)
        both = np.isfinite(x) & np.isfinite(y)
        md = float(np.max(np.abs(x[both] - y[both]))) if both.any() else 0.0
        report["panel"][c] = {
            "n": int(both.sum()),
            "max_abs_diff": md,
            "nan_mismatch": int((np.isfinite(x) != np.isfinite(y)).sum()),
        }
    report["ok"] &= report["panel"]["symbol_mismatch"] == 0
    for c in ("score", "raw", "fwd_ret"):
        report["ok"] &= report["panel"][c]["max_abs_diff"] <= args.tol
        report["ok"] &= report["panel"][c]["nan_mismatch"] == 0

    # IC 重叠期（sign 相同 → 逐值一致）
    oi = pd.read_parquet(old / "ic.parquet")
    ni = pd.read_parquet(new / "ic.parquet")
    oi["trade_date"] = pd.to_datetime(oi["trade_date"])
    ni["trade_date"] = pd.to_datetime(ni["trade_date"])
    old_dates = sorted(pd.to_datetime(oi["trade_date"]).unique())
    new_dates = sorted(pd.to_datetime(ni["trade_date"]).unique())
    def _bad_horizon(d):
        oi_ = [x for x in old_dates if pd.Timestamp(x) > pd.Timestamp(d)]
        ni_ = [x for x in new_dates if pd.Timestamp(x) > pd.Timestamp(d)]
        return (pd.Timestamp(oi_[0]) if oi_ else None) != (pd.Timestamp(ni_[0]) if ni_ else None)
    bad_dates = {d for d in set(old_dates) & set(new_dates) if _bad_horizon(d)}
    mi = oi.merge(ni, on=["factor_code", "trade_date"], suffixes=("_old", "_new"))
    mi = mi[~mi["trade_date"].isin(bad_dates)]
    x = pd.to_numeric(mi["ic_old"], errors="coerce").to_numpy(dtype=float)
    y = pd.to_numeric(mi["ic_new"], errors="coerce").to_numpy(dtype=float)
    both = np.isfinite(x) & np.isfinite(y)
    report["horizon_excluded_dates"] = [str(pd.Timestamp(d).date()) for d in sorted(bad_dates)]
    report["ic"] = {
        "common": len(mi),
        "max_abs_diff": float(np.max(np.abs(x[both] - y[both]))) if both.any() else 0.0,
        "nan_mismatch": int((np.isfinite(x) != np.isfinite(y)).sum()),
    }
    report["ok"] &= report["ic"]["max_abs_diff"] <= args.tol
    report["ok"] &= report["ic"]["nan_mismatch"] == 0

    # 持有期收益（同一数据源，应完全一致）
    of = pd.read_parquet(old / "fwd_returns.parquet")
    nf = pd.read_parquet(new / "fwd_returns.parquet")
    of["trade_date"] = pd.to_datetime(of["trade_date"])
    nf["trade_date"] = pd.to_datetime(nf["trade_date"])
    mf = of.merge(nf, on=["trade_date", "symbol"], suffixes=("_old", "_new"))
    mf = mf[~mf["trade_date"].isin(bad_dates)]
    x = pd.to_numeric(mf["fwd_ret_old"], errors="coerce").to_numpy(dtype=float)
    y = pd.to_numeric(mf["fwd_ret_new"], errors="coerce").to_numpy(dtype=float)
    both = np.isfinite(x) & np.isfinite(y)
    report["fwd"] = {
        "common": len(mf),
        "max_abs_diff": float(np.max(np.abs(x[both] - y[both]))) if both.any() else 0.0,
        "nan_mismatch": int((np.isfinite(x) != np.isfinite(y)).sum()),
    }
    report["ok"] &= report["fwd"]["max_abs_diff"] <= args.tol
    report["ok"] &= report["fwd"]["nan_mismatch"] == 0

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
