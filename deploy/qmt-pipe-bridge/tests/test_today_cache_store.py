"""qmt_today_cache.parquet_store 集成单测（临时目录，含崩溃/降级）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from qmt_today_cache import models as m  # noqa: E402
from qmt_today_cache import parquet_store as ps  # noqa: E402


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%d %H:%M:%S")
               .replace(tzinfo=m.CN_TZ).timestamp() * 1000)


def _bar(symbol: str, minute_text: str, close: float, cum_v: float,
         date: str = "20261127") -> dict:
    minute = _ms(minute_text)
    return {
        "date": date,
        "symbol": symbol,
        "minute_ts": minute,
        "open": close - 0.1,
        "high": close + 0.1,
        "low": close - 0.2,
        "close": close,
        "volume": 100.0,
        "amount": 1000.0,
        "vwap": 10.0,
        "cum_volume": cum_v,
        "cum_amount": cum_v * 10.0,
        "source": "qmt",
        "final": True,
    }


def _store(tmp: str) -> ps.PartStore:
    return ps.PartStore(
        tmp,
        part_interval_s=30,
        part_max_rows=1000,
        retention_days=30,
        write_queue_max=100,
        compress="zstd",
        spool_fallback=True,
        disk_min_free_gb=0,
    )


def test_parquet_write_manifest_and_read():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.start()
        try:
            rows = [
                _bar("600036.SH", "20261127 09:30:00", 10.0, 100),
                _bar("600036.SH", "20261127 09:31:00", 10.2, 200),
                _bar("000001.SZ", "20261127 09:30:00", 12.0, 100),
            ]
            store.append("bar1m", rows)
            store.flush()
            status = store.status()
            assert status["format"] == "parquet"
            assert status["degraded"] is False
            assert status["parts"] >= 1
            assert status["dropped"] == 0

            manifest_path = os.path.join(tmp, "date=20261127", "manifest.json")
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            assert manifest["version"] == 1
            assert len(manifest["parts"]) == 1
            entry = manifest["parts"][0]
            assert entry["type"] == "bar1m"
            assert entry["rows"] == 3
            assert len(entry["sha256"]) == 64

            loaded = store.read(
                "bar1m",
                "20261127",
                symbols=["600036.SH"],
                start_ms=_ms("20261127 09:30:00"),
                end_ms=_ms("20261127 09:31:00"),
            )
            assert len(loaded) == 2
            assert all(row["symbol"] == "600036.SH" for row in loaded)
            assert loaded[0]["close"] == 10.0
        finally:
            store.stop()


def test_tmp_files_are_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.start()
        try:
            store.append("bar1m", [_bar("600036.SH", "20261127 09:30:00", 10.0, 100)])
            store.flush()
            part_dir = os.path.join(tmp, "date=20261127")
            tmp_file = os.path.join(part_dir, "type=bar1m", "hour=09", "part-999999.parquet.tmp")
            os.makedirs(os.path.dirname(tmp_file), exist_ok=True)
            with open(tmp_file, "w", encoding="utf-8") as fh:
                fh.write("not a parquet")
            loaded = store.read("bar1m", "20261127")
            assert len(loaded) == 1  # 只认 manifest 里的 part
        finally:
            store.stop()


def test_spool_fallback_without_pyarrow():
    old_pa, old_pq = ps.pa, ps.pq
    ps.pa = None
    ps.pq = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.start()
            try:
                store.append(
                    "bar1m", [_bar("600036.SH", "20261127 09:30:00", 10.0, 100)]
                )
                store.flush()
                status = store.status()
                assert status["format"] == "jsonl.gz"
                assert status["degraded"] is True
                loaded = store.read("bar1m", "20261127")
                assert len(loaded) == 1
                assert loaded[0]["close"] == 10.0
            finally:
                store.stop()
    finally:
        ps.pa, ps.pq = old_pa, old_pq


def test_retention_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = os.path.join(tmp, "date=20200101", "type=bar1m", "hour=09")
        os.makedirs(old_dir)
        with open(os.path.join(old_dir, "part-000001.parquet"), "w", encoding="utf-8") as fh:
            fh.write("x")
        store = ps.PartStore(tmp, retention_days=30)
        removed = store.cleanup(today="20261127")
        assert removed == 1
        assert not os.path.exists(os.path.join(tmp, "date=20200101"))


def test_flush_groups_by_date():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.start()
        try:
            store.append(
                "bar1m",
                [
                    _bar("600036.SH", "20261127 09:30:00", 10.0, 100),
                    _bar("600036.SH", "20261128 09:30:00", 11.0, 100, date="20261128"),
                ],
            )
            store.flush()
            assert os.path.isdir(os.path.join(tmp, "date=20261127"))
            assert os.path.isdir(os.path.join(tmp, "date=20261128"))
            assert len(store.read("bar1m", "20261127")) == 1
            assert len(store.read("bar1m", "20261128")) == 1
        finally:
            store.stop()


def test_manifest_path_traversal_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        date_dir = os.path.join(tmp, "date=20261127")
        os.makedirs(date_dir)
        manifest = {
            "version": 1,
            "date": "20261127",
            "parts": [
                {
                    "type": "bar1m",
                    "file": "../../../etc/passwd",
                    "format": "jsonl.gz",
                }
            ],
        }
        with open(os.path.join(date_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        store = ps.PartStore(tmp)
        assert store.read("bar1m", "20261127") == []


def _tick_row(symbol: str, ts_text: str, last: float, volume: float) -> dict:
    return {
        "date": "20261127",
        "ts_ms": _ms(ts_text),
        "symbol": symbol,
        "last": last,
        "volume": volume,
        "amount": volume * last,
        "bid1": last - 0.01,
        "ask1": last + 0.01,
        "source": "qmt",
    }


def test_tick_hot_parquet_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.start()
        try:
            store.append(
                "tick_hot",
                [
                    _tick_row("600036.SH", "20261127 10:00:00", 10.0, 100),
                    _tick_row("600036.SH", "20261127 10:00:03", 10.1, 120),
                ],
            )
            store.flush()
            loaded = store.read("tick_hot", "20261127", symbols=["600036.SH"])
            assert len(loaded) == 2
            assert loaded[-1]["last"] == 10.1
            assert loaded[-1]["bid1"] == 10.09
        finally:
            store.stop()


def test_per_kind_retention_keeps_fresh_kind():
    with tempfile.TemporaryDirectory() as tmp:
        date = "20261101"
        for kind in ("bar1m", "tick_hot"):
            path = os.path.join(tmp, "date=%s" % date, "type=%s" % kind, "hour=10")
            os.makedirs(path)
            with open(os.path.join(path, "part-000001.parquet"), "w", encoding="utf-8") as fh:
                fh.write("x")
        store = ps.PartStore(
            tmp,
            retention_days=30,
            kind_retention={"bar1m": 30, "tick_hot": 10},
        )
        store.cleanup(today="20261127")
        assert os.path.isdir(os.path.join(tmp, "date=%s" % date, "type=bar1m"))
        assert not os.path.exists(os.path.join(tmp, "date=%s" % date, "type=tick_hot"))


def _snap_row(symbol: str, ts_text: str, last: float) -> dict:
    return {
        "date": "20261127",
        "ts_ms": _ms(ts_text),
        "symbol": symbol,
        "last": last,
        "open": last - 0.1,
        "high": last + 0.2,
        "low": last - 0.2,
        "pre_close": last - 0.05,
        "volume": 1000,
        "amount": 1000 * last,
        "bid1": last - 0.01,
        "ask1": last + 0.01,
        "bid_vol1": 100,
        "ask_vol1": 100,
        "source": "qmt_snapshot",
        "quality": "ok",
    }


def test_snapshot3s_parquet_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.start()
        try:
            store.append(
                "snapshot3s",
                [
                    _snap_row("600036.SH", "20261127 10:00:00", 10.0),
                    _snap_row("000001.SZ", "20261127 10:00:00", 12.0),
                ],
            )
            store.flush()
            loaded = store.read("snapshot3s", "20261127")
            assert len(loaded) == 2
            assert {row["symbol"] for row in loaded} == {"600036.SH", "000001.SZ"}
            assert loaded[0]["quality"] == "ok"
        finally:
            store.stop()
