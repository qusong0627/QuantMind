"""快速通道纯逻辑：文章 → 聚合计数 → NewsDecision（无 IO）。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.shared.news_decision import NewsDecision, decide
from backend.shared.news_event_rules import (
    classify_event,
    is_speculative,
    source_quality,
    tier_for,
    validate_title,
)

CN_TZ = timezone(timedelta(hours=8))


def update_agg(
    agg: Mapping[str, Any],
    ticker: str,
    source: str,
    *,
    speculative: bool,
) -> dict[str, Any]:
    """不可变更新：返回新的 day_agg[ticker]。"""
    cur = dict(agg or {})
    sources = dict(cur.get("sources") or {})
    sources[source] = int(sources.get(source, 0)) + 1
    return {
        "n_articles": int(cur.get("n_articles", 0)) + 1,
        "n_spec": int(cur.get("n_spec", 0)) + (1 if speculative else 0),
        "sources": sources,
        "first_seen": cur.get("first_seen") or "",
    }


def _source_hhi(sources: Mapping[str, int], total: int) -> float:
    if total <= 0:
        return 1.0
    return sum((int(v) / total) ** 2 for v in sources.values())


def _expire_at(trade_date: str) -> str:
    day = datetime.strptime(str(trade_date)[:8], "%Y%m%d").replace(tzinfo=CN_TZ)
    return (day + timedelta(days=1)).replace(hour=9, minute=30).isoformat(
        timespec="seconds"
    )


def build_fast_event(
    *,
    article: Mapping[str, Any],
    ticker: str,
    name: str,
    pre5: float | None,
    agg: Mapping[str, Any],
    trade_date: str,
    now: str | None = None,
) -> NewsDecision | None:
    """单篇文章 × 单票 → 决策；L3/L4、验证失败、中性 → None。"""
    title = str(article.get("title") or "")
    enr = article.get("enrichment") or {}
    tags = enr.get("event_tags") or []
    sentiment = str(enr.get("sentiment_label") or "")
    category, direction, event_type = classify_event(title, tags, sentiment)
    if category in ("中性",):
        return None
    tier = tier_for(title, ticker, name)
    if tier not in ("L1", "L2"):
        return None
    if not validate_title(title, ticker, name, category):
        return None
    source = str(article.get("source_name") or "")
    spec = is_speculative(title, tags)
    n_articles = int(agg.get("n_articles", 0)) + 1
    sources = dict(agg.get("sources") or {})
    sources[source] = int(sources.get(source, 0)) + 1
    n_spec = int(agg.get("n_spec", 0)) + (1 if spec else 0)
    published = str(article.get("published_at") or now or datetime.now(CN_TZ).isoformat())
    event = {
        "decision_id": f"{trade_date}-{ticker}-{article.get('id')}",
        "code": ticker,
        "name": name,
        "as_of": now or datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "first_seen": published,
        "expire_at": _expire_at(trade_date),
        "tier": tier,
        "event_type": event_type,
        "category": category,
        "direction": direction,
        "pre5": pre5,
        "session": "",
        "cluster_size": n_articles,
        "layer": int(agg.get("n_articles", 0)),
        "echo_count": max(0, n_articles - 1),
        "source": source,
        "source_tier": "",
        "source_quality": source_quality(source),
        "n_speculative": n_spec,
        "n_articles": n_articles,
        "n_sources": len(sources),
        "source_hhi": _source_hhi(sources, n_articles),
        "stale": False,
    }
    return decide(event)
