"""统计 Top50 的涨停时点与后续涨停。

问题：
1. 收盘涨停的 Top50，是日内什么时点封板的？14:50 之前封板 = 我们的快照时已买不到；
2. 收盘没涨停的 Top50，T+1 / T+2 是否涨停？

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.limitup_timing
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

QUANTDB = Path("/data/quantdb")
MIN1 = QUANTDB / "1_kline_data" / "min1_kline"
OUT = Path("/data/stock_classifier/preclose")
MODEL = Path("/data/stock_classifier/models/lgb_h1_l1.pkl")
CUTOFF = pd.Timestamp("14:50").time()

BUCKETS = [
    ("09:25-09:50", "09:25", "09:50"),
    ("09:50-10:30", "09:50", "10:30"),
    ("10:30-11:30", "10:30", "11:30"),
    ("11:30-13:30", "11:30", "13:30"),
    ("13:30-14:30", "13:30", "14:30"),
    ("14:30-14:50", "14:30", "14:50"),
    ("14:50-15:00", "14:50", "15:01"),
]


def _digits(symbol: str) -> str:
    code = str(symbol)
    if "." in code:
        code = code.split(".", 1)[0]
    return code[-6:]


def limit_ratio(symbol: str, is_st: bool) -> float:
    if is_st:
        return 0.05
    code = _digits(symbol)
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def seal_time(day: pd.Timestamp, symbol: str, prev_close: float, ratio: float):
    """返回 (封板时刻, 是否在收盘前一直封住)。未摸到涨停返回 (None, False)。"""
    path = MIN1 / f"{symbol}.parquet"
    if not path.exists() or not np.isfinite(prev_close) or prev_close <= 0:
        return None, False
    limit_price = round(prev_close * (1 + ratio), 2)
    frame = pq.read_table(path, columns=["time", "open", "high", "low", "close"]).to_pandas()
    frame["time"] = pd.to_datetime(frame["time"])
    frame = frame[frame["time"].dt.normalize() == day]
    if frame.empty:
        return None, False
    frame = frame.sort_values("time")
    lows = frame["low"].to_numpy(float)
    hits = frame["high"].to_numpy(float) >= limit_price - 1e-6
    if not hits.any():
        return None, False
    # 从后往前找「一直不低于涨停价」的连续区间起点
    held = lows >= limit_price - 0.011
    start = None
    for i in range(len(frame) - 1, -1, -1):
        if held[i]:
            start = i
        else:
            break
    if start is None:
        first = int(np.argmax(hits))
        return frame["time"].iloc[first].time(), False
    return frame["time"].iloc[start].time(), True


def bucket_of(t) -> str:
    if t is None:
        return "未摸涨停"
    s = t.strftime("%H:%M")
    for name, lo, hi in BUCKETS:
        if lo <= s < hi:
            return name
    return "其他"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topn", type=int, default=50)
    args = parser.parse_args()

    with MODEL.open("rb") as fh:
        model = pickle.load(fh)
    feats, booster, mu = model["features"], model["booster"], model["mu"]

    features = pd.read_parquet(OUT / "features_1450.parquet")
    features["date"] = pd.to_datetime(features["date"])
    features["score"] = booster.predict(features[feats].to_numpy("float32")) @ mu

    calendar = sorted(
        p.name.split("=")[1]
        for p in (QUANTDB / "1_kline_data" / "daily_unadjusted").glob("dt=*")
    )
    index = {d: i for i, d in enumerate(calendar)}

    _cache: dict[str, pd.Series] = {}

    def close_on(day: str) -> pd.Series:
        if day not in _cache:
            frame = pq.read_table(
                QUANTDB / "1_kline_data" / "daily_unadjusted" / f"dt={day}" / "data.parquet",
                columns=["symbol", "close"],
            ).to_pandas()
            _cache[day] = frame.set_index("symbol")["close"].astype(float)
        return _cache[day]

    picks = []
    for date, group in features.groupby("date"):
        top = group.nlargest(args.topn, "score")
        picks.append(top[["date", "symbol", "is_st", "limit_up", "ret_next"]].assign(score=top["score"]))
    top = pd.concat(picks, ignore_index=True)

    # 后续涨停（T+1 / T+2）用特征表自身的 limit_up 列查
    flag = features.set_index(["date", "symbol"])["limit_up"]
    dates = sorted(pd.to_datetime(features["date"].unique()))
    next_map = {dates[i]: [dates[i + 1]] if i + 1 < len(dates) else [] for i in range(len(dates))}
    next2_map = {
        dates[i]: [dates[i + 2]] if i + 2 < len(dates) else [] for i in range(len(dates))
    }

    rows = []
    for r in top.itertuples(index=False):
        d = r.date
        nxt = next_map.get(d, [])
        nxt2 = next2_map.get(d, [])
        rows.append(
            {
                "date": d,
                "symbol": r.symbol,
                "is_st": bool(r.is_st),
                "limit_up": bool(r.limit_up),
                "lu_next": bool(flag.get((nxt[0], r.symbol), False)) if nxt else None,
                "lu_next2": bool(flag.get((nxt2[0], r.symbol), False)) if nxt2 else None,
            }
        )
    picks_df = pd.DataFrame(rows)

    # 封板时点
    lu = picks_df[picks_df["limit_up"]].copy()
    times = []
    sealed = []
    for r in lu.itertuples(index=False):
        ds = r.date.strftime("%Y%m%d")
        i = index.get(ds)
        prev = calendar[i - 1] if i and i > 0 else None
        prev_close = (
            close_on(prev).get(r.symbol, np.nan) if prev else np.nan
        )
        t, held = seal_time(r.date, r.symbol, float(prev_close), limit_ratio(r.symbol, r.is_st))
        times.append(bucket_of(t))
        sealed.append(held)
    lu["bucket"] = times
    lu["sealed_to_close"] = sealed

    report = {
        "n_picks": len(picks_df),
        "limit_up_share": round(float(picks_df["limit_up"].mean()), 4),
        "seal_bucket": {
            k: int(v) for k, v in lu["bucket"].value_counts().items()
        },
        "seal_bucket_share": {
            k: round(float(v), 3) for k, v in lu["bucket"].value_counts(normalize=True).items()
        },
        "sealed_to_close_share": round(float(np.mean(sealed)), 3) if sealed else None,
        "sealed_before_1450": int(
            sum(1 for b in lu["bucket"] if b in [x[0] for x in BUCKETS[:-1]])
        ),
        "sealed_after_1450": int(sum(1 for b in lu["bucket"] if b == "14:50-15:00")),
        "non_limit_up_next_day_limit_up": round(
            float(picks_df.loc[~picks_df["limit_up"], "lu_next"].dropna().mean()), 4
        ),
        "non_limit_up_next2_limit_up": round(
            float(picks_df.loc[~picks_df["limit_up"], "lu_next2"].dropna().mean()), 4
        ),
        "all_next_day_limit_up": round(
            float(picks_df["lu_next"].dropna().mean()), 4
        ),
    }
    (OUT / "limitup_timing.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n写出：{OUT / 'limitup_timing.json'}")


if __name__ == "__main__":
    main()
