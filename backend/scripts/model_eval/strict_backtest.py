"""严格 A 股回测器（模型训练管线的统一评估出口）。

口径（与训练标签一致）：
- t 日收盘后出信号；t+1 开盘买（execution_price=open）；t+1+5 收盘卖
- 剔除：ST（PIT）、停牌（开盘/成交量缺失）、次新 <60 交易日、开盘涨停买不进
- 卖出遇跌停/停牌顺延，最多 10 个交易日
- 成本：往返 0.15% / 0.25% / 0.40%（佣金+印花+滑点）
- 组合：TopN 等权；报「全部每日 cohort」与「非重叠 6 日周期」两套统计
- 显著性：重叠 cohort 用 Newey-West(lag=5) t 值
- 换手/成本拖累：每 6 个交易日完整换仓一次，年化换手 2×242/6 ≈ 81x

用法（容器内，cwd=/）：
  python3 backend/scripts/model_eval/strict_backtest.py \
    --pred /app/models/users/default/10000001/<model_id>/pred.parquet \
    --out-dir /data/model_eval/<model_id>

输出：
  <out-dir>/strict_report.md   人读报告
  <out-dir>/strict_metrics.json 机器可读指标
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HORIZON = 5
CYCLE_STEP = 6  # t 信号 -> t+1 买 -> t+6 卖；下一周期 t+6 信号、t+7 买


def to_suffix(s: str) -> str:
    if s.startswith("SH"):
        return s[2:] + ".SH"
    if s.startswith("SZ"):
        return s[2:] + ".SZ"
    if s.startswith("BJ"):
        return s[2:] + ".BJ"
    return s


def thr(sym: str) -> float:
    return 0.195 if sym.startswith(("30", "68")) else 0.095


def nw_t(x: np.ndarray, lag: int = HORIZON) -> float:
    """Newey-West t 值：重叠 5 日 cohort 的自相关校正。"""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    mu = x.mean()
    e = x - mu
    var = float(e @ e) / n
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float(e[k:] @ e[:-k]) / n
    return mu / np.sqrt(var / n) if var > 0 else float("nan")


def load_benchmark(
    index_dir: str, lo: str, hi: str, code: str = "000300.SH"
) -> pd.Series:
    """读取基准指数收盘价（date 字符串 → close）。"""
    files = [
        f"{index_dir}/dt={d}/data.parquet"
        for d in sorted(x.split("dt=")[1] for x in glob.glob(f"{index_dir}/dt=*"))
        if lo <= d <= hi
    ]
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")
    try:
        df = con.execute(
            f"""
            SELECT strftime(CAST(time AS DATE), '%Y%m%d') AS date,
                   CAST(close AS DOUBLE) AS close
            FROM read_parquet({files!r})
            WHERE symbol = ?
            """,
            [code],
        ).df()
    finally:
        con.close()
    if df.empty:
        raise RuntimeError(f"基准 {code} 无数据（{index_dir}）")
    return df.set_index("date")["close"].sort_index()


def load_daily(
    daily_dir: str, lo: str, hi: str
) -> tuple[pd.DataFrame, dict, list[str], dict]:
    files = [
        f"{daily_dir}/dt={d}/data.parquet"
        for d in sorted(x.split("dt=")[1] for x in glob.glob(f"{daily_dir}/dt=*"))
        if lo <= d <= hi
    ]
    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    dly = con.sql(f"""
        SELECT strftime(CAST(time AS DATE), '%Y%m%d') AS date, symbol,
               CAST(open AS DOUBLE) AS open, CAST(close AS DOUBLE) AS close,
               CAST(volume AS DOUBLE) AS vol, CAST(amount AS DOUBLE) AS amt
        FROM read_parquet({files!r})
    """).df()
    con.close()
    dly = dly.sort_values(["symbol", "date"]).reset_index(drop=True)
    dly["prev_close"] = dly.groupby("symbol")["close"].shift(1)
    first = dly.groupby("symbol")["date"].min().rename("first_date")
    dly = dly.merge(first, on="symbol", how="left")
    cal = sorted(dly["date"].unique())
    cidx = {d: i for i, d in enumerate(cal)}
    dly["di"] = dly["date"].map(cidx)
    dly["fi"] = dly["first_date"].map(cidx)
    dly["age"] = dly["di"] - dly["fi"]
    px = {
        (r.date, r.symbol): (r.open, r.close, r.prev_close, r.vol, r.amt)
        for r in dly.itertuples(index=False)
    }
    return dly, px, cal, cidx


def exit_return(
    px: dict,
    cal: list[str],
    cidx: dict,
    entry_date: str,
    sym: str,
    entry_open: float,
    horizon: int = HORIZON,
) -> tuple[float, int]:
    if entry_date not in cidx:
        return np.nan, -1
    idx = cidx[entry_date]
    t = thr(sym)
    for extra in range(0, 11):
        j = idx + horizon + extra
        if j >= len(cal):
            break
        row = px.get((cal[j], sym))
        if row is None:
            continue
        _o, c, pc, vol, _amt = row
        if (
            c is None
            or not np.isfinite(c)
            or vol is None
            or vol <= 0
            or pc is None
            or pc <= 0
        ):
            continue
        if c / pc - 1.0 <= -t + 0.003:
            continue
        return c / entry_open - 1.0, extra
    return np.nan, -1


def run(
    pred_path: str,
    daily_dir: str,
    st_path: str,
    topns: list[int],
    capitals: list[float],
    costs: list[float],
    horizon: int = HORIZON,
    benchmark_ma: int = 0,
    index_dir: str = "data/quantdb/1_kline_data/index_daily",
) -> tuple[pd.DataFrame, dict]:
    p = pd.read_parquet(pred_path, columns=["symbol", "trade_date", "pred", "split"])
    p = p[p["split"] == "test"].copy()
    p["date"] = pd.to_datetime(p["trade_date"]).dt.strftime("%Y%m%d")
    p["sym"] = p["symbol"].map(to_suffix)
    signal_dates = sorted(p["date"].unique())
    lo = (pd.Timestamp(signal_dates[0]) - pd.Timedelta(days=120)).strftime("%Y%m%d")
    dly, px, cal, cidx = load_daily(daily_dir, lo, "20991231")
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    p["entry_date"] = p["date"].map(nxt)
    p = p[p["entry_date"].notna()].copy()

    entry = dly[
        ["date", "symbol", "open", "close", "prev_close", "vol", "amt", "age"]
    ].rename(
        columns={
            "date": "entry_date",
            "symbol": "sym",
            "open": "entry_open",
            "close": "entry_close",
            "prev_close": "entry_prev",
            "vol": "entry_vol",
            "amt": "entry_amt",
            "age": "entry_age",
        }
    )

    st = pd.read_parquet(st_path, columns=["symbol", "time", "is_st"])
    st["entry_date"] = st["time"].astype(str)
    st = (
        st[st["is_st"].fillna(False).astype(bool)][["entry_date", "symbol"]]
        .drop_duplicates()
        .rename(columns={"symbol": "sym"})
    )
    st["is_st"] = True

    m = p.merge(entry, on=["entry_date", "sym"], how="left")
    m = m.merge(st, on=["entry_date", "sym"], how="left")
    m["is_st"] = m["is_st"].fillna(False)
    m["thr"] = m["sym"].map(thr)
    m["entry_chg"] = m["entry_open"] / m["entry_prev"] - 1.0
    m["limit_up_open"] = m["entry_chg"] >= m["thr"] - 0.003
    m["is_susp"] = m["entry_open"].isna() | (m["entry_vol"].fillna(0) <= 0)
    m["is_new"] = m["entry_age"].fillna(0) < 60
    m["tradable"] = (
        (~m["is_st"]) & (~m["limit_up_open"]) & (~m["is_susp"]) & (~m["is_new"])
    )

    exclusions = {
        "rows": int(len(m)),
        "tradable_pct": round(float(m["tradable"].mean()) * 100, 2),
        "st": int(m["is_st"].sum()),
        "limit_up_open": int(m["limit_up_open"].sum()),
        "suspended": int(m["is_susp"].sum()),
        "new": int(m["is_new"].sum()),
        "signal_days": len(signal_dates),
    }

    filter_info: dict = {"benchmark": "000300.SH", "ma": benchmark_ma}
    if benchmark_ma > 0:
        bench = load_benchmark(index_dir, lo, "20991231")
        bench_ma = bench.rolling(benchmark_ma).mean()
        allowed = set(bench.index[(bench > bench_ma) & bench_ma.notna()])
        total_days = int(m["date"].nunique())
        m = m[m["date"].isin(allowed)].copy()
        kept_days = int(m["date"].nunique())
        filter_info.update(
            {
                "total_days": total_days,
                "kept_days": kept_days,
                "kept_pct": round(kept_days / max(total_days, 1) * 100, 1),
            }
        )

    records: dict[int, pd.DataFrame] = {}
    for N in topns:
        sel = (
            m[m["tradable"]]
            .sort_values(["date", "pred"], ascending=[True, False])
            .groupby("date")
            .head(N)
        )
        recs = []
        for r in sel.itertuples(index=False):
            ret, extra = exit_return(
                px, cal, cidx, r.entry_date, r.sym, r.entry_open, horizon
            )
            if np.isfinite(ret):
                recs.append((r.date, ret, extra, r.entry_amt))
        records[N] = pd.DataFrame(recs, columns=["date", "ret", "extra", "amt"])

    metrics: dict = {
        "exclusions": exclusions,
        "filter": filter_info,
        "topn": {},
        "costs": {},
    }
    for cost in costs:
        ckey = f"{cost * 100:.2f}%"
        metrics["costs"][ckey] = {}
        for N in topns:
            d = records[N].copy()
            if d.empty:
                continue
            d["net"] = d.ret - cost
            coh = (
                d.groupby("date")
                .agg(n=("net", "size"), net=("net", "mean"), amt=("amt", "median"))
                .reset_index()
                .sort_values("date")
            )
            cycle_step = max(2, int(horizon) + 1)
            cyc = coh.iloc[::cycle_step]
            eq = (1 + cyc.net).cumprod()
            ann = (
                eq.iloc[-1] ** (242 / (cycle_step * len(cyc))) - 1
                if len(cyc)
                else np.nan
            )
            mdd = float((eq / eq.cummax() - 1).min()) if len(eq) else np.nan
            sharpe = (
                float(cyc.net.mean() / cyc.net.std() * np.sqrt(242 / cycle_step))
                if cyc.net.std() > 0
                else np.nan
            )
            monthly = (
                1 + cyc.set_index(pd.to_datetime(cyc.date, format="%Y%m%d")).net
            ).groupby(lambda x: x.to_period("M")).prod() - 1
            row = {
                "N": N,
                "days": int(len(coh)),
                "avg_picks": round(float(coh.n.mean()), 1),
                "net_5d_pct": round(float(coh.net.mean()) * 100, 4),
                "nw_t": round(float(nw_t(coh.net.to_numpy(), lag=horizon)), 2),
                "win_pct": round(float((coh.net > 0).mean()) * 100, 1),
                "cycles": int(len(cyc)),
                "ann_pct": round(float(ann) * 100, 1),
                "mdd_pct": round(float(mdd) * 100, 1),
                "sharpe": round(float(sharpe), 2),
                "turnover_x": round(2.0 * 242 / max(2, int(horizon) + 1), 0),
                "cost_drag_pct_yr": round(
                    float(cost) * 242 / max(2, int(horizon) + 1) * 100, 1
                ),
                "amt_med_wan": round(float(coh.amt.median()), 0),
                "monthly_net_pct": {
                    str(k): round(float(v) * 100, 2) for k, v in monthly.items()
                },
                "capitals": {},
            }
            for cap in capitals:
                row["capitals"][f"{cap:.0f}万"] = {
                    "pnl_per_cycle_wan": round(float(cap * cyc.net.mean()), 2),
                    "cum_fixed_wan": round(float(cap * cyc.net.sum()), 1),
                    "pos_per_stock_wan": round(float(cap / N), 2),
                    "participation_med_pct": round(
                        float((cap / N) / coh.amt.median() * 100), 3
                    ),
                }
            metrics["costs"][ckey][f"Top{N}"] = row
    return metrics


def render_report(model_id: str, metrics: dict, horizon: int = HORIZON) -> str:
    ex = metrics["exclusions"]
    lines = [
        f"# 严格回测报告 · {model_id}",
        "",
        f"> t 信号 → t+1 开盘买 → t+1+{horizon} 收盘卖；剔除 ST/停牌/次新/开盘涨停；跌停/停牌顺延；Newey-West(lag={horizon})。",
        "",
        "## 样本与剔除",
        "",
        f"- 信号日：{ex['signal_days']}；可交易占比：{ex['tradable_pct']}%",
        f"- 剔除：ST {ex['st']} / 开盘涨停 {ex['limit_up_open']} / 停牌 {ex['suspended']} / 次新 {ex['new']}",
        "",
    ]
    filt = metrics.get("filter") or {}
    if filt.get("ma"):
        lines.insert(
            -1,
            f"- 沪深300 MA{filt['ma']} 过滤：保留 {filt.get('kept_days')}/{filt.get('total_days')}"
            f" 个信号日（{filt.get('kept_pct')}%）",
        )
    for cost, rows in metrics["costs"].items():
        lines += [
            f"## 成本 {cost}",
            "",
            "| 组合 | 5日净 | NW t | 胜率 | 年化 | MDD | 夏普 | 年化换手 | 成本拖累 | 成交额中位 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for k, r in rows.items():
            lines.append(
                f"| {k} | {r['net_5d_pct']:+.3f}% | {r['nw_t']:+.2f} | {r['win_pct']:.1f}% | "
                f"{r['ann_pct']:+.1f}% | {r['mdd_pct']:.1f}% | {r['sharpe']:.2f} | {r['turnover_x']:.0f}x | "
                f"{r['cost_drag_pct_yr']:.1f}%/年 | {r['amt_med_wan']:.0f}万 |"
            )
        lines.append("")
        for k, r in rows.items():
            caps = r.get("capitals") or {}
            for cname, c in caps.items():
                lines.append(
                    f"- {k} · {cname}：每周期 {c['pnl_per_cycle_wan']:+.2f}万，累计(不复利) {c['cum_fixed_wan']:+.1f}万，"
                    f"单票 {c['pos_per_stock_wan']:.1f}万，参与率中位 {c['participation_med_pct']:.3f}%"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True, help="pred.parquet 路径")
    ap.add_argument("--model-id", default="")
    ap.add_argument("--daily-dir", default="data/quantdb/1_kline_data/daily_unadjusted")
    ap.add_argument(
        "--st-path", default="data/quantdb/2_base_sector/st_pit/st_pit.parquet"
    )
    ap.add_argument("--topn", default="20,50,100")
    ap.add_argument("--capital", default="100,200")
    ap.add_argument("--costs", default="0.0015,0.0025,0.0040")
    ap.add_argument("--out-dir", default="")
    ap.add_argument(
        "--horizon",
        type=int,
        default=5,
        help="持有交易日（与训练 target_horizon_days 一致）",
    )
    ap.add_argument(
        "--benchmark-ma", type=int, default=0, help="沪深300 MA 过滤；0=不过滤"
    )
    ap.add_argument(
        "--index-dir",
        default="data/quantdb/1_kline_data/index_daily",
        help="指数日线目录（沪深300=000300.SH）",
    )
    args = ap.parse_args()
    topns = [int(x) for x in args.topn.split(",")]
    capitals = [float(x) for x in args.capital.split(",")]
    costs = [float(x) for x in args.costs.split(",")]
    model_id = args.model_id or Path(args.pred).parent.name
    metrics = run(
        args.pred,
        args.daily_dir,
        args.st_path,
        topns,
        capitals,
        costs,
        horizon=args.horizon,
        benchmark_ma=args.benchmark_ma,
        index_dir=args.index_dir,
    )
    out = Path(args.out_dir or f"data/model_eval/{model_id}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "strict_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "strict_report.md").write_text(
        render_report(model_id, metrics, horizon=args.horizon), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: {kk: vv["net_5d_pct"] for kk, vv in v.items()}
                for k, v in metrics["costs"].items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"报告：{out / 'strict_report.md'}")


if __name__ == "__main__":
    main()
