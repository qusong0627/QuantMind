"""融券（空头）成交的现金/负债/冻结资金增量唯一实现。

撮合侧 :meth:`SimulationAccountManager._update_balance_margin`（Redis 真值）
与台账投影 :meth:`SimulationLedgerService.apply_trade_to_account_snapshot`
（PG 投影）此前各写一套公式，口径不一致 → 对账把 Redis 覆盖回台账值、下一笔
撮合又按 Redis 公式算，反复互相覆盖。此模块收口为单一口径，两边共用。

经济含义（融券卖空）：
- ``sell_to_open``：借券卖出所得 ``gross`` 冻结进 ``short_proceeds``，同时记
  负债 ``gross``；现金只扣当日借券费 ``borrow_fee``。
- ``buy_to_close``：买回花费 ``gross``，释放对应开仓成本 ``entry_val`` 的冻结
  资金与负债，实现盈亏 ``entry_val - gross - borrow_fee`` 结算进现金。
"""

from __future__ import annotations

BORROW_RATE_DAYS = 252.0


def default_borrow_rate() -> float:
    """年化融券费率（与撮合侧 settings.DEFAULT_BORROW_RATE 同源）。"""
    try:
        from backend.services.trade_shared.trade_config import settings

        return float(getattr(settings, "DEFAULT_BORROW_RATE", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def margin_trade_deltas(
    *,
    trade_action: str | None,
    gross: float,
    quantity: float,
    pos_cost: float,
    borrow_rate: float | None = None,
) -> dict[str, float]:
    """返回融券成交对账户字段的增量（cash/short_proceeds/liabilities/borrow_fee）。

    非融券动作返回全 0（调用方自行处理普通买卖）。
    """
    action = str(trade_action or "").strip().lower()
    rate = default_borrow_rate() if borrow_rate is None else float(borrow_rate)
    borrow_fee = float(gross or 0.0) * rate / BORROW_RATE_DAYS
    if action == "sell_to_open":
        return {
            "cash": -borrow_fee,
            "short_proceeds": float(gross or 0.0),
            "liabilities": float(gross or 0.0),
            "borrow_fee": borrow_fee,
        }
    if action == "buy_to_close":
        entry_val = float(pos_cost or 0.0) * float(quantity or 0.0)
        realized = entry_val - float(gross or 0.0) - borrow_fee
        return {
            "cash": realized,
            "short_proceeds": -entry_val,
            "liabilities": -entry_val,
            "borrow_fee": borrow_fee,
        }
    return {"cash": 0.0, "short_proceeds": 0.0, "liabilities": 0.0, "borrow_fee": 0.0}
