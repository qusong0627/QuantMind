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
        # inplace_predict：直接从 numpy 预测，避免为每次预测构建整份 DMatrix
        # （train/val/test/全窗口共 4 次调用、每次整表复制 7~14GB——2016 直读
        # 窗口下是预测阶段瞬时击中 48G 容器限额的放大器，OOMKilled 实证）。
        n_iter = model.best_iteration
        return model.inplace_predict(
            X, iteration_range=(0, (n_iter + 1) if n_iter is not None else 0)
        )
    elif model_type == "catboost":
        pred = model.predict_proba(X) if hasattr(model, "predict_proba") else model.predict(X)
    else:
        # sklearn 系（Ridge/RF/MLP）：.predict 内部 check_array 会整份复制输入
        # （640 万行 × 273 列 float32 ≈ 7GB 副本）——2026-09-16 ML8 批次 MLP
        # 训练完成后在预测处 OOMKilled 实证。分块预测把副本压到 0.5GB。
        _n = len(X)
        _step = 500_000
        if _n > _step:
            _parts = []
            for _i in range(0, _n, _step):
                _chunk = X[_i : _i + _step]
                _r = (
                    model.predict_proba(_chunk)
                    if hasattr(model, "predict_proba")
                    else model.predict(_chunk)
                )
                _parts.append(np.asarray(_r))
            pred = np.concatenate(_parts, axis=0)
        else:
            pred = model.predict_proba(X) if hasattr(model, "predict_proba") else model.predict(X)

    # sklearn/CatBoost 分类器默认 predict() 返回硬标签；选股排序与 AUC 均应使用
    # 正类概率，避免把大量样本压成 0/1 并丢失排序信息。
    pred_arr = np.asarray(pred)
    if pred_arr.ndim == 2 and pred_arr.shape[1] >= 2:
        return pred_arr[:, 1]
    return pred_arr.reshape(-1)
