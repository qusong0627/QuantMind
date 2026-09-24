#!/usr/bin/env python3
"""调度控制台（T-P1-06）：查看调度表 / 手动重跑。

用法（容器内）:
    python backend/scripts/schedule_ctl.py list
    python backend/scripts/schedule_ctl.py run sim_eod --date 2026-09-15 [--force]
    python backend/scripts/schedule_ctl.py run auto_inference --date 2026-09-14 [--force]
    python backend/scripts/schedule_ctl.py run market_sync_dispatch [--force]

设计：
- 任务清单唯一事实源 = backend/shared/scheduler_registry.py（禁止在本脚本另列任务）；
- `run` 只支持注册表里声明了 `rerun` 的任务（分发表 _RERUN_DISPATCH），未知/不支持 → 退出码 2；
- `--force`：**语义按任务而定**（见 `_force_notice`）——多数任务不看它，
  `decision_round` 看它并且是真钱语义（抢占槽位、会真的再下一批单）。
  出口只说该任务真实会发生什么，不含糊、不造假语义。

退出码：0 成功 / 1 执行失败 / 2 参数或任务不支持。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Callable

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

from backend.shared.scheduler_registry import (  # noqa: E402
    JOBS,
    JOBS_BY_KEY,
    heartbeat_key,
    switch_enabled,
)

_STATE_LABEL = {
    "ok": "✓ 新鲜",
    "stale": "✗ 过期",
    "off": "· 关闭",
    "missing": "! 无记录",
}


def _redis_client():
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client

    return get_redis_sentinel_client()


def cmd_list() -> int:
    redis = _redis_client()
    now_ts = time.time()
    print(f"{'KEY':<22}{'名称':<12}{'归属':<9}{'周期':<26}{'开关':<8}{'心跳'}")
    for spec in JOBS:
        enabled = switch_enabled(spec)
        age = None
        try:
            raw = redis.get(heartbeat_key(spec.key))
            if raw is not None:
                age = int(now_ts - float(raw))
        except Exception:  # noqa: BLE001
            pass
        if spec.heartbeat_ttl is None:
            state = "· 未接线"
        elif not enabled:
            state = "· 关闭"
        elif age is None:
            state = "! 无记录"
        elif age <= spec.heartbeat_ttl:
            state = f"✓ 新鲜（{age}s）"
        else:
            state = f"✗ 过期（{age}s > {spec.heartbeat_ttl}s）"
        print(
            f"{spec.key:<22}{spec.name:<12}{spec.owner:<9}{spec.schedule:<26}"
            f"{'on' if enabled else 'OFF':<8}{state}"
        )
    print("\n可手动重跑：", ", ".join(k for k, s in JOBS_BY_KEY.items() if s.rerun))
    return 0


def _run_sim_eod(date_str: str | None, force: bool) -> int:
    import asyncio
    from datetime import date

    from backend.services.simulation.services.eod_service import _execute_eod

    target = date.fromisoformat(date_str) if date_str else datetime_now_shanghai_date()

    ok = asyncio.run(_execute_eod(target))
    print(f"sim_eod {target}: {'完成' if ok else '未完成（见容器日志）'}")
    return 0 if ok else 1


def datetime_now_shanghai_date():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _run_inference(date_str: str | None, force: bool) -> int:
    import asyncio
    from datetime import date, timedelta

    from backend.services.engine.inference.router_service import InferenceRouterService

    # data_trade_date：默认取最近一个工作日（信号按 T+1 生效日落库，与调度口径一致）
    if date_str:
        data_date = date.fromisoformat(date_str).isoformat()
    else:
        probe = datetime_now_shanghai_date()
        while probe.weekday() >= 5:
            probe -= timedelta(days=1)
        data_date = probe.isoformat()

    service = InferenceRouterService()
    result = asyncio.run(
        service.run_daily_inference_script(
            date=data_date,
            tenant_id="default",
            user_id="system",
            redis_client=_redis_client(),
        )
    )
    print(
        f"inference data_date={data_date}: success={result.success} "
        f"run_id={getattr(result, 'run_id', '')} signals={getattr(result, 'signals_count', '?')} "
        f"reason={getattr(result, 'fallback_reason', '') or '-'}"
    )
    return 0 if result.success else 1


def _run_data_sync(date_str: str | None, force: bool) -> int:
    script = os.path.join(PROJECT_ROOT, "backend", "scripts", "quantdb_daily_sync.py")
    print(f"执行 {script}（输出直通）...")
    completed = subprocess.run([sys.executable, script], check=False)
    return 0 if completed.returncode == 0 else 1


def _run_dual_book(date_str: str | None, force: bool) -> int:
    import asyncio

    from backend.services.trade.services.dual_book_reconciliation_task import (
        run_dual_book_reconciliation,
    )
    from backend.services.trade_shared.deps import get_redis

    # 与 worker 同一客户端（报表统一落 trade 库；sentinel 客户端形态不含 .client）
    report = asyncio.run(run_dual_book_reconciliation(get_redis(), date_str))
    # 跳过与真单失败分开读：前者是「决定不发」，后者是「发了没成」
    print(
        f"dual_book {report.get('date')}: 差异={len(report.get('diffs') or [])} "
        f"未解释={len(report.get('unexplained') or [])} "
        f"跳过={report.get('skip_events')} 真单失败={report.get('failure_events')} "
        f"ok={report.get('ok')}"
    )
    return 0 if report.get("ok") else 1


def _run_shadow_compare(date_str: str | None, force: bool) -> int:
    import asyncio

    from backend.services.trade.services.shadow_compare_service import (
        run_shadow_compare,
    )
    from backend.services.trade_shared.deps import get_redis

    report = asyncio.run(run_shadow_compare(get_redis(), date_str))
    coverage = report.get("coverage") or {}
    print(
        f"mirror_shadow {report.get('date')}: 配对={coverage.get('matched')} "
        f"仅模拟={coverage.get('sim_only')} 仅真单={coverage.get('real_only')} "
        f"ok={report.get('ok')}"
    )
    return 0 if report.get("ok") else 1


def _run_eval_scores(date_str: str | None, force: bool) -> int:
    import asyncio

    from backend.scripts.eval.run_all import run_all
    from backend.shared.database_manager_v2 import close_database

    async def _run():
        try:
            return await run_all(date_str, save=True)
        finally:
            await close_database()

    summary = asyncio.run(_run())
    print(
        f"eval_scores {summary.get('date')}: 落分={summary.get('total_scored')} "
        f"异常={summary.get('total_errors')} 耗时={summary.get('elapsed_sec')}s"
    )
    return 0 if summary.get("total_scored") else 1


def _run_sentinel_backfill(date_str: str | None, force: bool) -> int:
    from backend.services.trade.services.sentinel_backfill import (
        backfill_pending,
        main as _service_main,
    )

    if not date_str:
        return _service_main()
    from datetime import date

    stats = backfill_pending(limit=1000, today=date.fromisoformat(date_str))
    print(f"sentinel_backfill {date_str}: {stats}")
    return 0


def _run_advice_backfill(date_str: str | None, force: bool) -> int:
    from backend.services.trade.services.advice_backfill import (
        backfill_once,
        main as _service_main,
    )

    if not date_str:
        return _service_main()
    from datetime import date

    stats = backfill_once(limit=1000, today=date.fromisoformat(date_str))
    print(f"advice_backfill {date_str}: {stats}")
    return 0


def _run_advice_generator(date_str: str | None, force: bool) -> int:
    # 建议卡按「当日信号」生成，date_str 语义不适用（服务内部取最新交易日），
    # 传了也不假装生效——直接说明后按默认口径跑，避免造出「指定日期」的假象。
    if date_str:
        print(
            f"提示：advice_generator 不接受日期参数（收到 {date_str}），按默认口径执行"
        )
    from backend.services.trade.services.advice_generator import main as _service_main

    return _service_main()


def _run_decision_round(date_str: str | None, force: bool, agent: str = "") -> int:
    """跑一次决策轮（P2.8）：默认按时刻表跑到点槽位，``--force`` 抢占槽位重跑。

    ``date_str`` 语义不适用（一轮的提示词、账户额度、池文件与槽位判定**全取当下**，
    回补某一天需要的是那天的池文件与账，不是参数），传了也不假装生效。真要补跑某个
    槽位用 ``--slot HHMM``——那才是本任务的重跑语义；控制台入口跑的是时刻表，因为
    「按下重跑」时用户要的是「现在这一轮别漏」，而不是复现 08:30。

    ``force`` 进来的含义比其他任务重：本任务是**真钱生产者**，这里的 ``--force``
    不是「重算一遍报告」，而是「抢占槽位、再问一次模型、可能再下一批单」（同槽同
    标的同向腿被幂等键挡住，模型给出新意图则照下）。所以它只在控制台的显式重跑
    路径上传下去，不影响 worker 的自动 tick。

    ``agent``（P2.9）：名册下只重跑点名的那一家。校验在 runner 里（点名不在名册里
    = rc 2）——**在这里放行等于把「补一家」变成「三家都下单」**，所以本任务只转手，
    绝不吞掉这个参数。
    """
    if date_str:
        print(f"提示：decision_round 不接受日期参数（收到 {date_str}），按当下槽位执行")
    # 驱动层（runner）持有 CLI；调度层 decision_round_tick 只有 round_tick，
    # 编排层 decision_round 只有 run_once。
    from backend.services.trade.services.decision_round_runner import (
        main as _service_main,
    )

    argv = ["--force"] if force else []
    if agent:
        argv += ["--agent", agent]
    return _service_main(argv)


def _run_leverage_trim(date_str: str | None, force: bool) -> int:
    """跑一轮减仓（P2.6）：**会真的提交减仓委托**（除非环境/档位/账户任一不满足）。

    ``date_str`` 语义不适用（一轮的档位、账户、行情全取当下）。``--force`` 只解除
    ``paused`` 软开关——交易时段、实盘闸门、券商选定三道判据照旧，因为「现在是不是
    能下单」不该由操作员的按钮决定。重跑不是幂等空转：真减一次，但同一个当日单号
    只提交一次、同标的当日失败到顶即停手（防废单死循环）。
    """
    if date_str:
        print(f"提示：leverage_trim 不接受日期参数（收到 {date_str}），按当下账户执行")
    from backend.services.trade.services.leverage_trim_runner import (
        main as _service_main,
    )

    return _service_main(["--once", "--force"] if force else ["--once"])


def _run_risk_tier(date_str: str | None, force: bool) -> int:
    """手动定档（P1.8 生产者）：直接算一次并按当日口径写入，不走 worker 的日键。

    重跑语义：与 worker 的日键无关（本入口不查、不置日键），但 `resolve_level` 的
    同日防抖仍然生效——重跑只可能复算或**收紧**，不会把当天已定的档位放宽。
    """
    import asyncio
    from datetime import date

    from backend.services.trade.services.risk_tier_producer import run_tier_decision

    target = date.fromisoformat(date_str) if date_str else None
    result = asyncio.run(run_tier_decision(today=target))
    print(
        f"risk_tier {result.get('date')}: level={result.get('level')} "
        f"({result.get('label')}) source={result.get('source')} "
        f"reasons={'·'.join(result.get('reasons') or []) or '(无)'}"
    )
    return 0 if result.get("ok") else 1


def _run_health_recheck(date_str: str | None, force: bool) -> int:
    import asyncio

    from backend.scripts.eval.health_recheck import run_health_recheck
    from backend.shared.database_manager_v2 import close_database

    async def _run():
        try:
            return await run_health_recheck(save=True)
        finally:
            await close_database()

    summary = asyncio.run(_run())
    print(
        f"health_recheck {summary.get('date')}: 策略={summary.get('n_strategies')} "
        f"复检={summary.get('ok')} 跳过={summary.get('skipped')} "
        f"告警={summary.get('alerts')} 异常={len(summary.get('errors') or [])}"
    )
    return 0 if not summary.get("errors") else 1


def _run_tca_report(date_str: str | None, force: bool) -> int:
    """执行损耗 TCA 读数（P1.6）：重跑 = 立刻按默认窗口（30 天）重算并落盘。

    重跑**覆盖当日同一份文件**（报告是快照，不是台账）——同一天跑两次不会留两份，
    也不会追加。要换窗口（比如全历史 ``--days 0``）请直接用读数面本身：
    ``python backend/scripts/tca_report.py --days 0``；``--date`` 在这里**不看**
    （窗口是"距今天数"而不是某一天，给了会被当成不存在的语义静默忽略）。
    """
    import asyncio

    from backend.scripts.tca_report import (
        DEFAULT_DAYS,
        ENV_ACCOUNT_USER,
        MIN_SAMPLE,
        collect,
        render,
        reports_dir,
        write_report,
    )
    from backend.shared.database_manager_v2 import close_database
    from backend.shared.simulation_account_keys import resolve_db_account_user

    user_id = resolve_db_account_user(ENV_ACCOUNT_USER)

    async def _run():
        try:
            return await collect(
                days=DEFAULT_DAYS, tenant_id="default", user_id=user_id
            )
        finally:
            await close_database()

    rep = asyncio.run(_run())
    print("\n".join(render(rep)))
    json_path, md_path = write_report(
        rep, reports_dir(), stamp=str(rep["generated"])[:10]
    )
    print(f"\n[TCA] 已落盘 {json_path} / {md_path}")
    sample_n = int((rep.get("sample") or {}).get("n") or 0)
    if sample_n < MIN_SAMPLE:
        print(f"[TCA] 样本 {sample_n} < {MIN_SAMPLE}：只展示不做结论（退出码 1 = 注意）")
        return 1
    return 0


_RERUN_DISPATCH: dict[str, Callable[[str | None, bool], int]] = {
    # 键 = 注册表任务键（唯一标识，禁止别名——测试防止漂移）
    "sim_eod": _run_sim_eod,
    "auto_inference": _run_inference,
    "market_sync_dispatch": _run_data_sync,
    "dual_book": _run_dual_book,
    "mirror_shadow": _run_shadow_compare,
    "eval_scores": _run_eval_scores,
    "health_recheck": _run_health_recheck,
    "risk_tier": _run_risk_tier,
    # T-RC-14：三个日级任务此前声明了 rerun 却不在分发表，守卫测试因此长期为红
    # （红灯的守卫等于没有守卫）。三者都有干净的 main() 一次性入口。
    "sentinel_backfill": _run_sentinel_backfill,
    "advice_backfill": _run_advice_backfill,
    "advice_generator": _run_advice_generator,
    # P2.8 决策轮（真钱生产者）：一次性入口就是 CLI 本身，「重跑」= 立刻跑一轮。
    "decision_round": _run_decision_round,
    # P2.6 减仓执行器（真钱风控动作）：同上，「重跑」= 立刻真减一次。
    "leverage_trim": _run_leverage_trim,
    # P1.6 TCA 读数面：纯读，重跑 = 重算并覆盖当日报告（无副作用）。
    "tca_report": _run_tca_report,
}


#: 认 ``--agent`` 的任务（P2.9）：**只有这些**允许带 ``--agent`` 重跑。
#: 其余任务没有「模型」这个维度，收到 ``--agent`` 一律 rc 2 —— 静默忽略是这里最坏的
#: 形态（操作员以为只补了一家模型，实际整个任务重跑了一遍，而 decision_round 会下单）。
_AGENT_RERUN: dict[str, Callable[[str | None, bool, str], int]] = {
    "decision_round": _run_decision_round,
}


def _force_notice(job_key: str) -> str:
    """``--force`` 的提示文案：按任务说清"会发生什么"。

    这段曾是「保留参数（当前无带守卫的任务需要绕过）」——在 decision_round 落地后
    那句话不再是事实：它的 ``--force`` 会抢占槽位、**真的再下一批单**。在真钱路径
    上，含糊的提示等于误导（操作员会当它是"重算一遍报告"）。
    """
    if job_key == "decision_round":
        return (
            "提示：--force = 抢占已认领的槽位、忽略当日 done 键，"
            "会真的再发一批新订单（同槽同标的同向腿被订单幂等键挡住）；"
            "只补名册里的一家加 --agent <模型名>（不给 = 名册全员各跑一轮）"
        )
    if job_key == "leverage_trim":
        return (
            "提示：--force = 忽略 paused 软开关，**会真的提交减仓委托**"
            "（交易时段/实盘闸门/券商选定三道判据不受 --force 影响；"
            "当日幂等号与同标的尝试上限仍然生效）"
        )
    return f"提示：--force 已传给 {job_key}；该任务的重跑本身不看这个参数"


def cmd_run(job_key: str, date_str: str | None, force: bool, agent: str = "") -> int:
    spec = JOBS_BY_KEY.get(job_key)
    if spec is None:
        print(f"未知任务: {job_key}；可用: {', '.join(JOBS_BY_KEY)}", file=sys.stderr)
        return 2
    if not spec.rerun or job_key not in _RERUN_DISPATCH:
        print(
            f"任务 {job_key} 不支持手动重跑（可重跑: {', '.join(_RERUN_DISPATCH)}）",
            file=sys.stderr,
        )
        return 2
    if agent and job_key not in _AGENT_RERUN:
        print(
            f"任务 {job_key} 没有模型维度：--agent 只对 "
            f"{', '.join(sorted(_AGENT_RERUN))} 有效（其余任务静默忽略该参数 = 让"
            "操作员以为只重跑了一家）",
            file=sys.stderr,
        )
        return 2
    if force:
        print(_force_notice(job_key))
    print(
        f"== 手动重跑 {spec.key}（{spec.name}）"
        f"{f'date={date_str}' if date_str else ''}"
        f"{f' agent={agent}' if agent else ''} =="
    )
    if agent:
        return _AGENT_RERUN[job_key](date_str, force, agent)
    return _RERUN_DISPATCH[job_key](date_str, force)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="调度控制台（注册表见 shared/scheduler_registry.py）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出调度表与心跳状态")
    run_p = sub.add_parser("run", help="手动重跑指定任务")
    run_p.add_argument("job", help="任务 key（如 sim_eod/inference/data_sync）")
    run_p.add_argument(
        "--date", default="", help="目标日期 YYYY-MM-DD（语义按任务而定）"
    )
    run_p.add_argument(
        "--force",
        action="store_true",
        help="按任务而定的重跑语义（decision_round：抢占槽位、会真的再下单）",
    )
    run_p.add_argument(
        "--agent",
        default="",
        help="只重跑名册里的这一家模型（decision_round 专用，P2.9）",
    )
    args = parser.parse_args()

    if args.cmd == "list":
        return cmd_list()
    return cmd_run(args.job, args.date or None, args.force, args.agent)


if __name__ == "__main__":
    raise SystemExit(main())
