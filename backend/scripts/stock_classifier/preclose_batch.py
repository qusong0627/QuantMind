"""并行抽取 14:50 特征矩阵（与模型无关，可复用）。

对每个交易日：
1. 装载 L1 计算窗口，把最后一天 forward/backward bar 换成 14:50 bar（前一日复权因子换算）；
2. 按前一日成交额取前 N 只，计算全部 L1 特征；
3. 只保留「盘中可算」的 32 个特征，并附上：
   - ``ret_next``：当日收盘 → 次日收盘（未复权）收益；
   - ``limit_up``：当日收盘涨停（买不进）；
   - ``is_st``：当日 ST；
   - ``amount``：当日成交额（万元，容量约束用）。

输出：``/data/stock_classifier/preclose/features_1450.parquet``

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.preclose_batch --workers 6
"""

from __future__ import annotations

import argparse
import logging
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.stock_classifier.preclose import (
    OUT,
    QUANTDB,
    _apply_bars,
    _subset_universe,
)

log = logging.getLogger("stock_classifier.preclose_batch")

DROP_PREFIXES = ("chip_", "ind_", "concept_", "style_", "fun_")
ST_DIR = QUANTDB / "factor_research" / "st_pit_cache"

_G: dict = {}


def _digits(value: str) -> str:
    code = str(value)
    if "." in code:
        code = code.split(".", 1)[0]
    return code[-6:]


def _load_st() -> set[tuple[pd.Timestamp, str]]:
    out: set[tuple[pd.Timestamp, str]] = set()
    for path in ST_DIR.glob("*.parquet"):
        if pq.read_metadata(path).num_rows == 0:
            continue
        frame = pq.read_table(path, columns=["symbol", "time", "is_st"]).to_pandas()
        frame = frame[frame["is_st"].astype(bool)]
        for _, row in frame.iterrows():
            out.add((pd.to_datetime(str(row["time"]), format="%Y%m%d"), _digits(row["symbol"])))
    return out


def _init(max_symbols: int) -> None:
    _G["max_symbols"] = max_symbols
    _G["bars"] = pq.read_table(OUT / "bars_1450.parquet").to_pandas()
    _G["bars"]["date"] = pd.to_datetime(_G["bars"]["date"])
    _G["adj"] = pq.read_table(OUT / "adj_factor.parquet").to_pandas()
    _G["adj"]["date"] = pd.to_datetime(_G["adj"]["date"])
    _G["st"] = _load_st()


def _limit_threshold(symbol: str) -> float:
    code = _digits(symbol)
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _unadj(day: pd.Timestamp) -> pd.Series:
    d = day.strftime("%Y%m%d")
    frame = pq.read_table(
        QUANTDB / "1_kline_data" / "daily_unadjusted" / f"dt={d}" / "data.parquet",
        columns=["symbol", "close"],
    ).to_pandas()
    return frame.set_index("symbol")["close"].astype(float)


def _one_date(target: str) -> pd.DataFrame | None:
    from backend.scripts.l1_factors.compute import compute_core

    try:
        day = pd.Timestamp(target)
        bars, adj = _G["bars"], _G["adj"]
        data, replaced = _apply_bars(target, bars, adj)
        if replaced < 500:
            log.warning("skip %s: 14:50 bar 仅 %d 只", target, replaced)
            return None
        data = _subset_universe(data, _G["max_symbols"])
        frame = compute_core(target, data)
        features = [c for c in frame.columns if not c.startswith(DROP_PREFIXES)]
        if len(features) < 25:
            log.warning("skip %s: 特征仅 %d 个", target, len(features))
            return None
        out = frame[features].copy()

        nxt = bars[bars["date"] > day]["date"].min()
        if pd.isna(nxt):
            return None
        px0 = _unadj(day)
        px1 = _unadj(nxt)
        # 上一交易日（可能跨周末）
        prev_dates = bars[bars["date"] < day]["date"]
        if prev_dates.empty:
            return None
        prev_day = prev_dates.max()
        px_prev = _unadj(prev_day)

        common = out.index.intersection(px0.index).intersection(px1.index)
        out = out.loc[common]
        out["ret_next"] = (px1.reindex(common) / px0.reindex(common) - 1.0).to_numpy()
        ret_1d = (px0.reindex(common) / px_prev.reindex(common) - 1.0).to_numpy()
        limits = np.array([_limit_threshold(s) for s in common])
        out["limit_up"] = np.isfinite(ret_1d) & (ret_1d >= limits - 0.005)
        out["is_st"] = [
            (day, _digits(s)) in _G["st"] for s in common
        ]
        amount = bars[bars["date"] == day].set_index("symbol")["amount"]
        out["amount"] = amount.reindex(common).to_numpy()
        out["date"] = day
        out["symbol"] = out.index.astype(str)
        return out.reset_index(drop=True)
    except Exception as exc:  # pragma: no cover
        log.warning("skip %s: %s", target, exc)
        return None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-symbols", type=int, default=2500)
    parser.add_argument("--dates", default="", help="可选，逗号分隔 YYYYMMDD")
    args = parser.parse_args()

    bars = pq.read_table(OUT / "bars_1450.parquet", columns=["date"]).to_pandas()
    all_dates = sorted(pd.to_datetime(bars["date"].unique()))
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        dates = [d.strftime("%Y%m%d") for d in all_dates]
    log.info("待抽取 %d 个交易日，%d workers，max_symbols=%d", len(dates), args.workers, args.max_symbols)

    with Pool(args.workers, initializer=_init, initargs=(args.max_symbols,)) as pool:
        frames = [f for f in pool.map(_one_date, dates) if f is not None and not f.empty]
    out = pd.concat(frames, ignore_index=True)
    path = OUT / "features_1450.parquet"
    out.to_parquet(path, index=False)
    log.info("写出 %s：%d 行 × %d 列，%d 个交易日", path, out.shape[0], out.shape[1], out["date"].nunique())


if __name__ == "__main__":
    main()
