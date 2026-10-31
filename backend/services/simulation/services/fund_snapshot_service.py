"""
Persist simulation account fund overview snapshots into PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.models.fund_snapshot import (
    SimulationFundSnapshot,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.simulation_account_keys import (
    parse_account_key as parse_canonical_account_key,
)

logger = logging.getLogger(__name__)


#: 周期采集写入 simulation_fund_snapshots.source 的取值
PERIODIC_FUND_SOURCE = "redis_simulation_account"
#: EOD 日终写入的取值。周期采集永不覆盖该来源的行：
#: 逐行跳过（持仓日快照/EOD 来源双重判据）+ upsert 的 SQL 层 WHERE 兜底。
EOD_FUND_SOURCE = "eod_settlement"


def _to_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None:
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _local_today() -> datetime.date:
    # Keep simple and deterministic; can be overridden by TZ env var.
    tz_name = os.getenv("SIM_FUND_SNAPSHOT_TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now().date()


def _parse_account_key(key: str) -> tuple[str, str] | None:
    # simulation:account:{tenant_id}:{user_id}（CN），带 :MARKET 后缀的市场账户
    # 与 CN 账户按 (tenant, user) 合并成一条用户级快照：快照表以
    # tenant/user/date 唯一，且台账 account_id（sim:{tenant}:{user}）本来
    # 就不分市场——跨市场资产应合计为用户总资产，而非串行覆盖或跳过。
    parsed = parse_canonical_account_key(key)
    if not parsed:
        return None
    tenant_id, user_id, _market = parsed
    return tenant_id, user_id


@dataclass
class SnapshotUpsertResult:
    upserted_rows: int
    scanned_accounts: int


class SimulationFundSnapshotService:
    #: 周期采集来源标记（= PERIODIC_FUND_SOURCE）
    PERIODIC_SOURCE = PERIODIC_FUND_SOURCE
    #: EOD 日终来源标记（= EOD_FUND_SOURCE）
    EOD_SOURCE = EOD_FUND_SOURCE

    @staticmethod
    def _read_settings_initial_cash(
        redis: RedisClient, tenant_id: str, user_id: str
    ) -> Decimal:
        if not redis.client:
            return Decimal("0")
        settings_key = f"simulation:settings:{tenant_id}:{user_id}"
        raw = redis.client.get(settings_key)
        if not raw:
            return Decimal("0")
        try:
            data = json.loads(raw)
        except Exception:
            return Decimal("0")
        return _to_decimal(data.get("initial_cash"), Decimal("0"))

    @classmethod
    async def get_baselines(
        cls,
        tenant_id: str,
        user_id: str,
        initial_capital: Decimal,
        as_of=None,
    ) -> dict[str, Decimal]:
        """计算日初/月初权益基线。

        取「as_of（默认今日）之前」「本月初之前」最近一条日快照的总资产作为基线；
        无历史快照（新账户/刚重置）时回退初始资金，保证开盘口径盈亏为 0。
        """
        today = as_of or _local_today()
        month_start = today.replace(day=1)
        day_open = initial_capital
        month_open = initial_capital
        try:
            from backend.shared.simulation_account_keys import ledger_user_id_candidates

            user_ids = ledger_user_id_candidates(user_id)
            async with get_session(read_only=True) as session:
                day_row = (
                    await session.execute(
                        select(SimulationFundSnapshot.total_asset)
                        .where(
                            SimulationFundSnapshot.tenant_id == tenant_id,
                            SimulationFundSnapshot.user_id.in_(user_ids),
                            SimulationFundSnapshot.snapshot_date < today,
                        )
                        .order_by(SimulationFundSnapshot.snapshot_date.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if day_row is not None:
                    day_open = _to_decimal(day_row, initial_capital)
                month_row = (
                    await session.execute(
                        select(SimulationFundSnapshot.total_asset)
                        .where(
                            SimulationFundSnapshot.tenant_id == tenant_id,
                            SimulationFundSnapshot.user_id.in_(user_ids),
                            SimulationFundSnapshot.snapshot_date < month_start,
                        )
                        .order_by(SimulationFundSnapshot.snapshot_date.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if month_row is not None:
                    month_open = _to_decimal(month_row, initial_capital)
                elif month_start == today:
                    # 每月 1 号：无上月快照时用日初权益当月初基线，避免当月盈亏重复计入今日变动
                    month_open = day_open
        except Exception as exc:
            logger.warning(
                "get_baselines failed tenant=%s user=%s: %s", tenant_id, user_id, exc
            )
        return {"day_open_equity": day_open, "month_open_equity": month_open}

    @staticmethod
    async def _read_ledger_initial_equity(tenant_id: str, user_id: str) -> Decimal:
        """从 PG 台账读账户初始权益（live 成交已同步写台账后，此处有真实值）。

        settings 缺失时此前直接回退 total_asset，导致 initial=total、盈亏恒为 0。
        """
        try:
            from backend.services.simulation.models.account import SimulationAccount
            from backend.services.simulation.services.ledger_service import (
                SimulationLedgerService,
            )

            account_id = SimulationLedgerService.build_account_id(tenant_id, user_id)
            async with get_session(read_only=True) as session:
                row = (
                    await session.execute(
                        select(SimulationAccount.initial_equity).where(
                            SimulationAccount.account_id == account_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None and Decimal(str(row)) > 0:
                    return Decimal(str(row))
        except Exception as exc:
            logger.debug("ledger initial equity lookup failed: %s", exc)
        return Decimal("0")

    @staticmethod
    async def lock_slot(session, tenant_id: str, user_id: str, snap_date) -> None:
        """取同一 slot（账户+日期）的事务级排他锁。

        周期采集与 EOD 共用同一把锁、同一顺序（单锁，无死锁），拿到锁后调用方
        必须重新检查 EOD 完成态，避免检查与写入之间的竞争。非 PG 环境降级为
        空操作（单测 fake session）。
        """
        try:
            from sqlalchemy import text as sa_text

            key = f"sim_eod_slot:{tenant_id}:{user_id}:{snap_date.isoformat()}"
            await session.execute(
                sa_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key}
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("EOD slot lock unavailable, proceeding without it: %s", exc)

    @staticmethod
    async def is_eod_completed(
        session, tenant_id: str, user_id: str, snap_date
    ) -> bool:
        """该账户该日的日终是否已完成。

        判据（任一成立即视为完成，逐账户判定）：
        1. 存在 EOD 写入的持仓日快照（仅 EOD 写 position_daily；零持仓账户无此行）；
        2. 当日资金快照来源为 EOD（覆盖零持仓账户）。
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
                        SimulationPositionDaily.snapshot_date == snap_date,
                    )
                )
            ).scalar_one_or_none()
            if (count or 0) > 0:
                return True
            source = (
                await session.execute(
                    select(SimulationFundSnapshot.source).where(
                        SimulationFundSnapshot.tenant_id == tenant_id,
                        SimulationFundSnapshot.user_id == str(user_id),
                        SimulationFundSnapshot.snapshot_date == snap_date,
                    )
                )
            ).scalar_one_or_none()
            return str(source or "") == EOD_FUND_SOURCE
        except Exception as exc:  # noqa: BLE001
            logger.debug("EOD completion check failed: %s", exc)
            return False

    @staticmethod
    async def upsert_eod_snapshot(
        session,
        *,
        tenant_id: str,
        user_id: str,
        snapshot_date,
        total_asset: float = 0.0,
        available_balance: float = 0.0,
        frozen_balance: float = 0.0,
        market_value: float = 0.0,
        initial_capital: float = 0.0,
        total_pnl: float = 0.0,
        today_pnl: float = 0.0,
    ) -> None:
        """写入 EOD 日终资金快照（无条件覆盖：EOD 估值永远赢）。

        调用方须持有 lock_slot 且已做完成态重查；与 replace_daily_snapshot
        在同一事务内提交，保证日账与资金快照是同一份重估。
        """
        row = {
            "tenant_id": tenant_id,
            "user_id": str(user_id),
            "snapshot_date": snapshot_date,
            "total_asset": total_asset,
            "available_balance": available_balance,
            "frozen_balance": frozen_balance,
            "market_value": market_value,
            "initial_capital": initial_capital,
            "total_pnl": total_pnl,
            "today_pnl": today_pnl,
            "source": EOD_FUND_SOURCE,
        }
        stmt = (
            pg_insert(SimulationFundSnapshot)
            .values(**row)
            .on_conflict_do_update(
                index_elements=["tenant_id", "user_id", "snapshot_date"],
                set_={
                    "total_asset": row["total_asset"],
                    "available_balance": row["available_balance"],
                    "frozen_balance": row["frozen_balance"],
                    "market_value": row["market_value"],
                    "initial_capital": row["initial_capital"],
                    "total_pnl": row["total_pnl"],
                    "today_pnl": row["today_pnl"],
                    "source": row["source"],
                    "updated_at": datetime.utcnow(),
                },
            )
        )
        await session.execute(stmt)

    @classmethod
    async def capture_all(
        cls,
        redis: RedisClient,
        snapshot_date=None,
        *,
        skip_eod_completed: bool = True,
    ) -> SnapshotUpsertResult:
        if not redis.client:
            return SnapshotUpsertResult(upserted_rows=0, scanned_accounts=0)
        # P0-7：EOD传入trade_date的场景已移至 EOD 内逐户直写；此处调用方
        # （周期 worker / 引擎 / 手动触发）一律按“周期采集”语义处理，默认今日
        snap_date = snapshot_date or _local_today()

        keys = list(redis.client.scan_iter(match="simulation:account:*", count=500))
        # 同一用户跨市场账户（CN/HK/US/...）合并为一条用户级快照：
        # 资产字段累加，盈亏在合并后的总资产上计算（与台账口径一致）。
        grouped: dict[tuple[str, str], dict[str, Decimal]] = {}
        for key in keys:
            parsed = _parse_account_key(str(key))
            if not parsed:
                continue
            tenant_id, user_id = parsed
            raw = redis.client.get(key)
            if not raw:
                continue
            try:
                account = json.loads(raw)
            except Exception:
                continue

            bucket = grouped.setdefault(
                (tenant_id, user_id),
                {
                    "total_asset": Decimal("0"),
                    "available_balance": Decimal("0"),
                    "frozen_balance": Decimal("0"),
                    "market_value": Decimal("0"),
                },
            )
            bucket["total_asset"] += _to_decimal(account.get("total_asset"))
            bucket["available_balance"] += _to_decimal(
                account.get("cash") or account.get("available_balance")
            )
            bucket["frozen_balance"] += _to_decimal(account.get("frozen_balance"))
            bucket["market_value"] += _to_decimal(
                account.get("market_value") or account.get("long_market_value")
            )

        if not grouped:
            return SnapshotUpsertResult(upserted_rows=0, scanned_accounts=len(keys))

        upserted = 0
        async with get_session(read_only=False) as session:
            for (tenant_id, user_id), bucket in grouped.items():
                # 与 EOD 共用 slot 锁并在锁后重查：已完成日终的账户跳过，
                # 周期采集永不覆盖 EOD 日终估值。
                await cls.lock_slot(session, tenant_id, user_id, snap_date)
                if skip_eod_completed and await cls.is_eod_completed(
                    session, tenant_id, user_id, snap_date
                ):
                    continue
                initial_capital = cls._read_settings_initial_cash(
                    redis, tenant_id, user_id
                )
                if initial_capital == 0:
                    initial_capital = await cls._read_ledger_initial_equity(
                        tenant_id, user_id
                    )
                if initial_capital == 0:
                    initial_capital = bucket["total_asset"]
                # 总盈亏 = 总资产 - 初始资金（手续费已从现金扣减，天然计入）
                total_pnl = bucket["total_asset"] - initial_capital
                # 当日盈亏 = 总资产 - 日初权益（snap_date之前最近快照基线）
                baselines = await cls.get_baselines(
                    tenant_id, user_id, initial_capital, as_of=snap_date
                )
                row = {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "snapshot_date": snap_date,
                    "total_asset": bucket["total_asset"],
                    "available_balance": bucket["available_balance"],
                    "frozen_balance": bucket["frozen_balance"],
                    "market_value": bucket["market_value"],
                    "initial_capital": initial_capital,
                    "total_pnl": total_pnl,
                    "today_pnl": bucket["total_asset"] - baselines["day_open_equity"],
                    "source": PERIODIC_FUND_SOURCE,
                }
                stmt = (
                    pg_insert(SimulationFundSnapshot)
                    .values(**row)
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "user_id", "snapshot_date"],
                        set_={
                            "total_asset": row["total_asset"],
                            "available_balance": row["available_balance"],
                            "frozen_balance": row["frozen_balance"],
                            "market_value": row["market_value"],
                            "initial_capital": row["initial_capital"],
                            "total_pnl": row["total_pnl"],
                            "today_pnl": row["today_pnl"],
                            "source": row["source"],
                            # naive 列沿用 utcnow 口径（与模型默认值一致；审计字段不展示）
                            "updated_at": datetime.utcnow(),
                        },
                        # SQL 层兜底：锁后重查与写入之间的极小窗口里若 EOD
                        # 已提交，该行不被覆盖（EOD 估值永远赢）。
                        where=SimulationFundSnapshot.source != EOD_FUND_SOURCE,
                    )
                )
                await session.execute(stmt)
                upserted += 1
                # 同步轻量日台账行：EOD 未跑时智能图表/审计仍有 account_daily 可读
                await cls._upsert_account_daily_summary(session, row)
            # 把存量 fund_snapshots 中缺失的 account_daily 补齐（只插缺的，不覆盖当日已写行以外的历史）
            seen_users = {(str(t), str(u)) for (t, u) in grouped}
            for tenant_id, user_id in seen_users:
                await cls._backfill_missing_account_daily(session, tenant_id, user_id)

        return SnapshotUpsertResult(upserted_rows=upserted, scanned_accounts=len(keys))

    @staticmethod
    async def _backfill_missing_account_daily(
        session, tenant_id: str, user_id: str
    ) -> None:
        """为尚未有 account_daily 的 fund_snapshot 日期补写汇总行。"""
        from sqlalchemy import select

        from backend.services.simulation.models.account_daily import (
            SimulationAccountDaily,
        )

        existing_dates = set(
            (
                await session.execute(
                    select(SimulationAccountDaily.snapshot_date).where(
                        SimulationAccountDaily.tenant_id == tenant_id,
                        SimulationAccountDaily.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        fund_rows = list(
            (
                await session.execute(
                    select(SimulationFundSnapshot).where(
                        SimulationFundSnapshot.tenant_id == tenant_id,
                        SimulationFundSnapshot.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for snap in fund_rows:
            if snap.snapshot_date in existing_dates:
                continue
            await SimulationFundSnapshotService._upsert_account_daily_summary(
                session,
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "snapshot_date": snap.snapshot_date,
                    "total_asset": snap.total_asset,
                    "available_balance": snap.available_balance,
                    "frozen_balance": snap.frozen_balance,
                    "market_value": snap.market_value,
                    "today_pnl": snap.today_pnl,
                    "total_pnl": snap.total_pnl,
                },
            )

    @staticmethod
    async def _upsert_account_daily_summary(session, row: dict[str, object]) -> None:
        """由资金快照写/更新 simulation_account_daily 汇总行（不含持仓明细）。"""
        from sqlalchemy import delete

        from backend.services.simulation.models.account_daily import (
            SimulationAccountDaily,
        )
        from backend.services.simulation.services.ledger_service import (
            SimulationLedgerService,
        )

        tenant_id = str(row["tenant_id"])
        user_id = str(row["user_id"])
        snapshot_date = row["snapshot_date"]
        account_id = SimulationLedgerService.build_account_id(tenant_id, user_id)
        total_asset = float(row["total_asset"] or 0)
        available = float(row["available_balance"] or 0)
        frozen = float(row["frozen_balance"] or 0)
        market_value = float(row["market_value"] or 0)
        today_pnl = float(row["today_pnl"] or 0)
        total_pnl = float(row["total_pnl"] or 0)
        cash = available + frozen

        await session.execute(
            delete(SimulationAccountDaily).where(
                SimulationAccountDaily.tenant_id == tenant_id,
                SimulationAccountDaily.user_id == user_id,
                SimulationAccountDaily.snapshot_date == snapshot_date,
            )
        )
        session.add(
            SimulationAccountDaily(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=user_id,
                snapshot_date=snapshot_date,
                snapshot_at=datetime.combine(snapshot_date, datetime.min.time()),
                cash=cash,
                available_cash=available,
                frozen_cash=frozen,
                long_market_value=max(0.0, market_value),
                short_market_value=0.0,
                total_asset=total_asset,
                liabilities=0.0,
                equity=total_asset,
                daily_pnl=today_pnl,
                total_pnl=total_pnl,
            )
        )

    @staticmethod
    async def list_user_daily(
        tenant_id: str,
        user_id: str,
        days: int = 30,
    ) -> list[SimulationFundSnapshot]:
        async with get_session(read_only=True) as session:
            stmt = (
                select(SimulationFundSnapshot)
                .where(
                    SimulationFundSnapshot.tenant_id == tenant_id,
                    SimulationFundSnapshot.user_id == user_id,
                )
                .order_by(SimulationFundSnapshot.snapshot_date.desc())
                .limit(max(1, min(days, 3650)))
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())


class SimulationFundSnapshotWorker:
    def __init__(self, redis: RedisClient, interval_seconds: int):
        self.redis = redis
        self.interval_seconds = max(60, int(interval_seconds))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="sim-fund-snapshot-worker")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                result = await SimulationFundSnapshotService.capture_all(self.redis)
                if result.scanned_accounts > 0:
                    logger.info(
                        "Simulation fund snapshot upserted: %s/%s",
                        result.upserted_rows,
                        result.scanned_accounts,
                    )
            except Exception as exc:
                logger.error(
                    "Simulation fund snapshot worker failed: %s", exc, exc_info=True
                )

            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                continue
