"""End-to-end backtest test using InMemoryProvider.

Validation gate for Day 2: a minimal user script runs through the loop and
returns Metrics with non-trivial cum_return + n_trades.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.strategy_lab.engine.data_provider import InMemoryProvider
from backend.services.engine.strategy_lab.engine.loop import run_backtest
from backend.services.engine.strategy_lab.sdk.context import Context


def _make_provider() -> InMemoryProvider:
    """Two stocks, 10 trading days, monotonically rising prices."""
    idx = pd.date_range("2025-01-02", periods=10, freq="B")
    a = pd.DataFrame(
        {
            "open": [10 + i * 0.1 for i in range(10)],
            "high": [10.5 + i * 0.1 for i in range(10)],
            "low": [9.5 + i * 0.1 for i in range(10)],  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low 序列
            "close": [10 + i * 0.1 for i in range(10)],
            "volume": [10000.0] * 10,
            "adj_close": [10 + i * 0.1 for i in range(10)],
        },
        index=idx,
    )
    b = a.copy()
    b["close"] = [20 + i * 0.2 for i in range(10)]
    b["open"] = b["close"]
    b["adj_close"] = b["close"]
    bench = pd.Series([100 + i for i in range(10)], index=idx)
    return InMemoryProvider({"A": a, "B": b}, benchmark_series=bench)


def _exec(code: str) -> dict:
    g: dict = {}
    exec(compile(code, "<test>", "exec"), g, g)
    return g


def test_buy_and_hold_returns_positive_metrics():
    code = """
def setup(ctx):
    ctx.universe = ["A", "B"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-15"
    ctx.cash = 1_000_000
    ctx.commission = 0.0
    ctx.slippage = 0.0
    ctx.tax_sell = 0.0
    ctx.transfer_fee = 0.0

_done = {"flag": False}

def on_bar(ctx, bar):
    if _done["flag"]:
        return
    if ctx.position(bar.symbol).qty == 0:
        ctx.buy(bar.symbol, weight=0.4, reason="initial entry")
"""
    user_globals = _exec(code)
    user_globals["__name__"] = "__strategy__"

    ctx = Context()
    provider = _make_provider()
    result = run_backtest(ctx=ctx, provider=provider, user_globals=user_globals)

    assert result.status == "success"
    assert result.metrics.n_trades >= 2
    # Prices rose monotonically -> cum_return must be positive
    assert result.metrics.cum_return > 0
    assert len(result.equity) == 10
    # Two distinct buys
    buy_syms = {t.symbol for t in result.trades if t.direction == "BUY"}
    assert buy_syms == {"A", "B"}


def test_setup_only_strategy_yields_zero_trades():
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-15"
    ctx.cash = 500_000
"""
    user_globals = _exec(code)
    ctx = Context()
    result = run_backtest(ctx=ctx, provider=_make_provider(), user_globals=user_globals)
    assert result.status == "success"
    assert result.metrics.n_trades == 0
    assert result.metrics.cum_return == 0.0


def test_set_target_holdings_rebalances():
    code = """
_first = {"v": True}

def setup(ctx):
    ctx.universe = ["A", "B"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-15"
    ctx.cash = 1_000_000
    ctx.commission = 0.0
    ctx.slippage = 0.0

def on_universe(ctx, date, snapshot):
    if _first["v"]:
        ctx.set_target_holdings(["A", "B"], reason="rebalance")
        _first["v"] = False
"""
    user_globals = _exec(code)
    ctx = Context()
    result = run_backtest(ctx=ctx, provider=_make_provider(), user_globals=user_globals)
    assert result.status == "success"
    assert result.metrics.n_trades == 2  # one buy per target
    assert {t.symbol for t in result.trades} == {"A", "B"}


def test_invalid_setup_raises():
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    # missing start/end/cash
"""
    user_globals = _exec(code)
    ctx = Context()
    with pytest.raises(ValueError):
        run_backtest(ctx=ctx, provider=_make_provider(), user_globals=user_globals)


def test_empty_calendar_raises():
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    ctx.start = "2030-01-01"
    ctx.end = "2030-01-10"
    ctx.cash = 1_000_000
"""
    user_globals = _exec(code)
    ctx = Context()
    with pytest.raises(RuntimeError, match="No trading days"):
        run_backtest(ctx=ctx, provider=_make_provider(), user_globals=user_globals)


def test_t_plus_one_blocks_same_day_sell():
    """Buying on day N and trying to sell on day N must not produce a SELL trade."""
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-08"
    ctx.cash = 100_000
    ctx.commission = 0.0
    ctx.slippage = 0.0

def on_bar(ctx, bar):
    if ctx.position(bar.symbol).qty == 0:
        ctx.buy(bar.symbol, weight=0.5, reason="enter")
    else:
        ctx.sell(bar.symbol, all=True, reason="exit-attempt")
"""
    user_globals = _exec(code)
    ctx = Context()
    result = run_backtest(ctx=ctx, provider=_make_provider(), user_globals=user_globals)
    assert result.status == "success"
    # First day buys; subsequent days hit the sell path and DO trade (T+1 satisfied)
    sells = [t for t in result.trades if t.direction == "SELL"]
    buys = [t for t in result.trades if t.direction == "BUY"]
    assert len(buys) >= 1
    # All sells must occur strictly after the first buy date
    first_buy_date = buys[0].date
    for s in sells:
        assert s.date > first_buy_date
