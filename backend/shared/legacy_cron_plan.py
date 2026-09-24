"""P4 定时任务迁移的**归属分类表** —— 隔壁 crontab 每一条作业的唯一裁决处。

为什么要有这张表：隔壁 crontab 是宿主机上的**活物**，76 行里混着本仓作业、隔壁作业、
机器级守护与第三方（记忆网关/NAS）的作业。迁移的失效形态有两种，**方向相反**：

* 删多了 —— 本仓的盘后复盘、排除名单、决策池的供数一起停，且**当场没有信号**
  （cron 删掉一条不报错，脚本不跑也不报错）；
* 删少了 —— 隔壁的 LLM 决策/下单/守护继续在盘中对同一账户下单（与 QuantMind 抢同一
  座桥、同一个券商账户），这比不迁更危险。

所以每条作业都要**先判过归属**才能动手，判据落在本表，机器核对（``check_plan`` +
``legacy_cron_target.check_evidence``），不靠人记。

判定口径（六类，只有前两类会被删）：

===========================  ==================================================
``qm_covered``               本仓已接手，**必须给证据**（见下）
``retire_stop``              只服务隔壁自己的系统，随「全套下线」一起停
``must_keep``                删了断本仓的粮（含**传递依赖**：本仓决策上下文 ← 隔壁池 ←
                             隔壁新闻简报，断链是静默的）
``migrate``                  作业本体本属本仓，只是调度挂在隔壁 cron 上
``keep_as_is``               机器级/第三方资产，不随隔壁停，也不归本仓
``pending_decision``         有争议：删它会给本仓的交易链或机器级安全网留缺口 ——
                             工具不删，列出来给人裁决
===========================  ==================================================

证据分两级（``legacy_cron_target`` 会核对存在性并打印）：

* ``registry:<键>`` —— 本仓调度注册表 ``backend/shared/scheduler_registry.py`` 里的作业。
  工具还会报它的**开关状态**；本仓两个关键开关默认关（``QM_DECISION_ROUND_ENABLED``、
  ``QM_LEVERAGE_TRIM_ENABLED``），「已接手」≠「此刻在跑」，看错就是静默裸奔。
* ``code:<相对路径>:<符号>`` —— 代码落点。工具只能确认文件在、符号也在，**判不了它
  此刻是否在跑**（实盘循环族不在注册表里）⇒ 删前必须人工确认。

今天（2026-09-24）的判定结果：49 个身份 → 删 24（qm_covered 8 + retire_stop 16）、
留 25。**留的比删的多是正常的**：本仓的决策上下文现在还挂在隔壁的池/新闻/排除名单上，
那几条不是「忘了删」，是**不能删**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.shared.legacy_cron import JobKey
from backend.shared.legacy_cron_target import DISPOSITIONS, REMOVABLE

__all__ = [
    "ADDITIONS",
    "Entry",
    "PLAN",
    "PLAN_BY_KEY",
    "check_plan",
]


@dataclass(frozen=True)
class Entry:
    """一条作业的裁决。``script`` + ``args`` 就是身份（见 ``legacy_cron.JobKey``）。

    ``why`` 会原样打给操作者看 —— 它是「这条为什么删/为什么留」的唯一交代，
    写的时候当成给三个月后的自己看。
    """

    script: str
    disposition: str
    args: tuple[str, ...] = ()
    why: str = ""
    covered_by: str = ""
    manual_checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def job(self) -> JobKey:
        return JobKey(self.script, self.args)


# ── 隔壁的作业（会真下单的 8 条在头部，与迁移计划 P4 的表逐行对应）────────────
PLAN: tuple[Entry, ...] = (
    # ① 真单/决策族：本仓已接手
    Entry(
        "live_hourly_analysis.py",
        "qm_covered",
        why="盘中/盘前/尾盘再决策（L4/L9/L52 三次触发同一身份）。P2.8 决策轮逐槽位接手"
        "（0830/0900…1400/1445），提示词与 schema 同口径。",
        covered_by="registry:decision_round",
    ),
    Entry(
        "live_llm_trade.py",
        "qm_covered",
        args=("--execute",),
        why="LLM 主决策下单（L20，北京 09:35）。P2.8 槽位 0935（rebalance）接手；本仓多一道"
        "闸门层（P2.1b）与审计表（P2.1d）。",
        covered_by="registry:decision_round",
    ),
    Entry(
        "live_llm_trade.py",
        "qm_covered",
        args=("--execute", "--catch-up"),
        why="调仓补跑（L58，北京 10:05/11:05）。P2.8 槽位 1005/1105（catch_up）接手；本仓多一条"
        "「当日该 schema 已出过决策则跳过」（done 键）。",
        covered_by="registry:decision_round",
    ),
    Entry(
        "live_price_watch.py",
        "qm_covered",
        why="守护条件单执行器（L24，盘中每分钟）—— 会真下单。P1.3 起由本仓 sltp 规则表 + "
        "执行器接手（逐项覆盖见迁移计划「结论一」）。",
        covered_by="code:backend/services/live_trading/services/sltp_executor.py:run_qmt_sltp_executor_task",
    ),
    Entry(
        "leverage_guard.py",
        "qm_covered",
        why="独立杠杆守护（L30，超 1.5× 权益自动减仓）—— 会真下单。P2.6 起由本仓 "
        "leverage_trim 常驻 worker 接手（生效上限 = min(配置, 档位)）。",
        covered_by="registry:leverage_trim",
    ),
    Entry(
        "live_l2_capture.py",
        "qm_covered",
        why="L2 因子采集/收盘定格（L25/L26）。本仓 TdxAiData 通道侧采集接手"
        "（同族还有 l2 实时面）。",
        covered_by="code:backend/services/live_trading/services/tdx_l2_capture_task.py:run_tdx_l2_capture_task",
        manual_checks=(
            "本仓 L2 采集是 trade 服务内的循环，**不在调度注册表里**（注册表只覆盖 celery 侧），"
            "工具判不了它此刻是否在跑：删 L25/L26 之前请确认本仓采集真在写（看当日 L2 落盘/状态键）。",
        ),
    ),
    Entry(
        "risk_budget_agent.py",
        "qm_covered",
        why="盘前风险预算定档（L36，北京 09:10）。本仓 `risk_tier` 就是它移植过来的"
        "（`backend/shared/risk/tiers.py` 模块头写明移植来源，输入三项一一对应、定档时点同点）。",
        covered_by="registry:risk_tier",
    ),
    Entry(
        "preflight_bridge.py",
        "qm_covered",
        why="盘前健检（L37：账户通道/台账对账/执行开关/条件位）。**四项并非一一对应**，"
        "本仓拆到了不同落点：①账户通道 ⇒ 端点内 `bridge_account_channel` 行（2026-09-24"
        "补，见 manual_checks）；②台账对账 ⇒ 隔壁对的是 live_ledger 分账台账，本仓在"
        "影子账/决策台账那条链上（`backend/scripts/decision_ledger.py` 一带），"
        "**不在这条端点里**；③执行开关 ⇒ 本仓没有 intraday_exec.json 那种「哨兵只打印"
        "不真卖」的独立开关，能不能真卖由总闸门 `ENABLE_REAL_TRADING` + 止盈止损规则表"
        "活性共同决定；④条件位清单 ⇒ P5 步骤 3 迁移工具"
        "（`backend/scripts/migrate_legacy_watch.py`）已把 live_watch.json 搬进本仓规则表。"
        "端点本体：`GET /api/v1/real-trading/preflight`（含日快照），由开户/开闸流程消费。",
        covered_by="code:backend/services/live_trading/routers/real_trading_preflight.py:preflight_check",
        manual_checks=(
            "本仓 preflight 是**按需端点**（开实盘/盘前人工触发 + 日快照），不是 cron 作业；"
            "隔壁那条是每天 09:12 无人值守自动跑。删它 = 少一层无人值守的盘前体检，"
            "请确认你接受「改为需要时才触发」。",
            "2026-09-24 复核：原先这条 `qm_covered` 属**过度声明** —— 当时端点里唯一探桥的是"
            "`check_tdx_bridge_online`（只读桥 health），而 health 的 tdx_connected 来自"
            "`health_check_fast`（发的 `get_match_stkinfo` 是**行情类**查询），交易端掉线时"
            "照样为真 ⇒ 结构上判不出 L37 的头号故障「桥假活」（2026-09-10 资产恒 0 与"
            "2026-09-24 查询超时各赔过一天）。已补 `check_bridge_account_channel`：真查一次"
            "`POST /api/v1/account/query`，「报错」与「200 但资产恒 0/读不出」两种形态都判"
            "不通过，并在两个 REAL 闸门（`/preflight` 的 ready、`/trading-precheck` 的"
            "passed）各注册独立一行 `bridge_account_channel`。**台账对账那一项仍未覆盖**"
            "（见 why ②）。",
        ),
    ),
    # ② 本仓的供数链：删了断粮（含传递依赖）
    Entry(
        "night_pool_agent.py",
        "must_keep",
        why="晚间市场研究（L33）。它把候选池写进**本仓** `data/reports/stock_picks/"
        "{date}_agent_picks.json`（09:35 消费的就是当日池），本仓 "
        "`backend/shared/decision_context_source.py` 已接这个目录 ⇒ 删了本仓决策上下文断粮。",
    ),
    Entry(
        "night_pool_agent.py",
        "must_keep",
        args=("--if-missing",),
        why="上一条的幂等补跑（L34，目标日池已存在则退出），覆盖 quantdb 分区同步延迟。",
    ),
    Entry(
        "hypothesis_lab.py",
        "must_keep",
        args=("--symbols", "80", "--days", "260"),
        why="假设库周度复测（L35）。本仓技能 `a-share-hypothesis-lab` 调**同一份实现**，"
        "且它回写的 `configs/hypotheses.json` 是提示词输入（迁移后仍要用）。",
    ),
    Entry(
        "event_radar.py",
        "must_keep",
        args=("collect",),
        why="事件雷达（L43，解禁/质押/回购）。**有副作用**：调 `risk_list.refresh()` 重建 "
        "`risk_block.json`，归一进本仓 `data/exclusions/cn.json` ⇒ 删了本仓排除名单断粮。",
    ),
    Entry(
        "<compound>",
        "must_keep",
        args=("fundamental_flags.py", "industry_risk.py"),
        why="L45 复合行：基本面劣化扫描 + 行业风险榜。前半喂本仓排除名单（"
        "`backend/shared/exclusion_list.py` 把 `fundamental_flags` 列为来源之一，约 1600 只，"
        "`data/fundamental_flags.json` 靠它日更）⇒ 删了本仓排除名单静默变陈旧；"
        "后半（industry_risk）只喂隔壁提示词。",
        manual_checks=(
            "复合行拆不得（工具只删行、不改写）：停隔壁后建议人工把它拆成只跑 "
            "`fundamental_flags.py build` 一条。",
        ),
    ),
    Entry(
        "news_brief.py",
        "must_keep",
        why="新闻简报（L47/L48/L49 三次触发同一身份）。它是**传递依赖**：产出落在隔壁 "
        "`data/news_brief/`，而 `night_pool_agent._news_block()` 读它拼提示词 ⇒ 停它 = "
        "本仓决策池的新闻块**静默变空**（脚本自己写的是「休市窗口为空」，不报错）。",
    ),
    Entry(
        "news_review.py",
        "must_keep",
        why="新闻晚间复盘（L50）。产出 `data/news_brief/reviews/{day}.json`，同样被 "
        "`night_pool_agent._news_block()` 读取（同上传递依赖）。",
    ),
    # ③ 本仓自己的作业，只是调度挂在隔壁 cron 上
    Entry(
        "postmarket_pipeline.py",
        "migrate",
        why="本仓盘后复盘（L18，JST00:00=北京23:00）。脚本在本仓 `scripts/`。",
    ),
    Entry(
        "postmarket_pipeline.py",
        "migrate",
        args=("--picks-only", "--wait-l2-min", "30"),
        why="本仓明日备选（L19，JST00:35=北京23:35）。",
    ),
    Entry(
        "alpha_library_refresh.sh",
        "migrate",
        why="本仓 alpha_library 因子库夜间全量刷新（L59）—— 数据集与脚本都属本仓。",
    ),
    # ④ 机器级/第三方：不随隔壁停，也不归本仓
    Entry(
        "switch-gateway.sh",
        "keep_as_is",
        args=("off",),
        why="TDAI 记忆网关收窗（L5/L8/L10）。第三方资产，与隔壁交易系统无关。",
    ),
    Entry(
        "switch-gateway.sh",
        "keep_as_is",
        args=("on",),
        why="TDAI 记忆网关开窗（L6/L11/L12）。",
    ),
    Entry(
        "<compound>",
        "keep_as_is",
        args=("reboot-resume-gateway.sh",),
        why="记忆网关重启续跑（L17，@reboot 后 sleep 90 再拉）。",
    ),
    Entry(
        "ensure_sata_mount.sh",
        "keep_as_is",
        why="NAS sata 挂载自愈看门狗（L62）。机器级挂载守护，与隔壁交易系统无关。",
        manual_checks=(
            "它守的容器 `l2-sata-fuse` 已不存在（2026-09-24 实测）：脚本还在跑，但它守的对象"
            "可能已经换人接手 —— 要么修脚本，要么确认这条不再需要。",
        ),
    ),
    # ⑤ 机器级安全网：删了会给「本仓之外的东西」留缺口 ⇒ 交人裁决
    Entry(
        "alert.sh",
        "pending_decision",
        why="告警检查（L13，每 5 分钟）：服务掉线/交易停滞/净值冻结/**备份过期/磁盘占用**。"
        "它检查的对象大半是隔壁的服务，但**机器级磁盘告警目前只有这一份**"
        "（本仓 diagnose/health.py 的磁盘检查是死代码）。停隔壁后它会开始报假警。",
        manual_checks=(
            "留着它 = 每天报「隔壁服务掉线 / 备份过期」的假警；停了它 = 机器级磁盘告警归零。"
            "二选一必须有人做：把磁盘检查摘出来另挂，或者接受没有磁盘告警。",
        ),
    ),
    Entry(
        "auto-heal.sh",
        "pending_decision",
        why="容器自愈（L15，每分钟拉起 mcp×3/api/dsh/ui-arena）。**自愈对象不全是隔壁的**："
        "`baymax-dsh` 上是 QuantBot（dsh 平台），`baymax-ui-arena` 供的 arena 已整棵移植进"
        "本仓实盘栏（数据仍读隔壁 8091）—— 停它 = 这两块掉线后没人拉起。",
        manual_checks=(
            "「全套下线」如果**包含** dsh/arena，这条应转 retire_stop；如果不包含，"
            "必须先给出替代的自愈手段再停。这个边界只有用户能定。",
        ),
    ),
    Entry(
        "status-probe.sh",
        "pending_decision",
        why="宿主侧服务探活（L16，每分钟）写 `logs/service_status.json`。探的端口含 "
        "`dsh:3081`（QuantBot 宿主）与 `api:8091`（arena 数据源），都不只服务隔壁。",
        manual_checks=("与 auto-heal.sh 同一个边界问题：dsh/arena 是否随隔壁下线。",),
    ),
    Entry(
        "host_watchdog.py",
        "pending_decision",
        why="主机存活看门狗（L73，每分钟）：离线/冻结 → QQ 告警。**机器级安全网，本仓无等价物**"
        "（本仓有延迟/新鲜度打点，但没有人看主机本身）。",
    ),
    Entry(
        "<compound>",
        "pending_decision",
        args=("host_watchdog.py",),
        why="上一条的重启补报（L74 是 @reboot 复合行：sleep 90 && host_watchdog.py --boot）："
        "报出重启期间的离线窗口。",
    ),
    Entry(
        "bridge_watch.py",
        "pending_decision",
        why="桥哨兵（L72，盘中每分钟）：断线/恢复即时 QQ 推送。**桥在本仓接手后还要继续用**"
        "（QMT 真单走同一座桥），本仓没有等价的即时告警。",
    ),
    Entry(
        "premarket_win_probe.py",
        "pending_decision",
        why="盘前 Win/桥在线探测 + QQ 通知（L66，北京 09:18）。桥要活到本仓接手之后，"
        "本仓无等价物。",
    ),
    Entry(
        "push_daily_briefs.py",
        "pending_decision",
        args=("morning",),
        why="QQ 定时简报·早盘（L68，北京 09:28）。**面向人的功能**，本仓无等价物。",
    ),
    Entry(
        "push_daily_briefs.py",
        "pending_decision",
        args=("postreview",),
        why="QQ 定时简报·复盘（L69，北京 15:45）。",
    ),
    Entry(
        "push_daily_briefs.py",
        "pending_decision",
        args=("evening",),
        why="QQ 定时简报·选股（L70，JST00:40=北京 23:40）。",
    ),
    Entry(
        "log_cleanup.py",
        "pending_decision",
        args=("--days", "30"),
        why="日志治理（L39，周日）：清 `/home/zbox/baymax/logs` 30 天前的日志。机器级卫生，"
        "本仓无等价物（本仓 logs/ 同样会长）。",
    ),
    # ⑥ 只服务隔壁自己的系统：随「全套下线」一起停
    Entry(
        "backup.sh",
        "retire_stop",
        why="每日备份（L7）：只打包隔壁自己的 data/configs/.env 到 /home/zbox/backups/baymax。"
        "被备份的是冻结的树，且待迁数据已由 P3 迁完。",
        manual_checks=(
            "停它之后 alert.sh 的「备份过期」检查会开始报警（见 alert.sh 那条）。",
        ),
    ),
    Entry(
        "live_hourly_analysis.py",
        "retire_stop",
        args=("--record-only",),
        why="净值采样（L14/L51 每分钟）：为隔壁 record_window 收盘定格供数，本身不下单。"
        "本仓有自己的记账/结算链路，不消费这个采样。",
    ),
    Entry(
        "live_llm_trade.py",
        "retire_stop",
        args=("--shadow-context",),
        why="隔壁 P1 影子 A/B 采样（L75，只记录不下单）：采的是隔壁提示词的上下文，"
        "消费者是 shadow_context_report.py（同批停）。",
    ),
    Entry(
        "shadow_context_report.py",
        "retire_stop",
        args=(
            "--days",
            "7",
            "--out",
            "/home/zbox/baymax/logs/shadow_context/report_latest.md",
            "--push",
        ),
        why="隔壁 P1 影子 A/B 周报（L76）。样本来自上一条，两者同生共死。",
    ),
    Entry(
        "replay_deferred.py",
        "retire_stop",
        why="延期单重放（L29，盘中每分钟）：桥断链拒单恢复后自动补执行 —— 会真下单。"
        "它是隔壁桥队列的配套件；本仓的幂等键（P2.3b）取代了它的去重作用，且本仓无此队列。",
        manual_checks=(
            "**这条是本批里最需要盯的**：它重放的是「桥拒过的单」。确认本仓的幂等键 + 桥侧"
            "行为能兜住「拒单后不再重放」的语义（即拒单就是拒单，不会第二天自己回来）。",
        ),
    ),
    Entry(
        "analysis_trigger_worker.py",
        "retire_stop",
        why="隔壁前端「立即分析」按钮的队列 worker（L31，每分钟）。隔壁 UI 下线即无消费者。",
    ),
    Entry(
        "post_review.py",
        "retire_stop",
        why="隔壁盘后复盘 agent（L32，北京 15:35）。本仓有 `postmarket_pipeline.py`（盘后复盘）"
        "与 daily-review 技能。",
    ),
    Entry(
        "daily_report_agent.py",
        "retire_stop",
        why="隔壁系统运行日报（L38，北京 17:00）：报的是隔壁服务自己的运行状况。",
    ),
    Entry(
        "hk_picks.py",
        "retire_stop",
        why="港股候选池（L21）：写隔壁 `data/hk_picks.json`，只有隔壁提示词消费"
        "（本仓全仓检索无消费者）。",
        manual_checks=(
            "**这是一项能力缺口**：本仓多市场平台目前没有 LLM 决策轮消费港股候选池。"
            "若日后要开港股 AI 决策，需要在本仓补一个等价池生产者。",
        ),
    ),
    Entry(
        "us_picks.py",
        "retire_stop",
        why="美股候选池（L27）：写隔壁 `data/us_picks.json`，本仓无消费者（同上）。",
    ),
    Entry(
        "decision_track.py",
        "retire_stop",
        args=("--backfill", "--update", "--report"),
        why="隔壁决策远期记分卡（L53）。它的收益口径唯一读 `daily_backward`，而本仓已判该数据集"
        "损坏（逐分区拼接缝上长假跳变、20 日窗 54.5% 标的差 >1pp）⇒ 它的记分**不可导入本仓**。",
    ),
    Entry(
        "rt_probe.py",
        "retire_stop",
        why="隔壁实时行情源健康看板（L41）。本仓有 freshness/延迟打点（P6-05）作为等价观测面。",
    ),
    Entry(
        "min_snapshot.py",
        "retire_stop",
        why="盘中富字段快照采样器（L42，TdxAiData 备胎）：写隔壁自己的快照，供隔壁提示词。"
        "本仓 P6 的 L0.5 热集快照是独立链路。",
    ),
    Entry(
        "ops_worker.py",
        "retire_stop",
        why="隔壁 ops 远程重启执行器（L40，每分钟）：执行隔壁 UI 下发的重启指令。"
        "隔壁 UI 下线即无指令来源。",
    ),
    Entry(
        "pine_transpile_worker.py",
        "retire_stop",
        why="隔壁 Pine 策略库转写/回测队列（L54，宿主侧 bwrap 沙箱）。",
    ),
    Entry(
        "pine_chat_worker.py",
        "retire_stop",
        why="隔壁 Pine 策略对话队列（L55，宿主侧调模型）。",
    ),
)


def _validate(plan: tuple[Entry, ...]) -> dict[JobKey, Entry]:
    """导入即自检：分类表自己写错（错类、缺证据、重复身份）必须在 import 时炸，
    不能等跑迁移时才发现。"""
    by_key: dict[JobKey, Entry] = {}
    for e in plan:
        if e.disposition not in DISPOSITIONS:
            raise ValueError(
                f"{e.job}: 未知处置 {e.disposition!r}（可选：{sorted(DISPOSITIONS)}）"
            )
        if e.disposition in REMOVABLE and not e.why:
            raise ValueError(f"{e.job}: 可删的处置必须写 why（报告要原样打给人看）")
        if e.disposition == "qm_covered" and not e.covered_by:
            raise ValueError(
                f"{e.job}: qm_covered 必须给证据（registry:<键> / code:<路径>:<符号>）"
            )
        if e.disposition != "qm_covered" and e.covered_by:
            raise ValueError(
                f"{e.job}: 只有 qm_covered 才写 covered_by（{e.covered_by!r}）"
            )
        if e.job in by_key:
            raise ValueError(f"{e.job}: 身份重复 —— 同一身份只能有一条裁决")
        by_key[e.job] = e
    return by_key


PLAN_BY_KEY: dict[JobKey, Entry] = _validate(PLAN)


def check_plan() -> dict[str, int]:
    """按处置计数（报告头部用）。"""
    counts: dict[str, int] = {}
    for e in PLAN:
        counts[e.disposition] = counts.get(e.disposition, 0) + 1
    return counts


# ── 迁移新增（隔壁没有对应行）────────────────────────────────────────────────
# P4 补：P1.6 影子账的日更。三个子命令原先只能手跑 —— **观测层不跑就是死层**
# （影子代价账的价值在趋势：`ok` 样本够了才谈保留/放宽/删除，单日快照没有意义）。
#
# 为什么走容器（`docker exec`）而不是宿主 python：宿主 `/usr/bin/python3` 没有
# `redis` 包，而 `extract` 要读 trade 库的留痕（`_redis()` 的 import 在 try 之外，
# 缺包是运行时崩），装了也没有 REDIS_HOST/DB 环境 —— 宿主直跑 = 静默死作业。
# 容器里依赖、环境、落点三件都现成：`QM_REPORTS_DIR=/data/reports` 让 `report`
# 落进宿主 `data/reports/risk_ghost/`（实测 2026-09-24 跑通，报告已留档）。
#
# 时刻：JST 00:45/00:50/00:55 = 北京 23:45/23:50/23:55，接在 postmarket_pipeline
# （JST 00:00/00:35）之后 —— 那天收盘的行情与留痕都已落盘。`report` 的 `--as-of`
# 默认取 CST 今日，此刻正是刚收盘的那个交易日。日期段 `2-6`（JST 周二~周六）
# 对应北京周一~周五，与隔壁 postmarket 两条同口径。
#
# 三条分开不串 `&&`：各自的退出码分开留痕，且 `extract` 失败时 `report` 仍能把
# 现有台账留档（定价/入账都是幂等的，重跑不放大）。
ADDITIONS: tuple[str, ...] = (
    "45 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3"
    " /app/backend/scripts/risk_ghost_ledger.py extract --days 7 --apply"
    " >> /home/zbox/projects/quantmind/logs/risk_ghost_ledger.log 2>&1"
    "  # 影子账日更①留痕→台账（JST00:45=北京23:45；--days 7 是重跑安全窗）",
    "50 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3"
    " /app/backend/scripts/risk_ghost_ledger.py price --apply"
    " >> /home/zbox/projects/quantmind/logs/risk_ghost_ledger.log 2>&1"
    "  # 影子账日更②事后定价（单调合并，ok 不被降级）",
    "55 0 * * 2-6 /usr/bin/docker exec -w /app quantmind python3"
    " /app/backend/scripts/risk_ghost_ledger.py report"
    " >> /home/zbox/projects/quantmind/logs/risk_ghost_ledger.log 2>&1"
    "  # 影子账日更③每日留档（--as-of 默认 CST 今日 → /data/reports/risk_ghost）",
)
