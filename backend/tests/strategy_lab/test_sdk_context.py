"""Tests for Context — config validation, params, orders, risk, log/plot."""

import pandas as pd
import pytest

from backend.services.engine.strategy_lab.sdk.context import (
    Context,
    OrderIntent,
    ParamSpec,
    RiskRule,
)
from backend.services.engine.strategy_lab.sdk.position import Position


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_defaults():
    c = Context()
    assert c.benchmark == "SH000300"
    assert c.commission == 0.0003
    assert c.slippage == 0.0005
    assert c.execution_model == "a_share_strict"
    assert c.engine == "qlib"
    assert c.max_positions == 10


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_universe_string_must_be_known():
    c = Context()
    c.universe = "csi300"
    assert c.universe == "csi300"
    with pytest.raises(ValueError, match="universe"):
        c.universe = "csi-bogus"


def test_universe_list_allowed():
    c = Context()
    c.universe = ["SH600036", "SZ000001"]
    assert c.universe == ["SH600036", "SZ000001"]


def test_dates_validated():
    c = Context()
    c.start = "2020-01-01"
    c.end = "2025-12-31"
    with pytest.raises(ValueError):
        c.start = "not-a-date"


def test_cash_must_be_positive():
    c = Context()
    with pytest.raises(ValueError):
        c.cash = 0
    c.cash = 1_000_000
    assert c.cash == 1_000_000


def test_commission_range():
    c = Context()
    with pytest.raises(ValueError):
        c.commission = 0.5
    with pytest.raises(ValueError):
        c.commission = -0.001
    c.commission = 0.0


def test_execution_model_whitelist():
    c = Context()
    c.execution_model = "simple"
    with pytest.raises(ValueError):
        c.execution_model = "lightning_fast"


def test_engine_whitelist():
    c = Context()
    with pytest.raises(ValueError):
        c.engine = "vectorbt"


def test_max_position_per_stock():
    c = Context()
    c.max_position_per_stock = 0.05
    with pytest.raises(ValueError):
        c.max_position_per_stock = 1.5
    with pytest.raises(ValueError):
        c.max_position_per_stock = 0


def test_assert_ready_missing():
    c = Context()
    c.universe = "csi300"
    c.start = "2020-01-01"
    with pytest.raises(ValueError, match="missing"):
        c.assert_ready()


def test_assert_ready_start_before_end():
    c = Context()
    c.universe = "csi300"
    c.start = "2025-01-01"
    c.end = "2020-01-01"
    c.cash = 100_000
    with pytest.raises(ValueError, match="before"):
        c.assert_ready()


def test_assert_ready_ok():
    c = Context()
    c.universe = "csi300"
    c.start = "2020-01-01"
    c.end = "2025-12-31"
    c.cash = 1_000_000
    c.assert_ready()  # does not raise


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
def test_param_first_call_registers_and_returns_default():
    c = Context()
    val = c.param("window", range(10, 50, 5), default=22)
    assert val == 22
    assert "window" in c.params
    spec = c.params["window"]
    assert isinstance(spec, ParamSpec)
    assert spec.default == 22
    assert spec.choices == list(range(10, 50, 5))


def test_param_second_call_returns_current():
    c = Context()
    c.param("window", [10, 20, 30], default=20)
    c.set_param("window", 30)
    val2 = c.param("window", [10, 20, 30], default=20)
    assert val2 == 30


def test_param_default_falls_back_to_first_choice():
    c = Context()
    val = c.param("k", choices=[5, 10, 20])
    assert val == 5


def test_param_needs_choices_or_default():
    c = Context()
    with pytest.raises(ValueError):
        c.param("nothing")


def test_param_invalid_name():
    c = Context()
    with pytest.raises(ValueError):
        c.param("12-bad", [1, 2])


def test_set_param_unknown_raises():
    c = Context()
    with pytest.raises(KeyError):
        c.set_param("never_declared", 42)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def test_buy_records_order():
    c = Context()
    c.buy("SH600036", weight=0.05, reason="S1_hit", detail={"s1": 9.9})  # fidelity: allow-limit-threshold — 非阈值：detail 夹具值（不是价格）
    intents = c._drain_orders()
    assert len(intents) == 1
    o = intents[0]
    assert isinstance(o, OrderIntent)
    assert o.symbol == "SH600036"
    assert o.side == "buy"
    assert o.weight == 0.05
    assert o.reason == "S1_hit"
    assert o.detail == {"s1": 9.9}  # fidelity: allow-limit-threshold — 非阈值：断言 detail 原样透传
    # Drained -> empty
    assert c._drain_orders() == []


def test_buy_requires_size():
    c = Context()
    with pytest.raises(ValueError):
        c.buy("SH600036")


def test_buy_weight_range():
    c = Context()
    with pytest.raises(ValueError):
        c.buy("SH600036", weight=1.5)


def test_sell_all():
    c = Context()
    c.sell("SH600036", all=True, reason="exit")
    o = c._drain_orders()[0]
    assert o.all is True


def test_set_position():
    c = Context()
    c.set_position("SH600036", weight=0.10, reason="rebalance")
    o = c._drain_orders()[0]
    assert o.side == "set_position"
    assert o.weight == 0.10


def test_set_target_holdings_caps_at_max_positions():
    c = Context()
    c.max_positions = 3
    c.set_target_holdings(["a", "b", "c", "d", "e"], weight="equal")
    o = c._drain_orders()[0]
    assert o.side == "set_target_holdings"
    assert len(o.targets) == 3
    assert o.targets == ["a", "b", "c"]


def test_set_target_holdings_only_equal_in_v1():
    c = Context()
    with pytest.raises(NotImplementedError):
        c.set_target_holdings(["a"], weight="cap_weighted")


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
def test_set_stop_loss_records_rule():
    c = Context()
    c.set_stop_loss("SH600036", -0.10)
    rules = c._drain_risk_rules()
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, RiskRule)
    assert r.kind == "stop_loss"
    assert r.value == -0.10


def test_set_stop_loss_range():
    c = Context()
    with pytest.raises(ValueError):
        c.set_stop_loss("x", 0.10)  # positive not allowed
    with pytest.raises(ValueError):
        c.set_stop_loss("x", -1.5)


def test_set_take_profit_range():
    c = Context()
    c.set_take_profit("x", 0.15)
    with pytest.raises(ValueError):
        c.set_take_profit("x", -0.10)


def test_account_stop_loss():
    c = Context()
    c.set_account_stop_loss(-0.20)
    r = c._drain_risk_rules()[0]
    assert r.kind == "account_stop_loss"
    assert r.symbol is None


def test_max_holding_days():
    c = Context()
    c.set_max_holding_days("SH600036", 30)
    r = c._drain_risk_rules()[0]
    assert r.kind == "max_holding_days"
    assert r.value == 30
    with pytest.raises(ValueError):
        c.set_max_holding_days("x", 0)


# ---------------------------------------------------------------------------
# Position queries
# ---------------------------------------------------------------------------
def test_position_default_empty():
    c = Context()
    p = c.position("SH600036")
    assert p.qty == 0
    assert isinstance(p, Position)


def test_position_returns_internal_record():
    c = Context()
    c._set_position(
        Position(symbol="SH600036", qty=100, cost=10_000, market_value=12_000, last_price=120)
    )
    p = c.position("SH600036")
    assert p.qty == 100
    assert p.pnl == 2000


def test_positions_dataframe_empty():
    c = Context()
    df = c.positions()
    assert df.empty
    assert "symbol" in df.columns


def test_positions_dataframe_filled():
    c = Context()
    c._set_position(Position(symbol="A", qty=100, cost=1000, market_value=1100))
    c._set_position(Position(symbol="B", qty=200, cost=2000, market_value=1900))
    df = c.positions()
    assert len(df) == 2
    assert set(df["symbol"]) == {"A", "B"}


# ---------------------------------------------------------------------------
# Logs / plots
# ---------------------------------------------------------------------------
def test_log_levels():
    c = Context()
    c._set_today(pd.Timestamp("2025-06-12"))
    c.log("hi", level="info")
    c.log("warn", level="warning")
    assert len(c._logs) == 2
    assert c._logs[0]["level"] == "info"
    assert c._logs[0]["ts"] == "2025-06-12T00:00:00"
    with pytest.raises(ValueError):
        c.log("x", level="trace")


def test_plot_line_drops_nan():
    c = Context()
    c._set_today(pd.Timestamp("2025-06-12"))
    c.plot_line("s1", 9.9, symbol="SH600036")  # fidelity: allow-limit-threshold — 非阈值：绘图值夹具（不是价格）
    c.plot_line("s1", float("nan"))
    c.plot_line("s1", float("inf"))
    c.plot_line("s1", "not-a-number")  # type: ignore[arg-type]
    assert len(c._plot_lines) == 1
    assert c._plot_lines[0]["value"] == 9.9  # fidelity: allow-limit-threshold — 非阈值：断言绘图值原样透传


def test_plot_marker():
    c = Context()
    c._set_today(pd.Timestamp("2025-06-12"))
    c.plot_marker("SH600036", type="alert", text="watch", price=10.5)
    assert c._plot_markers[0]["text"] == "watch"


# ---------------------------------------------------------------------------
# Provider stubs
# ---------------------------------------------------------------------------
def test_data_calls_without_provider_raise():
    c = Context()
    with pytest.raises(RuntimeError):
        c.history("x")


def test_data_calls_with_provider():
    class FakeProvider:
        def history(self, symbol, n, field, fields, symbols, today):
            return pd.Series([1.0, 2.0, 3.0])

        def feature(self, symbol, name, n, today):
            return 0.5

        def list_features(self):
            return ["momentum_20", "pe"]

        def snapshot(self, date, symbols):
            return pd.DataFrame({"close": [1.0]})

        def benchmark_history(self, symbol, n, today):
            return pd.Series([100.0, 101.0])

    c = Context()
    c._attach(FakeProvider(), broker=None, cash=100_000)
    assert list(c.history("x", n=3)) == [1.0, 2.0, 3.0]
    assert c.feature("x", "momentum_20") == 0.5
    assert c.list_features() == ["momentum_20", "pe"]
    assert c.benchmark_history(2).iloc[-1] == 101.0


# ---------------------------------------------------------------------------
# Reproducibility hash
# ---------------------------------------------------------------------------
def test_to_config_dict_serializable():
    c = Context()
    c.universe = "csi300"
    c.start = "2020-01-01"
    c.end = "2025-12-31"
    c.cash = 1_000_000
    cfg = c.to_config_dict()
    assert cfg["universe"] == "csi300"
    assert cfg["cash"] == 1_000_000
    # Stable key order across calls
    assert list(cfg.keys()) == sorted(cfg.keys())
