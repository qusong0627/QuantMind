"""Hive 分区交易日历 — 跨市场通用的「由 on-disk dt 分区推导交易日」工具。

市场数据目录统一按 dt=YYYYMMDD/data.parquet 组织，以实际存在的分区为准，
不依赖日历文件（避免日历与数据不同步导致日期错配）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from collections.abc import Sequence

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^dt=(\d{8})$")


def list_partition_dates(rel_path: str, data_dir: str | Path) -> list[str]:
    """返回 rel_path 目录下全部交易日分区（YYYYMMDD 升序）。

    分区目录命名为 dt=YYYYMMDD，Hive 风格；排序保证稳定时序。
    """
    full = Path(data_dir) / rel_path
    if not full.is_dir():
        return []
    dates = []
    for name in os.listdir(full):
        m = _DATE_RE.match(name)
        if m:
            dates.append(m.group(1))
    return sorted(dates)


def latest_partition_date(rel_path: str, data_dir: str | Path) -> str | None:
    """最新交易日分区（YYYYMMDD），无分区返回 None。"""
    dates = list_partition_dates(rel_path, data_dir)
    return dates[-1] if dates else None


def trading_days_until(
    rel_path: str, data_dir: str | Path, end: str | None, n: int
) -> list[str]:
    """截至 end（含，YYYYMMDD 或 None=最新）的最近 n 个交易日，降序、[0] 最新。"""
    dates = list_partition_dates(rel_path, data_dir)
    if end is not None:
        dates = [d for d in dates if d <= end.replace("-", "")]
    return dates[-n:][::-1]


def to_iso(ymd: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD。"""
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


def to_ymd(iso: str) -> str:
    """YYYY-MM-DD -> YYYYMMDD。"""
    return iso.replace("-", "")


def partition_dates_to_sql(ymd_dates: Sequence[str]) -> str:
    """YYYYMMDD/ISO 日期列表 -> SQL IN 子句插值（数字字面量，无注入面）。"""
    parts = [d.replace("-", "") for d in ymd_dates]
    return ",".join(parts)
