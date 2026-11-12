"""EOD 取价源与权益字段同步回归（纯函数 / 源码口径，无 DB/Redis 依赖）。

背景：stock_daily_latest 只在市场数据同步开启后刷新，未配置时会长期停在旧
日期；EOD 日终若只用它重估，会把权益打回 T-1。修复后 EOD 优先 local_market_data
（权威日线源），PG 仅兜底。
"""

from __future__ import annotations

import asyncio
from datetime import date

from backend.services.simulation.services import eod_service as eod
from backend.services.simulation.services.equity_settlement_worker import (
    _REMARK_LUA,
)
from backend.services.trade_shared.simulation_manager import SimulationAccountManager


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeSession:
    """只服务 PG 兜底路径：按 symbol 返回 (close, adj_factor) 行。"""

    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol

    async def execute(self, _query, params):
        return _FakeResult(self.rows_by_symbol.get(params["symbol"]))


class _FakeBar:
    def __init__(self, close: float = 9.87):
        self.close = close


class _FakeLocalMarketData:
    def latest_trade_date(self, on_or_before=None):
        return date(2026, 9, 23)

    def get_bar(self, _symbol, _trade_date):
        return _FakeBar()


def _patch_local(monkeypatch, lmd):
    monkeypatch.setattr(
        "backend.services.simulation.services.local_market_data.get_local_market_data",
        lambda market=None: lmd,
    )


def test_local_close_price_uses_local_parquet(monkeypatch):
    _patch_local(monkeypatch, _FakeLocalMarketData())
    assert eod._local_close_price_sync("SZ000419") == 9.87


def test_local_close_price_missing_bar_returns_zero(monkeypatch):
    class _Empty(_FakeLocalMarketData):
        def get_bar(self, _symbol, _trade_date):
            return None

    _patch_local(monkeypatch, _Empty())
    assert eod._local_close_price_sync("SZ000419") == 0.0


def test_local_close_price_no_trade_date_returns_zero(monkeypatch):
    class _NoDate(_FakeLocalMarketData):
        def latest_trade_date(self, on_or_before=None):
            return None

    _patch_local(monkeypatch, _NoDate())
    assert eod._local_close_price_sync("SZ000419") == 0.0


def test_load_close_price_prefers_local(monkeypatch):
    monkeypatch.setattr(eod, "_local_close_price_sync", lambda _symbol: 9.87)
    # 本地有价时不应触碰 PG session
    session = _FakeSession({})
    assert asyncio.run(eod._load_close_price(session, "SZ000419")) == 9.87


def test_load_close_price_falls_back_to_pg(monkeypatch):
    monkeypatch.setattr(eod, "_local_close_price_sync", lambda _symbol: 0.0)
    session = _FakeSession({"SZ000419": (4.98, 1.0)})
    assert asyncio.run(eod._load_close_price(session, "SZ000419")) == 4.98


def test_remark_lua_syncs_equity_with_total_asset():
    assert "account.equity = account.total_asset" in _REMARK_LUA


class _DatePinnedLocal:
    """按 trade_date 返回不同收盘价，用于锁定「结算日取当日价」口径。"""

    def __init__(self):
        self.calls: list[date | None] = []

    def latest_trade_date(self, on_or_before=None):
        return date(2026, 11, 10)

    def get_bar(self, _symbol, trade_date):
        self.calls.append(trade_date)
        if trade_date == date(2026, 11, 11):
            return _FakeBar(13.08)
        if trade_date == date(2026, 11, 10):
            return _FakeBar(12.68)
        return None


def test_local_close_price_pins_target_trade_date(monkeypatch):
    """EOD 结算 11-11 必须取 11-11 收盘价，不能退化成「最新可得日」。"""
    lmd = _DatePinnedLocal()
    _patch_local(monkeypatch, lmd)
    assert eod._local_close_price_sync("SZ001202", date(2026, 11, 11)) == 13.08
    assert lmd.calls == [date(2026, 11, 11)]


def test_local_close_price_target_date_missing_returns_zero(monkeypatch):
    """目标日无日线时返回 0（由可用性护栏延后结算），不得回退到 T-1 价格。"""
    lmd = _DatePinnedLocal()
    _patch_local(monkeypatch, lmd)
    assert eod._local_close_price_sync("SZ001202", date(2026, 11, 12)) == 0.0


def test_load_close_price_pg_fallback_filters_trade_date(monkeypatch):
    """PG 兜底也必须按 trade_date 过滤，不能取最新一行。"""
    captured: dict[str, object] = {}

    class _Session:
        async def execute(self, query, params):
            captured["sql"] = str(query)
            captured["params"] = dict(params)
            return _FakeResult(None)

    monkeypatch.setattr(eod, "_local_close_price_sync", lambda _s, _d=None: 0.0)
    asyncio.run(
        eod._load_close_price(_Session(), "SZ001202", date(2026, 11, 11))
    )
    assert "trade_date = :trade_date" in captured["sql"]
    assert captured["params"]["trade_date"] == date(2026, 11, 11)


def test_daily_bar_available_true_when_local_latest_reaches_target(monkeypatch):
    monkeypatch.setattr(
        eod, "_local_latest_trade_date_sync", lambda: date(2026, 11, 11)
    )
    assert asyncio.run(
        eod._daily_bar_available(_FakeSession({}), date(2026, 11, 11))
    )


def test_daily_bar_available_false_when_target_not_synced(monkeypatch):
    """D 当天 15:00 触发时 D 的日线尚未落盘 → 必须判定不可用（延后结算）。"""

    class _EmptySession:
        async def execute(self, _query, _params=None):
            return _FakeResult(None)

    monkeypatch.setattr(
        eod, "_local_latest_trade_date_sync", lambda: date(2026, 11, 10)
    )
    assert not asyncio.run(
        eod._daily_bar_available(_EmptySession(), date(2026, 11, 11))
    )


def test_daily_bar_available_true_via_pg_when_local_stale(monkeypatch):
    class _HitSession:
        async def execute(self, query, params=None):
            assert params["trade_date"] == date(2026, 11, 11)
            return _FakeResult((1,))

    monkeypatch.setattr(
        eod, "_local_latest_trade_date_sync", lambda: date(2026, 11, 10)
    )
    assert asyncio.run(
        eod._daily_bar_available(_HitSession(), date(2026, 11, 11))
    )


class _CountResult:
    def __init__(self, n):
        self._n = n

    def scalar_one_or_none(self):
        return self._n


class _CountSession:
    def __init__(self, n):
        self._n = n

    async def execute(self, _stmt):
        return _CountResult(self._n)


def test_eod_position_snapshot_exists_true():
    ok = asyncio.run(
        eod._eod_position_snapshot_exists(
            _CountSession(3), "default", "10000001", date(2026, 9, 23)
        )
    )
    assert ok is True


def test_eod_position_snapshot_exists_false():
    ok = asyncio.run(
        eod._eod_position_snapshot_exists(
            _CountSession(0), "default", "10000001", date(2026, 9, 23)
        )
    )
    assert ok is False


def test_eod_position_snapshot_exists_error_returns_false():
    class _Boom:
        async def execute(self, _stmt):
            raise RuntimeError("db down")

    ok = asyncio.run(
        eod._eod_position_snapshot_exists(
            _Boom(), "default", "10000001", date(2026, 9, 23)
        )
    )
    assert ok is False


def test_trade_lua_syncs_equity_with_total_asset():
    lua = SimulationAccountManager(redis=None)._update_balance_lua
    assert "account.equity = account.total_asset" in lua
