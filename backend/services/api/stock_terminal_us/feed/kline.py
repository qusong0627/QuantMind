"""美股个股终端 —— 日线 K 线与拆股事件。

库内就是未复权原始价（yfinance `auto_adjust=False`），终端**只提供一种口径**
（`adjust="none"`）+ 拆股标记，不自算前/后复权（与市场分析模块既定立场一致）。

拆股对未复权价是硬跳变：AAPL 2020-08-31 收 499.23 → 129.04（4:1）。
`amount` 是**美元原始成交额**（≈ close×volume），不是 A 股的「股/万元」。

`splits` 返回该标的**全部历史拆股事件**（升序，前端画标记时按当前窗口自行过滤）——
只在窗口内有拆股时才提示，会让长窗口（如 5 年）漏掉中间的拆股标记。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.market_days import list_partition_dates
from backend.services.api.market_analysis_shared.display import safe_float
from backend.services.api.stock_terminal_us.feed.base import (
    ADJUST,
    DATA_DIR,
    KLINE_REL,
    MAX_RANGE_DAYS,
    SPLITS_DIR,
    TERMINAL_NOTES,
    _read_symbol_bars,
    _symbol_exists,
    _symbol_table,
    _trading_days,
    _name_of,
    normalize_symbol,
    to_iso,
)

_DEFAULT_DAYS = 500


def _window(
    start_ymd: str | None, end_ymd: str | None, days: int
) -> tuple[list[str], bool]:
    """K 线窗口的交易日分区（降序，[0]=最新）与是否被硬上限截断。

    - 不给 start/end：最近 `days` 个交易日
    - 给了 start/end：该闭区间内的全部分区，忽略 days，但受 MAX_RANGE_DAYS 约束
      （显式分区列表是线性成本，超长窗口保留最近一段并置 truncated=True）
    """
    if start_ymd or end_ymd:
        all_days = list_partition_dates(KLINE_REL, DATA_DIR)
        sel = [
            d
            for d in all_days
            if (not start_ymd or d >= start_ymd) and (not end_ymd or d <= end_ymd)
        ]
        truncated = len(sel) > MAX_RANGE_DAYS
        return sel[-MAX_RANGE_DAYS:][::-1], truncated
    return _trading_days(None, days), False


def _splits(symbol: str) -> list[dict[str, Any]]:
    """该标的全部拆股事件（升序，date/ratio）。"""
    df = _symbol_table(SPLITS_DIR, symbol)
    if df.empty or "trade_date" not in df.columns:
        return []
    df = df.copy()
    df["_d"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["_r"] = pd.to_numeric(df.get("split_ratio"), errors="coerce")
    df = df[df["_d"].notna() & df["_r"].notna()].sort_values("_d")
    return [
        {"date": r["_d"].strftime("%Y-%m-%d"), "ratio": round(safe_float(r["_r"]), 4)}
        for _, r in df.iterrows()
    ]


def _rows_to_items(df: pd.DataFrame) -> list[dict[str, Any]]:
    """日线 DataFrame -> JSON 安全 items（NaN 一律出口归一，防前端白屏）。"""
    items: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        items.append(
            {
                "date": to_iso(str(r["dt"])),
                "open": round(safe_float(r.get("open")), 4),
                "high": round(safe_float(r.get("high")), 4),
                "low": round(safe_float(r.get("low")), 4),
                "close": round(safe_float(r.get("close")), 4),
                "volume": int(safe_float(r.get("volume"))),
                "amount": round(safe_float(r.get("amount")), 2),
            }
        )
    return items


def get_kline(
    symbol: str,
    days: int = _DEFAULT_DAYS,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any] | None:
    """单标的日线（未复权）+ 拆股事件；标的完全不存在时返回 None（路由转 404）。

    start/end 为 YYYY-MM-DD（或 YYYYMMDD），闭区间。
    结果按 (symbol, days, start, end) 做 5 分钟 TTL 缓存：切换标的/重渲染会反复
    打同一个窗口，而 500 日窗口的显式分区读是 0.2-0.5s 量级。
    """
    sym = normalize_symbol(symbol)
    if not sym or not _symbol_exists(sym):
        return None
    return cached(
        f"us_term:kline:{sym}:{days}:{start}:{end}",
        lambda: _build_kline(sym, days, start, end),
        ttl=300.0,
    )


def _build_kline(
    sym: str, days: int, start: str | None, end: str | None
) -> dict[str, Any]:
    start_ymd = str(start).replace("-", "").strip() if start else None
    end_ymd = str(end).replace("-", "").strip() if end else None
    days = max(30, min(int(days), 2000))
    ymd_days, truncated = _window(start_ymd, end_ymd, days)

    df = _read_symbol_bars(sym, ymd_days)
    items = _rows_to_items(df)

    lo = items[0]["date"] if items else None
    hi = items[-1]["date"] if items else None
    return {
        "symbol": sym,
        "name": _name_of(sym),
        "adjust": ADJUST,
        "count": len(items),
        "start_date": lo,
        "end_date": hi,
        "truncated": truncated,
        "items": items,
        # 全部历史拆股（前端按窗口过滤后画竖线），不是窗口内事件
        "splits": _splits(sym),
        "notes": TERMINAL_NOTES,
    }
