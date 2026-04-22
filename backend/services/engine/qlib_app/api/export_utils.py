"""Qlib 回测导出工具"""

from typing import Any, Optional


def _to_finite_float(value: Any) -> float | None:
    try:
        val = float(value)
    except Exception:
        return None
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return val


def _normalize_display_quantity(symbol: str, quantity: float) -> int:
    qty_int = int(round(float(quantity)))
    symbol_upper = str(symbol or "").upper()
    if symbol_upper.startswith(("SH", "SZ", "BJ")) and qty_int >= 100:
        lot_rounded = int(round(qty_int / 100.0) * 100)
        if abs(qty_int - lot_rounded) <= 2:
            return lot_rounded
    return qty_int


def _build_quick_trade_rows(
    *,
    trades: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    initial_capital: float | None,
) -> list[dict[str, Any]]:
    equity_by_date: dict[str, float] = {}
    for point in equity_curve:
        if not isinstance(point, dict):
            continue
        date_key = str(point.get("date", ""))[:10]
        value = _to_finite_float(point.get("value"))
        if date_key and value is not None:
            equity_by_date[date_key] = value

    running_balance: float | None = initial_capital
    rows: list[dict[str, Any]] = []

    for trade in trades:
        if not isinstance(trade, dict):
            continue

        factor = _to_finite_float(trade.get("factor"))
        has_valid_factor = factor is not None and factor > 0

        explicit_price = _to_finite_float(trade.get("price"))
        explicit_quantity = _to_finite_float(trade.get("quantity"))
        adj_price = _to_finite_float(trade.get("adj_price"))
        adj_quantity = _to_finite_float(trade.get("adj_quantity"))

        if adj_price is None:
            adj_price = explicit_price
        if adj_quantity is None:
            adj_quantity = explicit_quantity

        display_price = explicit_price if explicit_price is not None else 0.0
        display_quantity = explicit_quantity if explicit_quantity is not None else 0.0
        should_restore = explicit_quantity is None and has_valid_factor and adj_price is not None and adj_quantity is not None
        if should_restore:
            display_price = adj_price / factor
            display_quantity = adj_quantity * factor

        qty_int = _normalize_display_quantity(str(trade.get("symbol", "")), display_quantity)
        amount = _to_finite_float(trade.get("totalAmount", trade.get("total_amount")))
        if amount is None:
            amount = display_price * display_quantity

        commission = _to_finite_float(trade.get("commission"))
        if commission is None:
            commission = 0.0

        action_raw = str(trade.get("action", "")).strip().lower()
        is_buy = action_raw == "buy"
        is_sell = action_raw == "sell"

        has_balance = _to_finite_float(trade.get("balance")) is not None
        has_equity_after = _to_finite_float(trade.get("equity_after")) is not None
        if running_balance is not None and not has_balance and not has_equity_after:
            if is_buy:
                running_balance -= amount + commission
            if is_sell:
                running_balance += amount - commission

        trade_date = str(trade.get("date", ""))
        trade_day = trade_date[:10]
        eq_on_date = equity_by_date.get(trade_day)
        equity_balance = (
            eq_on_date
            if eq_on_date is not None
            else _to_finite_float(trade.get("equity_after"))
            if _to_finite_float(trade.get("equity_after")) is not None
            else _to_finite_float(trade.get("balance"))
            if _to_finite_float(trade.get("balance")) is not None
            else running_balance
        )

        rows.append(
            {
                "date": trade_date,
                "symbol": str(trade.get("symbol", "")),
                "action": "买入" if is_buy else "卖出",
                "display_price": display_price,
                "qty_int": qty_int,
                "amount": amount,
                "commission": commission,
                "equity_balance": equity_balance,
            }
        )
    return rows
