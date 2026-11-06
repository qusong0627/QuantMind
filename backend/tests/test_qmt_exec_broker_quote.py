"""QmtExecBroker.query_quote：市价单风控估值的数据源（2026-11-06 事故回归）。

历史实现返回空字典 → 所有经 qmt_exec 的市价单被风控闸以
``market_price_unavailable`` 拒绝，止损单永远发不出去。
"""
from __future__ import annotations

import asyncio

from backend.services.live_trading.services.broker_client import QmtExecBroker


class FakeClient:
    configured = True

    def __init__(self, ticks=None, error=None):
        self._ticks = ticks or {}
        self._error = error

    async def get_full_tick(self, codes):
        if self._error:
            raise self._error
        return {c: self._ticks[c] for c in codes if c in self._ticks}


def _broker(client):
    return QmtExecBroker(client=client)


def test_returns_last_price_from_pipe():
    broker = _broker(FakeClient({'600036.SH': {'lastPrice': 41.23}}))
    quote = asyncio.run(broker.query_quote('600036.SH'))
    assert quote == {'last_price': 41.23, 'symbol': '600036.SH'}


def test_missing_tick_returns_empty_fail_closed():
    broker = _broker(FakeClient({}))
    assert asyncio.run(broker.query_quote('600036.SH')) == {}


def test_pipe_error_returns_empty_not_raise():
    broker = _broker(FakeClient(error=RuntimeError('pipe timeout')))
    assert asyncio.run(broker.query_quote('600036.SH')) == {}


def test_zero_price_is_treated_as_missing():
    broker = _broker(FakeClient({'600036.SH': {'lastPrice': 0}}))
    assert asyncio.run(broker.query_quote('600036.SH')) == {}
