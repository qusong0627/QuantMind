"""LocalMarketData 单元测试。

不依赖网络或真实 parquet 数据；所有 DuckDB/parquet 交互通过 mock 隔离。
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backend.services.simulation.services.local_market_data import (
    DailyBar,
    LocalMarketData,
    _SCALE_CURRENT,
    _SCALE_LEGACY,
    _as_float,
    _detect_amount_scale,
    _from_dt_int,
    _to_dt_int,
    compute_limits,
    extract_st_symbols,
    limit_pct,
)


# ======================================================================
# compute_limits / limit_pct
# ======================================================================

class TestLimitPct:
    def test_main_board(self):
        assert limit_pct("600036.SH", is_st=False, trade_date=date(2026, 6, 1)) == Decimal("0.10")

    def test_chinext(self):
        assert limit_pct("300750.SZ", is_st=False, trade_date=date(2026, 6, 1)) == Decimal("0.20")

    def test_star(self):
        assert limit_pct("688981.SH", is_st=False, trade_date=date(2026, 6, 1)) == Decimal("0.20")

    def test_bse(self):
        assert limit_pct("830001.BJ", is_st=False, trade_date=date(2026, 6, 1)) == Decimal("0.30")

    def test_bse_prefix_92(self):
        assert limit_pct("920001.BJ", is_st=False, trade_date=date(2026, 6, 1)) == Decimal("0.30")

    def test_st_main_before_relaxation(self):
        assert limit_pct("000010.SZ", is_st=True, trade_date=date(2026, 7, 5)) == Decimal("0.05")

    def test_st_main_after_relaxation(self):
        assert limit_pct("000010.SZ", is_st=True, trade_date=date(2026, 7, 6)) == Decimal("0.10")

    def test_st_chinext_no_reduction(self):
        # 创业板 ST 不折减
        assert limit_pct("300XXX.SZ", is_st=True, trade_date=date(2026, 6, 1)) == Decimal("0.20")

    def test_st_star_no_reduction(self):
        assert limit_pct("688XXX.SH", is_st=True, trade_date=date(2026, 6, 1)) == Decimal("0.20")

    def test_st_bse_no_reduction(self):
        assert limit_pct("830001.BJ", is_st=True, trade_date=date(2026, 6, 1)) == Decimal("0.30")


class TestComputeLimits:
    def test_main_board_normal(self):
        up, down = compute_limits("600036.SH", 10.0, is_st=False, trade_date=date(2026, 6, 1))
        assert up == 11.0
        assert down == 9.0

    def test_main_board_st_pre_relaxation(self):
        up, down = compute_limits("000010.SZ", 2.0, is_st=True, trade_date=date(2026, 7, 5))
        assert up == 2.10
        assert down == 1.90

    def test_main_board_st_post_relaxation(self):
        up, down = compute_limits("000010.SZ", 2.0, is_st=True, trade_date=date(2026, 7, 6))
        assert up == 2.20
        assert down == 1.80

    def test_chinext_20pct(self):
        up, down = compute_limits("300750.SZ", 50.0, is_st=False, trade_date=date(2026, 6, 1))
        assert up == 60.0
        assert down == 40.0

    def test_star_20pct(self):
        up, down = compute_limits("688981.SH", 100.0, is_st=False, trade_date=date(2026, 6, 1))
        assert up == 120.0
        assert down == 80.0

    def test_bse_30pct_rounding(self):
        # BSE: 涨停截尾、跌停进位
        up, down = compute_limits("920000.BJ", 11.85, is_st=False, trade_date=date(2026, 6, 1))
        # 11.85 * 1.3 = 15.405 → 截尾 → 15.40
        assert up == 15.40
        # 11.85 * 0.7 = 8.295 → 进位 → 8.30
        assert down == 8.30

    def test_bse_no_fractional(self):
        up, down = compute_limits("830001.BJ", 10.0, is_st=False, trade_date=date(2026, 6, 1))
        assert up == 13.0
        assert down == 7.0

    def test_sse_half_up_rounding(self):
        # 10.78 * 1.1 = 11.858 → 四舍五入 → 11.86
        up, down = compute_limits("600000.SH", 10.78, is_st=False, trade_date=date(2026, 6, 1))
        assert up == 11.86

    def test_no_pre_close_new_listing(self):
        up, down = compute_limits("600000.SH", 0.0, is_st=False, trade_date=date(2026, 6, 1))
        assert up == math.inf
        assert down == 0.0

    def test_none_pre_close(self):
        up, down = compute_limits("600000.SH", None, is_st=False, trade_date=date(2026, 6, 1))
        assert up == math.inf
        assert down == 0.0

    def test_negative_pre_close(self):
        up, down = compute_limits("600000.SH", -5.0, is_st=False, trade_date=date(2026, 6, 1))
        assert up == math.inf
        assert down == 0.0


# ======================================================================
# _detect_amount_scale
# ======================================================================

class TestDetectAmountScale:
    def _make_df(self, close, volume, amount):
        return pd.DataFrame({"close": [close], "volume": [volume], "amount": [amount]})

    def test_legacy_scale(self):
        # close*volume/amount ≈ 1e4 → legacy
        df = self._make_df(10.0, 1e6, 1e3)
        assert _detect_amount_scale(df) == _SCALE_LEGACY

    def test_current_scale(self):
        # close*volume/amount ≈ 0.01 → current
        df = self._make_df(10.0, 1e2, 1e5)
        assert _detect_amount_scale(df) == _SCALE_CURRENT

    def test_empty_df(self):
        df = pd.DataFrame({"close": [], "volume": [], "amount": []})
        assert _detect_amount_scale(df) == _SCALE_CURRENT

    def test_zero_volume(self):
        df = self._make_df(10.0, 0, 0)
        assert _detect_amount_scale(df) == _SCALE_CURRENT


# ======================================================================
# extract_st_symbols
# ======================================================================

class TestExtractStSymbols:
    def test_flag_only(self):
        detail = pd.DataFrame({
            "Symbol": ["000001.SZ", "000010.SZ", "600036.SH"],
            "IsSTGP": ["0", "1", "0"],
            "Name": ["平安银行", "美丽生态", "招商银行"],
        })
        result = extract_st_symbols(detail)
        assert result == {"000010.SZ"}

    def test_name_only(self):
        detail = pd.DataFrame({
            "Symbol": ["000001.SZ", "000010.SZ"],
            "IsSTGP": ["0", "0"],
            "Name": ["平安银行", "*ST美丽"],
        })
        result = extract_st_symbols(detail)
        assert result == {"000010.SZ"}

    def test_large_string_dtype(self):
        # IsSTGP is large_string in real parquet; simulate with Arrow-backed
        # string that pd.to_numeric must handle. Use object dtype as proxy
        # since large_string requires pyarrow extension array.
        try:
            import pyarrow as pa

            isstgp = pd.array(["0", "1"], dtype=pd.ArrowDtype(pa.large_string()))
        except (ImportError, TypeError):
            isstgp = pd.array(["0", "1"], dtype=object)
        detail = pd.DataFrame({
            "Symbol": ["000001.SZ", "000010.SZ"],
            "IsSTGP": isstgp,
            "Name": ["平安银行", "美丽生态"],
        })
        result = extract_st_symbols(detail)
        assert result == {"000010.SZ"}

    def test_no_st_columns(self):
        detail = pd.DataFrame({"Symbol": ["000001.SZ"], "Name": ["平安银行"]})
        result = extract_st_symbols(detail)
        assert result == set()


# ======================================================================
# _as_float
# ======================================================================

class TestAsFloat:
    def test_normal(self):
        assert _as_float(3.14) == 3.14

    def test_none(self):
        assert _as_float(None) == 0.0

    def test_nan(self):
        assert _as_float(float("nan")) == 0.0

    def test_inf(self):
        assert _as_float(float("inf")) == 0.0

    def test_string(self):
        assert _as_float("abc") == 0.0


# ======================================================================
# _to_dt_int / _from_dt_int
# ======================================================================

class TestDtInt:
    def test_roundtrip(self):
        d = date(2026, 7, 28)
        assert _from_dt_int(_to_dt_int(d)) == d

    def test_value(self):
        assert _to_dt_int(date(2026, 7, 28)) == 20260728


# ======================================================================
# LocalMarketData._build_date (mocked hub)
# ======================================================================

class TestBuildDate:
    @staticmethod
    def _make_hub(scan_df, st_symbols=None, sessions=None):
        hub = MagicMock()
        hub.query.return_value = scan_df
        if st_symbols is not None:
            hub.fetch_stock_list.return_value = st_symbols
        else:
            hub.fetch_stock_list.return_value = pd.DataFrame()
        if sessions is not None:
            hub.query.side_effect = [
                scan_df,
                pd.DataFrame({"dt": sessions}),
            ]
        return hub

    def test_basic_bar_construction(self):
        scan_df = pd.DataFrame({
            "symbol": ["600036.SH", "600036.SH"],
            "dt": [20260717, 20260720],
            "open": [38.5, 38.6],
            "high": [39.0, 39.2],
            "low": [38.0, 38.4],
            "close": [38.65, 38.91],
            "volume": [1e6, 1e6],
            "amount": [1e3, 1e3],
        })
        hub = MagicMock()
        hub.query.return_value = scan_df
        hub.fetch_stock_list.return_value = pd.DataFrame()
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = [20260717, 20260720]
        bars = lmd._build_date(date(2026, 7, 20))
        bar = bars.get("600036.SH")
        assert bar is not None
        assert bar.close == 38.91
        assert bar.pre_close == 38.65
        assert bar.is_st is False
        assert bar.suspended is False
        # legacy scale: amount*1e4/volume
        assert bar.amount == pytest.approx(1e3 * 1e4)
        assert bar.volume == pytest.approx(1e6)

    def test_suspension_detection(self):
        scan_df = pd.DataFrame({
            "symbol": ["600036.SH", "600036.SH"],
            "dt": [20260717, 20260720],
            "open": [38.5, 0.0],
            "high": [39.0, 0.0],
            "low": [38.0, 0.0],
            "close": [38.65, 38.91],
            "volume": [1e6, 0.0],
            "amount": [1e3, 0.0],
        })
        hub = MagicMock()
        hub.query.return_value = scan_df
        hub.fetch_stock_list.return_value = pd.DataFrame()
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = [20260717, 20260720]
        bars = lmd._build_date(date(2026, 7, 20))
        assert bars["600036.SH"].suspended is True

    def test_st_flag(self):
        scan_df = pd.DataFrame({
            "symbol": ["000010.SZ", "000010.SZ"],
            "dt": [20260717, 20260720],
            "open": [1.6, 1.7],
            "high": [1.7, 1.8],
            "low": [1.5, 1.6],
            "close": [1.69, 1.76],
            "volume": [1e6, 1e6],
            "amount": [1e3, 1e3],
        })
        st_detail = pd.DataFrame({
            "Symbol": ["000010.SZ"],
            "IsSTGP": ["1"],
            "Name": ["*ST美丽"],
        })
        hub = MagicMock()
        hub.query.return_value = scan_df
        hub.fetch_stock_list.return_value = st_detail
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = [20260717, 20260720]
        bars = lmd._build_date(date(2026, 7, 20))
        assert bars["000010.SZ"].is_st is True

    def test_current_scale_vwap(self):
        # current scale: volume=手, amount=元
        scan_df = pd.DataFrame({
            "symbol": ["600036.SH", "600036.SH"],
            "dt": [20260717, 20260728],
            "open": [39.0, 39.5],
            "high": [39.5, 40.0],
            "low": [38.5, 39.0],
            "close": [39.0, 39.59],
            "volume": [1e5, 1e5],
            "amount": [4e6, 4e6],
        })
        hub = MagicMock()
        hub.query.return_value = scan_df
        hub.fetch_stock_list.return_value = pd.DataFrame()
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = [20260717, 20260728]
        bars = lmd._build_date(date(2026, 7, 28))
        bar = bars["600036.SH"]
        # volume 手→股: 1e5*100=1e7; amount已是元: 4e6
        assert bar.volume == pytest.approx(1e7)
        assert bar.amount == pytest.approx(4e6)
        # vwap = 4e6/1e7 = 0.4 → clearly wrong; this is because the test
        # data is synthetic. Real data would have amount ≈ close*volume.
        # The scale detection is the key thing tested here.

    def test_cache_hit(self):
        scan_df = pd.DataFrame({
            "symbol": ["600036.SH"],
            "dt": [20260720],
            "open": [38.6],
            "high": [39.2],
            "low": [38.4],
            "close": [38.91],
            "volume": [1e6],
            "amount": [1e3],
        })
        hub = MagicMock()
        hub.query.return_value = scan_df
        hub.fetch_stock_list.return_value = pd.DataFrame()
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = [20260720]
        lmd.load_date(date(2026, 7, 20))
        lmd.load_date(date(2026, 7, 20))
        # query should be called once (cache hit on second call)
        assert hub.query.call_count == 1

    def test_no_data_returns_empty(self):
        hub = MagicMock()
        hub.query.return_value = pd.DataFrame()
        hub.fetch_stock_list.return_value = pd.DataFrame()
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = []
        bars = lmd._build_date(date(2026, 7, 20))
        assert bars == {}

    def test_get_bar_normalizes_symbol(self):
        scan_df = pd.DataFrame({
            "symbol": ["600036.SH"],
            "dt": [20260720],
            "open": [38.6],
            "high": [39.2],
            "low": [38.4],
            "close": [38.91],
            "volume": [1e6],
            "amount": [1e3],
        })
        hub = MagicMock()
        hub.query.return_value = scan_df
        hub.fetch_stock_list.return_value = pd.DataFrame()
        lmd = LocalMarketData(hub=hub)
        lmd._session_dates = [20260720]
        bar = lmd.get_bar("SH600036", date(2026, 7, 20))
        assert bar is not None
        assert bar.symbol == "600036.SH"


# ======================================================================
# 分区直读：交易日枚举 + 单日取数（真实 tmp_path parquet）
# ======================================================================

class _FakeHub:
    """只暴露 LocalMarketData 需要的三个成员，不碰 DuckDB。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.queries: list[str] = []
        self._detail = pd.DataFrame()

    def query(self, sql: str) -> pd.DataFrame:
        self.queries.append(sql)
        return pd.DataFrame()

    def fetch_stock_list(self) -> pd.DataFrame:
        return self._detail


def _write_partition(root, dt_int: int, rows: pd.DataFrame | None) -> None:
    """按数据平台落盘布局写 dt=<YYYYMMDD>/data.parquet。"""
    part = root / "1_kline_data" / "daily_unadjusted" / f"dt={dt_int}"
    part.mkdir(parents=True, exist_ok=True)
    if rows is None:  # 同步中断：只留下空目录
        return
    rows.to_parquet(part / "data.parquet", index=False)


_DAILY = pd.DataFrame({
    "symbol": ["600036.SH", "000001.SZ"],
    "time": pd.to_datetime(["2026-07-17", "2026-07-17"]),
    "open": [38.5, 11.5],
    "high": [39.0, 11.9],
    "low": [38.0, 11.4],
    "close": [38.65, 11.8],
    "vol_in_stock": [1e6, 2e6],
    "amount": [1e3, 2e3],
})


class TestPartitionSessions:
    def test_sessions_come_from_partition_dirs_without_duckdb(self, tmp_path):
        hub = _FakeHub(tmp_path)
        for dt_int in (20260720, 20260717, 20260721):
            _write_partition(tmp_path, dt_int, _DAILY)
        lmd = LocalMarketData(hub=hub, market="CN")

        assert lmd._sessions() == [20260717, 20260720, 20260721]
        # 关键回归点：交易日枚举绝不走 "SELECT DISTINCT dt"（全分区扫描）
        assert hub.queries == []

    def test_trailing_empty_partition_is_dropped(self, tmp_path):
        for dt_int in (20260720, 20260721):
            _write_partition(tmp_path, dt_int, _DAILY)
        _write_partition(tmp_path, 20260722, None)  # 空目录
        lmd = LocalMarketData(hub=_FakeHub(tmp_path), market="CN")

        assert lmd._sessions() == [20260720, 20260721]
        assert lmd.latest_trade_date() == date(2026, 7, 21)

    def test_flat_per_symbol_files_are_ignored(self, tmp_path):
        # daily_unadjusted 目录同时存在 5000+ 个 per-symbol 平铺文件，
        # 它们不是交易日分区，不能进 sessions
        _write_partition(tmp_path, 20260720, _DAILY)
        (tmp_path / "1_kline_data" / "daily_unadjusted" / "600036.SH.parquet").write_bytes(b"x")
        lmd = LocalMarketData(hub=_FakeHub(tmp_path), market="CN")

        assert lmd._sessions() == [20260720]

    def test_sessions_are_cached_then_refreshed_after_ttl(self, tmp_path):
        _write_partition(tmp_path, 20260720, _DAILY)
        lmd = LocalMarketData(hub=_FakeHub(tmp_path), market="CN")
        assert lmd._sessions() == [20260720]

        _write_partition(tmp_path, 20260721, _DAILY)
        assert lmd._sessions() == [20260720]  # TTL 内仍用缓存

        with patch(
            "backend.services.simulation.services.local_market_data._SESSIONS_TTL_SEC", 0.0
        ):
            assert lmd._sessions() == [20260720, 20260721]

    def test_non_hive_layout_falls_back_to_view(self):
        hub = MagicMock()
        hub.query.return_value = pd.DataFrame({"dt": [20260717, 20260720]})
        lmd = LocalMarketData(hub=hub, market="HK")

        assert lmd._sessions() == [20260717, 20260720]
        sql = hub.query.call_args_list[0].args[0]
        assert "DISTINCT dt" in sql and "qhk_daily_forward" in sql


class TestPartitionScan:
    def test_build_date_reads_only_target_partitions(self, tmp_path):
        _write_partition(tmp_path, 20260717, _DAILY)
        _write_partition(tmp_path, 20260720, _DAILY.assign(close=[39.31, 12.1]))
        # 干扰分区：既不该被读、也不该进交易日（无数据）
        _write_partition(tmp_path, 20260719, None)
        hub = _FakeHub(tmp_path)
        lmd = LocalMarketData(hub=hub, market="CN")

        bars = lmd.load_date(date(2026, 7, 20))

        assert hub.queries == []  # 全程不碰 DuckDB
        assert bars["600036.SH"].close == 39.31
        assert bars["600036.SH"].pre_close == 38.65  # 来自 dt=20260717
        assert bars["600036.SH"].limit_up == 42.52  # 38.65*1.1=42.515 → 四舍五入到分
        assert bars["000001.SZ"].pre_close == 11.8
        assert bars["600036.SH"].volume == 1e6  # vol_in_stock 别名已归一

    def test_missing_partition_returns_empty_without_view_query(self, tmp_path):
        _write_partition(tmp_path, 20260720, _DAILY)
        hub = _FakeHub(tmp_path)
        lmd = LocalMarketData(hub=hub, market="CN")

        assert lmd.load_date(date(2026, 7, 2)) == {}
        assert hub.queries == []

    def test_bad_columns_fall_back_to_view(self, tmp_path):
        _write_partition(tmp_path, 20260720, pd.DataFrame({"foo": [1]}))
        hub = _FakeHub(tmp_path)
        hub.query = lambda sql: pd.DataFrame({
            "symbol": ["600036.SH"],
            "dt": [20260720],
            "open": [38.6],
            "high": [39.2],
            "low": [38.4],
            "close": [38.91],
            "volume": [1e6],
            "amount": [1e3],
        })
        lmd = LocalMarketData(hub=hub, market="CN")
        lmd._session_dates = [20260720]

        bars = lmd._build_date(date(2026, 7, 20))

        assert bars["600036.SH"].close == 38.91
