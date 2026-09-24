"""
Simulation account projection read service.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.shared.simulation_position_keys import build_position_key

_SH_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class ProjectionSnapshot:
    account: SimulationAccount | None
    positions: dict[str, dict[str, float]]


class SimulationProjectionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def build_account_id(tenant_id: str, user_id: str | int) -> str:
        return f"sim:{str(tenant_id or 'default').strip() or 'default'}:{str(user_id).strip()}"

    @staticmethod
    def merge_preserved(
        live: dict[str, Any] | None,
        rebuilt: dict[str, Any],
    ) -> dict[str, Any]:
        """把 Redis 独有、PG 台账不可考的字段从现值并入重建 payload。

        PG 没有 short_proceeds / warning_level / market / t1_settlement_date 列，
        重建（EOD / 对账 autofix）若整包覆盖会把这些字段清零，导致融券户权益
        跳变、风险等级丢失。保留现值并按 short_proceeds 重算总资产。
        """
        out = dict(rebuilt)
        live = live or {}
        short_proceeds = float(live.get("short_proceeds") or 0.0)
        if short_proceeds:
            out["short_proceeds"] = short_proceeds
            cash = float(out.get("cash") or 0.0)
            long_mv = float(out.get("long_market_value") or 0.0)
            short_mv = float(out.get("short_market_value") or 0.0)
            total_asset = round(cash + short_proceeds + long_mv - short_mv, 2)
            out["market_value"] = round(long_mv - short_mv, 2)
            out["total_asset"] = total_asset
            out["equity"] = total_asset
        for key in ("warning_level", "market", "t1_settlement_date"):
            value = live.get(key)
            if value not in (None, ""):
                out[key] = value
        return out

    @staticmethod
    def build_cache_payload(
        *,
        account: SimulationAccount,
        positions: dict[str, dict[str, float]] | None,
        source: str,
        rebuilt_at: datetime | None = None,
        short_proceeds: float = 0.0,
    ) -> dict[str, Any]:
        snapshot_dt = (
            rebuilt_at
            or getattr(account, "last_projected_at", None)
            or getattr(account, "updated_at", None)
            or getattr(account, "created_at", None)
            or datetime.now()
        )
        if snapshot_dt.tzinfo is not None:
            snapshot_dt = snapshot_dt.astimezone().replace(tzinfo=None)
        long_market_value = float(getattr(account, "long_market_value", 0.0) or 0.0)
        short_market_value = float(
            getattr(account, "short_market_value", 0.0) or 0.0
        )
        if positions:
            # 防御性补齐成本/现价别名：外部拼装的 positions 可能只有 cost_price
            # 或只有 cost，补齐后 Lua/撮合与台账侧读写各自口径都不丢。
            for _pos in positions.values():
                if not isinstance(_pos, dict):
                    continue
                _cost = _pos.get("cost", _pos.get("cost_price", 0.0))
                try:
                    _cost_f = float(_cost or 0.0)
                except (TypeError, ValueError):
                    _cost_f = 0.0
                _pos.setdefault("cost", _cost_f)
                _pos.setdefault("cost_price", _cost_f)
                _pos.setdefault("price", _pos.get("last_price", 0.0))
                _pos.setdefault("market_value", 0.0)
                _pos.setdefault("volume", 0.0)
            long_market_value, short_market_value, market_value = (
                SimulationProjectionService.summarize_position_market_value(positions)
            )
        else:
            market_value = round(long_market_value - short_market_value, 2)
        snapshot_at = snapshot_dt.isoformat()
        account_version = int(snapshot_dt.timestamp() * 1000)
        initial_equity = float(account.initial_equity or 0.0)
        cash = float(account.cash or 0.0)
        short_proceeds = float(short_proceeds or 0.0)
        total_asset = round(cash + short_proceeds + market_value, 2)
        payload = {
            "account_version": account_version,
            "snapshot_at": snapshot_at,
            "cash": cash,
            # 模拟盘无现金冻结机制（订单即时全成，无挂单占用），可用现金恒等于
            # 现金，冻结恒为 0。历史曾把 PG 里失真的 available_cash/frozen_cash
            # 原样回填，导致前端"冻结"显示旧值，故此处按现金派生。
            "available_cash": cash,
            "frozen_cash": 0.0,
            "short_proceeds": short_proceeds,
            "market_value": market_value,
            "long_market_value": long_market_value,
            "short_market_value": short_market_value,
            "total_asset": total_asset,
            "equity": total_asset,
            "liabilities": float(getattr(account, "liabilities", 0.0) or 0.0),
            "maintenance_margin_ratio": float(getattr(account, "maintenance_margin_ratio", 0.0) or 0.0),
            "initial_equity": initial_equity,
            "day_open_equity": initial_equity,
            "month_open_equity": initial_equity,
            "positions": positions or {},
            "baseline": {
                "initial_equity": initial_equity,
                "day_open_equity": initial_equity,
                "month_open_equity": initial_equity,
            },
            "rebuild_source": source,
            "rebuilt_at": snapshot_at,
        }
        return payload

    @staticmethod
    def summarize_position_market_value(
        positions: dict[str, dict[str, float]] | None,
    ) -> tuple[float, float, float]:
        long_market_value = 0.0
        short_market_value = 0.0
        for pos in (positions or {}).values():
            if not isinstance(pos, dict):
                continue
            market_value = float(pos.get("market_value") or 0.0)
            side = str(pos.get("side") or "long").strip().lower()
            if side == "short":
                short_market_value += market_value
            else:
                long_market_value += market_value
        net_market_value = round(long_market_value - short_market_value, 2)
        return (
            round(long_market_value, 2),
            round(short_market_value, 2),
            net_market_value,
        )

    async def load_projection(
        self,
        *,
        tenant_id: str,
        user_id: str | int,
        latest_price_loader,
    ) -> ProjectionSnapshot:
        account_id = self.build_account_id(tenant_id, user_id)
        account = await self.db.get(SimulationAccount, account_id)
        positions = await self._load_positions_from_lots(
            account_id=account_id,
            latest_price_loader=latest_price_loader,
        )
        return ProjectionSnapshot(account=account, positions=positions)

    async def _load_positions_from_lots(
        self,
        *,
        account_id: str,
        latest_price_loader,
    ) -> dict[str, dict[str, float]]:
        stmt = (
            select(
                SimulationPositionLot,
            )
            .where(
                SimulationPositionLot.account_id == account_id,
                SimulationPositionLot.status == "open",
                SimulationPositionLot.quantity_remaining > 0,
            )
            .order_by(
                SimulationPositionLot.symbol.asc(),
                SimulationPositionLot.position_side.asc(),
                SimulationPositionLot.open_date.asc().nullsfirst(),
                SimulationPositionLot.id.asc(),
            )
        )
        lots = list((await self.db.execute(stmt)).scalars().all())
        if not lots:
            return {}

        symbols = sorted(
            {
                str(lot.symbol).strip().upper()
                for lot in lots
                if str(lot.symbol).strip()
            }
        )
        price_pairs = await asyncio.gather(
            *[latest_price_loader(symbol) for symbol in symbols],
            return_exceptions=True,
        )
        price_map: dict[str, float] = {}
        for symbol, value in zip(symbols, price_pairs, strict=True):
            price_map[symbol] = float(value) if isinstance(value, (int, float)) else 0.0

        grouped: dict[tuple[str, str], dict[str, float]] = {}
        # T+1 判定按上海交易日（与 load_available_quantities / 撮合侧一致），
        # 不能用 date.today()（依赖进程本地时区，容器非上海时凌晨会差一天）。
        as_of_date = datetime.now(_SH_TZ).date()
        for lot in lots:
            normalized_symbol = str(lot.symbol or "").strip().upper()
            side = str(lot.position_side or "long").strip().lower()
            qty = float(lot.quantity_remaining or 0.0)
            if not normalized_symbol or qty <= 0:
                continue
            key = (normalized_symbol, side)
            bucket = grouped.setdefault(
                key,
                {
                    "quantity": 0.0,
                    "available_quantity": 0.0,
                    "cost_amount": 0.0,
                },
            )
            bucket["quantity"] += qty
            bucket["cost_amount"] += float(lot.cost_amount or 0.0) * (
                qty / float(lot.quantity_open or qty or 1.0)
            )
            bucket["available_quantity"] += self._lot_available_quantity(
                lot,
                as_of_date=as_of_date,
            )

        positions: dict[str, dict[str, float]] = {}
        for (normalized_symbol, side), bucket in grouped.items():
            qty = float(bucket["quantity"] or 0.0)
            if not normalized_symbol or qty <= 0:
                continue
            price = float(price_map.get(normalized_symbol) or 0.0)
            total_cost = float(bucket["cost_amount"] or 0.0)
            cost_price = total_cost / qty if qty > 0 else 0.0
            market_value = round(price * qty, 2) if price > 0 else 0.0
            key = build_position_key(normalized_symbol, side)
            cost_rounded = round(cost_price, 4) if cost_price > 0 else 0.0
            positions[key] = {
                "symbol": normalized_symbol,
                "volume": qty,
                "available_volume": float(bucket["available_quantity"] or 0.0),
                "frozen_volume": max(
                    0.0,
                    round(qty - float(bucket["available_quantity"] or 0.0), 6),
                ),
                "price": round(price, 4) if price > 0 else 0.0,
                "last_price": round(price, 4) if price > 0 else 0.0,
                "market_value": market_value,
                # 口径统一：live Redis 持仓用 `cost`（Lua/撮合读写），台账侧用
                # `cost_price`。双写别名，任一入口重建 Redis 都不丢成本口径。
                "cost": cost_rounded,
                "cost_price": cost_rounded,
                "side": side,
            }
        return positions

    async def get_available_quantity(
        self,
        *,
        tenant_id: str,
        user_id: str | int,
        symbol: str,
        position_side: str = "long",
        as_of_date: date | None = None,
    ) -> float:
        account_id = self.build_account_id(tenant_id, user_id)
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_side = str(position_side or "long").strip().lower()
        stmt = (
            select(SimulationPositionLot)
            .where(
                SimulationPositionLot.account_id == account_id,
                SimulationPositionLot.symbol == normalized_symbol,
                SimulationPositionLot.position_side == normalized_side,
                SimulationPositionLot.status == "open",
                SimulationPositionLot.quantity_remaining > 0,
            )
        )
        lots = list((await self.db.execute(stmt)).scalars().all())
        if not lots:
            return 0.0
        target_date = as_of_date or date.today()
        return round(
            sum(
                self._lot_available_quantity(lot, as_of_date=target_date)
                for lot in lots
            ),
            6,
        )

    async def load_available_quantities(
        self,
        *,
        tenant_id: str,
        user_id: str | int,
        as_of_date: date | None = None,
    ) -> dict[tuple[str, str], float]:
        """Return ledger-derived sellable quantities grouped by symbol and side."""
        account_id = self.build_account_id(tenant_id, user_id)
        stmt = select(SimulationPositionLot).where(
            SimulationPositionLot.account_id == account_id,
            SimulationPositionLot.status == "open",
            SimulationPositionLot.quantity_remaining > 0,
        )
        lots = list((await self.db.execute(stmt)).scalars().all())
        target_date = as_of_date or datetime.now(_SH_TZ).date()
        quantities: dict[tuple[str, str], float] = {}
        for lot in lots:
            symbol = str(lot.symbol or "").strip().upper()
            side = str(lot.position_side or "long").strip().lower()
            if not symbol:
                continue
            key = (symbol, side)
            quantities[key] = quantities.get(key, 0.0) + self._lot_available_quantity(
                lot,
                as_of_date=target_date,
            )
        return {key: round(value, 6) for key, value in quantities.items()}

    @staticmethod
    def _lot_available_quantity(
        lot: SimulationPositionLot,
        *,
        as_of_date: date,
    ) -> float:
        qty = max(0.0, float(lot.quantity_remaining or 0.0))
        if qty <= 0:
            return 0.0
        side = str(lot.position_side or "long").strip().lower()
        if side != "long":
            return qty
        open_dt = lot.open_date
        if isinstance(open_dt, datetime):
            # Ledger timestamps are stored as naive UTC. T+1 is based on the
            # exchange-local trade date, not UTC's calendar date.
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            open_trade_date = open_dt.astimezone(_SH_TZ).date()
            if open_trade_date >= as_of_date:
                return 0.0
        return qty
