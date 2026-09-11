"""
Simulation end-of-day settlement service.

Runs after overnight daily-data refresh (default 03:05 Asia/Shanghai) and performs:
1. Re-mark all accounts to latest close prices via projection service
2. Write daily account/position snapshots
3. Capture fund snapshots
4. Warn about stuck pending orders
5. Rebuild Redis cache from ledger projection
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, func

from backend.services.trade_shared.redis_client import redis_client
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.fund_snapshot import (
    SimulationFundSnapshot,
)
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.models.order import OrderStatus
from backend.services.simulation.services.daily_snapshot_service import (
    SimulationDailySnapshotService,
)
from backend.services.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService,
)
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.trade_account_cache import (
    write_json_cache,
    write_trade_account_cache,
)
from backend.shared.trading_calendar import calendar_service

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Shanghai")
_TRIGGER_TIME = time(
    *map(int, (os.getenv("SIM_EOD_TRIGGER_TIME", "03:05") or "03:05").split(":"))
)
_INTERVAL_SEC = 60


def _resolve_target_trade_date(now: datetime | None = None) -> date:
    current = now or datetime.now(_TZ)
    if _TRIGGER_TIME.hour < 12:
        return current.date() - timedelta(days=1)
    return current.date()


async def _should_run_eod(trade_date: date) -> bool:
    try:
        return await calendar_service.is_trading_day(
            market="SSE",
            trade_date=trade_date,
            tenant_id="default",
            user_id="0",
        )
    except Exception:
        return trade_date.weekday() < 5


def _past_trigger_window(now: datetime | None = None) -> bool:
    current = now or datetime.now(_TZ)
    trigger_dt = datetime.combine(current.date(), _TRIGGER_TIME, tzinfo=_TZ)
    return current >= trigger_dt


async def run_simulation_eod_worker() -> None:
    logger.info(
        "Simulation EOD worker started: trigger_time=%s",
        _TRIGGER_TIME.strftime("%H:%M"),
    )
    last_run_date: date | None = None

    while True:
        try:
            now = datetime.now(_TZ)
            target_trade_date = _resolve_target_trade_date(now)

            if (
                last_run_date != target_trade_date
                and _past_trigger_window(now)
                and await _should_run_eod(target_trade_date)
            ):
                result = await _execute_eod(target_trade_date)
                if result:
                    last_run_date = target_trade_date
                    logger.info("Simulation EOD completed for %s", target_trade_date)
        except asyncio.CancelledError:
            logger.info("Simulation EOD worker cancelled")
            raise
        except Exception as exc:
            logger.error("Simulation EOD error: %s", exc, exc_info=True)

        await asyncio.sleep(_INTERVAL_SEC)


def _read_short_proceeds(tenant_id: str, user_id: str) -> float:
    """Best-effort读取Redis侧融券冻结资金（P0-6）。

    PG SimulationAccount无该列，EOD重算时缺它会导致融券户权益跳变。
    Redis缺键/不可用返回0.0（现金户无影响）。
    """
    try:
        from backend.shared.simulation_account_keys import account_key
        from backend.shared.trade_account_cache import read_json_cache

        data = read_json_cache(redis_client, account_key(tenant_id, user_id, "CN"))
        return float((data or {}).get("short_proceeds") or 0.0)
    except Exception:
        return 0.0


async def _execute_eod(trade_date: date) -> bool:
    """Run the full EOD pipeline. Returns True only when all accounts succeed."""
    try:
        async with get_session(read_only=False) as session:
            accounts = list(
                (
                    await session.execute(
                        select(SimulationAccount).where(
                            SimulationAccount.status == "active"
                        )
                    )
                )
                .scalars()
                .all()
            )

            if not accounts:
                return True

            projection_svc = SimulationProjectionService(session)
            snapshot_svc = SimulationDailySnapshotService(session)

            # P0-2：逐户独立提交。单户异常只回滚该户，不污染后续户事务；
            # P0-6：total计入Redis侧short_proceeds，与盘中equity口径对齐。
            failed_accounts = 0
            for account in accounts:
                try:
                    projection = await projection_svc.load_projection(
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        latest_price_loader=lambda symbol: _load_close_price(
                            session, symbol
                        ),
                    )
                    projection_account = projection.account
                    if projection_account is None:
                        await session.rollback()
                        continue

                    positions = projection.positions or {}

                    long_mv = 0.0
                    short_mv = 0.0
                    for pos in positions.values():
                        if not isinstance(pos, dict):
                            continue
                        mv = float(pos.get("market_value") or 0.0)
                        side = str(pos.get("side") or "long").strip().lower()
                        if side == "short":
                            short_mv += mv
                        else:
                            long_mv += mv

                    cash = float(projection_account.cash or 0.0)
                    liabilities = float(projection_account.liabilities or 0.0)
                    short_proceeds = _read_short_proceeds(
                        account.tenant_id, account.user_id
                    )
                    total_asset = round(cash + short_proceeds + long_mv - short_mv, 4)

                    projection_account.long_market_value = round(long_mv, 4)
                    projection_account.short_market_value = round(short_mv, 4)
                    projection_account.total_asset = total_asset
                    projection_account.equity = total_asset
                    if liabilities > 0:
                        projection_account.maintenance_margin_ratio = round(
                            total_asset / liabilities, 6
                        )
                    projection_account.last_projected_at = datetime.utcnow()

                    account_payload = SimulationProjectionService.build_cache_payload(
                        account=projection_account,
                        positions=positions,
                        source="eod_remarking",
                    )

                    await snapshot_svc.replace_daily_snapshot(
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        account_id=account.account_id,
                        snapshot_date=trade_date,
                        account_payload=account_payload,
                        positions=positions,
                    )

                    _rebuild_redis(
                        account=projection_account,
                        positions=positions,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                    )
                    # 逐户提交：该户成功即落盘，不等待全部账户
                    await session.commit()
                except Exception as exc:
                    failed_accounts += 1
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    logger.error(
                        "EOD remark failed for account=%s: %s",
                        account.account_id,
                        exc,
                        exc_info=True,
                    )

        pending_count = await _check_pending_orders()
        if pending_count > 0:
            logger.warning(
                "EOD: %d simulation order(s) still in pending status", pending_count
            )

        # 容器重启后 EOD 可能重跑昨日的 trade_date。若 fund_snapshots 已有该日数据,
        # 跳过 capture_all 避免公司行为 apply 后的值覆盖原始快照。
        try:
            async with get_session(read_only=True) as check_session:
                existing_count = (
                    await check_session.execute(
                        select(func.count())
                        .select_from(SimulationFundSnapshot)
                        .where(SimulationFundSnapshot.snapshot_date == trade_date)
                    )
                ).scalar_one_or_none()
        except Exception:
            existing_count = 0

        if existing_count and existing_count > 0:
            logger.info(
                "EOD fund snapshot for %s already exists (%d rows), skipping capture",
                trade_date,
                existing_count,
            )
        else:
            try:
                # P0-7：EOD资金快照按trade_date记，与account_daily对齐
                await SimulationFundSnapshotService.capture_all(
                    redis_client, snapshot_date=trade_date
                )
            except Exception as exc:
                logger.warning("EOD fund snapshot capture failed: %s", exc)

        if failed_accounts:
            logger.error(
                "Simulation EOD finished with %d failed account(s)", failed_accounts
            )
            return False
        return True

    except Exception as exc:
        logger.error("EOD pipeline failed: %s", exc, exc_info=True)
        return False


async def _check_pending_orders() -> int:
    try:
        async with get_session(read_only=True) as session:
            result = await session.execute(
                select(func.count())
                .select_from(SimulationOrderV2)
                .where(
                    SimulationOrderV2.status == OrderStatus.PENDING.value,
                )
            )
            return int(result.scalar_one_or_none() or 0)
    except Exception:
        return 0


def _redis_live_position_count(sim_key: str) -> int:
    """读取 Redis 现有账户的持仓数（防空投影覆盖的判断依据）。"""
    try:
        if not redis_client.client:
            return 0
        from backend.shared.trade_account_cache import read_json_cache

        current = read_json_cache(redis_client, sim_key) or {}
        positions = current.get("positions") or {}
        if isinstance(positions, str):
            import json as _json

            try:
                positions = _json.loads(positions)
            except Exception:
                return 0
        if not isinstance(positions, dict):
            return 0
        return sum(
            1
            for pos in positions.values()
            if isinstance(pos, dict) and float(pos.get("volume") or 0) > 0
        )
    except Exception:
        return 0


def _rebuild_redis(
    *,
    account: SimulationAccount,
    positions: dict,
    tenant_id: str,
    user_id: str,
) -> None:
    if not redis_client.client:
        return
    sim_key = f"simulation:account:{tenant_id}:{str(user_id).strip()}"
    # 空投影保护：ledger 为空（live 台账未建/迁移未跑）时 projection.positions 为空，
    # 此时覆盖会把 Redis 里的实盘持仓清零（"回到初始状态"）。跳过并告警。
    if not positions and _redis_live_position_count(sim_key) > 0:
        logger.error(
            "EOD rebuild skipped for %s: ledger projection empty but Redis holds live positions "
            "(live fills not recorded to ledger?)",
            sim_key,
        )
        return
    payload = SimulationProjectionService.build_cache_payload(
        account=account,
        positions=positions,
        source="eod_remarking",
    )
    write_json_cache(redis_client, sim_key, payload)
    write_trade_account_cache(redis_client, tenant_id, user_id, payload)


async def _load_close_price(session, symbol: str) -> float:
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
