"""数据加载（P2 由 train.py 拆出，逐行搬运）。

本地 parquet / QuantDB 直读、ST 与北交所过滤、行业合并、泄漏列剔除、
标签构建（T+1 执行口径，见 data.splits._EXECUTION_LAG_DAYS）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from data.splits import _EXECUTION_LAG_DAYS

logger = logging.getLogger("quantmind.train")


def _load_local_parquet(
    local_dir: Path,
    year: int,
    required_columns: list[str],
    clip_start: pd.Timestamp | None = None,
    clip_end: pd.Timestamp | None = None,
) -> pd.DataFrame | None:
    file_path = local_dir / f"model_features_{year}.parquet"
    if not file_path.exists():
        return None
    try:
        logger.info(f"Local data hit: {file_path}")

        schema_cols = set(pq.ParquetFile(file_path).schema_arrow.names)
        selected_cols = [c for c in required_columns if c in schema_cols]
        if "trade_date" not in selected_cols or "symbol" not in selected_cols:
            logger.warning(
                "Skip parquet missing required base columns trade_date/symbol: %s",
                file_path,
            )
            return None
        df = pd.read_parquet(file_path, columns=selected_cols, engine="pyarrow")

        # 先按日期裁剪每年数据，避免把无关年份全量堆进内存
        if "trade_date" in df.columns and (clip_start is not None or clip_end is not None):
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            mask = pd.Series(True, index=df.index)
            if clip_start is not None:
                mask &= df["trade_date"] >= clip_start
            if clip_end is not None:
                mask &= df["trade_date"] <= clip_end
            df = df.loc[mask].copy()

        # 数值列统一降为 float32，降低内存峰值
        for col in df.columns:
            if col in {"trade_date", "symbol"}:
                continue
            if pd.api.types.is_numeric_dtype(df[col]):
                df[col] = df[col].astype(np.float32, copy=False)

        return df
    except Exception as exc:
        logger.warning(f"  ⚠ Failed to read local parquet {file_path}: {exc}")
        return None


# ── 数据加载 ──────────────────────────────────────────────────────────────────
_MARKET_PARQUET_FILES: dict[str, str] = {
    "HK": "model_features_hk.parquet",
    "US": "model_features_us.parquet",
    "CRYPTO": "model_features_crypto.parquet",
    "FUTURES": "model_features_futures.parquet",
}
# 各市场 6_ml_datasets 数据根目录（训练容器内环境变量，由编排器挂载设置）
_MARKET_DATA_DIR_ENV: dict[str, str] = {
    "CN": "QUANTDB_DATA_DIR",
    "HK": "QUANTHK_DATA_DIR",
    "US": "QUANTUS_DATA_DIR",
    "CRYPTO": "QUANTBC_DATA_DIR",
    "FUTURES": "QUANTFUTURES_DATA_DIR",
}


def load_data(
    train_start: str,
    train_end: str,
    features: list[str],
    target_horizon_days: int = 1,
    target_mode: str = "return",
    cache_dir: str | None = None,
    valid_end: str | None = None,
    test_end: str | None = None,
    source_mode: str = "LOCAL",
    local_dir: str | None = None,
    market: str = "CN",
    industry_as_feature: bool = False,
    factor_source: str | None = None,
    quantdb_dir: str | None = None,
    factor_field_sources: dict[str, str] | None = None,
) -> tuple:
    local_root = Path(local_dir).expanduser() if local_dir else None
    if local_root is None:
        raise RuntimeError("local_dir must be provided; COS data download has been removed")

    market_upper = str(market or "CN").upper()

    # 仅读取训练必需列，避免整表加载导致 OOM
    horizon = max(1, int(target_horizon_days or 1))
    horizon_col = f"mom_ret_{horizon}d"
    required_columns = list(
        dict.fromkeys(
            ["trade_date", "symbol", "mom_ret_1d", horizon_col, "is_st", "volume"]
            + list(features)
        )
    )
    # features_daily.return_Nd 是未来 N 日收益（return_1d[T] == pct_change[T+1]），
    # 曾被别名映射为 mom_ret_Nd 当特征使用，导致标签泄漏与虚高 RankIC。
    # 现只读取 l1_factors 提供的 mom_ret_Nd（过去收益），不做任何回退映射。
    _read_columns = list(required_columns)
    logger.info(
        "Memory-optimized read: selected %d columns (horizon=%s, market=%s)",
        len(required_columns),
        horizon,
        market_upper,
    )

    # 给标签构建预留边界，避免裁剪过早影响 shift/rolling
    range_start = pd.Timestamp(train_start) - pd.Timedelta(days=max(7, horizon + 3))
    upper_bound = test_end or valid_end or train_end
    range_end = pd.Timestamp(upper_bound) + pd.Timedelta(days=max(7, horizon + 3))

    direct_factor_source = str(factor_source or "").strip()
    if direct_factor_source and market_upper in _MARKET_DATA_DIR_ENV:
        # Direct QuantDB mode: one factor source only, never materialise or merge snapshots.
        # 与 A 股一致：直接读该市场 6_ml_datasets 下的因子分区
        # （HK: l1_factors/ccass_factors/south_factors）。
        from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader

        reader = QuantDBFactorReader(
            quantdb_dir or os.getenv(_MARKET_DATA_DIR_ENV[market_upper]) or None,
            market=market_upper,
        )
        # 标签构建缓冲(range_start)可能早于数据可用起点(如 train_start 恰为数据首日)。
        # 数据缺失部分无法提供，钳制到数据起点即可，避免 assert_ready 越界抛错。
        _status = reader.describe(direct_factor_source)
        if _status.min_date:
            range_start = max(range_start, pd.Timestamp(_status.min_date))
        if _status.max_date:
            range_end = min(range_end, pd.Timestamp(_status.max_date))
        df = reader.read_range(
            direct_factor_source,
            features=features,
            feature_sources=factor_field_sources,
            start=range_start.date(),
            end=range_end.date(),
        )
        logger.info(
            "Direct QuantDB factor source %s: %d rows, %s to %s",
            direct_factor_source,
            len(df),
            df["trade_date"].min() if not df.empty else "N/A",
            df["trade_date"].max() if not df.empty else "N/A",
        )
        # 与 core parquet 分支一致：数值列统一降为 float32，降低内存峰值。
        # Direct QuantDB 读取默认 float64，325 列 × 440 万行 ≈ 11.5GB；
        # 后续 drop/holiday 过滤/sort_values 各复制一次，峰值会突破
        # 训练容器 48GB mem_limit 被 OOM(SIGKILL 137) 杀死。
        # 标签构建/IC 计算/LightGBM 全部接受 float32，精度损失可忽略。
        _direct_f32_start = time.time()
        for col in df.columns:
            if col in {"trade_date", "symbol"}:
                continue
            if pd.api.types.is_numeric_dtype(df[col]):
                df[col] = df[col].astype(np.float32, copy=False)
        logger.info(
            "Direct QuantDB columns downcast to float32 in %.1fs",
            time.time() - _direct_f32_start,
        )
    elif market_upper in _MARKET_PARQUET_FILES:
        # ── 非 A 股市场：从单一 parquet 文件加载 ──
        parquet_name = _MARKET_PARQUET_FILES[market_upper]
        parquet_path = local_root / parquet_name
        if not parquet_path.exists():
            raise RuntimeError(
                f"市场 {market_upper} parquet 文件不存在: {parquet_path}"
            )
        logger.info("Loading market-specific parquet: %s", parquet_path)

        # 非 A 股文件使用 'instrument' 列而非 'symbol'
        # 先检查 parquet schema，过滤掉不存在的列（如 mom_ret_2d）
        schema_cols = set(pq.ParquetFile(parquet_path).schema_arrow.names)
        # symbol/instrument 列名兼容
        has_instrument = "instrument" in schema_cols
        valid_cols = []
        missing_cols = []
        for c in _read_columns:
            if c in schema_cols:
                valid_cols.append(c)
            elif c == "symbol" and has_instrument:
                valid_cols.append("instrument")
            else:
                missing_cols.append(c)
        if missing_cols:
            logger.warning("Columns not in parquet (skipped): %s", missing_cols)

        try:
            df = pd.read_parquet(parquet_path, columns=valid_cols, engine="pyarrow")
        except Exception:
            df = pd.read_parquet(parquet_path, columns=valid_cols, engine="pyarrow")
        if "instrument" in df.columns and "symbol" not in df.columns:
            df = df.rename(columns={"instrument": "symbol"})

        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df = df[df["trade_date"].notna()].copy()
        # 日期裁剪
        mask = (df["trade_date"] >= range_start) & (df["trade_date"] <= range_end)
        df = df.loc[mask].copy()
        logger.info("Market %s raw data: %d rows, date range: %s to %s",
                     market_upper, len(df),
                     df["trade_date"].min() if not df.empty else "N/A",
                     df["trade_date"].max() if not df.empty else "N/A")
    else:
        # ── A 股：优先使用 core parquet（78列），回退到年度 parquet 文件 ──
        core_parquet_path = local_root / "model_features_core.parquet"

        if core_parquet_path.exists():
            # 使用精简版 core parquet（78列，内存友好）
            logger.info("Using core parquet (78 factors): %s", core_parquet_path)

            schema_cols = set(pq.ParquetFile(core_parquet_path).schema_arrow.names)
            valid_cols = [c for c in _read_columns if c in schema_cols]
            missing_cols = [c for c in _read_columns if c not in schema_cols]
            if missing_cols:
                logger.warning("Columns not in core parquet (skipped): %s", missing_cols)

            if "trade_date" not in valid_cols or "symbol" not in valid_cols:
                raise RuntimeError("Core parquet missing required columns: trade_date or symbol")

            df = pd.read_parquet(core_parquet_path, columns=valid_cols, engine="pyarrow")
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df[df["trade_date"].notna()].copy()

            # 日期裁剪
            mask = (df["trade_date"] >= range_start) & (df["trade_date"] <= range_end)
            df = df.loc[mask].copy()

            # 数值列统一降为 float32
            for col in df.columns:
                if col in {"trade_date", "symbol"}:
                    continue
                if pd.api.types.is_numeric_dtype(df[col]):
                    df[col] = df[col].astype(np.float32, copy=False)

            logger.info("Core parquet loaded: %d rows, date range: %s to %s",
                       len(df),
                       df["trade_date"].min() if not df.empty else "N/A",
                       df["trade_date"].max() if not df.empty else "N/A")
        else:
            # 回退到年度 parquet 文件（197列，内存占用大）
            logger.warning("Core parquet not found, falling back to yearly parquet files")
            start_year = pd.Timestamp(train_start).year
            ends = [train_end]
            if valid_end:
                ends.append(valid_end)
            if test_end:
                ends.append(test_end)
            end_year = max(pd.Timestamp(e).year for e in ends)

            chunks = []
            for year in range(max(start_year - 1, 2016), end_year + 1):
                df_year = _load_local_parquet(
                    local_root,
                    year,
                    required_columns=_read_columns,
                    clip_start=range_start,
                    clip_end=range_end,
                )
                if df_year is not None:
                    if not df_year.empty:
                        chunks.append(df_year)
                else:
                    logger.warning(f"No data file found for year {year} in {local_root}, skipping")

            if not chunks:
                raise RuntimeError("No data loaded from local storage")

            df = pd.concat(chunks, axis=0, ignore_index=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df[df["trade_date"].notna()].copy()
            logger.info(f"Raw concat size: {len(df)} rows. Date range: {df['trade_date'].min()} to {df['trade_date'].max()}")

        # 过滤北交所代码（4/8开头）——仅 A 股
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        df = df[~df["symbol"].str.startswith(("4", "8"))].copy()
        logger.info(f"After symbol filter: {len(df)} rows")

        # 过滤 ST/*ST 股票
        if "is_st" in df.columns:
            before = len(df)
            df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(int)
            df = df[df["is_st"] == 0].copy()
            logger.info(f"After ST filter: {len(df)} rows (removed {before - len(df)} ST rows)")

        # 行业条件化：合并 ind_code_l1（CSRC 一级行业编码）
        if industry_as_feature or "ind_code_l1" in features:
            try:
                # 优先从 QuantDB 全量挂载目录查找，回退到 feature_snapshots 子目录
                _qdb_mount = os.getenv("QUANTDB_DATA_DIR", "/tmp/quantdb_data")
                _sector_dirs = [
                    Path(_qdb_mount) / "2_base_sector",  # local_docker_orchestrator 挂载的 QuantDB 全量数据
                    local_root / "2_base_sector",  # feature_snapshots 内的子目录（兼容旧部署）
                ]
                ind_detail_path = None
                for _d in _sector_dirs:
                    for _name in ("instrument_list.parquet", "instrument_detail.parquet"):
                        _p = _d / "instrument_detail" / _name
                        if _p.exists():
                            ind_detail_path = _p
                            break
                    if ind_detail_path is not None:
                        break
                if ind_detail_path is not None:
                    ind_df = pd.read_parquet(ind_detail_path, engine="pyarrow")
                    sym_col = "symbol" if "symbol" in ind_df.columns else "wind_code" if "wind_code" in ind_df.columns else None
                    if sym_col and "rs_hycode_sim" in ind_df.columns:
                        ind_map = ind_df[[sym_col, "rs_hycode_sim"]].dropna()
                        ind_map = ind_map.rename(columns={sym_col: "symbol", "rs_hycode_sim": "ind_code_l1"})
                        ind_map["symbol"] = ind_map["symbol"].astype(str).str.zfill(6)
                        ind_map["ind_code_l1"] = pd.Categorical(ind_map["ind_code_l1"]).codes.astype(np.float32)
                        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
                        df = df.merge(ind_map, on="symbol", how="left")
                        # 缺失行业映射到 max(code)+1 的独立类别 id（而非 fillna(-1) 或 0）：
                        # 负 id 会被 CatBoost 拒绝；并入 0 会与第一个真实行业混淆。
                        _ind_max = float(ind_map["ind_code_l1"].max()) if len(ind_map) else -1.0
                        df["ind_code_l1"] = df["ind_code_l1"].fillna(_ind_max + 1).astype(np.float32)
                        logger.info("Industry mapping merged: %d/%d rows have ind_code_l1",
                                    (df["ind_code_l1"] >= 0).sum(), len(df))
                    else:
                        logger.warning("instrument_detail.parquet missing symbol/wind_code or rs_hycode_sim columns")
                else:
                    logger.warning("instrument_detail.parquet not found (searched: %s)", ", ".join(str(d) for d in _sector_dirs))
            except Exception as e:
                logger.warning("Failed to merge industry data (non-fatal): %s", e)

    # ── 丢弃 features_daily.return_Nd：这些列是【未来 N 日收益】 ──
    # return_1d[T] == pct_change[T+1]，当特征使用会直接泄漏标签。
    # 历史上曾把它们重命名为 mom_ret_Nd，导致 RankIC 虚高到 0.7+。
    # 仅旧快照 parquet（A 股 features_daily 血统）携带该列；直读因子源
    # （l1/l2/ccass/south）的 return_Nd 为过去收益（pct_change 口径），
    # 不适用此剔除（HK l1_factors 的 return_1d 即过去 1 日收益）。
    _LEAKY_RETURN_COLS = (
        "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
    )
    if not direct_factor_source:
        _leaky_present = [c for c in _LEAKY_RETURN_COLS if c in df.columns]
        if _leaky_present:
            df = df.drop(columns=_leaky_present, errors="ignore")
            logger.warning(
                "Dropped forward-looking return columns (label leakage): %s", _leaky_present
            )

    # 如果仍缺 mom_ret_1d，尝试从 pct_change 或 close 构建
    if "mom_ret_1d" not in df.columns:
        if "pct_change" in df.columns:
            df["mom_ret_1d"] = pd.to_numeric(df["pct_change"], errors="coerce") / 100.0
            logger.info("Built mom_ret_1d from pct_change column")
        elif "close" in df.columns:
            df["mom_ret_1d"] = df.groupby("symbol")["close"].pct_change(1)
            logger.info("Built mom_ret_1d from close column pct_change")
        else:
            raise RuntimeError("Column 'mom_ret_1d' not found and cannot be constructed (no pct_change or close)")

    # 剔除节假日填充行：QuantDB parquet 含约 6.6% 的假交易日
    # （close>0、mom_ret_1d=0，但全市场 volume==0），如春节/清明/劳动节。
    # 必须在 label 构造前剔除：shift(-N) 按行位移，若序列含假日，
    # "未来 N 个交易日收益" 实际只跨 N-k 个真实交易日，导致标签时间尺度不一致。
    if "volume" in df.columns:
        _day_vol = df.groupby("trade_date")["volume"].max()
        _real_days = _day_vol[_day_vol > 0].index
        _dropped_days = len(_day_vol) - len(_real_days)
        if _dropped_days > 0:
            _rows_before = len(df)
            df = df[df["trade_date"].isin(_real_days)].copy()
            logger.info(
                "Dropped %d non-trading days (holiday fill rows): %d -> %d rows",
                _dropped_days, _rows_before, len(df),
            )
    else:
        logger.warning(
            "Column 'volume' unavailable — cannot filter holiday fill rows; "
            "labels may span fewer real trading days than target_horizon_days"
        )

    # 标签：基于 target_horizon_days 构建 N 日远期收益
    # 注：mom_ret_{N}d 列是过去 N 日收益（backward-looking），如 mom_ret_5d[T] = (close[T]-close[T-5])/close[T-5]
    # shift(-N) 后，行 T 得到行 T+N 的值 = (close[T+N]-close[T])/close[T]，即正确的 N 日远期收益
    # 等价于: label = next_N_day_return = pct_change(N).shift(-N)
    # 从参数读取预测周期（不依赖全局 cfg）
    _horizon = max(1, int(target_horizon_days or 1))

    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    _mom_col = f"mom_ret_{_horizon}d"
    if direct_factor_source:
        # Raw factor sources carry close, so labels are always true forward returns.
        _lag = _EXECUTION_LAG_DAYS
        execution_close = df.groupby("symbol")["close"].shift(-_lag)
        future_close = df.groupby("symbol")["close"].shift(-(_lag + _horizon))
        df["label"] = future_close / execution_close - 1.0
    elif _horizon == 1:
        # mom_ret_1d[T+2] = close[T+2] / close[T+1] - 1，匹配 T+1 执行。
        df["label"] = df.groupby("symbol")["mom_ret_1d"].shift(-(_horizon + _EXECUTION_LAG_DAYS))
    elif _mom_col in df.columns:
        # mom_ret_H[T+1+H] = close[T+1+H] / close[T+1] - 1。
        df["label"] = df.groupby("symbol")[_mom_col].shift(-(_horizon + _EXECUTION_LAG_DAYS))
    else:
        # 回退：通过滚动累乘 1d 收益构造 N 日远期收益
        df["label"] = (
            df.groupby("symbol")["mom_ret_1d"]
            .transform(lambda s: (1 + s).rolling(_horizon).apply(np.prod, raw=True) - 1)
            .shift(-(_horizon + _EXECUTION_LAG_DAYS))
        )
    logger.info(
        "Label built with target_horizon_days=%s (%s)",
        _horizon,
        "direct close" if direct_factor_source else _mom_col if _mom_col in df.columns else "rolling",
    )

    valid_count_before = len(df)
    df = df[df["label"].notna()].copy()
    logger.info(f"After label shift & dropna: {len(df)} rows (dropped {valid_count_before - len(df)} rows with missing labels)")

    # 分类目标保留为 0/1，不能再做截面 rank；否则 binary objective 会收到
    # 连续标签而退化成语义不明确的回归任务。
    _target_mode = str(target_mode or "return").lower()
    if _target_mode == "classification":
        from preprocessing import binarize_labels
        df["label"] = binarize_labels(df["label"].to_numpy())
        _n_pos = int((df["label"] == 1).sum())
        _n_neg = int((df["label"] == 0).sum())
        logger.info(f"Classification target: positive={_n_pos}, negative={_n_neg} (ratio={_n_pos / max(1, _n_pos + _n_neg):.3f})")

    # 裁剪到请求日期范围
    mask = (df["trade_date"] >= train_start) & (df["trade_date"] <= train_end)
    # 如果有验证集/测试集，扩大 mask 范围以包含它们
    if valid_end:
        mask = (df["trade_date"] >= train_start) & (df["trade_date"] <= valid_end)
    if test_end:
        mask = (df["trade_date"] >= train_start) & (df["trade_date"] <= test_end)

    df = df[mask].copy()
    logger.info(f"After date range clip ({train_start} to {test_end or valid_end or train_end}): {len(df)} rows")

    # 校验特征列
    missing = [f for f in features if f not in df.columns]
    if missing:
        logger.warning(f"Features not found in parquet (ignored): {missing}")
        features = [f for f in features if f in df.columns]
    if not features:
        raise RuntimeError("No valid feature columns found")

    keep_cols = ["symbol", "trade_date", "label"] + features
    df = df[keep_cols].reset_index(drop=True)

    # 收益预测使用截面 rank 目标，强调同日选股排序；分类预测保持二元标签。
    if _target_mode != "classification":
        df["label"] = df.groupby("trade_date")["label"].rank(pct=True) - 0.5

    logger.info(
        f"Data ready: {len(df):,} rows, {len(features)} features, "
        f"{df['trade_date'].min().date()} ~ {df['trade_date'].max().date()}"
    )
    return df, features
