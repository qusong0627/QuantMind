"""
Simulation margin monitor: warn and force-liquidate under-collateralized accounts.

Thresholds (configurable via env):
  - SIM_MARGIN_WARNING_RATIO (default 1.3): write MARGIN_WARNING cash ledger entry
  - SIM_MARGIN_LIQUIDATION_RATIO (default 1.1): FIFO close short positions until ratio recovers
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from sqlalchemy import select

from backend.services.trade_shared.redis_client import redis_client
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.simulation_account_keys import account_key
from backend.shared.trade_account_cache import (
    write_json_cache,
    write_trade_account_cache,
)

logger = logging.getLogger(__name__)

_WARNING_RATIO = float(os.getenv("SIM_MARGIN_WARNING_RATIO", "1.3") or "1.3")
_LIQUIDATION_RATIO = float(os.getenv("SIM_MARGIN_LIQUIDATION_RATIO", "1.1") or "1.1")
_INTERVAL_SEC = int(os.getenv("SIM_MARGIN_MONITOR_INTERVAL_SECONDS", "300") or "300")


async def _scan_and_monitor() -> tuple[int, int]:
    """Scan all margin accounts. Returns (warnings, liquidations)."""
    warnings = 0
    liquidations = 0

    from backend.services.simulation.services.simulation_manager import (
        SimulationAccountManager,
    )

    async with get_session(read_only=False) as session:
        accounts = list(
            (
                await session.execute(
                    select(SimulationAccount).where(
                        SimulationAccount.status == "active",
                        SimulationAccount.liabilities > 0,
                    )
                )
            )
            .scalars()
            .all()
        )

        manager = SimulationAccountManager(redis_client)

        from backend.services.simulation.services.order_submission_service import (
            SimulationOrderSubmissionService,
        )

        submission_service = SimulationOrderSubmissionService(session, manager)

        for account in accounts:
            try:
                equity = float(account.equity or account.total_asset or 0.0)
                liabilities = float(account.liabilities or 0.0)
                if liabilities <= 0:
                    continue
                ratio = equity / liabilities

                if ratio < _LIQUIDATION_RATIO:
                    count = await _force_liquidate(
                        session=session,
                        account=account,
                        submission_service=submission_service,
                        target_ratio=_WARNING_RATIO,
                    )
                    liquidations += count
                elif ratio < _WARNING_RATIO:
                    await _write_warning(session=session, account=account, ratio=ratio)
                    warnings += 1
            except Exception as exc:
                logger.error(
                    "Margin monitor failed for account=%s: %s",
                    account.account_id,
                    exc,
                    exc_info=True,
                )

    return warnings, liquidations


async def _write_warning(
    *,
    session,
    account: SimulationAccount,
    ratio: float,
) -> None:
    now = datetime.utcnow()
    existing = await session.execute(
        select(SimulationCashLedger)
        .where(
            SimulationCashLedger.account_id == account.account_id,
            SimulationCashLedger.event_type == "MARGIN_WARNING",
        )
        .order_by(SimulationCashLedger.occurred_at.desc())
        .limit(1)
    )
    last_warning = existing.scalar_one_or_none()
    if last_warning is not None:
        last_dt = getattr(last_warning, "occurred_at", None)
        if last_dt is not None and (now - last_dt).total_seconds() < 86400:
            return

    session.add(
        SimulationCashLedger(
            account_id=account.account_id,
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            event_type="MARGIN_WARNING",
            ref_type="margin_monitor",
            ref_id=now.strftime("%Y-%m-%d"),
            amount=0.0,
            balance_after=float(account.cash or 0.0),
            trade_date=now,
            occurred_at=now,
            note=f"maintenance_margin_ratio={ratio:.4f} below warning threshold {_WARNING_RATIO}",
        )
    )
    await session.commit()
    logger.warning(
        "Margin warning: account=%s ratio=%.4f < %.4f",
        account.account_id,
        ratio,
        _WARNING_RATIO,
    )


async def _force_liquidate(
    *,
    session,
    account: SimulationAccount,
    submission_service,
    target_ratio: float,
) -> int:
    """Close short positions FIFO until ratio >= target_ratio or all shorts exhausted."""
    account_id = account.account_id
    now = datetime.utcnow()
    liquidated = 0

    lots = list(
        (
            await session.execute(
                select(SimulationPositionLot)
                .where(
                    SimulationPositionLot.account_id == account_id,
                    SimulationPositionLot.position_side == "short",
                    SimulationPositionLot.status == "open",
                    SimulationPositionLot.quantity_remaining > 0,
                )
                .order_by(
                    SimulationPositionLot.open_date.asc().nullsfirst(),
                    SimulationPositionLot.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    for lot in lots:
        equity = float(account.equity or account.total_asset or 0.0)
        liabilities = float(account.liabilities or 0.0)
        if liabilities <= 0 or equity / liabilities >= target_ratio:
            break

        qty = float(lot.quantity_remaining or 0.0)
        if qty <= 0:
            continue

        board_lot_qty = int(qty // 100) * 100
        if board_lot_qty <= 0:
            # 不足一手按实际数量平，不超卖（原先或100股必触发INSUFFICIENT）
            board_lot_qty = int(qty)
        board_lot_qty = min(int(board_lot_qty), int(qty))

        # P0-4：先成交、后记账。成交失败不留幽灵流水，避免审计与余额对不上。
        outcome = await submission_service.submit_and_fill(
            tenant_id=account.tenant_id,
            user_id=int(account.user_id),
            symbol=lot.symbol,
            side="buy",
            quantity=float(board_lot_qty),
            order_type="market",
            trade_action="buy_to_close",
            position_side="short",
            is_margin_trade=True,
            trigger_source="forced_liquidation",
            remarks=f"Forced liquidation: margin ratio below {_LIQUIDATION_RATIO}",
        )

        if outcome.success:
            session.add(
                SimulationCashLedger(
                    account_id=account.account_id,
                    tenant_id=account.tenant_id,
                    user_id=account.user_id,
                    event_type="FORCED_LIQUIDATION",
                    ref_type="margin_monitor",
                    ref_id=f"{lot.id}:{outcome.order_id or ''}",
                    amount=0.0,
                    balance_after=float(account.cash or 0.0),
                    trade_date=now,
                    occurred_at=now,
                    note=f"forced liquidation: closing {board_lot_qty} shares of {lot.symbol} short position",
                )
            )
            await session.commit()
            liquidated += 1
            await session.refresh(account)
            logger.warning(
                "Forced liquidation executed: account=%s symbol=%s qty=%d outcome=%s",
                account.account_id,
                lot.symbol,
                board_lot_qty,
                outcome.message,
            )
        else:
            logger.error(
                "Forced liquidation failed: account=%s symbol=%s qty=%d reason=%s",
                account.account_id,
                lot.symbol,
                board_lot_qty,
                outcome.message,
            )
            break

    if liquidated > 0:
        await _rebuild_redis_cache(account)

    return liquidated


async def _rebuild_redis_cache(account: SimulationAccount) -> None:
    if not redis_client.client:
        return
    async with get_session(read_only=True) as session:
        projection = await SimulationProjectionService(session).load_projection(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            latest_price_loader=lambda symbol: _load_price(session, symbol),
        )
        if projection.account is None:
            return
        payload = SimulationProjectionService.build_cache_payload(
            account=projection.account,
            positions=projection.positions or {},
            source="margin_monitor_projection",
        )
        sim_key = account_key(account.tenant_id, account.user_id)
        write_json_cache(redis_client, sim_key, payload)
        write_trade_account_cache(
            redis_client, account.tenant_id, account.user_id, payload
        )


async def _load_price(session, symbol: str) -> float:
    from sqlalchemy import text
    from backend.shared.stock_utils import StockCodeUtil

    prefix = StockCodeUtil.to_prefix(symbol)
    suffix = StockCodeUtil.to_suffix(prefix)
    query = text(
        "SELECT close, adj_factor FROM stock_daily_latest "
        "WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 1"
    )
    for candidate in (prefix, suffix):
        result = await session.execute(query, {"symbol": candidate})
        row = result.fetchone()
        if not row:
            continue
        close_price = float(row[0] or 0.0)
        if close_price <= 0:
            continue
        return close_price
    return 0.0


async def run_simulation_margin_monitor_worker() -> None:
    logger.info(
        "Simulation margin monitor started: warning_ratio=%.2f liquidation_ratio=%.2f interval=%ds",
        _WARNING_RATIO,
        _LIQUIDATION_RATIO,
        _INTERVAL_SEC,
    )
    while True:
        try:
            warnings, liquidations = await _scan_and_monitor()
            if warnings or liquidations:
                logger.info(
                    "Margin monitor cycle: %d warnings, %d liquidations",
                    warnings,
                    liquidations,
                )
        except asyncio.CancelledError:
            logger.info("Simulation margin monitor cancelled")
            raise
        except Exception as exc:
            logger.error("Margin monitor error: %s", exc, exc_info=True)
        await asyncio.sleep(_INTERVAL_SEC)
