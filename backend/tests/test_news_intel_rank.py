"""资讯中心 2.0 权重模型单测（TDD：先固定研究口径，再写界面）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.shared.news_event_rules import classify_event
from backend.shared.news_intel_rank import (
    assign_echo_layers,
    compute_weight,
    importance_for,
    is_aggregator_domain,
    rank_article,
    rank_sector_article,
)


def _rank(**overrides):
    base = {
        "title": "600519.SH 贵州茅台收到监管警示函",
        "code": "600519.SH",
        "name": "贵州茅台",
        "event_tags": ["监管处罚"],
        "sentiment_label": "bearish",
        "source": "新浪财经",
        "source_domain": "finance.sina.com.cn",
        "published_at": datetime.now(timezone.utc),
        "pre5": 0.07,
    }
    base.update(overrides)
    return rank_article(**base)


def test_hard_risk_high_pre5_is_p0_and_top_weight():
    hit = _rank()
    assert hit.decision == "P0"
    assert hit.category == "硬利空"
    assert hit.risk_level == "critical"
    assert hit.primary_label == "硬利空 · 高位"
    assert hit.weight >= 90
    assert hit.importance == "high"


def test_hard_risk_mid_pre5_is_p1():
    hit = _rank(pre5=0.03)
    assert hit.decision == "P1"
    assert hit.primary_label == "硬利空 · 中位"
    assert 60 <= hit.weight < 100


def test_positive_high_pre5_is_no_chase_not_sell():
    hit = _rank(
        title="600519.SH 贵州茅台发布回购公告",
        event_tags=["回购"],
        sentiment_label="bullish",
        pre5=0.06,
    )
    assert hit.decision == "P2"
    assert hit.primary_label == "利好 · 高位（不追）"
    assert "买入" not in hit.primary_label
    assert "卖出" not in hit.primary_label
    assert "减仓" not in hit.primary_label


def test_soft_risk_in_bounce_band_is_watch():
    hit = _rank(
        title="600519.SH 贵州茅台股东减持计划",
        event_tags=["减持解禁"],
        sentiment_label="neutral",
        pre5=0.01,
    )
    assert hit.decision == "WATCH"
    assert hit.primary_label == "软利空 · 反弹区间"


def test_missing_pre5_degrades_to_info_never_guesses():
    hit = _rank(pre5=None)
    assert hit.decision == "INFO"
    assert hit.pre5 is None
    assert hit.pre5_bucket == "NA"


def test_l1_outweighs_l3_for_same_event():
    l1 = _rank(title="600519.SH 收到监管警示函")
    l3 = _rank(title="白酒行业今日普跌", name="贵州茅台")
    assert l1.tier == "L1"
    assert l3.tier == "L3"
    assert l1.weight > l3.weight
    assert l3.is_subject is False


def test_deep_repost_loses_weight():
    fresh = _rank(layer=0)
    echo3 = _rank(layer=3)
    assert fresh.weight > echo3.weight
    assert any("转载第 3 层" in r for r in echo3.reasons)


def test_duplicate_and_aggregator_are_downweighted():
    normal = _rank()
    dup = _rank(is_dup=True)
    agg = _rank(source_domain="news.google.com")
    assert dup.weight < normal.weight
    assert agg.weight < normal.weight
    assert is_aggregator_domain("news.google.com") is True
    assert is_aggregator_domain("finance.sina.com.cn") is False


def test_speculative_article_is_downweighted():
    normal = _rank()
    spec = _rank(title="600519.SH 涨停！连板龙头爆炒", event_tags=["监管处罚"])
    assert spec.speculative is True
    assert spec.weight < normal.weight


def test_source_quality_changes_weight_not_decision():
    good = _rank(source="新浪财经")
    bad = _rank(source="财联社电报")
    assert good.decision == bad.decision == "P0"
    assert good.weight > bad.weight


def test_reason_lines_are_capped_and_human_readable():
    hit = _rank(layer=3, is_dup=True, source_domain="news.google.com")
    assert 1 <= len(hit.reasons) <= 5
    assert all(isinstance(r, str) and r for r in hit.reasons)


def test_weight_is_clamped_and_monotonic():
    assert compute_weight(
        decision="P0", tier="L1", category="硬利空", risk_level="critical",
        layer=0, is_dup=False, aggregator=False, source_quality_value=1.0,
        speculative=False, published_at=datetime.now(timezone.utc),
    ) <= 100
    assert compute_weight(
        decision="INFO", tier="L4", category="中性", risk_level="none",
        layer=5, is_dup=True, aggregator=True, source_quality_value=-1.0,
        speculative=True, published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    ) >= 0
    high = _rank()
    low = _rank(pre5=None, layer=3, is_dup=True, source_domain="news.google.com")
    assert high.weight > low.weight
    assert importance_for(high.weight) == "high"
    assert importance_for(10) == "low"


def test_assign_echo_layers_clusters_same_title_and_flags_dup():
    base = datetime(2026, 11, 18, 9, 30, tzinfo=timezone.utc)
    rows = [
        {"title": "贵州茅台回购公告 - 新浪", "published_at": base},
        {"title": "贵州茅台回购公告 - 网易", "published_at": base + timedelta(minutes=30)},
        {"title": "贵州茅台回购公告 - 东方财富", "published_at": base + timedelta(hours=8)},
        {"title": "另一条不同新闻", "published_at": base + timedelta(minutes=10)},
    ]
    assign_echo_layers(rows)
    assert rows[0]["layer"] == 0
    assert rows[1]["layer"] == 1 and rows[1]["is_dup"] is True
    # 超过 6 小时但仍在同一 24h 簇：层数继续加，但不再算同题重复
    assert rows[2]["layer"] == 2 and rows[2]["is_dup"] is False
    assert rows[3]["layer"] == 0


def test_sector_negative_continues_positive_does_not_chase():
    now = datetime.now(timezone.utc)
    bad = rank_sector_article(
        title="某概念板块遭遇监管处罚",
        event_tags=["监管处罚"],
        source="新浪财经",
        published_at=now,
    )
    good = rank_sector_article(
        title="某概念板块迎来政策利好",
        event_tags=["回购"],
        sentiment_label="bullish",
        source="新浪财经",
        published_at=now,
    )
    assert bad.primary_label == "板块利空 · 发酵"
    assert good.primary_label == "板块利好 · 不追"
    assert bad.weight > good.weight
    assert bad.decision == good.decision == "WATCH"


def test_sector_uses_sentiment_not_title_regex():
    hit = rank_sector_article(
        title="SpaceX 400亿美元AI芯片债务融资部分份额最快本周发售",
        event_tags=["产业"],
        sentiment_label="bullish",
        source="金十数据",
        published_at=datetime.now(timezone.utc),
    )
    assert hit.direction == "利好"
    assert hit.primary_label == "板块利好 · 不追"

def test_broker_research_report_is_not_a_company_event():
    hit = _rank(
        title="[东吴证券]环保行业跟踪周报：生态环境部就固废污染防治征求意见",
        event_tags=["监管处罚"],
        pre5=0.07,
        source="东方财富网-行业研报",
    )
    assert hit.is_research is True
    assert hit.category == "中性"
    assert hit.decision == "INFO"
    assert hit.risk_level == "none"
    assert any("券商研报" in reason for reason in hit.reasons)


def test_real_broker_company_news_is_still_scored():
    hit = _rank(
        title="中信证券：收到监管警示函",
        code="600030.SH",
        name="中信证券",
        event_tags=["监管处罚"],
        pre5=0.07,
        source="新浪财经",
    )
    assert hit.is_research is False
    assert hit.decision == "P0"
    assert hit.risk_level == "critical"


def test_hard_risk_tag_wins_over_soft_risk():
    category, direction, tag = classify_event(
        "", ["业绩风险", "退市风险"], "bearish"
    )
    assert category == "硬利空"
    assert direction == "利空"
    assert tag == "退市风险"


def test_info_positive_is_labelled_no_chase_not_generic_info():
    hit = _rank(
        title="贵州茅台发布回购公告",
        event_tags=["回购"],
        sentiment_label="bullish",
        pre5=0.01,
    )
    assert hit.decision == "INFO"
    assert hit.primary_label == "利好 · 不追"
    assert "买入" not in hit.primary_label


def test_info_soft_negative_outside_bounce_band_is_observe():
    hit = _rank(
        title="600519.SH 贵州茅台股东减持计划",
        event_tags=["减持解禁"],
        pre5=-0.05,
    )
    assert hit.decision == "INFO"
    assert hit.primary_label == "软利空 · 观察"


def test_multi_stock_roundup_does_not_mark_each_stock():
    hit = rank_article(
        title="公告精选︱同宇新材：子公司拟投资建设年产20万吨电子树脂项目",
        code="600340.SH",
        name="华夏幸福",
        event_tags=["立案调查", "中标"],
        sentiment_label="bearish",
        source="东方财富网快讯",
        published_at=datetime.now(timezone.utc),
        pre5=0.07,
    )
    assert hit.category == "中性"
    assert hit.decision == "INFO"
    assert hit.risk_level == "none"


def test_event_with_subject_in_title_still_counts():
    hit = rank_article(
        title="600340.SH 华夏幸福被立案调查",
        code="600340.SH",
        name="华夏幸福",
        event_tags=["立案调查"],
        source="新浪财经",
        published_at=datetime.now(timezone.utc),
        pre5=0.07,
    )
    assert hit.category == "硬利空"
    assert hit.decision == "P0"
    assert hit.risk_level == "critical"
