"""每日选股长序列载荷（设计 §1.6）：跨日期的 `eval_scores` 行 → 详情图。

每日选股天然就是时间序列：每个交易日一行评分卡，序列 = 跨日期的
「入选数 / 事后 T+H 超额 / 命中率」。这条线会自己生长——`backfill_recent.py`
在窗口闭合后幂等重算，回填一到位当天的超额就出现在曲线上。

**两条纪律**：

1. **待回填的日子不进曲线**：T+H 前向数据没齐就跳过并计数，绝不补 0——补 0 会被
   读成「那天超额是 0」，而事实是「还不知道」；
2. **维度缺省不猜**：``dimensions.coverage.detail.picked`` 缺失就当这天不参与，
   不拿别处数字顶上。

每个日期各自的侧车只装**截至该日**的历史（调用侧按日期切片），避免详情页把未来
数据画进「当时看不到」的曲线里。
"""

from __future__ import annotations

import math
from typing import Any

# 单条曲线点数上限：超过只留最近的点（近端才是决策依据）
MAX_SERIES_POINTS = 240


def _as_float(raw: Any) -> float | None:
    """数值化；None / 非数 / NaN / inf → None（缺测，不是 0）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _detail(row: Any, dim_key: str) -> dict[str, Any] | None:
    """取某维的 detail（维度不存在 / detail 不是字典 → None）。"""
    if not isinstance(row, dict):
        return None
    dimensions = row.get("dimensions")
    if not isinstance(dimensions, dict):
        return None
    dim = dimensions.get(dim_key)
    if not isinstance(dim, dict):
        return None
    detail = dim.get("detail")
    return detail if isinstance(detail, dict) else None


def selection_point(row: Any) -> dict[str, Any] | None:
    """一行 ``eval_scores``（每日选股）→ 序列点；没有日期 → None。"""
    if not isinstance(row, dict):
        return None
    day = str(row.get("snapshot_date") or "").strip()
    if not day:
        return None

    coverage = _detail(row, "coverage")
    realized = _detail(row, "realized")
    picked = _as_float((coverage or {}).get("picked"))

    pending = bool((realized or {}).get("pending"))
    mean_excess = None if pending else _as_float((realized or {}).get("mean_excess"))
    hit_rate = None if pending else _as_float((realized or {}).get("hit_rate"))
    horizon = _as_float((realized or {}).get("horizon"))

    return {
        "date": day,
        "picked": picked,
        "mean_excess": mean_excess,
        "hit_rate": hit_rate,
        "pending": pending,
        "horizon": int(horizon) if horizon else None,
        "has_realized": realized is not None,
    }


def _dedupe_sorted(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同日多行只留最后一行（重跑写了新快照），再按日期升序。"""
    by_date: dict[str, dict[str, Any]] = {}
    for point in points:
        by_date[point["date"]] = point
    return [by_date[day] for day in sorted(by_date)]


def _series_from(
    window: list[dict[str, Any]], field: str, *, as_int: bool = False
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for point in window:
        value = _as_float(point.get(field))
        if value is None:
            continue
        points.append({"date": point["date"], "value": int(value) if as_int else value})
    return points


def history_upto(
    history: list[dict[str, Any]] | None, day: str
) -> list[dict[str, Any]]:
    """截至 ``day``（含当日）的历史行。

    **详情页不许把当时看不到的未来画进曲线**：某日的侧车只装该日及以前的行，
    否则「2026-09-15 的选股质量曲线」里会出现 09-18 的成绩。ISO 日期串按字典序
    比较即等价于按时间比较。
    """
    day_text = str(day or "")
    if not day_text:
        return []
    out: list[dict[str, Any]] = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        row_day = str(row.get("snapshot_date") or "")
        if row_day and row_day <= day_text:
            out.append(row)
    return out


def daily_selection_series_payload(
    history: list[dict[str, Any]] | None,
    *,
    scalars: dict[str, Any] | None = None,
    max_points: int = MAX_SERIES_POINTS,
    extra_note: str = "",
) -> dict[str, Any]:
    """跨日期评分行 → ``{series, scalars, notes}``。

    ``history`` 是 ``[{snapshot_date, dimensions}]``（顺序不限，同日多行留最后一行）；
    调用方负责先按日期切片（见 :func:`history_upto`）。``extra_note`` 用来带出
    「历史读取失败」这类缺数据的原因——缺数据可以，不说原因不行。``scalars`` 由
    调用方补当日卡片自己的数（评分/评级）——图上数字与卡上分同源。
    """
    raw_points = [p for p in (selection_point(row) for row in (history or [])) if p]
    points = _dedupe_sorted(raw_points)
    n_dates = len(points)

    truncated = ""
    window = points
    if n_dates > max_points:
        window = points[-max_points:]
        truncated = (
            f"历史共 {n_dates} 天，曲线只画最近 {max_points} 天"
            f"（截去最早 {n_dates - max_points} 天）"
        )

    picked = _series_from(window, "picked")
    excess = _series_from(window, "mean_excess")
    hit = _series_from(window, "hit_rate")

    n_skipped = sum(1 for p in window if p["picked"] is None)
    n_pending = sum(1 for p in window if p["pending"])
    n_no_realized = sum(
        1 for p in window if not p["pending"] and p["mean_excess"] is None
    )
    horizons = sorted({p["horizon"] for p in window if p["horizon"]})
    horizon_text = f"T+{horizons[0]}" if len(horizons) == 1 else "T+H"

    picked_notes = [
        part
        for part in (
            truncated,
            f"{n_skipped} 个交易日没有入选数维度（不拿别处数字顶上）"
            if n_skipped
            else "",
            "没有历史行（该对象还没跑过评分卡）" if not n_dates else "",
        )
        if part
    ]
    realized_notes = [
        part
        for part in (
            truncated,
            f"{n_pending} 个交易日的 {horizon_text} 前向数据未齐（待回填）"
            if n_pending
            else "",
            f"{n_no_realized} 个交易日没有事后验证维度" if n_no_realized else "",
            "这些天不在曲线上（不假填 0）" if (n_pending or n_no_realized) else "",
            "没有历史行（该对象还没跑过评分卡）" if not n_dates else "",
        )
        if part
    ]

    latest = window[-1] if window else None
    out_scalars: dict[str, Any] = {
        "n_dates": n_dates,
        "n_pending": n_pending,
        "n_skipped": n_skipped,
        "latest_date": latest["date"] if latest else "",
        "latest_picked": latest["picked"] if latest else None,
        "latest_excess": latest["mean_excess"] if latest else None,
        "latest_hit_rate": latest["hit_rate"] if latest else None,
        "horizon": horizons[0] if len(horizons) == 1 else None,
    }
    out_scalars.update(scalars or {})

    def _notes(parts: list[str]) -> str:
        return "；".join([*parts, extra_note] if extra_note else parts)

    return {
        "series": {
            "picked": picked,
            "realized_excess": excess,
            "hit_rate": hit,
        },
        "scalars": out_scalars,
        "notes": {
            "picked": _notes(picked_notes),
            "realized_excess": _notes(realized_notes),
            "hit_rate": _notes(realized_notes),
        },
    }
