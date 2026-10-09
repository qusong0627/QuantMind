import asyncio
import importlib
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.shared.auth import AuthManager, get_internal_call_secret
from backend.shared.config_manager import init_unified_config
from backend.shared.cors import resolve_cors_origins
from backend.shared.database_pool import init_default_databases as init_sync_db_pool
from backend.shared.error_contract import install_error_contract_handlers
from backend.shared.logging_config import get_logger
from backend.shared.openapi_utils import quantmind_generate_unique_id
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.service_health_metrics import (
    build_metrics_response,
    set_service_health,
)

logger = get_logger(__name__)

#: 挖掘排队清扫间隔（秒）：排水主触发在「任务收尾 / 取消 / 启动」，清扫器只做
#: 兜底（收尾路径异常漏触发、或外部因素让名额空转）。
MINING_QUEUE_SWEEP_INTERVAL_S = 60


async def _mining_queue_sweeper(launcher) -> None:
    """排队任务兜底清扫器：每 60s 排一次水（drain 自身吞单任务异常）。"""
    while True:
        await asyncio.sleep(MINING_QUEUE_SWEEP_INTERVAL_S)
        try:
            await launcher.drain_queue()
        except Exception as e:  # noqa: BLE001 - 清扫器绝不因单次失败退出
            logger.warning(f"mining queue sweep failed: {e}")


# 兼容 qlib_app 内部裸导入路径（from qlib_app.*）
if "qlib_app" not in sys.modules:
    sys.modules["qlib_app"] = importlib.import_module("backend.services.engine.qlib_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.startup_healthy = True
    try:
        await init_unified_config(service_name="quantmind-engine")
        logger.info("✅ QuantMind Engine Unified Config Loaded")
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Unified config load failed: {e}")

    try:
        init_sync_db_pool(pool_size=20, max_overflow=10)
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Sync database pool init failed: {e}")

    try:
        # Skip qlib bootstrap at startup — qlib.init() can get stuck in CPU loops
        # in containerized environments. Tables will be ensured lazily on first use.
        # Qlib itself initializes on first backtest request via service.initialize().
        from backend.services.engine.qlib_app.services.backtest_persistence import (
            BacktestPersistence,
        )
        from backend.services.engine.qlib_app.services.optimization_persistence import (
            OptimizationPersistence,
        )
        await BacktestPersistence().ensure_tables()
        await OptimizationPersistence().ensure_tables()
        logger.info("✅ Backtest/Optimization tables ensured (qlib init deferred to first use)")
    except Exception as e:
        logger.error(f"❌ Table ensure failed: {e} (non-fatal)")

    try:
        from backend.services.engine.qlib_app.services.rd_agent_persistence import RDAgentFactorPersistence

        await RDAgentFactorPersistence().ensure_tables()
        # 跨市场回测台账扩展（T-FB-06）：kind/params_json 列、七态词表、序列表。
        # 依赖上一步的基表，次序不可颠倒。
        from backend.services.engine.factor_backtest import store as factor_backtest_store

        await factor_backtest_store.ensure_tables()
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ AlphaAgent factors table ensure failed: {e}")

    try:
        # 批量回测重启可重入（T-FB-09）：中断批次重新入队，孤儿 running 行收口。
        # 数据面是台账行，恢复失败只告警（状态轮询还有兜底入口）。
        from backend.services.engine.factor_backtest import batch as factor_backtest_batch

        resumed = await factor_backtest_batch.resume_interrupted()
        if resumed:
            logger.info(f"♻️ Factor backtest batches resumed: {resumed}")
    except Exception as e:
        logger.error(f"❌ Factor backtest batch resume failed: {e} (non-fatal)")

    try:
        # 因子池三表（P1）：池是增益层不是主链路，建表失败只告警不阻启动
        from backend.shared.factor_pool_contract import ensure_factor_pool_tables_async

        await ensure_factor_pool_tables_async()
    except Exception as e:
        logger.error(f"❌ Factor pool table ensure failed: {e} (non-fatal)")

    try:
        from backend.services.engine.quantbot.task_store import QuantBotTaskStore

        await QuantBotTaskStore().ensure_tables()
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ QuantBot tasks table ensure failed: {e}")

    try:
        # 挖掘任务中心（机构级 P0）：建表 + 重启对账——本进程此刻没有任何活任务，
        # 表里还挂着 pending/running 的行必然是上次进程留下的孤儿（见 store docstring）
        from backend.services.engine.alpha_agent.task_store import get_mining_task_store

        _mining_store = get_mining_task_store()
        await _mining_store.ensure_tables()
        await _mining_store.reconcile_orphans()
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Mining task center ensure/reconcile failed: {e}")

    # 因子挖掘排队（机构级 P0 · A1）：注册排空时的 LLM 重解析器（排队行绝不落
    # 密钥，排空才按 (user_id, tenant_id) 重取值）→ 启动排水（接续上次进程遗留
    # 的 queued 行：reconcile 不碰 queued，_load_tasks 原样保留）→ 60s 兜底清扫
    # 器。drain 自带重入闸与单任务隔离，并发触发安全。
    mining_startup_drain: asyncio.Task | None = None
    mining_sweeper_task: asyncio.Task | None = None
    try:
        from backend.services.engine.alpha_agent.launcher import get_launcher
        from backend.services.engine.routers.alpha_agent import (
            register_mining_queue_llm_resolver,
        )

        register_mining_queue_llm_resolver()
        _mining_launcher = get_launcher()
        mining_startup_drain = asyncio.create_task(
            _mining_launcher.drain_queue(), name="mining-queue-startup-drain"
        )
        mining_sweeper_task = asyncio.create_task(
            _mining_queue_sweeper(_mining_launcher), name="mining-queue-sweeper"
        )
        logger.info("✅ Mining queue ready (startup drain + 60s sweeper)")
    except Exception as e:
        logger.warning(f"⚠️ Mining queue startup skipped: {e} (non-fatal)")

    try:
        # 文档中心（T-FM-07）：建表 + 重启续轮询（parsing 行是 MinerU 队列里的
        # 活任务，本进程接管；超龄 uploaded 对账定格）+ 留存 GC。
        # 文档链默认关（ENABLE_DOC_MINING=false），失败只告警不阻启动。
        from backend.services.engine.alpha_agent.doc_parse_service import (
            get_doc_parse_service,
        )
        from backend.services.engine.alpha_agent.doc_store import get_doc_store

        await get_doc_store().ensure_tables()
        _doc_svc = get_doc_parse_service()
        _resumed = await _doc_svc.resume_pending()
        _gc = await _doc_svc.gc_expired()
        logger.info(
            "✅ Doc center ready (resumed=%d parsing, gc=%d expired)", _resumed, _gc
        )
    except Exception as e:
        logger.error(f"❌ Doc center ensure/resume failed: {e} (non-fatal)")

    try:
        # 挖掘任务日志 GC（T-FM-20）：日志根在 /data（LOG_TRACE_PATH，容器重建不丢），
        # 终态任务目录超留存线（LOG_TRACE_RETENTION_DAYS，默认 90 天）在此清。
        # 历史本体在 rd_agent_mining_tasks，清日志不影响任何记录。
        from backend.services.engine.alpha_agent.launcher import gc_task_logs

        _gc_logs = gc_task_logs()
        logger.info(
            "✅ Alpha task log GC: pruned=%d scanned=%d",
            _gc_logs["pruned"],
            _gc_logs["scanned"],
        )
    except Exception as e:
        logger.warning(f"Alpha task log GC failed: {e} (non-fatal)")

    try:
        from backend.shared.model_registry import model_registry_service

        await model_registry_service.ensure_tables()
    except Exception as e:
        app.state.startup_healthy = False
        logger.error(f"❌ Model registry table ensure failed: {e}")

    set_service_health("quantmind-engine", bool(getattr(app.state, "startup_healthy", True)))

    # 启动 VectorizedMatcher（受 ENABLE_VECTORIZED_MATCHER 环境变量控制）
    vm_task: asyncio.Task | None = None
    try:
        from backend.services.trade.services.vectorized_matcher import VectorizedMatcher
        _vm = VectorizedMatcher()
        vm_task = asyncio.create_task(_vm.start(), name="vectorized_matcher")
        app.state.vectorized_matcher = _vm
        logger.info("✅ VectorizedMatcher task created (active if ENABLE_VECTORIZED_MATCHER=true)")
    except Exception as e:
        logger.warning(f"⚠️ VectorizedMatcher startup skipped: {e}")

    # 启动热集实时推理服务（P6 T-P6-08；Redis 配置门控，默认关，配置热读无需重启）
    try:
        from backend.services.engine.inference.realtime_service import default_service

        default_service().start()
        logger.info("✅ RealtimeInference service loop created (active if qm:realtime:infer:config.enabled=true)")
    except Exception as e:
        logger.warning(f"⚠️ RealtimeInference startup skipped: {e}")

    # 启动日内市场状态服务（P6 T-P6-13；Redis 配置门控，默认关）
    try:
        from backend.services.engine.realtime_regime import default_service as regime_service

        regime_service().start()
        logger.info("✅ RealtimeRegime service loop created (active if qm:realtime:regime:config.enabled=true)")
    except Exception as e:
        logger.warning(f"⚠️ RealtimeRegime startup skipped: {e}")

    # 启动识别引擎（P6 T-P6-14；Redis 配置门控默认关；含异动类型契约自愈）
    try:
        from backend.shared.anomaly_contract import ensure_anomaly_types

        await asyncio.to_thread(ensure_anomaly_types)
        from backend.services.engine.anomaly_engine import default_service as anomaly_service

        anomaly_service().start()
        logger.info("✅ AnomalyEngine service loop created (active if qm:engine:anomaly:config.enabled=true)")
    except Exception as e:
        logger.warning(f"⚠️ AnomalyEngine startup skipped: {e}")

    # 启动新闻情报服务（P6 T-P6-12；Redis 配置门控默认关）
    try:
        from backend.services.engine.news_intel_engine import default_service as news_intel_service

        news_intel_service().start()
        logger.info("✅ NewsIntel service loop created (active if qm:engine:news_intel:config.enabled=true)")
    except Exception as e:
        logger.warning(f"⚠️ NewsIntel startup skipped: {e}")

    # 启动预热向量解析/字段检索（2026-05-03：暂时关闭强制预热以加快启动速度）
    warmup_enabled = os.getenv("AI_STRATEGY_WARMUP", "false").strip().lower() not in ("0", "false", "no", "off")
    if warmup_enabled:
        try:
            from backend.services.engine.ai_strategy.services.startup_health import run_startup_health_checks
            await run_startup_health_checks()
            logger.info("✅ AI Strategy Warmup completed successfully")
        except Exception as e:
            app.state.startup_healthy = False
            logger.error(f"❌ AI Strategy Warmup failed: {e}")
    else:
        logger.info("AI Strategy Warmup disabled by env")

    # 预热 QuantDB DuckDB 连接（放入后台任务执行，不阻塞 lifespan 启动）
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if hub.available:
            asyncio.create_task(asyncio.to_thread(hub.warm_up))
            logger.info("✅ QuantDB DuckDB warm-up scheduled in background")
        else:
            logger.info("QuantDB data not available, skipping warm-up")
    except Exception as e:
        logger.warning(f"⚠️ QuantDB warm-up failed (non-fatal): {e}")

    # 内置股票池成分 TXT 每日刷新 worker（启动即刷 + 按日期去重）
    builtin_pool_task: asyncio.Task | None = None
    try:
        from backend.shared.stock_pool import run_builtin_pool_refresh_worker

        builtin_pool_task = asyncio.create_task(
            run_builtin_pool_refresh_worker(), name="builtin-pool-refresh"
        )
        logger.info("✅ Builtin stock-pool TXT refresh worker started")
    except Exception as e:
        logger.warning(f"⚠️ Builtin stock-pool refresh worker start skipped: {e}")

    # --- 此处 Yield，之后代码在 shutdown 时运行 ---
    try:
        from backend.shared.system_events import record_system_event_async

        ok = bool(getattr(app.state, "startup_healthy", True))
        await record_system_event_async(
            event_type="service_lifecycle",
            level="info" if ok else "error",
            source="quantmind-engine",
            title="推理引擎服务启动完成" if ok else "推理引擎服务启动异常",
            message="QuantMind Engine 启动完成" if ok else "Engine 启动存在初始化失败，请检查日志",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass
    yield

    # --- 停止逻辑 ---
    if builtin_pool_task and not builtin_pool_task.done():
        builtin_pool_task.cancel()
    # 挖掘排队：清扫器必须停（否则测试/重载时长眠任务泄漏）；启动排水是一次性
    # 任务，未跑完也取消，避免 "Task was destroyed but it is pending" 噪音。
    if mining_startup_drain and not mining_startup_drain.done():
        mining_startup_drain.cancel()
    if mining_sweeper_task and not mining_sweeper_task.done():
        mining_sweeper_task.cancel()
    if vm_task and not vm_task.done():
        try:
            app.state.vectorized_matcher.stop()
            await asyncio.wait_for(vm_task, timeout=5.0)
        except Exception:
            vm_task.cancel()
    try:
        from backend.shared.system_events import record_system_event

        record_system_event(
            event_type="service_lifecycle",
            level="info",
            source="quantmind-engine",
            title="推理引擎服务关闭",
            message="QuantMind Engine 正常关闭",
        )
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        pass
    logger.info("🔚 QuantMind Engine shutdown complete")


def _run_ai_strategy_warmup_sync() -> None:
    from backend.services.engine.ai_strategy.services.selection.schema_retriever import (
        get_schema_retriever,
    )
    from backend.services.engine.ai_strategy.services.selection.vector_parser import (
        get_strategy_vector_parser,
    )

    asyncio.run(get_strategy_vector_parser())
    asyncio.run(get_schema_retriever())


async def _bootstrap_qlib_runtime() -> None:
    """在后台补齐 qlib 初始化与回测表检查（不阻塞 engine 启动）。"""
    from backend.services.engine.qlib_app import get_qlib_service
    from backend.services.engine.qlib_app.services.backtest_persistence import (
        BacktestPersistence,
    )
    from backend.services.engine.qlib_app.services.optimization_persistence import (
        OptimizationPersistence,
    )

    try:
        qlib_service = get_qlib_service()
        # 使用线程执行 qlib.init()，设置 90s 超时避免无限卡死
        await asyncio.wait_for(asyncio.to_thread(qlib_service.initialize), timeout=90)
        logger.info("✅ Qlib runtime initialized successfully")
    except asyncio.TimeoutError:
        logger.error("❌ Qlib runtime initialization timed out after 90s (non-fatal)")
    except Exception as e:
        logger.error(f"❌ Qlib runtime initialization failed: {e} (non-fatal)")

    try:
        await BacktestPersistence().ensure_tables()
        await OptimizationPersistence().ensure_tables()
        logger.info("✅ Backtest/Optimization tables ensured")
    except Exception as e:
        logger.error(f"❌ Table ensure failed: {e} (non-fatal)")


app = FastAPI(
    title="QuantMind Computational Engine",
    version="2.0.0",
    description="收敛后的计算引擎服务（整合了 AI 策略生成、模型推理、回测等模块）",
    lifespan=lifespan,
    generate_unique_id_function=quantmind_generate_unique_id,
)

install_request_id_middleware(app)
install_error_contract_handlers(app)
install_access_log_middleware(app, service_name="quantmind-engine")


# 必须持有**用户身份**（不只是内部密钥）才能访问的引擎路由前缀。
#
# 为什么单独拎成模块级常量：网关（`engine_proxy`）对**每一个**转发请求都无条件
# 注入 `X-Internal-Call`，所以「内部密钥匹配」这一条在网关面前恒为真——真正的
# 关卡是下面那句 `if not user_id and any(...)`。前缀表漏掉一个前缀，该前缀下的
# 全部路由就变成**匿名可达**，而且不报错、不打日志，没有任何一层会提示。
# 拎出来的目的就是让它可被测试直接断言（见
# `backend/tests/test_factor_research_auth_gate.py`）。
#
# 判定用的是 `str.startswith`，所以每一项**必须带结尾斜杠**：写成
# `/api/v1/analysis` 会连带把 `/api/v1/analysis-anything` 一起放进来。
PROTECTED_PREFIXES: tuple[str, ...] = (
    "/api/v1/qlib/",
    "/api/v1/strategies/",
    "/api/v1/inference/",
    "/api/v1/analysis/",
    "/api/v1/selection/",
    "/api/v1/scanner/",
    "/api/v1/strategy/",
    "/api/v1/backtest/",
    "/api/v1/rd-agent/",
    "/api/v1/alpha-agent/",
    "/api/v1/pipeline/",
    "/api/v1/admin/",
    # 因子研究：`/scan` 会吐出本地挖出来的因子名与来源库、`/leaderboard` 与
    # `/catalog` 会吐出私人因子库的目录——都是私有 alpha。此前不在表内，
    # 2026-10-07 实测匿名 `GET /api/v1/factor-research/scan?dataset=private`
    # 经网关返回 200（对照 `/api/v1/qlib/health` 正确地 401）。
    "/api/v1/factor-research/",
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    统一认证中间件：支持内部信任 Header (X-User-Id) 或直接通过 JWT 令牌 (Bearer) 校验。
    """
    path = request.url.path
    method = request.method.upper()
    internal_secret = request.headers.get("X-Internal-Call")
    expected_secret = get_internal_call_secret()

    # 1. 仅当内部密钥匹配时才信任网关透传的身份 Header。
    #    否则 X-User-Id 可被任意客户端伪造以冒充其他用户。
    is_internal_call = bool(expected_secret) and internal_secret == expected_secret
    if is_internal_call:
        user_id = request.headers.get("X-User-Id")
        tenant_id = request.headers.get("X-Tenant-Id", "default")
    else:
        user_id = None
        tenant_id = "default"

    # 2. 如果没有信任 Header，尝试直接校验 JWT (支持 Nginx 直接转发)
    auth_header = request.headers.get("Authorization")
    if not user_id and auth_header and auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ")[1]
            payload = AuthManager().verify_token(token)
            user_id = str(payload.get("sub") or payload.get("user_id") or "")
            tenant_id = str(payload.get("tenant_id") or "default")
        except Exception:
            # 校验失败不立即报错，留待具体的路由逻辑（或 internal_secret 检查）决定
            pass

    # 所有 /api/v1/* 业务路由必须通过内部密钥或有效的用户身份。
    # 只有在内部密钥不匹配且用户身份也缺失的情况下才报错（OPTIONS 放行）。
    # 额外：对于需要用户上下文的业务路由（backtest/strategies/inference/analysis/selection），
    # 即使内部密钥匹配，也必须提供 user_id，否则下游会报 "Missing authenticated user context"。
    if method != "OPTIONS" and path.startswith("/api/v1/"):
        if not is_internal_call and not user_id:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required (Invalid internal secret or missing user context)"},
            )
        # 需要用户上下文的受保护路由列表（唯一出处见模块级 PROTECTED_PREFIXES）
        if not user_id and any(path.startswith(p) for p in PROTECTED_PREFIXES):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "需要登录。请确认您的登录状态并重试。"},
            )

    if user_id:
        # admin 身份规范化（与 api 侧 auth 中间件同口径）：旧 token 的 sub='00000001'/'admin'
        # 归一为 10000001，否则下游按 legacy id 读写（实测事故：策略列表自动同步模板写入
        # user_id=1 的克隆行，"用户策略不存在"/改名失败）
        try:
            from backend.shared.admin_identity import (
                is_admin_user_id,
                normalize_admin_user_id,
            )

            if is_admin_user_id(user_id):
                user_id = normalize_admin_user_id(user_id)
        except Exception:  # noqa: BLE001 - 规范化失败不阻断（保持旧行为）
            pass
        # 注入到 request.state
        request.state.user = {"user_id": user_id, "tenant_id": tenant_id, "sub": user_id}

    response = await call_next(request)
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(logger=logger),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 0. 用户策略管理（必须先于 AI 策略路由注册，否则 /strategies/{strategy_id}
#    动态路由会先于 /strategies/templates 等静态路由匹配，导致 templates 接口
#    被同步数据库查询阻塞、引发事件循环死锁）
try:
    from backend.services.engine.qlib_app.api.user_strategies import router as strategies_router

    app.include_router(strategies_router, prefix="/api/v1/strategies", tags=["Strategies"])
except ImportError as e:
    logger.error(f"❌ Failed to load Strategies router: {e}")

# 1. AI 策略生成
try:
    from backend.services.engine.ai_strategy.api.routes.strategy_backtest_loop import router as strategy_loop_router
    from backend.services.engine.ai_strategy.api.v1.routes import router as ai_strat_router
    from backend.services.engine.ai_strategy.api.v1.wizard import router as wizard_router

    app.include_router(ai_strat_router, prefix="/api/v1", tags=["AI Strategy"])
    app.include_router(wizard_router, prefix="/api/v1", tags=["Strategy Wizard"])
    app.include_router(strategy_loop_router, prefix="/api/v1", tags=["Strategy Loop"])
except ImportError as e:
    logger.error(f"❌ Failed to load AI Strategy routers: {e}")


# 2. 模型推理
try:
    from backend.services.engine.routers.inference import router as inference_router

    app.include_router(inference_router, prefix="/api/v1/inference", tags=["AI Inference"])
except ImportError as e:
    logger.error(f"❌ Failed to load AI Inference router: {e}")

# 3. 闭环编排
try:
    from backend.services.engine.routers.pipeline import router as pipeline_router

    app.include_router(pipeline_router, prefix="/api/v1/pipeline", tags=["Pipeline"])
except ImportError as e:
    logger.error(f"❌ Failed to load Pipeline router: {e}")

# 3.1 实盘E2E最小契约接口
try:
    from backend.services.engine.routers.realtime_contract import router as realtime_contract_router

    app.include_router(realtime_contract_router, prefix="/api/v1")
except ImportError as e:
    logger.error(f"❌ Failed to load Realtime Contract router: {e}")

# 3.2 选股（策略 v2.0 三层过滤）
try:
    from backend.services.engine.routers.selection import router as selection_router
    from backend.services.engine.routers.scanner import router as scanner_router

    app.include_router(selection_router, prefix="/api/v1")
    app.include_router(scanner_router, prefix="/api/v1", tags=["Scanner"])
except ImportError as e:
    logger.error(f"❌ Failed to load Selection router: {e}")

# 4. 量化回测
try:
    from backend.services.engine.qlib_app.api.backtest import router as backtest_router

    app.include_router(backtest_router, prefix="/api/v1", tags=["Qlib Backtest"])
except ImportError as e:
    logger.error(f"❌ Failed to load Qlib Backtest router: {e}")

# 4.0 全局股票池（用户态只读；写侧在 /api/v1/admin/stock-pools）
try:
    from backend.services.engine.routers.stock_pool import router as stock_pool_router

    app.include_router(stock_pool_router, prefix="/api/v1")
    logger.info("✅ Stock Pool router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Stock Pool router: {e}")

# 4.1 高级分析
try:
    from backend.services.engine.qlib_app.api.analysis import router as analysis_router

    # analysis_router 自带 /api/v1/analysis 前缀
    app.include_router(analysis_router, tags=["Qlib Analysis"])
except ImportError as e:
    logger.error(f"❌ Failed to load Qlib Analysis router: {e}")

# 5.1 管理员策略模板管理
try:
    from backend.services.engine.qlib_app.api.admin_templates import router as admin_templates_router

    app.include_router(
        admin_templates_router,
        prefix="/api/v1/admin/strategy-templates",
        tags=["Admin-StrategyTemplates"],
    )
    logger.info("✅ Admin Strategy Templates router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Admin Strategy Templates router: {e}")

# 6. 股票查询与智能选股
try:
    from backend.services.engine.stock_query_app.routes import router as stock_router
    from backend.services.engine.stock_query_app.smart_screener_api import router as smart_screener_router

    app.include_router(stock_router)
    app.include_router(smart_screener_router)
    logger.info("✅ Stock Query & Smart Screener routers loaded")
except ImportError as e:
    logger.warning(f"⚠️ Stock Query router not available: {e}")


try:
    from backend.services.engine.routers.ai_ide.chat import router as ai_chat_router
    from backend.services.engine.routers.ai_ide.config import router as ai_config_router
    from backend.services.engine.routers.ai_ide.executor import router as ai_executor_router
    from backend.services.engine.routers.ai_ide.workspace import router as ai_workspace_router

    app.include_router(ai_chat_router, prefix="/api/v1/ai-ide/ai", tags=["Cloud IDE-AI"])
    app.include_router(ai_config_router, prefix="/api/v1/ai-ide/config", tags=["Cloud IDE-Config"])
    app.include_router(ai_executor_router, prefix="/api/v1/ai-ide/execute", tags=["Cloud IDE-Executor"])
    app.include_router(ai_workspace_router, prefix="/api/v1/ai-ide/files", tags=["Cloud IDE-Workspace"])
    logger.info("✅ Cloud AI-IDE routers loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Cloud AI-IDE routers: {e}")


try:
    from backend.services.engine.routers.rd_agent import router as rd_agent_router

    app.include_router(rd_agent_router)
    logger.info("✅ RD-Agent integration routers loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load RD-Agent routers: {e}")

try:
    from backend.services.engine.routers.alpha_agent import router as alpha_agent_router

    app.include_router(alpha_agent_router)
    logger.info("✅ AlphaAgent integration routers loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load AlphaAgent routers: {e}")

try:
    # 文档中心（T-FM-08）：整组端点自带 ENABLE_DOC_MINING 闸门（router 级依赖），
    # 默认关时全部 403，注册本身无副作用
    from backend.services.engine.routers.alpha_agent_docs import (
        router as alpha_agent_docs_router,
    )

    app.include_router(alpha_agent_docs_router)
    logger.info("✅ AlphaAgent docs routers loaded (gated by ENABLE_DOC_MINING)")
except ImportError as e:
    logger.error(f"❌ Failed to load AlphaAgent docs routers: {e}")

try:
    from backend.services.engine.routers.trading_agents import router as trading_agents_router

    app.include_router(trading_agents_router)
    logger.info("✅ TradingAgents routers loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load TradingAgents routers: {e}")

try:
    from backend.services.engine.routers.quantbot_router import router as quantbot_router

    app.include_router(quantbot_router)
    logger.info("✅ QuantBot router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load QuantBot router: {e}")

try:
    from backend.services.engine.strategy_lab.routers import router as strategy_lab_router

    app.include_router(strategy_lab_router, prefix="/api/v1/ai-ide", tags=["Strategy Lab"])
    logger.info("✅ Strategy Lab router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Strategy Lab router: {e}")

try:
    # 因子报告（Alphalens 式）：技能中心「因子报告」页数据源
    from backend.services.engine.factor_report import router as factor_report_router

    app.include_router(factor_report_router)
    logger.info("✅ Factor Report router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Factor Report router: {e}")

try:
    # 因子研究（factor-lib-demo 改造版）：排行榜/单因子/对比/合成
    from backend.services.engine.factor_research.router import router as factor_research_router

    app.include_router(factor_research_router)
    logger.info("✅ Factor Research router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Factor Research router: {e}")

try:
    # 跨市场回测中心（T-FB）：矩阵/单因子/台账/曲线
    from backend.services.engine.factor_backtest.router import router as factor_backtest_router

    app.include_router(factor_backtest_router)
    logger.info("✅ Factor Backtest router loaded")
except ImportError as e:
    logger.error(f"❌ Failed to load Factor Backtest router: {e}")


@app.get("/health")
async def health_check():
    startup_healthy = bool(getattr(app.state, "startup_healthy", True))
    set_service_health("quantmind-engine", startup_healthy)
    return {
        "status": "healthy" if startup_healthy else "degraded",
        "service": "quantmind-engine",
    }


@app.get("/")
async def root():
    return {"message": "QuantMind Engine Core V2 is running"}


@app.get("/metrics")
async def metrics():
    return build_metrics_response()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, access_log=False)
