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

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from backend.services.trade.services.decision_round import run_once
from backend.services.trade.services.decision_round_alerts import (
    MANUAL_RERUN_HINT,
    Notifier,
    alert_round,
    summarize_round,
)
from backend.services.trade.services.decision_round_core import (
    DEFAULT_GRACE_MIN,
    DONE_TTL_S,
    ENV_GRACE_MIN,
    LOG_KEY,
    STATUS_SKIPPED,
    TENANT_ID,
    AgentRun,
    RoundDeps,
    RoundResult,
    RoundSlot,
    as_float,
    due_slots,
    round_id_for,
    select_runs,
    slot_keys,
)
from backend.services.trade.services.decision_round_io import (
    claim_slot,
    default_round_deps,
    native_redis_client,
    write_status,
)
from backend.services.trade.services.decision_round_wake import (
    CTX_TTL_S,
    DAILY_WAKE_LIMIT,
    MIN_ROUND_GAP_MIN,
    STATE_TTL_S,
    WAKE_CTX_KEY,
    WAKE_EVAL_MIN_INTERVAL_S,
    WAKE_STATE_KEY,
    NewsHit,
    WakeObs,
    WakeState,
    budget_ok,
    context_text,
    decode_state,
    encode_state,
    evaluate_triggers,
    filter_triggers,
    idle_state,
    one_line_note,
    snapshot_pnl,
    wake_enabled,
    woken_state,
)
from backend.shared.decision.contract import SCHEMA_INTRADAY
from backend.shared.decision_blocks import book_imbalance, parse_brief_ts
from backend.shared.decision_context_source import day_change_pct
from backend.shared.stock_utils import StockCodeUtil

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
        f"{MANUAL_RERUN_HINT}"
    )


def _raiser(exc: BaseException) -> Callable[[], Any]:
    """造一个「取绑定就抛」的路子（名册解析失败时那一家用）。"""

    def _load() -> Any:
        raise exc

    return _load


def _agent_runs(
    deps: RoundDeps, runs: Sequence[AgentRun]
) -> tuple[tuple[str, RoundDeps], ...]:
    """本轮要跑的 ``(agent, 该家的 deps)`` 序列。

    空名册 → ``(("", deps),)``：**单家路径逐字不变**（键不带家段，绑定照旧走
    ``deps.load_llm``）。有名册时每家换掉 ``load_llm`` 一个注入点，其余（账户、
    持仓、池、行情、执行段）**同一批**——多模型竞争的语义是「同一座账户、同一份
    行情，几家模型各出主意」，不是「几套世界」。
    """
    if not runs:
        return (("", deps),)
    return tuple((r.agent, replace(deps, load_llm=r.load_llm)) for r in runs)


def _alert_user(deps: RoundDeps) -> str:
    """告警要推给谁：决策账户（与下单同一个账户坐标）。取不到就返回空串。

    不抛——账户解析是**通知**的前置，不是这一轮的成败条件；它炸了该由
    ``alert_round`` 记 warning 后不推，而不是把已经跑完的一轮变成异常。
    """
    try:
        return str(deps.account_user() or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DecisionRound] 告警账户解析失败，本轮失败通知不推: %s", exc)
        return ""


async def round_tick(
    *,
    deps: RoundDeps | None = None,
    native: Any = None,
    now: datetime | None = None,
    grace_min: int | None = None,
    force: bool = False,
    slot: RoundSlot | None = None,
    agent: str = "",
    notify: Notifier | None = None,
) -> tuple[RoundResult, ...]:
    """一次轮询：到点的槽位逐个、槽位内**逐家**「认领 → 跑 → 置 done → 写状态」。

    ``notify``：失败可见性的推口（P4 附-②）。``None`` ⇒ **真有事时**才现造生产通知器
    （落库 → 前端通知中心；无事的一轮连它都不碰）；测试逐项换替身。只在真出了事时
    用：aborted / 有腿失败 / 模型未出决策 / 执行段异常——「按设计跳过」不推。同一家
    同一类失败一天一条，去重键走本层的原生客户端（同认领/状态键，理由见模块 docstring）。
    轮次跑完另落一条**值班摘要**（T2-5）：日志每轮都落、QQ 只推动钱的轮，失败告警
    已送达的轮不重复推——见 ``decision_round_alerts.summarize_round``。

    ``force``：**手动重跑**——抢占槽位认领（覆盖写）并忽略当日 done 键。只忽略 done
    键是不够的（认领键在 done 之前就把它挡住了，CLI 于是报「无到点槽位」）；覆盖写
    只能拦住之后到点的自动 tick，正在跑的那次靠执行段的订单幂等键兜底，见
    ``claim_slot``。``slot``：显式指定槽位（CLI ``--slot``），跳过时刻表与交易日
    判定——操作员点名要跑就是意图，跳过只留日志。``agent``：名册里只跑点名的那家
    （CLI ``--agent``），**不在名册里 = 抛 ValueError**（静默跑全员会让「重跑一家」
    变成三家都下单）。

    名册（P2.9）在这层展开：一次 tick 只解析一次（轮询期间不换），每家一个
    ``(槽位键, done 键)`` 与一个绑定。**一家跑不动不带停其余家**——某家欠费/改配置
    是常态，让它拖停整槽等于「便宜的模型挂了，主力模型当天不决策」。
    """
    deps = deps or default_round_deps()
    now = now or deps.now()
    grace = DEFAULT_GRACE_MIN if grace_min is None else int(grace_min)
    day = now.date()

    # 名册解析失败（env 写错一项）⇒ **一家都跑不动**：合成一个「取绑定就抛」的家，
    # 让每一槽都如实记 llm_failed + 原始原因。绝不回落到单家三件套——全局 key 还
    # 有效时那会变成「名册里另外两家静默消失、轮次照常下单」。
    try:
        runs = tuple((deps.roster or (lambda: ()))() or ())
    except Exception as exc:  # noqa: BLE001 名册坏了 = 配置错，报出去而不是猜
        logger.error(
            "[DecisionRound] 名册解析失败，本轮按「一家都跑不动」处理: %s", exc
        )
        runs = (AgentRun(agent="", load_llm=_raiser(exc)),)
    runs = select_runs(runs, agent)

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
            for run_agent, run_deps in _agent_runs(deps, runs):
                slot_key, done_key = slot_keys(day, s, run_agent)
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
                        # 「补跑槽存在」的全部意义就是**当日还没出过这个 schema 的
                        # 决策**；读不到 done 键 = 不知道出没出过，此时跑下去可能发出
                        # **第二批**建仓计划（补跑的 round_id 与主槽不同 ⇒ 订单幂等键
                        # 不同，挡不住重复腿）。所以 fail-closed：本轮不跑、不置 done、
                        # 放掉认领让窗口内还能再试。
                        reason = (
                            f"done 键读取失败（{type(exc).__name__}: {exc}）：无法确认"
                            f"当日是否已有 {s.schema} 决策，补跑不做（修好后窗口内会"
                            "重试，也可用 --force 显式重跑）"
                        )
                        logger.warning("[DecisionRound] %s: %s", slot_key, reason)
                        skipped = RoundResult(
                            status=STATUS_SKIPPED,
                            day=day,
                            slot=s,
                            round_id=round_id_for(day, s),
                            note=reason,
                            agent=run_agent,
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
                            agent=run_agent,
                        )
                        write_status(client, skipped, at=now)
                        out.append(skipped)
                        continue
                result = await run_once(s, deps=run_deps, now=now, day=day)
                if run_agent and result.agent != run_agent:
                    # 绑定取不出来时（llm_failed 那条）``run_once`` 认不出家——而状态键
                    # 正是按家分段的：不补这一下，某家欠费时 ``last:{agent}`` 一把都不
                    # 会写，三家在 ``log`` 里只剩三行「llm_failed」，运营查不出是哪家。
                    result = replace(result, agent=run_agent)
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
                # 失败可见性（P4 附-②）写在状态键**之后**：通知里若带跳转，落地时
                # 状态键已经是在说的那一份；且推失败只记 warning，不影响下面的
                # done 键与返回值——一轮真跑完的结果不许被通知层吃掉。
                # ``notifier`` 留空交给 alert_round 现造：没有要推的事就连通知设施
                # 都不碰（正常的一轮不该在通知链路上留足迹）。
                alerted = await alert_round(
                    result, notifier=notify, user_id=_alert_user(deps), redis=client
                )
                # 值班摘要（T2-5）：状态键此前没有程序化读者——摘要把它变成日志
                # （每轮）与 QQ（动钱或本该动钱的轮，见 ``summary_due``）。失败告警
                # 已送达的轮不重复推 QQ，但摘要仍落日志：告警被当日去重挡下（同类
                # 第二条）时，摘要就是补位的那声回执。摘要层炸了同样不影响本轮结果。
                await summarize_round(result, alerted=alerted)
                out.append(result)
        return tuple(out)
    finally:
        if native is None and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# ══ 丙1 唤醒评估（Stage 2 第二批）══════════════════════════════════════
#
# 判据全在 ``decision_round_wake``（纯函数）；本层只做**编排**：闸门 → 观测 →
# 判 → 合成槽轮（复用 ``round_tick`` 显式槽位路径，认领/done/执行/审计全套不改）。
# 与 ``round_tick`` 同任务串行（runner 先 await round_tick 再 await wake_tick），
# 由此天然没有「唤醒轮与正点轮并发下单」的窗口。

#: 板块类判据每日一次的可见留痕（无源门控的姿态说明，见 ``wake.sector_triggers``）。
_SECTOR_GAP_NOTE = "板块类判据无源（880xxx 板块指数未常驻热集）：本轮不评，判据就绪待源"


def _wake_grace_min() -> int:
    """唤醒评估的「槽位临期不评」窗（与 ``runner._grace_min`` 同式同 env）。

    tick 不许反向 import runner（源码守卫钉死分层），这里复写四行而不是借道。
    """
    try:
        return max(0, int(os.getenv(ENV_GRACE_MIN, "") or DEFAULT_GRACE_MIN))
    except ValueError:
        return DEFAULT_GRACE_MIN


def _snap_now(snap: Mapping[str, Any] | None) -> float | None:
    """快照现价（``Now``/``now``）——与 ``shared.decision_blocks`` 同口径的本地小抄。"""
    if not isinstance(snap, Mapping):
        return None
    raw = snap.get("Now") if snap.get("Now") is not None else snap.get("now")
    return as_float(raw)


def _log_head_ts(client: Any) -> float:
    """轮次日志键头（最近一轮状态）→ epoch；读不到/坏值 → 0.0（当无近轮放行）。

    解析失败 **fail-open**：把「读不到」当「刚跑过」会让真触发被 20 分钟闸门静默
    吞掉——漏一次止损唤醒远比多发一轮贵。
    """
    try:
        head = client.lrange(LOG_KEY, 0, 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DecisionRound] 轮次日志读取失败（按无近轮评估）: %s", exc)
        return 0.0
    if not head:
        return 0.0
    try:
        return datetime.fromisoformat(
            str(json.loads(head[0]).get("ts") or "")
        ).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _write_wake_state(client: Any, key: str, state: WakeState) -> None:
    """状态键落库（心跳/基线/冷却）。失败只降可见性——判据下轮重算，不影响决策。"""
    try:
        client.hset(key, mapping=encode_state(state))
        client.expire(key, STATE_TTL_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DecisionRound] 唤醒状态写入失败 %s: %s", key, exc)


async def gather_wake_obs(deps: RoundDeps) -> WakeObs | None:
    """一次评估的观测（丙1 判据原料）：持仓（桥口径）× 快照 × 简报。

    持仓读不到 → ``None``（**不评**：没有观测的「无触发」是假阴性）；行情/简报
    缺位只让对应字段为 ``None``/0（判据自己跳过缺数项）。持仓取**桥账户整座**
    不做分账裁剪：触发器看的是账户真实盈亏，按分账裁剪会让另一家的票跌穿也不叫醒
    （可见性防线在轮内，不在评估器）。
    """
    user = str(deps.account_user() or "").strip()
    if not user:
        return None
    try:
        positions, _meta = await deps.load_positions(TENANT_ID, user)
    except Exception as exc:  # noqa: BLE001 观测缺失 → 本轮不评
        logger.warning("[DecisionRound] 唤醒评估：持仓读取失败，跳过: %s", exc)
        return None
    codes: list[str] = []
    names: dict[str, str] = {}
    costs: dict[str, float] = {}
    for p in (positions or {}).values():
        if not isinstance(p, Mapping):
            continue
        raw = str(p.get("symbol") or "").strip()
        if not raw:
            continue
        code = StockCodeUtil.to_suffix(raw)
        if not code or code in names:
            continue
        codes.append(code)
        names[code] = str(p.get("name") or "").strip() or code
        cost = as_float(p.get("cost_price"))
        if cost is not None:
            costs[code] = cost
    if not codes:
        return WakeObs()  # 空仓 = 有效观测（无触发），与「观测失败」是两回事

    snapped: Mapping[str, Mapping[str, Any]] = {}
    try:
        client = deps.quote_client()
    except Exception as exc:  # noqa: BLE001 行情不可用：字段缺数，判据自跳过
        logger.warning("[DecisionRound] 唤醒评估：行情客户端不可用: %s", exc)
        client = None
    if client is not None:
        try:
            snapped = deps.read_snaps(client, codes) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DecisionRound] 唤醒评估：快照读取失败: %s", exc)
            snapped = {}

    pnl_pct: dict[str, float | None] = {}
    day_chg: dict[str, float | None] = {}
    imb5: dict[str, float | None] = {}
    for code in codes:
        snap = snapped.get(code)
        pnl_pct[code] = snapshot_pnl(costs.get(code), _snap_now(snap))
        day_chg[code] = day_change_pct(snap)
        imb5[code] = book_imbalance(snap)

    brief_ts = 0.0
    news_hits: list[NewsHit] = []
    if deps.load_news_files is not None:
        brief: Mapping[str, Any] | None = None
        try:
            brief, _watch = deps.load_news_files()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DecisionRound] 唤醒评估：新闻简报读取失败: %s", exc)
        brief_ts = parse_brief_ts(brief) or 0.0
        held = set(codes)
        seen: set[str] = set()
        decisions = (brief or {}).get("decisions")
        for item in decisions if isinstance(decisions, list) else []:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip()
            if code not in held or code in seen:
                continue
            seen.add(code)
            decision = str(item.get("decision") or "")
            if decision not in ("P0", "P1"):
                continue
            news_hits.append(
                NewsHit(
                    code=code,
                    name=str(item.get("name") or ""),
                    verdict=str(item.get("direction") or ""),
                    impact=as_float(item.get("impact")),
                    event_type=str(item.get("event_type") or ""),
                    headline=str(item.get("reason") or ""),
                    decision=decision,
                    pre5=as_float(item.get("pre5")),
                    layer=int(as_float(item.get("layer")) or 0),
                    decision_id=str(item.get("decision_id") or ""),
                )
            )
        items = (brief or {}).get("holdings")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip()
            if code not in held or code in seen:
                continue
            seen.add(code)
            news_hits.append(
                NewsHit(
                    code=code,
                    name=str(item.get("name") or ""),
                    verdict=str(item.get("verdict") or ""),
                    impact=as_float(item.get("impact")),
                    event_type=str(item.get("event_type") or ""),
                    headline=str(item.get("headline") or ""),
                )
            )
    return WakeObs(
        codes=tuple(codes),
        names=names,
        pnl_pct=pnl_pct,
        day_chg=day_chg,
        imb5=imb5,
        brief_ts=brief_ts,
        news_hits=tuple(news_hits),
    )


async def wake_tick(
    *,
    deps: RoundDeps | None = None,
    native: Any = None,
    now: datetime | None = None,
    notify: Notifier | None = None,
) -> tuple[RoundResult, ...]:
    """唤醒评估（每次轮询一调）：四类触发 → 合成槽轮 → 复用 ``round_tick``。

    **契约：绝不抛**（runner 里就一行 ``await wake_tick()``，循环不许因它出事）。
    闸门从外到内：总开关 → 交易时段/交易日 → Redis → 评估节拍（60s，兼作评估器
    存活心跳 ``last_eval_ts``）→ 槽位临期不评 → 距上一轮任何轮 ≥20 分钟
    （判据在轮次日志键头）→ 观测 → 四类触发 → 冷却过滤 → 日预算。放行则
    **先落唤醒说明、先推状态、再跑轮**：轮若中途夭折，预算与冷却已经消费，
    不会 60 秒一遍死循环。
    """
    try:
        return await _wake_tick(deps=deps, native=native, now=now, notify=notify)
    except Exception as exc:  # noqa: BLE001 唤醒评估绝不拖累轮次循环
        logger.error("[DecisionRound] 唤醒评估异常: %s", exc, exc_info=True)
        return ()


async def _wake_tick(
    *,
    deps: RoundDeps | None,
    native: Any,
    now: datetime | None,
    notify: Notifier | None,
) -> tuple[RoundResult, ...]:
    if not wake_enabled():
        return ()
    deps = deps or default_round_deps()
    now = now or deps.now()
    try:
        if not deps.is_trading_time(now):
            return ()
        # 报单缓冲（09:15-09:30 / 12:55-13:00）可下单但没有连续竞价行情：唤醒轮
        # 在这段起来只会看到空池（2026-10-19 12:55 三家模型三连中止）。唤醒只认
        # 连续竞价，缓冲期的事件交给随后正点槽（13:00）处理。
        is_continuous = deps.is_continuous_trading or deps.is_trading_time
        if not is_continuous(now):
            return ()
        trading_day = await deps.is_trading_day(now.date())
    except Exception as exc:  # noqa: BLE001 判不了就不评（与 round_tick 同 fail-closed）
        logger.warning("[DecisionRound] 唤醒评估：时段/交易日判定失败，跳过: %s", exc)
        return ()
    if not trading_day:
        return ()
    client = native
    if client is None:
        try:
            client = native_redis_client()
        except Exception as exc:  # noqa: BLE001
            logger.error("[DecisionRound] 唤醒评估：Redis 客户端构造失败: %s", exc)
            return ()
    try:
        return await _wake_cycle(deps, client, now, notify)
    finally:
        if native is None and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


async def _wake_cycle(
    deps: RoundDeps, client: Any, now: datetime, notify: Notifier | None
) -> tuple[RoundResult, ...]:
    """闸门与判据（客户端已就绪、时段已验）。``now`` 为 aware 北京墙钟。"""
    day_str = f"{now:%Y%m%d}"
    state_key = WAKE_STATE_KEY.format(day=day_str)
    try:
        raw = client.hgetall(state_key)
    except Exception as exc:  # noqa: BLE001 状态读不到 → 不评（基线没了 = 判据失真）
        logger.warning("[DecisionRound] 唤醒评估：状态读取失败，跳过: %s", exc)
        return ()
    state = decode_state(raw, day=day_str)
    if now.timestamp() - state.last_eval_ts < WAKE_EVAL_MIN_INTERVAL_S:
        return ()

    def _heartbeat(st: WakeState) -> WakeState:
        return replace(st, last_eval_ts=now.timestamp())

    # 槽位临期不评：正点槽宽限窗里 round_tick 自己会跑，唤醒白烧一轮预算。
    if due_slots(now, grace_min=_wake_grace_min()):
        _write_wake_state(client, state_key, _heartbeat(state))
        return ()
    log_round_ts = _log_head_ts(client)
    if log_round_ts and now.timestamp() - log_round_ts < MIN_ROUND_GAP_MIN * 60.0:
        _write_wake_state(client, state_key, _heartbeat(state))
        return ()
    obs = await gather_wake_obs(deps)
    if obs is None:
        _write_wake_state(client, state_key, _heartbeat(state))
        return ()

    fired, held = filter_triggers(evaluate_triggers(obs, state, now), state, now)
    sector_note = "" if state.sector_gap_day == day_str else _SECTOR_GAP_NOTE
    if not fired:
        new_state = idle_state(state, obs, now=now, log_round_ts=log_round_ts)
        parts = [one_line_note((), held)] if held else []
        if sector_note:
            parts.append(sector_note)
        _write_wake_state(
            client,
            state_key,
            replace(
                new_state,
                last_note="；".join(parts) or new_state.last_note,
                sector_gap_day=day_str,
            ),
        )
        return ()
    if not budget_ok(state) and not any(t.priority for t in fired):
        # 预算用尽：照 ``woken_state`` 记冷却（防 60 秒一遍刷屏），但**不跑轮**。
        new_state = woken_state(
            state, obs, now=now, fired=fired, log_round_ts=log_round_ts
        )
        note = f"日唤醒预算用尽（{DAILY_WAKE_LIMIT} 轮）：{one_line_note(fired, held)}"
        _write_wake_state(
            client,
            state_key,
            replace(new_state, last_note=note, sector_gap_day=day_str),
        )
        logger.warning("[DecisionRound] %s", note)
        return ()

    slot = RoundSlot(f"{now:%H%M}", SCHEMA_INTRADAY)
    round_id = round_id_for(now.date(), slot)
    # 先落说明与状态、再跑轮（顺序即契约，见 wake_tick docstring）。
    try:
        client.set(
            WAKE_CTX_KEY.format(round_id=round_id),
            context_text(fired, now, suppressed=held),
            ex=CTX_TTL_S,
        )
    except Exception as exc:  # noqa: BLE001 说明写失败：轮照跑（退化为普通轮）
        logger.warning("[DecisionRound] 唤醒说明写入失败 %s: %s", round_id, exc)
    new_state = woken_state(state, obs, now=now, fired=fired, log_round_ts=log_round_ts)
    _write_wake_state(
        client,
        state_key,
        replace(
            new_state,
            last_note=one_line_note(fired, held),
            sector_gap_day=day_str,
        ),
    )
    logger.warning(
        "[DecisionRound] 触发唤醒（%s）→ 合成槽 %s：%s",
        now.strftime("%H:%M"),
        slot.label,
        one_line_note(fired, held),
    )
    results = await round_tick(
        deps=deps, native=client, now=now, slot=slot, notify=notify
    )
    for r in results:
        logger.info(
            "[DecisionRound] 唤醒轮 %s agent=%s status=%s decisions=%d legs=%d "
            "submitted=%d note=%s",
            r.round_id,
            r.agent or "-",
            r.status,
            r.decisions,
            r.legs,
            r.submitted,
            r.note,
        )
    return results
