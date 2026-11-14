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
    owner: str  # trade | celery | api（api=常驻在 API 进程内的任务，如训练回收器）
    schedule: str  # 人类可读周期
    switch_env: str | None  # 开关环境变量（None=常开）
    switch_default_on: bool
    heartbeat_ttl: int | None  # 心跳有效期（秒）；None=暂未接线（不进体检）
    rerun: str | None  # 手动重跑命令（None=不支持）
    desc: str = ""


JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        "equity_settle",
        "权益结算",
        "worker",
        "trade",
        "30s 周期",
        "SIM_EQUITY_SETTLE_ENABLED",
        True,
        150,
        None,
        "对账确权→行情重估→资金快照；盘外/重启后权益不冻结",
    ),
    JobSpec(
        "sim_eod",
        "模拟盘日终结算",
        "worker",
        "trade",
        "每日 03:05（60s 轮询）",
        "SIM_EOD_WORKER_ENABLED",
        True,
        600,
        "python backend/scripts/schedule_ctl.py run sim_eod --date YYYY-MM-DD",
        "日级台账快照（依赖 PG 台账非空，T-P1-04 后逐步生效）",
    ),
    JobSpec(
        "pending_order",
        "挂单消费",
        "worker",
        "trade",
        "15s 周期",
        "SIM_PENDING_ORDER_WORKER_ENABLED",
        True,
        120,
        None,
        "消化 status=pending 的模拟单",
    ),
    JobSpec(
        "tdx_hot_set_feed",
        "TDX 桥热集行情（P6）",
        "worker",
        "trade",
        "盘中持续轮转（0.18s/只，预算 333/min）",
        "TDX_HOTSET_FEED_ENABLED",
        True,
        600,
        None,
        "热集 529 只经通达信桥(.13:8550/L2)取快照 → market:snapshot/series（T-P6-02 桥源席）",
    ),
    JobSpec(
        "quote_freshness_watch",
        "行情快照断流监视（M8）",
        "worker",
        "trade",
        "盘中每 60s（采样热集前 120 只）",
        "QM_QUOTE_WATCH_ENABLED",
        True,
        600,
        None,
        "market:snapshot 数据心跳：按 freshness 谓词判最新写入年龄，断流/恢复边沿告警"
        "（数据面判活，与 tdx_hot_set_feed 的循环心跳互补）",
    ),
    # TdxAiData 订阅集群守护（2026-10-14）：分片 worker 此前**无任何定时拉起入口**
    # ——容器重建/崩溃后订阅静默不在，直到有人手动点面板自检。告警（M8）≠ 恢复，
    # 本任务是恢复面：体检缺片即拉起（节流 120s），订阅关时只心跳不拉起。
    JobSpec(
        "tdx_cluster_watch",
        "TdxAiData 订阅集群守护（P6）",
        "worker",
        "trade",
        "60s 巡检（缺片即拉起，节流 120s）",
        "QM_TDX_CLUSTER_WATCH_ENABLED",
        True,
        600,
        "python backend/scripts/schedule_ctl.py run tdx_cluster_watch",
        "分片 SDK worker 的自动拉起：cluster 体检（无副作用）→ worker != up 即 "
        "ensure_all（含 degraded 缺片）；持续崩溃时按最小间隔节流防拉起风暴",
    ),
    JobSpec(
        "sentinel_push",
        "哨兵告警消费（P6）",
        "worker",
        "trade",
        "5s 长轮询（消费组 sentinel）",
        "QM_SENTINEL_WORKER_ENABLED",
        True,
        600,
        None,  # 常驻消费循环，无「跑一次」语义 → 不可重跑（此前声明命令但无分发，守卫为红）
        "intel:events → sentinel_alerts 留痕 + 分级推送（Redis 门控 qm:sentinel:config，T-P6-15）",
    ),
    JobSpec(
        "sentinel_backfill",
        "哨兵 T+1 回填（P6）",
        "worker",
        "trade",
        "交易日 16:10（300s 轮询）",
        "QM_SENTINEL_WORKER_ENABLED",
        True,
        900,
        "python backend/scripts/schedule_ctl.py run sentinel_backfill --date YYYY-MM-DD",
        "告警 T+1 兑现回填（命中/误报）→ 误报率报表（T-P6-15）",
    ),
    JobSpec(
        "advice_backfill",
        "建议卡兑现回填（P6）",
        "worker",
        "trade",
        "每日 01:40（300s 轮询；日键防重）",
        "QM_ADVICE_BACKFILL_ENABLED",
        True,
        900,
        "python backend/scripts/schedule_ctl.py run advice_backfill --date YYYY-MM-DD",
        "建议卡 T+1/T+3/T+5 超额兑现（决策日收盘口径）→ 建议成功率统计（T-P6-16 闭环）",
    ),
    JobSpec(
        "advice_generator",
        "建议卡规则生成（P6）",
        "worker",
        "trade",
        "交易日 16:20（300s 轮询；日键防重）",
        "QM_ADVICE_GEN_ENABLED",
        True,
        900,
        "python backend/scripts/schedule_ctl.py run advice_generator",
        "信号×情报共振 → 观察仓建议卡（否决/去重/regime 门控/每日≤3 张，人在环执行）",
    ),
    JobSpec(
        "holding_sentinel",
        "持仓哨兵（用户级预警）",
        "worker",
        "trade",
        "60s 周期",
        "QM_SENTINEL_WORKER_ENABLED",
        True,
        300,
        None,
        "持仓/自选 ∪ 分数迁移 ∪ sentinel_alerts 利空 → qm_holding_alerts + 站内通知",
    ),
    JobSpec(
        "hot_set_builder",
        "热集构建（P6）",
        "worker",
        "trade",
        "60s 周期",
        "QM_HOT_SET_BUILD_ENABLED",
        True,
        300,
        None,
        "全用户持仓并集 ∪ 候选池 → Redis 热集集合（订阅采集数据源，T-P6-06）",
    ),
    JobSpec(
        "live_equity_sampler",
        "实盘净值分钟采样",
        "worker",
        "trade",
        "交易日 9:25-11:30/13:00-15:10（60s 周期）",
        "QM_LIVE_EQUITY_SAMPLER_ENABLED",
        True,
        300,
        None,
        "桥实时资产 + 每 agent 分账 → live_equity.jsonl（实况图数据源，旧栈停机后迁回）",
    ),
    JobSpec(
        "t1_unlock",
        "T+1 解锁",
        "worker",
        "trade",
        "60s 轮询（交易日 09:16 生效）",
        None,
        True,
        300,
        None,
        "次日补齐 available_volume",
    ),
    JobSpec(
        "corp_action",
        "公司行为处理",
        "worker",
        "trade",
        "60s 轮询",
        None,
        True,
        300,
        None,
        "分红送转到账（apply_due_actions）",
    ),
    JobSpec(
        "dual_book",
        "双轨对账（模拟↔真单数量）",
        "worker",
        "trade",
        "交易日 15:10（60s 轮询）",
        "MIRROR_RECONCILE_ENABLED",
        True,
        600,
        "python backend/scripts/schedule_ctl.py run dual_book --date YYYYMMDD",
        "模拟成交 vs 镜像真单数量差异，Redis mirror:reconcile:{date}",
    ),
    JobSpec(
        "mirror_shadow",
        "影子对照（模拟↔真单价格）",
        "worker",
        "trade",
        "交易日 15:15（60s 轮询）",
        "MIRROR_SHADOW_ENABLED",
        True,
        600,
        "python backend/scripts/schedule_ctl.py run mirror_shadow --date YYYYMMDD",
        "成交价偏差/成交率/滑点实现/跟踪误差，Redis mirror:shadow:{date}（T-P2-06）",
    ),
    JobSpec(
        "eval_scores",
        "评分六卡（EOD）",
        "worker",
        "trade",
        "交易日 16:00（60s 轮询）",
        "EVAL_SCORES_WORKER_ENABLED",
        True,
        600,
        "python backend/scripts/schedule_ctl.py run eval_scores --date YYYY-MM-DD",
        "因子/模型/策略/账户/实盘账户/每日选股六卡 → eval_scores 表（T-P4-05b）",
    ),
    JobSpec(
        "health_recheck",
        "月度体检复检",
        "worker",
        "trade",
        "每月首周（3600s 轮询）",
        "HEALTH_RECHECK_ENABLED",
        True,
        7200,
        "python backend/scripts/schedule_ctl.py run health_recheck --date YYYY-MM-DD",
        "SIM/LIVE 策略月度回测体检复检（T-P4-06 ③；结论退化告警）",
    ),
    JobSpec(
        "risk_tier",
        "风险档位定档（P1.8）",
        "worker",
        "trade",
        "交易日 09:10（300s 轮询；日键防重）",
        "QM_RISK_TIER_ENABLED",
        True,
        900,
        "python backend/scripts/schedule_ctl.py run risk_tier [--date YYYY-MM-DD]",
        "波动/回撤/情绪 → 当日档位 qm:risk:tier（闸门买入侧参数只收紧，tiers.py 消费）",
    ),
    JobSpec(
        "auto_inference",
        "自动推理",
        "celery_beat",
        "celery",
        "交易日 08:00",
        None,
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run auto_inference --date YYYY-MM-DD",
        "全市场信号生成（runner 级单实例锁保护，见 T-P1-02）",
    ),
    JobSpec(
        "market_sync_dispatch",
        "市场同步派发",
        "celery_beat",
        "celery",
        "每分钟",
        "MARKET_SYNC_SCHEDULE_ENABLED",
        True,
        600,
        "python backend/scripts/schedule_ctl.py run market_sync_dispatch",
        "按 Redis 同步调度配置派发各市场数据同步",
    ),
    # P1 滚动训练：beat 只做「到点判定 + HTTP POST 内部端点」，真正的训练提交在
    # API 进程（celery worker 无 docker.sock）。心跳每 tick 都写（月度任务若只在
    # 派发成功时写一次，C07 会误报 29 天 stale）。**默认关**：开启自动月度重训
    # 是显式运维动作；--force = 不看 enabled/到点/本期标记，立刻试一轮。
    JobSpec(
        "retrain_dispatch",
        "滚动重训派发",
        "celery_beat",
        "celery",
        "每分钟 tick（due 判定按配置）",
        "RETRAIN_SCHEDULER_ENABLED",
        False,
        1800,
        "python backend/scripts/schedule_ctl.py run retrain_dispatch",
        "滚动训练：窗口计算→内部派发→campaign 记账；mark-after-dispatch",
    ),
    # P2-4 融合权重周期刷新（设计《机构级模型融合》§7）：每日 22:30 扫全部融合
    # 模型按成员近窗 IC 重算权重，防抖阈值（0.02）下实际写盘呈周更节律。
    JobSpec(
        "fusion_refresh",
        "融合权重刷新",
        "celery_beat",
        "celery",
        "每日 22:30（防抖后实际周更）",
        "FUSION_REFRESH_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run fusion_refresh",
        "融合模型重算权重 → weight_snapshot.json（防抖；manual/equal 与缺员跳过）",
    ),
    # P2-5 值班摘要（trade 常驻，与收盘报表同侪）：全天七段盘点 → QQ + 通知面。
    # 心跳写在常驻循环里；它同时是死手检查核对的「15:40 done 键」生产者。
    JobSpec(
        "duty_summary",
        "值班摘要（P2-5）",
        "worker",
        "trade",
        "每交易日 15:40（60s 轮询）",
        "QM_DUTY_SUMMARY_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run duty_summary",
        "收盘报表/池/轮次/精确否决/镜像/跳发/实时信号七段盘点 → QQ；读不到的段如实标"
        "「不可读」，绝不渲染成 0",
    ),
    # 2026-10-12 用户拍板重规划：非持仓/大盘情报不再碎片化即时推，聚合到
    # 08:35 盘前 / 11:35 午间 / 15:15 盘后 三档卡片式速览。
    JobSpec(
        "intel_briefing",
        "QQ 情报速览（3 档）",
        "worker",
        "trade",
        "每交易日 08:35/11:35/15:15（60s 轮询）",
        "QM_QQ_BRIEFING_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run intel_briefing",
        "非持仓情报 Redis 缓冲 → 卡片式速览合集（利空/风险优先，15 行折叠）；"
        "只认最近一档（错过的档不补发），读失败不落 done 键下周期重试",
    ),
    # 通达信桥数据面采集（2026-10-15 批次 1）：盘后档位制全量采集
    # （pricevol/universe/more_info/go/gp/sc/bk；zdt 挂起），心跳写在常驻循环里。
    JobSpec(
        "tdx_ingest",
        "通达信桥数据采集",
        "worker",
        "trade",
        "每交易日 7 档（15:31/15:45/16:45/17:20/17:30/23:50/次日 06:30；60s 轮询）",
        "TDX_INGEST_ENABLED",
        True,
        3600,
        "python backend/scripts/schedule_ctl.py run tdx_ingest",
        "桥数据面全量落 PG + parquet 归档（_nc 绕缓存、令牌桶 180/min）；GP/BK/SC/GO "
        "家族待 Windows 数据包（blocked 不重试不告警），zdt 挂起；心跳 TTL 1h——"
        "防大档位（more_info 全 A ≈31min）被体检误判 stale",
    ),
    # P2-5 值班死手检查：**住 celery（跨进程树）**——trade 整体死亡时它还得能响。
    # 核对当日四类回执（报表/摘要/停滞检查/实时信号），缺失集收缩语义告警。
    JobSpec(
        "duty_deadman",
        "值班死手检查（P2-5）",
        "celery_beat",
        "celery",
        "每交易日 16:00~23:30（每 30min；非交易日任务体秒退）",
        "DUTY_DEADMAN_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run duty_deadman [--force]",
        "「该响没响」告警：四类回执缺失/不可读 → QQ；补齐发恢复并当日封账（--force 跳过"
        "非交易日闸门与封账键）",
    ),
    JobSpec(
        "news_enrich",
        "新闻富化",
        "celery_beat",
        "celery",
        "~30s",
        None,
        True,
        900,
        None,
        "Huntly 新闻入库富化",
    ),
    JobSpec(
        "news_matcher",
        "新闻匹配重载",
        "celery_beat",
        "celery",
        "分钟级",
        None,
        True,
        900,
        None,
        "新闻标题匹配器热重载",
    ),
    JobSpec(
        "news_tag_rollup",
        "新闻标签汇总",
        "celery_beat",
        "celery",
        "每 30 分钟",
        "NEWS_TAG_ROLLUP_ENABLED",
        True,
        5400,
        None,
        "近 20 天新闻标签（利空/利好）→ news_stock_tags，供候选列表与推送前风险排除",
    ),
    JobSpec(
        "strategy_lab_scan",
        "策略实验室日扫",
        "celery_beat",
        "celery",
        "交易日 23:00",
        "STRATEGY_LAB_SCAN_ENABLED",
        True,
        345600,
        None,
    ),
    JobSpec(
        "backfill_quality",
        "推理质量回填",
        "celery_beat",
        "celery",
        "每日 02:30",
        None,
        True,
        345600,
        None,
        "滞后 5 天回填真实收益算 Rank IC",
    ),
    # P1.6 执行损耗 TCA：纯读（成交×委托×决策账 → 报告落盘），故挂在 celery 而非
    # trade 常驻循环里——它不需要券商会话，也不需要与交易循环共享内存态。
    # 心跳 TTL 4 天（同 backfill_quality）：cron 是 mon-sat，周末最长间隔 48h。
    JobSpec(
        "tca_report",
        "执行损耗 TCA（P1.6）",
        "celery_beat",
        "celery",
        "周一至周六 16:40",
        "TCA_REPORT_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run tca_report",
        "成交损耗读数（基准=orders.ref_price，正号=比基准差）→ "
        "data/reports/tca/{date}_tca.{json,md}；补写前的老成交计「不可定价」不倒推基准",
    ),
    # 天风 50 因子日频物化：06:30/07:30/08:30/09:30 四个幂等重试点，脚本先查库内
    # 最大 dt，已最新就秒退。引擎与公式在 /data/quantcustom/factor_engines/tianfeng/。
    JobSpec(
        "tianfeng_daily",
        "天风因子日频物化",
        "celery_beat",
        "celery",
        "周一至周六 06:30~09:30（每小时，幂等）",
        "TIANFENG_DAILY_MATERIALIZE_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run tianfeng_daily",
        "重算最近 400 个交易日并覆盖 tianfeng_daily 分区；引擎在 "
        "/data/quantcustom/factor_engines/tianfeng/update_daily.py",
    ),
    # T-RC-14：两个关键交易循环此前不在册——它们挂掉时 C07 无项可判，
    # 表现为「体检全绿但策略不再调仓」。心跳写 ``qm:sched:hb:*``（db0）。
    JobSpec(
        "sim_hosted",
        "模拟盘托管调度",
        "worker",
        "trade",
        "30s 轮询（窗口内触发）",
        "ENABLE_SIMULATION_HOSTED_SCHEDULER",
        True,
        300,
        None,
        "读活跃快照→判定调仓日/时段→SimulationEngine.run_cycle（T-P3 托管）",
    ),
    JobSpec(
        "manual_execution",
        "手动执行任务消费",
        "worker",
        "trade",
        "1s 长轮询队列",
        None,
        True,
        300,
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
        "decision_round",
        "决策轮（P2.8）",
        "worker",
        "trade",
        "交易日 11 个槽位（08:30~14:45，30s 轮询、45min 补跑窗）",
        "QM_DECISION_ROUND_ENABLED",
        False,
        300,
        "python backend/scripts/schedule_ctl.py run decision_round --force "
        "[--agent <模型名>]",
        "池 → 闸门筛 → LLM 决策 → 执行段（下单/守护规则）→ 审计表；真钱生产者，默认关。"
        "重跑 = 抢占槽位再跑一轮 = 会真的再下单（同槽同向同标的被幂等键挡住）；"
        "开了名册（QM_DECISION_LLM_ROSTER）时一家模型一轮，只补一家加 --agent",
    ),
    # P2.6 减仓执行器：档位表 ``leverage_trim_to`` 的消费者（闸门的 ``l1.leverage_cap``
    # 只拒买、不压仓）。开关同样走 ``env_flags.env_flag``（**只有 "true" 生效**）。
    # 心跳写在 worker 常驻循环里；**默认关闭**——切流是 P5 的显式动作。
    JobSpec(
        "leverage_trim",
        "杠杆减仓执行器（P2.6）",
        "worker",
        "trade",
        "盘中 60s 一拍（``qm:risk:trim:config.interval_sec`` 可调）",
        "QM_LEVERAGE_TRIM_ENABLED",
        False,
        300,
        "python backend/scripts/schedule_ctl.py run leverage_trim --force",
        "总杠杆越过档位 ``leverage_max`` → 减到 ``leverage_trim_to``（整手/T+1/跌停/在途都复核）。"
        "真钱风控动作，默认关。重跑 = 立刻真减一次（当日幂等号与同标的尝试上限仍然生效）",
    ),
    # P0-3 训练僵尸作业回收器：宿主内存 OOM 血泪后的判尸机制。先探活（在管 task /
    # docker 容器 / PID 三探针）再判死——可验证存活的容器重挂监管，判死标 failed 且
    # **默认不自动重投**（防双跑）。住在 api 服务（启动即扫 + 每小时）。
    JobSpec(
        "training_reaper",
        "训练僵尸作业回收",
        "worker",
        "api",
        "API 启动即扫 + 每小时（``TRAINING_JOB_REAPER_INTERVAL_SECONDS`` 可调）",
        "TRAINING_JOB_REAPER_ENABLED",
        True,
        7200,
        "python backend/scripts/schedule_ctl.py run training_reaper",
        "重启/宕机后残留 pending/running 训练作业的判尸回收：在管或探针存活一律不动；"
        "判死 → 标 failed（reason 落日志）+ 释放训练单飞锁；重跑 = 立刻真扫真标",
    ),
    # P3 Regime 感知（设计 §6.2）：市场状态日表 qm_regime_daily 的日更。
    # 心跳 TTL 4 天（同 backfill_quality）：cron 是 mon-fri，周末最长间隔 72h。
    JobSpec(
        "regime_persist",
        "市场状态日更（P3）",
        "celery_beat",
        "celery",
        "交易日 16:30",
        "REGIME_PERSIST_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run regime_persist",
        "指数历史 → qm_regime_daily（生效日口径，历史行冻结）：每市场重算尾部 10 个"
        "生效日吸收迟到数据；表外市场诚实拒绝；重跑 = 立刻按日更形态再写一遍（0 新行即幂等）",
    ),
    # 收盘市场阶段快照：研究总控事实包 + 次日交易 agent 盘前参考块共用。
    # 23:30 在日线同步（实测 20:58 落分区）之后、夜间研究 02:10 之前。
    JobSpec(
        "market_regime_snapshot",
        "市场阶段快照（研究/交易 agent）",
        "celery_beat",
        "celery",
        "交易日 23:30",
        "MARKET_REGIME_SNAPSHOT_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run market_regime_snapshot [--date YYYY-MM-DD]",
        "六块评分/五层阶段/事件/情绪/Q1-Q5 → /data/market-analysis/regime/{date}.json；"
        "研究总控事实包与次日交易 agent 盘前参考块同源；日线未就绪时按实际最新日期落盘，"
        "文本写明日期，不冒充复盘日；重跑 = 同日覆盖",
    ),
    # 收盘情绪 QQ 提示：15:10 交易日一次，同日去重；只发手机提示，不进订单链路。
    JobSpec(
        "close_sentiment_alert",
        "收盘情绪 QQ 提示",
        "celery_beat",
        "celery",
        "交易日 15:10",
        "CLOSE_SENTIMENT_ALERT_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run close_sentiment_alert",
        "今日实时广度（qm:regime:intraday）+ 上一交易日六块/事件/情绪/Q1–Q5 → QQ 一条；"
        "官方情绪快照次日凌晨才产出，不冒充今日；同日去重，手动重跑当天不重复发",
    ),
    # 决策账定价回填：决策轮只写审计行，入场价/前向收益是另一条作业；不跑就永远
    # 没有 fwd/entry_px，按槽位/agent 的前向胜率也算不出来。
    JobSpec(
        "decision_ledger_scorecard",
        "决策账定价回填",
        "celery_beat",
        "celery",
        "交易日 07:10",
        "DECISION_LEDGER_SCORECARD_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run decision_ledger_scorecard",
        "qm_decision_ledger 补 entry_px + t1/t5/t20/t60 收益 + 形态标签（窗口 40 天，"
        "已定价行重算；price_row_monotone 只升不降，漏跑自动补）",
    ),
    # 心跳 TTL 4 天（同 tca/regime）：cron 是 mon-fri，周末最长间隔 72h。
    JobSpec(
        "factor_board",
        "因子概率板（市场分析）",
        "celery_beat",
        "celery",
        "交易日 08:40",
        "FACTOR_BOARD_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run factor_board [--date YYYY-MM-DD]",
        "精选因子（联合筛选 ∩ 近60日 IC 闸 × 族封顶）→ 合成分位板 + 行业/概念三层"
        "共振 + 四象限 → data/market-analysis/factor_board/{decision_date}.json；"
        "硬门不过退码 1 且不落盘；重跑 = 立刻按日更形态再构建一份（同日覆盖，"
        "--date 指定决策日=模型信号目标交易日，不给则取 PG 最新批量信号）",
    ),
    # P2-d 可交易性掩码增量刷新：收盘后 18:40 mon-fri；TTL 4 天（同 factor_board
    # 的周末 72h 逻辑）。产物供概率板名单过滤 + 因子报告可交易轨。
    JobSpec(
        "trade_mask_refresh",
        "可交易性掩码刷新（概率板/因子报告）",
        "celery_beat",
        "celery",
        "交易日 18:40（重扫最近约两周，区间替换合并）",
        "TRADE_MASK_REFRESH_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run trade_mask_refresh [--date YYYY-MM-DD]",
        "逐日扫盘面（涨跌停/停牌；ST 整票不入掩码）→ trade_mask.parquet 稀疏产物，"
        "段外历史原样保留（区间替换）；区间无交易日 no-op 退码 0；"
        "重跑 = 立刻再扫一遍（--date 指定重扫段尾，缺省=最近两周；确定性幂等）",
    ),
    # 资金流延续证据产物（市场分析 · 资金流全景 v2 §3/§8）：倍频轮询 + 任务体
    # 新鲜度门控（同 market_snapshot 的理由——同步时刻由用户「同步调度」配置，
    # 不假设固定时刻；产物已跟进库内最新 L2 分区即跳过）。心跳 TTL 4 天
    # （同 factor_board）：cron 是 mon-sat，周末最长间隔 72h。
    JobSpec(
        "flow_evidence",
        "资金流延续证据（市场分析）",
        "celery_beat",
        "celery",
        "mon-sat 19:00–23:40 + 次日 04:00–05:40 每 20 分钟轮询（新鲜度门控：产物已跟进最新分区即跳过）",
        "FLOW_EVIDENCE_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run flow_evidence",
        "板块主力净额「连N」全史条件统计（探针对照 + 可执行 T+1 收盘建仓双口径、"
        "闸门五项、动量分层）+ 当日命中榜 → latest.json / evidence.json / "
        "panel_sector.parquet（/data/market-analysis/flow_evidence）；校验不过"
        "退码 1 不落盘；重跑 = 立刻按日更形态全史再构建一份（同日覆盖）",
    ),
    # P2-f 因子概率板滚动重选：月频（beat 排每月 1–12 日工作日 08:50，任务体判
    # 月首交易日）。心跳 TTL 32 天：beat 每触发日都刷，但窗口外日子不跑任务体
    # ——留足「上月 12 日 → 本月首个工作日」的最大间隔（约 3 周）。
    JobSpec(
        "factor_board_reselect",
        "因子概率板滚动重选（P2-f）",
        "celery_beat",
        "celery",
        "应用月首个交易日 08:50（beat 1–12 日工作日，月首判据在任务体）",
        "FACTOR_BOARD_RESELECT_ENABLED",
        True,
        2764800,
        "python backend/scripts/schedule_ctl.py run factor_board_reselect [--date YYYY-MM-DD]",
        "前窗选（重跑选择管线，同窗 85/65）→ 应用月台本 rolling/{YYYY-MM}.json，"
        "并把上月台本 oos 段按后窗 RankIC 回填（前窗选后窗验）；非月首退码 0 跳过；"
        "重跑 = 立刻跑一次（--date 指定训练窗尾 as_of=历史回填；--force 绕过月首判据）",
    ),
    # 聪明钱产物（市场分析 · 聪明钱追踪与选股 细案 §7，2026-10-15）：东财晚间发布
    # 节奏不定，交易日 19:10 与 21:10 两档（第二档 catch-up，脚本幂等、重叠窗口
    # 自愈）。心跳 TTL 4 天（同 factor_board）：cron 是 mon-fri，周末最长间隔 72h。
    JobSpec(
        "smart_money",
        "聪明钱产物（市场分析）",
        "celery_beat",
        "celery",
        "交易日 19:10 与 21:10（第二档 catch-up，幂等重跑）",
        "SMART_MONEY_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run smart_money",
        "事件采集（龙虎榜/机构席位/大宗/调研/两融/北向）→ 事件池评分（入柱裁定见 "
        "evidence.json，负向柱方向对齐）→ latest.json/{date}.json/scores/（"
        "/data/market-analysis/smart_money）；重跑 = 立刻跳过采集按全量重算一份"
        "（同日覆盖原子写；--date 不适用——评分追随库内最新交易日）",
    ),
    # 港股聪明钱产物（市场分析 · 港股聪明钱深度分析 细案 §7，2026-10-16）：与 A 股
    # 聪明钱错峰 30 分钟（19:40/21:40），避免构建 IO 峰值叠加；构建只读本地
    # parquet（ccass_top50/hsgt_south/daily_forward 由港股市场同步链维护，本特性
    # 无独立采集脚本）。心跳 TTL 4 天（cron mon-fri，周末最长间隔 72h）。
    JobSpec(
        "hk_smart_money",
        "港股聪明钱产物（市场分析）",
        "celery_beat",
        "celery",
        "交易日 19:40 与 21:40（第二档 catch-up，幂等重跑）",
        "HK_SMART_MONEY_ENABLED",
        True,
        345600,
        "python backend/scripts/schedule_ctl.py run hk_smart_money",
        "增量事件仓（CCASS 5 类型差分 + 南向直读）→ 逐日复合分 → latest/"
        "consensus/trends（/data/market-analysis/hk_smart_money）；零入柱 = 真裁定"
        "（evidence-only 空态照常发布概览）；evidence 未裁定退码 2 拒绝出分"
        "（首建须先手动跑 validate，celery 如实转 failed）；重跑 = 立刻按全量重算"
        "一份（同日覆盖原子写；--date 不适用——评分追随库内最新交易日）",
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
    job_keys: tuple[str, ...] | list[str],
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


def classify_scheduler_status(
    entries: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
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
