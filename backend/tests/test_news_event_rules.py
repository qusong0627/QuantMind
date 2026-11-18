"""新闻事件规则单测（生产快速通道用）。"""

from __future__ import annotations

from backend.shared.news_event_rules import (
    classify_event,
    is_speculative,
    source_quality,
    tier_for,
    validate_title,
)


def test_classify_hard_risk_from_title():
    cat, direction, et = classify_event("某公司因涉嫌信息披露违法违规被证监会立案调查", [], "")
    assert cat == "硬利空"
    assert direction == "利空"
    assert et == "立案调查"


def test_classify_positive_from_tags():
    cat, direction, et = classify_event("某公司公告", ["中标"], "neutral")
    assert cat == "硬利好"
    assert direction == "利好"
    assert et == "中标"


def test_classify_soft_risk_from_title():
    cat, direction, et = classify_event("某公司股东拟减持不超过2%股份", [], "")
    assert cat == "软利空"
    assert direction == "利空"
    assert et == "减持解禁"


def test_classify_sentiment_fallback():
    cat, direction, et = classify_event("某公司发布新品", [], "bullish")
    assert cat == "泛利好"
    assert direction == "利好"


def test_validate_title_requires_company_and_keyword():
    assert validate_title("某公司被立案调查", "600519", "贵州茅台", "硬利空") is False
    assert validate_title("贵州茅台被立案调查", "600519", "贵州茅台", "硬利空") is True
    assert validate_title("600519被立案调查", "600519", "", "硬利空") is True


def test_validate_title_negation_and_rating():
    assert validate_title("贵州茅台澄清：未被立案调查", "600519", "贵州茅台", "硬利空") is False
    assert validate_title("贵州茅台维持增持评级", "600519", "贵州茅台", "硬利好") is False


def test_validate_title_rejects_broker_research_author():
    assert validate_title(
        "[东吴证券]贵州茅台业绩点评：维持看好", "600519", "贵州茅台", "硬利好"
    ) is False


def test_validate_title_rejects_roundup():
    assert validate_title(
        "贵州茅台、五粮液等白酒股涨停复盘", "600519", "贵州茅台", "硬利好"
    ) is False


def test_tier_for():
    assert tier_for("600519 被立案调查", "600519", "贵州茅台") == "L1"
    assert tier_for("贵州茅台被立案调查", "600519", "贵州茅台") == "L2"
    assert tier_for("白酒板块震荡", "600519", "贵州茅台") == "L3"
    assert tier_for("白酒板块震荡", "", "") == "L4"


def test_is_speculative():
    assert is_speculative("某股3连板，游资接力", []) is True
    assert is_speculative("某公司发布年报", []) is False


def test_source_quality_sign():
    assert source_quality("新浪财经-国内滚动") > 0
    assert source_quality("财联社电报") < 0
    assert source_quality("未知来源") == 0.0
