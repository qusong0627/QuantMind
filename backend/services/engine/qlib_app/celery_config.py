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
# 新闻 20 天标签汇总：窗口天数（用户指定 20）+ 独立开关。
# 独立开关的理由：注册表按 switch_env 判定「任务关掉」还是「任务停摆」，
# 借用 NEWS_ENRICH_ENABLED 的话，关掉富化会把汇总报成停摆（体检假阳性）。
NEWS_TAG_ROLLUP_ENABLED = os.getenv("NEWS_TAG_ROLLUP_ENABLED", "true").lower() == "true"
NEWS_TAG_WINDOW_DAYS = int(os.getenv("NEWS_TAG_WINDOW_DAYS", "20"))

# 执行损耗 TCA 日读数（P1.6）。开关独立于其它新闻/推理任务——理由同
# NEWS_TAG_ROLLUP_ENABLED：注册表按 switch_env 判「关闭」还是「停摆」，
# 借别人的开关会让体检把「本就没开」报成「停摆」。
TCA_REPORT_ENABLED = os.getenv("TCA_REPORT_ENABLED", "true").lower() == "true"
# 市场状态日更持久化（P3 · 设计 §6.2）：交易日 16:30 写 qm_regime_daily。
# 开关独立（同上理由）：关掉它只影响「状态时间线」的日频留痕，不影响推理/回测
# 里的 regime 计算（那些是即时算的）。
REGIME_PERSIST_ENABLED = os.getenv("REGIME_PERSIST_ENABLED", "true").lower() == "true"
# 滚动窗口天数（含产出当天）。取数走 orders.ref_price（决策链取价时刻），
# 补写是 2026-09-24 上的，故窗口开头的成交天然落在「不可定价」——报告里如实分列。
TCA_REPORT_DAYS = int(os.getenv("TCA_REPORT_DAYS", "30"))

# Celery配置
beat_schedule = {}
if AUTO_INFERENCE_ENABLED:
    beat_schedule = {
        # 交易日 08:00 触发自动推理扫描，支持多策略依次执行
        "auto-inference-window-scan-weekdays": {
            "task": "engine.tasks.auto_inference_if_needed",
            "schedule": crontab(minute="0", hour="8", day_of_week="1-5"),
        },
        # 盘前数据迟到补跑：08:00 因「数据范围不足」失败时，09:05 再试一次。
        # 08:00 成功时被 ALREADY_DONE / 推理锁跳过（幂等）；09:05 开跑约 6 分钟，
        # 仍早于 09:35 决策轮。2026-10-29 全是 08:00 数据门禁失败、当天无信号，
        # 这条兜底让上游迟到一天时仍有盘中信号。
        "auto-inference-retry-0905": {
            "task": "engine.tasks.auto_inference_if_needed",
            "schedule": crontab(minute="5", hour="9", day_of_week="1-5"),
        },
        # 推理质量回填：每日 02:30 回填已完成推理但缺 quality 记录的日期
        # （滞后 5 天等真实收益兑现，算生产 Rank IC）
        "backfill-inference-quality-daily": {
            "task": "engine.tasks.backfill_inference_quality",
            "schedule": crontab(minute="30", hour="2", day_of_week="1-6"),
            "kwargs": {"horizon_days": 5, "limit": 500},
        },
    }

# 执行损耗 TCA 日读数（P1.6）：16:40 = 盘后（15:00 收）且晚于 eval_scores（16:00）
# 与 advice_generator（16:20）——那些任务不看成交，但把报表挤在同一分钟内只会让
# 盘后的 PG 更忙。**全量落盘**（滚动 30 天窗口，一份/天），不推送：这是读数面，
# 要人在仪表盘上看，不是报警。沿用 backfill_quality 的 mon-sat 形态：周日不跑，
# 周六跑（周六那份覆盖含周五收盘的窗口，周末打开也看得到最近一个月）。
if TCA_REPORT_ENABLED:
    beat_schedule["tca-daily-report"] = {
        "task": "engine.tasks.tca_daily_report",
        "schedule": crontab(minute="40", hour="16", day_of_week="1-6"),
        "kwargs": {"days": TCA_REPORT_DAYS},
    }

# 市场状态日更（P3 · 设计 §6.2）：交易日 16:30 写 qm_regime_daily（生效日口径，
# 历史行冻结）。16:30 = 盘后半小时：日线数据（指数）此时已可读；与 TCA 的 16:40
# 错开一分钟，避免盘后同一分钟挤两个任务。只跑工作日：周末没有新生效日。
if REGIME_PERSIST_ENABLED:
    beat_schedule["regime-daily-persist"] = {
        "task": "engine.tasks.regime_daily_persist",
        "schedule": crontab(minute="30", hour="16", day_of_week="1-5"),
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
# 新闻 20 天标签汇总（候选列表的新闻利空/利好标注、推送前风险排除的数据源）。
# 半小时一跑而非日频：隔夜才算一次的话，早上刚出的立案调查要等第二天才拦得住。
# 单轮实测约 5s（发布时间索引只增量补新 id），任务自带单实例锁。
if NEWS_TAG_ROLLUP_ENABLED:
    beat_schedule["news-tag-rollup"] = {
        "task": "engine.tasks.news_tag_rollup",
        "schedule": crontab(minute="*/30"),
        "kwargs": {"days": NEWS_TAG_WINDOW_DAYS},
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

# 滚动重训调度检查（P1，每分钟 tick；是否真正到点派发由 Redis
# quantmind:retrain_schedule:{market} 配置决定）。默认**关**：开启自动月度重训
# 是一次显式运维动作（与实盘双开关同哲学——代码全留，仅收敛入口）。
if os.getenv("RETRAIN_SCHEDULER_ENABLED", "false").lower() == "true":
    beat_schedule["retrain-dispatch"] = {
        "task": "engine.tasks.dispatch_retrain",
        "schedule": crontab(minute="*", hour="*"),
    }

# 融合模型权重周期刷新（P2-4；设计《机构级模型融合》§7）：每日 22:30 扫全部
# 融合模型，按成员近窗 IC 重算权重；最大权重变动 < 0.02 不落盘（防抖）——
# 阈值下实际写盘自然呈周更节律。纯本地重算 + 原子写快照，默认开，可关。
if os.getenv("FUSION_REFRESH_ENABLED", "true").lower() == "true":
    beat_schedule["fusion-weights-refresh"] = {
        "task": "engine.tasks.refresh_fusion_weights",
        "schedule": crontab(minute="30", hour="22"),
    }

# 值班死手检查（P2-5）：16:00~23:30 每 30 分钟核对当日回执（收盘报表/值班摘要/
# 决策轮停滞检查/实时信号覆盖），缺失集收缩语义告警 + 补齐恢复 + 当日封账。
# **住在 celery（跨进程树）**：trade 整体死亡时它是唯一还能响的裁判位——「该响
# 没响」的检查绝不能与被检查对象同生共死。16:00 = 值班摘要（15:40）后首个半点；
# 23:30 收尾（过 0 点换日键，不再核对）。非交易日由任务体秒退。
if os.getenv("DUTY_DEADMAN_ENABLED", "true").lower() == "true":
    beat_schedule["duty-deadman-check"] = {
        "task": "engine.tasks.duty_deadman_check",
        "schedule": crontab(minute="0,30", hour="16-23"),
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

# 因子概率板（市场分析 · 细案 §六，2026-10-11）：T 日批量模型信号（前一晚链已
# 首更，T 日 08:00 auto-inference 盘前再刷——实测：10-12 批 10-10 03:06 已落
# PG，工作日批 08:00 落）× 因子 T-1 截面 → 当日决策板（精选因子合成分位 +
# 三层共振 + 四象限），落 QM_FACTOR_BOARD_DIR=/data/market-analysis/factor_board，
# API/前端读取。
# 08:40 选点：盘前（09:10 risk_tier 之前）且晚于 08:00 auto-inference（读当日
# 最新批），也避开 08:00 同分钟挤 engine。独立开关 FACTOR_BOARD_ENABLED——注册
# 表按 switch_env 判「关闭/停摆」，借别的开关会让体检把「本就没开」报成「停摆」
#（同 TCA/regime 的理由）。
# 只跑周一~五：板是盘前消费物，每个交易日 08:40 建当日一版；周六就算跑，建的
# 也是下周一那份（周一 08:40 随即同日期重建覆盖）——纯冗余，不排它。
if os.getenv("FACTOR_BOARD_ENABLED", "true").lower() == "true":
    beat_schedule["factor-board-daily"] = {
        "task": "engine.tasks.factor_board_build",
        "schedule": crontab(minute="40", hour="8", day_of_week="1-5"),
    }

# 可交易性掩码增量刷新（P2-d，2026-10-11）：收盘数据落盘后 18:40 重扫最近两周
# （脚本内区间替换合并，老日期原样保留），供因子概率板名单层过滤与因子报告
# 可交易轨消费。只跑周一~五；区间内无交易日脚本 no-op 退码 0，不会白报失败。
# 独立开关 TRADE_MASK_REFRESH_ENABLED（借别的开关会让体检把「本就没开」报成「停摆」）。
if os.getenv("TRADE_MASK_REFRESH_ENABLED", "true").lower() == "true":
    beat_schedule["trade-mask-refresh"] = {
        "task": "engine.tasks.trade_mask_refresh",
        "schedule": crontab(minute="40", hour="18", day_of_week="1-5"),
    }

# 资金流延续证据（市场分析 · 资金流全景 v2 §3/§8，2026-10-11）：板块主力净额
# 「连N」全史条件统计（探针对照 + 可执行 T+1 收盘建仓双口径、闸门五项、动量分层）
# + 当日命中榜 → QM_FLOW_EVIDENCE_DIR=/data/market-analysis/flow_evidence，API/
# 前端读取。数据同步时刻以用户在前端「同步调度」的配置为准，不假设固定时刻：
# beat 在晚间（19:00–23:40）与次日清晨（04:00–05:40）各每 20 分钟轮询，任务自带
# 新鲜度门控——库内 L2 分区未超前产物交易日时直接跳过，等同步落盘后的某一次
# 轮询自然产出；节假日无新分区时全天跳过属正常。含周六：周五的数据常周六晨落盘。
# 独立开关 FLOW_EVIDENCE_ENABLED（借别的开关会让体检把「本就没开」报成「停摆」）。
if os.getenv("FLOW_EVIDENCE_ENABLED", "true").lower() == "true":
    beat_schedule["flow-evidence-daily"] = {
        "task": "engine.tasks.flow_evidence_build",
        "schedule": crontab(minute="*/20", hour="4-5,19-23", day_of_week="mon-sat"),
    }

# 因子概率板滚动重选（P2-f，细案 §七）：每月首个交易日 08:50（晚于 08:40 产板）
# 以最近分区为 as_of 重跑选择管线 → 应用月台本 + 回填上月 OOS。
# beat 排每月 1–12 日工作日（月首交易日可能晚到，如国庆后 10-08），任务体再判
# 「最近可用分区在上一自然月」，非月首静默跳过（退码 0）。
if os.getenv("FACTOR_BOARD_RESELECT_ENABLED", "true").lower() == "true":
    beat_schedule["factor-board-reselect"] = {
        "task": "engine.tasks.factor_board_reselect",
        "schedule": crontab(
            minute="50", hour="8", day_of_month="1-12", day_of_week="1-5"
        ),
    }

# 聪明钱产物（市场分析 · 聪明钱追踪与选股 细案 §7，2026-10-15）：东财晚间发布
# 节奏不定，19:10 先建一版、21:10 第二档 catch-up（脚本含采集，幂等 + 重叠窗口
# 自愈，同日重跑覆盖）。落 QM_MARKET_SNAPSHOT_DIR 根下 market-analysis/smart_money
#（worker 容器未注入该 env 时由 datasets.product_dir 的容器兜底解析 /data），
# API/前端读取。独立开关 SMART_MONEY_ENABLED（借别的开关会让体检把「本就没开」
# 报成「停摆」）。
if os.getenv("SMART_MONEY_ENABLED", "true").lower() == "true":
    beat_schedule["smart-money-daily"] = {
        "task": "engine.tasks.smart_money_build",
        "schedule": crontab(minute="10", hour="19", day_of_week="1-5"),
    }
    beat_schedule["smart-money-daily-catchup"] = {
        "task": "engine.tasks.smart_money_build",
        "schedule": crontab(minute="10", hour="21", day_of_week="1-5"),
    }

# 港股聪明钱产物（市场分析 · 港股聪明钱深度分析 细案 §7，2026-10-16）：与 A 股
# 聪明钱错峰 30 分钟（19:40 先建、21:40 第二档 catch-up，脚本幂等，同日重跑
# 覆盖）。构建只读本地 parquet（ccass_top50/hsgt_south 由港股市场同步链维护，
# 无独立采集脚本）。落 QM_MARKET_SNAPSHOT_DIR 根下 market-analysis/hk_smart_money
# （worker 容器未注入该 env 时由 datasets.product_dir 的容器兜底解析 /data），
# API/前端读取。独立开关 HK_SMART_MONEY_ENABLED。
if os.getenv("HK_SMART_MONEY_ENABLED", "true").lower() == "true":
    beat_schedule["hk-smart-money-daily"] = {
        "task": "engine.tasks.hk_smart_money_build",
        "schedule": crontab(minute="40", hour="19", day_of_week="1-5"),
    }
    beat_schedule["hk-smart-money-catchup"] = {
        "task": "engine.tasks.hk_smart_money_build",
        "schedule": crontab(minute="40", hour="21", day_of_week="1-5"),
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
