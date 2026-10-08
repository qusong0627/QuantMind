"""组合权重优化器（P2 组合实验室）——纯函数模块，不碰 DB。

口径（与 ``backend/tests/test_combo_optimizer.py`` 互钉，改公式必须过测试）：

- ``w = u / Σ|u|``（L1 归一，允许负权=反向暴露；对 u 的正缩放目标不变，
  DE 边界 (−1,1)^d 只提供搜索空间）；
- 目标 = **train 窗内日均 rank-IC 最大**（scipy ``differential_evolution``
  求 −IC 最小；固定 seed → 同参可复现，seed 随 train_metrics.config 落库）；
- train/valid 按日期**时序** 70/30——随机拆分会把同日/未来信息漏进训练窗；
- 日采样上限 ``max_days``（linspace，沿 ``pool_panels.sample_days`` 同款）；
- 组合值 = Σ wᵢ·zᵢ（面板 zscore 列）；换手/扣成本与单因子评估器**同一实现**
  （``evaluators.turnover_cost``：多头=前 30%、首日不计成本、0.2% 双边单源）；
- 面板自带的 ``fret``（前瞻收益）列在所选因子上必须一致（同市场同口径）——
  不一致宁可报错（不同前瞻期混算出的 IC 是假指标），load 侧由此模块负责。

面板缺 ``fret`` 的历史因子：对相应因子重跑 ``mining_pool_rebuild --panels``
（面板重建会顺带补收益列），再进组合实验室。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import pool_panels
from .evaluators.turnover_cost import compute_turnover_cost, daily_net_returns

logger = logging.getLogger(__name__)

MIN_FACTORS = 2
MAX_FACTORS = 12
MIN_COMBO_DAYS = 20
MIN_SYMBOLS_PER_DAY = 3
#: 因子间 fret 最大允许差（float32 落盘后仍应逐位同值；给定小容差防浮点噪声）
FRET_TOL = 1e-5
_ZERO_NORM = 1e-12


@dataclass(frozen=True)
class ComboConfig:
    max_days: int = 120
    train_ratio: float = 0.7
    popsize: int = 15
    maxiter: int = 200
    tol: float = 1e-6
    seed: int = 42
    time_budget_s: float = 180.0
    cost_rate: float | None = None  # None → factor_research.analysis.COST_RATE


DEFAULT_CONFIG = ComboConfig()


def resolve_cost_rate(config: ComboConfig) -> float:
    """None → 研究口径单源（``factor_research.analysis.COST_RATE``）。"""
    if config.cost_rate is not None:
        return float(config.cost_rate)
    from backend.services.engine.factor_research.analysis import COST_RATE

    return float(COST_RATE)


def _z_columns(dataset: pd.DataFrame) -> list[str]:
    """z0..zN 按**数值**排序（字典序会把 z10 排到 z2 前）。"""
    cols = [
        c for c in dataset.columns if len(c) > 1 and c[0] == "z" and c[1:].isdigit()
    ]
    return sorted(cols, key=lambda c: int(c[1:]))


def _z_matrix(dataset: pd.DataFrame) -> np.ndarray:
    return dataset[_z_columns(dataset)].to_numpy(dtype="float64")


def build_dataset(
    frames: Mapping[str, pd.DataFrame],
    factor_ids: Sequence[str],
    *,
    max_days: int = DEFAULT_CONFIG.max_days,
) -> pd.DataFrame:
    """面板集合 → 组合长表（逐日截面交集 + 日采样 + 最小样本闸门）。

    列：``trade_date`` / ``symbol`` / ``z0..z{d-1}``（factor_ids 顺序）/ ``fret``。
    """
    order = list(factor_ids)
    if len(order) < MIN_FACTORS:
        raise ValueError(f"组合至少需要 {MIN_FACTORS} 个因子（收到 {len(order)}）")
    if len(order) > MAX_FACTORS:
        raise ValueError(f"因子数超上限：{len(order)} > {MAX_FACTORS}")
    if len(set(order)) != len(order):
        raise ValueError("因子列表有重复")

    missing = [fid for fid in order if fid not in frames or frames[fid] is None]
    if missing:
        raise ValueError(f"无面板因子：{', '.join(missing[:5])}")
    no_fret = [fid for fid in order if "fret" not in frames[fid].columns]
    if no_fret:
        raise ValueError(
            "以下因子面板缺收益列（fret），请先重算面板"
            f"（mining_pool_rebuild --panels）：{', '.join(no_fret[:5])}"
        )

    merged: pd.DataFrame | None = None
    for i, fid in enumerate(order):
        part = frames[fid][["trade_date", "symbol", "zscore", "fret"]].copy()
        part["trade_date"] = part["trade_date"].astype(str)
        part = part.drop_duplicates(subset=["trade_date", "symbol"], keep="last")
        part = part.rename(columns={"zscore": f"z{i}", "fret": f"fret{i}"})
        merged = (
            part
            if merged is None
            else merged.merge(part, on=["trade_date", "symbol"], how="inner")
        )
        if merged.empty:
            raise ValueError("所选因子面板无重叠 (交易日, 标的)")

    fret_cols = [f"fret{i}" for i in range(len(order))]
    frets = merged[fret_cols].to_numpy(dtype="float64")
    keep = np.isfinite(frets).all(axis=1)
    merged = merged[keep]
    if merged.empty:
        raise ValueError("所选因子面板无重叠 (交易日, 标的)")
    frets = merged[fret_cols].to_numpy(dtype="float64")
    dev = float(np.abs(frets - frets[:, [0]]).max()) if len(order) > 1 else 0.0
    if dev > FRET_TOL:
        raise ValueError(
            "所选因子的前瞻收益不一致（回测口径/前瞻期不同），无法组合评估："
            f"最大差 {dev:.3e}"
        )

    days = sorted(merged["trade_date"].unique())
    if max_days > 0 and len(days) > int(max_days):
        days = pool_panels.sample_days(days, int(max_days))
    merged = merged[merged["trade_date"].isin(set(days))]

    per_day = merged.groupby("trade_date")["symbol"].nunique()
    ok_days = set(per_day[per_day >= MIN_SYMBOLS_PER_DAY].index)
    merged = merged[merged["trade_date"].isin(ok_days)]
    n_days = int(merged["trade_date"].nunique())
    if n_days < MIN_COMBO_DAYS:
        raise ValueError(f"组合样本不足：有效交易日 {n_days} < {MIN_COMBO_DAYS}")

    z_cols = [f"z{i}" for i in range(len(order))]
    out = merged[["trade_date", "symbol", *z_cols]].copy()
    out["fret"] = merged["fret0"].to_numpy(dtype="float64")
    for col in z_cols:
        out[col] = out[col].astype("float64")
    return out.sort_values(["trade_date", "symbol"], kind="stable").reset_index(
        drop=True
    )


def split_dataset(
    dataset: pd.DataFrame, train_ratio: float = DEFAULT_CONFIG.train_ratio
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按日期时序拆分：前 ratio 的交易日为 train，其余 valid（两端非空）。"""
    days = sorted(dataset["trade_date"].unique())
    n_train = int(round(len(days) * float(train_ratio)))
    n_train = max(1, min(len(days) - 1, n_train))
    train_days = set(days[:n_train])
    train = dataset[dataset["trade_date"].isin(train_days)]
    valid = dataset[~dataset["trade_date"].isin(train_days)]
    return train, valid


def combo_values(dataset: pd.DataFrame, weights: Sequence[float]) -> np.ndarray:
    """Σ wᵢ·zᵢ（weights 按 factor_ids / z0..zN 顺序）。"""
    w = np.asarray(list(weights), dtype="float64")
    z = _z_matrix(dataset)
    if z.shape[1] != len(w):
        raise ValueError(f"权重维度不匹配：{len(w)} vs {z.shape[1]}")
    return z @ w


def _daily_rank_ic_series(
    day: pd.Series, combo: np.ndarray, fret: np.ndarray
) -> pd.Series:
    """逐日截面 Spearman（对日分组，秩→皮尔逊；核心数组版供 DE 内环复用）。"""
    c = pd.Series(combo, index=day.index)
    r = pd.Series(fret, index=day.index)
    c_rank = c.groupby(day).rank()
    r_rank = r.groupby(day).rank()
    cc = c_rank - c_rank.groupby(day).transform("mean")
    rr = r_rank - r_rank.groupby(day).transform("mean")
    cov = (cc * rr).groupby(day).sum()
    vc = (cc**2).groupby(day).sum()
    vr = (rr**2).groupby(day).sum()
    denom = (vc * vr) ** 0.5
    return (cov / denom.where(denom > 0)).dropna()


def daily_rank_ic(dataset: pd.DataFrame, combo: np.ndarray) -> pd.Series:
    return _daily_rank_ic_series(
        dataset["trade_date"], combo, dataset["fret"].to_numpy(dtype="float64")
    )


def _paired_frame(dataset: pd.DataFrame, combo: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": dataset["trade_date"].to_numpy(),
            "symbol": dataset["symbol"].to_numpy(),
            "factor": combo,
            "ret": dataset["fret"].to_numpy(dtype="float64"),
        }
    )


def window_metrics(
    dataset: pd.DataFrame, weights: Sequence[float], cost_rate: float
) -> dict[str, Any]:
    """单窗指标：rank-IC 族 + 换手扣成本族（与单因子评估器同实现同键名）。"""
    combo = combo_values(dataset, weights)
    ic = daily_rank_ic(dataset, combo)
    mean_ic = float(ic.mean()) if len(ic) else None
    icir: float | None = None
    if len(ic) >= 2:
        sd = float(ic.std(ddof=1))
        if sd > 0:
            icir = float(ic.mean() / sd)
    out: dict[str, Any] = {
        "mean_rank_ic": mean_ic,
        "rank_icir": icir,
        "n_days": int(len(ic)),
        "n_obs": int(len(dataset)),
    }
    out.update(compute_turnover_cost(_paired_frame(dataset, combo), float(cost_rate)))
    return out


def optimize_combo(
    frames: Mapping[str, pd.DataFrame],
    factor_ids: Sequence[str],
    config: ComboConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """主入口：面板集 → L1 归一权重 + 两窗指标 + valid 扣成本净值曲线。

    返回 ``{weights, train_metrics, valid_metrics, train_window}``；
    ``train_metrics.config`` 是完整参数回执（seed 落库可复现）。
    """
    from scipy.optimize import differential_evolution

    order = list(factor_ids)
    dataset = build_dataset(frames, order, max_days=config.max_days)
    train, valid = split_dataset(dataset, config.train_ratio)
    cost_rate = resolve_cost_rate(config)

    z_tr = _z_matrix(train)
    fret_tr = train["fret"].to_numpy(dtype="float64")
    day_tr = train["trade_date"].reset_index(drop=True)

    def _objective(u: np.ndarray) -> float:
        norm = float(np.abs(u).sum())
        if norm < _ZERO_NORM:
            return 1.0  # 全零方向：最大惩罚（IC 最大为 1，−IC 最小为 −1）
        combo = z_tr @ (u / norm)
        ic = _daily_rank_ic_series(day_tr, combo, fret_tr)
        mean_ic = float(ic.mean()) if len(ic) else float("nan")
        return 1.0 if not np.isfinite(mean_ic) else -mean_ic

    deadline = time.monotonic() + float(config.time_budget_s)
    # scipy ≥1.11 单参新式回调（intermediate_result），StopIteration 即停、
    # 返回当前最优；老版本双参形态下 intermed 为 None，只做时间闸门。
    best_seen: dict[str, np.ndarray] = {}

    def _tick(intermediate_result: Any = None) -> None:
        x = getattr(intermediate_result, "x", None)
        if x is not None:
            best_seen["x"] = np.asarray(x, dtype="float64")
        if time.monotonic() >= deadline:
            raise StopIteration

    converged = False
    nfev: int | None = None
    try:
        result = differential_evolution(
            _objective,
            bounds=[(-1.0, 1.0)] * len(order),
            seed=int(config.seed),
            popsize=int(config.popsize),
            maxiter=int(config.maxiter),
            tol=float(config.tol),
            polish=True,
            workers=1,
            updating="deferred",
            callback=_tick,
        )
        u = np.asarray(result.x, dtype="float64")
        converged = bool(getattr(result, "success", False))
        nfev = int(getattr(result, "nfev", 0)) or None
    except StopIteration:
        # 版本差异兜底：个别 scipy 版本不吞 StopIteration，用回调里记的当前最优。
        if "x" not in best_seen:
            raise
        u = best_seen["x"]
        logger.warning("[combo] 时间预算 %.0fs 到，返回当前最优", config.time_budget_s)

    norm = float(np.abs(u).sum())
    if norm < _ZERO_NORM:
        raise RuntimeError("优化未收敛出有效方向（Σ|u|≈0）")
    w = u / norm
    weights = {fid: float(wi) for fid, wi in zip(order, w, strict=True)}

    train_metrics = window_metrics(train, w, cost_rate)
    valid_metrics = window_metrics(valid, w, cost_rate)
    net = daily_net_returns(_paired_frame(valid, combo_values(valid, w)), cost_rate)
    valid_metrics["curve"] = {
        "dates": [str(d) for d in net.index],
        "values": [float(v) for v in (1.0 + net).cumprod()],
    }
    train_metrics["config"] = {
        "seed": int(config.seed),
        "popsize": int(config.popsize),
        "maxiter": int(config.maxiter),
        "tol": float(config.tol),
        "max_days": int(config.max_days),
        "train_ratio": float(config.train_ratio),
        "time_budget_s": float(config.time_budget_s),
        "cost_rate": float(cost_rate),
        "converged": converged,
        "n_evaluations": nfev,
    }

    tr_days = sorted(train["trade_date"].unique())
    va_days = sorted(valid["trade_date"].unique())
    train_window = (
        f"train {len(tr_days)}d {tr_days[0]}~{tr_days[-1]} / "
        f"valid {len(va_days)}d {va_days[0]}~{va_days[-1]}"
    )
    return {
        "weights": weights,
        "train_metrics": train_metrics,
        "valid_metrics": valid_metrics,
        "train_window": train_window,
    }
