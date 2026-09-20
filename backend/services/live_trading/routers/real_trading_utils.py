import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
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
from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.models.preflight_snapshot import PreflightSnapshot
from backend.services.trade_shared.models.real_account_snapshot import RealAccountSnapshot
from backend.services.trade_shared.portfolio.models import Portfolio
from backend.services.trade_shared.models.trade import Trade
from backend.services.trade_shared.redis_client import RedisClient, get_redis
from backend.services.trade_shared.utils.redis_cache import redis_cache
from backend.services.trade_shared.schemas.live_trade_config import (
    ExecutionConfigSchema,
    LiveTradeConfigSchema,
)
from backend.services.live_trading.services.k8s_manager import k8s_manager
from backend.services.trade.services.real_account_snapshot_guard import (
    is_effectively_empty_snapshot,
    is_inconsistent_zero_total_snapshot,
)
from backend.services.trade.services.trading_precheck_service import (
    _check_inference_model_exists,
    run_trading_readiness_precheck,
)
from backend.services.trade_shared.trade_config import settings
from backend.shared.freshness import STALE, UNAVAILABLE, quote_policy
from backend.shared.margin_stock_pool import get_margin_stock_pool_service
from backend.shared.notification_publisher import publish_notification_async
from backend.shared.strategy_storage import get_strategy_storage_service
from backend.shared.stock_utils import StockCodeUtil

router = APIRouter()
logger = logging.getLogger(__name__)
REAL_ACCOUNT_SNAPSHOT_VIEW_NAME = "real_account_snapshot_overview_v"

#: 快照 source 显示名（后端唯一处；前端直接吃 ``account_source_label``，不再各写一份）
SNAPSHOT_SOURCE_LABELS: dict[str, str] = {
    "qmt_exec": "QMT 迅投",
    "qmt_bridge": "QMT 桥",
    "tdx_bridge": "通达信桥",
    "manual_override": "手工录入",
}

#: 请求源没有可用数据时的降级原因（如实标注，绝不静默拿另一个账户的数字顶包）
DOWNGRADE_SOURCE_MISSING = "requested_source_no_snapshot"
DOWNGRADE_SOURCE_UNUSABLE = "requested_source_no_usable_snapshot"

#: 各源最新一行的查询：同一 (tenant, user) 下 QMT 与通达信是两个**互不相交的真实账户**
#: （实测 50 只 / 8 只），两条流每 30s 交错写库，混在一起取最新等于掷硬币。
_PER_SOURCE_LATEST_SQL = """
    SELECT DISTINCT ON (source)
        source,
        account_id,
        user_id,
        snapshot_at,
        snapshot_date,
        snapshot_month,
        total_asset,
        cash,
        market_value,
        today_pnl_raw,
        total_pnl_raw,
        floating_pnl_raw,
        payload_json
    FROM real_account_snapshots
    WHERE tenant_id = :tenant_id
      AND user_id IN :user_ids
    ORDER BY source, snapshot_at DESC, id DESC
"""


def snapshot_source_label(source: str | None) -> str:
    """快照源显示名（未知源原样返回，不编造中文名）。"""
    src = str(source or "").strip()
    if not src:
        return "未知来源"
    return SNAPSHOT_SOURCE_LABELS.get(src, src)


def account_snapshot_broker_key(source: str | None) -> str | None:
    """快照 source → CN 券商键（页面「设为交易券商」按它调 PUT /broker-config/selected/CN）。

    反查表住在 ``backend.shared.real_positions``（券商↔源映射的唯一家），此处只转发：
    前端若自己猜映射，映射一变就切错券商，而切错券商的后果是订单换个柜台发。
    """
    from backend.shared.real_positions import broker_for_snapshot_source

    return broker_for_snapshot_source(source)


def is_account_source_explicitly_selected() -> bool:
    """当前实盘券商是否为用户在页面上显式选定（否则是 REAL_BROKER_TYPE 兜底）。"""
    from backend.shared.real_positions import selected_broker_is_explicit

    return selected_broker_is_explicit()


def resolve_account_snapshot_source(explicit: str | None = None) -> str | None:
    """账户快照读取源仲裁（唯一实现）：显式指定 > 当前实盘券商 > 不指定（全源最新可用）。

    「同源」是硬约束：本模块同时服务展示、预检、风控与下单预算。读的是 A 账户、
    订单发往 B 柜台，风控就是拿另一个账户的资金在对单。
    """
    chosen = str(explicit or "").strip()
    if chosen:
        return chosen
    try:
        from backend.shared.real_positions import (
            active_broker_type,
            snapshot_source_for_broker,
        )

        return snapshot_source_for_broker(active_broker_type())
    except Exception as exc:  # noqa: BLE001 - 取不到券商选择就退回「全源最新可用」
        logger.debug("账户快照源仲裁失败，退回全源最新: %s", exc)
        return None


def _snapshot_candidate_user_ids(user_id: str) -> list[str]:
    """user_id 候选集（历史写入过前导零 / 去零两种形态，都要对上）。"""
    normalized = str(user_id or "").strip()
    if not normalized:
        return []
    candidates = {normalized}
    if normalized.isdigit():
        candidates.add(str(int(normalized)))
        candidates.add(normalized.zfill(8))
    return sorted(candidates)


def _snapshot_ts_key(value: Any) -> float:
    """快照时间戳排序键（naive 时间按 UTC 口径，与 ``_parse_snapshot_timestamp`` 一致）。"""
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    return 0.0


def select_account_snapshot_target(
    source_rows: list[dict[str, Any]], *, requested_source: str | None
) -> tuple[str | None, str | None]:
    """源仲裁（纯函数）：返回 ``(目标源, 降级原因)``。

    请求源只要有数据就用它——**哪怕另一个源更新**（选定即权威，页面不许自己换账户）；
    请求源完全没数据时退回最新源并如实标注降级原因。
    """
    if not source_rows:
        return None, (DOWNGRADE_SOURCE_MISSING if requested_source else None)
    newest = max(source_rows, key=lambda r: _snapshot_ts_key(r.get("snapshot_at")))
    newest_source = str(newest.get("source") or "") or None
    if not requested_source:
        return newest_source, None
    known = {str(r.get("source") or "") for r in source_rows}
    if requested_source in known:
        return requested_source, None
    return newest_source, DOWNGRADE_SOURCE_MISSING


async def fetch_account_source_rows(
    db: AsyncSession, *, tenant_id: str, user_id: str
) -> list[dict[str, Any]]:
    """各 source 最新一行快照（含 payload）：源仲裁与「按源看」摘要共用同一查询。"""
    user_ids = _snapshot_candidate_user_ids(user_id)
    if not user_ids:
        return []
    stmt = text(_PER_SOURCE_LATEST_SQL).bindparams(bindparam("user_ids", expanding=True))
    result = await db.execute(stmt, {"tenant_id": tenant_id, "user_ids": user_ids})
    return [dict(row) for row in result.mappings().all()]


def _annotate_account_source(
    contract: dict[str, Any],
    *,
    requested_source: str | None,
    downgrade_reason: str | None,
) -> dict[str, Any]:
    """补源标注：本次读的是哪个源、请求的是哪个源、是否拿别的账户顶了包。"""
    actual = str(contract.get("source") or "")
    dropped = bool(requested_source) and actual != requested_source
    return {
        **contract,
        "account_source": actual,
        "account_source_label": snapshot_source_label(actual),
        "requested_source": requested_source,
        "source_downgraded": dropped,
        "source_downgraded_reason": downgrade_reason if dropped else None,
    }


def is_usable_account_snapshot_row(row: dict[str, Any]) -> bool:
    """该行快照是否可用（空壳 / 总资产矛盾行都不算），供「按源看」逐源标注。"""
    return _select_latest_usable_snapshot_row([row]) is not None


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
    # 桥可能返回已清仓残留（volume=0），过滤后仅统计真实持仓
    if isinstance(positions, list):
        positions = [
            p for p in positions if float(p.get("volume") or 0) > 0
        ]

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
    # 桥不回报浮动盈亏(raw=0)时按持仓兜底: Σ(现价−成本价)×持仓量
    if abs(floating_pnl) < 1e-9 and isinstance(positions, list):
        computed = 0.0
        for p in positions:
            vol = float(p.get("volume") or 0)
            if vol <= 0:
                continue
            price = float(p.get("price") or 0)
            cost = float(p.get("cost_price") or 0)
            if price <= 0:
                mv = float(p.get("market_value") or 0)
                price = mv / vol if mv > 0 else 0
            if price > 0 and cost > 0:
                computed += (price - cost) * vol
        if computed:
            floating_pnl = round(computed, 2)
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
    # 唯一口径见 shared/simulation_account_keys：管理员族 10000001，其它数字补零 8 位。
    # 禁止手写 zfill(8)——曾导致 admin 被写成 000admin，重启恢复与状态查询分裂。
    from backend.shared.simulation_account_keys import active_strategy_key

    return active_strategy_key(tenant_id, user_id)


def _read_active_strategy_raw(redis: RedisClient, tenant_id: str, user_id: str):
    """读 active_strategy，命中历史别名时回写规范键。"""
    from backend.shared.simulation_account_keys import active_strategy_lookup_keys

    client = getattr(redis, "client", None)
    if client is None:
        return None
    canonical = _active_strategy_key(tenant_id, user_id)
    for key in active_strategy_lookup_keys(tenant_id, user_id):
        raw = client.get(key)
        if not raw:
            continue
        if key != canonical:
            try:
                client.set(canonical, raw)
            except Exception:
                pass
        return raw
    return None


def _delete_active_strategy_aliases(redis: RedisClient, tenant_id: str, user_id: str) -> None:
    from backend.shared.simulation_account_keys import active_strategy_lookup_keys

    client = getattr(redis, "client", None)
    if client is None:
        return
    for key in active_strategy_lookup_keys(tenant_id, user_id):
        try:
            client.delete(key)
        except Exception:
            continue


def _normalize_identity(
    auth: AuthContext,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[str, str]:
    """
    统一身份来源：JWT 为准；兼容传参时必须与 JWT 一致。
    数字 user_id 按 8 位补零（兼容历史整数 ID）；管理员族收口 10000001。
    避免与 qm_user_models 中存储的原始 user_id 不一致导致默认模型查询失败。
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

    from backend.shared.simulation_account_keys import normalize_runtime_user

    normalized_user = normalize_runtime_user(token_user_id)
    return normalized_user, token_tenant_id


def normalize_db_user_id(user_id: Any) -> str:
    """数据库 user_id 口径统一为字符串（数字补零到 8 位，与 _normalize_identity 一致）。

    内部调度链路里 user_id 常被转成 int（Redis 模拟账户键口径），直接拿去查
    VARCHAR 列会报 ``operator does not exist: character varying = integer``，
    且 "1" 与库里的 "00000001" 对不上——所有面向 DB 的查询都先过这里。
    """
    value = str(user_id or "").strip()
    return value.zfill(8) if value.isdigit() else value


async def _fetch_active_portfolio_snapshot(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    strategy_id: str | None,
    mode: str | None = None,
) -> dict | None:
    sid = str(strategy_id or "").strip()

    # user_id 在数据库中是 VARCHAR 类型，直接用字符串查询（数字统一补零口径）
    normalized_user_id = normalize_db_user_id(user_id)
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

    # 当日收益率统一口径：当日盈亏 / 初始资金（与模拟账户 daily_return_ratio、
    # 前端 requireDerived 一致）。此前用持仓市值做分母，空仓/轻仓时失真且三处对不上。
    if initial_capital > 0:
        raw_daily_return = daily_pnl / initial_capital
    else:
        raw_daily_return = _decimal_to_float(getattr(portfolio, "daily_return", 0.0), 0.0)

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
    await db.commit()


async def _query_snapshot_view_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_ids: list[str],
    source: str | None,
) -> list[dict[str, Any]]:
    """账户快照视图取行（可选按源过滤；先按 snapshot_at 倒序，供逐行回退）。"""
    source_clause = "AND source = :source" if source else ""
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
              {source_clause}
            ORDER BY snapshot_at DESC, id DESC
            LIMIT 20
            """
    ).bindparams(bindparam("user_ids", expanding=True))
    params: dict[str, Any] = {"tenant_id": tenant_id, "user_ids": user_ids}
    if source:
        params["source"] = source
    result = await db.execute(view_stmt, params)
    return [dict(row) for row in result.mappings().all()]


async def _query_snapshot_table_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_ids: list[str],
    source: str | None,
) -> list[RealAccountSnapshot]:
    """视图不可用时的表兜底取行（同样支持按源过滤）。"""
    stmt = select(RealAccountSnapshot).where(
        RealAccountSnapshot.tenant_id == tenant_id,
        RealAccountSnapshot.user_id.in_(user_ids),
    )
    if source:
        stmt = stmt.where(RealAccountSnapshot.source == source)
    stmt = stmt.order_by(
        desc(RealAccountSnapshot.snapshot_at), desc(RealAccountSnapshot.id)
    ).limit(20)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _select_latest_usable_snapshot_entity(
    rows: list[Any],
) -> Any | None:
    """表兜底路径的可用行选择（与视图路径同一套守卫，只是入参是 ORM 实体）。"""
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
        return candidate
    return None


async def _fetch_latest_real_account_snapshot(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    source: str | None = None,
) -> dict[str, Any] | None:
    """最新实盘账户快照（**按源仲裁**：选定就用选定的，降级必标注）。

    ``source=None`` → 当前实盘券商（Redis ``broker:selected:CN``）对应的源；
    显式传 ``source`` → 只读该源（前端「按源看」）。
    请求源没有可用快照时退回全源最新可用行，并在 ``source_downgraded`` /
    ``source_downgraded_reason`` 里如实报出——绝不把另一个账户的数字当自己的用。
    """
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return None

    candidate_ids = _snapshot_candidate_user_ids(normalized_user_id)
    requested_source = resolve_account_snapshot_source(source)

    # 先按源取各自最新一行 → 仲裁出本次该读哪个源（这一步同时回答「有没有这个源」）
    target_source: str | None = requested_source
    downgrade_reason: str | None = None
    try:
        source_rows = await fetch_account_source_rows(
            db, tenant_id=tenant_id, user_id=normalized_user_id
        )
        target_source, downgrade_reason = select_account_snapshot_target(
            source_rows, requested_source=requested_source
        )
    except Exception as exc:
        logger.warning(
            "real account source summary unavailable, read requested source directly: tenant=%s user=%s err=%s",
            tenant_id,
            normalized_user_id,
            exc,
        )
        try:
            await db.rollback()
        except Exception as rollback_exc:
            logger.warning(
                "real account source summary rollback failed: tenant=%s user=%s err=%s",
                tenant_id,
                normalized_user_id,
                rollback_exc,
            )

    try:
        snapshots = await _query_snapshot_view_rows(
            db, tenant_id=tenant_id, user_ids=candidate_ids, source=target_source
        )
        snapshot = _select_latest_usable_snapshot_row(snapshots)
        if snapshot is None and target_source is not None:
            # 目标源窗口内没有可用行（该源从未上报 / 最新行是空壳或矛盾行）→ 全源最新 + 标注
            snapshots = await _query_snapshot_view_rows(
                db, tenant_id=tenant_id, user_ids=candidate_ids, source=None
            )
            snapshot = _select_latest_usable_snapshot_row(snapshots)
            if snapshot is not None:
                downgrade_reason = downgrade_reason or DOWNGRADE_SOURCE_UNUSABLE
        if snapshot is not None:
            payload_json = snapshot.get("payload_json") or {}
            if not isinstance(payload_json, dict):
                payload_json = {}
            total_asset = float(snapshot.get("total_asset") or 0.0)
            day_open_equity = float(snapshot.get("day_open_equity") or 0.0)
            month_open_equity = float(snapshot.get("month_open_equity") or 0.0)
            initial_equity = float(snapshot.get("initial_equity") or 0.0)
            today_pnl_raw = float(snapshot.get("today_pnl_raw") or 0.0)
            contract = _build_real_account_contract(
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
            return _annotate_account_source(
                contract,
                requested_source=requested_source,
                downgrade_reason=downgrade_reason,
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

    rows = await _query_snapshot_table_rows(
        db, tenant_id=tenant_id, user_ids=candidate_ids, source=target_source
    )
    snapshot = _select_latest_usable_snapshot_entity(rows)
    if snapshot is None and target_source is not None:
        rows = await _query_snapshot_table_rows(
            db, tenant_id=tenant_id, user_ids=candidate_ids, source=None
        )
        snapshot = _select_latest_usable_snapshot_entity(rows)
        if snapshot is not None:
            downgrade_reason = downgrade_reason or DOWNGRADE_SOURCE_UNUSABLE
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
    contract = _build_real_account_contract(
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
    return _annotate_account_source(
        contract,
        requested_source=requested_source,
        downgrade_reason=downgrade_reason,
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
            ) from None
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
            ) from None
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
        "order_type": "MARKET",
        "max_price_deviation": 0.02,
        "max_orders_per_cycle": 20,
    }


def _normalize_live_trade_config(
    user_live_cfg: dict,
    base_live_cfg: dict,
    *,
    allow_after_hours: bool = False,
) -> dict:
    """合并并校验实盘/模拟时段配置。

    T-P2-07：AFTER_HOURS（盘后固定价格 15:05–15:30）仅模拟托管支持——
    allow_after_hours=True（SIMULATION 启动）时放行并校验时刻落在窗口内；
    默认 False（REAL 口径）显式拒绝（QMT 盘后通道待核实，fail-closed 带明确话术）。
    """
    merged = dict(_default_live_trade_config())
    merged.update(base_live_cfg or {})
    merged.update(user_live_cfg or {})

    try:
        LiveTradeConfigSchema.model_validate(merged)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"live_trade_config 非法: {exc}"
        ) from exc

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
    normalized["order_type"] = str(normalized.get("order_type") or "MARKET").upper()
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

    # T-P3-07：时段校验按**策略市场本地时钟**（市场唯一事实源 shared/market_sessions）。
    # A股 "14:45"=北京钟点；美股 "15:50"=美东钟点——本地钟不随夏令时漂移，校验零 DST 复杂度。
    from backend.shared.market_sessions import in_session_hhmm, session_ranges_local

    market_key = (
        (user_live_cfg or {}).get("market")
        or (base_live_cfg or {}).get("market")
        or "CN"
    )
    session_ranges = session_ranges_local(market_key)
    enabled_sessions = normalized.get("enabled_sessions") or []
    if "AFTER_HOURS" in enabled_sessions and not allow_after_hours:
        raise HTTPException(
            status_code=400,
            detail=(
                "盘后时段（AFTER_HOURS）实盘通道待核实（T-P2-07 / T-P3-07），"
                "当前仅模拟盘支持"
            ),
        )
    for key in ("sell_time", "buy_time"):
        target = str(normalized.get(key) or "")
        in_session = any(
            in_session_hhmm(target, *session_ranges[s])
            for s in enabled_sessions
            if s in session_ranges
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
    raw = str(os.getenv("PREFLIGHT_STREAM_SYMBOLS", "SZ000001,SH600000")).strip()
    symbols = [item.strip() for item in raw.split(",") if item.strip()]
    return symbols or ["SZ000001", "SH600000"]


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


def _get_remote_quote_redis_config() -> tuple[str, int, str | None, int]:
    """远端行情快照 Redis 配置（与 stream 写入端 RemoteRedisDataSource 对齐）。

    T-P0-03：默认值与读取逻辑收敛到 backend/shared/remote_quote_config.py
    （与模拟撮合 L0 取价共用一份，消除两处重复的免费行情服默认值）。
    REMOTE_QUOTE_DISABLED=true 时抛错，由调用方降级到交易 Redis。
    """
    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    resolved = resolve_remote_quote_redis()
    if resolved is None:
        raise RuntimeError("远端行情 Redis 未配置或已禁用（REMOTE_QUOTE_DISABLED）")
    return resolved


def _get_stream_series_redis_client():
    """
    Stream 行情时序 Redis（quote->series）客户端。

    优先直连 REMOTE_QUOTE_REDIS_*（与 quantmind-stream 的 quote->series
    写入端一致），远端探测异常时由调用方降级到交易 Redis。
    """
    host, port, password, db = _get_remote_quote_redis_config()
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


def _check_quantdb_latest_daily(market: str = "CN") -> tuple[bool, str]:
    """检查本地市场数据库是否有最近交易日日线（供模拟盘撮合兜底）。

    market=CN 检查 quantdb；HK/US/FUTURES/CRYPTO 分别检查对应市场的
    6 大类数据目录（同一 LocalMarketData 入口）。
    """
    market_upper = str(market or "CN").upper()
    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )

        # 必须走进程内共享实例：每次 new 一个 LocalMarketData 会丢掉交易日与
        # 按日行情缓存，健康检查就会反复重新枚举交易日（旧实现每次都付全表扫描）。
        market_data = get_local_market_data(market_upper)
        latest_date = market_data.latest_trade_date()
        if latest_date is None:
            return False, f"{market_upper} 市场数据库无可用日线"
        return True, latest_date.isoformat()
    except Exception as exc:
        return False, f"{market_upper} 行情库检查失败: {exc}"


def check_tdx_bridge_online() -> tuple[bool, str]:
    """探测通达信桥健康状态（QMT Agent 缺失时的兜底交易通道）。

    返回 (online, detail)。桥已配置且 /api/v1/health 返回 200 即视为在线。
    """
    bridge_url = str(getattr(settings, "TDX_BRIDGE_URL", "") or "").strip()
    bridge_token = str(getattr(settings, "TDX_BRIDGE_TOKEN", "") or "").strip()
    if not bridge_url or not bridge_token:
        return False, "TDX 桥未配置（TDX_BRIDGE_URL/TOKEN 为空）"
    try:
        resp = httpx.get(f"{bridge_url.rstrip('/')}/api/v1/health", timeout=3.0)
        if resp.status_code == 200:
            payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            tdx_connected = bool((payload or {}).get("tdx_connected", True))
            return True, (
                "TDX 桥在线，通达信已连接" if tdx_connected else "TDX 桥在线，通达信客户端未连接"
            )
        return False, f"TDX 桥返回 HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"TDX 桥不可达: {exc}"


def _probe_freshest_series_age(redis_like, symbols: list[str]) -> tuple[str | None, float | None]:
    """探测 symbols 中最新的 market:series 年龄（秒，保留浮点）；无数据返回 (None, None)。"""
    matched: str | None = None
    latest_age: float | None = None
    for symbol in symbols:
        normalized = StockCodeUtil.to_prefix(symbol)
        key = f"market:series:{normalized}"
        latest = redis_like.zrevrange(key, 0, 0, withscores=True)
        if latest:
            _, score = latest[0]
            age = time.time() - float(score)
            if latest_age is None or age < latest_age:
                matched = normalized
                latest_age = age
    return matched, latest_age


def check_stream_series_freshness(
    redis_client=None, *, allow_quantdb_fallback: bool = False, market: str = "CN"
) -> dict[str, Any]:
    """
    统一的 Stream 行情时序新鲜度检测逻辑（按市场）。

    分级口径（T-P6-05）唯一走 ``backend.shared.freshness``：fresh/stale 视为就绪
    （stale 在 details.level 如实标注），unavailable 就绪失败。

    allow_quantdb_fallback=True 时（模拟盘）：当 Redis 时序缺失/不可用时，
    回退检查该市场本地行情库是否有最近交易日日线（CN=quantdb、HK=quanthk、
    US=quantus…）。模拟撮合引擎直读对应市场库，有日线即可撮合，故视为就绪；
    标记 source=quantdb 供调用方区分。
    返回 {ok, message, details}
    """
    market_upper = str(market or "CN").upper()
    stream_symbols = _resolve_preflight_symbols()
    stream_redis, stream_redis_host, stream_redis_port = _get_stream_series_redis_client()
    policy = quote_policy()
    threshold_sec = int(policy.stale_within_s)

    matched_symbol = None
    latest_age_sec = None
    latest_age_raw: float | None = None
    level = UNAVAILABLE
    remote_probe_error = ""
    used_fallback = False
    try:
        stream_redis.ping()
        matched_symbol, latest_age_raw = _probe_freshest_series_age(stream_redis, stream_symbols)
        level = policy.classify(latest_age_raw)
    except Exception as exc:
        # 远端探测异常时降级到交易 Redis，并在 details 回显原因
        remote_probe_error = str(exc)
        if redis_client:
            try:
                used_fallback = True
                matched_symbol, latest_age_raw = _probe_freshest_series_age(
                    redis_client, stream_symbols
                )
                level = policy.classify(latest_age_raw)
            except Exception:
                pass

    if latest_age_raw is not None:
        latest_age_sec = max(0, int(latest_age_raw))
    ok = level != UNAVAILABLE
    if ok and level == STALE:
        message = f"行情陈旧但可用（{matched_symbol} 延迟 {latest_age_sec}s，超 fresh {int(policy.fresh_within_s)}s）"
    elif ok:
        message = f"行情新鲜（{matched_symbol} 延迟 {latest_age_sec}s）"
    else:
        message = (
            f"行情延迟过高（{matched_symbol} 延迟 {latest_age_sec}s > {threshold_sec}s）"
            if matched_symbol
            else "未发现可用行情序列"
        )

    source = "stream_series"
    if not ok and allow_quantdb_fallback:
        qdb_ok, qdb_detail = _check_quantdb_latest_daily(market_upper)
        if qdb_ok:
            ok = True
            source = "quantdb_daily"
            db_label = {
                "CN": "QuantDB", "HK": "QuantHK", "US": "QuantUS",
                "FUTURES": "QuantFutures", "CRYPTO": "QuantBC",
            }.get(market_upper, "QuantDB")
            message = (
                f"Redis 行情时序未接入，回退 {db_label} 日线可用"
                f"（最近交易日 {qdb_detail}）"
            )
        else:
            message = f"Redis 行情时序未接入且 {market_upper} 市场日线不可用: {qdb_detail}"

    return {
        "ok": ok,
        "message": message,
        "source": source,
        "details": {
            "matched_symbol": matched_symbol,
            "age_seconds": latest_age_sec,
            "level": level,
            "threshold_seconds": threshold_sec,
            "fresh_within_seconds": policy.fresh_within_s,
            "series_redis": f"{stream_redis_host}:{stream_redis_port}",
            "remote_probe_error": remote_probe_error,
            "used_fallback": used_fallback,
        },
    }


def check_stream_quote_persist_rate(
    redis_client=None, *, allow_quantdb_fallback: bool = False
) -> dict[str, Any]:
    """
    统一的 Stream 行情落库速率检测逻辑。

    allow_quantdb_fallback=True 时（模拟盘）：落库统计缺失时回退检查 QuantDB
    最近交易日日线是否可用，模拟撮合引擎直读 QuantDB 可正常撮合。
    """
    try:
        # 获取落库监控 Key (由 stream 服务写入远端行情 Redis)
        # 优先直连远端（与写入端一致），异常时降级到交易 Redis
        key = "market:stream:persist_stats"
        stats_raw = None
        remote_probe_error = ""
        try:
            stream_redis, _, _ = _get_stream_series_redis_client()
            stats_raw = stream_redis.get(key)
        except Exception as exc:
            remote_probe_error = str(exc)

        if not stats_raw and redis_client:
            try:
                stats_raw = redis_client.get(key)
            except Exception:
                pass

        if not stats_raw:
            if allow_quantdb_fallback:
                qdb_ok, qdb_detail = _check_quantdb_latest_daily()
                if qdb_ok:
                    return {
                        "ok": True,
                        "message": (
                            "行情落库统计未接入，回退 QuantDB 日线可用"
                            f"（最近交易日 {qdb_detail}）"
                        ),
                        "source": "quantdb_daily",
                        "details": {},
                    }
                return {
                    "ok": False,
                    "message": f"未检测到行情落库统计信息且 QuantDB 日线不可用: {qdb_detail}",
                    "details": {},
                }
            return {"ok": False, "message": "未检测到行情落库统计信息", "details": {}}

        stats = json.loads(stats_raw)
        rps = float(stats.get("quotes_per_sec", 0))
        last_update = float(stats.get("ts", 0))
        age = max(0, int(time.time() - last_update))

        ok = rps > 0 and age < 60
        message = (
            f"行情落库正常 ({rps:.1f} qps)"
            if ok
            else f"行情落库异常 (qps={rps:.1f}, age={age}s)"
        )

        return {
            "ok": ok,
            "message": message,
            "source": "stream_persist" if ok else "stale",
            "details": {**stats, "remote_probe_error": remote_probe_error},
        }
    except Exception as e:
        return {"ok": False, "message": f"行情落库检测异常: {e}", "details": {}}



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
