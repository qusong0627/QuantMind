"""美股分析师动向 —— 评级升降级 / 目标价隐含空间 / 评级分布。

数据源 `4_analyst/`：
- `upgrades_downgrades`  约 17 万条事件（每股约 377 条），含机构名、评级前后值、
  目标价前后值 —— 美股最有信息量的一块分析师数据
- `analyst_price_targets` 每股 1 行的目标价快照（均值/中位/高低）
- `recommendations`      月度滚动评级分布（strongBuy/buy/hold/sell/strongSell）

隐含空间 = 目标价均值 / 最新收盘 - 1。收盘价走日线截面快照，
**目标价快照与收盘价可能不是同一天**，面板需标注两者日期。
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
    _market_pct_snapshot,
    _names,
)

_ANALYST_TTL = 1800.0

# 目标价隐含空间的合理上限（%）：超过视为退市/并购残留造成的失真，剔除不上榜
_UPSIDE_CAP = 200.0

# 同一天同一机构可能重复报送（不同分析师），按 (symbol, date, firm) 取最新一条
_UPGRADE_KEY = ["symbol", "GradeDate", "Firm"]


def get_analyst_upgrades(days: int = 30, limit: int = 40) -> dict[str, Any]:
    """近期评级升降级流水（含目标价调整）。"""
    days = max(1, min(int(days), 180))
    limit = max(5, min(int(limit), 100))

    def _load() -> dict[str, Any]:
        empty = {"as_of": "", "days": days, "total": 0, "items": []}
        if not _avail():
            return empty
        df = _load_analyst_table("upgrades_downgrades")
        if df.empty:
            return empty
        df = df.copy()
        df["grade_dt"] = pd.to_datetime(df["GradeDate"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[df["grade_dt"].notna()]
        df = df[df["grade_dt"] >= today - timedelta(days=days)]
        if df.empty:
            return empty
        df = df.sort_values("grade_dt", ascending=False).drop_duplicates(
            _UPGRADE_KEY, keep="first"
        )
        df = df.head(limit)
        names = _names(df["symbol"].tolist())
        items = []
        for _, r in df.iterrows():
            prior = safe_float(r.get("priorPriceTarget"), None)
            cur = safe_float(r.get("currentPriceTarget"), None)
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "grade_date": r["grade_dt"].strftime("%Y-%m-%d"),
                    "firm": str(r.get("Firm") or ""),
                    "action": str(r.get("Action") or ""),
                    "from_grade": str(r.get("FromGrade") or ""),
                    "to_grade": str(r.get("ToGrade") or ""),
                    "price_target_action": str(r.get("priceTargetAction") or ""),
                    "current_price_target": cur,
                    "prior_price_target": prior,
                    "price_target_change_pct": (
                        round((cur / prior - 1) * 100, 2)
                        if cur is not None and prior not in (None, 0)
                        else None
                    ),
                    # 评级方向：靠 ToGrade/FromGrade 的关键词粗判，供前端着色
                    "direction": _grade_direction(
                        r.get("FromGrade"), r.get("ToGrade"), r.get("Action")
                    ),
                }
            )
        return {
            "as_of": today.strftime("%Y-%m-%d"),
            "days": days,
            "total": len(items),
            "items": items,
        }

    return cached(f"us_analyst_upgrades:{days}:{limit}", _load, ttl=_ANALYST_TTL)


# 评级词汇 → 档位，**按顺序匹配（先具体后宽泛）**。
# 各家机构自定档位词，这里归一到标准四档：
#   3 = 看多（Buy / Outperform / Overweight / Accumulate）
#   2 = 中性（Hold / Neutral / Market Perform / Equal Weight）
#   1 = 看空（Underperform / Underweight / Reduce）
#   0 = 强看空（Sell / Strong Sell）
# 注意 "market perform" 必须用完整短语 —— 单独用 "perform" 会误命中 "outperform"。
_GRADE_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("strong sell", 0),
    ("strong buy", 3),
    ("sell", 0),
    ("buy", 3),
    ("outperform", 3),
    ("underperform", 1),
    ("overweight", 3),
    ("underweight", 1),
    ("accumulate", 3),
    ("positive", 3),
    ("negative", 1),
    ("reduce", 1),
    ("market perform", 2),
    ("equal weight", 2),
    ("hold", 2),
    ("neutral", 2),
)


def _grade_direction(from_grade: Any, to_grade: Any, action: Any) -> str:
    """由评级变化与动作粗判方向：up / down / neutral。

    yahoo 的 ToGrade/FromGrade 是各家机构自定的档位词（Overweight、
    Outperform、Sector Weight...），没有统一枚举，因此按关键词匹配；
    匹配不到时回落到 Action 字段（up/down/main/init/reit）。
    """
    to_s = str(to_grade or "").lower()
    fr_s = str(from_grade or "").lower()
    act = str(action or "").lower()
    to_rank = _grade_rank(to_s)
    fr_rank = _grade_rank(fr_s)
    if to_rank is not None and fr_rank is not None and to_rank != fr_rank:
        return "up" if to_rank > fr_rank else "down"
    if fr_rank is None and to_rank is not None:
        # 首次覆盖（FromGrade 为空）：直接给出档位方向，不当作中性
        if to_rank >= 3:
            return "up"
        if to_rank <= 1:
            return "down"
    if act in ("up", "upgrade"):
        return "up"
    if act in ("down", "downgrade"):
        return "down"
    return "neutral"


def _grade_rank(text: str) -> int | None:
    """把评级词映射到 0-3 档（越大越看多）；识别不出返回 None。"""
    if not text:
        return None
    for keyword, rank in _GRADE_KEYWORDS:
        if keyword in text:
            return rank
    return None


def get_analyst_targets(limit: int = 30) -> dict[str, Any]:
    """目标价隐含空间榜：目标价均值相对最新收盘的上行/下行空间。

    收盘价来自日线截面（date 见 `close_date`），目标价来自快照（无日期字段），
    两者时间口径不一致时前端需以收盘日为准标注。

    离群值防护：退市/并购残留标的会让「目标价 / 现价」失真到几十倍
    （实测 PARA 现价 0.98、目标价均值 42 → +4186%），因此隐含空间超过
    `_UPSIDE_CAP` 的记录一律剔除，不上榜。
    """
    limit = max(5, min(int(limit), 60))

    def _load() -> dict[str, Any]:
        empty = {"close_date": "", "items": []}
        if not _avail():
            return empty
        pt = _load_analyst_table("analyst_price_targets")
        if pt.empty:
            return empty
        close_date, snap = _market_pct_snapshot()
        if snap.empty:
            return empty
        close = snap[["symbol", "close"]]
        df = pt.merge(close, on="symbol", how="inner")
        df["mean"] = pd.to_numeric(df["mean"], errors="coerce")
        df = df[(df["mean"].notna()) & (df["close"].fillna(0) > 0)]
        if df.empty:
            return empty
        df["upside_pct"] = (df["mean"] / df["close"] - 1) * 100
        df = df[df["upside_pct"].between(-_UPSIDE_CAP, _UPSIDE_CAP)]
        if df.empty:
            return empty
        df = df.sort_values("upside_pct", ascending=False).head(limit)
        names = _names(df["symbol"].tolist())
        items = []
        for _, r in df.iterrows():
            items.append(
                {
                    "symbol": r["symbol"],
                    "name": names.get(r["symbol"], r["symbol"]),
                    "close": round(safe_float(r["close"]), 2),
                    "target_mean": round(safe_float(r["mean"]), 2),
                    "target_high": _opt(r.get("high")),
                    "target_low": _opt(r.get("low")),
                    "upside_pct": round(safe_float(r["upside_pct"]), 2),
                }
            )
        from backend.services.api.market_analysis_shared.market_days import to_iso

        return {
            "close_date": to_iso(close_date) if close_date else "",
            "items": items,
        }

    return cached(f"us_analyst_targets:{limit}", _load, ttl=_ANALYST_TTL)


def get_analyst_ratings() -> dict[str, Any]:
    """全市场评级分布（取最新一期 `0m`）：强买/买入/持有/卖出/强卖汇总。"""

    def _load() -> dict[str, Any]:
        empty = {
            "as_of": "",
            "total_coverage": 0,
            "strong_buy": 0,
            "buy": 0,
            "hold": 0,
            "sell": 0,
            "strong_sell": 0,
            "bull_ratio": 0.0,
            "top_rated": [],
            "bottom_rated": [],
        }
        if not _avail():
            return empty
        rec = _load_analyst_table("recommendations")
        if rec.empty:
            return empty
        df = rec.copy()
        df["period"] = df["period"].astype(str)
        cur = df[df["period"] == "0m"].copy()
        if cur.empty:
            return empty
        for c in ("strongBuy", "buy", "hold", "sell", "strongSell"):
            cur[c] = pd.to_numeric(cur[c], errors="coerce").fillna(0)
        cur["total"] = cur[["strongBuy", "buy", "hold", "sell", "strongSell"]].sum(axis=1)
        cur = cur[cur["total"] > 0]
        if cur.empty:
            return empty
        sums = {
            "strong_buy": int(cur["strongBuy"].sum()),
            "buy": int(cur["buy"].sum()),
            "hold": int(cur["hold"].sum()),
            "sell": int(cur["sell"].sum()),
            "strong_sell": int(cur["strongSell"].sum()),
        }
        total = sum(sums.values())
        bull = sums["strong_buy"] + sums["buy"]
        cur["bull_ratio"] = (cur["strongBuy"] + cur["buy"]) / cur["total"] * 100
        cur = cur.sort_values("bull_ratio", ascending=False)
        names = _names(cur["symbol"].tolist())

        def _rows(sub: pd.DataFrame) -> list[dict[str, Any]]:
            out = []
            for _, r in sub.iterrows():
                out.append(
                    {
                        "symbol": r["symbol"],
                        "name": names.get(r["symbol"], r["symbol"]),
                        "bull_ratio": round(safe_float(r["bull_ratio"]), 1),
                        "total": int(r["total"]),
                        "strong_buy": int(r["strongBuy"]),
                        "buy": int(r["buy"]),
                        "hold": int(r["hold"]),
                        "sell": int(r["sell"]),
                        "strong_sell": int(r["strongSell"]),
                    }
                )
            return out

        return {
            "as_of": date.today().strftime("%Y-%m-%d"),
            "total_coverage": total,
            **sums,
            "bull_ratio": round(bull / total * 100, 1) if total else 0.0,
            "top_rated": _rows(cur.head(15)),
            "bottom_rated": _rows(cur.tail(15).iloc[::-1]),
        }

    return cached("us_analyst_ratings", _load, ttl=_ANALYST_TTL)


def _opt(value: Any) -> float | None:
    """可选数值：空值返回 None。"""
    out = safe_float(value, None)
    return round(out, 2) if out is not None else None
