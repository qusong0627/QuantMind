"""
用户策略管理 API（PG + COS 统一存储版）

所有读写操作统一通过 backend.shared.strategy_storage.StrategyStorageService。
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import exchange_calendars as xcals
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

try:
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client
    from backend.shared.strategy_storage import get_strategy_storage_service
    from backend.shared.utils import normalize_user_id
except ImportError:
    from shared.database_manager_v2 import get_session  # type: ignore
    from shared.strategy_storage import get_strategy_storage_service  # type: ignore
    from shared.redis_sentinel_client import get_redis_sentinel_client  # type: ignore
    from shared.utils import normalize_user_id  # type: ignore

from backend.services.engine.qlib_app.services.strategy_templates import (
    get_all_templates,
    invalidate_templates_cache,
)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)

# 注意：不要在定义 router 时设置 prefix，由 main.py 统一管理
router = APIRouter()

ACTIVE_STRATEGIES_KEY = "quantmind:active_strategies"
_RUNTIME_STATES = {"running", "starting", "stopped", "error"}
_ACTIVATE_INFERENCE_LOCK_PREFIX = "qm:lock:inference:on_activate"


def _get_trade_redis():
    """
    获取 trade-redis 客户端，与 Runner 使用同一 Redis 实例。
    配置来源：SIGNAL_STREAM_REDIS_HOST 环境变量（docker-compose.server.yml 中已配置）。
    返回 None 表示未配置（单 Redis 开发环境）。
    """
    host = str(os.getenv("SIGNAL_STREAM_REDIS_HOST", "")).strip()
    if not host:
        return None
    try:
        from redis import Redis
        return Redis(
            host=host,
            port=int(os.getenv("SIGNAL_STREAM_REDIS_PORT", "6379")),
            db=int(os.getenv("SIGNAL_STREAM_REDIS_DB", "0")),
            password=os.getenv("SIGNAL_STREAM_REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
        )
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").warning("trade_redis_unavailable", "无法连接 trade-redis", error=e)
        return None


def _is_valid_bearer_jwt(auth_header: str) -> bool:
    token = str(auth_header or "").strip()
    if not token.lower().startswith("bearer "):
        return False
    raw_token = token[7:].strip()
    parts = raw_token.split(".")
    return len(parts) == 3 and all(part.strip() for part in parts)


async def _trigger_inference_after_activate(*, strategy_id: str, tenant_id: str, user_id: str, market: str = "A") -> None:
    """
    策略激活后异步触发一次推理发布（按 tenant_id + user_id 定向产信号）。
    """
    try:
        from backend.services.engine.inference.router_service import InferenceRouterService
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").warning("inference_import_failed", "导入 InferenceRouterService 失败", error=e)
        return

    from backend.services.engine.data_platform.calendars.calendar import get_calendar

    market_upper = (market or "A").upper()
    _MARKET_XCAL = {"A": "XSHG", "HK": "XHKG", "US": "XNYS"}
    _MARKET_TZ = {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York", "CRYPTO": "UTC"}

    tz_name = _MARKET_TZ.get(market_upper, "Asia/Shanghai")
    now_local = datetime.now(ZoneInfo(tz_name))

    if market_upper == "CRYPTO":
        # 加密货币 7×24，每天都交易
        data_trade_date_obj = now_local.date()
    else:
        xcal_name = _MARKET_XCAL.get(market_upper, "XSHG")
        cal = xcals.get_calendar(xcal_name)
        data_trade_date_obj = cal.previous_session(now_local.date()).date() if now_local.time() < datetime.strptime("09:30", "%H:%M").time() else now_local.date()
    data_trade_date = data_trade_date_obj.isoformat()
    if market_upper == "CRYPTO":
        prediction_trade_date = (data_trade_date_obj + timedelta(days=1)).isoformat()
    else:
        prediction_trade_date = cal.next_session(data_trade_date_obj).date().isoformat()
    redis = None
    lock_key = f"{_ACTIVATE_INFERENCE_LOCK_PREFIX}:{prediction_trade_date}:{tenant_id}:{user_id}"
    try:
        redis = get_redis_sentinel_client()
        owner = f"{strategy_id}:{user_id}:{int(datetime.now().timestamp())}"
        if not bool(redis.set(lock_key, owner, ex=1800, nx=True)):
            StructuredTaskLogger(
                logger,
                "user-strategies",
                {"strategy_id": strategy_id, "tenant_id": tenant_id, "user_id": user_id},
            ).info("inference_skipped", "已有同用户同日推理在执行/完成，跳过", date=prediction_trade_date)
            return
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").warning("inference_lock_failed", "Redis 锁获取失败，降级继续执行", error=e)

    try:
        router_service = InferenceRouterService()
        result = await asyncio.to_thread(
            router_service.run_daily_inference_script,
            date=data_trade_date,
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            redis_client=redis,
            market=market,
        )
        StructuredTaskLogger(
            logger,
            "user-strategies",
            {"strategy_id": strategy_id, "tenant_id": tenant_id, "user_id": user_id},
        ).info(
            "inference_complete",
            "触发完成",
            data_trade_date=data_trade_date,
            prediction_trade_date=prediction_trade_date,
            success=getattr(result, "success", False),
            run_id=getattr(result, "run_id", ""),
            signals=getattr(result, "signals_count", 0),
            fallback_used=getattr(result, "fallback_used", False),
            fallback_reason=getattr(result, "fallback_reason", ""),
        )
    except Exception as e:
        StructuredTaskLogger(
            logger,
            "user-strategies",
            {"strategy_id": strategy_id, "tenant_id": tenant_id, "user_id": user_id},
        ).exception(
            "inference_failed",
            "执行失败",
            data_trade_date=data_trade_date,
            prediction_trade_date=prediction_trade_date,
            error=e,
        )


def _normalize_base_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in {"draft", "d"}:
        return "draft"
    if text in {"active", "repository", "repo"}:
        return "repository"
    if text in {"live_trading", "live"}:
        return "live_trading"
    return "draft"


def _base_to_effective_status(base_status: str) -> str:
    # 生命周期状态不代表当前运行态，兜底统一显示 stopped
    return "stopped"


def _normalize_runtime_state(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if text in _RUNTIME_STATES:
        return text
    return None


def _resolve_trade_service_url() -> str:
    direct = str(os.getenv("TRADE_SERVICE_URL", "")).strip()
    if direct:
        parsed = urlparse(direct)
        hostname = (parsed.hostname or "").strip().lower()
        # 容器内若读到本地开发地址（127.0.0.1/localhost），会错误指向当前 engine 容器。
        if os.path.exists("/.dockerenv") and hostname in {"127.0.0.1", "localhost"}:
            return "http://quantmind-trade:8002/api/v1/real-trading/status"
        return f"{direct.rstrip('/')}/api/v1/real-trading/status"
    return "http://quantmind-trade:8002/api/v1/real-trading/status"


def _get_user_id(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    if user:
        return str(user.get("user_id") or user.get("sub"))
    return None


def _get_tenant_id(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    return str(user.get("tenant_id") or "default")


def _to_iso_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text_value = str(value).strip()
    return text_value or None


async def _fetch_latest_backtest_summaries(user_id: str, tenant_id: str) -> dict[str, dict[str, Any]]:
    """
    按策略 ID 提取最近一次回测摘要。

    这里只使用已落库的回测摘要，不回源加载大结果文件，避免仪表盘列表接口变重。
    """
    normalized_user_id = normalize_user_id(user_id)
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT DISTINCT ON (COALESCE(config_json->>'strategy_id', ''))
                    COALESCE(config_json->>'strategy_id', '') AS strategy_id,
                    status,
                    created_at,
                    completed_at,
                    result_json
                FROM qlib_backtest_runs
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND COALESCE(config_json->>'strategy_id', '') <> ''
                ORDER BY
                    COALESCE(config_json->>'strategy_id', ''),
                    completed_at DESC NULLS LAST,
                    created_at DESC
                """
            ),
            {"user_id": normalized_user_id, "tenant_id": tenant_id},
        )

    summaries: dict[str, dict[str, Any]] = {}
    for row in rows.mappings().all():
        strategy_id = str(row.get("strategy_id") or "").strip()
        if not strategy_id:
            continue

        result_json = row.get("result_json")
        payload = result_json if isinstance(result_json, dict) else {}
        summaries[strategy_id] = {
            "status": row.get("status"),
            "created_at": _to_iso_string(row.get("created_at")),
            "completed_at": _to_iso_string(row.get("completed_at")),
            "total_return": payload.get("total_return", payload.get("annual_return")),
            "today_return": payload.get("today_return", payload.get("daily_return")),
            "risk_level": payload.get("risk_level"),
            "error_code": payload.get("error_code"),
            "error_message": payload.get("error_message"),
            "last_update": _to_iso_string(payload.get("last_update") or row.get("completed_at") or row.get("created_at")),
            "execution_latency_ms": (
                int(float(payload.get("execution_time")) * 1000)
                if payload.get("execution_time") is not None
                else None
            ),
        }
    return summaries


async def _perform_sync(user_id: str):
    """
    执行模板同步的内部逻辑：将内置模板同步到用户的个人策略数据库。
    同步 DB 操作通过 asyncio.to_thread 避免阻塞事件循环。
    """
    svc = get_strategy_storage_service()
    from sqlalchemy import text

    from backend.shared.strategy_storage import _ensure_int_user_id, get_db

    # 彻底清理残留：如果用户已有"抗下行 Alpha 策略"，则将其移除
    # 避免因名称冲突导致的新模板(多空TopK)无法同步
    def _cleanup_old_templates():
        try:
            uid_int = _ensure_int_user_id(user_id)
            with get_db() as session:
                session.execute(
                    text("DELETE FROM strategies WHERE user_id = :uid AND name = '抗下行 Alpha 策略'"), {"uid": uid_int}
                )
                session.execute(
                    text("DELETE FROM strategies WHERE user_id = :uid AND parameters->>'strategy_type' = 'downside_alpha'"),
                    {"uid": uid_int},
                )
                session.commit()
            return True
        except Exception:
            return False

    cleanup_ok = await asyncio.to_thread(_cleanup_old_templates)
    if cleanup_ok:
        StructuredTaskLogger(logger, "user-strategies", {"user_id": user_id}).info(
            "sync_cleanup", "清理旧模板", strategy="抗下行 Alpha 策略/downside_alpha"
        )
    else:
        StructuredTaskLogger(logger, "user-strategies", {"user_id": user_id}).warning(
            "sync_cleanup_failed", "Failed to cleanup obsolete strategy in sync"
        )

    templates = get_all_templates()
    synced_count = 0
    for t in templates:
        # 检查是否已存在同名策略 (同步 DB 调用，放线程池)
        existing = await asyncio.to_thread(svc.list, user_id=user_id, search=t.name)
        if any(s["name"] == t.name for s in existing):
            continue

        params = {"strategy_type": t.id, "topk": 50, "signal": "<PRED>"}
        # 市场专属模板（strategy_templates/*.json markets 字段）标注市场，
        # 港股模板写 market=HK 供策略库按市场过滤；A 股（markets 为空）不写 = 现状
        if t.markets and "hong_kong" in t.markets and "a_share" not in t.markets:
            params["market"] = "HK"

        await svc.save(
            user_id=user_id,
            name=t.name,
            code=t.code,
            metadata={
                "description": t.description,
                "tags": [t.category, t.difficulty, "SystemSync"],
                "status": "ACTIVE",
                "is_verified": True,
                "parameters": params,
            },
        )
        synced_count += 1
    return synced_count


# ============================================================================
# 请求/响应模型
# ============================================================================


class StrategyCreateRequest(BaseModel):
    """创建策略请求"""
    name: str = Field(..., min_length=1, max_length=200, description="策略名称")
    code: str = Field("", description="策略代码")
    description: str = Field("", description="策略描述")
    category: str = Field("strategy_lab", description="策略分类")
    author: str = Field("", description="作者")
    tags: list[str] = Field(default_factory=list, description="标签")
    parameters: dict[str, Any] = Field(default_factory=dict, description="策略参数")


class StrategyUpdateRequest(BaseModel):
    """更新策略请求"""
    name: str | None = Field(None, description="策略名称")
    code: str | None = Field(None, description="策略代码")
    description: str | None = Field(None, description="策略描述")
    tags: list[str] | None = Field(None, description="标签")
    parameters: dict[str, Any] | None = Field(None, description="策略参数")


class StrategyListItem(BaseModel):
    id: str
    name: str
    description: str = ""
    status: str = "draft"
    base_status: str = "draft"
    runtime_state: str | None = None
    effective_status: str = "stopped"
    total_return: float = 0.0
    today_return: float = 0.0
    today_pnl: float = 0.0
    risk_level: str = "medium"
    created_at: str | None = None
    updated_at: str | None = None
    last_update: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    last_failed_at: str | None = None
    last_signal_at: str | None = None
    execution_latency_ms: int | None = None
    category: str = "db_stored"
    tags: list[str] = Field(default_factory=list)
    code: str = ""
    cos_url: str | None = None
    is_verified: bool = False
    is_system: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)


class StrategyListResponse(BaseModel):
    total: int
    strategies: list[StrategyListItem]


async def _fetch_real_trading_status(request: Request) -> dict[str, Any] | None:
    auth_header = request.headers.get("authorization")
    if not auth_header or not _is_valid_bearer_jwt(auth_header):
        StructuredTaskLogger(logger, "user-strategies").warning(
            "real_trading_skip", "skip real-trading/status due to invalid auth header"
        )
        return None
    tenant_id = str((getattr(request.state, "user", {}) or {}).get("tenant_id") or "default")
    headers = {"Authorization": auth_header, "X-Tenant-Id": tenant_id}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            resp = await client.get(_resolve_trade_service_url(), headers=headers)
        if resp.status_code != 200:
            StructuredTaskLogger(logger, "user-strategies").warning(
                "real_trading_failed", "fetch real-trading/status failed", status=resp.status_code
            )
            return None
        payload = resp.json()
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").warning(
            "real_trading_exception", "fetch real-trading/status exception", error=e
        )
        return None


# ============================================================================
# 核心业务接口
# ============================================================================


@router.get("", response_model=StrategyListResponse)
async def list_user_strategies(
    request: Request,
    category: str | None = Query(None),
    search: str | None = Query(None),
    tags: str | None = Query(None),
    market: str | None = Query(None, description="按市场过滤策略列表（A/CN/HK/US...；缺省不过滤）"),
):
    """获取当前用户的策略列表。如果是新用户则自动初始化模板。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()
        tag_list = tags.split(",") if tags else None
        tenant_id = _get_tenant_id(request)

        items = await asyncio.to_thread(
            svc.list, user_id=user_id, category=category, search=search, tags=tag_list, market=market
        )

        # 港股无市场专属模板：市场过滤时跳过自动同步（避免给港股用户灌 A 股模板）
        if not items and not search and not tags and market not in ("HK", "hk"):
            await _perform_sync(user_id)
            items = await asyncio.to_thread(svc.list, user_id=user_id, market=market)

        backtest_summaries = await _fetch_latest_backtest_summaries(user_id=user_id, tenant_id=tenant_id)
        trading_status = await _fetch_real_trading_status(request)
        runtime_state = _normalize_runtime_state((trading_status or {}).get("status"))
        strategy_payload = (trading_status or {}).get("strategy") if isinstance(trading_status, dict) else {}
        if not isinstance(strategy_payload, dict):
            strategy_payload = {}
        active_strategy_id = str(strategy_payload.get("id") or "").strip()
        active_strategy_name = str(strategy_payload.get("name") or "").strip().lower()
        active_template_id = active_strategy_id.replace("sys_", "", 1) if active_strategy_id.startswith("sys_") else ""
        trade_portfolio = (trading_status or {}).get("portfolio") if isinstance(trading_status, dict) else None

        def _to_float(value: Any, default: float = 0.0) -> float:
            try:
                if value is None:
                    return default
                result = float(value)
                return result if result == result else default
            except Exception:
                return default

        strategies = []
        for item in items:
            base_status = _normalize_base_status(item.get("status"))
            item_id = str(item.get("id") or "")
            item_name = str(item.get("name") or "").strip().lower()
            item_parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            item_strategy_type = str(item_parameters.get("strategy_type") or "").strip().lower()

            is_active_item = False
            if active_strategy_id and item_id == active_strategy_id:
                is_active_item = True
            elif active_template_id and item_strategy_type == active_template_id.lower():
                is_active_item = True
            elif active_strategy_name and item_name == active_strategy_name:
                is_active_item = True

            item_runtime_state = runtime_state if is_active_item else None
            effective_status = item_runtime_state or _base_to_effective_status(base_status)
            summary = backtest_summaries.get(item_id, {})
            summary_total_return = summary.get("total_return")
            summary_today_return = summary.get("today_return")
            summary_risk_level = summary.get("risk_level")
            summary_execution_latency = summary.get("execution_latency_ms")
            risk_level = summary_risk_level if isinstance(summary_risk_level, str) and summary_risk_level else (
                item.get("parameters", {}).get("risk_level") if isinstance(item.get("parameters"), dict) else None
            )
            if not isinstance(risk_level, str) or not risk_level.strip():
                risk_level = "medium"

            total_return = _to_float(summary_total_return, 0.0)
            if total_return == 0.0 and summary.get("status") == "failed":
                total_return = 0.0
            today_return = _to_float(summary_today_return, 0.0)
            today_pnl = 0.0
            if active_strategy_id and item_id == active_strategy_id:
                trade_today_return = (trading_status or {}).get("daily_return")
                if trade_today_return is None and isinstance(trade_portfolio, dict):
                    trade_today_return = trade_portfolio.get("daily_return")
                trade_daily_pnl = (trading_status or {}).get("daily_pnl")
                if trade_daily_pnl is None and isinstance(trade_portfolio, dict):
                    trade_daily_pnl = trade_portfolio.get("daily_pnl")
                if trade_today_return is not None:
                    today_return = _to_float(trade_today_return, today_return)
                elif trade_daily_pnl is not None and isinstance(trade_portfolio, dict):
                    initial_capital = _to_float(trade_portfolio.get("initial_capital"), 0.0)
                    if initial_capital > 0:
                        today_return = _to_float(trade_daily_pnl, 0.0) / initial_capital * 100.0
                if trade_daily_pnl is not None:
                    today_pnl = _to_float(trade_daily_pnl, 0.0)
            execution_latency_ms = None
            if summary_execution_latency is not None:
                try:
                    execution_latency_ms = int(float(summary_execution_latency))
                except Exception:
                    execution_latency_ms = None
            strategies.append(
                StrategyListItem(
                    id=item_id,
                    name=item["name"],
                    description=item["description"] or "",
                    status=effective_status,
                    base_status=base_status,
                    runtime_state=item_runtime_state,
                    effective_status=effective_status,
                    total_return=total_return,
                    today_return=today_return,
                    today_pnl=today_pnl,
                    risk_level=risk_level,
                    created_at=item.get("created_at"),
                    updated_at=item["updated_at"],
                    tags=item["tags"],
                    is_verified=item["is_verified"],
                    cos_url=item.get("cos_url"),
                    last_update=summary.get("last_update"),
                    error_code=summary.get("error_code"),
                    error_message=summary.get("error_message"),
                    last_failed_at=summary.get("completed_at") if summary.get("status") == "failed" else None,
                    last_signal_at=summary.get("created_at"),
                    execution_latency_ms=execution_latency_ms,
                    parameters=item.get("parameters") or {},
                )
            )

        return StrategyListResponse(total=len(strategies), strategies=strategies)
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").exception("list_failed", "获取策略列表失败", error=e)
        raise HTTPException(status_code=500, detail=str(e))


# --- 重要：静态路径必须放在动态路径 {strategy_id} 之前 ---


@router.post("")
async def create_strategy(request: Request, body: StrategyCreateRequest):
    """创建新策略。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()
        result = await svc.save(
            user_id=user_id,
            name=body.name,
            code=body.code,
            metadata={
                "description": body.description,
                "tags": body.tags,
                "status": "DRAFT",
                "is_verified": False,
                "parameters": body.parameters,
            },
        )
        return {
            "strategy_id": result["id"],
            "id": result["id"],
            "name": body.name,
            "message": "策略创建成功",
        }
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").exception("create_failed", "创建策略失败", error=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{strategy_id}")
async def update_strategy(strategy_id: str, request: Request, body: StrategyUpdateRequest):
    """更新策略。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()

        # 验证策略存在
        try:
            sid = int(strategy_id)
        except ValueError:
            sid = strategy_id

        existing = await svc.get(sid, user_id=user_id)
        if not existing:
            raise HTTPException(status_code=404, detail="策略不存在")

        # 合并更新字段
        new_name = body.name if body.name is not None else existing.get("name", "")
        new_code = body.code if body.code is not None else existing.get("code", "")
        new_description = body.description if body.description is not None else existing.get("description", "")
        new_tags = body.tags if body.tags is not None else existing.get("tags", [])
        new_parameters = body.parameters if body.parameters is not None else existing.get("parameters", {})

        result = await svc.save(
            user_id=user_id,
            strategy_id=strategy_id,
            name=new_name,
            code=new_code,
            metadata={
                "description": new_description,
                "tags": new_tags,
                "status": existing.get("status", "DRAFT"),
                "is_verified": existing.get("is_verified", False),
                "parameters": new_parameters,
            },
        )
        return {
            "strategy_id": result["id"],
            "id": result["id"],
            "name": new_name,
            "message": "策略更新成功",
        }
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies", {"strategy_id": strategy_id}).exception(
            "update_failed", "更新策略失败", error=e
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/templates")
async def list_strategy_templates(response: Response):
    """获取所有预置策略模板（动态从 strategy_templates/ 目录加载）。"""
    # 与后端 TTL 对齐，告知客户端最多缓存 60s
    response.headers["Cache-Control"] = "max-age=60, public"
    templates = await asyncio.to_thread(get_all_templates)
    return {"templates": [t.model_dump() for t in templates]}


@router.post("/sync")
async def sync_templates(request: Request):
    """手动触发：同步系统模板到个人中心（同时刷新模板文件缓存）。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        # 先失效模板文件缓存，确保 _perform_sync 使用最新模板
        invalidate_templates_cache()

        count = await _perform_sync(user_id)
        return {"success": True, "synced_count": count, "message": f"成功同步 {count} 个模板"}
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").exception("sync_failed", "同步模板失败", error=e)
        raise HTTPException(status_code=500, detail=str(e))


# --- 动态路径参数 ---


@router.get("/{strategy_id}")
async def get_strategy_detail(strategy_id: str, request: Request):
    """获取策略详情。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()
        try:
            sid = int(strategy_id)
        except ValueError:
            sid = strategy_id

        strategy = await svc.get(sid, user_id=user_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="策略不存在")
        return strategy
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").exception("detail_failed", "获取策略详情失败", error=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{strategy_id}/activate")
async def activate_strategy(strategy_id: str, request: Request):
    """激活策略：将策略配置同步到 Redis 活跃池。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()
        try:
            sid = int(strategy_id)
        except ValueError:
            sid = strategy_id

        strategy = await svc.get(sid, user_id=user_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="策略不存在")

        is_verified = strategy.get("is_verified", False)
        if not is_verified and not str(strategy_id).startswith("sys_"):
            return {"success": False, "message": "策略尚未通过回测验证，无法激活交易"}

        normalized_user_id = normalize_user_id(user_id)
        tenant_id = _get_tenant_id(request)
        strategy_market = str(strategy.get("parameters", {}).get("market") or strategy.get("market") or "A").upper()
        config_to_cache = {
            "strategy_id": strategy_id,
            "user_id": normalized_user_id,
            "tenant_id": tenant_id,
            "name": strategy["name"],
            "activated_at": datetime.now().isoformat(),
            **strategy.get("parameters", {}),
        }

        redis = get_redis_sentinel_client()
        config_json = json.dumps(config_to_cache)
        redis.hset(ACTIVE_STRATEGIES_KEY, strategy_id, config_json)
        redis.publish("quantmind:events:strategy_activated", strategy_id)

        # 同步写入 trade-redis，确保 VectorizedMatcher（运行于 engine 服务）
        # 和未来其他消费者在 trade-redis 侧也能读到 active_strategies
        trade_redis = _get_trade_redis()
        if trade_redis:
            try:
                trade_redis.hset(ACTIVE_STRATEGIES_KEY, strategy_id, config_json)
            except Exception as e:
                StructuredTaskLogger(logger, "user-strategies", {"strategy_id": strategy_id}).warning(
                    "trade_redis_sync_failed", "同步写入 trade-redis 失败（不影响主流程）", error=e
                )

        asyncio.create_task(
            _trigger_inference_after_activate(
                strategy_id=str(strategy_id),
                tenant_id=tenant_id,
                user_id=normalized_user_id,
                market=strategy_market,
            )
        )

        StructuredTaskLogger(
            logger,
            "user-strategies",
            {"strategy_id": strategy_id, "user_id": normalized_user_id, "tenant_id": tenant_id},
        ).info("activate", "策略已激活")
        return {"success": True, "message": "策略已成功激活", "data": config_to_cache}
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies", {"strategy_id": strategy_id}).exception(
            "activate_failed", "激活策略失败", error=e
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{strategy_id}/activate")
async def deactivate_strategy(strategy_id: str, request: Request):
    """停用策略：从 Redis 活跃池中移除。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        redis = get_redis_sentinel_client()
        redis.hdel(ACTIVE_STRATEGIES_KEY, strategy_id)

        # 同步从 trade-redis 移除
        trade_redis = _get_trade_redis()
        if trade_redis:
            try:
                trade_redis.hdel(ACTIVE_STRATEGIES_KEY, strategy_id)
            except Exception as e:
                StructuredTaskLogger(logger, "user-strategies", {"strategy_id": strategy_id}).warning(
                    "trade_redis_remove_failed", "从 trade-redis 移除失败（不影响主流程）", error=e
                )

        return {"success": True, "message": "策略已停用"}
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies", {"strategy_id": strategy_id}).exception(
            "deactivate_failed", "停用策略失败", error=e
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{strategy_id}")
async def delete_user_strategy(strategy_id: str, request: Request):
    """删除策略。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()

        # 检查是否为活跃状态，活跃状态不允许直接删除
        redis = get_redis_sentinel_client()
        if redis.hexists(ACTIVE_STRATEGIES_KEY, str(strategy_id)):
            return {"success": False, "message": "该策略正在实盘运行，请先停用后再删除"}

        result = await svc.delete(strategy_id, user_id=user_id)
        if not result:
            raise HTTPException(status_code=404, detail="策略不存在或无权删除")

        return {"success": True, "message": "策略已成功删除"}
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies", {"strategy_id": strategy_id}).exception(
            "delete_failed", "删除策略失败", error=e
        )
        raise HTTPException(status_code=500, detail=str(e))
