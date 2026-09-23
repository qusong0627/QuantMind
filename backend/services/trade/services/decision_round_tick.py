"""轮次**调度**（P2.8）：到点 → 认领槽位 → 跑一轮 → 置 done 键 → 写状态键。

为什么与 ``decision_round`` 分家：``decision_round.run_once`` 回答「**一轮里**发生了
什么」（取数→提示词→LLM→闸门→执行→守护→审计），本模块回答「**哪些槽位该跑、谁在
跑、今天跑过没有**」——两件事的读者不同（前者排障看一轮，后者排障看时间轴），合在
一起会到 900 行且都是编排，违反单文件上限（见测试里的体量守卫）。

分层（单向依赖，测试有源码守卫）::

    runner → tick → round → io → core

``tick`` 是目录里唯一同时 import ``run_once`` 与 ``claim_slot``/``write_status`` 的
地方：认领/去重/状态键走 io 层的**原生** Redis 客户端（包装客户端会把异常吞成
``None``，见 ``decision_round`` 模块 docstring），一轮的编排走 round 层。

fail-closed 姿态（只列本层独有的，一轮内的分层见 ``decision_round``）:

=========================  ==========================================
读不到的东西                 姿态
========================= 非交易日/日历读不到           不跑（宁缺勿滥：降级判定会
                             把中秋国庆判成交易日）
Redis 客户端构造失败         不跑（没有去重就开跑，两个进程会把同一轮
                             下成两批真单）
槽位认领失败                 不跑，且**不许当「已跑过」**（否则这一槽
                             永不执行）
槽位已被别人认领             跳过；认领在而 done 键不在时告警（夭折）
补跑槽 done 键读不到         不跑（SKIPPED + 放掉认领）：不知道当日出没
                             出过该 schema 的决策 ⇒ 跑下去可能发第二批单
done 键写入失败              照常返回本轮结果（决策已做、单已下），只
                             记 error：下个补跑槽可能重来，靠订单幂等键
=========================  ==========================================
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

from backend.services.trade.services.decision_round import run_once
from backend.services.trade.services.decision_round_core import (
    DEFAULT_GRACE_MIN,
    DONE_TTL_S,
    STATUS_SKIPPED,
    RoundDeps,
    RoundResult,
    RoundSlot,
    due_slots,
    round_id_for,
    slot_keys,
)
from backend.services.trade.services.decision_round_io import (
    claim_slot,
    default_round_deps,
    native_redis_client,
    write_status,
)

logger = logging.getLogger(__name__)


def _release_claim(client: Any, slot_key: str) -> None:
    """放掉**自己刚认领的**槽位键：窗口内下一次 tick 还能再试一遍。

    认领键的语义是「有人正在跑」。判出「这一槽不该跑」之后若不放开，同一个 45 分钟
    宽限窗里后续每一次 tick 都只会说一句「已被认领」——真因（如 done 键读失败）修好
    了也没人再来。只删自己认领的那把（``claimed=True`` 之后才调用）。

    与 ``--force`` 抢占有极小的竞争面（对方改写为 manual 后被这里删掉）：那之后自动
    tick 最多再跑一次补跑判定，而它读 done 键——同一故障下读失败会走「不猜也不跑」
    分支（见 :func:`round_tick`），所以不会因此多发一批单。
    """
    try:
        client.delete(slot_key)
    except Exception as exc:  # noqa: BLE001 放不开只是少一次重试机会，不许影响本轮结果
        logger.warning("[DecisionRound] 认领键释放失败 %s: %s", slot_key, exc)


def _abandoned_claim_note(client: Any, done_key: str) -> str:
    """认领键在、done 键不在 ⇒ 这一槽今天大概率不会再被**自动** tick 碰到。

    「有人正在跑」与「跑到一半没了」在认领键上长得一模一样，但后果不同：后者意味着
    本轮在 LLM 调用期间（数十秒窗口）被重启/被杀，认领键 TTL 还有两天，后续每一次自动
    tick 都只说一句 info 就跳过——状态键里没有这一轮、审计表里没有这一轮、心跳照写
    （worker 活着），**唯一的痕迹就是这行日志**。所以这里降级为 warning 并给出补跑入口。
    不自动抢占：覆盖写拦不住真在跑的那一轮，且会让「重启后自动补跑」变成一条没人复核的
    隐性下单路径（要抢由人来点 ``--force``）。
    """
    try:
        if client.get(done_key):
            return ""
    except Exception:  # noqa: BLE001 读不到就不猜
        return ""
    return (
        f"且无 done 键（{done_key}）：可能上一轮中途夭折，自动 tick 不会再跑它；"
        "要补这一槽：python backend/scripts/schedule_ctl.py run decision_round --force"
    )


async def round_tick(
    *,
    deps: RoundDeps | None = None,
    native: Any = None,
    now: datetime | None = None,
    grace_min: int | None = None,
    force: bool = False,
    slot: RoundSlot | None = None,
) -> tuple[RoundResult, ...]:
    """一次轮询：到点的槽位逐个「认领 → 跑 → 置 done → 写状态」。

    ``force``：**手动重跑**——抢占槽位认领（覆盖写）并忽略当日 done 键。只忽略 done
    键是不够的（认领键在 done 之前就把它挡住了，CLI 于是报「无到点槽位」）；覆盖写
    只能拦住之后到点的自动 tick，正在跑的那次靠执行段的订单幂等键兜底，见
    ``claim_slot``。``slot``：显式指定槽位（CLI ``--slot``），跳过时刻表与交易日
    判定——操作员点名要跑就是意图，跳过只留日志。
    """
    deps = deps or default_round_deps()
    now = now or deps.now()
    grace = DEFAULT_GRACE_MIN if grace_min is None else int(grace_min)
    day = now.date()

    if slot is not None:
        due: tuple[RoundSlot, ...] = (slot,)
        logger.warning(
            "[DecisionRound] 显式指定槽位 %s（跳过时刻表/交易日判定）", slot.label
        )
    else:
        try:
            trading_day = await deps.is_trading_day(day)
        except Exception as exc:  # noqa: BLE001 日历不可用 → 不跑（宁缺勿滥）
            logger.warning("[DecisionRound] 交易日判定失败，本 tick 不跑: %s", exc)
            return ()
        if not trading_day:
            logger.debug("[DecisionRound] %s 非交易日，跳过", day)
            return ()
        due = due_slots(now, grace_min=grace)
    if not due:
        return ()

    client = native
    if client is None:
        try:
            client = native_redis_client()
        except Exception as exc:  # noqa: BLE001 没有 Redis 就没有去重 → 不跑
            logger.error("[DecisionRound] Redis 客户端构造失败，本 tick 不跑: %s", exc)
            return ()
    try:
        out: list[RoundResult] = []
        for s in due:
            slot_key, done_key = slot_keys(day, s)
            # 抢占了别人的认领要在**这一轮的结果里**留痕：运营读状态键时若只看到
            # 「ok」，就分不清这轮是自动跑的（一轮一天一次）还是人手点出来的
            # （可以点很多次）——而后者决定了复盘时该不该按「计划内」看待这批单。
            takeover = ""
            if force:
                try:
                    holder = client.get(slot_key)
                except Exception as exc:  # noqa: BLE001 读不到就当没人认领
                    logger.debug("[DecisionRound] 认领键读失败: %s", exc)
                    holder = None
                if holder:
                    takeover = f"手动重跑：抢占已认领槽位（原认领={holder}）"
                    logger.warning("[DecisionRound] %s: %s", slot_key, takeover)
            try:
                claimed = claim_slot(client, slot_key, force=force)
            except Exception as exc:  # noqa: BLE001 认领失败 = 不跑（不许当「已跑过」）
                logger.error(
                    "[DecisionRound] 槽位认领失败 %s: %s（本轮不跑）", slot_key, exc
                )
                continue
            if not claimed:
                abandoned = _abandoned_claim_note(client, done_key)
                if abandoned:
                    logger.warning(
                        "[DecisionRound] 槽位已被认领 %s（跳过）：%s",
                        slot_key,
                        abandoned,
                    )
                else:
                    logger.info("[DecisionRound] 槽位已被认领 %s（跳过）", slot_key)
                continue
            if s.catch_up and not force:
                try:
                    already = client.get(done_key)
                except Exception as exc:  # noqa: BLE001 读不到 done 键 → 不猜、也不跑
                    # 「补跑槽存在」的全部意义就是**当日还没出过这个 schema 的决策**；
                    # 读不到 done 键 = 不知道出没出过，此时跑下去可能发出**第二批**建仓
                    # 计划（补跑的 round_id 与主槽不同 ⇒ 订单幂等键不同，挡不住重复腿）。
                    # 所以 fail-closed：本轮不跑、不置 done、放掉认领让窗口内还能再试。
                    reason = (
                        f"done 键读取失败（{type(exc).__name__}: {exc}）：无法确认当日"
                        f"是否已有 {s.schema} 决策，补跑不做（修好后窗口内会重试，"
                        "也可用 --force 显式重跑）"
                    )
                    logger.warning("[DecisionRound] %s: %s", slot_key, reason)
                    skipped = RoundResult(
                        status=STATUS_SKIPPED,
                        day=day,
                        slot=s,
                        round_id=round_id_for(day, s),
                        note=reason,
                    )
                    write_status(client, skipped, at=now)
                    out.append(skipped)
                    _release_claim(client, slot_key)
                    continue
                if already:
                    skipped = RoundResult(
                        status=STATUS_SKIPPED,
                        day=day,
                        slot=s,
                        round_id=round_id_for(day, s),
                        note=f"当日已出过 {s.schema} 决策（{done_key}）：补跑跳过",
                    )
                    write_status(client, skipped, at=now)
                    out.append(skipped)
                    continue
            result = await run_once(s, deps=deps, now=now, day=day)
            if takeover:
                result = replace(
                    result,
                    note=f"{takeover}；{result.note}" if result.note else takeover,
                )
            if result.ok:
                try:
                    client.set(done_key, "1", ex=DONE_TTL_S)
                except Exception as exc:  # noqa: BLE001 置键失败：下个补跑槽可能重来
                    logger.error(
                        "[DecisionRound] done 键写入失败 %s（补跑槽可能重跑）: %s",
                        done_key,
                        exc,
                    )
            write_status(client, result, at=deps.now())
            out.append(result)
        return tuple(out)
    finally:
        if native is None and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
