#!/usr/bin/env python3
"""Avoid-bottom overlay 回测：宽基里剔除模型 bottom X%，等权持有剩余。

与 strict_backtest 的区别：不买 TopN，而是在一个可投资的宽基（按市值 TopN 的
当期可交易股票）里**剔除模型最差的 X%**，持有剩余全部。目的是检验模型的
「避雷/空头腿」alpha 能否在低换手长多组合里兑现。

口径：
- t 日收盘出信号 → t+1 开盘等权买入 → 持有 rebalance 个交易日后收盘卖。
- 剔除 ST/停牌/次新/开盘涨停/成交额不足；市值 TopN 用当期 valuation 快照（PIT）。
- 组合与「同宽基等权」对比，超额 = 组合毛收益 − 宽基毛收益；成本两边同扣。
- 2025 valid 调参；2026 test 只跑最终一组，避免在测试集上搜参。

用法（容器内）：
    python backend/scripts/model_eval/overlay_backtest.py \
        --pred /data/training_jobs/<run_id>/pred.parquet \
        --split valid --universe-top 300 --exclude-bottom 0.2 --rebalance 20 \
        --out-dir /data/model_eval/overlay_t5_valid_u300_x20
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.model_eval.strict_backtest import (
    exit_return,
    load_benchmark,
    load_daily,
    thr,
    to_suffix,
)

MIN_AMOUNT_WAN = 1000.0
MIN_UNIVERSE = 100


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


def _st_set(st_path: str) -> dict[str, set[str]]:
    st = pd.read_parquet(st_path, columns=["symbol", "time", "is_st"])
    st = st[st["is_st"].fillna(False).astype(bool)].copy()
    st["date"] = st["time"].astype(str)
    st["sym"] = st["symbol"].astype(str).map(to_suffix)
    out: dict[str, set[str]] = {}
    for row in st[["date", "sym"]].itertuples(index=False):
        out.setdefault(row.date, set()).add(row.sym)
    return out


def split_by_pred(
    frame: pd.DataFrame, exclude_bottom: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """pred 越大越好：返回 (持有前 1-x, 剔除尾部 x)。

    2026-11-10 首版把方向写反了（剔除头部、持有尾部），此函数单独锁住方向。
    """
    ordered = frame.sort_values("pred", ascending=False).reset_index(drop=True)
    n_bottom = int(math.floor(len(ordered) * float(exclude_bottom)))
    if n_bottom <= 0:
        return ordered, ordered.iloc[0:0]
    return ordered.iloc[: len(ordered) - n_bottom], ordered.iloc[
        len(ordered) - n_bottom :
    ]


def _periods(
    pred: pd.DataFrame,
    split: str,
    dly: pd.DataFrame,
    px: dict,
    cal: list[str],
    cidx: dict,
    st_by_date: dict[str, set[str]],
    universe_top: int,
    exclude_bottom: float,
    rebalance: int,
) -> pd.DataFrame:
    data = pred[pred["split"] == split].copy()
    data["date"] = pd.to_datetime(data["trade_date"]).dt.strftime("%Y%m%d")
    data["sym"] = data["symbol"].astype(str).map(to_suffix)
    signal_dates = sorted(data["date"].unique())
    dates = signal_dates[:: max(1, int(rebalance))]
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    entry = dly[["date", "symbol", "open", "prev_close", "vol", "amt", "age"]].rename(
        columns={
            "date": "entry_date",
            "symbol": "sym",
            "open": "entry_open",
            "prev_close": "entry_prev",
            "vol": "entry_vol",
            "amt": "entry_amt",
            "age": "entry_age",
        }
    )
    rows = []
    for date in dates:
        entry_date = nxt.get(date)
        if not entry_date or entry_date not in cidx:
            continue
        exit_idx = cidx[entry_date] + int(rebalance)
        if exit_idx >= len(cal):
            break
        exit_date = cal[exit_idx]
        pred_t = data[data["date"] == date][["sym", "pred"]].drop_duplicates("sym")
        frame = pred_t.merge(
            entry[entry["entry_date"] == entry_date], on="sym", how="inner"
        )
        if frame.empty:
            continue
        frame["pred"] = pd.to_numeric(frame["pred"], errors="coerce")
        frame = frame[np.isfinite(frame["pred"])].copy()
        if frame.empty:
            continue
        mv = _load_mv(date)
        frame["mv"] = frame["sym"].map(mv)
        st_set = st_by_date.get(entry_date, set())
        tradable = (
            frame["entry_open"].notna()
            & (frame["entry_vol"].fillna(0) > 0)
            & (frame["entry_age"].fillna(0) >= 60)
            & (frame["entry_amt"].fillna(0) >= MIN_AMOUNT_WAN)
            & frame["mv"].notna()
            & ~frame["sym"].isin(st_set)
        )
        entry_chg = frame["entry_open"] / frame["entry_prev"] - 1.0
        limit_up = entry_chg >= frame["sym"].map(thr) - 0.003
        frame = frame[tradable & ~limit_up].copy()
        if len(frame) < MIN_UNIVERSE:
            continue
        if int(universe_top) > 0:
            frame = frame.nlargest(int(universe_top), "mv").copy()
        if len(frame) < MIN_UNIVERSE:
            continue
        hold, bottom = split_by_pred(frame, exclude_bottom)
        if len(hold) < 50:
            continue
        hold_rets = [
            r
            for r in (
                exit_return(
                    px, cal, cidx, entry_date, sym, open_, horizon=int(rebalance)
                )[0]
                for sym, open_ in zip(hold["sym"], hold["entry_open"], strict=True)
            )
            if np.isfinite(r)
        ]
        univ_rets = [
            r
            for r in (
                exit_return(
                    px, cal, cidx, entry_date, sym, open_, horizon=int(rebalance)
                )[0]
                for sym, open_ in zip(frame["sym"], frame["entry_open"], strict=True)
            )
            if np.isfinite(r)
        ]
        bottom_rets = [
            r
            for r in (
                exit_return(
                    px, cal, cidx, entry_date, sym, open_, horizon=int(rebalance)
                )[0]
                for sym, open_ in zip(bottom["sym"], bottom["entry_open"], strict=True)
            )
            if np.isfinite(r)
        ]
        if len(hold_rets) < 30 or len(univ_rets) < 30:
            continue
        rows.append(
            {
                "date": date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "n_universe": len(frame),
                "n_hold": len(hold),
                "port": float(np.mean(hold_rets)),
                "universe": float(np.mean(univ_rets)),
                "bottom": float(np.mean(bottom_rets)) if bottom_rets else np.nan,
                "excess": float(np.mean(hold_rets) - np.mean(univ_rets)),
            }
        )
    return pd.DataFrame(rows)


def _metrics(periods: pd.DataFrame, cost: float, rebalance: int) -> dict:
    if periods.empty:
        return {}
    port = periods["port"].to_numpy(float) - cost
    univ = periods["universe"].to_numpy(float) - cost
    excess = periods["excess"].to_numpy(float)
    n = len(periods)
    ann_factor = 242.0 / int(rebalance)

    def _curve_stats(rets: np.ndarray) -> tuple[float, float, float]:
        eq = np.cumprod(1.0 + rets)
        ann = float(eq[-1] ** (ann_factor / n) - 1.0) if n else float("nan")
        mdd = float((eq / np.maximum.accumulate(eq) - 1.0).min()) if n else float("nan")
        std = float(rets.std(ddof=1)) if n > 1 else 0.0
        sharpe = (
            float(rets.mean() / std * math.sqrt(ann_factor))
            if std > 0
            else float("nan")
        )
        return ann, mdd, sharpe

    port_ann, port_mdd, port_sharpe = _curve_stats(port)
    univ_ann, univ_mdd, univ_sharpe = _curve_stats(univ)
    ex_std = float(excess.std(ddof=1)) if n > 1 else 0.0
    ex_t = float(excess.mean() / ex_std * math.sqrt(n)) if ex_std > 0 else float("nan")
    return {
        "periods": n,
        "avg_universe": round(float(periods["n_universe"].mean()), 1),
        "avg_hold": round(float(periods["n_hold"].mean()), 1),
        "port_period_net_pct": round(float(port.mean()) * 100, 4),
        "univ_period_net_pct": round(float(univ.mean()) * 100, 4),
        "excess_period_pct": round(float(excess.mean()) * 100, 4),
        "excess_t": round(ex_t, 2),
        "excess_win_pct": round(float((excess > 0).mean()) * 100, 1),
        "bottom_period_pct": round(float(periods["bottom"].mean()) * 100, 4),
        "port_ann_pct": round(port_ann * 100, 1),
        "port_mdd_pct": round(port_mdd * 100, 1),
        "port_sharpe": round(port_sharpe, 2),
        "univ_ann_pct": round(univ_ann * 100, 1),
        "univ_mdd_pct": round(univ_mdd * 100, 1),
        "univ_sharpe": round(univ_sharpe, 2),
        "monthly_excess_pct": {
            str(k): round(float(v) * 100, 2)
            for k, v in (
                periods.set_index(pd.to_datetime(periods["date"], format="%Y%m%d"))
                .groupby(lambda x: x.to_period("M"))["excess"]
                .mean()
            ).items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--split", default="valid", choices=["valid", "test"])
    ap.add_argument("--universe-top", type=int, default=300)
    ap.add_argument("--exclude-bottom", type=float, default=0.2)
    ap.add_argument("--rebalance", type=int, default=20)
    ap.add_argument("--costs", default="0.0015,0.0025,0.0040")
    ap.add_argument(
        "--daily-dir", default="/data/quantdb/1_kline_data/daily_unadjusted"
    )
    ap.add_argument(
        "--st-path", default="/data/quantdb/2_base_sector/st_pit/st_pit.parquet"
    )
    ap.add_argument("--index-dir", default="/data/quantdb/1_kline_data/index_daily")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    costs = [float(x) for x in args.costs.split(",")]

    pred = pd.read_parquet(args.pred, columns=["symbol", "trade_date", "pred", "split"])
    split_pred = pred[pred["split"] == args.split]
    if split_pred.empty:
        raise SystemExit(f"pred 中没有 split={args.split}")
    dates = pd.to_datetime(split_pred["trade_date"]).dt.strftime("%Y%m%d")
    lo = (pd.Timestamp(dates.min()) - pd.Timedelta(days=120)).strftime("%Y%m%d")
    dly, px, cal, cidx = load_daily(args.daily_dir, lo, "20991231")
    st_by_date = _st_set(args.st_path)
    periods = _periods(
        pred,
        args.split,
        dly,
        px,
        cal,
        cidx,
        st_by_date,
        args.universe_top,
        args.exclude_bottom,
        args.rebalance,
    )
    bench = load_benchmark(args.index_dir, lo, "20991231")
    periods["bench"] = [
        float(bench.get(r.exit_date, np.nan) / bench.get(r.entry_date, np.nan) - 1.0)
        if r.entry_date in bench.index and r.exit_date in bench.index
        else np.nan
        for r in periods.itertuples(index=False)
    ]
    result = {
        "config": {
            "pred": args.pred,
            "split": args.split,
            "universe_top": args.universe_top,
            "exclude_bottom": args.exclude_bottom,
            "rebalance": args.rebalance,
        },
        "periods": periods.to_dict(orient="records"),
        "metrics": {
            f"{cost * 100:.2f}%": _metrics(periods, cost, args.rebalance)
            for cost in costs
        },
    }
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "overlay_metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    print(f"报告：{out / 'overlay_metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
