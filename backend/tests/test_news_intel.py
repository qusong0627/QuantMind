"""新闻情报分级与 veto 纯函数测试（T-P6-12）：分级金样 + 实体/时效/schema 契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit
_CST = timezone(timedelta(hours=8))


def _row(**kw):
    base = {
        "huntly_page_id": 1,
        "tickers": ["600036.SH"],
        "industries": ["银行"],
        "event_tags": [],
        "sentiment_score": 0.0,
        "sentiment_label": "neutral",
        "title": "招商银行公告",
        "title_hash": 123,
        "enriched_at": datetime.now(_CST).isoformat(),
    }
    base.update(kw)
    return base


# ── 分级金样 ────────────────────────────────────────────────────────


def test_classify_risk_tag_wins_over_sentiment():
    from backend.services.engine.news_intel import classify_news

    # 风险标签优先（即便情绪分正）
    v = classify_news(_row(event_tags=["财务造假"], sentiment_score=0.8, sentiment_label="bullish"))
    assert v is not None and v.level == "critical" and v.kind == "risk_event"
    v2 = classify_news(_row(event_tags=["立案调查"]))
    assert v2 is not None and v2.level == "critical"


def test_classify_caution_and_bearish():
    from backend.services.engine.news_intel import classify_news

    v = classify_news(_row(event_tags=["减持"]))
    assert v is not None and v.level == "warn" and v.kind == "negative"
    v2 = classify_news(_row(sentiment_score=-0.75, sentiment_label="bearish"), min_abs_score=0.6)
    assert v2 is not None and v2.level == "warn"
    # 情绪分不足阈且无标签 → 不推
    assert classify_news(_row(sentiment_score=-0.3, sentiment_label="bearish")) is None


def test_classify_positive_and_neutral_skips():
    from backend.services.engine.news_intel import classify_news

    v = classify_news(_row(event_tags=["中标"]))
    assert v is not None and v.level == "info" and v.kind == "positive"
    v2 = classify_news(_row(sentiment_score=0.8, sentiment_label="bullish"))
    assert v2 is not None and v2.level == "info"
    # 中性 / 无实体 → None（不推总线）
    assert classify_news(_row()) is None
    assert classify_news(_row(sentiment_score=0.9, sentiment_label="bullish", tickers=[])) is None


def test_normalize_targets_filters_and_dedupes():
    from backend.services.engine.news_intel import normalize_targets

    out = normalize_targets([
        "600036.SH", "600036.SH", "00700.HK", "AAPL", "232380082.IB", "", "600036", "AB.CD",
    ])
    assert out == ("600036.SH", "00700.HK", "AAPL")


def test_normalize_targets_caps_at_64():
    from backend.services.engine.news_intel import normalize_targets

    out = normalize_targets([f"{600000 + i}.SH" for i in range(100)])
    assert len(out) == 64


def test_is_fresh_window_and_bad_input():
    from backend.services.engine.news_intel import is_fresh

    now = datetime.now(timezone.utc)
    assert is_fresh((now - timedelta(minutes=30)).isoformat(), now=now, max_age_min=120)
    assert not is_fresh((now - timedelta(minutes=180)).isoformat(), now=now, max_age_min=120)
    assert not is_fresh(None, now=now, max_age_min=120)
    assert not is_fresh("not-a-date", now=now, max_age_min=120)
    # naive datetime 视为 UTC
    assert is_fresh((now - timedelta(minutes=10)).replace(tzinfo=None), now=now, max_age_min=120)


def test_build_news_event_passes_intel_schema():
    """事件必须过总线 schema 校验（未知字段拒收——防未来改字段悄悄破契约）。"""
    from backend.services.engine.news_intel import build_news_event, classify_news
    from backend.shared.intel_events import validate_event

    row = _row(event_tags=["处罚"], sentiment_score=-0.9, sentiment_label="bearish",
               title="某公司被处罚" * 40)
    verdict = classify_news(row)
    assert verdict is not None
    event = build_news_event(row, verdict, ts=1789600000.0)
    validated = validate_event(event)
    assert validated["type"] == "news" and validated["level"] == "critical"
    assert validated["targets"] == ["600036.SH"]
    assert validated["source"] == "news_intel"
    assert len(str(validated["payload"]["title"])) <= 200


# ── veto 纯函数 ─────────────────────────────────────────────────────


def test_news_veto_config_flag_shapes():
    from backend.services.live_trading.services.news_veto import _config_flag

    assert _config_flag({"risk": {"veto": {"news_event": True}}}) is True
    assert _config_flag({"risk": {"veto": {"news_event": "true"}}}) is True
    assert _config_flag({"risk": {"veto_news_event": "on"}}) is True
    assert _config_flag({"veto_news_event": 1}) is True
    assert _config_flag({"risk": {"veto": {"news_event": False}}}) is False  # 显式关闭
    assert _config_flag({}) is False
    assert _config_flag(None) is False


def test_filter_news_veto_buys_only_blocks_buys():
    from backend.services.live_trading.services.news_veto import filter_news_veto_buys

    class _O:
        def __init__(self, side, symbol):
            self.side, self.symbol = side, symbol

    orders = [_O("BUY", "600036.SH"), _O("SELL", "600036.SH"), _O("BUY", "000001.SZ")]
    kept, dropped = filter_news_veto_buys(orders, {"600036.SH"})
    assert [o.symbol for o in dropped] == ["600036.SH"]
    assert len(kept) == 2
    # 前缀/后缀形态归一（标记为后缀式，订单可能带前缀式）
    kept2, dropped2 = filter_news_veto_buys([_O("BUY", "SH600036")], {"600036.SH"})
    assert not kept2 and len(dropped2) == 1


def test_news_intel_config_parsing():
    from backend.services.engine.news_intel_engine import NewsIntelConfig

    cfg = NewsIntelConfig.from_mapping({
        "enabled": "true", "cadence_s": "30", "max_age_min": "60",
        "min_abs_score": "0.7", "veto_enabled": "false", "spike_min_articles": "3",
    })
    assert cfg.enabled and cfg.cadence_s == 30.0 and cfg.max_age_min == 60.0
    assert cfg.min_abs_score == 0.7 and cfg.veto_enabled is False and cfg.spike_min_articles == 3
    assert NewsIntelConfig.from_mapping(None).enabled is False


def test_target_market_and_split():
    from backend.services.engine.news_intel import split_targets_by_market, target_market

    assert target_market("600036.SH") == "CN" and target_market("000001.SZ") == "CN"
    assert target_market("830799.BJ") == "CN"
    assert target_market("00700.HK") == "HK"
    assert target_market("AAPL") == "US"
    buckets = split_targets_by_market(("600036.SH", "AAPL", "00700.HK", "000001.SZ"))
    assert buckets == {"CN": ("600036.SH", "000001.SZ"), "US": ("AAPL",), "HK": ("00700.HK",)}


def test_build_news_events_per_market_with_schema():
    from backend.services.engine.news_intel import build_news_events, classify_news
    from backend.shared.intel_events import validate_event

    row = _row(event_tags=["中标"], tickers=["600036.SH", "AAPL"], sentiment_score=0.9,
               sentiment_label="bullish")
    verdict = classify_news(row)
    assert verdict is not None
    events = build_news_events(row, verdict, ts=1789600000.0)
    markets = sorted(e["market"] for e in events)
    assert markets == ["CN", "US"]
    for event in events:
        validated = validate_event(event)
        assert validated["market"] in {"CN", "US"}
    cn = next(e for e in events if e["market"] == "CN")
    assert cn["targets"] == ["600036.SH"]
