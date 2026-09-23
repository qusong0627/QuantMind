"""``watch`` 决策 → 守护单规则（P2.1c 纯核心：只做映射，不碰 IO）。

为什么这一层是移植的**主力**而不是边角
--------------------------------------
P2.1a 的实测：隔壁 1421 条决策里 ``watch`` 占 **79.2%**（1125 条），买入只有 4 条。
守护意图就是这套系统的主要产出；只搬买卖等于搬了个零头。

移植源是隔壁 ``scripts/live_price_watch.py:363-439`` 的 ``_rules_from_decisions``。
逐条对齐，但**修掉它的一处已知丢弃**：

* ``invalidation``（「什么情况下这个决策就错了」）隔壁 81.1% 的决策产出它、
  解析器也留着它，却在落规则这一步被整条丢掉、哨兵更没读过——记了但从没用过。
  本模块把它随规则一起带出（:attr:`WatchRule.invalidation`），落库位在 P2.1d 的
  决策审计表。这与本仓闸门登记表的 ``falsify`` 是同一个思想：**没有失效条件的
  规则是单向棘轮**，挂上去就没人能说它什么时候该摘。

比例三态（移植自隔壁 2026-09-12 审查 HIGH-1，方向**刻意**与买入相反）
--------------------------------------------------------------------
``watch`` 是「保护已有持仓」的声明，所以漏挂的代价是**当日裸奔**：

* 明说 ``pct=0``（或不表达卖出量）→ **不挂**，落拒绝理由。模型明确说不减，
  替它按全仓挂就是凭空多卖；
* 没给比例 / 给了脏值（``NaN`` / ``"0.3股"``）→ **按全仓挂** + 留注记。
  这不是「猜比例下单」，是保护层的偏向：执行链对脏值可以停手，但条件位漏挂
  等于这个防守位今天没人管。两条都留痕，不静默。

去重（移植自隔壁 001312 实录）
------------------------------
同标、同（止损价, 止盈价, 减仓比例）的重复项只留第一条：两条同价规则会各触发
一次，第二笔白跑一轮并被在途闸门拦下。价位按三位小数归一后比较（``_lvl`` 同口径）。
**不同价位的同标的规则都保留**（先触发的先成交，重复下单由在途闸门兜底）——
那是两个不同的意图，去掉哪个都是替模型做决定。

刻意不做的事（划清与 P2.4 的界）
--------------------------------
* **不写任何存储**：本模块只产出 :class:`WatchPlan`，Redis 读改写（整组替换、
  与人工规则共存）是 P2.4；
* **不定归属**：隔壁按「每个 agent 一整组、最新分析说了算、本轮没 watch 就
  整组清空」管理，本仓执行器的规则表是**一张平表**、没有 owner 字段
  （``normalize_rule`` 还会静默丢掉 ``DEFAULT_RULE`` 之外的键）。整组替换要落地
  必须先给规则表加归属标记——这是 P2.4 的前置，不是本层能顺手补的。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from backend.services.live_trading.services import sltp_executor as executor
from backend.shared.decision.contract import Decision

#: 缺比例 / 脏值时的减仓比例：**全仓**（见模块头的三态说明）
FULL_REDUCE_PCT = 1.0

#: 注记（进规则台账，与拒绝理由分开：注记是「挂上了但要知道这件事」）
NOTE_PCT_MISSING = "决策未给减仓比例 → 按全仓挂条件位"
NOTE_PCT_DIRTY = "减仓比例无法解析 → 按全仓挂条件位（执行链对脏值停手）"
NOTE_PCT_CLAMPED = "减仓比例 >1（疑似百分数）→ 按全仓挂条件位"

#: 拒绝理由
REJECT_NO_CODE = "决策缺 code"
REJECT_NO_LEVEL = "既无 stop_loss 也无 take_profit：没有价位的条件位没有意义"
REJECT_PCT_ZERO = "决策明说 pct≤0（不表达卖出量）→ 不挂条件位"
REJECT_DUPLICATE = "与本轮已挂规则同标的同价位同比例（去重）"

#: 价位归一小数位（去重签名用；与隔壁 `_lvl` 同口径）
_LEVEL_DIGITS = 3


@dataclass(frozen=True)
class WatchRule:
    """一条待挂的守护规则：执行器规则 + 隔壁丢掉的那几个字段。

    :attr:`rule` 是**执行器词表**（``DEFAULT_RULE`` 的键）的**独立字典**——每条
    规则一份，调用方改动不会串到别的规则上。不在该词表里的键会在
    ``executor.normalize_rule`` 里被静默丢掉，所以归属标记之类的东西不能塞进它。
    """

    symbol: str
    rule: dict[str, Any]
    agent: str = ""
    #: 决策在本轮列表里的序号——**组内唯一**的事实来源（台账按它定位到具体那条）
    index: int = 0
    #: 失效条件（模型自己写的「这个决策什么时候就错了」）
    invalidation: str = ""
    risk_amount: float | None = None
    confidence: float = 0.0
    reason: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejection:
    """一条没挂上的 watch 决策及其理由（**必须留痕**：静默少挂一个=当日裸奔）。"""

    index: int
    code: str
    reason: str


@dataclass(frozen=True)
class WatchPlan:
    """一轮 watch 的全部产出。``rules + rejected`` 覆盖本轮**每一条** watch 决策。"""

    rules: tuple[WatchRule, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    agent: str = ""
    #: 全局注记（与逐规则注记分开：那些是规则级事实，这里是本轮级别的）
    notes: tuple[str, ...] = ()

    def outcomes(self) -> dict[int, dict[str, object]]:
        """序号 → 该条决策的执行结果（审计表的 ``armed``/``reject_reason``/``notes``）。

        :meth:`plan_watch` 保证每条 ``watch`` 决策落在 ``rules`` 或 ``rejected``
        之一，故这里对本轮**每条** watch 都有项——没挂上的那半边带着拒绝理由，
        正是隔壁静默丢掉的那部分。非 watch 决策在本层没有结果（买卖段是 P2.3），
        缺席即「本层未接触」，与「挂了但没挂上」不是一回事。
        """
        out: dict[int, dict[str, object]] = {}
        for w in self.rules:
            out[w.index] = {"armed": True, "reject_reason": "", "notes": list(w.notes)}
        for r in self.rejected:
            out[r.index] = {"armed": False, "reject_reason": r.reason, "notes": []}
        return out


def _level(x: float | None) -> float | None:
    """价位归一（去重签名用）：数字 → 三位小数，其余 → None。"""
    if x is None:
        return None
    try:
        return round(float(x), _LEVEL_DIGITS)
    except (TypeError, ValueError):
        return None


def _reduce_pct(d: Decision) -> tuple[float | None, tuple[str, ...]]:
    """watch 的减仓比例 → ``(比例, 注记)``；``None`` = 明说不可执行，不挂。"""
    p = d.pct
    if p.is_dirty:
        return FULL_REDUCE_PCT, (NOTE_PCT_DIRTY,)
    if not p.is_given:
        return FULL_REDUCE_PCT, (NOTE_PCT_MISSING,)
    if p.value <= 0:
        return None, ()
    if p.value > 1:
        return FULL_REDUCE_PCT, (NOTE_PCT_CLAMPED,)
    return p.value, ()


def _rule_dict(d: Decision, symbol: str, reduce_pct: float) -> dict[str, Any]:
    """按**执行器词表**构造规则：只出现 ``DEFAULT_RULE`` 里有的键。

    ``move_stop`` 是单值（「上触 105 就把防守抬到 105」）→ 触发价与目标价**同值**
    即零间隙棘轮（P1.3b 口径，隔壁 49.3% 的决策都是这一形态）。
    """
    rule = dict(executor.DEFAULT_RULE)
    rule.update(
        {
            "symbol": symbol,
            "enabled": True,
            "side": "SELL",
            "stop_loss_price": d.stop_loss,
            "take_profit_price": d.take_profit,
            "move_stop_trigger": d.move_stop,
            "move_stop_to": d.move_stop,
            "reduce_pct": reduce_pct,
        }
    )
    return rule


def watch_signature(rule: WatchRule) -> tuple[str, float | None, float | None, float]:
    """去重签名：标的 + （止损价, 止盈价, 减仓比例）。价位按三位小数归一。"""
    r = rule.rule
    return (
        rule.symbol,
        _level(r.get("stop_loss_price")),
        _level(r.get("take_profit_price")),
        float(r.get("reduce_pct") or FULL_REDUCE_PCT),
    )


def plan_watch(decisions: Iterable[Decision], *, agent: str = "") -> WatchPlan:
    """本轮决策里的 ``watch`` → 待挂规则（其余动作原样忽略）。

    每条 watch 决策**必落**到 ``rules`` 或 ``rejected`` 之一（不丢行）：
    「没挂上」和「没这条决策」在事后排查里是两件事，前者要有人看见理由。
    """
    rules: list[WatchRule] = []
    rejected: list[Rejection] = []
    seen: set[tuple[str, float | None, float | None, float]] = set()

    for i, d in enumerate(decisions or []):
        if not d.is_watch:
            continue
        code = str(d.code or "").strip()
        if not code:
            rejected.append(Rejection(i, "", REJECT_NO_CODE))
            continue
        if d.stop_loss is None and d.take_profit is None:
            # 隔壁同口径：只见 move_stop 的决策不挂（没有触发价位的守护位）
            rejected.append(Rejection(i, code, REJECT_NO_LEVEL))
            continue
        reduce_pct, notes = _reduce_pct(d)
        if reduce_pct is None:
            rejected.append(Rejection(i, code, REJECT_PCT_ZERO))
            continue

        # 先归一标的、再造规则、**最后校验**：被校验的那份就是被交出去的那份
        # （校验一份、使用另一份是这类映射层最典型的自欺）。
        symbol = executor.normalize_symbol(code)
        rule = _rule_dict(d, symbol, reduce_pct)
        reason = executor.rule_reject_reason(rule)
        if reason:
            # 组合校验走执行器的**单源**（本层不复制一份判断）——例如棘轮不成对、
            # reduce_pct 越界。不清掉非法字段接着用：那会把意图悄悄改小。
            rejected.append(Rejection(i, code, reason))
            continue

        candidate = WatchRule(
            symbol=symbol,
            rule=rule,
            agent=agent,
            index=i,
            invalidation=str(d.invalidation or ""),
            risk_amount=d.risk_amount,
            confidence=float(d.confidence or 0.0),
            reason=str(d.reason or ""),
            notes=notes,
        )
        sig = watch_signature(candidate)
        if sig in seen:
            rejected.append(Rejection(i, code, REJECT_DUPLICATE))
            continue
        seen.add(sig)
        rules.append(candidate)

    return WatchPlan(rules=tuple(rules), rejected=tuple(rejected), agent=agent)


__all__ = [
    "FULL_REDUCE_PCT",
    "NOTE_PCT_CLAMPED",
    "NOTE_PCT_DIRTY",
    "NOTE_PCT_MISSING",
    "REJECT_DUPLICATE",
    "REJECT_NO_CODE",
    "REJECT_NO_LEVEL",
    "REJECT_PCT_ZERO",
    "Rejection",
    "WatchPlan",
    "WatchRule",
    "plan_watch",
    "watch_signature",
]
