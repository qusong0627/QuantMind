"""news_brief → decisions 纯函数单测。"""

from __future__ import annotations

from backend.shared.news_brief_decisions import build_decisions, merge_into_brief


def _brief():
    return {
        "ts": "2026-11-18T10:05:00+08:00",
        "holdings": [
            {
                "code": "600519.SH",
                "name": "贵州茅台",
                "verdict": "利空",
                "impact": 2,
                "event_type": "问询处罚",
                "headline": "贵州茅台被立案调查",
                "source": "财联社",
            },
            {
                "code": "300750.SZ",
                "name": "宁德时代",
                "verdict": "利好",
                "impact": 2,
                "event_type": "回购增持",
                "headline": "宁德时代拟回购",
                "source": "新浪财经",
            },
        ],
        "watch_list": [
            {
                "code": "000001.SZ",
                "name": "平安银行",
                "logic": "业绩承压",
                "trigger": "跌破均线",
            }
        ],
        "segments": {
            "news-micro": {
                "events": [
                    {
                        "tickers": ["600519.SH"],
                        "name": "贵州茅台",
                        "event_type": "涨停",
                        "sentiment": 0.8,
                    }
                ]
            }
        },
    }


def test_hard_risk_high_pre5_is_p0():
    decisions, _risk = build_decisions(
        _brief(),
        {"600519.SH": 0.07, "300750.SZ": 0.06, "000001.SZ": 0.03},
        trade_date="20261118",
        now="2026-11-18T10:06:00+08:00",
    )
    by_code = {d.code: d for d in decisions}
    assert by_code["600519.SH"].decision.value == "P0"
    assert by_code["600519.SH"].no_add is True


def test_positive_high_pre5_is_p2():
    decisions, _risk = build_decisions(
        _brief(),
        {"600519.SH": 0.07, "300750.SZ": 0.06, "000001.SZ": 0.03},
        trade_date="20261118",
        now="2026-11-18T10:06:00+08:00",
    )
    by_code = {d.code: d for d in decisions}
    assert by_code["300750.SZ"].decision.value == "P2"


def test_missing_pre5_is_info_with_missing_flag():
    decisions, _risk = build_decisions(
        _brief(),
        {"300750.SZ": 0.06},
        trade_date="20261118",
        now="2026-11-18T10:06:00+08:00",
    )
    by_code = {d.code: d for d in decisions}
    assert by_code["600519.SH"].decision.value == "INFO"
    assert "pre5" in by_code["600519.SH"].data_quality["missing"]


def test_risk_overlay_has_counts():
    _decisions, risk = build_decisions(
        _brief(),
        {"600519.SH": 0.07, "300750.SZ": 0.06, "000001.SZ": 0.03},
        trade_date="20261118",
        now="2026-11-18T10:06:00+08:00",
    )
    assert "600519.SH" in risk
    assert risk["600519.SH"]["n_speculative"] >= 1


def test_decisions_are_unique_by_code():
    decisions, _risk = build_decisions(
        _brief(),
        {"600519.SH": 0.07, "300750.SZ": 0.06, "000001.SZ": 0.03},
        trade_date="20261118",
        now="2026-11-18T10:06:00+08:00",
    )
    codes = [d.code for d in decisions]
    assert len(codes) == len(set(codes))


def test_merge_into_brief_adds_v2_fields():
    brief = _brief()
    out = merge_into_brief(
        brief,
        {"600519.SH": 0.07, "300750.SZ": 0.06, "000001.SZ": 0.03},
        trade_date="20261118",
        now="2026-11-18T10:06:00+08:00",
    )
    assert out["schema_version"] == "news_brief.v2"
    assert out["decisions"]
    assert out["risk_overlay"]
    assert out["data_quality"]["n_decisions"] == len(out["decisions"])

