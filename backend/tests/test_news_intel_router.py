"""资讯中心 2.0 纯查询逻辑单测（不依赖 FastAPI / DB）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.shared.news_intel_query import (
    SectorIndex,
    aggregate_commodities,
    aggregate_sectors,
    aggregate_topics,
    entity_label,
    market_of,
    read_rule,
    suffix,
    user_key,
)
from backend.shared.news_intel_rank import rank_article


def test_router_prefix_and_auth_dependency():
    pytest.importorskip("fastapi")
    from backend.services.api.routers.news_intel import router

    assert router.prefix == "/api/v1/news-intel"
    assert router.dependencies, "整组路由必须要求登录"


def test_news_read_cache_returns_payload_until_ttl():
    pytest.importorskip("fastapi")
    from backend.services.api.routers import news_intel as ni

    key = ("CN", 1)
    ni._store(ni._OVERVIEW_CACHE, key, {"n_articles": 7})
    assert ni._cached(ni._OVERVIEW_CACHE, key) == {"n_articles": 7}

    ts, payload = ni._OVERVIEW_CACHE[key]
    ni._OVERVIEW_CACHE[key] = (ts - ni._NEWS_CACHE_TTL - 1, payload)
    assert ni._cached(ni._OVERVIEW_CACHE, key) is None


def test_suffix_normalizes_prefix_and_bare_codes():
    assert suffix("SH600519") == "600519.SH"
    assert suffix("600519") == "600519.SH"
    assert suffix("600519.sh") == "600519.SH"
    assert suffix("000001.SZ") == "000001.SZ"


def test_market_of_distinguishes_a_hk_us():
    assert market_of("600519.SH") == "CN"
    assert market_of("000001") == "CN"
    assert market_of("0700.HK") == "HK"
    assert market_of("AAPL") == "US"
    assert market_of("BRK.B") == "US"
    assert market_of("黄金") == "UNKNOWN"


def test_aggregate_topics_by_key_terms():
    rows = [
        {"page_id": i, "key_terms": ["人工智能", "AI"], "tickers": ["AAPL"],
         "event_tags": ["回购"], "sentiment_label": "bullish", "sentiment_score": 0.5}
        for i in range(1, 4)
    ]
    out = {x["name"]: x for x in aggregate_topics(rows)}
    assert out["人工智能"]["n_articles"] == 3
    assert out["人工智能"]["n_positive"] == 3
    assert out["人工智能"]["top_tickers"][0]["code"] == "AAPL"


def test_aggregate_commodities_by_keyword():
    rows = [
        {"page_id": i, "title": "国际金价再创新高", "key_terms": ["黄金"],
         "event_tags": [], "sentiment_label": "bullish", "sentiment_score": 0.6}
        for i in range(1, 4)
    ]
    out = {x["name"]: x for x in aggregate_commodities(rows)}
    assert out["黄金"]["n_articles"] == 3
    assert out["黄金"]["n_positive"] == 3


def test_user_key_defaults_tenant():
    assert user_key({"user_id": 42}) == ("default", "42")


def test_entity_label_uses_ticker_entity_sentiment():
    row = {"entity_sentiments": {"ticker:600519.SH": 0.62}}
    assert entity_label(row, "600519.SH") == "bullish"
    assert entity_label({"entity_sentiments": {"ticker:600519.SH": -0.4}}, "600519.SH") == "bearish"
    assert entity_label({"entity_sentiments": {}}, "600519.SH") is None


def test_sector_index_maps_symbol_to_multiple_sectors():
    frame = pd.DataFrame(
        [
            {"SectorCode": "BK001", "SectorName": "芯片", "SectorType": "概念板块", "Symbol": "600519.SH"},
            {"SectorCode": "BK002", "SectorName": "食品", "SectorType": "行业板块(一级)", "Symbol": "600519.SH"},
        ]
    )
    index = SectorIndex(frame)
    assert index.by_code["BK001"]["type"] == "concept"
    assert index.by_code["BK002"]["type"] == "industry1"
    assert len(index.by_symbol["600519.SH"]) == 2


def test_style_sector_is_flagged_but_theme_is_not():
    frame = pd.DataFrame(
        [
            {"SectorCode": "BK001", "SectorName": "近期新低", "SectorType": "概念板块", "Symbol": "600519.SH"},
            {"SectorCode": "BK002", "SectorName": "芯片", "SectorType": "概念板块", "Symbol": "600519.SH"},
            {"SectorCode": "BK003", "SectorName": "MSCI成份", "SectorType": "概念板块", "Symbol": "600519.SH"},
        ]
    )
    index = SectorIndex(frame)
    assert index.by_code["BK001"]["style"] is True
    assert index.by_code["BK002"]["style"] is False
    assert index.by_code["BK003"]["style"] is True


def test_sector_heat_prefers_title_match_over_membership():
    frame = pd.DataFrame(
        [
            {"SectorCode": "BK001", "SectorName": "人工智能", "SectorType": "概念板块", "Symbol": "600519.SH"},
            {"SectorCode": "BK002", "SectorName": "芯片", "SectorType": "概念板块", "Symbol": "600519.SH"},
        ]
    )
    index = SectorIndex(frame)
    rows = [
        {
            "page_id": 1,
            "title": "人工智能行业迎来政策利好",
            "tickers": ["600519.SH"],
            "event_tags": ["回购"],
            "sentiment_label": "bullish",
            "sentiment_score": 0.5,
        }
    ]
    out = {x["name"]: x for x in aggregate_sectors(rows, index)}
    assert "人工智能" in out
    assert "芯片" not in out


def test_aggregate_sectors_counts_risk_and_heat():
    frame = pd.DataFrame(
        [
            {"SectorCode": "BK001", "SectorName": "芯片", "SectorType": "概念板块", "Symbol": "600519.SH"},
            {"SectorCode": "BK002", "SectorName": "食品", "SectorType": "行业板块(一级)", "Symbol": "600519.SH"},
        ]
    )
    index = SectorIndex(frame)
    rows = [
        {
            "page_id": 1,
            "tickers": ["600519.SH"],
            "event_tags": ["监管处罚"],
            "sentiment_label": "bearish",
            "sentiment_score": -0.6,
        },
        {
            "page_id": 2,
            "tickers": ["600519.SH"],
            "event_tags": ["回购"],
            "sentiment_label": "bullish",
            "sentiment_score": 0.5,
        },
    ]
    out = {x["code"]: x for x in aggregate_sectors(rows, index)}
    assert out["BK001"]["n_articles"] == 2
    assert out["BK001"]["n_negative"] == 1
    assert out["BK001"]["n_positive"] == 1
    assert out["BK001"]["n_risk"] == 1
    assert out["BK001"]["risk_ratio"] == 0.5


def _p0_rank():
    return rank_article(
        title="600519.SH 收到监管警示函",
        code="600519.SH",
        name="贵州茅台",
        event_tags=["监管处罚"],
        source="新浪财经",
        published_at=datetime.now(timezone.utc),
        pre5=0.07,
    )


def test_read_rule_p0_is_explicit_and_has_no_buy_words():
    text = read_rule([_p0_rank()], 0.07)
    assert "硬利空" in text
    assert "回避" in text
    for banned in ("买入", "卖出", "减仓"):
        assert banned not in text


def test_read_rule_positive_high_pre5_is_no_chase():
    rank = rank_article(
        title="600519.SH 回购公告",
        code="600519.SH",
        name="贵州茅台",
        event_tags=["回购"],
        source="新浪财经",
        published_at=datetime.now(timezone.utc),
        pre5=0.06,
    )
    text = read_rule([rank], 0.06)
    assert "不追" in text
    assert "买入" not in text
