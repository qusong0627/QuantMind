"""分位阈值唯一实现（T-P4-03）——**纯函数**。

设计铁律（统一交易栈 §4.1）：选股/风控阈值**只允许引用分位**，绝对分数只做诊断展示。
根因：模型分数分布随训练数据/目标漂移（实测某模型全市场 ∈ [-0.048, 0.012]），
硬编码绝对阈值（[0.10,0.12]、avgTop1≥0.09）在分布错位时**静默归零**（恒空仓）。

本模块把"绝对阈值"换成"由当日分数分布推得的百分位阈值"：
- 阈值与被测数据同分布 ⇒ **尺度等变**（分数整体放大/缩小/平移，选股结果不变）；
- 分位参数恒为 0..1 ⇒ 任何模型尺度都不可能再错位（量纲回归测试锁定）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class QuantileThresholdProfile:
    """分位参数（全 0..1；可按策略风格调参）。"""

    score_min_q: float = 0.98  # 个股带下界（≈ 前 2%）
    score_max_q: float = (
        1.0  # 个股带上界（分位模式无"过热上限"，由行业门/趋势过滤承担）
    )
    entry_avg_top1_q: float = 0.90  # 行业 avgTop1 入场阈值分位
    exit_avg_top1_q: float = 0.70  # 行业 avgTop1 空仓阈值分位
    strong_top1_q: float = 0.98  # 强行业判定（行业 Top1）分位
    strong_industry_min: int = 2  # 强行业数下限（沿用绝对模式口径）


DEFAULT_PROFILE = QuantileThresholdProfile()


@dataclass(frozen=True)
class ThresholdSet:
    """解析后的阈值（分数空间；由当日分布推得，含审计留痕）。"""

    mode: str
    score_min: float
    score_max: float
    entry_avg_top1: float
    exit_avg_top1: float
    strong_top1: float
    strong_industry_min: int
    quantiles: dict[str, Any] = field(default_factory=dict)


def _percentile_nearest_rank(ordered: Sequence[float], q: float) -> float:
    """最近秩分位（与 shadow_compare/_percentile 同口径）；ordered 必须已升序。"""
    n = len(ordered)
    if n == 0:
        raise ValueError("empty")
    rank = max(1, min(n, math.ceil(float(q) * n)))
    return float(ordered[rank - 1])


def resolve_thresholds(
    scores: Sequence[float],
    profile: QuantileThresholdProfile | None = None,
) -> ThresholdSet | None:
    """分数序列 → 分位阈值集；空输入返回 None。纯函数、无 IO。"""
    prof = profile or DEFAULT_PROFILE
    values = sorted(
        float(s) for s in scores if s is not None and math.isfinite(float(s))
    )
    if not values:
        return None
    p = lambda q: _percentile_nearest_rank(values, q)  # noqa: E731
    return ThresholdSet(
        mode="quantile",
        score_min=p(prof.score_min_q),
        score_max=p(prof.score_max_q),
        entry_avg_top1=p(prof.entry_avg_top1_q),
        exit_avg_top1=p(prof.exit_avg_top1_q),
        strong_top1=p(prof.strong_top1_q),
        strong_industry_min=int(prof.strong_industry_min),
        quantiles={
            "score_min_q": prof.score_min_q,
            "score_max_q": prof.score_max_q,
            "entry_avg_top1_q": prof.entry_avg_top1_q,
            "exit_avg_top1_q": prof.exit_avg_top1_q,
            "strong_top1_q": prof.strong_top1_q,
        },
    )


def thresholds_to_dict(thresholds: ThresholdSet | None) -> dict[str, Any] | None:
    """稳定序列化（审计/meta 用）。"""
    if thresholds is None:
        return None
    return {
        "mode": thresholds.mode,
        "score_min": thresholds.score_min,
        "score_max": thresholds.score_max,
        "entry_avg_top1": thresholds.entry_avg_top1,
        "exit_avg_top1": thresholds.exit_avg_top1,
        "strong_top1": thresholds.strong_top1,
        "strong_industry_min": thresholds.strong_industry_min,
        "quantiles": dict(thresholds.quantiles),
    }


def market_state_quantile(
    avg_top1: float | None, thresholds: ThresholdSet | None
) -> str:
    """分位口径市场状态（训练页/选股链/扫描器三方共用）。

    绝对阶梯（avgTop1≥0.12/0.10/0.09/0.06）在窄分布模型下会把一切判为"熊市"；
    分位口径按当日分布定位：≥强行业分位=牛市、≥入场分位=偏强、
    ≥入场/空仓均值=震荡、≥空仓分位=偏弱、否则熊市。
    """
    if thresholds is None or avg_top1 is None:
        return "无信号"
    value = float(avg_top1)
    if value >= thresholds.strong_top1:
        return "牛市"
    if value >= thresholds.entry_avg_top1:
        return "震荡偏强"
    mid = (thresholds.entry_avg_top1 + thresholds.exit_avg_top1) / 2.0
    if value >= mid:
        return "震荡"
    if value >= thresholds.exit_avg_top1:
        return "震荡偏弱"
    return "熊市"
