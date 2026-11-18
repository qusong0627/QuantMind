"""SLTP 分账「当日买入量」查询的参数类型护栏（T+1 可用量）。

回归的是一次真实事故（2026-11-18）：``ledger_today_buys`` 把
``trade_date_str()`` 返回的 **字符串** 直接绑到 ``trade_date``（PostgreSQL DATE）
参数上，asyncpg 在运行时抛 ``DataError: 'str' object has no attribute 'toordinal'``。
调用方 fail-closed，于是**所有 ``llm:`` 归属的止损触发都不会下单**——
止损被读库类型错误堵死，比没有止损更糟。

既有执行器测试把 ``ledger_today_buys`` 整个 mock 掉，SQL 参数从未被检查过，
所以这个 bug 全绿通过。本文件直接调用真实的 deps 闭包，只替换 session，
断言参数是 ``datetime.date``（而不是 str）。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from backend.services.live_trading.services import sltp_executor as ex
from backend.services.live_trading.services.trading_session import now_shanghai


class _FakeResult:
    def __init__(self, value: float) -> None:
        self._value = value

    def scalar(self) -> float:
        return self._value


class _FakeSession:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, sql, params=None):
        self._sink.append((str(sql), dict(params or {})))
        return _FakeResult(0.0)


class _FakeSessionCtx:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._sink)

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture()
def captured(monkeypatch):
    """构造真实 deps，只替换 DB session 与券商客户端（不触网）。"""
    from backend.shared import database_manager_v2

    sink: list = []
    monkeypatch.setattr(
        database_manager_v2,
        "get_session",
        lambda *a, **k: _FakeSessionCtx(sink),
    )
    monkeypatch.setattr(ex, "_active_broker", lambda: "tdx")
    monkeypatch.setattr(ex, "_build_sltp_client", lambda broker: object())
    deps = ex._build_default_deps(redis=object(), tenant_id="default")
    return deps, sink


def test_ledger_today_buys_binds_date_not_str(captured) -> None:
    """DATE 列必须绑 date 对象：asyncpg 不接受 str，传错=止损 fail-closed。"""
    deps, sink = captured

    value = asyncio.run(
        deps.ledger_today_buys("deepseek-v4-flash", "600011.SH", "10000001", "default")
    )

    assert value == pytest.approx(0.0)
    assert sink, "应发出一次查询"
    sql, params = sink[-1]
    assert "qm_agent_ledger_fill" in sql
    assert "trade_date=:d" in sql
    assert isinstance(params["d"], date), (
        f"trade_date 参数必须是 datetime.date，实际 {type(params['d']).__name__}："
        "asyncpg 会把 str 当非法 DATE 参数抛 DataError，导致止损 fail-closed"
    )
    assert not isinstance(params["d"], datetime), "DATE 列用 date，不用 datetime"
    assert params["d"] == now_shanghai().date()
    assert params["a"] == "deepseek-v4-flash"
    assert params["c"] == "600011.SH"
