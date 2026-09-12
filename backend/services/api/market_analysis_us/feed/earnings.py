"""美股财报季 —— 日历 / 超预期 / 预期修正。

美股是事件驱动市场，财报季的节奏对量化调仓影响很大，这是本模块相对
港股/A 股模块的差异化能力之一。

数据源 `4_analyst/`（每股一文件）：
- `calendar`           下一场财报日 + EPS/营收预期区间（每股 1 行，快照）
- `earnings_dates`     历史 + 未来财报日与超预期（每股约 25 行）
- `earnings_history`   近 4 个季度实际 vs 预期
- `earnings_estimate` / `revenue_estimate`  当前季度/年度的预期与增速

口径坑：
- `calendar.Earnings Date` 是**数组**（yfinance 返回 list），需取首元素
- `earnings_dates.Earnings Date` 带时区（Asia/Tokyo），需先归一再比较
- `growth` / `surprisePercent` 是**小数**（0.0863 = 8.63%），展示前 ×100
- 超预期幅度会出现 200%+ 的极端值（如 NKE 0.72 vs 预估 0.13）：已用
  `earnings_history` 与 `earnings_dates` 两张独立来源交叉验证一致，
  是**源数据的预估列本身偏低**，不是本模块的计算口径问题，不要「修正」
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import safe_float
from backend.services.api.market_analysis_us.feed.base import (
    _avail,
    _load_analyst_table,
    _names,
)

_EARNINGS_TTL = 1800.0  # 分析师类数据每晚更新一次


def _first_date(value: Any) -> pd.Timestamp | None:
    """`calendar.Earnings Date` 可能是数组/标量/字符串 —— 统一取首个日期。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    elif hasattr(value, "__len__") and not isinstance(value, str):
        try:  # numpy array
            value = value[0] if len(value) else None
        except TypeError:
            pass
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts


def _naive_dates(series: pd.Series) -> pd.Series:
    """带时区的时间戳归一到「无时区的日期」（投资界按当地日历日看财报）。"""
    ts = pd.to_datetime(series, errors="coerce", utc=True)
    return ts.dt.tz_convert(None).dt.normalize()


def get_earnings_calendar(days: int = 30, limit: int = 50) -> dict[str, Any]:
    """未来 N 天内待披露的财报（含 EPS/营收预期区间）。"""
    days = max(1, min(int(days), 120))
    limit = max(5, min(int(limit), 100))

    def _load() -> dict[str, Any]:
        empty = {"as_of": "", "days": days, "total": 0, "items": []}
        if not _avail():
            return empty
        cal = _load_analyst_table("calendar")
        if cal.empty:
            return empty
        rows = []
        today = pd.Timestamp(date.today())
        horizon = today + timedelta(days=days)
        for _, r in cal.iterrows():
            dt = _first_date(r.get("Earnings Date"))
            if dt is None:
                continue
            if dt.tzinfo is not None:
                dt = dt.tz_convert(None)
            dt = dt.normalize()
            if not (today <= dt <= horizon):
                continue
            rows.append(
                {
                    "symbol": r["symbol"],
                    "earnings_date": dt.strftime("%Y-%m-%d"),
                    "days_until": int((dt - today).days),
                    "eps_avg": _f(r.get("Earnings Average")),
                    "eps_low": _f(r.get("Earnings Low")),
                    "eps_high": _f(r.get("Earnings High")),
                    "revenue_avg": _f(r.get("Revenue Average")),
                    "revenue_low": _f(r.get("Revenue Low")),
                    "revenue_high": _f(r.get("Revenue High")),
                }
            )
        rows.sort(key=lambda x: (x["earnings_date"], x["symbol"]))
        names = _names([r["symbol"] for r in rows])
        for r in rows:
            r["name"] = names.get(r["symbol"], r["symbol"])
        return {
            "as_of": today.strftime("%Y-%m-%d"),
            "days": days,
            "total": len(rows),
            "items": rows[:limit],
        }

    return cached(f"us_earnings_calendar:{days}:{limit}", _load, ttl=_EARNINGS_TTL)


def get_earnings_surprises(limit: int = 30, lookback_days: int = 120) -> dict[str, Any]:
    """近期已披露财报的超预期榜（实际 EPS vs 预期）。

    只取**已公布**（Reported EPS 非空）的记录，按超预期幅度排序。
    财报季的「超预期幅度」是 PEAD（财报后漂移）策略的原始信号。
    """
    limit = max(5, min(int(limit), 50))
    lookback_days = max(7, min(int(lookback_days), 400))

    def _load() -> dict[str, Any]:
        empty = {"as_of": "", "lookback_days": lookback_days, "items": []}
        if not _avail():
            return empty
        ed = _load_analyst_table("earnings_dates")
        if ed.empty or "Reported EPS" not in ed.columns:
            return empty
        df = ed.copy()
        df["report_date"] = _naive_dates(df["Earnings Date"])
        df["reported"] = pd.to_numeric(df["Reported EPS"], errors="coerce")
        df["estimate"] = pd.to_numeric(df["EPS Estimate"], errors="coerce")
        df["surprise"] = pd.to_numeric(df["Surprise(%)"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[df["reported"].notna() & df["report_date"].notna()]
        df = df[df["report_date"] >= today - timedelta(days=lookback_days)]
        if df.empty:
            return empty
        df = df.sort_values("report_date", ascending=False).drop_duplicates(
            ["symbol"], keep="first"
        )
        # Surprise(%) 在部分行是小数、部分行是百分数 —— 统一按「差值/预期」重算最稳
        calc = ((df["reported"] - df["estimate"]) / df["estimate"].abs()) * 100
        df["surprise_pct"] = calc.where(df["estimate"].notna() & (df["estimate"] != 0))
        df["surprise_pct"] = df["surprise_pct"].fillna(df["surprise"])
        df = df[df["surprise_pct"].notna()]
        df = df.sort_values("surprise_pct", ascending=False).head(limit)
        names = _names(df["symbol"].tolist())
        items = []
        for _, r in df.iterrows():
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "report_date": r["report_date"].strftime("%Y-%m-%d"),
                    "reported_eps": _f(r["reported"]),
                    "estimate_eps": _f(r["estimate"]),
                    "surprise_pct": round(safe_float(r["surprise_pct"]), 2),
                }
            )
        return {
            "as_of": today.strftime("%Y-%m-%d"),
            "lookback_days": lookback_days,
            "items": items,
        }

    return cached(f"us_earnings_surprises:{limit}:{lookback_days}", _load, ttl=_EARNINGS_TTL)


def get_earnings_revisions(limit: int = 30) -> dict[str, Any]:
    """盈利预期修正榜：当前季度/下季度的 EPS 与营收同比增速。

    `growth` 是小数（0.0863 = +8.63%），此处统一换算成百分数输出。
    """
    limit = max(5, min(int(limit), 50))

    def _load() -> dict[str, Any]:
        empty = {"items": []}
        if not _avail():
            return empty
        eps = _load_analyst_table("earnings_estimate")
        rev = _load_analyst_table("revenue_estimate")
        if eps.empty:
            return empty
        eps = eps.copy()
        eps["period"] = eps["period"].astype(str)
        cur = eps[eps["period"] == "0q"][["symbol", "avg", "growth", "numberOfAnalysts"]]
        cur = cur.rename(
            columns={
                "avg": "eps_avg",
                "growth": "eps_growth",
                "numberOfAnalysts": "analyst_count",
            }
        )
        if not rev.empty:
            rev = rev.copy()
            rev["period"] = rev["period"].astype(str)
            r0 = rev[rev["period"] == "0q"][["symbol", "growth"]].rename(
                columns={"growth": "revenue_growth"}
            )
            cur = cur.merge(r0, on="symbol", how="left")
        else:
            cur["revenue_growth"] = None
        cur["eps_growth"] = pd.to_numeric(cur["eps_growth"], errors="coerce")
        cur = cur[cur["eps_growth"].notna()]
        cur = cur.sort_values("eps_growth", ascending=False).head(limit)
        names = _names(cur["symbol"].tolist())
        items = []
        for _, r in cur.iterrows():
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "eps_avg": _f(r["eps_avg"]),
                    "eps_growth_pct": round(safe_float(r["eps_growth"]) * 100, 2),
                    "revenue_growth_pct": (
                        round(safe_float(r["revenue_growth"]) * 100, 2)
                        if pd.notna(r.get("revenue_growth"))
                        else None
                    ),
                    "analyst_count": int(safe_float(r.get("analyst_count"))),
                }
            )
        return {"items": items}

    return cached(f"us_earnings_revisions:{limit}", _load, ttl=_EARNINGS_TTL)


def _f(value: Any) -> float | None:
    """转 float；空值返回 None（不落 0，0 会被前端当成真实值）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out
