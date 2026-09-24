"""决策轮**失败可见性**（P4 附-②）：哪一轮要推人、推什么、一天推几次。

问题（迁移计划「P4 附：延期单重放（replay_deferred.py）——本仓的能力缺口」那节）：
一轮里「单没出去」有三个落点——审计行（要不要去看）、状态键（知不知道该看）、
**推送**（不用看就知道）。决策轮前两个都有，第三个此前没有；而**状态键一个读者也
没有**（无 API、无面板，只有 ``scripts/decision_ledger.py`` 这类手动入口）。于是
「计划要卖、单没出去」这条完整链条在系统里的唯一痕迹是审计表的 ``reject_reason``
一列：要人到场才看得见，而人不在场正是需要它的场合。本模块补的就是这一条。

**不做什么**（同属判据，写在这里免得后来者以为漏了）:

* **不重放已出的决策**。腿提交失败**不**自动重发：同 ``round_id`` 的幂等键是**稳定**
  的（``build_llm_decision_client_order_id`` 只吃 round/symbol/side/agent，没有代次
  段），因此「再发一次」与「第一次其实已到柜台」在键上分不开——把幂等命中当成功，
  等于把「不知道」写成「已成交」。重发与否由人到场对着 ``orders`` 定
  （迁移计划的姿态：**报告失败，下一轮重新决策**）。
* **不改 ``done`` 键语义**。把「有腿失败」判成 ``ok=False`` 会让补跑槽**整轮重来**，
  而那一轮里已经成交的腿会被**再下一遍**（补跑槽的 round_id 不同 ⇒ 订单幂等键不同，
  挡不住）。可见性的代价不该是重复下单。

去重口径：**（交易日，家，类别）一天一次**。真出事时值班要知道的是「今天有这类失败」，
不是它出现了几次——次数在状态键与审计表里。去重键**只在送达之后**才记（同止损失败
告警那条纪律）：推失败却先记了键，等于这条告警永远没人看到。
"""

from __future__ import annotations

import json
import logging
from collections.abc import MutableSet, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from backend.shared.alert_delivery import (
    ALERT_TTL_S,
    Alert,
    Notifier,
    deliver_alert,
)
from backend.services.trade.services.decision_round_core import (
    SLOTS,
    STATUS_ABORTED,
    STATUS_ERROR,
    STATUS_LLM_FAILED,
    STATUS_SKIPPED,
    TENANT_ID,
    RoundResult,
    RoundSlot,
)

logger = logging.getLogger(__name__)

#: 告警类别（同时也是去重键的一段）：一类失败一天最多推一条。
ALERT_ABORTED = "aborted"
ALERT_FAILED = "failed"
ALERT_LLM_FAILED = "llm_failed"
ALERT_ERROR = "error"
#: **整天一条轮次都没跑成**——与上面四类不是一回事：那四类都以「跑过一轮」为前提
#: （判据吃的是 ``RoundResult``），而这一类的前提恰恰是**没有** ``RoundResult``。
ALERT_STALLED = "stalled"

#: 去重键存活期（秒）：与 ``alert_delivery.ALERT_TTL_S`` 同值（保留本名给既有读者）。
_ALERT_TTL_S = ALERT_TTL_S

_KEY_FMT = "qm:decision:alert:{day}:{agent}:{kind}"

#: 停滞键形与上面区分开：它没有家段可言（见 ``stall_key``）。
_STALL_KEY_FMT = "qm:decision:alert:{day}:stalled"

#: 手动补跑入口——**唯一出处**：告警正文与 tick 的夭折提示共用这一句，免得两处
#: 各写一遍、改了一处另一处就成了错指令。
MANUAL_RERUN_HINT = (
    "要立刻补这一轮：python backend/scripts/schedule_ctl.py run decision_round --force"
)

#: 查因入口（不带 ``--force``）：CLI 在空结果时**逐项点名**四种成因（没到点/非交易日/
#: 日历读不到/槽位已被认领），且有到点未认领的槽位会真的跑掉——查与补一步到位。
#: 与 ``MANUAL_RERUN_HINT`` 同样只此一处，告警正文引用它而不是另抄一份。
MANUAL_DIAGNOSE_HINT = (
    "查因并补跑：python backend/scripts/schedule_ctl.py run decision_round"
)


#: 一条待推的告警。形状与投递纪律（去重、送达后才记键）在 ``shared.alert_delivery``，
#: 本模块只负责**判据**（哪一轮要推、推什么）。保留 ``RoundAlert`` 这个名字给既有读者。
RoundAlert = Alert


def _label(result: RoundResult) -> str:
    """人话里的轮次名：``09:35 rebalance「pro」``（家段缺省不带，免得空引号）。"""
    slot = result.slot.label if result.slot else "?"
    who = f"「{result.agent}」" if result.agent else ""
    return f"{slot}{who}"


def round_alert(result: RoundResult) -> RoundAlert | None:
    """这一轮有没有要推给人的事；没有就 ``None``。

    ``skipped`` 是「按设计跳过」（补跑槽看到当日已有决策），不是失败——推它等于
    每个补跑槽都误报一次。``llm_failed`` 与「腿失败」分开计类：前者是运维问题
    （欠费/改配置），后者是「本来说要做、结果没做」，处置的人可能都不是同一个。
    """
    if result.status == STATUS_SKIPPED:
        return None
    label = _label(result)
    note = str(result.note or "").strip() or "（无附注）"

    if result.status == STATUS_LLM_FAILED:
        return RoundAlert(
            kind=ALERT_LLM_FAILED,
            level="error",
            title=f"决策轮 {label} 模型未出决策",
            content=f"{note}。\n补跑槽会自动重试；若全天如此，先查模型配置与额度。",
        )

    if result.status == STATUS_ERROR:
        return RoundAlert(
            kind=ALERT_ERROR,
            level="error",
            title=f"决策轮 {label} 执行段异常",
            content=(
                f"{note}。\n本轮部分腿**可能已经真发出去了**——请以 orders 里的实际"
                "委托为准核对，不要按「没执行」重下单。"
            ),
        )

    if result.status == STATUS_ABORTED:
        # ``aborted`` 的语义是**一张单都没发**（在途账读不到 ⇒ fail-closed）。
        return RoundAlert(
            kind=ALERT_ABORTED,
            level="error",
            title=f"决策轮 {label} 中止：一张单都没发",
            content=(
                f"计划 {int(result.legs or 0)} 条腿，因 {note} 全部未提交。\n"
                f"补跑槽（10:05 / 11:05）会自动重来；{MANUAL_RERUN_HINT}"
            ),
        )

    if int(result.failed or 0) > 0:
        return RoundAlert(
            kind=ALERT_FAILED,
            level="error",
            title=f"决策轮 {label} 有 {int(result.failed)} 条腿下单失败",
            content=(
                f"轮次 {result.round_id}：{int(result.submitted or 0)} 条已提交、"
                f"{int(result.failed)} 条失败。\n失败腿**不会自动重发**——请先核对 "
                "orders 里的实际委托，再决定是否补单。"
            ),
        )

    return None


def alert_key(result: RoundResult, kind: str) -> str:
    """去重键：``(交易日, 家, 类别)``。家段为空（单家路径）时也照样分段。"""
    day = result.day.isoformat() if result.day else "?"
    return _KEY_FMT.format(day=day, agent=result.agent or "-", kind=kind)


def stall_key(day: date | None) -> str:
    """停滞去重键：``(交易日, stalled)``——**不带家段**。

    「今天一轮都没有」是账户级事实，不是某一家的事：按家分段会让「A 家没跑」与
    「整条流水线停摆」在键上长得一样，而这两件事的处置完全不同。
    """
    return _STALL_KEY_FMT.format(day=day.isoformat() if day else "?")


def stall_due(
    now: datetime, *, grace_min: int, last_slot: RoundSlot | None = None
) -> bool:
    """「今天的轮次该出结果了」——时刻闸门（纯函数）。

    单独成函数是为了让调用方能**先闸时刻再取数**：worker 每 ``POLL_S``（缺省 30s）
    查一次，绝大多数 tick 的答案都是「还没到点」，此时连 Redis 与日历都不该问。
    ``stall_alert`` 内部也调用它——判据自带闸门，调用方忘了先判也判不出提前告警。
    """
    last = last_slot or SLOTS[-1]
    return now >= last.due_at(now.date()) + timedelta(minutes=max(0, grace_min))


def stall_alert(
    *,
    now: datetime,
    raw_entries: Sequence[object],
    expect_rounds: bool,
    grace_min: int,
    last_slot: RoundSlot | None = None,
    calendar_note: str = "",
) -> Alert | None:
    """整天一轮都没跑成 ⇒ 一条告警；否则 ``None``。**纯函数**（不碰时间也不碰 IO）。

    为什么需要它（``alert_round`` 覆盖不到的那一类）：上面四类告警的判据吃的是
    ``RoundResult``——**「跑过一轮、但跑砸了」**。而 worker 活着、每个 tick 都被
    更早的闸门挡回（典型：交易日历不可用 → ``is_trading_day`` 抛 → 本 tick 不跑）
    时，**一个 ``RoundResult`` 都不会产生**，于是四种告警一条都不发。外部能看到的
    只有 ``trade:decision-round:last`` 冻在上一次，而那把键**没有任何程序化读者**
    ——表现就是「面板全绿、整天没决策」。C07 看得见这种情况吗？看不见：它判的是
    进程心跳，而心跳写在循环体**顶部**，进程活着它就新鲜（``decision_round_runner``）。
    所以这两条是**互补**的：C07 = 进程活着，本条 = 活着且在出活。

    ``expect_rounds`` 由调用方给（**三态里的第三态归调用方**）：正常日子是交易日的
    判定结果；日历读不到时按「工作日就算该出」处理——那是**故意选的方向**，因为
    恰好是这种日子最需要有人知道决策层停了（见 ``_expect_rounds_today``）。
    非交易日传 ``False``：周末 worker 照样每 30s 醒一次，此时「没有轮次」是**对的**。

    ``raw_entries`` 是 ``trade:decision-round:log`` 的原始元素（LPUSH 进去的 JSON
    串）。**读不懂的条目一律跳过而不抛**，解析失败也不当成「跑过了」——判据的失败
    方向必须是「报出来」，不是「静默认为没事」。
    """
    day = now.date()
    if not stall_due(now, grace_min=grace_min, last_slot=last_slot):
        return None
    if not expect_rounds:
        return None
    today = day.isoformat()
    for entry in raw_entries:
        if not isinstance(entry, (str, bytes)):
            continue
        try:
            parsed = json.loads(entry)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and str(parsed.get("day") or "") == today:
            # 今天有轮次——成没成是 ``alert_round`` 的事，本条只问「有没有跑」。
            return None
    last = last_slot or SLOTS[-1]
    note = str(calendar_note or "").strip()
    return Alert(
        kind=ALERT_STALLED,
        level="error",
        title=f"决策轮整天没跑：{day.isoformat()} 一条轮次都没有",
        content=(
            f"已过最后槽位 {last.label} + 宽限 {max(0, grace_min)} 分钟，"
            f"trade:decision-round:log 里今天没有任何一轮。"
            + (f"\n{note}" if note else "")
            + "\n常见成因：①交易日历不可用（降级判定会被拒绝，fail-closed）"
            "②worker 起来但每个 tick 都被更早的闸门挡回。\n"
            f"{MANUAL_DIAGNOSE_HINT}\n"
            f"{MANUAL_RERUN_HINT}"
        ),
    )


def default_notifier() -> Notifier:
    """生产通知器：走 ``publish_notification_async``（落库 → 前端通知中心）。

    ``action_url`` 指交易台：决策台账目前**没有**页面（只有 CLI 入口），指到一个
    不存在的页面比不指更坏；交易台是值班真要去核对委托的地方。
    """

    async def _notify(
        user_id: str, title: str, content: str, level: str = "info"
    ) -> bool:
        from backend.shared.notification_publisher import publish_notification_async

        return bool(
            await publish_notification_async(
                user_id=str(user_id),
                tenant_id=TENANT_ID,
                title=title,
                content=content,
                type="trading",
                level=level,
                action_url="/trading",
            )
        )

    return _notify


async def alert_stall(
    *,
    now: datetime,
    raw_entries: Sequence[object],
    expect_rounds: bool,
    grace_min: int,
    user_id: object,
    redis: Any = None,
    notifier: Notifier | None = None,
    seen: MutableSet[str] | None = None,
) -> bool:
    """该报就报，报成功才记去重键。**绝不抛**（与 ``alert_round`` 同纪律）。

    ``redis`` 是**原生**客户端（键与去重都在它上面），不是本仓那层包装——包装层
    ``set`` 不认 ``ex=`` 且吞异常，投递键会静默写不下去（见 ``alert_delivery``）。

    ``seen`` 必须由**常驻循环**传（与 ``deliver_alert`` 同义）：本条是循环里的判据，
    Redis 读不出来时（而 Redis 挂了正是「整天没跑成」的常见成因之一）去重键也写不
    下去，不传 ``seen`` 就成了每 ``POLL_S`` 一条——把人训练成不看通知。
    """
    draft = stall_alert(
        now=now,
        raw_entries=raw_entries,
        expect_rounds=expect_rounds,
        grace_min=grace_min,
    )
    if draft is None:
        return False
    return await deliver_alert(
        draft,
        key=stall_key(now.date()),
        user_id=user_id,
        redis=redis,
        notifier_factory=(lambda: notifier)
        if notifier is not None
        else default_notifier,
        seen=seen,
        log_prefix="[DecisionRound]",
    )


async def alert_round(
    result: RoundResult,
    *,
    notifier: Notifier | None = None,
    user_id: object,
    redis: Any = None,
) -> bool:
    """该推就推，推成功才记去重键。**绝不抛**：调用点在刚跑完的一轮之后，
    此刻单可能已经真出去了，通知层的毛病不许把它变成异常。

    ``notifier=None`` ⇒ 真有事时才现造生产通知器（``default_notifier``）：
    「没有要推的事」这条路径**连通知设施都不碰**——正常的一轮跑一万次也不该
    在通知链路上留任何足迹。

    返回「这一次真的推出去了吗」。``user_id`` 为空 ⇒ 不推（推给空账户的通知
    落在谁那里都不对），但留 warning——空账户本身就是一条要查的事。
    """
    draft = round_alert(result)
    if draft is None:
        return False
    return await deliver_alert(
        draft,
        key=alert_key(result, draft.kind),
        user_id=user_id,
        redis=redis,
        # 现造而不是先造：见 docstring「没有要推的事就连通知设施都不碰」。
        notifier_factory=(lambda: notifier)
        if notifier is not None
        else default_notifier,
        log_prefix="[DecisionRound]",
    )
