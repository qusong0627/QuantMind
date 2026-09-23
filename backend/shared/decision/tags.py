"""入场前形态标签（P2.1d 记分卡用）——纯函数，只吃收盘序列。

为什么要有它
------------
记分卡回答「这个模型选股行不行」；标签回答**「它在什么模式下不行」**。同一批
`bullish` 决策，若「追高」那一组的 t5 超额系统性为负，处置是改提示词（要求不追高），
不是把整个模型关掉——没有这层归因，只有「总体不显著」一个结论，没法行动。

口径来源
--------
逐字移植隔壁 `scripts/decision_track.py:_tags`（常量见下），**只改一处**：
隔壁从 `bars[:idx]` 里现取 K 线，本函数只吃**入场日之前**的收盘序列——取数归
`GhostMarket`，本模块不碰 I/O。

三条纪律
--------
* **绝不前视**：序列必须在入场日**之前**截止。把入场日自己算进去，「接近60日高」
  就会因为当天大涨而恒真（当天自己的收盘就是这个窗口的最后一根）。
* **无效值先剔**：`None`/非正/非有限值不参与任何窗口（`ma20` 少一根就是另一个数）。
* **历史不够就返回空**：不足 `min_history` 根时**不猜**（隔壁也是 `[]`）——样本
  不够的标签比没有标签更坏，它会被当成一个有效分组统计下去。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

#: 「追高/超跌」的 5 日涨跌幅阈值（隔壁同值）
SURGE_THRESHOLD = 0.08

#: 「接近60日高」的距离阈值（距窗口最高收盘 −5% 以内）
NEAR_HIGH_THRESHOLD = -0.05

#: 均线窗口（交易日）
MA_WINDOW = 20

#: 「接近60日高」的回看窗口（交易日；隔壁注释：受面板长度限制，不用 52 周口径）
HIGH_LOOKBACK = 60

#: 至少要有这么多根有效收盘才出标签（少于 MA20 一个窗口就没法判均线）
MIN_HISTORY = MA_WINDOW + 1

TAG_CHASE = "追高"
TAG_DIP = "超跌"
TAG_NEAR_HIGH = "接近60日高"
TAG_ABOVE_MA = "站上MA20"
TAG_BELOW_MA = "破MA20"


def _valid(closes: Iterable[float | None]) -> list[float]:
    """剔掉 None/非正/非有限——一个 NaN 就能把 `max()` 和均值一起污染。"""
    out: list[float] = []
    for c in closes:
        v = c if isinstance(c, (int, float)) else None
        if v is None:
            continue
        f = float(v)
        if math.isfinite(f) and f > 0:
            out.append(f)
    return out


def form_tags(
    closes_before_entry: Sequence[float | None],
    *,
    min_history: int = MIN_HISTORY,
    high_lookback: int = HIGH_LOOKBACK,
) -> tuple[str, ...]:
    """入场日**之前**的收盘序列 → 形态标签（可空元组）。

    返回顺序固定（追高/超跌 → 接近60日高 → 均线），便于对账时逐字比对。
    """
    closes = _valid(closes_before_entry)
    if len(closes) < min_history:
        return ()

    last = closes[-1]
    tags: list[str] = []

    # 5 日涨跌幅：需要 6 根（末根与 5 根之前那根比）
    chg5 = 0.0
    if len(closes) >= 6:
        base = closes[-6]
        if base > 0:
            chg5 = last / base - 1.0
    if chg5 > SURGE_THRESHOLD:
        tags.append(TAG_CHASE)
    elif chg5 < -SURGE_THRESHOLD:
        tags.append(TAG_DIP)

    window = closes[-high_lookback:] if high_lookback > 0 else closes
    high = max(window)
    if high > 0 and last / high - 1.0 > NEAR_HIGH_THRESHOLD:
        tags.append(TAG_NEAR_HIGH)

    ma = sum(closes[-MA_WINDOW:]) / MA_WINDOW
    tags.append(TAG_ABOVE_MA if last > ma else TAG_BELOW_MA)
    return tuple(tags)


__all__ = [
    "HIGH_LOOKBACK",
    "MA_WINDOW",
    "MIN_HISTORY",
    "NEAR_HIGH_THRESHOLD",
    "SURGE_THRESHOLD",
    "TAG_ABOVE_MA",
    "TAG_BELOW_MA",
    "TAG_CHASE",
    "TAG_DIP",
    "TAG_NEAR_HIGH",
    "form_tags",
]
