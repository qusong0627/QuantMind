"""Kronos A股零样本测试：截面指标与基线（纯函数）。"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from backend.scripts.stock_classifier.panel import rankdata_nan

MIN_CROSS_SECTION = 20


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    xd = x - x.mean()
    yd = y - y.mean()
    denom = float(np.sqrt((xd * xd).sum() * (yd * yd).sum()))
    return float((xd * yd).sum() / denom) if denom > 0 else float("nan")


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """不依赖 scipy 的 Spearman：先平均秩，再算 Pearson。"""
    rx = rankdata_nan(x)
    ry = rankdata_nan(y)
    ok = np.isfinite(rx) & np.isfinite(ry)
    if ok.sum() < MIN_CROSS_SECTION:
        return float("nan")
    return _pearson(rx[ok], ry[ok])


def cross_section_metrics(
    scores: pd.Series,
    fwd_ret: pd.Series,
    topk: int = 20,
    tradable: pd.Series | None = None,
) -> dict[str, float]:
    """单日截面指标：Pearson IC、Spearman RankIC、TopK 超额与胜率。"""
    df = pd.concat(
        [scores.rename("score"), fwd_ret.rename("fwd")], axis=1, join="inner"
    ).dropna()
    if tradable is not None:
        df = df.join(tradable.rename("tradable"), how="inner")
        df = df[df["tradable"].fillna(False).astype(bool)]
    if len(df) < MIN_CROSS_SECTION:
        return _empty_metrics()

    ic = _pearson(df["score"].to_numpy(dtype=float), df["fwd"].to_numpy(dtype=float))
    rank_ic = _spearman(df["score"].to_numpy(dtype=float), df["fwd"].to_numpy(dtype=float))
    k = max(1, min(int(topk), len(df)))
    top = df.nlargest(k, "score")
    pool_mean = float(df["fwd"].mean())
    top_mean = float(top["fwd"].mean())
    excess = top_mean - pool_mean
    return {
        "n": float(len(df)),
        "ic": ic,
        "rank_ic": rank_ic,
        "topk_excess": excess,
        "topk_hit": 1.0 if excess > 0 else 0.0,
        "topk_mean": top_mean,
        "pool_mean": pool_mean,
    }


def _empty_metrics() -> dict[str, float]:
    return {
        "n": 0.0,
        "ic": float("nan"),
        "rank_ic": float("nan"),
        "topk_excess": float("nan"),
        "topk_hit": float("nan"),
        "topk_mean": float("nan"),
        "pool_mean": float("nan"),
    }


def summarize(per_date: Iterable[dict[str, float]]) -> dict[str, float]:
    """把逐日指标汇总为均值/标准差/ICIR/胜率。"""
    rows = list(per_date)
    out: dict[str, float] = {"n_dates": float(len(rows))}
    for key in ("ic", "rank_ic", "topk_excess", "topk_hit"):
        vals = np.array([r.get(key, np.nan) for r in rows], dtype=float)
        vals = vals[np.isfinite(vals)]
        if len(vals) == 0:
            out[f"{key}_mean"] = float("nan")
            out[f"{key}_std"] = float("nan")
            out[f"{key}_icir"] = float("nan")
            continue
        mean = float(vals.mean())
        std = float(vals.std(ddof=1)) if len(vals) > 1 else float("nan")
        out[f"{key}_mean"] = mean
        out[f"{key}_std"] = std
        out[f"{key}_icir"] = (
            float(mean / std * np.sqrt(len(vals))) if std and np.isfinite(std) else float("nan")
        )
    return out


def baseline_score(
    panel: pd.DataFrame,
    signal_date: pd.Timestamp,
    lookback: int = 20,
    kind: str = "momentum",
) -> float:
    """朴素基线：过去 ``lookback`` 日动量；``reversal`` 取负。"""
    if "close" not in panel.columns:
        return float("nan")
    idx = panel.index
    pos = int(idx.get_indexer([pd.Timestamp(signal_date)])[0])
    if pos < 0 or pos - lookback < 0:
        return float("nan")
    past = float(panel["close"].iloc[pos - lookback])
    now = float(panel["close"].iloc[pos])
    if not (np.isfinite(past) and np.isfinite(now) and past > 0):
        return float("nan")
    mom = now / past - 1.0
    if kind == "momentum":
        return mom
    if kind == "reversal":
        return -mom
    raise ValueError(f"unknown baseline kind: {kind}")
