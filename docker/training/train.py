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
import os as _qm_os
_QM_WS = _qm_os.environ.get("TRAINING_WORKSPACE_DIR") or "/workspace"

import argparse
import gc
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
import torch
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("quantmind.train")


# ── B4 拆包：类型集合/注册表/分派与训练器实现见 docker/training/model_trainers/ ──
# （训练容器经挂载与镜像 COPY 双通道同步；本地见编排器 volumes，远端见 rsync。）
from model_trainers.metrics import _compute_metrics
from model_trainers.registry import (
    _ALL_MODEL_TYPES,
    _DL_MODEL_TYPES,
    _dispatch_dl,
    _dispatch_gbdt_sklearn,
)
from model_trainers.trainers_gbdt import (
    DEFAULT_LGB_PARAMS,
    _train_catboost,
    _train_lgb,
    _train_xgb,
)
# P1 拆包：诊断/小工具（与 model_trainers 同级，训练容器三通道同步）。
# 经此 import，train.compute_psi_drift 等名字保持可用，旧测试零改动。
from diagnostics.drift import compute_psi_drift
from diagnostics.explain import _compute_shap_summary, _normalize_explain_cfg
from diagnostics.utils import _sanitize_nan_inf, detect_hardware
from diagnostics.wfa import train_wfa
from data.factor_selection import _log_factor_selection_summary, select_top_factors
from data.loading import load_data
from data.splits import _EXECUTION_LAG_DAYS, _prepare_arrays, _split_data
from model_trainers.predict import _predict_with_model


TRAINING_BASE_FEATURES: list[str] = [
    "mom_ret_1d",
    "mom_ret_5d",
    "mom_ret_20d",
    "liq_volume",
    "liq_amount",
    "fun_turnover_1",
]
# ── 训练 ──────────────────────────────────────────────────────────────────────

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


# ── 深度学习训练 ────────────────────────────────────────────────────────────────

# Qlib TS 模型映射: model_type → (qlib_module, qlib_class)


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
        # DL 模型在 _train_dl() 中已保存 model.pth，此处仅返回文件名
        return "model.pth"
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
        else:
            # B2：树组查表分派（optuna 预搜索与分位分支保持原逻辑）
            model = _dispatch_gbdt_sklearn(cfg, model_type, features, X_train, y_train, X_val, y_val)
    elif model_type in ("linear", "random_forest", "mlp"):
        model = _dispatch_gbdt_sklearn(cfg, model_type, features, X_train, y_train, X_val, y_val)
    elif model_type == "nativetft":
        return _dispatch_dl(cfg, model_type, features, train_df, val_df, test_df, df, fill_values, hardware, single=False, t_start=train_t0)
    elif model_type in _DL_MODEL_TYPES:
        return _dispatch_dl(cfg, model_type, features, train_df, val_df, test_df, df, fill_values, hardware, single=False, t_start=train_t0)
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
        # B2：树组查表分派（optuna 预搜索保持原逻辑）
        model = _dispatch_gbdt_sklearn(cfg, model_type, features, X_train, y_train, X_val, y_val)
    elif model_type in ("linear", "random_forest", "mlp"):
        model = _dispatch_gbdt_sklearn(cfg, model_type, features, X_train, y_train, X_val, y_val)
    elif model_type == "nativetft":
        return _dispatch_dl(cfg, model_type, features, train_df, val_df, test_df, df, fill_values, hardware, single=True, t_start=t0)
    elif model_type in _DL_MODEL_TYPES:
        return _dispatch_dl(cfg, model_type, features, train_df, val_df, test_df, df, fill_values, hardware, single=True, t_start=t0)
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
    oof_path = Path(_QM_WS) / "oof_predictions.parquet"
    oof_df.to_parquet(oof_path, engine="pyarrow", compression="zstd", index=False)
    logger.info("OOF predictions saved to %s", oof_path)

    # 保存元学习器
    import pickle
    meta_model_path = Path(_QM_WS) / "meta_model.pkl"
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
    result_path = Path(_QM_WS) / "result.json"

    try:
        if not cfg_path.exists():
            raise RuntimeError(f"Config file not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text())

        # B1 配置校验门：与 api 侧同一 TrainingConfig 双端复用，非法枚举等纯输入
        # 错误在启动秒级失败，而不是数据加载后才抛。校验只做 gate，不改写 cfg，
        # 下游逻辑零改动。兼容镜像（无 backend/shared）降级跳过。
        try:
            from backend.shared.training.schemas import TrainingConfig

            TrainingConfig.from_dict(cfg)
        except ImportError:
            logger.warning("training schemas unavailable (compat image); skip config validation")
        except Exception as exc:
            raise RuntimeError(f"Invalid training config: {exc}") from exc

        run_id          = cfg.get("run_id", "unknown")
        job_name        = cfg.get("job_name", "unnamed")
        result_path     = Path(cfg.get("output", {}).get("result_path", _qm_os.path.join(_QM_WS, "result.json")))
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
            workspace = Path(_QM_WS)
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
            pred_path = Path(_QM_WS) / "pred.parquet"
            pred_df.to_parquet(pred_path, engine="pyarrow", compression="zstd", index=False)
            logger.info(f"Predictions saved to {pred_path}")

            pred_qlib = (
                pred_df[["trade_date", "symbol", "pred"]]
                .rename(columns={"trade_date": "datetime", "symbol": "instrument", "pred": "score"})
                .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
                .set_index(["datetime", "instrument"])
                .sort_index()
            )
            pred_pkl_path = Path(_QM_WS) / "pred.pkl"
            pred_qlib.to_pickle(pred_pkl_path)
            logger.info(f"Backtest-compatible pred.pkl saved ({len(pred_qlib):,} rows)")

            # 保存对比报告
            comparison_path = workspace / "model_comparison.json"
            comparison_path.write_text(json.dumps(multi_result["comparison"], ensure_ascii=False, indent=2, default=str))

            # SHAP（仅 LightGBM 基模型）
            shap_info: dict[str, Any] = {"enabled": False, "status": "disabled"}
            if "lightgbm" in multi_result["models"]:
                lgb_res = multi_result["models"]["lightgbm"]
                shap_summary_path = Path(_QM_WS) / "shap_summary.csv"
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
            (Path(_QM_WS) / "metadata.json").write_bytes(metadata_bytes)
            logger.info("metadata.json saved locally")

            # 复制推理脚本模板
            template_path = Path(_qm_os.getenv("TRAINING_INFERENCE_TEMPLATE") or "/app/backend/services/engine/inference/templates/inference_parquet.py")
            inference_dest = Path(_QM_WS) / "inference.py"
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
                    {"name": saved_models.get(primary_type, "model.lgb"), "local": f"{_QM_WS}/{saved_models.get(primary_type, 'model.lgb')}"},
                    {"name": "pred.parquet",  "local": str(Path(_QM_WS) / "pred.parquet")},
                    {"name": "metadata.json", "local": str(Path(_QM_WS) / "metadata.json")},
                    {"name": "inference.py",  "local": str(Path(_QM_WS) / "inference.py")},
                    {"name": "config.yaml",   "local": f"{_QM_WS}/config.yaml"},
                    {"name": "result.json",   "local": f"{_QM_WS}/result.json"},
                    {"name": "model_comparison.json", "local": f"{_QM_WS}/model_comparison.json"},
                ] + [
                    {"name": f"pred_{mt}.parquet", "local": f"{_QM_WS}/pred_{mt}.parquet"}
                    for mt in multi_result["model_types"]
                ] + [
                    {"name": fn, "local": f"{_QM_WS}/{fn}"}
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
                    {"name": "meta_model.pkl", "local": f"{_QM_WS}/meta_model.pkl"},
                    {"name": "oof_predictions.parquet", "local": f"{_QM_WS}/oof_predictions.parquet"},
                ])
            if shap_info.get("status") == "completed" and (Path(_QM_WS) / "shap_summary.csv").exists():
                result["artifacts"].append({"name": "shap_summary.csv", "local": f"{_QM_WS}/shap_summary.csv"})

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
            workspace = Path(_QM_WS)
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
            pred_path = Path(_QM_WS) / "pred.parquet"
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
            pred_pkl_path = Path(_QM_WS) / "pred.pkl"
            pred_qlib.to_pickle(pred_pkl_path)
            logger.info(f"Backtest-compatible pred.pkl saved ({pred_pkl_path.stat().st_size/1024/1024:.1f} MB, {len(pred_qlib):,} rows)")

            shap_summary_path = Path(_QM_WS) / "shap_summary.csv"
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
            (Path(_QM_WS) / "metadata.json").write_bytes(metadata_bytes)
            logger.info("metadata.json saved locally")

            # 复制统一推理脚本模板（而非内联生成旧版脚本）
            template_path = Path(_qm_os.getenv("TRAINING_INFERENCE_TEMPLATE") or "/app/backend/services/engine/inference/templates/inference_parquet.py")
            inference_dest = Path(_QM_WS) / "inference.py"
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
                    {"name": model_filename,  "local": f"{_QM_WS}/{model_filename}"},
                    {"name": "pred.parquet",  "local": str(Path(_QM_WS) / "pred.parquet")},
                    {"name": "metadata.json", "local": str(Path(_QM_WS) / "metadata.json")},
                    {"name": "inference.py",  "local": str(Path(_QM_WS) / "inference.py")},
                    {"name": "config.yaml",   "local": f"{_QM_WS}/config.yaml"},
                    {"name": "result.json",   "local": f"{_QM_WS}/result.json"},
                ] + [
                    {"name": filename, "local": f"{_QM_WS}/{filename}"}
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
                result["artifacts"].append({"name": "shap_summary.csv", "local": f"{_QM_WS}/shap_summary.csv"})

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
