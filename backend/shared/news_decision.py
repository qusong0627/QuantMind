"""新闻决策信封（P0 阶段）：把研究结论变成确定性 P0/P1/P2/WATCH/INFO。

设计纪律：
- 纯函数、无 IO；调用方注入 event / stock_day；
- decision 只由这里的规则生成，LLM 不得改；
- 新闻层只出风险/观察标记，不产生买入信号；
- 缺失 pre5 一律 INFO，绝不猜。

与 ``docs/新闻源智能体_设计与数据流_20261118.md`` /
``docs/新闻源智能体_实施计划_P0-P4_20261118.md`` 对齐。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

SCHEMA_VERSION = "news_decision.v1"


class Decision(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    WATCH = "WATCH"
    INFO = "INFO"


class Action(str, Enum):
    REDUCE = "reduce"
    WEAK_REDUCE = "weak_reduce"
    NO_CHASE = "no_chase"
    WATCH = "watch"
    NONE = "none"


_DECISION_VALUES = {d.value for d in Decision}
_ACTION_VALUES = {a.value for a in Action}
_TIERS = {"L1", "L2", "L3", "L4"}

_REQUIRED = (
    "decision_id",
    "code",
    "as_of",
    "first_seen",
    "expire_at",
    "tier",
    "event_type",
    "category",
    "direction",
    "pre5",
    "decision",
    "action",
    "confidence",
    "reason",
    "data_quality",
)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class NewsDecision:
    decision_id: str
    code: str
    name: str
    as_of: str
    first_seen: str
    expire_at: str
    tier: str
    event_type: str
    category: str
    direction: str
    pre5: float | None
    pre5_bucket: str
    session: str
    cluster_size: int
    layer: int
    echo_count: int
    source: str
    source_tier: str
    source_quality: float | None
    n_speculative: int
    n_articles: int
    n_sources: int
    source_hhi: float | None
    decision: Decision
    action: Action
    confidence: float
    reason: str
    reason_code: str
    no_add: bool
    data_quality: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "code": self.code,
            "name": self.name,
            "as_of": self.as_of,
            "first_seen": self.first_seen,
            "expire_at": self.expire_at,
            "tier": self.tier,
            "event_type": self.event_type,
            "category": self.category,
            "direction": self.direction,
            "pre5": self.pre5,
            "pre5_bucket": self.pre5_bucket,
            "session": self.session,
            "cluster_size": self.cluster_size,
            "layer": self.layer,
            "echo_count": self.echo_count,
            "source": self.source,
            "source_tier": self.source_tier,
            "source_quality": self.source_quality,
            "n_speculative": self.n_speculative,
            "n_articles": self.n_articles,
            "n_sources": self.n_sources,
            "source_hhi": self.source_hhi,
            "decision": self.decision.value,
            "action": self.action.value,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "reason_code": self.reason_code,
            "no_add": self.no_add,
            "data_quality": dict(self.data_quality),
        }


def _pre5_bucket(pre5: float | None) -> str:
    if pre5 is None:
        return "NA"
    if pre5 < -0.02:
        return "<-2%"
    if pre5 < 0:
        return "-2~0%"
    if pre5 < 0.02:
        return "0~2%"
    if pre5 <= 0.05:
        return "2~5%"
    return ">5%"


def _base_confidence(decision: Decision) -> float:
    return {
        Decision.P0: 0.78,
        Decision.P1: 0.55,
        Decision.P2: 0.62,
        Decision.WATCH: 0.50,
        Decision.INFO: 0.30,
    }[decision]


def decide(event: Mapping[str, Any]) -> NewsDecision:
    """确定性决策：唯一入口。

    event 字段见 ``docs/新闻源智能体_设计与数据流_20261118.md`` 的决策信封。
    缺失 pre5 → INFO，并在 data_quality.missing 标注。
    """
    pre5 = _f(event.get("pre5"))
    missing: list[str] = []
    if pre5 is None:
        missing.append("pre5")

    category = _s(event.get("category"))
    direction = _s(event.get("direction"))
    tier = _s(event.get("tier")) or "L2"
    layer = int(_f(event.get("layer")) or 0)

    if category == "硬利空" and pre5 is not None and pre5 > 0.05:
        decision, action = Decision.P0, Action.REDUCE
        reason_code = "hard_risk_high_pre5"
        reason = "硬利空+pre5>5%；P0 回避/减仓，T+1 开盘卖；不抄底"
    elif category == "硬利空" and pre5 is not None and 0.02 < pre5 <= 0.05:
        decision, action = Decision.P1, Action.WEAK_REDUCE
        reason_code = "hard_risk_mid_pre5"
        reason = "硬利空+pre5 2~5%；P1 弱减仓（p=0.106，只观察）"
    elif category in ("硬利好", "泛利好") and pre5 is not None and pre5 > 0.05:
        decision, action = Decision.P2, Action.NO_CHASE
        reason_code = "positive_high_pre5"
        reason = "利好+pre5>5%；P2 不追/不加仓（不是卖出信号）"
    elif category == "软利空" and pre5 is not None and -0.02 <= pre5 <= 0.05:
        decision, action = Decision.WATCH, Action.WATCH
        reason_code = "soft_risk_bounce"
        reason = "软利空+pre5 -2~5%；WATCH 不追空，不做买入"
    elif tier == "L1" and direction == "利好":
        decision, action = Decision.WATCH, Action.WATCH
        reason_code = "l1_positive"
        reason = "L1 代码利好；WATCH 观察池，非策略"
    else:
        decision, action = Decision.INFO, Action.NONE
        reason_code = "no_action"
        reason = "无动作"

    sq = _f(event.get("source_quality"))
    confidence = _base_confidence(decision)
    if sq is not None:
        confidence += 0.05 * max(-1.0, min(1.0, sq))
    confidence = max(0.0, min(1.0, confidence))

    no_add = decision in (Decision.P0, Decision.P2) or layer >= 3
    data_quality = {
        "stale": bool(event.get("stale", False)),
        "missing": missing,
    }

    return NewsDecision(
        decision_id=_s(event.get("decision_id"))
        or f"{_s(event.get('code'))}-{_s(event.get('as_of'))}-{_s(event.get('event_type'))}",
        code=_s(event.get("code")),
        name=_s(event.get("name")),
        as_of=_s(event.get("as_of")),
        first_seen=_s(event.get("first_seen")),
        expire_at=_s(event.get("expire_at")),
        tier=tier,
        event_type=_s(event.get("event_type")),
        category=category,
        direction=direction,
        pre5=pre5,
        pre5_bucket=_pre5_bucket(pre5),
        session=_s(event.get("session")),
        cluster_size=int(_f(event.get("cluster_size")) or 0),
        layer=layer,
        echo_count=int(_f(event.get("echo_count")) or 0),
        source=_s(event.get("source")),
        source_tier=_s(event.get("source_tier")),
        source_quality=sq,
        n_speculative=int(_f(event.get("n_speculative")) or 0),
        n_articles=int(_f(event.get("n_articles")) or 0),
        n_sources=int(_f(event.get("n_sources")) or 0),
        source_hhi=_f(event.get("source_hhi")),
        decision=decision,
        action=action,
        confidence=confidence,
        reason=reason,
        reason_code=reason_code,
        no_add=no_add,
        data_quality=data_quality,
    )


def risk_overlay(stock_day: Mapping[str, Any]) -> dict[str, Any]:
    """个股/市场风险 overlay；只做仓位/止损提示，不改 decision。"""
    pct = _f(stock_day.get("n_spec_pct"))
    high = pct is not None and pct >= 0.8
    return {
        "n_speculative": int(_f(stock_day.get("n_speculative")) or 0),
        "n_articles": int(_f(stock_day.get("n_articles")) or 0),
        "n_sources": int(_f(stock_day.get("n_sources")) or 0),
        "source_hhi": _f(stock_day.get("source_hhi")),
        "echo_share": _f(stock_day.get("echo_share")),
        "n_spec_pct": pct,
        "position_hint": "de_risk" if high else "normal",
        "stop_hint": "wider" if high else "normal",
    }


def _parse_iso(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def validate_decision(doc: Mapping[str, Any], *, now: str | None = None) -> None:
    """校验决策信封；坏值 raise ValueError（调用方决定降级/丢弃）。"""
    for key in _REQUIRED:
        if key not in doc:
            raise ValueError(f"missing field: {key}")
    if doc["decision"] not in _DECISION_VALUES:
        raise ValueError(f"bad decision: {doc['decision']}")
    if doc["action"] not in _ACTION_VALUES:
        raise ValueError(f"bad action: {doc['action']}")
    if doc["tier"] not in _TIERS:
        raise ValueError(f"bad tier: {doc['tier']}")
    if not _s(doc["code"]):
        raise ValueError("empty code")
    conf = _f(doc["confidence"])
    if conf is None or not (0.0 <= conf <= 1.0):
        raise ValueError("confidence out of range")
    as_of = _parse_iso(doc["as_of"])
    first_seen = _parse_iso(doc["first_seen"])
    expire_at = _parse_iso(doc["expire_at"])
    if first_seen > as_of:
        raise ValueError("first_seen after as_of")
    if expire_at <= as_of:
        raise ValueError("expire_at not after as_of")
    if now is not None:
        current = _parse_iso(now)
        if as_of > current:
            raise ValueError("as_of in the future")
        if first_seen > current:
            raise ValueError("first_seen in the future")


def to_envelope(
    decisions: Sequence[NewsDecision],
    *,
    as_of: str,
    trade_date: str,
    market_risk: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "trade_date": trade_date,
        "items": [d.to_dict() for d in decisions],
        "risk_overlay": dict(market_risk or {}),
    }
