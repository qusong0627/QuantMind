"""美股资金与筹码 —— 内部人交易 / 机构持仓 / 派息日历 / 拆股。

对应港股模块的「南向资金 + CCASS 席位」，但美股的制度性披露口径完全不同：
- **内部人交易**（SEC Form 4，强制披露）：高管/董事/10% 以上股东的买卖，
  其中「Purchase」是最有信号价值的一类（全样本 2014-2026 仅 982 笔，
  近 90 天 103 笔）；`Transaction` 列在本数据源里**恒为空**，
  类型必须从 `Text` 前缀解析
- **机构持仓**：`mutual_fund_holders` 为逐机构明细（含 pctChange），
  `major_holders` 为 4 行无标签宽表（顺序固定：内部人占比 / 机构占比 /
  机构流通股占比 / 机构家数），按位置取值并已全样本校验
- **派息日历**：`calendar` 表含 `Ex-Dividend Date` / `Dividend Date`（404/489 只）
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached
from backend.services.api.market_analysis_shared.display import fmt_yi, safe_float
from backend.services.api.market_analysis_us.feed.base import (
    DIVIDEND_GLOB,
    SPLITS_GLOB,
    _avail,
    _load_analyst_table,
    _load_events,
    _names,
)

_HOLDINGS_TTL = 1800.0

# 内部人交易里真正有信号的两类（授予/行权/赠与属于薪酬事件，不是择时信号）
_INSIDER_SIGNAL_TYPES = ("Purchase", "Sale")

# major_holders 的行序（yfinance 固定表序，已对 487 只全样本校验：
# 第 0 行 100% 落在 [0,1]、第 3 行 100% 是 >=9 的整数）
_MAJOR_HOLDER_FIELDS = (
    "insiders_pct",
    "institutions_pct",
    "institutions_float_pct",
    "institutions_count",
)


def _insider_type(text: Any) -> str:
    """从 `Text` 前缀解析交易类型（`Transaction` 列恒为空，不可用）。"""
    if not text:
        return ""
    return str(text).split(" at price")[0].strip()


def get_insider_movers(days: int = 90, limit: int = 20) -> dict[str, Any]:
    """内部人交易榜：净买入 / 净卖出（近 N 天，按金额排序）。

    只统计 Purchase 与 Sale —— 授予、行权、赠与是薪酬事件，不是择时信号。
    同一人可能多笔，按 (symbol, insider) 汇总后再排序。
    """
    days = max(7, min(int(days), 365))
    limit = max(5, min(int(limit), 50))

    def _load() -> dict[str, Any]:
        empty = {
            "as_of": "",
            "days": days,
            "buy_count": 0,
            "sell_count": 0,
            "buy_amount_yi": 0.0,
            "sell_amount_yi": 0.0,
            "top_buys": [],
            "top_sells": [],
        }
        if not _avail():
            return empty
        it = _load_analyst_table("insider_transactions")
        if it.empty:
            return empty
        df = it.copy()
        df["txn_type"] = df["Text"].map(_insider_type)
        df = df[df["txn_type"].isin(_INSIDER_SIGNAL_TYPES)]
        df["txn_date"] = pd.to_datetime(df["Start Date"], errors="coerce")
        df["shares"] = pd.to_numeric(df["Shares"], errors="coerce")
        df["value"] = pd.to_numeric(df["Value"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[df["txn_date"].notna()]
        df = df[df["txn_date"] >= today - timedelta(days=days)]
        df = df[df["value"].notna() & (df["value"] > 0)]
        if df.empty:
            return empty

        names = _names(df["symbol"].unique().tolist())

        def _rank(sub: pd.DataFrame) -> list[dict[str, Any]]:
            agg = (
                sub.groupby(["symbol", "Insider", "Position"], dropna=False)
                .agg(
                    value=("value", "sum"),
                    shares=("shares", "sum"),
                    trades=("value", "size"),
                    last_date=("txn_date", "max"),
                )
                .reset_index()
                .sort_values("value", ascending=False)
                .head(limit)
            )
            out = []
            for _, r in agg.iterrows():
                out.append(
                    {
                        "symbol": r["symbol"],
                        "name": names.get(r["symbol"], r["symbol"]),
                        "insider": str(r["Insider"] or ""),
                        "position": str(r["Position"] or ""),
                        "value_yi": fmt_yi(safe_float(r["value"])),
                        "shares": int(safe_float(r["shares"])),
                        "trades": int(safe_float(r["trades"])),
                        "last_date": r["last_date"].strftime("%Y-%m-%d"),
                    }
                )
            return out

        buys = df[df["txn_type"] == "Purchase"]
        sells = df[df["txn_type"] == "Sale"]
        return {
            "as_of": today.strftime("%Y-%m-%d"),
            "days": days,
            "buy_count": int(len(buys)),
            "sell_count": int(len(sells)),
            "buy_amount_yi": fmt_yi(float(buys["value"].sum())),
            "sell_amount_yi": fmt_yi(float(sells["value"].sum())),
            "top_buys": _rank(buys),
            "top_sells": _rank(sells),
        }

    return cached(f"us_insider_movers:{days}:{limit}", _load, ttl=_HOLDINGS_TTL)


def get_institutional_holders(limit: int = 30) -> dict[str, Any]:
    """机构持仓：全市场机构结构 + 单个标的的机构增减持榜。

    增减持按「机构家数变化」与「估算增减股数」双口径：
    `pctChange` 是该机构自身持股的变化率，还原股数变化用
    `Shares * pctChange / (1 + pctChange)`（Shares 为期末持股）。
    """
    limit = max(5, min(int(limit), 50))

    def _load() -> dict[str, Any]:
        empty = {
            "institutions_pct_median": None,
            "insiders_pct_median": None,
            "coverage": 0,
            "top_increases": [],
            "top_decreases": [],
        }
        if not _avail():
            return empty
        mh = _load_analyst_table("major_holders")
        mf = _load_analyst_table("mutual_fund_holders")
        med_inst = med_ins = None
        coverage = 0
        if not mh.empty:
            mh = mh.copy()
            mh["pos"] = mh.groupby("symbol").cumcount()
            wide = mh.pivot_table(
                index="symbol", columns="pos", values="Value", aggfunc="first"
            )
            wide.columns = [
                _MAJOR_HOLDER_FIELDS[c] if c < len(_MAJOR_HOLDER_FIELDS) else f"pos{c}"
                for c in wide.columns
            ]
            coverage = int(len(wide))
            if "institutions_pct" in wide.columns:
                med_inst = round(
                    safe_float(wide["institutions_pct"].median()) * 100, 1
                )
            if "insiders_pct" in wide.columns:
                med_ins = round(safe_float(wide["insiders_pct"].median()) * 100, 2)

        if mf.empty:
            return {
                "institutions_pct_median": med_inst,
                "insiders_pct_median": med_ins,
                "coverage": coverage,
                "top_increases": [],
                "top_decreases": [],
            }
        df = mf.copy()
        df["pctHeld"] = pd.to_numeric(df["pctHeld"], errors="coerce")
        df["pctChange"] = pd.to_numeric(df["pctChange"], errors="coerce")
        df["Shares"] = pd.to_numeric(df["Shares"], errors="coerce")
        df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
        df["delta_shares"] = df["Shares"] * df["pctChange"] / (1 + df["pctChange"])
        df["delta_value"] = df["Value"] * df["pctChange"] / (1 + df["pctChange"])
        df = df[df["delta_value"].notna()]

        agg = (
            df.groupby("symbol")
            .agg(
                delta_value=("delta_value", "sum"),
                holders=("Holder", "size"),
                increased=("pctChange", lambda s: int((s > 0).sum())),
                decreased=("pctChange", lambda s: int((s < 0).sum())),
                inst_pct=("pctHeld", "sum"),
            )
            .reset_index()
        )
        names = _names(agg["symbol"].tolist())

        def _rows(sub: pd.DataFrame) -> list[dict[str, Any]]:
            out = []
            for _, r in sub.iterrows():
                out.append(
                    {
                        "symbol": r["symbol"],
                        "name": names.get(r["symbol"], r["symbol"]),
                        "delta_value_yi": fmt_yi(safe_float(r["delta_value"])),
                        "holders": int(safe_float(r["holders"])),
                        "increased": int(r["increased"]),
                        "decreased": int(r["decreased"]),
                        # 注意：这是**明细表内已列出的头部机构**合计占比，
                        # 不是全机构持股（全口径见 major_holders 的 institutions_pct）
                        "top_holders_pct": round(safe_float(r["inst_pct"]) * 100, 1),
                    }
                )
            return out

        return {
            "institutions_pct_median": med_inst,
            "insiders_pct_median": med_ins,
            "coverage": coverage,
            "report_date": _report_date(mf),
            "top_increases": _rows(
                agg.sort_values("delta_value", ascending=False).head(limit)
            ),
            "top_decreases": _rows(
                agg.sort_values("delta_value").head(limit)
            ),
        }

    return cached(f"us_institutional_holders:{limit}", _load, ttl=_HOLDINGS_TTL)


def _report_date(mf: pd.DataFrame) -> str:
    """机构持仓的披露日（13F 口径，通常滞后一个季度）。"""
    if "Date Reported" not in mf.columns:
        return ""
    ts = pd.to_datetime(mf["Date Reported"], errors="coerce").max()
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def get_dividend_calendar(days: int = 60, limit: int = 40) -> dict[str, Any]:
    """未来 N 天内的除息日历（`calendar` 表的 Ex-Dividend Date）。

    除息日对量化有意义：持有到除息日可获股息但股价会除权，
    跨窗口收益计算需要避开或补偿。
    """
    days = max(7, min(int(days), 180))
    limit = max(5, min(int(limit), 100))

    def _load() -> dict[str, Any]:
        empty = {"as_of": "", "days": days, "total": 0, "items": []}
        if not _avail():
            return empty
        cal = _load_analyst_table("calendar")
        if cal.empty or "Ex-Dividend Date" not in cal.columns:
            return empty
        df = cal.copy()
        df["ex_date"] = pd.to_datetime(df["Ex-Dividend Date"], errors="coerce")
        df["pay_date"] = pd.to_datetime(df["Dividend Date"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[df["ex_date"].notna()]
        df = df[df["ex_date"] >= today]
        df = df[df["ex_date"] <= today + timedelta(days=days)]
        if df.empty:
            return empty
        df = df.sort_values("ex_date").head(limit)
        names = _names(df["symbol"].tolist())
        items = []
        for _, r in df.iterrows():
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "ex_dividend_date": r["ex_date"].strftime("%Y-%m-%d"),
                    "dividend_date": (
                        r["pay_date"].strftime("%Y-%m-%d")
                        if pd.notna(r["pay_date"])
                        else None
                    ),
                    "days_until": int((r["ex_date"] - today).days),
                }
            )
        return {
            "as_of": today.strftime("%Y-%m-%d"),
            "days": days,
            "total": len(items),
            "items": items,
        }

    return cached(f"us_dividend_calendar:{days}:{limit}", _load, ttl=_HOLDINGS_TTL)


def get_recent_splits(days: int = 365, limit: int = 30) -> dict[str, Any]:
    """近期拆股记录（`3_financial_data/splits` 事件表）。

    拆股会污染未复权日线的跨期收益，榜单同时用于排查
    「某只股票 20 日收益异常」的原因。
    """
    days = max(30, min(int(days), 1825))
    limit = max(5, min(int(limit), 100))

    def _load() -> dict[str, Any]:
        empty = {"as_of": "", "days": days, "items": []}
        if not _avail():
            return empty
        sp = _load_events(SPLITS_GLOB)
        if sp.empty:
            return empty
        df = sp.copy()
        df["split_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df["ratio"] = pd.to_numeric(df["split_ratio"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[df["split_date"].notna()]
        df = df[df["split_date"] >= today - timedelta(days=days)]
        if df.empty:
            return empty
        df = df.sort_values("split_date", ascending=False).head(limit)
        names = _names(df["symbol"].tolist())
        items = []
        for _, r in df.iterrows():
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "split_date": r["split_date"].strftime("%Y-%m-%d"),
                    "ratio": round(safe_float(r["ratio"]), 4),
                }
            )
        return {
            "as_of": today.strftime("%Y-%m-%d"),
            "days": days,
            "items": items,
        }

    return cached(f"us_recent_splits:{days}:{limit}", _load, ttl=_HOLDINGS_TTL)


def get_dividend_history_summary(limit: int = 30) -> list[dict[str, Any]]:
    """近期派息记录汇总（`3_financial_data/dividend` 事件表，按标的汇总）。

    与「派息日历」（未来）互补：这里是**已发生**的派息，
    用于识别稳定分红标的（股息策略的原始名单）。
    """
    limit = max(5, min(int(limit), 60))

    def _load() -> list[dict[str, Any]]:
        if not _avail():
            return []
        dv = _load_events(DIVIDEND_GLOB)
        if dv.empty:
            return []
        df = dv.copy()
        df["pay_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df["dividend"] = pd.to_numeric(df["dividend"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[df["pay_date"].notna() & df["dividend"].notna()]
        df = df[df["pay_date"] >= today - timedelta(days=365)]
        if df.empty:
            return []
        agg = (
            df.groupby("symbol")
            .agg(
                payments=("dividend", "size"),
                total=("dividend", "sum"),
                last_date=("pay_date", "max"),
            )
            .reset_index()
        )
        # 至少按季派息（近一年 >= 3 次）才纳入「稳定分红」名单
        agg = agg[agg["payments"] >= 3]
        agg = agg.sort_values("total", ascending=False).head(limit)
        names = _names(agg["symbol"].tolist())
        return [
            {
                "symbol": r["symbol"],
                "name": names.get(r["symbol"], r["symbol"]),
                "payments": int(r["payments"]),
                "total_per_share": round(safe_float(r["total"]), 2),
                "last_date": r["last_date"].strftime("%Y-%m-%d"),
            }
            for _, r in agg.iterrows()
        ]

    return cached(f"us_dividend_summary:{limit}", _load, ttl=_HOLDINGS_TTL)
