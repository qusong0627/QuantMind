"""``qmt_exec_reconciler.apply_execution_report`` 状态机单测（假 DB，不连真库）。

重点覆盖**终态守卫**：订单进入 FILLED/CANCELLED 后，陈旧或乱序的回报（桥重发、
poller 重启补拉、重连后的全量快照）不得把它改回中间态，也不得再累计成交。
其余是常规状态推进与去重的回归。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from backend.services.live_trading.services import qmt_exec_reconciler as mod
from backend.services.trade_shared.models.enums import OrderSide, OrderStatus


class FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = list(rows or [])

    def scalars(self) -> FakeResult:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Any:
        return self.first()


class FakeSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = list(rows or [])
        self.added: list[Any] = []

    async def execute(self, *_args: Any, **_kwargs: Any) -> FakeResult:
        return FakeResult(self.rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _order(
    status: OrderStatus = OrderStatus.SUBMITTED,
    *,
    filled: float = 0.0,
    quantity: float = 100.0,
    filled_value: float = 0.0,
) -> Any:
    return SimpleNamespace(
        order_id="9001",
        tenant_id="default",
        user_id="1",
        portfolio_id=None,
        symbol="SH600519",
        symbol_name="贵州茅台",
        side=OrderSide.BUY,
        trade_action=None,
        position_side=None,
        is_margin_trade=False,
        trading_mode="REAL",
        quantity=quantity,
        price=10.0,
        filled_quantity=filled,
        filled_value=filled_value,
        average_price=(filled_value / filled) if filled else None,
        status=status,
        remarks=None,
        exchange_order_id=None,
        filled_at=None,
        cancelled_at=None,
    )


def _apply(
    order: Any,
    status: Any,
    *,
    qty: Any = None,
    price: Any = None,
    trade_id: str = "",
    session: FakeSession | None = None,
) -> tuple[OrderStatus, FakeSession]:
    session = session or FakeSession()
    result = asyncio.run(
        mod.apply_execution_report(
            session,
            order=order,
            status_raw=status,
            filled_quantity=qty,
            filled_price=price,
            exchange_trade_id=trade_id,
            exchange_order_id="1001",
        )
    )
    return result, session


class TestTerminalGuard:
    def test_filled_not_regressed_by_stale_submitted(self) -> None:
        order = _order(OrderStatus.FILLED, filled=100.0, filled_value=1000.0)
        result, session = _apply(order, "SUBMITTED", qty=0)
        assert result is OrderStatus.FILLED
        assert order.status is OrderStatus.FILLED
        assert order.filled_quantity == 100.0
        assert session.added == []

    def test_cancelled_not_regressed_by_late_fill(self) -> None:
        """已撤单后到的成交回报不得改状态、不得再入账成交。"""
        order = _order(OrderStatus.CANCELLED, filled=40.0, filled_value=400.0)
        result, session = _apply(
            order, "PARTIALLY_FILLED", qty=40.0, price=10.0, trade_id="T9"
        )
        assert result is OrderStatus.CANCELLED
        assert order.status is OrderStatus.CANCELLED
        assert order.filled_quantity == 40.0
        assert session.added == []

    def test_cancelled_not_regressed_by_stale_partial_snapshot(self) -> None:
        order = _order(OrderStatus.CANCELLED)
        result, _ = _apply(order, "PARTIALLY_FILLED", qty=0)
        assert result is OrderStatus.CANCELLED

    def test_rejected_not_regressed(self) -> None:
        order = _order(OrderStatus.REJECTED)
        result, _ = _apply(order, "SUBMITTED")
        assert result is OrderStatus.REJECTED

    def test_filled_still_accepts_new_trade(self) -> None:
        """已成单收到新成交号：状态不动，但成交明细仍要落库（补录）。"""
        order = _order(OrderStatus.FILLED, filled=100.0, filled_value=1000.0)
        result, session = _apply(
            order, "FILLED", qty=100.0, price=10.0, trade_id="T-new"
        )
        assert result is OrderStatus.FILLED
        assert len(session.added) == 1
        assert order.filled_quantity == 200.0


class TestStatusTransitions:
    def test_partial_fill_report(self) -> None:
        order = _order()
        result, session = _apply(
            order, "PARTIALLY_FILLED", qty=40.0, price=10.0, trade_id="T1"
        )
        assert result is OrderStatus.PARTIALLY_FILLED
        assert order.filled_quantity == 40.0
        assert order.average_price == 10.0
        assert len(session.added) == 1

    def test_full_fill_promotes_to_filled(self) -> None:
        order = _order()
        result, session = _apply(
            order, "PARTIALLY_FILLED", qty=100.0, price=10.0, trade_id="T1"
        )
        assert result is OrderStatus.FILLED
        assert order.filled_at is not None
        assert len(session.added) == 1

    def test_cancel_after_partial_fill_keeps_cancelled(self) -> None:
        """部成后撤单：状态必须是 CANCELLED 并落 cancelled_at（不能被累计成交改回部成）。"""
        order = _order(OrderStatus.PARTIALLY_FILLED, filled=40.0, filled_value=400.0)
        result, _ = _apply(order, "CANCELLED")
        assert result is OrderStatus.CANCELLED
        assert order.status is OrderStatus.CANCELLED
        assert order.cancelled_at is not None

    def test_filled_without_quantity_degrades_to_submitted(self) -> None:
        order = _order()
        result, _ = _apply(order, "FILLED", qty=0)
        assert result is OrderStatus.SUBMITTED

    def test_duplicate_trade_id_not_double_counted(self) -> None:
        existing = SimpleNamespace(exchange_trade_id="T1")
        order = _order()
        result, session = _apply(
            order,
            "PARTIALLY_FILLED",
            qty=40.0,
            price=10.0,
            trade_id="T1",
            session=FakeSession(rows=[existing]),
        )
        assert result is OrderStatus.PARTIALLY_FILLED
        assert order.filled_quantity == 0.0
        assert session.added == []
