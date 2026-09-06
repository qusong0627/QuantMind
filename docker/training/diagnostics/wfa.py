"""WFA 稳定性诊断（P2 由 train.py 拆出，逐行搬运）。

仅支持树模型 + linear（其余 warning+None），语义与注册表分派不同，
保持直调（见 test_training_registry_dispatch 锁定）。
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from data.splits import _prepare_arrays
from model_trainers.metrics import _compute_metrics
from model_trainers.predict import _predict_with_model
from model_trainers.registry import _DL_MODEL_TYPES
from model_trainers.trainers_gbdt import (
    _train_catboost,
    _train_lgb,
    _train_linear,
    _train_xgb,
)

logger = logging.getLogger("quantmind.train")


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
