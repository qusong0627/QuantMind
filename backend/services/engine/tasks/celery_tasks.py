from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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


from backend.services.engine.services.signal_generator import global_signal_generator


@celery_app.task(
    name="engine.tasks.generate_global_signals",
    max_retries=3,
    default_retry_delay=60,
)
def generate_global_signals(universe: str = "all", mock: bool = False) -> dict[str, Any]:
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

    logger.info("Global signal generation task started: universe=%s trade_date=%s", universe, trade_date)
    result = _run_async(global_signal_generator.generate_and_broadcast(universe, mock=mock))
    return {"status": "success" if result else "failed", "universe": universe, "trade_date": trade_date}


@celery_app.task(
    bind=True,
    name="engine.tasks.run_pipeline_run",
    max_retries=1,
    default_retry_delay=30,
)
def run_pipeline_run(self, run_id: str) -> dict[str, Any]:
    """Celery 任务：执行 pipeline run。"""
    logger.info("Pipeline Celery task started: run_id=%s task_id=%s", run_id, self.request.id)
    service = PipelineService()
    result = _run_async(service.execute_run(run_id))
    return result.model_dump()


@celery_app.task(
    name="engine.tasks.auto_inference_if_needed",
    max_retries=1,
    default_retry_delay=60,
)
def auto_inference_if_needed() -> dict[str, Any]:
    """
    Celery Beat 定时任务：每日 03:00-08:00 自动扫描并执行所有活跃策略的推理。

    逻辑：
    1. 获取所有处于 'running' 状态且绑定了策略的投资组合。
    2. 针对每个策略：
        a. 检查是否已完成推理。
        b. 尝试获取策略级分布式锁。
        c. 执行推理脚本。
    """
    from zoneinfo import ZoneInfo
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker
    import exchange_calendars as xcals
    from backend.services.engine.inference.router_service import InferenceRouterService

    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    cal = xcals.get_calendar("XSHG")

    # 确定特征日期 (data_trade_date) 和预测日期 (prediction_trade_date)
    # 如果开盘前运行，T 是上一个交易日，T+1 是今日
    # 如果开盘后运行，T 是今日，T+1 是下一个交易日
    if now_local.time() < datetime.strptime("09:30", "%H:%M").time():
        data_trade_date_obj = cal.previous_session(now_local.date()).date()
    else:
        data_trade_date_obj = now_local.date()

    data_trade_date = data_trade_date_obj.isoformat()
    prediction_trade_date = cal.next_session(data_trade_date_obj).date().isoformat()

    # 0. 排除非交易日
    try:
        if not cal.is_session(data_trade_date):
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

        # 总是包含一个系统级别的虚拟任务（全局默认模型）
        tasks = [{"tenant_id": "default", "user_id": "system", "strategy_id": "global", "model_id": None}]
        for p in active_portfolios:
            tasks.append({
                "tenant_id": p.tenant_id,
                "user_id": str(p.user_id),
                "strategy_id": str(p.strategy_id or "default"),
                "model_id": None,
            })

        # 添加用户自动推理设置任务
        for s in auto_inference_settings:
            tasks.append({
                "tenant_id": s.tenant_id,
                "user_id": str(s.user_id),
                "strategy_id": None,  # 用户级别推理，不绑定特定策略
                "model_id": str(s.model_id),
            })

        # 去重（同一 tenant_id + user_id 可能同时出现在 portfolios 和 settings 中）
        seen = set()
        unique_tasks = []
        for t in tasks:
            key = (t["tenant_id"], t["user_id"], t.get("strategy_id"), t.get("model_id"))
            if key not in seen:
                seen.add(key)
                unique_tasks.append(t)
        tasks = unique_tasks

        logger.info("[AutoInference] 发现需检查的任务总数: %d (活跃策略=%d, 自动推理设置=%d)",
                     len(tasks), len(active_portfolios), len(auto_inference_settings))

        results = []
        redis = None
        try:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client
            redis = get_redis_sentinel_client()
        except: pass

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
                {"d": prediction_trade_date, "tid": tid, "uid": uid}
            ).first()

            if exists:
                continue

            # 尝试获取锁
            lock_scope = f"{tid}:{uid}:{sid or mid or 'default'}"
            if not _try_acquire_strategy_lock(lock_scope, prediction_trade_date, "celery_auto"):
                logger.info("[AutoInference] 任务锁冲突，跳过: tid=%s uid=%s", tid, uid)
                continue

            try:
                logger.info("[AutoInference] 正在执行任务: tenant=%s user=%s strategy=%s model=%s",
                            tid, uid, sid, mid)
                exec_res = router_service.run_daily_inference_script(
                    date=data_trade_date,
                    tenant_id=tid,
                    user_id=uid,
                    strategy_id=None if sid == "global" else sid,
                    model_id=mid,
                    redis_client=redis
                )
                results.append({
                    "tenant_id": tid,
                    "user_id": uid,
                    "success": exec_res.success,
                    "run_id": exec_res.run_id
                })
            finally:
                # 释放锁
                try:
                    lock_key = f"{_INFERENCE_LOCK_KEY_PREFIX}:{lock_scope}:{prediction_trade_date}"
                    redis.delete(lock_key)
                except: pass

        return {
            "status": "completed",
            "date": prediction_trade_date,
            "processed_count": len(results),
            "details": results
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
def run_strategy_backtest_loop(self, task_id: str, request_payload: dict[str, Any]) -> dict[str, Any]:
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

        def _progress_callback(iteration: int, stage: Any, progress: float, best_score: float) -> None:
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
            strategy_type=(StrategyType(request.strategy_type) if request.strategy_type else None),
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
            symbols=request.target_assets or ["000001.SZ"],
            start_date=start_date,
            end_date=end_date,
            timeframe=request.timeframe,
        )

        loop_manager = StrategyBacktestLoop(loop_config)
        result = await loop_manager.run_loop(strategy_request, market_data, progress_callback=_progress_callback)

        best_strategy = {}
        performance_metrics = {}
        best_score = 0.0
        if result.best_iteration:
            best_score = float(getattr(result.best_iteration, "performance_score", 0.0) or 0.0)
            if getattr(result.best_iteration, "strategy_response", None):
                best_strategy = result.best_iteration.strategy_response.to_dict()
            if getattr(result.best_iteration, "backtest_result", None):
                performance_metrics = result.best_iteration.backtest_result.performance_metrics or {}

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
    name="engine.tasks.sync_market_data_daily_task",
    max_retries=0,
)
def sync_market_data_daily_task(target_date: str | None = None, max_symbols: int = 0, apply: bool = True) -> dict[str, Any]:
    """
    Celery 任务：从 Baostock 同步基础行情到 market_data_daily。
    解决 Admin UI 手动触发时的 Gateway Timeout 问题。
    """
    import subprocess
    import sys
    import os
    from pathlib import Path

    project_root = Path(os.getcwd())
    script_path = project_root / "scripts" / "data" / "ingestion" / "sync_market_data_daily_from_baostock.py"

    if not script_path.exists():
        logger.error("[SyncTask] 脚本不存在: %s", script_path)
        return {"success": False, "error": "script_not_found"}

    # 寻找 Python 解释器
    python_candidates = [
        project_root / ".venv" / "bin" / "python",
        Path("/app/.venv/bin/python"),
        Path(sys.executable),
        Path("/usr/bin/python3"),
    ]
    python_exec = next((str(p) for p in python_candidates if p.exists()), sys.executable)

    cmd = [python_exec, str(script_path)]
    if target_date:
        cmd.extend(["--target-date", target_date])
    if max_symbols > 0:
        cmd.extend(["--max-symbols", str(max_symbols)])
    if apply:
        cmd.append("--apply")

    logger.info("[SyncTask] 启动同步脚本: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=3600, # 1小时
            env=os.environ.copy(),
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # 尝试解析输出中的 JSON
        import json
        parsed = {}
        for line in reversed(stdout.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                try:
                    parsed = json.loads(line)
                    break
                except: continue

        if proc.returncode != 0:
            logger.error("[SyncTask] 脚本执行失败 exit=%d, stderr=%s", proc.returncode, stderr[:500])
            return {
                "success": False,
                "exit_code": proc.returncode,
                "error": parsed.get("error") or "execution_failed",
                "stdout": stdout[-1000:],
                "stderr": stderr[-1000:]
            }

        logger.info("[SyncTask] 同步成功")
        return {
            "success": True,
            "exit_code": 0,
            "result": parsed,
            "stdout": stdout[-1000:]
        }
    except Exception as e:
        logger.exception("[SyncTask] 任务执行异常")
        return {"success": False, "error": str(e)}


@celery_app.task(
    name="engine.tasks.get_data_status_task",
    max_retries=1,
)
def get_data_status_task() -> dict[str, Any]:
    """
    Celery 任务：扫描 Qlib 数据目录和数据库状态，并缓存结果。
    改善 Admin UI 响应速度。
    """
    import os
    import json
    import struct
    from pathlib import Path
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from sqlalchemy import text
    import exchange_calendars as xcals

    # 复用 model_management.py 的逻辑，但增加 BJ 标持支持
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))

    # 获取目标日期规则
    cal_xshg = xcals.get_calendar("XSHG")
    if now_local.time() < datetime.strptime("09:30", "%H:%M").time():
        trade_date_obj = cal_xshg.previous_session(now_local.date()).date()
    else:
        trade_date_obj = now_local.date()
    trade_date = trade_date_obj.isoformat()

    qlib_data_dir = Path(os.getcwd()) / "db" / "qlib_data"
    calendars_path = qlib_data_dir / "calendars" / "day.txt"
    instruments_all_path = qlib_data_dir / "instruments" / "all.txt"
    features_root = qlib_data_dir / "features"

    qlib_info: dict[str, Any] = {
        "qlib_dir": str(qlib_data_dir),
        "exists": qlib_data_dir.exists() and qlib_data_dir.is_dir(),
        "calendar_total_days": 0,
        "calendar_start_date": None,
        "calendar_last_date": None,
        "instruments": {"total": 0, "sh": 0, "sz": 0, "bj": 0, "other": 0},
        "feature_dirs_total": 0,
        "feature_dirs_sh_sz_bj": 0,
        "latest_date_coverage": {
            "target_date": None,
            "at_target_count": 0,
            "older_count": 0,
            "invalid_count": 0,
        },
        "topn_samples": {
            "sample_size": 20,
            "older_samples": [],
            "invalid_samples": [],
        },
    }

    calendar: list[str] = []
    if calendars_path.exists():
        try:
            calendar = [x.strip() for x in calendars_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            if calendar:
                qlib_info["calendar_total_days"] = len(calendar)
                qlib_info["calendar_start_date"] = calendar[0]
                qlib_info["calendar_last_date"] = calendar[-1]
                qlib_info["latest_date_coverage"]["target_date"] = calendar[-1]
        except Exception as e:
            qlib_info["calendar_error"] = str(e)

    if instruments_all_path.exists():
        try:
            for line in instruments_all_path.read_text(encoding="utf-8").splitlines():
                if not line.strip(): continue
                code = line.split()[0].strip().upper()
                qlib_info["instruments"]["total"] += 1
                if code.startswith("SH"): qlib_info["instruments"]["sh"] += 1
                elif code.startswith("SZ"): qlib_info["instruments"]["sz"] += 1
                elif code.startswith("BJ"): qlib_info["instruments"]["bj"] += 1
                else: qlib_info["instruments"]["other"] += 1
        except Exception as e:
            qlib_info["instruments_error"] = str(e)

    if features_root.exists() and features_root.is_dir() and calendar:
        feature_dirs = [p for p in features_root.iterdir() if p.is_dir()]
        qlib_info["feature_dirs_total"] = len(feature_dirs)

        cal_len = len(calendar)
        target_date = calendar[-1]
        at_target_count = 0
        older_count = 0
        invalid_count = 0
        sample_size = 20
        older_samples = []
        invalid_samples = []

        for inst_dir in feature_dirs:
            inst = inst_dir.name.upper()
            # 兼容 SH, SZ, BJ
            if not inst.startswith(("SH", "SZ", "BJ")):
                continue

            qlib_info["feature_dirs_sh_sz_bj"] += 1
            close_bin = inst_dir / "close.day.bin"

            if not close_bin.exists():
                invalid_count += 1
                if len(invalid_samples) < sample_size:
                    invalid_samples.append({"symbol": inst, "reason": "missing_close_bin", "file": str(close_bin)})
                continue

            try:
                size = close_bin.stat().st_size
                if size < 8:
                    invalid_count += 1
                    if len(invalid_samples) < sample_size:
                        invalid_samples.append({"symbol": inst, "reason": "bin_too_small", "file": str(close_bin)})
                    continue

                with close_bin.open("rb") as f:
                    head = f.read(4)
                if len(head) < 4:
                    start_idx = None
                else:
                    start_idx = int(struct.unpack("<f", head)[0])

                if start_idx is None:
                    invalid_count += 1
                    continue

                nrows = (size - 4) // 4
                end_idx = start_idx + nrows - 1
                if not (0 <= end_idx < cal_len):
                    invalid_count += 1
                    continue

                last_date = calendar[end_idx]
                if last_date == target_date:
                    at_target_count += 1
                else:
                    older_count += 1
                    if len(older_samples) < sample_size:
                        older_samples.append({"symbol": inst, "last_date": last_date, "lag_days": max(0, cal_len - 1 - end_idx)})
            except:
                invalid_count += 1

        qlib_info["latest_date_coverage"] = {
            "target_date": target_date,
            "at_target_count": at_target_count,
            "older_count": older_count,
            "invalid_count": invalid_count,
        }
        qlib_info["topn_samples"] = {
            "sample_size": sample_size,
            "older_samples": sorted(older_samples, key=lambda x: x["last_date"])[:sample_size],
            "invalid_samples": invalid_samples[:sample_size],
        }

    db_info = {"trade_date": trade_date, "latest_trade_date": None, "latest_updated_at": None, "today_rows": 0, "feature_column_count": 0}

    # feature_snapshots 检测（替代 market_data_daily）
    feature_snapshots_info: dict[str, Any] = {
        "exists": False,
        "snapshot_dir": str(Path(os.getcwd()) / "db" / "feature_snapshots"),
        "file_count": 0,
        "scanned_files": 0,
        "failed_files": 0,
        "total_rows": 0,
        "min_date": None,
        "max_date": None,
        "latest_date_coverage": {
            "target_date": trade_date,
            "at_target_count": 0,
            "older_count": 0,
            "invalid_count": 0,
        },
        "topn_samples": {
            "sample_size": 20,
            "older_samples": [],
            "invalid_samples": [],
        },
        "suggested_periods": None,
    }

    try:
        from backend.services.api.routers.admin.model_management_utils import _scan_feature_snapshots_status
        feature_snapshots_info = _scan_feature_snapshots_status(target_date=trade_date, topn=20)
    except Exception as e:
        feature_snapshots_info["error"] = str(e)

    result = {
        "checked_at": now_local.isoformat(),
        "trade_date": trade_date,
        "qlib_data": qlib_info,
        "feature_snapshots": feature_snapshots_info,
    }

    # 存入 Redis
    try:
        redis = get_redis_sentinel_client()
        redis.set("qm:admin:data_status", json.dumps(result), ex=300) # 5分钟缓存
    except Exception as e:
        logger.warning("Failed to cache data status to Redis: %s", e)

    return result
