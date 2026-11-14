"""QMT/TDX 健康告警盘外节流：盘中不限，盘前/盘后每段最多 2 次。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.services.live_trading.services import health_alert_throttle as throttle

CST = timezone(timedelta(hours=8))


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        self.expires[key] = ttl

    def close(self):
        pass


class TestKey:
    def test_pre_and_post_periods(self):
        pre = datetime(2026, 11, 16, 9, 0, tzinfo=CST)
        post = datetime(2026, 11, 16, 15, 10, tzinfo=CST)
        assert throttle.throttle_key("tdx_bridge", pre).endswith("20261116:pre")
        assert throttle.throttle_key("qmt_pipe", post).endswith("20261116:post")

    def test_channel_is_isolated(self):
        now = datetime(2026, 11, 16, 8, 0, tzinfo=CST)
        assert throttle.throttle_key("a", now) != throttle.throttle_key("b", now)


class TestAllow:
    def test_intraday_is_unlimited(self, monkeypatch):
        monkeypatch.setattr(throttle, "is_trading_time", lambda now=None: True)
        client = FakeRedis()
        now = datetime(2026, 11, 16, 10, 0, tzinfo=CST)
        assert all(
            throttle.allow_health_alert("tdx_bridge", now, client=client)
            for _ in range(5)
        )
        assert client.counts == {}

    def test_outside_session_caps_at_two(self, monkeypatch):
        monkeypatch.setattr(throttle, "is_trading_time", lambda now=None: False)
        client = FakeRedis()
        now = datetime(2026, 11, 16, 8, 0, tzinfo=CST)
        assert throttle.allow_health_alert("tdx_bridge", now, client=client) is True
        assert throttle.allow_health_alert("tdx_bridge", now, client=client) is True
        assert throttle.allow_health_alert("tdx_bridge", now, client=client) is False
        key = throttle.throttle_key("tdx_bridge", now)
        assert client.expires[key] == throttle.KEY_TTL_S

    def test_pre_and_post_have_separate_budgets(self, monkeypatch):
        monkeypatch.setattr(throttle, "is_trading_time", lambda now=None: False)
        client = FakeRedis()
        pre = datetime(2026, 11, 16, 8, 0, tzinfo=CST)
        post = datetime(2026, 11, 16, 16, 0, tzinfo=CST)
        for _ in range(2):
            assert throttle.allow_health_alert("qmt_pipe", pre, client=client)
        assert not throttle.allow_health_alert("qmt_pipe", pre, client=client)
        assert throttle.allow_health_alert("qmt_pipe", post, client=client)

    def test_redis_failure_is_fail_open(self, monkeypatch):
        monkeypatch.setattr(throttle, "is_trading_time", lambda now=None: False)

        class Broken:
            def incr(self, key):
                raise ConnectionError("redis down")

        now = datetime(2026, 11, 16, 8, 0, tzinfo=CST)
        assert throttle.allow_health_alert("tdx_bridge", now, client=Broken()) is True
