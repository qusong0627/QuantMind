"""生产新闻事件规则：标题/标签 → 类别、方向、层级、来源质量。

纯函数，无 IO。研究口径见 ``docs/新闻源智能体_设计与数据流_20261118.md``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 顺序敏感：先硬利空、再软利空、再利好；同一标题只取第一条。
_RULES: tuple[tuple[str, str, str, str], ...] = (
    # category, direction, event_type, regex
    ("硬利空", "利空", "财务造假", r"财务造假|虚增|造假|舞弊"),
    ("硬利空", "利空", "立案调查", r"立案|调查"),
    ("硬利空", "利空", "退市风险", r"退市|面值退市|终止上市|风险警示|\*?ST"),
    ("硬利空", "利空", "债务风险", r"债务|违约|逾期|兑付|重整|破产"),
    ("硬利空", "利空", "事故停产", r"事故|爆炸|火灾|停产|召回|泄漏"),
    ("硬利空", "利空", "制裁关税", r"制裁|关税|出口管制|实体清单|反倾销"),
    ("硬利空", "利空", "监管处罚", r"处罚|警示函|监管函|问询函|监管|违规|违法|责令|整改|约谈"),
    ("软利空", "利空", "业绩风险", r"亏损|下滑|预减|不及预期|减值|商誉|下降"),
    ("软利空", "利空", "减持解禁", r"减持|解禁|限售"),
    ("软利空", "利空", "质押", r"质押|平仓"),
    ("软利空", "利空", "诉讼", r"诉讼|仲裁|起诉"),
    ("软利空", "利空", "舆情", r"舆情|举报|维权|投诉|丑闻|曝光"),
    ("硬利好", "利好", "中标", r"中标"),
    ("硬利好", "利好", "订单", r"订单|合同|签约"),
    ("硬利好", "利好", "业绩预增", r"预增|净利润.*增长|同比增长|扭亏"),
    ("硬利好", "利好", "净利润增长", r"净利润.*增长|同比增长"),
    ("硬利好", "利好", "回购", r"回购"),
    ("硬利好", "利好", "增持", r"增持"),
    ("硬利好", "利好", "分红", r"分红|派息|送转"),
    ("硬利好", "利好", "并购", r"并购|收购|合并|资产注入"),
    ("硬利好", "利好", "重组", r"重组|重整"),
    ("治理利好", "利好", "股权激励", r"股权激励|员工持股"),
    ("治理利好", "利好", "战略合作", r"战略合作"),
)

_TAG_CATEGORY = {
    "财务造假": ("硬利空", "利空"),
    "立案调查": ("硬利空", "利空"),
    "退市风险": ("硬利空", "利空"),
    "债务风险": ("硬利空", "利空"),
    "事故停产": ("硬利空", "利空"),
    "制裁关税": ("硬利空", "利空"),
    "监管处罚": ("硬利空", "利空"),
    "业绩风险": ("软利空", "利空"),
    "减持解禁": ("软利空", "利空"),
    "质押": ("软利空", "利空"),
    "诉讼": ("软利空", "利空"),
    "舆情": ("软利空", "利空"),
    "中标": ("硬利好", "利好"),
    "订单": ("硬利好", "利好"),
    "业绩预增": ("硬利好", "利好"),
    "净利润增长": ("硬利好", "利好"),
    "扭亏为盈": ("硬利好", "利好"),
    "回购": ("硬利好", "利好"),
    "增持": ("硬利好", "利好"),
    "分红": ("硬利好", "利好"),
    "并购": ("硬利好", "利好"),
    "重组": ("硬利好", "利好"),
    "股权激励": ("治理利好", "利好"),
    "战略合作": ("治理利好", "利好"),
}

_CATEGORY_RE = {
    "硬利空": r"财务造假|虚增|造假|舞弊|立案|调查|退市|面值退市|终止上市|风险警示|\*?ST|"
              r"债务|违约|逾期|兑付|重整|破产|事故|爆炸|火灾|停产|召回|泄漏|"
              r"制裁|关税|出口管制|实体清单|反倾销|处罚|警示函|监管函|问询函|"
              r"监管|违规|违法|责令|整改|约谈",
    "软利空": r"亏损|下滑|预减|不及预期|减值|商誉|下降|减持|解禁|限售|质押|平仓|"
              r"诉讼|仲裁|起诉|舆情|举报|维权|投诉|丑闻|曝光",
    "硬利好": r"中标|订单|合同|签约|预增|净利润.*增长|同比增长|扭亏|回购|增持|"
              r"分红|派息|送转|并购|收购|合并|资产注入|重组|重整",
    "治理利好": r"股权激励|员工持股|战略合作",
    "泛利好": r"利好|上涨|大涨|涨停|新高",
    "泛利空": r"利空|下跌|大跌|跌停|新低",
}

_NEG = re.compile(r"终止|取消|放弃|失败|未|不|无|澄清|辟谣|否认|没有|不存在")
_RATING = re.compile(r"研报|评级|维持|目标价|首次覆盖|买入评级|增持评级|推荐评级")
_SPEC = re.compile(
    r"涨停|跌停|封板|一字板|地天板|天地板|炸板|连板|首板|二板|三板|晋级|断板|"
    r"题材|概念|热点|炒作|爆炒|妖股|龙头|打板|游资|散户|接力|追高|"
    r"龙虎榜|席位|营业部|异动|高换手|换手率|天量|放量|缩量|拉升|跳水|"
    r"次新股|新股|低价股|小盘股|壳资源|ST"
)

#: 券商研报作者前缀：``[东吴证券]`` / ``中信证券：`` / ``【国金证券】``。
#: 这类标题里的「监管/处罚/增持」说的是被覆盖的行业或个股，不是券商自身事件；
#: 研究口径（2026-11-18 §2.1）把作者从「被报道主体」里剔除。
_RESEARCH_AUTHOR = re.compile(
    r"^\s*[\[【(（]?\s*([一-龥A-Za-z]{2,12}(?:证券|研究院|研究所))"
    r"\s*[\]】)）]?\s*[：:]?"
)
_RESEARCH_CONTENT = re.compile(
    r"行业研究|策略|晨报|周报|月报|研报|研究|评级|目标价|首次覆盖|维持|上调|下调|"
    r"看好|推荐|增持|点评|跟踪|展望|建议|关注|有望|晨会"
)

#: 多票汇总稿：一篇稿子里同时讲 N 家公司，事件标签无法归到单一主体。
_ROUNDUP = re.compile(
    r"公告集锦|公告精选|公告速递|公告一览|公告汇总|晚间公告|早间公告|"
    r"盘前播报|收盘播报|涨停复盘|板块复盘|市场综述|收评|午评|早评|"
    r"要闻速递|业绩速递|公告解读"
)

_GOOD_SOURCES = {
    "新浪财经": 0.60,
    "人民日报": 0.60,
    "东方财富网-行业研报": 0.50,
    "东方财富-行业研报": 0.50,
    "集思录": 0.50,
    "同花顺实时新闻": 0.35,
    "ulapia": 0.35,
    "股讯": 0.30,
    "澎湃新闻": 0.20,
    "路透": 0.10,
    "彭博": 0.10,
}
_BAD_SOURCES = {
    "东方财富网 快讯": -0.80,
    "东方财富网快讯": -0.80,
    "财联社电报": -0.70,
    "财联社 - 电报": -0.70,
    "财联社热榜": -0.50,
    "7x24": -0.40,
    "7*24": -0.40,
    "格隆汇快讯": -0.30,
    "商业 - 最新新闻 - Google 新闻": -0.20,
    "华尔街见闻 - 实时快讯": -0.10,
}


def _tag_names(tags: Iterable[str] | None) -> list[str]:
    if not tags:
        return []
    return [str(t).strip() for t in tags if str(t).strip()]


def classify_event(
    title: str,
    event_tags: Iterable[str] | None = None,
    sentiment_label: str = "",
) -> tuple[str, str, str]:
    """标题/标签 → (category, direction, event_type)。

    先看 enrichment event_tags（更准），再标题正则，最后 sentiment 兜底。
    """
    tags = _tag_names(event_tags)
    # 同一篇稿可能同时带「业绩风险」和「退市风险」；硬风险必须优先，
    # 不能因为数组顺序把退市稿降成软利空。
    for category, direction in (
        ("硬利空", "利空"),
        ("软利空", "利空"),
        ("硬利好", "利好"),
        ("治理利好", "利好"),
    ):
        for tag in tags:
            mapped = _TAG_CATEGORY.get(tag)
            if mapped and mapped[0] == category:
                return category, direction, tag

    text = str(title or "")
    for cat, direction, event_type, pattern in _RULES:
        if re.search(pattern, text):
            return cat, direction, event_type

    label = str(sentiment_label or "").lower()
    if label == "bullish":
        return "泛利好", "利好", "情绪利好"
    if label == "bearish":
        return "泛利空", "利空", "情绪利空"
    return "中性", "中性", "中性"


def _title_keyword(title: str, category: str) -> re.Match[str] | None:
    pattern = _CATEGORY_RE.get(category)
    if not pattern:
        return None
    return re.search(pattern, str(title or ""))


def validate_title(title: str, code: str, name: str, category: str) -> bool:
    """标题级验证：关键词 + 公司名/代码 + 否定 + 研报评级。"""
    text = str(title or "")
    m = _title_keyword(text, category)
    if m is None:
        return False
    digits = re.sub(r"\D", "", str(code or ""))[:6]
    has_code = bool(digits and digits in text)
    has_name = bool(name and str(name) in text)
    if not (has_code or has_name):
        return False
    if _ROUNDUP.search(text):
        return False
    if _RESEARCH_AUTHOR.search(text) and _RESEARCH_CONTENT.search(text):
        return False
    pre = text[max(0, m.start() - 12):m.start()]
    if _NEG.search(pre):
        return False
    if category in ("硬利好", "治理利好") and _RATING.search(text):
        return False
    if "增持" in text and category in ("硬利好", "治理利好"):
        if not re.search(r"股东|实控人|董监高|拟增持|增持计划|增持股份|增持公司", text):
            return False
    return True


def tier_for(title: str, code: str, name: str) -> str:
    text = str(title or "")
    digits = re.sub(r"\D", "", str(code or ""))[:6]
    if digits and digits in text:
        return "L1"
    if name and str(name) in text:
        return "L2"
    if digits or name:
        return "L3"
    return "L4"


def is_speculative(title: str, event_tags: Iterable[str] | None = None) -> bool:
    text = str(title or "") + " " + " ".join(_tag_names(event_tags))
    return bool(_SPEC.search(text))


def is_research_report(title: str) -> bool:
    """券商研报（作者前缀 + 研究词）→ 不是公司事件，不得据标题打风险/利好。"""
    text = str(title or "").strip()
    return bool(_RESEARCH_AUTHOR.match(text) and _RESEARCH_CONTENT.search(text))


def is_roundup(title: str) -> bool:
    """多票汇总稿（公告集锦/涨停复盘/收评）→ 事件标签不能落到单只股票。"""
    return bool(_ROUNDUP.search(str(title or "")))


def source_quality(source: str) -> float:
    src = str(source or "")
    for key, value in _BAD_SOURCES.items():
        if key in src:
            return value
    for key, value in _GOOD_SOURCES.items():
        if key in src:
            return value
    return 0.0
