"""因子长序列载荷（设计 §1.6）：因子面板 → 前端详情图。

`factor_series.parquet` 里每个因子每一天一行，列已经备好：``ic``（当期）、
``ic_1/2/5/10/20``（不同持有期）、``q1..q10``（十分位组合收益）。本模块只做
**摊平 + 分段聚合**，不重算面板——图上数与卡上分同源。

与模型不同，**因子的 IC 衰减是真能算的**（面板自带多周期列），所以这里给出
``ic_decay``；模型那边只有单一持有期标签，只能如实标注「不可算」。
"""

from __future__ import annotations

from typing import Any

import numpy as np

# 面板自带的持有期列（顺序即展示顺序）
HORIZONS = (1, 2, 5, 10, 20)
# 十分位列
DECILES = tuple(f"q{i}" for i in range(1, 11))
# 单条曲线点数上限（与 model_series 同口径）
MAX_SERIES_POINTS = 2000


def _date_iso(raw: Any) -> str:
    """面板日期（int32 ``YYYYMMDD``）→ ISO；已是字符串则原样。"""
    text = str(raw)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text


def _mean(frame: Any, col: str) -> float | None:
    """列均值（全 NaN → None，不返回 0——0 会被读成「真的一点效果都没有」）。"""
    if col not in getattr(frame, "columns", ()):
        return None
    values = np.asarray(frame[col].to_numpy(dtype=float), dtype=float)
    values = values[np.isfinite(values)]
    return None if values.size == 0 else float(values.mean())


def top_correlations(
    names: list[str], matrix: list[list[Any]], index: int, *, limit: int = 5
) -> list[dict[str, Any]]:
    """相关矩阵单行 → 最相关的 k 个因子（``[{name, value}]``，|相关| 降序）。

    「有没有重复」这个问题只报一个 ``max_corr`` 数字看不出跟谁重——把对手名字一起
    带上；矩阵行里 None/非法值跳过（缺测不等于 0 相关）。
    """
    if index < 0 or index >= len(names) or index >= len(matrix):
        return []
    row = matrix[index] or []
    pairs: list[tuple[str, float]] = []
    for j, name in enumerate(names):
        if j == index or j >= len(row):
            continue
        raw = row[j]
        if raw is None:
            continue
        try:
            value = abs(float(raw))
        except (TypeError, ValueError):
            continue
        if np.isfinite(value):
            pairs.append((str(name), value))
    pairs.sort(key=lambda item: item[1], reverse=True)
    return [
        {"name": name, "value": value} for name, value in pairs[: max(1, int(limit))]
    ]


def factor_series_payload(
    frame: Any, *, correlations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """单因子面板（已按 factor 过滤）→ ``{series, scalars, notes}``。

    ``correlations`` 由评分卡从 ``factor_report.json`` 的相关矩阵取好传入（本函数
    不读盘）；缺该参数时如实记 note，不留空白序列。
    """
    notes: dict[str, Any] = {}
    dates = [_date_iso(v) for v in frame["date"].tolist()] if "date" in frame else []
    ic = frame["ic"].to_numpy(dtype=float) if "ic" in frame else np.array([])

    daily_ic = [
        {"date": day, "value": float(val)}
        for day, val in zip(dates, ic, strict=False)
        if np.isfinite(val)
    ]
    if len(daily_ic) > MAX_SERIES_POINTS:
        dropped = len(daily_ic) - MAX_SERIES_POINTS
        daily_ic = daily_ic[-MAX_SERIES_POINTS:]
        notes["daily_ic"] = (
            f"序列共 {len(dates)} 点，只保留最近 {MAX_SERIES_POINTS} 点（截去最早 {dropped} 点）"
        )

    decile_mean = [
        {"bucket": idx, "value": val}
        for idx, col in enumerate(DECILES, start=1)
        if (val := _mean(frame, col)) is not None
    ]
    if not decile_mean:
        notes["decile_mean"] = "面板无 q1..q10 列（十分位组合收益未算）"

    ic_decay = [
        {"horizon": h, "value": val}
        for h in HORIZONS
        if (val := _mean(frame, f"ic_{h}")) is not None
    ]
    if not ic_decay:
        notes["ic_decay"] = "面板无 ic_1/2/5/10/20 列（多周期 IC 未算）"

    segment_ic = _by_year(dates, ic)
    if not segment_ic:
        notes["segment_ic"] = "分年 IC 缺省（有效日不足或日期列缺失）"

    corr = list(correlations or [])
    if not corr:
        notes["correlation"] = (
            "相关性缺该因子（factor_report.json 的 correlation.matrix 里没有它，"
            "或矩阵未生成）"
        )

    return {
        "series": {
            "daily_ic": daily_ic,
            "decile_mean": decile_mean,
            "ic_decay": ic_decay,
            "segment_ic": segment_ic,
            "correlation": corr,
        },
        "scalars": {
            "ic_mean": _mean(frame, "ic"),
            "ic_ir": _ic_ir(ic),
            "turnover_mean": _mean(frame, "turnover"),
            "coverage_mean": _mean(frame, "coverage"),
            "n_days": int(np.isfinite(ic).sum()) if ic.size else 0,
        },
        "notes": notes,
    }


def _ic_ir(ic: Any) -> float | None:
    values = np.asarray(ic, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return None
    std = float(values.std(ddof=1))
    return None if std == 0 else float(values.mean() / std)


def _by_year(dates: list[str], ic: Any) -> list[dict[str, Any]]:
    """分年 IC：按日期前四位聚合（段短于 1 年也照报，n_days 一起带出）。"""
    buckets: dict[str, list[float]] = {}
    for day, val in zip(dates, np.asarray(ic, dtype=float), strict=False):
        if not np.isfinite(val) or len(day) < 4:
            continue
        buckets.setdefault(day[:4], []).append(float(val))
    return [
        {
            "label": year,
            "value": float(np.mean(vals)),
            "n_days": len(vals),
            "is_min_segment": False,
        }
        for year, vals in sorted(buckets.items())
    ]
