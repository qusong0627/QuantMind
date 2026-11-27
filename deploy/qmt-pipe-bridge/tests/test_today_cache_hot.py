"""qmt_today_cache.hot 单测（热集 tick 去重/环形/stale/恢复）。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from qmt_today_cache import models as m  # noqa: E402
from qmt_today_cache.hot import HotTickStore  # noqa: E402


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%d %H:%M:%S")
               .replace(tzinfo=m.CN_TZ).timestamp() * 1000)


def _tick(text: str, last: float, volume: float, bid: float, ask: float) -> dict:
    return {
        "timetag": text,
        "lastPrice": last,
        "volume": volume,
        "amount": volume * last,
        "bidPrice": [bid, bid - 0.01],
        "askPrice": [ask, ask + 0.01],
    }


def test_hot_tick_change_dedup_and_interval():
    store = HotTickStore(max_symbols=10, max_per_symbol=100,
                         min_interval_s=2.0, stale_s=10.0)
    now = _ms("20261127 10:00:00")
    assert len(store.ingest({"600036.SH": _tick("20261127 10:00:00", 10.0, 100, 9.99, 10.01)}, now)) == 1
    # 1 秒后完全相同：未到最小间隔，跳过
    assert store.ingest({"600036.SH": _tick("20261127 10:00:01", 10.0, 100, 9.99, 10.01)}, _ms("20261127 10:00:01")) == []
    # 价格变化：即使未到间隔也要存
    assert len(store.ingest({"600036.SH": _tick("20261127 10:00:01", 10.1, 100, 10.09, 10.11)}, _ms("20261127 10:00:01"))) == 1
    # 2 秒后未变化：达到间隔，存
    assert len(store.ingest({"600036.SH": _tick("20261127 10:00:03", 10.1, 100, 10.09, 10.11)}, _ms("20261127 10:00:03"))) == 1
    result = store.query(["600036.SH"], _ms("20261127 10:00:00"), _ms("20261127 10:00:05"), now_ms=_ms("20261127 10:00:04"))
    cols = result["600036.SH"]
    assert len(cols["ts_ms"]) == 3
    assert cols["last"][-1] == 10.1
    assert cols["stale"] is False


def test_hot_tick_ring_and_stale():
    store = HotTickStore(max_symbols=10, max_per_symbol=60,
                         min_interval_s=0.0, stale_s=5.0)
    base = datetime(2026, 11, 27, 10, 0, 0, tzinfo=m.CN_TZ)
    for i in range(65):
        moment = base + timedelta(seconds=i)
        text = moment.strftime("%Y%m%d %H:%M:%S")
        store.ingest(
            {"000001.SZ": _tick(text, 10.0 + i * 0.01, 100 + i, 9.9, 10.1)},
            int(moment.timestamp() * 1000),
        )
    cols = store.query(
        ["000001.SZ"], _ms("20261127 10:00:00"), _ms("20261127 10:02:00"),
        now_ms=_ms("20261127 10:01:20"),
    )["000001.SZ"]
    assert len(cols["ts_ms"]) == 60  # 环形只留最近 60 个
    assert cols["stale"] is True
    assert cols["age_ms"] >= 11000


def test_hot_tick_load_recovery_and_dedup():
    store = HotTickStore(max_symbols=10, max_per_symbol=100,
                         min_interval_s=0.0, stale_s=10.0)
    now = _ms("20261127 10:00:00")
    rows = store.ingest({"600036.SH": _tick("20261127 10:00:00", 10.0, 100, 9.99, 10.01)}, now)
    assert len(rows) == 1
    store.reset()
    assert store.load(rows) == 1
    assert store.load(rows) == 0
    result = store.query(
        ["600036.SH"], _ms("20261127 09:59:00"), _ms("20261127 10:01:00"),
        now_ms=_ms("20261127 10:00:01"),
    )["600036.SH"]
    assert len(result["ts_ms"]) == 1
    assert result["last"][0] == 10.0
    assert result["stale"] is False


def test_hot_tick_empty_query_is_gap():
    store = HotTickStore()
    result = store.query(
        ["600036.SH"], _ms("20261127 10:00:00"), _ms("20261127 10:01:00"),
        now_ms=_ms("20261127 10:01:00"),
    )["600036.SH"]
    assert result["gap"] is True
    assert result["stale"] is True
    assert result["ts_ms"] == []
