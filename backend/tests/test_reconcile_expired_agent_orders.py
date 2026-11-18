"""过期委托补记的判定测试（P0，2026-11-18）。"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.scripts.reconcile_expired_agent_orders import build_recovery


def _order(**kw):
    base = {
        "order_id": "ord-1",
        "agent": "deepseek-v4-pro",
        "symbol": "601825.SH",
        "side": "sell",
        "filled_quantity": 1400,
        "average_price": None,
        "price": 9.01,
        "created_at": datetime(2026, 11, 11, 6, 48, tzinfo=timezone.utc),
    }
    base.update(kw)
    return base


def test_buy_recovered_from_account_excess_using_cost() -> None:
    order = _order(
        symbol="601658.SH", side="buy", filled_quantity=3300, price=5.70
    )
    account_excess = {"601658.SH": {"ledger": 0.0, "account": 3300.0}}

    rec = build_recovery(order, {}, account_excess, {"601658.SH": 5.632})

    assert rec is not None
    assert rec.side == "buy"
    assert rec.volume == 3300
    assert rec.price == 5.632
    assert rec.code == "601658.SH"


def test_buy_skipped_when_position_gap_smaller_than_order() -> None:
    order = _order(symbol="601658.SH", side="buy", filled_quantity=3300)
    account_excess = {"601658.SH": {"ledger": 0.0, "account": 1200.0}}

    assert build_recovery(order, {}, account_excess, {"601658.SH": 5.632}) is None


def test_sell_recovered_from_ledger_excess_using_order_price() -> None:
    order = _order()
    ledger_excess = {"601825.SH": {"ledger": 6900.0, "account": 5500.0}}

    rec = build_recovery(order, ledger_excess, {}, {})

    assert rec is not None
    assert rec.side == "sell"
    assert rec.volume == 1400
    assert rec.price == 9.01


def test_sell_skipped_when_no_ledger_excess() -> None:
    order = _order()
    assert build_recovery(order, {}, {}, {}) is None


def test_uses_order_quantity_when_filled_quantity_is_zero() -> None:
    """过期单的 filled_quantity 恒为 0（没收到回报）；数量按委托量+持仓差判。"""
    order = _order(
        symbol="002958.SZ", side="buy", filled_quantity=0, quantity=2400, price=3.03
    )
    account_excess = {"002958.SZ": {"ledger": 0.0, "account": 2400.0}}

    rec = build_recovery(order, {}, account_excess, {"002958.SZ": 3.001})

    assert rec is not None
    assert rec.volume == 2400
    assert rec.price == 3.001
