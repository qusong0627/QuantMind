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
        "tdx_hot_set_feed", "TDX 桥热集行情（P6）", "worker", "trade",
        "盘中持续轮转（0.18s/只，预算 333/min）",
        "TDX_HOTSET_FEED_ENABLED", True, 600, None,
        "热集 529 只经通达信桥(.13:8550/L2)取快照 → market:snapshot/series（T-P6-02 桥源席）",
    ),
    JobSpec(
        "sentinel_push", "哨兵告警消费（P6）", "worker", "trade", "5s 长轮询（消费组 sentinel）",
        "QM_SENTINEL_WORKER_ENABLED", True, 600,
        None,  # 常驻消费循环，无「跑一次」语义 → 不可重跑（此前声明命令但无分发，守卫为红）
        "intel:events → sentinel_alerts 留痕 + 分级推送（Redis 门控 qm:sentinel:config，T-P6-15）",
    ),
    JobSpec(
        "sentinel_backfill", "哨兵 T+1 回填（P6）", "worker", "trade",
        "交易日 16:10（300s 轮询）",
        "QM_SENTINEL_WORKER_ENABLED", True, 900,
        "python backend/scripts/schedule_ctl.py run sentinel_backfill --date YYYY-MM-DD",
        "告警 T+1 兑现回填（命中/误报）→ 误报率报表（T-P6-15）",
    ),
    JobSpec(
        "advice_backfill", "建议卡兑现回填（P6）", "worker", "trade",
        "每日 01:40（300s 轮询；日键防重）",
        "QM_ADVICE_BACKFILL_ENABLED", True, 900,
        "python backend/scripts/schedule_ctl.py run advice_backfill --date YYYY-MM-DD",
        "建议卡 T+1/T+3/T+5 超额兑现（决策日收盘口径）→ 建议成功率统计（T-P6-16 闭环）",
    ),
    JobSpec(
        "advice_generator", "建议卡规则生成（P6）", "worker", "trade",
        "交易日 16:20（300s 轮询；日键防重）",
        "QM_ADVICE_GEN_ENABLED", True, 900,
        "python backend/scripts/schedule_ctl.py run advice_generator",
        "信号×情报共振 → 观察仓建议卡（否决/去重/regime 门控/每日≤3 张，人在环执行）",
    ),
    JobSpec(
        "holding_sentinel", "持仓哨兵（用户级预警）", "worker", "trade", "60s 周期",
        "QM_SENTINEL_WORKER_ENABLED", True, 300, None,
        "持仓/自选 ∪ 分数迁移 ∪ sentinel_alerts 利空 → qm_holding_alerts + 站内通知",
    ),
    JobSpec(
        "hot_set_builder", "热集构建（P6）", "worker", "trade", "60s 周期",
        "QM_HOT_SET_BUILD_ENABLED", True, 300, None,
        "全用户持仓并集 ∪ 候选池 → Redis 热集集合（订阅采集数据源，T-P6-06）",
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
        "dual_book", "双轨对账（模拟↔真单数量）", "worker", "trade",
        "交易日 15:10（60s 轮询）",
        "MIRROR_RECONCILE_ENABLED", True, 600,
        "python backend/scripts/schedule_ctl.py run dual_book --date YYYYMMDD",
        "模拟成交 vs 镜像真单数量差异，Redis mirror:reconcile:{date}",
    ),
    JobSpec(
        "mirror_shadow", "影子对照（模拟↔真单价格）", "worker", "trade",
        "交易日 15:15（60s 轮询）",
        "MIRROR_SHADOW_ENABLED", True, 600,
        "python backend/scripts/schedule_ctl.py run mirror_shadow --date YYYYMMDD",
        "成交价偏差/成交率/滑点实现/跟踪误差，Redis mirror:shadow:{date}（T-P2-06）",
    ),
    JobSpec(
        "eval_scores", "评分五卡（EOD）", "worker", "trade",
        "交易日 16:00（60s 轮询）",
        "EVAL_SCORES_WORKER_ENABLED", True, 600,
        "python backend/scripts/schedule_ctl.py run eval_scores --date YYYY-MM-DD",
        "因子/模型/策略/账户/每日选股五卡 → eval_scores 表（T-P4-05b）",
    ),
    JobSpec(
        "health_recheck", "月度体检复检", "worker", "trade",
        "每月首周（3600s 轮询）",
        "HEALTH_RECHECK_ENABLED", True, 7200,
        "python backend/scripts/schedule_ctl.py run health_recheck --date YYYY-MM-DD",
        "SIM/LIVE 策略月度回测体检复检（T-P4-06 ③；结论退化告警）",
    ),
    JobSpec(
        "risk_tier", "风险档位定档（P1.8）", "worker", "trade",
        "交易日 09:10（300s 轮询；日键防重）",
        "QM_RISK_TIER_ENABLED", True, 900,
        "python backend/scripts/schedule_ctl.py run risk_tier [--date YYYY-MM-DD]",
        "波动/回撤/情绪 → 当日档位 qm:risk:tier（闸门买入侧参数只收紧，tiers.py 消费）",
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
        "news_tag_rollup", "新闻标签汇总", "celery_beat", "celery", "每 30 分钟",
        "NEWS_TAG_ROLLUP_ENABLED", True, 5400, None,
        "近 20 天新闻标签（利空/利好）→ news_stock_tags，供候选列表与推送前风险排除",
    ),
    JobSpec(
        "strategy_lab_scan", "策略实验室日扫", "celery_beat", "celery", "交易日 23:00",
        "STRATEGY_LAB_SCAN_ENABLED", True, 345600, None,
    ),
    JobSpec(
        "backfill_quality", "推理质量回填", "celery_beat", "celery", "每日 02:30",
        None, True, 345600, None, "滞后 5 天回填真实收益算 Rank IC",
    ),
    # T-RC-14：两个关键交易循环此前不在册——它们挂掉时 C07 无项可判，
    # 表现为「体检全绿但策略不再调仓」。心跳写 ``qm:sched:hb:*``（db0）。
    JobSpec(
        "sim_hosted", "模拟盘托管调度", "worker", "trade", "30s 轮询（窗口内触发）",
        "ENABLE_SIMULATION_HOSTED_SCHEDULER", True, 300, None,
        "读活跃快照→判定调仓日/时段→SimulationEngine.run_cycle（T-P3 托管）",
    ),
    JobSpec(
        "manual_execution", "手动执行任务消费", "worker", "trade", "1s 长轮询队列",
        None, True, 300,
        None,  # 常驻消费循环，无「跑一次」语义 → 不可重跑
        "队列 → process_task：REAL/SHADOW 托管任务与手动单（无开关，随 trade 服务常开）",
    ),
    # P2.8（决策层移植）：隔壁 crontab 的决策时刻 → 本仓常驻 worker（11 个槽位：
    # 8 个盘中 + 3 个建仓，其中 2 个是补跑槽）。
    # 开关口径是 ``env_flags.env_flag``（**只有 "true" 生效**），比本注册表
    # switch_enabled 的宽词表（1/yes/on 也算真）窄——``list`` 在 ``=1/yes/on`` 时会
    # 显示「开」而 worker 并未启动，此时以 C07 的「无记录」为准（心跳是这条差异的
    # 唯一可观测信号）。**默认关闭**：切流是 P5 的显式动作。
    JobSpec(
        "decision_round", "决策轮（P2.8）", "worker", "trade",
        "交易日 11 个槽位（08:30~14:45，30s 轮询、45min 补跑窗）",
        "QM_DECISION_ROUND_ENABLED", False, 300,
        "python backend/scripts/schedule_ctl.py run decision_round --force",
        "池 → 闸门筛 → LLM 决策 → 执行段（下单/守护规则）→ 审计表；真钱生产者，默认关。"
        "重跑 = 抢占槽位再跑一轮 = 会真的再下单（同槽同向同标的被幂等键挡住）",
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


def read_heartbeats(
    job_keys: "tuple[str, ...] | list[str]",
    *,
    now_ts: float | None = None,
    redis_client: Any = None,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """批量读心跳 → ``[{key,name,enabled,state,age,ttl}]``（T-RC-20）。

    判定规则与体检 C07 完全一致（``ok``/``stale``/``off``/``missing``）。抽到这里
    是为了让**前端守护条**与体检看到同一个结论——两处各写一遍必然漂移，而「界面上
    显示活着、体检报死了」比只有一处更糟。

    读失败不抛：守护条拿不到心跳时如实报 ``missing``，不假装 ``ok``。
    """
    now = time.time() if now_ts is None else float(now_ts)
    client = redis_client
    if client is None:
        try:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client

            client = get_redis_sentinel_client()
        except Exception as exc:  # noqa: BLE001 - 连不上 Redis 也要给出结论
            logger.debug("[Scheduler] 心跳读取客户端不可用: %s", exc)
            client = None

    entries: list[dict[str, Any]] = []
    for key in job_keys:
        spec = JOBS_BY_KEY.get(key)
        if spec is None:
            logger.warning("[Scheduler] 未注册任务读心跳: %s", key)
            continue
        enabled = switch_enabled(spec, env)
        raw = None
        if client is not None:
            try:
                raw = client.get(heartbeat_key(key))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Scheduler] 心跳读取失败 %s: %s", key, exc)
                raw = None
        age: int | None = None
        if raw is not None:
            try:
                value = raw.decode() if isinstance(raw, bytes) else raw
                age = int(now - float(value))
            except (TypeError, ValueError):
                age = None
        ttl = spec.heartbeat_ttl
        if not enabled:
            state = "off"
        elif age is None or ttl is None:
            state = "missing"
        elif age <= ttl:
            state = "ok"
        else:
            state = "stale"
        entries.append(
            {
                "key": spec.key,
                "name": spec.name,
                "enabled": enabled,
                "state": state,
                "age": age,
                "ttl": ttl,
            }
        )
    return entries


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
