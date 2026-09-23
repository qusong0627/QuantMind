"""决策执行段纯核心：**决策 → 腿计划 + 否决记录**（不取数、不下单、不碰 Redis）。

分层（与 ``context`` / ``llm_call`` 同一条线）
---------------------------------------------
本模块回答的是「模型说的这一条，能变成一张什么单」：

* 取数（持仓、行情、额度、在途委托）在 ``services/trade/services/decision_executor.py``；
* 下单在 :func:`~backend.services.simulation.services.order_router.submit_order`；
* 守护单（``watch``）在 P2.4 的 ``watch_map`` → sltp 规则表，本层只把它们**点出来**
  （``ExecutionPlan.watches``），不落规则。

分家的收益与前两批一样：**全部分支都能在无网络、无账户下断言**——「卖出非持仓」
「T+1 可卖量为 0」「跌停不接」「同轮重复决策同一代码」这些真线上一天才碰一次的分支，
在这里是构造几个对象的事。

与既有同族模块的边界（别在这里重复实现）
----------------------------------------
* ``gates.check_buy`` —— 买入侧闸门（标的边界/池成员/涨跌停/虚拟现金/买不起一手）。
  本层**调用**它，不复制任何一条判定。
* ``lot_rules.align_sell_quantity`` / ``push_plan.align_buy_quantity`` —— 数量整手口径。
  本层**调用**它们，不自己算板别与手数。
* ``lot_rules.resolve_limit_price`` —— 限价唯一出处。本层只传 ``max_slip``。

不复用 ``push_plan.plan_quantity`` 的**后半段**（problem 文案）是刻意的：那一层面向
推送确认面板，产出的是给人读的中文；本层产出的是可聚合的 ``rule_id``（影子代价账按
id 分组，拿句子当键会在改文案那天断档）。算术复用，措辞不复用。

不做的事
--------
* **时段闸与账户级冻结不在这里**：它们决定「这一轮要不要执行」，不是「这一条决策该
  不该执行」。调用点不执行即可（决策照常入账，见 P2.3a 的审计表）；把「收盘了」记成
  某一只票的否决理由，会和 ``l0.session`` 的分栏口径打架（见 ``push_plan`` 的
  ``classify_decisions``）。
* **不查真钱**：``quota`` 是调用点给的额度（子账户口径），账户级现金/杠杆/单票占比
  由 ``OrderRouter`` 内的风控引擎在提交时判——那才是唯一的事实源。
* **不做卖出侧的涨跌停阈值计算**：阈值一律由调用点注入（唯一事实源见 ``gates`` 模块
  docstring），本层只比大小。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.services.live_trading.services.lot_rules import (
    align_sell_quantity,
    resolve_limit_price,
)
from backend.shared.decision.contract import (
    BUY,
    FRAC_DIRTY,
    FRAC_OK,
    FRAC_ZERO,
    HOLD,
    SELL,
    WATCH,
    Decision,
    DecisionBatch,
)
from backend.shared.decision.gates import BuyGate, check_buy
from backend.shared.push_plan import align_buy_quantity
from backend.shared.stock_utils import StockCodeUtil

__all__ = [
    "ALL_RULE_IDS",
    "ExecutionPlan",
    "Holding",
    "LIMIT_SLIP",
    "Leg",
    "Quote",
    "RULE_INFLIGHT_DUP",
    "RULE_NO_QUOTE",
    "RULE_SELL_LIMIT_DOWN",
    "RULE_SELL_NOT_HELD",
    "Veto",
    "at_limit_down",
    "inflight_dup",
    "no_quote",
    "plan_orders",
    "sell_not_held",
]

# ── 本族规则 id（执行段：从「决策已过买入闸」到「腿交出去」之间那几步）──────
#: 卖出没有对应持仓。**单列**而不并进 ``l4.symbol_boundary``：那条判的是
#: 「这只票该不该碰」（黑名单/ST），这条判的是「这本账里有没有它」——多模型分账下
#: 后者是**跨 agent 卖仓**的防线，原因与处置都不同。
RULE_SELL_NOT_HELD = "l2.sell_not_held"
#: 无有效参考价（算不出股数、报不出价）。
RULE_NO_QUOTE = "l3.no_quote"
#: 同标的同方向已有未确认的在途委托（或同轮已产出同向腿）。
RULE_INFLIGHT_DUP = "l3.inflight_dup"
#: 卖出侧的跌停不接。**与买入侧的 ``l4.limit_down`` 分列**：同一个谓词、两套经济含义
#: （买入侧是「别接飞刀」，卖出侧是「当天最差价且未必成交」），合用一个 id 会让影子
#: 账把两族的样本混在一起算代价。
RULE_SELL_LIMIT_DOWN = "l4.sell_limit_down"

#: 本族全部规则 id。**新增规则必须同时**：加常量、进本元组、在
#: ``shared/risk/gate_registry.py`` 加条目——三处缺一即红
#: （``tests/test_risk_gate_registry.py`` 按**三族并集**双向覆盖）。
ALL_RULE_IDS: tuple[str, ...] = (
    RULE_SELL_NOT_HELD,
    RULE_NO_QUOTE,
    RULE_INFLIGHT_DUP,
    RULE_SELL_LIMIT_DOWN,
)

#: 限价相对基准价的缓冲。**1%** 是隔壁实测口径（``exec_cost`` 的价带说明：
#: 「买入 = 基准 +1%，卖出 = 基准 −1%」），不是本仓 ``resolve_limit_price`` 的 2%
#: 默认值——决策腿是「贴着打保成交」，挂远了等于把这一轮意图作废。
LIMIT_SLIP = 0.01


def _norm(code: object) -> str:
    """任意形态代码 → 后缀式（``600036.SH``）。与 ``gates._norm`` 同口径。"""
    return StockCodeUtil.to_suffix(str(code or "").strip())


def _finite(x: object) -> float | None:
    """宽松转 float；``bool``/非数/非有限 → ``None``（按「没给」处理，绝不臆造）。"""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ── 入参对象（调用点已经从各存储取好）────────────────────────────────
@dataclass(frozen=True)
class Holding:
    """本 agent 名下的一只持仓（**分账后**的口径，不是账户总持仓）。"""

    symbol: str
    #: 可卖数量（T+1 之后、扣掉已被挂单占用的部分）。
    available: float = 0.0
    name: str = ""


@dataclass(frozen=True)
class Quote:
    """一只票的行情切片。缺哪一项就哪一项判不了（不补默认值）。"""

    symbol: str
    price: float | None = None  # 现价：算股数、报限价的基准
    day_chg_ratio: float | None = None  # 当日涨跌幅（**比例**，0.1 = +10%）
    limit_threshold_ratio: float | None = None  # 当日涨跌停阈值（**比例**）
    #: 是否停牌。``None`` = **不知道**（不是「没停牌」，见 ``gates.check_halted``）。
    halted: bool | None = None


# ── 输出对象 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Leg:
    """一张要下的单（价格已定，数量已整手归一）。

    ``index`` = 这条腿对应 ``batch.decisions`` 里的**第几条**。必须有：审计表按序号
    回填执行结果（``decision_ledger_store.build_records`` 的 ``outcomes``），而同一批
    里可以出现两个同标的（第二个会被去重拦掉）——按代码回填会把结果挂错行。
    """

    index: int
    symbol: str
    side: str  # buy | sell
    quantity: float
    limit_price: float | None
    reason: str
    note: str = ""

    @property
    def is_buy(self) -> bool:
        return self.side == BUY

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "reason": self.reason,
            "note": self.note,
        }


@dataclass(frozen=True)
class Veto:
    """一条被拦下的决策（``rule`` 是稳定的 id，留痕/代价账按它聚合）。"""

    index: int
    symbol: str
    side: str
    rule: str
    reason: str
    evidence: tuple[tuple[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "rule_id": self.rule,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """一轮决策的执行计划（成功与否同构：腿是能下的，其余全是记录）。"""

    legs: tuple[Leg, ...] = ()
    vetoes: tuple[Veto, ...] = ()
    #: 明确不动作的决策（``hold``，或卖出比例为 0 这类「模型说了不做」）。
    #: 只记代码：理由在批次里，这里再存一份迟早分叉。
    noops: tuple[str, ...] = ()
    #: 只记代码：``watch`` 的规则落库是 P2.4 的事，本层不碰 sltp 表。
    watches: tuple[str, ...] = ()
    #: 必须可见的说明（某项没判、口径回退…）。放行的腿也常常带着它。
    notes: tuple[str, ...] = ()

    @property
    def buys(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in self.legs if leg.is_buy)

    @property
    def sells(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in self.legs if not leg.is_buy)


def _veto(
    index: int, symbol: str, side: str, rule: str, reason: str, **evidence: Any
) -> Veto:
    return Veto(index, symbol, side, rule, reason, tuple(evidence.items()))


# ── 本族的四条判据（模块级纯函数：登记表的 ``where`` 要能机械解析到这里）──
def sell_not_held(symbol: str, holdings: Mapping[str, Holding]) -> bool:
    """本账里有没有这只票（``symbol`` 已归一，空串一律算「没有」）。"""
    return not symbol or symbol not in holdings


def no_quote(price: float | None) -> bool:
    """现价是否不可用（缺失/非数/非有限/≤0）——算不出股数，也报不出限价。"""
    px = _finite(price)
    return px is None or px <= 0


def inflight_dup(symbol: str, side: str, *, inflight: frozenset, placed: set) -> bool:
    """同标的同方向是否已经有了：本轮已出腿（``placed``）或跨轮在途（``inflight``）。"""
    return (symbol, side) in placed or (symbol, side) in inflight


def at_limit_down(
    day_chg_ratio: float | None, limit_threshold_ratio: float | None
) -> bool:
    """是否已到跌停板（两个入参都是**比例**：0.098 = 当日跌 9.8%）。

    「到板」用 ``<=``（含等号）。**缺一不判**（返回 ``False`` 由调用点留痕）——
    阈值与跌幅都不可得时猜一个的代价是拦下一批本该卖出的单。阈值 ``≤0`` 同样不判
    （不是「跌停幅度为 0」的意思，是配置缺失）。

    但**单位错不在此列**：阈值 ``> 1`` 抛 ``ValueError``。A 股最大跌幅 20%，
    ``thr > 1`` 时 ``chg <= -thr`` 恒假 ⇒ ``l4.sell_limit_down`` 一条都不拦，
    而账面看不出任何异常（这正是「静默失效的闸门」的样子）。与
    :func:`~backend.shared.decision.gates.check_limit_reach` 同口径（那里也 raise）。
    """
    chg, thr = _finite(day_chg_ratio), _finite(limit_threshold_ratio)
    if chg is None or thr is None or thr <= 0:
        return False
    if thr > 1:
        raise ValueError(
            f"limit_threshold_ratio={thr!r} 超出比例区间 (0, 1]——"
            f"这看起来是百分点（如 10.0）而不是比例（如 0.1）；"
            f"静默按错单位比较会让跌停腿一条都不拦"
        )
    return chg <= -thr


def _limit(side: str, base: float | None) -> tuple[float | None, str]:
    """限价：``resolve_limit_price`` 的单边带（唯一出处），缓冲用 :data:`LIMIT_SLIP`。

    基准价不可用 → ``(None, "no_reference_price")``：**绝不臆造一个价**。调用点把
    ``None`` 解释为「不带限价」，由下游按市价口径处置。
    """
    price, problem = resolve_limit_price(side, base, max_slip=LIMIT_SLIP)
    return (price, "") if not problem else (None, problem)


def plan_orders(
    batch: DecisionBatch,
    *,
    holdings: Mapping[str, Holding] | None = None,
    quotes: Mapping[str, Quote] | None = None,
    gate: BuyGate | None = None,
    quota: float | None = None,
    new_buys_round: int = 0,
    inflight: frozenset[tuple[str, str]] = frozenset(),
) -> ExecutionPlan:
    """一条决策批次 → 执行计划（**纯函数**，同一入参永远同一结果）。

    参数：

    * ``holdings`` —— 本 agent 名下的持仓（键可以是前缀式或后缀式，内部归一）；
    * ``quotes`` —— 行情切片，键同上；某只票缺席 = 那只票的行情项一律「不判」；
    * ``gate`` —— ``gates.BuyGate``（买入侧的全部外部约束）；``None`` = 本轮不做买入
      （见末条）；
    * ``quota`` —— 买入可用额度（子账户口径）。``None`` = 未知 → 买入腿**不下**
      （不知道能花多少就不花），记 ``l1.available_cash`` 否决；
    * ``new_buys_round`` —— 本轮**已**产生的新开仓数（在途，由调用点传入）；
    * ``inflight`` —— 已有未确认委托的 ``(symbol, side)`` 集合（后缀式）。

    判定顺序（每条决策）：
    ``规范代码 → 在途重复 → 类型专属判定 → 数量 → 价 → 出腿``

    * ``hold`` → ``noops``；``watch`` → ``watches``（P2.4 落地，这里只点出来）；
    * **卖出永不因「看空」被拦**（``gates`` 的纪律）：本层对卖出的每一条否决都是
      「这张单下不出去或不该由本账下」，不是「不该卖」；
    * ``gate=None`` 时买入决策进 ``noops``：调用点自己已经决定本轮不做买入（用户指令
      冻结 / 时段外），记成「被闸拦下」会凭空给某条规则记一笔并不存在的成本。
    """
    plans = _Plan(
        batch=batch,
        holdings=holdings,
        quotes=quotes,
        gate=gate,
        quota=quota,
        new_buys_round=new_buys_round,
        inflight=inflight,
    )
    plans.run()
    # 卖出腿排前：先回收资金再买（隔壁执行段的顺序），且买入额度由调用点在成交后重取。
    legs = tuple(plans.sell_legs) + tuple(plans.buy_legs)
    return ExecutionPlan(
        legs=legs,
        vetoes=tuple(plans.vetoes),
        noops=tuple(plans.noops),
        watches=tuple(plans.watches),
        notes=tuple(plans.notes),
    )


class _Plan:
    """累加器（可变局部对象）：把「每条决策判一遍」与「产出一份不可变计划」分开。"""

    def __init__(
        self,
        *,
        batch: DecisionBatch,
        holdings: Mapping[str, Holding] | None,
        quotes: Mapping[str, Quote] | None,
        gate: BuyGate | None,
        quota: float | None,
        new_buys_round: int,
        inflight: frozenset[tuple[str, str]],
    ) -> None:
        self.batch = batch
        self.holdings = {_norm(k): v for k, v in (holdings or {}).items() if _norm(k)}
        self.quotes = {_norm(k): v for k, v in (quotes or {}).items() if _norm(k)}
        self.gate = gate
        self.quota = _finite(quota)
        #: 已有未确认委托的 ``(code, side)``（跨轮；调用点从在途账查）
        self.inflight = {(_norm(s), str(sd).strip().lower()) for s, sd in inflight}
        self.new_buys = int(new_buys_round or 0)
        self.sell_legs: list[Leg] = []
        self.buy_legs: list[Leg] = []
        self.vetoes: list[Veto] = []
        self.noops: list[str] = []
        self.watches: list[str] = []
        self.notes: list[str] = []
        #: 本轮已出腿的 ``(code, side)``：模型同一批里重复决策同一代码时不再下第二张
        #: （真线上更常见的是**跨轮**重复，那由 ``inflight`` 挡）。
        self.placed: set[tuple[str, str]] = set()

    # ── 工具 ────────────────────────────────────────────────────────
    def quote(self, symbol: str) -> Quote:
        return self.quotes.get(symbol) or Quote(symbol=symbol)

    def note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    def dup(self, index: int, side: str, symbol: str) -> bool:
        """同标的同方向是否已经有了：跨轮（``inflight``）或本轮（``placed``）。

        理由分两种写：本轮重复是**模型自己**在同一批里说了两遍，跨轮重复是
        **在途委托没回执**——排查方向不同，不能合并成一句话。
        """
        if not inflight_dup(symbol, side, inflight=self.inflight, placed=self.placed):
            return False
        reason = (
            "本轮已有同向腿，不再下第二张"
            if (symbol, side) in self.placed
            else f"{symbol} 已有在途未确认的{_side_cn(side)}委托"
        )
        self.vetoes.append(_veto(index, symbol, side, RULE_INFLIGHT_DUP, reason))
        return True

    # ── 主循环 ──────────────────────────────────────────────────────
    def run(self) -> None:
        for index, d in enumerate(self.batch.decisions):
            symbol = _norm(d.code)
            if d.action == HOLD:
                if symbol:
                    self.noops.append(symbol)
            elif d.action == WATCH:
                if symbol:
                    self.watches.append(symbol)
            elif d.action == SELL:
                self.sell(index, d, symbol)
            elif d.action == BUY:
                self.buy(index, d, symbol)

    # ── 卖出 ────────────────────────────────────────────────────────
    def sell(self, index: int, d: Decision, symbol: str) -> None:
        side = SELL
        if not symbol:
            # 没有代码的卖出：连「哪本账里有它」都问不出来。记在 sell_not_held 下，
            # 理由与原值一起留痕（谓词确实是「这条卖出没有对应持仓」）。
            self.vetoes.append(
                _veto(
                    index,
                    "",
                    side,
                    RULE_SELL_NOT_HELD,
                    "决策没有标的代码，无法执行",
                    code=d.code,
                )
            )
            return
        if sell_not_held(symbol, self.holdings):
            self.vetoes.append(
                _veto(
                    index,
                    symbol,
                    side,
                    RULE_SELL_NOT_HELD,
                    f"{symbol} 不在本账持仓内（防跨 agent 卖仓）",
                )
            )
            return
        holding = self.holdings[symbol]
        if self.dup(index, side, symbol):
            return
        avail = _finite(holding.available) or 0.0
        if avail <= 0:
            self.vetoes.append(
                _veto(
                    index,
                    symbol,
                    side,
                    "l1.t1_sellable",
                    "可卖量为 0（T+1 锁定或已被挂单占用）",
                    available=avail,
                )
            )
            return

        pct, frac = d.sell_intent()
        if frac == FRAC_DIRTY:
            # 停手留痕：把「想减 30%」执行成清仓是不可逆的方向放大（见 contract）
            self.vetoes.append(
                _veto(
                    index,
                    symbol,
                    side,
                    "l2.pct_invalid",
                    f"卖出比例无法解析（模型给了 {d.pct.raw!r}）：停手留痕",
                    pct_raw=d.pct.raw,
                )
            )
            return
        if frac == FRAC_ZERO:
            # 明说 0 / 负值 = 明确不动作，与 hold 同类（不是被拦下的单）
            self.noops.append(symbol)
            return
        if frac != FRAC_OK:
            # 缺比例（模型说了卖、只是漏了幅度）→ **按清仓**，与 contract.sell_intent
            # 同口径。这一支与买入侧刻意相反：卖出方向已由模型给出，比例只是幅度；
            # 缺幅度时按 0 股处理就是「模型让卖、系统静默不卖」。
            self.note(f"{symbol} 卖出：未给比例，按清仓（可卖 {avail:g} 股）")
            pct = 1.0

        qty, note = align_sell_quantity(symbol, avail * pct, avail)
        if qty <= 0:
            # ``align_sell_quantity`` 的契约：``can_use > 0`` 时**必返回正数**
            # （碎股豁免 / 不足最小申报量都走全清）。走到这里说明契约破了——
            # 不留 rule id（影子账按 id 分组，一个永不触发的条目会冒充「从未命中的
            # 规则」），只留一条看得见的 note 并且**绝不出零股腿**。
            self.note(
                f"{symbol} 卖出：整手归一后为 0（可卖 {avail:g} 股，比例 {pct:.0%}）"
                f"：跳过。{note or 'lot_rules 契约可能已变'}"
            )
            return

        q = self.quote(symbol)
        if self.sell_limit_down(index, symbol, side, q, qty):
            return
        limit, problem = _limit(side, q.price)
        if problem:
            self.note(f"{symbol} 卖出：{problem}（不带限价）")
        self.sell_legs.append(
            Leg(
                index=index,
                symbol=symbol,
                side=side,
                quantity=qty,
                limit_price=limit,
                reason=d.reason,
                note=note,
            )
        )
        self.placed.add((symbol, side))

    def sell_limit_down(
        self, index: int, symbol: str, side: str, q: Quote, qty: float
    ) -> bool:
        """卖出侧的跌停不接（阈值由调用点注入；缺一不判 + 留痕）。"""
        need = (_finite(q.day_chg_ratio), _finite(q.limit_threshold_ratio))
        if not at_limit_down(*need):
            if None in need or (need[1] is not None and need[1] <= 0):
                self.note(
                    f"{symbol} 卖出：当日涨跌幅/涨跌停阈值不可用（{need[0]!r}/{need[1]!r}）"
                    "：跌停未判"
                )
            return False
        self.vetoes.append(
            _veto(
                index,
                symbol,
                side,
                RULE_SELL_LIMIT_DOWN,
                f"已在跌停（跌幅 {need[0]:.4f} ≤ -{need[1]:.4f}，均为比例）："
                "当日最差价，不接",
                day_chg_ratio=need[0],
                limit_threshold_ratio=need[1],
                quantity=qty,
            )
        )
        return True

    # ── 买入 ────────────────────────────────────────────────────────
    def buy(self, index: int, d: Decision, symbol: str) -> None:
        side = BUY
        if self.gate is None:
            # 调用点自己决定本轮不做买入（用户冻结/时段外）。不记否决：记了就是凭空
            # 给某条规则记一笔并不存在的成本。
            self.noops.append(symbol)
            return
        if self.dup(index, side, symbol):
            return

        held = symbol in self.holdings
        q = self.quote(symbol)
        price = _finite(q.price)
        pct_hint = _finite(d.pct.value) if d.pct.is_given else None
        verdict = check_buy(
            d,
            gate=self.gate,
            held=held,
            new_buys_round=self.new_buys,
            halted=q.halted,
            day_chg_ratio=q.day_chg_ratio,
            limit_threshold_ratio=q.limit_threshold_ratio,
            need_amount=(
                None
                if self.quota is None or pct_hint is None
                else self.quota * pct_hint
            ),
            budget=self.quota,
            price=price,
        )
        if not verdict.allowed:
            self.vetoes.append(
                Veto(
                    index, symbol, side, verdict.rule, verdict.reason, verdict.evidence
                )
            )
            return
        if verdict.note:
            self.note(f"{symbol} 买入：{verdict.note}")
        if not held:
            # 与隔壁同口径：**过闸即计数**，不等到真的下出去（本轮上限是「本轮最多新开
            # 几只」，不是「本轮最多成交几只」）。计数点写在这里，别挪到出腿之后。
            self.new_buys += 1

        if no_quote(price):
            self.vetoes.append(
                _veto(index, symbol, side, RULE_NO_QUOTE, "无有效现价：算不出股数")
            )
            return
        if self.quota is None:
            self.vetoes.append(
                _veto(
                    index, symbol, side, "l1.available_cash", "买入额度未知：不判也不花"
                )
            )
            return
        # 额度 ≤ 0 **不在这里判**：``check_buy`` 收到的 ``budget`` 就是本额度，
        # 「买不起一手」已在上面按 ``l3.below_min_lot`` 拦下（那里能算出差多少钱，
        # 归因更准）。本层再加一条同义否决 = 一条永远不触发的规则 id
        # （影子代价账按 id 分组，永不触发的条目会烂在报告里）。
        raw = self.quota * verdict.pct / price
        qty, note = align_buy_quantity(symbol, raw)
        if qty <= 0:
            self.vetoes.append(
                _veto(
                    index,
                    symbol,
                    side,
                    "l3.below_min_lot",
                    note or f"额度 {self.quota:g} 买不起一手",
                    quota=self.quota,
                    price=price,
                    want=raw,
                )
            )
            return
        limit, problem = _limit(side, price)
        if problem:
            self.note(f"{symbol} 买入：{problem}（不带限价）")
        self.buy_legs.append(
            Leg(
                index=index,
                symbol=symbol,
                side=side,
                quantity=qty,
                limit_price=limit,
                reason=d.reason,
                note=note,
            )
        )
        self.placed.add((symbol, side))


def _side_cn(side: str) -> str:
    return "买入" if side == BUY else "卖出"
