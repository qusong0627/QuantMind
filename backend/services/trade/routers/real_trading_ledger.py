from datetime import date, datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.deps import AuthContext, get_auth_context, get_db
from backend.services.trade.services.real_account_ledger_service import (
    backfill_daily_ledgers_from_snapshots,
    list_real_account_daily_ledgers,
)

from .real_trading_utils import (
    _fetch_latest_real_account_snapshot,
    _normalize_identity,
    _fetch_real_account_baseline,
    _upsert_real_account_baseline
)
from backend.services.trade.portfolio.models import Portfolio
from backend.services.trade.models.real_account_ledger import RealAccountLedgerDailySnapshot
from sqlalchemy import and_, select

router = APIRouter()


class RealAccountLedgerDailySnapshotResponse(BaseModel):
    account_id: str
    snapshot_date: date
    last_snapshot_at: datetime | None = None
    snapshot_kind: Literal["daily_ledger"] = "daily_ledger"
    total_asset: float
    cash: float
    market_value: float
    initial_equity: float
    day_open_equity: float
    month_open_equity: float
    broker_today_pnl_raw: float
    today_pnl_raw: float
    monthly_pnl_raw: float
    total_pnl_raw: float
    floating_pnl_raw: float
    daily_pnl: float
    monthly_pnl: float
    total_pnl: float
    floating_pnl: float
    daily_return_pct: float
    total_return_pct: float
    daily_return_ratio: float
    total_return_ratio: float
    baseline: dict
    position_count: int
    settlement_finalized: bool = False
    settlement_finalized_at: str | None = None
    settlement_snapshot_count: int = 0
    source: str


class RealAccountSettingsResponse(BaseModel):
    initial_equity: float
    last_modified_at: str | None = None
    can_modify: bool = True


class RealAccountSettingsRequest(BaseModel):
    initial_equity: float


@router.get("/account/ledger/daily", response_model=list[RealAccountLedgerDailySnapshotResponse])
async def get_account_daily_ledger(
    days: int = Query(default=30, ge=1, le=3650),
    account_id: str | None = Query(default=None),
    tenant_id: str | None = None,
    user_id: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    resolved_user_id, resolved_tenant_id = _normalize_identity(auth, user_id=user_id, tenant_id=tenant_id)
    current_snapshot = await _fetch_latest_real_account_snapshot(
        db,
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
    )
    resolved_account_id = str(current_snapshot.get("account_id") or "").strip() if current_snapshot else ""
    target_account_id = str(account_id or resolved_account_id or "").strip()

    rows = await list_real_account_daily_ledgers(
        db,
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
        account_id=target_account_id or None,
        days=days,
    )

    if not rows and target_account_id:
        await backfill_daily_ledgers_from_snapshots(
            db,
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
            account_id=target_account_id,
            days=days,
        )
        await db.commit()
        rows = await list_real_account_daily_ledgers(
            db,
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
            account_id=target_account_id,
            days=days,
        )

    if not rows:
        return []

    return [
        RealAccountLedgerDailySnapshotResponse(
            account_id=str(row.account_id or target_account_id or resolved_account_id or ""),
            snapshot_date=row.snapshot_date,
            last_snapshot_at=row.last_snapshot_at,
            snapshot_kind="daily_ledger",
            total_asset=float(row.total_asset or 0.0),
            cash=float(row.cash or 0.0),
            market_value=float(row.market_value or 0.0),
            initial_equity=float(row.initial_equity or 0.0),
            day_open_equity=float(row.day_open_equity or 0.0),
            month_open_equity=float(row.month_open_equity or 0.0),
            broker_today_pnl_raw=float(row.today_pnl_raw or 0.0),
            today_pnl_raw=float(row.today_pnl_raw or 0.0),
            monthly_pnl_raw=float(row.monthly_pnl_raw or 0.0),
            total_pnl_raw=float(row.total_pnl_raw or 0.0),
            floating_pnl_raw=float(row.floating_pnl_raw or 0.0),
            daily_pnl=(
                float((row.total_asset or 0.0) - (row.day_open_equity or 0.0))
                if float(row.day_open_equity or 0.0) > 0
                else float(row.today_pnl_raw or 0.0)
            ),
            monthly_pnl=(
                float((row.total_asset or 0.0) - (row.month_open_equity or 0.0))
                if float(row.month_open_equity or 0.0) > 0
                else float(row.monthly_pnl_raw or 0.0)
            ),
            total_pnl=float(row.total_pnl_raw or 0.0),
            floating_pnl=float(row.floating_pnl_raw or 0.0),
            daily_return_pct=float(row.daily_return_pct or 0.0),
            total_return_pct=float(row.total_return_pct or 0.0),
            daily_return_ratio=float((row.daily_return_pct or 0.0) / 100.0),
            total_return_ratio=float((row.total_return_pct or 0.0) / 100.0),
            baseline={
                "initial_equity": float(row.initial_equity or 0.0),
                "day_open_equity": float(row.day_open_equity or 0.0),
                "month_open_equity": float(row.month_open_equity or 0.0),
            },
            position_count=int(row.position_count or 0),
            settlement_finalized=bool((row.payload_json or {}).get("settlement_finalized")),
            settlement_finalized_at=(row.payload_json or {}).get("settlement_finalized_at"),
            settlement_snapshot_count=int((row.payload_json or {}).get("settlement_snapshot_count") or 0),
            source=str(row.source or "qmt_bridge"),
        )
        for row in rows
    ]


@router.get("/account/settings", response_model=RealAccountSettingsResponse)
async def get_real_account_settings(
    tenant_id: str | None = None,
    user_id: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    resolved_user_id, resolved_tenant_id = _normalize_identity(auth, user_id=user_id, tenant_id=tenant_id)
    current_snapshot = await _fetch_latest_real_account_snapshot(
        db,
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
    )
    resolved_account_id = str((current_snapshot or {}).get("account_id") or resolved_user_id or "").strip()

    baseline = await _fetch_real_account_baseline(
        db,
        tenant_id=resolved_tenant_id,
        user_id=resolved_user_id,
        account_id=resolved_account_id,
    )
    
    initial_equity = float(baseline["initial_equity"]) if baseline else 0.0
    last_modified_at = baseline["first_snapshot_at"].isoformat() if baseline and baseline.get("first_snapshot_at") else None
    
    return RealAccountSettingsResponse(
        initial_equity=initial_equity,
        last_modified_at=last_modified_at,
        can_modify=True
    )


@router.put("/account/settings")
async def update_real_account_settings(
    request: RealAccountSettingsRequest,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    手动更新实盘账户的平台基准（仅用于修正平台统计盈亏，不影响券商实际资金）。
    """
    resolved_user_id, resolved_tenant_id = _normalize_identity(
        auth, user_id=user_id, tenant_id=tenant_id
    )

    # 1. 持久化到基线表 (real_account_baselines)
    try:
        current_snapshot_data = await _fetch_latest_real_account_snapshot(
            db, tenant_id=resolved_tenant_id, user_id=resolved_user_id
        )
        resolved_account_id = str((current_snapshot_data or {}).get("account_id") or resolved_user_id or "").strip()
        if not resolved_account_id:
            raise HTTPException(status_code=400, detail="未检测到该用户的实盘账户 ID，请确保 QMT Agent 已正常上报过一次数据")

        await _upsert_real_account_baseline(
            db,
            tenant_id=resolved_tenant_id,
            user_id=resolved_user_id,
            account_id=resolved_account_id,
            initial_equity=request.initial_equity,
            first_snapshot_at=datetime.utcnow(),
            source="manual_update"
        )
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to update real account baseline: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    # 2. 同时同步到活跃 Portfolio
    try:
        from sqlalchemy import update
        stmt = (
            update(Portfolio)
            .where(
                and_(
                    Portfolio.tenant_id == resolved_tenant_id,
                    Portfolio.user_id == resolved_user_id,
                    Portfolio.mode == "REAL"
                )
            )
            .values(initial_capital=request.initial_equity, updated_at=datetime.utcnow())
        )
        await db.execute(stmt)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to sync initial_equity to portfolio: %s", e)

    # 3. 立即刷新当日账本中的累计盈亏数据
    try:
        sh_tz = ZoneInfo("Asia/Shanghai")
        today_date = datetime.now(sh_tz).date()
        
        ledger_stmt = select(RealAccountLedgerDailySnapshot).where(
            and_(
                RealAccountLedgerDailySnapshot.tenant_id == resolved_tenant_id,
                RealAccountLedgerDailySnapshot.user_id == resolved_user_id,
                RealAccountLedgerDailySnapshot.account_id == resolved_account_id,
                RealAccountLedgerDailySnapshot.snapshot_date == today_date
            )
        ).limit(1)
        res_ledger = await db.execute(ledger_stmt)
        ledger = res_ledger.scalar_one_or_none()
        
        if ledger:
            total_asset = float(ledger.total_asset or 0.0)
            initial_equity = float(request.initial_equity)
            new_cumulative_pnl = total_asset - initial_equity
            new_total_return_pct = (new_cumulative_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0
            
            ledger.initial_equity = initial_equity
            ledger.total_return_pct = new_total_return_pct
            ledger.updated_at = datetime.utcnow()
            db.add(ledger)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to sync initial_equity to daily ledger: %s", e)

    await db.commit()
    return {"status": "success", "message": "实盘统计基准已更新并同步到当日账本"}


