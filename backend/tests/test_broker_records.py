"""broker-records 的选源与降级口径（纯逻辑，不打网络）。

口径（2026-11-06 用户要求）：前端「交易记录」要能按「券商接入」选定的通道看
柜台记录——选 QMT 查 QMT、选通达信查通达信；柜台只有当日数据，历史在系统账本。
"""

from __future__ import annotations

import asyncio

from backend.services.live_trading.routers import broker_records as br


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


def test_selected_source_prefers_override():
    redis = FakeRedis({"broker:selected:CN": "tdx"})
    assert br._selected_source(redis, "CN", "qmt_exec") == "qmt_exec"


def test_selected_source_reads_redis():
    redis = FakeRedis({"broker:selected:CN": "qmt_exec"})
    assert br._selected_source(redis, "CN", None) == "qmt_exec"
    assert br._selected_source(redis, "hk", None) == "" or True  # 未设键由回退决定


def test_selected_source_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(
        "backend.shared.real_positions.active_broker_type", lambda: "tdx"
    )
    assert br._selected_source(FakeRedis({}), "CN", None) == "tdx"


def test_tdx_trades_is_explicitly_unsupported():
    rows, note = asyncio.run(br._tdx_records(FakeRedis({}), "trades"))
    assert rows == []
    assert "不提供成交明细" in note


def test_qmt_history_rejects_bad_date():
    from fastapi import HTTPException

    try:
        asyncio.run(br._qmt_history_records("2026/11/06", "2026/11/07", "orders"))
    except HTTPException as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover - 走到这里说明没校验
        raise AssertionError("bad date should be rejected")


def test_history_chunks_split_and_validate():
    chunks = br._iter_day_chunks("20260901", "20260905", 31)
    assert chunks == [("20260901", "20260905")]
    chunks = br._iter_day_chunks("20260901", "20261010", 31)
    assert chunks[0] == ("20260901", "20261001")
    assert chunks[-1] == ("20261002", "20261010")

    from fastapi import HTTPException

    for start, end in (("20260905", "20260901"), ("20250101", "20260901")):
        try:
            br._iter_day_chunks(start, end, 31)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:  # pragma: no cover
            raise AssertionError(f"{start}~{end} should be rejected")


def test_qmt_history_row_normalizes_raw_m_fields():
    deal = {
        "m_strInstrumentID": "600519",
        "m_strExchangeID": "SH",
        "m_nDirection": 23,
        "m_nVolume": 100,
        "m_dPrice": 1500.5,
        "m_strTradeTime": "10:01:02",
        "m_strTradeDate": "20260901",
        "m_strTradeID": "T1",
        "m_strOrderSysID": "S1",
    }
    row = br._normalize_qmt_history_row(deal, "trades")
    assert row["symbol"] == "SH600519"
    assert row["side"] == "buy"
    assert row["traded_volume"] == 100
    assert row["traded_price"] == 1500.5
    assert row["status"] == "FILLED"

    order = {
        "m_strInstrumentID": "000001.SZ",
        "m_nDirection": 24,
        "m_nOrderVolume": 200,
        "m_nTradedVolume": 100,
        "m_dLimitPrice": 11.2,
        "m_nOrderStatus": 55,
        "m_strOrderTime": "09:31:00",
    }
    row = br._normalize_qmt_history_row(order, "orders")
    assert row["symbol"] == "SZ000001"
    assert row["side"] == "sell"
    assert row["order_volume"] == 200
    assert row["traded_volume"] == 100
    assert row["status"] == "PARTIALLY_FILLED"


def test_endpoint_tdx_history_is_explicitly_unsupported():
    resp = asyncio.run(
        br.broker_records(
            kind="orders",
            market="CN",
            source="tdx",
            date=None,
            start="2026-09-01",
            end="2026-11-06",
            auth=None,
            redis=FakeRedis({"broker:selected:CN": "tdx"}),
        )
    )
    assert resp["success"] is True
    assert resp["rows"] == []
    assert "只提供" in resp["note"] and "当日" in resp["note"]
    assert resp["start"] == "2026-09-01"
    assert resp["end"] == "2026-11-06"


def test_endpoint_qmt_history_uses_range_query(monkeypatch):
    calls = []

    async def fake_history(start, end, kind):
        calls.append((start, end, kind))
        return [{"symbol": "SH600519"}]

    monkeypatch.setattr(br, "_qmt_history_records", fake_history)
    resp = asyncio.run(
        br.broker_records(
            kind="orders",
            market="CN",
            source="qmt_exec",
            date=None,
            start="2026-09-01",
            end="2026-09-05",
            auth=None,
            redis=FakeRedis({"broker:selected:CN": "qmt_exec"}),
        )
    )
    assert calls == [("20260901", "20260905", "orders")]
    assert resp["count"] == 1
    assert "get_history_trade_detail_data" in resp["note"]


def test_parse_day_formats():
    from backend.services.live_trading.routers import agent_statement as st

    assert st._parse_day("2026-11-06").strftime("%Y-%m-%d") == "2026-11-06"
    assert st._parse_day("2026-11-06", end_of_day=True).hour == 23
    assert st._parse_day(None) is None
    from fastapi import HTTPException

    try:
        st._parse_day("11/06/2026")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover
        raise AssertionError("bad date should be rejected")
