"""美股个股终端 —— 标的池：列表 / 搜索 / 个股头部概要。

标的池 = security_master（标普500 + 纳指补充）∩ 行业映射，共 516 只。
其中约 33 只是已退市/被并购的壳标的（ANSS / ATVI / SBNY 等），最新分区没有报价 ——
`/list` 默认**只列最新交易日有行情的约 484 只**（避免「搜出来点进去没有 K 线」），
`include_delisted=true` 时才带出壳标的并标 `has_quote=false` + `delisted=true`。
它们仍可按代码直查 /profile、/detail（报价字段为空，如实呈现）。

排序口径：市值降序（大票在前）—— 与 A 股终端按推理分排序同理，都是「先看最有
信息量的」。市值/PE/PB 来自 f10 快照（美元）。

⚠️ `en_name` 在 security_master 里**全为空串**（516/516 实测），列表/概要的
英文名回退恒为空 —— `/list?q=` 实际只按 symbol + cn_name 命中（见 README 口径表）。
⚠️ 美股目前**没有推理分数**（engine_signal_scores 无 US 行），列表条目里
不提供 fusion / side / position_score 字段，前端按空态处理。
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.display import fmt_yi, safe_float
from backend.services.api.stock_terminal_us.feed.base import (
    ADJUST,
    TERMINAL_NOTES,
    _f10_snapshot,
    _latest_snapshot,
    _symbol_exists,
    _symbol_meta,
    cap_display,
    change_pct,
    latest_bars,
    normalize_symbol,
    to_iso,
)

_NOTES = TERMINAL_NOTES


def list_symbols(
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    include_delisted: bool = False,
) -> dict[str, Any]:
    """标的池列表 / 搜索（q 匹配 symbol / cn_name / en_name，不区分大小写）+ 分页。"""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 600))  # 池子总共约 484 只，允许一次拉全

    trade_date, snap = _latest_snapshot()
    df = _symbol_meta().copy()
    if not snap.empty:
        df = df.merge(snap[["symbol", "close", "pct_change"]], on="symbol", how="left")
    else:
        for col in ("close", "pct_change"):
            df[col] = None

    # 默认池 = 最新分区有报价的标的；include_delisted 时保留壳标的并标 delisted
    df["has_quote"] = df["close"].notna()
    if not include_delisted:
        df = df[df["has_quote"]]

    f10 = _f10_snapshot()
    cols = [c for c in ("market_cap", "pe_ratio", "pb_ratio") if c in f10.columns]
    df = df.merge(f10[["symbol", *cols]], on="symbol", how="left")

    if q and q.strip():
        kw = q.strip().lower()
        hit = (
            df["symbol"].str.lower().str.contains(kw, regex=False)
            | df["cn_name"].str.lower().str.contains(kw, regex=False)
            | df["en_name"].str.lower().str.contains(kw, regex=False)
        )
        df = df[hit]

    df = df.sort_values(
        ["market_cap", "symbol"], ascending=[False, True], na_position="last"
    )
    total = int(len(df))
    pages = max(1, math.ceil(total / page_size))
    chunk = df.iloc[(page - 1) * page_size : page * page_size]

    items = [
        {
            "symbol": r["symbol"],
            "name": r["cn_name"] or r["symbol"],  # 前端搜索框直接读 name
            "cn_name": r["cn_name"],
            "en_name": r["en_name"],
            "display_name": r["cn_name"] or r["en_name"] or r["symbol"],
            "sector": r["sector"],
            "sector_cn": r["sector_cn"],
            "industry": r["industry"],
            "market_cap": round(safe_float(r["market_cap"]), 2),
            "market_cap_yi": fmt_yi(safe_float(r["market_cap"])),  # 亿美元
            "cap_display": cap_display(r["market_cap"]),  # "$4.80万亿"，前端免换算
            "pe_ratio": (
                round(safe_float(r["pe_ratio"]), 2) if pd.notna(r["pe_ratio"]) else None
            ),
            "pb_ratio": (
                round(safe_float(r["pb_ratio"]), 4) if pd.notna(r["pb_ratio"]) else None
            ),
            "close": round(safe_float(r["close"]), 4) if pd.notna(r["close"]) else None,
            "pct_change": (
                round(safe_float(r["pct_change"]), 2)
                if pd.notna(r["pct_change"])
                else None
            ),
            "has_quote": bool(r["has_quote"]),
            "delisted": not bool(r["has_quote"]),
        }
        for _, r in chunk.iterrows()
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "trade_date": to_iso(trade_date) if trade_date else None,
        "adjust": ADJUST,
        "notes": _NOTES,
    }


def get_profile(symbol: str) -> dict[str, Any] | None:
    """个股头部信息：名称 / 行业 / 最新收盘 / 涨跌幅 / 市值 / 52 周高低。

    标的不存在（池外且最新分区无成交）返回 None，由路由转 404。
    退市/壳标的仍返回记录（`has_quote=false` + 报价字段 null），不 404。
    """
    sym = normalize_symbol(symbol)
    if not sym or not _symbol_exists(sym):
        return None

    meta = _symbol_meta()
    mrow = meta[meta["symbol"] == sym]
    m = mrow.iloc[0] if not mrow.empty else None

    bars = latest_bars(sym, 2)
    # 所有键**始终存在**（缺值给 None，不给 undefined）—— 前端 xxx.toFixed 白屏的
    # 根因就是「键时有时无」，停牌/退市标的缺少前收时尤其容易踩
    last: dict[str, Any] = {
        "trade_date": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "prev_close": None,
        "pct_change": None,
        "volume": None,
        "amount": None,
    }
    if not bars.empty:
        r = bars.iloc[-1]
        last.update(
            {
                "trade_date": to_iso(str(r["dt"])),
                "open": round(safe_float(r.get("open")), 4),
                "high": round(safe_float(r.get("high")), 4),
                "low": round(safe_float(r.get("low")), 4),
                "close": round(safe_float(r.get("close")), 4),
                "volume": int(safe_float(r.get("volume"))),
                "amount": round(safe_float(r.get("amount")), 2),
            }
        )
        if len(bars) >= 2:
            prev_close = safe_float(bars.iloc[-2].get("close"))
            last["prev_close"] = round(prev_close, 4)
            last["pct_change"] = change_pct(last["close"], prev_close)

    f10 = _f10_snapshot()
    frow = f10[f10["symbol"] == sym]
    f = frow.iloc[0] if not frow.empty else None

    high_52w = safe_float(f.get("52w_high")) if f is not None else 0.0
    close = safe_float(last.get("close"))
    dist_high = (
        round((close / high_52w - 1) * 100, 2) if high_52w > 0 and close > 0 else None
    )
    market_cap = safe_float(f.get("market_cap")) if f is not None else 0.0

    return {
        "symbol": sym,
        "cn_name": str(m["cn_name"]) if m is not None else sym,
        "en_name": str(m["en_name"]) if m is not None else "",
        "display_name": (str(m["cn_name"]) or sym) if m is not None else sym,
        "sector": str(m["sector"]) if m is not None else "Unknown",
        "sector_cn": str(m["sector_cn"]) if m is not None else "未分类",
        "industry": str(m["industry"]) if m is not None else "",
        "has_quote": bool(last["trade_date"]),
        "trade_date": last.get("trade_date"),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "close": last.get("close"),
        "prev_close": last.get("prev_close"),
        "pct_change": last.get("pct_change"),
        "volume": last.get("volume"),
        "amount": last.get("amount"),
        "market_cap": round(market_cap, 2),
        "market_cap_yi": fmt_yi(market_cap),
        "cap_display": cap_display(market_cap),
        "pe_ratio": round(safe_float(f.get("pe_ratio")), 2) if f is not None else None,
        "pb_ratio": round(safe_float(f.get("pb_ratio")), 4) if f is not None else None,
        "dividend_yield": (
            round(safe_float(f.get("dividend_yield")), 2) if f is not None else None
        ),
        "high_52w": round(high_52w, 4) if high_52w else None,
        "low_52w": (
            round(safe_float(f.get("52w_low")), 4)
            if f is not None and safe_float(f.get("52w_low"))
            else None
        ),
        "dist_from_52w_high_pct": dist_high,
        "adjust": ADJUST,
        "notes": _NOTES,
    }
