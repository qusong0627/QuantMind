"""全市场多分类模型的实盘口径回测：扣费 / 可交易性 / 风格中性化。

与 ``analyze.py`` 的区别：
- 按**次日收盘成交**（信号 T → 成交 T+1），收益取 T+1 起算的前视收益；
- 剔除 ST（PIT）、成交额不足、涨停（面板已剔）；
- 可选行业/市值/波动中性化（逐日 OLS 残差）；
- 换手缓冲（top 20% 内继续持有，只换出跌出 20% 的）；
- 成本：双边佣金 2.5bp + 双边滑点 5bp + 卖出印花税 5bp ≈ 单边换手 20bp。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.backtest --horizon 1
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
ST_DIR = Path("/data/quantdb/factor_research/st_pit_cache")
INDUSTRY_PATH = Path("/data/market_regime/external/sw_l1_membership_pit.parquet")

SPLIT_BOUNDS = {
    "valid": ("2024-06-03", "2025-06-30"),
    "test": ("2025-07-01", "2026-06-30"),
    "oos": ("2026-07-01", "2026-11-05"),
}
MIN_AMOUNT_WAN = 5000.0  # 组合可投标的成交额下限（万元）
COST_RATE = 0.0020  # 单位换手成本
TOP_FRAC = 0.10
BUFFER_FRAC = 0.20


def _digits(value: str) -> str:
    code = str(value)
    if "." in code:
        code = code.split(".", 1)[0]
    return code[-6:]


def load_panel(horizon: int, features: list[str], split: str) -> pd.DataFrame:
    start, end = SPLIT_BOUNDS[split]
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


def load_extras(start: str, end: str) -> pd.DataFrame:
    frames = []
    for part in sorted(FACTOR_DIR.glob("dt=*/data.parquet")):
        date = pd.to_datetime(part.parent.name.split("=")[1])
        if not (pd.Timestamp(start) <= date <= pd.Timestamp(end)):
            continue
        frame = pq.read_table(
            part, columns=["symbol", "amount", "fun_mv_rank", "vol_std_20"]
        ).to_pandas()
        frame["date"] = date
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_industry() -> pd.DataFrame:
    ind = pq.read_table(INDUSTRY_PATH).to_pandas()
    ind["symbol_key"] = ind["symbol"].astype(str).map(_digits)
    ind["start_date"] = pd.to_datetime(ind["start_date"]).astype("datetime64[ns]")
    ind = ind.sort_values("start_date")
    return ind[["symbol_key", "start_date", "industry_l1"]]


def load_st() -> pd.DataFrame:
    frames = []
    for path in ST_DIR.glob("*.parquet"):
        if pq.read_metadata(path).num_rows == 0:
            continue
        frame = pq.read_table(path, columns=["symbol", "time", "is_st"]).to_pandas()
        frame = frame[frame["is_st"].astype(bool)]
        if frame.empty:
            continue
        frame["date"] = pd.to_datetime(
            frame["time"].astype(str), format="%Y%m%d"
        ).astype("datetime64[ns]")
        frame["symbol_key"] = frame["symbol"].astype(str).map(_digits)
        frames.append(frame[["symbol_key", "date"]])
    if not frames:
        return pd.DataFrame(columns=["symbol_key", "date"])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def attach_context(frame: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"]).astype("datetime64[ns]")
    frame["symbol_key"] = frame["symbol"].astype(str).map(_digits)
    extras = load_extras(
        str(frame["date"].min().date()), str(frame["date"].max().date())
    )
    extras["symbol_key"] = extras["symbol"].astype(str).map(_digits)
    frame = frame.merge(
        extras[["date", "symbol_key", "amount", "fun_mv_rank", "vol_std_20"]],
        on=["date", "symbol_key"],
        how="left",
    )
    ind = load_industry()
    frame = frame.sort_values("date")
    frame = pd.merge_asof(
        frame,
        ind.rename(columns={"start_date": "date"}),
        on="date",
        by="symbol_key",
        direction="backward",
    )
    st = load_st()
    st["is_st"] = True
    frame = frame.merge(st, on=["date", "symbol_key"], how="left")
    frame["is_st"] = frame["is_st"].fillna(False)

    # 次日收盘成交：signal T 的收益 = 同一只票在下一个交易日的 fwd_ret
    dates = np.sort(frame["date"].unique())
    next_map = {dates[i]: dates[i + 1] for i in range(len(dates) - 1)}
    frame["next_date"] = frame["date"].map(next_map)
    nxt = frame[["symbol", "date", "fwd_ret"]].rename(
        columns={"date": "next_date", "fwd_ret": "next_close_ret"}
    )
    frame = frame.merge(nxt, on=["symbol", "next_date"], how="left")

    # 次日开盘成交：exec_ret_h = close[T+h]/open[T+1]-1（已是执行口径，无需再移位）
    exec_path = ROOT / f"exec_ret_h{horizon}.parquet"
    if exec_path.exists():
        ex = pq.read_table(exec_path).to_pandas()
        ex.index = pd.to_datetime(ex.index)
        ex = ex.loc[ex.index.isin(np.sort(frame["date"].unique()))]
        ex = ex.stack().rename("exec_open_ret").reset_index()
        ex.columns = ["date", "symbol", "exec_open_ret"]
        ex["date"] = pd.to_datetime(ex["date"]).astype("datetime64[ns]")
        frame = frame.merge(ex, on=["date", "symbol"], how="left")
    return frame.reset_index(drop=True)


def neutralize(frame: pd.DataFrame, score_col: str) -> pd.Series:
    """逐日截面 OLS：score ~ cap + cap² + vol + 行业哑变量，取残差。"""
    out = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby("date", sort=False):
        s = group[score_col].to_numpy(float)
        cap = group["fun_mv_rank"].to_numpy(float)
        vol = group["vol_std_20"].to_numpy(float)
        ind = group["industry_l1"].astype(str).to_numpy()
        ok = np.isfinite(s) & np.isfinite(cap) & np.isfinite(vol)
        if ok.sum() < 200:
            continue
        cap_v = np.clip(cap[ok], 1e-4, 1 - 1e-4)
        design = np.column_stack([np.ones(ok.sum()), cap_v, cap_v**2, vol[ok]]).astype(
            float
        )
        dummies = pd.get_dummies(pd.Series(ind[ok]), drop_first=True).to_numpy(float)
        design = np.column_stack([design, dummies])
        try:
            beta, *_ = np.linalg.lstsq(design, s[ok], rcond=None)
        except np.linalg.LinAlgError:
            continue
        out.loc[group.index[ok]] = s[ok] - design @ beta
    return out


def portfolio(
    groups: dict,
    score_col: str,
    ret_col: str,
    top_frac: float,
    buffer_frac: float,
    cost_rate: float,
    step: int,
) -> dict:
    dates = sorted(groups)
    sampled = dates[:: max(step, 1)]
    prev: set[str] = set()
    recs = []
    for date in sampled:
        group = groups[date]
        group = group[np.isfinite(group[score_col].to_numpy(float))]
        if len(group) < 200:
            continue
        s = group[score_col]
        enter = set(group.loc[s >= s.quantile(1 - top_frac), "symbol"])
        hold = set(group.loc[s >= s.quantile(1 - buffer_frac), "symbol"])
        holdings = (prev & hold) | enter
        if not holdings:
            prev = set()
            continue
        picked = group[group["symbol"].isin(holdings)]
        ret = picked[ret_col].to_numpy(float)
        ok = np.isfinite(ret)
        if ok.sum() < 20:
            prev = holdings
            continue
        port_ret = float(np.nanmean(ret[ok]))
        bench_ret = float(np.nanmean(group[ret_col].to_numpy(float)))
        one_way_turnover = (
            len(holdings.symmetric_difference(prev)) / 2.0 / max(len(holdings), 1)
            if prev
            else 1.0
        )
        recs.append(
            {
                "date": date,
                "port_ret": port_ret,
                "bench_ret": bench_ret,
                "turnover": one_way_turnover,
                "n": len(holdings),
            }
        )
        prev = holdings
    if not recs:
        return {}
    df = pd.DataFrame(recs).sort_values("date").reset_index(drop=True)
    df["cost"] = df["turnover"] * cost_rate
    df["net_ret"] = df["port_ret"] - df["cost"]
    df["excess_gross"] = df["port_ret"] - df["bench_ret"]
    df["excess_net"] = df["net_ret"] - df["bench_ret"]
    df["equity_net"] = (1 + df["net_ret"]).cumprod()
    df["equity_bench"] = (1 + df["bench_ret"]).cumprod()
    peak = np.maximum.accumulate(df["equity_net"])
    n = len(df)
    periods_per_year = 252.0 / max(step, 1)
    return {
        "n_periods": int(n),
        "total_net": round(float(df["equity_net"].iloc[-1] - 1), 4),
        "total_bench": round(float(df["equity_bench"].iloc[-1] - 1), 4),
        "excess_gross_ann": round(
            float(df["excess_gross"].mean() * periods_per_year), 4
        ),
        "excess_net_ann": round(float(df["excess_net"].mean() * periods_per_year), 4),
        "excess_net_ir": round(
            float(
                df["excess_net"].mean()
                / df["excess_net"].std()
                * np.sqrt(periods_per_year)
            ),
            3,
        )
        if df["excess_net"].std() > 0
        else float("nan"),
        "excess_net_win": round(float((df["excess_net"] > 0).mean()), 3),
        "max_drawdown": round(float(np.min(df["equity_net"] / peak - 1)), 4),
        "turnover_period": round(float(df["turnover"].mean()), 4),
        "cost_ann": round(float(df["cost"].mean() * periods_per_year), 4),
        "avg_holdings": round(float(df["n"].mean()), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=1)
    parser.add_argument("--splits", default="test,oos")
    parser.add_argument("--cost", type=float, default=COST_RATE)
    args = parser.parse_args()

    with (ROOT / "models" / f"lgb_h{args.horizon}.pkl").open("rb") as fh:
        model = pickle.load(fh)
    features, booster, mu = model["features"], model["booster"], model["mu"]
    step = max(args.horizon, 1)

    out: dict = {"horizon": args.horizon, "cost_rate": args.cost, "splits": {}}
    for split in [s.strip() for s in args.splits.split(",") if s.strip()]:
        frame = load_panel(args.horizon, features, split)
        if frame.empty:
            continue
        proba = booster.predict(frame[features].to_numpy("float32"))
        frame["exp_ret"] = proba @ mu
        frame = attach_context(frame, args.horizon)
        frame = frame[
            (frame["amount"] >= MIN_AMOUNT_WAN)
            & (~frame["is_st"].astype(bool))
            & np.isfinite(frame["exec_open_ret"])
        ].copy()
        frame["neutral_score"] = neutralize(frame, "exp_ret")
        groups = {d: g for d, g in frame.groupby("date", sort=True)}  # noqa: C416

        results = {
            "open_gross": portfolio(
                groups, "exp_ret", "exec_open_ret", TOP_FRAC, BUFFER_FRAC, 0.0, step
            ),
            "open_costed": portfolio(
                groups,
                "exp_ret",
                "exec_open_ret",
                TOP_FRAC,
                BUFFER_FRAC,
                args.cost,
                step,
            ),
            "open_neutral": portfolio(
                groups,
                "neutral_score",
                "exec_open_ret",
                TOP_FRAC,
                BUFFER_FRAC,
                args.cost,
                step,
            ),
            "close_costed": portfolio(
                groups,
                "exp_ret",
                "next_close_ret",
                TOP_FRAC,
                BUFFER_FRAC,
                args.cost,
                step,
            ),
            "open_slow5": portfolio(
                groups, "exp_ret", "exec_open_ret", TOP_FRAC, 0.30, args.cost, 5
            ),
            "cost_sensitivity": {
                f"{round(c * 1e4)}bp": portfolio(
                    groups,
                    "exp_ret",
                    "exec_open_ret",
                    TOP_FRAC,
                    BUFFER_FRAC,
                    c,
                    step,
                )
                for c in (0.001, 0.002, 0.004)
            },
        }
        out["splits"][split] = results
        print(f"\n=== T+{args.horizon} {split} ===")
        for label in (
            "open_gross",
            "open_costed",
            "open_neutral",
            "close_costed",
            "open_slow5",
        ):
            r = results[label]
            if r:
                print(
                    f"  {label:15s} 净超额年化={r['excess_net_ann']:+.2%} "
                    f"IR={r['excess_net_ir']:.2f} 胜率={r['excess_net_win']:.1%} "
                    f"MaxDD={r['max_drawdown']:.1%} 换手={r['turnover_period']:.1%} "
                    f"持仓={r['avg_holdings']:.0f}"
                )
        print(
            "  成本敏感性:",
            {
                k: f"{v['excess_net_ann']:+.2%}"
                for k, v in results["cost_sensitivity"].items()
                if v
            },
        )

    path = ROOT / "reports" / f"backtest_h{args.horizon}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出: {path}")


if __name__ == "__main__":
    main()
