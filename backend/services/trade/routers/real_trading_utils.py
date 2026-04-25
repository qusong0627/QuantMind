import asyncio
import json
import logging
import os
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import bindparam, desc, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

import redis as redis_lib
from backend.services.trade.deps import AuthContext, get_auth_context, get_db
from backend.services.trade.models.order import Order
from backend.services.trade.models.preflight_snapshot import PreflightSnapshot
from backend.services.trade.models.real_account_snapshot import RealAccountSnapshot
from backend.services.trade.portfolio.models import Portfolio
from backend.services.trade.models.trade import Trade
from backend.services.trade.redis_client import RedisClient, get_redis
from backend.services.trade.utils.redis_cache import redis_cache
from backend.services.trade.schemas.live_trade_config import (
    ExecutionConfigSchema,
    LiveTradeConfigSchema,
)
from backend.services.trade.services.k8s_manager import k8s_manager
from backend.services.trade.services.real_account_snapshot_guard import (
    is_effectively_empty_snapshot,
    is_inconsistent_zero_total_snapshot,
)
from backend.services.trade.services.trading_precheck_service import (
    _check_inference_model_exists,
    run_trading_readiness_precheck,
)
from backend.services.trade.trade_config import settings
from backend.shared.margin_stock_pool import get_margin_stock_pool_service
from backend.shared.notification_publisher import publish_notification_async
from backend.shared.strategy_storage import get_strategy_storage_service

router = APIRouter()
logger = logging.getLogger(__name__)
REAL_ACCOUNT_SNAPSHOT_VIEW_NAME = "real_account_snapshot_overview_v"


def _select_latest_usable_snapshot_row(
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not rows:
        return None
    for row in rows:
        if is_effectively_empty_snapshot(
            total_asset=row.get("total_asset"),
            cash=row.get("cash"),
            market_value=row.get("market_value"),
            payload_json=row.get("payload_json"),
        ):
            continue
        if is_inconsistent_zero_total_snapshot(
            total_asset=row.get("total_asset"),
            cash=row.get("cash"),
            market_value=row.get("market_value"),
            payload_json=row.get("payload_json"),
        ):
            continue
        if float(row.get("total_asset") or 0.0) <= 1e-8:
            continue
        return row
    return None


def _build_real_account_contract(
    *,
    user_id: str,
    tenant_id: str,
    account_id: str,
    snapshot_at: str | None,
    snapshot_date: str | None,
    snapshot_month: str | None,
    total_asset: float,
    cash: float,
    market_value: float,
    broker_today_pnl_raw: float,
    total_pnl_raw: float,
    floating_pnl_raw: float,
    initial_equity: float,
    day_open_equity: float,
    month_open_equity: float,
    source: str,
    payload_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload_json if isinstance(payload_json, dict) else {}
    positions = payload.get("positions") or []

    daily_pnl = (
        total_asset - day_open_equity if day_open_equity > 0 else broker_today_pnl_raw
    )
    monthly_pnl = total_asset - month_open_equity if month_open_equity > 0 else 0.0

    # 核心变动：将“总盈亏”定义为“相比平台基准的累计盈亏”
    cumulative_pnl = (
        total_asset - initial_equity
        if initial_equity > 0
        else float(total_pnl_raw or 0.0)
    )
    total_pnl = cumulative_pnl
    broker_total_pnl = float(total_pnl_raw or 0.0)
    floating_pnl = float(floating_pnl_raw or 0.0)
    realized_pnl = cumulative_pnl - floating_pnl

    daily_return_pct = (
        (daily_pnl / day_open_equity * 100.0) if day_open_equity > 0 else 0.0
    )
    total_return_pct = (
        (cumulative_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0
    )

    return {
        "snapshot_kind": "account_snapshot",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "snapshot_at": snapshot_at,
        "snapshot_date": snapshot_date,
        "snapshot_month": snapshot_month,
        "total_asset": float(total_asset or 0.0),
        "available_cash": float(cash or 0.0),
        "cash": float(cash or 0.0),
        "market_value": float(market_value or 0.0),
        "broker_today_pnl_raw": float(broker_today_pnl_raw or 0.0),
        "today_pnl_raw": float(broker_today_pnl_raw or 0.0),
        "total_pnl_raw": total_pnl,
        "broker_total_pnl": broker_total_pnl,
        "cumulative_pnl": cumulative_pnl,
        "realized_pnl": realized_pnl,
        "floating_pnl_raw": floating_pnl,
        "today_pnl": float(daily_pnl),
        "daily_pnl": float(daily_pnl),
        "monthly_pnl": float(monthly_pnl),
        "total_pnl": total_pnl,
        "floating_pnl": floating_pnl,
        # 兼容旧字段：保留 daily_return/total_return 为“百分数口径”
        "daily_return": float(daily_return_pct),
        "total_return": float(total_return_pct),
        "daily_return_pct": float(daily_return_pct),
        "total_return_pct": float(total_return_pct),
        "daily_return_ratio": float(daily_return_pct / 100.0),
        "total_return_ratio": float(total_return_pct / 100.0),
        "initial_equity": float(initial_equity or 0.0),
        "day_open_equity": float(day_open_equity or 0.0),
        "month_open_equity": float(month_open_equity or 0.0),
        "baseline": {
            "initial_equity": float(initial_equity or 0.0),
            "day_open_equity": float(day_open_equity or 0.0),
            "month_open_equity": float(month_open_equity or 0.0),
        },
        "is_online": True,
        "source": source or "qmt_bridge",
        "payload_json": payload,
        "positions": positions,
        "position_count": len(positions)
        if isinstance(positions, list)
        else len(positions or []),
    }


class TradingPrecheckItem(BaseModel):
    key: str
    label: str
    passed: bool
    detail: str


class TradingPrecheckResponse(BaseModel):
    passed: bool
    checked_at: str
    items: list[TradingPrecheckItem]
    trading_permission: str | None = None
    signal_readiness: dict[str, Any] | None = None


# 策略文件存储基准路径
SHARED_STORAGE_PATH = os.path.abspath("userdata/strategies")


def get_strategy_path(user_id: str):
    return os.path.join(SHARED_STORAGE_PATH, user_id)


def _active_strategy_key(tenant_id: str, user_id: str) -> str:
    tenant = (tenant_id or "").strip() or "default"
    return f"trade:active_strategy:{tenant}:{str(user_id).zfill(8)}"


def _normalize_identity(
    auth: AuthContext,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[str, str]:
    """
    统一身份来源：JWT 为准；兼容传参时必须与 JWT 一致。
    """
    token_user_id = str(auth.user_id).strip()
    token_tenant_id = str(auth.tenant_id or "default").strip() or "default"

    if user_id is not None and str(user_id).strip() != token_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden user_id override",
        )
    if tenant_id is not None and str(tenant_id).strip() != token_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden tenant_id override",
        )

    return str(token_user_id).zfill(8), token_tenant_id


async def _fetch_active_portfolio_snapshot(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str | None,
    mode: str | None = None,
) -> dict | None:
    sid = str(strategy_id or "").strip()

    # user_id 在数据库中是 VARCHAR 类型，直接使用字符串查询
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return None

    # 有 strategy_id 时精确匹配；否则取该用户最近的 portfolio
    base_where = [
        Portfolio.tenant_id == tenant_id,
        Portfolio.user_id == normalized_user_id,
        Portfolio.is_deleted.is_(False),
    ]
    if sid:
        try:
            strategy_id_int = int(sid)
            base_where.append(Portfolio.strategy_id == strategy_id_int)
        except ValueError:
            # 非整数 ID（如系统模板 sys_xxx），跳过 strategy_id 精确匹配，
            # 取该用户最近的活跃组合即可
            pass

    # 增加交易模式过滤，防止实盘与模拟数据混淆
    if mode:
        normalized_mode = str(mode).strip().upper()
        if normalized_mode in {"REAL", "SHADOW", "SIMULATION"}:
            base_where.append(Portfolio.trading_mode == normalized_mode)

    stmt = (
        select(Portfolio)
        .options(selectinload(Portfolio.positions))
        .where(*base_where)
        .order_by(
            desc(Portfolio.run_status == "running"),
            desc(Portfolio.updated_at),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    portfolio = result.scalars().first()
    if portfolio is None:
        return None

    def _decimal_to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            result_value = float(value)
            return result_value if result_value == result_value else default
        except Exception:
            return default

    initial_capital = _decimal_to_float(
        getattr(portfolio, "initial_capital", None), 0.0
    )
    daily_pnl = _decimal_to_float(getattr(portfolio, "daily_pnl", None), 0.0)
    total_value = _decimal_to_float(getattr(portfolio, "total_value", None), 0.0)
    available_cash = _decimal_to_float(getattr(portfolio, "available_cash", None), 0.0)
    frozen_cash = _decimal_to_float(getattr(portfolio, "frozen_cash", None), 0.0)
    # 持仓市值 = 总资产 - 可用现金 - 冻结资金 (简化计算，也可根据 positions 累加)
    market_value = total_value - available_cash - frozen_cash

    # 重新计算当日收益率以确保实时性 (优先使用持仓市值作为分母)
    if market_value > 0:
        raw_daily_return = daily_pnl / market_value
    else:
        # 兜底逻辑：若无持仓市值，尝试从数据库获取或使用初始资金计算
        raw_daily_return = getattr(portfolio, "daily_return", 0.0)
        if (not raw_daily_return or raw_daily_return == 0) and initial_capital > 0:
            raw_daily_return = daily_pnl / initial_capital
        elif not raw_daily_return:
            raw_daily_return = 0.0

    total_pnl = _decimal_to_float(getattr(portfolio, "total_pnl", None), 0.0)
    total_return = _decimal_to_float(getattr(portfolio, "total_return", None), 0.0)

    return {
        "portfolio_id": portfolio.id,
        "daily_pnl": daily_pnl,
        "daily_return": _decimal_to_float(raw_daily_return, 0.0) * 100.0,
        "total_pnl": total_pnl,
        "total_return": total_return * 100.0,
        "total_value": total_value,
        "initial_capital": initial_capital,
        "run_status": getattr(portfolio, "run_status", None),
        "position_count": len(
            [p for p in portfolio.positions if p.status == "holding"]
        ),
        "updated_at": portfolio.updated_at.isoformat()
        if getattr(portfolio, "updated_at", None)
        else None,
    }


async def _fetch_real_account_baseline(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
) -> dict[str, Any] | None:
    stmt = text(
        """
        SELECT initial_equity, first_snapshot_at, source
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
        "initial_equity": float(row.get("initial_equity") or 0.0),
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
    source: str = "manual_update",
) -> None:
    stmt = text(
        """
        INSERT INTO real_account_baselines (
            tenant_id,
            user_id,
            account_id,
            initial_equity,
            first_snapshot_at,
            source
        )
        VALUES (
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
    await db.commit()


async def _fetch_latest_real_account_snapshot(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return None

    candidate_user_ids = {normalized_user_id}
    if normalized_user_id.isdigit():
        candidate_user_ids.add(str(int(normalized_user_id)))
        candidate_user_ids.add(normalized_user_id.zfill(8))

    candidate_ids = sorted(candidate_user_ids)
    view_stmt = text(
        f"""
            SELECT
                id,
                tenant_id,
                user_id,
                account_id,
                snapshot_at,
                snapshot_date,
                snapshot_month,
                total_asset,
                cash,
                market_value,
                today_pnl_raw,
                total_pnl_raw,
                floating_pnl_raw,
                initial_equity,
                day_open_equity,
                month_open_equity,
                source,
                payload_json
            FROM {REAL_ACCOUNT_SNAPSHOT_VIEW_NAME}
            WHERE tenant_id = :tenant_id
              AND user_id IN :user_ids
            ORDER BY snapshot_at DESC, id DESC
            LIMIT 20
            """
    ).bindparams(bindparam("user_ids", expanding=True))
    try:
        result = await db.execute(
            view_stmt, {"tenant_id": tenant_id, "user_ids": candidate_ids}
        )
        snapshots = list(result.mappings().all())
        snapshot = _select_latest_usable_snapshot_row(snapshots)
        if snapshot is not None:
            payload_json = snapshot.get("payload_json") or {}
            if not isinstance(payload_json, dict):
                payload_json = {}
            total_asset = float(snapshot.get("total_asset") or 0.0)
            day_open_equity = float(snapshot.get("day_open_equity") or 0.0)
            month_open_equity = float(snapshot.get("month_open_equity") or 0.0)
            initial_equity = float(snapshot.get("initial_equity") or 0.0)
            today_pnl_raw = float(snapshot.get("today_pnl_raw") or 0.0)
            return _build_real_account_contract(
                user_id=str(snapshot.get("user_id") or normalized_user_id),
                tenant_id=str(snapshot.get("tenant_id") or tenant_id),
                account_id=str(snapshot.get("account_id") or normalized_user_id),
                snapshot_at=snapshot["snapshot_at"].isoformat()
                if snapshot.get("snapshot_at")
                else None,
                snapshot_date=snapshot["snapshot_date"].isoformat()
                if snapshot.get("snapshot_date")
                else None,
                snapshot_month=snapshot.get("snapshot_month"),
                total_asset=total_asset,
                cash=float(snapshot.get("cash") or 0.0),
                market_value=float(snapshot.get("market_value") or 0.0),
                broker_today_pnl_raw=today_pnl_raw,
                total_pnl_raw=float(snapshot.get("total_pnl_raw") or 0.0),
                floating_pnl_raw=float(snapshot.get("floating_pnl_raw") or 0.0),
                initial_equity=initial_equity,
                day_open_equity=day_open_equity,
                month_open_equity=month_open_equity,
                source=str(snapshot.get("source") or "qmt_bridge"),
                payload_json=payload_json,
            )
    except Exception as exc:
        logger.warning(
            "real account snapshot view unavailable, fallback to table query: tenant=%s user=%s err=%s",
            tenant_id,
            normalized_user_id,
            exc,
        )
        try:
            await db.rollback()
        except Exception as rollback_exc:
            logger.warning(
                "real account snapshot view fallback rollback failed: tenant=%s user=%s err=%s",
                tenant_id,
                normalized_user_id,
                rollback_exc,
            )

    stmt = (
        select(RealAccountSnapshot)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id.in_(candidate_ids),
        )
        .order_by(desc(RealAccountSnapshot.snapshot_at), desc(RealAccountSnapshot.id))
        .limit(20)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    snapshot = None
    for candidate in rows:
        if is_effectively_empty_snapshot(
            total_asset=getattr(candidate, "total_asset", 0.0),
            cash=getattr(candidate, "cash", 0.0),
            market_value=getattr(candidate, "market_value", 0.0),
            payload_json=getattr(candidate, "payload_json", None),
        ):
            continue
        if is_inconsistent_zero_total_snapshot(
            total_asset=getattr(candidate, "total_asset", 0.0),
            cash=getattr(candidate, "cash", 0.0),
            market_value=getattr(candidate, "market_value", 0.0),
            payload_json=getattr(candidate, "payload_json", None),
        ):
            continue
        if float(getattr(candidate, "total_asset", 0.0) or 0.0) <= 1e-8:
            continue
        snapshot = candidate
        break
    if snapshot is None:
        return None

    baseline_row = await _fetch_real_account_baseline(
        db,
        tenant_id=tenant_id,
        user_id=snapshot.user_id,
        account_id=snapshot.account_id,
    )
    initial_equity = (
        float(baseline_row["initial_equity"])
        if baseline_row is not None
        else float(snapshot.total_asset or 0.0)
    )
    valid_asset_filter = RealAccountSnapshot.total_asset > 1e-8

    prev_close_stmt = (
        select(RealAccountSnapshot.total_asset)
        .where(
            RealAccountSnapshot.tenant_id == snapshot.tenant_id,
            RealAccountSnapshot.user_id == snapshot.user_id,
            RealAccountSnapshot.account_id == snapshot.account_id,
            RealAccountSnapshot.snapshot_date < snapshot.snapshot_date,
            valid_asset_filter,
        )
        .order_by(desc(RealAccountSnapshot.snapshot_at), desc(RealAccountSnapshot.id))
        .limit(1)
    )
    prev_close_result = await db.execute(prev_close_stmt)
    prev_close_equity = prev_close_result.scalar_one_or_none()

    same_day_first_stmt = (
        select(RealAccountSnapshot.total_asset)
        .where(
            RealAccountSnapshot.tenant_id == snapshot.tenant_id,
            RealAccountSnapshot.user_id == snapshot.user_id,
            RealAccountSnapshot.account_id == snapshot.account_id,
            RealAccountSnapshot.snapshot_date == snapshot.snapshot_date,
            valid_asset_filter,
        )
        .order_by(RealAccountSnapshot.snapshot_at.asc(), RealAccountSnapshot.id.asc())
        .limit(1)
    )
    same_day_first_result = await db.execute(same_day_first_stmt)
    same_day_first_equity = same_day_first_result.scalar_one_or_none()

    month_first_stmt = (
        select(RealAccountSnapshot.total_asset)
        .where(
            RealAccountSnapshot.tenant_id == snapshot.tenant_id,
            RealAccountSnapshot.user_id == snapshot.user_id,
            RealAccountSnapshot.account_id == snapshot.account_id,
            RealAccountSnapshot.snapshot_month == snapshot.snapshot_month,
            valid_asset_filter,
        )
        .order_by(RealAccountSnapshot.snapshot_at.asc(), RealAccountSnapshot.id.asc())
        .limit(1)
    )
    month_first_result = await db.execute(month_first_stmt)
    month_first_equity = month_first_result.scalar_one_or_none()

    day_open_equity = float(
        prev_close_equity
        if prev_close_equity is not None
        else (same_day_first_equity or 0.0)
    )
    month_open_equity = float(month_first_equity or 0.0)
    return _build_real_account_contract(
        user_id=str(snapshot.user_id),
        tenant_id=str(snapshot.tenant_id),
        account_id=str(snapshot.account_id),
        snapshot_at=snapshot.snapshot_at.isoformat() if snapshot.snapshot_at else None,
        snapshot_date=snapshot.snapshot_date.isoformat()
        if snapshot.snapshot_date
        else None,
        snapshot_month=snapshot.snapshot_month,
        total_asset=float(snapshot.total_asset or 0.0),
        cash=float(snapshot.cash or 0.0),
        market_value=float(snapshot.market_value or 0.0),
        broker_today_pnl_raw=float(snapshot.today_pnl_raw or 0.0),
        total_pnl_raw=float(snapshot.total_pnl_raw or 0.0),
        floating_pnl_raw=float(snapshot.floating_pnl_raw or 0.0),
        initial_equity=initial_equity,
        day_open_equity=day_open_equity,
        month_open_equity=month_open_equity,
        source=str(snapshot.source or "qmt_bridge"),
        payload_json=snapshot.payload_json or {},
    )


async def _writeback_strategy_lifecycle_status(
    *,
    strategy_id: str | None,
    user_id: str,
    lifecycle_status: str,
    retries: int = 2,
) -> None:
    sid = str(strategy_id or "").strip()
    if not sid or not sid.isdigit():
        return
    svc = get_strategy_storage_service()
    for attempt in range(retries + 1):
        try:
            ok = await run_in_threadpool(
                svc.update_lifecycle_status,
                sid,
                user_id,
                lifecycle_status,
            )
            if not ok:
                logger.warning(
                    "策略状态回写未命中记录 strategy_id=%s user_id=%s target=%s",
                    sid,
                    user_id,
                    lifecycle_status,
                )
            return
        except Exception as e:
            logger.warning(
                "策略状态回写失败 strategy_id=%s user_id=%s target=%s attempt=%s err=%s",
                sid,
                user_id,
                lifecycle_status,
                attempt + 1,
                e,
            )
            if attempt < retries:
                await asyncio.sleep(0.25 * (attempt + 1))


def _schedule_status_writeback(
    *,
    strategy_id: str | None,
    user_id: str,
    lifecycle_status: str,
) -> None:
    async def _runner() -> None:
        await _writeback_strategy_lifecycle_status(
            strategy_id=strategy_id,
            user_id=user_id,
            lifecycle_status=lifecycle_status,
        )

    task = asyncio.create_task(_runner())
    task.add_done_callback(
        lambda t: (
            logger.error("策略状态回写后台任务异常: %s", t.exception(), exc_info=True)
            if t.exception()
            else None
        )
    )


def _schedule_user_notification(
    *,
    user_id: str,
    tenant_id: str,
    title: str,
    content: str,
    type: str = "trading",
    level: str = "info",
    action_url: str | None = None,
    check_preference: bool = False,
) -> None:
    """
    调度用户通知（异步后台任务）

    Args:
        check_preference: 是否检查用户通知偏好（默认不检查，确保关键通知送达）
    """

    async def _runner() -> None:
        if check_preference:
            try:
                from backend.shared.notification_preference import (
                    should_send_notification,
                )
                from backend.shared.database_manager_v2 import get_session

                async with get_session(read_only=True) as session:
                    should_send = await should_send_notification(
                        session=session,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        notification_type=type,
                    )
                if not should_send:
                    logger.debug(
                        "Notification skipped due to user preference: user=%s type=%s title=%s",
                        user_id,
                        type,
                        title,
                    )
                    return
            except Exception as e:
                logger.warning(
                    "Failed to check notification preference, sending anyway: %s", e
                )

        await publish_notification_async(
            user_id=user_id,
            tenant_id=tenant_id,
            title=title,
            content=content,
            type=type,
            level=level,
            action_url=action_url,
        )

    task = asyncio.create_task(_runner())
    task.add_done_callback(
        lambda t: (
            logger.warning("通知发布后台任务异常: %s", t.exception())
            if t.exception()
            else None
        )
    )


def _parse_user_id(raw_user_id: str) -> str:
    """获取用户ID (字符串类型，兼容 'admin' 等非数字ID)"""
    if not raw_user_id:
        raise HTTPException(status_code=400, detail="Invalid user_id in token")
    return raw_user_id


def _normalize_execution_config(user_exec_cfg: dict, base_exec_cfg: dict) -> dict:
    """
    合并并校验执行风控参数。单位均为小数（例如 -0.03 表示 -3%）。
    """
    merged = dict(base_exec_cfg or {})
    merged.update(user_exec_cfg or {})

    # 日内大跌拦截: [-10%, -1%]
    if "max_buy_drop" in merged:
        try:
            max_buy_drop = float(merged["max_buy_drop"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="execution_config.max_buy_drop 非法"
            )
        if not (-0.10 <= max_buy_drop <= -0.01):
            raise HTTPException(
                status_code=400,
                detail="execution_config.max_buy_drop 超出范围[-0.10, -0.01]",
            )
        merged["max_buy_drop"] = max_buy_drop

    # 全局止损触发: [-20%, -3%]
    if "stop_loss" in merged:
        try:
            stop_loss = float(merged["stop_loss"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="execution_config.stop_loss 非法"
            )
        if not (-0.20 <= stop_loss <= -0.03):
            raise HTTPException(
                status_code=400,
                detail="execution_config.stop_loss 超出范围[-0.20, -0.03]",
            )
        merged["stop_loss"] = stop_loss

    return merged


def _default_execution_config() -> dict:
    return {"max_buy_drop": -0.03, "stop_loss": -0.08}


def _default_live_trade_config() -> dict:
    return {
        "rebalance_days": 3,
        "schedule_type": "interval",
        "trade_weekdays": [],
        "enabled_sessions": ["PM"],
        "sell_time": "14:30",
        "buy_time": "14:45",
        "sell_first": True,
        "order_type": "LIMIT",
        "max_price_deviation": 0.02,
        "max_orders_per_cycle": 20,
    }


def _normalize_live_trade_config(user_live_cfg: dict, base_live_cfg: dict) -> dict:
    merged = dict(_default_live_trade_config())
    merged.update(base_live_cfg or {})
    merged.update(user_live_cfg or {})

    try:
        LiveTradeConfigSchema.model_validate(merged)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"live_trade_config 非法: {exc}")

    normalized = dict(merged)
    normalized["schedule_type"] = str(
        normalized.get("schedule_type") or "interval"
    ).lower()
    normalized["trade_weekdays"] = [
        str(item).upper() for item in (normalized.get("trade_weekdays") or [])
    ]
    normalized["enabled_sessions"] = [
        str(item).upper() for item in (normalized.get("enabled_sessions") or [])
    ]
    normalized["order_type"] = str(normalized.get("order_type") or "LIMIT").upper()
    normalized["sell_first"] = bool(normalized.get("sell_first", True))
    normalized["rebalance_days"] = int(normalized.get("rebalance_days") or 3)
    normalized["max_orders_per_cycle"] = int(
        normalized.get("max_orders_per_cycle") or 20
    )
    if (
        "max_price_deviation" in normalized
        and normalized["max_price_deviation"] is not None
    ):
        normalized["max_price_deviation"] = float(normalized["max_price_deviation"])

    session_ranges = {
        "AM": ("09:30", "11:30"),
        "PM": ("13:00", "15:00"),
    }
    enabled_sessions = normalized.get("enabled_sessions") or []
    for key in ("sell_time", "buy_time"):
        target = str(normalized.get(key) or "")
        in_session = any(
            start <= target <= end
            for start, end in (
                session_ranges[s] for s in enabled_sessions if s in session_ranges
            )
        )
        if not in_session:
            raise HTTPException(
                status_code=400,
                detail=f"live_trade_config.{key} 必须落在已选执行时段内",
            )

    return normalized


def _parse_bridge_report_ts(report: dict) -> float | None:
    """
    从柜台桥接上报中提取时间戳（秒）:
    兼容常见字段: timestamp/ts/last_seen/updated_at/report_ts/report_time
    """
    candidates = (
        "timestamp",
        "ts",
        "last_seen",
        "updated_at",
        "report_ts",
        "report_time",
    )
    for key in candidates:
        raw = report.get(key)
        if raw is None:
            continue
        # 数字时间戳
        if isinstance(raw, (int, float)):
            ts = float(raw)
            # 兼容毫秒时间戳
            return ts / 1000.0 if ts > 1e12 else ts
        # 字符串时间戳 / ISO8601
        if isinstance(raw, str):
            text_raw = raw.strip()
            if not text_raw:
                continue
            try:
                ts = float(text_raw)
                return ts / 1000.0 if ts > 1e12 else ts
            except Exception:
                pass
            try:
                iso = text_raw.replace("Z", "+00:00")
                return datetime.fromisoformat(iso).timestamp()
            except Exception:
                continue
    return None


def _resolve_preflight_symbols() -> list[str]:
    raw = str(os.getenv("PREFLIGHT_STREAM_SYMBOLS", "000001.SZ,600000.SH")).strip()
    symbols = [item.strip() for item in raw.split(",") if item.strip()]
    return symbols or ["000001.SZ", "600000.SH"]


@lru_cache(maxsize=1)
def _load_root_env_map() -> dict[str, str]:
    """
    兜底读取项目根 .env，避免服务进程未注入变量时配置丢失。
    """
    env_map: dict[str, str] = {}
    try:
        root_env = Path(__file__).resolve().parents[4] / ".env"
        if not root_env.exists():
            return env_map
        for line in root_env.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            key = k.strip()
            value = v.strip().strip("'").strip('"')
            if key:
                env_map[key] = value
    except Exception:
        return {}
    return env_map


def _get_env_with_root_fallback(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    return _load_root_env_map().get(key, default)


def _resolve_runner_image_for_mode() -> tuple[str, str]:
    configured = str(os.getenv("STRATEGY_RUNNER_IMAGE", "")).strip()
    if configured:
        return configured, "configured"
    default_image = (
        "quantmind-ml-runtime:latest"
        if k8s_manager.mode == "docker"
        else "asia-east1-docker.pkg.dev/gen-lang-client-0953736716/quantmind-repo/quantmind-qlib-runner:latest"
    )
    return default_image, "default"


def _get_stream_series_redis_client():
    """
    Stream 行情时序 Redis（quote->series）客户端。
    OSS 版本使用统一 Redis 实例 (REDIS_DB_MARKET)。
    """
    host = _get_env_with_root_fallback("REDIS_HOST", "localhost")
    port = int(_get_env_with_root_fallback("REDIS_PORT", "6379") or "6379")
    password = _get_env_with_root_fallback("REDIS_PASSWORD", "") or None
    db = int(_get_env_with_root_fallback("REDIS_DB_MARKET", "3"))
    client = redis_lib.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        socket_timeout=3.0,
        socket_connect_timeout=3.0,
    )
    return client, host, port


def _local_today_for_preflight():
    tz_name = os.getenv("PREFLIGHT_SNAPSHOT_TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now().date()


async def _upsert_preflight_snapshot(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    trading_mode: str,
    ready: bool,
    checks: list[dict],
) -> None:
    total_checks = len(checks)
    passed_checks = sum(1 for item in checks if bool(item.get("ok")))
    failed_required_keys = [
        str(item.get("key"))
        for item in checks
        if bool(item.get("required")) and not bool(item.get("ok"))
    ]
    now = datetime.now()
    snapshot_date = _local_today_for_preflight()

    stmt = pg_insert(PreflightSnapshot).values(
        tenant_id=tenant_id,
        user_id=user_id,
        trading_mode=trading_mode,
        snapshot_date=snapshot_date,
        ready=bool(ready),
        total_checks=total_checks,
        passed_checks=passed_checks,
        required_failed_count=len(failed_required_keys),
        failed_required_keys=failed_required_keys,
        checks=checks,
        source="preflight_api",
        last_checked_at=now,
        run_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_preflight_snapshot_daily",
        set_={
            "ready": bool(ready),
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "required_failed_count": len(failed_required_keys),
            "failed_required_keys": failed_required_keys,
            "checks": checks,
            "last_checked_at": now,
            "run_count": PreflightSnapshot.run_count + 1,
            "updated_at": now,
        },
    )
    await db.execute(stmt)
    await db.commit()
