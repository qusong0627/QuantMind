import asyncio
import multiprocessing as mp
import os
from contextlib import asynccontextmanager

try:
    mp.set_start_method("spawn", force=True)
except RuntimeError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.services.live_trading.routers import real_trading
from backend.services.simulation.routers import simulation, simulation_history, simulation_orders
from backend.services.trade.routers import (
    internal_strategy,
    portfolios,
    positions,
    simulation_batch,
    trading_history,
    trading_orders,
)
from backend.services.simulation.replay.router import router as replay_router
from backend.shared.config_manager import init_unified_config
from backend.shared.cors import resolve_cors_origins
from backend.shared.error_contract import install_error_contract_handlers
from backend.shared.logging_config import get_logger
from backend.shared.openapi_utils import quantmind_generate_unique_id
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.schema_registry import create_registered_tables
from backend.shared.service_health_metrics import (
    build_metrics_response,
    set_service_health,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.startup_healthy = True
    app.state.db_connected = False
    app.state.redis_connected = False
    app.state.execution_stream_consumer = None

    scanner_task = None
    margin_task = None
    snapshot_task = None
    ledger_settlement_task = None
    manual_execution_task = None
    sandbox_signal_task = None
    tdx_account_sync_task = None
    tdx_quote_feed_task = None
    tdx_l2_capture_task = None
    tdx_l2_realtime_task = None
    t1_unlock_task = None

    try:
        await init_unified_config(service_name="quantmind-trade")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade unified config init failed: %s", e, exc_info=True)

    from backend.shared.database_manager_v2 import close_database, init_database

    try:
        await init_database()
        from backend.shared.database_manager_v2 import get_db_manager

        await create_registered_tables(
            get_db_manager()._master_engine,
            schema_keys=("trade.core", "trade.portfolio", "trade.simulation"),
        )
        from backend.services.live_trading.services.manual_execution_persistence import manual_execution_persistence

        await manual_execution_persistence.ensure_tables()
        app.state.db_connected = True
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade database init failed: %s", e, exc_info=True)

    from backend.services.trade_shared.redis_client import redis_client

    try:
        redis_client.connect()
        app.state.redis_connected = True
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade redis init failed: %s", e, exc_info=True)

    # 应用 Redis 持久化的通达信桥运行时配置（PUT /tdx/config 跨 respawn 生效）
    try:
        from backend.services.trade.routers.tdx_config import apply_runtime_config

        apply_runtime_config()
    except Exception as e:
        logger.warning("trade tdx runtime config apply failed: %s", e)

    try:
        from backend.services.trade_shared.utils.stock_lookup import warmup_stock_cache

        warmup_stock_cache()
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade stock cache warmup failed: %s", e, exc_info=True)

    try:
        from backend.services.trade.services.execution_stream_consumer import ExecutionStreamConsumer

        exec_consumer = ExecutionStreamConsumer()
        await exec_consumer.start()
        app.state.execution_stream_consumer = exec_consumer
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade execution stream consumer start failed: %s", e, exc_info=True)

    try:
        from backend.services.trade.services.margin_interest_scanner import run_margin_interest_scanner
        from backend.services.trade.services.order_timeout_scanner import run_order_timeout_scanner
        from backend.services.trade.services.portfolio_snapshot_task import run_portfolio_snapshot_task
        from backend.services.trade.services.real_account_ledger_settlement_task import (
            run_real_account_ledger_settlement_task,
        )
        from backend.services.live_trading.services.manual_execution_worker import run_manual_execution_worker
        from backend.services.live_trading.services.tdx_account_sync_task import run_tdx_account_sync_task

        scanner_task = asyncio.create_task(run_order_timeout_scanner())
        margin_task = asyncio.create_task(run_margin_interest_scanner())
        snapshot_task = asyncio.create_task(run_portfolio_snapshot_task())
        ledger_settlement_task = asyncio.create_task(run_real_account_ledger_settlement_task())
        manual_execution_task = asyncio.create_task(run_manual_execution_worker(), name="manual-execution-worker")
        tdx_account_sync_task = asyncio.create_task(
            run_tdx_account_sync_task(interval_seconds=30),
            name="tdx-account-sync",
        )
        from backend.services.live_trading.services.tdx_quote_feed import run_tdx_quote_feed_task

        tdx_quote_feed_task = asyncio.create_task(
            run_tdx_quote_feed_task(),
            name="tdx-quote-feed",
        )
        from backend.services.simulation.services.simulation_t1_unlock_task import (
            run_simulation_t1_unlock_task,
        )

        t1_unlock_task = asyncio.create_task(
            run_simulation_t1_unlock_task(),
            name="simulation-t1-unlock",
        )
        from backend.services.simulation.services.simulation_corporate_action_task import (
            run_simulation_corporate_action_task,
        )

        corp_action_task = asyncio.create_task(
            run_simulation_corporate_action_task(),
            name="simulation-corporate-action",
        )
        # simulation_fund_snapshot_task 已删除（自动重置导致手动任务后金额被重置为 0）
        from backend.services.live_trading.services.tdx_l2_capture_task import run_tdx_l2_capture_task
        from backend.services.live_trading.services.tdx_l2_realtime import run_tdx_l2_realtime_task

        tdx_l2_capture_task = asyncio.create_task(
            run_tdx_l2_capture_task(), name="tdx-l2-capture"
        )
        tdx_l2_realtime_task = asyncio.create_task(
            run_tdx_l2_realtime_task(), name="tdx-l2-realtime"
        )
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade background scanners start failed: %s", e, exc_info=True)

    # 启动沙箱进程池（用于模拟盘）
    try:
        from backend.services.trade.sandbox.manager import sandbox_manager

        pool_size = int(os.getenv("SANDBOX_POOL_SIZE", "1"))
        sandbox_manager.pool_size = pool_size
        sandbox_manager.start_pool()
        logger.info("Sandbox worker pool started with %d workers", pool_size)
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade sandbox pool start failed: %s", e, exc_info=True)

    # 恢复容器重启前的模拟盘沙箱运行状态（trade:active_strategy:* 标记）
    try:
        from backend.services.simulation.services.simulation_runtime_restorer import (
            SimulationRuntimeRestorer,
        )

        restorer = SimulationRuntimeRestorer(redis_client)
        restored_count = await restorer.restore_all()
        if restored_count > 0:
            logger.info("Simulation runtime restored %d sandboxes after restart", restored_count)
    except Exception as e:
        logger.warning("Simulation runtime restore failed: %s", e)

    # 启动沙箱信号消费者（将沙箱信号转换为模拟盘订单）
    try:
        from backend.services.trade.services.sandbox_signal_consumer import sandbox_signal_consumer

        sandbox_signal_task = asyncio.create_task(sandbox_signal_consumer.start(), name="sandbox-signal-consumer")
        app.state.sandbox_signal_consumer = sandbox_signal_consumer
        logger.info("Sandbox signal consumer started")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error("trade sandbox signal consumer start failed: %s", e, exc_info=True)

    # 启动模拟盘定时调度器
    try:
        enabled = os.getenv("ENABLE_SIMULATION_SCHEDULER", "false").lower() in {"1", "true", "yes", "on"}
        if enabled:
            from backend.services.simulation.scheduler import simulation_scheduler

            await simulation_scheduler.start()
            app.state.simulation_scheduler = simulation_scheduler
            logger.info("Simulation scheduler started")
    except Exception as e:
        logger.error("trade simulation scheduler start failed: %s", e, exc_info=True)

    # 启动模拟盘策略级托管调度器（按前端弹窗配置的调仓周期/时间点触发）
    try:
        hosted_enabled = os.getenv("ENABLE_SIMULATION_HOSTED_SCHEDULER", "true").lower() in {"1", "true", "yes", "on"}
        if hosted_enabled:
            from backend.services.simulation.services.simulation_hosted_scheduler import (
                SimulationHostedScheduler,
            )

            hosted_scheduler = SimulationHostedScheduler(redis_client)
            await hosted_scheduler.start()
            app.state.simulation_hosted_scheduler = hosted_scheduler
            logger.info("Simulation hosted scheduler started")
    except Exception as e:
        logger.error("trade simulation hosted scheduler start failed: %s", e, exc_info=True)

    healthy = bool(app.state.startup_healthy and app.state.db_connected and app.state.redis_connected)
    set_service_health("quantmind-trade", healthy)

    try:
        from backend.shared.system_events import record_system_event_async

        await record_system_event_async(
            event_type="service_lifecycle",
            level="info" if healthy else "error",
            source="quantmind-trade",
            title="交易核心启动完成" if healthy else "交易核心启动异常",
            message="QuantMind Trade 启动完成" if healthy else "Trade 启动存在初始化失败，请检查日志",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass

    yield

    for task in (scanner_task, margin_task, snapshot_task, ledger_settlement_task, manual_execution_task, sandbox_signal_task, tdx_account_sync_task, tdx_quote_feed_task, tdx_l2_capture_task, tdx_l2_realtime_task, t1_unlock_task, corp_action_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("trade background task stop failed: %s", e)

    exec_consumer = getattr(app.state, "execution_stream_consumer", None)
    if exec_consumer is not None:
        try:
            await exec_consumer.stop()
        except Exception as e:
            logger.warning("trade execution stream consumer stop failed: %s", e)

    # 停止沙箱信号消费者
    sandbox_consumer = getattr(app.state, "sandbox_signal_consumer", None)
    if sandbox_consumer is not None:
        try:
            await sandbox_consumer.stop()
        except Exception as e:
            logger.warning("trade sandbox signal consumer stop failed: %s", e)

    # 停止模拟盘调度器
    simulation_scheduler = getattr(app.state, "simulation_scheduler", None)
    if simulation_scheduler is not None:
        try:
            await simulation_scheduler.stop()
        except Exception as e:
            logger.warning("trade simulation scheduler stop failed: %s", e)

    # 停止模拟盘策略级托管调度器
    hosted_scheduler = getattr(app.state, "simulation_hosted_scheduler", None)
    if hosted_scheduler is not None:
        try:
            await hosted_scheduler.stop()
        except Exception as e:
            logger.warning("trade simulation hosted scheduler stop failed: %s", e)

    # 停止沙箱进程池
    try:
        from backend.services.trade.sandbox.manager import sandbox_manager

        sandbox_manager.stop_pool()
        logger.info("Sandbox worker pool stopped")
    except Exception as e:
        logger.warning("trade sandbox pool stop failed: %s", e)

    try:
        await close_database()
    except Exception as e:
        logger.warning("trade database close failed: %s", e)

    try:
        redis_client.close()
    except Exception as e:
        logger.warning("trade redis close failed: %s", e)

    try:
        from backend.shared.system_events import record_system_event

        record_system_event(
            event_type="service_lifecycle",
            level="info",
            source="quantmind-trade",
            title="交易核心关闭",
            message="QuantMind Trade 正常关闭",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass


app = FastAPI(
    title="QuantMind Trade Core",
    version="2.0.0",
    lifespan=lifespan,
    generate_unique_id_function=quantmind_generate_unique_id,
)

install_request_id_middleware(app)
install_error_contract_handlers(app)
install_access_log_middleware(app, service_name="quantmind-trade")

app.include_router(trading_orders.router, prefix="/api/v1/orders", tags=["Orders"])
app.include_router(trading_history.router, prefix="/api/v1/trades", tags=["Trades"])
app.include_router(real_trading.router, prefix="/api/v1/real-trading", tags=["Real Trading"])
app.include_router(portfolios.router, prefix="/api/v1/portfolios", tags=["Portfolios"])
app.include_router(positions.router, prefix="/api/v1", tags=["Positions"])
app.include_router(simulation.router, prefix="/api/v1/simulation", tags=["Simulation-Account"])
app.include_router(simulation_orders.router, prefix="/api/v1/simulation", tags=["Simulation-Orders"])
app.include_router(simulation_history.router, prefix="/api/v1/simulation", tags=["Simulation-Trades"])
app.include_router(simulation_batch.router)
app.include_router(internal_strategy.router)
app.include_router(replay_router)

from backend.services.trade.routers.tdx_config import router as tdx_config_router
from backend.services.trade.routers.tdx_quote_feed import router as tdx_quote_feed_router
from backend.services.trade.routers.tdx_l2 import router as tdx_l2_router
from backend.services.trade.routers.broker_config import router as broker_config_router

app.include_router(tdx_config_router, prefix="/api/v1", tags=["TDX-Bridge"])
app.include_router(tdx_quote_feed_router, prefix="/api/v1", tags=["TDX-Bridge"])
app.include_router(tdx_l2_router, prefix="/api/v1", tags=["TDX-L2"])
app.include_router(broker_config_router, prefix="/api/v1", tags=["Broker-Config"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    startup_healthy = bool(getattr(app.state, "startup_healthy", True))
    db_connected = bool(getattr(app.state, "db_connected", startup_healthy))
    redis_connected = bool(getattr(app.state, "redis_connected", startup_healthy))
    healthy = bool(startup_healthy and db_connected and redis_connected)

    set_service_health("quantmind-trade", healthy)
    return {
        "status": "healthy" if healthy else "degraded",
        "service": "quantmind-trade",
        "components": {
            "database": "connected" if db_connected else "disconnected",
            "redis": "connected" if redis_connected else "disconnected",
        },
    }


@app.get("/")
async def root():
    return {"message": "QuantMind Trade Core V2 is running"}


@app.get("/metrics")
async def metrics():
    return build_metrics_response()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002, access_log=False)
