"""D3 适配器骨架测试。

策略：
- 不联网；不依赖适配器底层库（baostock/efinance/qstock/mootdx）的具体行为
- 校验 register() 在依赖缺失时静默返回 False
- 校验类属性（name / markets / fields）+ supports() 行为
- 校验 symbol 转换 / 关键内部 helper

真实联网烟测留给 D8 cron。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.data_platform.adapters import (
    register_all,
    list_known,
)
from backend.services.engine.data_platform.adapters import (
    baostock_adapter, efinance_adapter, qstock_adapter,
    investment_data_adapter, eltdx_adapter,
)


@pytest.mark.skip(reason="A股适配器已收敛为 quantdb_local 单源，baostock/qstock/investment_data/eltdx 已停注册。停用保留，后期可能用于旧链路回归。")
def test_list_known_has_all_five():
    names = list_known()
    for n in (
        "baostock_adapter", "efinance_adapter", "qstock_adapter",
        "investment_data_adapter", "eltdx_adapter",
    ):
        assert n in names


@pytest.mark.skip(reason="A股适配器已收敛为 quantdb_local 单源，investment_data 等旧适配器已停注册。停用保留。")
def test_register_all_returns_dict():
    res = register_all()
    assert isinstance(res, dict)
    # investment_data 无第三方依赖，必须成功
    assert res.get("investment_data_adapter") is True


# ---------------------------------------------------------------------------
# baostock
# ---------------------------------------------------------------------------
def test_baostock_class_attrs():
    cls = baostock_adapter.BaostockAdapter
    assert cls.name == "baostock"
    assert cls.markets == ["A"]
    assert "daily_kline" in cls.fields
    assert "adj_factor" in cls.fields


def test_baostock_symbol_conversion():
    from backend.services.engine.data_platform.adapters.baostock_adapter import (
        _from_bs_symbol, _to_bs_symbol,
    )
    assert _to_bs_symbol("600519.SH") == "sh.600519"
    assert _to_bs_symbol("000001.SZ") == "sz.000001"
    assert _from_bs_symbol("sh.600519") == "600519.SH"


def test_baostock_supports():
    a = baostock_adapter.BaostockAdapter()
    assert a.supports("daily_kline", "A")
    assert not a.supports("daily_kline", "HK")
    assert not a.supports("realtime_quote", "A")


# ---------------------------------------------------------------------------
# efinance
# ---------------------------------------------------------------------------
def test_efinance_class_attrs():
    cls = efinance_adapter.EfinanceAdapter
    assert cls.name == "efinance"
    assert set(cls.markets) == {"A", "HK", "US"}
    assert "daily_kline" in cls.fields
    assert "minute_kline" in cls.fields
    assert "money_flow" in cls.fields


def test_efinance_guess_exchange():
    from backend.services.engine.data_platform.adapters.efinance_adapter import _guess_exchange
    assert _guess_exchange("600519", "A") == "SH"
    assert _guess_exchange("000001", "A") == "SZ"
    assert _guess_exchange("833533", "A") == "BJ"
    assert _guess_exchange("00700", "HK") == "HK"


def test_efinance_normalize_kline():
    from backend.services.engine.data_platform.adapters.efinance_adapter import _normalize_kline
    raw = pd.DataFrame([
        {"日期": "2025-01-02", "开盘": 10.0, "收盘": 10.5, "最高": 11.0,
         "最低": 9.9, "成交量": 100, "成交额": 1050},  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的「最低」
    ])
    df = _normalize_kline(raw, symbol="600519.SH", source="efinance")
    assert df.iloc[0]["close"] == 10.5
    assert df.iloc[0]["symbol"] == "600519.SH"
    assert df.iloc[0]["source"] == "efinance"
    assert df.iloc[0]["adj_factor"] == 1.0


def test_efinance_unavailable_when_lib_missing():
    if efinance_adapter._EF_AVAILABLE:
        pytest.skip("efinance installed; cannot test missing-lib path")
    from datetime import date
    from backend.services.engine.data_platform.base import DataUnavailable
    a = efinance_adapter.EfinanceAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


# ---------------------------------------------------------------------------
# qstock
# ---------------------------------------------------------------------------
def test_qstock_class_attrs():
    cls = qstock_adapter.QstockAdapter
    assert cls.name == "qstock"
    assert cls.markets == ["A"]
    assert "hot_signal" in cls.fields
    assert "news" in cls.fields


def test_qstock_guess_exchange():
    from backend.services.engine.data_platform.adapters.qstock_adapter import _guess_exchange
    assert _guess_exchange("600519") == "SH"
    assert _guess_exchange("000001") == "SZ"
    assert _guess_exchange("833533") == "BJ"


# ---------------------------------------------------------------------------
# investment_data
# ---------------------------------------------------------------------------
def test_investment_data_class_attrs():
    cls = investment_data_adapter.InvestmentDataAdapter
    assert cls.name == "investment_data"
    assert cls.markets == ["A"]
    assert {"daily_kline", "adj_factor", "dividend", "financial_report"} <= cls.fields


def test_investment_data_unavailable_when_no_dir(monkeypatch):
    monkeypatch.delenv("QM_INVESTMENT_DATA_DIR", raising=False)
    from datetime import date
    from backend.services.engine.data_platform.base import DataUnavailable
    a = investment_data_adapter.InvestmentDataAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


def test_investment_data_reads_parquet(tmp_path, monkeypatch):
    """构造真实 parquet → adapter 能读出。"""
    root = tmp_path / "investment_data"
    daily_dir = root / "parquet" / "cn_stock_daily"
    daily_dir.mkdir(parents=True)
    df = pd.DataFrame([
        {"symbol": "600519.SH", "trade_date": pd.Timestamp("2025-01-02"),
         "open": 1500, "high": 1520, "low": 1495, "close": 1510,
         "volume": 100000, "amount": 1.5e8, "adj_factor": 1.0},
        {"symbol": "600519.SH", "trade_date": pd.Timestamp("2025-01-03"),
         "open": 1510, "high": 1530, "low": 1505, "close": 1525,
         "volume": 110000, "amount": 1.67e8, "adj_factor": 1.0},
    ])
    df.to_parquet(daily_dir / "600519.SH.parquet", index=False)

    monkeypatch.setenv("QM_INVESTMENT_DATA_DIR", str(root))
    from datetime import date
    a = investment_data_adapter.InvestmentDataAdapter()
    out = a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))
    assert len(out) == 2
    assert out.iloc[0]["source"] == "investment_data"
    assert out.iloc[1]["close"] == 1525


def test_investment_data_filter_by_date_range(tmp_path, monkeypatch):
    root = tmp_path / "investment_data"
    d = root / "parquet" / "cn_stock_daily"
    d.mkdir(parents=True)
    df = pd.DataFrame([
        {"symbol": "X.SH", "trade_date": pd.Timestamp("2025-01-02"),
         "open": 10, "high": 11, "low": 9, "close": 10.5,
         "volume": 1, "amount": 10, "adj_factor": 1.0},
        {"symbol": "X.SH", "trade_date": pd.Timestamp("2025-01-10"),
         "open": 10, "high": 11, "low": 9, "close": 11.5,
         "volume": 1, "amount": 10, "adj_factor": 1.0},
    ])
    df.to_parquet(d / "X.SH.parquet", index=False)
    monkeypatch.setenv("QM_INVESTMENT_DATA_DIR", str(root))
    from datetime import date
    a = investment_data_adapter.InvestmentDataAdapter()
    out = a.fetch_daily("X.SH", date(2025, 1, 1), date(2025, 1, 5))
    assert len(out) == 1
    assert out.iloc[0]["close"] == 10.5


# ---------------------------------------------------------------------------
# eltdx
# ---------------------------------------------------------------------------
def test_eltdx_class_attrs():
    cls = eltdx_adapter.EltdxAdapter
    assert cls.name == "eltdx"
    assert cls.markets == ["A"]
    assert {"daily_kline", "minute_kline", "tick", "auction", "realtime_quote"} <= cls.fields


def test_eltdx_split_symbol():
    from backend.services.engine.data_platform.adapters.eltdx_adapter import _split_symbol
    assert _split_symbol("600519.SH") == ("600519", 1)
    assert _split_symbol("000001.SZ") == ("000001", 0)
    assert _split_symbol("600519")[1] == 1


def test_eltdx_unavailable_when_lib_missing():
    if eltdx_adapter._ELTDX_AVAILABLE:
        pytest.skip("mootdx installed")
    from datetime import date
    from backend.services.engine.data_platform.base import DataUnavailable
    a = eltdx_adapter.EltdxAdapter()
    with pytest.raises(DataUnavailable):
        a.fetch_daily("600519.SH", date(2025, 1, 1), date(2025, 1, 5))


# ---------------------------------------------------------------------------
# 集成：register_all() 后 registry 应可用
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="A股适配器已收敛为 quantdb_local 单源，investment_data 已停注册。停用保留，后期可能用于旧链路回归。")
def test_registered_sources_visible_in_registry():
    register_all()
    from backend.services.engine.data_platform.registry import get_registry
    reg = get_registry()
    sources = reg.list_sources()
    # investment_data 至少应存在（无外部依赖）
    assert "investment_data" in sources
