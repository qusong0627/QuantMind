"""D4 第二批适配器测试（akshare/mootdx/tdx_api/injoyai_tdx/simonlin_a_stock）。

策略同 D3：离线 + 不联网；libs 缺失时跳过实际调用。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend.services.engine.data_platform.adapters import list_known, register_all
from backend.services.engine.data_platform.adapters import (
    akshare_adapter, mootdx_adapter, tdx_api_adapter,
    injoyai_tdx_adapter, simonlin_a_stock_adapter,
)
from backend.services.engine.data_platform.base import (
    DataUnavailable, InvalidFieldRequest,
)


# ---------------------------------------------------------------------------
# 列表 / 注册
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="A股适配器已收敛为 quantdb_local 单源，tdx_api 等旧 A股适配器已停注册（HK/US 仍走 akshare/efinance/yahoo_finance）。停用保留，后期可能用于旧链路回归。")
def test_d4_modules_listed():
    names = list_known()
    for n in (
        "akshare_adapter", "mootdx_adapter", "tdx_api_adapter",
        "injoyai_tdx_adapter", "simonlin_a_stock_adapter",
    ):
        assert n in names


@pytest.mark.skip(reason="A股适配器已收敛为 quantdb_local 单源，tdx_api/simonlin_a_stock 已停注册。停用保留。")
def test_register_all_includes_d4():
    res = register_all()
    # tdx_api / simonlin_a_stock 无强依赖，恒注册
    assert res.get("tdx_api_adapter") is True
    assert res.get("simonlin_a_stock_adapter") is True


# ---------------------------------------------------------------------------
# akshare
# ---------------------------------------------------------------------------
def test_akshare_class_attrs():
    cls = akshare_adapter.AkshareAdapter
    assert cls.name == "akshare"
    assert set(cls.markets) == {"A", "HK", "US"}
    assert "futures_kline" in cls.fields
    assert "options_chain" in cls.fields


def test_akshare_symbol_helpers():
    from backend.services.engine.data_platform.adapters.akshare_adapter import (
        _ak_symbol_a, _ak_pure_code, _guess_a_exchange,
    )
    assert _ak_symbol_a("600519.SH") == "sh600519"
    assert _ak_symbol_a("000001.SZ") == "sz000001"
    assert _ak_pure_code("600519.SH") == "600519"
    assert _guess_a_exchange("600519") == "SH"
    assert _guess_a_exchange("000001") == "SZ"
    assert _guess_a_exchange("833533") == "BJ"


def test_akshare_unavailable_when_lib_missing():
    if akshare_adapter._AK_AVAILABLE:
        pytest.skip("akshare installed")
    a = akshare_adapter.AkshareAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


# ---------------------------------------------------------------------------
# mootdx
# ---------------------------------------------------------------------------
def test_mootdx_class_attrs():
    cls = mootdx_adapter.MootdxAdapter
    assert cls.name == "mootdx"
    assert cls.markets == ["A"]
    assert "daily_kline" in cls.fields
    assert "minute_kline" in cls.fields


def test_mootdx_unavailable_when_no_tdx_dir(monkeypatch):
    if not mootdx_adapter._MOOTDX_AVAILABLE:
        pytest.skip("mootdx not installed")
    monkeypatch.delenv("QM_TDX_DIR", raising=False)
    a = mootdx_adapter.MootdxAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


def test_mootdx_meta_not_supported():
    a = mootdx_adapter.MootdxAdapter()
    with pytest.raises(InvalidFieldRequest):
        a.fetch_meta("A")


# ---------------------------------------------------------------------------
# tdx_api
# ---------------------------------------------------------------------------
def test_tdx_api_class_attrs():
    cls = tdx_api_adapter.TdxApiAdapter
    assert cls.name == "tdx_api"
    assert cls.markets == ["A"]
    assert {"daily_kline", "minute_kline", "tick", "auction", "realtime_quote"} <= cls.fields


def test_tdx_api_market_code_split():
    from backend.services.engine.data_platform.adapters.tdx_api_adapter import _to_market_code
    assert _to_market_code("600519.SH") == (1, "600519")
    assert _to_market_code("000001.SZ") == (0, "000001")
    assert _to_market_code("600519") == (1, "600519")


def test_tdx_api_fails_when_server_unreachable():
    a = tdx_api_adapter.TdxApiAdapter()
    # 默认 base url 是 localhost:7708，CI/容器环境基本访问不到
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


# ---------------------------------------------------------------------------
# injoyai_tdx
# ---------------------------------------------------------------------------
def test_injoyai_class_attrs():
    cls = injoyai_tdx_adapter.InjoyaiTdxAdapter
    assert cls.name == "injoyai_tdx"
    assert cls.markets == ["A"]


def test_injoyai_unavailable_when_lib_missing():
    if injoyai_tdx_adapter._INJOY_AVAILABLE:
        pytest.skip("injoyai_tdx installed")
    a = injoyai_tdx_adapter.InjoyaiTdxAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


def test_injoyai_meta_not_supported():
    a = injoyai_tdx_adapter.InjoyaiTdxAdapter()
    with pytest.raises(InvalidFieldRequest):
        a.fetch_meta("A")


# ---------------------------------------------------------------------------
# simonlin_a_stock
# ---------------------------------------------------------------------------
def test_simonlin_class_attrs():
    cls = simonlin_a_stock_adapter.SimonLinAStockAdapter
    assert cls.name == "simonlin_a_stock"
    assert cls.markets == ["A"]
    assert "daily_kline" in cls.fields


def test_simonlin_unavailable_when_no_dir(monkeypatch):
    monkeypatch.delenv("QM_SIMONLIN_A_DIR", raising=False)
    a = simonlin_a_stock_adapter.SimonLinAStockAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


def test_simonlin_reads_local_parquet(tmp_path, monkeypatch):
    root = tmp_path / "simonlin"
    daily = root / "daily"
    daily.mkdir(parents=True)
    df = pd.DataFrame([
        {"symbol": "000001.SZ", "trade_date": pd.Timestamp("2025-01-02"),
         "open": 10, "high": 11, "low": 9.5, "close": 10.5,  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low
         "volume": 1000, "amount": 10500, "adj_factor": 1.0},
        {"symbol": "000001.SZ", "trade_date": pd.Timestamp("2025-01-03"),
         "open": 10.5, "high": 11.2, "low": 10.3, "close": 11.0,
         "volume": 1100, "amount": 12000, "adj_factor": 1.0},
    ])
    df.to_parquet(daily / "000001.SZ.parquet", index=False)
    monkeypatch.setenv("QM_SIMONLIN_A_DIR", str(root))
    a = simonlin_a_stock_adapter.SimonLinAStockAdapter()
    out = a.fetch_daily("000001.SZ", date(2025, 1, 1), date(2025, 1, 5))
    assert len(out) == 2
    assert out.iloc[1]["close"] == 11.0
    assert out.iloc[1]["source"] == "simonlin_a_stock"
