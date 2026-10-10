"""方向加权抽样（T-MV-03）—— 「random」不再是均匀随机，而是按空白度加权。

空白度口径：候选方向在**本用户 × 本市场**的挖掘史出现次数 n
（``rd_agent_mining_tasks`` 全状态计数——failed/cancelled 也是「挖过」）。
权重 ``w = 1/(1+n)``：没挖过的方向 1.0，挖过 k 次降为 1/(1+k)。史全空时
权重全 1，自然退化为旧的均匀随机（行为兼容，不是突变）。

**可复现**（验收：任务记录含抽中方向）：抽签用显式 seed 的
``random.Random(seed)``；seed/候选/权重/命中全部落任务 ``direction_meta``
（JSON）。复算方按 meta 重放 ``Random(seed).choices(candidates, weights)``
必得同一命中——``random()`` 序列跨 Python 版本稳定（CPython 文档保证），
所以 meta 就是「方向怎么抽出来的」的完整凭证。

为什么权重不直接取池内饱和度（T-MV-02 的 ``saturation``）：L1 目录方向
词表与池内归类（``factor_classify``）不重合（实测桥接 ~5/14），硬拼映射
不可靠；挖掘史按**方向字符串**精确计数，无跨词表对齐问题、跨市场自洽、
且天然可复现。配额制（T-MV-13）落地后此处权重口径不变，配额约束在
候选过滤层叠加。

并行方向数（T-MV-04）走 :func:`sample_weighted_directions_n`：同权重口径的
**不放回**逐步抽样，N 条各自带独立可重放的 meta；N 不小于候选数时直派全集
（没抽签 → meta=None），单条路径仍走 :func:`sample_weighted_direction` 原样。

纪律：读史失败**不拦任务创建**——退均匀兜底，meta 如实标注
``uniform_fallback``（不假装加权成功）；空候选显式 ValueError
（静默返回空方向会让任务凭空「无方向」，比报错更难查）。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: meta.weighting 的两个取值——加权公式正常执行 / 读史失败退均匀兜底。
WEIGHTING_BLANKNESS = "blankness"
WEIGHTING_UNIFORM_FALLBACK = "uniform_fallback"

#: seed 上限（2**31）：够随机即可，落 JSON 里保持人类可读的十进制整数。
_SEED_RANGE = 2**31


def blankness_weights(attempts: Sequence[int]) -> list[float]:
    """w = 1/(1+n)。脏数据（负数）按 0 收敛——权重不可为负，也不能被 -1 除零。"""
    return [1.0 / (1 + max(0, int(n))) for n in attempts]


async def _read_attempts(
    clean: Sequence[str],
    *,
    user_id: str,
    market: str,
    counter: Callable[..., Any] | None,
) -> tuple[dict[str, int], str]:
    """读挖掘史计数，返回 ``(attempts_by_dir, weighting)``。

    读失败**不抛**：退均匀兜底并把 weighting 标成 ``uniform_fallback``（调用方
    落进 meta——不假装加权成功）。单条/并行两条抽样路径共用这一份读史语义。
    """
    try:
        if counter is None:
            from .task_store import get_mining_task_store

            counter = get_mining_task_store().count_by_direction
        attempts_by_dir = await counter(
            user_id=user_id, market=market, directions=list(clean)
        )
        return attempts_by_dir, WEIGHTING_BLANKNESS
    except Exception as e:  # noqa: BLE001 - 读史失败退均匀，绝不拦任务创建
        logger.warning("direction mining-history read failed, uniform fallback: %s", e)
        return {}, WEIGHTING_UNIFORM_FALLBACK


async def sample_weighted_direction(
    directions: Sequence[str],
    *,
    user_id: str,
    market: str,
    counter: Callable[..., Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """按空白度加权抽一个方向，返回 ``(picked, meta)``；meta 落任务库可复现。

    ``counter`` 注入点（默认 ``task_store.count_by_direction``）：单测固定计数、
    失败语义由 ``_read_attempts`` 统一兜底。候选顺序 = 传入顺序——choices 的
    累计权重按序展开，重放依赖它，不许重排。
    """
    clean = [d for d in directions if isinstance(d, str) and d.strip()]
    if not clean:
        raise ValueError("sample_weighted_direction 需要至少一个非空方向")

    attempts_by_dir, weighting = await _read_attempts(
        clean, user_id=user_id, market=market, counter=counter
    )

    attempts = [int(attempts_by_dir.get(d, 0)) for d in clean]
    weights = blankness_weights(attempts)
    seed = random.SystemRandom().randrange(_SEED_RANGE)
    picked = random.Random(seed).choices(clean, weights=weights, k=1)[0]

    meta = {
        "mode": "random",
        "weighting": weighting,
        "seed": seed,
        "picked": picked,
        "candidates": [
            {"direction": d, "attempts": n, "weight": w}
            for d, n, w in zip(clean, attempts, weights, strict=True)
        ],
    }
    return picked, meta


async def sample_weighted_directions_n(
    directions: Sequence[str],
    n: int,
    *,
    user_id: str,
    market: str,
    counter: Callable[..., Any] | None = None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """并行方向数（T-MV-04）：加权抽 N 条**互不相同**的方向，逐条带可复现 meta。

    返回 ``[(direction, meta|None)]``，顺序 = 抽取顺序（先抽中的排前）。
    语义边界（与验收「设置页 N 方向 → 实际派发 N 任务」一一对应）：

    - ``n <= 1``：委托 :func:`sample_weighted_direction`——单条路径行为一字不变。
    - ``n >= 候选数``：全集按传入顺序直派、逐条 meta=None——没抽签就没有抽签
      凭证（与 selected 同纪律，不为「全都要」伪造 seed）。
    - 其余：逐步**不放回**抽样——每步的候选 = 剩余集合、权重按剩余集合重算，
      meta 的 candidates 是**该步开始时**的剩余快照，所以每条 meta 都能独立
      重放复现（复算方无需知道其它步）。
    - 入参去重（保序）：两条同名方向派两个任务不是「并行方向」，是重复任务。
    """
    clean: list[str] = []
    seen: set[str] = set()
    for d in directions:
        if isinstance(d, str) and d.strip() and d not in seen:
            seen.add(d)
            clean.append(d)
    if not clean:
        raise ValueError("sample_weighted_directions_n 需要至少一个非空方向")
    if n <= 1:
        picked, meta = await sample_weighted_direction(
            clean, user_id=user_id, market=market, counter=counter
        )
        return [(picked, meta)]
    if n >= len(clean):
        return [(d, None) for d in clean]

    attempts_by_dir, weighting = await _read_attempts(
        clean, user_id=user_id, market=market, counter=counter
    )

    remaining = list(clean)
    picks: list[tuple[str, dict[str, Any] | None]] = []
    for _ in range(n):
        attempts = [int(attempts_by_dir.get(d, 0)) for d in remaining]
        weights = blankness_weights(attempts)
        seed = random.SystemRandom().randrange(_SEED_RANGE)
        picked = random.Random(seed).choices(remaining, weights=weights, k=1)[0]
        meta = {
            "mode": "random",
            "weighting": weighting,
            "seed": seed,
            "picked": picked,
            "candidates": [
                {"direction": d, "attempts": a, "weight": w}
                for d, a, w in zip(remaining, attempts, weights, strict=True)
            ],
        }
        picks.append((picked, meta))
        remaining.remove(picked)
    return picks
