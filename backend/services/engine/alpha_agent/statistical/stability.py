"""统计派 · 子窗稳定性与滚动再选择（移植 Tier 3 / T-S3-01/02）。

一次折划分，两条证据，一条闸：

1. **子窗稳定性**（:func:`reselect_statistics`）——把挖掘窗按**全体候选中证
   日期的并集**切成 ``folds`` 个等份，一次算齐两件证据：

   - **折间符号一致性**（``sign_consistency``）：逐折 RankIC 与全窗同号的
     折占比——**闸取它**（引擎层 ``min_fold_sign_consistency``）：符号翻脸
     （先正后负 / 先负后正）是窗段过拟合的典型形态，拒之有名；
   - **滚动再选择命中率**（``reselect_rate``）：检查点 = 第 2..K 折末（展开
     窗），逐点按「折 1..j 合并日均 RankIC」重排 Top-K 的命中比例。**降为
     审计量**（2026-10-11 csi300 实跑实证：它是「跨候选抢 K 席」的相对量，
     19/28 候选命中 0/3 但折间全同号——席位被同簇因子霸榜时一致因子集体
     误伤，刀锋判决，不当闸）。率低 = 靠的是窗尾偶发强段，证据照记、随
     报告上屏，人审用。

   **诚实边界（写死在明面上）**：重放只重演**排名阶段**——不逐检查点重跑
   BH-FDR 与样本外闸（两者要各自的周期估计，逐点重算是另一个量级的机制）。
   因此重放候选池 = 检查点上「有可算 IC 日」的全部候选，比生产选择池宽，
   算出的率值是**保守**下界。折结构不足（任一折有效日 < ``min_fold_days``）
   即 ``eligible=False``——闸不响，如实记 note，不拿假精确拒人。

2. **IC 半衰期**（:func:`ic_half_life`）——多周期 ``|RankIC(h)|`` 上做
   ``log|IC| = a − λh`` 最小二乘，``half_life = ln2/λ``。纯审计量（进报告，
   不进闸）：λ ≤ 0（不衰减/上升）→ NaN 如实标；与 best horizon 异号的周期点
   不参与拟合（混号的对数衰减没有意义）。

折划分按**位置等分**（不是自然日）：A 股停牌/节假日下日历等分会产生长短悬殊
的折，等份位置对每个候选都是同一批日期（并集口径），跨候选可比。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

#: 默认折数（4 折 ≈ 三个检查点；再多折在 4 年窗上单折样本就开始薄了）
DEFAULT_FOLDS = 4
#: 单折最少有效 IC 日（低于此不判稳定性——折太薄上的证据是噪声）
DEFAULT_MIN_FOLD_DAYS = 10
#: 默认折间符号一致性闸（低于即「折间符号不稳」）
DEFAULT_MIN_FOLD_SIGN_CONSISTENCY = 0.75
#: 半衰期对数线性拟合的最少点数（少于 3 点没有拟合自由度）
MIN_HALF_LIFE_POINTS = 3


@dataclass(frozen=True)
class ReselectStats:
    """单候选的子窗证据（不可判定时 rate/consistency = NaN，note 说明原因）。

    ``reselect_rate`` 是审计量（证据照记，不当闸）；闸在引擎层用
    ``sign_consistency``。
    """

    fold_ics: tuple[float, ...]  # 每折 RankIC 均值（该折无有效日为 NaN）
    sign_consistency: float  # 与全窗同号的折占比（无有效折 → NaN）——闸取它
    reselect_rate: float  # 检查点入选比例（不可判定 → NaN）——审计量，不进闸
    reselect_hits: int
    reselect_checkpoints: int
    eligible: bool  # 折结构足够（每折有效日 ≥ min_fold_days），率可判定
    note: str = ""  # 不可判定原因（如实上屏）


def ic_half_life(
    horizons: Sequence[int],
    rank_ics: Sequence[float],
    *,
    min_points: int = MIN_HALF_LIFE_POINTS,
) -> tuple[float, float]:
    """多周期 |RankIC| 衰减 → ``(half_life_bars, λ)``；拟合不成返回 NaN。

    口径：取与最强 |RankIC| 同号且 |IC| > 0 的 (h, |IC|) 点，``log|IC|`` 对 h
    做一次多项式拟合（斜率 = −λ）；``half_life = ln2/λ``。点数 < ``min_points``
    或 λ ≤ 0 → half_life = NaN（λ 仍如实返回：λ ≤ 0 = 「不衰减」，不是算不出）。
    """
    points = [
        (int(h), float(ic))
        for h, ic in zip(horizons, rank_ics, strict=True)
        if np.isfinite(ic) and ic != 0
    ]
    if not points:
        return float("nan"), float("nan")
    best_sign = np.sign(max(points, key=lambda p: abs(p[1]))[1])
    same_sign = [(h, abs(ic)) for h, ic in points if np.sign(ic) == best_sign]
    if len(same_sign) < max(2, min_points):
        return float("nan"), float("nan")
    x = np.asarray([h for h, _ in same_sign], dtype=float)
    y = np.log(np.asarray([v for _, v in same_sign], dtype=float))
    slope = float(np.polyfit(x, y, 1)[0])  # log|IC| = a − λh → slope = −λ
    decay = -slope
    half_life = (
        float(np.log(2) / decay) if np.isfinite(decay) and decay > 0 else float("nan")
    )
    return half_life, decay


def fold_boundaries(dates: pd.DatetimeIndex, folds: int) -> np.ndarray:
    """并集日期 → 逐日期折号（位置等分；``len(dates) < folds`` 时尽量均分）。"""
    positions = np.arange(len(dates))
    chunks = np.array_split(positions, folds)
    ends = np.cumsum([len(chunk) for chunk in chunks])
    return np.searchsorted(ends, positions, side="right").astype(int)


def reselect_statistics(
    daily_ranks: Mapping[str, pd.Series],
    *,
    folds: int = DEFAULT_FOLDS,
    top_k: int,
    min_fold_days: int = DEFAULT_MIN_FOLD_DAYS,
) -> dict[str, ReselectStats]:
    """全部候选的滚动再选择证据（一次算齐——检查点重排是跨候选操作）。

    ``daily_ranks``：key → 该候选 best horizon 的挖掘窗逐日 RankIC 序列
    （index = trade_date）。空的/全 NaN 序列会得到 ``eligible=False`` 与原因。
    """
    if folds < 2:
        raise ValueError(f"folds 至少为 2（收到 {folds!r}）")
    if top_k < 1:
        raise ValueError(f"top_k 至少为 1（收到 {top_k!r}）")

    series = {key: s.astype(float).dropna() for key, s in daily_ranks.items()}
    n_checkpoints = folds - 1
    missing = {
        key: ReselectStats(
            (), float("nan"), float("nan"), 0, n_checkpoints, False, "无有效 IC 日"
        )
        for key, s in series.items()
        if s.empty
    }
    usable = {key: s for key, s in series.items() if not s.empty}
    if not usable:
        return missing

    union = pd.DatetimeIndex(sorted(set().union(*(s.index for s in usable.values()))))
    fold_lookup = pd.Series(fold_boundaries(union, folds), index=union)

    fold_ics: dict[str, tuple[float, ...]] = {}
    fold_counts: dict[str, np.ndarray] = {}
    sign_consistency: dict[str, float] = {}
    sums_upto: dict[str, np.ndarray] = {}
    counts_upto: dict[str, np.ndarray] = {}
    for key, s in usable.items():
        fold_ids = fold_lookup.reindex(s.index).to_numpy(dtype=int)
        sums = np.zeros(folds)
        counts = np.zeros(folds, dtype=int)
        grouped = s.groupby(fold_ids)
        for fid, group in grouped:
            sums[int(fid)] = float(group.sum())
            counts[int(fid)] = int(len(group))
        fold_ics[key] = tuple(
            sums[i] / counts[i] if counts[i] else float("nan") for i in range(folds)
        )
        fold_counts[key] = counts
        overall = float(s.mean())
        active = [i for i in range(folds) if counts[i]]
        sign_consistency[key] = (
            float(
                np.mean(
                    [np.sign(sums[i] / counts[i]) == np.sign(overall) for i in active]
                )
            )
            if active and overall != 0
            else float("nan")
        )
        sums_upto[key] = np.cumsum(sums)
        counts_upto[key] = np.cumsum(counts)

    hits = dict.fromkeys(usable, 0)
    for checkpoint in range(2, folds + 1):
        cursor = checkpoint - 1  # 折 1..checkpoint 的累计下标（0-based 含）
        pool: list[tuple[str, float]] = []
        for key in usable:
            count = int(counts_upto[key][cursor])
            if count >= min_fold_days:
                pool.append((key, abs(float(sums_upto[key][cursor]) / count)))
        if not pool:
            continue
        pool.sort(key=lambda item: (-item[1], item[0]))
        for key, _value in pool[:top_k]:
            hits[key] += 1

    out: dict[str, ReselectStats] = dict(missing)
    for key in usable:
        eligible = bool(np.all(fold_counts[key] >= min_fold_days))
        note = "" if eligible else f"折内有效日不足（单折 < {min_fold_days} 天）"
        out[key] = ReselectStats(
            fold_ics=fold_ics[key],
            sign_consistency=sign_consistency[key],
            reselect_rate=hits[key] / n_checkpoints,
            reselect_hits=hits[key],
            reselect_checkpoints=n_checkpoints,
            eligible=eligible,
            note=note,
        )
    return out
