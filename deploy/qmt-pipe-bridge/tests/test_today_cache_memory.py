"""qmt_today_cache.memory 单测（最新快照 + 1m 聚合）。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from qmt_today_cache import memory as mem  # noqa: E402
from qmt_today_cache import models as m  # noqa: E402


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%d %H:%M:%S")
               .replace(tzinfo=m.CN_TZ).timestamp() * 1000)


def _tick(text: str, last: float, volume: float, amount: float) -> dict:
    return {
        "timetag": text,
        "lastPrice": last,
        "volume": volume,
        "amount": amount,
        "lastClose": last,
    }


def test_latest_snapshot_hit_stale_and_miss():
    snapshot = mem.LatestSnapshot(max_age_s=5.0, max_symbols=100)
    ts = _ms("20261127 14:55:32")
    assert snapshot.update({"600036.SH": _tick("20261127 14:55:32", 42.7, 1, 2)}, ts) == 1
    hit, reason = snapshot.get(["600036.SH"], ts + 1000)
    assert reason == "hit" and hit["600036.SH"]["lastPrice"] == 42.7
    hit, reason = snapshot.get(["600036.SH"], ts + 6001)
    assert hit is None and reason == "stale"
    hit, reason = snapshot.get(["000001.SZ"], ts)
    assert hit is None and reason == "miss"


def test_bar1m_aggregation_volume_and_final():
    store = mem.Bar1mStore(max_bars_per_symbol=10)
    ticks = {
        "600036.SH": _tick("20261127 09:30:00", 10.0, 100, 1000),
    }
    assert store.ingest(ticks, _ms("20261127 09:30:01")) == []
    ticks = {"600036.SH": _tick("20261127 09:30:30", 10.5, 150, 1500)}
    assert store.ingest(ticks, _ms("20261127 09:30:31")) == []
    ticks = {"600036.SH": _tick("20261127 09:31:00", 10.2, 200, 2000)}
    finalized = store.ingest(ticks, _ms("20261127 09:31:01"))
    assert len(finalized) == 1
    bar = finalized[0]
    assert bar["minute_ts"] == _ms("20261127 09:30:00")
    assert bar["open"] == 10.0 and bar["high"] == 10.5 and bar["low"] == 10.0
    assert bar["close"] == 10.5
    assert bar["volume"] == 150.0 and bar["amount"] == 1500.0
    assert bar["final"] is True

    rows = store.finalize_all(_ms("20261127 09:31:02"))
    assert len(rows) == 1
    assert rows[0]["minute_ts"] == _ms("20261127 09:31:00")
    assert rows[0]["volume"] == 50.0 and rows[0]["amount"] == 500.0
    assert rows[0]["close"] == 10.2

    result = store.query(
        ["600036.SH"], _ms("20261127 09:30:00"), _ms("20261127 09:31:59")
    )
    assert len(result["600036.SH"]) == 2
    assert all(item["final"] for item in result["600036.SH"])


def test_bar1m_query_includes_partial_current_bar():
    store = mem.Bar1mStore(max_bars_per_symbol=10)
    ticks = {"000001.SZ": _tick("20261127 09:30:05", 12.0, 100, 1200)}
    store.ingest(ticks, _ms("20261127 09:30:06"))
    result = store.query(
        ["000001.SZ"], _ms("20261127 09:30:00"), _ms("20261127 09:31:00")
    )
    bars = result["000001.SZ"]
    assert len(bars) == 1
    assert bars[0]["final"] is False
    assert bars[0]["volume"] == 100.0


def test_bar1m_skips_stale_and_late_ticks():
    store = mem.Bar1mStore(max_bars_per_symbol=10)
    now = _ms("20261127 09:30:00")
    stale = {"600036.SH": _tick("20261127 09:00:00", 10.0, 100, 1000)}
    assert store.ingest(stale, now) == []  # 超过 6h 的旧 tick 丢弃
    store.ingest(
        {"600036.SH": _tick("20261127 09:30:30", 10.0, 100, 1000)},
        _ms("20261127 09:30:31"),
    )
    late = {"600036.SH": _tick("20261127 09:30:00", 9.0, 90, 810)}
    assert store.ingest(late, _ms("20261127 09:30:32")) == []


def test_bar1m_load_recovers_cumulative_and_dedup():
    store = mem.Bar1mStore(max_bars_per_symbol=10)
    ticks = {"600036.SH": _tick("20261127 09:30:00", 10.0, 100, 1000)}
    store.ingest(ticks, _ms("20261127 09:30:01"))
    ticks = {"600036.SH": _tick("20261127 09:31:00", 10.2, 200, 2000)}
    finalized = store.ingest(ticks, _ms("20261127 09:31:01"))
    rows = finalized + store.finalize_all(_ms("20261127 09:31:02"))
    store.reset()  # 模拟进程重启后从磁盘恢复
    assert store.load(rows) == 2  # 09:30 + 09:31 两根完成 bar
    assert store.load(rows) == 0  # 去重
    # 恢复后首 tick：起始累计量=已落盘 bar 的 cum_volume，不得把全天量算进当前分钟
    ticks = {"600036.SH": _tick("20261127 09:32:00", 10.3, 300, 3000)}
    store.ingest(ticks, _ms("20261127 09:32:01"))
    result = store.query(
        ["600036.SH"], _ms("20261127 09:32:00"), _ms("20261127 09:32:59")
    )
    bar = result["600036.SH"][0]
    assert bar["final"] is False
    assert bar["volume"] == 100.0  # 300 - 200


def test_bar1m_counts_and_reset():
    store = mem.Bar1mStore(max_bars_per_symbol=10)
    store.ingest(
        {"600036.SH": _tick("20261127 09:30:00", 10.0, 100, 1000)},
        _ms("20261127 09:30:01"),
    )
    counts = store.counts()
    assert counts["symbols"] == 0 and counts["current_bars"] == 1
    store.finalize_all()
    counts = store.counts()
    assert counts["symbols"] == 1 and counts["completed_bars"] == 1
    store.reset()
    assert store.counts()["completed_bars"] == 0
