"""构建「大盘方向模型」特征表（L1/L2 因子截面聚合 + 广度 + 量价 + 两融/宏观/基差）。

输出：``/data/market_direction/features/market_direction_features.parquet``
      ``/data/market_direction/features/feature_meta.json``

口径（全部 PIT，2026-11-06）：
- 因子截面聚合只读当日分区 ``l1_l2_factors/dt=YYYYMMDD``；
- 大盘/小盘分组用当日 ``fun_mv_rank``（流通市值截面分位，逐日计算）；
- 因子聚合做 252 日滚动分位（尾随窗口，min_periods=120），保证跨年份可比；
- 广度用 ``market_breadth/by_universe.parquet``（其宇宙与分位均为 PIT）；
- 两融用 ``_lag1`` 字段（T+1 披露）；宏观/基差只做 ≤5 日 ffill 补周末缺口；
- 标签 ``fwd_ret_h`` = close[T+h]/close[T]-1，``fwd_ret_open_h`` = close[T+h]/open[T+1]-1。

运行（容器内）：
  cd /app && python3 -m backend.scripts.market_direction.build_features
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.market_direction.feature_spec import (
    ALL_FEATURE_COLUMNS,
    BASIS_COLUMNS,
    BREADTH_ZSCORE_COLUMNS,
    FACTOR_AGG_COLUMNS,
    FACTOR_AUX_COLUMNS,
    FACTOR_FEATURE_COLUMNS,
    FACTOR_SPECS,
    LARGE_CAP_BREADTH_SOURCE,
    MACRO_COLUMNS,
    MARGIN_COLUMNS,
    MARKET_BREADTH_SOURCE,
    TARGET_INDICES,
)
from backend.scripts.market_direction.features import (
    aggregate_partition,
    assign_split,
    compute_labels,
    rolling_pct_rank,
)

log = logging.getLogger("market_direction.features")

HORIZONS: tuple[int, ...] = (1, 3, 5)
QUANTDB = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
OUT_DIR = Path(os.environ.get("QM_MARKET_DIRECTION_DIR", "/data/market_direction"))
FEATURE_DIR = OUT_DIR / "features"

Z_WINDOW = 252
Z_MIN_PERIODS = 120


# ---------------------------------------------------------------------------
# 指数量价
# ---------------------------------------------------------------------------


def load_index_prices(codes: dict[str, str]) -> pd.DataFrame:
    """从 ``index_daily`` 分区读取目标指数的 OHLCV。"""
    frames: list[pd.DataFrame] = []
    for part in sorted((QUANTDB / "1_kline_data" / "index_daily").glob("dt=*")):
        try:
            df = pq.read_table(
                part,
                columns=[
                    "symbol",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                ],
            ).to_pandas()
        except Exception as exc:  # pragma: no cover - 数据面异常
            log.warning("skip %s: %s", part, exc)
            continue
        df = df[df["symbol"].isin(codes)]
        if not df.empty:
            frames.append(df)
    if not frames:
        raise RuntimeError("index_daily 没有目标指数数据")
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["time"]).dt.normalize()
    out = out.drop(columns=["time"]).rename(columns={"symbol": "index_code"})
    return out.sort_values(["index_code", "trade_date"]).reset_index(drop=True)


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    roll_down = down.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _rolling_z(
    series: pd.Series, window: int = Z_WINDOW, min_periods: int = Z_MIN_PERIODS
) -> pd.Series:
    mean = series.rolling(window, min_periods=min_periods).mean()
    std = series.rolling(window, min_periods=min_periods).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def build_index_tech(px: pd.DataFrame) -> pd.DataFrame:
    """单指数技术面特征（只用尾随窗口）。"""
    df = px.sort_values("trade_date").reset_index(drop=True).copy()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    amount = df["amount"].astype(float)

    ret = close.pct_change()
    out = pd.DataFrame({"trade_date": df["trade_date"], "index_code": df["index_code"]})
    out["idx_ret_1"] = ret
    for window in (3, 5, 10, 20, 60, 120):
        out[f"idx_ret_{window}"] = close.pct_change(window)
    for window in (5, 10, 20, 60, 120):
        ma = close.rolling(window, min_periods=window).mean()
        out[f"idx_ma_gap_{window}"] = close / ma - 1.0

    out["idx_rsi_6"] = _rsi(close, 6)
    out["idx_rsi_14"] = _rsi(close, 14)

    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    out["idx_macd_hist_norm"] = (dif - dea) / close

    ma20 = close.rolling(20, min_periods=20).mean()
    std20 = close.rolling(20, min_periods=20).std(ddof=0)
    out["idx_boll_pctb"] = (close - (ma20 - 2 * std20)) / (
        4 * std20.replace(0.0, np.nan)
    )

    out["idx_vol_10"] = ret.rolling(10, min_periods=10).std(ddof=0)
    out["idx_vol_20"] = ret.rolling(20, min_periods=20).std(ddof=0)
    out["idx_vol_60"] = ret.rolling(60, min_periods=60).std(ddof=0)
    out["idx_vol_ratio_10_60"] = out["idx_vol_10"] / out["idx_vol_60"].replace(
        0.0, np.nan
    )

    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out["idx_atr_pct_14"] = true_range.rolling(14, min_periods=14).mean() / close

    roll_max = close.rolling(Z_WINDOW, min_periods=60).max()
    out["idx_dd_252"] = close / roll_max - 1.0
    out["idx_dist_high_20"] = close / close.rolling(20, min_periods=20).max() - 1.0
    out["idx_dist_low_20"] = close / close.rolling(20, min_periods=20).min() - 1.0

    out["idx_vol_z_20"] = _rolling_z(volume, 20, 20)
    out["idx_amt_z_20"] = _rolling_z(amount, 20, 20)
    return out


# ---------------------------------------------------------------------------
# 市场广度 / 两融 / 宏观 / 基差
# ---------------------------------------------------------------------------


def _load_breadth_universe(universe: str, mapping: dict[str, str]) -> pd.DataFrame:
    path = QUANTDB / "5_technical_derived" / "market_breadth" / "by_universe.parquet"
    frame = pq.read_table(path).to_pandas()
    frame = frame[frame["universe"] == universe].copy()
    frame["trade_date"] = pd.to_datetime(frame["date"]).dt.normalize()
    cols = [c for c in mapping if c in frame.columns]
    missing = set(mapping) - set(cols)
    if missing:
        log.warning("breadth universe=%s 缺列: %s", universe, sorted(missing))
    out = frame[["trade_date", *cols]].rename(columns=mapping)
    return out.sort_values("trade_date").reset_index(drop=True)


def load_market_panel() -> pd.DataFrame:
    """广度 + 两融 + 宏观 + 基差，按交易日合并。"""
    market = _load_breadth_universe("all", MARKET_BREADTH_SOURCE)
    large = _load_breadth_universe("hs300", LARGE_CAP_BREADTH_SOURCE)
    market = market.merge(large, on="trade_date", how="outer")

    # 上涨家数占比（n_up/n_total）不在 _load_breadth_universe 的映射里，单独取
    breadth_raw = pq.read_table(
        QUANTDB / "5_technical_derived" / "market_breadth" / "by_universe.parquet",
        columns=["date", "universe", "n_up", "n_total"],
    ).to_pandas()
    breadth_raw["trade_date"] = pd.to_datetime(breadth_raw["date"]).dt.normalize()
    adv = breadth_raw[breadth_raw["universe"] == "all"][
        ["trade_date", "n_up", "n_total"]
    ].copy()
    adv["mkt_adv_ratio"] = adv["n_up"] / adv["n_total"].replace(0, np.nan)
    market = market.merge(
        adv[["trade_date", "mkt_adv_ratio"]], on="trade_date", how="left"
    )

    for col in BREADTH_ZSCORE_COLUMNS:
        if col in market.columns:
            market[col] = _rolling_z(market[col])

    margin_path = QUANTDB / "5_technical_derived" / "margin" / "panel_market.parquet"
    if margin_path.exists():
        margin = pq.read_table(margin_path).to_pandas()
        margin["trade_date"] = pd.to_datetime(margin["date"]).dt.normalize()
        cols = [c for c in MARGIN_COLUMNS if c in margin.columns]
        market = market.merge(
            margin[["trade_date", *cols]].rename(columns=MARGIN_COLUMNS),
            on="trade_date",
            how="left",
        )
    else:
        log.warning("缺两融面板: %s", margin_path)

    macro_path = OUT_DIR.parent / "market_regime" / "external" / "macro_daily.parquet"
    if macro_path.exists():
        macro = pq.read_table(macro_path).to_pandas()
        macro["trade_date"] = pd.to_datetime(macro["trade_date"]).dt.normalize()
        cols = [c for c in MACRO_COLUMNS if c in macro.columns]
        market = market.merge(macro[["trade_date", *cols]], on="trade_date", how="left")
        market[cols] = market[cols].ffill(limit=5)
    else:
        log.warning("缺宏观数据: %s", macro_path)

    basis_path = (
        OUT_DIR.parent / "market_regime" / "external" / "index_futures_basis.parquet"
    )
    if basis_path.exists():
        basis = pq.read_table(basis_path).to_pandas()
        basis["trade_date"] = pd.to_datetime(basis["trade_date"]).dt.normalize()
        cols = [c for c in BASIS_COLUMNS if c in basis.columns]
        market = market.merge(basis[["trade_date", *cols]], on="trade_date", how="left")
        market[cols] = market[cols].ffill(limit=3)
    else:
        log.warning("缺基差数据: %s", basis_path)

    return market.sort_values("trade_date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 因子截面聚合
# ---------------------------------------------------------------------------


def build_factor_aggregates() -> pd.DataFrame:
    parts = sorted(
        (QUANTDB / "6_ml_datasets" / "l1_l2_factors").glob("dt=*/data.parquet")
    )
    if not parts:
        raise RuntimeError("l1_l2_factors 分区不存在")
    needed = list(
        dict.fromkeys([*FACTOR_AUX_COLUMNS, *(s.factor for s in FACTOR_SPECS)])
    )
    rows: list[dict[str, float | pd.Timestamp]] = []
    started = time.time()
    for i, part in enumerate(parts, 1):
        try:
            frame = pq.read_table(part, columns=needed).to_pandas()
        except Exception as exc:  # pragma: no cover - 数据面异常
            log.warning("skip %s: %s", part, exc)
            continue
        if frame.empty:
            continue
        trade_date = pd.to_datetime(part.parent.name.split("=")[1])
        record = aggregate_partition(frame, FACTOR_SPECS)
        record["trade_date"] = trade_date
        rows.append(record)
        if i % 200 == 0:
            log.info("因子聚合 %d/%d（%.0fs）", i, len(parts), time.time() - started)
    out = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    log.info("因子聚合完成：%d 个交易日，%.0fs", len(out), time.time() - started)

    for col in FACTOR_AGG_COLUMNS:
        out[f"{col}_pct"] = rolling_pct_rank(out[col].to_numpy(float))
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_dataset() -> pd.DataFrame:
    prices = load_index_prices(TARGET_INDICES)
    market = load_market_panel()
    factors = build_factor_aggregates()

    frames: list[pd.DataFrame] = []
    for code in TARGET_INDICES:
        px = prices[prices["index_code"] == code]
        if px.empty:
            log.warning("指数 %s 无量价，跳过", code)
            continue
        tech = build_index_tech(px)
        frame = tech.merge(market, on="trade_date", how="left")
        frame = frame.merge(factors, on="trade_date", how="left")
        labels = compute_labels(
            px["trade_date"], px["close"], px["open"], horizon=HORIZONS[0]
        )
        frame = frame.merge(
            labels[["trade_date", "close", "fwd_ret_1", "fwd_ret_open_1", "y_1"]],
            on="trade_date",
            how="left",
        )
        for horizon in HORIZONS[1:]:
            extra = compute_labels(
                px["trade_date"], px["close"], px["open"], horizon=horizon
            )
            frame = frame.merge(
                extra[
                    [
                        "trade_date",
                        f"fwd_ret_{horizon}",
                        f"fwd_ret_open_{horizon}",
                        f"y_{horizon}",
                    ]
                ],
                on="trade_date",
                how="left",
            )
        frame["index_code"] = code
        frames.append(frame)

    data = pd.concat(frames, ignore_index=True)
    data["split"] = assign_split(data["trade_date"], _split_bounds())
    return data.sort_values(["index_code", "trade_date"]).reset_index(drop=True)


def _split_bounds() -> dict[str, tuple[str, str]]:
    from backend.scripts.market_direction.features import SPLIT_BOUNDS

    return SPLIT_BOUNDS


def write_meta(data: pd.DataFrame, path: Path) -> None:
    feature_cols = [c for c in ALL_FEATURE_COLUMNS if c in data.columns]
    missing = [c for c in ALL_FEATURE_COLUMNS if c not in data.columns]
    meta = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "rows": int(len(data)),
        "indices": sorted(data["index_code"].unique().tolist()),
        "date_min": str(data["trade_date"].min().date()),
        "date_max": str(data["trade_date"].max().date()),
        "feature_count": len(feature_cols),
        "features": feature_cols,
        "missing_feature_columns": missing,
        "split_rows": data.groupby("split").size().to_dict(),
        "missing_rate": {
            col: round(float(data[col].isna().mean()), 4) for col in feature_cols
        },
        "provenance": {
            "factor_source": "quantdb/6_ml_datasets/l1_l2_factors/dt=*",
            "index_prices": "quantdb/1_kline_data/index_daily",
            "breadth": "quantdb/5_technical_derived/market_breadth/by_universe.parquet",
            "margin": "quantdb/5_technical_derived/margin/panel_market.parquet",
            "macro": "data/market_regime/external/macro_daily.parquet",
            "futures_basis": "data/market_regime/external/index_futures_basis.parquet",
        },
        "leakage_guards": [
            "截面聚合只读当日 dt= 分区",
            "滚动分位只用尾随 252 日窗口（min_periods=120）",
            "标签 fwd_ret_* / y_* 不进入特征清单",
            "两融使用 _lag1 字段（T+1 披露）",
            "切分用固定日期边界，训练/验证/测试尾部按 horizon+2 剔除重叠标签",
        ],
    }
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="构建大盘方向模型特征表")
    parser.add_argument("--start", default="2017-01-01", help="保留的起始日期")
    args = parser.parse_args()

    FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    data = build_dataset()
    data = data[data["trade_date"] >= pd.Timestamp(args.start)].reset_index(drop=True)

    out_path = FEATURE_DIR / "market_direction_features.parquet"
    data.to_parquet(out_path, index=False)
    write_meta(data, FEATURE_DIR / "feature_meta.json")
    log.info("写出 %s：%d 行 × %d 列", out_path, data.shape[0], data.shape[1])
    print(
        data.groupby(["index_code", "split"]).size().unstack(fill_value=0).to_string()
    )


if __name__ == "__main__":
    main()
