"""单笔申报数量上限（2026-07-06 新规，官方规则原文核实 2026-09-16）测试。

口径：主板/创业板 限价≤30万、市价≤15万；科创板 限价≤10万、市价≤5万；
盘后固定价格（全 A 股/ETF）≤100 万股；非 CN 不设限。唯一实现 market_rules.order_quantity_cap，
执行侧接线：模拟 execution_engine（含会话判定）+ 回测 BacktestEngine（同规则，保平价）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.shared.backtest_engine.core.engine import BacktestEngine
from backend.shared.backtest_engine.core.order import Order, OrderSide, OrderStatus, OrderType

_BACKEND = Path(__file__).resolve().parents[1]


# ── 纯函数表 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_order_quantity_cap_table():
    from backend.services.simulation.services.market_rules import order_quantity_cap

    # 主板/创业板：限价 30 万、市价 15 万
    assert order_quantity_cap("600036.SH", order_type="limit") == 300_000
    assert order_quantity_cap("600036.SH", order_type="market") == 150_000
    assert order_quantity_cap("300750.SZ", order_type="limit") == 300_000  # 创业板同主板
    assert order_quantity_cap("000001.SZ", order_type="market") == 150_000
    # 科创板：限价 10 万、市价 5 万
    assert order_quantity_cap("688111.SH", order_type="limit") == 100_000
    assert order_quantity_cap("688111.SH", order_type="market") == 50_000
    # 盘后固定价格：统一 100 万股（不分板块）
    assert (
        order_quantity_cap("600036.SH", order_type="limit", session="after_hours_fixed")
        == 1_000_000
    )
    assert (
        order_quantity_cap("688111.SH", order_type="market", session="after_hours_fixed")
        == 1_000_000
    )
    # 类型缺失 → 取最宽松档（限价档，宁可不误拒）
    assert order_quantity_cap("600036.SH") == 300_000
    assert order_quantity_cap("688111.SH") == 100_000
    # 非 CN 不设限
    assert order_quantity_cap("0700.HK") is None
    assert order_quantity_cap("AAPL", market="US") is None


# ── 回测引擎接线（与模拟同规则）────────────────────────────────────


def _frame() -> pd.DataFrame:
    dates = pd.date_range("2026-03-02", periods=3, freq="B")
    return pd.DataFrame(
        {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100_000_000},
        index=dates,
    )


def test_backtest_engine_enforces_caps():
    eng = BacktestEngine(initial_cash=100_000_000.0, enable_risk_management=False)
    bar = pd.Series({"volume": 100_000_000, "close": 10.0, "high": 10.0, "low": 10.0})

    # 限价 40 万股 > 30 万上限 → 拒
    over = Order(
        symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=400_000, price=10.0
    )
    eng._execute_order(over, bar)
    assert over.status == OrderStatus.REJECTED
    assert "超过上限" in str(over.reject_reason)

    # 限价 30 万股 = 上限 → 成交
    ok = Order(
        symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=300_000, price=10.0
    )
    eng._execute_order(ok, bar)
    assert ok.status == OrderStatus.FILLED

    # 市价 16 万股 > 15 万上限 → 拒
    mkt_over = Order(
        symbol="SH600036", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=160_000
    )
    eng._execute_order(mkt_over, bar)
    assert mkt_over.status == OrderStatus.REJECTED
    assert "超过上限" in str(mkt_over.reject_reason)

    # 科创板限价 20 万股 > 10 万上限 → 拒
    star_over = Order(
        symbol="SH688111", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=200_000, price=10.0
    )
    eng._execute_order(star_over, bar)
    assert star_over.status == OrderStatus.REJECTED

    # 卖出同样受限（限价 31 万股 → 拒）
    sell_over = Order(
        symbol="SH600036", side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=310_000, price=10.0
    )
    eng._execute_order(sell_over, bar)
    assert sell_over.status == OrderStatus.REJECTED
    assert "超过上限" in str(sell_over.reject_reason)


# ── 源守卫 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cap_wiring_source_guards():
    exec_src = (
        _BACKEND / "services/simulation/services/execution_engine.py"
    ).read_text(encoding="utf-8")
    assert "order_quantity_cap(" in exec_src
    assert "单笔申报数量超过上限" in exec_src
    # 会话判定必须先于申报校验（盘后上限依赖会话）
    assert exec_src.index("after_hours_fixed = (") < exec_src.index("order_quantity_cap(")

    bt_src = (_BACKEND / "shared/backtest_engine/core/engine.py").read_text(
        encoding="utf-8"
    )
    assert "order_quantity_cap(" in bt_src

    mr_src = (_BACKEND / "services/simulation/services/market_rules.py").read_text(
        encoding="utf-8"
    )
    assert "_CAP_AFTER_HOURS = 1_000_000" in mr_src
    assert "order_quantity_cap" in mr_src
