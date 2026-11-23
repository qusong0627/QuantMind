"""价格阈值预警判定单测（纯函数，不连 DB/Redis）。"""

from __future__ import annotations

import json

import pytest

from backend.services.trade.services.price_alert import (
    FIRED_PREFIX,
    evaluate_price_alerts,
    parse_config,
)

DAY = "20261123"


def _rule(symbol: str = "601825.SH", below: float = 9.34, note: str = "") -> dict:
    return {"symbol": symbol, "below": below, "note": note}


@pytest.mark.unit
def test_parse_config_accepts_json_string_and_filters_bad_rules():
    raw = json.dumps(
        {
            "enabled": True,
            "rules": [
                {"symbol": "601825.SH", "below": 9.34},
                {"symbol": "", "below": 9.0},
                {"symbol": "600000.SH", "below": 0},
                {"symbol": "600001.SH", "below": "bad"},
            ],
        }
    )
    cfg = parse_config(raw)

    assert cfg["enabled"] is True
    assert [r["symbol"] for r in cfg["rules"]] == ["601825.SH"]


@pytest.mark.unit
def test_parse_config_bad_json_disables_alerts():
    assert parse_config("{not json") == {"enabled": False, "rules": []}
    assert parse_config(None) == {"enabled": False, "rules": []}


@pytest.mark.unit
def test_evaluate_hits_when_price_breaks_below():
    hits = evaluate_price_alerts(
        [_rule()], {"601825.SH": 9.33}, fired=set(), day=DAY
    )

    assert len(hits) == 1
    assert hits[0]["symbol"] == "601825.SH"
    assert hits[0]["price"] == 9.33
    assert hits[0]["fired_key"] == f"{FIRED_PREFIX}:601825.SH:9.34:{DAY}"


@pytest.mark.unit
def test_evaluate_skips_when_price_above_or_missing():
    assert evaluate_price_alerts([_rule()], {"601825.SH": 9.35}, fired=set(), day=DAY) == []
    assert evaluate_price_alerts([_rule()], {}, fired=set(), day=DAY) == []
    assert evaluate_price_alerts([_rule()], {"601825.SH": 0}, fired=set(), day=DAY) == []


@pytest.mark.unit
def test_evaluate_respects_daily_cooldown():
    fired = {f"{FIRED_PREFIX}:601825.SH:9.34:{DAY}"}
    assert evaluate_price_alerts([_rule()], {"601825.SH": 9.30}, fired=fired, day=DAY) == []

    # 换一天（不同冷却键）再次命中
    assert len(
        evaluate_price_alerts(
            [_rule()],
            {"601825.SH": 9.30},
            fired=fired,
            day="20261124",
        )
    ) == 1


@pytest.mark.unit
def test_evaluate_multiple_rules_only_reports_hits():
    rules = [
        _rule("601825.SH", 9.34),
        _rule("601658.SH", 5.60),
    ]
    hits = evaluate_price_alerts(
        rules, {"601825.SH": 9.30, "601658.SH": 5.90}, fired=set(), day=DAY
    )

    assert [h["symbol"] for h in hits] == ["601825.SH"]
