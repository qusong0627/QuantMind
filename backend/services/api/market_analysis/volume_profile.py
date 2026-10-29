"""筹码结构 / 成交价格分布（Volume Profile）与主力、散户成本。

用途：给定指数或个股，回答四个问题：
1. 主力/散户的成交加权平均成本在什么价格；
2. 成交量最大的价格带（POC / 70% 价值区）在哪；
3. 14% 低分位成交价（潜在低吸位）与 −14% 回撤位；
4. 日内成交最集中的时段（仅当本地分钟数据覆盖最近交易日；否则如实不可用）。

口径边界：
- 个股主力=超大单+大单、散户=中单+小单；成本=按当日净流入为正的金额加权日均价。
- 指数没有逐笔席位，主力/散户成本用「全市场」同口径资金流加权指数日典型价，标注 scope=全市场。
- 14% 是**成交额从最低价向上的 14% 累计分位价**，不是跌幅；另给 recent_high×0.86 的 −14% 回撤位。
- 日内时段依赖本地 min5 数据；数据陈旧时明确不可用，不用旧数据冒充当前。
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.shared.quantdb_flow_units import normalize_l2_flow_money_to_yuan
from backend.shared.stock_utils import StockCodeUtil

from . import quantdb_feed

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()
_TTL = 1800.0
_BINS = 24
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000852.SH": "中证1000",
    "ALL.A": "A股指数（全A等权）",
}


def _data_dir() -> Path:
    return Path(os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb"))


def _norm_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if s in INDEX_NAMES:
        return s
    if s.startswith(("SH", "SZ", "BJ")) and "." not in s:
        return f"{s[2:]}.{s[:2]}"
    return s


def _index_prices(symbol: str, days: int, date: str | None = None) -> pd.DataFrame:
    """指数日线：官方 OHLC/成交额；全A用 by_universe 等权合成列。"""
    root = _data_dir()
    if symbol == "ALL.A":
        p = root / "5_technical_derived/market_breadth/by_universe.parquet"
        if not p.is_file():
            return pd.DataFrame()
        df = pd.read_parquet(p)
        df = df[df["universe"] == "all"].copy()
        df["date"] = pd.to_datetime(df["date"].astype(str))
        df = df.set_index("date").sort_index()
        out = pd.DataFrame(
            {
                "open": df["ew_open"],
                "high": df["ew_high"],
                "low": df["ew_low"],
                "close": df["ew_close"],
                # ew_amount 是成分均值；成交额权重必须用全宇宙总成交额
                "amount": df["amt_total"],
                "volume": np.nan,
            }
        )
        if date:
            out = out.loc[:pd.Timestamp(date)]
        return out.tail(days)
    p = root / "5_technical_derived/market_breadth/index_ohlc.parquet"
    if not p.is_file():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    df = df[df["symbol"] == symbol].copy()
    df["date"] = pd.to_datetime(df["date"].astype(str))
    df = df.set_index("date").sort_index()
    out = df[["open", "high", "low", "close", "amount"]].assign(volume=np.nan)
    if date:
        out = out.loc[:pd.Timestamp(date)]
    return out.tail(days)


def _stock_prices(symbol: str, days: int, date: str | None = None) -> pd.DataFrame:
    today, _ = quantdb_feed._market_pct_snapshot()
    if not today:
        return pd.DataFrame()
    anchor = str(date).replace("-", "") if date else str(today).zfill(8)
    trade_days = quantdb_feed._trading_days(anchor, days)
    if not trade_days:
        return pd.DataFrame()
    df = quantdb_feed._read_partitioned(
        "1_kline_data/daily_unadjusted",
        trade_days,
        "symbol, high, low, close, volume, amount",
    )
    if df.empty:
        return pd.DataFrame()
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df = df[df["symbol"] == symbol].copy()
    df["dt"] = pd.to_datetime(df["dt"].astype(str))
    df = df.set_index("dt").sort_index()
    if date:
        df = df.loc[:pd.Timestamp(date)]
    return df[["high", "low", "close", "volume", "amount"]].tail(days)


def _market_flows(days: int) -> pd.DataFrame:
    """全市场主力/散户净额（亿元），指数成本用。"""
    try:
        from backend.scripts.market_analysis.main_force import load_main_force
        from backend.scripts.market_analysis.retail_force import load_retail_force

        mf = load_main_force(days=max(days * 3, 400))
        rf = load_retail_force(days=max(days * 3, 400))
        out = pd.DataFrame(index=mf.index)
        out["main_yi"] = mf["main_net_yi"]
        out["retail_yi"] = rf["retail_net_yi"].reindex(out.index)
        return out
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _stock_flows(symbol: str, days: int, date: str | None = None) -> pd.DataFrame:
    today, _ = quantdb_feed._market_pct_snapshot()
    if not today:
        return pd.DataFrame()
    anchor = str(date).replace("-", "") if date else str(today).zfill(8)
    flow_days = quantdb_feed._l2_days(anchor, days)
    if not flow_days:
        return pd.DataFrame()
    df = quantdb_feed._read_partitioned(
        "6_ml_datasets/l2_factors",
        flow_days,
        "symbol, flow_super_net, flow_large_net, flow_medium_net, flow_small_net, amount, flow_net_amount, flow_net_ratio",
    )
    if df.empty:
        return pd.DataFrame()
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df = df[df["symbol"] == symbol].copy()
    if df.empty:
        return pd.DataFrame()
    df = normalize_l2_flow_money_to_yuan(df)
    df["dt"] = pd.to_datetime(df["dt"].astype(str))
    df = df.set_index("dt").sort_index()
    df["main_yi"] = (df["flow_super_net"] + df["flow_large_net"]) / 1e8
    df["retail_yi"] = (df["flow_medium_net"] + df["flow_small_net"]) / 1e8
    return df[["main_yi", "retail_yi"]]


def _weighted_cost(prices: pd.Series, weights: pd.Series) -> float | None:
    w = weights.reindex(prices.index).fillna(0.0).clip(lower=0.0)
    total = float(w.sum())
    if total <= 0:
        return None
    return float((prices * w).sum() / total)


def _profile(
    frame: pd.DataFrame, bins: int = _BINS
) -> tuple[list[dict], float | None, float | None, float | None, float | None, list[dict]]:
    """按成交额把每日 [low, high] 均匀摊到价格 bins，返回 profile/POC/VAH/VAL/p14。"""
    if frame.empty:
        return [], None, None, None, None, []
    lo = float(frame["low"].min())
    hi = float(frame["high"].max())
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [], None, None, None, None, []
    edges = np.linspace(lo, hi, bins + 1)
    weights = np.zeros(bins, dtype=float)
    for row in frame.itertuples(index=False):
        d_lo = max(float(row.low), lo)
        d_hi = min(float(row.high), hi)
        amt = float(row.amount or 0.0)
        if not math.isfinite(amt) or amt <= 0:
            continue
        if d_hi <= d_lo:
            idx = min(max(int((float(row.close) - lo) / (hi - lo) * bins), 0), bins - 1)
            weights[idx] += amt
            continue
        for i in range(bins):
            overlap = min(d_hi, edges[i + 1]) - max(d_lo, edges[i])
            if overlap > 0:
                weights[i] += amt * overlap / (d_hi - d_lo)
    total = float(weights.sum())
    if total <= 0:
        return [], None, None, None, None, []
    rows = [
        {
            "low": round(float(edges[i]), 3),
            "high": round(float(edges[i + 1]), 3),
            "mid": round(float((edges[i] + edges[i + 1]) / 2), 3),
            "amount_yi": round(float(weights[i]) / 1e8, 3),
            "share": round(float(weights[i] / total), 4),
        }
        for i in range(bins)
    ]
    poc_i = int(np.argmax(weights))
    # 70% value area：从 POC 向两侧扩，每次纳入金额更大的一侧
    included = {poc_i}
    cum = weights[poc_i]
    left, right = poc_i - 1, poc_i + 1
    while cum < 0.7 * total and (left >= 0 or right < bins):
        lw = weights[left] if left >= 0 else -1
        rw = weights[right] if right < bins else -1
        if rw >= lw:
            included.add(right)
            cum += rw
            right += 1
        else:
            included.add(left)
            cum += lw
            left -= 1
    val = float(edges[min(included)])
    vah = float(edges[max(included) + 1])
    # 14% 累计成交额分位价（从最低价往上）
    target = 0.14 * total
    acc = 0.0
    p14 = None
    for i, w in enumerate(weights):
        if acc + w >= target:
            frac = (target - acc) / w if w > 0 else 0.0
            p14 = float(edges[i] + frac * (edges[i + 1] - edges[i]))
            break
        acc += w
    nodes = sorted(
        [
            {"price": rows[i]["mid"], "share": rows[i]["share"], "amount_yi": rows[i]["amount_yi"]}
            for i in range(bins)
        ],
        key=lambda x: -x["share"],
    )[:3]
    return rows, float(edges[poc_i + 1] - (edges[poc_i + 1] - edges[poc_i]) / 2), vah, val, p14, nodes


def _time_windows(symbol: str, last_date: pd.Timestamp) -> dict[str, Any]:
    p = _data_dir() / "1_kline_data/min5_kline" / f"{symbol}.parquet"
    if not p.is_file():
        return {"available": False, "note": "本地无该标的分钟数据；交易时段分析不可用"}
    try:
        df = pd.read_parquet(p)
        df["time"] = pd.to_datetime(df["time"])
    except Exception:  # noqa: BLE001
        return {"available": False, "note": "分钟数据读取失败；交易时段分析不可用"}
    if df.empty or "amount" not in df:
        return {"available": False, "note": "分钟数据缺少成交额；交易时段分析不可用"}
    max_dt = df["time"].max()
    if last_date and max_dt < last_date - pd.Timedelta(days=7):
        return {
            "available": False,
            "note": f"本地分钟数据截至 {max_dt:%Y-%m-%d}，落后于日线 {last_date:%Y-%m-%d}，不用旧数据冒充当前交易时段",
        }
    df["date"] = df["time"].dt.date
    dates = sorted(df["date"].unique())[-5:]
    df = df[df["date"].isin(dates)].copy()
    df["clock"] = df["time"].dt.strftime("%H:%M")
    # 30 分钟桶
    def bucket(t: str) -> str:
        h, m = int(t[:2]), int(t[3:])
        start = 30 if m >= 30 else 0
        end = start + 30
        return f"{h:02d}:{start:02d}-{h:02d}:{end:02d}" if end < 60 else f"{h:02d}:{start:02d}-{(h+1):02d}:00"
    df["bucket"] = df["clock"].map(bucket)
    agg = df.groupby("bucket")["amount"].sum().sort_values(ascending=False)
    total = float(agg.sum()) or 1.0
    top = [
        {"bucket": k, "share": round(float(v / total), 4), "amount_yi": round(float(v) / 1e8, 2)}
        for k, v in agg.head(3).items()
    ]
    return {"available": True, "note": f"基于最近 {len(dates)} 个本地交易日分钟数据", "top": top}


def _cached(key: str, loader):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    value = loader()
    with _LOCK:
        _CACHE[key] = (now, value)
    return value


def get_volume_profile(
    symbol: str, window: int = 60, date: str | None = None
) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    window = max(20, min(250, int(window or 60)))
    key = f"{sym}:{window}:{date or 'latest'}"

    def _load() -> dict[str, Any]:
        is_index = sym in INDEX_NAMES
        if is_index:
            prices = _index_prices(sym, 1200, date)
            flows = _market_flows(max(window * 3, 120))
            cost_scope = "全市场主力/散户资金加权（指数无逐笔席位）"
        else:
            prices = _stock_prices(sym, max(window * 3, 120), date)
            flows = _stock_flows(sym, max(window * 3, 120), date)
            cost_scope = "个股主力/散户资金加权"
        if prices.empty:
            return {"available": False, "reason": "no_price", "symbol": sym}
        prices = prices.dropna(subset=["high", "low", "close"]).copy()
        if date and not prices.empty:
            prices = prices.loc[:pd.Timestamp(date)]
            flows = flows.loc[:pd.Timestamp(date)] if not flows.empty else flows
        prices = prices.tail(window)
        if flows is not None and not flows.empty:
            flows = flows.reindex(prices.index)
        if len(prices) < 10:
            return {"available": False, "reason": "too_few_days", "symbol": sym}
        if is_index:
            typical = (prices["high"] + prices["low"] + prices["close"]) / 3.0
            prices["vwap"] = typical
        else:
            vol = prices["volume"].replace(0, np.nan)
            prices["vwap"] = (prices["amount"].astype(float) * 1e4) / vol
            prices["vwap"] = prices["vwap"].where(prices["vwap"].notna(), prices["close"])
        last = float(prices["close"].iloc[-1])
        high = float(prices["high"].max())
        low = float(prices["low"].min())
        main_cost = None
        retail_cost = None
        if not flows.empty:
            f = flows.reindex(prices.index)
            main_cost = _weighted_cost(prices["vwap"], f.get("main_yi", pd.Series(dtype=float)))
            retail_cost = _weighted_cost(prices["vwap"], f.get("retail_yi", pd.Series(dtype=float)))
        profile, poc, vah, val, p14, nodes = _profile(prices)
        last_date = prices.index[-1]
        return {
            "available": True,
            "symbol": sym,
            "name": INDEX_NAMES.get(sym) or sym,
            "trade_date": last_date.strftime("%Y-%m-%d"),
            "window": window,
            "scope": "index" if is_index else "stock",
            "cost_scope": cost_scope,
            "last": round(last, 3),
            "high": round(high, 3),
            "low": round(low, 3),
            "vwap": round(
                float(
                    (prices["vwap"] * prices["amount"]).sum()
                    / max(float(prices["amount"].sum()), 1e-9)
                ),
                3,
            ),
            "main_cost": None if main_cost is None else round(main_cost, 3),
            "retail_cost": None if retail_cost is None else round(retail_cost, 3),
            "main_premium_pct": None if main_cost is None else round((last / main_cost - 1) * 100, 2),
            "retail_premium_pct": None if retail_cost is None else round((last / retail_cost - 1) * 100, 2),
            "poc": None if poc is None else round(poc, 3),
            "vah": None if vah is None else round(vah, 3),
            "val": None if val is None else round(val, 3),
            "p14_price": None if p14 is None else round(p14, 3),
            "p14_pct_from_last": None if p14 is None else round((p14 / last - 1) * 100, 2),
            "drawdown14": round(high * 0.86, 3),
            "drawdown_from_high_pct": round((last / high - 1) * 100, 2),
            "high_nodes": nodes,
            "profile": profile,
            "time_windows": _time_windows(sym, last_date),
            "method": "成交额在每日高低价区间均匀摊到 24 个价格箱；POC=最大成交额箱；价值区=围绕 POC 累计 70%；14%=从最低价向上累计成交额 14% 的分位价。",
        }

    return _cached(key, _load)
