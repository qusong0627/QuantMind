#!/usr/bin/env python3
"""模型 pred 的市值暴露诊断：按日 Spearman(pred, log(total_mv))。

H1 size-neutral 的验收项：暴露应从 v1 的约 -0.094 降到 |ρ|<0.03。
用法（容器内）：
    python backend/scripts/model_eval/size_exposure.py \
        --pred /data/training_jobs/<run_id>/pred.parquet --split test --sample 10
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.model_eval.strict_backtest import to_suffix


def _load_mv(date: str) -> pd.Series:
    path = (
        Path("/data/quantdb/5_technical_derived/valuation")
        / f"dt={date.replace('-', '')}"
        / "data.parquet"
    )
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_parquet(path, columns=["symbol", "total_mv"])
    df["sym"] = df["symbol"].astype(str).map(to_suffix)
    return df.drop_duplicates("sym").set_index("sym")["total_mv"].astype(float)


def size_exposure(pred_path: str, split: str = "test", sample: int = 10) -> dict:
    pred = pd.read_parquet(pred_path, columns=["symbol", "trade_date", "pred", "split"])
    pred = pred[pred["split"] == split].copy()
    if pred.empty:
        return {"split": split, "dates": 0}
    pred["sym"] = pred["symbol"].astype(str).map(to_suffix)
    dates = sorted(pred["trade_date"].unique())
    step = max(1, len(dates) // max(1, sample))
    picked = dates[::step][:sample]
    values = []
    for date in picked:
        ds = pd.Timestamp(date).strftime("%Y%m%d")
        x = pred[pred["trade_date"] == date][["sym", "pred"]].copy()
        mv = _load_mv(ds)
        x["mv"] = x["sym"].map(mv)
        x = x[np.isfinite(x["mv"]) & (x["mv"] > 0) & np.isfinite(x["pred"])]
        if len(x) < 100:
            continue
        rho = (
            x[["pred"]].assign(logmv=np.log(x["mv"])).corr(method="spearman").iloc[0, 1]
        )
        values.append(float(rho))
    return {
        "split": split,
        "dates": len(values),
        "mean_spearman": float(np.mean(values)) if values else None,
        "median_spearman": float(np.median(values)) if values else None,
        "min_spearman": float(np.min(values)) if values else None,
        "max_spearman": float(np.max(values)) if values else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--split", default="test", choices=["train", "valid", "test"])
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    result = size_exposure(args.pred, args.split, args.sample)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
