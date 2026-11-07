"""尾部行为分析：分数日间稳定性、做空尾部收益、下跌持续性。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.tail_analysis
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

QUANTDB = Path("/data/quantdb")
OUT = Path("/data/stock_classifier/preclose")
MODEL = Path("/data/stock_classifier/models/lgb_h1_l1.pkl")
HORIZONS = (1, 2, 3, 5)
BORROW_ANNUAL = 0.08


def _digits(symbol: str) -> str:
    code = str(symbol)
    if "." in code:
        code = code.split(".", 1)[0]
    return code[-6:]


def main() -> None:
    with MODEL.open("rb") as fh:
        model = pickle.load(fh)
    feats, booster, mu = model["features"], model["booster"], model["mu"]

    features = pd.read_parquet(OUT / "features_1450.parquet")
    features["date"] = pd.to_datetime(features["date"])
    features["score"] = booster.predict(features[feats].to_numpy("float32")) @ mu
    features = features[~features["limit_up"].astype(bool)]

    calendar = sorted(
        p.name.split("=")[1]
        for p in (QUANTDB / "1_kline_data" / "daily_unadjusted").glob("dt=*")
    )
    index = {d: i for i, d in enumerate(calendar)}
    dates = sorted(pd.to_datetime(features["date"].unique()))
    needed = set()
    for d in dates:
        i = index[d.strftime("%Y%m%d")]
        for h in HORIZONS:
            if i + h < len(calendar):
                needed.add(calendar[i + h])

    closes: dict[str, pd.Series] = {}
    for day in sorted(needed | {d.strftime("%Y%m%d") for d in dates}):
        frame = pq.read_table(
            QUANTDB / "1_kline_data" / "daily_unadjusted" / f"dt={day}" / "data.parquet",
            columns=["symbol", "close"],
        ).to_pandas()
        closes[day] = frame.set_index("symbol")["close"].astype(float)

    # ---------- 1. 分数日间变化 ----------
    daily_std, rank_autocorr, overlap, entry_rate, abs_change = [], [], [], [], []
    prev = None
    for d in dates:
        g = features[features["date"] == d]
        daily_std.append(float(g["score"].std()))
        cur = g.set_index("symbol")["score"]
        if prev is not None:
            joined = pd.concat([prev.rename("p"), cur.rename("c")], axis=1).dropna()
            if len(joined) > 200:
                rank_autocorr.append(
                    float(joined["p"].rank().corr(joined["c"].rank()))
                )
                abs_change.append(float((joined["c"] - joined["p"]).abs().mean()))
                p0 = set(joined.nsmallest(max(int(len(joined) * 0.1), 1), "p").index)
                c0 = set(joined.nsmallest(max(int(len(joined) * 0.1), 1), "c").index)
                overlap.append(len(p0 & c0) / max(len(c0), 1))
                entry_rate.append(len(c0 - p0) / max(len(c0), 1))
        prev = cur

    # ---------- 2/3. 尾部做空与持续性 ----------
    tail_rows = []
    for d in dates:
        g = features[features["date"] == d]
        if len(g) < 300:
            continue
        i = index[d.strftime("%Y%m%d")]
        c0 = closes[d.strftime("%Y%m%d")]
        cutoff = g["score"].quantile(0.1)
        bottom = g[g["score"] <= cutoff]["symbol"].to_list()
        top = g.nlargest(max(int(len(g) * 0.1), 1), "score")["symbol"].to_list()
        univ = g["symbol"].to_list()
        row = {"date": d}
        for h in HORIZONS:
            if i + h >= len(calendar):
                continue
            ch = closes[calendar[i + h]]
            def fwd(syms):
                px = ch.reindex(syms).to_numpy(float) / c0.reindex(syms).to_numpy(float) - 1
                return float(np.nanmean(px))
            row[f"bot_{h}"] = fwd(bottom)
            row[f"top_{h}"] = fwd(top)
            row[f"univ_{h}"] = fwd(univ)
        tail_rows.append(row)
    tail = pd.DataFrame(tail_rows)

    report: dict = {
        "n_days": int(len(dates)),
        "score_daily": {
            "cross_section_std_mean": round(float(np.mean(daily_std)), 4),
            "rank_autocorr_day_over_day": round(float(np.mean(rank_autocorr)), 4),
            "abs_score_change": round(float(np.mean(abs_change)), 4),
            "bottom_decile_overlap": round(float(np.mean(overlap)), 3),
            "bottom_decile_new_names": round(float(np.mean(entry_rate)), 3),
        },
        "forward_returns_bp": {},
        "short_tail": {},
    }
    for h in HORIZONS:
        col = f"bot_{h}"
        if col not in tail:
            continue
        bot = tail[col].mean() * 1e4
        top = tail[f"top_{h}"].mean() * 1e4
        univ = tail[f"univ_{h}"].mean() * 1e4
        report["forward_returns_bp"][f"d{h}"] = {
            "bottom_decile": round(float(bot), 1),
            "top_decile": round(float(top), 1),
            "universe": round(float(univ), 1),
            "bottom_minus_universe": round(float(bot - univ), 1),
        }
        # 做空底部十分位：毛收益 = -bot；融资成本按持有 h 天
        borrow_bp = BORROW_ANNUAL * h / 252 * 1e4
        report["short_tail"][f"d{h}"] = {
            "gross_bp": round(float(-bot), 1),
            "borrow_bp": round(float(borrow_bp), 1),
            "net_bp": round(float(-bot - borrow_bp), 1),
            "win_rate": round(float((tail[col] < 0).mean()), 3),
        }

    (OUT / "tail_analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n写出：{OUT / 'tail_analysis.json'}")


if __name__ == "__main__":
    main()
