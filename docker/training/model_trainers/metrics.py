"""训练指标：IC/RankIC/全集 metrics（B4 由 train.py 拆出，无状态纯函数）。"""
from __future__ import annotations
import numpy as np
import pandas as pd

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
