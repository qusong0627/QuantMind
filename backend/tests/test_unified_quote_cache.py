"""统一行情合并（TDX 主 + QMT 备）纯函数单测。"""

from __future__ import annotations

from backend.services.live_trading.services.unified_quote_cache import (
    merge_latest,
    publish_merged,
    read_tdx_snapshots,
    tdx_snapshot_keys,
)


def _qmt(last: float, ts: float, *, stale: bool = False) -> dict:
    return {
        "last": [last],
        "ts_ms": [int(ts * 1000)],
        "bid1": [last - 0.01],
        "ask1": [last + 0.01],
        "stale": stale,
    }


def test_tdx_primary_when_both_fresh():
    now = 1000.0
    tdx = {
        "600036.SH": {
            "last": 42.7,
            "ts": now - 1,
            "source": "tdx",
            "bid1_p": 42.69,
            "ask1_p": 42.71,
        }
    }
    qmt = {"600036.SH": _qmt(42.7, now - 2)}
    merged = merge_latest(tdx, qmt, now=now)
    row = merged["600036.SH"]
    assert row["source"] == "tdx"
    assert row["sources"] == ["tdx", "qmt"]
    assert row["quality"] == "ok"
    assert row["divergence_pct"] is None


def test_divergent_when_price_gap_over_threshold():
    now = 1000.0
    tdx = {"600036.SH": {"last": 42.7, "ts": now - 1, "source": "tdx"}}
    qmt = {"600036.SH": _qmt(41.0, now - 2)}
    row = merge_latest(tdx, qmt, now=now)["600036.SH"]
    assert row["quality"] == "divergent"
    assert row["divergence_pct"] and row["divergence_pct"] > 1.0


def test_qmt_fills_when_tdx_stale_or_missing():
    now = 1000.0
    tdx = {"600036.SH": {"last": 42.7, "ts": now - 30, "source": "tdx"}}
    qmt = {"600036.SH": _qmt(42.8, now - 2)}
    row = merge_latest(tdx, qmt, now=now)["600036.SH"]
    assert row["source"] == "qmt"
    assert row["sources"] == ["qmt"]
    assert row["last"] == 42.8


def test_tdx_fills_when_qmt_stale():
    now = 1000.0
    tdx = {"600036.SH": {"last": 42.7, "ts": now - 1, "source": "tdx"}}
    qmt = {"600036.SH": _qmt(42.7, now - 20, stale=True)}
    row = merge_latest(tdx, qmt, now=now)["600036.SH"]
    assert row["source"] == "tdx"


def test_both_stale_is_missing():
    now = 1000.0
    tdx = {"600036.SH": {"last": 42.7, "ts": now - 30, "source": "tdx"}}
    qmt = {"600036.SH": _qmt(42.7, now - 20, stale=True)}
    assert merge_latest(tdx, qmt, now=now) == {}


def test_canonical_qmt_row_is_not_treated_as_tdx():
    now = 1000.0
    tdx = {"600036.SH": {"last": 42.7, "ts": now - 1, "source": "qmt"}}
    qmt = {"600036.SH": _qmt(42.8, now - 2)}
    row = merge_latest(tdx, qmt, now=now)["600036.SH"]
    assert row["source"] == "qmt"
    assert row["sources"] == ["qmt"]


def test_unsuffixed_symbol_is_normalized():
    now = 1000.0
    tdx = {"600036": {"last": 42.7, "ts": now - 1, "source": "tdx"}}
    merged = merge_latest(tdx, {}, now=now)
    assert "600036.SH" in merged


def test_tdx_snapshot_keys_are_prefix_lowercase():
    source, canonical = tdx_snapshot_keys("600036.SH")
    assert source == "market:snapshot:tdx:sh600036"
    assert canonical == "market:snapshot:sh600036"
    source2, canonical2 = tdx_snapshot_keys("sz000001")
    assert source2 == "market:snapshot:tdx:sz000001"
    assert canonical2 == "market:snapshot:sz000001"


class _FakePipe:
    def __init__(self, data):
        self.data = data
        self.keys = []

    def hgetall(self, key):
        self.keys.append(key)
        return self

    def execute(self):
        return [self.data.get(key, {}) for key in self.keys]


class _FakeClient:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def pipeline(self, transaction=False):
        return _FakePipe(self.data)

    def close(self):
        self.closed = True


def test_read_tdx_snapshots_prefers_source_key_and_falls_back():
    client = _FakeClient(
        {
            "market:snapshot:tdx:sh600036": {
                "last": "42.7",
                "ts": "999",
                "source": "tdx_bridge",
            },
            "market:snapshot:sh600036": {"last": "41.0", "ts": "998"},
            "market:snapshot:sz000001": {"last": "12.2", "ts": "997"},
        }
    )
    rows = read_tdx_snapshots(
        ["600036.SH", "000001.SZ"], client_factory=lambda: client
    )
    assert rows["600036.SH"]["last"] == "42.7"
    assert rows["000001.SZ"]["last"] == "12.2"
    assert client.closed is True


class _WritePipe:
    def __init__(self):
        self.items = []

    def hset(self, key, mapping=None):
        self.items.append(("hset", key, mapping))
        return self

    def expire(self, key, ttl):
        self.items.append(("expire", key, ttl))
        return self

    def execute(self):
        return []


class _WriteClient:
    def __init__(self):
        self.pipe = _WritePipe()

    def pipeline(self, transaction=False):
        return self.pipe


def test_publish_merged_writes_source_and_quality():
    client = _WriteClient()
    merged = {
        "600036.SH": {
            "last": 42.7,
            "ts": 999.0,
            "source": "tdx",
            "quality": "ok",
            "sources": ["tdx", "qmt"],
            "divergence_pct": None,
            "bid1": 42.69,
            "ask1": 42.71,
        }
    }
    assert publish_merged(client, merged, ttl_s=10) == 1
    key = "market:snapshot:merged:600036.SH"
    hset = [item for item in client.pipe.items if item[0] == "hset"]
    assert hset and hset[0][1] == key
    assert hset[0][2]["sources"] == "tdx,qmt"
    assert any(item[0] == "expire" and item[1] == key for item in client.pipe.items)
