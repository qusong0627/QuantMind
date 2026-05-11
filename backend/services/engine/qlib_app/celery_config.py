"""
Celery配置 - Qlib服务专用

提供异步回测任务队列支持
"""

import os
import socket
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

PROJECT_ROOT = Path(__file__).resolve().parents[4]

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv(PROJECT_ROOT / ".env")


# Redis连接配置
def _is_running_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _resolve_redis_host() -> str:
    # 强制优先检查环境变量 REDIS_HOST
    # 注意：在 Docker Compose 环境下，环境变量会被注入
    configured = os.getenv("REDIS_HOST")
    if configured:
        # 如果注入的是 localhost，但在容器内，我们需要修正它
        if _is_running_in_docker() and configured in ("localhost", "127.0.0.1"):
            return "quantmind-redis"
        return configured

    # 如果没配置，默认尝试使用 docker 内部服务名
    if _is_running_in_docker():
        return "quantmind-redis"

    return "localhost"


REDIS_HOST = _resolve_redis_host()
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB_BROKER = int(os.getenv("REDIS_DB_BROKER", "3"))  # Qlib专用broker DB
REDIS_DB_BACKEND = int(os.getenv("REDIS_DB_BACKEND", "4"))  # Qlib专用backend DB

# 构建连接URL
if REDIS_PASSWORD:
    BROKER_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}" f"/{REDIS_DB_BROKER}"
    BACKEND_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}" f"/{REDIS_DB_BACKEND}"
else:
    BROKER_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB_BROKER}"
    BACKEND_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB_BACKEND}"

# 创建Celery应用
celery_app = Celery(
    "qlib_service",
    broker=BROKER_URL,
    backend=BACKEND_URL,
)

# 并发/性能参数（可通过环境变量覆盖）
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "3600"))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "3300"))
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "1"))
CELERY_WORKER_MAX_TASKS_PER_CHILD = int(os.getenv("CELERY_WORKER_MAX_TASKS_PER_CHILD", "10"))
CELERY_WORKER_DISABLE_RATE_LIMITS = os.getenv("CELERY_WORKER_DISABLE_RATE_LIMITS", "false").lower() == "true"
CELERY_TASK_ACKS_LATE = os.getenv("CELERY_TASK_ACKS_LATE", "true").lower() == "true"
CELERY_TASK_REJECT_ON_WORKER_LOST = os.getenv("CELERY_TASK_REJECT_ON_WORKER_LOST", "true").lower() == "true"
CELERY_RESULT_EXPIRES = int(os.getenv("CELERY_RESULT_EXPIRES", "86400"))

CELERY_QUEUE = os.getenv("QLIB_CELERY_QUEUE", "qlib_backtest").strip() or "qlib_backtest"
CELERY_EXCHANGE = os.getenv("QLIB_CELERY_EXCHANGE", "qlib")
CELERY_ROUTING_KEY = os.getenv("QLIB_CELERY_ROUTING_KEY", "qlib.backtest")
AUTO_INFERENCE_ENABLED = os.getenv("AUTO_INFERENCE_ENABLED", "true").lower() == "true"

# Celery配置
beat_schedule = {}
if AUTO_INFERENCE_ENABLED:
    beat_schedule = {
        # 交易日 00:00 触发自动推理扫描，支持多策略依次执行
        "auto-inference-window-scan-weekdays": {
            "task": "engine.tasks.auto_inference_if_needed",
            "schedule": crontab(minute="0", hour="0", day_of_week="1-5"),
        },
    }

celery_app.conf.update(
    # 序列化
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 任务追踪
    task_track_started=True,
    task_send_sent_event=True,
    # 超时设置（回测可能耗时较长）
    task_time_limit=CELERY_TASK_TIME_LIMIT,  # 硬限制
    task_soft_time_limit=CELERY_TASK_SOFT_TIME_LIMIT,  # 软限制
    # 工作进程配置
    worker_prefetch_multiplier=CELERY_WORKER_PREFETCH_MULTIPLIER,
    worker_max_tasks_per_child=CELERY_WORKER_MAX_TASKS_PER_CHILD,
    worker_disable_rate_limits=CELERY_WORKER_DISABLE_RATE_LIMITS,
    # 结果配置
    result_expires=CELERY_RESULT_EXPIRES,
    result_extended=True,  # 存储扩展结果信息
    # 重试配置
    task_acks_late=CELERY_TASK_ACKS_LATE,  # 任务完成后才ack
    task_reject_on_worker_lost=CELERY_TASK_REJECT_ON_WORKER_LOST,
    # 队列配置
    task_default_queue=CELERY_QUEUE,
    task_default_exchange=CELERY_EXCHANGE,
    task_default_routing_key=CELERY_ROUTING_KEY,
    # 任务路由
    task_routes={
        "backend.services.engine.qlib_app.tasks.*": {"queue": CELERY_QUEUE},
        "qlib_app.tasks.*": {"queue": CELERY_QUEUE},
    },
    imports=(
        "backend.services.engine.qlib_app.tasks",
        "backend.services.engine.tasks.celery_tasks",
    ),
    # 监控配置
    worker_send_task_events=True,
    beat_schedule=beat_schedule,
)

# 自动发现任务
celery_app.autodiscover_tasks(
    [
        "backend.services.engine.qlib_app",
        "backend.services.engine",
    ]
)
