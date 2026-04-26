"""
Celery异步任务定义

提供回测任务的异步执行能力
"""

import asyncio
import io
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from celery import Task

import redis
from backend.services.engine.qlib_app import (
    get_genetic_optimization_service,
    get_optimization_service,
    get_qlib_service,
)
from backend.services.engine.qlib_app.celery_config import celery_app
from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibGeneticOptimizationRequest,
    QlibOptimizationRequest,
)
from backend.services.engine.qlib_app.services.optimization_persistence import (
    OptimizationPersistence,
)
from backend.services.engine.qlib_app.services.optimization_service import (
    OptimizationCancelledError,
)
from backend.shared.auth import get_internal_call_secret
from backend.services.engine.qlib_app.utils.structured_logger import (
    StructuredTaskLogger,
)

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "CeleryTasks")

# 配置回调地址
API_BASE_URL = os.getenv("BACKTEST_PROGRESS_URL", "http://quantmind-engine:8001")
# Redis 配置
REDIS_HOST = os.getenv("REDIS_HOST", "quantmind-redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_DB = int(os.getenv("REDIS_DB", os.getenv("REDIS_DB_DEFAULT", 0)))
OPTIMIZATION_LOCK_KEY = os.getenv(
    "QLIB_OPTIMIZATION_LOCK_KEY", "qlib:optimization:global_lock"
)
OPTIMIZATION_LOCK_TTL = int(os.getenv("QLIB_OPTIMIZATION_LOCK_TTL", "7200"))
OPTIMIZATION_LOCK_WAIT_SECONDS = int(
    os.getenv("QLIB_OPTIMIZATION_LOCK_WAIT_SECONDS", "3")
)
optimization_persistence = OptimizationPersistence()


def _get_redis_client() -> redis.Redis | None:
    try:
        return redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
        )
    except Exception as exc:
        task_logger.error(
            "redis_connect_failed", "Failed to connect to Redis", error=str(exc)
        )
        return None


def _try_acquire_optimization_lock(client: redis.Redis | None, owner: str) -> bool:
    if client is None:
        return True
    return bool(
        client.set(OPTIMIZATION_LOCK_KEY, owner, nx=True, ex=OPTIMIZATION_LOCK_TTL)
    )


def _release_optimization_lock(client: redis.Redis | None, owner: str) -> None:
    if client is None:
        return
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    try:
        client.eval(script, 1, OPTIMIZATION_LOCK_KEY, owner)
    except Exception as exc:
        task_logger.warning(
            "optimization_lock_release_failed",
            "Failed to release optimization lock",
            error=str(exc),
        )


def _send_progress_update(data: dict[str, Any]):
    """通过 HTTP 发送进度更新到 FastAPI"""
    try:
        url = f"{API_BASE_URL}/api/v1/qlib/progress"
        headers = {"X-Internal-Call": get_internal_call_secret()}
        requests.post(url, json=data, headers=headers, timeout=1)
    except Exception as e:
        task_logger.warning(
            "progress_update_failed", "Failed to send progress update", error=str(e)
        )


async def _persist_optimization_progress(
    optimization_id: str | None,
    payload: dict[str, Any],
    *,
    status: str | None = None,
    error_message: str | None = None,
) -> None:
    if not optimization_id:
        return
    await optimization_persistence.update_run(
        optimization_id,
        status=status,
        completed_count=payload.get("completed_count"),
        failed_count=payload.get("failed_count"),
        current_params=payload.get("current_params"),
        best_params=payload.get("best_params"),
        best_metric_value=payload.get("best_metric_value"),
        result_summary=payload.get("result_summary"),
        all_results=payload.get("all_results"),
        error_message=error_message,
    )


class TaskRedisLogHandler(logging.Handler):
    """按任务写入 Redis 的日志处理器。"""

    def __init__(self, log_key: str, context_label: str = ""):
        super().__init__(level=logging.INFO)
        self.key = f"qlib:logs:{log_key}"
        self.context_label = context_label.strip()
        try:
            self.redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                decode_responses=True,
            )
        except Exception as e:
            task_logger.error(
                "redis_logging_connect_failed",
                "Failed to connect to Redis for logging",
                error=str(e),
            )
            self.redis_client = None

    def _push_text(self, text: str) -> None:
        if not self.redis_client:
            return
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            context_prefix = f"[{self.context_label}] " if self.context_label else ""
            lines = text.splitlines()
            if "\r" in text:
                self.redis_client.rpush(
                    self.key, f"[{timestamp}] {context_prefix}{text}"
                )
            else:
                for line in lines:
                    if line.strip():
                        self.redis_client.rpush(
                            self.key, f"[{timestamp}] {context_prefix}{line}"
                        )
            self.redis_client.expire(self.key, 3600)
        except Exception:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record)
        self._push_text(message)

    def emit_text(self, text: str, level: int = logging.INFO) -> None:
        if not text:
            return
        record = logging.LogRecord(
            name="qlib.task.stdout",
            level=level,
            pathname="",
            lineno=0,
            msg=text,
            args=(),
            exc_info=None,
        )
        self.emit(record)


class TaskStreamAdapter(io.TextIOBase):
    """将 print/直接写入的文本转成任务日志。"""

    def __init__(
        self,
        handler: TaskRedisLogHandler,
        original_stream=None,
        level: int = logging.INFO,
    ):
        self.handler = handler
        self.original_stream = original_stream
        self.level = level
        self.buffer = ""

    def write(self, s):
        if self.original_stream:
            self.original_stream.write(s)
        if not s:
            return 0
        self.buffer += s
        if "\n" in self.buffer or "\r" in self.buffer or len(self.buffer) > 50:
            self.flush()
        return len(s)

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass
        if self.buffer:
            self.handler.emit_text(self.buffer, level=self.level)
            self.buffer = ""


class TaskLogCapture:
    """任务级日志捕获：挂载 Redis handler，并把 stdout/stderr 归到当前任务。"""

    def __init__(self, log_key: str, context_label: str = ""):
        self.log_key = log_key
        self.context_label = context_label
        self.handler = TaskRedisLogHandler(log_key, context_label=context_label)
        self.stdout_capture = None
        self.stderr_capture = None
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.root_logger = logging.getLogger()
        self._previous_level = self.root_logger.level
        self._attached = False

        formatter = logging.Formatter("%(levelname)s - %(name)s - %(message)s")
        self.handler.setFormatter(formatter)

    def __enter__(self):
        if not self._attached:
            self.root_logger.addHandler(self.handler)
            self._attached = True
        self.stdout_capture = TaskStreamAdapter(
            self.handler, self.original_stdout, level=logging.INFO
        )
        self.stderr_capture = TaskStreamAdapter(
            self.handler, self.original_stderr, level=logging.ERROR
        )
        sys.stdout = self.stdout_capture
        sys.stderr = self.stderr_capture
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.stdout_capture:
            self.stdout_capture.flush()
        if self.stderr_capture:
            self.stderr_capture.flush()

        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        if self._attached:
            try:
                self.root_logger.removeHandler(self.handler)
            except Exception:
                pass
            self._attached = False
        try:
            self.handler.close()
        except Exception:
            pass


class TaskStructuredLogger:
    """给任务日志统一添加固定前缀，便于 worker 控制台和 Redis 日志对齐。"""

    def __init__(self, base_logger: logging.Logger, context: str):
        self._base_logger = base_logger
        self._context = context.strip()

    def _format(self, message: str) -> str:
        return f"[{self._context}] {message}" if self._context else message

    def debug(self, message: str, *args, **kwargs) -> None:
        self._base_logger.debug(self._format(message), *args, **kwargs)

    def info(self, message: str, *args, **kwargs) -> None:
        self._base_logger.info(self._format(message), *args, **kwargs)

    def warning(self, message: str, *args, **kwargs) -> None:
        self._base_logger.warning(self._format(message), *args, **kwargs)

    def error(self, message: str, *args, **kwargs) -> None:
        self._base_logger.error(self._format(message), *args, **kwargs)

    def exception(self, message: str, *args, **kwargs) -> None:
        self._base_logger.exception(self._format(message), *args, **kwargs)


class CallbackTask(Task):
    """带回调的任务基类"""

    def on_success(self, retval, task_id, args, kwargs):
        task_logger.info(
            "celery_task_success", "Task finished successfully", task_id=task_id
        )

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # Celery 传入的 einfo 不是 logging 期望的 exc_info 三元组时，会触发格式化异常。
        # 这里统一标准化，避免污染任务状态查询链路。
        exc_info = None
        if isinstance(einfo, tuple) and len(einfo) == 3 and einfo[0] is not None:
            exc_info = einfo
        elif isinstance(exc, BaseException):
            exc_info = (type(exc), exc, exc.__traceback__)
        task_logger.error(
            "celery_task_failed",
            "Task failed",
            task_id=task_id,
            error=str(exc),
            exc_info=exc_info,
        )


def _to_jsonable(payload: Any) -> Any:
    if payload is None or isinstance(payload, (str, int, float, bool)):
        return payload
    if isinstance(payload, dict):
        return {str(k): _to_jsonable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple, set)):
        return [_to_jsonable(v) for v in payload]
    if hasattr(payload, "model_dump"):
        try:
            return _to_jsonable(payload.model_dump(mode="json"))
        except Exception:
            pass
    if hasattr(payload, "json"):
        try:
            return _to_jsonable(json.loads(payload.json()))
        except Exception:
            pass
    if hasattr(payload, "dict"):
        try:
            return _to_jsonable(payload.dict())
        except Exception:
            pass
    if isinstance(payload, BaseException):
        return {"error": str(payload), "exception_type": type(payload).__name__}
    return str(payload)


_async_loop: asyncio.AbstractEventLoop | None = None


def _run_async(coro):
    global _async_loop
    if _async_loop is None or _async_loop.is_closed():
        _async_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_async_loop)
    return _async_loop.run_until_complete(coro)


def _resolve_provider_path(path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return path
    # tasks.py 位于 backend/services/engine/qlib_app，下探 4 级回到项目根目录
    root_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    return os.path.normpath(os.path.join(root_dir, path))


def _get_qlib_service_instance() -> Any:
    return get_qlib_service()


@celery_app.task(
    bind=True,
    base=CallbackTask,
    name="qlib_app.tasks.run_backtest_async",
    max_retries=2,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
)
def run_backtest_async(self, request_dict: dict[str, Any]) -> dict[str, Any]:
    """异步执行回测任务"""
    task_id = self.request.id
    request = QlibBacktestRequest(**request_dict)
    backtest_id = request_dict.get("backtest_id")

    tenant_id = request.tenant_id
    task_log = TaskStructuredLogger(
        logger,
        f"qlib-backtest task_id={task_id} backtest_id={backtest_id} tenant_id={tenant_id} user_id={request.user_id}",
    )

    task_log.info("开始异步回测")

    # 进度：0%
    self.update_state(
        state="PROGRESS", meta={"progress": 0.0, "status": "initializing"}
    )
    _send_progress_update(
        {
            "backtest_id": backtest_id,
            "progress": 0.0,
            "status": "running",
            "message": "Initializing...",
        }
    )

    try:
        service = _get_qlib_service_instance()
        service.initialize()

        # 进度：10%
        self.update_state(state="PROGRESS", meta={"progress": 0.1, "status": "running"})
        _send_progress_update(
            {
                "backtest_id": backtest_id,
                "progress": 0.1,
                "status": "running",
                "message": "Qlib initialized",
            }
        )

        log_key = f"{tenant_id}:{backtest_id}"
        log_context = f"backtest_id={backtest_id} task_id={task_id} tenant_id={tenant_id} user_id={request.user_id}"
        with TaskLogCapture(log_key, context_label=log_context):
            result = _run_async(service.run_backtest(request))

        result_dict = _to_jsonable(result)

        # 进度：100%
        self.update_state(state="SUCCESS", meta={"result": result_dict})
        _send_progress_update(
            {
                "backtest_id": backtest_id,
                "progress": 1.0,
                "status": "completed",
                "result": result_dict,
            }
        )

        # 核心增强：如果是自定义策略回测成功，标记为“已验证可用”
        if request.strategy_id and request.strategy_id.isdigit():
            from backend.shared.strategy_storage import get_strategy_storage_service

            try:
                # 运行到这里说明回测无异常，策略逻辑闭环，可以标记为实盘可用
                storage_svc = get_strategy_storage_service()
                _run_async(
                    storage_svc.mark_as_verified(request.strategy_id, request.user_id)
                )
                task_log.info(
                    f"策略 {request.strategy_id} 回测成功，已开启实盘准入标识"
                )
            except Exception as e:
                task_log.warning(f"无法标记策略验证状态: {e}")

        # Invalid cache when a backtest is finished saving
        from backend.services.engine.qlib_app.cache_manager import get_cache_manager

        try:
            get_cache_manager().invalidate_user_history(
                f"{tenant_id}:{request.user_id}"
            )
        except Exception as e:
            task_log.warning(f"Failed to invalidate cache: {e}")

        return result_dict

    except Exception as exc:
        import traceback

        full_error = traceback.format_exc()
        task_log.exception(f"Backtest failed: {exc}")
        # 避免以 FAILURE + 普通 dict 写入结果后端，触发 Celery 异常反序列化错误。
        self.update_state(
            state="PROGRESS", meta={"status": "failed", "error": str(exc)}
        )
        _send_progress_update(
            {
                "backtest_id": backtest_id,
                "status": "failed",
                "error_message": str(exc),
                "full_error": full_error,
            }
        )
        raise


@celery_app.task(name="qlib_app.tasks.log_frontend_error")
def log_frontend_error(error_data: dict[str, Any]):
    """记录前端报错到 worker 日志"""
    backtest_id = error_data.get("backtest_id", "N/A")
    user_id = error_data.get("user_id", "N/A")
    message = error_data.get("message", "Unknown error")
    stack = error_data.get("stack", "")

    task_logger.error(
        "frontend_error_forwarded",
        "Forwarded frontend error",
        backtest_id=backtest_id,
        user_id=user_id,
        message=message,
        stack=stack,
    )
    return {"status": "logged"}


@celery_app.task(name="qlib_app.tasks.get_backtest_status")
def get_backtest_status(task_id: str) -> dict[str, Any]:
    from celery.result import AsyncResult

    result = AsyncResult(task_id, app=celery_app)
    try:
        state = result.state
    except Exception as exc:
        state = "UNKNOWN"
        info = {"error": f"读取任务状态失败: {exc}"}
        return {
            "task_id": task_id,
            "state": state,
            "info": info,
            "ready": False,
            "successful": False,
            "failed": False,
        }

    try:
        info = result.info if result.info else {}
    except Exception as exc:
        info = {"error": f"读取任务详情失败: {exc}"}

    try:
        ready = result.ready()
    except Exception:
        ready = False
    try:
        successful = result.successful() if ready else False
    except Exception:
        successful = False
    try:
        failed = result.failed() if ready else False
    except Exception:
        failed = False

    return {
        "task_id": task_id,
        "state": state,
        "info": info,
        "ready": ready,
        "successful": successful,
        "failed": failed,
    }


@celery_app.task(name="qlib_app.tasks.cleanup_old_results")
def cleanup_old_results(days: int = 7):
    return {"message": "cleaned"}


@celery_app.task(
    bind=True,
    base=CallbackTask,
    name="qlib_app.tasks.run_optimization_async",
)
def run_optimization_async(self, request_dict: dict[str, Any]) -> dict[str, Any]:
    """异步执行网格参数优化任务"""
    task_id = self.request.id
    optimization_id = request_dict.get("optimization_id")

    request = QlibOptimizationRequest(**request_dict)
    tenant_id = request.base_request.tenant_id or "default"
    user_id = request.base_request.user_id
    task_log = TaskStructuredLogger(
        logger,
        f"qlib-optimization task_id={task_id} optimization_id={optimization_id} tenant_id={tenant_id} user_id={user_id}",
    )

    task_log.info("开始网格优化")

    self.update_state(
        state="PROGRESS",
        meta={
            "optimization_id": optimization_id,
            "progress": 0.0,
            "status": "initializing",
            "message": "正在初始化网格优化任务...",
            "total_tasks": 0,
            "completed_count": 0,
            "failed_count": 0,
        },
    )
    _send_progress_update(
        {
            "backtest_id": optimization_id,
            "optimization_id": optimization_id,
            "progress": 0.0,
            "status": "initializing",
            "message": "Starting grid optimization...",
        }
    )

    try:
        service = get_optimization_service()
        total_tasks = request.total_combinations()

        async def cancellation_checker() -> bool:
            if not optimization_id:
                return False
            status = await optimization_persistence.get_status(optimization_id)
            return status == "cancelled"

        redis_client = _get_redis_client()
        lock_owner = optimization_id or task_id
        lock_acquired = False

        while not lock_acquired:
            if _run_async(cancellation_checker()):
                raise OptimizationCancelledError("用户手动停止优化任务")
            lock_acquired = _try_acquire_optimization_lock(redis_client, lock_owner)
            if lock_acquired:
                break

            queued_tasks = _run_async(
                optimization_persistence.count_by_statuses(
                    ["running", "queued", "pending"]
                )
            )
            queued_ahead = max(0, queued_tasks - 1)
            queued_message = (
                f"当前已有 {queued_ahead} 个参数优化任务在前方等待，正在排队"
                if queued_ahead > 0
                else "当前已有参数优化任务执行中，正在排队等待"
            )
            queued_meta = {
                "optimization_id": optimization_id,
                "progress": 0.0,
                "status": "queued",
                "message": queued_message,
                "total_tasks": total_tasks,
                "completed_count": 0,
                "failed_count": 0,
                "result_summary": {
                    "queue_ahead": queued_ahead,
                },
            }
            self.update_state(state="PROGRESS", meta=queued_meta)
            _run_async(
                _persist_optimization_progress(
                    optimization_id,
                    queued_meta,
                    status="queued",
                )
            )
            _send_progress_update(
                {
                    "backtest_id": optimization_id,
                    **queued_meta,
                }
            )
            time.sleep(max(1, OPTIMIZATION_LOCK_WAIT_SECONDS))

        running_meta = {
            "optimization_id": optimization_id,
            "progress": 0.1,
            "status": "running",
            "message": "网格优化任务执行中...",
            "total_tasks": total_tasks,
            "completed_count": 0,
            "failed_count": 0,
        }
        self.update_state(state="PROGRESS", meta=running_meta)
        _run_async(
            _persist_optimization_progress(
                optimization_id,
                running_meta,
                status="running",
            )
        )

        log_key = f"{tenant_id}:{optimization_id}"
        log_context = f"optimization_id={optimization_id} task_id={task_id} tenant_id={tenant_id} user_id={user_id}"

        async def progress_callback(meta: dict[str, Any]) -> None:
            state_payload = {
                "optimization_id": optimization_id,
                "progress": meta.get("progress", 0.0),
                "status": meta.get("status", "running"),
                "message": meta.get("message"),
                "total_tasks": meta.get("total_tasks", total_tasks),
                "completed_count": meta.get("completed_count", 0),
                "failed_count": meta.get("failed_count", 0),
                "current_params": meta.get("current_params"),
                "best_params": meta.get("best_params"),
                "best_metric_value": meta.get("best_metric_value"),
                "result_summary": meta.get("result_summary", {}),
            }
            self.update_state(state="PROGRESS", meta=state_payload)
            await _persist_optimization_progress(optimization_id, meta)
            _send_progress_update(
                {
                    "backtest_id": optimization_id,
                    **state_payload,
                }
            )

        with TaskLogCapture(log_key, context_label=log_context):
            result = _run_async(
                service.run_optimization(
                    request,
                    progress_callback=progress_callback,
                    optimization_id=optimization_id,
                    cancellation_checker=cancellation_checker,
                )
            )
        result_dict = _to_jsonable(result)

        # 保持与提交接口返回的 optimization_id 一致
        if optimization_id:
            result_dict["optimization_id"] = optimization_id

        self.update_state(
            state="SUCCESS",
            meta={
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "completed",
                "message": "网格优化已完成",
                "total_tasks": total_tasks,
                "completed_count": len(result_dict.get("all_results") or []),
                "failed_count": max(
                    0, total_tasks - len(result_dict.get("all_results") or [])
                ),
                "best_params": result_dict.get("best_params") or {},
                "result": result_dict,
            },
        )
        _run_async(
            optimization_persistence.update_run(
                optimization_id,
                status="completed",
                completed_count=len(result_dict.get("all_results") or []),
                failed_count=max(
                    0, total_tasks - len(result_dict.get("all_results") or [])
                ),
                best_params=result_dict.get("best_params") or {},
                best_metric_value=(
                    (
                        result_dict.get("all_results", [])[0]
                        .get("metrics", {})
                        .get(result_dict.get("target_metric"))
                    )
                    if result_dict.get("all_results")
                    else None
                ),
                result_summary={
                    "best_params": result_dict.get("best_params") or {},
                    "target_metric": result_dict.get("target_metric"),
                    "execution_time": result_dict.get("execution_time"),
                },
                all_results=result_dict.get("all_results") or [],
            )
        )
        _send_progress_update(
            {
                "backtest_id": optimization_id,
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "completed",
                "message": "Grid optimization completed",
                "total_tasks": total_tasks,
                "completed_count": len(result_dict.get("all_results") or []),
                "failed_count": max(
                    0, total_tasks - len(result_dict.get("all_results") or [])
                ),
                "best_params": result_dict.get("best_params") or {},
                "result": result_dict,
            }
        )

        return result_dict
    except OptimizationCancelledError as exc:
        task_log.info(f"网格优化已取消: {exc}")
        cancelled_result = {
            "optimization_id": optimization_id,
            "status": "cancelled",
            "message": str(exc),
            "all_results": [],
            "best_params": {},
            "target_metric": request.optimization_target,
            "execution_time": 0,
        }
        self.update_state(
            state="CANCELLED",
            meta={
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "cancelled",
                "message": str(exc),
                "result": cancelled_result,
            },
        )
        _run_async(
            optimization_persistence.update_run(
                optimization_id,
                status="cancelled",
                error_message=str(exc),
            )
        )
        _send_progress_update(
            {
                "backtest_id": optimization_id,
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "cancelled",
                "message": str(exc),
            }
        )
        return cancelled_result
    except Exception as exc:
        task_log.exception(f"网格优化失败: {exc}")
        self.update_state(
            state="PROGRESS",
            meta={
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "failed",
                "message": str(exc),
                "error": str(exc),
            },
        )
        _run_async(
            optimization_persistence.update_run(
                optimization_id,
                status="failed",
                error_message=str(exc),
            )
        )
        _send_progress_update(
            {
                "backtest_id": optimization_id,
                "optimization_id": optimization_id,
                "status": "failed",
                "message": str(exc),
            }
        )
        raise
    finally:
        _release_optimization_lock(_get_redis_client(), optimization_id or task_id)


@celery_app.task(
    bind=True,
    base=CallbackTask,
    name="qlib_app.tasks.run_genetic_optimization_async",
    max_retries=1,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
)
def run_genetic_optimization_async(
    self, request_dict: dict[str, Any]
) -> dict[str, Any]:
    """异步执行遗传算法参数优化任务"""
    task_id = self.request.id
    optimization_id = request_dict.get("optimization_id")

    request = QlibGeneticOptimizationRequest(**request_dict)
    tenant_id = request.base_request.tenant_id or "default"
    user_id = request.base_request.user_id
    task_log = TaskStructuredLogger(
        logger,
        f"qlib-genetic-optimization task_id={task_id} optimization_id={optimization_id} tenant_id={tenant_id} user_id={user_id}",
    )

    # 初始状态更新
    task_log.info("开始遗传优化")
    self.update_state(
        state="PROGRESS",
        meta={
            "optimization_id": optimization_id,
            "progress": 0.0,
            "status": "initializing",
            "message": "正在初始化遗传优化任务...",
        },
    )
    _send_progress_update(
        {
            "backtest_id": optimization_id,  # 复用字段方便前端
            "optimization_id": optimization_id,
            "progress": 0.0,
            "status": "initializing",
            "message": "Starting GA...",
        }
    )

    try:
        service = get_genetic_optimization_service()

        async def cancellation_checker() -> bool:
            if not optimization_id:
                return False
            status = await optimization_persistence.get_status(optimization_id)
            return status == "cancelled"

        def progress_callback(meta: dict[str, Any]) -> None:
            # 更新 Celery 状态
            self.update_state(state="PROGRESS", meta=meta)

            # 发送 HTTP 通知
            payload = meta.copy()
            payload["backtest_id"] = optimization_id  # 确保前端能收到
            _send_progress_update(payload)

        # 使用任务级日志上下文捕获日志
        log_key = f"{tenant_id}:{optimization_id}"
        log_context = (
            f"optimization_id={optimization_id} task_id={task_id} tenant_id={tenant_id}"
        )
        with TaskLogCapture(log_key, context_label=log_context):
            result = _run_async(
                service.run_optimization(
                    request,
                    progress_callback=progress_callback,
                    cancellation_checker=cancellation_checker,
                )
            )

        result_dict = _to_jsonable(result)

        # 持久化遗传算法结果到数据库
        total_individuals = request.population_size * request.generations
        _run_async(
            optimization_persistence.update_run(
                optimization_id,
                status="completed",
                completed_count=total_individuals,
                failed_count=0,
                best_params=result_dict.get("best_params") or {},
                best_metric_value=result_dict.get("best_fitness"),
                result_summary={
                    "best_params": result_dict.get("best_params") or {},
                    "best_fitness": result_dict.get("best_fitness"),
                    "target_metric": request.optimization_target,
                    "execution_time": result_dict.get("execution_time"),
                    "generations": len(result_dict.get("history") or []),
                },
                all_results=[],
            )
        )

        self.update_state(
            state="SUCCESS",
            meta={
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "completed",
                "message": "遗传优化已完成",
                "best_params": result_dict.get("best_params") or {},
                "result": result_dict,
            },
        )
        _send_progress_update(
            {
                "backtest_id": optimization_id,
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "completed",
                "message": "Genetic optimization completed",
                "best_params": result_dict.get("best_params") or {},
                "result": result_dict,
            }
        )

        return result_dict
    except OptimizationCancelledError as exc:
        task_log.info(f"遗传算法优化已取消: {exc}")
        cancelled_result = {
            "optimization_id": optimization_id,
            "status": "cancelled",
            "message": str(exc),
            "all_results": [],
            "best_params": {},
            "target_metric": request.optimization_target,
            "execution_time": 0,
        }
        self.update_state(
            state="CANCELLED",
            meta={
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "cancelled",
                "message": str(exc),
                "result": cancelled_result,
            },
        )
        _run_async(
            optimization_persistence.update_run(
                optimization_id,
                status="cancelled",
                error_message=str(exc),
            )
        )
        _send_progress_update(
            {
                "backtest_id": optimization_id,
                "optimization_id": optimization_id,
                "progress": 1.0,
                "status": "cancelled",
                "message": str(exc),
            }
        )
        return cancelled_result
    except Exception as exc:
        task_log.exception(f"GA Optimization failed: {exc}")
        self.update_state(
            state="PROGRESS", meta={"status": "failed", "error": str(exc)}
        )
        _send_progress_update(
            {
                "backtest_id": optimization_id,
                "status": "failed",
                "error_message": str(exc),
            }
        )
        raise
    finally:
        _release_optimization_lock(_get_redis_client(), optimization_id or task_id)
