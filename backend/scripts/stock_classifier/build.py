"""构建全市场多分类面板：收盘宽表 → 训练窗 IC 筛选 → 逐日面板。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.build all

产物（``/data/stock_classifier``）：
  close_wide.parquet                 收盘宽表（date × symbol）
  fwd_ret_h{1,3,5}.parquet           前视收益宽表
  screening/screen_h{h}.json         训练窗 IC 筛选结果（正反方向都保留）
  panel_h{h}/year=YYYY.parquet       训练面板（symbol/date/features/y）
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.stock_classifier.panel import (
    MIN_AMOUNT_WAN,
    N_CLASSES_DEFAULT,
    OHLCV_COLUMNS,
    assign_quantile_classes,
    daily_rank_ic,
    dedup_by_corr,
    is_limit_up,
    select_by_ic,
)

log = logging.getLogger("stock_classifier.build")

QUANTDB = Path("/data/quantdb")
FACTOR_DIR = QUANTDB / "6_ml_datasets" / "l1_l2_factors"
OUT = Path("/data/stock_classifier")

HORIZONS = (1, 3, 5)
TRAIN_START = "2018-01-02"
TRAIN_END = "2024-05-31"
SCREEN_MIN_ABS_IC = 0.010
SCREEN_MIN_ABS_ICIR = 0.15
SCREEN_MIN_COVERAGE = 0.80
DEDUP_CORR = 0.90
MAX_FEATURES = 50
CORR_SAMPLE_STEP = 20  # 每 20 个交易日取一天算截面相关


def _partitions() -> list[Path]:
    parts = sorted(FACTOR_DIR.glob("dt=*/data.parquet"))
    if not parts:
        raise RuntimeError(f"找不到因子分区：{FACTOR_DIR}")
    return parts


def _part_date(part: Path) -> pd.Timestamp:
    return pd.to_datetime(part.parent.name.split("=")[1])


def factor_columns() -> list[str]:
    schema = pq.read_schema(_partitions()[-1])
    return [c for c in schema.names if c not in OHLCV_COLUMNS]


# ---------------------------------------------------------------------------
# Pass 1：收盘宽表 + 前视收益
# ---------------------------------------------------------------------------


def build_close_wide() -> pd.DataFrame:
    rows: dict[pd.Timestamp, pd.Series] = {}
    started = time.time()
    parts = _partitions()
    for i, part in enumerate(parts, 1):
        frame = pq.read_table(part, columns=["symbol", "close"]).to_pandas()
        frame = frame[np.isfinite(frame["close"].to_numpy(float))]
        rows[_part_date(part)] = pd.Series(
            frame["close"].to_numpy(float), index=frame["symbol"].astype(str)
        )
        if i % 400 == 0:
            log.info("收盘宽表 %d/%d（%.0fs）", i, len(parts), time.time() - started)
    wide = pd.DataFrame(rows).T.sort_index()
    wide.index.name = "date"
    wide = wide.astype("float32")
    OUT.mkdir(parents=True, exist_ok=True)
    wide.to_parquet(OUT / "close_wide.parquet")
    log.info("close_wide %s（%.0fs）", wide.shape, time.time() - started)
    return wide


def build_exec_returns(
    close: pd.DataFrame, open_: pd.DataFrame
) -> dict[int, pd.DataFrame]:
    """次日开盘成交口径：close[T+h] / open[T+1] - 1。"""
    out: dict[int, pd.DataFrame] = {}
    for horizon in HORIZONS:
        ret = close.shift(-horizon) / open_.shift(-1) - 1.0
        ret = ret.astype("float32")
        ret.to_parquet(OUT / f"exec_ret_h{horizon}.parquet")
        out[horizon] = ret
        log.info("exec_ret_h%d %s", horizon, ret.shape)
    return out


def build_open_wide() -> pd.DataFrame:
    rows: dict[pd.Timestamp, pd.Series] = {}
    started = time.time()
    parts = _partitions()
    for i, part in enumerate(parts, 1):
        frame = pq.read_table(part, columns=["symbol", "open"]).to_pandas()
        frame = frame[np.isfinite(frame["open"].to_numpy(float))]
        rows[_part_date(part)] = pd.Series(
            frame["open"].to_numpy(float), index=frame["symbol"].astype(str)
        )
        if i % 800 == 0:
            log.info("开盘宽表 %d/%d（%.0fs）", i, len(parts), time.time() - started)
    wide = pd.DataFrame(rows).T.sort_index()
    wide.index.name = "date"
    wide = wide.astype("float32")
    wide.to_parquet(OUT / "open_wide.parquet")
    log.info("open_wide %s（%.0fs）", wide.shape, time.time() - started)
    return wide


def build_forward_returns(wide: pd.DataFrame) -> dict[int, pd.DataFrame]:
    out: dict[int, pd.DataFrame] = {}
    for horizon in HORIZONS:
        fwd = wide.shift(-horizon) / wide - 1.0
        fwd = fwd.astype("float32")
        fwd.to_parquet(OUT / f"fwd_ret_h{horizon}.parquet")
        out[horizon] = fwd
        log.info("fwd_ret_h%d %s", horizon, fwd.shape)
    return out


# ---------------------------------------------------------------------------
# Pass 2：训练窗 IC 筛选
# ---------------------------------------------------------------------------


def screen(horizon: int) -> dict:
    fwd = pq.read_table(OUT / f"fwd_ret_h{horizon}.parquet").to_pandas()
    fwd.index = pd.to_datetime(fwd.index)
    cols = factor_columns()
    parts = [
        p
        for p in _partitions()
        if pd.Timestamp(TRAIN_START) <= _part_date(p) <= pd.Timestamp(TRAIN_END)
    ]
    ic_rows: list[np.ndarray] = []
    corr_sum = np.zeros((len(cols), len(cols)), dtype=np.float64)
    corr_days = 0
    started = time.time()
    for i, part in enumerate(parts, 1):
        date = _part_date(part)
        frame = pq.read_table(part, columns=["symbol", *cols]).to_pandas()
        symbols = frame["symbol"].astype(str).to_numpy()
        if date not in fwd.index:
            continue
        y = fwd.loc[date].reindex(symbols).to_numpy(float)
        block = frame[cols].to_numpy("float32")
        ic_rows.append(daily_rank_ic(block, y))
        if i % CORR_SAMPLE_STEP == 0:
            ranked = pd.DataFrame(block).rank(axis=0)
            corr = ranked.corr(min_periods=200).to_numpy(float)
            corr = np.nan_to_num(corr, nan=0.0)
            corr_sum += corr
            corr_days += 1
        if i % 200 == 0:
            log.info(
                "筛选 h%d %d/%d（%.0fs）", horizon, i, len(parts), time.time() - started
            )
    ic_matrix = np.vstack(ic_rows)
    np.savez_compressed(
        OUT / "screening" / f"ic_h{horizon}.npz",
        ic=ic_matrix.astype(np.float32),
        columns=np.array(cols, dtype=object),
    )
    corr = corr_sum / max(corr_days, 1)
    np.save(OUT / "screening" / f"corr_h{horizon}.npy", corr.astype(np.float32))

    coverage = np.isfinite(ic_matrix).mean(axis=0)
    keep = select_by_ic(
        ic_matrix,
        coverage,
        min_abs_ic=SCREEN_MIN_ABS_IC,
        min_abs_icir=SCREEN_MIN_ABS_ICIR,
        min_coverage=SCREEN_MIN_COVERAGE,
    )
    selected = dedup_by_corr(
        keep, corr, max_features=MAX_FEATURES, corr_threshold=DEDUP_CORR
    )

    mean_ic = np.nanmean(ic_matrix, axis=0)
    std_ic = np.nanstd(ic_matrix, axis=0)
    icir = np.divide(mean_ic, std_ic, out=np.zeros_like(mean_ic), where=std_ic > 0)
    payload = {
        "horizon": horizon,
        "train_start": TRAIN_START,
        "train_end": TRAIN_END,
        "n_days": int(len(parts)),
        "n_factors": len(cols),
        "n_passed": len(keep),
        "n_selected": len(selected),
        "rule": {
            "min_abs_ic": SCREEN_MIN_ABS_IC,
            "min_abs_icir": SCREEN_MIN_ABS_ICIR,
            "min_coverage": SCREEN_MIN_COVERAGE,
            "dedup_corr": DEDUP_CORR,
            "max_features": MAX_FEATURES,
        },
        "selected": [
            {
                "name": cols[j],
                "mean_ic": round(float(mean_ic[j]), 5),
                "icir": round(float(icir[j]), 4),
                "coverage": round(float(coverage[j]), 4),
                "direction": int(np.sign(mean_ic[j])),
            }
            for j in selected
        ],
        "rejected_top": [
            {
                "name": cols[j],
                "mean_ic": round(float(mean_ic[j]), 5),
                "icir": round(float(icir[j]), 4),
                "coverage": round(float(coverage[j]), 4),
            }
            for j in np.argsort(-np.abs(icir))[:80]
            if j not in set(selected)
        ][:30],
    }
    (OUT / "screening").mkdir(parents=True, exist_ok=True)
    (OUT / "screening" / f"screen_h{horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "h%d 筛选完成：%d 因子 → 通过 %d → 去重后 %d（%.0fs）",
        horizon,
        len(cols),
        len(keep),
        len(selected),
        time.time() - started,
    )
    return payload


# ---------------------------------------------------------------------------
# Pass 3：逐日面板
# ---------------------------------------------------------------------------


def build_panel(horizon: int, features: list[str], n_classes: int) -> None:
    fwd = pq.read_table(OUT / f"fwd_ret_h{horizon}.parquet").to_pandas()
    fwd.index = pd.to_datetime(fwd.index)
    out_dir = OUT / f"panel_h{horizon}"
    out_dir.mkdir(parents=True, exist_ok=True)
    part_cols = ["symbol", "close", "amount", "mom_ret_1d", *features]
    part_cols = list(dict.fromkeys(part_cols))
    by_year: dict[int, list[pd.DataFrame]] = {}
    started = time.time()
    parts = _partitions()
    for i, part in enumerate(parts, 1):
        date = _part_date(part)
        if date not in fwd.index:
            continue
        frame = pq.read_table(part, columns=part_cols).to_pandas()
        symbols = frame["symbol"].astype(str).to_numpy()
        y = fwd.loc[date].reindex(symbols).to_numpy(float)
        amount = frame["amount"].to_numpy(float)
        ret = frame["mom_ret_1d"].to_numpy(float)
        ok = (
            np.isfinite(y)
            & (amount >= MIN_AMOUNT_WAN)
            & np.isfinite(frame["close"].to_numpy(float))
            & ~is_limit_up(pd.Series(symbols), pd.Series(ret))
        )
        if ok.sum() < 100:
            continue
        block = pd.DataFrame(
            frame.loc[ok, features].to_numpy("float32"), columns=features
        )
        block.insert(0, "symbol", symbols[ok])
        block.insert(1, "date", np.datetime64(date, "D"))
        block["fwd_ret"] = y[ok].astype("float32")
        block["y"] = assign_quantile_classes(y[ok], n_classes=n_classes)
        block = block[block["y"] >= 0]
        by_year.setdefault(date.year, []).append(block)
        if i % 400 == 0:
            log.info(
                "面板 h%d %d/%d（%.0fs）", horizon, i, len(parts), time.time() - started
            )

    for year, frames in sorted(by_year.items()):
        year_dir = out_dir / f"year={year}"
        year_dir.mkdir(parents=True, exist_ok=True)
        pd.concat(frames, ignore_index=True).to_parquet(
            year_dir / "data.parquet", index=False
        )
        log.info("写出 h%d year=%d：%d 行", horizon, year, sum(len(f) for f in frames))
    log.info("面板 h%d 完成（%.0fs）", horizon, time.time() - started)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="构建全市场多分类面板")
    parser.add_argument("stage", choices=("close", "open", "screen", "panel", "all"))
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument("--classes", type=int, default=N_CLASSES_DEFAULT)
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "screening").mkdir(parents=True, exist_ok=True)

    if args.stage in ("close", "all"):
        wide = build_close_wide()
        build_forward_returns(wide)

    if args.stage in ("open", "all"):
        close_wide = pq.read_table(OUT / "close_wide.parquet").to_pandas()
        close_wide.index = pd.to_datetime(close_wide.index)
        open_wide = build_open_wide()
        build_exec_returns(close_wide, open_wide)

    if args.stage in ("screen", "all"):
        for horizon in horizons:
            screen(horizon)

    if args.stage in ("panel", "all"):
        for horizon in horizons:
            payload = json.loads(
                (OUT / "screening" / f"screen_h{horizon}.json").read_text("utf-8")
            )
            features = [item["name"] for item in payload["selected"]]
            build_panel(horizon, features, args.classes)


if __name__ == "__main__":
    main()
