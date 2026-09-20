"""新闻标签聚合（通道 B）——**纯函数**：标签分方向 + A 股过滤 + 按 (标的,标签) 汇总。

上游是 `news_article_enrichment`（每篇一行，带 `tickers` / `event_tags` / 标题）
与 Huntly 的**发布时间**（见 `news_tag_contract` 的 `huntly_publish_index`）。
本模块只管「拿到文章后怎么算」，不碰 IO，便于金样测试。

**为什么发布时间必须来自 Huntly**：enricher 是回填式跑的，实测 enrich 时间比
发布时间中位滞后 **913.8 小时（约 38 天）**、p99 达 4050 小时；1804 篇风险文章里
只有 180 篇当天 enrich。用 enrich 时间当「最近 20 天」会把 5 个月前的旧闻当新闻。
（`connected_at` 是**上海墙钟字符串**，转换见 `news_tag_contract.parse_huntly_time`。）

**为什么 A 股要精确过滤**：近 25 天窗口里 tickers 实测 A 股 125,347 / 港股 1 /
美股 63,904——不过滤就会把 `FDX`（联邦快递）挂到 A 股候选列表上。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import re

from backend.services.engine.news_intel import (
    CAUTION_TAGS,
    POSITIVE_TAGS,
    RISK_TAGS,
)

#: 方向常量（落库 `direction` 列取值）
DIR_RISK = "risk"  # 监管/司法类严重事件 → **默认排除**
DIR_WEAK = "weak"  # 业绩类负面 → 只标注（精度低，见下）
DIR_WARN = "warn"  # 减持/解禁等警示 → 只标注
DIR_POS_STRONG = "pos_strong"  # 强利好 → 默认出徽章
DIR_POS_MOVE = "pos_move"  # 行情类利好 → 默认不出（命中面太广）

#: 业绩类负面：**只标注、不默认排除**。与监管类同为 RISK_TAGS 成员，但性质不同——
#: 监管类（立案调查/财务造假/处罚…）是**离散事实**，实测 20 天里 38 只命中
#: 「立案调查」共 184 篇，精度高；而业绩类是从一篇可能讲**行业**的文章里推断个股，
#: 实测就抓到过反例：「宁德时代日赚2.4亿 15家上市车企加起来赚不到一半」把
#: 宁德时代标成了「净利润下滑」——它恰恰是文中赚钱的那家。凭一篇这样的文章
#: 默认禁买个股，是把噪声当判决。
_WEAK_TAGS: frozenset[str] = frozenset(
    {"业绩暴雷", "业绩亏损", "净利润下滑", "业绩预减", "业绩不及预期"}
)

#: 行情类利好：近 20 天命中全市场约 39%，见谁都亮绿等于没信息。
_POS_MOVE_TAGS: frozenset[str] = frozenset({"涨停", "大涨", "创新高"})

#: 标签 → 方向。词表来源唯一（`news_intel`），本模块只做「加方向」这一件事。
#: 每个分支都写成词表的**子集**（差集/交集），这样「方向表里的键 ⊆ 词表」
#: 是结构上成立的，而不是只靠测试兜着——否则子集常量里一个错别字
#: 就会让词表外的标签也带上方向。
TAG_DIRECTIONS: dict[str, str] = {
    **dict.fromkeys(RISK_TAGS - _WEAK_TAGS, DIR_RISK),
    **dict.fromkeys(RISK_TAGS & _WEAK_TAGS, DIR_WEAK),
    **dict.fromkeys(CAUTION_TAGS, DIR_WARN),
    **dict.fromkeys(POSITIVE_TAGS - _POS_MOVE_TAGS, DIR_POS_STRONG),
    **dict.fromkeys(POSITIVE_TAGS & _POS_MOVE_TAGS, DIR_POS_MOVE),
}

#: 参与**默认排除**的方向（其余只标注）。前端与 /list 的 `exclude_news_risk`
#: 一律引用本常量，避免「哪些方向算严重」在多处各写一份。
EXCLUDING_DIRECTIONS: frozenset[str] = frozenset({DIR_RISK})

#: 全部方向（固定顺序，供 UI 分桶渲染；空桶也保留）
ALL_DIRECTIONS: tuple[str, ...] = (
    DIR_RISK,
    DIR_WEAK,
    DIR_WARN,
    DIR_POS_STRONG,
    DIR_POS_MOVE,
)

#: A 股后缀式（QuantDB / Qlib 行情层口径）。**必须精确匹配**：
#: `StockCodeUtil.to_suffix` 对不认识的码原样返回，用它做过滤等于没过滤。
_A_SHARE_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def classify_tag(tag: object) -> str | None:
    """标签 → 方向。词表外返回 ``None``（不猜）。"""
    return TAG_DIRECTIONS.get(str(tag)) if tag else None


def is_a_share_ticker(ticker: object) -> bool:
    """是否 A 股后缀式代码（``600036.SH`` / ``000001.SZ`` / ``830001.BJ``）。"""
    return bool(_A_SHARE_TICKER.match(str(ticker or "")))


@dataclass(frozen=True)
class TagHit:
    """一只票在一个标签上的汇总（落库一行）。"""

    symbol: str
    tag: str
    direction: str
    n: int
    first_published: datetime
    last_published: datetime
    sample_titles: tuple[str, ...]


def _iter_pairs(article: Mapping[str, object]) -> Iterable[tuple[str, str, str]]:
    """一篇文章 → ``(symbol, tag, title)`` 展开（已过滤 A 股与词表外标签）。

    两侧都**去重**：enricher 输出的 ``tickers`` / ``event_tags`` 是数组，
    同一只票或同一个标签重复出现都会把计数灌成 2——而 `n` 会被 UI 当作
    「近 20 天有几条新闻」直接展示，虚高就是假证据。
    """
    title = str(article.get("title") or "")
    tags = {str(t) for t in (article.get("event_tags") or []) if classify_tag(t)}
    symbols = {str(s) for s in (article.get("tickers") or []) if is_a_share_ticker(s)}
    for symbol in sorted(symbols):
        for tag in sorted(tags):
            yield symbol, tag, title


def aggregate_articles(
    articles: Iterable[Mapping[str, object]], *, sample_limit: int = 3
) -> tuple[TagHit, ...]:
    """文章流 → 按 ``(symbol, tag)`` 汇总的标签行（稳定排序）。

    ``articles`` 每项需带 ``published_at``（**发布时间**，aware datetime）、
    ``tickers``、``event_tags``、``title``。缺 ``published_at`` 的文章直接丢弃
    ——用 enrich 时间兜底正是本模块要避免的错口径。
    """
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for article in articles:
        published = article.get("published_at")
        if not isinstance(published, datetime):
            continue
        for symbol, tag, title in _iter_pairs(article):
            key = (symbol, tag)
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = {
                    "n": 1,
                    "first": published,
                    "last": published,
                    # (发布时间, 标题) 对照，取最近时用得上
                    "samples": [(published, title)] if title else [],
                }
                continue
            bucket["n"] = int(bucket["n"]) + 1
            if published < bucket["first"]:  # type: ignore[operator]
                bucket["first"] = published
            if published > bucket["last"]:  # type: ignore[operator]
                bucket["last"] = published
            if title:
                cast_samples = bucket["samples"]
                assert isinstance(cast_samples, list)
                cast_samples.append((published, title))

    hits: list[TagHit] = []
    for (symbol, tag), bucket in buckets.items():
        samples = bucket["samples"]
        assert isinstance(samples, list)
        # 取最近的 N 条（下钻要回答「为什么现在标它利空」），按时间倒序
        recent = sorted(samples, key=lambda p: p[0], reverse=True)[:sample_limit]
        hits.append(
            TagHit(
                symbol=symbol,
                tag=tag,
                direction=classify_tag(tag) or DIR_RISK,
                n=int(bucket["n"]),
                first_published=bucket["first"],  # type: ignore[arg-type]
                last_published=bucket["last"],  # type: ignore[arg-type]
                sample_titles=tuple(t for _, t in recent),
            )
        )
    return tuple(sorted(hits, key=lambda h: (h.symbol, h.tag)))


def summarize_symbol(hits: Iterable[TagHit]) -> dict[str, list[TagHit]]:
    """单票（或多票）标签行 → 按方向分桶。**空桶保留**（前端按固定桶渲染）。"""
    out: dict[str, list[TagHit]] = {d: [] for d in ALL_DIRECTIONS}
    for hit in hits:
        out.setdefault(hit.direction, []).append(hit)
    return out
