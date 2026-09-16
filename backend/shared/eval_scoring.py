"""评分方法统一引擎（T-P4-05b）——**纯函数唯一实现**（设计《评估与打分体系》§三）。

统一算法：
1) 每维度指标 → 得分映射：截面分位模式（本批对象内 winsorize 5%/95% 后分位映射）
   或绝对模式（阈值表线性/阶梯映射，用于跨期与红线）；
2) 总分 = Σ(维度分 × 权重)（权重和按 100 计；维度缺失 → 剩余权重归一，如实标注）；
3) 红线维度不合格 → 总分封顶 min(总分, 59)；
4) 评级：A ≥85 / B 70-84 / C 60-69 / D <60；低置信（样本不足）→ 评级附 "†"。

纪律：分数不是目的——每个维度必须带 ``detail`` 原始指标，可下钻可复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Sequence

GRADE_A_MIN = 85
GRADE_B_MIN = 70
GRADE_C_MIN = 60
RED_LINE_CAP = 59


def winsorize(
    values: Sequence[float], lower: float = 0.05, upper: float = 0.95
) -> list[float]:
    """按分位裁剪（5%/95%，§三 防单一异常值）；空输入原样返回。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return []
    ordered = sorted(vals)
    n = len(ordered)
    lo = ordered[max(0, min(n - 1, int(lower * n)))]
    hi = ordered[max(0, min(n - 1, int(upper * n) - 1))] if n > 1 else ordered[0]
    if lo > hi:
        lo, hi = hi, lo
    return [min(max(v, lo), hi) for v in vals]


def score_from_quantile(values: Sequence[float], value: float) -> float:
    """截面分位映射：value 在本批（winsorize 后）中的百分位 × 100。"""
    vals = winsorize(values)
    if not vals or value is None:
        return 50.0
    below = sum(1 for v in vals if v < float(value))
    equal = sum(1 for v in vals if v == float(value))
    return round((below + 0.5 * equal) / len(vals) * 100.0, 2)


def score_from_thresholds(
    value: float | None,
    thresholds: Sequence[tuple[float, float]],
    *,
    higher_is_better: bool = True,
) -> float | None:
    """绝对模式：阈值表线性映射。

    ``thresholds``：[(指标值, 分数)]，按指标值升序；higher_is_better=True 时
    指标越大得分越高（反之取反）。超出两端按端点截断；value=None → None。
    """
    if value is None or not thresholds:
        return None
    pts = sorted(thresholds, key=lambda x: x[0])
    v = float(value)
    if v <= pts[0][0]:
        return float(pts[0][1])
    if v >= pts[-1][0]:
        return float(pts[-1][1])
    for (v0, s0), (v1, s1) in zip(pts, pts[1:], strict=False):
        if v0 <= v <= v1:
            ratio = (v - v0) / (v1 - v0) if v1 > v0 else 0.0
            score = s0 + ratio * (s1 - s0)
            return round(score if higher_is_better else _mirror(score), 2)
    return None


def _mirror(score: float) -> float:
    return 100.0 - score


@dataclass(frozen=True)
class DimensionScore:
    """单维度评分（带原始指标证据与红线标记）。"""

    key: str
    label: str
    weight: float
    score: float | None  # None = 该维度本期不可评（权重重归一）
    red_line_failed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def grade_for(total: float, low_confidence: bool = False) -> str:
    if total >= GRADE_A_MIN:
        grade = "A"
    elif total >= GRADE_B_MIN:
        grade = "B"
    elif total >= GRADE_C_MIN:
        grade = "C"
    else:
        grade = "D"
    return grade + ("†" if low_confidence else "")


def combine_dimension_scores(
    dims: list[DimensionScore], *, low_confidence: bool = False
) -> dict[str, Any]:
    """加权合成：缺失维度按剩余权重归一并标注；红线 → 封顶 59；输出评级与明细。"""
    available = [d for d in dims if d.score is not None]
    missing = [d.key for d in dims if d.score is None]
    weight_sum = sum(float(d.weight) for d in available)
    if weight_sum <= 0:
        return {
            "score": None,
            "grade": None,
            "low_confidence": True,
            "missing_dims": missing,
            "red_line_failed": [d.key for d in dims if d.red_line_failed],
            "capped": False,
            "dimensions": {
                d.key: {
                    "label": d.label,
                    "score": d.score,
                    "weight": d.weight,
                    "red_line_failed": d.red_line_failed,
                    "detail": d.detail,
                }
                for d in dims
            },
            "detail": {d.key: d.detail for d in dims},
            "note": "全部维度不可评",
        }
    raw_total = sum(float(d.score) * float(d.weight) for d in available) / weight_sum
    red_failed = [d.key for d in dims if d.red_line_failed]
    capped = bool(red_failed)
    total = min(raw_total, RED_LINE_CAP) if capped else raw_total
    total = round(total, 1)
    return {
        "score": total,
        "raw_score": round(raw_total, 1),
        "grade": grade_for(total, low_confidence),
        "red_line_failed": red_failed,
        "capped": capped,
        "low_confidence": bool(low_confidence),
        "missing_dims": missing,
        "weights_used": {
            d.key: round(float(d.weight) / weight_sum, 4) for d in available
        },
        "dimensions": {
            d.key: {
                "label": d.label,
                "score": d.score,
                "weight": d.weight,
                "red_line_failed": d.red_line_failed,
                "detail": d.detail,
            }
            for d in dims
        },
    }
