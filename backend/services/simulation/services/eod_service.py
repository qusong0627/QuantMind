"""
Simulation end-of-day settlement service.

Runs after overnight daily-data refresh (default 06:05 Asia/Shanghai) and performs:
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
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
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
from backend.shared.simulation_margin_math import maintenance_margin_ratio
from backend.shared.trade_account_cache import (
    read_json_cache,
    write_json_cache,
    write_trade_account_cache,
)
from backend.shared.trading_calendar import calendar_service

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Shanghai")


def _parse_hhmm(raw: str | None, default: str) -> time:
    """解析 HH:MM 配置，非法值回退默认值，避免环境变量把 worker 打挂。"""
    parts = (raw or default).split(":")
    try:
        return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError, IndexError):
        return time(*map(int, default.split(":")))


_TRIGGER_TIME = _parse_hhmm(
    os.getenv("SIM_EOD_TRIGGER_TIME", "06:05") or "06:05", "06:05"
)
# 收盘时刻（上海）：过了这个点，账户现值已经是“当日日终”，不能再拿去回填 T-1。
_SESSION_CLOSE = _parse_hhmm(os.getenv("SIM_EOD_SESSION_CLOSE"), "15:00")
_INTERVAL_SEC = 60


def _resolve_target_trade_date(now: datetime | None = None) -> date:
    """返回「已收盘、待结算」的目标自然日（是否交易日由 _should_run_eod 兜底）。

    旧实现只判断触发时刻是否早于中午，06:05 触发时**任何时刻**都返回 T-1。
    容器在交易日盘中/盘后重启会重跑 EOD，用「当前权益」（已包含当日成交）
    去回填 T-1 的日账与资金快照，把当日盈亏错记到昨天（收益被打回 T-1）。

    现在按收盘时刻切分：
    - 收盘前：当日交易未结束，最近一个完整交易日是昨天 → 结算 T-1；
    - 收盘后：当日交易已结束，账户现值即当日日终 → 结算当日，
      绝不再用当日权益回填 T-1。
    下午/晚上触发（trigger.hour >= 12）沿用旧语义直接结算当日。
    """
    current = now or datetime.now(_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_TZ)
    else:
        current = current.astimezone(_TZ)

    if _TRIGGER_TIME.hour >= 12:
        return current.date()
    if current.time() < _SESSION_CLOSE:
        return current.date() - timedelta(days=1)
    return current.date()


async def _accounts_with_trades_after(session, trade_date: date) -> set[tuple[str, str]] | None:
    """查「在 trade_date（上海日历日）之后已有成交」的账户。

    EOD 用「当前账户现值」写历史日快照，这个前提只有在该日之后再无成交时
    才成立。一旦已有更晚的成交，现值已不是该日的日终状态，必须拒绝回填，
    否则当日盈亏会被错记到历史日。返回 None 表示查询失败（调用方应跳过本轮）。
    """
    from backend.services.simulation.models.trade import SimTrade

    next_day_utc = datetime.combine(
        trade_date + timedelta(days=1), time.min, tzinfo=_TZ
    ).astimezone(timezone.utc)
    try:
        rows = (
            await session.execute(
                select(SimTrade.tenant_id, SimTrade.user_id)
                .where(SimTrade.executed_at >= next_day_utc)
                .distinct()
            )
        ).all()
        return {(str(row.tenant_id), str(row.user_id)) for row in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EOD trade-timeline guard failed date=%s: %s", trade_date, exc
        )
        return None


async def _should_run_eod(trade_date: date) -> bool:
    try:
        # 使用通配 user_id=* 读全局交易日历，避免 user_id=0 鉴权/作用域落空
        return await calendar_service.is_trading_day(
            market="SSE",
            trade_date=trade_date,
            tenant_id="default",
            user_id="*",
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
                timeout_sec = max(
                    60,
                    int(os.getenv("SIM_EOD_CYCLE_TIMEOUT_SECONDS", "600")),
                )
                try:
                    result = await asyncio.wait_for(
                        _execute_eod(target_trade_date),
                        timeout=timeout_sec,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Simulation EOD timed out after %ss for %s",
                        timeout_sec,
                        target_trade_date,
                    )
                    result = False
                if result:
                    last_run_date = target_trade_date
                    logger.info("Simulation EOD completed for %s", target_trade_date)
                else:
                    logger.warning(
                        "Simulation EOD did not complete for %s; will retry",
                        target_trade_date,
                    )
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


async def _eod_position_snapshot_exists(
    session, tenant_id: str, user_id: str, trade_date: date
) -> bool:
    """该日 EOD 持仓快照是否已存在（= 日终已结算过）。

    仅 EOD 的 replace_daily_snapshot 会写 simulation_position_daily，盘中
    权益 worker 只维护账户级汇总，故它可作为「EOD 是否已跑过该日」的判据。
    """
    try:
        from backend.services.simulation.models.position_daily import (
            SimulationPositionDaily,
        )

        count = (
            await session.execute(
                select(func.count())
                .select_from(SimulationPositionDaily)
                .where(
                    SimulationPositionDaily.tenant_id == tenant_id,
                    SimulationPositionDaily.user_id == str(user_id),
                    SimulationPositionDaily.snapshot_date == trade_date,
                )
            )
        ).scalar_one_or_none()
        return bool(count)
    except Exception as exc:  # noqa: BLE001
        logger.debug("EOD position snapshot existence check failed: %s", exc)
        return False


async def _write_eod_fund_snapshot(
    session,
    *,
    tenant_id: str,
    user_id: str,
    trade_date: date,
    cash: float,
    long_mv: float,
    short_mv: float,
    total_asset: float,
    initial_equity: float,
) -> None:
    """用 EOD 同一份重估写资金快照（与 replace_daily_snapshot 同一事务）。

    口径与周期采集对齐：total/total_pnl/today_pnl 的计算方式一致，
    只是估值取自 EOD 收盘重估而非 Redis 现值。market_value 取净市值
    （long-short），与 EOD 重建后 Redis 的 projection 口径一致。
    """
    try:
        initial = float(initial_equity or 0.0)
        total = round(float(total_asset or 0.0), 4)
        if initial <= 0:
            initial = total
        try:
            baselines = await SimulationFundSnapshotService.get_baselines(
                tenant_id, str(user_id), Decimal(str(initial)), as_of=trade_date
            )
            day_open = float(baselines.get("day_open_equity") or initial)
        except Exception:
            day_open = initial
        await SimulationFundSnapshotService.upsert_eod_snapshot(
            session,
            tenant_id=tenant_id,
            user_id=str(user_id),
            snapshot_date=trade_date,
            total_asset=total,
            available_balance=round(float(cash or 0.0), 4),
            frozen_balance=0.0,
            market_value=round(float(long_mv or 0.0) - float(short_mv or 0.0), 4),
            initial_capital=round(initial, 4),
            total_pnl=round(total - initial, 4),
            today_pnl=round(total - day_open, 4),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EOD fund snapshot write failed tenant=%s user=%s date=%s: %s",
            tenant_id,
            user_id,
            trade_date,
            exc,
        )


async def _repair_eod_fund_snapshot(
    session, *, tenant_id: str, user_id: str, trade_date: date
) -> None:
    """EOD 重跑修复：持仓日快照已存在说明该日已结算，禁止用当前持仓回写历史。

     仅当资金快照缺失、非 EOD 来源、或与日账估值不一致时，按 account_daily
    （含历史日终估值）把同一份估值配对补齐；一致时直接返回。
    """
    try:
        from backend.services.simulation.models.account_daily import (
            SimulationAccountDaily,
        )

        daily = (
            (
                await session.execute(
                    select(SimulationAccountDaily).where(
                        SimulationAccountDaily.tenant_id == tenant_id,
                        SimulationAccountDaily.user_id == str(user_id),
                        SimulationAccountDaily.snapshot_date == trade_date,
                    )
                )
            )
            .scalars()
            .first()
        )
        if daily is None:
            return
        fund = (
            (
                await session.execute(
                    select(SimulationFundSnapshot).where(
                        SimulationFundSnapshot.tenant_id == tenant_id,
                        SimulationFundSnapshot.user_id == str(user_id),
                        SimulationFundSnapshot.snapshot_date == trade_date,
                    )
                )
            )
            .scalars()
            .first()
        )
        if (
            fund is not None
            and str(getattr(fund, "source", "") or "")
            == SimulationFundSnapshotService.EOD_SOURCE
            and abs(float(fund.total_asset or 0.0) - float(daily.total_asset or 0.0))
            < 1e-9
        ):
            return
        total = round(float(daily.total_asset or 0.0), 4)
        initial = 0.0
        if fund is not None and float(fund.initial_capital or 0.0) > 0:
            initial = float(fund.initial_capital)
        else:
            initial = round(total - float(daily.total_pnl or 0.0), 4)
        if initial <= 0:
            initial = total
        try:
            baselines = await SimulationFundSnapshotService.get_baselines(
                tenant_id, str(user_id), Decimal(str(initial)), as_of=trade_date
            )
            day_open = float(baselines.get("day_open_equity") or initial)
        except Exception:
            day_open = initial
        await SimulationFundSnapshotService.upsert_eod_snapshot(
            session,
            tenant_id=tenant_id,
            user_id=str(user_id),
            snapshot_date=trade_date,
            total_asset=total,
            available_balance=round(float(daily.available_cash or 0.0), 4),
            frozen_balance=round(float(daily.frozen_cash or 0.0), 4),
            market_value=round(
                float(daily.long_market_value or 0.0)
                - float(daily.short_market_value or 0.0),
                4,
            ),
            initial_capital=round(initial, 4),
            total_pnl=round(total - initial, 4),
            today_pnl=round(total - day_open, 4),
        )
        logger.info(
            "EOD fund snapshot repaired from account_daily tenant=%s user=%s date=%s total=%s",
            tenant_id,
            user_id,
            trade_date,
            total,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EOD fund snapshot repair failed tenant=%s user=%s date=%s: %s",
            tenant_id,
            user_id,
            trade_date,
            exc,
        )


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

            # 硬护栏：该日之后已有成交的账户，禁止用当前权益回填历史日快照。
            # 容器盘中/盘后重启、EOD 重跑、手工补跑都走这道校验。
            stale_accounts = await _accounts_with_trades_after(session, trade_date)
            if stale_accounts is None:
                logger.warning(
                    "Simulation EOD skipped for %s: trade timeline unavailable",
                    trade_date,
                )
                return False

            projection_svc = SimulationProjectionService(session)
            snapshot_svc = SimulationDailySnapshotService(session)

            # 硬护栏：目标交易日的收盘价必须已经落盘。EOD 在 D 当天收盘后触发
            # 时（容器重启/worker 轮询），D 的日线要等次日凌晨同步，此时结算
            # 只能拿到 D-1 价格——历史 BUG 正是这样把 T-1 估值写成 T 日终，使
            # 整条日收益曲线错后一天。宁可延后重试，也不用错价结算。
            if not await _daily_bar_available(session, trade_date):
                logger.info(
                    "Simulation EOD deferred for %s: daily bars not available yet",
                    trade_date,
                )
                return False

            # 预热本地日线缓存：取价优先 local_market_data（权威日线源），
            # 单次全市场加载后逐股命中缓存，避免并发重复读分区。
            await asyncio.to_thread(_prime_local_close_cache_sync, trade_date)

            # P0-2：逐户独立提交。单户异常只回滚该户，不污染后续户事务；
            # P0-6：total计入Redis侧short_proceeds，与盘中equity口径对齐。
            failed_accounts = 0
            for account in accounts:
                if (str(account.tenant_id), str(account.user_id)) in stale_accounts:
                    logger.warning(
                        "Simulation EOD skip %s for %s: trades after the settlement "
                        "date exist, refusing to backfill current equity into a "
                        "historical snapshot",
                        account.account_id,
                        trade_date,
                    )
                    continue
                try:
                    projection = await projection_svc.load_projection(
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        latest_price_loader=lambda symbol: _load_close_price(
                            session, symbol, trade_date
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
                    short_proceeds = await asyncio.to_thread(
                        _read_short_proceeds,
                        account.tenant_id,
                        account.user_id,
                    )
                    total_asset = round(cash + short_proceeds + long_mv - short_mv, 4)

                    projection_account.long_market_value = round(long_mv, 4)
                    projection_account.short_market_value = round(short_mv, 4)
                    projection_account.total_asset = total_asset
                    projection_account.equity = total_asset
                    projection_account.maintenance_margin_ratio = round(
                        maintenance_margin_ratio(
                            total_asset, short_mv, liabilities, cash
                        ),
                        6,
                    )
                    projection_account.last_projected_at = datetime.utcnow()

                    account_payload = SimulationProjectionService.build_cache_payload(
                        account=projection_account,
                        positions=positions,
                        source="eod_remarking",
                        short_proceeds=short_proceeds,
                    )

                    # slot 锁 + 锁后重查：EOD/EOD 重跑与周期采集共用同一把锁，
                    # 逐账户判定完成态，不再按“该日有任意资金快照”全局跳过
                    # （周期采集的普通快照不得被误判为 EOD 完成）。
                    await SimulationFundSnapshotService.lock_slot(
                        session, account.tenant_id, account.user_id, trade_date
                    )
                    if await SimulationFundSnapshotService.is_eod_completed(
                        session, account.tenant_id, account.user_id, trade_date
                    ):
                        logger.info(
                            "EOD daily snapshot for %s already exists (%s), "
                            "skipping overwrite (rerun)",
                            trade_date,
                            account.account_id,
                        )
                        # 重跑修复：历史日终估值保留在 account_daily；若资金快照
                        # 缺失或仍是周期旧值，按日账把同一份估值配对补齐。
                        await _repair_eod_fund_snapshot(
                            session,
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            trade_date=trade_date,
                        )
                    else:
                        await snapshot_svc.replace_daily_snapshot(
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            account_id=account.account_id,
                            snapshot_date=trade_date,
                            account_payload=account_payload,
                            positions=positions,
                        )
                        # 同一事务内用同一份重估配对写资金快照：两者提交后，
                        # is_eod_completed 才为真，EOD 完成态才算成立。
                        await _write_eod_fund_snapshot(
                            session,
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            trade_date=trade_date,
                            cash=cash,
                            long_mv=long_mv,
                            short_mv=short_mv,
                            total_asset=total_asset,
                            initial_equity=float(
                                getattr(projection_account, "initial_equity", 0.0)
                                or 0.0
                            ),
                        )

                    await asyncio.to_thread(
                        _rebuild_redis,
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
        short_proceeds=float(
            (read_json_cache(redis_client, sim_key) or {}).get("short_proceeds") or 0.0
        ),
    )
    # 保留 PG 不可考的 Redis 独有字段（warning_level / market / t1_settlement_date）
    payload = SimulationProjectionService.merge_preserved(
        read_json_cache(redis_client, sim_key), payload
    )
    write_json_cache(redis_client, sim_key, payload)
    write_trade_account_cache(redis_client, tenant_id, user_id, payload)


def _local_close_price_sync(
    symbol: str, trade_date: date | None = None
) -> float:
    """本地 parquet 日线收盘价（同步磁盘 IO，调用方须放线程）。

    与权益结算 worker 的兜底口径一致：local_market_data 是模拟盘权威日线源，
    随数据同步落盘；stock_daily_latest 只在同步开启后刷新，未配置时会长期停在
    旧日期，导致日终权益被打回 T-1 甚至更早。

    `trade_date` 必须由结算方显式传入目标交易日：不传才回退「最新可得日」。
    历史 BUG：EOD 在 D 当天 15:00 触发时，D 的日线要等次日凌晨同步才落盘，
    这里取「最新可得日」拿到的是 D-1 收盘价，却写进 D 的日终快照，导致整条
    日收益曲线整体错后一天（今日盈亏里混入前一日涨跌）。
    """
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )

        lmd = get_local_market_data("CN")
        target = trade_date or lmd.latest_trade_date()
        if target is None:
            return 0.0
        bar = lmd.get_bar(symbol, target)
        if bar is None:
            return 0.0
        close = float(bar.close or 0.0)
        return close if close > 0 else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("Local daily close load failed %s: %s", symbol, exc)
        return 0.0


def _prime_local_close_cache_sync(trade_date: date | None = None) -> None:
    """预热本地日线缓存，避免并发取价时重复读取同一分区。"""
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )

        lmd = get_local_market_data("CN")
        target = trade_date or lmd.latest_trade_date()
        if target is not None:
            lmd.load_date(target)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Prime local daily cache failed: %s", exc)


def _local_latest_trade_date_sync() -> date | None:
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )

        return get_local_market_data("CN").latest_trade_date()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Local latest trade date load failed: %s", exc)
        return None


async def _daily_bar_available(session, trade_date: date) -> bool:
    """目标交易日日线是否已落盘（本地 parquet 优先，PG 兜底）。

    EOD 只在目标日收盘价可得时才允许结算；否则一律延后重试，绝不用 T-1
    价格冒充 T 日终。日线同步默认次日 03:00，06:05 的 EOD 正常能命中。
    """
    local_latest = await asyncio.to_thread(_local_latest_trade_date_sync)
    if local_latest is not None and local_latest >= trade_date:
        return True
    try:
        from sqlalchemy import text

        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM stock_daily_latest "
                    "WHERE trade_date = :trade_date LIMIT 1"
                ),
                {"trade_date": trade_date},
            )
        ).fetchone()
        return row is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("EOD daily bar availability check failed: %s", exc)
        return False


async def _load_close_price(
    session, symbol: str, trade_date: date | None = None
) -> float:
    """收盘价：本地 parquet 日线优先，PG stock_daily_latest 兜底。

    传 `trade_date` 时严格取该交易日收盘价（EOD 结算口径）；不传则取最新
    可得日（Redis 缺键时按现值重建账户的口径）。本地 parquet 缺失（同步未
    落盘/新标的）时才回退到 PG 快照表，且同样按 trade_date 过滤。
    """
    if trade_date is None:
        local_price = await asyncio.to_thread(_local_close_price_sync, symbol)
    else:
        local_price = await asyncio.to_thread(
            _local_close_price_sync, symbol, trade_date
        )
    if local_price > 0:
        return local_price

    from sqlalchemy import text
    from backend.shared.stock_utils import StockCodeUtil

    suffix = StockCodeUtil.normalize(symbol)
    prefix = StockCodeUtil.to_prefix(suffix)
    if trade_date is not None:
        query = text(
            "SELECT close FROM stock_daily_latest "
            "WHERE symbol = :symbol AND trade_date = :trade_date LIMIT 1"
        )
        params: dict[str, object] = {"trade_date": trade_date}
    else:
        # 兜底取最新一行：stock_daily_latest 未开启同步时会停在旧日期，
        # 故仅用于「重建现值」而非日终结算。
        query = text(
            "SELECT close, adj_factor FROM stock_daily_latest "
            "WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 1"
        )
        params = {}
    for candidate in (suffix, prefix):
        result = await session.execute(query, {"symbol": candidate, **params})
        row = result.fetchone()
        if not row:
            continue
        close_price = float(row[0] or 0.0)
        if close_price <= 0:
            continue
        return close_price
    return 0.0
