"""新闻决策 schema / 规则单测（P0 阶段）。"""

from __future__ import annotations

import pytest

from backend.shared.news_decision import (
    Action,
    Decision,
    decide,
    risk_overlay,
    to_envelope,
    validate_decision,
)


def _event(
    *,
    category: str = "硬利空",
    tier: str = "L2",
    direction: str = "利空",
    pre5: float | None = 0.06,
    layer: int = 0,
):
    return {
        "decision_id": "20261118-600519.SH-1",
        "code": "600519.SH",
        "name": "贵州茅台",
        "as_of": "2026-11-18T10:05:00+08:00",
        "first_seen": "2026-11-18T10:02:13+08:00",
        "expire_at": "2026-11-19T09:30:00+08:00",
        "tier": tier,
        "event_type": "监管处罚",
        "category": category,
        "direction": direction,
        "pre5": pre5,
        "session": "早盘",
        "cluster_size": 3,
        "layer": layer,
        "echo_count": 2,
        "source": "财联社",
        "source_tier": "S",
        "source_quality": -0.42,
        "n_speculative": 2,
        "n_articles": 3,
        "n_sources": 2,
        "source_hhi": 0.56,
    }


def test_hard_risk_high_pre5_is_p0():
    d = decide(_event(pre5=0.072))
    assert d.decision is Decision.P0
    assert d.action is Action.REDUCE
    assert d.reason_code == "hard_risk_high_pre5"
    assert d.no_add is True


def test_five_percent_boundary_goes_to_p1_not_p0():
    d = decide(_event(pre5=0.05))
    assert d.decision is Decision.P1
    assert d.action is Action.WEAK_REDUCE


def test_hard_risk_mid_pre5_is_p1():
    d = decide(_event(pre5=0.03))
    assert d.decision is Decision.P1
    assert d.reason_code == "hard_risk_mid_pre5"


def test_low_pre5_hard_risk_is_info():
    d = decide(_event(pre5=0.01))
    assert d.decision is Decision.INFO
    assert d.action is Action.NONE


def test_positive_high_pre5_is_p2_no_chase():
    d = decide(_event(category="硬利好", direction="利好", pre5=0.06))
    assert d.decision is Decision.P2
    assert d.action is Action.NO_CHASE
    assert d.no_add is True


def test_soft_risk_in_band_is_watch():
    d = decide(_event(category="软利空", direction="利空", pre5=0.0))
    assert d.decision is Decision.WATCH
    assert d.action is Action.WATCH


def test_soft_risk_high_pre5_is_info_not_watch():
    d = decide(_event(category="软利空", direction="利空", pre5=0.08))
    assert d.decision is Decision.INFO


def test_l1_positive_is_watch():
    d = decide(_event(category="利好", direction="利好", tier="L1", pre5=0.0))
    assert d.decision is Decision.WATCH
    assert d.reason_code == "l1_positive"


def test_l2_positive_is_not_upgraded_to_l1():
    d = decide(_event(category="利好", direction="利好", tier="L2", pre5=0.0))
    assert d.decision is Decision.INFO


def test_missing_pre5_is_info_and_marked():
    d = decide(_event(pre5=None))
    assert d.decision is Decision.INFO
    assert d.data_quality["missing"] == ["pre5"]


def test_layer3_sets_no_add_even_on_watch():
    d = decide(_event(category="软利空", pre5=0.0, layer=3))
    assert d.decision is Decision.WATCH
    assert d.no_add is True


def test_source_quality_adjusts_confidence_but_not_decision():
    good = decide({**_event(pre5=0.072), "source_quality": 0.8})
    bad = decide({**_event(pre5=0.072), "source_quality": -0.8})
    assert good.decision is Decision.P0 and bad.decision is Decision.P0
    assert good.confidence > bad.confidence


def test_risk_overlay_high_spec_de_risks():
    out = risk_overlay({"n_speculative": 9, "n_spec_pct": 0.92, "n_articles": 12})
    assert out["position_hint"] == "de_risk"
    assert out["stop_hint"] == "wider"


def test_risk_overlay_normal_when_missing_percentile():
    out = risk_overlay({"n_speculative": 2, "n_articles": 3})
    assert out["position_hint"] == "normal"
    assert out["n_spec_pct"] is None


def test_validate_rejects_bad_decision_value():
    d = decide(_event())
    doc = d.to_dict()
    doc["decision"] = "P9"
    with pytest.raises(ValueError):
        validate_decision(doc)


def test_validate_rejects_future_as_of():
    d = decide(_event())
    doc = d.to_dict()
    doc["as_of"] = "2999-01-01T00:00:00+08:00"
    with pytest.raises(ValueError):
        validate_decision(doc, now="2026-11-18T12:00:00+08:00")


def test_envelope_shape():
    d = decide(_event())
    env = to_envelope(
        [d],
        as_of="2026-11-18T10:05:00+08:00",
        trade_date="20261118",
        market_risk={"market_n_spec_pct": 0.72},
    )
    assert env["schema_version"] == "news_decision.v1"
    assert env["trade_date"] == "20261118"
    assert env["items"][0]["decision"] == "P0"
    assert env["items"][0]["action"] == "reduce"
    assert env["risk_overlay"]["market_n_spec_pct"] == 0.72
