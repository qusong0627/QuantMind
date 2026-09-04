import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from backend.services.stream.market_app.api.v1 import (
    klines_router,
    quotes_router,
    symbols_router,
)
from backend.services.stream.market_app.database import AsyncSessionLocal, close_db, init_db
from backend.services.stream.ws_core.manager import manager as ws_manager
from backend.services.stream.ws_core.server import server as ws_server
from backend.services.stream.ws_core.server import websocket_endpoint as core_ws_endpoint
from backend.shared.auth import get_internal_call_secret
from backend.shared.config_manager import init_unified_config
from backend.shared.cors import resolve_cors_origins
from backend.shared.error_contract import install_error_contract_handlers
from backend.shared.logging_config import get_logger, setup_logging
from backend.shared.openapi_utils import quantmind_generate_unique_id
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.service_health_metrics import set_service_health

setup_logging("quantmind-stream")
logger = get_logger(__name__)


def _as_bool(raw: str, default: bool = True) -> bool:
    value = (raw or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _quote_cleanup_enabled() -> bool:
    return _as_bool(os.getenv("QUOTE_CLEANUP_ENABLED", "true"), default=True)


def _quote_cleanup_interval_seconds() -> int:
    raw = (os.getenv("QUOTE_CLEANUP_INTERVAL_SECONDS", "600") or "").strip()
    try:
        value = int(raw)
    except Exception:
        value = 600
    return max(60, value)


def _quote_cleanup_keep_today_only() -> bool:
    return _as_bool(os.getenv("QUOTE_CLEANUP_KEEP_TODAY_ONLY", "true"), default=True)


def _quote_daily_archive_enabled() -> bool:
    return _as_bool(os.getenv("QUOTE_DAILY_ARCHIVE_ENABLED", "true"), default=True)


async def _archive_quotes_before_today(session) -> int:
    """
    将 quotes 中历史数据（日<今天）聚合写入 quote_daily_summaries。
    返回本次 upsert 影响的汇总行数。
    """
    result = await session.execute(text("""
            WITH base AS (
                SELECT
                    q.id,
                    q.symbol,
                    COALESCE(NULLIF(q.data_source, ''), 'remote_redis') AS data_source,
                    q.timestamp,
                    q.timestamp::date AS trade_date,
                    q.current_price,
                    q.open_price,
                    q.high_price,
                    q.low_price,
                    q.close_price,
                    COALESCE(q.volume, 0)::bigint AS volume,
                    COALESCE(q.amount, 0)::double precision AS amount
                FROM quotes q
                WHERE q.timestamp::date < CURRENT_DATE
            ),
            agg AS (
                SELECT
                    trade_date,
                    symbol,
                    data_source,
                    MAX(COALESCE(high_price, current_price)) AS high_price,
                    MIN(COALESCE(low_price, current_price)) AS low_price,
                    AVG(current_price) AS avg_price,
                    SUM(volume) AS volume_sum,
                    SUM(amount) AS amount_sum,
                    COUNT(1)::int AS quote_count,
                    MIN(timestamp) AS first_quote_at,
                    MAX(timestamp) AS last_quote_at
                FROM base
                GROUP BY trade_date, symbol, data_source
            ),
            open_px AS (
                SELECT DISTINCT ON (trade_date, symbol, data_source)
                    trade_date,
                    symbol,
                    data_source,
                    COALESCE(open_price, current_price) AS open_price
                FROM base
                ORDER BY trade_date, symbol, data_source, timestamp ASC, id ASC
            ),
            close_px AS (
                SELECT DISTINCT ON (trade_date, symbol, data_source)
                    trade_date,
                    symbol,
                    data_source,
                    COALESCE(close_price, current_price) AS close_price
                FROM base
                ORDER BY trade_date, symbol, data_source, timestamp DESC, id DESC
            ),
            upserted AS (
                INSERT INTO quote_daily_summaries (
                    trade_date,
                    symbol,
                    data_source,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    avg_price,
                    volume_sum,
                    amount_sum,
                    quote_count,
                    first_quote_at,
                    last_quote_at,
                    created_at,
                    updated_at
                )
                SELECT
                    a.trade_date,
                    a.symbol,
                    a.data_source,
                    o.open_price,
                    a.high_price,
                    a.low_price,
                    c.close_price,
                    a.avg_price,
                    a.volume_sum,
                    a.amount_sum,
                    a.quote_count,
                    a.first_quote_at,
                    a.last_quote_at,
                    NOW(),
                    NOW()
                FROM agg a
                LEFT JOIN open_px o
                    ON o.trade_date = a.trade_date
                    AND o.symbol = a.symbol
                    AND o.data_source = a.data_source
                LEFT JOIN close_px c
                    ON c.trade_date = a.trade_date
                    AND c.symbol = a.symbol
                    AND c.data_source = a.data_source
                ON CONFLICT (trade_date, symbol, data_source)
                DO UPDATE SET
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    avg_price = EXCLUDED.avg_price,
                    volume_sum = EXCLUDED.volume_sum,
                    amount_sum = EXCLUDED.amount_sum,
                    quote_count = EXCLUDED.quote_count,
                    first_quote_at = EXCLUDED.first_quote_at,
                    last_quote_at = EXCLUDED.last_quote_at,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(1) AS cnt FROM upserted
            """))
    return int(result.scalar() or 0)


async def _cleanup_quotes_keep_today_only(session) -> int:
    """
    只保留 quotes 当天数据（按数据库 CURRENT_DATE 口径）。
    """
    result = await session.execute(text("DELETE FROM quotes WHERE timestamp::date < CURRENT_DATE"))
    return int(result.rowcount or 0)


async def _quote_cleanup_loop(stop_event: asyncio.Event) -> None:
    interval = _quote_cleanup_interval_seconds()
    keep_today_only = _quote_cleanup_keep_today_only()
    if not keep_today_only:
        logger.info("🧹 Quote cleanup disabled by QUOTE_CLEANUP_KEEP_TODAY_ONLY=false")
        return

    logger.info(
        "🧹 Quote cleanup loop started: keep_today_only=true, archive_enabled=%s, interval=%ss",
        _quote_daily_archive_enabled(),
        interval,
    )
    while not stop_event.is_set():
        try:
            archived = 0
            deleted = 0
            async with AsyncSessionLocal() as session:
                try:
                    if _quote_daily_archive_enabled():
                        archived = await _archive_quotes_before_today(session)
                    deleted = await _cleanup_quotes_keep_today_only(session)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
            logger.info(
                "🧹 Quote cleanup completed at %s, archived_rows=%s, deleted_rows=%s",
                datetime.now().isoformat(timespec="seconds"),
                archived,
                deleted,
            )
        except Exception as e:
            logger.error("Quote cleanup failed: %s", e, exc_info=True)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


try:
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest

    def _create_gauge(name: str, documentation: str) -> Gauge:
        collector = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
        if collector is not None:
            return collector  # type: ignore[return-value]
        return Gauge(name, documentation)

    STREAM_MARKET_DB_CONNECTED = _create_gauge(
        "stream_market_db_connected",
        "Whether stream market database is connected (1 connected, 0 disconnected)",
    )
    STREAM_SERVICE_DEGRADED = _create_gauge(
        "stream_service_degraded",
        "Whether stream service is running in degraded mode (1 degraded, 0 healthy)",
    )
except Exception:  # pragma: no cover - metrics is optional
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    STREAM_MARKET_DB_CONNECTED = None
    STREAM_SERVICE_DEGRADED = None

    def generate_latest() -> bytes:
        return b"# metrics unavailable\n"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.market_db_connected = False
    app.state.ws_core_started = False
    app.state.quote_cleanup_task = None
    app.state.quote_cleanup_stop = None

    try:
        await init_unified_config(service_name="quantmind-stream")
        logger.info("✅ QuantMind Stream Unified Config Loaded")
    except Exception as e:
        logger.error(f"❌ Unified config load failed: {e}")

    try:
        await init_db()
        app.state.market_db_connected = True
        set_service_health("quantmind-stream", True)
        if STREAM_MARKET_DB_CONNECTED is not None:
            STREAM_MARKET_DB_CONNECTED.set(1)
        if STREAM_SERVICE_DEGRADED is not None:
            STREAM_SERVICE_DEGRADED.set(0)
        logger.info("✅ Market Database initialized")
    except Exception as e:
        app.state.market_db_connected = False
        set_service_health("quantmind-stream", False)
        if STREAM_MARKET_DB_CONNECTED is not None:
            STREAM_MARKET_DB_CONNECTED.set(0)
        if STREAM_SERVICE_DEGRADED is not None:
            STREAM_SERVICE_DEGRADED.set(1)
        logger.warning(f"⚠️ Market Database initialization failed, continue in degraded mode: {e}")

    try:
        await ws_server.start()
        app.state.ws_core_started = True
        logger.info("✅ WebSocket Core Server started")
    except Exception as e:
        app.state.ws_core_started = False
        logger.error(f"❌ WebSocket Core Server failed to start: {e}")

    if _quote_cleanup_enabled():
        stop_event = asyncio.Event()
        app.state.quote_cleanup_stop = stop_event
        app.state.quote_cleanup_task = asyncio.create_task(_quote_cleanup_loop(stop_event))
    else:
        logger.info("🧹 Quote cleanup disabled by QUOTE_CLEANUP_ENABLED=false")

    try:
        from backend.shared.system_events import record_system_event_async

        ok = bool(getattr(app.state, "startup_healthy", True))
        await record_system_event_async(
            event_type="service_lifecycle",
            level="info" if ok else "error",
            source="quantmind-stream",
            title="行情服务启动完成" if ok else "行情服务启动异常",
            message="QuantMind Stream 启动完成" if ok else "Stream 启动存在初始化失败，请检查日志",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass

    yield

    cleanup_stop = getattr(app.state, "quote_cleanup_stop", None)
    cleanup_task = getattr(app.state, "quote_cleanup_task", None)
    if cleanup_stop is not None:
        cleanup_stop.set()
    if cleanup_task is not None:
        try:
            await cleanup_task
        except Exception as e:
            logger.warning("Quote cleanup task stop failed: %s", e)

    await ws_server.stop()
    await close_db()
    try:
        from backend.shared.system_events import record_system_event

        record_system_event(
            event_type="service_lifecycle",
            level="info",
            source="quantmind-stream",
            title="行情服务关闭",
            message="QuantMind Stream 正常关闭",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass
    logger.info("🔚 QuantMind Stream shutdown complete")


app = FastAPI(
    title="QuantMind Streaming Service",
    version="2.0.0",
    description="收敛后的行情与实时推送服务（整合了行情接口与 WebSocket 推送核心）",
    lifespan=lifespan,
    generate_unique_id_function=quantmind_generate_unique_id,
)

install_request_id_middleware(app)
install_error_contract_handlers(app)
install_access_log_middleware(app, service_name="quantmind-stream")

app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST 路由（行情）
app.include_router(quotes_router, prefix="/api/v1")
app.include_router(klines_router, prefix="/api/v1")
app.include_router(symbols_router, prefix="/api/v1")


# 通用 WebSocket 主入口
@app.websocket("/ws")
async def main_websocket_endpoint(websocket: WebSocket):
    await core_ws_endpoint(websocket)


# 行情专用 WebSocket（兼容旧客户端）- 共用 ws_core 统一协议
@app.websocket("/api/v1/ws/market")
async def market_websocket_compat_endpoint(websocket: WebSocket):
    await core_ws_endpoint(websocket)


@app.get("/health")
async def health_check():
    market_db_connected = bool(getattr(app.state, "market_db_connected", False))
    set_service_health("quantmind-stream", market_db_connected)
    if STREAM_MARKET_DB_CONNECTED is not None:
        STREAM_MARKET_DB_CONNECTED.set(1 if market_db_connected else 0)
    if STREAM_SERVICE_DEGRADED is not None:
        STREAM_SERVICE_DEGRADED.set(0 if market_db_connected else 1)
    return {
        "status": "healthy" if market_db_connected else "degraded",
        "service": "quantmind-stream",
        "market_db": "connected" if market_db_connected else "disconnected",
        "ws_connections": len(ws_manager.active_connections),
        "ws_core_started": bool(getattr(app.state, "ws_core_started", False)),
    }


@app.get("/api/v1/internal/debug/connections")
async def debug_connections(
    x_internal_call: str | None = Header(default=None),
):
    """临时调试端点：查看活跃 WS 连接元数据（仅内部调用）"""
    if str(x_internal_call or "").strip() != get_internal_call_secret():
        raise HTTPException(status_code=401, detail="Invalid internal secret")
    connections = []
    for conn_id, meta in ws_manager.connection_metadata.items():
        connections.append({
            "connection_id": conn_id,
            "active": conn_id in ws_manager.active_connections,
            "auth_source": meta.get("auth_source"),
            "tenant_id": meta.get("tenant_id"),
            "user_id": meta.get("user_id"),
            "authenticated": meta.get("authenticated"),
            "account_id": meta.get("account_id"),
            "binding_id": meta.get("binding_id"),
        })
    return {"total": len(connections), "connections": connections}


@app.get("/")
async def root():
    return {"message": "QuantMind Stream Service V2 is running"}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003, access_log=False)
