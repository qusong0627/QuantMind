"""每日日结（智能体台账口径）单测——纯计算，不连库。

口径（2026-11-06 用户确认）：以**智能体台账**为准（qm_agent_ledger_*），
执行事实（orders/trades）提供委托状态/费用/执行券商，真实账户快照只作资金锚。
金额只认成交，不认委托。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.services.trade.services import daily_settlement as ds


class _Result:
    def __init__(self, rows):
        self._rows = list(rows or [])
        self._mapped = True

    def mappings(self):
        self._mapped = True
        return self

    def one(self):
        return self._rows[0]

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows

    def fetchall(self):
        return self._rows


class FakeSession:
    """按 SQL 关键词返回预置行；同时记录 execute 次数。"""

    def __init__(
        self,
        *,
        orders=None,
        brokers=None,
        trades=None,
        fills=None,
        roundtrips=None,
        positions=None,
        snapshot=None,
        snapshots=None,
    ):
        self._orders = orders
        self._brokers = brokers or []
        self._trades = trades
        self._fills = fills or []
        self._roundtrips = roundtrips or []
        self._positions = positions or []
        self._snapshots = (
            snapshots if snapshots is not None else ([snapshot] if snapshot else [])
        )
        self.calls = 0
        self.committed = 0

    async def execute(self, clause, params=None):
        self.calls += 1
        sql = str(clause)
        if "SET LOCAL" in sql or "CREATE TABLE" in sql:
            return _Result([])
        if "INSERT INTO qm_daily_settlements" in sql:
            self.upserted = params
            return _Result([])
        if "qm_agent_ledger_fill" in sql:
            return _Result(self._fills)
        if "qm_agent_ledger_roundtrip" in sql:
            return _Result(self._roundtrips)
        if "qm_agent_ledger_position" in sql:
            return _Result(self._positions)
        if "real_account_snapshots" in sql:
            return _Result(self._snapshots)
        if "FROM trades" in sql:
            return _Result([self._trades])
        if "GROUP BY 1 ORDER BY 2 DESC" in sql:
            return _Result(self._brokers)
        if "FROM orders" in sql:
            return _Result([self._orders])
        raise AssertionError(f"unexpected SQL: {sql[:120]}")

    async def commit(self):
        self.committed += 1


DAY = date(2026, 11, 6)


def test_day_window_is_shanghai_wall_clock():
    start, end = ds.day_window(DAY)
    assert start == datetime(2026, 11, 6, 0, 0)
    assert end == datetime(2026, 11, 7, 0, 0)
    aware_start, aware_end = ds.aware_day_window(DAY)
    assert aware_start.utcoffset().total_seconds() == 8 * 3600
    assert aware_end - aware_start == end - start


@pytest.mark.asyncio
async def test_compute_day_books_from_agent_ledger_and_trades():
    session = FakeSession(
        orders={
            "total": 5,
            "filled": 3,
            "partial": 1,
            "cancelled": 0,
            "rejected": 1,
            "pending": 1,
            "filled_value": 123456.78,
        },
        brokers=[
            {"broker": "tdx", "n": 4, "v": 100000.0},
            {"broker": "", "n": 1, "v": 23456.78},
        ],
        trades={
            "value": 123456.78,
            "buy_value": 80000.0,
            "sell_value": 43456.78,
            "commission": 120.0,
            "stamp_duty": 43.45,
            "transfer_fee": 1.2,
        },
        fills=[
            {
                "agent": "deepseek-v4-flash",
                "fills": 2,
                "buy_vol": 1000,
                "sell_vol": 500,
                "buy_amt": 80000.0,
                "sell_amt": 43456.78,
            },
            {
                "agent": "glm-5.3-flash",
                "fills": 1,
                "buy_vol": 0,
                "sell_vol": 0,
                "buy_amt": 0,
                "sell_amt": 0,
            },
        ],
        roundtrips=[{"agent": "deepseek-v4-flash", "n": 1, "pnl": 2345.6}],
        positions=[
            {
                "agent": "deepseek-v4-flash",
                "code": "SH600183",
                "volume": 1100,
                "cost_price": 25.5,
                "updated_at": "2026-11-06 15:05:00",
            },
        ],
        snapshot={
            "cash": 22932177.87,
            "market_value": 821632.0,
            "total_asset": 23773059.87,
            "source": "qmt_exec",
            "snapshot_at": "2026-11-06 15:05:00",
        },
    )
    out = await ds.compute_day(
        session, tenant_id="default", user_id="10000001", day=DAY
    )

    assert out["orders_total"] == 5 and out["orders_filled"] == 3
    assert out["filled_value"] == 123456.78
    assert out["buy_value"] == 80000.0 and out["sell_value"] == 43456.78
    # 费用合计与净现金流：卖出 − 买入 − 三费
    assert out["total_fee"] == pytest.approx(120.0 + 43.45 + 1.2)
    assert out["net_cash_flow"] == pytest.approx(43456.78 - 80000.0 - 164.65)
    # 分账：成交 + 平仓盈亏落在 agent 上
    agent = out["agents_json"]["deepseek-v4-flash"]
    assert agent["fills"] == 2 and agent["roundtrips"] == 1
    assert agent["realized_pnl"] == 2345.6
    assert out["realized_pnl"] == 2345.6
    # 执行券商分布：空值归「(未标记)」（历史单）
    assert out["brokers_json"]["tdx"]["orders"] == 4
    assert out["brokers_json"]["(未标记)"]["orders"] == 1
    # 资金锚 + 状态
    assert out["total_asset"] == 23773059.87
    assert out["status"] == "closed"
    assert out["recon_json"]["snapshot_source"] == "qmt_exec"
    assert out["has_activity"] is True


@pytest.mark.asyncio
async def test_compute_day_without_snapshot_is_honest():
    session = FakeSession(
        orders={
            "total": 0,
            "filled": 0,
            "partial": 0,
            "cancelled": 0,
            "rejected": 0,
            "pending": 0,
            "filled_value": 0,
        },
        trades={
            "value": 0,
            "buy_value": 0,
            "sell_value": 0,
            "commission": 0,
            "stamp_duty": 0,
            "transfer_fee": 0,
        },
    )
    out = await ds.compute_day(
        session, tenant_id="default", user_id="10000001", day=DAY
    )
    assert out["cash"] is None and out["total_asset"] is None
    assert out["status"] == "no_snapshot"
    assert out["has_activity"] is False


def test_ddl_covers_bookkeeping_columns():
    for column in (
        "filled_value",
        "commission",
        "stamp_duty",
        "transfer_fee",
        "net_cash_flow",
        "realized_pnl",
        "agents_json",
        "brokers_json",
        "positions_json",
        "recon_json",
    ):
        assert column in ds.DDL
    assert "uq_daily_settlement_day" in ds.DDL


@pytest.mark.asyncio
async def test_run_day_upserts_and_commits():
    session = FakeSession(
        orders={
            "total": 1,
            "filled": 1,
            "partial": 0,
            "cancelled": 0,
            "rejected": 0,
            "pending": 0,
            "filled_value": 100.0,
        },
        trades={
            "value": 100.0,
            "buy_value": 100.0,
            "sell_value": 0,
            "commission": 0.03,
            "stamp_duty": 0,
            "transfer_fee": 0,
        },
    )
    out = await ds.run_day(session, tenant_id="default", user_id="10000001", day=DAY)
    assert out["trade_date"] == DAY
    # 建表 + 计算 + upsert 各一次提交路径
    assert session.committed >= 1


@pytest.mark.asyncio
async def test_trading_days_between_falls_back_to_weekdays(monkeypatch):
    import sys

    # 把日历模块置空 → import 抛 ImportError → 走工作日兜底
    monkeypatch.setitem(sys.modules, "backend.shared.trading_calendar", None)
    days = await ds.trading_days_between(date(2026, 11, 6), date(2026, 11, 9))
    # 11-07/11-08 是周末
    assert days == [date(2026, 11, 6), date(2026, 11, 9)]


_ZERO_ORDERS = {
    "total": 0,
    "filled": 0,
    "partial": 0,
    "cancelled": 0,
    "rejected": 0,
    "pending": 0,
    "filled_value": 0,
}
_ZERO_TRADES = {
    "value": 0,
    "buy_value": 0,
    "sell_value": 0,
    "commission": 0,
    "stamp_duty": 0,
    "transfer_fee": 0,
}
_TWO_ACCOUNTS = [
    {
        "source": "qmt_exec",
        "cash": 22932177.87,
        "market_value": 821632.0,
        "total_asset": 23773059.87,
        "snapshot_at": "2026-11-06 15:05:00",
    },
    {
        "source": "tdx_bridge",
        "cash": 900000.0,
        "market_value": 20242.89,
        "total_asset": 920242.89,
        "snapshot_at": "2026-11-06 15:06:00",
    },
]


@pytest.mark.asyncio
async def test_anchor_source_keeps_accounts_separate():
    """两个平台同期报快照时，资金锚按主账户取，另一账户只进 recon。"""
    session = FakeSession(
        orders=_ZERO_ORDERS, trades=_ZERO_TRADES, snapshots=_TWO_ACCOUNTS
    )
    out = await ds.compute_day(
        session,
        tenant_id="default",
        user_id="10000001",
        day=DAY,
        anchor_source="qmt_exec",
    )
    assert out["total_asset"] == 23773059.87
    assert out["recon_json"]["snapshot_source"] == "qmt_exec"
    assert out["recon_json"]["snapshots"]["tdx_bridge"]["total_asset"] == 920242.89


@pytest.mark.asyncio
async def test_without_anchor_uses_latest_snapshot_but_keeps_all():
    session = FakeSession(
        orders=_ZERO_ORDERS, trades=_ZERO_TRADES, snapshots=_TWO_ACCOUNTS
    )
    out = await ds.compute_day(
        session, tenant_id="default", user_id="10000001", day=DAY
    )
    # 没指定主账户 → 取快照时间最新的那条（tdx 15:06）
    assert out["recon_json"]["snapshot_source"] == "tdx_bridge"
    assert set(out["recon_json"]["snapshots"]) == {"qmt_exec", "tdx_bridge"}


def test_anchor_source_for_broker_mapping():
    assert ds.anchor_source_for_broker("qmt_exec") == "qmt_exec"
    assert ds.anchor_source_for_broker("tdx") == "tdx_bridge"
    assert ds.anchor_source_for_broker("tiger") is None
