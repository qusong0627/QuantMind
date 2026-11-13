"""模拟盘动态仓位 / 动态股票池（动态仓位TOPK、动态股票池TOPK）测试。"""

from __future__ import annotations

from datetime import date

import pytest

import backend.services.simulation.services.rebalance_calculator as rc
from backend.services.simulation.services.rebalance_calculator import (
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
    dynamic_config_from_params,
)
from backend.services.simulation.services.signal_loader import SignalScore

TD = date(2026, 3, 4)


def _sig(symbol: str, score: float) -> SignalScore:
    return SignalScore(
        symbol=symbol,
        score=score,
        trade_date=TD,
        run_id="t",
        tenant_id="t",
        user_id="t",
    )


def _quote(symbol: str, price: float = 10.0) -> Quote:
    return Quote(symbol=symbol, current_price=price)


def _account(held: dict[str, int], cash: float = 0.0) -> SimulationAccount:
    positions = {sym: {"volume": vol} for sym, vol in held.items() if vol > 0}
    market_value = sum(vol * 10.0 for vol in held.values())
    return SimulationAccount(
        cash=cash, total_asset=cash + market_value, positions=positions
    )


class TestDynamicExposure:
    def test_negative_mean_sells_down_to_half(self):
        calc = RebalanceCalculator()
        signals = [_sig("A", -0.01), _sig("B", -0.02), _sig("C", -0.03)]
        strategy = StrategyConfig(
            topk=3,
            n_drop=0,
            max_position_pct=1.0,
            dynamic_exposure=True,
            score_threshold=0.0,
            pos_up=1.0,
            pos_down=0.5,
        )
        account = _account({"A": 1000, "B": 1000, "C": 1000})
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account)

        sells = {o.symbol: o.quantity for o in orders if o.side == "SELL"}
        buys = [o for o in orders if o.side == "BUY"]
        # 3 万总资产 → 50% 目标 = 1.5 万，每只 5000 / 10 = 500 股
        assert sells == {"A": 500, "B": 500, "C": 500}
        assert buys == []

    def test_positive_mean_buys_to_full(self):
        calc = RebalanceCalculator()
        signals = [_sig("A", 0.01), _sig("B", 0.02), _sig("C", 0.03)]
        strategy = StrategyConfig(
            topk=3,
            n_drop=0,
            max_position_pct=1.0,
            dynamic_exposure=True,
            score_threshold=0.0,
            pos_up=1.0,
            pos_down=0.5,
        )
        account = _account({"A": 500, "B": 500, "C": 500}, cash=15_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        orders = calc.calculate(signals, strategy, quotes, account)

        buys = {o.symbol: o.quantity for o in orders if o.side == "BUY"}
        sells = [o for o in orders if o.side == "SELL"]
        assert sells == []
        assert buys == {"A": 500, "B": 500, "C": 500}

    def test_legacy_params_infer_dynamic_exposure(self):
        cfg = dynamic_config_from_params(
            {
                "score_threshold": 0.0,
                "pos_up": 1.0,
                "pos_down": 0.5,
                "defensive_market": "csi300",
            }
        )
        assert cfg["dynamic_exposure"] is True
        assert cfg["defensive_universe"] == "csi300"

        default_cfg = dynamic_config_from_params({})
        assert default_cfg["dynamic_exposure"] is False
        assert default_cfg["target_exposure"] == 1.0

    def test_crash_params_infer_crash_filter(self):
        cfg = dynamic_config_from_params(
            {
                "score_threshold": 0.0,
                "pos_down": 0.5,
                "crash_exposure": 0.3,
                "crash_ret10": -0.06,
            }
        )
        assert cfg["crash_filter"] is True
        assert cfg["crash_exposure"] == 0.3
        assert cfg["crash_ret10"] == -0.06

    def test_crash_filter_overrides_positive_mean(self, monkeypatch):
        calc = RebalanceCalculator()
        signals = [
            _sig("600000.SH", 0.01),
            _sig("600001.SH", 0.02),
            _sig("600002.SH", 0.03),
        ]
        strategy = StrategyConfig(
            topk=3,
            n_drop=0,
            max_position_pct=1.0,
            dynamic_exposure=True,
            score_threshold=0.0,
            pos_up=1.0,
            pos_down=0.5,
            crash_filter=True,
            crash_exposure=0.3,
            defensive_universe="csi300",
        )
        account = _account(
            {"600000.SH": 1000, "600001.SH": 1000, "600002.SH": 1000}
        )
        quotes = {s.symbol: _quote(s.symbol) for s in signals}
        monkeypatch.setattr(
            rc,
            "index_members_on",
            lambda market, day: {"600000.SH", "600001.SH", "600002.SH"},
        )

        orders = calc.calculate(
            signals, strategy, quotes, account, market_crash=True
        )

        # 均分为正本应 100%，但急跌信号把目标压到 30%：
        # 3 万总资产 × 30% / 3 只 / 10 元 = 300 股（整手 100），原 1000 股 → 卖 700
        sells = {o.symbol: o.quantity for o in orders if o.side == "SELL"}
        assert sells == {
            "600000.SH": 700,
            "600001.SH": 700,
            "600002.SH": 700,
        }
        assert [o for o in orders if o.side == "BUY"] == []


class TestDynamicUniverse:
    def test_negative_mean_filters_to_defensive_universe(self, monkeypatch):
        calc = RebalanceCalculator()
        signals = [
            _sig("A", -0.01),
            _sig("B", -0.02),
            _sig("C", -0.03),
            _sig("D", -0.04),
        ]
        strategy = StrategyConfig(
            topk=3,
            n_drop=0,
            max_position_pct=1.0,
            dynamic_exposure=True,
            score_threshold=0.0,
            pos_down=0.5,
            defensive_universe="csi300",
        )
        account = _account({}, cash=100_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        monkeypatch.setattr(rc, "index_members_on", lambda market, day: {"A", "B"})

        orders = calc.calculate(signals, strategy, quotes, account)

        buys = sorted(o.symbol for o in orders if o.side == "BUY")
        assert buys == ["A", "B"]

    def test_switch_sells_non_member_holdings(self, monkeypatch):
        calc = RebalanceCalculator()
        signals = [
            _sig("A", -0.01),
            _sig("B", -0.02),
            _sig("C", -0.03),
        ]
        strategy = StrategyConfig(
            topk=2,
            n_drop=0,
            max_position_pct=1.0,
            dynamic_exposure=True,
            score_threshold=0.0,
            pos_down=0.5,
            defensive_universe="csi300",
        )
        # 持仓 C 不属于沪深300，切池后必须卖出
        account = _account({"C": 1000}, cash=90_000)
        quotes = {s.symbol: _quote(s.symbol) for s in signals}

        monkeypatch.setattr(rc, "index_members_on", lambda market, day: {"A", "B"})

        orders = calc.calculate(signals, strategy, quotes, account)

        sells = sorted(o.symbol for o in orders if o.side == "SELL")
        buys = sorted(o.symbol for o in orders if o.side == "BUY")
        assert sells == ["C"]
        assert buys == ["A", "B"]


def test_engine_extracts_dynamic_strategy_kwargs():
    from backend.services.simulation.engine import _extract_strategy_config_kwargs

    code = """
STRATEGY_CONFIG = {
    "class": "DynamicPositionTopkStrategy",
    "kwargs": {
        "topk": 50,
        "n_drop": 10,
        "dynamic_exposure": True,
        "score_threshold": 0.0,
        "pos_up": 1.0,
        "pos_down": 0.5,
        "defensive_universe": "csi300",
    },
}
"""
    kwargs = _extract_strategy_config_kwargs(code)
    assert kwargs["dynamic_exposure"] is True
    assert kwargs["score_threshold"] == 0.0
    assert kwargs["pos_down"] == 0.5
    assert kwargs["defensive_universe"] == "csi300"


class _FakeHub:
    def __init__(self, closes: list[float]):
        self._closes = closes

    def fetch_index_kline(self, symbol, start, end):
        import pandas as pd

        return pd.DataFrame({"close": self._closes})


class _FakeMarketData:
    def __init__(self, closes: list[float]):
        self._hub = _FakeHub(closes)


def test_csi300_crash_active_triggers_on_fast_decline():
    from backend.services.simulation.services import market_regime

    market_regime._CACHE.clear()
    # 10 个交易日从 100 跌到 90，约 -10%
    md = _FakeMarketData([100.0 - i for i in range(11)])

    assert (
        market_regime.csi300_crash_active(
            md, date(2026, 3, 11), ret10_threshold=-0.06, ret5_threshold=-0.04
        )
        is True
    )


def test_csi300_crash_active_false_on_flat_market():
    from backend.services.simulation.services import market_regime

    market_regime._CACHE.clear()
    md = _FakeMarketData([100.0] * 11)

    assert (
        market_regime.csi300_crash_active(
            md, date(2026, 3, 11), ret10_threshold=-0.06, ret5_threshold=-0.04
        )
        is False
    )
