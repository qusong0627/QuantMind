"""
Simulation ledger/account projection service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.position_lot import SimulationPositionLot


@dataclass
class CashLedgerEntry:
    event_type: str
    amount: float
    note: str | None = None


def _naive_utc(value: datetime | None) -> datetime:
    """ledger 表的时间列是 naive 口径（与 SimTrade.executed_at 的 aware UTC 对齐到边界剥离）。

    直接把 aware 值写入 naive 列会在 asyncpg 层抛
    "can't subtract offset-naive and offset-aware datetimes"，导致整笔成交落库失败。
    """
    try:
        if value is not None and getattr(value, "tzinfo", None) is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        if value is not None:
            return value
    except Exception:
        pass
    return datetime.utcnow()


class SimulationLedgerService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def build_account_id(tenant_id: str, user_id: str | int) -> str:
        return f"sim:{str(tenant_id or 'default').strip() or 'default'}:{str(user_id).strip()}"

    @staticmethod
    def build_cash_entries(*, side: str, trade_value: float, commission: float, stamp_duty: float, transfer_fee: float) -> list[CashLedgerEntry]:
        normalized_side = str(side or "").strip().lower()
        principal_event = "BUY_SETTLEMENT" if normalized_side == "buy" else "SELL_PROCEEDS"
        principal_amount = -abs(float(trade_value or 0.0)) if normalized_side == "buy" else abs(float(trade_value or 0.0))
        entries = [CashLedgerEntry(event_type=principal_event, amount=principal_amount)]
        if commission:
            entries.append(CashLedgerEntry(event_type="COMMISSION", amount=-abs(float(commission))))
        if stamp_duty:
            entries.append(CashLedgerEntry(event_type="STAMP_DUTY", amount=-abs(float(stamp_duty))))
        if transfer_fee:
            entries.append(CashLedgerEntry(event_type="TRANSFER_FEE", amount=-abs(float(transfer_fee))))
        return entries

    @staticmethod
    def apply_trade_to_account_snapshot(
        *,
        trade: Any,
        account_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = dict(account_snapshot or {})
        cash = float(snapshot.get("cash") or 0.0)
        available_cash = float(snapshot.get("available_cash") or cash)
        short_proceeds = float(snapshot.get("short_proceeds") or 0.0)
        liabilities = float(snapshot.get("liabilities") or 0.0)
        short_market_value = float(snapshot.get("short_market_value") or 0.0)

        side = str(getattr(getattr(trade, "side", None), "value", getattr(trade, "side", "")) or "").strip().lower()
        trade_action = str(getattr(trade, "trade_action", None) or "").strip().lower()
        position_side = str(getattr(trade, "position_side", None) or "long").strip().lower()
        gross = float(getattr(trade, "trade_value", 0.0) or 0.0)
        total_fee = float(getattr(trade, "total_fee", 0.0) or 0.0)

        if position_side == "short" or trade_action in {"sell_to_open", "buy_to_close"}:
            if trade_action == "sell_to_open":
                cash -= total_fee
                available_cash -= total_fee
                short_proceeds += gross
                liabilities += gross
                short_market_value += gross
            elif trade_action == "buy_to_close":
                cash -= gross + total_fee
                available_cash -= gross + total_fee
                liabilities = max(0.0, liabilities - gross)
                short_proceeds = max(0.0, short_proceeds - gross)
                short_market_value = max(0.0, short_market_value - gross)
        else:
            if side == "buy":
                cash -= gross + total_fee
                available_cash -= gross + total_fee
            else:
                cash += gross - total_fee
                available_cash += gross - total_fee

        snapshot["cash"] = cash
        snapshot["available_cash"] = available_cash
        snapshot["short_proceeds"] = short_proceeds
        snapshot["liabilities"] = liabilities
        snapshot["short_market_value"] = short_market_value
        snapshot["total_asset"] = float(snapshot.get("total_asset") or 0.0)
        return snapshot

    async def record_trade(
        self,
        *,
        order: Any,
        trade: Any,
        account_snapshot: dict[str, Any] | None,
    ) -> None:
        tenant_id = str(getattr(order, "tenant_id", None) or "default").strip() or "default"
        user_id = str(getattr(order, "user_id", None) or "").strip()
        if not user_id:
            return
        account_id = self.build_account_id(tenant_id, user_id)
        before_snapshot = dict(account_snapshot or {})
        account = await self._ensure_account(account_id=account_id, tenant_id=tenant_id, user_id=user_id, account_snapshot=before_snapshot)
        after_snapshot = self.apply_trade_to_account_snapshot(
            trade=trade,
            account_snapshot=before_snapshot,
        )

        cash_entries = self.build_cash_entries(
            side=getattr(order.side, "value", order.side),
            trade_value=float(getattr(trade, "trade_value", 0.0) or 0.0),
            commission=float(getattr(trade, "commission", 0.0) or 0.0),
            stamp_duty=float(getattr(trade, "stamp_duty", 0.0) or 0.0),
            transfer_fee=float(getattr(trade, "transfer_fee", 0.0) or 0.0),
        )
        await self._append_cash_entries(
            account=account,
            tenant_id=tenant_id,
            user_id=user_id,
            ref_id=str(getattr(trade, "trade_id", "") or ""),
            trade_time=_naive_utc(getattr(trade, "executed_at", None)),
            entries=cash_entries,
            ending_balance=float(after_snapshot.get("cash") or account.cash or 0.0),
        )

        await self._apply_position_lots(
            account_id=account_id,
            tenant_id=tenant_id,
            user_id=user_id,
            order=order,
            trade=trade,
        )

        self._sync_account_projection(account, after_snapshot)

    async def _ensure_account(
        self,
        *,
        account_id: str,
        tenant_id: str,
        user_id: str,
        account_snapshot: dict[str, Any],
    ) -> SimulationAccount:
        account = await self.db.get(SimulationAccount, account_id)
        if account is None:
            initial_equity = float(
                account_snapshot.get("initial_equity")
                or ((account_snapshot.get("baseline") or {}).get("initial_equity") if isinstance(account_snapshot.get("baseline"), dict) else 0.0)
                or account_snapshot.get("total_asset")
                or account_snapshot.get("cash")
                or 0.0
            )
            account = SimulationAccount(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=user_id,
                initial_equity=initial_equity,
                cash=float(account_snapshot.get("cash") or 0.0),
                available_cash=float(account_snapshot.get("available_cash") or account_snapshot.get("cash") or 0.0),
                total_asset=float(account_snapshot.get("total_asset") or initial_equity),
                equity=float(account_snapshot.get("total_asset") or initial_equity),
                long_market_value=max(0.0, float(account_snapshot.get("market_value") or 0.0)),
                short_market_value=max(0.0, float(account_snapshot.get("short_market_value") or 0.0)),
                liabilities=float(account_snapshot.get("liabilities") or 0.0),
                maintenance_margin_ratio=float(account_snapshot.get("maintenance_margin_ratio") or 0.0),
            )
            self.db.add(account)
            await self.db.flush()
        return account

    async def _append_cash_entries(
        self,
        *,
        account: SimulationAccount,
        tenant_id: str,
        user_id: str,
        ref_id: str,
        trade_time: datetime,
        entries: list[CashLedgerEntry],
        ending_balance: float,
    ) -> None:
        running_balance = float(ending_balance or 0.0) - sum(
            float(entry.amount or 0.0) for entry in entries
        )
        for entry in entries:
            running_balance += float(entry.amount or 0.0)
            self.db.add(
                SimulationCashLedger(
                    account_id=account.account_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_type=entry.event_type,
                    ref_type="trade",
                    ref_id=ref_id or None,
                    amount=float(entry.amount or 0.0),
                    balance_after=running_balance,
                    trade_date=trade_time,
                    occurred_at=trade_time,
                    note=entry.note,
                )
            )

    async def _apply_position_lots(
        self,
        *,
        account_id: str,
        tenant_id: str,
        user_id: str,
        order: Any,
        trade: Any,
    ) -> None:
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if not symbol:
            return
        quantity = float(getattr(trade, "quantity", 0.0) or 0.0)
        if quantity <= 0:
            return
        price = float(getattr(trade, "price", 0.0) or 0.0)
        total_fee = float(getattr(trade, "total_fee", 0.0) or 0.0)
        side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", "")) or "").strip().lower()
        trade_action = str(getattr(order, "trade_action", None) or "").strip().lower()
        position_side = str(getattr(order, "position_side", None) or "long").strip().lower()
        occurred_at = _naive_utc(getattr(trade, "executed_at", None))
        fill_id = str(getattr(trade, "trade_id", "") or "")

        if position_side == "short":
            if trade_action == "sell_to_open":
                self.db.add(
                    SimulationPositionLot(
                        account_id=account_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        symbol=symbol,
                        position_side="short",
                        open_fill_id=fill_id or None,
                        open_date=occurred_at,
                        quantity_open=quantity,
                        quantity_remaining=quantity,
                        cost_price=price,
                        cost_amount=price * quantity,
                        status="open",
                    )
                )
                return
            if trade_action == "buy_to_close":
                await self._consume_lots(
                    account_id=account_id,
                    symbol=symbol,
                    position_side="short",
                    quantity=quantity,
                    closed_at=occurred_at,
                )
                return

        if side == "buy":
            self.db.add(
                SimulationPositionLot(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    symbol=symbol,
                    position_side="long",
                    open_fill_id=fill_id or None,
                    open_date=occurred_at,
                    quantity_open=quantity,
                    quantity_remaining=quantity,
                    cost_price=((price * quantity) + total_fee) / quantity if quantity > 0 else price,
                    cost_amount=(price * quantity) + total_fee,
                    status="open",
                )
            )
            return

        await self._consume_lots(
            account_id=account_id,
            symbol=symbol,
            position_side="long",
            quantity=quantity,
            closed_at=occurred_at,
        )

    async def _consume_lots(
        self,
        *,
        account_id: str,
        symbol: str,
        position_side: str,
        quantity: float,
        closed_at: datetime,
    ) -> None:
        remaining = float(quantity or 0.0)
        if remaining <= 0:
            return
        stmt: Select[tuple[SimulationPositionLot]] = (
            select(SimulationPositionLot)
            .where(
                SimulationPositionLot.account_id == account_id,
                SimulationPositionLot.symbol == symbol,
                SimulationPositionLot.position_side == position_side,
                SimulationPositionLot.status == "open",
                SimulationPositionLot.quantity_remaining > 0,
            )
            .order_by(
                SimulationPositionLot.open_date.asc().nullsfirst(),
                SimulationPositionLot.id.asc(),
            )
        )
        lots = list((await self.db.execute(stmt)).scalars().all())
        for lot in lots:
            if remaining <= 0:
                break
            available = float(lot.quantity_remaining or 0.0)
            if available <= 0:
                continue
            consumed = min(available, remaining)
            lot.quantity_remaining = max(0.0, available - consumed)
            remaining -= consumed
            if lot.quantity_remaining <= 1e-6:
                lot.quantity_remaining = 0.0
                lot.status = "closed"
                lot.closed_at = closed_at

    def _sync_account_projection(
        self,
        account: SimulationAccount,
        account_snapshot: dict[str, Any],
    ) -> None:
        market_value = float(account_snapshot.get("market_value") or 0.0)
        short_market_value = float(account_snapshot.get("short_market_value") or 0.0)
        account.cash = float(account_snapshot.get("cash") or account.cash or 0.0)
        account.available_cash = float(
            account_snapshot.get("available_cash")
            or account_snapshot.get("cash")
            or account.available_cash
            or 0.0
        )
        account.frozen_cash = max(0.0, account.cash - account.available_cash)
        account.long_market_value = max(0.0, market_value)
        account.short_market_value = max(0.0, short_market_value)
        account.total_asset = float(account_snapshot.get("total_asset") or account.total_asset or 0.0)
        account.liabilities = float(account_snapshot.get("liabilities") or account.liabilities or 0.0)
        account.equity = float(account_snapshot.get("total_asset") or account.equity or 0.0)
        account.maintenance_margin_ratio = float(
            account_snapshot.get("maintenance_margin_ratio")
            or account.maintenance_margin_ratio
            or 0.0
        )
        now = datetime.utcnow()
        account.last_trade_at = now
        account.last_projected_at = now
