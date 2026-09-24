"""TDX 执行腿（滚动买卖 + L2 实时）失败可见性：哪类失败要推人、推什么、一天几次。

问题（与 ``decision_round_alerts`` 同源）：这两条腿的失败**只落在 ``logger.warning``
上**。判据有、单测有、日志也在写——但日志需要一个到场的人去读，而「单没出去」这件事
恰恰发生在人不在场的时候。L2 实时从 70c538c9 起整整一个重构周期没下过一单，系统里
的全部痕迹就是日志里那行 "too many values to unpack"。

**不做什么**：不重放、不自动补单。腿失败**不**自动重发（同一次信号用同一批
幂等号，重发与「第一次其实已到柜台」在键上分不开）；重发与否由人到场对着 orders 定
——与决策轮告警同一条姿态。

去重口径：**（交易日，执行腿，类别）一天一次**（纪律在 ``shared.alert_delivery``：
送达之后才记键）。真出事时值班要知道的是「今天有这类失败」，次数在状态里。

**恢复通知**：连续循环（L2）与事件驱动（滚动）都有一个干净态，所以告警响起后必须
能落地——一次干净的运行/周期会把今天报过的类别合成一条「已恢复」。没有它，一条
告警挂在那里就再也分不清「还在坏」和「早好了」。

**时段闸门**：`positions_error` / `stalled` 只在交易时段推。非交易时段通达信客户端
通常不在线，此时「持仓读不到」没有让任何可做的事落空（预警与真单本来也出不去）；
交易时段内它才等于「本可以做的事没做」。时段判据用
``trading_session.is_trading_time``（与 QMT 轮询器/真单镜像同一口径），**由调用方
算好放进状态**——判据函数保持纯函数，可离线测。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableSet
from datetime import date
from typing import Any

from backend.shared.alert_delivery import (
    Alert,
    Notifier,
    NotifierFactory,
    alert_sent,
    deliver_alert,
)
from backend.services.live_trading.services.trading_session import now_shanghai

logger = logging.getLogger(__name__)

#: 执行腿（去重键的一段，也决定文案里自称什么）。
FAMILY_ROLLING = "rolling"
FAMILY_L2 = "l2"

FAMILY_LABELS = {
    FAMILY_ROLLING: "滚动买卖执行腿",
    FAMILY_L2: "L2 实时执行腿",
}

#: 告警类别（同时也是去重键的一段）。
KIND_ABORTED = "aborted"  # 整条腿没跑起来（无分数/桥没配）
KIND_POSITIONS_UNREADABLE = "positions_unreadable"  # 持仓读不到 → 信号整体跳过
KIND_ORDERS_FAILED = "orders_failed"  # 有腿真去下单但失败
KIND_CYCLE_ERROR = "cycle_error"  # L2 循环本周期异常
KIND_EXEC_ERROR = "exec_error"  # L2 执行段没能执行
KIND_STALLED = "stalled"  # 数据链断了/池子空了 → 触发已关

#: 可「恢复」的类别：一次干净运行后要发已恢复通知的那几类。`aborted` 不在此列
#: ——它每次运行重新判定（今天的推理没信号，不会因为下一轮干净就"恢复"）。
RESOLVABLE_KINDS = (
    KIND_POSITIONS_UNREADABLE,
    KIND_ORDERS_FAILED,
    KIND_CYCLE_ERROR,
    KIND_EXEC_ERROR,
    KIND_STALLED,
)

#: 池子连续不足多少轮才算「停摆」（L2 周期约 60s ⇒ 约 10 分钟）。低于它只说明
#: 池子还在构建（开盘头几分钟属正常），此时报警是每天一次的假警。
POOL_STARVED_ALERT_CYCLES = 10

#: 告警正文里最多列几条失败腿（屏幕上一屏能读完为准；总数在标题里）。
_MAX_LISTED = 5

_KEY_FMT = "qm:tdx:alert:{day}:{family}:{kind}"
_RESOLVED_KEY_FMT = "qm:tdx:alert:resolved:{day}:{family}"

#: 账户坐标的产品口径：与这两条腿运行时的账户同源（``TDX_ACCOUNT_USER_ID``）。
TENANT_ID = "default"


def alert_key(family: str, kind: str, day: date) -> str:
    """去重键：``(交易日, 执行腿, 类别)``。"""
    return _KEY_FMT.format(day=day.isoformat(), family=family, kind=kind)


def resolved_key(family: str, day: date) -> str:
    """恢复键：一天一条，**按腿不分类别**——三类一起好时推三条「已恢复」是刷屏。"""
    return _RESOLVED_KEY_FMT.format(day=day.isoformat(), family=family)


def _one_line(text: str, limit: int = 60) -> str:
    """压成一行并截断（通知标题要能在一行里读完）。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _list_failures(items: Any, limit: int = _MAX_LISTED) -> str:
    """失败腿清单（``symbol 方向：原因``），最多 ``limit`` 条，其余归入"另 N 条"。"""
    rows = [i for i in (items or []) if isinstance(i, Mapping)]
    lines = []
    for row in rows[:limit]:
        sym = str(row.get("symbol") or "?")
        side = {"buy": "买", "sell": "卖"}.get(str(row.get("side") or ""), "")
        lines.append(f"  {sym} {side}：{_one_line(row.get('error') or '未知原因', 80)}")
    if len(rows) > limit:
        lines.append(f"  …另 {len(rows) - limit} 条（见状态/日志）")
    return "\n".join(lines)


# ── 判据：滚动买卖腿 ────────────────────────────────────────────────
def rolling_alerts(result: Mapping[str, Any]) -> list[Alert]:
    """一次 ``run_rolling_push`` 的结果里有没有要推给人的事。

    输入是**结果字典本身**（``run_rolling_push`` 的返回值）：判据读的字段
    （``positions_error`` / ``signals_suppressed`` / ``failed_orders`` / ...）
    同时也是「设置→实盘」面板要显示的事实，一处产出两处用。
    """
    out: list[Alert] = []

    err = str(result.get("error") or "").strip()
    if not result.get("success") and err:
        # 早退型失败（无分数 / 桥没配）：没有更多事实可说，一条就够。
        return [
            Alert(
                kind=KIND_ABORTED,
                level="error",
                title=f"滚动买卖未执行：{_one_line(err)}",
                content=(
                    f"{err}\n"
                    "本轮**没有产生任何委托**（预警与真单都没有）。\n"
                    "执行模式与分数阈值在「设置→实盘」的滚动买卖面板；"
                    "若反复出现，查后端日志里 [TdxRolling] 段。"
                ),
            )
        ]

    pos_err = str(result.get("positions_error") or "").strip()
    suppressed = int(result.get("signals_suppressed") or 0)
    if pos_err and suppressed and bool(result.get("in_trading_hours")):
        out.append(
            Alert(
                kind=KIND_POSITIONS_UNREADABLE,
                level="error",
                title=f"滚动买卖 本轮 {suppressed} 条信号跳过：持仓读不到",
                content=(
                    f"{pos_err}\n"
                    f"本轮 {suppressed} 条买卖信号（含预警与真单）**整体未发**——"
                    "持仓未知时算什么都是错的：卖出腿会全空（想卖的卖不掉），"
                    "买入腿会当成空仓买满一篮子。\n"
                    "不用手工补：下一次推理完成后会自动重来。若持续如此，"
                    "先查通达信桥与持仓接口。"
                ),
            )
        )

    failed = result.get("failed_orders") or []
    if failed:
        out.append(
            Alert(
                kind=KIND_ORDERS_FAILED,
                level="error",
                title=f"滚动买卖 {len(failed)} 条腿下单失败",
                content=(
                    f"{_list_failures(failed)}\n"
                    "失败腿**不会自动重发**（同一批信号用同一批幂等号，重发与"
                    "「第一次其实已到柜台」在键上分不开）。\n"
                    "请先核对 orders 里的实际委托，再决定是否补单。"
                ),
            )
        )
    return out


# ── 判据：L2 实时腿 ────────────────────────────────────────────────
def l2_alerts(status: Mapping[str, Any]) -> list[Alert]:
    """L2 实时循环**本周期**状态里有没有要推给人的事。

    输入是 ``realtime_status``（同一份字典既镜像进 Redis 给人看、也喂给这里判读）。
    """
    out: list[Alert] = []

    cycle_err = str(status.get("cycle_error") or "").strip()
    if cycle_err:
        out.append(
            Alert(
                kind=KIND_CYCLE_ERROR,
                level="error",
                title=f"L2 实时 本周期异常：{_one_line(cycle_err)}",
                content=(
                    f"{cycle_err}\n"
                    "本周期整段跳过（本周期没有评分、没有下单）。循环仍在跑，"
                    "下一周期自动重试；连续出现说明是稳定故障，查后端日志 "
                    "[TdxL2] 段（异常已带 exc_info，有文件行号）。"
                ),
            )
        )

    pos_err = str(status.get("positions_error") or "").strip()
    skipped = int(status.get("trigger_skipped_symbols") or 0)
    if pos_err and skipped and bool(status.get("in_trading_hours")):
        out.append(
            Alert(
                kind=KIND_POSITIONS_UNREADABLE,
                level="error",
                title="L2 实时 本周期触发已跳过：持仓读不到",
                content=(
                    f"{pos_err}\n"
                    f"本周期池内 {skipped} 只标的的触发判断**整体未做**"
                    "（在途单重挂一并跳过）——持仓未知时卖出腿会全空、"
                    "买入腿会当成空仓买满一篮子。\n"
                    "下一周期自动重试。若持续如此，先查通达信桥与持仓接口。"
                ),
            )
        )

    exec_err = str(status.get("exec_error") or "").strip()
    if exec_err:
        out.append(
            Alert(
                kind=KIND_EXEC_ERROR,
                level="error",
                title=f"L2 实时 执行段未执行：{_one_line(exec_err)}",
                content=(
                    f"{exec_err}\n"
                    "本轮触发信号**没有送到券商**。多为执行模式配置异常，"
                    "在「设置→实盘」的 L2 面板核对执行模式（off/paper/tdx）。"
                ),
            )
        )

    n_failed = int(status.get("orders_failed_count") or 0)
    if n_failed:
        out.append(
            Alert(
                kind=KIND_ORDERS_FAILED,
                level="error",
                title=f"L2 实时 {n_failed} 条腿下单失败",
                content=(
                    f"{_list_failures(status.get('orders_failed'))}\n"
                    "失败腿**不会自动重发**；同标的的下一轮触发会被冷却/在途抑制集"
                    "挡住。请先核对 orders 里的实际委托，再决定是否补单。"
                ),
            )
        )

    if bool(status.get("in_trading_hours")):
        starved = int(status.get("pool_starved_cycles") or 0)
        stale = bool(status.get("capture_stale"))
        if stale or starved >= POOL_STARVED_ALERT_CYCLES:
            why = (
                "采集链路已陈旧（capture 任务不再更新周期戳）"
                if stale
                else f"因子池连续 {starved} 个周期不足 5 只"
            )
            out.append(
                Alert(
                    kind=KIND_STALLED,
                    level="warning",
                    title="L2 实时 数据链停摆：触发已关",
                    content=(
                        f"{why}。\n"
                        "循环还活着（分数照写），但**触发执行已关闭**——"
                        "拿断裂链路残留的陈旧因子下单比不下单更坏。\n"
                        "查采集任务 tdx-l2-capture 与通达信桥；池子大小见状态键 "
                        "tdx:l2:realtime:status 的 pool_size / pool_stale_skipped。"
                    ),
                )
            )
    return out


# ── 投递 ────────────────────────────────────────────────────────────
def default_notifier() -> Notifier:
    """生产通知器：走 ``publish_notification_async``（落库 → 前端通知中心）。

    ``action_url`` 指交易台——执行腿的处置动作（核对委托、看面板、改执行模式）
    都在那里，与决策轮告警指同一页（两处指不同页只会让人两头找）。
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


async def alert_exec_leg(
    family: str,
    alerts: list[Alert],
    *,
    user_id: object,
    redis: Any = None,
    notifier_factory: NotifierFactory | None = None,
    day: date | None = None,
    seen: MutableSet[str] | None = None,
) -> int:
    """逐条投递（各自去重）。返回**真的推出去了**几条。**绝不抛**。

    调用点是「刚跑完一次」之后：那里可能已经有真单出去了，通知层的毛病
    不许把那次运行的结果变成异常。

    ``seen``：循环型调用方传进程内去重集（理由见 ``alert_delivery.deliver_alert``）。
    """
    if not alerts:
        return 0
    today = day or now_shanghai().date()
    sent = 0
    for alert in alerts:
        try:
            delivered = await deliver_alert(
                alert,
                key=alert_key(family, alert.kind, today),
                user_id=user_id,
                redis=redis,
                notifier_factory=notifier_factory or default_notifier,
                seen=seen,
                log_prefix=f"[TdxExec:{family}]",
            )
        except Exception as exc:  # noqa: BLE001 兜底：投递纪律已自带 try，这里只防
            logger.warning(
                "[TdxExec:%s] 告警投递异常（%s）: %s", family, alert.kind, exc
            )
            continue
        sent += int(delivered)
    return sent


async def resolve_exec_leg(
    family: str,
    *,
    user_id: object,
    redis: Any = None,
    notifier_factory: NotifierFactory | None = None,
    day: date | None = None,
    at: str = "",
) -> bool:
    """今天报过、现在干净了 ⇒ 推一条「已恢复」（一天最多一条，按腿）。

    只在**本周期/本次运行没有任何告警**时调用（调用方负责这个前提：先看
    ``alerts`` 空不空）。判断"今天报过"用告警键本身——键只在送达之后才写，
    所以它存在 ⟺ 人真的看到过那条告警。
    """
    if redis is None:
        return False
    today = day or now_shanghai().date()
    reported = [
        k for k in RESOLVABLE_KINDS if alert_sent(redis, alert_key(family, k, today))
    ]
    if not reported:
        return False
    label = FAMILY_LABELS.get(family, family)
    stamp = at or now_shanghai().strftime("%H:%M")
    return await deliver_alert(
        Alert(
            kind="resolved",
            level="success",
            title=f"{label}已恢复",
            content=(
                f"今天报过的失败（{('、'.join(reported))}）在 {stamp} 的运行里已不再出现，"
                "执行腿恢复正常。\n"
                "今天早些时候的那些失败仍然成立——若期间有计划中的买卖，"
                "请核对 orders 与实际持仓后再决定是否补。"
            ),
        ),
        key=resolved_key(family, today),
        user_id=user_id,
        redis=redis,
        notifier_factory=notifier_factory or default_notifier,
        log_prefix=f"[TdxExec:{family}]",
    )
