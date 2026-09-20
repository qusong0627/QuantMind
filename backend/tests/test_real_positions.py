"""实盘持仓并集口径的单测（纯函数层）。

盯三件事：多券商两源都要并进来（不是取最新一条）、停更源不并入、
同票两源都报时按活跃券商取量。这三条错了都不会报错——只会让自选里少几只票、
或让哨兵对着早已卖出的持仓发提醒。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend.shared.real_positions import (
    merge_real_sources,
    snapshot_source_for_broker,
)

BASE = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)


def _payload(*positions: dict) -> str:
    """实盘快照 payload：实测是**双层编码**（JSON 字符串套 JSON），这里如实模拟。"""
    return json.dumps({"positions": list(positions)})


def _pos(symbol: str, volume: float, **extra) -> dict:
    return {"symbol": symbol, "volume": volume, "available_volume": volume, **extra}


class TestMergeRealSources:
    def test_unions_disjoint_brokers(self):
        # 实测 qmt_exec 50 只 / tdx_bridge 8 只互不相交：取最新一条等于掷硬币
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            (
                "tdx_bridge",
                BASE - timedelta(seconds=10),
                _payload(_pos("000001.SZ", 500)),
            ),
        ]
        out, meta = merge_real_sources(rows)

        assert set(out) == {"SH600036", "SZ000001"}
        assert out["SH600036"]["source"] == "qmt_exec"
        assert out["SZ000001"]["sources"] == ["tdx_bridge"]
        assert meta["sources"]["qmt_exec"]["stale"] is False

    def test_stale_source_is_reported_but_not_merged(self):
        # 停更源会把早已卖出的持仓一直留在表上 → 假持仓，比漏持仓更危险
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE - timedelta(hours=3), _payload(_pos("000001.SZ", 500))),
        ]
        out, meta = merge_real_sources(rows)

        assert set(out) == {"SH600036"}
        assert meta["sources"]["tdx_bridge"]["stale"] is True
        assert meta["sources"]["tdx_bridge"]["positions"] == 1  # 有仓位但不并入，如实报

    def test_active_broker_wins_when_both_report_same_symbol(self):
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE, _payload(_pos("600036.SH", 900))),
        ]
        out, _ = merge_real_sources(rows, active_source="tdx")

        assert out["SH600036"]["volume"] == 900
        assert out["SH600036"]["source"] == "tdx_bridge"
        assert out["SH600036"]["sources"] == ["qmt_exec", "tdx_bridge"]

    def test_larger_volume_wins_without_active_preference(self):
        rows = [
            ("qmt_exec", BASE, _payload(_pos("600036.SH", 200))),
            ("tdx_bridge", BASE, _payload(_pos("600036.SH", 900))),
        ]
        out, meta = merge_real_sources(rows, active_source="tiger")

        assert out["SH600036"]["volume"] == 900
        assert meta["active_broker"] is None  # 没有映射的券商 = 没有偏好，不假报

    def test_non_a_share_rows_are_dropped(self):
        rows = [("qmt_exec", BASE, _payload(_pos("AAPL", 10), _pos("600036.SH", 200)))]
        out, meta = merge_real_sources(rows)

        assert set(out) == {"SH600036"}
        assert meta["sources"]["qmt_exec"]["positions"] == 2  # 原始条数照实报

    def test_undecodable_payload_is_not_silently_empty(self):
        rows = [("qmt_exec", BASE, "{not json")]
        out, meta = merge_real_sources(rows)

        assert out == {}
        assert meta["sources"]["qmt_exec"]["positions"] == 0

    def test_empty_rows_report_no_snapshot(self):
        out, meta = merge_real_sources([])

        assert out == {}
        assert meta == {"sources": {}, "snapshot_at": None, "active_broker": None}


class TestSnapshotSourceForBroker:
    def test_unknown_broker_has_no_snapshot_source(self):
        assert snapshot_source_for_broker("tiger") is None
        assert snapshot_source_for_broker(None) is None
