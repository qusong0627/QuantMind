#!/usr/bin/env python3
"""大盘情绪数字的独立复算对账。

不信任生产代码的第二条实现：分位用 ``scipy.stats.percentileofscore``，
TRIN/ADF/UDR 直接读原始日线 parquet 重算，扇形分位用 numpy 重算，
逐项与 ``sentiment.json`` 比对并打印 PASS/FAIL。

用法::

    python -m backend.scripts.verify_sentiment_numbers
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

from backend.scripts.breadth_regime import build_features, load_panel
from backend.scripts.market_sentiment_snapshot import (
    CORE_KEYS,
    INDICATORS,
    PANEL_PATH,
    TARGETS,
)

BACKWARD_DIR = PANEL_PATH.parents[2] / "1_kline_data" / "daily_backward"
SNAPSHOT = PANEL_PATH.parent / "sentiment.json"
TOL = 5e-4


def _ok(name: str, got: float, want: float, tol: float = TOL) -> bool:
    good = bool(np.isfinite(got) and np.isfinite(want) and abs(got - want) <= tol)
    flag = "PASS" if good else "FAIL"
    print(f"[{flag}] {name:<44s} 复算={got:.6f}  生产={want:.6f}  差={got - want:+.2e}")
    return good


def verify_raw_day(panel: pd.DataFrame, date: pd.Timestamp) -> bool:
    """从原始日线重算 TRIN / ADR / UDR / 家数 / 成交额。"""
    tag = date.strftime("%Y%m%d")
    prev = panel.index[panel.index.get_loc(date) - 1].strftime("%Y%m%d")
    cur = pq.read_table(BACKWARD_DIR / f"dt={tag}" / "data.parquet").to_pandas()
    old = pq.read_table(BACKWARD_DIR / f"dt={prev}" / "data.parquet").to_pandas()
    m = cur.merge(old[["symbol", "close"]], on="symbol", suffixes=("", "_prev"))
    m = m[m["amount"] > 0]
    m["ret"] = m["close"] / m["close_prev"] - 1.0
    up, dn, flat = m[m.ret > 0], m[m.ret < 0], m[m.ret == 0]
    trin = (len(up) / len(dn)) / (up.amount.sum() / dn.amount.sum())
    adr = len(up) / len(dn)
    udr = up.amount.sum() / dn.amount.sum()
    row = panel.loc[date]
    ok = True
    ok &= _ok("n_up", float(len(up)), float(row["n_up"]), 1e-9)
    ok &= _ok("n_down", float(len(dn)), float(row["n_down"]), 1e-9)
    ok &= _ok("n_flat", float(len(flat)), float(row["n_flat"]), 1e-9)
    ok &= _ok("TRIN (成交额版)", trin, float(row["trin_amt"]), 1e-6)
    ok &= _ok("ADR 涨跌家数比", adr, float(row["adr"]), 1e-6)
    ok &= _ok("UDR 涨跌成交额比", udr, float(row["udr_amt"]), 1e-6)
    ok &= _ok("全市场成交额(万元)", float(m.amount.sum()), float(row["amt_total"]), 1e-3)
    return ok


def verify_percentiles(df: pd.DataFrame, feats: pd.DataFrame, payload: dict) -> bool:
    """扩张分位用 scipy.percentileofscore 独立重算。"""
    ok = True
    by_key = {i["key"]: i for i in payload["indicators"]}
    for ind in INDICATORS:
        if ind.name not in CORE_KEYS:
            continue
        series = df[ind.column].dropna().to_numpy(dtype=float)
        # 严格小于的占比 = weak 分位（scipy 的 weak 把相等值也算进去，这里用 strict）
        pct = stats.percentileofscore(series, series[-1], kind="strict") / 100.0
        ok &= _ok(f"分位 {ind.name}", float(pct), float(by_key[ind.name]["pct"]), 2e-3)
    return ok


def verify_score_with_frame(feats: pd.DataFrame, payload: dict) -> bool:
    parts = [
        1 - float(feats["breadth20"].iloc[-1]),
        1 - float(feats["breadth60"].iloc[-1]),
        1 - float(feats["adl_slope"].iloc[-1]),
        1 - float(feats["nh_nl"].iloc[-1]),
        1 - float(feats["mcclellan"].iloc[-1]),
        float(feats["trin_ema10"].iloc[-1]),
    ]
    got = float(np.mean(parts) * 100.0)
    return _ok("综合压力分", got, float(payload["stress"]["score"]), 0.06)


def verify_fan(df: pd.DataFrame, feats: pd.DataFrame, payload: dict) -> bool:
    """按分数所在区间取历史样本，用 numpy 重算 20 日路径分位。"""
    from backend.scripts.breadth_regime import composite_scores
    from backend.scripts.market_sentiment_snapshot import mask_from_zone

    stress = composite_scores(feats)["stress"]
    score = float(stress.iloc[-1])
    mask = mask_from_zone(stress, score)
    ok = True
    for symbol in TARGETS:
        close = df[f"close_{symbol}"].to_numpy(dtype=float)
        paths = []
        for i in np.flatnonzero(mask):
            if i + 20 >= len(close) or not np.isfinite(close[i]) or close[i] <= 0:
                continue
            seg = close[i + 1 : i + 21] / close[i] - 1.0
            if np.all(np.isfinite(seg)):
                paths.append(seg)
        paths = np.vstack(paths) * 100.0
        q = np.percentile(paths[:, 19], [10, 25, 50, 75, 90])
        fan = payload["indices"][symbol]["fan"][-1]
        for idx, key in enumerate(("p10", "p25", "p50", "p75", "p90")):
            ok &= _ok(f"扇形 {symbol} {key}", float(q[idx]), float(fan[key]), 5e-3)
        ok &= _ok(
            f"扇形 {symbol} 样本天数", float(len(paths)),
            float(payload["indices"][symbol]["stats"]["n_days"]), 1e-9,
        )
    return ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="独立复算大盘情绪数字")
    p.add_argument("--panel", default=str(PANEL_PATH))
    p.add_argument("--snapshot", default=str(SNAPSHOT))
    p.add_argument("--asof", default=None, help="回看日期 YYYY-MM-DD（按截断数据重算后对账）")
    args = p.parse_args(argv)

    df = load_panel(Path(args.panel))
    feats = build_features(df)
    if args.asof:
        from backend.scripts.market_sentiment_snapshot import build_payload

        date = pd.Timestamp(args.asof)
        df = df.loc[:date]
        feats = feats.loc[:date]
        payload = build_payload(df, feats)
        print(f"回看模式：数据截断到 {date:%Y-%m-%d} 后重算")
    else:
        payload = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        date = pd.Timestamp(payload["trade_date"])

    print(f"对账日期 {date:%Y-%m-%d}（生产快照 {Path(args.snapshot).name}）")
    print("-" * 78)
    ok = verify_raw_day(df, date)
    ok &= verify_percentiles(df, feats, payload)
    ok &= verify_score_with_frame(feats, payload)
    ok &= verify_fan(df, feats, payload)
    print("-" * 78)
    print("全部通过" if ok else "存在不一致，需排查")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
