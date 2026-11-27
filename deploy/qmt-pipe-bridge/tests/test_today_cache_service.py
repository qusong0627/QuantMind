"""qmt_today_cache.service 单测（内存查询/日期回滚/订单方法不接管）。"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from qmt_today_cache import config as cfg_mod  # noqa: E402
from qmt_today_cache import models as m  # noqa: E402
from qmt_today_cache.service import TodayCacheService  # noqa: E402


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%d %H:%M:%S")
               .replace(tzinfo=m.CN_TZ).timestamp() * 1000)


class _Clock:
    def __init__(self, text: str):
        self.now = _ms(text) / 1000.0

    def __call__(self):
        return self.now


def _tick(text: str, last: float, volume: float, amount: float) -> dict:
    return {
        "timetag": text,
        "lastPrice": last,
        "volume": volume,
        "amount": amount,
        "lastClose": last,
    }


def _service(tmp: str, clock: _Clock, **overrides) -> TodayCacheService:
    block = {
        "enabled": True,
        "dir": tmp,
        "snapshot_interval_s": 3,
        "max_age_s": 5,
        "part_interval_s": 300,
        "startup_grace_s": 0,
        "session_only": False,
    }
    block.update(overrides)
    config = cfg_mod.TodayCacheConfig.from_config(
        {"today_cache": block},
        tmp,
    )
    return TodayCacheService(
        config,
        fetcher=lambda codes: {},
        universe_provider=lambda: ["600036.SH"],
        session_provider=lambda: True,
        clock=clock,
    )


def test_get_full_tick_hit_and_stale():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 14:55:33")
        service = _service(tmp, clock)
        try:
            service.ingest_ticks(
                {"600036.SH": _tick("20261127 14:55:32", 42.7, 100, 1000)}
            )
            resp = service.try_handle_rpc(
                "get_full_tick",
                {"request_id": "r1", "params": {"codes": ["600036.SH"]}},
            )
            assert resp["ok"] is True
            assert resp["request_id"] == "r1"
            assert resp["data"]["600036.SH"]["lastPrice"] == 42.7
            clock.now += 10
            assert (
                service.try_handle_rpc(
                    "get_full_tick",
                    {"request_id": "r2", "params": {"codes": ["600036.SH"]}},
                )
                is None
            )
            assert service.status()["stale"] == 1
        finally:
            service.stop()


def test_cache_today_1m_and_status():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 09:31:02")
        service = _service(tmp, clock)
        try:
            service.ingest_ticks(
                {"600036.SH": _tick("20261127 09:30:00", 10.0, 100, 1000)}
            )
            clock.now = _ms("20261127 09:30:31") / 1000.0
            service.ingest_ticks(
                {"600036.SH": _tick("20261127 09:30:30", 10.5, 150, 1500)}
            )
            clock.now = _ms("20261127 09:31:01") / 1000.0
            service.ingest_ticks(
                {"600036.SH": _tick("20261127 09:31:00", 10.2, 200, 2000)}
            )
            resp = service.try_handle_rpc(
                "cache_today_1m",
                {
                    "request_id": "q1",
                    "params": {
                        "stock_list": ["600036.SH"],
                        "start_time": "20261127",
                        "end_time": "20261127",
                    },
                },
            )
            assert resp["ok"] is True
            bars = resp["data"]["600036.SH"]
            assert len(bars["minute_ts"]) == 2
            assert bars["final"] == [True, False]
            assert bars["volume"][0] == 150.0
            status = service.try_handle_rpc(
                "cache_status", {"request_id": "s1", "params": {}}
            )
            assert status["data"]["enabled"] is True
            assert status["data"]["queries"] == 1
        finally:
            service.stop()


def test_order_method_is_never_handled():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 09:31:02")
        service = _service(tmp, clock)
        try:
            for method in ("order_stock", "cancel_order_stock", "sync_positions"):
                assert service.try_handle_rpc(method, {"params": {}}) is None
        finally:
            service.stop()


def test_cache_today_1m_rejects_too_many_symbols():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 09:31:02")
        service = _service(tmp, clock, query_max_symbols=2)
        try:
            resp = service.try_handle_rpc(
                "cache_today_1m",
                {
                    "request_id": "big",
                    "params": {
                        "stock_list": ["600036.SH", "000001.SZ", "600519.SH"],
                        "start_time": "20261127",
                        "end_time": "20261127",
                    },
                },
            )
            assert resp["ok"] is False
            assert "TOO_MANY_SYMBOLS" in resp["error"]
        finally:
            service.stop()


def test_cache_hot_ticks_and_stale():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 10:00:01")
        service = _service(tmp, clock, hot_tick_stale_s=5)
        try:
            assert service.ingest_hot_ticks(
                {"600036.SH": _tick("20261127 10:00:00", 10.0, 100, 1000)}
            ) == 1
            resp = service.try_handle_rpc(
                "cache_hot_ticks",
                {
                    "request_id": "hot1",
                    "params": {
                        "stock_list": ["600036.SH"],
                        "start_time": "20261127",
                        "end_time": "20261127",
                    },
                },
            )
            cols = resp["data"]["600036.SH"]
            assert len(cols["ts_ms"]) == 1
            assert cols["last"][0] == 10.0
            assert cols["stale"] is False
            clock.now += 10
            resp = service.try_handle_rpc(
                "cache_hot_ticks",
                {
                    "request_id": "hot2",
                    "params": {
                        "stock_list": ["600036.SH"],
                        "start_time": "20261127",
                        "end_time": "20261127",
                    },
                },
            )
            assert resp["data"]["600036.SH"]["stale"] is True
        finally:
            service.stop()


def test_cache_hot_ticks_disabled_and_limit():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 10:00:01")
        service = _service(tmp, clock, hot_enabled=False)
        try:
            assert service.ingest_hot_ticks(
                {"600036.SH": _tick("20261127 10:00:00", 10.0, 100, 1000)}
            ) == 0
            resp = service.try_handle_rpc(
                "cache_hot_ticks",
                {"request_id": "x", "params": {"stock_list": ["600036.SH"]}},
            )
            assert resp["ok"] is False
            assert "HOT_TICKS_DISABLED" in resp["error"]
        finally:
            service.stop()
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 10:00:01")
        service = _service(tmp, clock, hot_query_max_symbols=2)
        try:
            resp = service.try_handle_rpc(
                "cache_hot_ticks",
                {
                    "request_id": "big",
                    "params": {
                        "stock_list": ["600036.SH", "000001.SZ", "600519.SH"]
                    },
                },
            )
            assert resp["ok"] is False
            assert "TOO_MANY_SYMBOLS" in resp["error"]
        finally:
            service.stop()


def test_ingest_snapshot3s_counts_and_round_ts():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 10:00:05")
        service = _service(tmp, clock, snapshot3s_enabled=True)
        try:
            round_ts = _ms("20261127 10:00:05")
            rows = service.ingest_snapshot3s(
                {
                    "600036.SH": _tick("20261127 10:00:05", 10.0, 100, 1000),
                    "000001.SZ": _tick("20261127 10:00:05", 12.0, 200, 2400),
                },
                round_ts,
            )
            assert rows == 2
            status = service.status()
            assert status["snapshot3s_rows"] == 2
            assert status["snapshot3s_rounds"] == 1
            assert status["snapshot3s_enabled"] is True
        finally:
            service.stop()


def test_date_rollover_resets_bars_and_latest():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock("20261127 14:55:33")
        service = _service(tmp, clock)
        try:
            service.ingest_ticks(
                {"600036.SH": _tick("20261127 14:55:32", 42.7, 100, 1000)}
            )
            assert service.status()["bar_symbols"] == 0  # current bar 未完成
            clock.now = _ms("20261128 09:30:01") / 1000.0
            service.ingest_ticks(
                {"600036.SH": _tick("20261128 09:30:00", 43.0, 100, 1000)}
            )
            assert service.status()["date"] == "20261128"
            # 昨日的 latest 已清空；今天的新 tick 命中
            resp = service.try_handle_rpc(
                "get_full_tick",
                {"request_id": "r", "params": {"codes": ["600036.SH"]}},
            )
            assert resp["data"]["600036.SH"]["lastPrice"] == 43.0
        finally:
            service.stop()
