"""D2 测试：字段路由 + 聚合器 + 清洗 + 交易日历。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend.services.engine.data_platform.aggregator import (
    FieldAggregator,
    FieldRoutingTable,
    _consensus_deviation,
    _merge_by_median,
)
from backend.services.engine.data_platform.base import (
    DataUnavailable,
    OfflineDataSourceAdapter,
)
from backend.services.engine.data_platform.calendars import (
    ChinaACalendar,
    HongKongCalendar,
    UnitedStatesCalendar,
    get_calendar,
    reset_calendars,
)
from backend.services.engine.data_platform.cleaner import DataCleaner
from backend.services.engine.data_platform.monitor import HealthMonitor
from backend.services.engine.data_platform.registry import SourceRegistry


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------
def test_routing_loads_real_yaml():
    rt = FieldRoutingTable()
    assert "A" in rt.list_markets()
    assert "HK" in rt.list_markets()
    assert "US" in rt.list_markets()


@pytest.mark.skip(reason="A股日线已收敛为 quantdb_local 单源（field_routing.yaml 主源改为 quantdb_local），baostock 仅保留作历史/备用。停用保留，后期可能用于旧链路回归。")
def test_routing_a_daily_kline_primary_baostock():
    rt = FieldRoutingTable()
    r = rt.get_route("A", "daily_kline")
    assert r.primary == "baostock"
    assert r.consensus is True
    assert "efinance" in r.fallbacks


@pytest.mark.skip(reason="HK 日线主源已在路由中调整（baostock 停注册，efinance 为 HK 共用）。停用保留，后期可能用于旧链路回归。")
def test_routing_hk_daily_kline_primary_efinance():
    rt = FieldRoutingTable()
    r = rt.get_route("HK", "daily_kline")
    assert r.primary == "efinance"


def test_routing_us_daily_kline_primary_yahoo():
    rt = FieldRoutingTable()
    r = rt.get_route("US", "daily_kline")
    assert r.primary == "yahoo_finance"


def test_routing_missing_field_raises():
    from backend.services.engine.data_platform.base import InvalidFieldRequest
    rt = FieldRoutingTable()
    with pytest.raises(InvalidFieldRequest):
        rt.get_route("A", "no_such_field")


# ---------------------------------------------------------------------------
# 适配器 mocks
# ---------------------------------------------------------------------------
def _make_df(symbol: str, close_seq: list[float], src: str = "src") -> pd.DataFrame:
    rows = []
    for i, c in enumerate(close_seq, start=2):
        rows.append({
            "symbol": symbol,
            "trade_date": date(2025, 1, i),
            "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
            "volume": 1000 * i, "amount": 1000 * i * c, "adj_factor": 1.0,
            "source": src,
        })
    return pd.DataFrame(rows)


class _OkAdapter(OfflineDataSourceAdapter):
    name = "ok_src"
    markets = ["A"]
    fields = {"daily_kline"}

    def fetch_daily(self, symbol, start, end, *, adjust="qfq"):
        return _make_df(symbol, [10.0, 10.1, 10.2], src=self.name)

    def fetch_meta(self, market):
        return pd.DataFrame()


class _FailAdapter(OfflineDataSourceAdapter):
    name = "fail_src"
    markets = ["A"]
    fields = {"daily_kline"}

    def fetch_daily(self, symbol, start, end, *, adjust="qfq"):
        raise DataUnavailable("nope")

    def fetch_meta(self, market):
        return pd.DataFrame()


class _DeviantAdapter(OfflineDataSourceAdapter):
    name = "deviant_src"
    markets = ["A"]
    fields = {"daily_kline"}

    def fetch_daily(self, symbol, start, end, *, adjust="qfq"):
        # 大幅偏离 (50%+)
        return _make_df(symbol, [15.0, 15.2, 15.4], src=self.name)

    def fetch_meta(self, market):
        return pd.DataFrame()


# 自定义路由表，避免依赖真实 YAML 中的 source 名
def _custom_routing(tmp_path, *, primary, fallbacks=None, consensus=False):
    import yaml as _yaml
    cfg = {
        "version": 1,
        "default_consensus_threshold": 0.02,
        "default_min_consensus_sources": 2,
        "markets": {
            "A": {
                "daily_kline": {
                    "tier": "T1",
                    "primary": primary,
                    "fallbacks": fallbacks or [],
                    "consensus": consensus,
                    "cleanup": True,
                }
            }
        }
    }
    p = tmp_path / "routing.yaml"
    p.write_text(_yaml.safe_dump(cfg), encoding="utf-8")
    return FieldRoutingTable(path=p)


# ---------------------------------------------------------------------------
# 聚合器：fallback 模式
# ---------------------------------------------------------------------------
def test_aggregator_primary_success(tmp_path):
    reg = SourceRegistry()
    reg.register(_OkAdapter)
    agg = FieldAggregator(
        registry=reg,
        routing=_custom_routing(tmp_path, primary="ok_src"),
        monitor=HealthMonitor(),
        cleaner=DataCleaner(),
    )
    res = agg.fetch(market="A", field="daily_kline", symbol="600519.SH",
                    start=date(2025, 1, 1), end=date(2025, 1, 5))
    assert res.source_used == "ok_src"
    assert not res.data.empty
    assert res.fallbacks_tried == []


def test_aggregator_falls_back(tmp_path):
    reg = SourceRegistry()
    reg.register(_FailAdapter)
    reg.register(_OkAdapter)
    agg = FieldAggregator(
        registry=reg,
        routing=_custom_routing(tmp_path, primary="fail_src", fallbacks=["ok_src"]),
        monitor=HealthMonitor(),
        cleaner=DataCleaner(),
    )
    res = agg.fetch(market="A", field="daily_kline", symbol="600519.SH",
                    start=date(2025, 1, 1), end=date(2025, 1, 5))
    assert res.source_used == "ok_src"
    assert "fail_src" in res.fallbacks_tried


def test_aggregator_all_fail_raises(tmp_path):
    reg = SourceRegistry()
    reg.register(_FailAdapter)
    agg = FieldAggregator(
        registry=reg,
        routing=_custom_routing(tmp_path, primary="fail_src"),
        monitor=HealthMonitor(),
        cleaner=DataCleaner(),
    )
    with pytest.raises(DataUnavailable):
        agg.fetch(market="A", field="daily_kline", symbol="X",
                  start=date(2025, 1, 1), end=date(2025, 1, 5))


# ---------------------------------------------------------------------------
# 聚合器：共识模式
# ---------------------------------------------------------------------------
def test_aggregator_consensus_filters_deviant(tmp_path):
    class _OkA(_OkAdapter):
        name = "src_a"
    class _OkB(_OkAdapter):
        name = "src_b"
    reg = SourceRegistry()
    reg.register(_OkA)
    reg.register(_OkB)
    reg.register(_DeviantAdapter)
    agg = FieldAggregator(
        registry=reg,
        routing=_custom_routing(
            tmp_path, primary="src_a",
            fallbacks=["src_b", "deviant_src"],
            consensus=True,
        ),
        monitor=HealthMonitor(),
        cleaner=DataCleaner(),
    )
    res = agg.fetch(market="A", field="daily_kline", symbol="X",
                    start=date(2025, 1, 1), end=date(2025, 1, 5))
    assert "src_a" in res.consensus_sources
    assert "src_b" in res.consensus_sources
    assert "deviant_src" not in res.consensus_sources
    assert "deviant_src" in res.fallbacks_tried


def test_consensus_deviation_helper():
    a = _make_df("X", [10.0, 10.0])
    b = _make_df("X", [10.0, 10.0])
    assert _consensus_deviation(a, b, on="close") == pytest.approx(0.0)
    c = _make_df("X", [11.0, 11.0])
    assert _consensus_deviation(a, c, on="close") == pytest.approx(0.1)


def test_merge_by_median_two_frames():
    a = _make_df("X", [10.0, 20.0])
    b = _make_df("X", [12.0, 22.0])
    merged = _merge_by_median([a, b], base_src="a")
    assert len(merged) == 2
    closes = sorted(merged["close"].tolist())
    assert closes == [11.0, 21.0]


# ---------------------------------------------------------------------------
# DataCleaner
# ---------------------------------------------------------------------------
def test_cleaner_l1_drops_missing_close():
    cleaner = DataCleaner()
    df = pd.DataFrame([
        {"symbol": "X", "trade_date": date(2025, 1, 2), "open": 10, "high": 11,
         "low": 9, "close": 10, "volume": 100},
        {"symbol": "X", "trade_date": date(2025, 1, 3), "open": 10, "high": 11,
         "low": 9, "close": None, "volume": 100},
    ])
    out, rpt = cleaner.clean(df, market="A", field="daily_kline")
    assert len(out) == 1
    assert rpt["rows_in"] == 2
    assert rpt["rows_out"] == 1


def test_cleaner_l2_marks_range_violation():
    cleaner = DataCleaner(strict=False)
    df = pd.DataFrame([
        {"symbol": "X", "trade_date": date(2025, 1, 2), "open": 10, "high": 8,
         "low": 9, "close": 10, "volume": 100},   # high < open: violation
    ])
    out, rpt = cleaner.clean(df, market="A", field="daily_kline")
    assert rpt["range_violations"] == 1
    assert "invalid_range" in out.columns
    assert bool(out.iloc[0]["invalid_range"])


def test_cleaner_l3_marks_outlier():
    cleaner = DataCleaner(strict=False)
    df = pd.DataFrame([
        {"symbol": "X", "trade_date": date(2025, 1, 2), "open": 10, "high": 10.5,
         "low": 9.5, "close": 10.0, "volume": 100},  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low
        {"symbol": "X", "trade_date": date(2025, 1, 3), "open": 100, "high": 110,
         "low": 95, "close": 105.0, "volume": 100},  # 10x change
    ])
    out, rpt = cleaner.clean(df, market="A", field="daily_kline")
    assert rpt["outliers_marked"] >= 1
    assert "outlier" in out.columns


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------
def test_calendar_factory_singletons():
    reset_calendars()
    a1 = get_calendar("A")
    a2 = get_calendar("A")
    assert a1 is a2
    assert isinstance(a1, ChinaACalendar)
    assert isinstance(get_calendar("HK"), HongKongCalendar)
    assert isinstance(get_calendar("US"), UnitedStatesCalendar)


def test_calendar_weekday_fallback_when_no_db():
    reset_calendars()
    cal = get_calendar("A")  # 无 db_loader
    # 2026-05-24 是周日 → False
    assert cal.is_trading_day(date(2026, 5, 24)) is False
    # 2026-05-25 是周一 → True (降级)
    assert cal.is_trading_day(date(2026, 5, 25)) is True


def test_calendar_db_loader_used():
    reset_calendars()
    def loader(market, d):
        # 模拟：2026-05-25 是节假日（虽然是周一）
        if d == date(2026, 5, 25):
            return {"is_trading": False, "is_half_day": False}
        return None  # 让其降级
    cal = get_calendar("A", db_loader=loader)
    assert cal.is_trading_day(date(2026, 5, 25)) is False
    assert cal.is_trading_day(date(2026, 5, 26)) is True   # 周二，降级


def test_calendar_next_prev_trading_day():
    reset_calendars()
    cal = get_calendar("A")
    # 周五 -> 下一交易日跳过周末
    fri = date(2026, 5, 22)
    assert cal.next_trading_day(fri) == date(2026, 5, 25)
    # 周一 -> 上一交易日跳到上周五
    mon = date(2026, 5, 25)
    assert cal.prev_trading_day(mon) == date(2026, 5, 22)


def test_calendar_market_open_close_timezones():
    reset_calendars()
    a = get_calendar("A")
    hk = get_calendar("HK")
    us = get_calendar("US")
    d = date(2026, 5, 25)
    assert str(a.market_open(d).tzinfo) == "Asia/Shanghai"
    assert str(hk.market_open(d).tzinfo) == "Asia/Hong_Kong"
    assert str(us.market_open(d).tzinfo) == "America/New_York"
    assert a.market_close(d).hour == 15
    assert us.market_close(d).hour == 16
