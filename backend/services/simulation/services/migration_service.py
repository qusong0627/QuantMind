"""
Replay legacy sim_trades into the new simulation ledger tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.account_daily import SimulationAccountDaily
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.fill import SimulationFill
from backend.services.simulation.models.order import SimOrder
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.models.position_daily import SimulationPositionDaily
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services.ledger_service import (
    SimulationLedgerService,
)


@dataclass
class SimulationMigrationResult:
    replayed_trades: int
    skipped_short_trades: int
    account_id: str


@dataclass
class SimulationHistoryBackfillResult:
    created_orders: int
    created_fills: int


class SimulationMigrationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.ledger_service = SimulationLedgerService(db)

    async def replay_legacy_trades(
        self,
        *,
        tenant_id: str,
        user_id: str,
        initial_equity: float,
        reset_existing: bool = True,
    ) -> SimulationMigrationResult:
        account_id = self.ledger_service.build_account_id(tenant_id, user_id)
        if reset_existing:
            await self._reset_new_ledger(account_id=account_id, tenant_id=tenant_id, user_id=user_id)

        trades = list(
            (
                await self.db.execute(
                    select(SimTrade)
                    .where(
                        SimTrade.tenant_id == tenant_id,
                        SimTrade.user_id == int(user_id),
                    )
                    .order_by(
                        SimTrade.executed_at.asc(),
                        SimTrade.id.asc(),
                    )
                )
            ).scalars().all()
        )

        account_snapshot: dict[str, object] = {
            "initial_equity": float(initial_equity or 0.0),
            "cash": float(initial_equity or 0.0),
            "available_cash": float(initial_equity or 0.0),
            "total_asset": float(initial_equity or 0.0),
            "positions": {},
            "liabilities": 0.0,
            "short_market_value": 0.0,
            "baseline": {
                "initial_equity": float(initial_equity or 0.0),
                "day_open_equity": float(initial_equity or 0.0),
                "month_open_equity": float(initial_equity or 0.0),
            },
        }

        replayed = 0
        skipped_short = 0
        for trade in trades:
            side_value = getattr(getattr(trade, "side", None), "value", getattr(trade, "side", ""))
            before_snapshot = dict(account_snapshot)
            order_stub = SimpleNamespace(
                tenant_id=trade.tenant_id,
                user_id=user_id,
                symbol=trade.symbol,
                side=SimpleNamespace(value=side_value),
                trade_action=getattr(trade, "trade_action", None),
                position_side=getattr(trade, "position_side", None) or "long",
            )
            account_snapshot = self.ledger_service.apply_trade_to_account_snapshot(
                trade=trade,
                account_snapshot=before_snapshot,
                order=order_stub,
            )
            if str(getattr(trade, "position_side", "long") or "long").strip().lower() == "short":
                skipped_short += 1
            await self.ledger_service.record_trade(
                order=order_stub,
                trade=trade,
                account_snapshot=before_snapshot,
            )
            replayed += 1

        return SimulationMigrationResult(
            replayed_trades=replayed,
            skipped_short_trades=skipped_short,
            account_id=account_id,
        )

    async def ensure_projection_from_legacy(
        self,
        *,
        tenant_id: str,
        user_id: str,
        initial_equity: float,
    ) -> bool:
        account_id = self.ledger_service.build_account_id(tenant_id, user_id)
        existing_account = await self.db.get(SimulationAccount, account_id)
        if existing_account is not None:
            return False

        legacy_count = int(
            (
                await self.db.execute(
                    select(SimTrade.id)
                    .where(
                        SimTrade.tenant_id == tenant_id,
                        SimTrade.user_id == int(user_id),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            or 0
        )
        if legacy_count <= 0:
            return False

        await self.replay_legacy_trades(
            tenant_id=tenant_id,
            user_id=user_id,
            initial_equity=initial_equity,
            reset_existing=False,
        )
        return True

    async def ensure_history_models_backfilled(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> SimulationHistoryBackfillResult:
        normalized_user_id = str(user_id)
        legacy_user_id = int(normalized_user_id)

        existing_orders = {
            row[0]
            for row in (
                await self.db.execute(
                    select(SimulationOrderV2.order_id).where(
                        SimulationOrderV2.tenant_id == tenant_id,
                        SimulationOrderV2.user_id == normalized_user_id,
                    )
                )
            ).all()
        }
        existing_fills = {
            row[0]
            for row in (
                await self.db.execute(
                    select(SimulationFill.fill_id).where(
                        SimulationFill.tenant_id == tenant_id,
                        SimulationFill.user_id == normalized_user_id,
                    )
                )
            ).all()
        }

        legacy_orders = list(
            (
                await self.db.execute(
                    select(SimOrder)
                    .where(
                        SimOrder.tenant_id == tenant_id,
                        SimOrder.user_id == legacy_user_id,
                    )
                    .order_by(SimOrder.created_at.asc(), SimOrder.id.asc())
                )
            ).scalars().all()
        )
        created_orders = 0
        for order in legacy_orders:
            if order.order_id in existing_orders:
                continue
            self.db.add(
                SimulationOrderV2(
                    order_id=order.order_id,
                    tenant_id=tenant_id,
                    user_id=normalized_user_id,
                    strategy_id=str(order.strategy_id) if order.strategy_id is not None else None,
                    account_id=self.ledger_service.build_account_id(tenant_id, normalized_user_id),
                    portfolio_id=int(order.portfolio_id or 0),
                    legacy_order_id=order.id,
                    symbol=order.symbol,
                    side=str(getattr(order.side, "value", order.side) or "").lower(),
                    position_side=str(order.position_side or "long").lower(),
                    trade_action=order.trade_action,
                    order_type=str(getattr(order.order_type, "value", order.order_type) or "").lower(),
                    quantity=float(order.quantity or 0.0),
                    price=float(order.price) if order.price is not None else None,
                    trigger_source="legacy_replay",
                    status=str(getattr(order.status, "value", order.status) or "pending").lower(),
                    rejected_reason=order.remarks if str(getattr(order.status, "value", order.status) or "").lower() == "rejected" else None,
                    trading_session_date=(order.submitted_at.date() if order.submitted_at else None),
                    submitted_at=order.submitted_at,
                )
            )
            created_orders += 1

        legacy_order_map = {order.order_id: order for order in legacy_orders}
        legacy_trades = list(
            (
                await self.db.execute(
                    select(SimTrade)
                    .where(
                        SimTrade.tenant_id == tenant_id,
                        SimTrade.user_id == legacy_user_id,
                    )
                    .order_by(SimTrade.executed_at.asc(), SimTrade.id.asc())
                )
            ).scalars().all()
        )
        created_fills = 0
        for trade in legacy_trades:
            if trade.trade_id in existing_fills:
                continue
            legacy_order = legacy_order_map.get(trade.order_id)
            self.db.add(
                SimulationFill(
                    fill_id=trade.trade_id,
                    order_id=trade.order_id,
                    legacy_trade_id=trade.id,
                    tenant_id=tenant_id,
                    user_id=normalized_user_id,
                    account_id=self.ledger_service.build_account_id(tenant_id, normalized_user_id),
                    strategy_id=str(legacy_order.strategy_id) if legacy_order and legacy_order.strategy_id is not None else None,
                    portfolio_id=int(trade.portfolio_id or 0),
                    symbol=trade.symbol,
                    side=str(getattr(trade.side, "value", trade.side) or "").lower(),
                    position_side=str(trade.position_side or "long").lower(),
                    trade_action=trade.trade_action,
                    fill_price=float(trade.price or 0.0),
                    fill_quantity=float(trade.quantity or 0.0),
                    gross_amount=float(trade.trade_value or 0.0),
                    commission=float(trade.commission or 0.0),
                    stamp_duty=float(trade.stamp_duty or 0.0),
                    transfer_fee=float(trade.transfer_fee or 0.0),
                    borrow_fee=0.0,
                    executed_at=trade.executed_at,
                    price_source=trade.price_source,
                )
            )
            created_fills += 1

        if created_orders or created_fills:
            await self.db.flush()
        return SimulationHistoryBackfillResult(
            created_orders=created_orders,
            created_fills=created_fills,
        )

    async def _reset_new_ledger(
        self,
        *,
        account_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        await self.db.execute(
            delete(SimulationCashLedger).where(
                SimulationCashLedger.account_id == account_id,
            )
        )
        await self.db.execute(
            delete(SimulationPositionLot).where(
                SimulationPositionLot.account_id == account_id,
            )
        )
        await self.db.execute(
            delete(SimulationAccountDaily).where(
                SimulationAccountDaily.tenant_id == tenant_id,
                SimulationAccountDaily.user_id == user_id,
            )
        )
        await self.db.execute(
            delete(SimulationPositionDaily).where(
                SimulationPositionDaily.tenant_id == tenant_id,
                SimulationPositionDaily.user_id == user_id,
            )
        )
        await self.db.execute(
            delete(SimulationAccount).where(
                SimulationAccount.account_id == account_id,
            )
        )
