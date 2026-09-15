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
    qmt_account_sync_task = None
    qmt_exec_poller_task = None
    mirror_queue_drainer_task = None
    qmt_sltp_executor_task = None
    dual_book_reconcile_task = None
    close_audit_task = None
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
        # 大 QMT 执行端（big-convert RPC）：账户快照 + 委托/成交回收
        # 未配置（QMT_EXEC_ENABLED=false 且页面未开启）时两个任务自行空转退出/低频等待
        from backend.services.live_trading.services.qmt_account_sync_task import (
            run_qmt_account_sync_task,
        )
        from backend.services.live_trading.services.qmt_exec_poller import (
            run_qmt_exec_poller_task,
        )

        qmt_account_sync_task = asyncio.create_task(
            run_qmt_account_sync_task(interval_seconds=30),
            name="qmt-account-sync",
        )
        qmt_exec_poller_task = asyncio.create_task(
            run_qmt_exec_poller_task(),
            name="qmt-exec-poller",
        )
        # 模拟盘 → 真单镜像：非交易时段入队的镜像单，开盘后由本任务补交
        from backend.services.live_trading.services.real_mirror_service import (
            run_mirror_queue_drainer,
        )

        mirror_queue_drainer_task = asyncio.create_task(
            run_mirror_queue_drainer(),
            name="mirror-queue-drainer",
        )
        # QMT 止盈/止损执行器：触发即以保护价（跌停价）下真单，默认关闭
        # （Redis qmt:sltp:executor:config.enabled 打开才工作）
        from backend.services.live_trading.services.sltp_executor import (
            run_qmt_sltp_executor_task,
        )

        qmt_sltp_executor_task = asyncio.create_task(
            run_qmt_sltp_executor_task(),
            name="qmt-sltp-executor",
        )
        # SIM ↔ 真单 双轨对账：每日收盘后对比两本账，差异超阈值发通知
        from backend.services.trade.services.dual_book_reconciliation_task import (
            run_dual_book_reconciliation_task,
        )

        dual_book_reconcile_task = asyncio.create_task(
            run_dual_book_reconciliation_task(),
            name="dual-book-reconcile",
        )
        # 收盘清理核对：柜台委托是否全部终结、本地有无 submitted 残留
        from backend.services.trade.services.close_cleanup_audit_task import (
            run_close_audit_task,
        )

        close_audit_task = asyncio.create_task(
            run_close_audit_task(),
            name="close-cleanup-audit",
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
        from backend.services.live_trading.services.risk_trigger_scanner import (
            RiskTriggerScanner,
            scan_enabled,
        )

        if scan_enabled():
            risk_scanner = RiskTriggerScanner(redis_client)
            await risk_scanner.start()
            app.state.risk_trigger_scanner = risk_scanner
            logger.info(
                "Risk trigger scanner started (interval=%ss)",
                risk_scanner.interval_seconds,
            )
        else:
            logger.info("Risk trigger scanner disabled (RISK_SCAN_ENABLED=false)")
        from backend.services.simulation.services.simulation_corporate_action_task import (
            run_simulation_corporate_action_task,
        )

        corp_action_task = asyncio.create_task(
            run_simulation_corporate_action_task(),
            name="simulation-corporate-action",
        )
        # 模拟盘持久化权益结算（对账确权→行情重估→权益持久化，默认 30s 周期，
        # 无交易时段门控，启动即执行首周期）。取代旧的三个独立 worker：
        # 每日 03:20 reconcile、300s fund snapshot、仅交易时段运行的 remark——
        # 三者组合在服务器重启后（尤其盘外）无人刷新权益数据。
        try:
            from backend.services.simulation.services.equity_settlement_worker import (
                SimulationEquitySettlementWorker,
                settle_enabled,
                settle_interval_seconds,
            )

            if settle_enabled():
                equity_settle_worker = SimulationEquitySettlementWorker(
                    redis_client, interval_seconds=settle_interval_seconds()
                )
                await equity_settle_worker.start()
                app.state.sim_equity_settle_worker = equity_settle_worker
                logger.info(
                    "Simulation equity settlement worker started (interval=%ss)",
                    equity_settle_worker.interval_seconds,
                )
            else:
                logger.info(
                    "Simulation equity settlement worker disabled "
                    "(SIM_EQUITY_SETTLE_ENABLED=false)"
                )
        except Exception as e:
            logger.error(
                "trade sim equity settlement worker start failed: %s", e, exc_info=True
            )

        # T-P0-06：模拟盘日终结算（EOD）。此前 run_simulation_eod_worker 全仓
        # 无调用方 → simulation_account_daily（日级台账快照）长期无写入者。
        # 默认开启；SIM_EOD_WORKER_ENABLED=false 关闭。
        try:
            from backend.services.simulation.services.eod_service import (
                run_simulation_eod_worker,
            )

            if str(os.getenv("SIM_EOD_WORKER_ENABLED", "true")).strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }:
                app.state.sim_eod_worker_task = asyncio.create_task(
                    run_simulation_eod_worker(), name="simulation-eod"
                )
                logger.info("Simulation EOD worker started")
            else:
                logger.info(
                    "Simulation EOD worker disabled (SIM_EOD_WORKER_ENABLED=false)"
                )
        except Exception as e:
            logger.error("trade sim EOD worker start failed: %s", e, exc_info=True)

        # T-P0-06：挂单消费者。此前 run_simulation_pending_order_worker 全仓无
        # 调用方 → status=pending 的模拟单永久悬空。默认开启；
        # SIM_PENDING_ORDER_WORKER_ENABLED=false 关闭。
        try:
            from backend.services.simulation.services.pending_order_worker import (
                run_simulation_pending_order_worker,
            )

            if str(os.getenv("SIM_PENDING_ORDER_WORKER_ENABLED", "true")).strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }:
                app.state.sim_pending_order_worker_task = asyncio.create_task(
                    run_simulation_pending_order_worker(),
                    name="simulation-pending-order",
                )
                logger.info("Simulation pending order worker started")
            else:
                logger.info(
                    "Simulation pending order worker disabled "
                    "(SIM_PENDING_ORDER_WORKER_ENABLED=false)"
                )
        except Exception as e:
            logger.error(
                "trade sim pending order worker start failed: %s", e, exc_info=True
            )
        # 策略监控推送源：把模拟盘实时盈亏写进 strategy_events，驱动仪表盘
        # 「策略监控」卡片刷新（WS 连上时前端会关掉轮询，只认推送）。
        try:
            from backend.services.trade.services.strategy_monitor_pusher import (
                StrategyMonitorPusher,
                push_enabled,
                push_interval_seconds,
            )

            if push_enabled():
                strategy_push_worker = StrategyMonitorPusher(
                    redis_client, interval_seconds=push_interval_seconds()
                )
                await strategy_push_worker.start()
                app.state.sim_strategy_push_worker = strategy_push_worker
                logger.info(
                    "Strategy monitor pusher started (interval=%ss)",
                    strategy_push_worker.interval_seconds,
                )
            else:
                logger.info(
                    "Strategy monitor pusher disabled (SIM_STRATEGY_PUSH_ENABLED=false)"
                )
        except Exception as e:
            logger.error(
                "trade strategy monitor pusher start failed: %s", e, exc_info=True
            )
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

    risk_scanner = getattr(app.state, "risk_trigger_scanner", None)
    if risk_scanner is not None:
        try:
            await risk_scanner.stop()
        except Exception as e:
            logger.warning("trade risk trigger scanner stop failed: %s", e)

    for task in (scanner_task, margin_task, snapshot_task, ledger_settlement_task, manual_execution_task, sandbox_signal_task, tdx_account_sync_task, qmt_account_sync_task, qmt_exec_poller_task, mirror_queue_drainer_task, qmt_sltp_executor_task, dual_book_reconcile_task, close_audit_task, tdx_quote_feed_task, tdx_l2_capture_task, tdx_l2_realtime_task, t1_unlock_task, corp_action_task):
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

    # 停止模拟盘持久化权益结算 worker
    equity_settle_worker = getattr(app.state, "sim_equity_settle_worker", None)
    if equity_settle_worker is not None:
        try:
            await equity_settle_worker.stop()
        except Exception as e:
            logger.warning("trade sim equity settlement worker stop failed: %s", e)

    # 停止策略监控推送源
    strategy_push_worker = getattr(app.state, "sim_strategy_push_worker", None)
    if strategy_push_worker is not None:
        try:
            await strategy_push_worker.stop()
        except Exception as e:
            logger.warning("trade strategy monitor pusher stop failed: %s", e)

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
from backend.services.trade.routers.qmt_mirror import router as qmt_mirror_router
from backend.services.trade.routers.qmt_sltp import router as qmt_sltp_router

app.include_router(tdx_config_router, prefix="/api/v1", tags=["TDX-Bridge"])
app.include_router(tdx_quote_feed_router, prefix="/api/v1", tags=["TDX-Bridge"])
app.include_router(tdx_l2_router, prefix="/api/v1", tags=["TDX-L2"])
app.include_router(broker_config_router, prefix="/api/v1", tags=["Broker-Config"])
app.include_router(qmt_mirror_router, prefix="/api/v1", tags=["QMT-Mirror"])
app.include_router(qmt_sltp_router, prefix="/api/v1", tags=["QMT-SLTP"])

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
