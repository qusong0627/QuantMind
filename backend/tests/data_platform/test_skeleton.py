"""D1 骨架冒烟测试：仅校验导入与基础契约，不依赖 Redis / Parquet / 真实数据源。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend.services.engine.data_platform import (
    DataUnavailable,
    InvalidFieldRequest,
    OfflineDataSourceAdapter,
    OHLCV_COLUMNS,
    SourceRegistry,
    get_registry,
)
from backend.services.engine.data_platform.monitor import HealthMonitor
from backend.services.engine.data_platform.storage import ParquetWriter


# ---------------------------------------------------------------------------
# Dummy adapter (用例内部，不污染全局 registry)
# ---------------------------------------------------------------------------
class _DummyAdapter(OfflineDataSourceAdapter):
    name = "dummy"
    markets = ["A"]
    fields = {"daily_kline"}

    def fetch_daily(self, symbol, start, end, *, adjust="qfq"):
        if symbol == "EMPTY.SH":
            raise DataUnavailable("no data")
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "trade_date": date(2025, 1, 2),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low
                    "close": 10.5,
                    "volume": 1000,
                    "amount": 10500,
                    "adj_factor": 1.0,
                    "source": self.name,
                }
            ]
        )

    def fetch_meta(self, market):
        return pd.DataFrame(
            [
                {
                    "symbol": "600519.SH",
                    "code": "600519",
                    "exchange": "SH",
                    "name": "贵州茅台",
                    "market": "A",
                    "source": self.name,
                }
            ]
        )


def _isolated_registry() -> SourceRegistry:
    r = SourceRegistry()
    r.register(_DummyAdapter, name="dummy")
    return r


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_registry_register_and_get():
    r = _isolated_registry()
    assert r.list_sources() == ["dummy"]
    adapter = r.get("dummy")
    assert isinstance(adapter, _DummyAdapter)
    # 单例
    assert r.get("dummy") is adapter


def test_registry_missing_raises():
    r = SourceRegistry()
    with pytest.raises(KeyError):
        r.get("nope")


def test_registry_sources_for_field_market():
    r = _isolated_registry()
    assert r.sources_for("daily_kline", "A") == ["dummy"]
    assert r.sources_for("daily_kline", "HK") == []
    assert r.sources_for("realtime_quote", "A") == []


def test_module_singleton_registry():
    a = get_registry()
    b = get_registry()
    assert a is b


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------
def test_adapter_fetch_daily_columns():
    adapter = _DummyAdapter()
    df = adapter.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))
    for col in ("symbol", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "adj_factor", "source"):
        assert col in df.columns
    assert all(c in OHLCV_COLUMNS for c in df.columns)


def test_adapter_data_unavailable():
    adapter = _DummyAdapter()
    with pytest.raises(DataUnavailable):
        adapter.fetch_daily("EMPTY.SH", date(2025, 1, 1), date(2025, 1, 5))


def test_adapter_optional_realtime_default_raises():
    adapter = _DummyAdapter()
    with pytest.raises(InvalidFieldRequest):
        adapter.fetch_realtime("600519.SH")


def test_adapter_supports():
    adapter = _DummyAdapter()
    assert adapter.supports("daily_kline", "A")
    assert not adapter.supports("daily_kline", "HK")
    assert not adapter.supports("realtime_quote", "A")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def test_parquet_writer_round_trip(tmp_path):
    w = ParquetWriter(root=tmp_path)
    df = pd.DataFrame(
        [
            {
                "symbol": "600519.SH",
                "trade_date": date(2025, 1, 2),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low
                "close": 10.5,
                "volume": 1000,
                "amount": 10500,
                "adj_factor": 1.0,
                "source": "dummy",
            }
        ]
    )
    paths = w.write_daily(df, source="dummy", market="A")
    assert len(paths) == 1
    assert paths[0].exists()
    loaded = pd.read_parquet(paths[0])
    assert len(loaded) == 1
    assert loaded.iloc[0]["close"] == 10.5


def test_parquet_writer_empty_noop(tmp_path):
    w = ParquetWriter(root=tmp_path)
    assert w.write_daily(pd.DataFrame(), source="dummy", market="A") == []


def test_parquet_writer_incremental_merge(tmp_path):
    w = ParquetWriter(root=tmp_path)
    base_row = dict(
        symbol="600519.SH", open=10.0, high=11.0, low=9.5,  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low
        volume=1000, amount=10500, adj_factor=1.0, source="dummy",
    )
    df1 = pd.DataFrame([{**base_row, "trade_date": date(2025, 1, 2), "close": 10.5}])
    df2 = pd.DataFrame([{**base_row, "trade_date": date(2025, 1, 3), "close": 10.8}])
    w.write_daily(df1, source="dummy", market="A")
    w.write_daily(df2, source="dummy", market="A")
    # 同一年同一标的合并
    files = list((tmp_path / "dummy" / "A" / "daily_kline" / "2025").glob("*.parquet"))
    assert len(files) == 1
    merged = pd.read_parquet(files[0])
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
def test_health_monitor_in_memory_success_and_error():
    m = HealthMonitor(redis_client=None)
    m.record_success("dummy", "daily_kline", rows=100, latency_ms=50.0)
    m.record_error("dummy", "daily_kline", error="boom", latency_ms=80.0)
    h = m.get_health("dummy", "daily_kline")
    assert "last_success_at" in h
    assert "last_error_at" in h
    assert h["last_error_msg"] == "boom"
    assert float(h["avg_latency_ms"]) > 0


def test_health_monitor_fallback_counter():
    m = HealthMonitor(redis_client=None)
    m.record_fallback("dummy", "daily_kline")
    m.record_fallback("dummy", "daily_kline")
    h = m.get_health("dummy", "daily_kline")
    assert int(h["fallback_triggered_count"]) == 2


def test_health_monitor_consensus_deviation_ema():
    m = HealthMonitor(redis_client=None)
    m.record_consensus_deviation("dummy", "daily_kline", deviation=0.1)
    m.record_consensus_deviation("dummy", "daily_kline", deviation=0.5)
    h = m.get_health("dummy", "daily_kline")
    v = float(h["consensus_deviation_avg"])
    # EMA: 0.1 -> first; then 0.1*0.8 + 0.5*0.2 = 0.18
    assert abs(v - 0.18) < 1e-6
