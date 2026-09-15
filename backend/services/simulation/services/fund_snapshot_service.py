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


def resolve_account_seed(
    account: dict,
    market: str,
    settings_initial: Decimal | None,
) -> Decimal | None:
    """单市场账户的初始资金（用户级快照 initial_capital 求和用）。纯函数。

    用户级快照按 (tenant, user) 合并跨市场账户（快照表以 tenant/user/date 唯一），
    因此初始资金必须按市场逐个求和——settings 只有一份（无市场维度），拿它当
    唯一初始会让每个新增市场账户的种子被计成盈利（曾致 +100 万假收益）。

    解析顺序：
      1. 账户内显式 initial_cash（init_account 落的字段，P0-05 起写入）；
      2. 未交易启发式：无持仓且现金 ≈ 总资产（从未成交 = 无盈亏）→ 以当前总资产为初始；
      3. CN 账户回退用户级 settings（历史存量账户唯一可信来源）；
      4. 其余返回 None（未知种子，不参与求和，由汇总层 WARNING 点名）。
    """
    explicit = account.get("initial_cash")
    if explicit is not None:
        try:
            return Decimal(str(explicit))
        except Exception:
            pass

    cash = _to_decimal(account.get("cash") or account.get("available_balance"))
    total = _to_decimal(account.get("total_asset"))
    positions = account.get("positions")
    has_positions = isinstance(positions, dict) and any(
        isinstance(pos, dict) and float(pos.get("volume") or 0) > 0
        for pos in positions.values()
    )
    if not has_positions and abs(cash - total) <= Decimal("0.01"):
        return total

    if str(market or "CN").upper() == "CN" and settings_initial is not None:
        return settings_initial
    return None


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
        # 同一用户跨市场账户（CN/HK/US/...）合并为一条用户级快照：
        # 资产字段累加，盈亏在合并后的总资产上计算（与台账口径一致）。
        # P0-05：initial_capital 同样按市场逐个求和（见 resolve_account_seed），
        # 不能用单份 settings 当唯一初始——否则每个新增市场账户的种子被计成盈利。
        grouped: dict[tuple[str, str], dict[str, Decimal]] = {}
        settings_cache: dict[tuple[str, str], Decimal] = {}
        for key in keys:
            parsed = parse_canonical_account_key(str(key))
            if not parsed:
                continue
            tenant_id, user_id, market = parsed
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
                    "initial_capital": Decimal("0"),
                },
            )
            bucket["total_asset"] += _to_decimal(account.get("total_asset"))
            bucket["available_balance"] += _to_decimal(
                account.get("cash") or account.get("available_balance")
            )
            bucket["frozen_balance"] += _to_decimal(account.get("frozen_balance"))
            bucket["market_value"] += _to_decimal(account.get("market_value"))

            settings_initial: Decimal | None = None
            if str(market).upper() == "CN":
                cache_key = (tenant_id, user_id)
                if cache_key not in settings_cache:
                    settings_cache[cache_key] = cls._read_settings_initial_cash(
                        redis, tenant_id, user_id
                    )
                settings_initial = settings_cache[cache_key] or None
            seed = resolve_account_seed(account, market, settings_initial)
            if seed is None:
                logger.warning(
                    "fund snapshot: unknown initial capital for %s (market=%s); "
                    "excluded from seed sum",
                    key,
                    market,
                )
            else:
                bucket["initial_capital"] += seed

        rows: list[dict[str, object]] = []
        for (tenant_id, user_id), bucket in grouped.items():
            initial_capital = bucket["initial_capital"]
            if initial_capital == 0:
                # 全部账户均无种子（历史数据/异常）：沿用原兜底链，保盈亏为 0 口径
                initial_capital = cls._read_settings_initial_cash(
                    redis, tenant_id, user_id
                )
                if initial_capital == 0:
                    initial_capital = await cls._read_ledger_initial_equity(
                        tenant_id, user_id
                    )
                if initial_capital == 0:
                    initial_capital = bucket["total_asset"]
            row = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                # P0-7：EOD按trade_date记，与account_daily对齐；周期采集默认今日
                "snapshot_date": snap_date,
                "total_asset": bucket["total_asset"],
                "available_balance": bucket["available_balance"],
                "frozen_balance": bucket["frozen_balance"],
                "market_value": bucket["market_value"],
                "initial_capital": initial_capital,
                "total_pnl": Decimal("0"),
                "today_pnl": Decimal("0"),
                "source": "redis_simulation_account",
            }
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
