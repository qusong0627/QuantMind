"""波动率目标模型：与收益模型同特征、同切分，输出 ``pred_vol`` 风险分数。

定位（2026-11-16）：KiT 样本外审计显示方向 alpha 对股票池不稳，但
vol RankIC 跨尺度维持在 0.5~0.6，是更稳的输出。收益模型之外再训一个
已实现波动率目标模型，只作为风险层输出，不改主模型分数、不参与排序。

设计约束：
- **一次运行只训一个 vol 模型**（不按 model_type 复制），因为风险输出与
  收益模型族无关；多模型/集成路径共用同一份切分。
- 目标列 ``label_vol`` 由 ``data/loading.py`` 在标签构建阶段写入；缺失时
  整体优雅降级（``enabled=False``），不阻断主训练。
- 模型固定 LightGBM（训练镜像已依赖），件名为 ``vol_model.txt``，与主模型
  同目录落盘，供推理/回测侧按需加载。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("training.vol_model")

VOL_LABEL = "label_vol"
VOL_MODEL_FILENAME = "vol_model.txt"
_PRED_CHUNK = 1_000_000
# 风险模型不需要全量样本；限行数把训练期内存从「全帧 numpy 副本」压到 ~1.2GB。
_MAX_TRAIN_ROWS = 2_000_000
_MAX_EVAL_ROWS = 500_000


def _sample_rows(df: pd.DataFrame, limit: int, seed: int) -> pd.DataFrame:
    if len(df) <= int(limit):
        return df
    return df.sample(n=int(limit), random_state=int(seed)).reset_index(drop=True)


def _require_lightgbm():
    try:
        import lightgbm as lgb
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "lightgbm is required for the volatility target model"
        ) from exc
    return lgb


def _fit_vol_model(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    features: list[str],
    *,
    seed: int = 42,
    n_estimators: int = 300,
):
    lgb = _require_lightgbm()
    X_train = train_df[features].to_numpy(dtype=np.float32, copy=False)
    y_train = train_df[VOL_LABEL].to_numpy(dtype=np.float32, copy=False)
    X_val = val_df[features].to_numpy(dtype=np.float32, copy=False)
    y_val = val_df[VOL_LABEL].to_numpy(dtype=np.float32, copy=False)
    model = lgb.LGBMRegressor(
        objective="regression",
        n_estimators=int(n_estimators),
        learning_rate=0.05,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        random_state=int(seed),
        n_jobs=4,
        verbosity=-1,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="l2",
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    return model


def _predict_frame(
    model: Any,
    frame: pd.DataFrame,
    features: list[str],
    *,
    chunk: int = _PRED_CHUNK,
) -> np.ndarray:
    """分块预测，避免整帧 numpy 副本与 LightGBM 内部放大叠加。"""
    out = np.empty(len(frame), dtype=np.float64)
    if not len(frame):
        return out
    X = frame[features]
    step = max(1, int(chunk))
    for start in range(0, len(frame), step):
        stop = min(start + step, len(frame))
        out[start:stop] = model.predict(
            X.iloc[start:stop].to_numpy(dtype=np.float32, copy=False)
        )
    return out


def _metrics(df: pd.DataFrame, y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    from model_trainers.metrics import _compute_metrics

    return _compute_metrics(
        df,
        np.asarray(y_true, dtype=np.float64),
        np.asarray(y_pred, dtype=np.float64),
    )


def train_vol_model(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    full_df: pd.DataFrame,
    features: list[str],
    out_dir: str | Path,
    *,
    seed: int = 42,
    n_estimators: int = 300,
) -> dict[str, Any]:
    """训练波动率目标模型，返回预测帧/指标/落盘路径。

    ``train_df/val_df/test_df`` 必须已按主模型同样的 embargo 切分；``full_df``
    用于生成全窗口 ``pred_vol``（与主模型 ``pred_df`` 同键：symbol/trade_date）。
    """
    if VOL_LABEL not in train_df.columns or VOL_LABEL not in val_df.columns:
        return {
            "enabled": False,
            "reason": f"{VOL_LABEL} missing from train/valid frames",
        }
    # 只保留数值特征：行业编码等类别列由 CatBoost cat_features 处理，LightGBM
    # 这条旁路不做独热/类别声明，直接跳过，避免 to_numpy(float) 抛错。
    feats = [
        f
        for f in features
        if f in train_df.columns
        and pd.api.types.is_numeric_dtype(train_df[f])
    ]
    if not feats:
        return {"enabled": False, "reason": "no usable numeric feature columns"}
    train_df = train_df[np.isfinite(train_df[VOL_LABEL])].reset_index(drop=True)
    val_df = val_df[np.isfinite(val_df[VOL_LABEL])].reset_index(drop=True)
    if len(train_df) < 100 or len(val_df) < 10:
        return {
            "enabled": False,
            "reason": f"too few finite {VOL_LABEL} rows (train={len(train_df)}, valid={len(val_df)})",
        }
    if len(test_df):
        test_df = test_df[np.isfinite(test_df[VOL_LABEL])].reset_index(drop=True)
    train_df = _sample_rows(train_df, _MAX_TRAIN_ROWS, seed)
    val_df = _sample_rows(val_df, _MAX_EVAL_ROWS, seed)
    if len(test_df):
        test_df = _sample_rows(test_df, _MAX_EVAL_ROWS, seed)

    model = _fit_vol_model(
        train_df, val_df, feats, seed=seed, n_estimators=n_estimators
    )
    train_pred = _predict_frame(model, train_df, feats)
    val_pred = _predict_frame(model, val_df, feats)
    test_pred = _predict_frame(model, test_df, feats) if len(test_df) else np.empty(0)
    metrics = {
        "train": _metrics(train_df, train_df[VOL_LABEL].to_numpy(), train_pred),
        "valid": _metrics(val_df, val_df[VOL_LABEL].to_numpy(), val_pred),
        "test": (
            _metrics(test_df, test_df[VOL_LABEL].to_numpy(), test_pred)
            if len(test_df)
            else {}
        ),
        "label": VOL_LABEL,
        "n_features": len(feats),
        "best_iteration": int(getattr(model, "best_iteration_", 0) or 0),
    }
    logger.info(
        "Vol model trained: n_features=%d best_iter=%s valid_rank_ic=%s",
        len(feats),
        metrics["best_iteration"],
        (metrics["valid"] or {}).get("rank_ic"),
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / VOL_MODEL_FILENAME
    # LGBMRegressor 的 booster_ 是原生 Booster，文本格式跨进程可读。
    model.booster_.save_model(str(model_path))

    full_pred = _predict_frame(model, full_df, feats)
    pred_df = full_df[["symbol", "trade_date"]].copy()
    pred_df["pred_vol"] = full_pred
    return {
        "enabled": True,
        "model": model,
        "model_path": str(model_path),
        "metrics": metrics,
        "pred_df": pred_df,
    }
