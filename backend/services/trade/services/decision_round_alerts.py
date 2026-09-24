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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.services.trade.services.decision_round_core import (
    STATUS_ABORTED,
    STATUS_ERROR,
    STATUS_LLM_FAILED,
    STATUS_SKIPPED,
    TENANT_ID,
    RoundResult,
)

logger = logging.getLogger(__name__)

#: 告警类别（同时也是去重键的一段）：一类失败一天最多推一条。
ALERT_ABORTED = "aborted"
ALERT_FAILED = "failed"
ALERT_LLM_FAILED = "llm_failed"
ALERT_ERROR = "error"

#: 去重键存活期（秒）。跨过一整个交易日即可：口径是「一天一次」，不是「一段时间一次」。
_ALERT_TTL_S = 90_000

_KEY_FMT = "qm:decision:alert:{day}:{agent}:{kind}"

#: 手动补跑入口——**唯一出处**：告警正文与 tick 的夭折提示共用这一句，免得两处
#: 各写一遍、改了一处另一处就成了错指令。
MANUAL_RERUN_HINT = (
    "要立刻补这一轮：python backend/scripts/schedule_ctl.py run decision_round --force"
)


@dataclass(frozen=True, slots=True)
class RoundAlert:
    """一条待推的告警（`level` 只取 ``publish_notification`` 认识的档位）。"""

    kind: str
    level: str
    title: str
    content: str


#: 通知器形状：``(user_id, title, content, level) -> Awaitable``。**账户坐标不在参数
#: 里**（与 ``Submitter`` 同一条纪律：它属于「这一轮是谁在跑」，构造时闭合进去）。
Notifier = Callable[..., Awaitable[Any]]


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


def _already_sent(redis: Any, key: str) -> bool:
    """读过键没有。**读不到按「没推过」办**（重复推优于沉默）。"""
    try:
        return bool(redis.get(key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DecisionRound] 告警去重键读取失败 %s: %s", key, exc)
        return False


def _remember(redis: Any, key: str) -> None:
    """记下「已推」。写失败只告警：下次会重复推一条，这不是要拦下的错。"""
    try:
        redis.set(key, "1", ex=_ALERT_TTL_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DecisionRound] 告警去重键写入失败 %s: %s", key, exc)


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

    uid = str(user_id or "").strip()
    if not uid:
        logger.warning(
            "[DecisionRound] %s 有失败（%s）但账户坐标为空，通知未推: %s",
            result.round_id,
            draft.kind,
            draft.title,
        )
        return False

    key = alert_key(result, draft.kind)
    if redis is not None and _already_sent(redis, key):
        return False

    send = notifier if notifier is not None else default_notifier()
    try:
        delivered = bool(await send(uid, draft.title, draft.content, draft.level))
    except Exception as exc:  # noqa: BLE001 通知炸了不许带走这一轮的结果
        logger.warning(
            "[DecisionRound] 通知发送失败（%s）: %s", draft.kind, exc, exc_info=True
        )
        return False

    if delivered and redis is not None:
        _remember(redis, key)
    if not delivered:
        logger.warning(
            "[DecisionRound] 通知未送达（%s）：下一轮同类失败会再试", draft.kind
        )
    return delivered
