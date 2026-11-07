"""重算 14:50 特征矩阵的标签列（交易日历口径）。

修正点：``prev_day`` / ``next_day`` 必须取**交易日历**上的相邻交易日，而不是分钟数据
里相邻的 bar 日（分钟数据有缺日，会让 ret 跨多天、把普通上涨误判成涨停）。

按用户口径：ST 不剔除，但 ST 的涨停阈值用 5%。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.preclose_relabel
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

QUANTDB = Path("/data/quantdb")
OUT = Path("/data/stock_classifier/preclose")
log = logging.getLogger("stock_classifier.preclose_relabel")


def _digits(value: str) -> str:
    code = str(value)
    if "." in code:
        code = code.split(".", 1)[0]
    return code[-6:]


def limit_threshold(symbol: str, is_st: bool) -> float:
    if is_st:
        return 0.05
    code = _digits(symbol)
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _close(day: str) -> pd.Series:
    frame = pq.read_table(
        QUANTDB / "1_kline_data" / "daily_unadjusted" / f"dt={day}" / "data.parquet",
        columns=["symbol", "close"],
    ).to_pandas()
    return frame.set_index("symbol")["close"].astype(float)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", default=str(OUT / "features_1450.parquet"))
    args = parser.parse_args()

    calendar = sorted(
        p.name.split("=")[1]
        for p in (QUANTDB / "1_kline_data" / "daily_unadjusted").glob("dt=*")
    )
    index = {d: i for i, d in enumerate(calendar)}
    log.info("交易日历 %d 天（%s ~ %s）", len(calendar), calendar[0], calendar[-1])

    features = pd.read_parquet(args.features)
    features["date"] = pd.to_datetime(features["date"])
    dates = sorted(features["date"].dt.strftime("%Y%m%d").unique())
    log.info("特征日期 %d 天", len(dates))

    rows = []
    for day in dates:
        if day not in index:
            log.warning("日期 %s 不在交易日历，跳过", day)
            continue
        i = index[day]
        if i == 0 or i + 1 >= len(calendar):
            continue
        prev_day, next_day = calendar[i - 1], calendar[i + 1]
        c0, cp, cn = _close(day), _close(prev_day), _close(next_day)
        common = c0.index.intersection(cp.index).intersection(cn.index)
        frame = pd.DataFrame({"symbol": common})
        frame["ret_next"] = (cn.reindex(common) / c0.reindex(common) - 1.0).to_numpy()
        ret_1d = (c0.reindex(common) / cp.reindex(common) - 1.0).to_numpy()
        frame["ret_1d"] = ret_1d
        frame["date"] = pd.to_datetime(day)
        rows.append(frame)
    labels = pd.concat(rows, ignore_index=True)

    merged = features.drop(
        columns=[c for c in ("ret_next", "limit_up") if c in features.columns]
    ).merge(labels, on=["date", "symbol"], how="left")
    is_st = merged["is_st"].astype(bool).to_numpy()
    thresholds = np.array(
        [limit_threshold(s, st) for s, st in zip(merged["symbol"], is_st)]
    )
    merged["limit_up"] = np.isfinite(merged["ret_1d"].to_numpy(float)) & (
        merged["ret_1d"].to_numpy(float) >= thresholds - 0.005
    )
    merged.to_parquet(args.features, index=False)

    summary = merged.groupby("date").agg(
        n=("symbol", "size"),
        ret=("ret_next", "mean"),
        lim=("limit_up", "mean"),
        st=("is_st", "mean"),
    )
    log.info(
        "重算完成：%d 行；每日涨停占比均值 %.1f%%；ST 占比 %.1f%%",
        len(merged),
        summary["lim"].mean() * 100,
        summary["st"].mean() * 100,
    )
    print(summary.head(5).round(4).to_string())
    print(summary.tail(3).round(4).to_string())


if __name__ == "__main__":
    main()
