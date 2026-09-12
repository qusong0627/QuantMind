"""GICS 板块 —— 热力矩形图 / 多周期轮动 / 板块内宽度 / 板块估值。

行业口径：`2_base_sector/sector/{SYMBOL}.parquet` 的 `sector` 列（GICS 11 大类，
yahoo 源），细分行业在 `industry` 列。**security_master 里没有行业字段**。
516 只覆盖中有 29 只 sector 为空，统一归入「未分类」而不是丢弃。

聚合一律取**中位数**而非均值：日线是未复权原始价，拆股当日的个股会出现
巨大跳变，均值会被单只股票带偏（中位数对异常稳健）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import fmt_yi, safe_float
from backend.services.api.market_analysis_shared.market_days import to_iso
from backend.services.api.market_analysis_us.feed.base import (
    INDEX_REL,
    KLINE_REL,
    _avail,
    _dedupe_bars,
    _f10_snapshot,
    _index_trading_days,
    _latest_index_date,
    _market_pct_snapshot,
    _names,
    _read_partitioned,
    _sector_cn,
    _sector_map,
    _trading_days,
)

# 轮动周期（交易日）
_ROTATION_WINDOWS = (1, 5, 20, 60)
_SECTOR_TTL = 900.0


def _sector_returns(window: int = 60) -> pd.DataFrame:
    """各标的在 1/5/20/60 日窗口的收益率（中位数聚合用）。

    返回 DataFrame[symbol, ret_1d, ret_5d, ret_20d, ret_60d]。
    需读 window+5 个交易日以保证最长窗口有前收。
    """
    need = max(_ROTATION_WINDOWS) + 5
    need = max(need, window + 1)
    days = _trading_days(None, need)
    if not days:
        return pd.DataFrame()
    k = _read_partitioned(KLINE_REL, days, columns="symbol, dt, close")
    if k.empty:
        return pd.DataFrame()
    k = _dedupe_bars(k, ["symbol", "dt"])
    k["dt"] = k["dt"].astype(str)
    k = k[k["close"].fillna(0) > 0]
    mat = k.pivot_table(index="dt", columns="symbol", values="close").sort_index()
    if len(mat) < 2:
        return pd.DataFrame()

    out = pd.DataFrame(index=mat.columns)
    for w in _ROTATION_WINDOWS:
        if len(mat) <= w:
            out[f"ret_{w}d"] = None
            continue
        base = mat.iloc[-1 - w]
        valid = base.notna() & (base > 0)
        out[f"ret_{w}d"] = ((mat.iloc[-1] / base - 1) * 100).where(valid)
    out.index.name = "symbol"
    return out.reset_index()


def _sector_frame() -> pd.DataFrame:
    """标的 × 行业 × 多周期收益的合并表（板块各视图共用）。"""

    def _load() -> pd.DataFrame:
        rets = _sector_returns()
        if rets.empty:
            return pd.DataFrame()
        smap = _sector_map()
        if smap.empty:
            return pd.DataFrame()
        return rets.merge(smap, on="symbol", how="inner")

    return cached("us:sector_frame", _load, ttl=_SECTOR_TTL)


def get_sector_heatmap(limit: int = 40) -> list[dict[str, Any]]:
    """GICS 板块热力图：平均涨幅 / 成交额 / 领涨龙头。

    对齐热力图组件的数据形状（name/value/pct_change/leader/leader_pct），
    与 A 股、港股共用同一个 treemap 组件。
    """
    limit = max(5, min(int(limit), 80))

    def _load() -> list[dict[str, Any]]:
        if not _avail():
            return []
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return []
        smap = _sector_map()
        if smap.empty:
            return []
        df = snap.merge(smap, on="symbol", how="inner")
        name_map = _names(df["symbol"].tolist())

        items: list[dict[str, Any]] = []
        for sector, g in df.groupby("sector", sort=False):
            amount = float(g["amount"].fillna(0).sum())
            if amount <= 0:
                continue
            top = g.sort_values("pct_change", ascending=False).iloc[0]
            items.append(
                {
                    "name": _sector_cn(sector),
                    "sector": sector,
                    "value": fmt_yi(amount),
                    "pct_change": round(float(g["pct_change"].median()), 2),
                    "leader": name_map.get(top["symbol"], top["symbol"]),
                    "leader_symbol": top["symbol"],
                    "leader_pct": round(safe_float(top["pct_change"]), 2),
                    "stock_count": int(len(g)),
                    "advance_count": int((g["pct_change"] > 0).sum()),
                }
            )
        items.sort(key=lambda x: x["value"], reverse=True)
        return items[:limit]

    return cached(f"us_sector_heatmap:{limit}", _load)


def get_sector_rotation(limit: int = 24) -> dict[str, Any]:
    """GICS 板块多周期轮动：1/5/20/60 日收益 + 相对标普强弱 + 板块内宽度。

    相对强弱（RS）用板块 20 日收益减标普 20 日收益；标普 20 日收益走
    index_daily（**注意其分区可能滞后个股**，故 RS 单独标注指数日期）。
    """
    limit = max(5, min(int(limit), 50))

    def _load() -> dict[str, Any]:
        if not _avail():
            return {"trade_date": "", "index_date": "", "sectors": []}
        frame = _sector_frame()
        if frame.empty:
            return {"trade_date": "", "index_date": "", "sectors": []}
        latest = _trading_days(None, 1)
        benchmark = _benchmark_return(20)

        rows: list[dict[str, Any]] = []
        for sector, g in frame.groupby("sector", sort=False):
            rets = {}
            for w in _ROTATION_WINDOWS:
                col = f"ret_{w}d"
                vals = g[col].dropna() if col in g.columns else pd.Series(dtype=float)
                rets[col] = round(float(vals.median()), 2) if len(vals) else None
            r20 = rets.get("ret_20d")
            rows.append(
                {
                    "name": _sector_cn(sector),
                    "sector": sector,
                    "ret_1d": rets.get("ret_1d"),
                    "ret_5d": rets.get("ret_5d"),
                    "ret_20d": r20,
                    "ret_60d": rets.get("ret_60d"),
                    "rs_20d": (
                        round(r20 - benchmark, 2)
                        if r20 is not None and benchmark is not None
                        else None
                    ),
                    "breadth_20d": (
                        round(float((g["ret_20d"].dropna() > 0).mean() * 100), 1)
                        if g["ret_20d"].notna().any()
                        else None
                    ),
                    "stock_count": int(len(g)),
                }
            )
        rows.sort(key=lambda x: (x["ret_20d"] is None, -(x["ret_20d"] or 0)))
        return {
            "trade_date": to_iso(latest[0]) if latest else "",
            "index_date": to_iso(_latest_index_date() or "") if _latest_index_date() else "",
            "benchmark_return_20d": benchmark,
            "sectors": rows[:limit],
        }

    return cached(f"us_sector_rotation:{limit}", _load, ttl=_SECTOR_TTL)


def _benchmark_return(window: int) -> float | None:
    """标普 500 的 window 日收益（%），供相对强弱使用。"""
    latest = _latest_index_date()
    if not latest:
        return None
    days = _index_trading_days(latest, window + 2)
    if len(days) < window + 1:
        return None
    df = _read_partitioned(INDEX_REL, days, columns="symbol, dt, close")
    if df.empty:
        return None
    df["dt"] = df["dt"].astype(str)
    sub = df[df["symbol"] == "SPX.US"].sort_values("dt")
    if len(sub) < window + 1:
        return None
    closes = [safe_float(c) for c in sub["close"].tolist()]
    base = closes[-1 - window]
    if base <= 0:
        return None
    return round((closes[-1] / base - 1) * 100, 2)


def get_sector_valuation(limit: int = 24) -> list[dict[str, Any]]:
    """板块估值温度计：GICS 板块 × PE/PB/股息率中位数 + 市值合计。

    数据源 `f10` 快照（**不是 valuation 分区**，后者曾长期静默写空）。
    PE 中位数只取正值（亏损公司的负 PE 会污染中位数）。
    """
    limit = max(5, min(int(limit), 50))

    def _load() -> list[dict[str, Any]]:
        if not _avail():
            return []
        f10 = _f10_snapshot()
        smap = _sector_map()
        if f10.empty or smap.empty:
            return []
        df = f10.merge(smap, on="symbol", how="inner")
        df["pe_pos"] = df["pe_ratio"].where(df["pe_ratio"] > 0)
        df["pb_pos"] = df["pb_ratio"].where(df["pb_ratio"] > 0)

        rows: list[dict[str, Any]] = []
        for sector, g in df.groupby("sector", sort=False):
            rows.append(
                {
                    "name": _sector_cn(sector),
                    "sector": sector,
                    "pe_median": _median_or_none(g["pe_pos"]),
                    "pb_median": _median_or_none(g["pb_pos"]),
                    "dividend_yield_median": _median_or_none(g["dividend_yield"]),
                    "market_cap_yi": fmt_yi(
                        float(g["market_cap"].fillna(0).sum())
                    ),
                    "stock_count": int(len(g)),
                }
            )
        rows.sort(key=lambda x: (x["pe_median"] is None, x["pe_median"] or 0))
        return rows[:limit]

    return cached(f"us_sector_valuation:{limit}", _load, ttl=_SECTOR_TTL)


def _median_or_none(series: pd.Series) -> float | None:
    """中位数（无有效值返回 None，**不返回 0**：0 会被前端当成真实估值）。"""
    vals = series.dropna()
    if vals.empty:
        return None
    return round(float(vals.median()), 2)
