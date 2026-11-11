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
    from backend.shared.strategy_template_sync import sync_builtin_templates
    from backend.shared.utils import normalize_user_id
except ImportError:
    from shared.database_manager_v2 import get_session  # type: ignore
    from shared.strategy_storage import get_strategy_storage_service  # type: ignore
    from shared.redis_sentinel_client import get_redis_sentinel_client  # type: ignore
    from shared.strategy_template_sync import sync_builtin_templates  # type: ignore
    from shared.utils import normalize_user_id  # type: ignore

from backend.services.engine.qlib_app.services.strategy_templates import (
    get_all_templates,
    invalidate_templates_cache,
)
from backend.services.engine.qlib_app.utils.structured_logger import (
    StructuredTaskLogger,
)

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
        StructuredTaskLogger(logger, "user-strategies").warning(
            "trade_redis_unavailable", "无法连接 trade-redis", error=e
        )
        return None


def _is_valid_bearer_jwt(auth_header: str) -> bool:
    token = str(auth_header or "").strip()
    if not token.lower().startswith("bearer "):
        return False
    raw_token = token[7:].strip()
    parts = raw_token.split(".")
    return len(parts) == 3 and all(part.strip() for part in parts)


async def _trigger_inference_after_activate(
    *, strategy_id: str, tenant_id: str, user_id: str, market: str = "A"
) -> None:
    """
    策略激活后异步触发一次推理发布（按 tenant_id + user_id 定向产信号）。
    """
    try:
        from backend.services.engine.inference.router_service import (
            InferenceRouterService,
        )
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").warning(
            "inference_import_failed", "导入 InferenceRouterService 失败", error=e
        )
        return

    from backend.services.engine.data_platform.calendars.calendar import get_calendar

    market_upper = (market or "A").upper()
    _MARKET_XCAL = {"A": "XSHG", "HK": "XHKG", "US": "XNYS"}
    _MARKET_TZ = {
        "A": "Asia/Shanghai",
        "HK": "Asia/Hong_Kong",
        "US": "America/New_York",
        "CRYPTO": "UTC",
    }

    tz_name = _MARKET_TZ.get(market_upper, "Asia/Shanghai")
    now_local = datetime.now(ZoneInfo(tz_name))

    if market_upper == "CRYPTO":
        # 加密货币 7×24，每天都交易
        data_trade_date_obj = now_local.date()
    else:
        xcal_name = _MARKET_XCAL.get(market_upper, "XSHG")
        cal = xcals.get_calendar(xcal_name)
        data_trade_date_obj = (
            cal.previous_session(now_local.date()).date()
            if now_local.time() < datetime.strptime("09:30", "%H:%M").time()
            else now_local.date()
        )
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
                {
                    "strategy_id": strategy_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            ).info(
                "inference_skipped",
                "已有同用户同日推理在执行/完成，跳过",
                date=prediction_trade_date,
            )
            return
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").warning(
            "inference_lock_failed", "Redis 锁获取失败，降级继续执行", error=e
        )

    try:
        router_service = InferenceRouterService()
        result = await asyncio.to_thread(
            router_service.run_daily_inference_script,
            date=data_trade_date,
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            redis_client=redis,
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
            # OSS 单容器（SERVICE_MODE=all）下 trade 同进程监听 8002，直连本机；
            # 只有分容器部署才走 quantmind-trade 域名。
            if os.getenv("SERVICE_MODE", "").strip().lower() == "all":
                return "http://127.0.0.1:8002/api/v1/real-trading/status"
            return "http://quantmind-trade:8002/api/v1/real-trading/status"
        return f"{direct.rstrip('/')}/api/v1/real-trading/status"
    if os.getenv("SERVICE_MODE", "").strip().lower() == "all":
        return "http://127.0.0.1:8002/api/v1/real-trading/status"
    return "http://quantmind-trade:8002/api/v1/real-trading/status"


def _fetch_local_active_configs() -> list[dict[str, Any]]:
    """直读 engine 激活写入的 Redis 活跃池（quantmind:active_strategies）。

    策略列表的运行态/收益归因不再强依赖 trade 服务 HTTP（分容器域名在
    OSS 单容器下不通、portfolios 表常为空）。trade HTTP 仅作实盘补充。
    任何失败返回 []，调用方回退原逻辑。
    """
    try:
        try:
            redis = get_redis_sentinel_client()
        except NameError:
            return []
        if redis is None:
            return []
        raw = redis.hgetall(ACTIVE_STRATEGIES_KEY) or {}
    except Exception as exc:
        logger.warning("local active strategies read failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    try:
        items = raw.items() if isinstance(raw, dict) else []
    except Exception:
        return []
    for field, payload in items:
        try:
            data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data = dict(data)
        try:
            field_str = field.decode() if isinstance(field, bytes) else str(field)
        except Exception:
            field_str = str(field)
        data.setdefault("strategy_id", field_str)
        out.append(data)
    return out


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


async def _fetch_latest_backtest_summaries(
    user_id: str, tenant_id: str
) -> dict[str, dict[str, Any]]:
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
            "last_update": _to_iso_string(
                payload.get("last_update")
                or row.get("completed_at")
                or row.get("created_at")
            ),
            "execution_latency_ms": (
                int(float(payload.get("execution_time")) * 1000)
                if payload.get("execution_time") is not None
                else None
            ),
        }
    return summaries


async def _perform_sync(user_id: str):
    """将内置模板同步到用户个人策略库；去重键为 strategy_type / template:<id> / 同名。"""
    return await sync_builtin_templates(user_id)


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


_STRATEGY_CODE_KWARG_KEYS = (
    "topk",
    "n_drop",
    "rebalance_days",
    "weight_mode",
    "min_score",
    "max_position_pct",
    "lot_size",
)


def _extract_strategy_config_kwargs(code_str: Any) -> dict[str, Any]:
    """从策略代码 STRATEGY_CONFIG.kwargs 提取选股参数。

    与 simulation engine 侧解析口径一致（AST 字面量解析）。
    解析失败返回 {}，调用方回退旧 parameters，不抛错。
    """
    import ast

    if not code_str or not str(code_str).strip():
        return {}
    try:
        tree = ast.parse(str(code_str))
    except Exception:
        return {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(t, ast.Name) and t.id == "STRATEGY_CONFIG" for t in targets
        ):
            continue
        try:
            cfg = ast.literal_eval(node.value)
        except Exception:
            return {}
        if not isinstance(cfg, dict):
            return {}
        kwargs = cfg.get("kwargs") if isinstance(cfg.get("kwargs"), dict) else {}
        merged = {**kwargs}
        for key in _STRATEGY_CODE_KWARG_KEYS:
            if key in cfg and key not in merged:
                merged[key] = cfg[key]
        return {k: merged[k] for k in _STRATEGY_CODE_KWARG_KEYS if k in merged}
    return {}


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
    # 个人收藏（星标）：仅属主可见可改，与社区 like_count 无关
    is_favorite: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)


class StrategyListResponse(BaseModel):
    total: int
    strategies: list[StrategyListItem]


def _get_sim_trade_redis():
    """直连模拟账户所在的 trade Redis DB（`REDIS_DB_TRADE`，默认 2）。

    engine 的 sentinel 客户端固定在 DB 0，读不到 `simulation:account:*`；
    此处短直连只读实时 total，不写。失败返回 None，调用方回退 PG 快照。
    """
    try:
        from redis import Redis

        host = (os.getenv("REDIS_HOST") or "").strip()
        if not host:
            host = "quantmind-redis" if os.path.exists("/.dockerenv") else "127.0.0.1"
        elif os.path.exists("/.dockerenv") and host in {"localhost", "127.0.0.1"}:
            host = "quantmind-redis"
        client = Redis(
            host=host,
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB_TRADE", "2")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
        )
        client.ping()
        return client
    except Exception as exc:
        logger.warning("sim trade redis unavailable: %s", exc)
        return None


def _read_sim_live_account(
    redis_client: Any, tenant_id: str, user_sub: str
) -> tuple[float, float] | None:
    """读实时模拟账户：跨市场 total 累加 + settings 初始资金。

    与 `SimulationFundSnapshotService.capture_all` 合并口径一致
    （同一用户 CN/HK/US 账户累加）。返回 (live_total, initial_capital)，
    无账户返回 None。
    """
    import json as _json

    from backend.shared.simulation_account_keys import (
        ledger_user_id_candidates,
        parse_account_key,
        settings_lookup_keys,
    )

    tenant = str(tenant_id or "default")
    aliases = set(ledger_user_id_candidates(user_sub))
    try:
        keys = list(
            redis_client.scan_iter(match=f"simulation:account:{tenant}:*", count=500)
        )
    except Exception as exc:
        logger.warning("sim live account scan failed: %s", exc)
        return None
    live_total = 0.0
    found = False
    for key in keys:
        try:
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            parsed = parse_account_key(key_str)
            if not parsed:
                continue
            key_tenant, key_user, _market = parsed
            if key_tenant != tenant or key_user not in aliases:
                continue
            raw = redis_client.get(key_str)
            if not raw:
                continue
            account = _json.loads(raw)
            if not isinstance(account, dict):
                continue
            live_total += float(account.get("total_asset") or 0.0)
            found = True
        except Exception:
            continue
    if not found:
        return None
    initial_capital = 0.0
    try:
        for skey in settings_lookup_keys(tenant, user_sub):
            sraw = redis_client.get(skey)
            if not sraw:
                continue
            sdata = _json.loads(sraw)
            if isinstance(sdata, dict) and sdata.get("initial_cash") is not None:
                initial_capital = float(sdata.get("initial_cash") or 0.0)
                break
    except Exception:
        pass
    return live_total, initial_capital


async def _query_sim_pg_snapshot_row(
    tenant_id: str, user_sub: str
) -> tuple[float, float, float, float] | None:
    """读 PG 资金快照最新行：(total_asset, today_pnl, total_pnl, initial_capital)。"""
    from backend.shared.simulation_account_keys import ledger_user_id_candidates

    candidates = ledger_user_id_candidates(user_sub)
    if not candidates:
        return None
    preferred = candidates[0]
    try:
        async with get_session(read_only=True) as session:
            placeholders = ",".join(f":u{i}" for i in range(len(candidates)))
            params: dict[str, Any] = {
                "tid": str(tenant_id or "default"),
                "preferred": preferred,
            }
            params.update({f"u{i}": c for i, c in enumerate(candidates)})
            row = (
                await session.execute(
                    text(
                        "SELECT total_asset, today_pnl, total_pnl, initial_capital "
                        "FROM simulation_fund_snapshots "
                        f"WHERE tenant_id = :tid AND CAST(user_id AS varchar) IN ({placeholders}) "
                        "ORDER BY snapshot_date DESC, "
                        "CASE WHEN CAST(user_id AS varchar) = :preferred THEN 0 ELSE 1 END "
                        "LIMIT 1"
                    ),
                    params,
                )
            ).fetchone()
    except Exception as exc:
        logger.warning("sim fund fallback query failed: %s", exc)
        return None
    if not row:
        return None
    try:
        return (
            float(row[0] or 0.0),
            float(row[1] or 0.0),
            float(row[2] or 0.0),
            float(row[3] or 0.0),
        )
    except Exception:
        return None


async def _fetch_sim_fund_fallback(
    tenant_id: str, user_sub: str
) -> dict[str, float] | None:
    """模拟盘收益兜底（策略监控用），口径与资金概览对齐。

    资金概览（`simulation.py` 账户接口）：total 取 Redis 实时，
    日初基线取 PG `get_baselines`，今日收益 = 今日盈亏 / 日初权益。
    此前这里只读 PG 快照最新行：EOD 落库后周期采集当天不再更新
    （`capture_all(skip_eod_completed=True)`），盘后实时重估只进 Redis
    不进 PG，策略卡片显示的是 EOD 时刻陈旧值；且今日收益误用初始资金
    作分母。现改为 Redis 实时优先，PG 快照仅作 Redis 不可达时的降级
    （分母同样按日初修正）。
    """
    from decimal import Decimal

    from backend.shared.simulation_account_keys import ledger_user_id_candidates

    tenant = str(tenant_id or "default")
    candidates = ledger_user_id_candidates(user_sub)
    preferred = candidates[0] if candidates else str(user_sub)

    try:
        from backend.services.simulation.services.fund_snapshot_service import (
            SimulationFundSnapshotService,
        )
    except Exception as exc:
        logger.warning("sim fund baselines import failed: %s", exc)
        SimulationFundSnapshotService = None  # type: ignore[no-redef]

    async def _day_open(initial: float) -> float:
        if SimulationFundSnapshotService is None:
            return initial
        try:
            baselines = await SimulationFundSnapshotService.get_baselines(
                tenant_id=tenant,
                user_id=preferred,
                initial_capital=Decimal(str(initial)),
            )
            return float(baselines.get("day_open_equity") or initial)
        except Exception as exc:
            logger.warning("sim fund baselines query failed: %s", exc)
            return initial

    # 1) Redis 实时优先
    redis_client = _get_sim_trade_redis()
    if redis_client is not None:
        try:
            live = _read_sim_live_account(redis_client, tenant, user_sub)
        except Exception as exc:
            logger.warning("sim live account read failed: %s", exc)
            live = None
        finally:
            try:
                redis_client.close()
            except Exception:
                pass
        if live is not None:
            live_total, initial = live
            if initial <= 0:
                pg_row = await _query_sim_pg_snapshot_row(tenant, user_sub)
                if pg_row is not None and pg_row[3] > 0:
                    initial = pg_row[3]
                else:
                    initial = live_total
            day_open = await _day_open(initial if initial > 0 else live_total)
            today_pnl = live_total - day_open
            total_pnl = live_total - initial if initial > 0 else 0.0
            return {
                "today_pnl": today_pnl,
                "total_pnl": total_pnl,
                "today_return": (
                    today_pnl / day_open * 100.0 if day_open > 0 else 0.0
                ),
                "total_return": (
                    total_pnl / initial * 100.0 if initial > 0 else 0.0
                ),
            }

    # 2) PG 快照降级：total 为快照时刻值，今日盈亏按日初基线重算
    pg_row = await _query_sim_pg_snapshot_row(tenant, user_sub)
    if pg_row is None:
        return None
    snap_total, _snap_today, snap_total_pnl, initial = pg_row
    if initial <= 0:
        initial = snap_total
    day_open = await _day_open(initial)
    today_pnl = snap_total - day_open
    total_pnl = snap_total - initial if initial > 0 else snap_total_pnl
    return {
        "today_pnl": today_pnl,
        "total_pnl": total_pnl,
        # 今日收益分母必须为日初权益（与资金概览/strategy_monitor_pusher 一致），
        # 此前误用初始资金，轻仓/浮盈时会系统性偏小。
        "today_return": (today_pnl / day_open * 100.0 if day_open > 0 else 0.0),
        "total_return": (total_pnl / initial * 100.0 if initial > 0 else 0.0),
    }


async def _fetch_real_trading_status(request: Request) -> dict[str, Any] | None:
    auth_header = request.headers.get("authorization")
    if not auth_header or not _is_valid_bearer_jwt(auth_header):
        StructuredTaskLogger(logger, "user-strategies").warning(
            "real_trading_skip", "skip real-trading/status due to invalid auth header"
        )
        return None
    tenant_id = str(
        (getattr(request.state, "user", {}) or {}).get("tenant_id") or "default"
    )
    headers = {"Authorization": auth_header, "X-Tenant-Id": tenant_id}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            resp = await client.get(_resolve_trade_service_url(), headers=headers)
        if resp.status_code != 200:
            StructuredTaskLogger(logger, "user-strategies").warning(
                "real_trading_failed",
                "fetch real-trading/status failed",
                status=resp.status_code,
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
):
    """获取当前用户的策略列表。如果是新用户则自动初始化模板。"""
    try:
        user_id = _get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")

        svc = get_strategy_storage_service()
        tag_list = tags.split(",") if tags else None
        tenant_id = _get_tenant_id(request)

        # 注释承诺过「新用户自动初始化模板」，先前漏实现。
        # 每次列表先补齐缺失内置策略；已有同 strategy_type / template:id / 同名则跳过。
        try:
            await _perform_sync(user_id)
        except Exception as e:
            StructuredTaskLogger(logger, "user-strategies").warning(
                "auto_sync_failed", "列表自动同步模板失败，继续返回已有策略", error=e
            )

        items = await asyncio.to_thread(
            svc.list, user_id=user_id, category=category, search=search, tags=tag_list
        )

        backtest_summaries = await _fetch_latest_backtest_summaries(
            user_id=user_id, tenant_id=tenant_id
        )
        trading_status = await _fetch_real_trading_status(request)
        runtime_state = _normalize_runtime_state((trading_status or {}).get("status"))
        strategy_payload = (
            (trading_status or {}).get("strategy")
            if isinstance(trading_status, dict)
            else {}
        )
        if not isinstance(strategy_payload, dict):
            strategy_payload = {}
        active_strategy_id = str(strategy_payload.get("id") or "").strip()
        active_strategy_name = str(strategy_payload.get("name") or "").strip().lower()
        active_template_id = (
            active_strategy_id.replace("sys_", "", 1)
            if active_strategy_id.startswith("sys_")
            else ""
        )
        trade_portfolio = (
            (trading_status or {}).get("portfolio")
            if isinstance(trading_status, dict)
            else None
        )
        # 模拟模式兜底：portfolios 表为空时用模拟账户资金快照归因活跃策略。
        # 实盘模式不启用（避免模拟/实盘混淆）。
        sim_fund: dict[str, float] | None = None
        try:
            sim_mode = (
                str((trading_status or {}).get("mode") or "SIMULATION").strip().upper()
            )
        except Exception:
            sim_mode = "SIMULATION"
        if sim_mode == "SIMULATION":
            sim_fund = await _fetch_sim_fund_fallback(tenant_id, user_id)
        # 本地活跃池（activate 写入，不依赖跨服务 HTTP，直接判定运行态）。
        local_active_cfgs = _fetch_local_active_configs()
        try:
            my_uids = {
                normalize_user_id(user_id),
                str(user_id or "").strip(),
            }
        except Exception:
            my_uids = {str(user_id or "").strip()}

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
            item_parameters = (
                item.get("parameters")
                if isinstance(item.get("parameters"), dict)
                else {}
            )
            item_strategy_type = (
                str(item_parameters.get("strategy_type") or "").strip().lower()
            )

            is_active_item = False
            if active_strategy_id and item_id == active_strategy_id:
                is_active_item = True
            elif (
                active_template_id and item_strategy_type == active_template_id.lower()
            ):
                is_active_item = True
            elif active_strategy_name and item_name == active_strategy_name:
                is_active_item = True

            # 本地活跃池兜底：trade HTTP 不通时仍能判定运行态（同用户+租户才算）。
            local_match = False
            if not is_active_item and local_active_cfgs:
                for cfg in local_active_cfgs:
                    try:
                        if str(cfg.get("tenant_id") or "default") != tenant_id:
                            continue
                        if str(cfg.get("user_id") or "") not in my_uids:
                            continue
                        cfg_sid = str(cfg.get("strategy_id") or "").strip()
                        cfg_name = str(cfg.get("name") or "").strip().lower()
                        cfg_type = str(cfg.get("strategy_type") or "").strip().lower()
                        if (
                            (cfg_sid and cfg_sid == item_id)
                            or (cfg_type and cfg_type == item_strategy_type)
                            or (cfg_name and cfg_name == item_name)
                        ):
                            local_match = True
                            break
                    except Exception:
                        continue
            is_active_item = is_active_item or local_match

            item_runtime_state = runtime_state if is_active_item else None
            if item_runtime_state is None and local_match:
                # 跨服务状态不可达时，本地激活记录即运行态。
                item_runtime_state = "running"
            effective_status = item_runtime_state or _base_to_effective_status(
                base_status
            )
            summary = backtest_summaries.get(item_id, {})
            summary_total_return = summary.get("total_return")
            summary_today_return = summary.get("today_return")
            summary_risk_level = summary.get("risk_level")
            summary_execution_latency = summary.get("execution_latency_ms")
            risk_level = (
                summary_risk_level
                if isinstance(summary_risk_level, str) and summary_risk_level
                else (
                    item.get("parameters", {}).get("risk_level")
                    if isinstance(item.get("parameters"), dict)
                    else None
                )
            )
            if not isinstance(risk_level, str) or not risk_level.strip():
                risk_level = "medium"

            total_return = _to_float(summary_total_return, 0.0)
            if total_return == 0.0 and summary.get("status") == "failed":
                total_return = 0.0
            today_return = _to_float(summary_today_return, 0.0)
            today_pnl = 0.0
            # P0-STRAT：运行态匹配用模糊口径（is_active_item，与 effective_status
            # 一致），否则 sys_ 模板策略永远走不到收益分支。
            if is_active_item:
                trade_today_return = (trading_status or {}).get("daily_return")
                if trade_today_return is None and isinstance(trade_portfolio, dict):
                    trade_today_return = trade_portfolio.get("daily_return")
                trade_daily_pnl = (trading_status or {}).get("daily_pnl")
                if trade_daily_pnl is None and isinstance(trade_portfolio, dict):
                    trade_daily_pnl = trade_portfolio.get("daily_pnl")
                # 模拟盘收益以实时账户为准（与资金概览同口径），避免空 portfolios 行
                # 的 0 盈亏盖住真实账户；活跃策略无条件覆盖，回测摘要的陈旧 total
                # 不得残留（此前仅 total==0 时覆盖，陈旧非零值会一直显示）。
                if sim_mode == "SIMULATION" and sim_fund is not None:
                    today_pnl = float(sim_fund["today_pnl"])
                    today_return = float(sim_fund["today_return"])
                    total_return = float(sim_fund["total_return"])
                else:
                    if trade_today_return is not None:
                        today_return = _to_float(trade_today_return, today_return)
                    elif trade_daily_pnl is not None and isinstance(trade_portfolio, dict):
                        initial_capital = _to_float(
                            trade_portfolio.get("initial_capital"), 0.0
                        )
                        if initial_capital > 0:
                            today_return = (
                                _to_float(trade_daily_pnl, 0.0) / initial_capital * 100.0
                            )
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
                    last_failed_at=summary.get("completed_at")
                    if summary.get("status") == "failed"
                    else None,
                    last_signal_at=summary.get("created_at"),
                    execution_latency_ms=execution_latency_ms,
                    parameters=item.get("parameters") or {},
                    category=item.get("tags", [None])[0]
                    if item.get("tags") and item.get("is_system")
                    else "db_stored",
                    is_system=bool(item.get("is_system", False)),
                    is_favorite=bool(item.get("is_favorite", False)),
                )
            )

        return StrategyListResponse(total=len(strategies), strategies=strategies)
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").exception(
            "list_failed", "获取策略列表失败", error=e
        )
        raise HTTPException(status_code=500, detail=str(e))


# --- 重要：静态路径必须放在动态路径 {strategy_id} 之前 ---


@router.post("/{strategy_id}/favorite")
async def favorite_strategy(strategy_id: str, request: Request):
    """收藏个人策略（幂等置位）。只能操作自己的策略。"""
    return await _set_strategy_favorite(strategy_id, request, True)


@router.delete("/{strategy_id}/favorite")
async def unfavorite_strategy(strategy_id: str, request: Request):
    """取消收藏个人策略（幂等清除）。只能操作自己的策略。"""
    return await _set_strategy_favorite(strategy_id, request, False)


async def _set_strategy_favorite(
    strategy_id: str, request: Request, favorite: bool
) -> dict[str, Any]:
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="未认证")
    if not str(strategy_id).isdigit():
        # 系统模板（sys_ 开头）不可收藏
        raise HTTPException(status_code=404, detail="策略不存在")

    svc = get_strategy_storage_service()
    try:
        sid = int(strategy_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="策略不存在")

    # svc.get 按 user_id 过滤：非属主直接 404，避免越权
    existing = await svc.get(sid, user_id=user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="策略不存在")

    ok = await svc.set_favorite(sid, user_id, favorite)
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"strategy_id": str(sid), "is_favorite": bool(favorite)}


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
        StructuredTaskLogger(logger, "user-strategies").exception(
            "create_failed", "创建策略失败", error=e
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{strategy_id}")
async def update_strategy(
    strategy_id: str, request: Request, body: StrategyUpdateRequest
):
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
        new_description = (
            body.description
            if body.description is not None
            else existing.get("description", "")
        )
        new_tags = body.tags if body.tags is not None else existing.get("tags", [])
        if body.parameters is not None:
            new_parameters = dict(body.parameters)
            # 显式 parameters 优先，但 code 里新增的 kwarg 键做缺失回填
            if body.code is not None:
                for k, v in _extract_strategy_config_kwargs(body.code).items():
                    new_parameters.setdefault(k, v)
        else:
            new_parameters = dict(existing.get("parameters", {}) or {})
            # code 更新但没带 parameters 时，用新代码 STRATEGY_CONFIG 覆盖旧值，
            # 否则 DB 里陈旧 topk=50 会一直屏蔽代码修改（模拟盘表现为改完仍跑50只）
            if body.code is not None:
                new_parameters.update(_extract_strategy_config_kwargs(new_code))

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
        StructuredTaskLogger(
            logger, "user-strategies", {"strategy_id": strategy_id}
        ).exception("update_failed", "更新策略失败", error=e)
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
        return {
            "success": True,
            "synced_count": count,
            "message": f"成功同步 {count} 个模板",
        }
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(logger, "user-strategies").exception(
            "sync_failed", "同步模板失败", error=e
        )
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
        StructuredTaskLogger(logger, "user-strategies").exception(
            "detail_failed", "获取策略详情失败", error=e
        )
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
        strategy_market = str(
            strategy.get("parameters", {}).get("market")
            or strategy.get("market")
            or "A"
        ).upper()
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
                StructuredTaskLogger(
                    logger, "user-strategies", {"strategy_id": strategy_id}
                ).warning(
                    "trade_redis_sync_failed",
                    "同步写入 trade-redis 失败（不影响主流程）",
                    error=e,
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
            {
                "strategy_id": strategy_id,
                "user_id": normalized_user_id,
                "tenant_id": tenant_id,
            },
        ).info("activate", "策略已激活")
        return {"success": True, "message": "策略已成功激活", "data": config_to_cache}
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(
            logger, "user-strategies", {"strategy_id": strategy_id}
        ).exception("activate_failed", "激活策略失败", error=e)
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
                StructuredTaskLogger(
                    logger, "user-strategies", {"strategy_id": strategy_id}
                ).warning(
                    "trade_redis_remove_failed",
                    "从 trade-redis 移除失败（不影响主流程）",
                    error=e,
                )

        return {"success": True, "message": "策略已停用"}
    except HTTPException:
        raise
    except Exception as e:
        StructuredTaskLogger(
            logger, "user-strategies", {"strategy_id": strategy_id}
        ).exception("deactivate_failed", "停用策略失败", error=e)
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
        StructuredTaskLogger(
            logger, "user-strategies", {"strategy_id": strategy_id}
        ).exception("delete_failed", "删除策略失败", error=e)
        raise HTTPException(status_code=500, detail=str(e))
