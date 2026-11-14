"""Regression cases from the September 2026 quick-backtest trade audit."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from qlib.backtest.decision import OrderDir

from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange


class Quote:
    def __init__(self, values):
        self.values = values

    def get_data(self, stock_id, start_time, end_time, field, method):
        return self.values.get(field)


def exchange(opening, close, reference=10.0, factor=1.0, deal_price="$open"):
    obj = CnExchange.__new__(CnExchange)
    obj.quote = Quote(
        {
            "$open": opening * factor,
            "$close": close * factor,
            "Ref($close, 1)": reference * factor,
            "$factor": factor,
        }
    )
    obj.buy_price = obj.sell_price = deal_price
    return obj


def blocked(obj, symbol="sh600000", day="2020-01-02", direction=OrderDir.BUY):
    timestamp = pd.Timestamp(day)
    return obj.check_stock_limit(symbol, timestamp, timestamp, direction)


@pytest.mark.parametrize("symbol", ["sz300811", "SZ300811", "300811.SZ"])
def test_historical_chinext_one_price_limit_is_blocked(symbol):
    assert blocked(exchange(11, 11), symbol)


def test_chinext_rule_switch_on_august_24():
    obj = exchange(11, 11.1)
    assert blocked(obj, "sz300811", "2020-08-21")
    assert not blocked(obj, "sz300811", "2020-08-24")
    assert blocked(exchange(12, 12), "sz300811", "2020-08-24")


@pytest.mark.parametrize("symbol", ["sh688001", "sh689009", "sz302001"])
def test_growth_board_twenty_percent(symbol):
    assert not blocked(exchange(11, 11.1), symbol, "2026-01-02")
    assert blocked(exchange(12, 12), symbol, "2026-01-02")


@pytest.mark.parametrize("symbol", ["bj920001", "920001.BJ", "BJ830001"])
def test_beijing_thirty_percent(symbol):
    assert not blocked(exchange(11, 11.1), symbol)
    assert blocked(exchange(13, 13), symbol)


def test_open_limit_then_board_opens_is_still_rejected_at_open():
    assert blocked(exchange(11, 10.2))


def test_afternoon_limit_does_not_reject_earlier_open_fill():
    assert not blocked(exchange(10.2, 11))
    assert blocked(exchange(10.2, 11, deal_price="$close"))


@pytest.mark.parametrize("price", [9.0, 10.0, 10.5, 11.0])
def test_strict_equal_open_close_rejects_close_fills_even_without_st_metadata(price):
    # The strict open == close policy is only observable on a close fill.
    assert blocked(exchange(price, price, deal_price="$close"))


@pytest.mark.parametrize("price", [9.0, 10.0, 10.5])
def test_open_fill_equal_open_close_is_allowed_below_limit(price):
    # At the open, today's close is unknown; only the limit price can veto.
    assert not blocked(exchange(price, price, deal_price="$open"))


@pytest.mark.parametrize(
    "future_close", [None, np.nan, np.inf, 0.0, 9.0, 10.0, 10.1, 11.0]
)
def test_open_buy_does_not_depend_on_future_close(future_close):
    obj = exchange(10.0, 10.1, deal_price="$open")
    obj.quote.values["$close"] = future_close
    assert not blocked(obj)


def test_limit_up_can_be_sold_and_limit_down_cannot():
    assert not blocked(exchange(11, 11), direction=OrderDir.SELL)
    assert blocked(exchange(9, 9), direction=OrderDir.SELL)
    assert not blocked(exchange(9, 9.2), direction=OrderDir.BUY)


def test_directionless_filter_checks_both_sides():
    assert blocked(exchange(11, 11), direction=None)
    assert blocked(exchange(9, 9), direction=None)
    assert not blocked(exchange(10, 10.1), direction=None)


def test_raw_cent_rounding_and_float32_adjusted_prices():
    obj = exchange(3.55, 3.55, reference=3.23, factor=2.030070066, deal_price="$close")
    obj.quote.values = {k: np.float32(v) for k, v in obj.quote.values.items()}
    assert blocked(obj)
    assert not blocked(exchange(3.54, 3.53, reference=3.23))


@pytest.mark.parametrize("field", ["$open", "$factor", "Ref($close, 1)"])
@pytest.mark.parametrize("value", [None, np.nan, np.inf, 0.0])
def test_missing_invalid_quotes_fail_closed_in_open_mode(field, value):
    obj = exchange(10, 10.1)
    obj.quote.values[field] = value
    assert blocked(obj)


@pytest.mark.parametrize("field", ["$open", "$close", "$factor", "Ref($close, 1)"])
@pytest.mark.parametrize("value", [None, np.nan, np.inf, 0.0])
def test_missing_invalid_quotes_fail_closed_in_close_mode(field, value):
    obj = exchange(10, 10.1, deal_price="$close")
    obj.quote.values[field] = value
    assert blocked(obj)


def test_missing_change_does_not_matter_if_reference_is_valid():
    assert not blocked(exchange(10, 10.1))


def test_non_cn_instruments_have_no_price_limit():
    assert not blocked(exchange(11, 11), "AAPL")


def test_quote_clipping_cannot_bypass_limit():
    obj = exchange(11, 11)
    order = SimpleNamespace(
        stock_id="sh600000",
        start_time=pd.Timestamp("2020-01-02"),
        end_time=pd.Timestamp("2020-01-02"),
        direction=OrderDir.BUY,
        deal_amount=100.0,
    )
    assert obj.quote_clipping(order).deal_amount == 0
