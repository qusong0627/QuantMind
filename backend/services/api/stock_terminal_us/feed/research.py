"""美股个股终端 —— 卖方覆盖与披露面板（分析师 / 财报 / 内部人 / 机构持仓）。

这四块都是「事件式披露」小表，每股一文件、TTL 缓存，与财务三表（`detail._financials`）
分开维护：改披露口径不必动财务标签映射。

响应形状对齐前端 `stock-terminal-us/types.ts`（缺数据的键给 null / 空数组，不省略）：
- `get_analysts`  -> {target, ratings[], upgrades[]}
- `get_earnings`  -> {history[], upcoming[]}
- `get_insiders`  -> {items[], net{}}
- `get_holdings`  -> {insiders_pct, institutions_pct, institutions_float_pct,
                      institutions_count, funds[], reported_date}

口径要点（都是踩过的坑）：
- `earnings_history.surprisePercent` 是**小数**（0.0452 = 4.52%），
  而 `earnings_dates.Surprise(%)` 是**百分数**（6.74）—— 两处口径不同，分别归一
- `earnings_dates.Earnings Date` **带时区**（`2026-10-29 16:00:00-04:00`），
  与 tz-naive 的 today 直接比较会抛 TypeError
- `calendar.Earnings Date` 是**列表列**（`array([datetime.date(...)])`），取首个元素；
  它是财报日期的权威口径（10-30），earnings_dates 的东方时区时间戳会差一天
- `major_holders` 是 4 行**无标签宽表**，顺序固定（内部人占比 / 机构占比 /
  机构流通股占比 / 机构家数），按位置取值；13F 口径披露滞后约一季
- 内部人交易 `Transaction` 列**恒为空串**，类型从 `Text` 前缀解析；
  AAPL 78 行里 40 行 `Text` 为空（授予/行权等无价格事件）→ 类型归 `other`
- `upgrades_downgrades.Action` 只出现 main/reit/down/up/init 五种，
  归一为前端枚举 up|down|init|reiterated|other；up/down 判定复用
  `market_analysis_us.feed.analysts._grade_direction`（评级词档位比较），不另写一套
- 目标价列里 0 是 yahoo 的「无值」占位（Needham 那行 currentPriceTarget=0.0），
  出口一律转 None，否则前端会显示 "0"
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.display import safe_float
from backend.services.api.market_analysis_us.feed.analysts import _grade_direction
from backend.services.api.market_analysis_us.feed.holdings import (
    _MAJOR_HOLDER_FIELDS,
    _insider_type,
)
from backend.services.api.stock_terminal_us.feed.base import _symbol_table
from backend.services.api.stock_terminal_us.feed.labels import num

ANALYST_REL = "4_analyst"
_UPGRADE_LIMIT = 30
_EARNINGS_HISTORY_LIMIT = 8
_EARNINGS_UPCOMING_LIMIT = 3
_INSIDER_LIMIT = 30
_FUND_LIMIT = 15

# 内部人交易类型归一：只有 Purchase / Sale 是择时信号，其余（授予/行权/赠与）一律 other
_INSIDER_TYPE_MAP = {"Purchase": "buy", "Sale": "sell"}

# Action 原始值（yahoo 全样本只有这五种）-> 前端枚举
_ACTION_WORDS: dict[str, str] = {
    "init": "init",
    "initiate": "init",
    "initiated": "init",
    "reit": "reiterated",
    "reiterated": "reiterated",
    "reiterate": "reiterated",
    "main": "reiterated",
    "maintain": "reiterated",
    "maintained": "reiterated",
    "up": "up",
    "upgrade": "up",
    "down": "down",
    "downgrade": "down",
}


def _naive_ts(series: Any) -> pd.Series:
    """日期列 -> 无时区 Timestamp（带时区的先归一到美东墙钟再摘掉时区）。"""
    ts = pd.to_datetime(series, errors="coerce")
    if isinstance(ts, pd.Series) and getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("America/New_York").dt.tz_localize(None)
    return ts


def _first_date(value: Any) -> str | None:
    """从可能为列表的值里取首个日期 -> YYYY-MM-DD。

    `calendar.Earnings Date` 在 parquet 里是列表列，直接 str() 会得到
    `"['2026-10-30']"` 而解析失败。
    """
    if hasattr(value, "__len__") and not isinstance(value, str):
        value = value[0] if len(value) else None
    if value is None:
        return None
    ts = pd.to_datetime(str(value), errors="coerce")
    return None if pd.isna(ts) else str(ts)[:10]


def _target(value: Any) -> float | None:
    """目标价出口：0/负数是 yahoo 的「无值」占位，转 None（否则前端显示 0）。"""
    v = safe_float(value, None)
    if v is None or v <= 0:
        return None
    return round(v, 2)


def _norm_action(from_grade: Any, to_grade: Any, action: Any) -> str:
    """评级动作归一到 up|down|init|reiterated|other。

    `init`（首次覆盖）与显式的 up/down 直接采信原始 Action；
    其余（reit/main 这类「维持」词，或 Action 为空的行）复用市场分析模块的
    `_grade_direction`：比较 FromGrade/ToGrade 的档位（Buy/Outperform/Overweight→3
    … Sell→0），评级真的动了就归 up/down（Action 说「维持」但档位动了是 yahoo
    的常见脏数据，此时以档位为准），没动才是 reiterated。
    """
    act = str(action or "").strip().lower()
    word = _ACTION_WORDS.get(act)
    if word == "init":
        return "init"
    if word in ("up", "down"):
        return word
    direction = _grade_direction(from_grade, to_grade, action)
    if direction in ("up", "down"):
        return direction
    return "reiterated" if word == "reiterated" else "other"


def get_analysts(sym: str) -> dict[str, Any]:
    """目标价快照 / 评级分布（0m 起按时间倒序）/ 近期升降级流水（30 条，日期倒序）。"""
    target: dict[str, Any] | None = None
    pt = _symbol_table(f"{ANALYST_REL}/analyst_price_targets", sym)
    if not pt.empty:
        r = pt.iloc[0]
        row = {
            "current": _target(r.get("current")),
            "high": _target(r.get("high")),
            "low": _target(r.get("low")),
            "mean": _target(r.get("mean")),
            "median": _target(r.get("median")),
        }
        if any(v is not None for v in row.values()):
            target = row

    ratings: list[dict[str, Any]] = []
    recs = _symbol_table(f"{ANALYST_REL}/recommendations", sym)
    if not recs.empty:
        recs = recs.copy()
        recs["_i"] = pd.to_numeric(recs.get("index"), errors="coerce")
        for _, r in recs.sort_values("_i").iterrows():
            ratings.append(
                {
                    # period: 0m = 当前月，其后 -1m/-2m/-3m（按 index 升序即时间倒序）
                    "period": str(r.get("period") or ""),
                    "strongBuy": int(safe_float(r.get("strongBuy"))),
                    "buy": int(safe_float(r.get("buy"))),
                    "hold": int(safe_float(r.get("hold"))),
                    "sell": int(safe_float(r.get("sell"))),
                    "strongSell": int(safe_float(r.get("strongSell"))),
                }
            )

    upgrades: list[dict[str, Any]] = []
    ud = _symbol_table(f"{ANALYST_REL}/upgrades_downgrades", sym)
    if not ud.empty:
        ud = ud.copy()
        ud["_d"] = pd.to_datetime(ud.get("GradeDate"), errors="coerce")
        ud = ud[ud["_d"].notna()].sort_values("_d", ascending=False)
        # 同日同机构可能重复报送（不同分析师），取最新一条（与市场分析模块同口径）
        ud = ud.drop_duplicates(["GradeDate", "Firm"], keep="first")
        for _, r in ud.head(_UPGRADE_LIMIT).iterrows():
            upgrades.append(
                {
                    "date": str(r["_d"])[:10],
                    "firm": str(r.get("Firm") or ""),
                    "to_grade": str(r.get("ToGrade") or ""),
                    "from_grade": str(r.get("FromGrade") or ""),
                    "action": _norm_action(
                        r.get("FromGrade"), r.get("ToGrade"), r.get("Action")
                    ),
                    "current_target": _target(r.get("currentPriceTarget")),
                    "prior_target": _target(r.get("priorPriceTarget")),
                }
            )
    return {"target": target, "ratings": ratings, "upgrades": upgrades}


def get_earnings(sym: str) -> dict[str, Any]:
    """季度 EPS 实际 vs 预期（近 8 期，倒序）+ 未来财报日（calendar 优先）。

    `upcoming` 的日期以 `calendar.Earnings Date` 为准（yahoo 日历口径，如 2026-10-30）；
    日历缺失/过期时才回落到 `earnings_dates` 的未来行（东八区显示会差一天）。
    """
    history: list[dict[str, Any]] = []
    hist = _symbol_table(f"{ANALYST_REL}/earnings_history", sym)
    if not hist.empty:
        hist = hist.copy()
        hist["_q"] = pd.to_datetime(hist.get("quarter"), errors="coerce")
        hist = hist[hist["_q"].notna()].sort_values("_q", ascending=False)
        for _, r in hist.head(_EARNINGS_HISTORY_LIMIT).iterrows():
            history.append(
                {
                    "quarter": str(r["_q"])[:10],
                    # 该表 surprisePercent 是小数（0.0452 = 4.52%），×100 归一到百分数
                    "actual": num(r.get("epsActual"), 4),
                    "estimate": num(r.get("epsEstimate"), 4),
                    "surprise_pct": num(safe_float(r.get("surprisePercent")) * 100),
                }
            )

    today = pd.Timestamp.today().normalize()
    upcoming: list[dict[str, Any]] = []
    cal = _symbol_table(f"{ANALYST_REL}/calendar", sym)
    if not cal.empty and "Earnings Date" in cal.columns:
        next_date = _first_date(cal.iloc[0].get("Earnings Date"))
        if next_date and pd.Timestamp(next_date) >= today:
            upcoming.append(
                {
                    "date": next_date,
                    "eps_estimate": num(cal.iloc[0].get("Earnings Average"), 4),
                    "revenue_estimate": num(cal.iloc[0].get("Revenue Average")),
                }
            )

    if not upcoming:
        dates = _symbol_table(f"{ANALYST_REL}/earnings_dates", sym)
        if not dates.empty:
            dates = dates.copy()
            dates["_d"] = _naive_ts(dates.get("Earnings Date"))
            future = dates[dates["_d"].notna() & (dates["_d"] >= today)].sort_values(
                "_d"
            )
            for _, r in future.head(_EARNINGS_UPCOMING_LIMIT).iterrows():
                upcoming.append(
                    {
                        "date": str(r["_d"])[:10],
                        "eps_estimate": num(r.get("EPS Estimate"), 4),
                        "revenue_estimate": None,
                    }
                )
    return {"history": history, "upcoming": upcoming}


def _insider_items(df: pd.DataFrame) -> list[dict[str, Any]]:
    df = df.copy()
    df["_d"] = pd.to_datetime(df.get("Start Date"), errors="coerce")
    df = df[df["_d"].notna()].sort_values("_d", ascending=False).head(_INSIDER_LIMIT)
    items: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        txn = _insider_type(r.get("Text"))
        items.append(
            {
                "date": str(r["_d"])[:10],
                "insider": str(r.get("Insider") or ""),
                "position": str(r.get("Position") or ""),
                # 类型只从 Text 前缀解析（Transaction 列恒空）；授予/行权等无价格事件 -> other
                "type": _INSIDER_TYPE_MAP.get(txn, "other"),
                "shares": int(safe_float(r.get("Shares"))),
                "value": num(r.get("Value")),
            }
        )
    return items


def get_insiders(sym: str) -> dict[str, Any]:
    """SEC Form 4 内部人交易（近 30 条，倒序）+ 这批流水的买卖净额。"""
    df = _symbol_table(f"{ANALYST_REL}/insider_transactions", sym)
    items = _insider_items(df) if not df.empty else []
    buys = [it for it in items if it["type"] == "buy"]
    sells = [it for it in items if it["type"] == "sell"]
    buy_value = round(sum(it["value"] or 0 for it in buys), 2)
    sell_value = round(sum(it["value"] or 0 for it in sells), 2)
    return {
        "items": items,
        "net": {
            "buy_value": buy_value,
            "sell_value": sell_value,
            "net_value": round(buy_value - sell_value, 2),
            "buy_count": len(buys),
            "sell_count": len(sells),
        },
    }


def _major_holders(mh: pd.DataFrame) -> dict[str, Any]:
    """major_holders 4 行固定表序 -> 占比字段（前 3 行是小数 ×100，第 4 行是家数）。"""
    out: dict[str, Any] = {}
    if mh.empty or "Value" not in mh.columns:
        return out
    vals = pd.to_numeric(mh["Value"], errors="coerce").tolist()
    for field, raw in zip(_MAJOR_HOLDER_FIELDS, vals, strict=False):
        if pd.isna(raw):
            continue
        out[field] = (
            int(safe_float(raw)) if field == "institutions_count" else num(raw * 100)
        )
    return out


def _fund_rows(mf: pd.DataFrame) -> list[dict[str, Any]]:
    mf = mf.copy()
    mf["_pct"] = pd.to_numeric(mf.get("pctHeld"), errors="coerce")
    mf = mf[mf["_pct"].notna()].sort_values("_pct", ascending=False).head(_FUND_LIMIT)
    funds: list[dict[str, Any]] = []
    for _, r in mf.iterrows():
        funds.append(
            {
                "holder": str(r.get("Holder") or ""),
                "pct_held": num(safe_float(r.get("pctHeld")) * 100),
                "shares": int(safe_float(r.get("Shares"))),
                "value": num(r.get("Value")),
                "pct_change": num(safe_float(r.get("pctChange")) * 100),
                "date_reported": str(r.get("Date Reported") or "")[:10],
            }
        )
    return funds


def get_holdings(sym: str) -> dict[str, Any]:
    """机构持股结构（major_holders）+ 头部机构明细（13F，滞后一季）。"""
    mh = _symbol_table(f"{ANALYST_REL}/major_holders", sym)
    major = _major_holders(mh)

    mf = _symbol_table(f"{ANALYST_REL}/mutual_fund_holders", sym)
    funds: list[dict[str, Any]] = []
    reported: str | None = None
    if not mf.empty:
        funds = _fund_rows(mf)
        if "Date Reported" in mf.columns:
            ts = pd.to_datetime(mf["Date Reported"], errors="coerce").max()
            reported = None if pd.isna(ts) else str(ts)[:10]
    return {
        "insiders_pct": major.get("insiders_pct"),
        "institutions_pct": major.get("institutions_pct"),
        "institutions_float_pct": major.get("institutions_float_pct"),
        "institutions_count": major.get("institutions_count"),
        "funds": funds,
        "reported_date": reported,
    }
