"""快速通道纯逻辑单测。"""

from __future__ import annotations

from backend.shared.news_fast_core import build_fast_event, update_agg


def _article(title: str, tags=(), sentiment="neutral"):
    return {
        "id": 138001,
        "title": title,
        "published_at": "2026-11-18T10:02:13+08:00",
        "source_name": "财联社",
        "enrichment": {"event_tags": list(tags), "sentiment_label": sentiment},
    }


def test_hard_risk_high_pre5_is_p0():
    d = build_fast_event(
        article=_article("贵州茅台被证监会立案调查", ["立案调查"]),
        ticker="600519.SH",
        name="贵州茅台",
        pre5=0.07,
        agg={},
        trade_date="20261118",
        now="2026-11-18T10:03:00+08:00",
    )
    assert d is not None
    assert d.decision.value == "P0"
    assert d.layer == 0
    assert d.n_articles == 1


def test_second_article_gets_echo_layer():
    agg = update_agg({}, "600519.SH", "财联社", speculative=False)
    d = build_fast_event(
        article=_article("贵州茅台被立案调查", ["立案调查"]),
        ticker="600519.SH",
        name="贵州茅台",
        pre5=0.07,
        agg=agg,
        trade_date="20261118",
        now="2026-11-18T10:03:00+08:00",
    )
    assert d is not None
    assert d.layer == 1
    assert d.echo_count == 1


def test_no_company_in_title_is_skipped():
    d = build_fast_event(
        article=_article("白酒板块震荡"),
        ticker="600519.SH",
        name="贵州茅台",
        pre5=0.07,
        agg={},
        trade_date="20261118",
    )
    assert d is None


def test_positive_high_pre5_is_p2():
    d = build_fast_event(
        article=_article("贵州茅台拟回购股份", ["回购"]),
        ticker="600519.SH",
        name="贵州茅台",
        pre5=0.06,
        agg={},
        trade_date="20261118",
    )
    assert d is not None
    assert d.decision.value == "P2"


def test_update_agg_counts_sources_and_spec():
    a = update_agg({}, "600519.SH", "财联社", speculative=True)
    b = update_agg(a, "600519.SH", "新浪财经", speculative=False)
    assert b["n_articles"] == 2
    assert b["n_spec"] == 1
    assert b["sources"] == {"财联社": 1, "新浪财经": 1}
    assert a["n_articles"] == 1  # immutable
