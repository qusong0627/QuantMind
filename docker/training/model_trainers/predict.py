"""统一预测接口（P2 由 train.py 拆出，逐行搬运）。

适配各框架 predict 语义：分类器取正类概率（选股排序与 AUC 口径），
回归取原始输出。WFA 与主流程共用。
"""
from __future__ import annotations

from typing import Any

import numpy as np


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
