"""指数多周期预测：P1（量能因子 + 分周期建模）与 P2（概率化输出）。

依据 ``breadth_regime`` 的实测结论（固定 holdout 2024+）：
- T+20：广度 + 量能（成交额/20 日均值、成交额冲击）最稳，中证1000 的 OOS IC
  从 0.07 提到 0.12（滚动）/ 0.11 提到 0.18（holdout）；
- T+5：短周期组合（波动、反转、量能、冲击）最好，中证1000 IC 0.16；
- T+10：介于两者之间，用量能+广度；
- 横截面扩散度、两融、20/60 日动量：**加进模型无增量**，不进特征集。

输出一律概率化：点预测只作参考，主指标是「上涨概率」与「八成区间」，
两者都用同一段历史的样本外残差校准，不做拍脑袋的假设分布。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backend.scripts.breadth_regime import composite_scores
from backend.scripts.research_stats import expanding_pct_rank, forward_return
from backend.scripts.trin_study import _design, _ridge_fit, walk_forward

#: 每个预测周期用哪组特征（预注册，不按结果临时加）
FEATURES_BY_HORIZON: dict[int, tuple[str, ...]] = {
    5: (
        "breadth20",
        "trin",
        "turn_z",
        "amt_shock",
        "stress",
        "vol20",
        "mom20",
        "ma60g",
    ),
    10: ("breadth20", "trin", "turn_z", "amt_shock"),
    20: ("breadth20", "trin", "turn_z", "amt_shock"),
}

HORIZONS = (5, 10, 20)
MIN_TRAIN = 750
REFIT_EVERY = 250
#: 回看块保留的最近样本点数（每 horizon 天取一个，避免重叠样本重复计数）
REVIEW_POINTS = 12


@dataclass(frozen=True)
class FeatureFrame:
    """某指数的特征帧（全部为扩张分位，0~1，逐日因果）。"""

    x: pd.DataFrame
    close: pd.Series


_FRAME_CACHE: dict[tuple[str, str], FeatureFrame] = {}


def feature_frame(df: pd.DataFrame, feats: pd.DataFrame, symbol: str) -> FeatureFrame:
    """特征帧只与「全量数据截止日 + 指数」有关，进程内缓存一次。"""
    key = (symbol, df.index[-1].strftime("%Y%m%d"))
    cached = _FRAME_CACHE.get(key)
    if cached is not None:
        return cached
    amt = df["amt_total"]
    close = df[f"close_{symbol}"]
    ret = close.pct_change()
    x = pd.DataFrame(index=df.index)
    x["breadth20"] = feats["breadth20"]
    x["trin"] = feats["trin_ema10"]
    x["turn_z"] = expanding_pct_rank(amt / amt.rolling(20).mean() - 1.0, 500)
    x["amt_shock"] = feats["amt_shock"]
    x["stress"] = expanding_pct_rank(composite_scores(feats)["stress"], 500)
    x["vol20"] = expanding_pct_rank(ret.rolling(20).std(), 500)
    x["mom20"] = expanding_pct_rank(close / close.shift(20) - 1.0, 500)
    x["ma60g"] = expanding_pct_rank(close / close.rolling(60).mean() - 1.0, 500)
    frame = FeatureFrame(x=x, close=close)
    _FRAME_CACHE[key] = frame
    return frame


def _fit_at(frame: FeatureFrame, horizon: int, as_of: pd.Timestamp) -> dict | None:
    """在 as_of 之前、且收益已走完的样本上拟合，预测 as_of 当天。"""
    cols = FEATURES_NAMES[horizon]
    xa = frame.x[list(cols)].to_numpy()
    y = forward_return(frame.close, horizon).to_numpy()
    idx = np.arange(len(frame.x))
    cut = int(frame.x.index.searchsorted(as_of, side="right"))
    if cut < MIN_TRAIN:
        return None
    train_ok = np.isfinite(y) & np.isfinite(xa).all(axis=1) & (idx < cut - horizon)
    if train_ok.sum() < 300:
        return None
    beta = _ridge_fit(_design(xa[train_ok]), y[train_ok])
    cur = xa[cut - 1]
    if not np.isfinite(cur).all():
        return None
    return {
        "pred": float((_design(cur[None, :]) @ beta)[0]),
        "n_train": int(train_ok.sum()),
        "beta": beta,
    }


#: 便于外部读取（与 FEATURES_BY_HORIZON 同源）
FEATURES_NAMES = FEATURES_BY_HORIZON


def _calibration(
    frame: FeatureFrame, horizon: int, as_of: pd.Timestamp, pred: float
) -> dict:
    """用同一段历史的样本外预测校准概率与区间。

    - ``prob_up``：把历史样本外预测按分位切成 5 档，取当前预测所在档的实际上涨比例；
    - ``p10/p50/p90``：预测值 + 样本外残差的分位（残差 = 实际 − 预测）。
    两处都只用 as_of 及以前的数据。
    """
    sliced = FeatureFrame(x=frame.x.loc[:as_of], close=frame.close.loc[:as_of])
    # walk_forward 约定目标列名为 close_{target}，这里包一层避免歧义
    tmp = sliced.close.to_frame("close_X")
    preds = walk_forward(
        sliced.x,
        tmp,
        "X",
        horizon,
        FEATURES_NAMES[horizon],
        min_train=MIN_TRAIN,
        refit_every=REFIT_EVERY,
    )
    y = forward_return(sliced.close, horizon)
    d = pd.concat([preds.rename("p"), y.rename("y")], axis=1).dropna()
    if len(d) < 200:
        return {
            "prob_up": None,
            "base_prob": None,
            "p10": None,
            "p50": None,
            "p90": None,
            "n_oos": int(len(d)),
            "accuracy": None,
            "review": [],
        }
    resid = (d["y"] - d["p"]).to_numpy()
    q10, q50, q90 = np.percentile(resid, [10, 50, 90])
    lo, hi = np.percentile(d["p"], [20, 80])
    if pred <= lo:
        bucket = d["p"] <= lo
    elif pred >= hi:
        bucket = d["p"] >= hi
    else:
        bucket = (d["p"] > lo) & (d["p"] < hi)
    prob = float((d.loc[bucket, "y"] > 0).mean()) if bucket.sum() >= 40 else None
    # 复盘：方向命中用**非重叠**样本（每 horizon 天取一点），否则 20 天窗口里
    # 相邻样本共享 19 天收益，命中率会被重复计数撑出虚高的 n。
    every = d.iloc[::horizon]
    hit = (
        float((np.sign(every["p"]) == np.sign(every["y"])).mean())
        if len(every)
        else float("nan")
    )
    mae = float((d["y"] - d["p"]).abs().mean())
    ic = float(d["p"].corr(d["y"], method="spearman")) if len(d) > 30 else float("nan")
    review = [
        {
            "date": ts.strftime("%Y-%m-%d"),
            "pred": round(float(row["p"]), 2),
            "actual": round(float(row["y"]), 2),
            "hit": bool(np.sign(row["p"]) == np.sign(row["y"])),
        }
        for ts, row in every.tail(REVIEW_POINTS).iterrows()
    ]
    return {
        "prob_up": None if prob is None else round(prob, 3),
        "base_prob": round(float((d["y"] > 0).mean()), 3),
        "p10": round(pred + float(q10), 2),
        "p50": round(pred + float(q50), 2),
        "p90": round(pred + float(q90), 2),
        "n_oos": int(len(d)),
        "accuracy": {
            "n_oos": int(len(d)),
            "n_eff": int(len(every)),
            "hit": round(hit, 3) if np.isfinite(hit) else None,
            "mae": round(mae, 2),
            "ic": round(ic, 3) if np.isfinite(ic) else None,
        },
        "review": review,
    }


def forecast_index(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    symbol: str,
    as_of: pd.Timestamp | None = None,
) -> dict:
    """一个指数的 T+5/10/20 预测（概率 + 区间 + 样本量）。"""
    frame = feature_frame(df, feats, symbol)
    cut_date = as_of if as_of is not None else frame.x.index[-1]
    out: dict[str, dict] = {}
    for h in HORIZONS:
        fit = _fit_at(frame, h, cut_date)
        if fit is None:
            out[str(h)] = {
                "pred": None,
                "prob_up": None,
                "p10": None,
                "p50": None,
                "p90": None,
                "accuracy": None,
                "review": [],
            }
            continue
        cal = _calibration(frame, h, cut_date, fit["pred"])
        out[str(h)] = {
            "pred": round(fit["pred"], 2),
            "n_train": fit["n_train"],
            **cal,
        }
    return out
