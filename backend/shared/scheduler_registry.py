"""调度注册表（T-P1-06）：全部周期性任务/worker 的唯一事实源。

背景：worker 散落在 `trade/main.py` 内嵌循环、beat 条目散落在 `celery_config`，
"现在到底有哪些定时任务在跑、心跳正不正常、能不能手动重跑"没有任何单一答案；
曾发生 EOD/挂单 worker 长时间无调用方而无人发现（2026-09-15 诊断）。

本注册表提供：
- **声明**：每个任务的归属服务、周期、开关环境变量、心跳 TTL、手动重跑命令；
- **心跳协议**：任务在循环/执行时写 ``qm:sched:hb:{key}`` = epoch 秒（best-effort，不抛出），
  体检 C07 按注册表逐项判定（ok / stale / off / missing）；
- **手动重跑**：`backend/scripts/schedule_ctl.py run <key>` 按 `rerun` 字段分派。

注意：`market_snapshot` / 部分 beat 任务的心跳尚未接线（heartbeat_ttl=nil 的项不进 C07），
接线时补 JobSpec 即可——加一项任务必须同时给出这四个字段（评审红线）。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

HEARTBEAT_KEY_PREFIX = "qm:sched:hb"


@dataclass(frozen=True)
class JobSpec:
    key: str
    name: str
    kind: str  # worker | celery_beat
    owner: str  # trade | celery
    schedule: str  # 人类可读周期
    switch_env: str | None  # 开关环境变量（None=常开）
    switch_default_on: bool
    heartbeat_ttl: int | None  # 心跳有效期（秒）；None=暂未接线（不进体检）
    rerun: str | None  # 手动重跑命令（None=不支持）
    desc: str = ""


JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        "equity_settle", "权益结算", "worker", "trade", "30s 周期",
        "SIM_EQUITY_SETTLE_ENABLED", True, 150, None,
        "对账确权→行情重估→资金快照；盘外/重启后权益不冻结",
    ),
    JobSpec(
        "sim_eod", "模拟盘日终结算", "worker", "trade", "每日 03:05（60s 轮询）",
        "SIM_EOD_WORKER_ENABLED", True, 600,
        "python backend/scripts/schedule_ctl.py run sim_eod --date YYYY-MM-DD",
        "日级台账快照（依赖 PG 台账非空，T-P1-04 后逐步生效）",
    ),
    JobSpec(
        "pending_order", "挂单消费", "worker", "trade", "15s 周期",
        "SIM_PENDING_ORDER_WORKER_ENABLED", True, 120, None,
        "消化 status=pending 的模拟单",
    ),
    JobSpec(
        "t1_unlock", "T+1 解锁", "worker", "trade", "60s 轮询（交易日 09:16 生效）",
        None, True, 300, None,
        "次日补齐 available_volume",
    ),
    JobSpec(
        "corp_action", "公司行为处理", "worker", "trade", "60s 轮询",
        None, True, 300, None,
        "分红送转到账（apply_due_actions）",
    ),
    JobSpec(
        "auto_inference", "自动推理", "celery_beat", "celery", "交易日 08:00",
        None, True, 345600,
        "python backend/scripts/schedule_ctl.py run auto_inference --date YYYY-MM-DD",
        "全市场信号生成（runner 级单实例锁保护，见 T-P1-02）",
    ),
    JobSpec(
        "market_sync_dispatch", "市场同步派发", "celery_beat", "celery", "每分钟",
        "MARKET_SYNC_SCHEDULE_ENABLED", True, 600,
        "python backend/scripts/schedule_ctl.py run market_sync_dispatch",
        "按 Redis 同步调度配置派发各市场数据同步",
    ),
    JobSpec(
        "news_enrich", "新闻富化", "celery_beat", "celery", "~30s",
        None, True, 900, None, "Huntly 新闻入库富化",
    ),
    JobSpec(
        "news_matcher", "新闻匹配重载", "celery_beat", "celery", "分钟级",
        None, True, 900, None, "新闻标题匹配器热重载",
    ),
    JobSpec(
        "strategy_lab_scan", "策略实验室日扫", "celery_beat", "celery", "交易日 23:00",
        "STRATEGY_LAB_SCAN_ENABLED", True, 345600, None,
    ),
    JobSpec(
        "backfill_quality", "推理质量回填", "celery_beat", "celery", "每日 02:30",
        None, True, 345600, None, "滞后 5 天回填真实收益算 Rank IC",
    ),
)

JOBS_BY_KEY: dict[str, JobSpec] = {job.key: job for job in JOBS}


def heartbeat_key(job_key: str) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}:{job_key}"


def switch_enabled(spec: JobSpec, env: dict[str, str] | None = None) -> bool:
    """开关判定（纯函数）：未配置用默认值；0/false/no/off 视为关。"""
    if not spec.switch_env:
        return True
    source = env if env is not None else os.environ
    raw = str(source.get(spec.switch_env, "")).strip().lower()
    if raw == "":
        return spec.switch_default_on
    return raw not in {"0", "false", "no", "off"}


def heartbeat(job_key: str, *, redis_client=None, ttl: int | None = None) -> bool:
    """写心跳（best-effort：任何异常只告警不抛出，绝不拖垮业务循环）。"""
    spec = JOBS_BY_KEY.get(job_key)
    if spec is None:
        logger.warning("[Scheduler] 未注册任务写心跳: %s", job_key)
        return False
    effective_ttl = ttl or spec.heartbeat_ttl or 300
    try:
        client = redis_client
        if client is None:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client

            client = get_redis_sentinel_client()
        client.set(heartbeat_key(job_key), int(time.time()), ex=effective_ttl)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Scheduler] 心跳写入失败 %s: %s", job_key, exc)
        return False


def classify_scheduler_status(entries: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
    """心跳状态判定（纯函数）→ (level, detail, metrics)。

    entries: [{key,name,enabled,state,age}]，state ∈ ok/stale/off/missing。
    规则：enabled 且 stale → fail（调度实际已停）；enabled 且 missing → warn（过渡期/未部署）；
    其余 ok；detail 点名，绝不静默。
    """
    stale = [e for e in entries if e.get("enabled") and e.get("state") == "stale"]
    missing = [e for e in entries if e.get("enabled") and e.get("state") == "missing"]
    off = [e for e in entries if not e.get("enabled")]
    metrics = {
        "total": len(entries),
        "stale": [e["key"] for e in stale],
        "missing": [e["key"] for e in missing],
        "off": [e["key"] for e in off],
    }
    if stale:
        detail = "; ".join(
            f"{e['key']} 心跳过期 {e.get('age')}s > {e.get('ttl')}s" for e in stale
        )
        return "fail", f"调度停摆: {detail}", metrics
    if missing:
        names = ", ".join(e["key"] for e in missing)
        return (
            "warn",
            f"{len(missing)} 项无心跳记录（新上线/未部署心跳，观察一周期）: {names}",
            metrics,
        )
    on_count = len([e for e in entries if e.get("enabled")])
    return "ok", f"{on_count} 项开启且心跳新鲜（{len(off)} 项按开关关闭）", metrics
