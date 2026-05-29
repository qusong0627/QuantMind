#!/usr/bin/env python3
"""
QuantMind 云端训练脚本 (CVM 容器内运行)
=========================================
参数传递方式：YAML 配置文件（固化在镜像中，参数通过挂载的 config.yaml 传入）

用法：
  docker run -v /host/workspace:/workspace quantmind:latest --config /workspace/config.yaml

config.yaml 结构：
  run_id / job_name
  data.train_start / data.train_end / data.features
  model.type / model.num_boost_round / model.val_ratio / model.params
  output.result_path
  callback.url / callback.secret
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("quantmind.train")

# ── LightGBM 默认参数 ────────────────────────────────────────────────────────
DEFAULT_LGB_PARAMS: dict[str, Any] = {
    "objective":         "regression",
    "metric":            "l2",
    "boosting":          "gbdt",
    "num_leaves":        127,
    "learning_rate":     0.05,
    "feature_fraction":  0.8,
    "bagging_fraction":  0.8,
    "bagging_freq":      5,
    "min_child_samples": 50,
    "n_jobs":            -1,
    "verbosity":         -1,
}

TRAINING_BASE_FEATURES: list[str] = [
    "mom_ret_1d",
    "mom_ret_5d",
    "mom_ret_20d",
    "liq_volume",
    "liq_amount",
    "liq_turnover_os",
]
_ALLOWED_SHAP_SPLIT = {"valid", "test", "train"}
_DEFAULT_EXPLAIN_CFG: dict[str, Any] = {
    "enable_shap": True,
    "shap_split": "valid",
    "shap_sample_rows": 30000,
}
_DEFAULT_SHAP_SAMPLE_ROWS = 30000
_MIN_SHAP_SAMPLE_ROWS = 1000
_MAX_SHAP_SAMPLE_ROWS = 100000
_SHAP_SAMPLE_RANDOM_STATE = 42

# ── 强行将 /workspace 加入 sys.path 并穿透遮蔽包，保证 CVM 容器内导入新开发的文件 ─────────────────────
try:
    import sys
    from pathlib import Path
    _workspace = Path("/workspace")
    if _workspace.is_dir() and str(_workspace) not in sys.path:
        sys.path.insert(0, str(_workspace))
    
    # 解决镜像中全局安装的旧 backend 包遮蔽挂载 workspace 代码的问题
    import backend
    if hasattr(backend, "__path__") and "/workspace/backend" not in backend.__path__:
        backend.__path__.append("/workspace/backend")
    import backend.shared
    if hasattr(backend.shared, "__path__") and "/workspace/backend/shared" not in backend.shared.__path__:
        backend.shared.__path__.append("/workspace/backend/shared")
except Exception:
    pass

from backend.shared.inference_contract import (
    build_daily_manifest,
    canonical_json_hash,
    normalize_fill_values,
)
from backend.shared.feature_preprocess import apply_cs_mad_zscore


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


# ── 评估指标 ─────────────────────────────────────────────────────────────────
def _ic(pred: np.ndarray, label: np.ndarray) -> float:
    mask = np.isfinite(pred) & np.isfinite(label)
    if mask.sum() < 10:
        return float("nan")
    return float(np.corrcoef(pred[mask], label[mask])[0, 1])


def _rank_ic_series(df: pd.DataFrame, pred_col: str, label_col: str) -> list[float]:
    daily = []
    for _, g in df.groupby("trade_date", sort=False):
        g = g[[pred_col, label_col]].dropna()
        if len(g) < 10:
            continue
        rp = g[pred_col].rank(method="average").to_numpy()
        rl = g[label_col].rank(method="average").to_numpy()
        v = _ic(rp, rl)
        if np.isfinite(v):
            daily.append(v)
    return daily


def _compute_metrics(df: pd.DataFrame, y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    ic     = _ic(y_pred, y_true)
    series = _rank_ic_series(df.assign(_pred=y_pred, _label=y_true), "_pred", "_label")
    rank_ic   = float(np.nanmean(series)) if series else float("nan")
    rank_icir = float(np.mean(series) / (np.std(series) + 1e-9)) if series else float("nan")
    rmse = float(np.sqrt(np.mean(np.square(y_pred - y_true)))) if len(y_true) else float("nan")
    labels = (y_true > 0).astype(int)
    pos = int(labels.sum())
    neg = int(len(labels) - pos)
    auc = float("nan")
    if pos > 0 and neg > 0:
        ranks = pd.Series(y_pred).rank(method="average").to_numpy()
        auc = float((ranks[labels == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))
    return {"ic": ic, "rank_ic": rank_ic, "rank_icir": rank_icir, "rmse": rmse, "auc": auc}


def _normalize_explain_cfg(raw: Any) -> dict[str, Any]:
    explain = raw if isinstance(raw, dict) else {}
    enable_shap = bool(explain.get("enable_shap", _DEFAULT_EXPLAIN_CFG["enable_shap"]))

    shap_split = str(explain.get("shap_split", _DEFAULT_EXPLAIN_CFG["shap_split"])).strip().lower()
    if shap_split not in _ALLOWED_SHAP_SPLIT:
        logger.warning("Invalid explain.shap_split=%s, fallback to 'valid'", shap_split)
        shap_split = "valid"

    sample_rows_raw = explain.get("shap_sample_rows", _DEFAULT_EXPLAIN_CFG["shap_sample_rows"])
    try:
        sample_rows = int(sample_rows_raw)
    except Exception:
        logger.warning("Invalid explain.shap_sample_rows=%s, fallback to %d", sample_rows_raw, _DEFAULT_SHAP_SAMPLE_ROWS)
        sample_rows = _DEFAULT_SHAP_SAMPLE_ROWS
    sample_rows = max(_MIN_SHAP_SAMPLE_ROWS, min(_MAX_SHAP_SAMPLE_ROWS, sample_rows))

    return {
        "enable_shap": enable_shap,
        "shap_split": shap_split,
        "shap_sample_rows": sample_rows,
    }


def _resolve_shap_source_frame(
    split_frames: dict[str, pd.DataFrame],
    preferred_split: str,
) -> tuple[str, pd.DataFrame]:
    ordered = [preferred_split] + [s for s in ("valid", "test", "train") if s != preferred_split]
    for split in ordered:
        frame = split_frames.get(split)
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            return split, frame
    return "", pd.DataFrame()


def _compute_shap_summary(
    *,
    model: lgb.Booster,
    split_frames: dict[str, pd.DataFrame],
    features: list[str],
    fill_values: dict[str, float],
    explain_cfg: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    shap_info: dict[str, Any] = {
        "enabled": bool(explain_cfg.get("enable_shap", True)),
        "status": "disabled",
        "split": str(explain_cfg.get("shap_split", "valid")),
        "rows_requested": int(explain_cfg.get("shap_sample_rows", _DEFAULT_SHAP_SAMPLE_ROWS)),
        "rows_used": 0,
        "file": "",
        "error": "",
        "elapsed_seconds": 0.0,
    }
    if not shap_info["enabled"]:
        return shap_info

    if not features:
        shap_info["status"] = "skipped"
        shap_info["error"] = "no_feature_columns"
        return shap_info

    start_ts = time.time()
    try:
        preferred_split = str(explain_cfg.get("shap_split", "valid")).strip().lower()
        selected_split, split_df = _resolve_shap_source_frame(split_frames, preferred_split)
        if split_df.empty:
            shap_info["status"] = "skipped"
            shap_info["error"] = "no_rows_for_shap"
            return shap_info

        rows_requested = int(explain_cfg.get("shap_sample_rows", _DEFAULT_SHAP_SAMPLE_ROWS))
        sample_df = split_df
        if len(sample_df) > rows_requested:
            sample_df = sample_df.sample(rows_requested, random_state=_SHAP_SAMPLE_RANDOM_STATE)

        x_df = sample_df[features].copy()
        for c in features:
            fill_v = fill_values.get(c, 0.0)
            if fill_v is None or (isinstance(fill_v, float) and np.isnan(fill_v)):
                fill_v = 0.0
            x_df[c] = x_df[c].astype("float32").fillna(fill_v)
        x = x_df.to_numpy(dtype=np.float32)

        contrib = model.predict(
            x,
            num_iteration=model.best_iteration or None,
            pred_contrib=True,
        )
        if not isinstance(contrib, np.ndarray) or contrib.ndim != 2:
            raise RuntimeError(f"unexpected SHAP contribution shape: {getattr(contrib, 'shape', None)}")
        if contrib.shape[1] < len(features):
            raise RuntimeError(f"contrib columns mismatch: got {contrib.shape[1]}, expect >= {len(features)}")

        shap_values = contrib[:, :len(features)]
        summary_df = pd.DataFrame(
            {
                "feature": features,
                "mean_abs_shap": np.mean(np.abs(shap_values), axis=0),
                "mean_shap": np.mean(shap_values, axis=0),
                "positive_ratio": np.mean(shap_values > 0, axis=0),
            }
        ).sort_values("mean_abs_shap", ascending=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        summary_df.to_csv(out_path, index=False)

        shap_info.update(
            {
                "status": "completed",
                "split": selected_split,
                "rows_requested": rows_requested,
                "rows_used": int(len(sample_df)),
                "file": out_path.name,
                "error": "",
            }
        )
        return shap_info
    except Exception as exc:  # noqa: BLE001
        logger.exception("SHAP summary generation failed: %s", exc)
        shap_info["status"] = "failed"
        shap_info["error"] = str(exc)
        return shap_info
    finally:
        shap_info["elapsed_seconds"] = float(time.time() - start_ts)


# ── 数据加载 ──────────────────────────────────────────────────────────────────
def load_data(
    train_start: str,
    train_end: str,
    features: list[str],
    target_horizon_days: int = 1,
    cache_dir: str | None = None,
    valid_end: str | None = None,
    test_end: str | None = None,
    source_mode: str = "LOCAL",
    local_dir: str | None = None,
    limit_up_weight: float = 0.5,
) -> tuple[pd.DataFrame, list[str]]:
    start_year = pd.Timestamp(train_start).year

    # 获取最晚的年份，确保包含 验证/测试 集所需的数据
    ends = [train_end]
    if valid_end: ends.append(valid_end)
    if test_end: ends.append(test_end)
    end_year = max(pd.Timestamp(e).year for e in ends)

    # 自动计算必要的核心列
    core_columns = ["trade_date", "symbol", "open", "close", "factor"]
    _horizon = max(1, int(target_horizon_days or 1))
    
    # 最终合并列：核心列 + 用户请求特征（去重）
    load_cols = list(dict.fromkeys(core_columns + features))
    logger.info(f"Memory optimization: Planning to load {len(load_cols)} columns from {len(range(max(start_year - 1, 2016), end_year + 1))} years")

    local_root = Path(local_dir).expanduser() if local_dir else None
    if local_root is None:
        raise RuntimeError("local_dir must be provided; COS data download has been removed")

    # 给标签构建预留边界，避免裁剪过早影响 shift/rolling
    range_start = pd.Timestamp(train_start) - pd.Timedelta(days=max(7, _horizon + 3))
    upper_bound = test_end or valid_end or train_end
    range_end = pd.Timestamp(upper_bound) + pd.Timedelta(days=max(7, _horizon + 3))

    chunks = []
    for year in range(max(start_year - 1, 2016), end_year + 1):
        df_year = _load_local_parquet(
            local_root,
            year,
            required_columns=load_cols,
            clip_start=range_start,
            clip_end=range_end,
        )
        if df_year is not None:
            if not df_year.empty:
                # 过滤北交所代码（4/8开头）
                df_year["symbol"] = df_year["symbol"].astype(str).str.zfill(6)
                df_year = df_year[~df_year["symbol"].str.startswith(("4", "8"))]
                chunks.append(df_year)
        else:
            logger.warning(f"No data file found for year {year} in {local_root}, skipping")

    if not chunks:
        raise RuntimeError("No data loaded from local storage")

    df = pd.concat(chunks, axis=0, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df[df["trade_date"].notna()].copy()
    logger.info(f"Raw concat size: {len(df)} rows. Date range: {df['trade_date'].min()} to {df['trade_date'].max()}")
    logger.info(f"After symbol filter: {len(df)} rows")

    # 标签：基于 target_horizon_days 构建 N 日远期收益
    if "open" not in df.columns or "close" not in df.columns:
        raise RuntimeError("Columns 'open' and 'close' are required for tradable label calculation")

    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    
    # 构造实盘可交易收益率：T+1开盘买入，T+H收盘卖出 (Close_{T+H} / Open_{T+1} - 1)
    grouped = df.groupby("symbol")
    future_close = grouped["close"].shift(-_horizon)
    next_open = grouped["open"].shift(-1)
    
    # 将不复权价格转换为复权价格，以修复除权除息导致的收益率计算失真
    if "factor" in df.columns:
        future_factor = grouped["factor"].shift(-_horizon)
        next_factor = grouped["factor"].shift(-1)
        future_close = future_close * future_factor
        next_open = next_open * next_factor
    
    # 获取 T 日收盘价，用于判断 T+1 开盘是否一字涨停
    current_close = df["close"]
    if "factor" in df.columns:
        current_close = current_close * df["factor"]
    
    # 智能识别涨跌幅限制 (科创板/创业板 20%, 主板 10%)
    symbols = df["symbol"].astype(str).str.zfill(6)
    is_star_gem = symbols.str.startswith(("688", "300"))
    
    # 设置 T+1 开盘涨停阈值 (留 0.5% 容错余量：20%->19.5%, 10%->9.5%)
    limit_threshold = np.where(is_star_gem, 0.195, 0.095)
    
    # 精细化涨跌停识别
    is_limit_up_open = (next_open / current_close - 1) >= limit_threshold
    is_limit_down_open = (next_open / current_close - 1) <= -limit_threshold
    is_extreme = is_limit_up_open | is_limit_down_open
    
    # 避免除以0的情况
    raw_label = np.where(next_open > 0, (future_close / next_open) - 1, np.nan)
    
    # 【改为软降权】：不再把极端样本的 label 设为 NaN，而是保留真实收益率，让模型能够学习到它们的特征分布
    df["label"] = raw_label
    
    # 新增 weight 列：正常样本权重为 1.0，开盘涨跌停的极端样本权重动态配置 (软降权)
    df["weight"] = np.where(is_extreme, limit_up_weight, 1.0)
    
    logger.info(f"Tradable label built with target_horizon_days={_horizon} (Close_T+{_horizon} / Open_T+1 - 1)")
    logger.info(f"Soft weighted {int(is_limit_up_open.sum())} limit-up and {int(is_limit_down_open.sum())} limit-down samples to {limit_up_weight}.")

    # 裁剪到请求日期范围
    ts_start = pd.Timestamp(train_start)
    ts_train_end = pd.Timestamp(train_end)
    mask = (df["trade_date"] >= ts_start) & (df["trade_date"] <= ts_train_end)
    if valid_end:
        mask = (df["trade_date"] >= ts_start) & (df["trade_date"] <= pd.Timestamp(valid_end))
    if test_end:
        mask = (df["trade_date"] >= ts_start) & (df["trade_date"] <= pd.Timestamp(test_end))

    df = df[mask].copy()
    logger.info(f"After date range clip ({ts_start.date()} to {pd.Timestamp(test_end or valid_end or train_end).date()}): {len(df)} rows")

    # 校验特征列
    missing = [f for f in features if f not in df.columns]
    if missing:
        logger.warning(f"Features not found in parquet (ignored): {missing}")
        features = [f for f in features if f in df.columns]
    if not features:
        raise RuntimeError("No valid feature columns found")

    # 1. 在过滤缺失标签前，冻结实际的输入特征数据快照，供推理一致性校验。
    # 这样可以确保如果某些股票因为未来几日 label 缺失被剔除，推理契约记录的仍然是完整的输入日快照，
    # 从而与推理预检（不含 label 过滤）时的快照行数及哈希完美对应。
    contract_manifest_df = df[["trade_date", "symbol", *features]].copy()
    contract_daily_manifest, contract_manifest_hash = build_daily_manifest(
        contract_manifest_df,
        features,
        trade_date_col="trade_date",
        symbol_col="symbol",
    )
    logger.info(
        "Daily manifest generated: dates=%d manifest_hash=%s",
        len(contract_daily_manifest),
        contract_manifest_hash[:12],
    )

    # 2. 接着再过滤掉没有有效标签的样本（用于 LightGBM 训练）
    valid_count_before = len(df)
    df = df[df["label"].notna()].copy()
    logger.info(f"After label shift & dropna: {len(df)} rows (dropped {valid_count_before - len(df)} rows with missing labels)")

    keep_cols = ["symbol", "trade_date", "label", "weight"] + features
    df = df[keep_cols].reset_index(drop=True)

    logger.info("Applying cross-sectional MAD + Z-Score normalization to features and labels...")
    df = apply_cs_mad_zscore(
        df,
        feature_columns=features,
        label_column="label",
        chunk_size=50,
        mad_multiplier=5.0,
    )

    logger.info(
        f"Data ready: {len(df):,} rows, {len(features)} features, "
        f"{df['trade_date'].min().date()} ~ {df['trade_date'].max().date()}"
    )
    return df, features, contract_daily_manifest, contract_manifest_hash


# ── 训练 ──────────────────────────────────────────────────────────────────────
def train_model(df: pd.DataFrame, features: list[str], cfg: dict) -> tuple:
    model_cfg       = cfg.get("model", {})
    num_boost_round = int(model_cfg.get("num_boost_round", 1000))
    early_stopping_rounds = int(model_cfg.get("early_stopping_rounds", 100) or 100)
    if early_stopping_rounds < 1:
        early_stopping_rounds = 1
    params          = {**DEFAULT_LGB_PARAMS, **model_cfg.get("params", {})}

    def _frame_range_text(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "EMPTY"
        return f"{frame['trade_date'].min().date()}~{frame['trade_date'].max().date()}"

    # 显式 split 优先于 val_ratio（config 有 split.valid 时 val_ratio 为 null）
    split_cfg = cfg.get("split", {})
    if split_cfg.get("valid"):
        valid_start_str, valid_end_str = split_cfg["valid"]
        requested_train = f"{split_cfg['train'][0]}~{split_cfg['train'][1]}"
        requested_val = f"{valid_start_str}~{valid_end_str}"
        train_df = df[df["trade_date"] <= pd.Timestamp(split_cfg["train"][1])].copy()
        val_df   = df[
            (df["trade_date"] >= pd.Timestamp(valid_start_str)) &
            (df["trade_date"] <= pd.Timestamp(valid_end_str))
        ].copy()
        if split_cfg.get("test"):
            test_start_str, test_end_str = split_cfg["test"]
            requested_test = f"{test_start_str}~{test_end_str}"
            test_df = df[
                (df["trade_date"] >= pd.Timestamp(test_start_str)) &
                (df["trade_date"] <= pd.Timestamp(test_end_str))
            ].copy()
        else:
            requested_test = requested_val
            test_df = val_df.copy()
        logger.info(f"Split mode: train~{split_cfg['train'][1]}  val {valid_start_str}~{valid_end_str}")
    else:
        val_ratio = float(model_cfg.get("val_ratio") or 0.15)
        dates     = sorted(df["trade_date"].unique())
        if not dates:
            raise RuntimeError("No rows available for split after preprocessing. 请检查训练时间窗口与特征快照覆盖范围。")
        val_start = dates[int(len(dates) * (1 - val_ratio))]
        train_df  = df[df["trade_date"] < val_start].copy()
        val_df    = df[df["trade_date"] >= val_start].copy()
        test_df = val_df.copy()
        train_start = pd.Timestamp(df["trade_date"].min()).date()
        train_end = (pd.Timestamp(val_start) - pd.Timedelta(days=1)).date()
        requested_train = f"{train_start}~{train_end}"
        requested_val = f"{pd.Timestamp(val_start).date()}~{pd.Timestamp(df['trade_date'].max()).date()}"
        requested_test = requested_val
        logger.info(
            f"val_ratio mode: train~{pd.Timestamp(val_start).date() - pd.Timedelta(days=1)}"
            f"  val {pd.Timestamp(val_start).date()}~"
        )

    if train_df.empty or val_df.empty or test_df.empty:
        available_range = "EMPTY"
        if not df.empty:
            available_range = f"{df['trade_date'].min().date()}~{df['trade_date'].max().date()}"
        raise RuntimeError(
            "Dataset split contains empty segment. "
            f"available={available_range}; "
            f"train={len(train_df)}({_frame_range_text(train_df)}) requested={requested_train}; "
            f"val={len(val_df)}({_frame_range_text(val_df)}) requested={requested_val}; "
            f"test={len(test_df)}({_frame_range_text(test_df)}) requested={requested_test}. "
            "请调整 train/valid/test 时间窗口，确保三段均与可用数据重叠。"
        )

    # 计算填充值：因为特征已经过截面 ZScore 标准化 (均值为0)，缺失值统一填充为 0.0
    fill_values = {c: 0.0 for c in features}

    def _fill(frame: pd.DataFrame) -> np.ndarray:
        x = frame[features].copy()
        for c in features:
            x[c] = x[c].astype("float32").fillna(fill_values[c])
        return x.to_numpy(dtype=np.float32)

    X_train, y_train = _fill(train_df), train_df["label"].astype("float32").to_numpy()
    X_val,   y_val   = _fill(val_df),   val_df["label"].astype("float32").to_numpy()

    w_train = train_df["weight"].astype("float32").to_numpy() if "weight" in train_df.columns else None

    ds_train = lgb.Dataset(X_train, label=y_train, weight=w_train, feature_name=features, free_raw_data=True)
    ds_val   = lgb.Dataset(X_val,   label=y_val,   feature_name=features, free_raw_data=True)

    callbacks = [
        lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=True),
        lgb.log_evaluation(100),
    ]
    model = lgb.train(
        params, ds_train,
        num_boost_round=num_boost_round,
        valid_sets=[ds_train, ds_val],
        valid_names=["train", "valid"],
        callbacks=callbacks,
    )

    y_train_pred = model.predict(_fill(train_df))
    y_val_pred = model.predict(_fill(val_df))
    y_test_pred = model.predict(_fill(test_df))
    train_m = _compute_metrics(train_df, y_train, y_train_pred)
    val_m   = _compute_metrics(val_df,   y_val,   y_val_pred)
    test_m  = _compute_metrics(test_df,  test_df["label"].astype("float32").to_numpy(), y_test_pred)

    logger.info(f"Train IC={train_m['ic']:.4f}  RankIC={train_m['rank_ic']:.4f}")
    logger.info(f"Val   IC={val_m['ic']:.4f}    RankIC={val_m['rank_ic']:.4f}  ICIR={val_m['rank_icir']:.4f}")

    # 生成全窗口预测：覆盖 train/valid/test 三段，供后续完整回测使用
    full_pred_df = df[["symbol", "trade_date", "label"]].copy()
    full_pred_df["pred"] = model.predict(_fill(df))
    full_pred_df["split"] = "train"
    full_pred_df.loc[
        (full_pred_df["trade_date"] >= val_df["trade_date"].min()) &
        (full_pred_df["trade_date"] <= val_df["trade_date"].max()),
        "split",
    ] = "valid"
    full_pred_df.loc[
        (full_pred_df["trade_date"] >= test_df["trade_date"].min()) &
        (full_pred_df["trade_date"] <= test_df["trade_date"].max()),
        "split",
    ] = "test"
    return (
        model,
        fill_values,
        train_m,
        val_m,
        test_m,
        full_pred_df.reset_index(drop=True),
        {
            "train": train_df.reset_index(drop=True),
            "valid": val_df.reset_index(drop=True),
            "test": test_df.reset_index(drop=True),
        },
    )


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main() -> int:
    # 最早期诊断日志：在任何处理之前打印，确保 Batch 环境中一定能看到
    print(f"[BOOT] python={sys.version}", flush=True)
    print(f"[BOOT] argv={sys.argv}", flush=True)

    parser = argparse.ArgumentParser(description="QuantMind Training — YAML config driven")
    parser.add_argument("--config", required=False, help="Path to config.yaml")
    try:
        args, unknown_args = parser.parse_known_args()
    except SystemExit as exc:
        if int(getattr(exc, "code", 1) or 0) == 0:
            return 0
        # Batch 运行时偶发注入畸形参数（如缺失值的已知 flag）会触发 argparse 退出码 2。
        # 这里降级为环境变量驱动启动，避免任务在入口阶段直接失败。
        logger.warning(f"Argparse failed with argv={sys.argv}; fallback to env-driven args")
        args = argparse.Namespace(config=None)
        unknown_args = []
    if unknown_args:
        logger.warning(f"Ignoring unknown CLI args from runtime: {unknown_args}")

    # 本地挂载 config.yaml，CLI 参数作为可选覆盖
    cfg_path = Path(args.config) if args.config else Path("/tmp/config.yaml")

    run_id     = "unknown"
    result: dict = {}
    callback_url    = ""
    callback_secret = ""
    result_path = Path("/workspace/result.json")

    try:
        if not cfg_path.exists():
            raise RuntimeError(f"Config file not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text())

        run_id          = cfg.get("run_id", "unknown")
        job_name        = cfg.get("job_name", "unnamed")
        result_path     = Path(cfg.get("output", {}).get("result_path", "/workspace/result.json"))
        callback_url    = cfg.get("callback", {}).get("url", "")
        callback_secret = cfg.get("callback", {}).get("secret", "")

        logger.info("=== QuantMind Training Start ===")
        logger.info(f"run_id={run_id}  job={job_name}  config={cfg_path}")
        # 数据加载（特征列自动补齐基础6列）
        submitted_features = list(dict.fromkeys([str(item).strip() for item in (cfg["data"].get("features", []) or []) if str(item).strip()]))
        auto_appended_features = [feature for feature in TRAINING_BASE_FEATURES if feature not in submitted_features]
        features = list(dict.fromkeys(TRAINING_BASE_FEATURES + submitted_features))
        source_mode = str((cfg.get("data", {}) or {}).get("source_mode") or "LOCAL").strip().upper()
        local_data_dir = str((cfg.get("data", {}) or {}).get("local_dir") or "").strip() or None
        explain_cfg = _normalize_explain_cfg(cfg.get("explain") or {})

        df, valid_features, contract_daily_manifest, contract_manifest_hash = load_data(
            cfg["data"]["train_start"],
            cfg["data"]["train_end"],
            features,
            target_horizon_days=int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1),
            cache_dir=cfg.get("cache", {}).get("dir"),
            valid_end=cfg.get("split", {}).get("valid", [None, None])[1],
            test_end=cfg.get("split", {}).get("test", [None, None])[1],
            source_mode=source_mode,
            local_dir=local_data_dir,
        )
        train_t0 = time.time()
        model, fill_values, train_m, val_m, test_m, pred_df, split_frames = train_model(df, valid_features, cfg)
        elapsed = float(time.time() - train_t0)
        logger.info("Training finished in %.2fs, best_iteration=%s", elapsed, model.best_iteration)

        # 保存模型
        model_path = Path("/workspace/model.lgb")
        model.save_model(str(model_path))
        logger.info(f"Model saved to {model_path}")

        # 保存预测结果（parquet 压缩用于存档，比 pickle 小 ~10x）
        pred_path = Path("/workspace/pred.parquet")
        pred_df.to_parquet(pred_path, engine="pyarrow", compression="zstd", index=False)
        logger.info(f"Predictions saved to {pred_path} ({pred_path.stat().st_size/1024/1024:.1f} MB)")

        # 同时保存回测引擎兼容格式 pred.pkl
        # 回测引擎要求: MultiIndex(datetime, instrument) + 'score' 列
        pred_qlib = (
            pred_df[["trade_date", "symbol", "pred"]]
            .rename(columns={"trade_date": "datetime", "symbol": "instrument", "pred": "score"})
            .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
            .set_index(["datetime", "instrument"])
            .sort_index()
        )
        pred_pkl_path = Path("/workspace/pred.pkl")
        pred_qlib.to_pickle(pred_pkl_path)
        logger.info(f"Backtest-compatible pred.pkl saved ({pred_pkl_path.stat().st_size/1024/1024:.1f} MB, {len(pred_qlib):,} rows)")

        shap_summary_path = Path("/workspace/shap_summary.csv")
        shap_info = _compute_shap_summary(
            model=model,
            split_frames=split_frames,
            features=valid_features,
            fill_values=fill_values,
            explain_cfg=explain_cfg,
            out_path=shap_summary_path,
        )
        if shap_info.get("status") == "completed":
            logger.info(
                "SHAP summary generated: split=%s rows=%s -> %s",
                shap_info.get("split"),
                shap_info.get("rows_used"),
                shap_summary_path,
            )
        elif shap_info.get("status") == "disabled":
            logger.info("SHAP summary disabled by config")
        elif shap_info.get("status") == "skipped":
            logger.warning("SHAP summary skipped: %s", shap_info.get("error") or "unknown")
        else:
            logger.warning("SHAP summary failed: %s", shap_info.get("error") or "unknown")

        # 构造 metadata
        metadata = {
            "run_id": run_id, "job_name": job_name,
            "framework": "lightgbm",
            "model_type": cfg.get("model", {}).get("type", "lightgbm"),
            "model_file": "model.lgb",
            # ====== Factor Tilt Injection ======
            # 显式倾斜流动性和估值因子，取代旧版代码中因 1e-9 缺失带来的“意外” 10 倍方差膨胀，
            # 以透明的手段合法拿回旧版的高额收益，防止因子溢出导致不可控风险。
            "factor_tilt": {
                "liq_amihud_20": 0.5,
                "style_bp": 0.2
            },
            # ===================================
            "feature_count": len(valid_features),
            "requested_feature_count": len(submitted_features),
            "requested_features": submitted_features,
            "auto_appended_feature_count": len(auto_appended_features),
            "auto_appended_features": auto_appended_features,
            "features": valid_features,
            "feature_columns": valid_features,
            "fill_values": fill_values,
            "train_start": cfg["data"]["train_start"],
            "train_end":   cfg["data"]["train_end"],
            "val_start":   (cfg.get("split", {}).get("valid") or [None, None])[0] or "",
            "val_end":     (cfg.get("split", {}).get("valid") or [None, None])[1] or "",
            "test_start":  (cfg.get("split", {}).get("test")  or [None, None])[0] or "",
            "test_end":    (cfg.get("split", {}).get("test")  or [None, None])[1] or "",
            "data_source": "parquet",
            "best_iteration": model.best_iteration,
            "target_horizon_days": int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1),
            "target_mode": str((cfg.get("label", {}) or {}).get("target_mode") or "return"),
            "label_formula": str((cfg.get("label", {}) or {}).get("label_formula") or ""),
            "effective_trade_date": str((cfg.get("label", {}) or {}).get("effective_trade_date") or ""),
            "training_window": str((cfg.get("label", {}) or {}).get("training_window") or ""),
            "metrics": {
                "train_ic": train_m["ic"], "train_rank_ic": train_m["rank_ic"], "train_rank_icir": train_m["rank_icir"],
                "val_ic": val_m["ic"], "val_rank_ic": val_m["rank_ic"], "val_rank_icir": val_m["rank_icir"],
                "test_ic": test_m["ic"], "test_rank_ic": test_m["rank_ic"], "test_rank_icir": test_m["rank_icir"],
            },
            "pred_coverage_start": str(pred_df["trade_date"].min().date()) if not pred_df.empty else "",
            "pred_coverage_end": str(pred_df["trade_date"].max().date()) if not pred_df.empty else "",
            "pred_rows": int(len(pred_df)),
            "shap": shap_info,
            "generated_at": datetime.utcnow().isoformat(),
            "elapsed_seconds": elapsed,
        }
        metadata_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
        Path("/workspace/metadata.json").write_bytes(metadata_bytes)
        logger.info("metadata.json saved locally")
        
        # 写入 inference_contract.json 确保推理阶段有迹可循
        normalized_fills = normalize_fill_values(fill_values)
        contract = {
            "contract_version": "1.0",
            "run_id": run_id,
            "job_name": job_name,
            "generated_at": datetime.utcnow().isoformat(),
            "manifest_hash": contract_manifest_hash,
            "features": valid_features,
            "fill_values": normalized_fills,
            "daily_manifest": contract_daily_manifest,
        }
        contract_json = json.dumps(contract, ensure_ascii=False, indent=2, allow_nan=False)
        contract_hash = canonical_json_hash(contract_json)
        contract["contract_hash"] = contract_hash
        Path("/workspace/inference_contract.json").write_text(
            json.dumps(contract, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        logger.info(
            "inference_contract.json generated. contract_hash=%s manifest_hash=%s",
            contract_hash[:12], contract_manifest_hash[:12],
        )

        # 复制统一推理脚本模板（而非内联生成旧版脚本）
        template_path = Path("/app/backend/services/engine/inference/templates/inference_parquet.py")
        inference_dest = Path("/workspace/inference.py")
        if template_path.is_file():
            inference_dest.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("inference.py copied from unified template: %s", template_path)
        else:
            # 兜底：模板不存在时写入简化版（仅记录警告）
            logger.warning("统一推理模板不存在: %s，使用简化版", template_path)
            _INFERENCE_SCRIPT_FALLBACK = '''#!/usr/bin/env python3
"""
QuantMind Parquet 数据源推理脚本 (inference.py 模板)
=====================================================
适用于训练数据来自 feature_snapshots/*.parquet 的 LightGBM 模型。

平台注入环境变量：
    MODEL_DIR      模型目录绝对路径（含 metadata.json + model.lgb）
    TRADE_DATE     推理日期（同 --date 参数，互为备份）
    OUTPUT_FORMAT  固定值 json

调用方式（由 InferenceScriptRunner 自动调用）：
    python inference.py --date YYYY-MM-DD --output /path/to/out.json

输出格式（写入 --output 文件）：
    [{"symbol": "sh600519", "score": 0.82}, ...]

exit code：
    0  = 成功
    1  = 致命错误（模型/元数据损坏）
    2  = 该日期无可用数据（触发 alpha158 兜底）
"""
from __future__ import annotations
import argparse, json, logging, os, sys
from pathlib import Path
import lightgbm as lgb
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("inference_parquet")

_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date", "-d", type=str, default=os.getenv("TRADE_DATE", ""))
    p.add_argument("--output", "-o", type=str, required=True)
    p.add_argument("--model-dir", type=str, default=os.getenv("MODEL_DIR", str(Path(__file__).parent)))
    p.add_argument("--data-dir", type=str, default=os.getenv("MODEL_TRAINING_DATA_DIR", _DEFAULT_DATA_DIR))
    return p.parse_args()

def load_metadata(model_dir):
    meta_path = Path(model_dir) / "metadata.json"
    if not meta_path.exists():
        logger.error("metadata.json 不存在: %s", meta_path); sys.exit(1)
    return json.loads(meta_path.read_text(encoding="utf-8"))

def load_model(model_dir, meta):
    model_path = Path(model_dir) / meta.get("model_file", "model.lgb")
    if not model_path.exists():
        candidates = list(Path(model_dir).glob("*.lgb")) + list(Path(model_dir).glob("*.txt"))
        if not candidates:
            logger.error("未找到 LightGBM 模型文件: %s", model_dir); sys.exit(1)
        model_path = candidates[0]
    logger.info("加载模型: %s", model_path.name)
    return lgb.Booster(model_file=str(model_path))

def load_date_data(trade_date, data_dir, meta):
    year = int(trade_date[:4])
    parquet_path = Path(data_dir) / f"model_features_{year}.parquet"
    if not parquet_path.exists():
        logger.warning("parquet 文件不存在: %s", parquet_path); return None
    df = pd.read_parquet(parquet_path, engine="pyarrow")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    day_df = df[df["trade_date"] == trade_date].copy()
    if len(day_df) == 0:
        logger.warning("日期 %s 无数据", trade_date); return None
    logger.info("找到 %d 条记录，日期=%s", len(day_df), trade_date)
    return day_df

def preprocess(df, meta):
    feature_cols = meta.get("feature_columns") or meta.get("features", [])
    fill_values  = meta.get("fill_values", {})
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        logger.warning("缺少 %d 个特征列，填 0: %s", len(missing), missing[:8])
        for c in missing: df[c] = 0.0
    X_df = df[feature_cols].copy()
    for col, val in fill_values.items():
        if col in X_df.columns: X_df[col] = X_df[col].fillna(val)
    return X_df.fillna(0.0), df["symbol"].tolist()

def main():
    args = parse_args()
    trade_date = (args.date or "").strip()
    if not trade_date:
        logger.error("未指定推理日期"); sys.exit(1)
    model_dir, data_dir, out_path = Path(args.model_dir), Path(args.data_dir), Path(args.output)
    logger.info("=== parquet 推理脚本 === date=%s  model_dir=%s", trade_date, model_dir)
    meta  = load_metadata(model_dir)
    day_df = load_date_data(trade_date, data_dir, meta)
    if day_df is None:
        print(f"日期 {trade_date} 无数据，触发兜底", file=sys.stderr); sys.exit(2)
    model = load_model(model_dir, meta)
    X_df, symbols = preprocess(day_df, meta)
    if len(X_df) == 0:
        print(f"日期 {trade_date} 预处理后无有效行", file=sys.stderr); sys.exit(2)
    scores = model.predict(X_df.values.astype(np.float32), num_iteration=meta.get("best_iteration"))
    signals = sorted(
        [{"symbol": s, "score": float(v)} for s, v in zip(symbols, scores) if v == v],
        key=lambda x: x["score"], reverse=True
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(signals, ensure_ascii=False), encoding="utf-8")
    logger.info("已写入信号文件: %s  (%d 条)", out_path, len(signals))

if __name__ == "__main__":
    main()
'''
            inference_dest.write_text(_INFERENCE_SCRIPT_FALLBACK, encoding="utf-8")
            logger.info("inference.py fallback version written to model directory")

        result = {
            "status": "completed",
            "run_id": run_id,
            "job_name": job_name,
            "metrics": {
                "train": {"rmse": train_m["rmse"], "auc": train_m["auc"]},
                "val": {"rmse": val_m["rmse"], "auc": val_m["auc"]},
                "test": {"rmse": test_m["rmse"], "auc": test_m["auc"]},
            },
            "artifacts": [
                {"name": "model.lgb",     "local": "/workspace/model.lgb"},
                {"name": "pred.parquet",  "local": "/workspace/pred.parquet"},
                {"name": "metadata.json", "local": "/workspace/metadata.json"},
                {"name": "inference.py",  "local": "/workspace/inference.py"},
                {"name": "config.yaml",   "local": "/workspace/config.yaml"},
                {"name": "result.json",   "local": "/workspace/result.json"},
            ],
            "summary": {
                "status": "训练完成",
                "message": f"训练完成，best_iteration={model.best_iteration}，产物已保存到本地模型目录",
            },
            "metadata": metadata,
            "error": "",
            "logs": f"val_rmse={val_m['rmse']:.6f}, val_auc={val_m['auc']:.6f}",
        }
        if shap_info.get("status") == "completed" and shap_summary_path.exists():
            result["artifacts"].append({"name": "shap_summary.csv", "local": "/workspace/shap_summary.csv"})

    except Exception as e:
        logger.exception(f"Training failed: {e}")
        result = {"status": "failed", "run_id": run_id, "error": str(e)}

    finally:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_json = json.dumps(result, ensure_ascii=False, indent=2)
        result_path.write_text(result_json)
        logger.info(f"result.json → {result_path}")

        if callback_url:
            try:
                resp = requests.post(
                    callback_url, json=result,
                    headers={"X-Internal-Call-Secret": callback_secret},
                    timeout=15,
                )
                logger.info(f"Callback → HTTP {resp.status_code}")
            except Exception as cb_err:
                logger.warning(f"Callback failed (non-fatal): {cb_err}")

    logger.info("=== Training Complete ===")
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
