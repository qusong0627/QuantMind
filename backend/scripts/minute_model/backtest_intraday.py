"""盘中 4h 买入模型的完整回测（14:50 买 Top20 -> 次日开盘卖，带 regime 过滤）。

## 口径

- 样本外区间：2025-07-01 ~ 2026-10-30（模型训练截止 2025-06-30）
- 每日 14:50：面板特征 -> 买入模型打分 -> 取 Top20（tradable 且 label_ok）
- 入场：当日 14:50 bar 收盘价（px）
- 出场：次日 09:31 bar 收盘价（开盘代理）；对照「持有到次日 14:50」
- 组合：等权 20 只，日度再平衡；不触发 regime 过滤则空仓
- 成本：买 0.015% 佣金 + 卖 0.015% 佣金 + 0.05% 印花税 + 滑点（可配），默认合计 0.15%
- 过滤：沪深300 前一日 20 日收益 > 0 且 距 20 日高回撤 > -3%

输出：
  data/minute_model/backtest/trades.parquet   逐笔台账
  data/minute_model/backtest/daily.parquet    逐日净值
  data/minute_model/backtest/metrics.json     指标
  data/minute_model/backtest/report.md        报告

用法：
  .venv/bin/python -m backend.scripts.minute_model.backtest_intraday --cost 0.0015
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger("minute_model.backtest")
PANEL = Path("data/minute_model/panel")
MIN1 = Path("data/quantdb/1_kline_data/l2_min1")
INDEX = Path("data/quantdb/1_kline_data/index_daily")
OUT = Path("data/minute_model/backtest")
CUTOFF = "14:50"
TOPN = 20
CUTOFF_IDX = 4


def _days(start: str, end: str) -> list[str]:
    out = []
    for p in PANEL.glob("date=*"):
        n = p.name.split("=", 1)
        if len(n) == 2 and n[1].isdigit() and start <= n[1] <= end and (p / "data.parquet").exists():
            out.append(n[1])
    return sorted(out)


def _predict_day(con, d: str, model, feats) -> pd.DataFrame:
    df = con.sql(f"SELECT * FROM read_parquet('{PANEL}/date={d}/data.parquet') WHERE cutoff = '{CUTOFF}'").df()
    df = df[df["label_ok"] & df["tradable"] & df["ret_4h"].notna()]
    if len(df) < 100:
        return pd.DataFrame()
    df["cutoff_idx"] = CUTOFF_IDX
    for c in feats:
        if c not in df.columns:
            df[c] = np.nan
    df["pred"] = model.predict(df[feats].astype("float32"))
    return df.nlargest(TOPN, "pred")


def _next_open(con, next_date: str, symbols: list[str]) -> pd.DataFrame:
    """次日 09:31 bar 收盘价（开盘代理）。"""
    q = f"""
        SELECT symbol, CAST(close AS DOUBLE) AS open_px
        FROM read_parquet('{MIN1}/dt={next_date}/data.parquet')
        WHERE time = TIMESTAMP '{next_date[:4]}-{next_date[4:6]}-{next_date[6:]} 09:31:00'
          AND symbol IN ({",".join(repr(s) for s in symbols)})
    """
    return con.sql(q).df()


def _benchmark(start: str, end: str) -> pd.Series:
    con = duckdb.connect()
    q = f"""
        SELECT CAST(time AS DATE) AS d, close FROM read_parquet('{INDEX}/dt=*/data.parquet')
        WHERE symbol = '000300.SH' AND time >= DATE '{start[:4]}-{start[4:6]}-{start[6:]}'
          AND time <= DATE '{end[:4]}-{end[4:6]}-{end[6:]}'
        ORDER BY d
    """
    df = con.sql(q).df()
    con.close()
    if df.empty:
        return pd.Series(dtype=float)
    df["ret"] = df["close"].pct_change().fillna(0.0)
    return df.set_index("d")["ret"]


def metrics(daily: pd.DataFrame, trades: pd.DataFrame, label: str, ret_col: str = "net_ret") -> dict:
    r = daily["ret"].astype(float)
    eq = (1 + r).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1
    n_days = int((r != 0).sum())
    ann = (eq.iloc[-1]) ** (242 / max(len(r), 1)) - 1 if len(r) else 0.0
    sharpe = r.mean() / r.std() * np.sqrt(242) if r.std() > 0 else 0.0
    wins = trades[ret_col] > 0
    gross_win = trades.loc[wins, ret_col].sum()
    gross_loss = -trades.loc[~wins, ret_col].sum()
    return {
        "label": label,
        "days_total": int(len(r)),
        "trading_days": n_days,
        "exposure_pct": round(n_days / max(len(r), 1) * 100, 1),
        "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2) if len(eq) else 0.0,
        "annualized_pct": round(ann * 100, 2),
        "max_drawdown_pct": round(dd.min() * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "best_day_pct": round(r.max() * 100, 2) if len(r) else 0.0,
        "worst_day_pct": round(r.min() * 100, 2) if len(r) else 0.0,
        "trades": int(len(trades)),
        "win_rate_pct": round(wins.mean() * 100, 1) if len(trades) else 0.0,
        "avg_trade_net_pct": round(trades[ret_col].mean() * 100, 3) if len(trades) else 0.0,
        "median_trade_net_pct": round(trades[ret_col].median() * 100, 3) if len(trades) else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
    }


def monthly_table(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    d["ym"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m")
    m = d.groupby("ym").apply(lambda g: (1 + g["ret"]).prod() - 1, include_groups=False)
    return pd.DataFrame({"monthly_ret_pct": (m * 100).round(2)})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20250701")
    ap.add_argument("--end", default="20261030")
    ap.add_argument("--cost", type=float, default=0.0015, help="单次进出总成本（默认 0.15%%）")
    ap.add_argument("--filter", default="trend_dd", choices=["none", "trend", "trend_dd"])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    buy = pickle.load(open(Path("data/minute_model/model/lgb_4h.pkl"), "rb"))
    model, feats = buy["model"], buy["features"]
    days = _days(args.start, args.end)
    log.info("回测区间 %s ~ %s，%d 天，成本 %.4f，过滤 %s",
             days[0], days[-1], len(days), args.cost, args.filter)

    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    trades = []
    t0 = time.time()
    for i, d in enumerate(days[:-1]):
        nd = days[i + 1]
        top = _predict_day(con, d, model, feats)
        if top.empty:
            continue
        opens = _next_open(con, nd, top["symbol"].tolist())
        m = top.merge(opens, on="symbol", how="left")
        m = m[m["open_px"].notna()]
        if m.empty:
            continue
        m["date"] = d
        m["next_date"] = nd
        m["gross_open_ret"] = m["open_px"] / m["px"] - 1.0
        m["gross_close_ret"] = m["ret_4h"]
        m["net_open_ret"] = m["gross_open_ret"] - args.cost
        m["net_close_ret"] = m["gross_close_ret"] - args.cost
        trades.append(m[["date", "next_date", "symbol", "px", "open_px", "pred",
                         "mkt_ret_20d", "mkt_dd_20d", "gross_open_ret", "gross_close_ret",
                         "net_open_ret", "net_close_ret"]])
        if i % 40 == 0:
            log.info("回测 %d/%d", i, len(days))
    con.close()
    tr = pd.concat(trades, ignore_index=True)
    tr["date"] = tr["date"].astype(str)
    tr.to_parquet(OUT / "trades.parquet", index=False)

    # regime 过滤（每日一个条件）
    if args.filter == "trend":
        ok = tr["mkt_ret_20d"] > 0
    elif args.filter == "trend_dd":
        ok = (tr["mkt_ret_20d"] > 0) & (tr["mkt_dd_20d"] > -0.03)
    else:
        ok = pd.Series(True, index=tr.index)
    tr["filtered"] = ok

    def daily_curve(frame: pd.DataFrame, ret_col: str, label: str):
        g = frame.groupby("date")[ret_col].mean().reset_index().rename(columns={ret_col: "ret"})
        all_days = pd.DataFrame({"date": sorted(set(days[:-1]))})
        g = all_days.merge(g, on="date", how="left").fillna({"ret": 0.0})
        g["label"] = label
        return g

    results = {}
    variants = {
        "unfiltered": tr,
        "filtered": tr[tr["filtered"]],
    }
    daily_all = []
    for name, frame in variants.items():
        for exit_name, col in (("next_open", "net_open_ret"), ("next_1440", "net_close_ret")):
            dly = daily_curve(frame, col, f"{name}|{exit_name}")
            daily_all.append(dly)
            results[f"{name}|{exit_name}"] = metrics(dly, frame, f"{name}|{exit_name}", col)

    daily = pd.concat(daily_all, ignore_index=True)
    daily.to_parquet(OUT / "daily.parquet", index=False)

    bench = _benchmark(days[0], days[-1])
    if len(bench):
        eq = (1 + bench).cumprod()
        results["benchmark_csi300"] = {
            "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2),
            "max_drawdown_pct": round((eq / eq.cummax() - 1).min() * 100, 2),
        }

    # 成本敏感性（用毛收益重算，不重复预测）
    sens = {}
    for c in (0.0010, 0.0015, 0.0025):
        for name, frame in variants.items():
            if frame.empty:
                continue
            g = frame.groupby("date")["gross_open_ret"].mean()
            r = g - c
            eq = (1 + r).cumprod()
            sens[f"{name}@{c*100:.2f}%"] = {
                "trading_days": int(len(g)),
                "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2),
                "max_drawdown_pct": round((eq / eq.cummax() - 1).min() * 100, 2),
                "avg_trade_net_pct": round(float(r.mean()) * 100, 3),
            }
    results["cost_sensitivity"] = sens

    (OUT / "metrics.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))

    # 月度表
    main_curve = daily[daily["label"] == "filtered|next_open"]
    mt = monthly_table(main_curve)
    mt.to_csv(OUT / "monthly.csv", encoding="utf-8-sig")
    print(mt.to_string())

    # Markdown 报告
    lines = ["# 盘中 4h 模型 · 完整回测", "",
             f"- 区间：{days[0]} ~ {days[-1]}（模型样本外）",
             f"- 规则：14:50 买 Top{TOPN}，次日开盘卖；过滤={args.filter}；成本={args.cost:.4f}",
             "", "## 指标", "",
             "| 方案 | 交易天数 | 总收益% | 年化% | 最大回撤% | 夏普 | 胜率% | 单笔均值% | 盈亏比 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, v in results.items():
        if "total_return_pct" in v:
            lines.append(
                f"| {k} | {v.get('trading_days','-')} | {v['total_return_pct']} | "
                f"{v.get('annualized_pct','-')} | {v['max_drawdown_pct']} | "
                f"{v.get('sharpe','-')} | {v.get('win_rate_pct','-')} | "
                f"{v.get('avg_trade_net_pct','-')} | {v.get('profit_factor','-')} |"
            )
    lines += ["", "## 月度收益（filtered|next_open）", ""]
    try:
        lines.append(mt.to_markdown())
    except Exception:
        lines.append("```\n" + mt.to_string() + "\n```")
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("报告:", OUT / "report.md")


if __name__ == "__main__":
    main()
