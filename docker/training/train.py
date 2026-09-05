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
import gc
import json
import logging
import os
import random
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
import torch
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("quantmind.train")

# ── 训练工作目录 ──────────────────────────────────────────────────────────────
# Docker 编排把宿主机任务目录 bind mount 到 /workspace（默认值）；
# 免 Docker 本机直跑（LocalProcessOrchestrator）时容器挂载不存在，编排器通过
# TRAINING_WORKSPACE_DIR 环境变量把工作目录指到本机真实路径（如
# {STORAGE_ROOT}/training_jobs/{run_id}）。产物（model.*、metadata、pred 等）
# 一律写到这里，与 config.output.result_path 同目录。
_WORKSPACE_ENV = (os.getenv("TRAINING_WORKSPACE_DIR") or "").strip()
WORKSPACE_DIR = Path(_WORKSPACE_ENV) if _WORKSPACE_ENV else WORKSPACE_DIR


# ── 硬件环境检测 ──────────────────────────────────────────────────────────────
def detect_hardware() -> dict[str, Any]:
    """检测运行环境的硬件配置（CPU、内存、GPU）。"""
    import os
    info: dict[str, Any] = {"cpu_count": os.cpu_count() or 1, "gpu_available": False, "gpu_count": 0, "gpu_name": "", "mem_total_gb": 0.0}
    try:
        import psutil
        info["mem_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            info["gpu_available"] = True
            info["gpu_count"] = torch.cuda.device_count()
            info["gpu_name"] = torch.cuda.get_device_name(0) if info["gpu_count"] > 0 else ""
    except ImportError:
        pass
    logger.info("Hardware: cpu=%d, mem=%.1fGB, gpu=%s(%d), gpu_name=%s",
                info["cpu_count"], info["mem_total_gb"],
                info["gpu_available"], info["gpu_count"], info["gpu_name"])
    return info


# 树模型线程数：默认 -1 = 用满所有核心（速度最快）。
# 可通过环境变量 TRAIN_NTHREADS 覆盖（如 TRAIN_NTHREADS=4 限制为 4 线程）。
# 多模型/OOF 同时训练时内存叠加易 OOM，可据此限流。
def _default_train_threads() -> int:
    raw = (os.getenv("TRAIN_NTHREADS") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid TRAIN_NTHREADS=%r, fallback to -1", raw)
    return -1


_TRAIN_NTHREAD = _default_train_threads()
# 训练信号在 T 日收盘后生成，最早在下一个交易日执行。训练、回测和线上
# forward label 都应使用相同的 T+1 执行口径。
_EXECUTION_LAG_DAYS = 1


# ── 模型默认参数 ──────────────────────────────────────────────────────────────
DEFAULT_LGB_PARAMS: dict[str, Any] = {
    "objective":         "regression",
    "metric":            "l2",
    "boosting":          "gbdt",
    "num_leaves":        31,
    "learning_rate":     0.02,
    "feature_fraction":  0.6,
    "bagging_fraction":  0.7,
    "bagging_freq":      5,
    "min_child_samples": 150,
    "path_smooth":       1.0,
    "lambda_l1":         0.5,
    "lambda_l2":         1.0,
    "max_depth":         -1,
    # LightGBM 原生 API 的线程参数名是 num_threads（n_jobs 是 sklearn 层的别名，
    # 直接传 n_jobs 给 lgb.train() 会被忽略并回退到默认线程数，导致多核不生效）
    "num_threads":       _TRAIN_NTHREAD,
    "verbosity":         -1,
}

DEFAULT_XGB_PARAMS: dict[str, Any] = {
    "objective":        "reg:squarederror",
    "eval_metric":      "rmse",
    "max_depth":        4,
    "learning_rate":    0.05,
    "subsample":        0.7,
    "colsample_bytree": 0.65,
    "reg_alpha":        0.5,
    "reg_lambda":       2.0,
    "min_child_weight": 100,
    "tree_method":      "hist",
    "nthread":          -1,
    "verbosity":        0,
}

DEFAULT_CATBOOST_PARAMS: dict[str, Any] = {
    "loss_function":    "RMSE",
    "depth":            6,
    "learning_rate":    0.03,
    "iterations":       1500,
    "l2_leaf_reg":      3.0,
    "random_strength":  1.5,
    "bagging_temperature": 0.8,
    "od_type":          "Iter",
    "od_wait":          100,
    "thread_count":     -1,
    "verbose":          100,
}

# 支持的模型类型集合
_TREE_MODEL_TYPES = {"lightgbm", "xgboost", "catboost", "linear", "random_forest"}
_DL_MODEL_TYPES = {"gru", "lstm", "alstm", "transformer", "tabnet", "tcn", "nativetft", "mlp", "hybrid_gru_tree"}
_CUSTOM_DL_MODEL_TYPES = {"nativetft", "mlp", "hybrid_gru_tree"}  # 非 Qlib 的自定义 DL 模型
_ALL_MODEL_TYPES = _TREE_MODEL_TYPES | _DL_MODEL_TYPES
_ENSEMBLE_MODEL_TYPES = _TREE_MODEL_TYPES - {"linear"}  # 可参与集成的树模型

TRAINING_BASE_FEATURES: list[str] = [
    "mom_ret_1d",
    "mom_ret_5d",
    "mom_ret_20d",
    "liq_volume",
    "liq_amount",
    "fun_turnover_1",
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


def _sanitize_nan_inf(obj):
    """递归清洗为 JSON 可序列化结构：NaN/Inf→None，numpy 标量→原生类型，
    其余不可序列化对象（如误入的模型对象）→None，保证训练不因 metadata 序列化而失败。"""
    import math

    import numpy as np

    if isinstance(obj, dict):
        return {k: _sanitize_nan_inf(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nan_inf(v) for v in obj]
    if isinstance(obj, (bool, str, int)) or obj is None:
        return obj
    if isinstance(obj, np.generic):
        obj = obj.item()
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (pd.Timestamp, datetime, Path)):
        return str(obj)
    try:
        import json as _json

        _json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        logger.warning("metadata 中发现不可序列化对象 %s，已置空", type(obj).__name__)
        return None


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
    # 剔除 NaN/Inf 对，避免 rmse/auc 传播为 NaN
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    valid = np.isfinite(y_true) & np.isfinite(y_pred)
    if valid.sum() < 10:
        return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"),
                "rmse": 0.0, "auc": 0.0, "score_direction": "normal"}
    y_true = y_true[valid]
    y_pred = y_pred[valid]
    # df 与 y 长度一致时同步过滤，避免 assign 长度不匹配
    if len(df) == len(valid):
        df = df.iloc[valid].copy()
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
    # 方向检测：IC < 0 说明模型预测与标签方向相反
    score_direction = "normal" if np.isnan(ic) or ic >= 0 else "reversed"
    return {"ic": ic, "rank_ic": rank_ic, "rank_icir": rank_icir, "rmse": rmse, "auc": auc, "score_direction": score_direction}


def _psi_single(a: np.ndarray, b: np.ndarray, n_bins: int = 10) -> float:
    """单个特征的 PSI（Population Stability Index）。

    PSI = Σ (actual% - expected%) * ln(actual% / expected%)
    以 a 为基准分布（expected），b 为待检分布（actual）。
    <0.05 无显著漂移；0.05~0.2 中等漂移；>0.2 显著漂移（compute_psi_drift 判级用）。
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 20 or len(b) < 20:
        return float("nan")
    # 分位数分箱（基准分布）
    edges = np.quantile(a, np.linspace(0, 1, n_bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    # 去重相邻边界
    unique_edges = []
    for e in edges:
        if not unique_edges or e != unique_edges[-1]:
            unique_edges.append(e)
    if len(unique_edges) < 3:
        return 0.0
    bin_a = np.histogram(a, bins=unique_edges)[0].astype(np.float64)
    bin_b = np.histogram(b, bins=unique_edges)[0].astype(np.float64)
    # 防止除零/对数零
    pct_a = bin_a / max(len(a), 1)
    pct_b = bin_b / max(len(b), 1)
    pct_a = np.clip(pct_a, 1e-6, None)
    pct_b = np.clip(pct_b, 1e-6, None)
    return float(np.sum((pct_b - pct_a) * np.log(pct_b / pct_a)))


def _rank_displacement(
    train_df: pd.DataFrame,
    recent_df: pd.DataFrame,
    feat: str,
) -> float:
    """每只股票在两个阶段的截面 rank 位移均值（身份级稳定性）。

    对每只股票：训练段内按日截面 rank(pct) 后取均值 → rank_tr[s]；
    recent 段同理 → rank_rc[s]。位移 = |rank_rc[s] - rank_tr[s]|（0~1）。
    取全市场均值为该特征的截面结构漂移强度：
    - ≈0：个股相对位置稳定 → 即使水平值大幅漂移（量能膨胀）也属良性；
    - 大：大量个股截面位置重排（风格切换/板块轮动）→ 真实结构漂移。
    比"rank 直方图 PSI"更强的原因：直方图只看宏观 rank 分布（涨跌家数结构），
    身份重排时分布不变测不出；位移跟踪每只股票的个体位置，身份重排必显形。

    只统计在两端都有足够观测的股票，避免次新/停复牌（仅 1-2 天）的噪声污染均值。
    若交集为空或任一端的股票数过少 → 返回 nan（不可估计），由调用方保守处理。
    """
    min_obs = 5  # 一只股票至少要在该段出现 5 个交易日，均值才有意义
    tr_rank = train_df.groupby("trade_date")[feat].rank(pct=True)
    tr_count = train_df.groupby("symbol")[feat].transform("size")
    tr_keep = tr_count >= min_obs
    tr_mean = tr_rank[tr_keep].groupby(train_df.loc[tr_keep, "symbol"].to_numpy()).mean()

    rc_rank = recent_df.groupby("trade_date")[feat].rank(pct=True)
    rc_count = recent_df.groupby("symbol")[feat].transform("size")
    rc_keep = rc_count >= min_obs
    rc_mean = rc_rank[rc_keep].groupby(recent_df.loc[rc_keep, "symbol"].to_numpy()).mean()

    common = tr_mean.index.intersection(rc_mean.index)
    if len(common) < 50:  # 交集过小 → 不可靠，保守返回 nan
        return float("nan")
    disp = (rc_mean.loc[common] - tr_mean.loc[common]).abs()
    return float(disp.mean())


def _compute_rank_disp_all(
    train_df: pd.DataFrame,
    recent_df: pd.DataFrame,
    features: list[str],
) -> dict[str, dict]:
    """批量计算全部特征的 rank_disp，供 compute_psi_drift 使用。

    向量化：一次 groupby.rank 算所有特征的日截面 rank，再按 symbol 聚合均值，
    避免逐特征重复扫描。与单特征版 `_rank_displacement` 保持一致：过滤掉观测
    < min_obs 天的股票，避免次新/停复牌（仅 1-2 天）的噪声污染均值。

    返回 {feature: {"mean": 位移均值, "std": 个股位移横截面标准差, "n": 交集股票数}}；
    不可估计时为 {"mean": nan, "std": nan, "n": 0}。std/n 供显著性检验用
    （位移均值的标准误 ≈ std/√n，相干性结构漂移在大面板上极显著）。
    """
    min_obs = 5
    nan_entry = {"mean": float("nan"), "std": float("nan"), "n": 0}
    if not features:
        return {}
    avail = [f for f in features if f in train_df.columns and f in recent_df.columns]
    missing = {f: dict(nan_entry) for f in features if f not in avail}

    def _per_symbol_mean_rank(df: pd.DataFrame) -> pd.DataFrame:
        keep = df.groupby("symbol")[avail[0]].transform("size") >= min_obs
        sub = df.loc[keep]
        if sub.empty:
            return pd.DataFrame(index=df["symbol"].unique())
        rank_df = sub.groupby("trade_date")[avail].rank(pct=True)
        rank_df["symbol"] = sub["symbol"].to_numpy()
        return rank_df.groupby("symbol")[avail].mean()

    tr_mean = _per_symbol_mean_rank(train_df)
    rc_mean = _per_symbol_mean_rank(recent_df)

    common = tr_mean.index.intersection(rc_mean.index)
    out = dict(missing)
    if len(common) < 50:
        out.update({f: dict(nan_entry) for f in avail})
        return out
    disp = (rc_mean.loc[common] - tr_mean.loc[common]).abs()
    out.update({
        f: {
            "mean": float(disp[f].mean()),
            "std": float(disp[f].std(ddof=1)) if len(common) > 1 else float("nan"),
            "n": int(len(common)),
        }
        for f in avail
    })
    return out


def compute_psi_drift(
    df: pd.DataFrame,
    features: list[str],
    train_start: str,
    train_end: str,
    n_recent_days: int = 30,
    top_n: int = 20,
) -> dict:
    """数据漂移检测：对比训练区间 vs 最近 n 个交易日的特征分布（PSI）。

    双通道检测，消除"牛市量能膨胀"类伪警：
    - `level_psi`（原 psi 字段）：原始水平值分箱 PSI（量纲敏感）。成交额/换手等
      水平特征在牛市中整体抬升时必然重度，但树模型只走 ≤/> 分叉、标签又是截面
      rank，单纯水平平移对预测力几乎无影响——这类是"良性量纲膨胀"。
    - `rank_disp`（新增）：每只股票在训练段与 recent 段的截面 rank 位移均值。
      只反映"个股相对位置是否重排"，对整体水平平移免疫，能抓住真实的风格切换/
      板块轮动等结构漂移。
    判级以 `rank_disp` 为主：rank_disp 高 = 真实结构漂移（severe）；rank_disp 低
    但 level_psi 高 = 良性量纲膨胀（降级到 stable/medium，附 `benign_scale=True`）。
    rank_disp 不可估计（股票交集过小/观测不足）时按 level_psi 保守判级并标
    `rank_reliable=False`，绝不静默归 0（否则会掩蔽真实漂移）。

    方法学要点（2026-08 修复误报）：
    - 基准窗与 recent 窗等长且相邻（紧邻其前的 n 个交易日）。旧实现拿整个
      训练窗（数千日）均值对 30 日窗均值，30 日侧采样噪声（每股均值 rank
      标准误 ~0.09）会被误读成截面重排，优秀模型也报"严重漂移"。
    - 判级 = 显著性检验 + 历史波动包络：在全部可用日期内取多组相邻等长窗
      位移，中位数为噪声本底、最大值为包络。z = (rank_disp − 本底) /
      (个股位移 std/√n)；severe 需 z≥8、幅度≥0.10 且超包络 25%，medium 需
      z≥4、幅度≥0.05 且超包络——仅高于单点本底但仍在历史波动区间内的
      风格轮动不再误报。本底不可估计时退回固定阈值 0.10/0.25。

    返回:
        {
          "enabled": True,
          "train_start": ..., "train_end": ...,
          "recent_start": ..., "recent_end": ...,
          "drift": {"stable": N, "medium": N, "severe": N},
          "top_drift_features": [ {feature, psi(=level_psi), rank_disp, level, benign_scale, rank_reliable}, ... ],
          "max_psi": float (最大结构漂移 = max rank_disp),
          "overall": "stable" | "warning" | "severe"
        }
    """
    if df is None or df.empty or not features:
        return {"enabled": False, "reason": "no data"}

    train_mask = (df["trade_date"] >= pd.Timestamp(train_start)) & (df["trade_date"] <= pd.Timestamp(train_end))
    train_df = df[train_mask]
    # 最近 n 个交易日
    all_dates = sorted(df["trade_date"].unique())
    recent_dates = all_dates[-n_recent_days:]
    if not recent_dates:
        return {"enabled": False, "reason": "no recent dates"}
    recent_df = df[df["trade_date"].isin(recent_dates)]
    if train_df.empty or recent_df.empty:
        return {"enabled": False, "reason": "empty train or recent frame"}

    # rank_disp 基准窗：与 recent 等长、紧邻其前的交易日窗口。
    # 不能用整个训练窗做基准——训练窗均值（数千日）极稳，而 recent 窗
    # （30 日）的每股 rank 均值标准误 ~0.09，两者相减会把纯采样噪声
    # 误判为截面重排（历史误报"严重漂移"的根因）。等长相邻窗对比时
    # 两侧噪声量级相当、相互抵消，剩下的才是真实结构漂移。
    prior_dates = all_dates[:-n_recent_days]
    if len(prior_dates) >= n_recent_days:
        baseline_dates = prior_dates[-n_recent_days:]
        baseline_source = "prior_window"
    else:
        # 历史不足（如新上市数据）：退回训练窗并降低置信
        baseline_dates = [d for d in all_dates if d not in set(recent_dates)]
        baseline_source = "train_window"
    baseline_df = df[df["trade_date"].isin(baseline_dates)]
    if baseline_df.empty:
        return {"enabled": False, "reason": "empty baseline frame"}

    # 只取可用特征
    usable = [f for f in features if f in df.columns]
    # 采样控制计算量：每边最多 5 万行
    train_sample = train_df[usable].dropna(how="all").sample(min(50000, len(train_df)), random_state=42)
    recent_sample = recent_df[usable].dropna(how="all").sample(min(50000, len(recent_df)), random_state=42)
    if train_sample.empty or recent_sample.empty:
        return {"enabled": False, "reason": "empty sample"}

    # 批量算全部特征的截面结构漂移（身份级 rank 位移，等长相邻窗对比）
    rank_disp_map = _compute_rank_disp_all(baseline_df, recent_df, usable)

    # 噪声本底校准：截面 rank 有强时间自相关（动量/估值类特征尤甚），
    # 短窗均值 rank 的采样噪声很大，任何固定阈值都会随市场状态误报。
    # 在全部可用日期内（模型验证段也包含——"正常波动"应以模型见过的
    # 全部市场状态为准）取最多 4 组相邻等长窗位移：中位数作噪声本底、
    # 最大值作"历史波动包络"。近期位移超出包络才算真实漂移，仅高于
    # 单点本底属于市场正常波动区间（牛市量能/风格轮动）。
    noise_floor_map: dict[str, dict] = {}
    pool_dates = all_dates[:-n_recent_days]
    ntd = len(pool_dates)
    if ntd >= 4 * n_recent_days:
        n_samples = min(8, max(1, (ntd - 2 * n_recent_days) // n_recent_days))
        step = (ntd - 2 * n_recent_days) // max(n_samples, 1) if n_samples > 1 else 0
        sample_specs = []
        for i in range(n_samples):
            start = min(i * step, ntd - 2 * n_recent_days)
            end = start + 2 * n_recent_days
            if (start, end) not in sample_specs:
                sample_specs.append((start, end))
        per_sample_maps = []
        for start, end in sample_specs:
            win_a = pool_dates[start: start + n_recent_days]
            win_b = pool_dates[start + n_recent_days: end]
            per_sample_maps.append(_compute_rank_disp_all(
                df[df["trade_date"].isin(win_a)],
                df[df["trade_date"].isin(win_b)],
                usable,
            ))
        for f in usable:
            means = [
                m[f]["mean"] for m in per_sample_maps
                if m.get(f, {}).get("mean") is not None and np.isfinite(m[f]["mean"])
            ]
            if means:
                noise_floor_map[f] = {
                    "mean": float(np.median(means)),      # 噪声本底
                    "envelope": float(np.max(means)),     # 历史波动包络
                }
    floor_values = [
        v["mean"] for v in noise_floor_map.values()
        if v.get("mean") is not None and np.isfinite(v["mean"])
    ]
    noise_floor = float(np.median(floor_values)) if floor_values else float("nan")
    adaptive = bool(floor_values)

    results = []
    for f in usable:
        a = train_sample[f].to_numpy()
        b = recent_sample[f].to_numpy()
        level_psi = _psi_single(a, b)
        if not np.isfinite(level_psi):
            continue
        disp_stat = rank_disp_map.get(f) or {}
        rank_disp = disp_stat.get("mean")
        # rank_disp 不可估计（交集过小/数据不足）→ 保守处理：
        # 不能当良性置 0（会掩蔽真实漂移），按水平 PSI 判级并标记 unreliable
        rank_reliable = bool(rank_disp is not None and np.isfinite(rank_disp))
        z = None
        feat_floor = None
        feat_envelope = None
        if not rank_reliable:
            rank_disp = level_psi  # 用水平 PSI 兜底判级（不静默归 0）
            if rank_disp >= 0.25:
                level = "severe"
            elif rank_disp >= 0.10:
                level = "medium"
            else:
                level = "stable"
        else:
            rank_disp = float(rank_disp)
            # 判级 = 统计显著性检验，而非固定阈值/本底倍数：
            # 位移均值的标准误 ≈ 个股位移横截面 std / √n，真实的结构漂移
            # 是个股层面的相干位移，几百只股票的面板上即使幅度不大
            # （如半壁板块 +3σ 重排，均值位移仅 ~0.24）也极显著；
            # 纯采样噪声则只贡献本底量级的非相干位移，z≈0。
            # 倍数法失效的原因：结构漂移的位移有几何上界（"半升半降"
            # 重排均值位移上界 ~0.25），短窗高噪时达不到 4×本底。
            feat_stat = noise_floor_map.get(f) or {}
            feat_floor = feat_stat.get("mean")
            feat_envelope = feat_stat.get("envelope", feat_floor)
            feat_std = disp_stat.get("std")
            feat_n = disp_stat.get("n", 0)
            floor_ok = (
                adaptive
                and feat_floor is not None and np.isfinite(feat_floor) and feat_floor > 1e-4
                and feat_std is not None and np.isfinite(feat_std) and feat_std > 1e-4
                and feat_n >= 50
            )
            if floor_ok:
                # 本底自身也是单次抽样估计 → 标准误放宽 √2
                se = feat_std / (feat_n ** 0.5) * (2.0 ** 0.5)
                z = (rank_disp - feat_floor) / se
                # 双重门槛：z 显著（排除短窗采样噪声）+ 超出历史波动包络
                # （排除"高于单点本底但仍在市场正常波动区间内"的风格轮动）。
                # 幅度下限防"统计显著但经济无意义"（超大面板微小相干位移）。
                if z >= 8.0 and rank_disp >= 0.10 and rank_disp >= 1.25 * feat_envelope:
                    level = "severe"
                elif z >= 4.0 and rank_disp >= 0.05 and rank_disp >= feat_envelope:
                    level = "medium"
                else:
                    level = "stable"
            else:
                # 本底不可估计：退回固定阈值（中等 ≥0.10 / 严重 ≥0.25）
                feat_floor = None
                feat_envelope = None
                if rank_disp >= 0.25:
                    level = "severe"
                elif rank_disp >= 0.10:
                    level = "medium"
                else:
                    level = "stable"
        # 水平高但截面结构未显著漂移 = 良性量纲膨胀
        benign_scale = rank_reliable and level_psi >= 0.05 and level == "stable"
        results.append({
            "feature": f,
            "psi": round(level_psi, 4),      # 兼容原字段（水平 PSI）
            "rank_disp": round(rank_disp, 4), # 截面结构漂移（身份级 rank 位移）
            "z": round(z, 2) if z is not None and np.isfinite(z) else None,
            "noise_floor": round(feat_floor, 4) if feat_floor is not None and np.isfinite(feat_floor) else None,
            "envelope": round(feat_envelope, 4) if feat_envelope is not None and np.isfinite(feat_envelope) else None,
            "level": level,
            "benign_scale": benign_scale,     # 良性量纲膨胀标记
            "rank_reliable": rank_reliable,   # rank 位移是否可估计
        })

    if not results:
        return {"enabled": False, "reason": "no computable features"}

    results.sort(key=lambda r: (r["rank_disp"], r["psi"]), reverse=True)
    drift_counts = {"stable": 0, "medium": 0, "severe": 0}
    for r in results:
        drift_counts[r["level"]] += 1

    # overall 判定基于 rank_disp（真实结构漂移），而非水平量纲
    severe_count = drift_counts["severe"]
    medium_count = drift_counts["medium"]
    # 2026-08 下调阈值以提高灵敏度：更少的 severe/medium 数即可触发告警
    severe_ratio = severe_count / max(1, len(results))
    if severe_count >= 3 or severe_ratio >= 0.3 or (severe_count + medium_count) >= max(5, len(results) * 0.3):
        overall = "severe"
    elif severe_count >= 1 or medium_count >= 3:
        overall = "warning"
    else:
        overall = "stable"

    return {
        "enabled": True,
        "train_start": train_start,
        "train_end": train_end,
        "baseline_start": str(baseline_dates[0].date()),
        "baseline_end": str(baseline_dates[-1].date()),
        "baseline_source": baseline_source,
        "recent_start": str(recent_dates[0].date()),
        "recent_end": str(recent_dates[-1].date()),
        "noise_floor": round(noise_floor, 4) if np.isfinite(noise_floor) else None,
        "adaptive_thresholds": adaptive,
        "drift": drift_counts,
        "top_drift_features": results[:top_n],
        "max_psi": round(max(r["rank_disp"] for r in results), 4),  # 最大结构漂移（rank_disp）
        "overall": overall,
    }



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
        has_symbol = "symbol" in schema_cols
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
            if valid_end: ends.append(valid_end)
            if test_end: ends.append(test_end)
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


# ── 训练 ──────────────────────────────────────────────────────────────────────

def _split_data(df: pd.DataFrame, cfg: dict) -> tuple:
    """数据切分：显式 split 优先于 val_ratio。返回 (train_df, val_df, test_df)。

    时间序列切分必须保证 train < val < test，严禁 test=val（经典数据泄漏）。
    """
    model_cfg = cfg.get("model", {})

    def _frame_range_text(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "EMPTY"
        return f"{frame['trade_date'].min().date()}~{frame['trade_date'].max().date()}"

    split_cfg = cfg.get("split", {})
    if split_cfg.get("valid"):
        valid_start_str, valid_end_str = split_cfg["valid"]
        train_start_str, train_end_str = split_cfg["train"]
        requested_train = f"{train_start_str}~{train_end_str}"
        requested_val = f"{valid_start_str}~{valid_end_str}"
        # train 必须有下界，否则会吃进 train_start 之前的数据；
        # 且 train_end 必须早于 valid_start，否则三段重叠造成泄漏
        if pd.Timestamp(train_end_str) >= pd.Timestamp(valid_start_str):
            raise RuntimeError(
                f"split.train end ({train_end_str}) must be strictly before "
                f"split.valid start ({valid_start_str}); overlapping segments "
                "leak validation data into training."
            )
        train_df = df[
            (df["trade_date"] >= pd.Timestamp(train_start_str)) &
            (df["trade_date"] <= pd.Timestamp(train_end_str))
        ].copy()
        val_df   = df[
            (df["trade_date"] >= pd.Timestamp(valid_start_str)) &
            (df["trade_date"] <= pd.Timestamp(valid_end_str))
        ].copy()
        if split_cfg.get("test"):
            test_start_str, test_end_str = split_cfg["test"]
            if pd.Timestamp(test_start_str) <= pd.Timestamp(valid_end_str):
                raise RuntimeError(
                    f"split.test start ({test_start_str}) must be strictly after "
                    f"split.valid end ({valid_end_str}); overlapping segments "
                    "make early stopping and final evaluation share data."
                )
            requested_test = f"{test_start_str}~{test_end_str}"
            test_df = df[
                (df["trade_date"] >= pd.Timestamp(test_start_str)) &
                (df["trade_date"] <= pd.Timestamp(test_end_str))
            ].copy()
        else:
            raise RuntimeError(
                "split.test is required when split.valid is configured. "
                "test=val is a classic data leakage pattern — early stopping "
                "and model selection would both happen on test data, "
                "inflating all reported metrics. "
                "Please add a 'test' section to the split config, e.g.:\n"
                "  split:\n"
                "    train: ['2020-01-01', '2023-12-31']\n"
                "    valid: ['2024-01-01', '2024-06-30']\n"
                "    test:  ['2024-07-01', '2024-12-31']"
            )
        logger.info(f"Split mode: train~{split_cfg['train'][1]}  val {valid_start_str}~{valid_end_str}")
    else:
        val_ratio = float(model_cfg.get("val_ratio") or 0.15)
        dates = sorted(df["trade_date"].unique())
        if not dates:
            raise RuntimeError("No rows available for split after preprocessing. 请检查训练时间窗口与特征快照覆盖范围。")
        # 三段式切分：train | val | test，避免 test=val 的数据泄漏
        test_ratio = val_ratio / 2.0
        val_start_idx = int(len(dates) * (1 - val_ratio))
        test_start_idx = int(len(dates) * (1 - test_ratio))
        val_start = dates[val_start_idx]
        test_start = dates[test_start_idx]
        train_df = df[df["trade_date"] < val_start].copy()
        val_df   = df[(df["trade_date"] >= val_start) & (df["trade_date"] < test_start)].copy()
        test_df  = df[df["trade_date"] >= test_start].copy()
        train_start = pd.Timestamp(df["trade_date"].min()).date()
        train_end = (pd.Timestamp(val_start) - pd.Timedelta(days=1)).date()
        requested_train = f"{train_start}~{train_end}"
        requested_val = f"{pd.Timestamp(val_start).date()}~{pd.Timestamp(test_start).date() - pd.Timedelta(days=1)}"
        requested_test = f"{pd.Timestamp(test_start).date()}~{pd.Timestamp(df['trade_date'].max()).date()}"
        logger.info(
            f"val_ratio mode (3-way split): train[{len(train_df)}]~{pd.Timestamp(val_start).date() - pd.Timedelta(days=1)}"
            f"  val[{len(val_df)}] {pd.Timestamp(val_start).date()}~{pd.Timestamp(test_start).date() - pd.Timedelta(days=1)}"
            f"  test[{len(test_df)}] {pd.Timestamp(test_start).date()}~"
        )

    # ── Embargo：标签是未来 horizon 日收益，train 末尾样本的标签落在 val 区间内 ──
    # 不隔离会让 val/test 的价格信息经标签渗回 train。裁掉每段尾部 horizon 个交易日。
    _horizon = max(1, int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1))
    _embargo_days = _horizon + _EXECUTION_LAG_DAYS
    if _embargo_days > 0:
        def _embargo(frame: pd.DataFrame, name: str) -> pd.DataFrame:
            if frame.empty:
                return frame
            days = sorted(frame["trade_date"].unique())
            if len(days) <= _embargo_days:
                logger.warning(
                    "Embargo skipped for %s: only %d trading days <= label span %d",
                    name, len(days), _embargo_days,
                )
                return frame
            cutoff = days[-_embargo_days]
            trimmed = frame[frame["trade_date"] < cutoff].copy()
            logger.info(
                "Embargo %s: dropped last %d trading days (%d -> %d rows)",
                name, _embargo_days, len(frame), len(trimmed),
            )
            return trimmed

        train_df = _embargo(train_df, "train")
        val_df = _embargo(val_df, "val")

    train_df = train_df.reset_index(drop=True)
    val_df = val_df.reset_index(drop=True)
    test_df = test_df.reset_index(drop=True)
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
    return train_df, val_df, test_df


def _wfa_parse_cfg(wfa_cfg: dict | None) -> dict:
    """解析并规范化 WFA 诊断配置。

    返回:
        {
          "enabled": bool,
          "strategy": "rolling" | "expanding",
          "n_windows": int,
          "train_years": int,   # 每窗训练长度（年，仅 rolling 用）
          "val_months": int,    # 每窗验证长度（月）
          "step_months": int,   # 窗口推进步长（月）
          "start": str,         # 首个窗口训练起点（可选，默认用数据起点）
          "max_train_end": str  # 诊断窗口的最晚训练终点（可选）
        }
    """
    if not isinstance(wfa_cfg, dict) or not wfa_cfg.get("enabled"):
        return {"enabled": False}

    strategy = str(wfa_cfg.get("strategy") or "rolling").strip().lower()
    if strategy not in ("rolling", "expanding"):
        logger.warning("Invalid wfa.strategy=%s, fallback to 'rolling'", strategy)
        strategy = "rolling"

    def _to_int(v, default: int, lo: int, hi: int) -> int:
        try:
            n = int(v)
        except Exception:
            n = default
        return max(lo, min(hi, n))

    return {
        "enabled": True,
        "strategy": strategy,
        "n_windows": _to_int(wfa_cfg.get("n_windows"), 4, 1, 12),
        "train_years": _to_int(wfa_cfg.get("train_years"), 3, 1, 8),
        "val_months": _to_int(wfa_cfg.get("val_months"), 12, 1, 36),
        "step_months": _to_int(wfa_cfg.get("step_months"), 12, 1, 36),
        "start": str(wfa_cfg.get("start") or "").strip(),
        "max_train_end": str(wfa_cfg.get("max_train_end") or "").strip(),
    }


def _wfa_split_window(
    df: pd.DataFrame,
    wfa: dict,
    idx: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造第 idx 个 WFA 窗口的 (train_df, val_df)。

    基于 df 中的实际交易日历（trade_date）推进，避免节假日/停牌导致窗口稀疏。
    rolling:  训练起点 = 验证起点 - train_years 年（按交易日回退），训练集长度固定。
    expanding:训练起点 = 数据最早日期，训练集随 idx 扩张。
    """
    all_dates = sorted(df["trade_date"].unique())
    if not all_dates:
        return pd.DataFrame(), pd.DataFrame()

    step_days = wfa["step_months"] * 30
    train_years_days = wfa["train_years"] * 365
    val_days = wfa["val_months"] * 30

    # 首个验证起点：rolling 需先留出 train_years 训练段 + val_days 验证段，
    # 保证首窗训练集完整；expanding 首窗即可从数据起点开始。
    start_str = wfa.get("start")
    base = pd.Timestamp(start_str) if start_str else pd.Timestamp(all_dates[0])
    if wfa["strategy"] == "rolling":
        first_anchor_offset = pd.Timedelta(days=train_years_days + val_days)
    else:
        first_anchor_offset = pd.Timedelta(days=val_days)
    anchor = base + first_anchor_offset + pd.Timedelta(days=idx * step_days)

    # 取 anchor 之前最近的交易日作为验证起点
    anchor_ts = pd.Timestamp(anchor.date())
    val_start = max((d for d in all_dates if d <= anchor_ts), default=None)
    if val_start is None:
        return pd.DataFrame(), pd.DataFrame()
    val_end = max((d for d in all_dates if d <= anchor_ts + pd.Timedelta(days=val_days)), default=val_start)

    if wfa["strategy"] == "expanding":
        train_start = all_dates[0]
    else:
        # rolling：训练起点 = 验证起点往前推 train_years 年（取最近交易日），长度固定。
        # 数据不足（如首个窗口往前推超过数据起点）时回退到数据最早日，保证窗口可运行。
        train_start = max(
            (d for d in all_dates if d <= val_start - pd.Timedelta(days=train_years_days)),
            default=all_dates[0],
        )

    train_df = df[
        (df["trade_date"] >= train_start) &
        (df["trade_date"] < val_start)
    ].copy()
    val_df = df[
        (df["trade_date"] >= val_start) &
        (df["trade_date"] <= val_end)
    ].copy()

    if train_df.empty or val_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    return train_df, val_df


def _train_wfa_single(
    cfg: dict,
    features: list[str],
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    wfa: dict,
    idx: int,
) -> dict | None:
    """训练单个 WFA 窗口，返回该窗口的指标。支持树模型 + linear。"""
    model_cfg = cfg.get("model", {})
    model_type = str(model_cfg.get("type", "lightgbm")).strip().lower()

    if model_type in _DL_MODEL_TYPES:
        logger.warning("[WFA] window %d: skip DL model '%s' (too slow for WFA)", idx, model_type)
        return None

    try:
        fill_values, X_train, y_train, X_val, y_val, _fill = _prepare_arrays(
            train_df, val_df, features, prep_cfg=cfg.get("preprocessing") or {}
        )
        if X_train.shape[0] < 100 or X_val.shape[0] < 10:
            logger.warning("[WFA] window %d: too few samples train=%d val=%d", idx, X_train.shape[0], X_val.shape[0])
            return None

        if model_type == "lightgbm":
            model = _train_lgb(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "xgboost":
            model = _train_xgb(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "catboost":
            model = _train_catboost(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "linear":
            model = _train_linear(cfg, features, X_train, y_train, X_val, y_val)
        else:
            logger.warning("[WFA] window %d: unsupported model '%s'", idx, model_type)
            return None

        y_val_pred = _predict_with_model(model, _fill(val_df), model_type, features)
        y_val_true = val_df["label"].astype("float32").to_numpy()
        m = _compute_metrics(val_df, y_val_true, y_val_pred)

        return {
            "window_idx": idx,
            "strategy": wfa["strategy"],
            "train_start": str(train_df["trade_date"].min().date()),
            "train_end": str(train_df["trade_date"].max().date()),
            "val_start": str(val_df["trade_date"].min().date()),
            "val_end": str(val_df["trade_date"].max().date()),
            "train_rows": int(len(train_df)),
            "val_rows": int(len(val_df)),
            "ic": m["ic"],
            "rank_ic": m["rank_ic"],
            "rank_icir": m["rank_icir"],
            "rmse": m["rmse"],
            "auc": m["auc"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[WFA] window %d failed: %s", idx, exc)
        return None


def train_wfa(df: pd.DataFrame, features: list[str], cfg: dict) -> dict:
    """Walk-Forward 稳定性诊断：滚动/扩张窗口训练并汇总 IC 稳定性。

    返回诊断报告（dict），不含正式模型产物。诊断不产出模型，仅用于评估
    模型在多个历史区间上的 IC 稳定性与参数漂移。
    """
    wfa = _wfa_parse_cfg(cfg.get("wfa"))
    if not wfa["enabled"]:
        return {"enabled": False}

    logger.info(
        "=== WFA Diagnosis: strategy=%s windows=%d train_years=%d val_months=%d step_months=%d ===",
        wfa["strategy"], wfa["n_windows"], wfa["train_years"], wfa["val_months"], wfa["step_months"],
    )

    model_cfg = cfg.get("model", {})
    model_type = str(model_cfg.get("type", "lightgbm")).strip().lower()

    # 训练时长预算：WFA 窗口间检查剩余时间，超时则停止后续窗口
    budget_min = int((cfg.get("max_time_minutes") or 120))
    budget_deadline = time.time() + budget_min * 60
    # 为正式训练预留至少 40% 时长，WFA 最多用 60%
    wfa_budget_deadline = time.time() + max(1, budget_min * 60 * 0.6)

    windows: list[dict] = []
    for idx in range(wfa["n_windows"]):
        if time.time() >= wfa_budget_deadline:
            logger.warning("[WFA] time budget (%.0f%% of %dmin) reached, stop at window %d", 60, budget_min, idx)
            break
        train_df, val_df = _wfa_split_window(df, wfa, idx)
        if train_df.empty or val_df.empty:
            logger.warning("[WFA] window %d skipped: empty split", idx)
            continue
        res = _train_wfa_single(cfg, features, train_df, val_df, wfa, idx)
        if res:
            windows.append(res)

    if not windows:
        return {"enabled": True, "strategy": wfa["strategy"], "windows": [], "error": "no windows completed"}

    ic_vals = [w["ic"] for w in windows if w["ic"] is not None and np.isfinite(w["ic"])]
    ric_vals = [w["rank_ic"] for w in windows if w["rank_ic"] is not None and np.isfinite(w["rank_ic"])]

    def _safe_mean(xs: list[float]) -> float:
        return float(np.mean(xs)) if xs else float("nan")

    def _safe_std(xs: list[float]) -> float:
        return float(np.std(xs)) if len(xs) > 1 else 0.0

    summary = {
        "strategy": wfa["strategy"],
        "n_windows": len(windows),
        "ic_mean": _safe_mean(ic_vals),
        "ic_std": _safe_std(ic_vals),
        "ic_min": float(min(ic_vals)) if ic_vals else float("nan"),
        "ic_max": float(max(ic_vals)) if ic_vals else float("nan"),
        "rank_ic_mean": _safe_mean(ric_vals),
        "rank_ic_std": _safe_std(ric_vals),
        "positive_rate": float(np.mean([v > 0 for v in ic_vals])) if ic_vals else float("nan"),
        # IC 稳定性：std 越小越稳；ICIR 综合收益/波动
        "stability": "stable" if (len(ic_vals) >= 2 and abs(_safe_std(ic_vals)) <= 0.02) else "unstable",
        "model_type": model_type,
    }
    # 综合 ICIR（跨窗口）
    if ric_vals and abs(_safe_std(ric_vals)) > 1e-12:
        summary["overall_icir"] = float(np.mean(ric_vals) / (np.std(ric_vals) + 1e-9))
    else:
        summary["overall_icir"] = float("nan")

    return {"enabled": True, **summary, "windows": windows}


def _prepare_arrays(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    prep_cfg: dict | None = None,
) -> tuple:
    """计算 fill_values 并转换为 numpy 数组。

    prep_cfg 启用时（`preprocessing.enabled=true`），对特征做截面预处理：
    per (trade_date, feature) 中位数填充 + 分位缩尾 + 截面 Z-score。
    类别特征（ind_code_l1/l2）不参与变换（保持原始编码）。
    返回 (fill_values, X_train, y_train, X_val, y_val, _fill_fn)。
    """
    import math
    from preprocessing import cross_sectional_preprocess

    prep_cfg = prep_cfg or {}
    prep_enabled = bool(prep_cfg.get("enabled", False))
    _exclude = {"ind_code_l1", "ind_code_l2"}
    _prep_feats = [f for f in features if f not in _exclude]

    if prep_enabled and _prep_feats:
        _winsor = bool(prep_cfg.get("winsor", True))
        train_df = cross_sectional_preprocess(train_df, _prep_feats, enabled=True, winsor=_winsor)
        val_df = cross_sectional_preprocess(val_df, _prep_feats, enabled=True, winsor=_winsor)
        logger.info(
            "Cross-sectional preprocessing enabled: %d features (exclude %s)",
            len(_prep_feats), sorted(_exclude & set(features)),
        )

    fill_values_raw = train_df[features].median().to_dict()
    fill_values = {k: (0.0 if (isinstance(v, float) and math.isnan(v)) else v) for k, v in fill_values_raw.items()}

    def _fill(frame: pd.DataFrame) -> np.ndarray:
        x = frame[features].copy()
        for c in features:
            x[c] = x[c].astype("float32").fillna(fill_values[c])
        return x.to_numpy(dtype=np.float32)

    X_train = _fill(train_df)
    y_train = train_df["label"].astype("float32").to_numpy()
    X_val = _fill(val_df)
    y_val = val_df["label"].astype("float32").to_numpy()
    return fill_values, X_train, y_train, X_val, y_val, _fill


def _lgb_rank_ic_feval(preds: np.ndarray, dataset: lgb.Dataset) -> tuple[str, float, bool]:
    """LightGBM 自定义评估：全局 Rank IC（Spearman），作为 l2 的补充监控指标。

    不参与早停决策（early stopping 仍以 l2 为准），仅输出日志供观察
    rank_ic 是否随迭代提升，防止模型只优化 l2 而 rank_ic 停滞。
    """
    label = dataset.get_label()
    try:
        from scipy.stats import spearmanr
        valid = np.isfinite(preds) & np.isfinite(label)
        if valid.sum() < 10:
            return "rank_ic", 0.0, True
        rho, _ = spearmanr(preds[valid], label[valid])
        if not np.isfinite(rho):
            rho = 0.0
        return "rank_ic", float(rho), True  # higher is better
    except Exception:
        return "rank_ic", 0.0, True


def _train_lgb(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
               X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """LightGBM 训练。"""
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_LGB_PARAMS, **model_cfg.get("params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["objective"] = "binary"
        params["metric"] = "binary_logloss"
    # 线程数：num_threads=-1 用满所有核心；多模型/OOF 训练时内存叠加易 OOM，
    # 可用 TRAIN_NTHREADS 环境变量限流
    params["num_threads"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed（用户未显式覆盖时）
    params.setdefault("seed", int((cfg.get("seed") or 42)))
    params.setdefault("bagging_seed", int((cfg.get("seed") or 42)))
    params.setdefault("feature_fraction_seed", int((cfg.get("seed") or 42)))
    num_boost_round = int(model_cfg.get("num_boost_round", 1000))
    early_stopping_rounds = max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100))

    ds_train = lgb.Dataset(X_train, label=y_train, feature_name=features, free_raw_data=True)
    ds_val = lgb.Dataset(X_val, label=y_val, feature_name=features, free_raw_data=True)

    callbacks = [
        lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=True),
        lgb.log_evaluation(100),
    ]
    # 补充监控 rank_ic（不影响早停，仅日志），帮助判断是否只优化 l2 而 rank_ic 停滞
    if str(model_cfg.get("monitor_rank_ic", "true")).lower() in ("1", "true", "yes", "on"):
        # lightgbm 4.x 的 record_evaluation(eval_result) 需要 dict 而非 Dataset，
        # 传空 dict，feval 返回的 rank_ic 会被自动写入。
        callbacks.append(lgb.record_evaluation({}))
    model = lgb.train(
        params, ds_train,
        num_boost_round=num_boost_round,
        valid_sets=[ds_train, ds_val],
        valid_names=["train", "valid"],
        feval=_lgb_rank_ic_feval,
        callbacks=callbacks,
    )
    return model


_QUANTILE_LEVELS = (0.1, 0.5, 0.9)


def _quantile_mode_enabled(cfg: dict) -> bool:
    """Whether this run requests the deliberately narrow v1 quantile contract."""
    return str((cfg.get("model", {}) or {}).get("prediction_mode") or "point").lower() == "quantile"


def _validate_quantile_config(cfg: dict, model_type: str) -> None:
    """Reject unsupported combinations before any expensive training work starts."""
    if not _quantile_mode_enabled(cfg):
        return
    context = cfg.get("context", {}) or {}
    target_mode = str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower()
    model_types = (cfg.get("model", {}) or {}).get("types") or [model_type]
    if (
        model_type != "lightgbm"
        or len(model_types) != 1
        or str(model_types[0]).lower() != "lightgbm"
        or target_mode != "return"
        or str(context.get("market") or "CN").upper() != "CN"
    ):
        raise ValueError(
            "prediction_mode=quantile 仅支持 A 股(CN)单 LightGBM 的未来收益率回归模型"
        )


def _pinball_loss(y_true: np.ndarray, prediction: np.ndarray, alpha: float) -> float:
    error = np.asarray(y_true, dtype=float) - np.asarray(prediction, dtype=float)
    return float(np.mean(np.maximum(alpha * error, (alpha - 1.0) * error)))


def _train_lgb_quantiles(
    cfg: dict,
    features: list[str],
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    val_dates: pd.Series,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Train P10/P50/P90 LightGBM models and calibrate the outer interval.

    The latest 20% of validation trading days is held out from early stopping and
    used only for conformal calibration.  This keeps the reported test coverage
    out-of-sample and avoids turning a volatility heuristic into a fake interval.
    """
    unique_dates = np.sort(pd.to_datetime(val_dates).dropna().unique())
    if len(unique_dates) < 5:
        raise ValueError("分位推理需要至少 5 个验证交易日用于早停和校准")
    calibration_days = max(1, int(np.ceil(len(unique_dates) * 0.2)))
    calibration_start = unique_dates[-calibration_days]
    calibration_mask = pd.to_datetime(val_dates).to_numpy() >= calibration_start
    early_stop_mask = ~calibration_mask
    if int(early_stop_mask.sum()) < 20 or int(calibration_mask.sum()) < 20:
        raise ValueError("分位推理验证集不足：早停段和校准段各至少需要 20 个样本")

    model_cfg = cfg.get("model", {}) or {}
    models: dict[str, Any] = {}
    raw_predictions: dict[str, np.ndarray] = {}
    for alpha, key in zip(_QUANTILE_LEVELS, ("p10", "p50", "p90"), strict=True):
        quantile_cfg = dict(cfg)
        quantile_model_cfg = dict(model_cfg)
        quantile_params = {
            **DEFAULT_LGB_PARAMS,
            **(model_cfg.get("params") or {}),
            "objective": "quantile",
            "metric": "quantile",
            "alpha": alpha,
        }
        quantile_model_cfg["params"] = quantile_params
        quantile_cfg["model"] = quantile_model_cfg
        models[key] = _train_lgb(
            quantile_cfg, features, X_train, y_train,
            X_val[early_stop_mask], y_val[early_stop_mask],
        )
        raw_predictions[key] = models[key].predict(X_val, num_iteration=models[key].best_iteration)

    raw_p10 = raw_predictions["p10"]
    raw_p50 = raw_predictions["p50"]
    raw_p90 = raw_predictions["p90"]
    ordered = np.sort(np.column_stack((raw_p10, raw_p50, raw_p90)), axis=1)
    raw_p10, raw_p50, raw_p90 = ordered[:, 0], ordered[:, 1], ordered[:, 2]
    cal_y = y_val[calibration_mask]
    nonconformity = np.maximum(raw_p10[calibration_mask] - cal_y, cal_y - raw_p90[calibration_mask])
    # Finite-sample conformal quantile for a target central 80% interval.
    q_index = min(len(nonconformity) - 1, int(np.ceil((len(nonconformity) + 1) * 0.8)) - 1)
    conformal_offset = float(np.partition(nonconformity, q_index)[q_index])
    calibrated_p10 = raw_p10 - conformal_offset
    calibrated_p90 = raw_p90 + conformal_offset
    calibrated = np.sort(np.column_stack((calibrated_p10, raw_p50, calibrated_p90)), axis=1)
    coverage_raw = float(np.mean((y_val >= raw_p10) & (y_val <= raw_p90)))
    coverage_calibrated = float(np.mean((y_val >= calibrated[:, 0]) & (y_val <= calibrated[:, 2])))
    calibration = {
        "method": "conformalized_quantile_regression",
        "central_coverage": 0.8,
        "calibration_fraction": 0.2,
        "calibration_start": str(pd.Timestamp(calibration_start).date()),
        "sample_count": int(calibration_mask.sum()),
        "offset": conformal_offset,
        "raw_coverage": coverage_raw,
        "calibrated_coverage": coverage_calibrated,
        "mean_interval_width": float(np.mean(calibrated[:, 2] - calibrated[:, 0])),
        "pinball_loss": {
            "p10": _pinball_loss(y_val, raw_p10, 0.1),
            "p50": _pinball_loss(y_val, raw_p50, 0.5),
            "p90": _pinball_loss(y_val, raw_p90, 0.9),
        },
    }
    return models, calibration


def _train_xgb(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
               X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """XGBoost 训练。"""
    import xgboost as xgb
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_XGB_PARAMS, **model_cfg.get("xgb_params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["objective"] = "binary:logistic"
        params["eval_metric"] = "auc"
    # 限制线程：nthread=-1 用满所有核心，多模型/OOF 训练时内存叠加易 OOM
    params["nthread"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed
    params.setdefault("seed", int((cfg.get("seed") or 42)))
    # LightGBM 用 max_depth=-1 表示不限深度，XGBoost 只接受 >=0，直接传会启动失败
    if int(params.get("max_depth") or 0) < 0:
        logger.warning(
            "xgb_params.max_depth=%s invalid for XGBoost (LightGBM convention), fallback to %s",
            params["max_depth"], DEFAULT_XGB_PARAMS["max_depth"],
        )
        params["max_depth"] = DEFAULT_XGB_PARAMS["max_depth"]
    num_boost_round = int(model_cfg.get("num_boost_round", 1000))
    early_stopping_rounds = max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100))

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=features)
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=features)

    evals_result: dict = {}
    model = xgb.train(
        params, dtrain,
        num_boost_round=num_boost_round,
        evals=[(dtrain, "train"), (dval, "valid")],
        evals_result=evals_result,
        early_stopping_rounds=early_stopping_rounds,
        verbose_eval=100,
    )
    return model


def _train_catboost(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
                    X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """CatBoost 训练。支持 cat_features（行业编码等类别特征）。"""
    from catboost import CatBoost, Pool
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_CATBOOST_PARAMS, **model_cfg.get("catboost_params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["loss_function"] = "Logloss"
        params.setdefault("eval_metric", "AUC")
    # 限制线程：thread_count=-1 用满所有核心，多模型/OOF 训练时内存叠加易 OOM
    params["thread_count"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed
    params.setdefault("random_seed", int((cfg.get("seed") or 42)))
    # iterations 覆盖 num_boost_round
    if "iterations" not in model_cfg.get("catboost_params", {}):
        params["iterations"] = int(model_cfg.get("num_boost_round", 1000))

    # 识别类别特征（ind_code_l1 等）
    cat_feature_indices = []
    for i, feat in enumerate(features):
        if feat in ("ind_code_l1", "ind_code_l2"):
            cat_feature_indices.append(i)
    if cat_feature_indices:
        # CatBoost 要求类别特征为 int 类型
        for idx in cat_feature_indices:
            X_train[:, idx] = X_train[:, idx].astype(int)
            X_val[:, idx] = X_val[:, idx].astype(int)

    # has_time：数据按时间顺序排列时开启，CatBoost 按时间处理序列数据，
    # 避免未来信息影响过去预测。需行序为时间序（load_data 已按 symbol+trade_date 排序）。
    # 注意：旧版 catboost 的 Pool 不支持 has_time 参数，需 try/except 降级。
    has_time = str(params.get("has_time", "false")).lower() in ("1", "true", "yes", "on")
    _pool_kwargs: dict = {
        "feature_names": features,
        "cat_features": cat_feature_indices if cat_feature_indices else None,
    }
    if has_time:
        try:
            train_pool = Pool(X_train, label=y_train, has_time=True, **_pool_kwargs)
            val_pool = Pool(X_val, label=y_val, has_time=True, **_pool_kwargs)
        except TypeError:
            logger.warning("当前 catboost 版本不支持 has_time，降级为不带该参数")
            train_pool = Pool(X_train, label=y_train, **_pool_kwargs)
            val_pool = Pool(X_val, label=y_val, **_pool_kwargs)
    else:
        train_pool = Pool(X_train, label=y_train, **_pool_kwargs)
        val_pool = Pool(X_val, label=y_val, **_pool_kwargs)

    model = CatBoost(params)
    model.fit(train_pool, eval_set=val_pool, early_stopping_rounds=max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100)))
    return model


def _train_linear(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
                  X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """线性基线：收益任务用 Ridge，分类任务用 LogisticRegression。"""
    from sklearn.linear_model import LogisticRegression, Ridge
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    alpha = float(dl_params.get("alpha", 3.0))
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        model = LogisticRegression(C=1.0 / max(alpha, 1e-8), max_iter=1000,
                                   random_state=int((cfg.get("seed") or 42)))
    else:
        model = Ridge(alpha=alpha, random_state=int((cfg.get("seed") or 42)))
    model.fit(X_train, y_train)
    return model


def _train_rf(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
              X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """随机森林：Bagging 基线，用于对比 Boosting 是否真优于 Bagging。

    与 LightGBM/XGBoost 走同一 _prepare_arrays 数据流（含可选截面预处理）。
    参数：n_estimators（默认 300）、max_depth（默认 12，防止百万级数据
    完全展开导致节点数爆炸 OOM）、max_features（默认 sqrt）。
    """
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    seed = int((cfg.get("seed") or 42))
    n_estimators = int(dl_params.get("n_estimators", 300))
    # 默认 max_depth=None 时树完全展开：300 万行 × 300 棵树 ≈ 9 亿节点，
    # 内存需求数百 GB（实测 300 万行冒烟直接 OOM SIGKILL 137）。
    # 默认限深 12（约 4096 叶子/树，精度与完全展开差异 <1%），可显式覆盖。
    max_depth = dl_params.get("max_depth", 12)
    max_features = str(dl_params.get("max_features", "sqrt"))
    model_class = RandomForestClassifier if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification" else RandomForestRegressor
    model = model_class(
        n_estimators=n_estimators,
        max_depth=int(max_depth) if max_depth else None,
        max_features=max_features,
        n_jobs=_TRAIN_NTHREAD,
        random_state=seed,
    )
    model.fit(X_train, y_train)
    return model


def _train_mlp(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
               X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """MLP 基线：神经网络最简对照，验证 RNN/Transformer 是否真优于全连接。

    用 sklearn MLPRegressor（结构化表格数据基线足够），与树模型共享数据流。
    参数：hidden_layer_sizes（默认 [64, 32]）、alpha（L2，默认 1e-3）、
    early_stopping（默认 True，用 val 集早停）。
    """
    from sklearn.neural_network import MLPClassifier, MLPRegressor
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    seed = int((cfg.get("seed") or 42))
    hidden = dl_params.get("hidden_layer_sizes") or dl_params.get("hidden_size")
    if isinstance(hidden, (int, float)):
        hidden = [int(hidden), max(1, int(hidden) // 2)]
    elif not isinstance(hidden, (list, tuple)):
        hidden = [64, 32]
    alpha = float(dl_params.get("alpha", 1e-3))
    model_class = MLPClassifier if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification" else MLPRegressor
    model = model_class(
        hidden_layer_sizes=[int(h) for h in hidden],
        alpha=alpha,
        learning_rate_init=float(dl_params.get("lr", 0.001)),
        max_iter=int(dl_params.get("n_epochs", 500)),
        early_stopping=True,
        n_iter_no_change=10,
        random_state=seed,
    )
    model.fit(X_train, y_train)
    return model


# ── 深度学习训练 ────────────────────────────────────────────────────────────────

# Qlib TS 模型映射: model_type → (qlib_module, qlib_class)
_QLIB_TS_MODEL_MAP: dict[str, tuple[str, str]] = {
    "gru":         ("qlib.contrib.model.pytorch_gru_ts",         "GRU"),
    "lstm":        ("qlib.contrib.model.pytorch_lstm_ts",        "LSTM"),
    "alstm":       ("qlib.contrib.model.pytorch_alstm_ts",       "ALSTM"),
    "transformer": ("qlib.contrib.model.pytorch_transformer_ts", "TransformerModel"),
    "tcn":         ("qlib.contrib.model.pytorch_tcn_ts",         "TCN"),
}
_QLIB_FLAT_MODEL_MAP: dict[str, tuple[str, str]] = {
    "tabnet":      ("qlib.contrib.model.pytorch_tabnet",         "TabnetModel"),
}
_QLIB_MODEL_MAP = {**_QLIB_TS_MODEL_MAP, **_QLIB_FLAT_MODEL_MAP}


class _TSLazyDataset(torch.utils.data.Dataset):
    """Lazy TS dataset: 按需生成滚动窗口，避免一次性加载全部窗口到内存。

    存储原始数据 (per-instrument contiguous arrays)，__getitem__ 时动态切片。
    内存占用: O(total_rows * d_feat) 而非 O(N_windows * step_len * d_feat)。
    """

    def __init__(self, X: np.ndarray, y: np.ndarray, instrument_offsets: list[int], step_len: int):
        self.X = X              # [total_rows, d_feat] float32 contiguous
        self.y = y              # [total_rows] float32
        self.step_len = step_len
        # 每个 instrument 的有效窗口起始行号 (全局索引)。
        # 向量化构造：逐 instrument 的 Python 循环在千万行规模下会退化为分钟级开销，
        # 且 list 存数百万 int 对象内存放大数倍，这里直接生成 ndarray。
        bounds = np.asarray(instrument_offsets, dtype=np.int64)
        starts = bounds[:-1]
        lengths = bounds[1:] - starts
        n_windows = np.maximum(lengths - step_len + 1, 0)
        keep = n_windows > 0
        if not keep.any():
            raise ValueError(f"No valid TS samples (step_len={step_len}, rows={len(X)})")
        starts, n_windows = starts[keep], n_windows[keep]
        # 每个 instrument 生成 [start, start+1, ..., start+n_windows-1]
        base = np.repeat(starts, n_windows)
        within = np.arange(n_windows.sum(), dtype=np.int64) - np.repeat(
            np.concatenate([[0], np.cumsum(n_windows)[:-1]]), n_windows
        )
        self.indices = base + within

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, idx: int) -> tuple["torch.Tensor", "torch.Tensor"]:
        import torch
        start = self.indices[idx]
        window = self.X[start : start + self.step_len].copy()  # [step_len, d_feat]
        label = self.y[start + self.step_len - 1]
        # Qlib TS 模型期望: data[:, 0:-1] = features, data[-1, -1] = label
        label_col = np.full((self.step_len, 1), np.float32(0.0))
        label_col[-1, 0] = label
        row = np.concatenate([window, label_col], axis=1)  # [step_len, d_feat+1]
        # Qlib train_epoch 期望 (data, weight) 元组
        weight = torch.tensor(1.0, dtype=torch.float32)
        return torch.from_numpy(row), weight


def _build_ts_dataloader(
    df_X: pd.DataFrame,
    df_y: pd.Series,
    step_len: int,
    batch_size: int,
    shuffle: bool = True,
    feat_norm: tuple[np.ndarray, np.ndarray] | None = None,
    drop_last: bool = True,
) -> "tuple[torch.utils.data.DataLoader, tuple[np.ndarray, np.ndarray]]":
    """将扁平 DataFrame (MultiIndex: instrument x datetime) 转为 3D DataLoader。

    每个样本是 [step_len, d_feat+1]，最后一列为 label (取自最后一个时间步)。
    使用 LazyDataset 按需生成窗口，内存占用 O(rows * d_feat)。

    参数:
        feat_norm: (mean, std) 元组。如果为 None，从数据中计算统计量并返回。
                   验证集应传入训练集的统计量，避免 look-ahead。

    返回:
        (DataLoader, (mean, std)) — mean/std 始终返回，方便下游记录。
    """
    import torch
    from torch.utils.data import DataLoader

    X_values = np.ascontiguousarray(df_X.values, dtype=np.float32)
    y_values = np.ascontiguousarray(df_y.values, dtype=np.float32)

    # 填充特征中的 NaN/inf（GRU 只 mask label 中的 NaN，不处理 feature 中的 NaN）
    nan_count = np.isnan(X_values).sum()
    inf_count = np.isinf(X_values).sum()
    if nan_count > 0 or inf_count > 0:
        logger.info("Cleaning features: %d NaN, %d inf -> 0.0", nan_count, inf_count)
        X_values = np.nan_to_num(X_values, nan=0.0, posinf=0.0, neginf=0.0)

    # 清理标签中的 NaN/inf，否则 _TSLazyDataset 会让 label_col 含 NaN，
    # 导致 Transformer/TCN 的 loss_fn 和 metric_fn 输出 NaN
    y_nan = np.isnan(y_values).sum()
    y_inf = np.isinf(y_values).sum()
    if y_nan > 0 or y_inf > 0:
        logger.info("Cleaning labels: %d NaN, %d inf -> 0.0", y_nan, y_inf)
        y_values = np.nan_to_num(y_values, nan=0.0, posinf=0.0, neginf=0.0)

    # Z-score 标准化：Qlib DataHandlerLP 默认做全局 z-score，我们绕过数据管道
    # 直接用 parquet 数据，需手动标准化。不标准化的话，特征值范围在 ±10^10
    # 级别的数据进入 Transformer self-attention softmax 会产生 NaN。
    # 每列分别计算 mean/std，验证集复用训练集的统计量（避免 look-ahead）。
    if feat_norm is not None:
        _feat_mean, _feat_std = feat_norm
    else:
        _feat_mean = X_values.mean(axis=0)
        _feat_std = X_values.std(axis=0)
        _feat_std = np.where(_feat_std == 0, 1.0, _feat_std)  # 避免除零
    X_values = (X_values - _feat_mean) / _feat_std
    # 标准化后再次确保无 NaN（mean/std 计算过程中可能引入）
    _post_nan = np.isnan(X_values).sum()
    if _post_nan:
        X_values = np.nan_to_num(X_values, nan=0.0)

    if isinstance(df_X.index, pd.MultiIndex):
        # 单次取出 instrument 层，避免在循环内反复 get_level_values + 全量 == 比较
        # （旧实现对每只股票各扫一遍全表，5400股 x 640万行 ≈ 350亿次比较，单核跑数小时）
        inst_codes, inst_uniques = pd.factorize(df_X.index.get_level_values(0), sort=False)
        # 按 instrument 稳定排序，使同股票行连续（stable 保持各股票内部的时间顺序）
        order = np.argsort(inst_codes, kind="stable")
        X_values = X_values[order]
        y_values = y_values[order]
        counts = np.bincount(inst_codes, minlength=len(inst_uniques))
        offsets = np.concatenate([[0], np.cumsum(counts)]).tolist()
    else:
        offsets = [0, len(X_values)]

    dataset = _TSLazyDataset(X_values, y_values, offsets, step_len)
    # 记录排序索引（相对 df_X 的行），供推理 scatter 回原始行号
    if isinstance(df_X.index, pd.MultiIndex):
        dataset.original_rows = order
    logger.info("TS DataLoader: %d samples from %d rows (step_len=%d)", len(dataset), len(X_values), step_len)
    # drop_last=True 与 Qlib 官方 fit() 一致：末批若只剩 1 个样本，
    # collate 会把 weight 压成 0 维标量，Qlib loss_fn 内的 weight[mask] 会抛
    # IndexError: too many indices for tensor of dimension 0。
    loader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        drop_last=drop_last,
        num_workers=0,
    )
    return loader, (_feat_mean, _feat_std)


def _train_dl(
    model_type: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    dl_params: dict[str, Any],
    output_dir: Path,
    hardware: dict[str, Any] | None = None,
) -> tuple:
    """Qlib 深度学习模型训练。

    返回 (model_obj, train_metrics, val_metrics, dl_metadata)
    """
    import importlib
    import copy
    import torch

    mod_path, cls_name = _QLIB_MODEL_MAP[model_type]
    mod = importlib.import_module(mod_path)
    ModelCls = getattr(mod, cls_name)

    d_feat = len(features)
    is_ts = model_type in _QLIB_TS_MODEL_MAP

    # 构建模型参数（按 Qlib 实际 API 签名映射 + 量化最佳实践调优）
    # 前端 dl_params 传 key 不带 dl_ 前缀（如 dropout, batch_size, hidden_size），
    # train.py 之前用 dl_ 前缀（如 dl_dropout, dl_batch_size, dl_hidden_size），
    # 兼容两种写法：优先取 dl_ 前缀 key，回退到无前缀 key。
    def _dl(key: str, default: Any) -> Any:
        return dl_params.get(f"dl_{key}", dl_params.get(key, default))

    model_params: dict[str, Any] = {}
    if model_type == "transformer":
        # Transformer: d_model 需被 nhead 整除；lr 宜小（1e-4），dropout 0.2 防过拟合
        d_model = int(_dl("hidden_size", 64))
        nhead = max(1, d_model // 16)  # 64/16=4 heads, 128/16=8 heads
        d_model = nhead * (d_model // nhead)
        if d_model < nhead:
            d_model = nhead * 2
        model_params.update({
            "d_feat": d_feat,
            "d_model": d_model,
            "nhead": nhead,
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
            "lr": float(_dl("lr", 0.0001)),  # Transformer 需较小学习率
        })
    elif model_type == "tabnet":
        # TabNet: out_dim=1 绕过 Qlib decoder bug；n_steps 控制决策步数
        model_params.update({
            "d_feat": d_feat,
            "out_dim": 1,
            "final_out_dim": 1,
            "n_d": int(_dl("hidden_size", 64)),
            "n_a": int(_dl("hidden_size", 64)),
            "n_steps": max(1, int(_dl("num_layers", 5))),
            "lr": float(_dl("lr", 0.005)),  # TabNet lr 宜适中
        })
    elif model_type == "tcn":
        # TCN: n_chans 建议 64~128，dropout 0.2~0.3 防过拟合，lr 1e-4
        model_params.update({
            "d_feat": d_feat,
            "n_chans": int(_dl("hidden_size", 128)),
            "kernel_size": int(_dl("kernel_size", 5)),
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
            "lr": float(_dl("lr", 0.0001)),
        })
    else:
        # GRU/LSTM/ALSTM: hidden_size 64~128，dropout 0.2 防过拟合，lr 1e-3
        model_params.update({
            "d_feat": d_feat,
            "hidden_size": int(_dl("hidden_size", 64)),
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
        })

    n_epochs    = int(_dl("n_epochs", 200))
    batch_size  = int(_dl("batch_size", 4000))
    lr          = float(_dl("lr", 0.001))
    step_len    = int(dl_params.get("dl_step_len", 20))
    early_stop  = int(dl_params.get("early_stopping_rounds", 20))
    metric_name = str(dl_params.get("metric", "")).lower()

    # 确定 GPU 和训练参数
    # 所有 Qlib DL 模型（GRU/LSTM/ALSTM/TransformerModel/TCN/TabnetModel）都接受 GPU/n_epochs/lr/batch_size/early_stop/metric
    gpu_id = 0
    if hardware and not hardware.get("gpu_available"):
        gpu_id = -1
    model_params["GPU"] = gpu_id
    model_params["n_epochs"] = n_epochs
    model_params.setdefault("lr", lr)  # 不覆盖模型特定 lr（Transformer/TCN/TabNet 已在上方设置）
    model_params["batch_size"] = batch_size
    model_params["early_stop"] = early_stop
    model_params["metric"] = metric_name

    logger.info("DL model: %s, params=%s, is_ts=%s", model_type, model_params, is_ts)

    # 实例化模型
    model_obj = ModelCls(**model_params)

    # TabNet: Qlib 的 tabnet_model(feature, priors) 返回 (vec, sparse_loss) 元组，
    # 但 train_epoch 内部 self.loss_fn(pred, label) 未解包 pred。
    # 修复：覆盖 loss_fn 自动解包元组。out_dim=1 已在 model_params 中设置。
    if model_type == "tabnet":
        _orig_loss_fn = model_obj.loss_fn.__func__ if hasattr(model_obj.loss_fn, '__func__') else model_obj.loss_fn
        def _safe_loss_fn(self, pred, label):
            if isinstance(pred, tuple):
                pred = pred[0]
            if hasattr(pred, 'dim') and pred.dim() == 2 and pred.shape[1] == 1:
                pred = pred.squeeze(-1)
            return _orig_loss_fn(self, pred, label)
        import types
        model_obj.loss_fn = types.MethodType(_safe_loss_fn, model_obj)
        _orig_metric_fn = model_obj.metric_fn.__func__ if hasattr(model_obj.metric_fn, '__func__') else model_obj.metric_fn
        def _safe_metric_fn(self, pred, label):
            return -self.loss_fn(pred, label)
        model_obj.metric_fn = types.MethodType(_safe_metric_fn, model_obj)

    # 准备训练/验证数据
    X_train = train_df[features]
    y_train = train_df["label"]
    X_val = val_df[features]
    y_val = val_df["label"]

    # Qlib TS 模型（pytorch_*_ts）的 train_epoch/test_epoch 只接受 DataLoader，
    # 且要求样本形如 [step_len, d_feat+1]（最后一列末行为 label）；
    # flat 模型（pytorch_tabnet 等）才接受 (x_df, y_df)。
    is_ts = model_type in _QLIB_TS_MODEL_MAP

    if is_ts:
        # TS 滑窗必须按 symbol 分组，否则窗口会跨越不同股票边界产生污染样本。
        # train_df 是扁平 RangeIndex，这里用 (symbol, trade_date) 建 MultiIndex
        # 供 _build_ts_dataloader 计算每只股票的连续区间 offset。
        def _ts_indexed(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
            f = frame.sort_values(["symbol", "trade_date"])
            idx = pd.MultiIndex.from_arrays(
                [f["symbol"].to_numpy(), f["trade_date"].to_numpy()],
                names=["instrument", "datetime"],
            )
            return f[features].set_axis(idx), f["label"].set_axis(idx)

        _xt, _yt = _ts_indexed(train_df)
        _xv, _yv = _ts_indexed(val_df)
        _train_loader, _feat_norm = _build_ts_dataloader(
            _xt, _yt, step_len=step_len, batch_size=batch_size, shuffle=True
        )
        _val_loader, _ = _build_ts_dataloader(
            _xv, _yv, step_len=step_len, batch_size=batch_size, shuffle=False,
            feat_norm=_feat_norm,  # 验证集复用训练集统计量
        )
        train_args: tuple = (_train_loader,)
        val_args: tuple = (_val_loader,)

        # Qlib Transformer/TCN 的 train_epoch/test_epoch 用 `for data in loader`
        # （不解包 weight），而 GRU/LSTM/ALSTM 用 `for data, weight in loader`。
        # 包装 DataLoader，让 Transformer/TCN 只返回 data tensor。
        _needs_weight_unpack = model_type not in ("transformer", "tcn")
        if not _needs_weight_unpack:
            _orig_train_loader = train_args[0]
            _orig_val_loader = val_args[0]

            class _WeightlessLoader:
                """Strip weight from (data, weight) tuples for Qlib models that don't expect it."""
                def __init__(self, loader):
                    self._loader = loader
                def __iter__(self):
                    for batch in self._loader:
                        data = batch[0] if isinstance(batch, (list, tuple)) else batch
                        yield data
                def __len__(self):
                    return len(self._loader)

            train_args = (_WeightlessLoader(_orig_train_loader),)
            val_args = (_WeightlessLoader(_orig_val_loader),)
    else:
        train_args = (X_train, y_train)
        val_args = (X_val, y_val)

    # 训练循环 (直接调用 Qlib 模型的 train_epoch/test_epoch)
    best_score = -np.inf
    best_epoch = 0
    stop_steps = 0
    best_state = None
    evals: dict[str, list[float]] = {"train": [], "valid": []}

    logger.info("DL training: %d epochs, batch_size=%d, lr=%s", n_epochs, batch_size, lr)

    for epoch in range(n_epochs):
        model_obj.train_epoch(*train_args)

        # Evaluate — TS 模型 test_epoch 只返回 score，flat 模型返回 (loss, score)
        _val_out = model_obj.test_epoch(*val_args)
        if isinstance(_val_out, tuple):
            val_loss, val_score = _val_out
        else:
            val_loss, val_score = float("nan"), float(_val_out)

        train_score = float("nan")  # placeholder, not computed each epoch
        evals["train"].append(train_score)
        evals["valid"].append(val_score)

        if (epoch + 1) % 10 == 0 or epoch == 0:
            logger.info("Epoch %d/%d: valid=%.6f", epoch + 1, n_epochs, val_score)

        if val_score > best_score:
            best_score = val_score
            best_epoch = epoch
            stop_steps = 0
            # 保存最佳状态
            inner_model = None
            for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                              "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                              "tabnet_model", "transformer_model"):
                inner_model = getattr(model_obj, attr_name, None)
                if inner_model is not None:
                    break
            if inner_model is not None:
                best_state = copy.deepcopy(inner_model.state_dict())
        else:
            stop_steps += 1
            if stop_steps >= early_stop:
                logger.info("Early stop at epoch %d (best=%d, score=%.6f)", epoch, best_epoch, best_score)
                break

    # 恢复最佳模型
    if best_state is not None:
        inner_model = None
        for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                          "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                          "tabnet_model", "transformer_model"):
            inner_model = getattr(model_obj, attr_name, None)
            if inner_model is not None:
                break
        if inner_model is not None:
            inner_model.load_state_dict(best_state)

    # 保存模型（多模型模式必须带后缀：所有 DL 共用一个 output_dir，
    # 单写 model.pth 会互相覆盖，最后只剩最后一个模型的权重）
    torch.save(best_state, str(output_dir / f"model_{model_type}.pth"))
    logger.info("DL model saved: model_%s.pth (best_epoch=%d, best_score=%.6f)", model_type, best_epoch, best_score)

    # DL 元数据 (供推理重建模型)
    dl_metadata = {
        "model_type": model_type,
        "model_class_name": cls_name,
        "model_params": {k: v for k, v in model_params.items() if k not in ("GPU", "n_epochs", "lr", "batch_size", "early_stop", "metric")},
        "is_sequence_model": is_ts,
        "input_spec": {
            "tensor_shape": [None, step_len, d_feat] if is_ts else [None, d_feat],
            "feature_columns": features,
        },
        "dl_params": {k: v for k, v in dl_params.items()},
    }
    # feat_norm: TS 模型训练时用训练集 mean/std 标准化（验证集复用），
    # 推理必须用同一统计量，否则输入分布漂移导致预测不可信。
    if is_ts and _feat_norm is not None:
        _fm, _fs = _feat_norm
        dl_metadata["feat_norm"] = {
            "mean": [float(v) for v in _fm],
            "std": [float(v) for v in _fs],
        }

    # 计算指标：train/val 用模型预测算真实 rmse/auc（不再硬编码 NaN）
    # test 指标由 train_model 基于 test_df 计算。
    def _dl_metrics(frame: pd.DataFrame) -> dict:
        try:
            pred_df = _predict_dl(output_dir, frame, features, dl_metadata)
            frame_m = frame.copy()
            if "symbol" not in frame_m.columns or "trade_date" not in frame_m.columns:
                frame_m = frame.reset_index()
                if "instrument" in frame_m.columns:
                    frame_m["symbol"] = frame_m["instrument"]
                    frame_m["trade_date"] = frame_m["datetime"] if "datetime" in frame_m.columns else 0
            merged = frame_m.merge(pred_df[["symbol", "trade_date", "pred"]], on=["symbol", "trade_date"], how="left")
            if merged["pred"].notna().sum() < 10:
                logger.warning("DL metrics: 预测不足 10 行有效，rmse/auc 置 0")
                return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}
            y_true = merged["label"].astype("float32").to_numpy()
            y_pred = merged["pred"].fillna(0).astype("float32").to_numpy()
            return _compute_metrics(merged, y_true, y_pred)
        except Exception as exc:
            logger.warning("DL metrics 计算失败: %s", exc)
            return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}

    train_m = _dl_metrics(train_df)
    val_m = _dl_metrics(val_df)
    train_m.setdefault("rmse", 0.0); train_m.setdefault("auc", 0.0)
    val_m.setdefault("rmse", 0.0); val_m.setdefault("auc", 0.0)

    return model_obj, train_m, val_m, dl_metadata


def _train_nativetft(
    model_type: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    dl_params: dict[str, Any],
    output_dir: Path,
    hardware: dict[str, Any] | None = None,
) -> tuple:
    """NativeTFT (轻量 TFT 变体) 训练。

    架构: input_proj → GRU → MultiheadAttention → GRN → output
    非 Qlib 模型，使用自定义训练循环 + _build_ts_dataloader 构建 3D 窗口。
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    import copy

    d_feat = len(features)
    _dl_h = dl_params.get("dl_hidden_size", dl_params.get("hidden_size", 64))
    hidden_dim = int(_dl_h)
    _dl_nh = dl_params.get("dl_num_heads", dl_params.get("num_heads", 4))
    num_heads = max(1, int(_dl_nh))
    # 确保 hidden_dim 被 num_heads 整除
    hidden_dim = num_heads * (hidden_dim // num_heads)
    if hidden_dim < num_heads:
        hidden_dim = num_heads * 2
    dropout = float(dl_params.get("dl_dropout", dl_params.get("dropout", 0.2)))
    step_len = int(dl_params.get("dl_step_len", dl_params.get("step_len", 20)))
    n_epochs = int(dl_params.get("dl_n_epochs", dl_params.get("n_epochs", 200)))
    batch_size = int(dl_params.get("dl_batch_size", dl_params.get("batch_size", 4000)))
    lr = float(dl_params.get("dl_lr", dl_params.get("lr", 0.0005)))
    early_stop = int(dl_params.get("early_stopping_rounds", dl_params.get("early_stop", 20)))

    device = torch.device("cpu")
    if hardware and hardware.get("gpu_available"):
        device = torch.device("cuda:0")

    # ── 构建模型 ──
    class _GRN(nn.Module):
        def __init__(self, input_size, hidden_size, output_size, p_dropout):
            super().__init__()
            self.lin1 = nn.Linear(input_size, hidden_size)
            self.lin2 = nn.Linear(hidden_size, hidden_size)
            self.gate = nn.Linear(hidden_size, output_size)
            self.drop = nn.Dropout(p_dropout)
            self.norm = nn.LayerNorm(output_size)
            self.skip = nn.Linear(input_size, output_size) if input_size != output_size else nn.Identity()

        def forward(self, x):
            h = F.elu(self.lin1(x))
            h = self.lin2(h)
            h = self.drop(h)
            g = self.gate(h).sigmoid()
            return self.norm(self.skip(x) + g * h)

    class _NativeTFTNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_proj = nn.Linear(d_feat, hidden_dim)
            self.gru = nn.GRU(hidden_dim, hidden_dim, batch_first=True)
            self.attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=num_heads, batch_first=True)
            self.grn = _GRN(hidden_dim, hidden_dim, hidden_dim, dropout)
            self.output_layer = nn.Linear(hidden_dim, 1)

        def forward(self, x):
            h = self.input_proj(x)
            h_gru, _ = self.gru(h)
            attn_out, _ = self.attn(h_gru, h_gru, h_gru)
            h = h_gru + attn_out
            h = self.grn(h[:, -1, :])
            return self.output_layer(h).squeeze(-1)

    model = _NativeTFTNet().to(device)
    logger.info("NativeTFT: d_feat=%d, hidden=%d, heads=%d, step_len=%d, device=%s",
                d_feat, hidden_dim, num_heads, step_len, device)

    # ── 构建 DataLoader ──
    # TS 滑窗必须按 symbol 分组，否则窗口会跨越不同股票边界产生污染样本。
    # train_df/val_df 是扁平 RangeIndex，这里用 (symbol, trade_date) 建 MultiIndex
    # 供 _build_ts_dataloader 计算每只股票的连续区间 offset。
    # 训练集计算 mean/std 统计量，验证集复用（避免 look-ahead）；
    # feat_norm 持久化进 dl_metadata 供推理复现标准化。
    def _ts_indexed(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
        f = frame.sort_values(["symbol", "trade_date"])
        idx = pd.MultiIndex.from_arrays(
            [f["symbol"].to_numpy(), f["trade_date"].to_numpy()],
            names=["instrument", "datetime"],
        )
        return f[features].set_axis(idx), f["label"].set_axis(idx)

    _xt, _yt = _ts_indexed(train_df)
    _xv, _yv = _ts_indexed(val_df)
    train_loader, _feat_norm = _build_ts_dataloader(
        _xt, _yt, step_len, batch_size, shuffle=True)
    val_loader, _ = _build_ts_dataloader(
        _xv, _yv, step_len, batch_size, shuffle=False,
        feat_norm=_feat_norm)

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()

    # ── 训练循环 ──
    best_score = -np.inf
    best_epoch = 0
    stop_steps = 0
    best_state = None
    evals: dict[str, list[float]] = {"train": [], "valid": []}

    logger.info("NativeTFT training: %d epochs, batch_size=%d, lr=%s", n_epochs, batch_size, lr)

    for epoch in range(n_epochs):
        # Train
        model.train()
        epoch_loss = 0.0
        n_batches = 0
        for batch in train_loader:
            data = batch[0] if isinstance(batch, (list, tuple)) else batch
            feature = data[:, :, 0:-1].float().to(device)
            label = batch[1].float().to(device) if isinstance(batch, (list, tuple)) and len(batch) > 1 else None
            if label is None:
                label = data[:, -1, -1].float().to(device)

            optimizer.zero_grad()
            pred = model(feature)
            loss = loss_fn(pred, label)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            n_batches += 1

        # Evaluate
        model.eval()
        val_preds = []
        val_labels = []
        with torch.no_grad():
            for batch in val_loader:
                data = batch[0] if isinstance(batch, (list, tuple)) else batch
                feature = data[:, :, 0:-1].float().to(device)
                label = batch[1].float().to(device) if isinstance(batch, (list, tuple)) and len(batch) > 1 else None
                if label is None:
                    label = data[:, -1, -1].float().to(device)
                pred = model(feature)
                val_preds.append(pred.cpu().numpy())
                val_labels.append(label.cpu().numpy())

        val_pred_arr = np.concatenate(val_preds)
        val_label_arr = np.concatenate(val_labels)
        # IC (Pearson correlation) as validation metric
        val_score = float(np.corrcoef(val_pred_arr, val_label_arr)[0, 1]) if len(val_pred_arr) > 1 else 0.0

        evals["train"].append(float("nan"))
        evals["valid"].append(val_score)

        if (epoch + 1) % 10 == 0 or epoch == 0:
            logger.info("Epoch %d/%d: valid_ic=%.6f, loss=%.6f", epoch + 1, n_epochs, val_score,
                        epoch_loss / max(1, n_batches))

        if val_score > best_score:
            best_score = val_score
            best_epoch = epoch
            stop_steps = 0
            best_state = copy.deepcopy(model.state_dict())
        else:
            stop_steps += 1
            if stop_steps >= early_stop:
                logger.info("Early stopping at epoch %d (best=%d, score=%.6f)", epoch + 1, best_epoch, best_score)
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    else:
        logger.warning("NativeTFT best_state 为 None（可能 val_score 全 NaN），使用最终权重")
        best_state = copy.deepcopy(model.state_dict())

    # 保存模型（多模型模式带后缀，避免覆盖同目录其他 DL 权重）
    torch.save(best_state, str(output_dir / f"model_{model_type}.pth"))
    logger.info("NativeTFT saved: model_%s.pth (best_epoch=%d, best_ic=%.6f)", model_type, best_epoch, best_score)

    dl_metadata = {
        "model_type": "NativeTFT",
        "model_arch": {
            "input_dim": d_feat,
            "hidden_dim": hidden_dim,
            "num_heads": num_heads,
            "dropout": dropout,
        },
        "is_sequence_model": True,
        "input_spec": {
            "tensor_shape": [None, step_len, d_feat],
            "feature_columns": features,
        },
        "dl_params": {k: v for k, v in dl_params.items()},
    }
    # feat_norm: 训练集 mean/std（验证集已复用），推理必须用同一统计量
    if _feat_norm is not None:
        _fm, _fs = _feat_norm
        dl_metadata["feat_norm"] = {
            "mean": [float(v) for v in _fm],
            "std": [float(v) for v in _fs],
        }

    # 计算真实 train/val 指标（不再硬编码 NaN rmse/auc）
    def _tft_metrics(frame: pd.DataFrame) -> dict:
        try:
            pred_df = _predict_nativetft(output_dir, frame, features, dl_metadata)
            frame_m = frame.copy()
            if "symbol" not in frame_m.columns or "trade_date" not in frame_m.columns:
                frame_m = frame.reset_index()
                if "instrument" in frame_m.columns:
                    frame_m["symbol"] = frame_m["instrument"]
                    frame_m["trade_date"] = frame_m["datetime"] if "datetime" in frame_m.columns else 0
            merged = frame_m.merge(pred_df[["symbol", "trade_date", "pred"]], on=["symbol", "trade_date"], how="left")
            if merged["pred"].notna().sum() < 10:
                return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}
            y_true = merged["label"].astype("float32").to_numpy()
            y_pred = merged["pred"].fillna(0).astype("float32").to_numpy()
            return _compute_metrics(merged, y_true, y_pred)
        except Exception as exc:
            logger.warning("NativeTFT metrics 计算失败: %s", exc)
            return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"), "rmse": 0.0, "auc": 0.0}

    train_m = _tft_metrics(train_df)
    val_m = _tft_metrics(val_df)
    train_m.setdefault("rmse", 0.0); train_m.setdefault("auc", 0.0)
    val_m.setdefault("rmse", 0.0); val_m.setdefault("auc", 0.0)

    return model, train_m, val_m, dl_metadata


def _predict_dl(
    model_dir: Path,
    df_X: pd.DataFrame,
    features: list[str],
    dl_metadata: dict[str, Any],
    batch_size: int = 8000,
) -> np.ndarray:
    """加载训练好的 DL 模型并预测。"""
    import importlib
    import torch

    cls_name = dl_metadata.get("model_class_name", "")
    model_params = dl_metadata.get("model_params", {})
    is_ts = dl_metadata.get("is_sequence_model", False)

    # 找到对应的 Qlib 模型类
    model_cls = None
    for _map in (_QLIB_TS_MODEL_MAP, _QLIB_FLAT_MODEL_MAP):
        for _mt, (_mod_path, _cls_name) in _map.items():
            if _cls_name == cls_name:
                mod = importlib.import_module(_mod_path)
                model_cls = getattr(mod, _cls_name)
                break
        if model_cls is not None:
            break

    if model_cls is None:
        raise ValueError(f"Cannot find Qlib model class: {cls_name}")

    infer_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    logger.info("DL inference device: %s", infer_device)
    # 推理设备跟随可用算力：有 GPU 就用 cuda（本路径不调用 qlib 高层 API，
    # GPU 参数仅保持语义一致，实际前向由下方手写循环驱动）
    model_params["GPU"] = 0 if torch.cuda.is_available() else -1
    model_obj = model_cls(**model_params)

    # 加载权重（多模型模式保存为 model_{type}.pth；旧模型兼容回退 model.pth）
    _mt = str(dl_metadata.get("model_type") or "").lower()
    if _mt:
        model_path = model_dir / f"model_{_mt}.pth"
        if not model_path.exists():
            model_path = model_dir / "model.pth"
    else:
        model_path = model_dir / "model.pth"
    if not model_path.exists():
        raise FileNotFoundError(f"model.pth not found at {model_path}")

    state_dict = torch.load(str(model_path), map_location="cpu")
    inner_model = None
    for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                      "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                      "tabnet_model", "transformer_model"):
        inner_model = getattr(model_obj, attr_name, None)
        if inner_model is not None:
            break
    if inner_model is not None:
        inner_model.load_state_dict(state_dict)
    model_obj.fitted = True

    # 预测
    if is_ts:
        step_len = dl_metadata.get("dl_params", {}).get("dl_step_len", 20)
        # 复用训练集标准化统计量（推理输入必须与训练同分布）
        feat_norm = None
        _fn = dl_metadata.get("feat_norm")
        if isinstance(_fn, dict) and _fn.get("mean") and _fn.get("std"):
            feat_norm = (np.asarray(_fn["mean"], dtype=np.float32),
                         np.asarray(_fn["std"], dtype=np.float32))
        # TS 滑窗必须按 symbol 分组：df_X 是扁平 RangeIndex，
        # 用 (symbol, trade_date) 建 MultiIndex 供 loader 计算股票连续区间。
        _pred_df = df_X.copy()
        if "symbol" in _pred_df.columns and "trade_date" in _pred_df.columns:
            _pred_df = _pred_df.sort_values(["symbol", "trade_date"])
            _idx = pd.MultiIndex.from_arrays(
                [_pred_df["symbol"].to_numpy(), _pred_df["trade_date"].to_numpy()],
                names=["instrument", "datetime"],
            )
            _X = _pred_df[features].set_axis(_idx)
            _y = pd.Series(0.0, index=_idx)
        else:
            _X = df_X[features]
            _y = pd.Series(0.0, index=df_X.index)
        loader, _ = _build_ts_dataloader(_X, _y, step_len, batch_size, shuffle=False, feat_norm=feat_norm, drop_last=False)
        # 找到内部模型
        inner_model = None
        for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                          "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                          "tabnet_model", "transformer_model"):
            inner_model = getattr(model_obj, attr_name, None)
            if inner_model is not None:
                break
        if inner_model is not None:
            inner_model.eval()
            inner_model = inner_model.to(infer_device)
        preds = []
        is_tcn = "TCN" in cls_name
        for batch in loader:
            data = batch[0] if isinstance(batch, (list, tuple)) else batch
            feature = data[:, :, 0:-1].to(infer_device)
            # TCN 期望 channels-first [batch, d_feat, seq]（训练时 qlib 内部 transpose，
            # 推理需手动补）；GRU/LSTM/ALSTM batch_first，Transformer 内部自处理。
            if is_tcn:
                feature = feature.transpose(1, 2)
            with torch.no_grad():
                pred = inner_model(feature.float())
                if isinstance(pred, tuple):
                    pred = pred[0]
                if hasattr(pred, 'dim') and pred.dim() == 2 and pred.shape[1] == 1:
                    pred = pred.squeeze(-1)
                preds.append(pred.detach().cpu().numpy())
        raw_pred = np.concatenate(preds)

        # 时序滑窗预测天然比全量行少（每只股票前 step_len-1 行无完整窗口）。
        # 用 dataset.original_rows（排序索引→原始 df_X 行）把预测 scatter
        # 回原始行的窗口末端位置，其余填 NaN，返回 DataFrame(symbol,trade_date,pred)。
        n_total = len(_pred_df)
        out = np.full(n_total, np.nan, dtype=np.float32)
        if raw_pred.shape[0] > 0 and hasattr(loader.dataset, "original_rows"):
            ds = loader.dataset
            orig = ds.original_rows[ds.indices] + step_len - 1
            if orig.max() < n_total and len(orig) == raw_pred.shape[0]:
                out[orig] = raw_pred
            else:
                logger.warning("DL predict scatter 不匹配: pred=%d orig=%d n_total=%d -> 全 NaN",
                               raw_pred.shape[0], len(orig), n_total)
        else:
            logger.warning("DL predict 无 original_rows 或无预测: pred=%d", raw_pred.shape[0])
        return pd.DataFrame({
            "symbol": _pred_df["symbol"].to_numpy(),
            "trade_date": _pred_df["trade_date"].to_numpy(),
            "pred": out,
        })
    else:
        X_values = df_X[features].values.astype(np.float32)
        # 填充 NaN/Inf：tabnet 内部断言禁止 NaN 输入
        if np.isnan(X_values).any() or np.isinf(X_values).any():
            logger.warning("DL flat predict: 特征含 %d NaN/%d Inf，填 0",
                           int(np.isnan(X_values).sum()), int(np.isinf(X_values).sum()))
            X_values = np.nan_to_num(X_values, nan=0.0, posinf=0.0, neginf=0.0)
        X_tensor = torch.from_numpy(X_values).to(infer_device)
        inner_model = None
        for attr_name in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                          "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                          "tabnet_model", "transformer_model"):
            inner_model = getattr(model_obj, attr_name, None)
            if inner_model is not None:
                break
        if inner_model is not None:
            inner_model.eval()
            inner_model = inner_model.to(infer_device)
        is_tabnet = "Tabnet" in cls_name
        preds = []
        for i in range(0, len(X_tensor), batch_size):
            batch = X_tensor[i:i+batch_size]
            with torch.no_grad():
                if is_tabnet:
                    # qlib TabNet.forward(x, priors) 需要 priors 参数（训练时 qlib 内部构造）
                    priors = torch.ones(batch.shape[0], batch.shape[1], dtype=batch.dtype, device=infer_device)
                    pred = inner_model(batch.float(), priors)
                else:
                    pred = inner_model(batch.float())
                if isinstance(pred, tuple):
                    pred = pred[0]
                if hasattr(pred, 'dim') and pred.dim() == 2 and pred.shape[1] == 1:
                    pred = pred.squeeze(-1)
                preds.append(pred.detach().cpu().numpy())
        _flat_pred = np.concatenate(preds)
        return pd.DataFrame({
            "symbol": df_X["symbol"].to_numpy(),
            "trade_date": df_X["trade_date"].to_numpy(),
            "pred": _flat_pred,
        })


def _predict_nativetft(
    model_dir: Path,
    df_X: pd.DataFrame,
    features: list[str],
    dl_metadata: dict[str, Any],
    batch_size: int = 8000,
) -> np.ndarray:
    """加载训练好的 NativeTFT 模型并预测。"""
    import torch

    model_arch = dl_metadata.get("model_arch", {})
    input_dim = int(model_arch.get("input_dim", len(features)))
    hidden_dim = int(model_arch.get("hidden_dim", 64))
    num_heads = int(model_arch.get("num_heads", 4))
    dropout = float(model_arch.get("dropout", 0.1))
    step_len = int(dl_metadata.get("dl_params", {}).get("dl_step_len", 20))

    # 重建模型架构 (与 _train_nativetft 中一致)
    import torch.nn as nn
    import torch.nn.functional as F

    class _GRN(nn.Module):
        def __init__(self, input_size, hidden_size, output_size, p_dropout):
            super().__init__()
            self.lin1 = nn.Linear(input_size, hidden_size)
            self.lin2 = nn.Linear(hidden_size, hidden_size)
            self.gate = nn.Linear(hidden_size, output_size)
            self.drop = nn.Dropout(p_dropout)
            self.norm = nn.LayerNorm(output_size)
            self.skip = nn.Linear(input_size, output_size) if input_size != output_size else nn.Identity()

        def forward(self, x):
            h = F.elu(self.lin1(x))
            h = self.lin2(h)
            h = self.drop(h)
            g = self.gate(h).sigmoid()
            return self.norm(self.skip(x) + g * h)

    class _NativeTFTNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_proj = nn.Linear(input_dim, hidden_dim)
            self.gru = nn.GRU(hidden_dim, hidden_dim, batch_first=True)
            self.attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=num_heads, batch_first=True)
            self.grn = _GRN(hidden_dim, hidden_dim, hidden_dim, dropout)
            self.output_layer = nn.Linear(hidden_dim, 1)

        def forward(self, x):
            h = self.input_proj(x)
            h_gru, _ = self.gru(h)
            attn_out, _ = self.attn(h_gru, h_gru, h_gru)
            h = h_gru + attn_out
            h = self.grn(h[:, -1, :])
            return self.output_layer(h).squeeze(-1)

    infer_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    logger.info("NativeTFT inference device: %s", infer_device)
    model = _NativeTFTNet().to(infer_device)
    # 多模型模式保存为 model_nativetft.pth（兼容旧单模型 model.pth）
    model_path = model_dir / "model_nativetft.pth"
    if not model_path.exists():
        model_path = model_dir / "model.pth"
    if not model_path.exists():
        raise FileNotFoundError(f"model.pth not found at {model_path}")
    state_dict = torch.load(str(model_path), map_location=infer_device)
    model.load_state_dict(state_dict)
    model.eval()

    # 复用训练集标准化统计量（推理输入必须与训练同分布）
    feat_norm = None
    _fn = dl_metadata.get("feat_norm")
    if isinstance(_fn, dict) and _fn.get("mean") and _fn.get("std"):
        feat_norm = (np.asarray(_fn["mean"], dtype=np.float32),
                     np.asarray(_fn["std"], dtype=np.float32))
    # TS 滑窗必须按 symbol 分组，否则窗口跨股票边界产生污染样本
    _pred_df = df_X.copy()
    if "symbol" in _pred_df.columns and "trade_date" in _pred_df.columns:
        _pred_df = _pred_df.sort_values(["symbol", "trade_date"])
        _idx = pd.MultiIndex.from_arrays(
            [_pred_df["symbol"].to_numpy(), _pred_df["trade_date"].to_numpy()],
            names=["instrument", "datetime"],
        )
        _X = _pred_df[features].set_axis(_idx)
        _y = pd.Series(0.0, index=_idx)
    else:
        _X = df_X[features]
        _y = pd.Series(0.0, index=df_X.index)

    # 构建 TS DataLoader 并预测
    loader, _ = _build_ts_dataloader(
        _X, _y, step_len, batch_size, shuffle=False, feat_norm=feat_norm, drop_last=False)

    preds = []
    with torch.no_grad():
        for batch in loader:
            data = batch[0] if isinstance(batch, (list, tuple)) else batch
            feature = data[:, :, 0:-1].float().to(infer_device)
            pred = model(feature)
            preds.append(pred.cpu().numpy())
    raw_pred = np.concatenate(preds)

    # 与 _predict_dl 相同的 scatter 逻辑：用 original_rows 映射回原始行
    n_total = len(_pred_df)
    out = np.full(n_total, np.nan, dtype=np.float32)
    if raw_pred.shape[0] > 0 and hasattr(loader.dataset, "original_rows"):
        ds = loader.dataset
        orig = ds.original_rows[ds.indices] + step_len - 1
        if orig.max() < n_total and len(orig) == raw_pred.shape[0]:
            out[orig] = raw_pred
    return pd.DataFrame({
        "symbol": _pred_df["symbol"].to_numpy(),
        "trade_date": _pred_df["trade_date"].to_numpy(),
        "pred": out,
    })


def _predict_with_model(model: Any, X: np.ndarray, model_type: str, features: list[str] | None = None) -> np.ndarray:
    """统一预测接口，适配不同框架。"""
    if model_type == "lightgbm":
        return model.predict(X, num_iteration=model.best_iteration)
    elif model_type == "xgboost":
        import xgboost as xgb
        dmat = xgb.DMatrix(X, feature_names=features)
        n_iter = model.best_iteration
        return model.predict(dmat, iteration_range=(0, (n_iter + 1) if n_iter is not None else 0))
    elif model_type == "catboost":
        pred = model.predict_proba(X) if hasattr(model, "predict_proba") else model.predict(X)
    else:
        pred = model.predict_proba(X) if hasattr(model, "predict_proba") else model.predict(X)

    # sklearn/CatBoost 分类器默认 predict() 返回硬标签；选股排序与 AUC 均应使用
    # 正类概率，避免把大量样本压成 0/1 并丢失排序信息。
    pred_arr = np.asarray(pred)
    if pred_arr.ndim == 2 and pred_arr.shape[1] >= 2:
        return pred_arr[:, 1]
    return pred_arr.reshape(-1)


def _save_model(model: Any, model_type: str, out_dir: Path) -> str:
    """保存模型到文件，返回实际文件名。"""
    if model_type == "lightgbm":
        path = out_dir / "model.lgb"
        model.save_model(str(path))
        return "model.lgb"
    elif model_type == "xgboost":
        path = out_dir / "model.xgb"
        model.save_model(str(path))
        return "model.xgb"
    elif model_type == "catboost":
        path = out_dir / "model.cbm"
        model.save_model(str(path), format="cbm")
        return "model.cbm"
    elif model_type == "linear":
        import pickle
        path = out_dir / "model.pkl"
        with open(path, "wb") as f:
            pickle.dump(model, f)
        return "model.pkl"
    elif model_type in _DL_MODEL_TYPES:
        # MLP 用 sklearn 实现（存 pkl）；其余 DL 模型在 _train_dl() 中已保存 model.pth
        if model_type == "mlp":
            import pickle
            path = out_dir / "model.pkl"
            with open(path, "wb") as f:
                pickle.dump(model, f)
            return "model.pkl"
        # hybrid_gru_tree 由专用训练管线保存 gru_encoder.pth + 树模型文件
        if model_type == "hybrid_gru_tree":
            return "model.pkl"
        # DL 模型在 _train_dl() 中已保存 model_{type}.pth，此处仅返回文件名，
        # 4235 行统一改名循环发现同名时跳过（不再覆盖/错配权重）
        return f"model_{model_type}.pth"
    else:
        import pickle
        path = out_dir / "model.pkl"
        with open(path, "wb") as f:
            pickle.dump(model, f)
        return "model.pkl"


def _get_model_framework(model_type: str) -> str:
    """返回模型框架名。"""
    mapping = {
        "lightgbm": "lightgbm",
        "xgboost": "xgboost",
        "catboost": "catboost",
        "linear": "sklearn",
        "random_forest": "sklearn",
        "gru": "pytorch",
        "lstm": "pytorch",
        "alstm": "pytorch",
        "transformer": "pytorch",
        "tra": "pytorch",
        "hist": "pytorch",
        "tabnet": "pytorch",
        "tcn": "pytorch",
        "nativetft": "pytorch",
        "mlp": "pytorch",
        "hybrid_gru_tree": "pytorch",
    }
    return mapping.get(model_type, "unknown")


def train_model(df: pd.DataFrame, features: list[str], cfg: dict, hardware: dict | None = None,
                need_full_pred: bool = True) -> tuple:
    """统一训练入口：根据 model_type 路由到对应训练函数。"""
    model_cfg = cfg.get("model", {})
    _optuna_result = None
    model_type = str(model_cfg.get("type", "lightgbm")).strip().lower()

    if model_type not in _ALL_MODEL_TYPES:
        raise ValueError(f"Unsupported model_type: {model_type}")
    _validate_quantile_config(cfg, model_type)

    # 检查深度学习模型是否有 GPU
    if model_type in _DL_MODEL_TYPES and hardware and not hardware.get("gpu_available"):
        logger.warning("DL model '%s' requested but no GPU detected. Training will be slow on CPU.", model_type)

    # 数据切分
    train_df, val_df, test_df = _split_data(df, cfg)
    fill_values, X_train, y_train, X_val, y_val, _fill = _prepare_arrays(
        train_df, val_df, features, prep_cfg=cfg.get("preprocessing") or {}
    )

    # 路由到对应训练函数
    logger.info("Training model: %s (framework=%s)", model_type, _get_model_framework(model_type))
    train_t0 = time.time()

    if model_type in ("lightgbm", "xgboost", "catboost"):
        # Optuna 自动超参搜索：显式 optuna.enabled=true 时，先搜索最优参数再训练。
        # _train_single_model 的 OOF fold（need_full_pred=False）不触发，避免重复搜索。
        _optuna_cfg = cfg.get("optuna", {}) or {}
        if _optuna_cfg.get("enabled") and need_full_pred:
            _optuna_result = _tune_tree_hyperparams(
                cfg, model_type, features, X_train, y_train, X_val, y_val, val_df
            )
            if _optuna_result and _optuna_result.get("best_params"):
                # 将最优参数合并进模型参数后重新训练
                _best = _optuna_result["best_params"]
                _merge_cfg = dict(cfg)
                _model_cfg = dict(cfg.get("model", {}))
                if model_type == "lightgbm":
                    _model_cfg["params"] = {**(_model_cfg.get("params") or {}), **_best}
                elif model_type == "xgboost":
                    _model_cfg["xgb_params"] = {**(_model_cfg.get("xgb_params") or {}), **_best}
                elif model_type == "catboost":
                    _model_cfg["catboost_params"] = {**(_model_cfg.get("catboost_params") or {}), **_best}
                _merge_cfg["model"] = _model_cfg
                cfg = _merge_cfg
        quantile_result: dict[str, Any] | None = None
        if model_type == "lightgbm" and _quantile_mode_enabled(cfg):
            quantile_models, calibration = _train_lgb_quantiles(
                cfg, features, X_train, y_train, X_val, y_val, val_df["trade_date"]
            )
            # P50 intentionally remains the platform score used by ranking/trading.
            model = quantile_models["p50"]
            quantile_result = {"models": quantile_models, "calibration": calibration}
        elif model_type == "lightgbm":
            model = _train_lgb(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "xgboost":
            model = _train_xgb(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "catboost":
            model = _train_catboost(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "linear":
        model = _train_linear(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "random_forest":
        model = _train_rf(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "mlp":
        model = _train_mlp(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "nativetft":
        dl_params = model_cfg.get("dl_params", {})
        output_dir = WORKSPACE_DIR
        model, train_m, val_m, dl_metadata = _train_nativetft(
            model_type, train_df, val_df, features, dl_params, output_dir, hardware=hardware
        )
        train_elapsed = time.time() - train_t0
        logger.info("Training finished in %.2fs (%s)", train_elapsed, model_type)
        logger.info(f"Val IC={val_m['ic']:.4f}")

        y_full_pred = _predict_nativetft(output_dir, df, features, dl_metadata)
        full_pred_df = df[["symbol", "trade_date", "label"]].copy()
        # 时序 DL 预测返回 DataFrame(symbol,trade_date,pred)，按 key 对齐合并
        full_pred_df = full_pred_df.merge(
            y_full_pred[["symbol", "trade_date", "pred"]],
            on=["symbol", "trade_date"], how="left",
        )
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

        test_mask = full_pred_df["split"] == "test"
        y_test_pred = full_pred_df.loc[test_mask, "pred"].values
        y_test_true = full_pred_df.loc[test_mask, "label"].values
        test_m = _compute_metrics(test_df, y_test_true.astype("float32"), y_test_pred.astype("float32"))

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
            model_type,
            dl_metadata,
        )
    elif model_type in _DL_MODEL_TYPES:
        dl_params = model_cfg.get("dl_params", {})
        output_dir = WORKSPACE_DIR
        model, train_m, val_m, dl_metadata = _train_dl(
            model_type, train_df, val_df, features, dl_params, output_dir, hardware=hardware
        )
        train_elapsed = time.time() - train_t0
        logger.info("Training finished in %.2fs (%s)", train_elapsed, model_type)
        logger.info(f"Val IC={val_m['ic']:.4f}")

        # DL 模型生成全窗口预测
        y_full_pred = _predict_dl(output_dir, df, features, dl_metadata)
        full_pred_df = df[["symbol", "trade_date", "label"]].copy()
        # 时序 DL 预测返回 DataFrame(symbol,trade_date,pred)，按 key 对齐合并
        full_pred_df = full_pred_df.merge(
            y_full_pred[["symbol", "trade_date", "pred"]],
            on=["symbol", "trade_date"], how="left",
        )
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

        # 计算 test 集指标
        test_mask = full_pred_df["split"] == "test"
        y_test_pred = full_pred_df.loc[test_mask, "pred"].values
        y_test_true = full_pred_df.loc[test_mask, "label"].values
        test_m = _compute_metrics(test_df, y_test_true.astype("float32"), y_test_pred.astype("float32"))

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
            model_type,
            dl_metadata,
        )
    else:
        raise ValueError(f"Unsupported model_type: {model_type}")

    train_elapsed = time.time() - train_t0
    logger.info("Training finished in %.2fs (%s)", train_elapsed, model_type)

    # 统一预测 (树模型)
    y_train_pred = _predict_with_model(model, _fill(train_df), model_type, features)
    y_val_pred = _predict_with_model(model, _fill(val_df), model_type, features)
    y_test_pred = _predict_with_model(model, _fill(test_df), model_type, features)
    train_m = _compute_metrics(train_df, y_train, y_train_pred)
    val_m   = _compute_metrics(val_df,   y_val,   y_val_pred)
    test_m  = _compute_metrics(test_df,  test_df["label"].astype("float32").to_numpy(), y_test_pred)

    logger.info(f"Train IC={train_m['ic']:.4f}  RankIC={train_m['rank_ic']:.4f}")
    logger.info(f"Val   IC={val_m['ic']:.4f}    RankIC={val_m['rank_ic']:.4f}  ICIR={val_m['rank_icir']:.4f}")

    # 生成全窗口预测
    full_pred_df = df[["symbol", "trade_date", "label"]].copy()
    full_pred_df["pred"] = _predict_with_model(model, _fill(df), model_type, features)
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
        model_type,
        _optuna_result,
        quantile_result if model_type == "lightgbm" and _quantile_mode_enabled(cfg) else None,
    )


# ── 因子筛选 ────────────────────────────────────────────────────────────────────
def select_top_factors(
    df: pd.DataFrame,
    features: list[str],
    label_col: str = "label",
    n_top: int = 80,
    ic_threshold: float = 0.01,
    icir_threshold: float = 0.15,
    correlation_threshold: float = 0.9,
) -> tuple[list[str], dict[str, Any]]:
    """专业因子筛选：IC/ICIR 初筛 → 相关性去冗余 → 稳定性检验。

    返回 (selected, report)：
    - selected: 入选特征列表
    - report: 结构化筛选报告（每特征的 IC/ICIR/覆盖率/入选或淘汰原因），
      写入 result metadata，供前端展示"为什么选/为什么不选"。

    report 结构:
        {
          "method": "ic_icir",
          "thresholds": {"n_top", "ic_threshold", "icir_threshold", "correlation_threshold"},
          "stage_counts": {"input", "ic_pass", "corr_pass", "stable", "selected"},
          "train_rows": int,
          "features": [
            {"name", "ic", "icir", "ic_positive_rate", "n_days", "coverage", "status", "reason"}
          ],
        }
    """
    logger.info("=== Factor Selection: IC/ICIR screening ===")
    logger.info("Input: %d features, target top-%d", len(features), n_top)

    # 覆盖率：训练段特征非空比例（与 NaN 数据洞直接挂钩，如 L2 vpin 系 2024Q4-2025Q1 缺失）
    coverage_map: dict[str, float] = {}
    present_cols = [f for f in features if f in df.columns]
    if present_cols:
        try:
            cov_arr = df[present_cols].notna().mean(axis=0)
            coverage_map = {f: float(cov_arr[f]) for f in present_cols}
        except Exception:
            coverage_map = {f: 1.0 for f in present_cols}

    # Step 1: 日频 Rank IC 计算（原始 spearmanr 算法，数值 100% 正确）
    # 多进程并行：按特征分片给多个进程同时算（fork，DataFrame 零拷贝共享），
    # 默认 min(CPU 核数, 特征数) 个 worker，TRAIN_IC_WORKERS 环境变量可覆盖。
    # 算法、逐日循环、dropna 规则与旧串行实现完全一致——只加速不近似。
    t0_sel = time.time()
    ic_results: dict[str, dict] = {}
    try:
        from parallel_utils import compute_daily_ics

        ic_results = compute_daily_ics(df, features, label_col=label_col)
        logger.info(
            "IC/ICIR screening done in %.1fs (%d features)",
            time.time() - t0_sel, len(ic_results),
        )
    except ImportError:
        # parallel_utils 未随 train.py 同步（旧镜像/旧编排器）：回退串行，不中断训练
        logger.warning("parallel_utils not found, falling back to serial IC computation")
        from scipy.stats import spearmanr

        for feat in features:
            if feat not in df.columns:
                continue
            daily_ics = []
            for _, g in df.groupby("trade_date", sort=False):
                valid = g[[feat, label_col]].dropna()
                if len(valid) < 30:
                    continue
                ic, _ = spearmanr(valid[feat], valid[label_col])
                if np.isfinite(ic):
                    daily_ics.append(ic)
            if len(daily_ics) < 20:
                ic_results[feat] = {"ic_mean": 0.0, "icir": 0.0, "ic_positive_rate": 0.0, "n_days": len(daily_ics)}
                continue
            arr = np.array(daily_ics)
            ic_results[feat] = {
                "ic_mean": float(np.mean(arr)),
                "icir": float(np.mean(arr) / (np.std(arr) + 1e-9)),
                "ic_positive_rate": float(np.mean(arr > 0)),
                "n_days": len(arr),
                "daily_ics": daily_ics,  # 供 Step 4 稳定性检验复用，避免二次计算
            }
        logger.info("IC/ICIR screening done in %.1fs (%d features)", time.time() - t0_sel, len(ic_results))

    # 逐特征决策原因（status: selected / rejected + reason 说明被哪个门槛淘汰）
    decisions: dict[str, str] = {}
    for feat in features:
        if feat not in ic_results:
            decisions[feat] = "特征不在训练数据中"
        elif int(ic_results[feat].get("n_days") or 0) < 20:
            decisions[feat] = "IC 有效样本天数不足(<20日)"
        else:
            decisions[feat] = ""  # 进入阈值判定

    # Step 2: IC阈值初筛
    candidates = {}
    for f, r in ic_results.items():
        if f not in decisions or decisions[f]:
            continue
        if abs(r["ic_mean"]) >= ic_threshold and abs(r["icir"]) >= icir_threshold:
            candidates[f] = r
        elif abs(r["ic_mean"]) < ic_threshold:
            decisions[f] = f"|IC|={abs(r['ic_mean']):.4f} < 阈值 {ic_threshold}"
        else:
            decisions[f] = f"|ICIR|={abs(r['icir']):.3f} < 阈值 {icir_threshold}"
    logger.info("After IC/ICIR threshold: %d candidates (|IC|>=%.2f, |ICIR|>=%.1f)",
                len(candidates), ic_threshold, icir_threshold)

    # Step 3: ICIR 排序 + 贪心去冗余
    sorted_features = sorted(candidates.keys(),
        key=lambda f: abs(candidates[f]["icir"]), reverse=True)

    selected: list[str] = []
    for feat in sorted_features:
        if len(selected) >= n_top:
            decisions[feat] = "超出 top-N 名额"
            continue
        if len(selected) == 0:
            selected.append(feat)
            continue
        # 抽样计算相关性（全量可能 OOM）
        sample_n = min(50000, len(df))
        corr_df = df[selected + [feat]].sample(sample_n, random_state=42).corr()
        max_corr = corr_df[feat].drop(feat).abs().max()
        if max_corr < correlation_threshold:
            selected.append(feat)
        else:
            decisions[feat] = f"与已选特征相关性 {max_corr:.3f} >= {correlation_threshold}"

    logger.info("After correlation pruning (thresh=%.2f): %d selected",
                correlation_threshold, len(selected))

    # Step 4: 稳定性检验（复用 Step 1 已算的逐日 IC 序列，避免二次双重循环）
    # 滚动60日 IC 标准差 / 均值 → 稳定性比率
    stable = []
    for feat in selected:
        daily_series = ic_results[feat].get("daily_ics")
        if not daily_series or len(daily_series) < 20:
            continue
        daily_ics = np.asarray(daily_series, dtype=np.float64)
        rolling_std = pd.Series(daily_ics).rolling(60, min_periods=20).std()
        mean_ic = abs(float(np.mean(daily_ics)))
        if mean_ic > 0 and rolling_std.mean() / (mean_ic + 1e-9) < 2.0:
            stable.append(feat)
        else:
            decisions[feat] = "IC 稳定性不足(滚动60日波动 > 2×|IC均值|)"

    if len(stable) >= 30:
        selected = stable[:n_top]
        logger.info("After stability filter: %d stable factors", len(selected))
    else:
        # 稳定因子不足 30 个时保留相关性去冗余后的名单（老行为），
        # 并把被稳定性淘汰的决策回收（它们仍入选）
        for feat in selected:
            decisions.pop(feat, None)

    # 输出 top-10 供日志
    for i, feat in enumerate(selected[:10]):
        r = ic_results[feat]
        logger.info("  %2d. %-30s IC=%.4f  ICIR=%.3f  IC>0=%.1f%%",
                    i + 1, feat, r["ic_mean"], r["icir"], r["ic_positive_rate"] * 100)

    # ── 组装结构化筛选报告 ──
    selected_set = set(selected)
    report_features = []
    for feat in features:
        r = ic_results.get(feat)
        if r is None:
            report_features.append({
                "name": feat, "ic": None, "icir": None, "ic_positive_rate": None,
                "n_days": 0, "coverage": round(coverage_map.get(feat, 1.0), 4),
                "status": "rejected", "reason": decisions.get(feat, "特征不在训练数据中"),
            })
            continue
        report_features.append({
            "name": feat,
            "ic": round(float(r.get("ic_mean", 0.0)), 4),
            "icir": round(float(r.get("icir", 0.0)), 3),
            "ic_positive_rate": round(float(r.get("ic_positive_rate", 0.0)), 4),
            "n_days": int(r.get("n_days", 0)),
            "coverage": round(coverage_map.get(feat, 1.0), 4),
            "status": "selected" if feat in selected_set else "rejected",
            "reason": "通过全部筛选" if feat in selected_set else (decisions.get(feat) or "未通过筛选"),
        })
    report_features.sort(key=lambda x: (x["status"] != "selected", -(abs(x["icir"] or 0))))
    report = {
        "method": "ic_icir",
        "thresholds": {
            "n_top": n_top,
            "ic_threshold": ic_threshold,
            "icir_threshold": icir_threshold,
            "correlation_threshold": correlation_threshold,
        },
        "stage_counts": {
            "input": len(features),
            "ic_pass": len(candidates),
            "corr_pass": len(selected),
            "stable": len(stable) if len(stable) >= 30 else len(selected),
            "selected": len(selected),
        },
        "train_rows": int(len(df)),
        "features": report_features,
        "selected": selected,
    }
    return selected, report


def _log_factor_selection_summary(report: dict[str, Any]) -> None:
    """把筛选报告压缩成可读日志：漏斗 + 淘汰原因统计 + 高 ICIR 但被拒的"可惜"名单。"""
    sc = report.get("stage_counts") or {}
    logger.info(
        "Factor selection funnel: %d -> IC/ICIR %d -> corr %d -> stable %d -> selected %d",
        sc.get("input", 0), sc.get("ic_pass", 0), sc.get("corr_pass", 0),
        sc.get("stable", 0), sc.get("selected", 0),
    )
    reasons: dict[str, int] = {}
    for f in report.get("features") or []:
        if f.get("status") != "selected":
            r = str(f.get("reason") or "未通过筛选")
            reasons[r] = reasons.get(r, 0) + 1
    for r, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
        logger.info("  rejected x%-3d %s", cnt, r)
    # 高 |ICIR| 却被拒的特征（"可惜"名单：多为稳定性/覆盖率问题）
    rejected = [
        f for f in (report.get("features") or [])
        if f.get("status") != "selected" and f.get("icir")
    ]
    notable = sorted(rejected, key=lambda f: -abs(f["icir"]))[:8]
    if notable:
        logger.info("Notable rejections (high |ICIR| but dropped):")
        for f in notable:
            logger.info(
                "  %-32s IC=%.4f ICIR=%.3f cov=%.0f%% -> %s",
                f.get("name", ""), f.get("ic", 0.0), f.get("icir", 0.0),
                (f.get("coverage") or 0.0) * 100, f.get("reason", ""),
            )




# ── Optuna 自动超参搜索 ────────────────────────────────────────────────────────
def _tune_tree_hyperparams(
    cfg: dict,
    model_type: str,
    features: list[str],
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    val_df: pd.DataFrame,
) -> dict | None:
    """Optuna 自动搜索树模型超参，以验证集 Rank ICIR 为目标。

    返回最优参数 dict（合并进模型参数）；未安装 optuna 时返回 None（优雅降级）。
    搜索空间面向 A 股选股场景（截面 rank 收益标签、防过拟合优先）。
    """
    try:
        import optuna
    except ImportError:
        logger.warning("optuna 未安装，跳过超参搜索（pip install optuna 可启用）")
        return None

    optuna_cfg = cfg.get("optuna", {}) or {}
    n_trials = max(5, int(optuna_cfg.get("n_trials", 20)))
    seed = int((cfg.get("seed") or 42))
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def _objective(trial) -> float:
        params: dict[str, Any] = {}
        if model_type == "lightgbm":
            params.update({
                "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.1, log=True),
                "num_leaves": trial.suggest_int("num_leaves", 15, 127),
                "min_child_samples": trial.suggest_int("min_child_samples", 20, 500),
                "feature_fraction": trial.suggest_float("feature_fraction", 0.4, 0.9),
                "bagging_fraction": trial.suggest_float("bagging_fraction", 0.4, 0.9),
                "lambda_l1": trial.suggest_float("lambda_l1", 0.0, 5.0),
                "lambda_l2": trial.suggest_float("lambda_l2", 0.0, 10.0),
            })
            model = _train_lgb({**cfg, "model": {**cfg.get("model", {}), "params": {**cfg.get("model", {}).get("params", {}), **params}}}, features, X_train, y_train, X_val, y_val)
        elif model_type == "xgboost":
            params.update({
                "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.1, log=True),
                "max_depth": trial.suggest_int("max_depth", 3, 8),
                "subsample": trial.suggest_float("subsample", 0.5, 0.9),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.4, 0.9),
                "min_child_weight": trial.suggest_int("min_child_weight", 20, 300),
                "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 5.0),
            })
            model = _train_xgb({**cfg, "model": {**cfg.get("model", {}), "xgb_params": {**cfg.get("model", {}).get("xgb_params", {}), **params}}}, features, X_train, y_train, X_val, y_val)
        elif model_type == "catboost":
            params.update({
                "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.1, log=True),
                "depth": trial.suggest_int("depth", 4, 10),
                "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 1.0, 10.0, log=True),
                "random_strength": trial.suggest_float("random_strength", 0.5, 5.0),
            })
            model = _train_catboost({**cfg, "model": {**cfg.get("model", {}), "catboost_params": {**cfg.get("model", {}).get("catboost_params", {}), **params}}}, features, X_train, y_train, X_val, y_val)
        else:
            return -1.0

        y_pred = _predict_with_model(model, X_val, model_type, features)
        m = _compute_metrics(val_df, y_val.astype("float32"), np.asarray(y_pred, dtype=np.float32).flatten())
        return float(m["rank_icir"]) if np.isfinite(m["rank_icir"]) else -1.0

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(_objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_params
    logger.info("Optuna %s best params (trial=%d, val_rank_icir=%.4f): %s",
                model_type, study.best_trial.number, study.best_value, best)
    return {
        "best_params": best,
        "best_value": float(study.best_value),
        "n_trials": n_trials,
    }


# ── 多模型并行训练 ──────────────────────────────────────────────────────────────
def _train_single_model(
    model_type: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    df: pd.DataFrame,
    features: list[str],
    cfg: dict,
    hardware: dict | None = None,
    need_full_pred: bool = True,
) -> dict[str, Any]:
    """训练单个模型，返回结果字典（可序列化）。

    need_full_pred=False 时跳过全量预测与 pred_df 生成：
    OOF fold 训练只需要 fold 内验证集预测，传全量 df 会导致每个 fold 都
    对全量数据做一次预测 + 生成 644 万行 DataFrame，9 次叠加是 OOM 主因之一。
    """
    logger.info("--- Training %s ---", model_type)
    t0 = time.time()

    model_cfg = cfg.get("model", {})
    _optuna_result = None
    fill_values, X_train, y_train, X_val, y_val, _fill = _prepare_arrays(
        train_df, val_df, features, prep_cfg=cfg.get("preprocessing") or {}
    )

    if model_type in ("lightgbm", "xgboost", "catboost"):
        # Optuna 自动超参搜索：显式 optuna.enabled=true 时，先搜索最优参数再训练。
        # _train_single_model 的 OOF fold（need_full_pred=False）不触发，避免重复搜索。
        _optuna_cfg = cfg.get("optuna", {}) or {}
        if _optuna_cfg.get("enabled") and need_full_pred:
            _optuna_result = _tune_tree_hyperparams(
                cfg, model_type, features, X_train, y_train, X_val, y_val, val_df
            )
            if _optuna_result and _optuna_result.get("best_params"):
                # 将最优参数合并进模型参数后重新训练
                _best = _optuna_result["best_params"]
                _merge_cfg = dict(cfg)
                _model_cfg = dict(cfg.get("model", {}))
                if model_type == "lightgbm":
                    _model_cfg["params"] = {**(_model_cfg.get("params") or {}), **_best}
                elif model_type == "xgboost":
                    _model_cfg["xgb_params"] = {**(_model_cfg.get("xgb_params") or {}), **_best}
                elif model_type == "catboost":
                    _model_cfg["catboost_params"] = {**(_model_cfg.get("catboost_params") or {}), **_best}
                _merge_cfg["model"] = _model_cfg
                cfg = _merge_cfg
        if model_type == "lightgbm":
            model = _train_lgb(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "xgboost":
            model = _train_xgb(cfg, features, X_train, y_train, X_val, y_val)
        elif model_type == "catboost":
            model = _train_catboost(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "linear":
        model = _train_linear(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "random_forest":
        model = _train_rf(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "mlp":
        model = _train_mlp(cfg, features, X_train, y_train, X_val, y_val)
    elif model_type == "nativetft":
        output_dir = WORKSPACE_DIR
        dl_params = model_cfg.get("dl_params", {})
        model, train_m, val_m, dl_metadata = _train_nativetft(
            model_type, train_df, val_df, features, dl_params, output_dir, hardware=hardware
        )
        y_full_pred = _predict_nativetft(output_dir, df, features, dl_metadata)
        full_pred_df = df[["symbol", "trade_date", "label"]].copy()
        # 时序 DL 预测返回 DataFrame(symbol,trade_date,pred)，按 key 对齐合并
        full_pred_df = full_pred_df.merge(
            y_full_pred[["symbol", "trade_date", "pred"]],
            on=["symbol", "trade_date"], how="left",
        )
        full_pred_df["split"] = "train"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= val_df["trade_date"].min()) &
            (full_pred_df["trade_date"] <= val_df["trade_date"].max()), "split"] = "valid"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= test_df["trade_date"].min()) &
            (full_pred_df["trade_date"] <= test_df["trade_date"].max()), "split"] = "test"
        test_mask = full_pred_df["split"] == "test"
        y_test_pred = full_pred_df.loc[test_mask, "pred"].values
        y_test_true = full_pred_df.loc[test_mask, "label"].values
        test_m = _compute_metrics(test_df, y_test_true.astype("float32"), y_test_pred.astype("float32"))
        elapsed = time.time() - t0
        return {
            "model_type": model_type,
            "model": model,
            "fill_values": fill_values,
            "train_m": train_m,
            "val_m": val_m,
            "test_m": test_m,
            "dl_metadata": dl_metadata,
            "full_pred_df": full_pred_df,
            "elapsed": elapsed,
        }
    elif model_type in _DL_MODEL_TYPES:
        output_dir = WORKSPACE_DIR
        dl_params = model_cfg.get("dl_params", {})
        model, train_m, val_m, dl_metadata = _train_dl(
            model_type, train_df, val_df, features, dl_params, output_dir, hardware=hardware
        )
        y_full_pred = _predict_dl(output_dir, df, features, dl_metadata)
        full_pred_df = df[["symbol", "trade_date", "label"]].copy()
        # 时序 DL 预测返回 DataFrame(symbol,trade_date,pred)，按 key 对齐合并
        full_pred_df = full_pred_df.merge(
            y_full_pred[["symbol", "trade_date", "pred"]],
            on=["symbol", "trade_date"], how="left",
        )
        full_pred_df["split"] = "train"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= val_df["trade_date"].min()) &
            (full_pred_df["trade_date"] <= val_df["trade_date"].max()), "split"] = "valid"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= test_df["trade_date"].min()) &
            (full_pred_df["trade_date"] <= test_df["trade_date"].max()), "split"] = "test"
        test_mask = full_pred_df["split"] == "test"
        y_test_pred = full_pred_df.loc[test_mask, "pred"].values
        y_test_true = full_pred_df.loc[test_mask, "label"].values
        test_m = _compute_metrics(test_df, y_test_true.astype("float32"), y_test_pred.astype("float32"))
        elapsed = time.time() - t0
        return {
            "model_type": model_type,
            "model": model,
            "fill_values": fill_values,
            "train_m": train_m,
            "val_m": val_m,
            "test_m": test_m,
            "pred_df": full_pred_df.reset_index(drop=True),
            "split_frames": {"train": train_df.reset_index(drop=True), "valid": val_df.reset_index(drop=True), "test": test_df.reset_index(drop=True)},
            "dl_metadata": dl_metadata,
            "elapsed": elapsed,
        }
    else:
        raise ValueError(f"Unsupported model_type: {model_type}")

    # 树模型预测
    y_train_pred = _predict_with_model(model, _fill(train_df), model_type, features)
    y_val_pred = _predict_with_model(model, _fill(val_df), model_type, features)
    y_test_pred = _predict_with_model(model, _fill(test_df), model_type, features)
    train_m = _compute_metrics(train_df, y_train, y_train_pred)
    val_m = _compute_metrics(val_df, y_val, y_val_pred)
    test_m = _compute_metrics(test_df, test_df["label"].astype("float32").to_numpy(), y_test_pred)

    # 全量预测（pred_df）只在需要时生成：OOF fold 训练不需要，跳过可避免
    # 每个 fold 对全量数据(644万行)预测+拷贝，9次叠加是 OOM 主因
    if need_full_pred:
        full_pred_df = df[["symbol", "trade_date", "label"]].copy()
        full_pred_df["pred"] = _predict_with_model(model, _fill(df), model_type, features)
        full_pred_df["split"] = "train"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= val_df["trade_date"].min()) &
            (full_pred_df["trade_date"] <= val_df["trade_date"].max()), "split"] = "valid"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= test_df["trade_date"].min()) &
            (full_pred_df["trade_date"] <= test_df["trade_date"].max()), "split"] = "test"
    else:
        full_pred_df = None

    best_iteration = getattr(model, "best_iteration", None)
    if best_iteration is None and hasattr(model, "get_best_iteration"):
        try:
            best_iteration = model.get_best_iteration()
        except Exception:
            best_iteration = None

    elapsed = time.time() - t0
    logger.info("%s finished in %.2fs, best_iter=%s, val_ic=%.4f, val_icir=%.4f",
                model_type, elapsed, best_iteration, val_m["ic"], val_m["rank_icir"])

    return {
        "model_type": model_type,
        "model": model,
        "fill_values": fill_values,
        "train_m": train_m,
        "val_m": val_m,
        "test_m": test_m,
        "pred_df": full_pred_df.reset_index(drop=True) if full_pred_df is not None else None,
        "split_frames": {"train": train_df.reset_index(drop=True), "valid": val_df.reset_index(drop=True), "test": test_df.reset_index(drop=True)},
        "best_iteration": best_iteration,
        "optuna": _optuna_result,
        "elapsed": elapsed,
    }


def train_multi_models(
    df: pd.DataFrame,
    features: list[str],
    cfg: dict,
    hardware: dict | None = None,
) -> dict[str, Any]:
    """多模型并行训练：数据加载一次，依次训练多个模型，生成对比报告。

    返回 dict 包含：
    - models: {model_type: {model, fill_values, metrics, pred_df, ...}}
    - comparison: 对比报告
    - primary_model_type: 最佳模型类型
    """
    model_cfg = cfg.get("model", {})
    model_types_raw = model_cfg.get("types", [model_cfg.get("type", "lightgbm")])
    if isinstance(model_types_raw, str):
        model_types_raw = [model_types_raw]
    model_types = [str(t).strip().lower() for t in model_types_raw]

    # 验证
    for mt in model_types:
        if mt not in _ALL_MODEL_TYPES:
            raise ValueError(f"Unsupported model_type: {mt}")

    ensemble_method = str(model_cfg.get("ensemble", "none")).strip().lower()
    if ensemble_method not in ("none", "stacking", "blending", "voting"):
        raise ValueError(f"Unsupported ensemble method: {ensemble_method}")

    logger.info("=== Multi-Model Training: %s ===", model_types)
    logger.info("Ensemble method: %s", ensemble_method)

    # 数据切分（共享）
    train_df, val_df, test_df = _split_data(df, cfg)

    # 依次训练每个模型
    model_results: dict[str, dict] = {}
    for mt in model_types:
        model_results[mt] = _train_single_model(
            mt, train_df, val_df, test_df, df, features, cfg, hardware=hardware
        )

    # 生成对比报告
    comparison_rows = []
    for mt, res in model_results.items():
        vm = res["val_m"]
        comparison_rows.append({
            "model_type": mt,
            "val_ic": round(vm["ic"], 6),
            "val_rank_ic": round(vm["rank_ic"], 6),
            "val_rank_icir": round(vm["rank_icir"], 4),
            "val_rmse": round(vm["rmse"], 6),
            "val_auc": round(vm["auc"], 6),
            "test_ic": round(res["test_m"]["ic"], 6),
            "test_rank_ic": round(res["test_m"]["rank_ic"], 6),
            "test_rank_icir": round(res["test_m"]["rank_icir"], 4),
            "elapsed_seconds": round(res["elapsed"], 1),
        })

    # 按 ICIR 排序确定最佳模型
    comparison_rows.sort(key=lambda r: abs(r["val_rank_icir"]), reverse=True)
    best = comparison_rows[0]["model_type"]

    logger.info("=== Model Comparison ===")
    logger.info("%-12s %10s %10s %10s %10s", "Model", "Val IC", "RankIC", "ICIR", "Time(s)")
    for row in comparison_rows:
        logger.info("%-12s %10.4f %10.4f %10.4f %10.1f",
                    row["model_type"], row["val_ic"], row["val_rank_ic"], row["val_rank_icir"], row["elapsed_seconds"])
    logger.info("Best model: %s (val_icir=%.4f)", best, comparison_rows[0]["val_rank_icir"])

    return {
        "models": model_results,
        "comparison": comparison_rows,
        "primary_model_type": best,
        "model_types": model_types,
        "ensemble_method": ensemble_method,
        "split_frames": {"train": train_df.reset_index(drop=True), "valid": val_df.reset_index(drop=True), "test": test_df.reset_index(drop=True)},
    }


def _generate_oof_predictions(
    model_type: str,
    train_df: pd.DataFrame,
    features: list[str],
    cfg: dict,
    n_folds: int = 3,
    hardware: dict | None = None,
) -> pd.Series:
    """时序扩展窗口 K-Fold 生成 OOF 预测。

    返回与 train_df 等长的 OOF 预测（fold 未覆盖部分为 NaN）。
    """
    dates = sorted(train_df["trade_date"].unique())
    n_dates = len(dates)
    if n_dates < n_folds + 1:
        logger.warning("Too few dates (%d) for %d folds, reducing to %d", n_dates, n_folds, max(1, n_dates - 1))
        n_folds = max(1, n_dates - 1)

    fold_size = n_dates // (n_folds + 1)
    oof_pred = pd.Series(np.nan, index=train_df.index, name="oof_pred")

    for fold_i in range(n_folds):
        train_end_idx = fold_size * (fold_i + 1)
        val_start_idx = train_end_idx
        val_end_idx = min(train_end_idx + fold_size, n_dates)

        if val_end_idx <= val_start_idx:
            continue

        train_dates = set(dates[:train_end_idx])
        val_dates = set(dates[val_start_idx:val_end_idx])

        fold_train = train_df[train_df["trade_date"].isin(train_dates)]
        fold_val = train_df[train_df["trade_date"].isin(val_dates)]

        if len(fold_train) < 100 or len(fold_val) < 10:
            logger.warning("Fold %d too small (train=%d, val=%d), skipping", fold_i, len(fold_train), len(fold_val))
            continue

        # 训练 fold 基模型：OOF 只需要 fold 内验证集预测，跳过全量 pred_df 生成
        # （fold 传全量 df 会对 644 万行做全量预测 + 拷贝，9 次叠加是 OOM 主因）
        fold_result = _train_single_model(
            model_type, fold_train, fold_val, fold_val,
            train_df, features, cfg, hardware=hardware, need_full_pred=False,
        )
        fold_model = fold_result["model"]
        fill_values = fold_result["fill_values"]

        # 预测 fold 验证集
        X_val = fold_val[features].fillna(fill_values).values
        fold_pred = np.asarray(
            _predict_with_model(fold_model, X_val, model_type, features)
        ).flatten()

        oof_pred.iloc[fold_val.index] = fold_pred
        logger.info("OOF fold %d: train=%d dates, val=%d dates, pred_rows=%d",
                     fold_i, len(train_dates), len(val_dates), len(fold_val))

    return oof_pred


def train_stacking(
    df: pd.DataFrame,
    features: list[str],
    cfg: dict,
    model_types: list[str],
    n_folds: int = 3,
    hardware: dict | None = None,
) -> dict[str, Any]:
    """Stacking 集成训练：时序 K-Fold OOF + Ridge 元学习器。

    流程：
    1. 数据切分 train/val/test
    2. 对每个基模型生成 OOF 预测（时序扩展窗口）+ 训练全量基模型
    3. 构建元特征矩阵 [oof_lgb, oof_xgb, oof_cbm]
    4. 训练 Ridge 元学习器
    5. 在 val/test 上评估集成效果
    """
    from sklearn.linear_model import Ridge

    model_cfg = cfg.get("model", {})
    train_df, val_df, test_df = _split_data(df, cfg)

    # Step 1: 生成各基模型 OOF 预测 + 全量基模型
    oof_preds: dict[str, pd.Series] = {}
    base_fill_values: dict[str, dict] = {}
    base_results: dict[str, dict] = {}

    for mt in model_types:
        logger.info("=== Stacking: generating OOF for %s ===", mt)
        oof_preds[mt] = _generate_oof_predictions(
            mt, train_df, features, cfg, n_folds=n_folds, hardware=hardware,
        )
        gc.collect()

        # 全量基模型：既用于 val/test 评估，也是保存/推理时使用的模型。
        # stacking 用 OOF 构建元特征，不需要 base 的 pred_df，跳过全量预测省内存
        base_result = _train_single_model(
            mt, train_df, val_df, test_df, df, features, cfg, hardware=hardware,
            need_full_pred=False,
        )
        # stacking 用 OOF 构建元特征，pred_df（全量1000万行预测）不再需要，
        # 立即释放降低峰值内存，避免 3 个模型累积后 OOM
        base_result.pop("pred_df", None)
        base_result.pop("full_pred_df", None)
        base_results[mt] = base_result
        base_fill_values[mt] = base_result["fill_values"]
        logger.info("Base model %s: val_icir=%.4f, test_icir=%.4f",
                     mt, base_result["val_m"]["rank_icir"], base_result["test_m"]["rank_icir"])
        gc.collect()

    base_models: dict[str, Any] = {mt: base_results[mt]["model"] for mt in model_types}

    # Step 2: 构建元特征矩阵（OOF 预测作为特征）
    meta_features_train = pd.DataFrame({
        f"oof_{mt}": oof_preds[mt] for mt in model_types
    })
    # 去除 NaN 行（某些 fold 未覆盖的样本）
    valid_mask = meta_features_train.notna().all(axis=1)
    meta_X_train = meta_features_train[valid_mask].values
    label_col = "label"
    meta_y_train = train_df.loc[valid_mask, label_col].values

    logger.info("Meta-learner training samples: %d (from %d train samples)",
                len(meta_y_train), len(train_df))

    # Step 3: 训练 Ridge 元学习器（alpha 可配，默认 1.0）
    meta_alpha = float(model_cfg.get("meta_alpha", 1.0))
    meta_model = Ridge(alpha=meta_alpha, fit_intercept=True, random_state=42)
    meta_model.fit(meta_X_train, meta_y_train)
    logger.info("Ridge meta-learner (alpha=%.3f) coefficients: %s", meta_alpha, dict(zip(
        [f"oof_{mt}" for mt in model_types], meta_model.coef_.round(4)
    )))

    # Step 4: 在 val/test 上评估集成
    def _predict_base(model_type: str, data_df: pd.DataFrame) -> np.ndarray:
        fv = base_fill_values[model_type]
        X = data_df[features].fillna(fv).values
        model = base_models[model_type]
        return np.asarray(_predict_with_model(model, X, model_type, features)).flatten()

    # Val 集成预测
    val_base_preds = {mt: _predict_base(mt, val_df) for mt in model_types}
    meta_X_val = np.column_stack([val_base_preds[mt] for mt in model_types])
    val_ensemble_pred = meta_model.predict(meta_X_val)

    # Test 集成预测
    test_base_preds = {mt: _predict_base(mt, test_df) for mt in model_types}
    meta_X_test = np.column_stack([test_base_preds[mt] for mt in model_types])
    test_ensemble_pred = meta_model.predict(meta_X_test)

    # 评估集成指标
    def _calc_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
        from scipy.stats import spearmanr
        rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))
        ic = float(np.corrcoef(y_true, y_pred)[0, 1]) if len(y_true) > 2 else 0.0
        rank_ic, _ = spearmanr(y_true, y_pred)
        rank_ic = float(rank_ic) if not np.isnan(rank_ic) else 0.0
        icir = ic / (np.std(y_pred) + 1e-9)
        rank_icir = rank_ic / (np.std(y_pred) + 1e-9)
        return {"rmse": rmse, "ic": ic, "rank_ic": rank_ic, "icir": icir, "rank_icir": rank_icir, "auc": 0.0}

    val_ensemble_m = _calc_metrics(val_df[label_col].values, val_ensemble_pred)
    test_ensemble_m = _calc_metrics(test_df[label_col].values, test_ensemble_pred)

    logger.info("=== Stacking Ensemble Results ===")
    logger.info("Val:  IC=%.4f, RankIC=%.4f, ICIR=%.4f", val_ensemble_m["ic"], val_ensemble_m["rank_ic"], val_ensemble_m["rank_icir"])
    logger.info("Test: IC=%.4f, RankIC=%.4f, ICIR=%.4f", test_ensemble_m["ic"], test_ensemble_m["rank_ic"], test_ensemble_m["rank_icir"])

    # 对比：最佳单模型 vs 集成
    best_single = max(base_results.items(), key=lambda x: abs(x[1]["val_m"]["rank_icir"]))
    logger.info("Best single (%s): val_icir=%.4f vs Stacking: val_icir=%.4f",
                best_single[0], best_single[1]["val_m"]["rank_icir"], val_ensemble_m["rank_icir"])

    # 构建全量预测 DataFrame（val/test 用集成预测，train 用 OOF 集成预测）
    # train/val/test 已 reset_index 且 df 按 symbol 排序，不能按位置索引回 df，
    # 必须按 (symbol, trade_date) 对齐，否则预测会写到错误的行上。
    oof_ensemble = meta_model.predict(meta_X_train)
    pred_parts = pd.concat([
        pd.DataFrame({
            "trade_date": train_df.loc[valid_mask, "trade_date"].values,
            "symbol": train_df.loc[valid_mask, "symbol"].values,
            "pred": oof_ensemble,
        }),
        pd.DataFrame({
            "trade_date": val_df["trade_date"].values,
            "symbol": val_df["symbol"].values,
            "pred": val_ensemble_pred,
        }),
        pd.DataFrame({
            "trade_date": test_df["trade_date"].values,
            "symbol": test_df["symbol"].values,
            "pred": test_ensemble_pred,
        }),
    ], ignore_index=True)
    pred_df = df[["trade_date", "symbol"]].merge(
        pred_parts, on=["trade_date", "symbol"], how="left"
    )

    # 保存 OOF 预测（诊断用）
    oof_df = pd.DataFrame({
        "trade_date": train_df["trade_date"],
        "symbol": train_df["symbol"],
        **{f"oof_{mt}": oof_preds[mt] for mt in model_types},
        "label": train_df[label_col],
    })
    oof_path = WORKSPACE_DIR / "oof_predictions.parquet"
    oof_df.to_parquet(oof_path, engine="pyarrow", compression="zstd", index=False)
    logger.info("OOF predictions saved to %s", oof_path)

    # 保存元学习器
    import pickle
    meta_model_path = WORKSPACE_DIR / "meta_model.pkl"
    with open(meta_model_path, "wb") as f:
        pickle.dump({
            "model": meta_model,
            "model_types": model_types,
            "n_folds": n_folds,
        }, f)
    logger.info("Meta-learner saved to %s", meta_model_path)

    # 生成对比报告
    comparison_rows = []
    for mt, res in base_results.items():
        vm = res["val_m"]
        comparison_rows.append({
            "model_type": mt,
            "val_ic": round(vm["ic"], 6),
            "val_rank_ic": round(vm["rank_ic"], 6),
            "val_rank_icir": round(vm["rank_icir"], 4),
            "val_rmse": round(vm["rmse"], 6),
            "val_auc": round(vm["auc"], 6),
            "test_ic": round(res["test_m"]["ic"], 6),
            "test_rank_ic": round(res["test_m"]["rank_ic"], 6),
            "test_rank_icir": round(res["test_m"]["rank_icir"], 4),
            "elapsed_seconds": round(res["elapsed"], 1),
        })
    comparison_rows.append({
        "model_type": "stacking_ensemble",
        "val_ic": round(val_ensemble_m["ic"], 6),
        "val_rank_ic": round(val_ensemble_m["rank_ic"], 6),
        "val_rank_icir": round(val_ensemble_m["rank_icir"], 4),
        "val_rmse": round(val_ensemble_m["rmse"], 6),
        "val_auc": round(val_ensemble_m["auc"], 6),
        "test_ic": round(test_ensemble_m["ic"], 6),
        "test_rank_ic": round(test_ensemble_m["rank_ic"], 6),
        "test_rank_icir": round(test_ensemble_m["rank_icir"], 4),
        "elapsed_seconds": 0.0,
    })
    comparison_rows.sort(key=lambda r: abs(r["val_rank_icir"]), reverse=True)

    best_type = comparison_rows[0]["model_type"]
    primary_type = best_type if best_type in model_types else model_types[0]

    return {
        "models": base_results,
        "base_models": base_models,
        "base_fill_values": base_fill_values,
        "meta_model": meta_model,
        "comparison": comparison_rows,
        "primary_model_type": primary_type,
        "model_types": model_types,
        "ensemble_method": "stacking",
        "val_ensemble_m": val_ensemble_m,
        "test_ensemble_m": test_ensemble_m,
        "pred_df": pred_df,
        "oof_preds": oof_preds,
        "split_frames": {"train": train_df.reset_index(drop=True), "valid": val_df.reset_index(drop=True), "test": test_df.reset_index(drop=True)},
    }


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
    result_path = WORKSPACE_DIR / "result.json"

    try:
        if not cfg_path.exists():
            raise RuntimeError(f"Config file not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text())

        run_id          = cfg.get("run_id", "unknown")
        job_name        = cfg.get("job_name", "unnamed")
        result_path     = Path(cfg.get("output", {}).get("result_path", str(WORKSPACE_DIR / "result.json")))
        callback_url    = cfg.get("callback", {}).get("url", "")
        callback_secret = cfg.get("callback", {}).get("secret", "")

        logger.info("=== QuantMind Training Start ===")
        logger.info(f"run_id={run_id}  job={job_name}  config={cfg_path}")

        # 全局随机种子：保证同一配置下训练可复现（种子可配置，默认 42）
        _seed = int((cfg.get("seed") or 42))
        random.seed(_seed)
        np.random.seed(_seed)
        try:
            torch.manual_seed(_seed)
        except Exception:
            pass
        logger.info("Global random seed set to %d", _seed)

        # 硬件环境检测
        hardware = detect_hardware()

        # 数据加载（特征列自动补齐基础6列）
        submitted_features = list(dict.fromkeys([str(item).strip() for item in (cfg["data"].get("features", []) or []) if str(item).strip()]))
        factor_source = str((cfg.get("data", {}) or {}).get("factor_source") or "").strip()
        if factor_source:
            # Factor catalog already owns the selected raw QuantDB fields.
            auto_appended_features = []
            features = submitted_features
        else:
            auto_appended_features = [feature for feature in TRAINING_BASE_FEATURES if feature not in submitted_features]
            features = list(dict.fromkeys(TRAINING_BASE_FEATURES + submitted_features))
        source_mode = str((cfg.get("data", {}) or {}).get("source_mode") or "LOCAL").strip().upper()
        local_data_dir = str((cfg.get("data", {}) or {}).get("local_dir") or "").strip() or None
        explain_cfg = _normalize_explain_cfg(cfg.get("explain") or {})
        context_cfg = cfg.get("context", {}) or {}
        market = str(context_cfg.get("market", "CN")).upper()

        df, valid_features = load_data(
            cfg["data"]["train_start"],
            cfg["data"]["train_end"],
            features,
            target_horizon_days=int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1),
            target_mode=str((cfg.get("label", {}) or {}).get("target_mode") or "return"),
            cache_dir=cfg.get("cache", {}).get("dir"),
            valid_end=cfg.get("split", {}).get("valid", [None, None])[1],
            test_end=cfg.get("split", {}).get("test", [None, None])[1],
            source_mode=source_mode,
            local_dir=local_data_dir,
            market=market,
            industry_as_feature=bool(cfg.get("context", {}).get("industry_as_feature", False)),
            factor_source=factor_source or None,
            quantdb_dir=str((cfg.get("data", {}) or {}).get("quantdb_dir") or "").strip() or None,
            factor_field_sources=(cfg.get("data", {}) or {}).get("factor_field_sources") or None,
        )

        # ── 因子筛选 ──
        factor_selection_cfg = cfg.get("factor_selection", {}) or {}
        factor_selection_method = str(factor_selection_cfg.get("method", "")).strip().lower()
        factor_selection_report: dict[str, Any] | None = None
        if factor_selection_method in ("ic_icir", "combined") or submitted_features and len(submitted_features) == 1 and submitted_features[0].lower().startswith("auto_top"):
            n_top = int(factor_selection_cfg.get("n_top", 80))
            ic_thresh = float(factor_selection_cfg.get("ic_threshold", 0.01))
            icir_thresh = float(factor_selection_cfg.get("icir_threshold", 0.15))
            corr_thresh = float(factor_selection_cfg.get("correlation_threshold", 0.9))
            logger.info("=== Auto Factor Selection: top-%d ===", n_top)
            # 特征选择属于拟合过程的一部分：只能看到训练段。此前直接把完整
            # train/valid/test df 传入，会让 test 标签影响入选因子及最终样本外指标。
            selection_train_df, _, _ = _split_data(df, cfg)
            valid_features, factor_selection_report = select_top_factors(
                selection_train_df, valid_features, label_col="label",
                n_top=n_top, ic_threshold=ic_thresh,
                icir_threshold=icir_thresh, correlation_threshold=corr_thresh,
            )
            logger.info(
                "Selected %d features from training segment only (%d rows)",
                len(valid_features), len(selection_train_df),
            )
            if factor_selection_report:
                _log_factor_selection_summary(factor_selection_report)

        # ── WFA 稳定性诊断（可选）：数据就绪后、正式训练前执行 ──
        wfa_result = train_wfa(df, valid_features, cfg)

        # ── 数据漂移检测（PSI）：训练区间 vs 最近交易日分布对比 ──
        drift_cfg = cfg.get("drift") or {}
        if drift_cfg.get("enabled") is False:
            psi_result = {"enabled": False, "reason": "disabled by config"}
        else:
            psi_result = compute_psi_drift(
                df,
                valid_features,
                cfg["data"]["train_start"],
                cfg["data"]["train_end"],
                n_recent_days=int(drift_cfg.get("n_recent_days", 30)),
            )
        if psi_result.get("enabled"):
            logger.info(
                "Data drift (PSI): overall=%s max_rank_disp=%.4f stable=%d medium=%d severe=%d",
                psi_result.get("overall"),
                psi_result.get("max_psi", float("nan")),
                psi_result.get("drift", {}).get("stable", 0),
                psi_result.get("drift", {}).get("medium", 0),
                psi_result.get("drift", {}).get("severe", 0),
            )

        train_t0 = time.time()

        # ── 判断单模型 vs 多模型 ──
        model_cfg = cfg.get("model", {})
        model_types_raw = model_cfg.get("types", None)
        is_multi_model = bool(model_types_raw and isinstance(model_types_raw, list) and len(model_types_raw) > 1)

        if is_multi_model:
            # ── 多模型训练路径 ──
            ensemble_method = str(model_cfg.get("ensemble", "none")).strip().lower()
            if ensemble_method == "stacking":
                multi_result = train_stacking(
                    df, valid_features, cfg,
                    model_types=[str(t).strip().lower() for t in model_types_raw],
                    n_folds=int(model_cfg.get("n_folds", 3)),
                    hardware=hardware,
                )
            else:
                multi_result = train_multi_models(df, valid_features, cfg, hardware=hardware)
            elapsed = float(time.time() - train_t0)
            primary_type = multi_result["primary_model_type"]
            is_stacking = multi_result.get("ensemble_method") == "stacking"

            # 保存各基模型
            workspace = WORKSPACE_DIR
            saved_models: dict[str, str] = {}
            for mt, res in multi_result["models"].items():
                suffix_map = {"lightgbm": "_lgb", "xgboost": "_xgb", "catboost": "_cbm", "linear": "_lin"}
                suffix = suffix_map.get(mt, f"_{mt}")
                model_filename = _save_model(res["model"], mt, workspace.with_name(workspace.name) if False else workspace)
                ext = Path(model_filename).suffix
                new_name = f"model{suffix}{ext}"
                if model_filename != new_name:
                    (workspace / model_filename).rename(workspace / new_name)
                    model_filename = new_name
                saved_models[mt] = model_filename
                logger.info("Saved %s model: %s", mt, model_filename)

            # 获取主模型指标和预测
            primary_res = multi_result["models"][primary_type]
            if is_stacking:
                # Stacking: 使用集成预测和集成指标
                model = primary_res["model"]
                fill_values = primary_res["fill_values"]
                val_m = multi_result["val_ensemble_m"]
                test_m = multi_result["test_ensemble_m"]
                train_m = primary_res["train_m"]
                pred_df = multi_result["pred_df"]
                split_frames = primary_res["split_frames"]
                actual_model_type = "stacking"
                dl_metadata = primary_res.get("dl_metadata")
            else:
                model = primary_res["model"]
                fill_values = primary_res["fill_values"]
                train_m, val_m, test_m = primary_res["train_m"], primary_res["val_m"], primary_res["test_m"]
                pred_df = primary_res["pred_df"]
                split_frames = primary_res["split_frames"]
                actual_model_type = primary_type
                dl_metadata = primary_res.get("dl_metadata")

            best_iteration = getattr(model, "best_iteration", None)
            if best_iteration is None and hasattr(model, "get_best_iteration"):
                try:
                    best_iteration = model.get_best_iteration()
                except Exception:
                    best_iteration = None

            # 保存预测
            pred_path = WORKSPACE_DIR / "pred.parquet"
            pred_df.to_parquet(pred_path, engine="pyarrow", compression="zstd", index=False)
            logger.info(f"Predictions saved to {pred_path}")

            pred_qlib = (
                pred_df[["trade_date", "symbol", "pred"]]
                .rename(columns={"trade_date": "datetime", "symbol": "instrument", "pred": "score"})
                .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
                .set_index(["datetime", "instrument"])
                .sort_index()
            )
            pred_pkl_path = WORKSPACE_DIR / "pred.pkl"
            pred_qlib.to_pickle(pred_pkl_path)
            logger.info(f"Backtest-compatible pred.pkl saved ({len(pred_qlib):,} rows)")

            # 保存对比报告
            comparison_path = workspace / "model_comparison.json"
            comparison_path.write_text(json.dumps(multi_result["comparison"], ensure_ascii=False, indent=2, default=str))

            # SHAP（仅 LightGBM 基模型）
            shap_info: dict[str, Any] = {"enabled": False, "status": "disabled"}
            if "lightgbm" in multi_result["models"]:
                lgb_res = multi_result["models"]["lightgbm"]
                shap_summary_path = WORKSPACE_DIR / "shap_summary.csv"
                shap_info = _compute_shap_summary(
                    model=lgb_res["model"],
                    split_frames=lgb_res["split_frames"],
                    features=valid_features,
                    fill_values=lgb_res["fill_values"],
                    explain_cfg=explain_cfg,
                    out_path=shap_summary_path,
                )
            else:
                logger.info("SHAP skipped: no LightGBM in multi-model run")

            # 保存各基模型独立预测（parquet）
            for mt, res in multi_result["models"].items():
                base_pred_path = workspace / f"pred_{mt}.parquet"
                # stacking 模式为省内存已 pop 掉 base 的 pred_df（用 OOF 做元特征），这里跳过即可
                base_pred = res.get("pred_df")
                if base_pred is None:
                    logger.info("Skip saving %s base pred (pred_df=None, stacking mode)", mt)
                    continue
                base_pred.to_parquet(base_pred_path, engine="pyarrow", compression="zstd", index=False)

            # 构造 metadata
            metadata = {
                "run_id": run_id, "job_name": job_name,
                "is_multi_model": True,
                "is_ensemble": is_stacking,
                "model_types": multi_result["model_types"],
                "primary_model_type": primary_type,
                "framework": _get_model_framework(primary_type),
                "model_type": actual_model_type,
                "model_file": saved_models.get(primary_type, ""),
                "saved_models": saved_models,
                "comparison": multi_result["comparison"],
                "ensemble_method": multi_result["ensemble_method"],
                "hardware": hardware,
                "feature_count": len(valid_features),
                "requested_feature_count": len(submitted_features),
                "requested_features": submitted_features,
                "auto_appended_feature_count": len(auto_appended_features),
                "auto_appended_features": auto_appended_features,
                "factor_selection": factor_selection_report,
                "features": valid_features,
                "feature_columns": valid_features,
                "fill_values": fill_values,
                "train_start": cfg["data"]["train_start"],
                "train_end":   cfg["data"]["train_end"],
                "val_start":   (cfg.get("split", {}).get("valid") or [None, None])[0] or "",
                "val_end":     (cfg.get("split", {}).get("valid") or [None, None])[1] or "",
                "test_start":  (cfg.get("split", {}).get("test")  or [None, None])[0] or "",
                "test_end":    (cfg.get("split", {}).get("test")  or [None, None])[1] or "",
                "data_source": "quantdb_factors" if factor_source else "parquet",
                "factor_source": factor_source or None,
                "factor_catalog_version": str((cfg.get("data", {}) or {}).get("factor_catalog_version") or "") or None,
                "factor_schema_hash": str((cfg.get("data", {}) or {}).get("factor_schema_hash") or "") or None,
                "quantdb_dir": str((cfg.get("data", {}) or {}).get("quantdb_dir") or "") or None,
                "factor_field_sources": (cfg.get("data", {}) or {}).get("factor_field_sources") or {},
                "factor_catalog_published_at": str((cfg.get("data", {}) or {}).get("factor_catalog_published_at") or "") or None,
                "factor_coverage": (cfg.get("data", {}) or {}).get("factor_coverage") or {},
                "context": context_cfg,
                "best_iteration": best_iteration,
                "target_horizon_days": int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1),
                "execution_lag_days": _EXECUTION_LAG_DAYS,
                "target_mode": str((cfg.get("label", {}) or {}).get("target_mode") or "return"),
                "preprocessing": (cfg.get("preprocessing") or {}) if (cfg.get("preprocessing") or {}).get("enabled") else None,
                "label_formula": str((cfg.get("label", {}) or {}).get("label_formula") or ""),
                "effective_trade_date": str((cfg.get("label", {}) or {}).get("effective_trade_date") or ""),
                "training_window": str((cfg.get("label", {}) or {}).get("training_window") or ""),
                "metrics": {
                    "train_ic": train_m["ic"], "train_rank_ic": train_m["rank_ic"], "train_rank_icir": train_m["rank_icir"],
                    "val_ic": val_m["ic"], "val_rank_ic": val_m["rank_ic"], "val_rank_icir": val_m["rank_icir"],
                    "test_ic": test_m["ic"], "test_rank_ic": test_m["rank_ic"], "test_rank_icir": test_m["rank_icir"],
                    "score_direction": val_m.get("score_direction", "normal"),
                },
                "pred_coverage_start": str(pred_df["trade_date"].min().date()) if not pred_df.empty else "",
                "pred_coverage_end": str(pred_df["trade_date"].max().date()) if not pred_df.empty else "",
                "pred_rows": int(len(pred_df)),
                "shap": shap_info,
                "generated_at": datetime.utcnow().isoformat(),
                "elapsed_seconds": elapsed,
            }
            if is_stacking:
                metadata["base_model_files"] = saved_models
                metadata["meta_model_file"] = "meta_model.pkl"
                metadata["n_folds"] = int(model_cfg.get("n_folds", 5))
                metadata["base_model_fill_values"] = multi_result.get("base_fill_values", {})
                metadata["fold_method"] = "expanding_window"
                metadata["meta_learner"] = "ridge"
            if dl_metadata:
                metadata.update(dl_metadata)

            metadata_bytes = json.dumps(_sanitize_nan_inf(metadata), ensure_ascii=False, indent=2).encode()
            (WORKSPACE_DIR / "metadata.json").write_bytes(metadata_bytes)
            logger.info("metadata.json saved locally")

            # 复制推理脚本模板
            template_path = Path("/app/backend/services/engine/inference/templates/inference_parquet.py")
            inference_dest = WORKSPACE_DIR / "inference.py"
            if template_path.is_file():
                inference_dest.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("inference.py copied from unified template: %s", template_path)

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
                    {"name": saved_models.get(primary_type, "model.lgb"), "local": f"/workspace/{saved_models.get(primary_type, 'model.lgb')}"},
                    {"name": "pred.parquet",  "local": "/workspace/pred.parquet"},
                    {"name": "metadata.json", "local": "/workspace/metadata.json"},
                    {"name": "inference.py",  "local": "/workspace/inference.py"},
                    {"name": "config.yaml",   "local": "/workspace/config.yaml"},
                    {"name": "result.json",   "local": "/workspace/result.json"},
                    {"name": "model_comparison.json", "local": "/workspace/model_comparison.json"},
                ] + [
                    {"name": f"pred_{mt}.parquet", "local": f"/workspace/pred_{mt}.parquet"}
                    for mt in multi_result["model_types"]
                ] + [
                    {"name": fn, "local": f"/workspace/{fn}"}
                    for fn in saved_models.values() if fn != saved_models.get(primary_type)
                ],
                "summary": {
                    "status": "Stacking集成训练完成" if is_stacking else "多模型训练完成",
                    "message": f"{'Stacking集成' if is_stacking else '训练'}完成({len(multi_result['model_types'])}个模型)，最佳={primary_type}，val_icir={val_m['rank_icir']:.4f}",
                },
                "metadata": metadata,
                "error": "",
                "logs": f"val_rmse={val_m['rmse']:.6f}, val_auc={val_m['auc']:.6f}, best={primary_type}",
            }
            if is_stacking:
                result["artifacts"].extend([
                    {"name": "meta_model.pkl", "local": "/workspace/meta_model.pkl"},
                    {"name": "oof_predictions.parquet", "local": "/workspace/oof_predictions.parquet"},
                ])
            if shap_info.get("status") == "completed" and (WORKSPACE_DIR / "shap_summary.csv").exists():
                result["artifacts"].append({"name": "shap_summary.csv", "local": "/workspace/shap_summary.csv"})

        else:
            # ── 单模型训练路径（向后兼容） ──
            train_result = train_model(df, valid_features, cfg, hardware=hardware)
            # train_model 返回 11-tuple (分位 LightGBM) / 10-tuple (树模型含 optuna)
            # / 9-tuple (DL 含 dl_metadata) / 8-tuple。
            quantile_result = None
            # 注意：train_model 各分支的元组尾部语义不同——
            #   11/10 元组: ..., dl_metadata, optuna_result, quantile_result（树模型）
            #   9 元组:     ..., model_type, dl_metadata（DL 模型）
            # 历史上 10 元组曾按 (..., dl_metadata, optuna_result) 解包，
            # 把含 LightGBM Booster 的 quantile_result 误当 optuna 结果，
            # 导致 metadata.json 序列化崩溃、训练被误判失败。
            if len(train_result) == 11:
                model, fill_values, train_m, val_m, test_m, pred_df, split_frames, actual_model_type, dl_metadata, optuna_result, quantile_result = train_result
            elif len(train_result) == 10:
                model, fill_values, train_m, val_m, test_m, pred_df, split_frames, actual_model_type, optuna_result, quantile_result = train_result
                dl_metadata = None
            elif len(train_result) == 9:
                model, fill_values, train_m, val_m, test_m, pred_df, split_frames, actual_model_type, dl_metadata = train_result
                optuna_result = None
            else:
                model, fill_values, train_m, val_m, test_m, pred_df, split_frames, actual_model_type = train_result
                dl_metadata = None
                optuna_result = None
            elapsed = float(time.time() - train_t0)

            # 获取 best_iteration（不同框架方式不同）
            best_iteration = getattr(model, "best_iteration", None)
            if best_iteration is None and hasattr(model, "get_best_iteration"):
                try:
                    best_iteration = model.get_best_iteration()
                except Exception:
                    best_iteration = None
            logger.info("Training finished in %.2fs, best_iteration=%s, model_type=%s", elapsed, best_iteration, actual_model_type)

            # 保存模型（多框架）
            workspace = WORKSPACE_DIR
            model_filename = _save_model(model, actual_model_type, workspace)
            logger.info(f"Model saved to {workspace / model_filename}")
            quantile_model_files: dict[str, str] = {}
            if quantile_result:
                for quantile_key, quantile_model in quantile_result["models"].items():
                    filename = f"model_{quantile_key}.lgb"
                    quantile_model.save_model(str(workspace / filename))
                    quantile_model_files[quantile_key] = filename
                # `model.lgb` remains P50 for every existing consumer.
                model_filename = quantile_model_files["p50"]
                logger.info("Quantile model artifacts saved: %s", quantile_model_files)

            # 保存预测结果（parquet 压缩用于存档，比 pickle 小 ~10x）
            pred_path = WORKSPACE_DIR / "pred.parquet"
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
            pred_pkl_path = WORKSPACE_DIR / "pred.pkl"
            pred_qlib.to_pickle(pred_pkl_path)
            logger.info(f"Backtest-compatible pred.pkl saved ({pred_pkl_path.stat().st_size/1024/1024:.1f} MB, {len(pred_qlib):,} rows)")

            shap_summary_path = WORKSPACE_DIR / "shap_summary.csv"
            # SHAP: pred_contrib 仅支持 LightGBM；其他框架暂跳过
            if actual_model_type != "lightgbm":
                explain_cfg_shap = {**explain_cfg, "enable_shap": False}
                logger.info("SHAP disabled: pred_contrib not supported for %s", actual_model_type)
            else:
                explain_cfg_shap = explain_cfg
            shap_info = _compute_shap_summary(
                model=model,
                split_frames=split_frames,
                features=valid_features,
                fill_values=fill_values,
                explain_cfg=explain_cfg_shap,
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
                "framework": _get_model_framework(actual_model_type),
                "model_type": actual_model_type,
                "model_file": model_filename,
                "seed": int((cfg.get("seed") or 42)),
                "hardware": hardware,
                "feature_count": len(valid_features),
                "requested_feature_count": len(submitted_features),
                "requested_features": submitted_features,
                "auto_appended_feature_count": len(auto_appended_features),
                "auto_appended_features": auto_appended_features,
                "factor_selection": factor_selection_report,
                "features": valid_features,
                "feature_columns": valid_features,
                "fill_values": fill_values,
                "train_start": cfg["data"]["train_start"],
                "train_end":   cfg["data"]["train_end"],
                "val_start":   (cfg.get("split", {}).get("valid") or [None, None])[0] or "",
                "val_end":     (cfg.get("split", {}).get("valid") or [None, None])[1] or "",
                "test_start":  (cfg.get("split", {}).get("test")  or [None, None])[0] or "",
                "test_end":    (cfg.get("split", {}).get("test")  or [None, None])[1] or "",
                "data_source": "quantdb_factors" if factor_source else "parquet",
                "factor_source": factor_source or None,
                "factor_catalog_version": str((cfg.get("data", {}) or {}).get("factor_catalog_version") or "") or None,
                "factor_schema_hash": str((cfg.get("data", {}) or {}).get("factor_schema_hash") or "") or None,
                "quantdb_dir": str((cfg.get("data", {}) or {}).get("quantdb_dir") or "") or None,
                "factor_field_sources": (cfg.get("data", {}) or {}).get("factor_field_sources") or {},
                "factor_catalog_published_at": str((cfg.get("data", {}) or {}).get("factor_catalog_published_at") or "") or None,
                "factor_coverage": (cfg.get("data", {}) or {}).get("factor_coverage") or {},
                "context": context_cfg,
                "best_iteration": best_iteration,
                "target_horizon_days": int((cfg.get("label", {}) or {}).get("target_horizon_days") or 1),
                "execution_lag_days": _EXECUTION_LAG_DAYS,
                "target_mode": str((cfg.get("label", {}) or {}).get("target_mode") or "return"),
                "prediction_mode": "quantile" if quantile_result else "point",
                "preprocessing": (cfg.get("preprocessing") or {}) if (cfg.get("preprocessing") or {}).get("enabled") else None,
                "label_formula": str((cfg.get("label", {}) or {}).get("label_formula") or ""),
                "effective_trade_date": str((cfg.get("label", {}) or {}).get("effective_trade_date") or ""),
                "training_window": str((cfg.get("label", {}) or {}).get("training_window") or ""),
                "metrics": {
                    "train_ic": train_m["ic"], "train_rank_ic": train_m["rank_ic"], "train_rank_icir": train_m["rank_icir"],
                    "val_ic": val_m["ic"], "val_rank_ic": val_m["rank_ic"], "val_rank_icir": val_m["rank_icir"],
                    "test_ic": test_m["ic"], "test_rank_ic": test_m["rank_ic"], "test_rank_icir": test_m["rank_icir"],
                    "score_direction": val_m.get("score_direction", "normal"),
                },
                "pred_coverage_start": str(pred_df["trade_date"].min().date()) if not pred_df.empty else "",
                "pred_coverage_end": str(pred_df["trade_date"].max().date()) if not pred_df.empty else "",
                "pred_rows": int(len(pred_df)),
                "shap": shap_info,
                "generated_at": datetime.utcnow().isoformat(),
                "elapsed_seconds": elapsed,
            }
            # Optuna 搜索结果写入 metadata（若启用）
            if optuna_result:
                metadata["optuna"] = optuna_result
            if quantile_result:
                metadata.update({
                    "prediction_contract": {
                        "kind": "quantile_return",
                        "quantiles": list(_QUANTILE_LEVELS),
                        "primary_score": "p50",
                    },
                    "quantile_models": quantile_model_files,
                    "calibration": quantile_result["calibration"],
                })
            # DL 模型特有元数据 (model_class_name, model_params, input_spec 等)
            if dl_metadata:
                metadata.update(dl_metadata)

            metadata_bytes = json.dumps(_sanitize_nan_inf(metadata), ensure_ascii=False, indent=2).encode()
            (WORKSPACE_DIR / "metadata.json").write_bytes(metadata_bytes)
            logger.info("metadata.json saved locally")

            # 复制统一推理脚本模板（而非内联生成旧版脚本）
            template_path = Path("/app/backend/services/engine/inference/templates/inference_parquet.py")
            inference_dest = WORKSPACE_DIR / "inference.py"
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
适用于训练数据来自 feature_snapshots/*.parquet 的 LightGBM/XGBoost 模型。

平台注入环境变量：
    MODEL_DIR      模型目录绝对路径（含 metadata.json + model.lgb/model.xgb）
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
import pickle
import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
except ImportError:
    lgb = None
try:
    import xgboost as xgb
except ImportError:
    xgb = None
try:
    from catboost import CatBoost
except ImportError:
    CatBoost = None
try:
    import torch
except ImportError:
    torch = None

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
    model_file = meta.get("model_file", "")
    model_path = Path(model_dir) / model_file if model_file else None
    if not model_path or not model_path.exists():
        for ext in ("*.xgb", "*.lgb", "*.cbm", "*.pkl", "*.pth", "*.pt", "*.txt", "*.bin"):
            candidates = list(Path(model_dir).glob(ext))
            if candidates:
                model_path = candidates[0]; break
        else:
            logger.error("未找到模型文件: %s", model_dir); sys.exit(1)
    suffix = model_path.suffix.lower()
    logger.info("加载模型: %s (格式=%s)", model_path.name, suffix)
    if suffix == ".xgb":
        if xgb is None: logger.error("XGBoost 未安装"); sys.exit(1)
        booster = xgb.Booster(); booster.load_model(str(model_path)); return ("xgb", booster)
    elif suffix == ".cbm":
        if CatBoost is None: logger.error("CatBoost 未安装"); sys.exit(1)
        m = CatBoost(); m.load_model(str(model_path), format="cbm"); return ("catboost", m)
    elif suffix == ".pkl":
        with open(model_path, "rb") as f: m = pickle.load(f)
        return ("sklearn", m)
    elif suffix in (".pth", ".pt"):
        if torch is None: logger.error("PyTorch 未安装"); sys.exit(1)
        model_class_name = meta.get("model_class_name")
        _QLIB_MAP = {"GRU":("qlib.contrib.model.pytorch_gru_ts","GRU"),"LSTM":("qlib.contrib.model.pytorch_lstm_ts","LSTM"),"ALSTM":("qlib.contrib.model.pytorch_alstm_ts","ALSTM"),"Transformer":("qlib.contrib.model.pytorch_transformer_ts","Transformer"),"TCN":("qlib.contrib.model.pytorch_tcn_ts","TCN"),"TabNet":("qlib.contrib.model.pytorch_tabnet","TabNet")}
        if model_class_name and model_class_name in _QLIB_MAP:
            import importlib
            mod_path, cls_name = _QLIB_MAP[model_class_name]
            mod = importlib.import_module(mod_path); ModelCls = getattr(mod, cls_name)
            infer_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
            logger.info("DL 推理模型 %s device: %s", cls_name, infer_device)
            mp = dict(meta.get("model_params", {})); mp["GPU"] = 0 if torch.cuda.is_available() else -1
            model_obj = ModelCls(**mp)
            sd = torch.load(str(model_path), map_location="cpu", weights_only=True)
            inner = getattr(model_obj, "model", None)
            if inner is None:
                for a in ("gru_model","lstm_model","alstm_model","transformer_model","tcn_model","tabnet_model"):
                    inner = getattr(model_obj, a, None)
                    if inner is not None: break
            if inner is not None and sd is not None:
                inner.load_state_dict(sd); inner.eval(); inner.to(infer_device)
            model_obj.device = infer_device  # 供调用方前向搬张量
            model_obj.fitted = True; return ("torch_qlib", model_obj)
        m = torch.load(str(model_path), map_location="cpu", weights_only=False)
        if hasattr(m, "eval"): m.eval()
        infer_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        if torch.cuda.is_available():
            m.to(infer_device)
        m.device = infer_device  # 供调用方前向搬张量
        return ("torch", m)
    else:
        if lgb is None: logger.error("LightGBM 未安装"); sys.exit(1)
        return ("lgb", lgb.Booster(model_file=str(model_path)))

_MARKET_PARQUET = {"HK": "model_features_hk.parquet", "US": "model_features_us.parquet", "CRYPTO": "model_features_crypto.parquet", "FUTURES": "model_features_futures.parquet"}

def load_date_data(trade_date, data_dir, meta):
    market = str((meta.get("context") or {}).get("market", "")).upper()
    if market in _MARKET_PARQUET:
        parquet_path = Path(data_dir) / _MARKET_PARQUET[market]
    else:
        year = int(trade_date[:4])
        parquet_path = Path(data_dir) / f"model_features_{year}.parquet"
    if not parquet_path.exists():
        logger.warning("parquet 文件不存在: %s", parquet_path); return None
    df = pd.read_parquet(parquet_path, engine="pyarrow")
    if "symbol" not in df.columns and "instrument" in df.columns:
        df = df.rename(columns={"instrument": "symbol"})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    day_df = df[df["trade_date"] == trade_date].copy()
    if len(day_df) == 0:
        logger.warning("日期 %s 无数据", trade_date); return None
    # 过滤不可交易（停牌、零成交、ST）
    if "close" in day_df.columns:
        day_df = day_df[pd.to_numeric(day_df["close"], errors="coerce") > 0]
    if "volume" in day_df.columns:
        day_df = day_df[pd.to_numeric(day_df["volume"], errors="coerce") > 0]
    if "is_st" in day_df.columns:
        day_df = day_df[pd.to_numeric(day_df["is_st"], errors="coerce") != 1]
    if len(day_df) == 0:
        logger.warning("日期 %s 过滤后无数据", trade_date); return None
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
    model_type, model = load_model(model_dir, meta)
    X_df, symbols = preprocess(day_df, meta)
    if len(X_df) == 0:
        print(f"日期 {trade_date} 预处理后无有效行", file=sys.stderr); sys.exit(2)
    X_values = X_df.values.astype(np.float32)
    best_iter = meta.get("best_iteration")
    if model_type == "xgb":
        dmat = xgb.DMatrix(X_values, feature_names=list(X_df.columns))
        scores = model.predict(dmat, iteration_range=(0, best_iter) if best_iter else None)
    elif model_type == "catboost":
        scores = model.predict(X_values)
    elif model_type == "sklearn":
        scores = model.predict_proba(X_values)[:, 1] if hasattr(model, "predict_proba") else model.predict(X_values)
    elif model_type in ("torch_qlib", "torch"):
        inner = model
        if model_type == "torch_qlib":
            inner = getattr(model, "model", None)
            if inner is None:
                for a in ("gru_model","lstm_model","alstm_model","transformer_model","tcn_model","tabnet_model"):
                    inner = getattr(model, a, None)
                    if inner is not None: break
            if inner is None: logger.error("DL 内部模型未找到"); sys.exit(1)
        inner.eval()
        xt = torch.from_numpy(X_values)
        dev = getattr(model, "device", None) or getattr(inner, "device", None)
        if dev is not None: xt = xt.to(dev)
        with torch.no_grad(): pred = inner(xt).detach().cpu().numpy()
        scores = pred.flatten()
    else:
        scores = model.predict(X_values, num_iteration=best_iter)
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
                    {"name": model_filename,  "local": f"/workspace/{model_filename}"},
                    {"name": "pred.parquet",  "local": "/workspace/pred.parquet"},
                    {"name": "metadata.json", "local": "/workspace/metadata.json"},
                    {"name": "inference.py",  "local": "/workspace/inference.py"},
                    {"name": "config.yaml",   "local": "/workspace/config.yaml"},
                    {"name": "result.json",   "local": "/workspace/result.json"},
                ] + [
                    {"name": filename, "local": f"/workspace/{filename}"}
                    for filename in quantile_model_files.values()
                    if filename != model_filename
                ],
                "summary": {
                    "status": "训练完成",
                    "message": f"训练完成({actual_model_type})，best_iteration={best_iteration}，产物已保存到本地模型目录",
                },
                "metadata": metadata,
                "error": "",
                "logs": f"val_rmse={val_m['rmse']:.6f}, val_auc={val_m['auc']:.6f}",
            }
            if shap_info.get("status") == "completed" and shap_summary_path.exists():
                result["artifacts"].append({"name": "shap_summary.csv", "local": "/workspace/shap_summary.csv"})

        # ── 注入 WFA 诊断结果到 result 与 metadata ──
        if wfa_result.get("enabled"):
            result["wfa"] = wfa_result
            if isinstance(result.get("metadata"), dict):
                result["metadata"]["wfa"] = wfa_result
            logger.info(
                "WFA diagnosis attached: %d windows, IC mean=%.4f std=%.4f",
                len(wfa_result.get("windows", [])),
                wfa_result.get("ic_mean", float("nan")),
                wfa_result.get("ic_std", float("nan")),
            )

        # ── 注入数据漂移检测（PSI）结果 ──
        if psi_result.get("enabled"):
            result["drift"] = psi_result
            if isinstance(result.get("metadata"), dict):
                result["metadata"]["drift"] = psi_result

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.exception(f"Training failed: {e}")
        result = {"status": "failed", "run_id": run_id, "error": str(e), "traceback": tb}

    finally:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_clean = _sanitize_nan_inf(result)
        result_json = json.dumps(result_clean, ensure_ascii=False, indent=2)
        result_path.write_text(result_json)
        logger.info(f"result.json → {result_path}")

        if callback_url:
            try:
                resp = requests.post(
                    callback_url, json=result_clean,
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
