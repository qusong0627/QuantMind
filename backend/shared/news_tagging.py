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

**为什么还要「主体确认」**（v2，本节为新增）：上游 matcher 的口径是「文本里出现
实体 → 打这个标的」，于是实测量出三类系统性假阳性，7 天 A 股标签对里有 **38%**
是噪声，且已经漏进 `news_stock_tags` 变成会误导交易判断的徽章：

1. **源站本身是上市公司**：`东方财富`（finance.eastmoney.com，7 天 508 条）/
   `同花顺`（news.10jqka.com.cn，864 条）/ `太平洋汽车网`（pcauto，153 条）。
   署名出现在**每一篇**该站文章里，实测「标题里真提到这只票」的比例只有
   0~4%。后果不是多几条噪声，而是这些票的 risk 标签会触发默认排除——
   `news_stock_tags` 里 300033.SZ 拿到 6 条「立案调查」、3 条「财务造假」，
   原文分别是《十五五规划：对财务造假立体化追责》这类宏观稿。
2. **招标/采购平台源**：6 个央企采购站（bid.powerchina.cn 等）被当 RSS 订阅，
   7 天 1242 条。601669.SH（中国电建）因此拿到 **389 条「中标」pos_strong**
   ——那是它自己招标网站的中标公示流水，不是公司新闻。
3. **简称是通用名词**：`农产品`（000061.SZ）、`太平洋`（601099.SH）会命中任意
   财经/汽车文本。

处置分三层，互不依赖：域名黑名单（`NON_NEWS_DOMAINS`，覆盖率 100%，需要
`source_domain` 字段）→ 主体确认（`SUBJECT_NAME_REQUIRED`，标题里以正文主体
身份出现名称，覆盖率 96~100%，**不需要**新字段）→ 否定守卫（「均未涉嫌内幕
交易」不得判成内幕交易）。近重复折叠是**另一个可选函数**，不改 `n` 的语义
（`n` 是文章数，UI 直接展示「近 N 天几条新闻」，改它的口径会连带改前端文案）。
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


# ---------------------------------------------------------------- 噪声守卫（v2）

#: **非新闻源**：正文是招标/采购/中标公示的站点，不是新闻。
#: 实测 7 天 1242 条，601669.SH 一家就 247 条。整篇丢弃（不产出任何标签）。
#: 命中面只在域名上，不做「标题含招标」这类词面判断——实测词面只覆盖 32%。
NON_NEWS_DOMAINS: frozenset[str] = frozenset(
    {
        "bid.powerchina.cn",  # 中国电建招标
        "caigou.chinatelecom.com.cn",  # 中国电信采购
        "ec.ceec.net.cn",  # 中国能建采购
        "bid.cnooc.com.cn",  # 中国海油招标
        "www.crccep.com",  # 中国铁建采购
        "www.cuecp.cn",  # 中国联通采购
        "www.cnpharm.com",  # 中国医药采购
        "sci99.com",  # 卓创资讯行情页
        "www.sci99.com",
    }
)

#: 公告型标题兜底（未知来源的采购站）：与域名黑名单叠加，不是替代。
#: 只认**公告体**词面，不含裸「中标」——「公司中标重大项目」是真新闻。
NON_NEWS_TITLE = re.compile(
    r"招标公告|采购公告|询价公告|比价公告|中标候选人公示|中标结果(?:公告|公示)"
    r"|成交结果(?:公告|公示)|竞争性磋商|供应商(?:征集|入围)|招标文件|流标|废标"
    r"|单一来源采购|采购结果(?:公告|公示)"
)

#: **需主体确认**的票：简称与媒体名/通用名词同形。
#: 只认「标题里以正文主体身份出现公司名」，不再凭正文/署名匹配。
SUBJECT_NAME_REQUIRED: dict[str, str] = {
    "300033.SZ": "同花顺",
    "300059.SZ": "东方财富",
    "601099.SH": "太平洋",
    "301299.SZ": "卓创资讯",
    "603888.SH": "新华网",
    "000061.SZ": "农产品",
}

#: 同形但**不是这只票**的写法：名称作为更长实体名的一部分出现。
#: 「太平洋汽车网」是媒体，「太平洋」是 601099.SH（太平洋证券）——不拦就会把
#: 汽车评测稿挂到券商头上（实测 pcauto 源 153 条）。
SUBJECT_NAME_CONFLICT: dict[str, re.Pattern[str]] = {
    "601099.SH": re.compile(r"太平洋(?:汽车|汽车网|财险|保险|人寿)"),
}

#: 标题尾部的来源署名（`… - 新华网` / `…__金十数据`）。主体确认要把这段剥掉再看，
#: 否则「宏观稿 + 署名=公司名」会被当成公司新闻。
_ATTRIBUTION_TAIL = re.compile(r"\s*[-—_|｜]\s*([^-—_|｜]{1,16})\s*$")

#: 否定词窗：`均未涉嫌内幕交易` 里「内幕交易」前面 4 个字内有否定词 → 判为被否定。
#: 窗口收得很窄是有意的：`未按规定披露被立案调查` 的「未」修饰的是「披露」，
#: 不是「立案调查」——放宽到 10 字就会把这条真利空洗掉。
_NEGATION = re.compile(
    r"(?:均未|并未|未有|没有|未|不|无|否认|不存在|并非|免于|澄清|辟谣)"
    r"(?:涉嫌|涉及|存在|构成|被|遭)?[^，。；！？]{0,4}$"
)


def normalize_title(title: object) -> str:
    """标题归一（剥署名尾巴 + 去标点空白），用于近重复识别。"""
    text = str(title or "").strip()
    tail = _ATTRIBUTION_TAIL.search(text)
    if tail:
        text = text[: tail.start()]
    return _TITLE_NOISE.sub("", text)


_TITLE_NOISE = re.compile(r"[\s，。！？：；、“”‘’（）()《》〈〉【】\[\]…\.\,\!\?\"\'\-—_|｜/\\]+")


def _name_as_subject(title: str, name: str) -> bool:
    """公司名是否作为**正文主体**出现在标题里（排除仅出现在署名尾巴）。"""
    if not name or name not in title:
        return False
    body = title
    for _ in range(3):  # 最多剥三层尾巴（`… - xx - 金十数据`）
        tail = _ATTRIBUTION_TAIL.search(body)
        if not tail or name not in tail.group(1):
            break
        body = body[: tail.start()]
    return name in body


def subject_is_confirmed(symbol: str, title: str) -> bool:
    """该标的是否确为本文主体。未列入 `SUBJECT_NAME_REQUIRED` 的票一律放行。"""
    name = SUBJECT_NAME_REQUIRED.get(symbol)
    if name is None:
        return True
    conflict = SUBJECT_NAME_CONFLICT.get(symbol)
    if conflict is not None and conflict.search(title):
        return False
    return _name_as_subject(title, name)


def tag_is_negated(title: str, tag: str) -> bool:
    """标题里的标签词是否被否定（否定句不得当作事件发生）。"""
    hits = list(re.finditer(re.escape(tag), title))
    if not hits:
        return False
    return all(
        _NEGATION.search(title[max(0, m.start() - 14) : m.start()]) for m in hits
    )


def article_is_noise(title: str, domain: str = "") -> bool:
    """整篇丢弃：非新闻源域名 / 公告型标题。"""
    return domain in NON_NEWS_DOMAINS or bool(NON_NEWS_TITLE.search(title))


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
    """一篇文章 → ``(symbol, tag, title)`` 展开（已过噪声守卫）。

    守卫顺序（先便宜后贵，且都只作用在**本文**上，不跨文章）：
    整篇丢弃（非新闻源 / 公告体）→ 逐标的确认主体 → 逐标签去否定词。

    两侧都**去重**：enricher 输出的 ``tickers`` / ``event_tags`` 是数组，
    同一只票或同一个标签重复出现都会把计数灌成 2——而 `n` 会被 UI 当作
    「近 20 天有几条新闻」直接展示，虚高就是假证据。

    ``source_domain``（可选，来自 `huntly_publish_index`）缺失时退化为纯标题判断：
    主体确认仍生效，只有域名黑名单不生效。
    """
    title = str(article.get("title") or "")
    domain = str(article.get("source_domain") or "").strip().lower()
    if article_is_noise(title, domain):
        return
    tags = {
        str(t)
        for t in (article.get("event_tags") or [])
        if classify_tag(t) and not tag_is_negated(title, str(t))
    }
    symbols = {
        str(s)
        for s in (article.get("tickers") or [])
        if is_a_share_ticker(s) and subject_is_confirmed(str(s), title)
    }
    for symbol in sorted(symbols):
        for tag in sorted(tags):
            yield symbol, tag, title


def collapse_near_duplicates(
    articles: Iterable[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """折叠「同一篇稿被 N 家转载」的副本（保留**最早**发布的那条）。

    实测热门票重复率：002594.SZ 50%、BIDU 27%、NVDA 28%、整体 9.5%。不折叠时
    `n=12` 可能只是同一篇通稿被 12 家转载，读起来像 12 个独立信源。

    **不改 `aggregate_articles` 的 `n` 语义**：`n` 是「文章数」，UI 直接展示
    「近 N 天几条新闻」，本函数是调用方**可选**的前置步骤（要几条独立信源就
    先转一遍）。键含标的集合，避免把「同一标题、不同公司」误合并。
    """
    kept: list[Mapping[str, object]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for article in sorted(
        articles,
        key=lambda a: (
            a.get("published_at") is None,
            str(a.get("published_at") or ""),
        ),
    ):
        symbols = tuple(sorted(str(s) for s in (article.get("tickers") or [])))
        key = (symbols, normalize_title(article.get("title")))
        if key in seen:
            continue
        seen.add(key)
        kept.append(article)
    return kept


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
