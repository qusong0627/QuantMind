#!/usr/bin/env python3
"""板块方向概率状态表（离线）：大盘压力 × 板块资金加速 × 板块价格位置 → 之后 h 日超额。

输出 ``sector_direction.json``：每个状态格的历史 P(板块等权超额>0)、均值、样本与日期级胜率。
当前板块状态由 API 实时判定（压力区、3 日资金加速分位、5 日涨幅分位），查表得到方向概率。

纪律：
- 状态只用 t 日及以前；前向收益 t+1 收盘建仓 → t+1+h 收盘；
- 状态分位按**当日全板块截面**三分（低/中/高），不是固定阈值；
- 日期级胜率按全局交易日每 h 天抽稀，降低重叠样本；
- 概率是历史状态频率，不是未来保证；样本 n 一并披露。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.scripts.breadth_regime import (  # noqa: E402
    build_features,
    composite_scores,
    load_panel,
)
from backend.scripts.market_sentiment_snapshot import PANEL_PATH  # noqa: E402

log = logging.getLogger("build_sector_direction")

SECTOR_PANEL_REL = "market-analysis/flow_evidence/panel_sector.parquet"
HORIZONS = (3, 5, 10, 20)
STRESS_ZONES = (
    (80.0, "extreme"),
    (60.0, "pressure"),
    (40.0, "neutral"),
    (20.0, "strong"),
    (-1.0, "overheat"),
)
FLOW_BUCKETS = ("q1", "q2", "q3", "q4", "q5")
PRICE_BUCKETS = ("q1", "q2", "q3", "q4", "q5")
MIN_CELL_N = 200

CONVENTIONS = {
    "stress": "大盘广度压力分（0~100，越高越冷/超卖）；分区 80/60/40/20",
    "flow": "板块 3 日净额比均值 − 前 3 日均值（资金加速），按当日全板块截面五分",
    "price": "板块 5 日等权涨幅，按当日全板块截面五分",
    "label": "板块等权前向收益 − 当日全板块均值 > 0（可执行 T+1 收盘建仓）",
    "date_win": "全局交易日每 h 天抽稀的日期级篮子胜率（50%=无方向）",
}
DISCLOSURE = (
    "方向概率是「历史同状态板块跑赢的比例」，不是未来保证；样本 n 与日期级胜率同时披露。"
    "状态分位按当日全板块截面计算，板块数少时请结合 n 使用。"
)


def _stress_series() -> pd.Series:
    df = load_panel(PANEL_PATH)
    feats = build_features(df)
    s = composite_scores(feats)["stress"]
    s.index = pd.to_datetime(s.index)
    return s


def _zone(score: float) -> str:
    for threshold, key in STRESS_ZONES:
        if score >= threshold:
            return key
    return "neutral"


def _quintile(frame: pd.DataFrame) -> pd.DataFrame:
    """按当日全板块截面五分（q1..q5）；标签与 API 端 _buckets 必须一致。"""
    r = frame.rank(axis=1, pct=True)
    out = pd.DataFrame("q3", index=frame.index, columns=frame.columns, dtype=object)
    labels = ("q1", "q2", "q3", "q4", "q5")
    for i, label in enumerate(labels):
        lo, hi = i / 5, (i + 1) / 5
        if i == 0:
            mask = r <= hi
        elif i == len(labels) - 1:
            mask = r > lo
        else:
            mask = (r > lo) & (r <= hi)
        out = out.mask(mask, label)
    return out


def build(data_dir: Path, out_dir: Path, start: str = "20180101") -> dict:
    started = time.time()
    panel_path = data_dir.parent / SECTOR_PANEL_REL
    if not panel_path.is_file():
        panel_path = Path("data") / SECTOR_PANEL_REL
    panel = pd.read_parquet(panel_path)
    panel["dt"] = pd.to_datetime(panel["dt"].astype(str))
    panel = panel[panel["dt"] >= pd.Timestamp(start)].sort_values(["sector", "dt"])
    dates = sorted(panel["dt"].unique())
    sectors = sorted(panel["sector"].unique())
    if not dates or not sectors:
        raise RuntimeError("板块面板为空")

    def W(col: str) -> pd.DataFrame:
        return panel.pivot(index="dt", columns="sector", values=col).reindex(
            index=dates, columns=sectors
        )

    ret = W("ret")
    net = W("net_ratio")
    stress = _stress_series().reindex(pd.Index(dates)).ffill()
    zones = stress.map(lambda v: _zone(float(v)) if pd.notna(v) else "neutral")

    cur3 = net.rolling(3, min_periods=2).mean()
    accel = cur3 - cur3.shift(3)
    r5 = ret.rolling(5, min_periods=3).sum()
    flow_q = _quintile(accel)
    price_q = _quintile(r5)

    def exec_fwd(h: int) -> pd.DataFrame:
        return ret.shift(-2).rolling(h).sum().shift(-(h - 1))

    fwd = {h: exec_fwd(h) for h in HORIZONS}
    rel = {h: x.sub(x.mean(axis=1, skipna=True), axis=0) for h, x in fwd.items()}

    cells: list[dict] = []
    for h in HORIZONS:
        e = rel[h].to_numpy()
        z = zones.to_numpy()
        fq = flow_q.to_numpy()
        pq = price_q.to_numpy()
        for _zi, zone in enumerate(
            ("overheat", "strong", "neutral", "pressure", "extreme")
        ):
            for _fi, fb in enumerate(FLOW_BUCKETS):
                for _pi, pb in enumerate(PRICE_BUCKETS):
                    m = (z == zone)[:, None] & (fq == fb) & (pq == pb) & np.isfinite(e)
                    n = int(m.sum())
                    if n < MIN_CELL_N:
                        continue
                    vals = e[m]
                    daily = np.nanmean(np.where(m, e, np.nan), axis=1)
                    # 全局交易日每 h 天抽稀
                    keep = np.array(
                        [
                            i % h == 0 and np.isfinite(daily[i])
                            for i in range(len(daily))
                        ]
                    )
                    sample = daily[keep]
                    # 日期级胜率是展示用降重叠指标；样本太薄时置空，状态格仍保留
                    date_win = (
                        round(float((sample > 0).mean()), 3)
                        if len(sample) >= 10
                        else None
                    )
                    cells.append(
                        {
                            "h": h,
                            "stress": zone,
                            "flow": fb,
                            "price": pb,
                            "n": n,
                            "win": round(float((vals > 0).mean()), 3),
                            "mean": round(float(vals.mean()), 3),
                            "date_win": date_win,
                            "n_dates": int(len(sample)),
                        }
                    )

    payload = {
        "schema": "sector_direction.v1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "window": [
            pd.Timestamp(dates[0]).strftime("%Y%m%d"),
            pd.Timestamp(dates[-1]).strftime("%Y%m%d"),
        ],
        "conventions": CONVENTIONS,
        "disclosure": DISCLOSURE,
        "cells": cells,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sector_direction.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    log.info(
        "写出 %s（%d 个状态格，用时 %.0fs）", path, len(cells), time.time() - started
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description="构建板块方向概率状态表")
    p.add_argument(
        "--data-dir", default=os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb")
    )
    p.add_argument("--out-dir", default=None)
    p.add_argument("--start", default="20180101")
    args = p.parse_args(argv)
    out = args.out_dir or str(
        Path(args.data_dir).parent / "market-analysis" / "sector_direction"
    )
    build(Path(args.data_dir), Path(out), args.start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
