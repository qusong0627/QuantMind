#!/usr/bin/env python3
"""对已选特征计算验证/测试窗的单因子 RankIC（台账审计用，不参与选特征）。

口径与选择器一致：次日开盘买入的远期收益 ``close[t+h]/open[t+1]-1``，
同一套可交易过滤（ST/涨停/停牌/成交额/上市满 60 日/市值可得）。
输出按 valid（2025）与 test（2026）分窗，供 registry 记录；test 只报不调。

用法（容器内）：
    python backend/scripts/stock_classifier/oos_ic.py \
        --selection /data/stock_classifier/feature_selection/daily_feature_selection_2016_2024.json \
        --top 100 --start 2025-01-01 --end 2026-11-05 --step 5 \
        --out /data/stock_classifier/feature_selection/oos_daily_2025_2026.json
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.stock_classifier.select_daily_features import (
    MIN_AMOUNT_WAN,
    _ic_matrix,
    _limit_mask,
    _load_price_wide,
    _load_st,
    _load_total_mv,
    _sample_dates,
)
from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)

log = logging.getLogger("oos_ic")

VALID_START = "2025-01-01"
TEST_START = "2026-01-01"


def _aggregate(values: list[float]) -> dict | None:
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None
    mean = float(vals.mean())
    std = float(vals.std(ddof=1)) if vals.size > 1 else 0.0
    # 与选择器同口径：ICIR = mean/std（不乘 sqrt(n)）。乘 sqrt(n) 会随采样
    # 密度变化，导致 OOS ICIR 与选择 ICIR 不可比。
    icir = mean / std if std > 0 else 0.0
    pos = (
        float((np.sign(vals) == np.sign(mean)).mean())
        if mean != 0
        else float((vals > 0).mean())
    )
    return {"mean": mean, "icir": icir, "pos": pos, "n": int(vals.size)}


def compute_oos(
    selection_path: str | Path,
    top: int,
    start: str,
    end: str,
    step: int,
) -> dict:
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    entries = selection["selected"][str(top)]
    features = [str(e["code"]) for e in entries]
    sources = {str(e["code"]): str(e["source"]) for e in entries}
    close, open_ = _load_price_wide()
    st = _load_st()
    st_by_date: dict[pd.Timestamp, set[str]] = {}
    for row in st.itertuples():
        st_by_date.setdefault(row.time, set()).add(str(row.symbol))
    listed_ok = close.notna().rolling(60, min_periods=60).sum() >= 60
    reader = QuantDBFactorReader(market="CN")
    dates = _sample_dates(close, step, start, end)
    log.info("OOS 采样 %d 日（%s ~ %s，step=%d）", len(dates), start, end, step)

    per_code: dict[str, dict[str, dict[str, list[float]]]] = {
        code: {"valid": {}, "test": {}} for code in features
    }
    coverage_hits = {window: np.zeros(len(features)) for window in ("valid", "test")}
    seen = dict.fromkeys(("valid", "test"), 0)
    n_dates = {"valid": 0, "test": 0}
    last_usable = None
    processed = 0

    for date in dates:
        i = close.index.get_loc(date)
        if i + 6 >= len(close.index):
            continue
        try:
            frame = reader.read_day(
                "l1_factors",
                features=features,
                feature_sources=sources,
                trade_date=date.strftime("%Y-%m-%d"),
            )
        except Exception as exc:  # noqa: BLE001 - 单日失败不终止
            log.warning("%s 读取失败跳过: %s", date.date(), exc)
            continue
        if frame.empty:
            continue
        frame["symbol"] = frame["symbol"].astype(str)
        missing = [code for code in features if code not in frame.columns]
        for code in missing:
            frame[code] = np.nan
        syms = frame["symbol"].to_numpy()
        vals = frame[features].to_numpy(dtype=np.float32)
        close_t = close.iloc[i].reindex(syms).to_numpy(float)
        amount = (
            frame["amount"].to_numpy(float)
            if "amount" in frame
            else np.full(len(frame), np.nan)
        )
        mv = _load_total_mv(date).reindex(syms).to_numpy(float)
        st_set = st_by_date.get(date, set())
        listed = listed_ok.iloc[i].reindex(syms).to_numpy(bool)
        limit = _limit_mask(frame, close)
        ok = (
            np.isfinite(close_t)
            & (close_t > 0)
            & np.isfinite(amount)
            & (amount >= MIN_AMOUNT_WAN)
            & listed
            & np.isfinite(mv)
            & ~limit
            & np.array([s not in st_set for s in syms])
        )
        if int(ok.sum()) < 300:
            continue
        idx5, idx3, idx1 = close.index[i + 5], close.index[i + 3], close.index[i + 1]
        o1 = open_.loc[idx1].reindex(syms).to_numpy(float)
        c1 = close.loc[idx1].reindex(syms).to_numpy(float)
        c3 = close.loc[idx3].reindex(syms).to_numpy(float)
        c5 = close.loc[idx5].reindex(syms).to_numpy(float)
        y1 = c1 / o1 - 1.0
        y1cc = c1 / close_t - 1.0
        y3 = c3 / o1 - 1.0
        y5 = c5 / o1 - 1.0
        sub = ok & np.isfinite(y5)
        if int(sub.sum()) < 300:
            continue
        x = vals[sub]
        window = "test" if date >= pd.Timestamp(TEST_START) else "valid"
        n_dates[window] += 1
        last_usable = date
        coverage_hits[window] += np.isfinite(x).sum(axis=0)
        seen[window] += int(sub.sum())
        matrices = {
            "ic5": _ic_matrix(x, y5[sub]),
            "ic3": _ic_matrix(x, y3[sub]),
            "ic1": _ic_matrix(x, y1[sub]),
            "ic1_cc": _ic_matrix(x, y1cc[sub]),
        }
        for j, code in enumerate(features):
            bucket = per_code[code][window]
            for key, matrix in matrices.items():
                value = float(matrix[j])
                if np.isfinite(value):
                    bucket.setdefault(key, []).append(value)
        processed += 1
        if processed % 20 == 0:
            log.info("OOS %d/%d 日（last=%s）", processed, len(dates), date.date())

    out: dict[str, dict] = {"valid": {}, "test": {}}
    for window in ("valid", "test"):
        for j, code in enumerate(features):
            agg = {
                key: _aggregate(per_code[code][window].get(key, []))
                for key in ("ic5", "ic3", "ic1", "ic1_cc")
            }
            if not agg["ic5"]:
                continue
            coverage = float(coverage_hits[window][j] / max(seen[window], 1))
            out[window][code] = {
                "ic5": agg["ic5"]["mean"],
                "icir5": agg["ic5"]["icir"],
                "pos5": agg["ic5"]["pos"],
                "ic3": agg["ic3"]["mean"] if agg["ic3"] else None,
                "icir3": agg["ic3"]["icir"] if agg["ic3"] else None,
                "ic1": agg["ic1"]["mean"] if agg["ic1"] else None,
                "icir1": agg["ic1"]["icir"] if agg["ic1"] else None,
                "ic1_cc": agg["ic1_cc"]["mean"] if agg["ic1_cc"] else None,
                "icir1_cc": agg["ic1_cc"]["icir"] if agg["ic1_cc"] else None,
                "coverage": coverage,
                "n": agg["ic5"]["n"],
            }
    return {
        "valid": out["valid"],
        "test": out["test"],
        "meta": {
            "start": start,
            "end": end,
            "step": step,
            "label": "close[t+h]/open[t+1]-1",
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "n_valid_dates": n_dates["valid"],
            "n_test_dates": n_dates["test"],
            "last_usable_date": str(last_usable.date()) if last_usable else None,
            "n_features_requested": len(features),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", required=True)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--start", default=VALID_START)
    ap.add_argument("--end", default="2026-11-05")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    payload = compute_oos(args.selection, args.top, args.start, args.end, args.step)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"wrote {out} valid={len(payload['valid'])} test={len(payload['test'])} "
        f"dates={payload['meta']['n_valid_dates']}/{payload['meta']['n_test_dates']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
