"""Redis 序列行情解析单测（纯函数，不依赖网络）。"""

from __future__ import annotations

import json

from backend.shared.freshness import FreshnessPolicy
from backend.services.simulation.services.redis_series_quote import (
    parse_series_member,
    series_key_for,
)

_POLICY = FreshnessPolicy(fresh_within_s=60, stale_within_s=300)  # 与默认口径一致


def test_series_key_uses_prefix_format():
    assert series_key_for("600036.SH") == "market:series:SH600036"
    assert series_key_for("SH600036") == "market:series:SH600036"
    assert series_key_for("0700.HK") == "market:series:0700.HK"
    assert series_key_for("HK00700") == "market:series:HK00700"
    assert series_key_for("AAPL") == "market:series:AAPL"
    assert series_key_for("not-a-code-!!!") is None


def test_parse_fresh_tick():
    member = json.dumps(
        {"price": 40.9, "open": 40.5, "source": "remote_redis"},
        ensure_ascii=False,
    )
    tick = parse_series_member(member, 1_000_000.0, 1_000_100.0, _POLICY)
    assert tick is not None
    assert tick["price"] == 40.9
    assert tick["age_s"] == 100.0
    assert tick["open"] == 40.5
    assert tick["freshness"] == "stale"  # 100s：超 fresh(60) 未超 stale(300)，可用须标注


def test_parse_stale_or_bad_tick_returns_none():
    member = json.dumps({"price": 40.9})
    # 超龄（unavailable）
    assert parse_series_member(member, 1_000_000.0, 1_000_400.0, _POLICY) is None
    # 零价格
    assert parse_series_member(json.dumps({"price": 0}), 1_000_000.0, 1_000_100.0, _POLICY) is None
    # 非法 JSON
    assert parse_series_member("not-json", 1_000_000.0, 1_000_100.0, _POLICY) is None
    # 未来时间戳（超时钟偏斜容差）
    assert parse_series_member(member, 1_000_200.0, 1_000_100.0, _POLICY) is None
    # 未来戳在容差内（-2s）→ fresh 可用
    tick = parse_series_member(member, 1_000_102.0, 1_000_100.0, _POLICY)
    assert tick is not None and tick["freshness"] == "fresh"
