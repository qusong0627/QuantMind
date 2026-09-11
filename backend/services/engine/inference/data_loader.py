"""
Shared data loading utilities for inference and backtesting.

Extracted from inference_parquet.py template to avoid code duplication.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .trading_cost import limit_threshold

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"

# 前瞻标签列名。绝不能与任何特征列同名 —— 见 load_forward_labels 文档。
FORWARD_RETURN_COL = "fwd_return"


def _quantdb_reader(meta: dict, data_dir: Path):
    """Return the direct reader only for models explicitly bound to QuantDB.

    Old models deliberately remain on their immutable parquet snapshots.
    """
    if meta.get("data_source") != "quantdb_factors":
        return None
    from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader

    pinned_dir = Path(str(meta.get("quantdb_dir") or ""))
    return QuantDBFactorReader(pinned_dir if pinned_dir.is_dir() else data_dir)


def resolve_parquet_path(data_dir: Path, trade_date: str, meta: dict | None = None) -> Path | None:
    """Resolve parquet file path based on market context and date."""
    meta = meta or {}
    market = ""
    ctx = meta.get("context")
    if isinstance(ctx, dict):
        market = str(ctx.get("market", "")).upper()

    _MARKET_PARQUET: dict[str, str] = {
        "HK": "model_features_hk.parquet",
        "US": "model_features_us.parquet",
        "CRYPTO": "model_features_crypto.parquet",
        "FUTURES": "model_features_futures.parquet",
    }

    if market in _MARKET_PARQUET:
        p = Path(data_dir) / _MARKET_PARQUET[market]
        if p.exists():
            return p
        logger.warning("市场 parquet 文件不存在: %s", p)

    # CN or fallback: year-based parquet
    year = int(trade_date[:4])
    p = Path(data_dir) / f"model_features_{year}.parquet"
    if p.exists():
        return p

    # Legacy: no year suffix
    p = Path(data_dir) / "model_features.parquet"
    if p.exists():
        return p

    return None


def filter_untradable_rows(
    df: pd.DataFrame,
    exclude_limit_moves: bool = False,
) -> pd.DataFrame:
    """Filter untradable rows (suspended, zero volume, ST stocks).

    exclude_limit_moves: 额外剔除信号日触及涨跌停的标的。回测须开启 ——
    信号日涨停的股票次日一字板买不进，计入组合会高估收益。推理路径保持
    默认关闭，以免改变现有线上行为。
    """
    if df.empty:
        return df

    filtered = df.copy()

    if "close" in filtered.columns:
        filtered = filtered.loc[
            pd.to_numeric(filtered["close"], errors="coerce") > 0
        ].copy()

    if "volume" in filtered.columns:
        filtered = filtered.loc[
            pd.to_numeric(filtered["volume"], errors="coerce") > 0
        ].copy()

    if "is_st" in filtered.columns:
        filtered = filtered.loc[
            pd.to_numeric(filtered["is_st"], errors="coerce") != 1
        ].copy()

    if exclude_limit_moves and "pctchange" in filtered.columns:
        pct = pd.to_numeric(filtered["pctchange"], errors="coerce")
        if "listing_market" in filtered.columns:
            threshold = filtered["listing_market"].map(limit_threshold)
        else:
            threshold = pd.Series(limit_threshold(None), index=filtered.index)
        at_limit = pct.abs() >= threshold
        dropped = int(at_limit.sum())
        if dropped:
            logger.info("剔除触及涨跌停的标的: %d 条", dropped)
            filtered = filtered.loc[~at_limit.fillna(False)].copy()

    return filtered


def load_date_data(
    trade_date: str,
    data_dir: Path | str | None = None,
    meta: dict | None = None,
    exclude_limit_moves: bool = False,
) -> pd.DataFrame | None:
    """Load feature data for a specific date. Returns None if no data available."""
    data_dir = Path(data_dir) if data_dir else Path(_DEFAULT_DATA_DIR)
    meta = meta or {}

    reader = _quantdb_reader(meta, data_dir)
    if reader is not None:
        source = str(meta.get("factor_source") or "l1_l2_factors")
        features = list(meta.get("feature_columns") or meta.get("features") or [])
        try:
            status = reader.assert_ready(source, start=trade_date, end=trade_date)
            expected_hash = str(meta.get("factor_schema_hash") or "")
            if expected_hash and expected_hash != status.schema_hash:
                raise RuntimeError(
                    f"QuantDB schema changed for {source}: expected {expected_hash}, got {status.schema_hash}"
                )
            day_df = reader.read_day(
                source, features=features, trade_date=trade_date,
                feature_sources=meta.get("factor_field_sources") or None,
            )
        except Exception as exc:
            logger.error("QuantDB direct inference cannot load %s: %s", trade_date, exc)
            return None
        day_df["trade_date"] = pd.to_datetime(day_df["trade_date"]).dt.strftime("%Y-%m-%d")
    else:
        parquet_path = resolve_parquet_path(data_dir, trade_date, meta)
        if parquet_path is None:
            logger.warning(
                "找不到可用的 parquet 文件 (data_dir=%s, market=%s)",
                data_dir, (meta.get("context") or {}).get("market", ""),
            )
            return None

        df = pd.read_parquet(parquet_path, engine="pyarrow")
        # Non-A-share parquet uses 'instrument' column instead of 'symbol'
        if "symbol" not in df.columns and "instrument" in df.columns:
            df = df.rename(columns={"instrument": "symbol"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        day_df = df[df["trade_date"] == trade_date].copy()

    if len(day_df) == 0:
        logger.warning("日期 %s 在 parquet 中无数据", trade_date)
        return None

    # 同一 (symbol, trade_date) 若有重复行，后续 set_index 对齐会抛错并静默丢弃整天
    day_df = day_df.drop_duplicates(subset=["symbol"], keep="last")

    before_filter = len(day_df)
    day_df = filter_untradable_rows(day_df, exclude_limit_moves=exclude_limit_moves)
    after_filter = len(day_df)
    if before_filter != after_filter:
        logger.info(
            "过滤不可交易记录: %d -> %d (剔除 %d 条)",
            before_filter, after_filter, before_filter - after_filter,
        )

    if len(day_df) == 0:
        logger.warning("日期 %s 过滤后无可交易数据", trade_date)
        return None

    return day_df


def load_forward_labels(
    dates: list[str],
    horizon: int,
    data_dir: Path | str | None = None,
    meta: dict | None = None,
    signal_lag_days: int = 1,
) -> pd.DataFrame:
    """构造真正的 N 日**前瞻**收益，作为回测评估的 ground truth。

    这是回测正确性的关键。parquet 里的 `mom_ret_{N}d` 是**过去** N 日收益
    (close.pct_change(N))，而且它本身就在很多模型的 feature_columns 里 ——
    直接拿它当 "实际收益" 算 IC，度量的是 "模型能否复现自己的输入"，
    会得到虚高且毫无意义的 IC。

    这里用原始 close 重建：fwd_return[T] = close[T+N] / close[T] - 1，
    与训练侧 docker/training/train.py 的 label 定义一致。用 close 而非
    mom_ret.shift(-N) 的原因：close 无复权口径歧义，且 horizon 取非标准值
    (如 7) 时 mom_ret_7d 列并不存在。

    signal_lag_days: A 股 T+1 结算约束。默认 1，表示 T 日信号在 T+1 才执行。
        当 lag=1 时，返回标签从 signal_date 对应的 T+1 日开始：
        每条记录 (signal_date=T, symbol, fwd_return) 中，
        fwd_return = close[T+1+N] / close[T+1] - 1。
        这确保了回测收益从实际可执行的日期开始计算，避免前视偏差。

    返回 [symbol, trade_date, fwd_return]，末尾 horizon 个交易日自然为 NaN
    并被剔除 —— 这也是判断实现是否正确的信号：若最后一天仍能算出标签，
    说明又用回了过去收益。
    """
    horizon = max(1, int(horizon))
    signal_lag_days = max(0, int(signal_lag_days))
    data_dir = Path(data_dir) if data_dir else Path(_DEFAULT_DATA_DIR)
    meta = meta or {}

    if not dates:
        return pd.DataFrame(columns=["symbol", "trade_date", FORWARD_RETURN_COL])

    reader = _quantdb_reader(meta, data_dir)
    if reader is not None:
        source = str(meta.get("factor_source") or "l1_l2_factors")
        try:
            # Include enough trailing dates for a valid forward return window.
            available = reader.available_dates(source, start=min(dates))
            if not available:
                return pd.DataFrame(columns=["symbol", "trade_date", FORWARD_RETURN_COL])
            max_needed = available[min(len(available) - 1, len(dates) + horizon + signal_lag_days - 1)]
            frame = reader.read_range(
                source, features=[], start=min(dates), end=max_needed,
            )
            labels = reader.forward_labels(
                frame, horizon=horizon, signal_lag_days=signal_lag_days,
            ).rename(columns={"label": FORWARD_RETURN_COL})
            labels["trade_date"] = pd.to_datetime(labels["trade_date"]).dt.strftime("%Y-%m-%d")
            return labels[labels["trade_date"].isin(dates)].dropna(subset=[FORWARD_RETURN_COL])
        except Exception as exc:
            logger.error("构造 QuantDB 前瞻标签失败: %s", exc)
            return pd.DataFrame(columns=["symbol", "trade_date", FORWARD_RETURN_COL])

    # 需要读取的年度 parquet：回测区间 + 末尾 horizon 天可能跨年
    years = {int(d[:4]) for d in dates if len(d) >= 4}
    years |= {max(years) + 1}
    paths: list[Path] = []
    for year in sorted(years):
        path = resolve_parquet_path(data_dir, f"{year}-01-01", meta)
        if path is not None and path not in paths:
            paths.append(path)

    if not paths:
        logger.warning("构造前瞻标签失败：找不到 parquet (data_dir=%s)", data_dir)
        return pd.DataFrame(columns=["symbol", "trade_date", FORWARD_RETURN_COL])

    wanted = ["symbol", "instrument", "trade_date", "close", "volume", "is_st"]
    frames: list[pd.DataFrame] = []
    for path in paths:
        try:
            cols = _existing_columns(path, wanted)
            frames.append(pd.read_parquet(path, engine="pyarrow", columns=cols))
        except Exception as exc:
            logger.warning("读取前瞻标签数据失败 %s: %s", path.name, exc)

    if not frames:
        return pd.DataFrame(columns=["symbol", "trade_date", FORWARD_RETURN_COL])

    df = pd.concat(frames, ignore_index=True)
    if "symbol" not in df.columns and "instrument" in df.columns:
        df = df.rename(columns={"instrument": "symbol"})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")

    # 停牌/退市行的 close 为 0 或负，参与 pct_change 会产出 inf
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.loc[df["close"] > 0].copy()

    # 剔除节假日填充行（close>0 但全市场 volume==0）。shift(-N) 按行位移，
    # 序列里混入假日会让 "未来 N 个交易日" 实际只跨 N-k 个真实交易日。
    # 当前 feature_snapshots 数据源无此类行，保留是为了防御数据源变化。
    if "volume" in df.columns:
        day_volume = df.groupby("trade_date")["volume"].max()
        real_days = day_volume[day_volume > 0].index
        dropped_days = len(day_volume) - len(real_days)
        if dropped_days > 0:
            logger.info("构造标签时剔除 %d 个非交易日（假日填充行）", dropped_days)
            df = df[df["trade_date"].isin(real_days)].copy()

    df = df.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    # 信号延迟：signal_lag_days > 0 时，T 日信号在 T+lag 日才执行。
    # 因此标签从 T+lag 开始计算：fwd_return = close[T+lag+horizon] / close[T+lag] - 1
    # 实现方式：将 trade_date 向前移 lag 天，然后按常规方式计算 label。
    # 移完后 trade_date 列表示"执行日"，原 trade_date 对应"信号日"。
    if signal_lag_days > 0:
        # 将 trade_date 转为 datetime 以便 shift
        df["trade_date_dt"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values(["symbol", "trade_date_dt"]).reset_index(drop=True)
        # 先按全量序列取执行日（T+lag）的日期与收盘价，再裁掉无执行日的首部行。
        # 若先裁行再 shift，末日执行价会丢失，连带少算一批本可计算的信号标签。
        df["exec_date"] = df.groupby("symbol")["trade_date_dt"].shift(-signal_lag_days)
        df["exec_close"] = df.groupby("symbol")["close"].shift(-signal_lag_days)
        df = df[df["exec_date"].notna() & df["exec_close"].notna()].copy()
        df["close"] = df["exec_close"]
        df["trade_date"] = df["trade_date_dt"].dt.strftime("%Y-%m-%d")
        df = df.drop(columns=["trade_date_dt", "exec_date", "exec_close"])
        df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    future_close = df.groupby("symbol")["close"].shift(-horizon)
    df[FORWARD_RETURN_COL] = future_close / df["close"] - 1.0
    df[FORWARD_RETURN_COL] = df[FORWARD_RETURN_COL].replace(
        [np.inf, -np.inf], np.nan
    )

    labels = df.loc[
        df[FORWARD_RETURN_COL].notna(),
        ["symbol", "trade_date", FORWARD_RETURN_COL],
    ].copy()

    if labels.empty:
        logger.warning(
            "前瞻标签为空 (horizon=%d, lag=%d)：数据末尾不足 %d 个交易日",
            horizon, signal_lag_days, horizon + signal_lag_days,
        )
    else:
        logger.info(
            "前瞻标签构造完成: %d 行, 最后可标注日期 %s (horizon=%d, lag=%d)",
            len(labels), labels["trade_date"].max(), horizon, signal_lag_days,
        )

    return labels


def _existing_columns(path: Path, wanted: list[str]) -> list[str]:
    """只取 parquet 里真实存在的列，避免 pyarrow 因缺列直接抛错。"""
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(path).schema.names)
    return [c for c in wanted if c in names]


def preprocess(
    df: pd.DataFrame,
    meta: dict,
) -> tuple[pd.DataFrame, list[str]]:
    """Prepare features according to metadata, return (X_df, symbols)."""
    feature_cols = meta.get("feature_columns") or meta.get("features", [])
    fill_values = meta.get("fill_values", {})

    # features_daily.return_Nd 是未来 N 日收益，不能当作 mom_ret_Nd 使用：
    # 训练侧已改为只用 l1_factors 的 mom_ret_Nd（过去收益），推理侧必须一致，
    # 否则线上会把未来收益喂给模型，导致预测分布与训练时完全不同。
    _leaky = [
        c for c in ("return_1d", "return_3d", "return_5d",
                    "return_10d", "return_20d", "return_60d")
        if c in df.columns
    ]
    if _leaky:
        df = df.drop(columns=_leaky, errors="ignore")
        logger.warning("Dropped forward-looking return columns: %s", _leaky)

    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        logger.warning("缺少 %d 个特征列，将填 0: %s", len(missing), missing[:8])
        for c in missing:
            df = df.copy()
            df[c] = 0.0

    X_df = df[feature_cols].copy()

    for col, val in fill_values.items():
        if col in X_df.columns:
            X_df[col] = X_df[col].fillna(val)
    X_df = X_df.fillna(0.0)

    symbols = df["symbol"].tolist()
    return X_df, symbols


def get_available_dates(
    data_dir: Path | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    meta: dict | None = None,
) -> list[str]:
    """Get list of available trading dates from parquet data."""
    data_dir = Path(data_dir) if data_dir else Path(_DEFAULT_DATA_DIR)
    meta = meta or {}

    reader = _quantdb_reader(meta, data_dir)
    if reader is not None:
        try:
            return reader.available_dates(
                str(meta.get("factor_source") or "l1_l2_factors"),
                start=start_date, end=end_date,
            )
        except Exception as exc:
            logger.error("读取 QuantDB 交易日失败: %s", exc)
            return []

    # Collect all parquet files
    parquet_files = sorted(data_dir.glob("model_features_*.parquet"))
    if not parquet_files:
        legacy = data_dir / "model_features.parquet"
        if legacy.exists():
            parquet_files = [legacy]

    if not parquet_files:
        return []

    dates: set[str] = set()
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf, columns=["trade_date"], engine="pyarrow")
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
            dates.update(df["trade_date"].unique())
        except Exception as e:
            logger.warning("读取 parquet 日期失败 %s: %s", pf.name, e)

    sorted_dates = sorted(dates)
    if start_date:
        sorted_dates = [d for d in sorted_dates if d >= start_date]
    if end_date:
        sorted_dates = [d for d in sorted_dates if d <= end_date]

    return sorted_dates
