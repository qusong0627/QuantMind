"""新闻情报分级（T-P6-12）——实体/事件分级/时效的**纯函数**实现（金样可测，无 IO）。

输入 = ``news_article_enrichment`` 行（enricher 已算好 tickers/industries/event_tags/
sentiment_score/sentiment_label/enriched_at，见 services/api/news/enricher.py）。
输出 = 总线事件要素（level/kind/targets）。分级优先级（金样锁定）：

1. **风险事件（critical）**：命中 ``RISK_TAGS``（源自 finance_lexicon 的 75 个 event_tag
   中的监管/财务/暴雷类）——不管情绪分正负（风险优先）；
2. **利空（warn）**：命中 ``CAUTION_TAGS``，或 sentiment=bearish 且 |score| ≥ 阈值；
3. **利好（info）**：命中 ``POSITIVE_TAGS``，或 sentiment=bullish 且 |score| ≥ 阈值；
4. 其余不出事件（中性不推总线，防噪声）。

时效：只接 ``enriched_at`` 在 ``max_age_min`` 内的行（分钟级接入；陈旧文章不触发 veto）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# 风险事件标签（critical；来源：finance_lexicon event_tag 监管/财务/暴雷类）
RISK_TAGS: frozenset[str] = frozenset({
    "财务造假", "立案调查", "内幕交易", "操纵市场", "处罚", "监管函", "警示函",
    "业绩暴雷", "业绩亏损", "净利润下滑", "业绩预减", "业绩不及预期", "债务",
})
# 谨慎信号（warn）
CAUTION_TAGS: frozenset[str] = frozenset({"减持", "解禁", "减值", "破发", "大跌"})
# 利好信号（info）
POSITIVE_TAGS: frozenset[str] = frozenset({
    "中标", "分红", "回购", "增持", "业绩预增", "净利润增长", "扭亏为盈",
    "涨停", "大涨", "创新高", "战略合作", "并购", "订单", "股权激励",
})

LEVEL_CRITICAL = "critical"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"

KIND_RISK_EVENT = "risk_event"
KIND_NEGATIVE = "negative"
KIND_POSITIVE = "positive"
KIND_SENTIMENT_SPIKE = "sentiment_spike"

_MAX_TARGETS = 64


@dataclass(frozen=True)
class NewsVerdict:
    """单篇文章的分级结果。"""

    level: str
    kind: str
    targets: tuple[str, ...]
    event_tags: tuple[str, ...]
    sentiment_score: float
    sentiment_label: str


def normalize_targets(tickers: Iterable[Any], *, limit: int = _MAX_TARGETS) -> tuple[str, ...]:
    """允许的标的口径：CN 后缀式（600000.SH / 8xxxxx.BJ）/ 港股（00700.HK）/ 美股纯字母；
    其余（债券 IB、指数点、空串）剔除。保持序、去重、截断。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tickers or []:
        text = str(raw or "").strip().upper()
        if not text or text in seen:
            continue
        ok = False
        if len(text) == 9 and text[6] == "." and text[:6].isdigit() and text[7:] in {"SH", "SZ", "BJ"}:
            ok = True
        elif text.endswith(".HK") and len(text) == 8 and text[:5].isdigit():
            ok = True
        elif text.isalpha() and 1 <= len(text) <= 6:
            ok = True
        if ok:
            seen.add(text)
            out.append(text)
        if len(out) >= max(1, int(limit)):
            break
    return tuple(out)


def is_fresh(enriched_at: Any, *, now: datetime, max_age_min: float) -> bool:
    """enriched_at（datetime 或 ISO 串）在 max_age_min 内；无法判定 → False（不接）。"""
    ts: datetime | None = None
    if isinstance(enriched_at, datetime):
        ts = enriched_at
    elif enriched_at:
        text = str(enriched_at).strip()
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            # 容忍 "+08"（无冒号）等非严格 ISO 偏移（PG ::text 形态）
            import re as _re

            fixed = _re.sub(r"([+-]\d{2})$", r"\1:00", text)
            try:
                ts = datetime.fromisoformat(fixed)
            except ValueError:
                ts = None
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (now - ts).total_seconds() / 60.0
    return 0 <= age_min <= max(0.0, float(max_age_min))


def classify_news(
    row: Mapping[str, Any],
    *,
    min_abs_score: float = 0.6,
) -> NewsVerdict | None:
    """单篇文章 → 分级（None=不推）。row 为 enrichment 行（dict）。"""
    tags = tuple(str(t) for t in (row.get("event_tags") or []) if str(t).strip())
    tagset = set(tags)
    try:
        score = float(row.get("sentiment_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    label = str(row.get("sentiment_label") or "").strip().lower()
    targets = normalize_targets(row.get("tickers") or [])
    if not targets:
        return None  # 无实体链接 → 不可行动，不推

    if tagset & RISK_TAGS:
        return NewsVerdict(LEVEL_CRITICAL, KIND_RISK_EVENT, targets, tags, score, label)
    if tagset & CAUTION_TAGS or (label == "bearish" and abs(score) >= min_abs_score):
        return NewsVerdict(LEVEL_WARN, KIND_NEGATIVE, targets, tags, score, label)
    if tagset & POSITIVE_TAGS or (label == "bullish" and abs(score) >= min_abs_score):
        return NewsVerdict(LEVEL_INFO, KIND_POSITIVE, targets, tags, score, label)
    return None


def actions_for(verdict: NewsVerdict) -> tuple[str, ...]:
    if verdict.kind == KIND_RISK_EVENT:
        return ("risk_review",)
    if verdict.kind == KIND_NEGATIVE:
        return ("risk_watch",)
    return ("heat_watch",)


def target_market(symbol: str) -> str:
    """标的 → 市场段（intel 总线 market 枚举）：CN 后缀式 / HK / 美股纯字母。"""
    text = str(symbol or "").strip().upper()
    if len(text) == 9 and text[6] == "." and text[:6].isdigit() and text[7:] in {"SH", "SZ", "BJ"}:
        return "CN"
    if text.endswith(".HK"):
        return "HK"
    return "US"


def split_targets_by_market(targets: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """按市场分段（WS 主题 intel.{tenant}.market.{MKT} 按市场路由——混market事件会串频道）。"""
    buckets: dict[str, list[str]] = {}
    for symbol in targets:
        buckets.setdefault(target_market(symbol), []).append(str(symbol))
    return {market: tuple(symbols) for market, symbols in buckets.items()}


def build_news_events(
    row: Mapping[str, Any],
    verdict: NewsVerdict,
    *,
    ts: float,
    source: str = "news_intel",
    max_title: int = 200,
) -> list[dict[str, Any]]:
    """按市场拆分后的 intel 事件列表（每市场一条；market 字段如实）。"""
    events: list[dict[str, Any]] = []
    for market, targets in split_targets_by_market(verdict.targets).items():
        base = build_news_event(row, verdict, ts=ts, source=source, max_title=max_title)
        base["market"] = market
        base["targets"] = list(targets)[:64]
        events.append(base)
    return events


def build_news_event(
    row: Mapping[str, Any],
    verdict: NewsVerdict,
    *,
    ts: float,
    source: str = "news_intel",
    max_title: int = 200,
) -> dict[str, Any]:
    """enrichment 行 + 分级 → intel_events schema 事件（未知字段拒收，故只放 schema 内字段）。"""
    return {
        "ts": float(ts),
        "type": "news",
        "market": "CN",
        "targets": list(verdict.targets),
        "level": verdict.level,
        "payload": {
            "kind": verdict.kind,
            "title": str(row.get("title") or "")[:max_title],
            "sentiment_score": round(float(verdict.sentiment_score), 4),
            "sentiment_label": verdict.sentiment_label,
            "event_tags": list(verdict.event_tags)[:12],
            "industries": [str(i) for i in (row.get("industries") or [])][:8],
            "page_id": row.get("huntly_page_id"),
            "title_hash": row.get("title_hash"),
        },
        "actions_hint": list(actions_for(verdict))[:8],
        "source": source,
    }


def batch_cursor_id(rows: Sequence[Mapping[str, Any]]) -> str:
    """批内游标（取最大 enriched_at ISO 串 + page_id 兜底；空批返回空串）。"""
    best_iso = ""
    best_page = -1
    for row in rows:
        iso = str(row.get("enriched_at") or "")
        page = int(row.get("huntly_page_id") or 0)
        if (iso, page) > (best_iso, best_page):
            best_iso, best_page = iso, page
    return best_iso if best_iso else (str(best_page) if best_page >= 0 else "")
