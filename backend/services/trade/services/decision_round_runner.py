"""决策轮（P2.8）的**驱动层**：常驻 worker 与 CLI。

分层（依赖方向单向，见 ``test_decision_round.py`` 的分层守卫）::

    runner  ← 谁在什么时候调用轮次（本文件：常驻循环 / 操作员命令行）
      ↓
    tick    ← 哪些槽位该跑、谁在跑、今天跑过没有（``decision_round_tick``）
      ↓
    round   ← 一轮做什么（取数→提示词→LLM→执行→守护→审计，``decision_round``）
      ↓
    io      ← 与外界打交道（客户端构造、状态键、认领键，``decision_round_io``）
      ↓
    core    ← 纯逻辑（槽位表、到点判定、结果结构，``decision_round_core``）

**为什么单独一层**：驱动层要回答的问题（多久轮询一次、开关怎么判、退出怎么收）
与「一轮里发生了什么」完全无关，混在一个文件里时，审阅一轮的下单逻辑必须先在
一百多行的 argparse 与 sleep 循环里找入口。分开之后，``decision_round.py`` 里
只剩编排，本文件里不出现任何下单/取数代码——**这里出错只会「不跑」，不会「乱下」**。
（``tick`` 层是「到点/认领/去重」——它同样属于「什么时候跑」而不是「跑起来做什么」，
故与 worker 分开、但在同一张依赖图上。）

与 ``trade/main.py`` 的分工：任务由 main 的 lifespan 起停（那里也判一次开关），
本模块的 worker 是**循环体**；``ENV_FLAG`` 判两次是有意的——main 里判决定「要不要
建这个任务」，这里判决定「循环要不要真的开始跑」，两次都拦在真钱路径之外。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Sequence

from backend.services.trade.services.decision_round_core import (
    DEFAULT_GRACE_MIN,
    DEFAULT_POLL_S,
    ENV_FLAG,
    ENV_GRACE_MIN,
    ENV_POLL_S,
    SLOTS,
    SLOTS_BY_HHMM,
    STATUS_SKIPPED,
    RoundResult,
    due_slots,
)
from backend.services.trade.services.decision_round_tick import round_tick

logger = logging.getLogger(__name__)


def _poll_s() -> int:
    try:
        return max(5, int(os.getenv(ENV_POLL_S, "") or DEFAULT_POLL_S))
    except ValueError:
        return DEFAULT_POLL_S


def _grace_min() -> int:
    try:
        return max(0, int(os.getenv(ENV_GRACE_MIN, "") or DEFAULT_GRACE_MIN))
    except ValueError:
        return DEFAULT_GRACE_MIN


async def run_decision_round_worker() -> None:
    """常驻：每 ``QM_DECISION_ROUND_POLL_S`` 秒查一次到点槽位。

    开关 ``QM_DECISION_ROUND_ENABLED`` **在这里判**（trade/main.py 也判一次：
    关着的时候压根不该建这个任务）。循环体只捕获异常不退出——一轮失败下一轮补。
    """
    from backend.shared.env_flags import env_flag
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    if not env_flag(ENV_FLAG):
        logger.info(
            '[DecisionRound] %s 未开启（仅 "true" 生效），worker 不启动', ENV_FLAG
        )
        return
    logger.info(
        "[DecisionRound] 轮次循环启动（%d 个槽位、轮询 %ds、宽限 %dmin）",
        len(SLOTS),
        _poll_s(),
        _grace_min(),
    )
    while True:
        try:
            _sched_heartbeat("decision_round")
        except Exception:  # noqa: BLE001
            pass
        try:
            results = await round_tick(grace_min=_grace_min())
            for r in results:
                logger.info(
                    "[DecisionRound] %s %s status=%s decisions=%d legs=%d "
                    "submitted=%d watch=%d audit=%d note=%s",
                    r.slot.label if r.slot else "-",
                    r.round_id,
                    r.status,
                    r.decisions,
                    r.legs,
                    r.submitted,
                    r.watch_armed,
                    r.audit_rows,
                    r.note,
                )
        except Exception as exc:  # noqa: BLE001 循环不许死
            logger.error("[DecisionRound] tick 异常: %s", exc, exc_info=True)
        await asyncio.sleep(_poll_s())


def main(argv: Sequence[str] | None = None) -> int:
    """CLI：跑一次（``--slot HHMM`` 指定槽位 / ``--force`` 手动重跑）。

    ``--dry-run``：只打印当前 tick 会跑哪些槽位，不认领、不调模型。
    """
    import argparse

    parser = argparse.ArgumentParser(description="决策轮（P2.8）单跑入口")
    parser.add_argument("--slot", default="", help="指定槽位 HHMM（默认按时刻表）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="手动重跑：抢占已认领的槽位并忽略当日 done 键（会真的再下一轮单）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印会跑哪些槽位")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO)
    if args.dry_run:
        from backend.shared.decision_context_source import now_cn

        now = now_cn()
        due = due_slots(now, grace_min=_grace_min())
        print(
            f"北京时间 {now:%F %T}：到点槽位 "
            f"{[s.label for s in due] or '（无）'}；当日全部槽位 "
            f"{[s.label for s in SLOTS]}"
        )
        return 0

    slot = SLOTS_BY_HHMM.get(args.slot) if args.slot else None
    if args.slot and slot is None:
        print(
            f"未知槽位 {args.slot}（合法：{', '.join(SLOTS_BY_HHMM)})", file=sys.stderr
        )
        return 2

    async def _run() -> tuple[RoundResult, ...]:
        from backend.shared.database_manager_v2 import close_database

        try:
            return await round_tick(force=args.force, slot=slot, grace_min=_grace_min())
        finally:
            await close_database()

    results = asyncio.run(_run())
    if not results:
        # 空结果有四种成因，且**都不长这样**：「没到点」「非交易日」「日历读不到」
        # 「槽位已被认领」。前三种是等待对象的差别，第四种相反——那一槽**正在或
        # 已经**跑过。写成一句话会把第四种读成第一种，运营于是去查时刻表，而真因
        # 在 Redis 键上。逐项点名，并把「确实要再来一遍」的入口写出来。
        print(
            "本轮无结果：可能①没到点/已过宽限窗 ②非交易日 ③交易日历读不到 "
            "④槽位已被认领（worker 正在跑，或今天已跑过）。\n"
            "  已到点、要立刻再跑一轮：加 --force 抢占槽位；"
            "只跑指定槽位再配 --slot HHMM。",
            file=sys.stderr,
        )
        return 1
    for r in results:
        print(
            f"{r.round_id} {r.status} decisions={r.decisions} legs={r.legs} "
            f"submitted={r.submitted} watch={r.watch_armed} audit={r.audit_rows} "
            f"— {r.note}"
        )
    # 「按设计跳过」不是失败：补跑槽在当天已有该 schema 的决策时就是跳过
    # （``STATUS_SKIPPED``）。退出码若为 1，依赖退出码的封装（脚本、控制台按钮、
    # cron 包装）会把「一切正常、无需再跑」读成「跑挂了」，于是有人去查不存在的问题。
    return 0 if all(r.ok or r.status == STATUS_SKIPPED for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
