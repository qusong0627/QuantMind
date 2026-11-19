"""资讯中心 2.0 的排序权重（纯函数，无 IO）。

设计目标：**一条新闻只回答一个问题**——它现在改变什么；其余证据收进 ``reasons``。
权重只用于排序与视觉强调，不产生任何交易动作；决策等级完全复用
``backend.shared.news_decision.decide``（研究回测里唯一通过的口径）。

研究依据（2026-11-18）：
- ``硬利空 + pre5>5%`` 是唯一通过决策回测的格子（每事件避开 +186.8bp，t=2.81）；
- ``利好 + pre5>5%`` 是「不追」，不是卖出；
- 转载层越深越差（3+ 层在泛利好/泛利空/硬利好/软利空上都是负的）；
- 快讯/热榜源只是在股票涨完之后发，来源质量只调 confidence/权重，不改决策；
- 缺失 pre5 一律 INFO，绝不猜。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from backend.shared.news_decision import Decision, decide
from backend.shared.news_event_rules import (
    classify_event,
    is_research_report,
    is_roundup,
    is_speculative,
    source_quality,
    tier_for,
    validate_title,
)

CN_TZ = timezone(timedelta(hours=8))

#: 决策等级 → 展示语。**不出现买入/卖出/减仓**（展示面词表纪律）。
PRIMARY_LABELS: dict[str, str] = {
    "P0": "硬利空 · 高位",
    "P1": "硬利空 · 中位",
    "P2": "利好 · 高位（不追）",
    "WATCH": "观察",
    "INFO": "信息",
}

#: 决策等级基础权重。基础值 + 最大加成（约 46）不超过 100，
#: 否则 P0 内部会被 clamp 压平，转载/重复/来源质量就失去区分度。
_BASE_WEIGHT: dict[str, int] = {
    "P0": 58,
    "P1": 46,
    "P2": 38,
    "WATCH": 24,
    "INFO": 6,
}

_TIER_BONUS = {"L1": 12, "L2": 8, "L3": 0, "L4": -4}

_CATEGORY_BONUS = {
    "硬利空": 6,
    "硬利好": 4,
    "泛利空": 4,
    "软利空": 2,
    "泛利好": 0,
    "治理利好": 0,
    "中性": 0,
}

_AGGREGATOR_DOMAINS = (
    "news.google.com",
    "google.com",
    "baidu.com",
    "toutiao.com",
    "weibo.com",
)

_RISK_LEVEL = {"硬利空": "critical", "软利空": "high"}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # Huntly 的墙钟是上海时间；DB 侧本应全部 aware，naive 只做兜底。
        return value.replace(tzinfo=CN_TZ).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def is_aggregator_domain(domain: str | None) -> bool:
    text = str(domain or "").strip().lower()
    if not text:
        return False
    return any(key in text for key in _AGGREGATOR_DOMAINS)


def risk_level_for(category: str) -> str:
    return _RISK_LEVEL.get(str(category or ""), "none")


def importance_for(weight: int) -> str:
    if weight >= 68:
        return "high"
    if weight >= 38:
        return "medium"
    return "low"


def primary_label(decision: str, *, category: str, reason_code: str = "") -> str:
    """一个主标签，最多再加一个限定词。"""
    if decision == "WATCH":
        if reason_code == "soft_risk_bounce":
            return "软利空 · 反弹区间"
        if reason_code == "l1_positive":
            return "L1 利好 · 观察"
        return "观察"
    if decision == "INFO":
        # 研究结论：利好没有买入格；软/泛利空不在反弹区间时只观察。
        if category in ("硬利好", "治理利好", "泛利好"):
            return "利好 · 不追"
        if category == "硬利空":
            return "硬利空 · 信息"
        if category == "软利空":
            return "软利空 · 观察"
        if category == "泛利空":
            return "利空 · 观察"
    return PRIMARY_LABELS.get(decision, "信息")


def compute_weight(
    *,
    decision: str,
    tier: str,
    category: str,
    risk_level: str,
    layer: int,
    is_dup: bool,
    aggregator: bool,
    source_quality_value: float | None,
    speculative: bool,
    published_at: datetime | None,
    now: datetime | None = None,
    non_event: bool = False,
) -> int:
    """研究驱动的 0..100 权重；单调性见单测。"""
    weight = _BASE_WEIGHT.get(decision, _BASE_WEIGHT["INFO"])
    weight += _TIER_BONUS.get(tier, 0)
    weight += _CATEGORY_BONUS.get(category, 0)
    if risk_level == "critical":
        weight += 10
    elif risk_level == "high":
        weight += 5
    if layer <= 0:
        weight += 4
    elif layer == 1:
        weight += 0
    elif layer == 2:
        weight -= 6
    else:
        weight -= 18
    if is_dup:
        weight -= 12
    if aggregator:
        weight -= 25
    if source_quality_value is not None:
        weight += int(round(max(-1.0, min(1.0, source_quality_value)) * 8))
    if speculative:
        weight -= 8
    if non_event:
        weight -= 10

    pub = _as_utc(published_at)
    if pub is not None:
        ref = _as_utc(now) or datetime.now(timezone.utc)
        age_days = max(0.0, (ref - pub).total_seconds() / 86400.0)
        if age_days <= 1:
            weight += 6
        elif age_days <= 3:
            weight += 3
        elif age_days > 7:
            weight -= 8

    return max(0, min(100, weight))


@dataclass(frozen=True, slots=True)
class RankedArticle:
    weight: int
    importance: str
    decision: str
    action: str
    primary_label: str
    category: str
    direction: str
    event_type: str
    tier: str
    is_subject: bool
    risk_level: str
    speculative: bool
    layer: int
    is_dup: bool
    aggregator: bool
    source_quality: float | None
    pre5: float | None
    pre5_bucket: str
    is_research: bool = False
    is_roundup: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "weight": self.weight,
            "importance": self.importance,
            "decision": self.decision,
            "action": self.action,
            "primary_label": self.primary_label,
            "category": self.category,
            "direction": self.direction,
            "event_type": self.event_type,
            "tier": self.tier,
            "is_subject": self.is_subject,
            "risk_level": self.risk_level,
            "speculative": self.speculative,
            "layer": self.layer,
            "is_dup": self.is_dup,
            "aggregator": self.aggregator,
            "source_quality": self.source_quality,
            "pre5": self.pre5,
            "pre5_bucket": self.pre5_bucket,
            "is_research": self.is_research,
            "is_roundup": self.is_roundup,
            "reasons": list(self.reasons),
        }


def _reason_lines(
    *,
    decision: str,
    tier: str,
    layer: int,
    pre5_bucket: str,
    source: str,
    speculative: bool,
    is_dup: bool,
    aggregator: bool,
    research: bool = False,
    roundup: bool = False,
) -> tuple[str, ...]:
    out: list[str] = []
    if decision in ("P0", "P1", "P2"):
        out.append(f"{PRIMARY_LABELS[decision]} · pre5 {pre5_bucket}")
    if tier in ("L1", "L2"):
        out.append("代码命中" if tier == "L1" else "标题主体")
    if layer <= 0:
        out.append("首发")
    elif layer >= 3:
        out.append(f"转载第 {layer} 层（降权）")
    else:
        out.append(f"转载第 {layer} 层")
    if is_dup:
        out.append("同题重复")
    if aggregator:
        out.append("聚合页（降权）")
    if speculative:
        out.append("投机稿")
    if research:
        out.append("券商研报（不计个股事件）")
    if roundup:
        out.append("汇总稿（不计个股事件）")
    if source:
        out.append(f"来源 {source}")
    return tuple(out[:5])


def rank_article(
    *,
    title: str,
    code: str,
    name: str = "",
    event_tags: Iterable[str] | None = None,
    sentiment_label: str | None = None,
    source: str | None = None,
    source_domain: str | None = None,
    published_at: datetime | None = None,
    pre5: float | None = None,
    layer: int = 0,
    is_dup: bool = False,
    now: datetime | None = None,
    force_mention: bool = False,
) -> RankedArticle:
    """单条（文章 × 股票）→ 决策等级 + 权重 + 主标签 + 依据。"""
    tags = list(event_tags or [])
    tier = tier_for(title, code, name) if code else "L4"
    research = is_research_report(title)
    roundup = is_roundup(title)
    if research or roundup:
        # 券商研报的作者不是被报道主体；汇总稿的事件标签也无法归到单只票。
        category, direction = "中性", "中性"
        event_type = "研报" if research else "汇总"
    else:
        category, direction, event_type = classify_event(title, tags, sentiment_label or "")
        # 事件必须能落到「这只票」：标题里有该票代码/全名，且事件词在标题里。
        # 否则就是「公告集锦/板块复盘」这类多票稿件——研究口径里不得据此判个股利空。
        if category != "中性" and not (
            tier in ("L1", "L2") and validate_title(title, code, name, category)
        ):
            category, direction, event_type = "中性", "中性", "提及"
    if force_mention:
        category, direction, event_type = "中性", "中性", "提及"
    risk_level = risk_level_for(category)
    speculative = is_speculative(title, tags) and not research and not roundup
    sq = source_quality(source or "") if source else None
    aggregator = is_aggregator_domain(source_domain)

    decision = decide(
        {
            "code": code,
            "name": name,
            "tier": tier,
            "category": category,
            "direction": direction,
            "pre5": pre5,
            "layer": layer,
            "source_quality": sq,
        }
    )

    weight = compute_weight(
        decision=decision.decision.value,
        tier=tier,
        category=category,
        risk_level=risk_level,
        layer=layer,
        is_dup=is_dup,
        aggregator=aggregator,
        source_quality_value=sq,
        speculative=speculative,
        published_at=published_at,
        now=now,
        non_event=research or roundup,
    )

    return RankedArticle(
        weight=weight,
        importance=importance_for(weight),
        decision=decision.decision.value,
        action=decision.action.value,
        primary_label=primary_label(
            decision.decision.value, category=category, reason_code=decision.reason_code
        ),
        category=category,
        direction=direction,
        event_type=event_type,
        tier=tier,
        is_subject=(not force_mention) and tier in ("L1", "L2"),
        risk_level=risk_level,
        speculative=speculative,
        layer=layer,
        is_dup=is_dup,
        aggregator=aggregator,
        source_quality=sq,
        pre5=pre5,
        pre5_bucket=decision.pre5_bucket,
        is_research=research,
        is_roundup=roundup,
        reasons=_reason_lines(
            decision=decision.decision.value,
            tier=tier,
            layer=layer,
            pre5_bucket=decision.pre5_bucket,
            source=str(source or ""),
            speculative=speculative,
            is_dup=is_dup,
            aggregator=aggregator,
            research=research,
            roundup=roundup,
        ),
    )


def rank_articles(
    rows: Sequence[Mapping[str, object]],
    *,
    code: str,
    name: str = "",
    pre5: float | None = None,
    now: datetime | None = None,
) -> list[RankedArticle]:
    """批量入口；``rows`` 需含 title/event_tags/sentiment_label/source/...。"""
    out: list[RankedArticle] = []
    for row in rows:
        out.append(
            rank_article(
                title=str(row.get("title") or ""),
                code=code,
                name=name,
                event_tags=row.get("event_tags") or (),
                sentiment_label=row.get("sentiment_label"),
                source=row.get("source"),
                source_domain=row.get("source_domain"),
                published_at=row.get("published_at"),
                pre5=pre5,
                layer=int(row.get("layer") or 0),
                is_dup=bool(row.get("is_dup")),
                now=now,
            )
        )
    return out


def rank_sector_article(
    *,
    title: str,
    event_tags: Iterable[str] | None = None,
    sentiment_label: str | None = None,
    source: str | None = None,
    source_domain: str | None = None,
    published_at: datetime | None = None,
    layer: int = 0,
    is_dup: bool = False,
    now: datetime | None = None,
    scope: str = "板块",
) -> RankedArticle:
    """板块级新闻：没有个股 pre5，因此不套 P0/P2 决策卡。

    研究口径（2026-11-18 §7.3）：
    - 板块利好发布前 30 分钟已涨 18~21bp，发布后无延续 → **不追**；
    - 板块利空（尤其概念板块）发布后继续发酵（post5 -2.3bp / post30 -7.3bp）。
    """
    tags = list(event_tags or [])
    research = is_research_report(title)
    roundup = is_roundup(title)
    if research or roundup:
        category, direction = "中性", "中性"
        event_type = "研报" if research else "汇总"
    else:
        # 板块级不按标题正则判方向：标题里的「债务/监管」常说的是某家公司或海外，
        # 不是这个板块；有 enrichment 事件标签就用标签，没有就用文章情绪。
        category, direction, event_type = classify_event("", tags, sentiment_label or "")
    risk_level = risk_level_for(category)
    speculative = is_speculative(title, tags) and not research and not roundup
    sq = source_quality(source or "") if source else None
    aggregator = is_aggregator_domain(source_domain)

    if direction == "利空":
        decision = "WATCH"
        if category == "硬利空":
            label = f"{scope}利空 · 发酵"
            extra = 10
        else:
            # 只有情绪偏空、没有硬事件：研究里没有「情绪利空继续发酵」的证据。
            label = f"{scope}偏空 · 观察"
            extra = 4
    elif direction == "利好":
        decision = "WATCH"
        label = f"{scope}利好 · 不追"
        extra = -5
    else:
        decision = "INFO"
        label = f"{scope} · 信息"
        extra = 0

    weight = compute_weight(
        decision=decision,
        tier="L3",
        category=category,
        risk_level=risk_level,
        layer=layer,
        is_dup=is_dup,
        aggregator=aggregator,
        source_quality_value=sq,
        speculative=speculative,
        published_at=published_at,
        now=now,
        non_event=research or roundup,
    )
    weight = max(0, min(100, weight + extra))

    return RankedArticle(
        weight=weight,
        importance=importance_for(weight),
        decision=decision,
        action="watch" if decision == "WATCH" else "none",
        primary_label=label,
        category=category,
        direction=direction,
        event_type=event_type,
        tier="L3",
        is_subject=False,
        risk_level=risk_level,
        speculative=speculative,
        layer=layer,
        is_dup=is_dup,
        aggregator=aggregator,
        source_quality=sq,
        pre5=None,
        pre5_bucket="NA",
        is_research=research,
        is_roundup=roundup,
        reasons=(
            label,
            "首发" if layer <= 0 else f"转载第 {layer} 层",
            *(("投机稿",) if speculative else ()),
            *(("券商研报（不计个股事件）",) if research else ()),
            *(("汇总稿（不计个股事件）",) if roundup else ()),
        ),
    )


def assign_echo_layers(rows: Sequence[dict[str, object]], *, gap_hours: float = 24.0,
                       dup_hours: float = 6.0) -> None:
    """就地写入 ``layer`` / ``is_dup``（同一归一标题按发布时间排序）。

    ``rows`` 必须已按发布时间升序；``published_at`` 为 aware datetime。
    与 ``data/news_intel/20261118/scripts/common_next.py::cluster_titles`` 同口径。
    """
    import re

    def norm_title(text: object) -> str:
        t = str(text or "")
        t = re.sub(r"\s*[-—_|｜]\s*[^-—_|｜]{1,16}\s*$", "", t)
        return re.sub(r"[\s，。！？：；、“”‘’（）()《》〈〉【】\[\]…\.\,\!\?\"\-—_|｜/\\]+", "", t)

    last_seen: dict[str, datetime] = {}
    first_pub: dict[str, datetime] = {}
    cluster_layer: dict[str, int] = {}
    for row in rows:
        key = norm_title(row.get("title"))
        pub = row.get("published_at")
        if not isinstance(pub, datetime):
            row["layer"] = 0
            row["is_dup"] = False
            continue
        prev = last_seen.get(key)
        if prev is None or (pub - prev) > timedelta(hours=gap_hours):
            layer = 0
            first_pub[key] = pub
            cluster_layer[key] = 0
        else:
            layer = cluster_layer.get(key, 0) + 1
            cluster_layer[key] = layer
        row["layer"] = layer
        row["is_dup"] = bool(
            layer > 0 and (pub - first_pub.get(key, pub)) <= timedelta(hours=dup_hours)
        )
        last_seen[key] = pub
