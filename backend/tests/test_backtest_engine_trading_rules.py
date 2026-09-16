"""统一回测引擎交易规则回归测试：买卖数量对账、停牌过滤、涨跌停过滤。"""

import pandas as pd
import pytest

from backend.shared.backtest_engine.core.engine import (
    BacktestEngine,
    get_price_limit_threshold,
)
from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderType
from backend.shared.backtest_engine.core.portfolio import Portfolio
from backend.shared.backtest_engine.strategies.base import BaseStrategy


def _order(symbol="SH600036", side=OrderSide.BUY, qty=100, otype=OrderType.MARKET):
    return Order(symbol=symbol, side=side, order_type=otype, quantity=qty)


def _bar(volume=100_000, close=10.0, high=10.0, low=9.5):
    return pd.Series({"volume": volume, "close": close, "high": high, "low": low})


class _BuyThenSellStrategy(BaseStrategy):
    """首个交易日买入 100 股，第二个交易日全量卖出，用于端到端数量对账。"""

    def __init__(self):
        super().__init__("buy_then_sell")
        self.calls = 0

    def on_data(self, market_data):
        sym = market_data["symbol"]
        if self.calls == 0:
            self.buy(sym, 100)
        elif self.calls == 1:
            qty = self.get_position(sym)
            if qty > 0:
                self.sell(sym, qty)
        self.calls += 1

    def on_order_filled(self, order):
        pass


# ---------- BUG1：买入/卖出数量对账 ----------
def test_portfolio_buy_then_sell_reconciles():
    p = Portfolio(initial_cash=100000.0)
    p.buy("SH600036", 100, price=10.0, commission=0.0)
    assert p.get_position("SH600036") == 100
    assert p.cash == pytest.approx(100000.0 - 100 * 10.0)

    # T+1（账本模型收口）：当日买入次日才可卖——卖出前先解锁
    p.unlock_t1()
    pnl = p.sell("SH600036", 100, price=12.0, commission=0.0)
    assert p.get_position("SH600036") == 0
    assert p.cash == pytest.approx(100000.0 - 1000.0 + 1200.0)
    assert pnl == pytest.approx(200.0)


def test_portfolio_sell_more_than_hold_rejected():
    p = Portfolio(initial_cash=100000.0)
    p.buy("SH600036", 100, price=10.0, commission=0.0)
    with pytest.raises(ValueError):
        p.sell("SH600036", 200, price=10.0, commission=0.0)


def test_engine_run_buy_sell_reconciles_positions():
    symbol = "SH600036"
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    df = pd.DataFrame(
        {
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1_000_000,
        },
        index=dates,
    )

    eng = BacktestEngine(initial_cash=100000.0, enable_risk_management=False)
    eng.set_data({symbol: df})
    eng.add_strategy(_BuyThenSellStrategy())
    eng.run()

    assert eng.portfolio.get_position(symbol) == 0
    buys = [t for t in eng.trades if t["side"] == "buy"]
    sells = [t for t in eng.trades if t["side"] == "sell"]
    assert len(buys) == 1 and len(sells) == 1
    assert buys[0]["quantity"] == sells[0]["quantity"] == 100


# ---------- BUG2：停牌过滤 ----------
def test_suspended_stock_cannot_trade():
    eng = BacktestEngine(enable_risk_management=False)
    assert not eng._can_execute_order(_order(), _bar(volume=0))
    assert not eng._can_execute_order(_order(), _bar(volume=float("nan")))
    assert not eng._can_execute_order(_order(), _bar(volume=float("NaN")))


def test_normal_day_can_trade():
    eng = BacktestEngine(enable_risk_management=False)
    assert eng._can_execute_order(_order(side=OrderSide.BUY), _bar(volume=100_000, close=10.0), prev_close=9.5)


# ---------- 涨跌停过滤（主板10% / 创业板·科创板20% / 北交所30%）----------
def test_limit_up_cannot_buy():
    eng = BacktestEngine(enable_risk_management=False)
    # 主板 +10% 封涨停 → 不可买入
    assert not eng._can_execute_order(
        _order(side=OrderSide.BUY), _bar(volume=100_000, close=11.0, high=11.0), prev_close=10.0
    )


def test_limit_up_can_sell():
    eng = BacktestEngine(enable_risk_management=False)
    assert eng._can_execute_order(
        _order(side=OrderSide.SELL), _bar(volume=100_000, close=11.0, high=11.0), prev_close=10.0
    )


def test_limit_down_cannot_sell():
    eng = BacktestEngine(enable_risk_management=False)
    # 主板 -10% 封跌停 → 不可卖出
    assert not eng._can_execute_order(
        _order(side=OrderSide.SELL), _bar(volume=100_000, close=9.0, low=9.0), prev_close=10.0
    )


def test_limit_down_can_buy():
    eng = BacktestEngine(enable_risk_management=False)
    assert eng._can_execute_order(
        _order(side=OrderSide.BUY), _bar(volume=100_000, close=9.0, low=9.0), prev_close=10.0
    )


def test_board_thresholds():
    # 主板 10%、创业板/科创板 20%、北交所 30%
    assert get_price_limit_threshold("SH600036") == 0.10
    assert get_price_limit_threshold("SZ000001") == 0.10
    assert get_price_limit_threshold("SA600036") == 0.10
    assert get_price_limit_threshold("SH688111") == 0.20
    assert get_price_limit_threshold("SZ300750") == 0.20
    assert get_price_limit_threshold("SZ301001") == 0.20
    assert get_price_limit_threshold("BJ830000") == 0.30
    assert get_price_limit_threshold("BJ430047") == 0.30


def test_limit_up_buy_blocked_within_gem_20pct():
    eng = BacktestEngine(enable_risk_management=False)
    # 创业板 +10% 未到 +20% → 仍可买入
    ok_bar = _bar(volume=100_000, close=11.0, high=11.0)
    assert eng._can_execute_order(_order(symbol="SZ300750", side=OrderSide.BUY), ok_bar, prev_close=10.0)
    # 创业板 +20% 封涨停 → 不可买入
    up_bar = _bar(volume=100_000, close=12.0, high=12.0)
    assert not eng._can_execute_order(_order(symbol="SZ300750", side=OrderSide.BUY), up_bar, prev_close=10.0)
