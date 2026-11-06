"""盘中 14:50 快照回放：分钟线 → 14:50 bar → L1 特征 → 推理 → 收盘买入。

三个步骤：
1. ``build_bars``：从 ``min1_kline`` 聚合每个交易日的 14:50 bar（未复权价 + 量额）；
2. ``build_adj``：用前一日 ``daily_forward / daily_unadjusted`` 的收盘比，把 14:50 bar
   换算到前复权口径（与因子表一致，且不使用当日收盘，PIT 安全）；
3. ``simulate``：逐日装载 L1 计算窗口、把最后一天 bar 换成 14:50 bar、重算特征、
   用 L1 盘中模型推理，并按当日收盘价买入、次日收盘卖出计收益。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.preclose build-bars
  cd /app && python3 -m backend.scripts.stock_classifier.preclose build-adj
  cd /app && python3 -m backend.scripts.stock_classifier.preclose simulate --dates 20260630,20260629
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

log = logging.getLogger("stock_classifier.preclose")

QUANTDB = Path("/data/quantdb")
MIN1_DIR = QUANTDB / "1_kline_data" / "min1_kline"
OUT = Path("/data/stock_classifier/preclose")
CUTOFF = "14:50"


def build_bars() -> None:
    """逐文件聚合 14:50 bar（未复权）。"""
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(MIN1_DIR.glob("*.parquet"))
    frames: list[pd.DataFrame] = []
    for i, path in enumerate(files, 1):
        try:
            frame = pq.read_table(
                path,
                columns=["time", "open", "high", "low", "close", "volume", "amount"],
            ).to_pandas()
        except Exception as exc:  # pragma: no cover
            log.warning("skip %s: %s", path, exc)
            continue
        if frame.empty:
            continue
        frame["time"] = pd.to_datetime(frame["time"])
        frame = frame[frame["time"].dt.strftime("%H:%M") <= CUTOFF]
        if frame.empty:
            continue
        frame["date"] = frame["time"].dt.normalize()
        grouped = (
            frame.groupby("date", sort=True)
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                amount=("amount", "sum"),
            )
            .reset_index()
        )
        grouped["symbol"] = path.stem
        frames.append(grouped)
        if i % 500 == 0:
            log.info("14:50 bar %d/%d", i, len(files))
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(OUT / "bars_1450.parquet", index=False)
    log.info("bars_1450 %s，%s ~ %s", out.shape, out["date"].min(), out["date"].max())


def build_adj() -> None:
    """前一日 forward/unadjusted 收盘比（PIT 复权因子）。"""
    OUT.mkdir(parents=True, exist_ok=True)
    dates = sorted(
        p.name.split("=")[1]
        for p in (QUANTDB / "1_kline_data" / "daily_forward").glob("dt=*")
    )
    frames: list[pd.DataFrame] = []
    for d in dates:
        fw = pq.read_table(
            QUANTDB / "1_kline_data" / "daily_forward" / f"dt={d}" / "data.parquet",
            columns=["symbol", "close"],
        ).to_pandas()
        bw = pq.read_table(
            QUANTDB / "1_kline_data" / "daily_backward" / f"dt={d}" / "data.parquet",
            columns=["symbol", "close"],
        ).to_pandas()
        un = pq.read_table(
            QUANTDB / "1_kline_data" / "daily_unadjusted" / f"dt={d}" / "data.parquet",
            columns=["symbol", "close"],
        ).to_pandas()
        merged = fw.merge(un, on="symbol", suffixes=("_fw", "_un"))
        merged = merged.merge(
            bw.rename(columns={"close": "close_bw"}), on="symbol", how="left"
        )
        merged = merged[(merged["close_un"] > 0) & merged["close_fw"].notna()]
        merged["adj_fw"] = merged["close_fw"] / merged["close_un"]
        merged["adj_bw"] = merged["close_bw"] / merged["close_un"]
        merged["date"] = pd.to_datetime(d)
        frames.append(merged[["date", "symbol", "adj_fw", "adj_bw"]])
    out = pd.concat(frames, ignore_index=True)
    out["adj_fw"] = out.groupby("symbol", sort=False)["adj_fw"].shift(1)
    out["adj_bw"] = out.groupby("symbol", sort=False)["adj_bw"].shift(1)
    out = out.dropna(subset=["adj_fw", "adj_bw"])
    out.to_parquet(OUT / "adj_factor.parquet", index=False)
    log.info("adj_factor %s", out.shape)


def _apply_bars(target: str, bars: pd.DataFrame, adj: pd.DataFrame):
    """把窗口数据的最后一天换成 14:50 bar（前复权口径）。"""
    from backend.scripts.l1_factors.compute import load_window

    data = load_window(QUANTDB, target)
    day = pd.Timestamp(target)
    bar = bars[bars["date"] == day].set_index("symbol")
    adj_fw = adj[adj["date"] == day].set_index("symbol")["adj_fw"]
    adj_bw = adj[adj["date"] == day].set_index("symbol")["adj_bw"]

    def _override(frames: dict, factors: pd.Series) -> int:
        count = 0
        for symbol, frame in frames.items():
            if symbol not in bar.index or symbol not in factors.index or frame.empty:
                continue
            row = bar.loc[symbol]
            factor = float(factors.loc[symbol])
            if not np.isfinite(factor) or factor <= 0:
                continue
            last = frame.index[-1]
            frame.loc[last, "open"] = float(row["open"]) * factor
            frame.loc[last, "high"] = float(row["high"]) * factor
            frame.loc[last, "low"] = float(row["low"]) * factor
            frame.loc[last, "close"] = float(row["close"]) * factor
            frame.loc[last, "volume"] = float(row["volume"])
            frame.loc[last, "amount"] = float(row["amount"])
            count += 1
        return count

    replaced = _override(data.forward, adj_fw)
    _override(data.backward, adj_bw)
    # 禁用 EOD 技术指标透传，避免盘中泄漏
    empty_ti = pd.DataFrame(columns=data.ti_frame.columns)
    data = dataclasses.replace(data, ti_frame=empty_ti)
    return data, replaced


def _unadj_close(day: pd.Timestamp) -> pd.Series:
    """当日未复权收盘价（买入/卖出价格基准）。"""
    d = day.strftime("%Y%m%d")
    frame = pq.read_table(
        QUANTDB / "1_kline_data" / "daily_unadjusted" / f"dt={d}" / "data.parquet",
        columns=["symbol", "close"],
    ).to_pandas()
    return frame.set_index("symbol")["close"].astype(float)


def _subset_universe(data, max_symbols: int):
    """按 backward 末行成交额取前 N 只（提速；不影响截面口径）。"""
    if max_symbols <= 0:
        return data
    scores = {}
    for symbol, frame in data.backward.items():
        if len(frame) and "amount" in frame.columns:
            value = frame["amount"].iloc[-1]
            if np.isfinite(value):
                scores[symbol] = float(value)
    keep = set(sorted(scores, key=lambda s: -scores[s])[:max_symbols])
    return dataclasses.replace(
        data,
        forward={s: f for s, f in data.forward.items() if s in keep},
        backward={s: f for s, f in data.backward.items() if s in keep},
        valuation={s: f for s, f in data.valuation.items() if s in keep},
    )


def simulate(
    dates: list[str], model_path: Path, top_frac: float = 0.1, max_symbols: int = 0
) -> None:
    from backend.scripts.l1_factors.compute import compute_core
    from backend.scripts.stock_classifier.panel import rankdata_nan

    with model_path.open("rb") as fh:
        model = pickle.load(fh)
    booster, features, mu = model["booster"], model["features"], model["mu"]
    bars = pq.read_table(OUT / "bars_1450.parquet").to_pandas()
    adj = pq.read_table(OUT / "adj_factor.parquet").to_pandas()

    rows = []
    for target in dates:
        try:
            data, replaced = _apply_bars(target, bars, adj)
            if replaced < 500:
                log.warning("skip %s: 14:50 bar 仅替换 %d 只，分钟数据缺日", target, replaced)
                continue
            data = _subset_universe(data, max_symbols)
            frame = compute_core(target, data)
        except Exception as exc:
            log.warning("skip %s: %s", target, exc)
            continue
        available = [c for c in features if c in frame.columns]
        if len(available) < len(features) * 0.8:
            log.warning(
                "skip %s: 特征缺失 %d/%d", target, len(available), len(features)
            )
            continue
        block = frame.reindex(columns=features)
        proba = booster.predict(block.to_numpy("float32"))
        score = proba @ mu
        day = pd.Timestamp(target)
        # 买入价 = 当日收盘（未复权），卖出价 = 次日收盘
        nxt = bars[bars["date"] > day]["date"].min()
        if pd.isna(nxt):
            continue
        px_now = _unadj_close(day)
        px_next = _unadj_close(nxt)
        adj_t = adj[adj["date"] == day].set_index("symbol")["adj_fw"]
        common = px_now.index.intersection(px_next.index).intersection(adj_t.index)
        common = common.intersection(frame.index.astype(str))
        if len(common) < 500:
            continue
        raw_now = px_now.reindex(common).astype(float)
        raw_next = px_next.reindex(common).astype(float)
        ret = raw_next / raw_now - 1.0
        sc = pd.Series(score, index=frame.index.astype(str)).reindex(common)
        ok = np.isfinite(sc.to_numpy(float)) & np.isfinite(ret.to_numpy(float))
        sc, ret = sc[ok], ret[ok]
        if len(sc) < 500:
            continue
        top = sc >= sc.quantile(1 - top_frac)
        rows.append(
            {
                "date": target,
                "n": int(len(sc)),
                "replaced": int(replaced),
                "top_ret": float(ret[top].mean()),
                "univ_ret": float(ret.mean()),
                "excess": float(ret[top].mean() - ret.mean()),
                "ic": float(
                    np.corrcoef(
                        rankdata_nan(sc.to_numpy()), rankdata_nan(ret.to_numpy())
                    )[0, 1]
                ),
            }
        )
        log.info(
            "%s 替换=%d 标的=%d 多头=%.3f%% 基准=%.3f%% 超额=%.3f%% IC=%.3f",
            target,
            replaced,
            len(sc),
            rows[-1]["top_ret"] * 100,
            rows[-1]["univ_ret"] * 100,
            rows[-1]["excess"] * 100,
            rows[-1]["ic"],
        )
    if rows:
        df = pd.DataFrame(rows)
        summary = {
            "n_days": int(len(df)),
            "excess_mean_bp": round(float(df["excess"].mean() * 1e4), 2),
            "excess_ann": round(float(df["excess"].mean() * 252), 4),
            "ic_mean": round(float(df["ic"].mean()), 4),
            "ic_pos_share": round(float((df["ic"] > 0).mean()), 3),
        }
        path = OUT / "preclose_sim.json"
        path.write_text(
            json.dumps(
                {"summary": summary, "days": rows, "model": str(model_path)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"写出: {path}")
    else:
        print("没有有效交易日")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="14:50 快照回放")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-bars")
    sub.add_parser("build-adj")
    sim = sub.add_parser("simulate")
    sim.add_argument("--dates", required=True, help="逗号分隔的 YYYYMMDD")
    sim.add_argument("--model", default="/data/stock_classifier/models/lgb_h1_l1.pkl")
    sim.add_argument("--max-symbols", type=int, default=0, help="按成交额取前 N 只提速")
    args = parser.parse_args()

    if args.cmd == "build-bars":
        build_bars()
    elif args.cmd == "build-adj":
        build_adj()
    else:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
        simulate(dates, Path(args.model), max_symbols=args.max_symbols)


if __name__ == "__main__":
    main()
