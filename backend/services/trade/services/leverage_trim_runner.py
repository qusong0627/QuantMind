"""减仓执行器的驱动层（P2.6）：常驻 worker 与操作员 CLI。

**出事只能是「不跑」，不能是「乱下」**：这里不出现任何取数与下单代码（编排在
``leverage_trim``，取数与提交在 ``leverage_trim_io``，本模块只回答「多久跑一次、
开关开没开、心跳写没写、操作员敲了什么」）。同 ``decision_round_runner`` 的分法。

Redis **取的是包装客户端**（``trade_shared.deps.get_redis``）：``deps.redis`` 要一并
交给下单派发链，那条链说的是包装的方言。本执行器自己的键位读写由 ``leverage_trim_io``
的 ``_client()`` 解包成原生句柄——包装客户端读不出 ``paused`` 开关，也写不进状态键
（评审 C1，详见 ``leverage_trim_io`` 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from backend.services.trade.services.leverage_trim import run_trim_cycle
from backend.services.trade.services.leverage_trim_core import (
    ACTION_BLOCKED,
    ACTION_IDLE,
)
from backend.services.trade.services.leverage_trim_io import (
    ENV_FLAG,
    ENV_POLL_S,
    MAX_INTERVAL_SEC,
    MIN_INTERVAL_SEC,
    TrimDeps,
    default_trim_deps,
    load_config,
)

logger = logging.getLogger(__name__)


# ── 常驻 worker ─────────────────────────────────────────────────────
def _poll_s(default: int = 60) -> int:
    """起点节拍（环境变量）。**上下夹取与配置侧同一对常量**——这条路径只决定
    「首轮之前那行日志」与配置读失败时的回落值，同一个数字有两个不夹取的口子
    就会重新长出一个比心跳 TTL 大的节拍（评审 M6 的病灶）。"""
    try:
        raw = int(os.getenv(ENV_POLL_S, "") or default)
    except ValueError:
        raw = default
    return min(MAX_INTERVAL_SEC, max(MIN_INTERVAL_SEC, raw))


async def run_leverage_trim_worker() -> None:
    """常驻：每 ``interval_sec`` 秒查一轮（默认 60s，配置可调）。

    开关 ``QM_LEVERAGE_TRIM_ENABLED`` **在这里判**（``trade/main.py`` 也判一次：
    关着时压根不建这个任务）。循环体只捕获异常不退出——一轮失败下一轮补。
    """
    from backend.shared.env_flags import env_flag
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    if not env_flag(ENV_FLAG):
        logger.info(
            '[LeverageTrim] %s 未开启（仅 "true" 生效），worker 不启动', ENV_FLAG
        )
        return
    interval = _poll_s()
    redis = None
    logger.info("[LeverageTrim] 减仓执行器循环启动（%ds 一拍）", interval)
    last_error = ""
    while True:
        try:
            _sched_heartbeat("leverage_trim")
        except Exception:  # noqa: BLE001
            pass
        try:
            if redis is None:
                from backend.services.trade_shared.deps import get_redis as _get_redis

                redis = _get_redis()
            cfg = load_config(redis)
            interval = int(cfg.get("interval_sec") or _poll_s())
            deps = default_trim_deps(redis)
            summary = await run_trim_cycle(deps, config=cfg)
            last_error = ""
            if summary.get("action") != ACTION_IDLE:
                logger.info(
                    "[LeverageTrim] %s %s（杠杆=%s）",
                    summary.get("action"),
                    summary.get("reason"),
                    summary.get("leverage"),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 循环不许死
            message = str(exc)
            if message != last_error:
                logger.error("[LeverageTrim] 轮询异常: %s", exc, exc_info=True)
                last_error = message
            interval = _poll_s()
        await asyncio.sleep(interval)


# ── 操作员入口 ──────────────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    """CLI：``--once`` 跑一轮并打印摘要；``--dry-run`` 只读账户与档位、不提交任何单。

    ``--dry-run`` 与 ``--once`` 走同一段取数与计划代码（同一份口径），只是把提交
    一步换成打印——运营据此在盘中确认「这一轮本来会卖什么」。
    """
    import argparse
    import os

    parser = argparse.ArgumentParser(description="减仓执行器（P2.6）单跑入口")
    parser.add_argument("--once", action="store_true", help="跑一轮（会真的下单）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只算不报（不提交任何单）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 paused 开关（交易时段、实盘闸门、券商选定仍然照判）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.once and not args.dry_run:
        # 没有默认动作：敲下模块名就**真减一次**是真钱路径上最不该有的默认值
        # （评审 LOW-3）。要跑就得明说要跑，要看不提交就 --dry-run。
        parser.error(
            "必须显式指定 --once（跑一轮，会真的下减仓单）或 --dry-run（只算不报）"
        )
    logging.basicConfig(level=logging.INFO)

    from backend.services.trade_shared.deps import get_redis

    async def _run() -> dict[str, Any]:
        from backend.shared.database_manager_v2 import close_database

        try:
            redis = get_redis()
            deps = default_trim_deps(redis)
            cfg = load_config(redis)
            if args.force and cfg.get("paused"):
                print(
                    "提示：--force 忽略 paused（自动循环的软开关）；"
                    "交易时段/实盘闸门/券商选定仍然照判",
                    file=sys.stderr,
                )
            if args.force or args.dry_run:
                cfg = {**cfg, "paused": False}
            if args.dry_run:
                # 用 ``replace`` 而不是逐字段重建：重建会在 TrimDeps 添字段时静默丢值，
                # 而这是个真钱执行器——演练与实跑必须走同一份接线，只差 dry_run 一位。
                deps = replace(
                    deps,
                    dispatch=_refuse_dispatch,  # 兜底哨兵（真闸在 submit_leg）
                    is_trading_time=lambda: True,  # 演练不挑时段
                    dry_run=True,
                    rehearsal=True,  # 演练不是真轮次：不写状态键（见 TrimDeps.rehearsal）
                )
            return await run_trim_cycle(deps, config=cfg)
        finally:
            await close_database()

    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if args.dry_run:
        print("（dry-run：未提交任何委托）", file=sys.stderr)
    return 0 if summary.get("action") != ACTION_BLOCKED else 1


async def _refuse_dispatch(order_data: dict[str, Any]) -> dict[str, Any]:
    """dry-run 的**兜底**派发替身：真被调到就说明 ``submit_leg`` 的演练闸破了。

    注意它只是兜底——``submit_leg`` 里 ``deps.dry_run`` 的提前返回才是真闸。
    本函数抛的异常会被 ``submit_leg`` 的 ``except`` 吞成「派发异常」腿，
    所以「没提交」这件事**不能靠它来证明**（回归测试用真派发替身断言零调用）。
    """
    raise RuntimeError(f"dry-run 不允许提交委托：{order_data.get('client_order_id')}")


if __name__ == "__main__":
    sys.exit(main())
