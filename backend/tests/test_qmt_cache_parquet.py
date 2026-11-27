"""qmt_cache_parquet 只读读取测试（临时目录，DuckDB）。"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq

from backend.services.live_trading.services import qmt_cache_parquet as q


def _write_snapshot3s(root: str) -> None:
    path = os.path.join(root, "date=20261127", "type=snapshot3s", "hour=10")
    os.makedirs(path)
    rows = [
        {
            "date": "20261127",
            "ts_ms": 1795756800000,
            "symbol": "600036.SH",
            "last": 10.0,
            "open": 9.9,
            "high": 10.2,
            "low": 9.8,
            "pre_close": 9.95,
            "volume": 1000.0,
            "amount": 10000.0,
            "bid1": 9.99,
            "ask1": 10.01,
            "bid_vol1": 100.0,
            "ask_vol1": 100.0,
            "source": "qmt_snapshot",
            "quality": "ok",
        },
        {
            "date": "20261127",
            "ts_ms": 1795756800000,
            "symbol": "000001.SZ",
            "last": 12.0,
            "open": 11.9,
            "high": 12.2,
            "low": 11.8,
            "pre_close": 11.95,
            "volume": 2000.0,
            "amount": 24000.0,
            "bid1": 11.99,
            "ask1": 12.01,
            "bid_vol1": 200.0,
            "ask_vol1": 200.0,
            "source": "qmt_snapshot",
            "quality": "ok",
        },
    ]
    pq.write_table(pa.Table.from_pylist(rows), os.path.join(path, "part-000001.parquet"))


def test_query_snapshot3s_filters_symbol_and_time():
    with tempfile.TemporaryDirectory() as tmp:
        _write_snapshot3s(tmp)
        old = os.environ.get("QM_QMT_CACHE_DIR")
        os.environ["QM_QMT_CACHE_DIR"] = tmp
        try:
            rows = q.query_snapshot3s(
                "20261127",
                symbols=["600036.SH"],
                start_ms=1795756800000,
                end_ms=1795756800000 + 1000,
            )
            assert len(rows) == 1
            assert rows[0]["symbol"] == "600036.SH"
            assert rows[0]["last"] == 10.0
        finally:
            if old is None:
                os.environ.pop("QM_QMT_CACHE_DIR", None)
            else:
                os.environ["QM_QMT_CACHE_DIR"] = old


def test_query_rejects_bad_kind_or_date_and_missing_dir():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("QM_QMT_CACHE_DIR")
        os.environ["QM_QMT_CACHE_DIR"] = tmp
        try:
            assert q.query("bad", "20261127") == []
            assert q.query("snapshot3s", "../../etc") == []
            assert q.query_snapshot3s("20261128") == []
        finally:
            if old is None:
                os.environ.pop("QM_QMT_CACHE_DIR", None)
            else:
                os.environ["QM_QMT_CACHE_DIR"] = old
