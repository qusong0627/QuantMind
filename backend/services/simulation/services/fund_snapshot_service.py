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

from sqlalchemy import select
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
    # 暂不纳入资金快照（快照表以 tenant/user/date 唯一，跨市场会串行覆盖），
    # 显式跳过并记录，避免静默丢失。
    parsed = parse_canonical_account_key(key)
    if not parsed:
        return None
    tenant_id, user_id, market = parsed
    if market != "CN":
        logger.debug("资金快照跳过非 CN 市场账户: %s", key)
        return None
    return tenant_id, user_id


@dataclass
class SnapshotUpsertResult:
    upserted_rows: int
    scanned_accounts: int


class SimulationFundSnapshotService:
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
    def _build_row(
        cls,
        tenant_id: str,
        user_id: str,
        account: dict[str, object],
        snapshot_date=None,
    ) -> dict[str, object]:
        total_asset = _to_decimal(account.get("total_asset"))
        available_balance = _to_decimal(
            account.get("cash") or account.get("available_balance")
        )
        frozen_balance = _to_decimal(account.get("frozen_balance"))
        market_value = _to_decimal(account.get("market_value"))
        initial_capital = _to_decimal(account.get("initial_capital"))
        today_pnl = _to_decimal(account.get("today_pnl"))
        total_pnl = _to_decimal(account.get("total_pnl"))
        # 注意：这里不能用 total_asset 预填 initial_capital，
        # 否则 capture_all 的 settings.initial_cash 回退永远不会命中，
        # 导致 total_pnl 恒为 0。缺失时保留 0，由调用方做回退。

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            # P0-7：EOD按trade_date记，与account_daily对齐；周期采集默认今日
            "snapshot_date": snapshot_date or _local_today(),
            "total_asset": total_asset,
            "available_balance": available_balance,
            "frozen_balance": frozen_balance,
            "market_value": market_value,
            "initial_capital": initial_capital,
            "total_pnl": total_pnl,
            "today_pnl": today_pnl,
            "source": "redis_simulation_account",
        }

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
            async with get_session(read_only=True) as session:
                day_row = (
                    await session.execute(
                        select(SimulationFundSnapshot.total_asset)
                        .where(
                            SimulationFundSnapshot.tenant_id == tenant_id,
                            SimulationFundSnapshot.user_id == user_id,
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
                            SimulationFundSnapshot.user_id == user_id,
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

    @classmethod
    async def capture_all(
        cls, redis: RedisClient, snapshot_date=None
    ) -> SnapshotUpsertResult:
        if not redis.client:
            return SnapshotUpsertResult(upserted_rows=0, scanned_accounts=0)
        # P0-7：EOD传入trade_date，其余调用方默认今日
        snap_date = snapshot_date or _local_today()

        keys = list(redis.client.scan_iter(match="simulation:account:*", count=500))
        rows: list[dict[str, object]] = []
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

            row = cls._build_row(tenant_id, user_id, account, snapshot_date=snap_date)
            if row["initial_capital"] == 0:
                row["initial_capital"] = cls._read_settings_initial_cash(
                    redis, tenant_id, user_id
                )
                if row["initial_capital"] == 0:
                    row["initial_capital"] = await cls._read_ledger_initial_equity(
                        tenant_id, user_id
                    )
                if row["initial_capital"] == 0:
                    row["initial_capital"] = row["total_asset"]
            # 总盈亏 = 总资产 - 初始资金（手续费已从现金扣减，天然计入）
            row["total_pnl"] = row["total_asset"] - row["initial_capital"]
            # 当日盈亏 = 总资产 - 日初权益（snap_date之前最近快照基线）
            baselines = await cls.get_baselines(
                tenant_id, user_id, row["initial_capital"], as_of=snap_date
            )
            row["today_pnl"] = row["total_asset"] - baselines["day_open_equity"]
            rows.append(row)

        if not rows:
            return SnapshotUpsertResult(upserted_rows=0, scanned_accounts=len(keys))

        async with get_session(read_only=False) as session:
            for row in rows:
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
                    )
                )
                await session.execute(stmt)

        return SnapshotUpsertResult(upserted_rows=len(rows), scanned_accounts=len(keys))

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
