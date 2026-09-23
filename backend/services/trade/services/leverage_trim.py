"""减仓执行器的一轮编排（P2.6）——``leverage_trim_to`` 的消费者。

本模块是闸门 ``l1.leverage_cap``（超限**只拒买**、不压回持仓）的**执行侧补集**：
读真账户、下真单，把超限的总敞口卖回档位给的 ``leverage_trim_to``。

分层（与决策轮 P2.8 同一张图）::

    runner  ← 多久跑一次、开关、心跳、操作员 CLI（``leverage_trim_runner``）
      ↓
    cycle   ← 一轮：档位 → 账户 → 建腿 → 计划（core）→ 提交 → 留痕（本文件）
      ↓
    submit  ← 腿 → 一张真委托：报价/幂等号/派发/告警闸（``leverage_trim_submit``）
      ↓
    io      ← 账户/行情 → 腿、配置/状态/报告键（``leverage_trim_io``）
      ↓
    core    ← 纯逻辑：阈值判定、缺键姿态、最大腿优先、轮内循环（``leverage_trim_core``）

（前三层各自只向下依赖：cycle 同时 import submit 与 io，submit 只 import io，io 只 import core。）

这一层只回答「一轮里按什么顺序做什么」，因此：

* **暂停 = 只报不卖**：``paused=true``（实盘闸门仍开着）时照常读档位/账户/行情、照常算
  该减什么，只是**一笔不提交、不记失败次数、不告警**——落 ``action=idle`` 且
  ``paused_would`` 给出本该执行的动作。「暂停期间账户还在越限」正是运维解暂停前要知道的
  那件事，所以这里不早退；实盘闸门也关着时才是真的无事可报，连账户都不读。
* **缺键 ≠ 故障**：档位有意不给减仓键（``FALLBACK_LIMITS`` 的形态）= ``idle``（设计姿态，
  不读账户、不告警）；该减却减不动（超限但无可执行腿、账户读不出）= ``blocked``
  （响亮留痕 + 每成因每日一次告警）。
* **告警去重的键必须稳定**：键里不能带杠杆这类每轮都在变的数，否则「每成因每日一次」
  会退化成「每 60s 一次」（2026-09-24 评审 H3）。稳定成因码来自 core 的
  ``TrimPlan.code`` / 本模块的固定串。

三道防「废单死循环」的闸（隔壁 2026-09-21 的 002074 实录：42 笔越界废单、每 2 分钟一笔）：

1. **报价**走 ``sltp_executor.resolve_protect_price``（默认 ``aggressive`` =
   ``max(跌停价, 现价×0.99)``），不写死 −2%；
2. **备注前缀 ``trim:``** 进强平族（风险闸的 ``l3.price_deviation`` 由 ±2% 放宽到
   ±20% 的 sanity 上界）——否则一次快速下跌里我们的 −1% 报价会被自己的闸拒掉；
3. **同标的当日失败计数**（``MAX_ATTEMPTS_PER_SYMBOL_PER_DAY``）到顶即停手告警，
   不靠「下一拍再试」把废单刷成循环。

幂等：委托号 ``trim-<code>-<日>-g<代次>``（``orders.client_order_id`` 唯一 + 派发层
先查后插）。代次按**当日该标的已用掉的号**递增（``submitted + burned + 1``，见
``leverage_trim_submit._bump_burned``）：崩溃重试撞同一号 → 返回 ``duplicate_skipped``
（不重复下单、也不多计一笔、**更不谎报成功**，见下）；同一天真的该再减一次（第一次
成交后价格又回到触发线上方）则代次 +1，照下。**只数成功笔数**是错的——被拒/幂等命中
的那一轮同样**用掉了**这个号（委托行已落库），不算它就会算出同一个代次、撞回那行，
被派发层当成「幂等命中」的 ok=True（HIGH-1）。防重不靠号：真在途的那张由
``read_inflight`` 逐腿跳过，下一轮换号也不会重复下单。

**幂等命中不算成功**：``duplicate_skipped`` 是第三种结局（已有人在途，本轮的「减仓」
没有发生新的动作）。它既不进「已提交」计数，也不触发「减仓已下单」通知——否则一个
卡住的执行器会每分钟给运维报一次平安（2026-09-24 评审 H2）。

**按现价算缺口、按保护价成交**：报价比现价低 1%（跌停附近最多低到跌停价），于是单轮
实际减仓略小于缺口，缺口按几何级数收敛（下一拍继续，防御档 ``trim_to == max`` 时约
1~3 轮收敛）。这是「保证成交」的代价，方向是**少卖**。

"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from backend.services.trade.services.leverage_trim_core import (
    ACTION_BLOCKED,
    ACTION_IDLE,
    ACTION_TRIM,
    LIMITS_DIRTY,
    aggregate_value,
    enforcing_cap_from_config,
    limits_from_budget,
    plan_trim,
)

from backend.services.trade.services.leverage_trim_io import (
    DEFAULT_CONFIG,
    MAX_ATTEMPTS_PER_SYMBOL_PER_DAY,
    TrimDeps,
    load_config,
    load_state,
    read_account,
    save_state,
    write_status,
)

from backend.services.trade.services.leverage_trim_submit import (
    _EXEC_DUPLICATE,
    LegOutcome,
    _alert_once,
    _attempts,
    _bump_attempt,
    is_priced as _is_priced,
    submit_leg,
)

logger = logging.getLogger(__name__)


#: 暂停态摘要的前缀（面板/告警按它分辨「有意不动手」与「动手失败」）。
PAUSED_PREFIX = "已暂停：只报不卖"


def _enforcing_cap(deps: TrimDeps) -> tuple[float | None, str]:
    """读配置视图 → ``(正在强制执行的杠杆上限, 问题)``。

    闸门真正在拦单的上限是 ``min(配置值, 档位值)``（``tiers.apply_to_rules`` 取更严者），
    只读档位会在 ``[配置上限, 档位上限)`` 留一条**死区**（评审 M2）。判据在 core
    （``enforcing_cap_from_config``，纯层可单测），这里只负责取数与异常兜底。
    """
    reader = getattr(deps, "load_risk_config", None)
    if reader is None:
        # 未接线的调用点（测试替身）：配置侧不参与，按档位单独走。
        return None, ""
    try:
        return enforcing_cap_from_config(reader())
    except Exception as exc:  # noqa: BLE001 读不到配置 = 不知道它的强制上限
        return None, f"风险配置读取失败：{type(exc).__name__}: {exc}"


async def run_trim_cycle(
    deps: TrimDeps, *, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """一轮减仓：返回可序列化摘要（日志/状态键/运维端点共用同一份）。"""
    now = deps.now()
    day = now.date().isoformat()
    cfg = dict(config or load_config(deps.redis))
    state = load_state(deps.redis, day)
    #: 轮初快照：状态**只在真变了**时才落盘（M4）——60s 一拍的空转不该每轮都写一次键。
    saved_json = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    summary: dict[str, Any] = {
        "action": ACTION_IDLE,
        "reason": "",
        "day": day,
        "at": now.isoformat(),
        "leverage": None,
        "equity": None,
        "position_value": None,
        "target_value": None,
        "need_value": 0.0,
        "planned": [],
        "skipped": [],
        #: 本轮计划覆盖不到的缺口（>0 = 修不完，下一轮接着来；**不许当 0 静默**）。
        "remaining_value": None,
        #: 本轮计划**超出**缺口的金额（>0 = 整手取整导致超卖，评审 M5 的披露位）。
        "overshoot_value": None,
        "legs": [],
        #: 幂等命中的腿数：既不算「已下单」，也不算失败（第三种结局，见 docstring H2）。
        "duplicates": 0,
        "tier": {},
        #: 配置侧正在强制执行的杠杆上限（None = 配置不构成约束）；见 core 的
        #: ``enforcing_cap_from_config``。**每个收尾口都要带上**：缺这个键会让「跑没跑过
        #: 配置合并」与「配置关着」在面板上长得一样。
        "enforcing_cap": None,
        "config": {
            "paused": bool(cfg.get("paused")),
            "protect_price_mode": cfg.get("protect_price_mode"),
        },
        #: 演练位。**每个收尾口都要带上**（原先只在「有计划」那条路上写）：一次演练的
        #: 结果落成「上一次轮次」时，缺这个键就读不出它压根没提交过委托。
        "dry_run": bool(deps.rehearsal),
        # 暂停态才有值：这一轮**本该**执行的动作（只报不卖，见模块 docstring）。
        "paused_would": None,
    }

    def _finish(action: str, reason: str) -> dict[str, Any]:
        summary["action"] = action
        summary["reason"] = reason
        now_json = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
        # 演练/暂停都不写状态键：它们不改计数、不去重（`_alert_once` 与 `_bump_attempt`
        # 都在 `dry_run` 下短路），写一份空壳只会让下一轮多一次读。
        if not deps.dry_run and now_json != saved_json:
            save_state(deps.redis, day, state)
        # 演练轮**不是**真轮次：不写状态键（`qm:risk:trim:last`），否则一份假的
        # 「上一轮」会盖掉运维面板上的真轮次。暂停是真轮次，照写。
        if not deps.rehearsal:
            write_status(deps.redis, summary)
        return summary

    paused = bool(cfg.get("paused"))
    if paused and not deps.real_enabled():
        # 实盘闸门也关着 = 这座账户根本没有真单在跑：没有要保护的东西，不必读账。
        return _finish(ACTION_IDLE, "已暂停（qm:risk:trim:config.paused=true）")
    if not deps.real_enabled():
        # 实盘闸门关着时这座账户根本没有真单在跑：没有要保护的东西，不必告警。
        return _finish(ACTION_IDLE, "实盘闸门关闭（ENABLE_REAL_TRADING）：无真单可护")
    if not deps.is_trading_time():
        return _finish(ACTION_IDLE, "非交易时段")

    # 暂停 = **只报不卖**（隔壁 leverage_guard 的口径：总闸关着也要说清本该强平什么）。
    # 不早退是因为「暂停期间账户还在越限」正是运维解暂停前要知道的那件事。沿用
    # dry-run 的同一套机制压住告警与当日计数——暂停是运维自己按的手，不是事故；
    # 下面每个收尾口都经 ``_stop``，把本该做的动作记进 ``paused_would``。
    if paused:
        deps = replace(deps, dry_run=True)

    def _stop(action: str, reason: str) -> dict[str, Any]:
        if not paused:
            return _finish(action, reason)
        summary["paused_would"] = action
        return _finish(ACTION_IDLE, f"{PAUSED_PREFIX}——{reason}")

    try:
        broker = str(deps.selected_broker() or "")
    except Exception as exc:  # noqa: BLE001 读不到选定值 ≠ 没选过（strict 语义）
        reason = (
            f"券商选定读取失败：{type(exc).__name__}: {exc}（不知道下单会去哪座账户，"
            "本轮不读账、不动作）"
        )
        # 去重键固定串（异常文本每轮都不同，带上它 = 每轮都算新成因）。
        await _alert_once(
            deps, state, "blocked:broker-unreadable", "减仓执行器无法判定账户", reason
        )
        return _stop(ACTION_BLOCKED, reason)
    if broker != deps.expected_broker:
        # 读的那座账户必须就是下单去的那座：选定别家时本执行器没有（也不该有）读取口径。
        return _stop(
            ACTION_IDLE,
            f"当前选定券商 {broker or '(未配置)'} ≠ {deps.expected_broker}："
            "本执行器只在 QMT 账户上减仓",
        )

    tier = None
    try:
        tier = deps.load_tier()
    except Exception as exc:  # noqa: BLE001 档位读不到 = 不知道目标，不动手
        reason = f"档位读取异常：{type(exc).__name__}: {exc}"
        await _alert_once(
            deps, state, "blocked:tier-unreadable", "减仓执行器读不到档位", reason
        )
        return _stop(ACTION_BLOCKED, reason)
    summary["tier"] = {
        "level": str(getattr(tier, "level", "") or ""),
        "source": str(getattr(tier, "source", "") or ""),
        "date": str(getattr(tier, "date", "") or ""),
    }
    cap, cap_problem = _enforcing_cap(deps)
    if cap_problem:
        # 配置说自己在强制（enabled 且非影子期）却读不出它强制多少：不知道就当不知道，
        # 不按档位单独减（那会在配置更严时卖出多于配置允许的量）。**不读账户**、
        # 不动手，与「档位读不到」同一姿态。
        await _alert_once(
            deps,
            state,
            "blocked:capview",
            "减仓执行器读不到风险配置的强制上限",
            f"{cap_problem}。本轮不动手（数据故障不是卖出信号）。",
        )
        return _stop(ACTION_BLOCKED, cap_problem)
    summary["enforcing_cap"] = cap
    limits_read = limits_from_budget(
        getattr(tier, "budget", None),
        level=str(getattr(tier, "level", "") or ""),
        source=str(getattr(tier, "source", "") or ""),
        enforcing_cap=cap,
    )
    summary["limits_kind"] = limits_read.kind
    if limits_read.limits is None:
        # 档位没给出可用的减仓参数 → **连账户都不读**（省一次柜台往返，且避免在
        # 「有意不动手」的姿态下因一次行情/账户抖动刷出 blocked 告警）。
        if limits_read.kind == LIMITS_DIRTY:
            await _alert_once(
                deps,
                state,
                f"dirtylimits:{limits_read.code or 'unknown'}",
                "档位减仓参数疑似脏文档",
                f"{limits_read.reason}。本轮不动手，请检查档位文档。",
            )
        return _stop(ACTION_IDLE, limits_read.reason)

    account = await read_account(deps)
    if not account.ok:
        reason = "；".join(account.errors)
        await _alert_once(
            deps, state, "blocked:account-unreadable", "减仓执行器无法读取账户", reason
        )
        return _stop(ACTION_BLOCKED, reason)

    position_value = aggregate_value(account.reported_value, account.legs)
    plan = plan_trim(
        equity=account.equity,
        position_value=position_value,
        legs=account.legs,
        limits=limits_read.limits,
        limits_reason=limits_read.reason,
    )
    summary.update(
        {
            "leverage": plan.leverage,
            "equity": plan.equity if plan.equity is not None else account.equity,
            "position_value": (
                plan.position_value
                if plan.position_value is not None
                else position_value
            ),
            "target_value": plan.target_value,
            "need_value": plan.need_value,
            "planned": [
                {
                    "symbol": leg.symbol,
                    "quantity": leg.quantity,
                    "price": leg.price,
                    "value": leg.value,
                    "note": leg.note,
                }
                for leg in plan.legs
            ],
            "skipped": [list(item) for item in plan.skipped],
            "positions": account.positions_count,
        }
    )
    summary["remaining_value"] = plan.remaining_value
    summary["overshoot_value"] = plan.overshoot_value
    if plan.action != ACTION_TRIM:
        if plan.action == ACTION_BLOCKED:
            # 去重键用**稳定成因码**（``plan.code``），不用 ``plan.reason``：后者带着
            # 每轮都在变的杠杆数，会让「同一成因当日一次」退化成「每 60s 一次」（H3）。
            await _alert_once(
                deps,
                state,
                f"blocked:{plan.code or 'unknown'}",
                "减仓执行器无法动作",
                plan.reason,
            )
        return _stop(plan.action, plan.reason)

    mode = str(cfg.get("protect_price_mode") or DEFAULT_CONFIG["protect_price_mode"])
    outcomes: list[LegOutcome] = []
    saved = saved_json  # 腿循环沿用轮初快照作基线（同上：变了才写）
    for leg in plan.legs:
        attempts = _attempts(state, leg.symbol)
        if attempts >= MAX_ATTEMPTS_PER_SYMBOL_PER_DAY:
            outcomes.append(
                LegOutcome(
                    symbol=leg.symbol,
                    quantity=leg.quantity,
                    price=leg.price,
                    ok=False,
                    note=(
                        f"当日已连续 {attempts} 次提交失败，停手等人工（防废单死循环）"
                    ),
                )
            )
            await _alert_once(
                deps,
                state,
                f"giveup:{leg.symbol}",
                f"{leg.symbol} 减仓连续失败已停手",
                f"当日该标的已尝试减仓 {attempts} 次均失败，本轮起停止尝试。"
                "请检查柜台/行情/风控拒因后人工处置。",
            )
            continue
        outcome = await submit_leg(deps, leg, state=state, day=day, mode=mode)
        if not outcome.ok and not deps.dry_run:
            _bump_attempt(state, leg.symbol)
        outcomes.append(outcome)
        if not deps.dry_run:
            # 逐腿落状态：崩在中间也不丢已确认的笔数；**但只在真变了时写**。
            leg_json = json.dumps(
                state, sort_keys=True, ensure_ascii=False, default=str
            )
            if leg_json != saved:
                save_state(deps.redis, day, state)
                saved = leg_json

    # **幂等命中不是成功**：``duplicate_skipped`` 是第三种结局——委托此前已在途，
    # 本轮没有发生新的减仓动作。计入「已下单」会让一个卡住的执行器每分钟给运维报一次
    # 平安（通知说「减仓已下单」，其实这一轮什么都没发），故单列（评审 H2）。
    duplicates = sum(1 for o in outcomes if o.execution == _EXEC_DUPLICATE)
    fresh = [o for o in outcomes if o.ok and o.execution != _EXEC_DUPLICATE]
    ok_count = len(fresh)
    summary["legs"] = [o.as_dict() for o in outcomes]
    summary["duplicates"] = duplicates
    if paused:
        priced = sum(1 for o in outcomes if _is_priced(o))
        return _stop(
            ACTION_TRIM,
            f"{plan.reason}（{len(outcomes)} 腿，{priced} 腿已定价，未提交）",
        )
    if deps.dry_run:
        # 演练的结论就是「这一轮本来会卖什么」：按计划回报，不按替身拒发回报。
        priced = sum(1 for o in outcomes if _is_priced(o))
        return _finish(
            ACTION_TRIM,
            f"{plan.reason}；**dry-run：未提交任何委托**"
            f"（{len(outcomes)} 腿，{priced} 腿已定价并生成单号）",
        )
    if ok_count:
        brief = "；".join(
            f"{o.symbol} {o.quantity:g}股 "
            + (f"@ {o.price:.2f}" if o.price > 0 else "(市价)")
            for o in fresh
        )
        try:
            await deps.notify(
                deps.user_id,
                f"减仓已下单 {ok_count} 腿",
                f"总杠杆 {plan.leverage:.4f} 超上限，目标 {plan.target_value:.0f} 元。{brief}"
                + (
                    f"（整手取整，比缺口多卖 {plan.overshoot_value:.0f} 元）"
                    if plan.overshoot_value > 0
                    else ""
                ),
                "info",
                tenant_id=deps.tenant_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[LeverageTrim] 减仓通知发送失败: %s", exc)
        reason = f"{plan.reason}；已提交 {ok_count}/{len(outcomes)} 腿"
        if duplicates:
            reason += f"（另有 {duplicates} 腿幂等命中：委托此前已在途，本轮未新发）"
        action = ACTION_TRIM
    elif duplicates:
        # 全部腿都是幂等命中：**没有新动作，但计划与在途委托都真实存在**。落 ``trim``
        # 而不是 ``idle``——``idle`` 在面板上读作「稳态，无需处置」，而这里账户仍超限、
        # 缺口靠一批在途委托去补（``legs`` 里看得见）。同时喊一次人（每日一次）：
        # 在途委托一直不落地时，「每轮都幂等命中」就是执行器卡住的形态。
        await _alert_once(
            deps,
            state,
            f"alldup:{day}",
            "减仓执行器：本轮全部腿幂等命中（无新委托）",
            f"{len(outcomes)} 腿的委托号此前已提交（委托在途未落地），本轮未新发任何委托。"
            "账户仍高于档位上限，请确认在途委托是否还需要撮合。",
        )
        return _finish(
            ACTION_TRIM,
            f"{plan.reason}；{duplicates} 腿幂等命中、0 腿新发（委托此前已在途，"
            "等它落地后下一轮再算）",
        )
    else:
        reason = f"{plan.reason}；**全部 {len(outcomes)} 腿提交失败**"
        action = ACTION_BLOCKED
        await _alert_once(
            deps,
            state,
            f"allfailed:{day}",
            "减仓执行器全部腿提交失败",
            "；".join(f"{o.symbol}: {o.note}" for o in outcomes)[:500],
        )
    return _finish(action, reason)
