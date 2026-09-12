"""美股个股终端 —— 内部人交易（SEC Form 4）与机构持仓（13F）两个披露面板。

响应形状对齐前端 `stock-terminal-us/types.ts`（缺数据的键给 null / 空数组，不省略）：
- `get_insiders`  -> {items[], net{}}
- `get_holdings`  -> {insiders_pct, institutions_pct, institutions_float_pct,
                      institutions_count, funds[], reported_date}

口径要点（都是踩过的坑）：
- 内部人交易 `Transaction` 列**恒为空串**，类型从 `Text` 前缀解析
  （`Sale at price...` / `Purchase at price...`）；AAPL 78 行里 40 行 `Text` 为空
  （授予/行权等无价格事件）→ 归 `other`，绝不当作买卖信号
- `net` 按**返回的这 30 条流水**计算（面板里数字与列表同源，不会出现
  「净额算的是全历史、列表只显示近 30 条」的口径错位）
- `major_holders` 是 4 行**无标签宽表**，顺序固定（内部人占比 / 机构占比 /
  机构流通股占比 / 机构家数），按位置取值；前 3 行是小数需 ×100
- `mutual_fund_holders` 的 Date Reported 为 13F 披露日，滞后约一个季度；
  `reported_date` 取明细表内最大披露日，供面板标注
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.display import safe_float
from backend.services.api.market_analysis_us.feed.holdings import (
    _MAJOR_HOLDER_FIELDS,
    _insider_type,
)
from backend.services.api.stock_terminal_us.feed.base import _symbol_table
from backend.services.api.stock_terminal_us.feed.labels import num

ANALYST_REL = "4_analyst"
_INSIDER_LIMIT = 30
_FUND_LIMIT = 15

# 内部人交易类型归一：只有 Purchase / Sale 是择时信号，其余（授予/行权/赠与）一律 other
_INSIDER_TYPE_MAP = {"Purchase": "buy", "Sale": "sell"}


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
