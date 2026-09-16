"""
Seed simulation ledger state from an externally confirmed holdings snapshot.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
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
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)


class SimulationSeedService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def reseed_from_holdings_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        initial_equity: float,
        available_cash: float,
        positions: list[dict[str, float | str]],
        clear_legacy_history: bool = True,
        source: str = "ocr_confirmed_snapshot",
    ) -> str:
        # OCR 持仓重灌仅作用于 CN 默认账户（市场化账户：CN 无后缀 id）
        account_id = SimulationProjectionService.build_account_id(tenant_id, user_id, "CN")
        now = datetime.utcnow()
        normalized_user_id = str(user_id)
        try:
            legacy_user_id = int(normalized_user_id)
        except (TypeError, ValueError):
            legacy_user_id = 0
        long_market_value = round(
            sum(float(item.get("quantity") or 0.0) * float(item.get("price") or 0.0) for item in positions),
            2,
        )
        total_asset = round(float(initial_equity or 0.0), 2)
        cash = round(float(available_cash or 0.0), 2)

        await self.db.execute(
            delete(SimulationCashLedger).where(
                SimulationCashLedger.tenant_id == tenant_id,
                SimulationCashLedger.user_id == normalized_user_id,
            )
        )
        await self.db.execute(
            delete(SimulationPositionLot).where(
                SimulationPositionLot.tenant_id == tenant_id,
                SimulationPositionLot.user_id == normalized_user_id,
            )
        )
        await self.db.execute(
            delete(SimulationAccountDaily).where(
                SimulationAccountDaily.tenant_id == tenant_id,
                SimulationAccountDaily.user_id == normalized_user_id,
            )
        )
        await self.db.execute(
            delete(SimulationPositionDaily).where(
                SimulationPositionDaily.tenant_id == tenant_id,
                SimulationPositionDaily.user_id == normalized_user_id,
            )
        )

        if clear_legacy_history:
            if legacy_user_id > 0:
                await self.db.execute(
                    delete(SimTrade).where(
                        SimTrade.tenant_id == tenant_id,
                        SimTrade.user_id == legacy_user_id,
                    )
                )
                await self.db.execute(
                    delete(SimOrder).where(
                        SimOrder.tenant_id == tenant_id,
                        SimOrder.user_id == legacy_user_id,
                    )
                )
            await self.db.execute(
                delete(SimulationFill).where(
                    SimulationFill.tenant_id == tenant_id,
                    SimulationFill.user_id == normalized_user_id,
                )
            )
            await self.db.execute(
                delete(SimulationOrderV2).where(
                    SimulationOrderV2.tenant_id == tenant_id,
                    SimulationOrderV2.user_id == normalized_user_id,
                )
            )

        account = await self.db.get(SimulationAccount, account_id)
        if account is None:
            account = SimulationAccount(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=normalized_user_id,
            )
            self.db.add(account)

        account.account_type = "cash"
        account.status = "active"
        account.initial_equity = total_asset
        account.cash = cash
        account.available_cash = cash
        account.frozen_cash = 0.0
        account.long_market_value = long_market_value
        account.short_market_value = 0.0
        account.total_asset = total_asset
        account.liabilities = 0.0
        account.equity = total_asset
        account.maintenance_margin_ratio = 0.0
        account.last_trade_at = now
        account.last_projected_at = now

        self.db.add(
            SimulationCashLedger(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=normalized_user_id,
                event_type="MANUAL_ADJUSTMENT",
                ref_type="seed_snapshot",
                ref_id=source,
                amount=total_asset,
                balance_after=total_asset,
                trade_date=now,
                occurred_at=now,
                note="seed initial equity from confirmed holdings snapshot",
            )
        )
        self.db.add(
            SimulationCashLedger(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=normalized_user_id,
                event_type="MANUAL_ADJUSTMENT",
                ref_type="seed_snapshot",
                ref_id=source,
                amount=-long_market_value,
                balance_after=cash,
                trade_date=now,
                occurred_at=now,
                note="seed holdings cost basis from confirmed holdings snapshot",
            )
        )

        for item in positions:
            quantity = float(item.get("quantity") or 0.0)
            price = float(item.get("price") or 0.0)
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol or quantity <= 0 or price <= 0:
                continue
            self.db.add(
                SimulationPositionLot(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    user_id=normalized_user_id,
                    market="CN",
                    symbol=symbol,
                    position_side="long",
                    open_fill_id=None,
                    open_date=now,
                    quantity_open=quantity,
                    quantity_remaining=quantity,
                    cost_price=price,
                    cost_amount=round(quantity * price, 2),
                    status="open",
                )
            )

        await self.db.flush()
        return account_id
