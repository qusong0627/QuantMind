"""训练结果四道闸：horizon curve / residual IC / permutation p / random batch。

背景（2026-11-16 KiT 样本外审计）：单池显著不等于可泛化。100 只 demo 池的
2~5 日信号置换 p<0.001，但随机 50 只 batch 上 IC 从 0.09 掉到 0.02~0.04，
top10 扣费后转负。因此训练报告除 IC/ICIR/分层外，再固定输出四道闸：

1. ``horizon_curve``：同一模型分对 1/2/3/5/10 日远期收益的 IC/top decile；
2. ``residual_ic``：中性化 size/vol/momentum/liquidity 后的增量 IC；
3. ``permutation``：横截面 shuffle 的 placebo p 值（按日截面置换，比较均值）；
4. ``random_batches``：按 symbol 随机分组的稳健性（均值/最小/正比例）。

所有函数为纯函数，输入 ``pred_df`` 至少含 ``trade_date``/``pred``/``symbol``
和收益列；缺列/缺因子时对应闸返回 ``skipped``，不抛异常、不阻断训练。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from model_trainers.metrics import _ic, _rank_ic_series

GATE_HORIZONS: tuple[int, ...] = (1, 2, 3, 5, 10)
DEFAULT_CONTROLS: tuple[str, ...] = (
    "total_cap",
    "fun_total_mv",
    "fun_float_mv",
    "amt_log",
    "amt_z_20",
    "vol_20",
    "vol_std_20",
    "vol_amp_20",
    "mom_20",
    "mom_ret_20d",
    "mom_ret_60d",
    "turnover",
    "turnover_20",
    "turn_20",
    "turn_z_20",
)

# 每类只取第一个存在的列，避免 size 别名同时进 OLS 造成共线。
# 没有 total_cap 的因子源（如 l1_factors）用 amt_log 作为流动性/规模代理。
_CONTROL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("total_cap", "fun_total_mv", "fun_float_mv", "amt_log", "amt_z_20"),
    ("vol_20", "vol_std_20", "vol_amp_20"),
    ("mom_20", "mom_ret_20d", "mom_ret_60d"),
    ("turnover", "turnover_20", "turn_20", "turn_z_20"),
)


def _resolve_controls(
    df: pd.DataFrame, controls: Sequence[str]
) -> list[str]:
    used: list[str] = []
    for group in _CONTROL_GROUPS:
        for c in group:
            if c in df.columns:
                used.append(c)
                break
    # 显式传入但不在任何分组里的列才追加；同组别名不再重复进 OLS。
    grouped = {c for group in _CONTROL_GROUPS for c in group}
    for c in controls:
        if c in df.columns and c not in grouped and c not in used:
            used.append(c)
    return used


def _stats(values: Iterable[float]) -> dict[str, Any]:
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=np.float64)
    if v.size == 0:
        return {
            "mean": None,
            "std": None,
            "icir": None,
            "win_rate": None,
            "t_stat": None,
            "n": 0,
        }
    mean = float(v.mean())
    std = float(v.std(ddof=1)) if v.size > 1 else 0.0
    return {
        "mean": round(mean, 6),
        "std": round(std, 6),
        "icir": round(mean / std, 4) if std > 1e-12 else None,
        "win_rate": round(float((v > 0).mean()), 4),
        "t_stat": (
            round(mean / (std / np.sqrt(v.size)), 2) if std > 1e-12 else None
        ),
        "n": int(v.size),
    }


def _top_decile_mean(df: pd.DataFrame, pred_col: str, ret_col: str) -> float:
    if df.empty:
        return float("nan")
    pct = df[pred_col].rank(pct=True)
    top = df.loc[pct >= 0.9, ret_col]
    return float(top.mean()) if len(top) else float("nan")


def permutation_p(
    pred_df: pd.DataFrame,
    *,
    ret_col: str = "label_return",
    n_perm: int = 200,
    seed: int = 42,
    max_days: int = 800,
) -> dict[str, Any]:
    """按日截面置换的 p 值：比较「均值 IC」而非单日 IC。

    旧坑：拿单次置换 IC 与 actual mean 比会把 p 值放大一个数量级；正确做法
    是每次置换都对所有日取均值，得到 placebo mean 的分布。
    """
    need = {"trade_date", "pred", ret_col}
    if not need <= set(pred_df.columns):
        return {"skipped": True, "reason": f"missing columns: {sorted(need - set(pred_df.columns))}"}
    df = pred_df[["trade_date", "pred", ret_col]].dropna()
    if df.empty:
        return {"skipped": True, "reason": "empty frame"}

    dates = pd.Index(df["trade_date"].unique())
    if len(dates) > max_days:
        rng0 = np.random.default_rng(seed)
        dates = pd.Index(rng0.choice(dates.to_numpy(), size=max_days, replace=False))
        df = df[df["trade_date"].isin(dates)]

    per_day: list[tuple[np.ndarray, np.ndarray]] = []
    for _, g in df.groupby("trade_date", sort=False):
        if len(g) < 10:
            continue
        pr = g["pred"].rank(method="average").to_numpy(dtype=np.float64)
        rr = g[ret_col].rank(method="average").to_numpy(dtype=np.float64)
        if np.std(pr) > 0 and np.std(rr) > 0:
            per_day.append((pr, rr))
    if not per_day:
        return {"skipped": True, "reason": "no usable cross-sections"}

    actual = float(np.mean([_ic(pr, rr) for pr, rr in per_day]))
    rng = np.random.default_rng(seed)
    placebo_means = np.empty(int(n_perm), dtype=np.float64)
    for i in range(int(n_perm)):
        ics = [
            _ic(rng.permutation(pr), rr) for pr, rr in per_day
        ]
        placebo_means[i] = float(np.mean(ics))
    p_value = float((placebo_means >= actual).mean())
    return {
        "actual_mean_ic": round(actual, 6),
        "placebo_mean": round(float(placebo_means.mean()), 6),
        "placebo_std": round(float(placebo_means.std()), 6),
        "p_value": round(p_value, 4),
        "n_perm": int(n_perm),
        "n_days": int(len(per_day)),
    }


def residual_ic(
    pred_df: pd.DataFrame,
    *,
    ret_col: str = "label_return",
    controls: Sequence[str] = DEFAULT_CONTROLS,
) -> dict[str, Any]:
    """中性化控制变量后的残差 RankIC（逐日 rank OLS）。

    只做归因/增量，不是因果识别；控制变量取 ``pred_df`` 中实际存在的数值列。
    """
    used = _resolve_controls(pred_df, controls)
    if not used:
        return {"skipped": True, "reason": "no control columns present"}
    need = {"trade_date", "pred", ret_col, *used}
    if not need <= set(pred_df.columns):
        return {"skipped": True, "reason": "missing control columns"}
    df = pred_df[["trade_date", "pred", ret_col, *used]].dropna()
    if df.empty:
        return {"skipped": True, "reason": "empty frame"}

    daily: list[float] = []
    for _, g in df.groupby("trade_date", sort=False):
        if len(g) < 10:
            continue
        y = g["pred"].rank(pct=True).to_numpy(dtype=np.float64)
        ret_rank = g[ret_col].rank(pct=True).to_numpy(dtype=np.float64)
        X = g[used].rank(pct=True).to_numpy(dtype=np.float64)
        A = np.column_stack([np.ones(len(X)), X])
        try:
            beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid = y - A @ beta
        v = _ic(resid, ret_rank)
        if np.isfinite(v):
            daily.append(v)
    if not daily:
        return {"skipped": True, "reason": "no usable cross-sections"}
    raw_ics = _rank_ic_series(df, "pred", ret_col)
    return {
        **{f"resid_{k}": v for k, v in _stats(daily).items()},
        "raw_mean_ic": round(float(np.mean(raw_ics)), 6) if raw_ics else None,
        "controls": used,
    }


def random_batch_report(
    pred_df: pd.DataFrame,
    *,
    ret_col: str = "label_return",
    n_batches: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    """按 symbol 随机分组的稳健性：模拟「随机 1/n 股票池」。"""
    need = {"trade_date", "symbol", "pred", ret_col}
    if not need <= set(pred_df.columns):
        return {"skipped": True, "reason": f"missing columns: {sorted(need - set(pred_df.columns))}"}
    df = pred_df[["trade_date", "symbol", "pred", ret_col]].dropna()
    symbols = np.asarray(sorted(df["symbol"].unique()))
    if len(symbols) < max(10, int(n_batches) * 2):
        return {"skipped": True, "reason": f"too few symbols ({len(symbols)})"}

    rng = np.random.default_rng(seed)
    shuffled = rng.permutation(symbols)
    groups = np.array_split(shuffled, int(n_batches))
    batches: list[dict[str, Any]] = []
    for i, group in enumerate(groups):
        sub = df[df["symbol"].isin(group)]
        ic = _rank_ic_series(sub, "pred", ret_col)
        top = []
        for _, g in sub.groupby("trade_date", sort=False):
            v = _top_decile_mean(g, "pred", ret_col)
            if np.isfinite(v):
                top.append(v)
        batches.append(
            {
                "batch": i,
                "n_symbols": int(len(group)),
                "n_rows": int(len(sub)),
                "rank_ic": _stats(ic),
                "top_decile_mean": round(float(np.mean(top)), 6) if top else None,
            }
        )

    ic_means = [b["rank_ic"]["mean"] for b in batches if b["rank_ic"]["mean"] is not None]
    top_means = [b["top_decile_mean"] for b in batches if b["top_decile_mean"] is not None]
    return {
        "n_batches": int(n_batches),
        "batch_symbol_frac": round(1.0 / max(1, int(n_batches)), 4),
        "rank_ic_mean": round(float(np.mean(ic_means)), 6) if ic_means else None,
        "rank_ic_min": round(float(np.min(ic_means)), 6) if ic_means else None,
        "rank_ic_max": round(float(np.max(ic_means)), 6) if ic_means else None,
        "rank_ic_positive_frac": round(float(np.mean(np.asarray(ic_means) > 0)), 4)
        if ic_means
        else None,
        "top_decile_mean_avg": round(float(np.mean(top_means)), 6) if top_means else None,
        "batches": batches,
    }


def horizon_curve(
    pred_df: pd.DataFrame,
    *,
    horizons: Sequence[int] = GATE_HORIZONS,
) -> dict[str, Any]:
    """同一模型分对多周期远期收益的 IC/top decile（需 ``label_return_h{h}``）。"""
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"label_return_h{int(h)}"
        if col not in pred_df.columns:
            continue
        sub = pred_df[["trade_date", "pred", col]].dropna()
        if sub.empty:
            continue
        ic = _rank_ic_series(sub, "pred", col)
        top = []
        for _, g in sub.groupby("trade_date", sort=False):
            v = _top_decile_mean(g, "pred", col)
            if np.isfinite(v):
                top.append(v)
        out[f"h{int(h)}"] = {
            "rank_ic": _stats(ic),
            "top_decile_mean": round(float(np.mean(top)), 6) if top else None,
        }
    if not out:
        return {"skipped": True, "reason": "no label_return_h* columns"}
    return out


def compute_gates(
    pred_df: pd.DataFrame,
    *,
    ret_col: str | None = None,
    controls: Sequence[str] = DEFAULT_CONTROLS,
    horizons: Sequence[int] = GATE_HORIZONS,
    n_perm: int = 200,
    n_batches: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    """汇总四道闸；缺列/缺因子的闸各自 skipped，不影响其它闸。"""
    if ret_col is None:
        ret_col = "label_return" if "label_return" in pred_df.columns else "label"
    meta = {
        "n_rows": int(len(pred_df)),
        "n_days": int(pred_df["trade_date"].nunique())
        if "trade_date" in pred_df.columns
        else 0,
        "n_symbols": int(pred_df["symbol"].nunique())
        if "symbol" in pred_df.columns
        else 0,
        "ret_col": ret_col,
        "controls": _resolve_controls(pred_df, controls),
        "horizons": [int(h) for h in horizons],
    }
    report: dict[str, Any] = {
        "version": 1,
        "meta": meta,
        "horizon_curve": horizon_curve(pred_df, horizons=horizons),
        "residual_ic": residual_ic(pred_df, ret_col=ret_col, controls=controls),
        "permutation": permutation_p(
            pred_df, ret_col=ret_col, n_perm=n_perm, seed=seed
        ),
        "random_batches": random_batch_report(
            pred_df, ret_col=ret_col, n_batches=n_batches, seed=seed
        ),
    }
    return report
