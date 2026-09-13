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

# worker/beat 不经过 main_oss 启动，管理台写入的 runtime.env（如
# QUANTDB_API_KEY）必须在此注入，否则定时同步拿不到运行时密钥。
# 真实环境变量优先，runtime.env 只补空缺，与 main_oss 语义一致。
from backend.shared.runtime_secrets import load_runtime_env

_runtime_secrets_loaded = load_runtime_env()
if _runtime_secrets_loaded:
    import logging

    logging.getLogger(__name__).info(
        "Loaded %d runtime secrets from runtime.env", _runtime_secrets_loaded
    )


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

CELERY_QUEUE = os.getenv("QLIB_CELERY_QUEUE", "qlib_backtest_srv").strip() or "qlib_backtest_srv"
CELERY_EXCHANGE = os.getenv("QLIB_CELERY_EXCHANGE", "qlib")
CELERY_ROUTING_KEY = os.getenv("QLIB_CELERY_ROUTING_KEY", "qlib.backtest")
AUTO_INFERENCE_ENABLED = os.getenv("AUTO_INFERENCE_ENABLED", "true").lower() == "true"
NEWS_ENRICH_ENABLED = os.getenv("NEWS_ENRICH_ENABLED", "true").lower() == "true"
NEWS_ENRICH_INTERVAL_SEC = int(os.getenv("NEWS_ENRICH_INTERVAL_SEC", "60"))
NEWS_MATCHER_RELOAD_SEC = int(os.getenv("NEWS_MATCHER_RELOAD_SEC", "600"))

# Celery配置
beat_schedule = {}
if AUTO_INFERENCE_ENABLED:
    beat_schedule = {
        # 交易日 00:00 触发自动推理扫描，支持多策略依次执行
        "auto-inference-window-scan-weekdays": {
            "task": "engine.tasks.auto_inference_if_needed",
            "schedule": crontab(minute="0", hour="0", day_of_week="1-5"),
        },
        # 推理质量回填：每日 02:30 回填已完成推理但缺 quality 记录的日期
        # （滞后 5 天等真实收益兑现，算生产 Rank IC）
        "backfill-inference-quality-daily": {
            "task": "engine.tasks.backfill_inference_quality",
            "schedule": crontab(minute="30", hour="2", day_of_week="1-6"),
            "kwargs": {"horizon_days": 5, "limit": 500},
        },
        # 时间平滑历史：每日 03:00 聚合近5日推理分数供融合平滑
        "build-smooth-history-daily": {
            "task": "engine.tasks.build_smooth_history",
            "schedule": crontab(minute="0", hour="3", day_of_week="1-6"),
            "kwargs": {"lookback_days": 5},
        },
    }

if NEWS_ENRICH_ENABLED:
    beat_schedule["news-enrich-recent"] = {
        "task": "engine.tasks.news_enrich_recent",
        "schedule": float(NEWS_ENRICH_INTERVAL_SEC),
        "kwargs": {"limit": 200},
    }
    beat_schedule["news-matcher-reload"] = {
        "task": "engine.tasks.news_matcher_reload",
        "schedule": float(NEWS_MATCHER_RELOAD_SEC),
    }

# 每日自动同步上游数据建议设置到次日 00:00 以后，按需错峰触发，避免集中请求。
# 具体执行时间以前端「同步调度」设置为准（Redis quantmind:sync_schedule:{market}），此处不再内置固定调度。
DAILY_SYNC_ENABLED = os.getenv("DAILY_SYNC_ENABLED", "true").lower() == "true"
if DAILY_SYNC_ENABLED:
    # A 股及多市场的定时同步统一走 market-sync-dispatch（每分钟轮询 Redis 调度），
    # 不再此处硬编码 03:00/03:40/03:50 固定任务，避免短时间大量同步请求。
    pass

# 市场定时同步调度检查（每分钟，具体触发时间由前端每市场配置，存 Redis）
if os.getenv("MARKET_SYNC_SCHEDULE_ENABLED", "true").lower() == "true":
    beat_schedule["market-sync-dispatch"] = {
        "task": "engine.tasks.dispatch_market_sync",
        "schedule": crontab(minute="*", hour="*"),
    }

# Strategy Lab daily scan — runs after the data sync settles (Day 16)
if os.getenv("STRATEGY_LAB_SCAN_ENABLED", "true").lower() == "true":
    beat_schedule["strategy-lab-daily-scan"] = {
        "task": "engine.tasks.strategy_lab_daily_scan",
        "schedule": crontab(minute="0", hour="23", day_of_week="1-5"),
        "kwargs": {"lookback_days": 7},
    }

# 交易日盘后计算市场分析快照（同步时间以用户在前端「同步调度」里的配置为准，
# 不假设固定时刻已同步完）。beat 在 04:00–05:50 每 10 分钟触发一次，任务自带
# 新鲜度门控：库内分区未超过线上快照时直接跳过不覆盖，等用户配置的同步完成后
# 的某一次轮询自然产出；节假日无新分区时全天跳过属正常行为。
# 产出 JSON + 标签 SQLite 到 QM_MARKET_SNAPSHOT_DIR=/data/market-analysis，API 读取。
if os.getenv("MARKET_SNAPSHOT_ENABLED", "true").lower() == "true":
    beat_schedule["market-snapshot"] = {
        "task": "engine.tasks.market_snapshot",
        # 含周六：每个交易日的数据在**次日早上**落快照，只跑到周五的话
        # 周五收盘的数据要等下一个周一才产出 —— 周末打开市场分析看到的是周四的
        # 数据（实测 2026-09-11 周五的数据在周日仍缺失）。任务自带新鲜度门控，
        # 周六无新分区时会自行跳过，不会白跑。
        "schedule": crontab(minute="*/10", hour="4-5", day_of_week="mon-sat"),
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
