"""美股个股终端 —— 个股详情聚合（响应形状与前端 `types.ts` 严格对齐）。

顶层：`{symbol, name, trade_date, overview, valuation, financials, analysts,
earnings, insiders, holdings, corporate_actions, notes}`。

**容错约定**：任一面板计算失败只记 warning 并返回空骨架（键齐全、值为 null/空数组），
整体不 500；某只标的缺某类数据是常态（如无分析师覆盖的小票），前端按「无数据」降级渲染。

**每股一文件**：这里一律走 `_symbol_table`（单标的文件 + TTL 缓存），
不调用 `_load_analyst_table`（全市场截面口径，单表 18 万行，为一只股票加载不值当）。

模块分工：
- 本文件：聚合器 + overview / valuation / financials(三表) / corporate_actions
- `research.py`：分析师 / 财报（卖方覆盖）
- `holdings.py`：内部人 / 机构持仓（筹码披露）
- `labels.py`：财务中文标签映射与数值出口工具

口径提醒：估值走 f10 快照（`5_technical_derived/valuation` 分区自 2026-08-28 起全 null，
不可用）；财务只有年报；机构持仓是 13F 滞后一季；`en_name` 全库为空串 -> 出口转 None。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.display import safe_float
from backend.services.api.market_analysis_us.feed.valuation import (
    _MIN_MARKET_CAP,
    _PB_MIN,
    _PE_MIN,
    _SIZE_TIERS,
)
from backend.services.api.stock_terminal_us.feed import holdings, research
from backend.services.api.stock_terminal_us.feed.base import (
    DIVIDEND_DIR,
    FIN_DIR,
    TERMINAL_NOTES,
    _f10_snapshot,
    _name_of,
    _symbol_exists,
    _symbol_meta,
    _symbol_table,
    cap_display,
    change_pct,
    latest_bars,
    normalize_symbol,
    to_iso,
)
from backend.services.api.stock_terminal_us.feed.kline import _splits
from backend.services.api.stock_terminal_us.feed.labels import (
    BALANCE_LABELS,
    CASHFLOW_LABELS,
    INCOME_LABELS,
    num,
)

logger = logging.getLogger(__name__)

_DIVIDEND_LIMIT = 20
_FISCAL_YEARS = 5

# ---- 面板空骨架（键齐全，前端不必防御 undefined；NaN/None 一律 null） ----


def _skeleton(names: str, **extra: Any) -> dict[str, Any]:
    """空骨架：空格分隔的键名 -> 值全为 None；**extra 用于 a=[]/a=None 这类显式缺省。"""
    return {**dict.fromkeys(names.split()), **extra}


_EMPTY_OVERVIEW = _skeleton(
    "cn_name en_name sector industry close pct_change market_cap cap_display week52_high week52_low avg_volume trade_date"
)
_EMPTY_VALUATION = _skeleton(
    "pe_ratio pb_ratio dividend_yield market_cap week52_high week52_low source asof size_tier stale_warning"
)
_EMPTY_FINANCIALS = _skeleton("", periods=[], income=[], balance=[], cashflow=[])
_EMPTY_ANALYSTS = _skeleton("", target=None, ratings=[], upgrades=[])
_EMPTY_EARNINGS = _skeleton("", history=[], upcoming=[])
_EMPTY_INSIDERS = _skeleton(
    "", items=[], net=_skeleton("buy_value sell_value net_value buy_count sell_count")
)
_EMPTY_HOLDINGS = _skeleton(
    "insiders_pct institutions_pct institutions_float_pct institutions_count reported_date",
    funds=[],
)
_EMPTY_CORPORATE = _skeleton("", dividends=[], splits=[])


def _panel(label: str, loader: Callable[[], Any], empty: Any) -> Any:
    """单面板容错：失败只记日志并返回空骨架，不影响其他面板。"""
    try:
        return loader()
    except Exception as exc:  # noqa: BLE001 - 面板级隔离是本模块的设计约定
        logger.warning("[stock-terminal-us] %s 面板失败: %s", label, exc)
        return empty


def _text(value: Any) -> str | None:
    """文本出口：空串与占位符（Unknown）一律 None（前端显示 -- 而不是空白）。"""
    s = str(value or "").strip()
    return None if not s or s == "Unknown" else s


def _f10_row(sym: str) -> Any:
    f10 = _f10_snapshot()
    row = f10[f10["symbol"] == sym] if not f10.empty else f10
    return None if row.empty else row.iloc[0]


def _quote(sym: str) -> tuple[str | None, float, float | None]:
    """最新收盘 (trade_date, close, pct_change)；无报价返回 (None, 0.0, None)。"""
    bars = latest_bars(sym, 2)
    if bars.empty:
        return None, 0.0, None
    last = bars.iloc[-1]
    close = safe_float(last.get("close"))
    trade_date = to_iso(str(last["dt"]))
    pct = None
    if len(bars) >= 2:
        pct = change_pct(close, safe_float(bars.iloc[-2].get("close")))
    return trade_date, close, pct


# ---- 面板 1：概览 ----


def _overview(
    sym: str, trade_date: str | None, close: float, pct: float | None
) -> dict:
    meta = _symbol_meta()
    row = meta[meta["symbol"] == sym]
    m = row.iloc[0] if not row.empty else None
    f = _f10_row(sym)
    market_cap = safe_float(f.get("market_cap")) if f is not None else 0.0
    return {
        "cn_name": (str(m["cn_name"]) if m is not None else None) or None,
        "en_name": _text(m["en_name"]) if m is not None else None,
        "sector": _text(m["sector"]) if m is not None else None,
        "industry": _text(m["industry"]) if m is not None else None,
        "close": round(close, 4) if close else None,
        "pct_change": pct,
        "market_cap": round(market_cap, 2) if market_cap else None,
        "cap_display": cap_display(market_cap),
        "week52_high": num(f.get("52w_high"), 4) if f is not None else None,
        "week52_low": num(f.get("52w_low"), 4) if f is not None else None,
        "avg_volume": int(safe_float(f.get("avg_volume"))) if f is not None else None,
        "trade_date": trade_date,
    }


# ---- 面板 2：估值 ----


def _valuation(sym: str, trade_date: str | None) -> dict:
    f = _f10_row(sym)
    if f is None:
        return dict(_EMPTY_VALUATION)
    market_cap = safe_float(f.get("market_cap"))
    pe = safe_float(f.get("pe_ratio"))
    pb = safe_float(f.get("pb_ratio"))
    # 陈旧快照门槛与市场分析模块同源：僵尸标的（PARA/SBNY 类）PE/PB 会失真
    suspicious = bool(
        (market_cap and market_cap < _MIN_MARKET_CAP)
        or (pe and pe < _PE_MIN)
        or (pb and pb < _PB_MIN)
    )
    return {
        "pe_ratio": num(f.get("pe_ratio")),
        "pb_ratio": num(f.get("pb_ratio"), 4),
        "dividend_yield": num(f.get("dividend_yield")),
        "market_cap": round(market_cap, 2) if market_cap else None,
        "week52_high": num(f.get("52w_high"), 4),
        "week52_low": num(f.get("52w_low"), 4),
        "source": "f10 快照",
        "asof": trade_date,
        "size_tier": next(
            (label for _k, label, floor in _SIZE_TIERS if market_cap >= floor), "小盘"
        ),
        "stale_warning": suspicious,
    }


# ---- 面板 3：财务（年报三表，键值与 periods 等长同序） ----


def _statement_df(sym: str, rel: str) -> pd.DataFrame:
    df = _symbol_table(rel, sym)
    if df.empty or "report_date" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["_d"] = pd.to_datetime(df["report_date"], errors="coerce")
    df = df[df["_d"].notna()]
    df["_period"] = df["_d"].dt.strftime("%Y-%m-%d")
    return df


def _statement_rows(df: pd.DataFrame, labels: tuple, periods: list[str]) -> list[dict]:
    """按 periods 对齐取数：只出现实际存在的列，且至少有一个非空值才成行。"""
    if df.empty:
        return []
    by_period = {r["_period"]: r for _, r in df.iterrows()}
    rows: list[dict[str, Any]] = []
    for col, label in labels:
        if col not in df.columns:
            continue
        values = [
            num(by_period[p].get(col)) if p in by_period else None for p in periods
        ]
        if any(v is not None for v in values):
            rows.append({"key": col, "label": label, "values": values})
    return rows


def _financials(sym: str) -> dict[str, Any]:
    income = _statement_df(sym, f"{FIN_DIR}/income")
    balance = _statement_df(sym, f"{FIN_DIR}/balance")
    cashflow = _statement_df(sym, f"{FIN_DIR}/cashflow")
    all_dates: set[str] = set()
    for df in (income, balance, cashflow):
        if not df.empty:
            all_dates.update(df["_period"].tolist())
    periods = sorted(all_dates, reverse=True)[:_FISCAL_YEARS]
    return {
        "periods": periods,
        "income": _statement_rows(income, INCOME_LABELS, periods),
        "balance": _statement_rows(balance, BALANCE_LABELS, periods),
        "cashflow": _statement_rows(cashflow, CASHFLOW_LABELS, periods),
    }


# ---- 面板 8：分红 / 拆股 ----


def _corporate_actions(sym: str) -> dict[str, Any]:
    dv = _symbol_table(DIVIDEND_DIR, sym)
    dividends: list[dict[str, Any]] = []
    if not dv.empty:
        dv = dv.copy()
        dv["_d"] = pd.to_datetime(dv.get("trade_date"), errors="coerce")
        dv["_v"] = pd.to_numeric(dv.get("dividend"), errors="coerce")
        dv = dv[dv["_d"].notna() & dv["_v"].notna()].sort_values("_d", ascending=False)
        for _, r in dv.head(_DIVIDEND_LIMIT).iterrows():
            dividends.append({"date": str(r["_d"])[:10], "amount": num(r["_v"], 4)})
    return {"dividends": dividends, "splits": _splits(sym)}


# ---- 聚合 ----


def get_detail(symbol: str) -> dict[str, Any] | None:
    """一次聚合 8 个面板；标的不存在返回 None（路由转 404）。"""
    sym = normalize_symbol(symbol)
    if not sym or not _symbol_exists(sym):
        return None
    trade_date, close, pct = _quote(sym)
    return {
        "symbol": sym,
        "name": _name_of(sym),
        "trade_date": trade_date,
        "overview": _panel(
            "overview", lambda: _overview(sym, trade_date, close, pct), _EMPTY_OVERVIEW
        ),
        "valuation": _panel(
            "valuation", lambda: _valuation(sym, trade_date), _EMPTY_VALUATION
        ),
        "financials": _panel("financials", lambda: _financials(sym), _EMPTY_FINANCIALS),
        "analysts": _panel(
            "analysts", lambda: research.get_analysts(sym), _EMPTY_ANALYSTS
        ),
        "earnings": _panel(
            "earnings", lambda: research.get_earnings(sym), _EMPTY_EARNINGS
        ),
        "insiders": _panel(
            "insiders", lambda: holdings.get_insiders(sym), _EMPTY_INSIDERS
        ),
        "holdings": _panel(
            "holdings", lambda: holdings.get_holdings(sym), _EMPTY_HOLDINGS
        ),
        "corporate_actions": _panel(
            "corporate_actions", lambda: _corporate_actions(sym), _EMPTY_CORPORATE
        ),
        "notes": TERMINAL_NOTES,
    }
