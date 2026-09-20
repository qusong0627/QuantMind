"""新闻标签聚合（通道 B）纯函数测试。

盯四件「错了也不报错、但会骗人」的事：

1. **方向词表是划分**：RISK/CAUTION/POSITIVE 三组必须两两不相交。一旦某个标签
   同时进了两组，`classify_tag` 的返回就取决于字典迭代顺序（实测会随 Python
   版本的 hash 随机化抖动），表现是「同一条新闻今天算利空明天算利好」。
2. **A 股过滤是精确匹配**：窗口内 tickers 实测 A 股 125,347 / 港股 1 / 美股 63,904。
   不过滤就会把 `FDX`（联邦快递）当 A 股标的挂到候选列表上。
3. **利好分强弱**：`涨停/大涨/创新高` 是行情标签，近 20 天命中全市场约 39%，
   见谁都亮绿等于没信息；只有 `业绩预增/回购/中标` 这类才默认出徽章。
4. **样本取最近的**：下钻要回答「为什么标它利空」，取最早的 3 条毫无用处。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.services.engine.news_intel import CAUTION_TAGS, POSITIVE_TAGS, RISK_TAGS
from backend.shared.news_tagging import (
    ALL_DIRECTIONS,
    DIR_POS_MOVE,
    DIR_POS_STRONG,
    DIR_RISK,
    DIR_WARN,
    DIR_WEAK,
    EXCLUDING_DIRECTIONS,
    aggregate_articles,
    classify_tag,
    is_a_share_ticker,
    summarize_symbol,
)

UTC = timezone.utc


def _dt(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def _article(day: int, tickers: list[str], tags: list[str], title: str = "标题"):
    return {"published_at": _dt(day), "tickers": tickers, "event_tags": tags, "title": title}


# ---------------------------------------------------------------- 方向词表


def test_tag_vocabulary_is_a_partition() -> None:
    """三组标签必须两两不相交——否则方向取决于字典顺序，会随机翻转。"""
    # Act / Assert
    assert not (RISK_TAGS & CAUTION_TAGS)
    assert not (RISK_TAGS & POSITIVE_TAGS)
    assert not (CAUTION_TAGS & POSITIVE_TAGS)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("立案调查", DIR_RISK),
        ("财务造假", DIR_RISK),
        ("警示函", DIR_RISK),
        ("操纵市场", DIR_RISK),
        ("净利润下滑", DIR_WEAK),
        ("业绩亏损", DIR_WEAK),
        ("业绩暴雷", DIR_WEAK),
        ("减持", DIR_WARN),
        ("解禁", DIR_WARN),
        ("大跌", DIR_WARN),
        ("业绩预增", DIR_POS_STRONG),
        ("回购", DIR_POS_STRONG),
        ("中标", DIR_POS_STRONG),
        ("涨停", DIR_POS_MOVE),
        ("大涨", DIR_POS_MOVE),
        ("创新高", DIR_POS_MOVE),
    ],
)
def test_classify_tag_maps_each_vocabulary_entry(tag: str, expected: str) -> None:
    """每一个既有标签都要有确定方向（漏一个 = 该标签静默不标注）。"""
    # Act / Assert
    assert classify_tag(tag) == expected


def test_every_vocabulary_tag_classifies_and_pos_move_is_market_wide() -> None:
    """词表全覆盖，且行情类那三个标签确实被单列出来。"""
    # Act
    all_tags = RISK_TAGS | CAUTION_TAGS | POSITIVE_TAGS
    mapped = {t: classify_tag(t) for t in all_tags}
    pos_move = {t for t, d in mapped.items() if d == DIR_POS_MOVE}

    # Assert
    assert all(d is not None for d in mapped.values())
    assert pos_move == {"涨停", "大涨", "创新高"}


def test_severe_and_weak_split_keeps_vocabulary_intact() -> None:
    """严重/业绩两档合起来必须仍等于 RISK_TAGS（不丢不增）。"""
    # Act
    severe = {t for t in RISK_TAGS if classify_tag(t) == DIR_RISK}
    weak = {t for t in RISK_TAGS if classify_tag(t) == DIR_WEAK}

    # Assert
    assert severe | weak == RISK_TAGS
    assert not (severe & weak)


def test_only_regulatory_events_are_default_excluding() -> None:
    """默认排除只认监管/司法类——业绩类**不得**进排除集合。

    实证依据：实测抓到过反例「宁德时代日赚2.4亿 15家上市车企加起来赚不到一半」
    把宁德时代标成「净利润下滑」，而它恰是文中赚钱的那家。凭这类文章默认禁买
    个股是把噪声当判决。
    """
    # Act / Assert
    assert EXCLUDING_DIRECTIONS == {DIR_RISK}
    assert DIR_WEAK not in EXCLUDING_DIRECTIONS
    assert DIR_WARN not in EXCLUDING_DIRECTIONS
    assert classify_tag("业绩亏损") not in EXCLUDING_DIRECTIONS
    assert classify_tag("立案调查") in EXCLUDING_DIRECTIONS


def test_all_directions_covers_every_classifiable_tag() -> None:
    """方向全集必须覆盖词表能产出的所有方向（否则分桶会漏）。"""
    # Act
    produced = {classify_tag(t) for t in (RISK_TAGS | CAUTION_TAGS | POSITIVE_TAGS)}

    # Assert
    assert produced == set(ALL_DIRECTIONS)


def test_classify_tag_returns_none_for_unknown() -> None:
    """词表外的标签返回 None（不猜方向）。"""
    # Act / Assert
    assert classify_tag("分红预案") is None
    assert classify_tag("") is None
    assert classify_tag(None) is None


# ---------------------------------------------------------------- A 股过滤


@pytest.mark.parametrize(
    "ticker", ["600036.SH", "000001.SZ", "300750.SZ", "688981.SH", "830001.BJ"]
)
def test_is_a_share_ticker_accepts_suffixed_a_shares(ticker: str) -> None:
    # Act / Assert
    assert is_a_share_ticker(ticker) is True


@pytest.mark.parametrize(
    ("ticker", "why"),
    [
        ("FDX", "美股 ticker 无后缀"),
        ("00700.HK", "港股"),
        ("600036", "裸码（本层口径必须带后缀）"),
        ("SH600036", "前缀式（那是 PG/前端口径）"),
        ("600036.SS", "非本仓后缀"),
        ("sh600036", "Qlib 小写口径"),
        ("", "空"),
    ],
)
def test_is_a_share_ticker_rejects_others(ticker: str, why: str) -> None:
    """非 A 股一律拒绝——实测窗口内美股 63,904 个 ticker，放过就是满屏假信号。"""
    # Act / Assert
    assert is_a_share_ticker(ticker) is False, why


# ---------------------------------------------------------------- 聚合


def test_aggregate_groups_by_symbol_and_tag() -> None:
    """按 (标的, 标签) 分组计数，首末时间取真实极值。"""
    # Arrange
    articles = [
        _article(10, ["600036.SH"], ["立案调查"]),
        _article(14, ["600036.SH"], ["立案调查"]),
        _article(12, ["600036.SH"], ["减持"]),
    ]

    # Act
    hits = {(h.symbol, h.tag): h for h in aggregate_articles(articles)}

    # Assert
    assert hits[("600036.SH", "立案调查")].n == 2
    assert hits[("600036.SH", "立案调查")].first_published == _dt(10)
    assert hits[("600036.SH", "立案调查")].last_published == _dt(14)
    assert hits[("600036.SH", "减持")].n == 1
    assert hits[("600036.SH", "立案调查")].direction == DIR_RISK
    assert hits[("600036.SH", "减持")].direction == DIR_WARN


def test_aggregate_counts_one_article_once_per_symbol_tag() -> None:
    """同篇文章重复出现同一标签只算一次（否则计数被权重失真）。"""
    # Arrange
    articles = [_article(10, ["600036.SH"], ["立案调查", "立案调查"])]

    # Act
    hits = aggregate_articles(articles)

    # Assert
    assert len(hits) == 1
    assert hits[0].n == 1


def test_aggregate_dedupes_symbol_repeated_in_tickers() -> None:
    """tickers 里同一只票重复出现（不同写法归一后相同）也只算一次。"""
    # Arrange
    articles = [_article(10, ["600036.SH", "600036.SH"], ["立案调查"])]

    # Act
    hits = aggregate_articles(articles)

    # Assert
    assert len(hits) == 1
    assert hits[0].n == 1


def test_aggregate_keeps_most_recent_sample_titles() -> None:
    """样本标题取**最近**的——下钻要回答「为什么现在标它利空」。"""
    # Arrange
    articles = [
        _article(10, ["600036.SH"], ["立案调查"], "最早的"),
        _article(12, ["600036.SH"], ["立案调查"], "中间的"),
        _article(15, ["600036.SH"], ["立案调查"], "最新的"),
    ]

    # Act
    hit = aggregate_articles(articles, sample_limit=2)[0]

    # Assert
    assert hit.sample_titles == ("最新的", "中间的")


def test_aggregate_drops_non_a_share_tickers() -> None:
    """美股/港股 ticker 不进结果（窗口内实测 63,904 个美股 ticker）。"""
    # Arrange
    articles = [
        _article(10, ["FDX", "00700.HK"], ["立案调查"]),
        _article(10, ["600036.SH"], ["立案调查"]),
    ]

    # Act
    hits = aggregate_articles(articles)

    # Assert
    assert [h.symbol for h in hits] == ["600036.SH"]


def test_aggregate_ignores_tags_outside_vocabulary() -> None:
    """词表外的标签不产出（否则每个新标签都会变成一次全表刷新）。"""
    # Arrange
    articles = [_article(10, ["600036.SH"], ["立案调查", "某个新标签"])]

    # Act
    hits = aggregate_articles(articles)

    # Assert
    assert [h.tag for h in hits] == ["立案调查"]


def test_aggregate_is_deterministically_sorted() -> None:
    """输出稳定排序（(symbol, tag)），否则每轮 upsert 的日志与 diff 都在抖。"""
    # Arrange
    articles = [
        _article(10, ["600036.SH"], ["减持"]),
        _article(10, ["000001.SZ"], ["立案调查"]),
        _article(10, ["600036.SH"], ["立案调查"]),
    ]

    # Act
    hits = aggregate_articles(articles)

    # Assert
    assert [(h.symbol, h.tag) for h in hits] == [
        ("000001.SZ", "立案调查"),
        ("600036.SH", "减持"),
        ("600036.SH", "立案调查"),
    ]


def test_aggregate_handles_empty_input() -> None:
    """空输入返回空（不是异常，也不是「全市场利好」）。"""
    # Act / Assert
    assert aggregate_articles([]) == ()


def test_aggregate_returns_tuple_for_immutability() -> None:
    """返回 tuple（不可变），调用方不能就地改。"""
    # Arrange / Act
    hits = aggregate_articles([_article(10, ["600036.SH"], ["立案调查"])])

    # Assert
    assert isinstance(hits, tuple)


def test_aggregate_skips_articles_without_publish_time() -> None:
    """没有发布时间的文章必须丢弃——用 enrich 时间兜底正是本次要避免的错口径。"""
    # Arrange
    bad = {"published_at": None, "tickers": ["600036.SH"], "event_tags": ["立案调查"], "title": "x"}
    good = _article(10, ["600036.SH"], ["立案调查"])

    # Act
    hits = aggregate_articles([bad, good])

    # Assert
    assert len(hits) == 1
    assert hits[0].n == 1


# ---------------------------------------------------------------- 汇总


def test_summarize_symbol_splits_by_direction() -> None:
    """单票汇总按方向分桶，五档各自独立（只有 risk 默认参与排除）。"""
    # Arrange
    hits = aggregate_articles(
        [
            _article(10, ["600036.SH"], ["立案调查"]),
            _article(11, ["600036.SH"], ["解禁"]),
            _article(12, ["600036.SH"], ["业绩亏损"]),
            _article(13, ["600036.SH"], ["业绩预增"]),
            _article(14, ["600036.SH"], ["涨停"]),
        ]
    )

    # Act
    summary = summarize_symbol(hits)

    # Assert
    assert [h.tag for h in summary[DIR_RISK]] == ["立案调查"]
    assert [h.tag for h in summary[DIR_WARN]] == ["解禁"]
    assert [h.tag for h in summary[DIR_WEAK]] == ["业绩亏损"]
    assert [h.tag for h in summary[DIR_POS_STRONG]] == ["业绩预增"]
    assert [h.tag for h in summary[DIR_POS_MOVE]] == ["涨停"]


def test_summarize_symbol_keeps_empty_buckets() -> None:
    """没有的方向也要存在（前端按固定桶渲染，缺键会显示 undefined）。"""
    # Arrange
    hits = aggregate_articles([_article(10, ["600036.SH"], ["立案调查"])])

    # Act
    summary = summarize_symbol(hits)

    # Assert
    assert set(summary) == set(ALL_DIRECTIONS)
    assert summary[DIR_WARN] == []
    assert summary[DIR_WEAK] == []
