"""T-P2-02 测试：撮合规则单实现——**规则平价测试**。

验收口径（细案）：同一 (symbol, 价格, 数量, 方向) 下
matcher / market_rules / 回测引擎 三处费用**逐分相等**（含最低佣金与卖方印花场景）；
申报数量归一一致（科创板 200 起 1 股递增，其余 100 整数倍）；涨跌停阈值同源。
"""

from pathlib import Path

import pandas as pd

from backend.services.simulation.services.ashare_matcher import (
    MatchConfig,
    compute_fees,
)
from backend.services.simulation.services.market_rules import (
    CN_RULES,
    normalize_order_quantity,
)

_BACKEND = Path(__file__).resolve().parents[1]

_FEE_CASES = [
    # (数量, 价格, 方向, 备注)
    (1000, 10.0, "buy", "常规买入"),
    (1000, 10.0, "sell", "常规卖出（含印花）"),
    (100, 10.0, "buy", "小额触发最低佣金"),
    (100000, 50.02, "sell", "大额卖出"),
    (200, 3.33, "buy", "科创板最小申报价位"),
]


def test_fee_parity_matcher_vs_market_rules():
    cfg = MatchConfig()  # 默认值来自 CN_RULES（单实现）
    for qty, price, side, note in _FEE_CASES:
        commission, stamp, transfer, total = compute_fees(qty, price, side, cfg)
        c2, s2, t2 = CN_RULES.compute_fee_breakdown(qty, price, side)
        assert (commission, stamp, transfer) == (c2, s2, t2), f"matcher≠rules: {note}"
        assert total == round(c2 + s2 + t2, 2), f"total 不平: {note}"


def test_fee_parity_backtest_engine():
    """回测引擎成交记录与 market_rules 逐分相等；买入按 lot 归一。"""
    from backend.shared.backtest_engine.core.engine import BacktestEngine
    from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderType

    engine = BacktestEngine(initial_cash=10_000_000.0, enable_risk_management=False)
    bar = pd.Series({"volume": 1_000_000, "close": 10.0, "high": 10.2, "low": 9.8})

    # 买入 150 股 → 应归一为 100（主板 100 整数倍）
    engine._execute_order(
        Order(symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=150),
        bar,
    )
    buy = engine.trades[-1]
    exec_price = 10.0 * (1 + engine.slippage_rate)
    c, s, t = CN_RULES.compute_fee_breakdown(int(buy["quantity"]), exec_price, "buy")
    assert buy["quantity"] == 100, f"买入应归一为 100，实际 {buy['quantity']}"
    assert (buy["commission"], buy["stamp_duty"], buy["transfer_fee"]) == (c, s, t)
    assert buy["total_fee"] == round(c + s + t, 2)

    # 卖出 100 股 → 含印花税，逐分相等
    engine._execute_order(
        Order(symbol="SH600036", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=100),
        bar,
    )
    sell = engine.trades[-1]
    c2, s2, t2 = CN_RULES.compute_fee_breakdown(100, exec_price * (1 - engine.slippage_rate), "sell")
    assert (sell["commission"], sell["stamp_duty"], sell["transfer_fee"]) == (c2, s2, t2)
    assert sell["stamp_duty"] > 0

    # 低于最小申报 → 拒（状态原地标记在订单对象上）
    from backend.shared.backtest_engine.core.order import OrderStatus

    below = Order(symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=50)
    engine._execute_order(below, bar)
    assert below.status == OrderStatus.REJECTED


def test_star_market_quantity_semantics():
    """科创板：≥200 可 1 股递增（201 合法）；低于 200 拒单；主板 150→100。"""
    assert normalize_order_quantity(201, "688981", "CN") == 201
    assert normalize_order_quantity(200, "SH688981", "CN") == 200
    assert normalize_order_quantity(150, "688981", "CN") == 0
    assert normalize_order_quantity(150, "600036", "CN") == 100
    assert normalize_order_quantity(250, "300750", "CN") == 200  # 创业板 100 整数倍
    # 回测引擎同语义
    from backend.shared.backtest_engine.core.engine import BacktestEngine
    from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderType, OrderStatus

    engine = BacktestEngine(initial_cash=1_000_000.0, enable_risk_management=False)
    bar = pd.Series({"volume": 1_000_000, "close": 60.0, "high": 61.0, "low": 59.0})
    engine._execute_order(
        Order(symbol="SH688981", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=201),
        bar,
    )
    assert engine.trades[-1]["quantity"] == 201
    below = Order(symbol="SH688981", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=150)
    engine._execute_order(below, bar)
    assert below.status == OrderStatus.REJECTED


def test_price_limit_threshold_single_source():
    """涨跌停阈值：回测引擎与 canonical 同源（含 ST 2026-07-06 切换两侧）。"""
    from datetime import date

    from backend.services.simulation.services.local_market_data import limit_pct
    from backend.shared.backtest_engine.core.engine import get_price_limit_threshold

    assert get_price_limit_threshold("SH600036") == float(
        limit_pct("600036", is_st=False, trade_date=date(2026, 9, 16))
    )
    assert get_price_limit_threshold("SZ300750") == 0.20
    assert get_price_limit_threshold("SH688981") == 0.20
    # ST 主板：2026-07-06 新规前 5%，后 10%（两侧都断言）
    before = get_price_limit_threshold("SH600036", is_st=True, trade_date=date(2026, 6, 30))
    after = get_price_limit_threshold("SH600036", is_st=True, trade_date=date(2026, 9, 16))
    assert before == 0.05 and after == 0.10


def test_source_single_implementation():
    """源断言：费用/手数不再有第二实现。"""
    matcher_src = (
        _BACKEND / "services/simulation/services/ashare_matcher.py"
    ).read_text(encoding="utf-8")
    assert "_COMMISSION_RATE = CN_RULES.commission_rate" in matcher_src, "费用常量须来自 CN_RULES"
    assert "def _floor_to_lot" not in matcher_src, "matcher 不得再有本地手数实现"

    engine_src = (
        _BACKEND / "shared/backtest_engine/core/engine.py"
    ).read_text(encoding="utf-8")
    assert "compute_fee_breakdown" in engine_src and "normalize_order_quantity" in engine_src
    assert "* self.commission_rate" not in engine_src, "回测引擎不得再有 flat 佣金计算"

    exec_src = (
        _BACKEND / "services/simulation/services/execution_engine.py"
    ).read_text(encoding="utf-8")
    assert "compute_fee_breakdown" in exec_src and "normalize_order_quantity" in exec_src

    rebalance_src = (
        _BACKEND / "services/simulation/services/rebalance_calculator.py"
    ).read_text(encoding="utf-8")
    assert "normalize_order_quantity" in rebalance_src
    assert "def _floor_to_lot" not in rebalance_src
