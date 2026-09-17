import logging
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.real_account_snapshot import RealAccountSnapshot
from backend.services.trade.services.real_account_ledger_service import (
    upsert_real_account_daily_ledger,
)
from backend.services.trade.services.real_account_snapshot_guard import (
    extract_positions_count,
    is_effectively_empty_snapshot,
    is_inconsistent_zero_total_snapshot,
    is_suspicious_asset_jump,
)
from backend.shared.auth import get_internal_call_secret

router = APIRouter(
    prefix="/api/v1/internal/strategy", tags=["Internal Strategy Gateway"]
)
logger = logging.getLogger(__name__)

async def verify_internal_call(x_internal_call: str = Header(None)):
    """验证请求是否来自受信任的内部服务。

    C1 加固（2026-09-17）：密钥**逐次实时读取**（runtime.env 权威，轮换免重启）；
    无有效密钥（含公开默认值被拒后为空）→ 一律 401；日志只记尝试值的 sha8（M1：不落原文）。
    """
    import hashlib
    import secrets

    expected = get_internal_call_secret()
    if (
        not expected
        or not x_internal_call
        or not secrets.compare_digest(str(x_internal_call), expected)
    ):
        digest = hashlib.sha256(str(x_internal_call or "").encode()).hexdigest()[:8]
        logger.warning("Unauthorized internal call attempt (secret_sha8=%s)", digest)
        raise HTTPException(status_code=401, detail="Invalid internal secret")


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        import pytz

        shanghai = pytz.timezone("Asia/Shanghai")
        if value.tzinfo is None:
            # 假设原始时间是 UTC，先赋予 UTC 再转上海
            return value.replace(tzinfo=timezone.utc).astimezone(shanghai).isoformat()
        return value.astimezone(shanghai).isoformat()
    return None


_SH_TZ = ZoneInfo("Asia/Shanghai")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        if parsed == parsed:
            return parsed
    except Exception:
        pass
    return float(default)


def _compute_floating_from_positions(positions: list[dict[str, Any]]) -> float:
    floating = 0.0
    for pos in positions:
        vol = _to_float(pos.get("volume"), 0.0)
        last_price = _to_float(pos.get("last_price"), 0.0)
        cost_price = _to_float(pos.get("cost_price"), 0.0)
        if vol <= 0 or last_price <= 0 or cost_price <= 0:
            continue
        floating += (last_price - cost_price) * vol
    return floating


def _compute_position_win_rate(positions: list[dict[str, Any]]) -> float:
    wins = 0
    total = 0
    for pos in positions:
        vol = _to_float(pos.get("volume"), 0.0)
        if vol <= 0:
            continue
        last_price = _to_float(pos.get("last_price"), 0.0)
        cost_price = _to_float(pos.get("cost_price"), 0.0)
        if last_price <= 0 or cost_price <= 0:
            continue
        total += 1
        if last_price > cost_price:
            wins += 1
    if total <= 0:
        return 0.0
    return float(wins) * 100.0 / float(total)


async def _query_real_account_baseline(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
) -> dict[str, Any] | None:
    stmt = text(
        """
        SELECT
            initial_equity,
            first_snapshot_at,
            source
        FROM real_account_baselines
        WHERE tenant_id = :tenant_id
          AND user_id = :user_id
          AND account_id = :account_id
        LIMIT 1
        """
    )
    result = await db.execute(
        stmt,
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "account_id": account_id,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return {
        "initial_equity": _to_float(row.get("initial_equity"), 0.0),
        "first_snapshot_at": row.get("first_snapshot_at"),
        "source": row.get("source") or "qmt_bridge_first_report",
    }


async def _upsert_real_account_baseline(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    initial_equity: float,
    first_snapshot_at: datetime,
    source: str = "qmt_bridge_first_report",
) -> None:
    stmt = text(
        """
        INSERT INTO real_account_baselines (
            id,
            tenant_id,
            user_id,
            account_id,
            initial_equity,
            first_snapshot_at,
            source
        )
        VALUES (
            DEFAULT,
            :tenant_id,
            :user_id,
            :account_id,
            :initial_equity,
            :first_snapshot_at,
            :source
        )
        ON CONFLICT (tenant_id, user_id, account_id)
        DO UPDATE SET
            initial_equity = EXCLUDED.initial_equity,
            source = EXCLUDED.source,
            first_snapshot_at = LEAST(real_account_baselines.first_snapshot_at, EXCLUDED.first_snapshot_at)
        """
    )
    await db.execute(
        stmt,
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "account_id": account_id,
            "initial_equity": float(initial_equity),
            "first_snapshot_at": first_snapshot_at.astimezone(timezone.utc).replace(
                tzinfo=None
            )
            if first_snapshot_at.tzinfo is not None
            else first_snapshot_at,
            "source": source,
        },
    )


async def _persist_real_account_snapshot(
    *,
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    account_id: str,
    snapshot_at: datetime,
    total_asset: float,
    cash: float,
    market_value: float,
    payload_today_pnl: float,
    payload_total_pnl: float,
    payload_floating_pnl: float,
    payload_json: dict[str, Any] | None = None,
) -> tuple[float, float, float, bool, str | None]:
    def _previous_business_day(d):
        candidate = d - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate = candidate - timedelta(days=1)
        return candidate

    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
    local_dt = snapshot_at.astimezone(_SH_TZ)
    snapshot_date = local_dt.date()
    snapshot_month = local_dt.strftime("%Y-%m")

    baseline_row = await _query_real_account_baseline(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        account_id=account_id,
    )
    baseline_initial_equity = baseline_row["initial_equity"] if baseline_row else None

    valid_asset_filter = RealAccountSnapshot.total_asset > 1e-8

    async def _query_first_asset(*conditions: Any) -> float | None:
        stmt = (
            select(RealAccountSnapshot.total_asset)
            .where(
                RealAccountSnapshot.tenant_id == tenant_id,
                RealAccountSnapshot.user_id == user_id,
                RealAccountSnapshot.account_id == account_id,
                valid_asset_filter,
                *conditions,
            )
            .order_by(RealAccountSnapshot.snapshot_at.asc())
            .limit(1)
        )
        result = await db.execute(stmt)
        value = result.scalar_one_or_none()
        return _to_float(value, 0.0) if value is not None else None

    async def _query_prev_close_asset() -> float | None:
        stmt = (
            select(RealAccountSnapshot.total_asset)
            .where(
                RealAccountSnapshot.tenant_id == tenant_id,
                RealAccountSnapshot.user_id == user_id,
                RealAccountSnapshot.account_id == account_id,
                RealAccountSnapshot.snapshot_date < snapshot_date,
                valid_asset_filter,
            )
            .order_by(RealAccountSnapshot.snapshot_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        value = result.scalar_one_or_none()
        return _to_float(value, 0.0) if value is not None else None

    async def _query_latest_valid_snapshot_assets() -> dict[str, float] | None:
        stmt = (
            select(
                RealAccountSnapshot.total_asset,
                RealAccountSnapshot.cash,
                RealAccountSnapshot.market_value,
            )
            .where(
                RealAccountSnapshot.tenant_id == tenant_id,
                RealAccountSnapshot.user_id == user_id,
                RealAccountSnapshot.account_id == account_id,
                valid_asset_filter,
            )
            .order_by(
                RealAccountSnapshot.snapshot_at.desc(), RealAccountSnapshot.id.desc()
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return {
            "total_asset": _to_float(row[0], 0.0),
            "cash": _to_float(row[1], 0.0),
            "market_value": _to_float(row[2], 0.0),
        }

    latest_valid_stmt = (
        select(RealAccountSnapshot.id)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
            valid_asset_filter,
        )
        .order_by(RealAccountSnapshot.snapshot_at.desc(), RealAccountSnapshot.id.desc())
        .limit(1)
    )
    latest_valid_result = await db.execute(latest_valid_stmt)
    has_previous_valid_snapshot = latest_valid_result.scalar_one_or_none() is not None
    latest_valid_assets = (
        await _query_latest_valid_snapshot_assets()
        if has_previous_valid_snapshot
        else None
    )

    if is_inconsistent_zero_total_snapshot(
        total_asset=total_asset,
        cash=cash,
        market_value=market_value,
        payload_json=payload_json,
    ):
        initial_equity = (
            baseline_initial_equity
            if baseline_initial_equity is not None
            else await _query_first_asset()
        )
        day_open_equity = await _query_prev_close_asset()
        if day_open_equity is None:
            day_open_equity = await _query_first_asset(
                RealAccountSnapshot.snapshot_date == snapshot_date
            )
        month_open_equity = await _query_first_asset(
            RealAccountSnapshot.snapshot_month == snapshot_month
        )
        logger.warning(
            "Rejected inconsistent zero-total real-account snapshot: tenant=%s user=%s account=%s snapshot_at=%s "
            "total_asset=%.2f cash=%.2f market_value=%.2f positions=%d",
            tenant_id,
            user_id,
            account_id,
            snapshot_at.isoformat(),
            total_asset,
            cash,
            market_value,
            extract_positions_count(payload_json),
        )
        return (
            initial_equity if initial_equity is not None else total_asset,
            day_open_equity if day_open_equity is not None else total_asset,
            month_open_equity if month_open_equity is not None else total_asset,
            False,
            "rejected_inconsistent_zero_total_snapshot",
        )

    if (
        is_effectively_empty_snapshot(
            total_asset=total_asset,
            cash=cash,
            market_value=market_value,
            payload_json=payload_json,
        )
        and has_previous_valid_snapshot
    ):
        initial_equity = (
            baseline_initial_equity
            if baseline_initial_equity is not None
            else await _query_first_asset()
        )
        day_open_equity = await _query_prev_close_asset()
        if day_open_equity is None:
            day_open_equity = await _query_first_asset(
                RealAccountSnapshot.snapshot_date == snapshot_date
            )
        month_open_equity = await _query_first_asset(
            RealAccountSnapshot.snapshot_month == snapshot_month
        )
        logger.warning(
            "Rejected suspicious empty real-account snapshot: tenant=%s user=%s account=%s snapshot_at=%s",
            tenant_id,
            user_id,
            account_id,
            snapshot_at.isoformat(),
        )
        return (
            initial_equity if initial_equity is not None else total_asset,
            day_open_equity if day_open_equity is not None else total_asset,
            month_open_equity if month_open_equity is not None else total_asset,
            False,
            "rejected_empty_snapshot",
        )

    if latest_valid_assets and is_suspicious_asset_jump(
        total_asset=total_asset,
        cash=cash,
        market_value=market_value,
        prev_total_asset=latest_valid_assets.get("total_asset"),
        prev_cash=latest_valid_assets.get("cash"),
        prev_market_value=latest_valid_assets.get("market_value"),
        payload_json=payload_json,
    ):
        initial_equity = (
            baseline_initial_equity
            if baseline_initial_equity is not None
            else await _query_first_asset()
        )
        day_open_equity = await _query_prev_close_asset()
        if day_open_equity is None:
            day_open_equity = await _query_first_asset(
                RealAccountSnapshot.snapshot_date == snapshot_date
            )
        month_open_equity = await _query_first_asset(
            RealAccountSnapshot.snapshot_month == snapshot_month
        )
        logger.warning(
            "Rejected suspicious asset-jump real-account snapshot: tenant=%s user=%s account=%s snapshot_at=%s "
            "prev(total_asset=%.2f cash=%.2f market_value=%.2f) current(total_asset=%.2f cash=%.2f market_value=%.2f)",
            tenant_id,
            user_id,
            account_id,
            snapshot_at.isoformat(),
            latest_valid_assets.get("total_asset", 0.0),
            latest_valid_assets.get("cash", 0.0),
            latest_valid_assets.get("market_value", 0.0),
            total_asset,
            cash,
            market_value,
        )
        return (
            initial_equity if initial_equity is not None else total_asset,
            day_open_equity if day_open_equity is not None else total_asset,
            month_open_equity if month_open_equity is not None else total_asset,
            False,
            "rejected_asset_jump_snapshot",
        )

    # 一次性自动回填：若历史不存在“当前日之前快照”且券商提供了当日盈亏，
    # 则推导上一交易日收盘权益 = 当前总资产 - 当日盈亏，并写入 synthetic 快照。
    has_prev_stmt = (
        select(RealAccountSnapshot.id)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
            RealAccountSnapshot.snapshot_date < snapshot_date,
        )
        .order_by(RealAccountSnapshot.snapshot_at.desc())
        .limit(1)
    )
    has_prev_result = await db.execute(has_prev_stmt)
    has_prev_before_today = has_prev_result.scalar_one_or_none() is not None
    if (not has_prev_before_today) and abs(payload_today_pnl) > 1e-8:
        inferred_prev_close = total_asset - payload_today_pnl
        if inferred_prev_close > 0:
            prev_trade_date = _previous_business_day(snapshot_date)
            prev_close_local = datetime.combine(
                prev_trade_date, dt_time(15, 0, 0), tzinfo=_SH_TZ
            )
            prev_close_utc = prev_close_local.astimezone(timezone.utc).replace(
                tzinfo=None
            )
            synthetic_row = RealAccountSnapshot(
                tenant_id=tenant_id,
                user_id=user_id,
                account_id=account_id,
                snapshot_at=prev_close_utc,
                snapshot_date=prev_trade_date,
                snapshot_month=prev_trade_date.strftime("%Y-%m"),
                total_asset=float(inferred_prev_close),
                cash=float(cash),
                market_value=float(max(inferred_prev_close - cash, 0.0)),
                today_pnl_raw=0.0,
                total_pnl_raw=0.0,
                floating_pnl_raw=float(payload_floating_pnl),
                source="auto_backfill_prev_close",
            )
            db.add(synthetic_row)
            await db.flush()

    row = RealAccountSnapshot(
        tenant_id=tenant_id,
        user_id=user_id,
        account_id=account_id,
        snapshot_at=snapshot_at.astimezone(timezone.utc).replace(tzinfo=None),
        snapshot_date=snapshot_date,
        snapshot_month=snapshot_month,
        total_asset=total_asset,
        cash=cash,
        market_value=market_value,
        today_pnl_raw=payload_today_pnl,
        total_pnl_raw=payload_total_pnl,
        floating_pnl_raw=payload_floating_pnl,
        source="qmt_bridge",
        payload_json=payload_json or {},
    )
    db.add(row)
    await db.flush()

    if baseline_initial_equity is None:
        first_report_stmt = (
            select(RealAccountSnapshot.snapshot_at, RealAccountSnapshot.total_asset)
            .where(
                RealAccountSnapshot.tenant_id == tenant_id,
                RealAccountSnapshot.user_id == user_id,
                RealAccountSnapshot.account_id == account_id,
                RealAccountSnapshot.source == "qmt_bridge",
            )
            .order_by(
                RealAccountSnapshot.snapshot_at.asc(), RealAccountSnapshot.id.asc()
            )
            .limit(1)
        )
        first_report_result = await db.execute(first_report_stmt)
        first_report_row = first_report_result.first()
        if first_report_row is None:
            first_report_stmt = (
                select(RealAccountSnapshot.snapshot_at, RealAccountSnapshot.total_asset)
                .where(
                    RealAccountSnapshot.tenant_id == tenant_id,
                    RealAccountSnapshot.user_id == user_id,
                    RealAccountSnapshot.account_id == account_id,
                )
                .order_by(
                    RealAccountSnapshot.snapshot_at.asc(), RealAccountSnapshot.id.asc()
                )
                .limit(1)
            )
            first_report_result = await db.execute(first_report_stmt)
            first_report_row = first_report_result.first()
        if first_report_row is not None:
            first_snapshot_at = first_report_row[0] or snapshot_at
            first_initial_equity = _to_float(first_report_row[1], total_asset)
            await _upsert_real_account_baseline(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                account_id=account_id,
                initial_equity=first_initial_equity,
                first_snapshot_at=first_snapshot_at,
            )
            baseline_initial_equity = first_initial_equity

    initial_equity = (
        baseline_initial_equity
        if baseline_initial_equity is not None
        else await _query_first_asset()
    )
    # 今日基线优先取“上一交易日最后权益”；若不存在才退化到“今日首条快照”。
    prev_close_equity = await _query_prev_close_asset()
    day_open_equity = (
        prev_close_equity
        if prev_close_equity is not None
        else await _query_first_asset(
            RealAccountSnapshot.snapshot_date == snapshot_date
        )
    )
    month_open_equity = await _query_first_asset(
        RealAccountSnapshot.snapshot_month == snapshot_month
    )

    ledger_position_count = extract_positions_count(payload_json)

    await upsert_real_account_daily_ledger(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        account_id=account_id,
        snapshot_at=snapshot_at,
        snapshot_date=snapshot_date,
        total_asset=total_asset,
        cash=cash,
        market_value=market_value,
        initial_equity=initial_equity if initial_equity is not None else total_asset,
        day_open_equity=day_open_equity if day_open_equity is not None else total_asset,
        month_open_equity=month_open_equity
        if month_open_equity is not None
        else total_asset,
        today_pnl=payload_today_pnl,
        total_pnl=payload_total_pnl,
        floating_pnl=payload_floating_pnl,
        position_count=ledger_position_count,
        source="qmt_bridge",
        payload_json=payload_json or {},
    )

    return (
        initial_equity if initial_equity is not None else total_asset,
        day_open_equity if day_open_equity is not None else total_asset,
        month_open_equity if month_open_equity is not None else total_asset,
        True,
        None,
    )


async def _compute_account_metrics(
    *,
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    account_id: str,
    total_asset: float,
    cash: float,
    market_value: float,
    snapshot_at: datetime,
    payload_today_pnl: float,
    payload_total_pnl: float,
    payload_floating_pnl: float,
    positions: list[dict[str, Any]],
    payload_json: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now_local = (
        snapshot_at.astimezone(_SH_TZ) if snapshot_at.tzinfo else datetime.now(_SH_TZ)
    )
    (
        initial_equity,
        day_open_equity,
        month_open_equity,
        snapshot_persisted,
        snapshot_reject_reason,
    ) = await _persist_real_account_snapshot(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        account_id=account_id,
        snapshot_at=snapshot_at,
        total_asset=total_asset,
        cash=cash,
        market_value=market_value,
        payload_today_pnl=payload_today_pnl,
        payload_total_pnl=payload_total_pnl,
        payload_floating_pnl=payload_floating_pnl,
        payload_json=payload_json,
    )

    derived_today_pnl = total_asset - day_open_equity
    derived_total_pnl = total_asset - initial_equity
    derived_monthly_pnl = total_asset - month_open_equity
    derived_floating_pnl = _compute_floating_from_positions(positions)
    use_payload_today = abs(payload_today_pnl) > 1e-8
    use_payload_total = abs(payload_total_pnl) > 1e-8
    use_payload_floating = abs(payload_floating_pnl) > 1e-8

    final_today_pnl = payload_today_pnl if use_payload_today else derived_today_pnl
    final_total_pnl = payload_total_pnl if use_payload_total else derived_total_pnl
    final_floating_pnl = (
        payload_floating_pnl if use_payload_floating else derived_floating_pnl
    )
    final_total_return = (
        (final_total_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0
    )
    win_rate = _compute_position_win_rate(positions)

    metrics = {
        "today_pnl": float(final_today_pnl),
        "total_pnl": float(final_total_pnl),
        "floating_pnl": float(final_floating_pnl),
        "monthly_pnl": float(derived_monthly_pnl),
        "total_return": float(final_total_return),
        "win_rate": float(win_rate),
    }
    metrics_meta = {
        "today_pnl_source": "broker_raw"
        if use_payload_today
        else "computed_from_db_snapshot",
        "total_pnl_source": "broker_raw"
        if use_payload_total
        else "computed_from_db_snapshot",
        "floating_pnl_source": "broker_raw"
        if use_payload_floating
        else "computed_from_positions",
        "monthly_pnl_source": "computed_from_db_snapshot",
        "total_return_source": "broker_raw"
        if use_payload_total
        else "computed_from_db_snapshot",
        "today_pnl_available": True,
        "total_pnl_available": True,
        "floating_pnl_available": True,
        "monthly_pnl_available": True,
        "total_return_available": initial_equity > 0,
        "win_rate_available": True,
        "quality": "ok",
        "updated_at": now_local.isoformat(),
        "snapshot_persisted": bool(snapshot_persisted),
        "snapshot_reject_reason": snapshot_reject_reason,
        "baseline": {
            "initial_equity": float(initial_equity),
            "day_open_equity": float(day_open_equity),
            "month_open_equity": float(month_open_equity),
            "baseline_date": now_local.date().isoformat(),
            "baseline_month": now_local.strftime("%Y-%m"),
        },
    }
    return metrics, metrics_meta
