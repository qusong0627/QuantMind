"""板块方向概率查表（/heatmap direction）单元测试。

状态格产物由离线 builder 生成；API 只做「当前状态判定 + 查表 + 大盘先验组合」。
测试锁定三件事：五分桶与产物一致、组合公式与中性带、缺失状态不补 0。
"""

from __future__ import annotations

from backend.services.api.market_analysis import sector_direction as sd


def test_quintile_buckets_match_builder_convention():
    values = {f"s{i}": float(i) for i in range(10)}
    buckets = sd._buckets(values)
    # pandas rank(pct=True) 口径：前 20% = q1，最后 20% = q5
    assert buckets["s0"] == "q1" and buckets["s1"] == "q1"
    assert buckets["s2"] == "q2" and buckets["s3"] == "q2"
    assert buckets["s4"] == "q3" and buckets["s5"] == "q3"
    assert buckets["s6"] == "q4" and buckets["s7"] == "q4"
    assert buckets["s8"] == "q5" and buckets["s9"] == "q5"


def test_buckets_require_enough_sectors():
    assert sd._buckets({"a": 1.0, "b": 2.0, "c": 3.0}) == {}


def _artifact() -> dict:
    def cell(h: int, win: float) -> dict:
        return {
            "h": h,
            "stress": "pressure",
            "flow": "q1",
            "price": "q1",
            "n": 1000,
            "win": win,
            "mean": 0.01,
            "date_win": 0.55,
            "n_dates": 20,
        }

    return {
        "schema": "sector_direction.v1",
        "disclosure": "历史频率不是未来保证",
        "cells": [cell(5, 0.60), cell(10, 0.40), cell(20, 0.50)],
    }


def _payload() -> dict:
    return {
        "trade_date": "2026-10-28",
        "stress": {"score": 77.2, "zone": "pressure", "zone_label": "承压"},
        "indices": {
            "ALL.A": {
                "stats": {
                    "win": 0.537,
                    "median": 0.73,
                    "p10": -7.34,
                    "p90": 9.91,
                    "n_days": 525,
                },
                "forecast": {
                    "5": {"prob_up": 0.55},
                    "10": {"prob_up": 0.50},
                    "20": {"prob_up": 0.45},
                },
            }
        },
    }


def test_attach_combines_market_prior_and_relative_probability(monkeypatch):
    monkeypatch.setattr(sd, "load_artifact", lambda: _artifact())
    monkeypatch.setattr(sd._sentiment, "load_sentiment", lambda: _payload())
    monkeypatch.setattr(
        sd, "_state_maps", lambda: ({"A": "q1", "B": "q1"}, {"A": "q1", "B": "q1"})
    )

    items = [{"name": "A", "value": 100}, {"name": "C", "value": 80}]
    out = sd.attach(items)
    out_items = out["items"]
    assert out_items is not items and out_items[0] is not items[0]

    market = out["market"]
    assert market["stress_zone"] == "pressure"
    assert market["market_prob"] == {"5": 0.55, "10": 0.5, "20": 0.45}

    a = out_items[0]["direction"]
    assert a is not None
    assert a["5"]["prob"] == 0.65 and a["5"]["label"] == "up"
    assert a["10"]["prob"] == 0.40 and a["10"]["label"] == "down"
    # 0.45 恰好落在中性带下沿：<=0.45 视为偏空，不冒充中性
    assert a["20"]["prob"] == 0.45 and a["20"]["label"] == "down"
    assert a["5"]["rel_prob"] == 0.60 and a["5"]["market_prob"] == 0.55

    # 没有状态格的板块不补方向，不补 0
    assert out_items[1]["direction"] is None


def test_attach_skips_historical_snapshot(monkeypatch):
    monkeypatch.setattr(sd, "load_artifact", lambda: _artifact())
    items = [{"name": "A"}]
    out = sd.attach(items, date="2026-10-20")
    assert out["market"] is None
    assert out["items"][0].get("direction") is None


def test_attach_degrades_without_sentiment(monkeypatch):
    monkeypatch.setattr(sd, "load_artifact", lambda: _artifact())
    monkeypatch.setattr(
        sd._sentiment,
        "load_sentiment",
        lambda: (_ for _ in ()).throw(RuntimeError("no snapshot")),
    )
    items = [{"name": "A"}]
    out = sd.attach(items)
    assert out["market"] is None
    assert out["items"][0].get("direction") is None


def test_market_block_available_without_direction(monkeypatch):
    monkeypatch.setattr(sd._sentiment, "load_sentiment", lambda: _payload())
    block = sd.market()
    assert block is not None
    assert block["available"] is False
    assert block["stress_zone"] == "pressure"
    assert block["market_prob"]["5"] == 0.55
    assert sd.market("2026-10-20") is None


def test_zone_mapping_from_sentiment_payload():
    assert sd._zone_of({"stress": {"zone": "extreme_pressure"}}) == "extreme"
    assert sd._zone_of({"stress": {"zone": "pressure"}}) == "pressure"
    assert sd._zone_of({"stress": {"zone": "overheat"}}) == "overheat"
    assert sd._zone_of({}) == "neutral"
