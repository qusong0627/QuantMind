"""
Real account daily ledger persistence and queries.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.models.real_account_ledger import RealAccountLedgerDailySnapshot
from backend.services.trade.models.real_account_snapshot import RealAccountSnapshot

logger = logging.getLogger(__name__)
_SH_TZ = ZoneInfo("Asia/Shanghai")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        parsed = float(value)
        return parsed if parsed == parsed else default
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        parsed = int(value)
        return parsed
    except Exception:
        return default


def _ensure_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


async def upsert_real_account_daily_ledger(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    snapshot_at: datetime,
    snapshot_date: date,
    total_asset: float,
    cash: float,
    market_value: float,
    initial_equity: float,
    day_open_equity: float,
    month_open_equity: float,
    today_pnl: float,
    total_pnl: float,
    floating_pnl: float,
    position_count: int,
    source: str,
    payload_json: dict[str, Any] | None = None,
) -> None:
    existing_stmt = select(RealAccountLedgerDailySnapshot).where(
        RealAccountLedgerDailySnapshot.tenant_id == tenant_id,
        RealAccountLedgerDailySnapshot.user_id == user_id,
        RealAccountLedgerDailySnapshot.account_id == account_id,
        RealAccountLedgerDailySnapshot.snapshot_date == snapshot_date,
    ).limit(1)
    existing_result = await db.execute(existing_stmt)
    existing = existing_result.scalar_one_or_none()
    incoming_snapshot_at = _ensure_utc_naive(snapshot_at)
    if existing is not None and existing.last_snapshot_at is not None:
        existing_snapshot_at = _ensure_utc_naive(existing.last_snapshot_at)
        if incoming_snapshot_at <= existing_snapshot_at:
            logger.info(
                "Skip stale real-account daily ledger upsert tenant=%s user=%s account=%s snapshot_date=%s incoming=%s existing=%s",
                tenant_id,
                user_id,
                account_id,
                snapshot_date.isoformat(),
                incoming_snapshot_at.isoformat(),
                existing_snapshot_at.isoformat(),
            )
            return

    derived_daily_pnl = total_asset - day_open_equity if day_open_equity > 0 else today_pnl
    derived_monthly_pnl = total_asset - month_open_equity if month_open_equity > 0 else total_pnl

    # 使用平台基准计算累计收益率，保证与仪表盘一致
    cumulative_pnl = total_asset - initial_equity if initial_equity > 0 else total_pnl
    total_return_pct = (cumulative_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0
    daily_return_pct = (derived_daily_pnl / day_open_equity * 100.0) if day_open_equity > 0 else 0.0

    row = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "account_id": account_id,
        "snapshot_date": snapshot_date,
        "last_snapshot_at": incoming_snapshot_at,
        "initial_equity": float(initial_equity or 0.0),
        "day_open_equity": float(day_open_equity or 0.0),
        "month_open_equity": float(month_open_equity or 0.0),
        "total_asset": float(total_asset or 0.0),
        "cash": float(cash or 0.0),
        "market_value": float(market_value or 0.0),
        # 原始字段保留券商/桥接层上报值，用于历史审计
        "today_pnl_raw": float(today_pnl or 0.0),
        "monthly_pnl_raw": float(derived_monthly_pnl),
        "total_pnl_raw": float(total_pnl or 0.0),
        "floating_pnl_raw": float(floating_pnl or 0.0),
        "daily_return_pct": float(daily_return_pct),
        "total_return_pct": float(total_return_pct),
        "position_count": int(position_count or 0),
        "source": str(source or "qmt_bridge"),
        "payload_json": payload_json or {},
    }

    stmt = (
        pg_insert(RealAccountLedgerDailySnapshot)
        .values(**row)
        .on_conflict_do_update(
            index_elements=[
                RealAccountLedgerDailySnapshot.tenant_id,
                RealAccountLedgerDailySnapshot.user_id,
                RealAccountLedgerDailySnapshot.account_id,
                RealAccountLedgerDailySnapshot.snapshot_date,
            ],
            set_={
                "last_snapshot_at": row["last_snapshot_at"],
                "initial_equity": row["initial_equity"],
                "day_open_equity": row["day_open_equity"],
                "month_open_equity": row["month_open_equity"],
                "total_asset": row["total_asset"],
                "cash": row["cash"],
                "market_value": row["market_value"],
                "today_pnl_raw": row["today_pnl_raw"],
                "monthly_pnl_raw": row["monthly_pnl_raw"],
                "total_pnl_raw": row["total_pnl_raw"],
                "floating_pnl_raw": row["floating_pnl_raw"],
                "daily_return_pct": row["daily_return_pct"],
                "total_return_pct": row["total_return_pct"],
                "position_count": row["position_count"],
                "source": row["source"],
                "payload_json": row["payload_json"],
            },
        )
    )
    await db.execute(stmt)


async def list_real_account_daily_ledgers(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str | None = None,
    days: int = 30,
) -> list[RealAccountLedgerDailySnapshot]:
    stmt = select(RealAccountLedgerDailySnapshot).where(
        RealAccountLedgerDailySnapshot.tenant_id == tenant_id,
        RealAccountLedgerDailySnapshot.user_id == user_id,
    )
    if account_id:
        stmt = stmt.where(RealAccountLedgerDailySnapshot.account_id == account_id)
    stmt = (
        stmt.order_by(desc(RealAccountLedgerDailySnapshot.snapshot_date), desc(RealAccountLedgerDailySnapshot.last_snapshot_at))
        .limit(max(1, min(days, 3650)))
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    return list(reversed(rows))


async def finalize_real_account_daily_ledgers(
    db: AsyncSession,
    *,
    snapshot_date: date,
    finalized_local_time: dt_time = dt_time(15, 0, 0),
) -> int:
    settlement_local_dt = datetime.combine(snapshot_date, finalized_local_time, tzinfo=_SH_TZ)
    settlement_utc_naive = settlement_local_dt.astimezone(timezone.utc).replace(tzinfo=None)

    rows_result = await db.execute(
        select(RealAccountLedgerDailySnapshot).where(
            RealAccountLedgerDailySnapshot.snapshot_date == snapshot_date,
        )
    )
    rows = list(rows_result.scalars().all())
    if not rows:
        return 0

    valid_asset_filter = or_(
        RealAccountSnapshot.total_asset > 1e-8,
        RealAccountSnapshot.cash > 1e-8,
        RealAccountSnapshot.market_value > 1e-8,
    )
    counts_result = await db.execute(
        select(
            RealAccountSnapshot.tenant_id,
            RealAccountSnapshot.user_id,
            RealAccountSnapshot.account_id,
            func.count(RealAccountSnapshot.id),
            func.max(RealAccountSnapshot.snapshot_at),
        )
        .where(
            RealAccountSnapshot.snapshot_date == snapshot_date,
            valid_asset_filter,
        )
        .group_by(
            RealAccountSnapshot.tenant_id,
            RealAccountSnapshot.user_id,
            RealAccountSnapshot.account_id,
        )
    )
    counts_map = {
        (str(row[0]), str(row[1]), str(row[2])): {
            "snapshot_count": int(row[3] or 0),
            "last_valid_snapshot_at": row[4],
        }
        for row in counts_result.all()
    }

    finalized_rows = 0
    for row in rows:
        key = (str(row.tenant_id), str(row.user_id), str(row.account_id))
        stats = counts_map.get(
            key,
            {
                "snapshot_count": 0,
                "last_valid_snapshot_at": row.last_snapshot_at,
            },
        )
        payload_json = dict(row.payload_json or {})
        payload_json["settlement_finalized"] = True
        payload_json["settlement_finalized_at"] = _iso_or_none(settlement_utc_naive)
        payload_json["settlement_snapshot_count"] = int(stats["snapshot_count"] or 0)
        payload_json["settlement_last_valid_snapshot_at"] = _iso_or_none(stats.get("last_valid_snapshot_at"))

        row.payload_json = payload_json
        row.source = "daily_settlement"
        if row.last_snapshot_at is None or _ensure_utc_naive(row.last_snapshot_at) < settlement_utc_naive:
            row.last_snapshot_at = settlement_utc_naive
        db.add(row)
        finalized_rows += 1

    logger.info(
        "Finalized real-account daily ledgers snapshot_date=%s rows=%d finalized_at=%s",
        snapshot_date.isoformat(),
        finalized_rows,
        settlement_utc_naive.isoformat(),
    )
    return finalized_rows


async def backfill_daily_ledgers_from_snapshots(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    days: int = 30,
) -> int:
    """Backfill daily ledger rows from persisted real_account_snapshots.

    This is used as a safety net for legacy users whose daily ledger rows were
    missing, so frontend daily-return charts can still read normalized data.
    """
    scoped_rows_stmt = (
        select(RealAccountSnapshot)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
        )
        .order_by(desc(RealAccountSnapshot.snapshot_at))
        .limit(max(1, min(days, 3650)) * 64)
    )
    scoped_result = await db.execute(scoped_rows_stmt)
    snapshots = list(scoped_result.scalars().all())
    if not snapshots:
        return 0

    baseline_stmt = (
        select(RealAccountSnapshot)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
        )
        .order_by(asc(RealAccountSnapshot.snapshot_at), asc(RealAccountSnapshot.id))
        .limit(1)
    )
    baseline_result = await db.execute(baseline_stmt)
    baseline_row = baseline_result.scalar_one_or_none()
    baseline_initial_equity = _to_float(
        getattr(baseline_row, "total_asset", None),
        _to_float(getattr(snapshots[-1], "total_asset", None), 0.0),
    )

    latest_per_day: dict[date, RealAccountSnapshot] = {}
    for row in snapshots:
        day = getattr(row, "snapshot_date", None)
        if day is None or day in latest_per_day:
            continue
        latest_per_day[day] = row
        if len(latest_per_day) >= max(1, min(days, 3650)):
            break

    upserted = 0
    for snapshot_date in sorted(latest_per_day.keys()):
        row = latest_per_day[snapshot_date]
        total_asset = _to_float(getattr(row, "total_asset", None), 0.0)
        cash = _to_float(getattr(row, "cash", None), 0.0)
        market_value = _to_float(getattr(row, "market_value", None), 0.0)
        today_pnl_raw = _to_float(getattr(row, "today_pnl_raw", None), 0.0)
        total_pnl_raw = _to_float(getattr(row, "total_pnl_raw", None), 0.0)
        floating_pnl_raw = _to_float(getattr(row, "floating_pnl_raw", None), 0.0)

        inferred_day_open = total_asset - today_pnl_raw
        day_open_equity = inferred_day_open if inferred_day_open > 0 else total_asset
        inferred_month_open = total_asset - total_pnl_raw
        month_open_equity = inferred_month_open if inferred_month_open > 0 else total_asset

        payload = getattr(row, "payload_json", None)
        payload_json = payload if isinstance(payload, dict) else {}
        positions = payload_json.get("positions") if isinstance(payload_json, dict) else None
        position_count = len(positions) if isinstance(positions, list) else 0

        await upsert_real_account_daily_ledger(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
            snapshot_at=getattr(row, "snapshot_at", datetime.utcnow()),
            snapshot_date=snapshot_date,
            total_asset=total_asset,
            cash=cash,
            market_value=market_value,
            initial_equity=baseline_initial_equity if baseline_initial_equity > 0 else total_asset,
            day_open_equity=day_open_equity,
            month_open_equity=month_open_equity,
            today_pnl=today_pnl_raw,
            total_pnl=total_pnl_raw,
            floating_pnl=floating_pnl_raw,
            position_count=position_count,
            source="qmt_bridge_backfill",
            payload_json=payload_json,
        )
        upserted += 1

    return upserted
