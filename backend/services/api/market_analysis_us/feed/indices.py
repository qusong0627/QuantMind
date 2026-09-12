"""美股核心指数脉搏 —— SPX / NDX / IXIC / DJI / SOX。

数据源 `1_kline_data/index_daily`（akshare 源），**只有 5 个指数、没有 VIX、
没有任何 ETF**。两处口径限制（页面必须如实反映，不要伪造）：
- `amount` 列恒为 0 → 不输出成交额，只输出 `volume`（`SOX.US` 的 volume 也是 0）
- 指数分区通常**滞后个股若干交易日**，故响应内单独带 `trade_date`，
  前端按面板标注日期，不与个股面板混用
"""

from __future__ import annotations

from typing import Any

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import pct, safe_float
from backend.services.api.market_analysis_shared.market_days import to_iso
from backend.services.api.market_analysis_us.feed.base import (
    INDEX_OVERVIEW,
    INDEX_REL,
    _avail,
    _index_trading_days,
    _latest_index_date,
    _read_partitioned,
)

# 指数分区滞后个股，取最近 40 个**指数分区**以覆盖一个月走势
_TREND_DAYS = 30
_SPARK_DAYS = 5


def get_indices_overview() -> list[dict[str, Any]]:
    """核心指数快照：价格 / 涨跌 / 5 日迷你走势 / 成交量。

    `amount` 恒为 0，故不含成交额字段；`turnover_yi` 恒为 None（前端隐藏）。
    """

    def _load() -> list[dict[str, Any]]:
        if not _avail():
            return []
        latest = _latest_index_date()
        if not latest:
            return []
        # 指数分区自己的交易日序列（可能与个股不同）
        days = _index_trading_days(latest, _TREND_DAYS)
        if not days:
            return []
        # amount 列在本数据源恒为 0，不读（省 I/O，也避免误把它当成交额）
        df = _read_partitioned(INDEX_REL, days, columns="symbol, dt, close, volume")
        if df.empty:
            return []
        df["dt"] = df["dt"].astype(str)
        # 量比基准：同指数前 20 个交易日的均量（不含当日）
        base = _index_volume_baseline()
        result: list[dict[str, Any]] = []
        for item in INDEX_OVERVIEW:
            sub = df[df["symbol"] == item["symbol"]].sort_values("dt")
            if sub.empty:
                continue
            closes = [safe_float(c) for c in sub["close"].tolist()]
            last = closes[-1]
            prev = closes[-2] if len(closes) > 1 else last
            change = last - prev
            volume = safe_float(sub["volume"].iloc[-1])
            avg_vol = base.get(item["symbol"])
            rvol = (
                round(volume / avg_vol, 2)
                if avg_vol and avg_vol > 0 and volume > 0
                else None
            )
            result.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "price": round(last, 2),
                    "change": round(change, 2),
                    "pct_change": pct(change / prev * 100 if prev else 0.0),
                    # amount 恒 0 → 不输出成交额；volume 为股数（SOX 为 0）
                    "turnover_yi": None,
                    "volume": int(volume) if volume > 0 else None,
                    "rvol": rvol,
                    "trend": [round(c, 2) for c in closes[-_SPARK_DAYS:]],
                    "trade_date": to_iso(str(sub["dt"].iloc[-1])),
                }
            )
        return result

    return cached("us_indices_overview", _load)


def _index_volume_baseline(window: int = 20) -> dict[str, float]:
    """各指数前 window 个交易日平均成交量（量比分母，不含最新日）。"""
    latest = _latest_index_date()
    if not latest:
        return {}
    days = _index_trading_days(latest, window + 1)[1:]
    if not days:
        return {}
    df = _read_partitioned(INDEX_REL, days, columns="symbol, volume")
    if df.empty:
        return {}
    agg = df.groupby("symbol")["volume"].mean()
    return {str(k): float(v) for k, v in agg.items() if v and v > 0}


def get_index_spread() -> dict[str, Any]:
    """指数间强弱对照：纳指 vs 道指、费半 vs 标普的 20 日相对强弱。

    美股风格轮动的粗粒度度量（成长/科技 vs 价值/传统），
    因本地无风格指数 ETF（IWF/IWD 缺失），用现有 5 个指数近似。
    """
    window = 20

    def _load() -> dict[str, Any]:
        if not _avail():
            return {"trade_date": "", "pairs": []}
        latest = _latest_index_date()
        if not latest:
            return {"trade_date": "", "pairs": []}
        days = _index_trading_days(latest, window + 5)
        if len(days) < window:
            return {"trade_date": "", "pairs": []}
        df = _read_partitioned(INDEX_REL, days, columns="symbol, dt, close")
        if df.empty:
            return {"trade_date": "", "pairs": []}
        df["dt"] = df["dt"].astype(str)
        mat = df.pivot_table(index="dt", columns="symbol", values="close").sort_index()

        def _ret(sym: str) -> float | None:
            if sym not in mat.columns:
                return None
            s = mat[sym].dropna()
            if len(s) < 2 or safe_float(s.iloc[0]) <= 0:
                return None
            return (safe_float(s.iloc[-1]) / safe_float(s.iloc[0]) - 1) * 100

        name_of = {i["symbol"]: i["name"] for i in INDEX_OVERVIEW}
        pairs = [
            ("NDX.US", "DJI.US", "成长 vs 价值"),
            ("SOX.US", "SPX.US", "半导体 vs 大盘"),
            ("IXIC.US", "SPX.US", "科技 vs 大盘"),
        ]
        out = []
        for a, b, label in pairs:
            ra, rb = _ret(a), _ret(b)
            if ra is None or rb is None:
                continue
            out.append(
                {
                    "label": label,
                    "left_symbol": a,
                    "left_name": name_of.get(a, a),
                    "right_symbol": b,
                    "right_name": name_of.get(b, b),
                    "left_return": round(ra, 2),
                    "right_return": round(rb, 2),
                    "spread": round(ra - rb, 2),
                    "window": window,
                }
            )
        return {"trade_date": to_iso(latest), "pairs": out, "window": window}

    return cached("us_index_spread", _load)
