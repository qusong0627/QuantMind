"""模型长序列载荷（设计 §1.6）：把 `_pred_stats` 的统计摊平成前端要的曲线。

**为什么单独一个模块**：`model_realized.py` 已近 800 行上限，而这段逻辑是纯摊平
（不重算、不近似），与评分口径无关，单独放便于单测。

**两条纪律**：

1. **图上数与卡上分同源**：这里只搬运 `_pred_stats` 已算好的统计，绝不重算——
   否则「分数 82、曲线却是另一组数」这种偏差没人能发现；
2. **算不出来的序列要写明为什么**：模型 `pred.parquet` 只有单一持有期标签，
   IC 衰减（按 horizon）本就不可算。如实写进 ``notes``，不画一条平的假衰减
   （那正是「没证据」被看成了「证据」）。
"""

from __future__ import annotations

from typing import Any

# 单条曲线的点数上限：超过就留最近的点（近端才是决策依据）
MAX_SERIES_POINTS = 2000

_BUCKET_ORDER_NOTE = "bucket 1 = 预测值最低档（A股口径：做多排前的在最高档）"
_IC_DECAY_NOTE = (
    "IC 衰减按持有期需要多周期标签，而 pred.parquet 只有单一持有期的 label —— "
    "不可算，不给平线占位"
)


def truncate_series(points: list[Any]) -> tuple[list[Any], str]:
    """超长序列留尾 → (截断后序列, 说明)。说明为空串表示未截断。"""
    if len(points) <= MAX_SERIES_POINTS:
        return points, ""
    dropped = len(points) - MAX_SERIES_POINTS
    return (
        points[-MAX_SERIES_POINTS:],
        f"序列共 {len(points)} 点，只保留最近 {MAX_SERIES_POINTS} 点（截去最早 {dropped} 点）",
    )


def _ic_points(ic_by_date: Any) -> list[dict[str, Any]]:
    """逐日 IC 字典 → 升序点列（None 值剔除）。"""
    if not isinstance(ic_by_date, dict):
        return []
    return [
        {"date": str(day), "value": float(val)}
        for day, val in sorted(ic_by_date.items())
        if val is not None
    ]


def _decile_points(strat: dict[str, Any]) -> list[dict[str, Any]]:
    """各档日均收益 → 点列（下标即档位，从 1 起）。"""
    means = strat.get("mean_returns") or []
    return [
        {"bucket": idx, "value": float(val)}
        for idx, val in enumerate(means, start=1)
        if val is not None
    ]


def _segment_points(robust: dict[str, Any]) -> list[dict[str, Any]]:
    """分段 IC（by_split / by_year）→ 按标签升序的点列。"""
    by_seg = robust.get("by_split") or {}
    if not isinstance(by_seg, dict):
        return []
    out = []
    for label, stats in sorted(by_seg.items(), key=lambda kv: str(kv[0])):
        if not isinstance(stats, dict) or stats.get("ic_mean") is None:
            continue
        out.append(
            {
                "label": str(label),
                "value": float(stats["ic_mean"]),
                "n_days": stats.get("n_days"),
                "is_min_segment": str(label) == str(robust.get("min_segment") or ""),
            }
        )
    return out


def _turnover_points(cost: dict[str, Any]) -> list[dict[str, Any]]:
    series = cost.get("turnover_series") or []
    return [
        {"pair": idx + 1, "value": float(val)}
        for idx, val in enumerate(series)
        if val is not None
    ]


def _note_for(name: str, stats: dict[str, Any], fallback: str) -> str:
    """缺省原因：优先带出该统计自带的 ``reason``（它知道缺的是什么）。"""
    if stats.get("sufficient"):
        return ""
    return str(stats.get("reason") or fallback)


def series_from_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """`_pred_stats` 输出 → ``{series, scalars, notes}``（前端 ``/eval/series`` 载荷）。"""
    strat = stats.get("strat") or {}
    robust = stats.get("robust") or {}
    health = stats.get("health") or {}
    cost = stats.get("cost") or {}

    daily_ic = truncate_series(_ic_points(stats.get("daily_ic")))
    deciles = _decile_points(strat)
    segments = _segment_points(robust)
    turnover = truncate_series(_turnover_points(cost))

    notes: dict[str, Any] = {
        "ic_decay": _IC_DECAY_NOTE,
        "bucket_order": _BUCKET_ORDER_NOTE,
        "daily_ic": _note_for(
            "daily_ic",
            health,
            "逐日 IC 序列缺省（不足两个有效交易日或全部为秩退化日）",
        )
        or daily_ic[1],
        "decile_mean": _note_for(
            "decile_mean", strat, "分档统计缺省（有效交易日不足或票数不够）"
        ),
        "segment_ic": _note_for(
            "segment_ic", robust, "分段统计缺省（子样本不足或无有效 IC）"
        ),
        "turnover": _note_for("turnover", cost, "换手缺省（有效交易日不足两个）"),
    }
    return {
        "series": {
            "daily_ic": daily_ic[0],
            "decile_mean": deciles,
            "segment_ic": segments,
            "turnover": turnover[0],
        },
        "scalars": {
            "ic_mean_20": health.get("ic_mean_20"),
            "ic_mean_60": health.get("ic_mean_60"),
            "ic_mean": health.get("ic_mean"),
            "icir": health.get("icir"),
            "ls_mean": strat.get("ls_mean"),
            "ls_ir": strat.get("ls_ir"),
            "monotonicity": strat.get("monotonicity"),
            "turnover_mean": cost.get("turnover_mean"),
            "cost_drag_annual": cost.get("cost_drag_annual"),
            "round_trip_cost": cost.get("round_trip_cost"),
            "n_days_ic": health.get("n_days"),
            "n_days_strat": strat.get("n_days"),
        },
        "notes": notes,
    }
