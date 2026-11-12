"""回归：容器盘中/盘后重启时 EOD 不得把当日盈亏回填到 T-1。

真实案例（2026-11-06）：50 笔成交全部发生在 11-06 11:54，但 23:25 容器
重启后 EOD 以 06:05 触发逻辑重跑 11-05，用「当前权益」把 50 个持仓和
-1.95% 亏损写进了 11-05 的日账/资金快照，前端每日收益率因此显示 11/05。

覆盖：
1. _resolve_target_trade_date 按收盘时刻切分，收盘后重启结算当日而非 T-1；
2. _accounts_with_trades_after 能识别结算日之后已有成交的账户；
3. _execute_eod 对这类账户直接跳过，不写历史日快照。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.services import eod_service as eod

SH = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 06:05 正常隔夜结算窗口：结算上一自然日
        (datetime(2026, 11, 6, 6, 5, tzinfo=SH), date(2026, 11, 5)),
        # 盘中重启：当日未收盘，仍是 T-1（但会被成交护栏拦下）
        (datetime(2026, 11, 6, 14, 0, tzinfo=SH), date(2026, 11, 5)),
        # 盘后重启：当日交易已结束，账户现值是当日日终 → 结算当日
        (datetime(2026, 11, 6, 23, 25, tzinfo=SH), date(2026, 11, 6)),
        # 次日凌晨的正常触发窗口：结算上一交易日（周五）
        (datetime(2026, 11, 7, 6, 5, tzinfo=SH), date(2026, 11, 6)),
        # UTC 输入也按上海日历换算
        (datetime(2026, 11, 6, 15, 25, tzinfo=ZoneInfo("UTC")), date(2026, 11, 6)),
    ],
)
def test_resolve_target_trade_date(now: datetime, expected: date):
    assert eod._resolve_target_trade_date(now) == expected


def test_resolve_target_trade_date_naive_datetime():
    assert eod._resolve_target_trade_date(datetime(2026, 11, 6, 23, 25)) == date(
        2026, 11, 6
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else 0


class _FakeCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


class _GuardSession:
    """只识别 _execute_eod 会发出的三类语句。"""

    def __init__(self, trade_rows, accounts=None):
        self.trade_rows = trade_rows
        self.accounts = accounts or []
        self.snapshot_calls = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "sim_trades" in sql:
            return _Rows(self.trade_rows)
        if "simulation_accounts" in sql:
            return _Rows(self.accounts)
        if "sim_orders" in sql or "count" in sql.lower():
            return _Rows([0])
        return _Rows([])

    async def commit(self):
        return None

    async def rollback(self):
        return None


def test_accounts_with_trades_after_finds_later_trades():
    session = _GuardSession(
        [SimpleNamespace(tenant_id="default", user_id=10000001)]
    )
    result = asyncio.run(eod._accounts_with_trades_after(session, date(2026, 11, 5)))
    assert result == {("default", "10000001")}


def test_accounts_with_trades_after_empty_when_no_later_trades():
    session = _GuardSession([])
    result = asyncio.run(eod._accounts_with_trades_after(session, date(2026, 11, 5)))
    assert result == set()


def test_execute_eod_skips_account_with_trades_after_settlement_date(monkeypatch):
    """11-06 已有成交时，重跑 11-05 的 EOD 必须整户跳过，不写历史日快照。"""
    account = SimpleNamespace(
        account_id="sim:default:10000001",
        tenant_id="default",
        user_id="10000001",
    )
    session = _GuardSession(
        trade_rows=[SimpleNamespace(tenant_id="default", user_id=10000001)],
        accounts=[account],
    )
    monkeypatch.setattr(
        eod, "get_session", lambda read_only=False: _FakeCtx(session)
    )
    monkeypatch.setattr(
        eod, "_prime_local_close_cache_sync", lambda *args, **kwargs: None
    )
    # 本用例只验证「结算日之后已有成交」护栏；日线可用性由独立用例覆盖。
    monkeypatch.setattr(
        eod, "_daily_bar_available", lambda *args, **kwargs: _true_async()
    )

    snapshot_calls = []
    monkeypatch.setattr(
        eod.SimulationDailySnapshotService,
        "replace_daily_snapshot",
        lambda self, **kwargs: _noop_async(snapshot_calls, kwargs),
    )
    monkeypatch.setattr(
        eod,
        "_write_eod_fund_snapshot",
        lambda session, **kwargs: _noop_async(snapshot_calls, kwargs),
    )
    monkeypatch.setattr(
        eod.SimulationProjectionService,
        "load_projection",
        lambda self, **kwargs: (_ for _ in ()).throw(
            AssertionError("projection must not be loaded for a stale account")
        ),
    )

    assert asyncio.run(eod._execute_eod(date(2026, 11, 5))) is True
    assert snapshot_calls == []


async def _noop_async(calls, payload):
    calls.append(payload)
    return None


async def _true_async():
    return True
