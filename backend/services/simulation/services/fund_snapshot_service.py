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
from backend.shared.fund_snapshot_contract import (
    ALL_MARKET,
    ensure_fund_snapshot_contract_async,
    fund_snapshot_has_market_column_async,
    normalize_snapshot_market,
)
from backend.shared.simulation_account_keys import (
    parse_account_key as parse_canonical_account_key,
)

logger = logging.getLogger(__name__)


def compute_market_baselines(
    today_totals: dict[str, Decimal],
    seeds: dict[str, Decimal | None],
    prev_day_totals: dict[str, Decimal],
    prev_month_totals: dict[str, Decimal],
    *,
    is_month_start: bool = False,
) -> dict[str, dict[str, Decimal]]:
    """按市场计算日初/月初权益基线（纯函数，T-P1-07 核心口径）。

    规则（机构口径）：
    - 单市场：日初基线 = 该市场 as_of 之前最近快照的总资产；无历史（新开市场首日）
      → 用该市场种子为基线——**种子注入不计入 today_pnl**；种子未知（None）
      → 用当日总资产（不声称任何盈亏，与"未知种子不参与求和"一致）。
    - ALL（合并行）：各市场基线之和——旧市场用昨日快照、新市场用种子，
      等价于"把新账户出现的当日种子计入基线"（T-P1-07 ② 的完整形态）。
    - 月初：优先"月初之前最近快照"；每月 1 号无上月快照时用日初基线（与旧实现同日历
      语义）；月中新建账户（无月初历史）用种子（盈亏=建户以来）。

    返回 {market 或 'ALL': {"day_open_equity": .., "month_open_equity": ..}}，必含 ALL 行。
    """
    out: dict[str, dict[str, Decimal]] = {}
    day_all = Decimal("0")
    month_all = Decimal("0")
    for market, total in today_totals.items():
        seed = seeds.get(market)
        prev_day = prev_day_totals.get(market)
        if prev_day is not None:
            day_open = prev_day
        elif seed is not None:
            day_open = seed
        else:
            day_open = total

        prev_month = prev_month_totals.get(market)
        if prev_month is not None:
            month_open = prev_month
        elif is_month_start:
            month_open = day_open
        elif seed is not None:
            month_open = seed
        else:
            month_open = day_open

        out[market] = {"day_open_equity": day_open, "month_open_equity": month_open}
        day_all += day_open
        month_all += month_open
    out[ALL_MARKET] = {"day_open_equity": day_all, "month_open_equity": month_all}
    return out


async def _latest_totals_before(
    session, tenant_id: str, user_id: str, before, *, market: str
) -> dict[str, Decimal]:
    """as_of 之前各市场最近一条快照总资产 {market: total}（单市场传具体市场名）。"""
    from sqlalchemy import text as sa_text

    rows = (
        await session.execute(
            sa_text(
                "SELECT DISTINCT ON (market) market, total_asset "
                "FROM simulation_fund_snapshots "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date < :d "
                "AND market = :m "
                "ORDER BY market, snapshot_date DESC"
            ),
            {"t": tenant_id, "u": user_id, "d": before, "m": market},
        )
    ).fetchall()
    return {str(r[0]): _to_decimal(r[1]) for r in rows}


async def _latest_totals_by_market_before(
    session, tenant_id: str, user_id: str, before
) -> dict[str, Decimal]:
    """as_of 之前**每个市场**最近一条快照总资产 {market: total}（不含 ALL 行）。"""
    from sqlalchemy import text as sa_text

    rows = (
        await session.execute(
            sa_text(
                "SELECT DISTINCT ON (market) market, total_asset "
                "FROM simulation_fund_snapshots "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date < :d "
                "AND market <> :all "
                "ORDER BY market, snapshot_date DESC"
            ),
            {"t": tenant_id, "u": user_id, "d": before, "all": ALL_MARKET},
        )
    ).fetchall()
    return {str(r[0]): _to_decimal(r[1]) for r in rows}


async def _latest_total_scoped(
    session, tenant_id: str, user_id: str, before, *, market: str | None
) -> Decimal | None:
    """as_of 之前最近一条快照总资产；market=None 表示旧库（不过滤市场）。"""
    from sqlalchemy import text as sa_text

    where_market = "AND market = :m " if market else ""
    params: dict[str, object] = {"t": tenant_id, "u": user_id, "d": before}
    if market:
        params["m"] = market
    row = (
        await session.execute(
            sa_text(
                "SELECT total_asset FROM simulation_fund_snapshots "
                "WHERE tenant_id = :t AND user_id = :u AND snapshot_date < :d "
                f"{where_market}"
                "ORDER BY snapshot_date DESC LIMIT 1"
            ),
            params,
        )
    ).fetchone()
    return _to_decimal(row[0]) if row is not None else None


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
        market: str = ALL_MARKET,
    ) -> dict[str, Decimal]:
        """计算日初/月初权益基线（T-P1-07：市场维度）。

        - market='ALL'（默认）：优先用 as_of 当天各市场行按 ``compute_market_baselines``
          求和（新市场首日用其种子，种子注入不计入 today_pnl）；当天无行时回退
          "as_of 之前最近 ALL 行 / initial_capital"（等价旧口径）。
        - market=具体市场：该市场 as_of 之前最近一行，否则 initial_capital（调用方传该市场种子）。
        - 旧库（market 列未就绪）：原用户级语义原样保留。
        """
        today = as_of or _local_today()
        month_start = today.replace(day=1)
        scope = normalize_snapshot_market(market)
        try:
            has_col = await fund_snapshot_has_market_column_async()
        except Exception:  # noqa: BLE001 - 探测异常按旧库处理
            has_col = False

        if not has_col:
            # ── 旧库兜底：原用户级语义（与升级前逐字一致） ──
            day_open = initial_capital
            month_open = initial_capital
            try:
                async with get_session(read_only=True) as session:
                    day_row = await _latest_total_scoped(
                        session, tenant_id, user_id, today, market=None
                    )
                    if day_row is not None:
                        day_open = day_row
                    month_row = await _latest_total_scoped(
                        session, tenant_id, user_id, month_start, market=None
                    )
                    if month_row is not None:
                        month_open = month_row
                    elif month_start == today:
                        month_open = day_open
            except Exception as exc:
                logger.warning(
                    "get_baselines failed tenant=%s user=%s: %s", tenant_id, user_id, exc
                )
            return {"day_open_equity": day_open, "month_open_equity": month_open}

        if scope == ALL_MARKET:
            try:
                async with get_session(read_only=True) as session:
                    today_rows = (
                        await session.execute(
                            select(
                                SimulationFundSnapshot.market,
                                SimulationFundSnapshot.total_asset,
                                SimulationFundSnapshot.initial_capital,
                            ).where(
                                SimulationFundSnapshot.tenant_id == tenant_id,
                                SimulationFundSnapshot.user_id == user_id,
                                SimulationFundSnapshot.snapshot_date == today,
                                SimulationFundSnapshot.market != ALL_MARKET,
                            )
                        )
                    ).all()
                    if today_rows:
                        today_totals = {str(r[0]): _to_decimal(r[1]) for r in today_rows}
                        seeds: dict[str, Decimal | None] = {
                            str(r[0]): _to_decimal(r[2]) for r in today_rows
                        }
                        prev_day = await _latest_totals_by_market_before(
                            session, tenant_id, user_id, today
                        )
                        prev_month = await _latest_totals_by_market_before(
                            session, tenant_id, user_id, month_start
                        )
                        baselines = compute_market_baselines(
                            today_totals,
                            seeds,
                            prev_day,
                            prev_month,
                            is_month_start=(month_start == today),
                        )
                        return baselines[ALL_MARKET]
                    # 当天尚无快照：回退最近 ALL 行 / 初始资金（旧口径）
                    day_open = initial_capital
                    month_open = initial_capital
                    day_row = await _latest_total_scoped(
                        session, tenant_id, user_id, today, market=ALL_MARKET
                    )
                    if day_row is not None:
                        day_open = day_row
                    month_row = await _latest_total_scoped(
                        session, tenant_id, user_id, month_start, market=ALL_MARKET
                    )
                    if month_row is not None:
                        month_open = month_row
                    elif month_start == today:
                        month_open = day_open
                    return {"day_open_equity": day_open, "month_open_equity": month_open}
            except Exception as exc:
                logger.warning(
                    "get_baselines(ALL) failed tenant=%s user=%s: %s",
                    tenant_id,
                    user_id,
                    exc,
                )
            return {
                "day_open_equity": initial_capital,
                "month_open_equity": initial_capital,
            }

        # 单市场：该市场 as_of 前最近一行，否则该市场种子（initial_capital 由调用方按市场传入）
        day_open = initial_capital
        month_open = initial_capital
        try:
            async with get_session(read_only=True) as session:
                day_row = await _latest_total_scoped(
                    session, tenant_id, user_id, today, market=scope
                )
                if day_row is not None:
                    day_open = day_row
                month_row = await _latest_total_scoped(
                    session, tenant_id, user_id, month_start, market=scope
                )
                if month_row is not None:
                    month_open = month_row
                elif month_start == today:
                    month_open = day_open
        except Exception as exc:
            logger.warning(
                "get_baselines(%s) failed tenant=%s user=%s: %s",
                scope,
                tenant_id,
                user_id,
                exc,
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
        # T-P1-07：契约未就绪（旧库/迁移失败）→ 旧口径单行写入，业务不中断
        if not await ensure_fund_snapshot_contract_async():
            return await cls._capture_all_legacy(redis, snap_date)

        keys = list(redis.client.scan_iter(match="simulation:account:*", count=500))
        # 市场桶：{(tenant, user, market): {资产字段求和, seed}}；ALL 行由市场桶求和派生。
        # T-P1-07：市场维度落行（非 CN 面板有自己的资金曲线）；新市场首日种子进基线
        # （compute_market_baselines），不再被算进 today_pnl。
        per_market: dict[tuple[str, str, str], dict[str, object]] = {}
        settings_cache: dict[tuple[str, str], Decimal] = {}
        for key in keys:
            parsed = parse_canonical_account_key(str(key))
            if not parsed:
                continue
            tenant_id, user_id, market = parsed
            market_key = str(market or "CN").upper()
            raw = redis.client.get(key)
            if not raw:
                continue
            try:
                account = json.loads(raw)
            except Exception:
                continue

            bucket = per_market.setdefault(
                (tenant_id, user_id, market_key),
                {
                    "total_asset": Decimal("0"),
                    "available_balance": Decimal("0"),
                    "frozen_balance": Decimal("0"),
                    "market_value": Decimal("0"),
                    "seed": None,
                },
            )
            bucket["total_asset"] = _to_decimal(bucket["total_asset"]) + _to_decimal(
                account.get("total_asset")
            )
            bucket["available_balance"] = _to_decimal(
                bucket["available_balance"]
            ) + _to_decimal(account.get("cash") or account.get("available_balance"))
            bucket["frozen_balance"] = _to_decimal(
                bucket["frozen_balance"]
            ) + _to_decimal(account.get("frozen_balance"))
            bucket["market_value"] = _to_decimal(bucket["market_value"]) + _to_decimal(
                account.get("market_value")
            )

            settings_initial: Decimal | None = None
            if market_key == "CN":
                cache_key = (tenant_id, user_id)
                if cache_key not in settings_cache:
                    settings_cache[cache_key] = cls._read_settings_initial_cash(
                        redis, tenant_id, user_id
                    )
                settings_initial = settings_cache[cache_key] or None
            seed = resolve_account_seed(account, market_key, settings_initial)
            if seed is None:
                logger.warning(
                    "fund snapshot: unknown initial capital for %s (market=%s); "
                    "excluded from seed sum",
                    key,
                    market_key,
                )
            else:
                bucket["seed"] = _to_decimal(bucket["seed"]) + seed

        if not per_market:
            return SnapshotUpsertResult(upserted_rows=0, scanned_accounts=len(keys))

        month_start = snap_date.replace(day=1)
        is_month_start = month_start == snap_date
        users = sorted({(t, u) for (t, u, _m) in per_market})
        rows: list[dict[str, object]] = []
        async with get_session(read_only=True) as session:
            for tenant_id, user_id in users:
                buckets = {
                    m: per_market[(tenant_id, user_id, m)]
                    for (t, u, m) in per_market
                    if (t, u) == (tenant_id, user_id)
                }
                today_totals = {
                    m: _to_decimal(b["total_asset"]) for m, b in buckets.items()
                }
                seeds: dict[str, Decimal | None] = {
                    m: (None if b["seed"] is None else _to_decimal(b["seed"]))
                    for m, b in buckets.items()
                }
                prev_day = await _latest_totals_by_market_before(
                    session, tenant_id, user_id, snap_date
                )
                prev_month = await _latest_totals_by_market_before(
                    session, tenant_id, user_id, month_start
                )
                baselines = compute_market_baselines(
                    today_totals,
                    seeds,
                    prev_day,
                    prev_month,
                    is_month_start=is_month_start,
                )
                for market_key, bucket in buckets.items():
                    total = _to_decimal(bucket["total_asset"])
                    seed = seeds[market_key]
                    # 单市场行：未知种子回退当日总资产（盈亏 0 口径，不声称未知盈亏）
                    initial_capital = seed if seed is not None else total
                    rows.append(
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "snapshot_date": snap_date,
                            "market": market_key,
                            "total_asset": total,
                            "available_balance": _to_decimal(
                                bucket["available_balance"]
                            ),
                            "frozen_balance": _to_decimal(bucket["frozen_balance"]),
                            "market_value": _to_decimal(bucket["market_value"]),
                            "initial_capital": initial_capital,
                            "total_pnl": total - initial_capital,
                            "today_pnl": total
                            - baselines[market_key]["day_open_equity"],
                            "source": "redis_simulation_account",
                        }
                    )

                # ALL（跨市场合并行）：与旧用户级口径同构
                total_all = sum(today_totals.values(), Decimal("0"))
                seed_all = sum(
                    (s for s in seeds.values() if s is not None), Decimal("0")
                )
                initial_all = seed_all
                if initial_all == 0:
                    initial_all = cls._read_settings_initial_cash(
                        redis, tenant_id, user_id
                    )
                    if initial_all == 0:
                        initial_all = await cls._read_ledger_initial_equity(
                            tenant_id, user_id
                        )
                    if initial_all == 0:
                        initial_all = total_all
                rows.append(
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "snapshot_date": snap_date,
                        "market": ALL_MARKET,
                        "total_asset": total_all,
                        "available_balance": sum(
                            (_to_decimal(b["available_balance"]) for b in buckets.values()),
                            Decimal("0"),
                        ),
                        "frozen_balance": sum(
                            (_to_decimal(b["frozen_balance"]) for b in buckets.values()),
                            Decimal("0"),
                        ),
                        "market_value": sum(
                            (_to_decimal(b["market_value"]) for b in buckets.values()),
                            Decimal("0"),
                        ),
                        "initial_capital": initial_all,
                        "total_pnl": total_all - initial_all,
                        # 新市场首日：其种子已进 ALL 基线（compute_market_baselines），
                        # 不再把一次性资本注入算成今日盈利（T-P1-07 ②）
                        "today_pnl": total_all - baselines[ALL_MARKET]["day_open_equity"],
                        "source": "redis_simulation_account",
                    }
                )

        if not rows:
            return SnapshotUpsertResult(upserted_rows=0, scanned_accounts=len(keys))

        async with get_session(read_only=False) as session:
            for row in rows:
                stmt = (
                    pg_insert(SimulationFundSnapshot)
                    .values(**row)
                    .on_conflict_do_update(
                        index_elements=[
                            "tenant_id",
                            "user_id",
                            "snapshot_date",
                            "market",
                        ],
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

    @classmethod
    async def _capture_all_legacy(
        cls, redis: RedisClient, snap_date
    ) -> SnapshotUpsertResult:
        """契约未就绪时的旧口径（用户级单行，逐字保留 P0-05 修复后的行为）。"""
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
        market: str = ALL_MARKET,
    ) -> list[SimulationFundSnapshot]:
        """用户日快照序列（T-P1-07：默认 ALL=跨市场合并行；传市场名取该市场序列）。"""
        scope = normalize_snapshot_market(market)
        has_col = await fund_snapshot_has_market_column_async()
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
            if has_col:
                stmt = stmt.where(SimulationFundSnapshot.market == scope)
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
