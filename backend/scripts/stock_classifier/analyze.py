"""样本外稳健性：分月 IC、市值分组 IC、非重叠多空曲线。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.analyze --horizon 5
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path("/data/stock_classifier")
FACTOR_DIR = Path("/data/quantdb/6_ml_datasets/l1_l2_factors")
SPLIT_BOUNDS = {
    "valid": ("2024-06-03", "2025-06-30"),
    "test": ("2025-07-01", "2026-06-30"),
    "oos": ("2026-07-01", "2026-11-05"),
}


def load_model(horizon: int) -> dict:
    with (ROOT / "models" / f"lgb_h{horizon}.pkl").open("rb") as fh:
        return pickle.load(fh)


def load_split(horizon: int, features: list[str], name: str) -> pd.DataFrame:
    start, end = SPLIT_BOUNDS[name]
    frames = []
    for year in range(2024, 2027):
        path = ROOT / f"panel_h{horizon}" / f"year={year}" / "data.parquet"
        if not path.exists():
            continue
        frame = pq.read_table(path).to_pandas()
        mask = (frame["date"] >= pd.Timestamp(start)) & (
            frame["date"] <= pd.Timestamp(end)
        )
        if mask.any():
            frames.append(
                frame.loc[mask, ["date", "symbol", "fwd_ret", "y", *features]]
            )
        del frame
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_cap(start: str, end: str) -> pd.DataFrame:
    """读 (date, symbol, fun_mv_rank) 用于市值分组。"""
    frames = []
    for part in sorted(FACTOR_DIR.glob("dt=*/data.parquet")):
        date = pd.to_datetime(part.parent.name.split("=")[1])
        if not (pd.Timestamp(start) <= date <= pd.Timestamp(end)):
            continue
        frame = pq.read_table(part, columns=["symbol", "fun_mv_rank"]).to_pandas()
        frame["date"] = date
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def daily_ic_series(frame: pd.DataFrame, score_col: str) -> pd.Series:
    from backend.scripts.stock_classifier.panel import rankdata_nan

    rows = []
    for date, group in frame.groupby("date", sort=True):
        s = group[score_col].to_numpy(float)
        r = group["fwd_ret"].to_numpy(float)
        ok = np.isfinite(s) & np.isfinite(r)
        if ok.sum() < 100:
            continue
        sr = rankdata_nan(s[ok])
        rr = rankdata_nan(r[ok])
        sd = sr - sr.mean()
        rd = rr - rr.mean()
        denom = np.sqrt((sd * sd).sum() * (rd * rd).sum())
        rows.append((date, float((sd * rd).sum() / denom) if denom > 0 else np.nan))
    return pd.Series(dict(rows)).sort_index()


def long_short(frame: pd.DataFrame, horizon: int, n_bins: int = 10) -> dict:
    """每 horizon 天取一个非重叠截面，做多头最高分箱 / 空头最低分箱。"""
    dates = np.sort(frame["date"].unique())
    sampled = dates[:: max(horizon, 1)]
    rets = []
    long_excess = []
    for date in sampled:
        group = frame[frame["date"] == date]
        if len(group) < 200:
            continue
        s = group["exp_ret"].to_numpy(float)
        r = group["fwd_ret"].to_numpy(float)
        ok = np.isfinite(s) & np.isfinite(r)
        if ok.sum() < 200:
            continue
        s, r = s[ok], r[ok]
        ranks = pd.Series(s).rank(method="first").to_numpy()
        bins = np.clip((ranks - 1) * n_bins // len(s), 0, n_bins - 1).astype(int)
        rets.append(float(r[bins == n_bins - 1].mean() - r[bins == 0].mean()))
        long_excess.append(float(r[bins == n_bins - 1].mean() - r.mean()))
    if not rets:
        return {}
    arr = np.array(rets)
    equity = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(equity)
    le = np.array(long_excess)
    le_equity = np.cumprod(1 + le)
    le_peak = np.maximum.accumulate(le_equity)
    return {
        "n_periods": int(len(arr)),
        "mean_per_period": round(float(arr.mean()), 5),
        "win_rate": round(float((arr > 0).mean()), 3),
        "total": round(float(equity[-1] - 1), 4),
        "max_drawdown": round(float(np.min(equity / peak - 1)), 4),
        "ir_annualized": round(
            float(arr.mean() / arr.std() * np.sqrt(252 / max(horizon, 1))), 3
        )
        if arr.std() > 0
        else float("nan"),
        "long_only_excess_mean": round(float(le.mean()), 5),
        "long_only_excess_total": round(float(le_equity[-1] - 1), 4),
        "long_only_excess_win_rate": round(float((le > 0).mean()), 3),
        "long_only_excess_maxdd": round(float(np.min(le_equity / le_peak - 1)), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--splits", default="test,oos")
    args = parser.parse_args()
    model = load_model(args.horizon)
    features, booster, mu = model["features"], model["booster"], model["mu"]
    out: dict = {"horizon": args.horizon, "splits": {}}

    cap = load_cap("2024-06-03", "2026-11-05")
    for name in [s.strip() for s in args.splits.split(",") if s.strip()]:
        frame = load_split(args.horizon, features, name)
        if frame.empty:
            continue
        proba = booster.predict(frame[features].to_numpy("float32"))
        frame["exp_ret"] = proba @ mu
        frame["p_max"] = proba.max(axis=1)
        merged = frame.merge(cap, on=["date", "symbol"], how="left")

        ic = daily_ic_series(merged, "exp_ret")
        monthly = ic.groupby(pd.to_datetime(ic.index).to_period("M")).mean()
        merged["cap_q"] = pd.qcut(
            merged["fun_mv_rank"], 5, labels=False, duplicates="drop"
        )
        by_cap = {}
        for q, group in merged.groupby("cap_q"):
            series = daily_ic_series(group, "exp_ret")
            by_cap[int(q)] = {
                "n_days": int(len(series)),
                "ic_mean": round(float(series.mean()), 4),
                "ic_ir": round(float(series.mean() / series.std()), 3)
                if series.std() > 0
                else None,
            }
        out["splits"][name] = {
            "ic_mean": round(float(ic.mean()), 4),
            "ic_ir": round(float(ic.mean() / ic.std()), 3),
            "ic_pos_share": round(float((ic > 0).mean()), 3),
            "monthly_ic": {str(k): round(float(v), 4) for k, v in monthly.items()},
            "by_cap_quintile": by_cap,
            "long_short": long_short(merged, args.horizon),
        }
        print(f"\n=== T+{args.horizon} {name} ===")
        print("  月度 IC:", out["splits"][name]["monthly_ic"])
        print("  市值分组:", out["splits"][name]["by_cap_quintile"])
        print("  多空:", out["splits"][name]["long_short"])

    path = ROOT / "reports" / f"robustness_h{args.horizon}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n写出:", path)


if __name__ == "__main__":
    main()
