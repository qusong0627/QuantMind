"""盘中结构采集任务的纯逻辑/解析单测（不连桥、不连 Redis）。"""

from __future__ import annotations

import json
from datetime import datetime

from backend.services.live_trading.services import intraday_market_watch as watch
from backend.services.live_trading.services.trading_session import TZ


class _FakeRedis:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def hgetall(self, _key):
        return dict(self.rows)


def test_load_extremes_parses_and_skips_bad_rows():
    client = _FakeRedis(
        {
            "600036.SH": json.dumps({"max": 43.5, "min": 42.8}),
            "600037.SH": "{bad json",
            "600038.SH": json.dumps({"max": None, "min": 10}),
        }
    )
    max_map, min_map = watch.load_extremes(client, "20261116")
    assert max_map == {"600036.SH": 43.5}
    assert min_map == {"600036.SH": 42.8, "600038.SH": 10.0}


def test_metric_events_require_volume_ratio_and_speed_and_dedupe():
    watch._event_seen.clear()
    metrics = {
        "SH600036": {"name": "招商银行", "vr": 2.8, "speed": 1.2},
        "SZ000725": {"name": "京东方A", "vr": 3.0, "speed": 0.2},
    }
    first = watch._metric_events(metrics, {}, 1000.0)
    assert len(first) == 1 and first[0]["code"] == "600036.SH"
    second = watch._metric_events(metrics, {}, 1100.0)
    assert second == []  # 30 分钟内同码去重
    third = watch._metric_events(metrics, {}, 3000.0)
    assert len(third) == 1


def test_watch_window_covers_continuous_session_and_sector_refresh():
    assert watch._watch_window(datetime(2026, 11, 16, 10, 0, tzinfo=TZ)) is True
    assert watch._watch_window(datetime(2026, 11, 16, 12, 0, tzinfo=TZ)) is False
    assert watch._watch_window(datetime(2026, 11, 16, 15, 40, tzinfo=TZ)) is True
    assert watch._watch_window(datetime(2026, 11, 16, 17, 0, tzinfo=TZ)) is False


def test_sector_refresh_window():
    assert watch._sector_refresh_window(datetime(2026, 11, 16, 15, 40, tzinfo=TZ))
    assert not watch._sector_refresh_window(datetime(2026, 11, 16, 14, 0, tzinfo=TZ))
