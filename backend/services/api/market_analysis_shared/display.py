"""展示口径工具 — 金额/市值/涨幅的跨市场一致的格式化与阈值。

港股金额（港元）与 A 股（人民币）量级不同，但「亿」的展示习惯一致。
单位切换规则沿用平台既有口径：>=1e11 按 亿 / >=1e7 按 万 / 否则原值。
"""

from __future__ import annotations

from typing import Union

_NUM = int | float


def fmt_yi(value: float, unit: str = "亿") -> float:
    """通缩金额到「亿」单位（保留两位）。"""
    if value is None or value != value:  # None / NaN
        return 0.0
    return round(float(value) / 1e8, 2)


def fmt_wan(value: float) -> float:
    """通缩金额到「万」单位。"""
    if value is None or value != value:
        return 0.0
    return round(float(value) / 1e4, 2)


def amount_in_display(value: float) -> float:
    """按量级选择 亿/万 口径（对齐 A 股 quantdb_feed 的展示逻辑）。"""
    if value is None or value != value:
        return 0.0
    if abs(value) >= 1e11:
        return round(value / 1e8, 1)
    if abs(value) >= 1e7:
        return round(value / 1e4, 1)
    return round(value, 1)


def pct(value: float | None, ndigits: int = 2) -> float:
    """百分比四舍五入（NaN/None -> 0.0）。"""
    if value is None or value != value:
        return 0.0
    return round(float(value), ndigits)


def safe_float(value, default: float = 0.0) -> float:
    """任意值安全转 float；None/NaN/空 -> default。"""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if f != f else f  # NaN
