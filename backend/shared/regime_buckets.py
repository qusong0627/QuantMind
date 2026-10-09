"""Regime 分桶统计（P3 · 设计 §6.3/§6.4 共用）：daily IC × 状态时间线 → 分桶证据。

**纯函数模块**（不碰 DB、不碰文件），两个消费方共用同一谓词，禁止各写一套：

- 评估层（§6.3，``scripts/eval/model_card.py``）：``compute_regime_block`` 生成
  模型卡「状态依赖」区块 + 随侧车落盘；
- 异常引擎（§6.4，``services/engine/anomaly_detectors.py``）：``is_weak_bucket`` /
  ``within_expectation`` 判断「当期 IC 是否落在弱区历史期望内」。

口径纪律：
- **弱区谓词唯一实现**：桶 ``mean_ic ≤ 0`` 且 ``n_days ≥ MIN_BUCKET_DAYS``（15）；
- **join 按信号日**：IC 日期 = 信号日，直接对 ``qm_regime_daily`` 的生效日
  （状态由截至前一交易日的行情算出——配对无前视）；
- **缺失如实计数**：IC 有、regime 无的日期计入 ``coverage.missing_regime_days``，
  不进任何桶、不补假值；
- **展示口径（强制）**：最差月与月间 std 优先；年均 IC 单值不作为对外口径
  （记忆：模型 IC 是市值风格的下注）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from backend.shared.market_regime import POSITION_BY_STATE, STATES

#: 弱区最小样本：桶内不足 15 天不下「弱区」结论（spec §6.4）
MIN_BUCKET_DAYS = 15

DISPLAY_NOTE = (
    "展示口径（强制）：最差月与月间 std 优先，弱区单独标注；"
    "禁止用年均 IC 单值对外（模型 IC 是市值风格的下注）"
)


def _points(points: Iterable[Any]) -> list[tuple[str, float]]:
    """归一输入：收 ``{"date","value"}`` 或 ``(date, value)`` 两种形态，剔 None/NaN。"""
    out: list[tuple[str, float]] = []
    for item in points or []:
        if isinstance(item, dict):
            day, value = item.get("date"), item.get("value")
        else:
            day, value = item[0], item[1]
        if day is None or value is None:
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            continue
        if math.isnan(val) or math.isinf(val):
            continue
        out.append((str(day), val))
    return out


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _std(values: list[float]) -> float | None:
    """样本 std（ddof=1）；不足两个值 → None（不是 0——0 会假装「无波动」）。"""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return round(math.sqrt(var), 6)


def bucket_of(daily_ic: Iterable[Any], states: dict[str, str], state: str) -> dict[str, Any]:
    """单桶统计：join 后该状态的 n_days / mean_ic / ic_std / hit_rate。"""
    values = [val for day, val in _points(daily_ic) if states.get(day) == state]
    hits = [v for v in values if v > 0]
    stats: dict[str, Any] = {
        "n_days": len(values),
        "mean_ic": _mean(values),
        "ic_std": _std(values),
        "hit_rate": round(len(hits) / len(values), 4) if values else None,
    }
    stats["weak"] = is_weak_bucket(stats)
    return stats


def is_weak_bucket(stats: dict[str, Any]) -> bool:
    """弱区谓词唯一实现：``mean_ic ≤ 0`` 且 ``n_days ≥ MIN_BUCKET_DAYS``。"""
    mean_ic = stats.get("mean_ic")
    n_days = int(stats.get("n_days") or 0)
    return mean_ic is not None and float(mean_ic) <= 0 and n_days >= MIN_BUCKET_DAYS


def within_expectation(value: float | None, stats: dict[str, Any], *, k: float = 2.0) -> bool:
    """``value`` 是否落在该桶历史分布 ``kσ`` 内（§6.4 归因降压条件）。

    样本不足（无值 / std 不可算）→ **False**：证不出「在期望内」就不降级，
    这是「用 regime 降误报」与「用 regime 掩盖真断链」的分界。
    """
    if value is None or stats.get("mean_ic") is None or stats.get("ic_std") is None:
        return False
    return abs(float(value) - float(stats["mean_ic"])) <= k * float(stats["ic_std"])


def position_reduction_advice(
    state: str | None,
    stats: dict[str, Any] | None,
    *,
    full_factor: float = 1.0,
) -> dict[str, Any] | None:
    """§6.5-L2 降险建议：状态属模型历史弱区 → 按 ``POSITION_BY_STATE`` 阶梯下调仓位系数。

    **纯函数、只建议不执行**（人工确认；实际减仓由风控链承接）。口径：

    - 判据 = 弱区谓词唯一实现 :func:`is_weak_bucket`（与 §6.4 同源）且 ``state`` 有效；
    - ``to_factor`` = ``POSITION_BY_STATE[state]``（1.0/0.7/0.3，与实时轨 position_hint 同源）；
    - 无下调空间（阶梯值 ≥ ``full_factor``，如弱区出现在 bull，阶梯已 1.0）→ **None**：
      诚实不出建议，不造「1.0→1.0」的假动作；
    - 缺状态 / 缺统计 / 词汇表外状态 → None。
    """
    if not state or not stats or not is_weak_bucket(stats):
        return None
    to_factor = POSITION_BY_STATE.get(str(state))
    if to_factor is None or to_factor >= float(full_factor):
        return None
    return {
        "state": str(state),
        "from_factor": float(full_factor),
        "to_factor": float(to_factor),
        "bucket_mean_ic": stats.get("mean_ic"),
        "bucket_ic_std": stats.get("ic_std"),
        "bucket_days": int(stats.get("n_days") or 0),
        "ladder": dict(POSITION_BY_STATE),
    }


def monthly_summary(daily_ic: Iterable[Any]) -> dict[str, Any]:
    """按月聚合 → {months, worst_month, month_std, n_months}（展示口径的骨架）。"""
    by_month: dict[str, list[float]] = {}
    for day, val in _points(daily_ic):
        by_month.setdefault(day[:7], []).append(val)
    months = [
        {"month": month, "mean_ic": _mean(vals), "n_days": len(vals)}
        for month, vals in sorted(by_month.items())
    ]
    means = [m["mean_ic"] for m in months if m["mean_ic"] is not None]
    worst = min(months, key=lambda m: m["mean_ic"]) if means else None
    return {
        "months": months,
        "worst_month": worst,
        "month_std": _std(means),
        "n_months": len(months),
    }


def compute_regime_block(
    daily_ic: Iterable[Any],
    states: dict[str, str],
    *,
    market: str,
    index: str | None,
) -> dict[str, Any]:
    """「状态依赖」区块（模型卡 + 侧车共用载荷；纯函数）。"""
    points = _points(daily_ic)
    buckets = {state: bucket_of(points, states, state) for state in STATES}
    joined = sum(b["n_days"] for b in buckets.values())
    weak = [state for state in STATES if buckets[state]["weak"]]
    monthly = monthly_summary(points)
    dates = [day for day, _ in points]
    block: dict[str, Any] = {
        "market": market,
        "index": index,
        "buckets": buckets,
        "weak_buckets": weak,
        "worst_month": monthly["worst_month"],
        "month_std": monthly["month_std"],
        "n_months": monthly["n_months"],
        "monthly_ic": monthly["months"],
        "coverage": {
            "ic_days": len(points),
            "joined_days": joined,
            "missing_regime_days": len(points) - joined,
            "regime_rows": len(states),
            "first_ic_date": dates[0] if dates else None,
            "last_ic_date": dates[-1] if dates else None,
        },
        "display_note": DISPLAY_NOTE,
    }
    notes: list[str] = []
    if weak:
        detail = "、".join(
            f"{s}（IC={buckets[s]['mean_ic']}，{buckets[s]['n_days']} 天）" for s in weak
        )
        notes.append(f"弱区标注：{detail}——该状态下模型无正向预测力")
    if block["coverage"]["missing_regime_days"] > 0:
        notes.append(
            f"{block['coverage']['missing_regime_days']} 个 IC 日无 regime 行"
            "（未 join 任何桶；时间线回填后可重算）"
        )
    empty = [s for s in STATES if buckets[s]["n_days"] == 0]
    if empty:
        notes.append(f"空桶（样本 0 天，不下结论）：{'、'.join(empty)}")
    block["notes"] = notes
    return block
