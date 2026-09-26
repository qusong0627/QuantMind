from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services.engine.qlib_app.celery_config import celery_app
from backend.services.engine.services.pipeline_service import PipelineService
from backend.services.engine.services.strategy_loop_persistence import (
    StrategyLoopPersistence,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.redis_sentinel_client import get_redis_sentinel_client

logger = logging.getLogger(__name__)

# 与 model_management.py 保持一致的锁配置
_INFERENCE_LOCK_KEY_PREFIX = "qm:lock:inference:daily"
_INFERENCE_LOCK_TTL_SEC = 1800  # 30 分钟


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _try_acquire_strategy_lock(strategy_id: str, trade_date: str, owner: str) -> bool:
    """尝试获取特定策略当日推理分布式锁。"""
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        redis = get_redis_sentinel_client()
        lock_key = f"{_INFERENCE_LOCK_KEY_PREFIX}:{strategy_id}:{trade_date}"
        return bool(redis.set(lock_key, owner, ex=_INFERENCE_LOCK_TTL_SEC, nx=True))
    except Exception as e:
        logger.warning("[InferenceLock] Redis 不可用，跳过策略锁检查: %s", e)
        return True


def _release_strategy_lock(strategy_id: str, trade_date: str, owner: str) -> None:
    """任务收尾时释放策略锁；只删自己持有的那把，避免误删他人的锁。"""
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        redis = get_redis_sentinel_client()
        lock_key = f"{_INFERENCE_LOCK_KEY_PREFIX}:{strategy_id}:{trade_date}"
        current = redis.get(lock_key)
        if current is None:
            return
        if isinstance(current, bytes):
            current = current.decode("utf-8", "ignore")
        if current == owner:
            redis.delete(lock_key)
    except Exception as e:
        # 释放失败不影响任务结果，锁仍由 TTL 兜底过期
        logger.warning("[InferenceLock] 释放策略锁失败（将由 TTL 兜底）: %s", e)


from backend.services.engine.services.signal_generator import global_signal_generator


@celery_app.task(
    name="engine.tasks.generate_global_signals",
    max_retries=3,
    default_retry_delay=60,
)
def generate_global_signals(
    universe: str = "all", mock: bool = False
) -> dict[str, Any]:
    """Celery 任务：生成全市场 Alpha 预测信号 (10万并发架构核心)。"""
    # 计算上海时区当日日期作为锁键（与 Admin 手动触发共享同一把锁）
    from zoneinfo import ZoneInfo

    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    acquired = _try_acquire_strategy_lock("global", trade_date, owner="celery_beat")
    if not acquired:
        logger.warning(
            "[InferenceLock] 当日全局推理锁已被占用（date=%s），Celery Beat 本次跳过。",
            trade_date,
        )
        return {"status": "skipped", "reason": "lock_held", "trade_date": trade_date}

    logger.info(
        "Global signal generation task started: universe=%s trade_date=%s",
        universe,
        trade_date,
    )
    result = _run_async(
        global_signal_generator.generate_and_broadcast(universe, mock=mock)
    )
    return {
        "status": "success" if result else "failed",
        "universe": universe,
        "trade_date": trade_date,
    }


@celery_app.task(
    bind=True,
    name="engine.tasks.run_pipeline_run",
    max_retries=1,
    default_retry_delay=30,
)
def run_pipeline_run(self, run_id: str) -> dict[str, Any]:
    """Celery 任务：执行 pipeline run。"""
    logger.info(
        "Pipeline Celery task started: run_id=%s task_id=%s", run_id, self.request.id
    )
    service = PipelineService()
    result = _run_async(service.execute_run(run_id))
    return result.model_dump()


@celery_app.task(
    name="engine.tasks.auto_inference_if_needed",
    max_retries=1,
    default_retry_delay=60,
)
def auto_inference_if_needed() -> dict[str, Any]:
    """[已下线] 原工作日 08:00 自动推理扫描。

    Beat 已取消；日常调度收敛为 ``engine.tasks.backfill_default_inference``
   （工作日 06:30，默认模型缺口补全）。本函数仅保留供紧急手动
    ``celery call``，请勿重新挂回 beat。
    """
    logger.warning(
        "[AutoInference] 任务已下线，请改用 engine.tasks.backfill_default_inference；"
        "本次仍按旧逻辑执行（手动调用兼容）"
    )
    from zoneinfo import ZoneInfo
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker
    from backend.shared.trading_calendar import calendar_service
    from backend.services.engine.inference.router_service import InferenceRouterService

    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))

    # 确定特征日期 (data_trade_date) 和预测日期 (prediction_trade_date)
    # 如果开盘前运行，T 是上一个交易日，T+1 是今日
    # 如果开盘后运行，T 是今日，T+1 是下一个交易日
    if now_local.time() < datetime.strptime("09:30", "%H:%M").time():
        data_trade_date_obj = _run_async(
            calendar_service.prev_trading_day(
                market="SSE",
                trade_date=now_local.date(),
                tenant_id="default",
                user_id="*",
            )
        )
    else:
        data_trade_date_obj = now_local.date()

    data_trade_date = data_trade_date_obj.isoformat()
    prediction_trade_date_obj = _run_async(
        calendar_service.next_trading_day(
            market="SSE",
            trade_date=data_trade_date_obj,
            tenant_id="default",
            user_id="*",
        )
    )
    prediction_trade_date = prediction_trade_date_obj.isoformat()

    # 0. 排除非交易日
    try:
        is_td = _run_async(
            calendar_service.is_trading_day(
                market="SSE",
                trade_date=data_trade_date_obj,
                tenant_id="default",
                user_id="*",
            )
        )
        if not is_td:
            logger.info("[AutoInference] 非交易日，跳过。date=%s", data_trade_date)
            return {"status": "skipped", "reason": "not_a_trading_day"}
    except Exception as e:
        logger.warning("[AutoInference] 日历检查异常: %s", e)

    # 1. 数据库准备
    sync_db_url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in sync_db_url:
        sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
    if not sync_db_url or "postgresql" not in sync_db_url:
        # 降级
        sync_db_url = "postgresql+psycopg2://postgres:quantmind2026@quantmind-postgresql:5432/quantmind"

    sync_engine = sa_create_engine(sync_db_url, pool_pre_ping=True)
    SessionLimit = sa_sessionmaker(bind=sync_engine)
    db = SessionLimit()

    # 2. 扫描活跃策略 + 用户自动推理设置
    try:
        # 调度留痕表（自动任务可观测性）
        db.execute(
            sa_text(
                """
                CREATE TABLE IF NOT EXISTS qm_model_inference_dispatch_logs (
                  id BIGSERIAL PRIMARY KEY,
                  trigger_source TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  strategy_id TEXT,
                  model_id TEXT,
                  data_trade_date DATE,
                  prediction_trade_date DATE,
                  status TEXT NOT NULL,
                  reason_code TEXT,
                  reason_detail TEXT,
                  run_id TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
        )
        db.execute(
            sa_text(
                """
                CREATE INDEX IF NOT EXISTS idx_qm_inf_dispatch_owner_created
                  ON qm_model_inference_dispatch_logs (tenant_id, user_id, created_at DESC);
                """
            )
        )
        db.execute(
            sa_text(
                """
                CREATE INDEX IF NOT EXISTS idx_qm_inf_dispatch_status_created
                  ON qm_model_inference_dispatch_logs (status, created_at DESC);
                """
            )
        )
        db.commit()

        def _write_dispatch_log(
            *,
            tenant_id: str,
            user_id: str,
            strategy_id: str | None,
            model_id: str | None,
            status: str,
            reason_code: str | None = None,
            reason_detail: str | None = None,
            run_id: str | None = None,
        ) -> None:
            db.execute(
                sa_text(
                    """
                    INSERT INTO qm_model_inference_dispatch_logs (
                      trigger_source, tenant_id, user_id, strategy_id, model_id,
                      data_trade_date, prediction_trade_date,
                      status, reason_code, reason_detail, run_id, created_at
                    ) VALUES (
                      :trigger_source, :tenant_id, :user_id, :strategy_id, :model_id,
                      :data_trade_date, :prediction_trade_date,
                      :status, :reason_code, :reason_detail, :run_id, NOW()
                    )
                    """
                ),
                {
                    "trigger_source": "celery_auto_inference_if_needed",
                    "tenant_id": str(tenant_id or "default"),
                    "user_id": str(user_id or ""),
                    "strategy_id": strategy_id,
                    "model_id": model_id,
                    "data_trade_date": data_trade_date,
                    "prediction_trade_date": prediction_trade_date,
                    "status": status,
                    "reason_code": reason_code,
                    "reason_detail": reason_detail,
                    "run_id": run_id,
                },
            )
            db.commit()

        # 查询所有活跃且绑定了策略的组合
        active_portfolios = db.execute(
            sa_text(
                "SELECT id, tenant_id, user_id, strategy_id FROM portfolios "
                "WHERE run_status = 'running' AND strategy_id IS NOT NULL AND is_deleted = False"
            )
        ).all()

        # 查询用户手动开启的自动推理设置（enabled=True）
        auto_inference_settings = db.execute(
            sa_text(
                "SELECT tenant_id, user_id, model_id, schedule_time "
                "FROM qm_model_inference_settings "
                "WHERE enabled = TRUE"
            )
        ).all()

        # 仅调度真实目标：活跃组合 + 已启用的自动推理设置（无系统虚任务/无兜底）
        tasks: list[dict[str, Any]] = []
        for p in active_portfolios:
            tasks.append(
                {
                    "tenant_id": p.tenant_id,
                    "user_id": str(p.user_id),
                    "strategy_id": str(p.strategy_id or "default"),
                    "model_id": None,
                }
            )

        for s in auto_inference_settings:
            tasks.append(
                {
                    "tenant_id": s.tenant_id,
                    "user_id": str(s.user_id),
                    "strategy_id": None,  # 用户级别推理，不绑定特定策略
                    "model_id": str(s.model_id),
                }
            )

        # 去重（同一 tenant_id + user_id 可能同时出现在 portfolios 和 settings 中）
        seen = set()
        unique_tasks = []
        for t in tasks:
            key = (
                t["tenant_id"],
                t["user_id"],
                t.get("strategy_id"),
                t.get("model_id"),
            )
            if key not in seen:
                seen.add(key)
                unique_tasks.append(t)
        tasks = unique_tasks

        logger.info(
            "[AutoInference] 发现需检查的任务总数: %d (活跃策略=%d, 自动推理设置=%d)",
            len(tasks),
            len(active_portfolios),
            len(auto_inference_settings),
        )

        if not tasks:
            logger.info(
                "[AutoInference] 无调度目标（无 running 组合且无启用的自动推理设置），跳过。"
            )
            _write_dispatch_log(
                tenant_id="default",
                user_id="system",
                strategy_id=None,
                model_id=None,
                status="skipped",
                reason_code="NO_TARGETS",
                reason_detail="no running portfolios and no enabled auto-inference settings",
            )
            return {
                "status": "skipped",
                "reason": "no_targets",
                "date": prediction_trade_date,
                "processed_count": 0,
                "details": [],
            }

        results = []
        redis = None
        try:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client

            redis = get_redis_sentinel_client()
        except:
            pass

        router_service = InferenceRouterService()

        # 3. 依次执行
        for task in tasks:
            tid = task["tenant_id"]
            uid = task["user_id"]
            sid = task.get("strategy_id")
            mid = task.get("model_id")

            # 检查当日是否已完成 (DB 记录)
            # 对于全局任务，检查 source='inference_script'，对于策略，检查 strategy_id
            exists = db.execute(
                sa_text(
                    "SELECT 1 FROM engine_feature_runs "
                    "WHERE trade_date = :d AND status = 'signal_ready' "
                    "AND tenant_id = :tid AND user_id = :uid LIMIT 1"
                ),
                {"d": prediction_trade_date, "tid": tid, "uid": uid},
            ).first()

            if exists:
                _write_dispatch_log(
                    tenant_id=tid,
                    user_id=uid,
                    strategy_id=sid,
                    model_id=mid,
                    status="skipped",
                    reason_code="ALREADY_DONE",
                    reason_detail="engine_feature_runs already has signal_ready record for target trade date",
                )
                continue

            # 尝试获取锁
            lock_scope = f"{tid}:{uid}:{sid or mid or 'default'}"
            if not _try_acquire_strategy_lock(
                lock_scope, prediction_trade_date, "celery_auto"
            ):
                logger.info("[AutoInference] 任务锁冲突，跳过: tid=%s uid=%s", tid, uid)
                _write_dispatch_log(
                    tenant_id=tid,
                    user_id=uid,
                    strategy_id=sid,
                    model_id=mid,
                    status="skipped",
                    reason_code="LOCK_HELD",
                    reason_detail=f"lock scope conflict: {lock_scope}",
                )
                continue

            try:
                logger.info(
                    "[AutoInference] 正在执行任务: tenant=%s user=%s strategy=%s model=%s",
                    tid,
                    uid,
                    sid,
                    mid,
                )
                exec_res = router_service.run_daily_inference_script(
                    date=data_trade_date,
                    tenant_id=tid,
                    user_id=uid,
                    strategy_id=None if sid == "global" else sid,
                    model_id=mid,
                    redis_client=redis,
                )
                results.append(
                    {
                        "tenant_id": tid,
                        "user_id": uid,
                        "success": exec_res.success,
                        "run_id": exec_res.run_id,
                    }
                )
                _write_dispatch_log(
                    tenant_id=tid,
                    user_id=uid,
                    strategy_id=sid,
                    model_id=mid,
                    status="success" if exec_res.success else "failed",
                    reason_code=None if exec_res.success else "EXECUTION_FAILED",
                    reason_detail=None
                    if exec_res.success
                    else str(
                        getattr(exec_res, "error", "")
                        or getattr(exec_res, "stderr", "")
                        or getattr(exec_res, "failure_stage", "")
                        or ""
                    ),
                    run_id=getattr(exec_res, "run_id", None),
                )
            except Exception as task_exc:
                _write_dispatch_log(
                    tenant_id=tid,
                    user_id=uid,
                    strategy_id=sid,
                    model_id=mid,
                    status="failed",
                    reason_code="EXCEPTION",
                    reason_detail=str(task_exc),
                    run_id=None,
                )
                raise
            finally:
                # 释放锁
                try:
                    lock_key = f"{_INFERENCE_LOCK_KEY_PREFIX}:{lock_scope}:{prediction_trade_date}"
                    redis.delete(lock_key)
                except:
                    pass

        return {
            "status": "completed",
            "date": prediction_trade_date,
            "processed_count": len(results),
            "details": results,
        }
    except Exception as e:
        logger.exception("[AutoInference] 扫描/执行任务异常: %s", e)
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="engine.tasks.run_strategy_backtest_loop",
    max_retries=0,
)
def run_strategy_backtest_loop(
    self, task_id: str, request_payload: dict[str, Any]
) -> dict[str, Any]:
    """Celery 任务：执行策略-回测闭环。"""
    logger.info(
        "Strategy-backtest loop Celery task started: task_id=%s celery_id=%s",
        task_id,
        self.request.id,
    )

    async def _run() -> dict[str, Any]:
        from backend.services.engine.ai_strategy.api.routes.strategy_backtest_loop import (
            StrategyBacktestLoopRequest,
        )
        from backend.services.engine.ai_strategy.shared.ai_providers import (
            ComplexityLevel,
            StrategyRequest,
            StrategyType,
        )
        from backend.services.engine.ai_strategy.shared.market_data import (
            MarketDataService,
        )
        from backend.services.engine.ai_strategy.shared.strategy_backtest_loop import (
            LoopConfig,
            StrategyBacktestLoop,
        )

        persistence = StrategyLoopPersistence()
        await persistence.ensure_tables()
        await persistence.update_task(
            task_id=task_id,
            status="running",
            updated_at=datetime.now(),
        )

        request = StrategyBacktestLoopRequest(**request_payload)

        def _progress_callback(
            iteration: int, stage: Any, progress: float, best_score: float
        ) -> None:
            self.update_state(
                state="STARTED",
                meta={
                    "current_iteration": int(iteration),
                    "total_iterations": int(request.max_iterations),
                    "current_stage": getattr(stage, "value", str(stage)),
                    "progress_percentage": float(progress * 100),
                    "best_score": float(best_score),
                    "errors": [],
                },
            )

        loop_config = LoopConfig(
            max_iterations=request.max_iterations,
            backtest_period=request.backtest_period,
            initial_capital=request.initial_capital,
            risk_tolerance=request.risk_tolerance,
        )
        strategy_request = StrategyRequest(
            prompt=request.prompt,
            strategy_type=(
                StrategyType(request.strategy_type) if request.strategy_type else None
            ),
            complexity_level=ComplexityLevel(request.complexity_level),
            target_assets=request.target_assets,
            timeframe=request.timeframe,
            risk_tolerance=request.risk_tolerance,
            backtest_period=request.backtest_period,
            custom_requirements=request.custom_requirements,
        )

        market_data_service = MarketDataService()
        end_date = datetime.now()
        start_date = end_date - timedelta(days=730)
        market_data = await market_data_service.get_market_data(
            symbols=request.target_assets or ["SZ000001"],
            start_date=start_date,
            end_date=end_date,
            timeframe=request.timeframe,
        )

        loop_manager = StrategyBacktestLoop(loop_config)
        result = await loop_manager.run_loop(
            strategy_request, market_data, progress_callback=_progress_callback
        )

        best_strategy = {}
        performance_metrics = {}
        best_score = 0.0
        if result.best_iteration:
            best_score = float(
                getattr(result.best_iteration, "performance_score", 0.0) or 0.0
            )
            if getattr(result.best_iteration, "strategy_response", None):
                best_strategy = result.best_iteration.strategy_response.to_dict()
            if getattr(result.best_iteration, "backtest_result", None):
                performance_metrics = (
                    result.best_iteration.backtest_result.performance_metrics or {}
                )

        all_iterations = []
        for iteration in result.all_iterations:
            all_iterations.append(
                {
                    "iteration": iteration.iteration,
                    "stage": iteration.stage.value,
                    "performance_score": iteration.performance_score,
                    "improvement": iteration.improvement,
                    "execution_time": iteration.execution_time,
                    "errors": iteration.errors,
                }
            )

        # 在进入数据库持久化之前，报告 99% 进度，告知前端正在保存结果
        self.update_state(
            state="STARTED",
            meta={
                "current_iteration": int(result.total_iterations),
                "total_iterations": int(result.total_iterations),
                "current_stage": "persistence",
                "progress_percentage": 99.0,
                "best_score": best_score,
                "errors": [],
            },
        )

        payload = {
            "task_id": task_id,
            "success": bool(result.success),
            "total_iterations": int(result.total_iterations),
            "best_strategy": best_strategy,
            "performance_metrics": performance_metrics,
            "learning_insights": dict(result.learning_insights or {}),
            "execution_time": float(result.total_time),
            "all_iterations": all_iterations,
            "best_score": best_score,
        }
        await persistence.update_task(
            task_id=task_id,
            status="completed",
            updated_at=datetime.now(),
            result_payload=payload,
        )
        return payload

    owner_user_id = request_payload.get("_owner_user_id")
    if not owner_user_id:
        raise RuntimeError("missing owner identity for strategy loop task")
    try:
        return _run_async(_run())
    except Exception as exc:
        persistence = StrategyLoopPersistence()
        _run_async(persistence.ensure_tables())
        _run_async(
            persistence.update_task(
                task_id=task_id,
                status="failed",
                updated_at=datetime.now(),
                error_message=str(exc),
            )
        )
        raise


@celery_app.task(
    name="engine.tasks.sync_stock_daily_latest_task",
    max_retries=0,
)
def sync_stock_daily_latest_task(
    target_date: str | None = None, max_symbols: int = 0, apply: bool = True
) -> dict[str, Any]:
    """
    [已废弃] 数据现由官方服务器统一推送，不再需要本地 Baostock 同步。
    """
    logger.info("[SyncTask] 该任务已废弃，数据由官方服务器统一推送")
    return {
        "success": True,
        "message": "数据同步任务已废弃，数据由官方服务器统一推送",
        "deprecated": True,
    }


@celery_app.task(name="engine.tasks.get_data_status_task")
def get_data_status_task(market: str = "a_share") -> dict[str, Any]:
    """
    Celery 任务：扫描指定市场的 Qlib 数据与 feature snapshot 状态并写入 Redis 缓存。

    与 API 端 `/admin/models/data-status` 共用 `scan_data_status`，
    避免双方扫描逻辑/缓存 key 漂移。

    交易日通过 `resolve_trade_date_sync` 同步解析（exchange_calendars），
    避免在 Celery worker 内通过 asyncio.run() 跑 calendar_service 时与主
    API 的 asyncpg 池绑定到不同事件循环的冲突。
    """
    import asyncio
    import json

    from backend.services.api.routers.admin.data_status_scanner import (
        resolve_trade_date_sync,
        scan_data_status,
    )

    trade_date = resolve_trade_date_sync(market)

    coro = scan_data_status(
        market=market,
        tenant_id="default",
        user_id="admin",
        trade_date=trade_date,
    )
    try:
        result = asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(coro)
        finally:
            loop.close()

    try:
        redis = get_redis_sentinel_client()
        redis.set(
            f"qm:admin:data_status:{market}",
            json.dumps(result),
            ex=300,
        )
    except Exception as e:
        logger.warning("Failed to cache data status to Redis: %s", e)

    return result


@celery_app.task(name="engine.tasks.warmup_stock_latest_cache")
def warmup_stock_latest_cache_task():
    """Celery 任务：预热 stock_daily_latest 表的 Redis 缓存。"""
    from backend.shared.market_data.stock_daily_latest_cache import stock_latest_cache
    import asyncio

    logger.info("[CacheWarmup] 正在启动 stock_daily_latest 缓存预热...")
    try:
        # 由于当前环境可能在同步的 Celery worker 中运行，使用 asyncio.run 或直接调用
        count = asyncio.run(stock_latest_cache.warmup_cache())
        logger.info(f"[CacheWarmup] 预热完成，共缓存 {count} 只股票行情。")
        return {"status": "success", "count": count}
    except Exception as e:
        logger.error(f"[CacheWarmup] 预热失败: {e}")
        return {"status": "failed", "error": str(e)}


# ============================================================
# 资讯 enrichment：股票/行业/事件标签 + 情感分
# ============================================================


@celery_app.task(name="engine.tasks.news_enrich_recent", bind=True, ignore_result=True)
def news_enrich_recent_task(self, limit: int = 200) -> dict[str, Any]:
    """每分钟扫描 Huntly 最近 N 篇文章，对未 enrich 的写入 news_article_enrichment。

    幂等：huntly_page_id 是主键 + model_version 不变则跳过。
    """
    from backend.services.api.news import run_enrichment_batch

    try:
        n = run_enrichment_batch(limit=limit)
        logger.info("[NewsEnrich] 完成: %d 篇新写入", n)
        return {"status": "success", "written": n}
    except Exception as e:
        logger.error("[NewsEnrich] 失败: %s", e)
        return {"status": "failed", "error": str(e)}


@celery_app.task(name="engine.tasks.news_matcher_reload", ignore_result=True)
def news_matcher_reload_task() -> dict[str, Any]:
    """每 10 分钟重载 stock_aliases / finance_lexicon 自动机，
    让管理员在 SQL 里新增的词条尽快生效。"""
    from backend.services.api.news import get_matcher

    try:
        m = get_matcher(force_reload=True)
        return {"status": "success", "aliases": m.alias_count, "lex": m.lex_count}
    except Exception as e:
        logger.error("[NewsMatcherReload] 失败: %s", e)
        return {"status": "failed", "error": str(e)}


@celery_app.task(name="engine.tasks.daily_data_sync", max_retries=0, bind=True)
def daily_data_sync_task(
    self,
    market: str = "A",
    symbols: str = "",
    incremental: bool = True,
    calibrate: bool = True,
    skip_pg: bool = False,
) -> dict[str, Any]:
    """
    Celery Beat 每日 22:30 自动增量同步全市场数据。

    A 股：QuantDB SDK → parquet → Qlib 缓存（单源，PG 填充可跳过）
    HK/US/crypto：investment_data → baostock → akshare → eltdx 多源聚合
    使用 Redis 分布式锁防止并发执行。
    """
    import redis as _redis

    lock_key = "quantmind:daily_sync:lock"
    lock_ttl = 3600  # 1 小时自动过期
    try:
        rds = _redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=2
        )
        acquired = rds.set(lock_key, self.request.id, nx=True, ex=lock_ttl)
        if not acquired:
            running_id = rds.get(lock_key)
            logger.warning("[DailySync] 已有任务在运行 (task_id=%s)，跳过", running_id)
            return {"status": "skipped", "reason": f"已有同步任务在运行: {running_id}"}
    except Exception as lock_exc:
        logger.warning("[DailySync] Redis 锁获取失败，继续执行: %s", lock_exc)

    logger.info(
        "[DailySync] 开始: market=%s incremental=%s symbols=%s",
        market,
        incremental,
        symbols[:100],
    )
    try:
        sym_list = (
            [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
        )

        if market.upper() == "A":
            from backend.scripts.quantdb_daily_sync import run_daily_sync
            from backend.shared.quantdb_sync_jobs import (
                new_celery_job,
                celery_progress_cb,
                upsert_job,
                _now_iso,
            )

            job = new_celery_job(with_pg=not skip_pg)
            job_id = job["job_id"]
            logger.info("[DailySync] 创建 Redis 同步任务 %s", job_id)

            result = run_daily_sync(
                skip_pg=skip_pg, progress_cb=celery_progress_cb(job_id)
            )
            upsert_job(job_id, status="completed", stage="done", finished_at=_now_iso())
            logger.info(
                "[DailySync] QuantDB 完成: parquet=%s pg_rows=%s qlib=%s",
                (result.get("parquet") or {}).get("total_downloaded"),
                (result.get("pg_fill") or {}).get("rows"),
                (result.get("qlib_cache") or {}).get("status"),
            )
            logger.info(
                "[DailySync] QuantDB 完成: parquet=%s pg_rows=%s qlib=%s",
                (result.get("parquet") or {}).get("total_downloaded"),
                (result.get("pg_fill") or {}).get("rows"),
                (result.get("qlib_cache") or {}).get("status"),
            )
            return result

        from backend.scripts.daily_data_sync import run_sync

        result = run_sync(
            market=market,
            symbols=sym_list,
            incremental=incremental,
            update_qlib=True,
            calibrate=calibrate,
        )
        logger.info(
            "[DailySync] 完成: inv=%d bs=%d ak=%d eltdx=%d errors=%d",
            result.get("investment_data_synced", 0),
            result.get("baostock_synced", 0),
            result.get("akshare_synced", 0),
            result.get("eltdx_synced", 0),
            len(result.get("errors", [])),
        )
        return result
    except Exception as e:
        logger.exception("[DailySync] 失败: %s", e)
        raise
    finally:
        try:
            rds.delete(lock_key)
        except Exception:
            pass


@celery_app.task(name="engine.tasks.update_qlib_cache", max_retries=0, bind=True)
def update_qlib_cache_task(self) -> dict[str, Any]:
    """独立增量更新 Qlib 缓存（不依赖完整每日同步）。

    从 QuantDB parquet 增量生成 Qlib 二进制缓存。即使主同步任务因
    PG 填充等环节超时被杀，此任务也能保证 Qlib 数据跟进最新交易日。
    """
    try:
        from backend.services.engine.qlib_data_builder import ensure_qlib_cache

        provider = ensure_qlib_cache("/data/quantdb")
        logger.info("[QlibCache] 更新完成: %s", provider)
        return {"status": "ok", "provider_uri": provider}
    except Exception as exc:
        logger.exception("[QlibCache] 更新失败: %s", exc)
        return {"status": "error", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Strategy Lab daily scan (Day 16)
# ---------------------------------------------------------------------------
@celery_app.task(
    name="engine.tasks.feature_snapshot",
    max_retries=1,
    default_retry_delay=120,
    bind=True,
)
def feature_snapshot_task(self, year: int = 0) -> dict[str, Any]:
    """Legacy compatibility task; new models read raw QuantDB factors directly."""
    from datetime import date as _date

    target_year = year if year > 0 else _date.today().year
    if os.getenv("QM_ENABLE_LEGACY_FEATURE_SNAPSHOT", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return {
            "status": "skipped",
            "year": target_year,
            "reason": "direct QuantDB factor reader is active; legacy snapshot generation disabled",
        }
    logger.info(
        "[FeatureSnapshot] 开始: year=%d task_id=%s", target_year, self.request.id
    )

    try:
        from backend.scripts.generate_feature_snapshots import _build_snapshot

        result = _build_snapshot(target_year)
        if result is None:
            logger.warning("[FeatureSnapshot] %d 年无数据，跳过", target_year)
            return {"status": "skipped", "year": target_year, "reason": "no_data"}

        logger.info(
            "[FeatureSnapshot] 完成: year=%d rows=%s symbols=%s",
            target_year,
            result.get("row_count"),
            result.get("symbol_count"),
        )
        return {"status": "success", **result}

    except Exception as e:
        logger.exception("[FeatureSnapshot] 失败: %s", e)
        raise


@celery_app.task(name="engine.tasks.strategy_lab_daily_scan")
def strategy_lab_daily_scan(lookback_days: int = 7) -> dict[str, Any]:
    """Run all watched Strategy Lab scripts and persist today's signals."""
    try:
        from backend.services.engine.strategy_lab.cron.daily_scan import run_daily_scan

        return run_daily_scan(lookback_days=lookback_days)
    except Exception as e:
        logger.exception("[StrategyLabScan] failed")
        return {"status": "failed", "error": str(e)}


@celery_app.task(
    name="engine.tasks.backfill_default_inference",
    max_retries=0,
)
def backfill_default_inference(
    tenant_id: str | None = None,
    user_id: str | None = None,
    model_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """工作日窗口内轮询：补全所有用户默认模型推理缺口（含历史空洞）。

    与前端「一键补全至最新」同链路（gap_backfill）：
    扫描 pred.parquet 覆盖，缺口上限截至 QuantDB 因子已产出日，
    逐日 InferenceScriptRunner 并合并回 pred.parquet。
    数据同步窗口约 01:00–06:00，本任务在窗口内每 30 分钟轮询，
    无缺口时直接跳过（上游分区晚到也能在当天补上）。
    """
    from backend.services.engine.inference.gap_backfill import (
        backfill_all_default_models,
    )

    # 轮询窗口防重入：全量调度（无过滤条件）拿不到锁说明上一轮仍在跑，跳过；
    # 带 tenant/user/model 的手动调用不受锁限制。Redis 不可用时降级放行。
    lock_owner = "celery_backfill_default_inference"
    lock_day: str | None = None
    if not (tenant_id or user_id or model_id) and not dry_run:
        from zoneinfo import ZoneInfo as _ZoneInfo

        lock_day = datetime.now(_ZoneInfo("Asia/Shanghai")).date().isoformat()
        if not _try_acquire_strategy_lock(
            "default_backfill", lock_day, owner=lock_owner
        ):
            logger.info(
                "[DefaultInferenceBackfill] 上一轮仍在执行（锁占用），本周期跳过"
            )
            _insert_backfill_dispatch_log(
                status="skipped",
                reason_code="LOCK_HELD",
                reason_detail="上一轮补全仍在执行（Redis 锁占用），本周期跳过",
            )
            return {"status": "skipped", "reason": "LOCK_HELD"}

    # 开始留痕：先写一条 running 记录，正常结束时再删掉。
    # 存在的意义是让「静默死亡」可见：任务被 OOM 杀掉时进程直接消失，来不及写任何
    # 结果行，这条 running 就会一直留在管理台「推理监控」里。
    # 2026-09-25 默认模型缺口连续被 OOM 杀掉十几次，管理台却一条记录都没有，
    # 就是因为只有成功路径才写留痕。
    marker_id = _insert_backfill_dispatch_log(
        status="running",
        reason_code="STARTED",
        reason_detail="任务已启动，等待完成；长期停留此状态说明任务中途被杀",
    )

    try:
        result = _run_async(
            backfill_all_default_models(
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                dry_run=dry_run,
            )
        )
        # 写入调度留痕，供管理台推理监控 / daily-review 技能读取
        try:
            _write_default_backfill_dispatch_logs(result)
        except Exception as log_exc:
            logger.warning(
                "[DefaultInferenceBackfill] 写 dispatch_logs 失败: %s", log_exc
            )
        _delete_backfill_dispatch_log(marker_id)
        logger.info(
            "[DefaultInferenceBackfill] done status=%s models=%s completed=%s "
            "partial=%s failed=%s skipped=%s",
            result.get("status"),
            result.get("total_models"),
            result.get("completed"),
            result.get("partial"),
            result.get("failed"),
            result.get("skipped"),
        )
        return result
    except Exception as exc:
        logger.exception("[DefaultInferenceBackfill] failed")
        _insert_backfill_dispatch_log(
            status="failed",
            reason_code="EXCEPTION",
            reason_detail=str(exc)[:500],
        )
        _delete_backfill_dispatch_log(marker_id)
        return {"status": "failed", "error": str(exc)}
    finally:
        # 收尾释放锁：锁的本意是防重入，不是限流。原来只靠 TTL(1800s) 自动过期，
        # 而调度间隔正好也是 30 分钟，于是下一轮总在锁过期前到达 → 隔轮被判
        # LOCK_HELD 跳过，实际轮询频率掉到 60 分钟。被 OOM 杀掉时 finally 不执行，
        # 仍由 TTL 兜底，原有保护不受影响。
        if lock_day:
            _release_strategy_lock("default_backfill", lock_day, owner=lock_owner)


_DISPATCH_LOG_DDL = """
CREATE TABLE IF NOT EXISTS qm_model_inference_dispatch_logs (
  id BIGSERIAL PRIMARY KEY,
  trigger_source TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  strategy_id TEXT,
  model_id TEXT,
  data_trade_date DATE,
  prediction_trade_date DATE,
  status TEXT NOT NULL,
  reason_code TEXT,
  reason_detail TEXT,
  run_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _backfill_dispatch_db():
    """默认模型补全留痕用的同步 DB 会话；不可用时返回 (None, None)。"""
    import os

    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    sync_db_url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in sync_db_url:
        sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
    if not sync_db_url or "postgresql" not in sync_db_url:
        return None, None
    engine = sa_create_engine(sync_db_url, pool_pre_ping=True)
    return sa_sessionmaker(bind=engine)(), engine


def _insert_backfill_dispatch_log(
    *,
    status: str,
    reason_code: str | None,
    reason_detail: str,
    model_id: str | None = None,
) -> int | None:
    """写一条默认模型补全留痕，返回自增 id（供结束时删除标记行）。"""
    from sqlalchemy import text as sa_text

    db = engine = None
    try:
        db, engine = _backfill_dispatch_db()
        if db is None:
            return None
        db.execute(sa_text(_DISPATCH_LOG_DDL))
        row_id = db.execute(
            sa_text(
                """
                INSERT INTO qm_model_inference_dispatch_logs (
                  trigger_source, tenant_id, user_id, strategy_id, model_id,
                  data_trade_date, prediction_trade_date,
                  status, reason_code, reason_detail, run_id, created_at
                ) VALUES (
                  'celery_backfill_default_inference', 'default', 'system', NULL,
                  :model_id, NULL, NULL,
                  :status, :reason_code, :reason_detail, NULL, NOW()
                ) RETURNING id
                """
            ),
            {
                "model_id": model_id,
                "status": status,
                "reason_code": reason_code,
                "reason_detail": str(reason_detail or "")[:500],
            },
        ).scalar()
        db.commit()
        return int(row_id) if row_id is not None else None
    except Exception as exc:
        logger.warning("[DefaultInferenceBackfill] 写调度留痕失败: %s", exc)
        return None
    finally:
        if db is not None:
            db.close()
        if engine is not None:
            engine.dispose()


def _delete_backfill_dispatch_log(row_id: int | None) -> None:
    """删除任务开始时的 running 标记行（任务已正常收尾时调用）。"""
    if row_id is None:
        return
    from sqlalchemy import text as sa_text

    db = engine = None
    try:
        db, engine = _backfill_dispatch_db()
        if db is None:
            return
        db.execute(
            sa_text("DELETE FROM qm_model_inference_dispatch_logs WHERE id = :id"),
            {"id": row_id},
        )
        db.commit()
    except Exception as exc:
        logger.warning("[DefaultInferenceBackfill] 删除调度留痕失败: %s", exc)
    finally:
        if db is not None:
            db.close()
        if engine is not None:
            engine.dispose()


def _write_default_backfill_dispatch_logs(result: dict[str, Any]) -> None:
    """把默认模型补全结果写入 qm_model_inference_dispatch_logs。"""
    import os
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    details = list(result.get("details") or [])
    if not details:
        return

    sync_db_url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in sync_db_url:
        sync_db_url = sync_db_url.replace("+asyncpg", "+psycopg2")
    if not sync_db_url or "postgresql" not in sync_db_url:
        return

    engine = sa_create_engine(sync_db_url, pool_pre_ping=True)
    Session = sa_sessionmaker(bind=engine)
    db = Session()
    try:
        db.execute(sa_text(_DISPATCH_LOG_DDL))
        for d in details:
            st = str(d.get("status") or "")
            if st == "up_to_date":
                status, reason = "skipped", "UP_TO_DATE"
            elif st == "dry_run":
                status, reason = "skipped", "DRY_RUN"
            elif st == "completed":
                status, reason = "success", None
            elif st == "partial":
                status, reason = "failed", "PARTIAL"
            else:
                status, reason = "failed", "BACKFILL_FAILED"
            cutoff = d.get("data_cutoff_date")
            db.execute(
                sa_text(
                    """
                    INSERT INTO qm_model_inference_dispatch_logs (
                      trigger_source, tenant_id, user_id, strategy_id, model_id,
                      data_trade_date, prediction_trade_date,
                      status, reason_code, reason_detail, run_id, created_at
                    ) VALUES (
                      :trigger_source, :tenant_id, :user_id, NULL, :model_id,
                      CAST(:data_trade_date AS DATE), CAST(:prediction_trade_date AS DATE),
                      :status, :reason_code, :reason_detail, NULL, NOW()
                    )
                    """
                ),
                {
                    "trigger_source": "celery_backfill_default_inference",
                    "tenant_id": str(d.get("tenant_id") or "default"),
                    "user_id": str(d.get("user_id") or ""),
                    "model_id": str(d.get("model_id") or "") or None,
                    "data_trade_date": cutoff,
                    "prediction_trade_date": cutoff,
                    "status": status,
                    "reason_code": reason,
                    "reason_detail": (
                        f"gap={d.get('gap')} appended={d.get('appended')} "
                        f"failed={d.get('failed')} error={d.get('error') or ''}"
                    )[:500],
                },
            )
        db.commit()
    finally:
        db.close()
        engine.dispose()


@celery_app.task(name="engine.tasks.backfill_inference_quality")
def backfill_inference_quality(
    horizon_days: int = 5, limit: int = 500
) -> dict[str, Any]:
    """回填推理质量：为已完成推理但无 quality 记录、且已过收益兑现期的模型算真实 IC。

    滞后 horizon_days 天执行（需等未来收益兑现）。扫描 qm_model_inference_runs 中
    data_trade_date <= 当前- horizon 且未在 qm_model_inference_quality 的日期。
    """
    from datetime import timedelta
    from sqlalchemy import text
    from backend.services.engine.inference.inference_quality_backfill import (
        inference_quality_backfill,
    )

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=horizon_days)).date()

        async def _scan_and_backfill():
            await inference_quality_backfill.ensure_tables()
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT DISTINCT r.tenant_id, r.user_id, r.model_id, r.data_trade_date
                            FROM qm_model_inference_runs r
                            WHERE r.status = 'completed'
                              AND r.signals_count > 0
                              AND r.data_trade_date <= :cutoff
                              AND NOT EXISTS (
                                  SELECT 1 FROM qm_model_inference_quality q
                                  WHERE q.model_id = r.model_id AND q.trade_date = r.data_trade_date
                              )
                            ORDER BY r.data_trade_date DESC
                            LIMIT :limit
                            """
                            ),
                            {"cutoff": cutoff, "limit": int(limit)},
                        )
                    )
                    .mappings()
                    .all()
                )
            results = []
            for row in rows:
                res = await inference_quality_backfill.backfill_date(
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    model_id=row["model_id"],
                    trade_date=str(row["data_trade_date"])[:10],
                    market="CN",
                    horizon=horizon_days,
                )
                results.append(res)
            ok = [r for r in results if r.get("status") == "ok"]
            return {
                "status": "completed",
                "scanned": len(rows),
                "ok": len(ok),
                "samples": results[:5],
            }

        loop = asyncio.new_event_loop()
        try:
            return asyncio.run(_scan_and_backfill())
        finally:
            loop.close()
    except Exception as e:
        logger.exception("[QualityBackfill] 失败: %s", e)
        return {"status": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# 市场定时同步调度（前端每市场配置 HH:MM，beat 每分钟派发检查）
# ---------------------------------------------------------------------------
@celery_app.task(name="engine.tasks.dispatch_market_sync")
def dispatch_market_sync() -> dict[str, Any]:
    """每分钟检查各市场定时同步配置，到点派发同步任务。"""
    try:
        from backend.services.engine.tasks.market_sync_scheduler import (
            dispatch_due_syncs,
        )

        return dispatch_due_syncs()
    except Exception as e:
        logger.exception("[SyncSchedule] 派发检查失败: %s", e)
        return {"status": "failed", "error": str(e)}


@celery_app.task(
    name="engine.tasks.run_market_scheduled_sync",
    # 上游数据源（yfinance 等）被限流时会把单个 ticker 拖到分钟级，整体超过全局
    # 3600s 硬限制 → 进程被 SIGKILL；叠加 acks_late 会导致任务重新入队、再次超时，
    # 形成死循环。这里收紧独立超时，并在派发即 ack，保证一次调度最多失败一次。
    soft_time_limit=int(os.getenv("MARKET_SYNC_SOFT_TIME_LIMIT", "1800")),
    time_limit=int(os.getenv("MARKET_SYNC_TIME_LIMIT", "2100")),
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_market_scheduled_sync(market: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """执行某市场的定时同步（由 dispatch_market_sync 派发）。"""
    try:
        from backend.services.engine.tasks.market_sync_scheduler import run_market_sync

        return run_market_sync(market, cfg)
    except Exception as e:
        logger.exception("[SyncSchedule] %s 定时同步失败: %s", market, e)
        return {"market": market, "status": "failed", "error": str(e)}


# 与 compute.py 同口径的轻量新鲜度检查（只读目录与 latest.json，不引重型依赖）。
# 快照分区目录：<data_dir>/1_kline_data/daily_unadjusted/dt=YYYYMMDD
_SNAPSHOT_PARTITION_REL = "1_kline_data/daily_unadjusted"


def _snapshot_source_state(
    data_dir: str, out_dir: str
) -> tuple[str | None, str | None]:
    """返回 (库内最大分区日期, latest.json 的 trade_date)，均为 YYYYMMDD 或 None。"""
    import json as _json
    from pathlib import Path as _P

    max_part: str | None = None
    part_root = _P(data_dir) / _SNAPSHOT_PARTITION_REL
    try:
        if part_root.is_dir():
            dates = [
                e.name.split("=", 1)[1]
                for e in part_root.iterdir()
                if e.is_dir()
                and e.name.startswith("dt=")
                and "=" in e.name
                and e.name.split("=", 1)[1].isdigit()
            ]
            max_part = max(dates) if dates else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MarketSnapshot] 扫描分区失败: %s", exc)

    latest_td: str | None = None
    latest_path = _P(out_dir) / "latest.json"
    try:
        if latest_path.is_file():
            td = str(
                _json.loads(latest_path.read_text(encoding="utf-8")).get("trade_date")
                or ""
            )
            td = td.strip().replace("-", "")
            if len(td) == 8 and td.isdigit():
                latest_td = td
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MarketSnapshot] 读取 latest.json 失败: %s", exc)
    return max_part, latest_td


@celery_app.task(name="engine.tasks.market_snapshot")
def run_market_snapshot() -> dict[str, Any]:
    """在服务器容器内计算市场分析快照，写入 QM_MARKET_SNAPSHOT_DIR。

    服务器即生产环境：数据(读取容器 /data/quantdb)与脚本都在容器内。
    交易日盘后由 beat 触发，API 通过 QM_MARKET_SNAPSHOT_DIR=/data/market-analysis 读取。

    新鲜度门控：仅当库内最大分区比线上快照（latest.json 的 trade_date）更新时
    才真正计算并覆盖 latest；否则直接跳过（同步还没跑完或今日无新数据），
    避免用过期数据静默覆盖线上快照。beat 在 04:00–05:50 每 10 分钟触发一次，
    给用户配置时间的同步留足完成窗口；节假日无新分区时全天跳过属正常行为。
    """
    import subprocess
    import sys
    from datetime import datetime
    from pathlib import Path as _Path

    try:
        root = _Path(__file__).resolve().parents[4]  # 仓库根目录（含 backend/）
        script = root / "backend" / "scripts" / "market_snapshot" / "compute.py"
        data_dir = os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb")
        out_dir = os.getenv("QM_MARKET_SNAPSHOT_DIR", "/data/market-analysis")
        _Path(out_dir).mkdir(parents=True, exist_ok=True)

        max_part, latest_td = _snapshot_source_state(str(data_dir), str(out_dir))
        if max_part is None:
            logger.error("[MarketSnapshot] 跳过：未找到任何日线分区")
            return {"status": "skipped", "reason": "no_partitions"}
        # 日期命名 SQLite（{YYYY-MM-DD}.db）由离线全量构建产出，内含 tags 标签库；
        # 流式刷新只写 latest.json + latest.db 的 sector_mv/meta。因此即使 latest.json
        # 已是最新交易日，只要对应日期库缺失，就必须补跑一次全量，否则「标签双向查询」断档。
        iso_part = f"{max_part[:4]}-{max_part[4:6]}-{max_part[6:8]}"
        dated_db = _Path(out_dir) / f"{iso_part}.db"
        if latest_td is not None and max_part <= latest_td:
            if dated_db.is_file():
                logger.info(
                    "[MarketSnapshot] 跳过：已是最新交易日 %s 且 %s.db 存在",
                    latest_td,
                    iso_part,
                )
                return {
                    "status": "skipped",
                    "reason": "stale",
                    "max_partition": max_part,
                    "latest_trade_date": latest_td,
                }
            logger.warning(
                "[MarketSnapshot] latest.json 已为 %s 但缺少离线日期库 %s.db（含 tags），强制全量重建",
                latest_td,
                iso_part,
            )

        cmd = [
            sys.executable,
            str(script),
            "--data-dir",
            data_dir,
            "--out",
            out_dir,
            "--periods",
            "1d",
            "5d",
            "20d",
        ]
        started = datetime.now()
        logger.info("[MarketSnapshot] 开始: %s", " ".join(str(c) for c in cmd))

        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True, timeout=1800
        )
        if proc.returncode != 0:
            logger.error(
                "[MarketSnapshot] 失败 code=%d stderr=%s",
                proc.returncode,
                proc.stderr[-4000:],
            )
            return {
                "status": "failed",
                "returncode": proc.returncode,
                "stderr_tail": proc.stderr[-4000:],
            }

        elapsed = (datetime.now() - started).total_seconds()
        logger.info("[MarketSnapshot] 完成 用时%.0fs\n%s", elapsed, proc.stdout[-2000:])
        return {
            "status": "success",
            "elapsed_s": round(elapsed, 1),
            "out_dir": out_dir,
            "stdout_tail": proc.stdout[-1200:],
        }
    except Exception as e:
        logger.exception("[MarketSnapshot] 失败: %s", e)
        return {"status": "failed", "error": str(e)}


@celery_app.task(name="engine.tasks.purge_archived_models")
def purge_archived_models_task(
    retention_days: int = 0, dry_run: bool = False
) -> dict[str, Any]:
    """清理超过保留期的已归档用户模型（DB 行 + 磁盘目录）。

    归档是软删除（`archive_model` 只改 status），磁盘目录（含 pred.parquet 全量历史
    分数）与 DB 行会永久残留。本任务按保留期做硬删除，由 beat 每日触发一次。

    安全约束（详见 model_registry_service.purge_archived_models）：
    - 仍被策略绑定的模型跳过，不会静默破坏在用策略；
    - readonly 系统模型跳过；
    - 目录必须位于 USER_MODELS_ROOT 之内，否则拒绝删除并计入 failed。

    retention_days<=0 时取 MODEL_ARCHIVE_RETENTION_DAYS（默认 7）。
    dry_run=True 只报告不删除，可先 `celery call` 预演。
    """
    from backend.shared.model_registry import model_registry_service

    days = int(retention_days or 0)
    if days <= 0:
        days = int(os.getenv("MODEL_ARCHIVE_RETENTION_DAYS", "7"))
    result = _run_async(
        model_registry_service.purge_archived_models(
            retention_days=days, dry_run=bool(dry_run)
        )
    )
    logger.info(
        "[PurgeArchivedModels] retention=%dd dry_run=%s scanned=%s purged=%s "
        "referenced=%s readonly=%s failed=%s",
        days,
        bool(dry_run),
        result.get("scanned"),
        result.get("purged"),
        len(result.get("skipped_referenced") or []),
        len(result.get("skipped_readonly") or []),
        len(result.get("failed") or []),
    )
    return result
