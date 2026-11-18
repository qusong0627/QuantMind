"""把 ``news_brief.latest.json`` 的 holdings/watch 转成新闻决策。

纯函数，无 IO；pre5 由调用方注入。LLM 只负责叙事，决策在这里生成。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.shared.news_decision import NewsDecision, decide, risk_overlay
from backend.shared.news_event_rules import source_quality, tier_for
from backend.shared.news_pre5 import pre5_lookup

CN_TZ = timezone(timedelta(hours=8))


def _dt(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(CN_TZ)


def _category(event_type: str, verdict: str) -> tuple[str, str]:
    et = str(event_type or "")
    vd = str(verdict or "")
    if et in ("问询处罚", "监管处罚"):
        return "硬利空", "利空"
    if et in ("减持", "解禁", "质押", "诉讼", "舆情"):
        return "软利空", "利空"
    if et == "业绩":
        return ("软利空", "利空") if vd == "利空" else ("硬利好", "利好")
    if et in ("回购增持", "中标订单", "涨价", "并购重组"):
        return "硬利好", "利好"
    if et == "政策监管":
        return ("硬利空", "利空") if vd == "利空" else ("中性", "中性")
    if et in ("涨停", "大涨异动"):
        return "泛利好", "利好"
    if et in ("跌停", "大跌异动"):
        return "泛利空", "利空"
    if et == "股东变动":
        return ("软利空", "利空") if vd == "利空" else ("中性", "中性")
    if vd == "利好":
        return "泛利好", "利好"
    if vd == "利空":
        return "泛利空", "利空"
    return "中性", "中性"


def _expire_at(trade_date: str) -> str:
    day = datetime.strptime(str(trade_date)[:8], "%Y%m%d")
    return (day + timedelta(days=1)).replace(hour=9, minute=30, second=0,
                                             microsecond=0).isoformat(timespec="seconds")


def _spec_count(events: list[Mapping[str, Any]], code: str) -> int:
    n = 0
    for e in events:
        if not isinstance(e, Mapping):
            continue
        if code not in [str(x) for x in (e.get("tickers") or [])]:
            continue
        if str(e.get("event_type") or "") in ("涨停", "大涨异动", "连板", "首板"):
            n += 1
    return n


def build_decisions(
    brief: Mapping[str, Any],
    pre5_map: Mapping[str, float],
    *,
    trade_date: str,
    now: str | None = None,
) -> tuple[list[NewsDecision], dict[str, dict[str, Any]]]:
    """holdings + watch_list → 逐票决策与 risk overlay。

    同一代码只保留一条：holdings 优先，watch_list 补缺。
    """
    now_dt = _dt(now or datetime.now(CN_TZ).isoformat())
    brief_ts = _dt(brief.get("ts") or now_dt.isoformat())
    age_h = max(0.0, (now_dt - brief_ts).total_seconds()) / 3600.0

    micro = ((brief.get("segments") or {}).get("news-micro") or {})
    events = micro.get("events") if isinstance(micro, Mapping) else []
    events = [e for e in (events or []) if isinstance(e, Mapping)]

    rows: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in (brief.get("holdings") or []):
        if isinstance(item, Mapping) and item.get("code"):
            code = str(item["code"])
            if code not in seen:
                seen.add(code)
                rows.append(item)
    for item in (brief.get("watch_list") or []):
        if isinstance(item, Mapping) and item.get("code"):
            code = str(item["code"])
            if code not in seen:
                seen.add(code)
                rows.append(item)

    decisions: list[NewsDecision] = []
    risk: dict[str, dict[str, Any]] = {}
    for item in rows:
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        verdict = str(item.get("verdict") or "")
        event_type = str(item.get("event_type") or item.get("logic") or "")
        headline = str(item.get("headline") or item.get("trigger") or item.get("logic") or "")
        category, direction = _category(event_type, verdict)
        tier = tier_for(headline, code, name)
        if tier not in ("L1", "L2"):
            tier = "L2"
        pre5 = pre5_lookup(code, dict(pre5_map))
        n_spec = _spec_count(events, code)
        event = {
            "decision_id": f"{trade_date}-{code}-brief-{event_type or 'unknown'}",
            "code": code,
            "name": name,
            "as_of": brief_ts.isoformat(timespec="seconds"),
            "first_seen": brief_ts.isoformat(timespec="seconds"),
            "expire_at": _expire_at(trade_date),
            "tier": tier,
            "event_type": event_type,
            "category": category,
            "direction": direction,
            "pre5": pre5,
            "session": "盘中",
            "cluster_size": 1,
            "layer": 0,
            "echo_count": 0,
            "source": str(item.get("source") or ""),
            "source_tier": "",
            "source_quality": source_quality(str(item.get("source") or "")),
            "n_speculative": n_spec,
            "n_articles": 1 + n_spec,
            "n_sources": 1,
            "source_hhi": 1.0,
            "stale": age_h > 4.0,
        }
        d = decide(event)
        decisions.append(d)
        risk[code] = risk_overlay(event)

    if decisions:
        n = len(decisions)
        order = sorted(range(n), key=lambda i: decisions[i].n_speculative)
        if n >= 5:
            for rank, i in enumerate(order):
                risk[decisions[i].code]["n_spec_pct"] = rank / (n - 1)
                high = rank / (n - 1) >= 0.8
                risk[decisions[i].code]["position_hint"] = "de_risk" if high else "normal"
                risk[decisions[i].code]["stop_hint"] = "wider" if high else "normal"
    return decisions, risk


def merge_into_brief(
    brief: dict[str, Any],
    pre5_map: Mapping[str, float],
    *,
    trade_date: str,
    now: str | None = None,
) -> dict[str, Any]:
    """原地补齐 latest.json 字段；返回同一个 dict（调用方负责原子落盘）。"""
    decisions, risk = build_decisions(
        brief, pre5_map, trade_date=trade_date, now=now
    )
    brief["schema_version"] = "news_brief.v2"
    brief["as_of"] = _dt(now or datetime.now(CN_TZ).isoformat()).isoformat(
        timespec="seconds"
    )
    brief["decisions"] = [d.to_dict() for d in decisions]
    brief["risk_overlay"] = risk
    missing: list[str] = []
    if not pre5_map:
        missing.append("pre5")
    brief["data_quality"] = {
        "stale": any(d.data_quality.get("stale") for d in decisions),
        "missing": missing,
        "n_decisions": len(decisions),
    }
    return brief
