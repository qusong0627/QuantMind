"""策略长序列载荷（设计 §1.6）：回测结果 JSON → 前端详情图。

策略是六类里序列最富的一类：`qlib_backtest_runs.result_file_path` 的结果 JSON
带逐日净值、逐日回撤、逐笔成交；净值曲线还能聚合出月度收益（与稳定性维同口径）。

**三条纪律**：

1. **回撤曲线有两种键名**（实测 66 份在盘结果：51 份 ``{date, drawdown}``、
   10 份 ``{date, drawdown, value}``、2 份只有净值没有回撤曲线）。只认 ``value``
   会把 51 份读成一条 0 线——「没证据」被当成「没回撤」，是最坏的一种静默失败；
2. **缺测不是 0**：值缺失的点丢弃并计数，写进 ``notes``；
3. **两条曲线共用 x 轴**：回撤只保留净值曲线覆盖到的日期，否则同图两轴不可比。
"""

from __future__ import annotations

import math
from typing import Any

# 单条曲线点数上限（与 factor_series / model_series 同口径）
MAX_SERIES_POINTS = 2000

# 回撤点可能用的键名（`drawdown` 为实测多数派，`value` 是新结果的并存列）
_VALUE_KEYS = ("drawdown", "value")


def _as_float(raw: Any) -> float | None:
    """数值化；None / 非数 / NaN / inf → None（缺测，不是 0）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_drawdown_entries(raw: Any) -> tuple[list[tuple[str, float]], int]:
    """``drawdown_curve`` → ``([(date, value)], 丢弃点数)``。

    兼容三种形态：``{date, drawdown}``、``{date, drawdown, value}``、纯数字列表。
    日期可能为空串（老形态无日期），由调用方决定如何对齐。
    """
    if not isinstance(raw, (list, tuple)):
        return [], 0
    points: list[tuple[str, float]] = []
    dropped = 0
    for item in raw:
        if isinstance(item, dict):
            value = None
            for key in _VALUE_KEYS:
                value = _as_float(item.get(key))
                if value is not None:
                    break
            date_text = str(item.get("date") or "")
        else:
            value = _as_float(item)
            date_text = ""
        if value is None:
            dropped += 1
            continue
        points.append((date_text, value))
    return points, dropped


def _equity_points(
    dates: list[str], equity: list[Any], max_points: int, dropped: int = 0
) -> tuple[list[dict[str, Any]], int, int, str]:
    """(图上点列, 丢弃点数, 可用点总数, 截断说明)。

    ``dropped`` 是调用方**已经**剔除的点数（装载层先过了一道类型闸门），这里
    继续累加——两层的缺失都要进 notes，否则「读进来时丢了几个点」无人知晓。
    """
    points: list[dict[str, Any]] = []
    for day, raw in zip(dates, equity, strict=False):
        value = _as_float(raw)
        if value is None:
            dropped += 1
            continue
        points.append({"date": str(day), "value": value})
    total = len(points)
    if total <= max_points:
        return points, dropped, total, ""
    return (
        points[-max_points:],
        dropped,
        total,
        f"净值序列共 {total} 点，只保留最近 {max_points} 点（截去最早 {total - max_points} 点）",
    )


def _drawdown_points(
    entries: list[tuple[str, Any]], dropped: int, equity_dates: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """回撤点列（只留净值曲线覆盖的日期）+ 需要写进 notes 的原因列表。

    ``dropped`` 是解析阶段已剔除的点数；这里再挡一道非有限值，两者合并计数
    （调用方可能直接给 ``[(date, value)]``，不保证已经过 :func:`parse_drawdown_entries`）。
    """
    points: list[dict[str, Any]] = []
    undated = 0
    off_axis = 0
    for day, raw in entries:
        value = _as_float(raw)
        if value is None:
            dropped += 1
            continue
        day_text = str(day or "")
        if not day_text:
            undated += 1
            continue
        if day_text not in equity_dates:
            off_axis += 1
            continue
        points.append({"date": day_text, "value": value})

    reasons: list[str] = []
    if not entries:
        reasons.append("回测结果没有 drawdown_curve（回撤曲线缺省，不给 0 线占位）")
    if dropped:
        reasons.append(f"{dropped} 个回撤点值为空已剔除（缺测不按 0 回撤处理）")
    if undated:
        reasons.append(
            f"{undated} 个回撤点没有日期，无法与净值曲线对齐（不按序号硬凑）"
        )
    if off_axis:
        reasons.append(f"{off_axis} 个回撤点不在净值曲线的日期上，已剔除（两轴不可比）")
    if entries and not points and not reasons:
        reasons.append("回撤点全部无法使用")
    return points, reasons


def _window_return(points: list[dict[str, Any]]) -> float | None:
    """窗口收益 = 末值/首值 − 1（首值 ≤ 0 时不给数，避免编出天文数字）。"""
    if len(points) < 2:
        return None
    first = float(points[0]["value"])
    last = float(points[-1]["value"])
    if first <= 0:
        return None
    return last / first - 1.0


def strategy_series_payload(
    *,
    dates: list[str],
    equity: list[Any],
    drawdown_entries: list[tuple[str, float]] | None,
    drawdown_dropped: int = 0,
    equity_dropped: int = 0,
    monthly: list[tuple[str, float]] | None = None,
    scalars: dict[str, Any] | None = None,
    max_points: int = MAX_SERIES_POINTS,
) -> dict[str, Any]:
    """回测曲线 → ``{series, scalars, notes}``（前端 ``/eval/series`` 载荷）。

    ``scalars`` 由策略卡传入（年化、最大回撤等**卡上已算好**的数），本函数不重算
    这些口径——图上的数与卡上的分必须同源。
    """
    eq_points, eq_dropped, eq_total, truncated = _equity_points(
        dates, equity, max_points, int(equity_dropped)
    )
    eq_dates = {str(p["date"]) for p in eq_points}
    dd_points, dd_reasons = _drawdown_points(
        list(drawdown_entries or []), int(drawdown_dropped), eq_dates
    )

    monthly_points = [
        {"label": str(label), "value": float(value)}
        for label, value in (monthly or [])
        if _as_float(value) is not None
    ]

    notes: dict[str, Any] = {
        "equity": "；".join(
            filter(
                None,
                [
                    "" if eq_points else "净值曲线为空（回测结果缺 equity_curve）",
                    truncated,
                    f"{eq_dropped} 个净值点值非法已剔除（不补前值）"
                    if eq_dropped
                    else "",
                ],
            )
        ),
        "drawdown": "；".join(dd_reasons),
        "monthly_return": (
            "" if monthly_points else "月度收益缺省（净值曲线不足两个月，或日期列缺失）"
        ),
    }

    out_scalars: dict[str, Any] = {
        # n_days = 回测窗口的真实长度（截断前可用点数）；n_points = 实际下发的点数
        "n_days": eq_total,
        "n_points": len(eq_points),
        "start": eq_points[0]["date"] if eq_points else "",
        "end": eq_points[-1]["date"] if eq_points else "",
        "window_return": _window_return(eq_points),
        # 回撤序列自己算一个（没有回撤序列时是 None，不是 0）
        "max_drawdown": min((p["value"] for p in dd_points), default=None),
    }
    out_scalars.update(scalars or {})

    return {
        "series": {
            "equity": eq_points,
            "drawdown": dd_points,
            "monthly_return": monthly_points,
        },
        "scalars": out_scalars,
        "notes": notes,
    }
