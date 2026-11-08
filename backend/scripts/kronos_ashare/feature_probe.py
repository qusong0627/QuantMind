"""Kronos 特征的增量检验：残差 RankIC（对既有因子正交后还剩多少）。

为什么要残差 IC：一个信号自己 IC 再高，只要它和现有因子高度相关，加进模型就是重复信息。
本模块把 Kronos 特征对「20 日动量」做秩空间残差化，再看残差与未来收益的秩相关。

用法（容器内）：
  python3 -m backend.scripts.kronos_ashare.feature_probe \
    --features /data/kronos_ashare/features/base_csi300_daily.parquet \
    --start 2024-06-03 --end 2026-06-30 --horizon 5 \
    --output /data/kronos_ashare/probe_base_h5.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.kronos_ashare.data import (
    init_qlib,
    load_panel,
    trading_calendar,
)
from backend.scripts.stock_classifier.panel import rankdata_nan

log = logging.getLogger("kronos_ashare.feature_probe")

FEATURE_COLUMNS = (
    "k_score_mean",
    "k_score_end",
    "k_path_vol",
    "k_path_range",
    "k_path_slope",
)


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    xd = x - x.mean()
    yd = y - y.mean()
    denom = float(np.sqrt((xd * xd).sum() * (yd * yd).sum()))
    return float((xd * yd).sum() / denom) if denom > 0 else float("nan")


def _rank_residualize(x: np.ndarray, z: np.ndarray) -> np.ndarray:
    """把 x 的秩对 z 的秩做线性回归，返回残差。"""
    rx = rankdata_nan(x)
    rz = rankdata_nan(z)
    ok = np.isfinite(rx) & np.isfinite(rz)
    out = np.full(len(x), np.nan)
    if ok.sum() < 3:
        return out
    zz = rz[ok] - rz[ok].mean()
    denom = float((zz * zz).sum())
    if denom <= 0:
        return out
    beta = float(((rx[ok] - rx[ok].mean()) * zz).sum() / denom)
    out[ok] = (rx[ok] - rx[ok].mean()) - beta * zz
    return out


def partial_rank_ic(scores: np.ndarray, fwd: np.ndarray, control: np.ndarray) -> float:
    """控制 ``control`` 后的秩相关（秩残差 + Pearson = 部分 Spearman）。"""
    resid = _rank_residualize(scores, control)
    ry = rankdata_nan(fwd)
    ok = np.isfinite(resid) & np.isfinite(ry)
    if ok.sum() < 20:
        return float("nan")
    return _pearson(resid[ok], ry[ok])


def _summarize(values: list[float]) -> dict[str, float]:
    arr = np.array([v for v in values if np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return {"n": 0.0, "mean": float("nan"), "std": float("nan"), "icir": float("nan"), "hit": float("nan")}
    std = float(arr.std(ddof=1)) if len(arr) > 1 else float("nan")
    return {
        "n": float(len(arr)),
        "mean": float(arr.mean()),
        "std": std,
        "icir": float(arr.mean() / std * np.sqrt(len(arr))) if std and np.isfinite(std) else float("nan"),
        "hit": float((arr > 0).mean()),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kronos 特征残差 IC 检验")
    p.add_argument("--features", required=True)
    p.add_argument("--qlib-dir", default="/data/qlib/cn_data")
    p.add_argument("--start", default="2024-06-03")
    p.add_argument("--end", default="2026-06-30")
    p.add_argument("--horizon", type=int, default=5)
    p.add_argument("--control-lookback", type=int, default=20, help="控制变量：过去 N 日动量")
    p.add_argument("--output", default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    feats = pd.read_parquet(args.features)
    feats["date"] = pd.to_datetime(feats["date"])
    feats = feats[(feats["date"] >= pd.Timestamp(args.start)) & (feats["date"] <= pd.Timestamp(args.end))]
    if feats.empty:
        log.error("empty feature file in range")
        return 2
    symbols = sorted(feats["symbol"].unique())
    dates = sorted(feats["date"].unique())
    log.info("features: %d rows, %d symbols, %d dates", len(feats), len(symbols), len(dates))

    init_qlib(args.qlib_dir)
    cal_start = (pd.Timestamp(args.start) - pd.Timedelta(days=200)).strftime("%Y-%m-%d")
    cal_end = (pd.Timestamp(args.end) + pd.Timedelta(days=200)).strftime("%Y-%m-%d")
    calendar = trading_calendar(cal_start, cal_end)
    panels = load_panel(symbols, cal_start, cal_end)
    log.info("loaded %d panels", len(panels))

    rows: list[dict[str, float | str]] = []
    for date in dates:
        day = feats[feats["date"] == date]
        pos = int(calendar.get_indexer([pd.Timestamp(date)])[0])
        if pos < 0 or pos + args.horizon >= len(calendar):
            continue
        fwd_date = calendar[pos + args.horizon]
        mom_date = calendar[pos - args.control_lookback] if pos - args.control_lookback >= 0 else None
        records = []
        for _, row in day.iterrows():
            panel = panels.get(row["symbol"])
            if panel is None or panel.empty:
                continue
            try:
                close_t = float(panel.loc[pd.Timestamp(date), "close"])
                close_f = float(panel.loc[fwd_date, "close"])
            except KeyError:
                continue
            mom = np.nan
            if mom_date is not None:
                try:
                    mom = close_t / float(panel.loc[mom_date, "close"]) - 1.0
                except KeyError:
                    mom = np.nan
            if not (np.isfinite(close_t) and np.isfinite(close_f) and close_t > 0):
                continue
            rec = {
                "symbol": row["symbol"],
                "fwd": close_f / close_t - 1.0,
                "mom": mom,
            }
            for col in FEATURE_COLUMNS:
                rec[col] = float(row[col]) if col in row and pd.notna(row[col]) else np.nan
            records.append(rec)
        if len(records) < 20:
            continue
        df = pd.DataFrame(records)
        entry: dict[str, float | str] = {"date": str(pd.Timestamp(date).date()), "n": float(len(df))}
        for col in FEATURE_COLUMNS:
            vals = df[col].to_numpy(float)
            fwd = df["fwd"].to_numpy(float)
            mom = df["mom"].to_numpy(float)
            ok = np.isfinite(vals) & np.isfinite(fwd)
            if ok.sum() >= 20:
                rx = rankdata_nan(vals[ok])
                ry = rankdata_nan(fwd[ok])
                entry[f"{col}_rank_ic"] = _pearson(rx, ry)
                entry[f"{col}_corr_mom"] = (
                    _pearson(rx, rankdata_nan(mom[ok])) if np.isfinite(mom[ok]).sum() >= 20 else np.nan
                )
                entry[f"{col}_partial_ic"] = partial_rank_ic(vals[ok], fwd[ok], mom[ok])
        rows.append(entry)

    if not rows:
        log.error("no usable cross-sections")
        return 2

    per_date = pd.DataFrame(rows)
    summary: dict[str, dict[str, float]] = {}
    for col in FEATURE_COLUMNS:
        for suffix in ("rank_ic", "partial_ic", "corr_mom"):
            key = f"{col}_{suffix}"
            if key in per_date.columns:
                summary[key] = _summarize(per_date[key].tolist())

    payload = {"config": vars(args), "summary": summary, "per_date": rows}
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
