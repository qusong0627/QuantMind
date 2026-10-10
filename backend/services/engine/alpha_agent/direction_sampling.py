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


async def sample_weighted_direction(
    directions: Sequence[str],
    *,
    user_id: str,
    market: str,
    counter: Callable[..., Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """按空白度加权抽一个方向，返回 ``(picked, meta)``；meta 落任务库可复现。

    ``counter`` 注入点（默认 ``task_store.count_by_direction``）：单测固定计数、
    失败语义由本函数统一兜底。候选顺序 = 传入顺序——choices 的累计权重按序
    展开，重放依赖它，不许重排。
    """
    clean = [d for d in directions if isinstance(d, str) and d.strip()]
    if not clean:
        raise ValueError("sample_weighted_direction 需要至少一个非空方向")

    weighting = WEIGHTING_BLANKNESS
    attempts_by_dir: dict[str, int] = {}
    try:
        if counter is None:
            from .task_store import get_mining_task_store

            counter = get_mining_task_store().count_by_direction
        attempts_by_dir = await counter(
            user_id=user_id, market=market, directions=clean
        )
    except Exception as e:  # noqa: BLE001 - 读史失败退均匀，绝不拦任务创建
        logger.warning("direction mining-history read failed, uniform fallback: %s", e)
        weighting = WEIGHTING_UNIFORM_FALLBACK
        attempts_by_dir = {}

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
