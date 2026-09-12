"""美股今日热门 —— 成交额榜 / 量比榜 / 涨跌幅榜 / 放量异动 / 涨跌分布。

**这是「看哪儿热」的核心面板。** 美股量化判断热门与否的标准口径：

- **成交额（Dollar Volume）** = close × volume，单位美元。相比成交量，它才是
  跨标的可比的「关注度」指标 —— 一只 20 美元的股票成交 1 亿股与一只
  800 美元的股票成交 100 万股，成交量差 100 倍但成交额可能相当。
- **量比（RVOL）** = 当日成交量 / **前** 20 个交易日平均成交量。基准不含当日，
  否则巨量当日会抬高分母把自己稀释掉。这是「异动」的第一判据。
- **距 52 周高点**：区分「新高附近的放量（强势突破）」与「下跌中的放量（恐慌出货）」。

量比基准不足 20 个交易日的标的（新股/次新股）rvol 置空，不参与量比榜 ——
短窗口基准会让它们的量比严重失真。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import fmt_yi, safe_float
from backend.services.api.market_analysis_shared.market_days import to_iso
from backend.services.api.market_analysis_us.feed.base import (
    _avail,
    _hot_snapshot,
    _sector_cn,
    _sector_map,
)
from backend.services.api.market_analysis_us.feed.breadth import _proximity_map

_HOT_TTL = 300.0

# 量比榜/异动榜的最低门槛：低于此值不算「放量」
MIN_RVOL = 1.5

_HOT_KINDS = ("amount", "rvol", "gainers", "losers")


def _hot_rows(limit: int, sort_col: str, ascending: bool = False,
              min_rvol: float | None = None) -> dict[str, Any]:
    """热门榜公共实现：截面对齐量比基准后按指定列排序。"""

    def _load() -> dict[str, Any]:
        empty = {"trade_date": "", "kind": sort_col, "items": []}
        if not _avail():
            return empty
        latest, df = _hot_snapshot()
        if not latest or df.empty:
            return empty
        if min_rvol is not None:
            df = df[df["rvol"].notna() & (df["rvol"] >= min_rvol)]
        df = df[df[sort_col].notna()]
        if df.empty:
            return empty
        df = df.sort_values(sort_col, ascending=ascending).head(limit)

        smap = _sector_map().set_index("symbol")["sector"].to_dict()
        prox = _proximity_map()
        # 名称映射走 base._names，但这里只需中文名，避免再读一次主表
        from backend.services.api.market_analysis_us.feed.base import _names

        names = _names(df["symbol"].tolist())

        items = []
        for _, r in df.iterrows():
            sym = r["symbol"]
            items.append(
                {
                    "symbol": sym,
                    "name": names.get(sym, sym),
                    "sector": _sector_cn(smap.get(sym)),
                    "close": round(safe_float(r["close"]), 2),
                    "pct_change": round(safe_float(r["pct_change"]), 2),
                    "amount_yi": fmt_yi(safe_float(r["amount"])),
                    "rvol": (
                        round(safe_float(r["rvol"]), 2)
                        if pd.notna(r.get("rvol"))
                        else None
                    ),
                    "drawdown_pct": prox.get(sym),
                }
            )
        return {"trade_date": to_iso(latest), "kind": sort_col, "items": items}

    return cached(
        f"us_hot:{sort_col}:{limit}:{ascending}:{min_rvol}", _load, ttl=_HOT_TTL
    )


def get_hot_stocks(kind: str = "amount", limit: int = 20) -> dict[str, Any]:
    """今日热门榜。

    kind:
    - `amount`  成交额榜（关注度，默认）
    - `rvol`    量比榜（相对自身历史的放量，门槛 MIN_RVOL）
    - `gainers` 涨幅榜
    - `losers`  跌幅榜
    """
    if kind not in _HOT_KINDS:
        kind = "amount"
    limit = max(5, min(int(limit), 50))
    if kind == "amount":
        return _hot_rows(limit, "amount")
    if kind == "rvol":
        return _hot_rows(limit, "rvol", min_rvol=MIN_RVOL)
    if kind == "gainers":
        return _hot_rows(limit, "pct_change", ascending=False)
    return _hot_rows(limit, "pct_change", ascending=True)


def get_unusual_volume(limit: int = 20, min_rvol: float = 2.0) -> dict[str, Any]:
    """放量异动榜：量比 ≥ min_rvol 的标的，按量比降序。"""
    limit = max(5, min(int(limit), 50))
    min_rvol = max(1.0, min(float(min_rvol), 20.0))
    res = _hot_rows(limit, "rvol", min_rvol=min_rvol)
    res["min_rvol"] = min_rvol
    return res


# 涨跌幅分布分桶（美股无涨跌停，按绝对幅度分档，±5% 以上视为异动）
_DIST_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (float("-inf"), -5.0, "≤-5%"),
    (-5.0, -3.0, "-5~-3%"),
    (-3.0, -2.0, "-3~-2%"),
    (-2.0, -1.0, "-2~-1%"),
    (-1.0, 0.0, "-1~0%"),
    (0.0, 1.0, "0~1%"),
    (1.0, 2.0, "1~2%"),
    (2.0, 3.0, "2~3%"),
    (3.0, 5.0, "3~5%"),
    (5.0, float("inf"), "≥5%"),
)


def get_market_distribution() -> dict[str, Any]:
    """全市场涨跌幅分布直方图 + 分位数。

    分布形状比单一均值更能说明市场状态：双峰（大涨大跌都多）= 分化行情，
    集中在 0 附近 = 窄幅震荡。
    """

    def _load() -> dict[str, Any]:
        empty = {"trade_date": "", "buckets": [], "quantiles": {}}
        if not _avail():
            return empty
        latest, df = _hot_snapshot()
        if not latest or df.empty:
            return empty
        pct = df["pct_change"].dropna()
        if pct.empty:
            return empty
        buckets = []
        for lo, hi, label in _DIST_BUCKETS:
            n = int(((pct > lo) & (pct <= hi)).sum())
            buckets.append({"label": label, "count": n})
        return {
            "trade_date": to_iso(latest),
            "total": int(len(pct)),
            "buckets": buckets,
            "quantiles": {
                "p10": round(float(pct.quantile(0.10)), 2),
                "p25": round(float(pct.quantile(0.25)), 2),
                "median": round(float(pct.median()), 2),
                "p75": round(float(pct.quantile(0.75)), 2),
                "p90": round(float(pct.quantile(0.90)), 2),
            },
        }

    return cached("us_market_distribution", _load, ttl=_HOT_TTL)


def get_market_stats() -> dict[str, Any]:
    """市场活力快照：成交额、放量标的占比、涨跌幅极值、量比中位数。

    给「大盘脉搏」顶部做一行紧凑的活力指标条，回答「今天市场活不活跃」。
    """

    def _load() -> dict[str, Any]:
        empty = {
            "trade_date": "",
            "total_amount_yi": 0.0,
            "rvol_median": None,
            "active_ratio": 0.0,
            "up_5pct": 0,
            "down_5pct": 0,
            "high_rvol_count": 0,
        }
        if not _avail():
            return empty
        latest, df = _hot_snapshot()
        if not latest or df.empty:
            return empty
        rvol = df["rvol"].dropna()
        pct = df["pct_change"].dropna()
        total = max(len(df), 1)
        return {
            "trade_date": to_iso(latest),
            "total_amount_yi": fmt_yi(safe_float(df["amount"].fillna(0).sum())),
            "rvol_median": round(float(rvol.median()), 2) if len(rvol) else None,
            # 放量标的占比：量比 ≥1.5 的家数 / 全池（衡量市场参与度）
            "active_ratio": round(float((rvol >= MIN_RVOL).sum() / total * 100), 1),
            "high_rvol_count": int((rvol >= 2.0).sum()),
            "up_5pct": int((pct >= 5).sum()),
            "down_5pct": int((pct <= -5).sum()),
        }

    return cached("us_market_stats", _load, ttl=_HOT_TTL)
