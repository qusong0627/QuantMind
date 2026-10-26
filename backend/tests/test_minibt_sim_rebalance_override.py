# -*- coding: utf-8 -*-
"""调仓计算器：minibt 显式目标股数（``target_quantities``）路径。

纪律：minibt 的 ``SIZE`` 是股数，不是权重——这条路径必须原样保留股数，
且目标为 0 时即使跌停也要生成卖单（由撮合层拒绝并留痕，不在规划层静默丢弃）。
"""

from __future__ import annotations

from datetime import date

from backend.services.simulation.services.rebalance_calculator import (
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
)
from backend.services.simulation.services.signal_loader import SignalScore

CALC = RebalanceCalculator()
SYMBOL = "600036.SH"


def _signal() -> SignalScore:
    return SignalScore(
        symbol=SYMBOL,
        score=1.0,
        trade_date=date(2026, 10, 26),
        run_id="minibt:req_1",
        tenant_id="default",
        user_id="10000001",
    )


def _account(volume: int = 0, *, available: int | None = None) -> SimulationAccount:
    positions = {}
    if volume:
        positions[SYMBOL] = {
            "volume": volume,
            "available_volume": volume if available is None else available,
            "price": 10.0,
            "market_value": volume * 10.0,
        }
    return SimulationAccount(
        cash=1_000_000.0, total_asset=1_000_000.0, positions=positions
    )


def _account_with_other(volume: int = 0, other: int = 500) -> SimulationAccount:
    account = _account(volume)
    account.positions["000001.SZ"] = {
        "volume": other,
        "available_volume": other,
        "price": 12.0,
        "market_value": other * 12.0,
    }
    return account


def _calculate(
    *,
    volume: int = 0,
    target: int,
    limit_down: bool = False,
    available: int | None = None,
    inflight: dict | None = None,
    account: SimulationAccount | None = None,
    target_symbol: str = SYMBOL,
):
    quote = Quote(symbol=SYMBOL, current_price=10.0, is_limit_down=limit_down)
    other_quote = Quote(symbol="000001.SZ", current_price=12.0)
    return CALC.calculate(
        signals=[_signal()],
        strategy=StrategyConfig(topk=1, max_position_pct=1.0),
        quotes={SYMBOL: quote, "000001.SZ": other_quote},
        account=account or _account(volume, available=available),
        inflight=inflight,
        target_quantities={target_symbol: target},
    )


def test_空仓按目标股数买入() -> None:
    orders = _calculate(volume=0, target=1000)
    assert [(o.side, o.quantity) for o in orders] == [("BUY", 1000)]


def test_持仓与目标一致不下单() -> None:
    assert _calculate(volume=1000, target=1000) == []


def test_目标为零清仓() -> None:
    orders = _calculate(volume=1000, target=0)
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 1000)]


def test_目标增减按差额下单() -> None:
    assert [(o.side, o.quantity) for o in _calculate(volume=1000, target=1500)] == [
        ("BUY", 500)
    ]
    assert [(o.side, o.quantity) for o in _calculate(volume=1500, target=1000)] == [
        ("SELL", 500)
    ]


def test_跌停也生成清仓卖单交给撮合层留痕() -> None:
    orders = _calculate(volume=1000, target=0, limit_down=True)
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 1000)]


def test_T加一可卖量钳制() -> None:
    orders = _calculate(volume=1000, target=0, available=300)
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 300)]


def test_在途卖单从差额里扣除() -> None:
    orders = _calculate(
        volume=1000,
        target=0,
        inflight={SYMBOL: {"SELL": 400}},
    )
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 600)]


def test_无target_quantities时旧行为不变_跌停信号被过滤() -> None:
    """基线：模型信号路径不传 target_quantities，跌停标的仍在规划层被过滤。"""
    quote = Quote(symbol=SYMBOL, current_price=10.0, is_limit_down=True)
    orders = CALC.calculate(
        signals=[_signal()],
        strategy=StrategyConfig(topk=1),
        quotes={SYMBOL: quote},
        account=_account(0),
    )
    assert orders == []


# ---------------------------------------------------------------------------
# 作用域：minibt 只管理自己的标的，账户里其它策略/历史持仓一律不碰
# ---------------------------------------------------------------------------


def test_清仓只卖目标标的_其他持仓不动() -> None:
    account = _account_with_other(volume=1000, other=500)
    orders = _calculate(volume=0, target=0, account=account)
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [(SYMBOL, "SELL", 1000)]


def test_买入只买目标标的_其他持仓不动() -> None:
    account = _account_with_other(volume=0, other=500)
    orders = _calculate(volume=0, target=1000, account=account)
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [(SYMBOL, "BUY", 1000)]


def test_前缀式目标键与后缀持仓对齐() -> None:
    account = _account_with_other(volume=1000, other=500)
    orders = _calculate(volume=0, target=0, account=account, target_symbol="SH600036")
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [(SYMBOL, "SELL", 1000)]


def test_前缀式持仓键也按后缀目标对齐() -> None:
    """账户存量键可能是前缀式（历史数据）；作用域必须归一后再比对。"""
    account = SimulationAccount(
        cash=1_000_000.0,
        total_asset=1_000_000.0,
        positions={
            "SH600036": {
                "volume": 1000,
                "available_volume": 1000,
                "price": 10.0,
                "market_value": 10_000.0,
            }
        },
    )
    orders = _calculate(volume=0, target=0, account=account)
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [(SYMBOL, "SELL", 1000)]
