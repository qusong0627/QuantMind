"""
Persist daily account/position snapshots from simulation projections.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.account_daily import SimulationAccountDaily
from backend.services.simulation.models.position_daily import (
    SimulationPositionDaily,
)


class SimulationDailySnapshotService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def replace_daily_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        account_id: str,
        snapshot_date: date,
        account_payload: dict[str, Any],
        positions: dict[str, dict[str, float]],
    ) -> None:
        snapshot_at = datetime.combine(snapshot_date, datetime.min.time())
        await self.db.execute(
            delete(SimulationPositionDaily).where(
                SimulationPositionDaily.tenant_id == tenant_id,
                SimulationPositionDaily.user_id == user_id,
                SimulationPositionDaily.snapshot_date == snapshot_date,
            )
        )
        await self.db.execute(
            delete(SimulationAccountDaily).where(
                SimulationAccountDaily.tenant_id == tenant_id,
                SimulationAccountDaily.user_id == user_id,
                SimulationAccountDaily.snapshot_date == snapshot_date,
            )
        )

        total_asset = float(account_payload.get("total_asset") or 0.0)
        initial_equity = float(
            account_payload.get("initial_equity")
            or account_payload.get("initial_capital")
            or (
                (account_payload.get("baseline") or {}).get("initial_equity")
                if isinstance(account_payload.get("baseline"), dict)
                else 0.0
            )
            or 0.0
        )
        total_pnl = float(
            account_payload.get("total_pnl") or (total_asset - initial_equity)
        )
        daily_pnl = float(
            account_payload.get("today_pnl") or account_payload.get("daily_pnl") or 0.0
        )
        cash = float(account_payload.get("cash") or 0.0)
        available_cash = float(account_payload.get("available_cash") or cash)
        short_market_value = float(account_payload.get("short_market_value") or 0.0)
        market_value = float(account_payload.get("market_value") or 0.0)

        self.db.add(
            SimulationAccountDaily(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=user_id,
                snapshot_date=snapshot_date,
                snapshot_at=snapshot_at,
                cash=cash,
                available_cash=available_cash,
                frozen_cash=max(0.0, cash - available_cash),
                long_market_value=max(0.0, market_value),
                short_market_value=max(0.0, short_market_value),
                total_asset=total_asset,
                liabilities=float(account_payload.get("liabilities") or 0.0),
                equity=float(account_payload.get("equity") or total_asset),
                daily_pnl=daily_pnl,
                total_pnl=total_pnl,
            )
        )

        for key, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            symbol = str(pos.get("symbol") or key or "").strip().upper()
            side = str(pos.get("side") or "long").strip().lower()
            qty = float(pos.get("volume") or 0.0)
            if not symbol or qty <= 0:
                continue
            price = float(pos.get("last_price") or pos.get("price") or 0.0)
            cost_price = float(
                pos.get("cost_price")
                or pos.get("avg_cost")
                or pos.get("avg_price")
                or pos.get("cost")
                or 0.0
            )
            market_value_item = float(pos.get("market_value") or (qty * price))
            unrealized = (
                (
                    (cost_price - price) * qty
                    if side == "short"
                    else (price - cost_price) * qty
                )
                if cost_price > 0 and price > 0
                else 0.0
            )
            self.db.add(
                SimulationPositionDaily(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    snapshot_date=snapshot_date,
                    snapshot_at=snapshot_at,
                    symbol=symbol,
                    position_side=side,
                    quantity=qty,
                    available_quantity=float(
                        pos.get("available_volume")
                        if pos.get("available_volume") is not None
                        else qty
                    ),
                    frozen_quantity=float(
                        pos.get("frozen_volume")
                        if pos.get("frozen_volume") is not None
                        else max(
                            0.0,
                            float(
                                pos.get("volume")
                                if pos.get("volume") is not None
                                else qty
                            )
                            - float(
                                pos.get("available_volume")
                                if pos.get("available_volume") is not None
                                else qty
                            ),
                        )
                    ),
                    cost_price=cost_price,
                    close_price=price,
                    market_value=market_value_item,
                    unrealized_pnl=unrealized,
                )
            )
