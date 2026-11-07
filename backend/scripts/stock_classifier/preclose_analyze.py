"""14:50 快照特征矩阵的组合分析：Top50/Top10%、过滤、成本、容量、多模型。

输入：``/data/stock_classifier/preclose/features_1450.parquet``
输出：``/data/stock_classifier/preclose/analysis_1450.{json,md}``

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.preclose_analyze
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.stock_classifier.panel import rankdata_nan

OUT = Path("/data/stock_classifier/preclose")
MODEL_DIR = Path("/data/stock_classifier/models")
DEFAULT_MODELS = {
    "seed42": "lgb_h1_l1.pkl",
    "seed7": "lgb_h1_l1_s7.pkl",
    "seed2024": "lgb_h1_l1_s2024.pkl",
}
PORTFOLIOS = (("top50", 50), ("top100", 100), ("top10pct", 0.10), ("top20pct", 0.20))
COST_RATE = 0.0020


def _load_model(path: Path) -> dict:
    with path.open("rb") as fh:
        return pickle.load(fh)


def _select(frame: pd.DataFrame, spec: float) -> pd.DataFrame:
    if spec >= 1:
        return frame.nlargest(int(spec), "score")
    return frame[frame["score"] >= frame["score"].quantile(1 - spec)]


def evaluate(
    features: pd.DataFrame,
    model: dict,
    min_amount_wan: float,
    cost_rate: float,
    portfolio_size: float = 1e7,
    include_st: bool = False,
) -> dict:
    feats = model["features"]
    booster, mu = model["booster"], model["mu"]
    rows = features.copy()
    score = booster.predict(rows[feats].to_numpy("float32")) @ mu
    rows["score"] = score

    rows = rows[~rows["limit_up"].astype(bool)]
    if not include_st:
        rows = rows[~rows["is_st"].astype(bool)]
    rows = rows[rows["amount"] >= min_amount_wan]
    rows = rows[np.isfinite(rows["ret_next"]) & np.isfinite(rows["score"])]

    dates = np.sort(rows["date"].unique())
    results: dict = {}
    for name, spec in PORTFOLIOS:
        prev: set[str] = set()
        recs = []
        for date in dates:
            day = rows[rows["date"] == date]
            if len(day) < 100:
                continue
            top = _select(day, spec)
            if len(top) == 0:
                continue
            turnover = (
                len(set(top["symbol"]) - prev) / max(len(top), 1) if prev else 1.0
            )
            recs.append(
                {
                    "date": date,
                    "port": float(top["ret_next"].mean()),
                    "univ": float(day["ret_next"].mean()),
                    "n": len(top),
                    "turnover": turnover,
                    "ic": float(
                        np.corrcoef(
                            rankdata_nan(day["score"].to_numpy()),
                            rankdata_nan(day["ret_next"].to_numpy()),
                        )[0, 1]
                    )
                    if len(day) > 50
                    else np.nan,
                    "participation": float(
                        (portfolio_size / max(len(top), 1))
                        / (max(float(top["amount"].median()), 1e-6) * 1e4)
                    ),
                }
            )
            prev = set(top["symbol"])
        if not recs:
            continue
        df = pd.DataFrame(recs)
        df["cost"] = df["turnover"] * cost_rate
        df["net"] = df["port"] - df["cost"]
        df["excess_net"] = df["net"] - df["univ"]
        equity = (1 + df["net"]).cumprod()
        bench = (1 + df["univ"]).cumprod()
        peak = np.maximum.accumulate(equity)
        n = len(df)
        results[name] = {
            "n_days": int(n),
            "avg_holdings": round(float(df["n"].mean()), 1),
            "port_total": round(float(equity.iloc[-1] - 1), 4),
            "bench_total": round(float(bench.iloc[-1] - 1), 4),
            "port_daily": round(float(df["net"].mean() * 1e4), 2),
            "excess_daily_bp": round(float(df["excess_net"].mean() * 1e4), 2),
            "excess_ann": round(float(df["excess_net"].mean() * 252), 4),
            "win_rate": round(float((df["excess_net"] > 0).mean()), 3),
            "max_drawdown": round(float(np.min(equity / peak - 1)), 4),
            "sharpe": round(
                float(df["net"].mean() / df["net"].std() * np.sqrt(252)), 2
            )
            if df["net"].std() > 0
            else None,
            "turnover_day": round(float(df["turnover"].mean()), 3),
            "cost_daily_bp": round(float(df["cost"].mean() * 1e4), 2),
            "ic_mean": round(float(df["ic"].mean()), 4),
            "ic_pos_share": round(float((df["ic"] > 0).mean()), 3),
            "participation_median": round(float(df["participation"].median()), 5),
        }
        # 分段（上/下半年）
        half = n // 2
        if half >= 5:
            results[name]["first_half_excess_bp"] = round(
                float(df["excess_net"].iloc[:half].mean() * 1e4), 2
            )
            results[name]["second_half_excess_bp"] = round(
                float(df["excess_net"].iloc[half:].mean() * 1e4), 2
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-amount", type=float, default=2000.0, help="成交额下限（万元）")
    parser.add_argument("--cost", type=float, default=COST_RATE)
    parser.add_argument("--portfolio-size", type=float, default=1e7, help="组合规模（元）")
    parser.add_argument("--include-st", action="store_true", help="不剔除 ST")
    parser.add_argument("--out", default=str(OUT / "analysis_1450.json"))
    args = parser.parse_args()

    features = pd.read_parquet(OUT / "features_1450.parquet")
    features["date"] = pd.to_datetime(features["date"])
    report: dict = {
        "n_days": int(features["date"].nunique()),
        "n_rows": int(len(features)),
        "date_min": str(features["date"].min().date()),
        "date_max": str(features["date"].max().date()),
        "min_amount_wan": args.min_amount,
        "cost_rate": args.cost,
        "models": {},
    }
    for tag, filename in DEFAULT_MODELS.items():
        path = MODEL_DIR / filename
        if not path.exists():
            continue
        model = _load_model(path)
        report["models"][tag] = evaluate(
            features,
            model,
            args.min_amount,
            args.cost,
            args.portfolio_size,
            args.include_st,
        )

    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 14:50 快照组合分析",
        "",
        f"- 交易日 {report['n_days']}，样本 {report['n_rows']} 行，"
        f"{report['date_min']} ~ {report['date_max']}",
        f"- 过滤：剔除涨停、ST={'剔除' if not args.include_st else '保留'}、成交额 ≥ {args.min_amount:.0f} 万元",
        f"- 成本：单边换手 {args.cost * 1e4:.0f}bp；组合规模 {args.portfolio_size / 1e4:.0f} 万元",
        "",
    ]
    for tag, res in report["models"].items():
        lines.append(f"## {tag}")
        lines.append("")
        lines.append(
            "| 组合 | 天数 | 平均持仓 | 净值 | 基准 | 日均净收益 | 日均净超额 | 年化超额 | 胜率 | MaxDD | Sharpe | 日换手 | IC |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for name, r in res.items():
            lines.append(
                f"| {name} | {r['n_days']} | {r['avg_holdings']:.0f} | "
                f"{r['port_total']:+.2%} | {r['bench_total']:+.2%} | "
                f"{r['port_daily']:+.1f}bp | {r['excess_daily_bp']:+.1f}bp | "
                f"{r['excess_ann']:+.1%} | {r['win_rate']:.0%} | "
                f"{r['max_drawdown']:.2%} | {r['sharpe']} | {r['turnover_day']:.0%} | "
                f"{r['ic_mean']:+.3f} |"
            )
        lines.append("")
    md = Path(args.out).with_suffix(".md")
    md.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n写出：{args.out} / {md}")


if __name__ == "__main__":
    main()
