"""美股市场宽度 —— 温度计 / 均线站位 / 52 周新高新低 / A-D 线。

美股量化最看重的一组指标，港股与 A 股模块都没有对应面板：
- **% 站上 MA50 / MA200**：中期趋势参与度
- **52 周新高 / 新低家数**：趋势扩张 or 收缩
- **A-D 线（累计涨跌家数）**：宽度趋势的经典刻度

口径说明：日线为未复权原始价，均线与高低点均按原始价计算。跨越拆股日的个股
会产生跳变（拆股不在 daily_forward 里复权），因此本模块**只做家数统计与占比**，
不做个股级价格比较，聚合结果对个别异常不敏感。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import fmt_yi, safe_float
from backend.services.api.market_analysis_shared.market_days import to_iso
from backend.services.api.market_analysis_us.feed.base import (
    BIG_MOVE_THRESHOLD,
    KLINE_REL,
    WINDOW_52W,
    WINDOW_MA50,
    WINDOW_MA200,
    _avail,
    _dedupe_bars,
    _market_pct_snapshot,
    _names,
    _read_partitioned,
    _trading_days,
)

# 宽度矩阵的缓存 TTL 比默认 300s 长：底层是「近一年日线」的较重读取，
# 而数据每天只更新一次（美股收盘后落盘），5 分钟内重复读没有意义。
_BREADTH_TTL = 900.0


def _empty_breadth(trade_date: str = "") -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "total_stocks": 0,
        "advance_count": 0,
        "decline_count": 0,
        "flat_count": 0,
        "big_up_count": 0,
        "big_down_count": 0,
        "total_turnover_yi": 0.0,
        "profit_effect": 50.0,
        "sentiment_score": 50.0,
        "median_pct": 0.0,
        "big_move_threshold": BIG_MOVE_THRESHOLD,
    }


def get_market_breadth() -> dict[str, Any]:
    """市场温度计：涨跌家数 / 上涨占比 / 中位数涨幅 / ±5% 异动家数 / 成交额。

    美股无涨跌停（有 LULD 熔断），±5% 是「异动」统计口径，**不等于涨停**。
    """

    def _load() -> dict[str, Any]:
        if not _avail():
            return _empty_breadth()
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return _empty_breadth(to_iso(latest) if latest else "")
        pct_s = snap["pct_change"].fillna(0.0)
        adv = int((pct_s > 0).sum())
        dec = int((pct_s < 0).sum())
        flat = int((pct_s == 0).sum())
        total_stocks = adv + dec + flat
        profit = round(adv / total_stocks * 100, 1) if total_stocks else 50.0
        total_amount = float(snap["amount"].fillna(0).sum()) if "amount" in snap else 0.0
        return {
            "trade_date": to_iso(latest),
            "total_stocks": total_stocks,
            "advance_count": adv,
            "decline_count": dec,
            "flat_count": flat,
            "big_up_count": int((pct_s >= BIG_MOVE_THRESHOLD).sum()),
            "big_down_count": int((pct_s <= -BIG_MOVE_THRESHOLD).sum()),
            "total_turnover_yi": fmt_yi(total_amount),
            "profit_effect": profit,
            # 50 = 中性；上涨占比 100 => 100，0 => 0
            "sentiment_score": round(50 + (profit - 50) * 2.0, 1),
            # 截面中位数对拆股/异常值稳健，比均值更能代表「普通股票今天怎么样」
            "median_pct": round(float(pct_s.median()), 2),
            "big_move_threshold": BIG_MOVE_THRESHOLD,
        }

    return cached("us_market_breadth", _load)


def _breadth_matrix(n_days: int) -> tuple[list[str], pd.DataFrame]:
    """近 n 日宽度的价格矩阵。

    返回 (dates 升序, DataFrame[index=date, columns=symbol, values=close])。
    预热窗口取 MA200 与 52 周（250 日）的较大者，保证首个输出日的
    长窗口指标已就绪（否则 52 周新高新低会全为 0）。
    矩阵规模约 310 日 × 500 只 ≈ 15 万格，pandas 滚动窗口秒级完成。
    """
    need = n_days + max(WINDOW_MA200, WINDOW_52W)
    days = _trading_days(None, need)  # 降序
    if not days:
        return [], pd.DataFrame()
    k = _read_partitioned(KLINE_REL, days, columns="symbol, dt, close")
    if k.empty:
        return [], pd.DataFrame()
    k = _dedupe_bars(k, ["symbol", "dt"])
    k["dt"] = k["dt"].astype(str)
    k = k[k["close"].fillna(0) > 0]
    mat = k.pivot_table(index="dt", columns="symbol", values="close")
    mat = mat.sort_index()
    return list(mat.index), mat


def get_breadth_history(days: int = 60) -> dict[str, Any]:
    """宽度时间序列：A-D 线 / % 站上 MA50、MA200 / 新高新低家数。

    每个交易日的广度按「该日有行情的股票」为分母，避免停牌股拉低占比。
    """
    days = max(10, min(int(days), 250))

    def _load() -> dict[str, Any]:
        if not _avail():
            return {"points": [], "trade_date": "", "summary": {}}
        dates, mat = _breadth_matrix(days)
        if mat.empty or len(dates) < 2:
            return {"points": [], "trade_date": "", "summary": {}}

        ma50 = mat.rolling(WINDOW_MA50, min_periods=WINDOW_MA50).mean()
        ma200 = mat.rolling(WINDOW_MA200, min_periods=WINDOW_MA200).mean()
        rolling_high = mat.rolling(WINDOW_52W, min_periods=WINDOW_52W).max()
        rolling_low = mat.rolling(WINDOW_52W, min_periods=WINDOW_52W).min()

        above50 = (mat > ma50) & ma50.notna() & mat.notna()
        above200 = (mat > ma200) & ma200.notna() & mat.notna()
        new_high = (mat >= rolling_high) & rolling_high.notna()
        new_low = (mat <= rolling_low) & rolling_low.notna()

        valid = mat.notna()
        valid_count = valid.sum(axis=1).replace(0, pd.NA)
        pct50 = (above50.sum(axis=1) / valid_count * 100).astype(float)
        pct200 = (above200.sum(axis=1) / valid_count * 100).astype(float)

        diff = mat.diff()
        adv = (diff > 0).sum(axis=1)
        dec = (diff < 0).sum(axis=1)

        window = dates[-days:]
        points: list[dict[str, Any]] = []
        # A-D 线累计从窗口起点归零
        ad_cum = 0
        for d in window:
            if d in adv.index:
                ad_cum += int(adv.loc[d] - dec.loc[d])
            points.append(
                {
                    "date": to_iso(d),
                    "advancers": int(adv.loc[d]) if d in adv.index else 0,
                    "decliners": int(dec.loc[d]) if d in dec.index else 0,
                    "pct_above_ma50": round(safe_float(pct50.get(d), None) or 0.0, 1),
                    "pct_above_ma200": round(safe_float(pct200.get(d), None) or 0.0, 1),
                    "new_highs": int(new_high.loc[d].sum()) if d in new_high.index else 0,
                    "new_lows": int(new_low.loc[d].sum()) if d in new_low.index else 0,
                    "ad_line": ad_cum,
                }
            )

        latest = points[-1] if points else {}
        return {
            "trade_date": latest.get("date", ""),
            "points": points,
            "summary": {
                "pct_above_ma50": latest.get("pct_above_ma50", 0.0),
                "pct_above_ma200": latest.get("pct_above_ma200", 0.0),
                "new_highs": latest.get("new_highs", 0),
                "new_lows": latest.get("new_lows", 0),
                "ad_line": latest.get("ad_line", 0),
            },
        }

    return cached(f"us_breadth_history:{days}", _load, ttl=_BREADTH_TTL)


def get_breadth_highlights(limit: int = 30) -> dict[str, Any]:
    """52 周位置榜：创新高 / 创新低 / 距高点最近 / 距高点最远。

    基于最新交易日截面自算（不用 f10 的 52w_high/52w_low 快照，
    那份快照是单点值、无法判断「今日是否创新高」）。
    """
    limit = max(5, min(int(limit), 50))

    def _load() -> dict[str, Any]:
        if not _avail():
            return {"trade_date": "", "new_highs": [], "new_lows": [],
                    "near_high": [], "far_from_high": [], "high_low_counts": {}}
        dates, mat = _breadth_matrix(1)
        if mat.empty or len(dates) < WINDOW_52W:
            return {"trade_date": "", "new_highs": [], "new_lows": [],
                    "near_high": [], "far_from_high": [], "high_low_counts": {}}

        rolling_high = mat.rolling(WINDOW_52W, min_periods=WINDOW_52W).max()
        rolling_low = mat.rolling(WINDOW_52W, min_periods=WINDOW_52W).min()
        last = dates[-1]
        close = mat.loc[last]
        hi = rolling_high.loc[last]
        lo = rolling_low.loc[last]

        valid = close.notna() & hi.notna() & lo.notna() & (hi > 0)
        close, hi, lo = close[valid], hi[valid], lo[valid]
        # 距 52 周高点的回撤幅度（负值=在高点下方）
        drawdown = ((close / hi) - 1) * 100

        def _row(sym: str) -> dict[str, Any]:
            return {
                "symbol": sym,
                "close": round(float(close[sym]), 2),
                "high_52w": round(float(hi[sym]), 2),
                "low_52w": round(float(lo[sym]), 2),
                "drawdown_pct": round(float(drawdown[sym]), 2),
                "pct_change": 0.0,
            }

        is_high = close >= hi
        is_low = close <= lo
        highs = [_row(s) for s in drawdown[is_high].sort_values(ascending=False).head(limit).index]
        lows = [_row(s) for s in drawdown[is_low].sort_values().head(limit).index]
        near = [_row(s) for s in drawdown.sort_values(ascending=False).head(limit).index]
        far = [_row(s) for s in drawdown.sort_values().head(limit).index]

        name_map = _names(close.index.tolist())
        for group in (highs, lows, near, far):
            for item in group:
                item["name"] = name_map.get(item["symbol"], item["symbol"])
        return {
            "trade_date": to_iso(last),
            "new_highs": highs,
            "new_lows": lows,
            "near_high": near,
            "far_from_high": far,
            "high_low_counts": {
                "new_highs": int(is_high.sum()),
                "new_lows": int(is_low.sum()),
                "total": int(len(close)),
            },
        }

    return cached(f"us_breadth_highlights:{limit}", _load, ttl=_BREADTH_TTL)


def _proximity_map() -> dict[str, float]:
    """symbol -> 距 52 周高点的百分比（0=正处高点，负值=低于高点）。

    供热门榜标注「这是新高附近的放量」还是「下跌中的放量」。
    复用宽度矩阵，结果缓存，多个面板共用一次计算。
    """

    def _load() -> dict[str, float]:
        dates, mat = _breadth_matrix(1)
        if mat.empty or len(dates) < WINDOW_52W:
            return {}
        last = dates[-1]
        close = mat.loc[last]
        hi = mat.rolling(WINDOW_52W, min_periods=WINDOW_52W).max().loc[last]
        valid = close.notna() & hi.notna() & (hi > 0)
        dd = ((close[valid] / hi[valid]) - 1) * 100
        return {s: round(float(v), 2) for s, v in dd.items()}

    return cached("us:proximity_map", _load, ttl=_BREADTH_TTL)


def get_profit_leaders(limit: int = 10) -> dict[str, Any]:
    """赚钱效应榜：涨幅 × 成交额活跃度综合评分 Top N。

    评分口径与港股一致（涨幅为主、成交额为辅），便于跨市场对照。
    """
    limit = max(5, min(int(limit), 30))

    def _load() -> dict[str, Any]:
        if not _avail():
            return {"trade_date": "", "items": []}
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return {"trade_date": "", "items": []}
        df = snap.copy()
        df["amount"] = df["amount"].fillna(0.0)
        # 成交额分位（0-1）与涨幅一起打分：涨幅 70% + 活跃度 30%
        amt_rank = df["amount"].rank(pct=True).fillna(0.0)
        df["score"] = df["pct_change"] * 0.7 + amt_rank * 30.0 * 0.3
        top = df.sort_values("score", ascending=False).head(limit)
        name_map = _names(top["symbol"].tolist())
        items = []
        for _, r in top.iterrows():
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": name_map.get(r["symbol"], r["symbol"]),
                    "close": round(safe_float(r["close"]) or 0.0, 2),
                    "pct_change": round(safe_float(r["pct_change"]) or 0.0, 2),
                    "amount_yi": fmt_yi(safe_float(r["amount"]) or 0.0),
                    "score": round(float(r["score"]), 2),
                }
            )
        return {"trade_date": to_iso(latest), "items": items}

    return cached(f"us_profit_leaders:{limit}", _load)
