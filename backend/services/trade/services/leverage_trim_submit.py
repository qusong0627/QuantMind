"""减仓执行器的**出口**（P2.6）：一条腿 → 一张真委托，外加「喊人只说一次」的告警闸。

从 ``leverage_trim_io`` 拆出来的理由与四层拆分同源：那一层原先同时装着
「外面长什么样」（账户/行情怎么读、键位长什么样）与「单子怎么发出去」，
两件事的读者不同——读侧出问题要看数据源，发侧出问题要看委托与拒因。
拆开后单向依赖是 ``cycle → submit → io → core``（本模块只多认一个 ``TrimDeps``）。

三条纪律（都是真钱侧的判据，改动前先读）：

* **提交咽喉上的 ``dry_run`` 闸**：演练不提交这件事由 ``submit_leg`` 自己保证，
  不是「调用方记得换个 dispatch」的约定（``decision_round`` 那份教训）。
* **当日幂等号用过的就不复用**：代次 = 已提交 + 已作废 + 1（见 ``_bump_burned``）。
* **告警只在送达后记键**：``_alert_once`` 的键是稳定成因码，且失败不记账——
  一次通知抖动不该把「该减仓却减不动」唯一的主动信号整日吞掉。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.services.trade.services.leverage_trim_core import trim_client_order_id
from backend.services.trade.services.leverage_trim_io import REMARK_PREFIX, TrimDeps

logger = logging.getLogger(__name__)

#: 派发层返回值的两种「已受理」形态（``status="success"`` 才算提交成功）。
_EXEC_DUPLICATE = "duplicate_skipped"
#: 演练腿的 ``execution`` 标记：腿走完了报价与幂等号，但**没有**委托出去。
_EXEC_DRY_RUN = "dry_run"


# ── 腿 → 委托 ────────────────────────────────────────────────────────
@dataclass
class LegOutcome:
    symbol: str
    quantity: float
    price: float
    ok: bool
    execution: str = ""
    order_id: str = ""
    note: str = ""
    #: 报单类型（``LIMIT``/``MARKET``）。市价腿的 ``price`` **合法地是 0**
    #: （``resolve_protect_price("market")`` 即 ``("MARKET", 0.0, …)``），故「有没有定价」
    #: 不能只看 ``price > 0``——那会把市价腿在演练回报里数成「未定价」（评审 M6）。
    order_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "price": self.price,
            "ok": self.ok,
            "execution": self.execution,
            "order_id": self.order_id,
            "note": self.note,
            "order_type": self.order_type,
        }


def is_priced(outcome: LegOutcome) -> bool:
    """这一腿有没有拿到可用报价（市价腿按类型算，不看 0 价，见 ``order_type`` 注释）。"""
    return float(outcome.price or 0) > 0 or outcome.order_type.upper() == "MARKET"


# ── 当日计数（内存态；落盘由 cycle 统一做，见 ``io.save_state``）──────
def _attempts(state: Mapping[str, Any], symbol: str) -> int:
    return int((state.get("attempts") or {}).get(symbol) or 0)


def _bump_attempt(state: dict[str, Any], symbol: str) -> int:
    attempts = dict(state.get("attempts") or {})
    attempts[symbol] = int(attempts.get(symbol) or 0) + 1
    state["attempts"] = attempts
    return attempts[symbol]


def _submitted_count(state: Mapping[str, Any], symbol: str) -> int:
    return int((state.get("submitted") or {}).get(symbol) or 0)


def _bump_submitted(state: dict[str, Any], symbol: str) -> int:
    submitted = dict(state.get("submitted") or {})
    submitted[symbol] = int(submitted.get(symbol) or 0) + 1
    state["submitted"] = submitted
    return submitted[symbol]


def _burned_count(state: Mapping[str, Any], symbol: str) -> int:
    return int((state.get("burned") or {}).get(symbol) or 0)


def _bump_burned(state: dict[str, Any], symbol: str) -> int:
    """作废一个当日幂等号：该号对应的委托行**已落库且不在途**（被拒 / 幂等命中）。

    代次必须把**用掉的号**都算上，而不是只算成功的笔数（评审 HIGH-1）：委托行在
    风控/整手预检**之前**就已落库（``create_order`` 先 commit，预检后才转 REJECTED），
    被拒的那一轮若不算号，下一轮会算出同一个代次 → 派发层先查后插按 cid 命中已有的
    被拒行（**不看状态**）返回「幂等命中」，被本模块当作 ok=True 的第三种结局——
    于是尝试计数永不增长、防废单的「停手等人工」永不触发、账户一整天钉在超限上。
    作废不影响防重：真在途的那张单会被下一轮 ``read_inflight`` 认出来并跳过该腿。
    """
    burned = dict(state.get("burned") or {})
    burned[symbol] = int(burned.get(symbol) or 0) + 1
    state["burned"] = burned
    return burned[symbol]


async def submit_leg(
    deps: TrimDeps, plan_leg: Any, *, state: dict[str, Any], day: str, mode: str
) -> LegOutcome:
    """提交一条减仓腿：报价 → 幂等号 → 派发 → 结果判读。

    代次 = 当日该标的**已用掉的号** + 1：确认提交的（``submitted``）+ 已作废的
    （``burned``：委托行落库后被拒 / 幂等命中）。不变量是「**用过的号不复用**」——
    只看成功笔数会让被拒的号在下一轮被重用，撞上派发层不看状态的先查后插（HIGH-1）。
    """
    symbol = plan_leg.symbol
    generation = _submitted_count(state, symbol) + _burned_count(state, symbol) + 1
    try:
        detail = await deps.client.get_instrument_detail(symbol)
    except Exception as exc:  # noqa: BLE001 拿不到保护位 → 不报价（fail-closed）
        return LegOutcome(
            symbol=symbol,
            quantity=plan_leg.quantity,
            price=0.0,
            ok=False,
            note=f"合约详情读取失败（无跌停价保护位）：{type(exc).__name__}: {exc}",
        )
    from backend.services.live_trading.services.sltp_executor import (
        resolve_protect_price,
    )

    order_type, price, price_note = resolve_protect_price(
        mode,
        detail if isinstance(detail, Mapping) else {},
        float(plan_leg.price),
        symbol,
    )
    if price is None or not order_type:
        return LegOutcome(
            symbol=symbol,
            quantity=plan_leg.quantity,
            price=0.0,
            ok=False,
            note=f"保护价不可得：{price_note}",
        )

    cid = trim_client_order_id(symbol, day, generation)
    order_data = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": float(plan_leg.quantity),
        "price": float(price),
        "order_type": str(order_type),
        "trading_mode": "REAL",
        "portfolio_id": 0,
        "strategy_id": None,
        "client_order_id": cid,
        "remarks": f"{REMARK_PREFIX}减仓执行器 {price_note}",
        # P1.6 TCA 基准价 = **计划时看到的价**（`plan_leg.price`，也是喂给保护价函数的
        # 那个现价）。上面落库的 `price` 是派生出来的保护价，两者量的是不同的事。
        "ref_price": float(plan_leg.price),
        # 整仓卖出的腿带上断言：派发层的整手预检读的是**当日快照**的可用量，而本执行器
        # 读的是柜台**实时**持仓 —— 当天已有成交时快照更大，一笔合法的碎股全清会被判
        # ``lot_blocked``（委托行已落库 ⇒ 每轮重试都被拒、该清的仓清不掉）。判据在
        # ``lot_rules.is_full_position_sell``，与 push_plan 的手填卖单、止损执行器同源。
        "full_position_sell": bool(plan_leg.full_exit),
    }
    if deps.dry_run:
        # **提交咽喉上的闸**：``dry_run`` 是 deps 的属性，不是「调用方记得换个 dispatch」
        # 的约定。这一条曾经只是 CLI 的约定（换 ``_refuse_dispatch`` 替身），而替身抛的
        # 异常会被下面的 ``except`` 吞成「派发异常」腿——于是任何**直接**置 dry_run=True
        # 的调用方（暂停态只报不卖）会真的把单打到柜台上，演练本身还「看起来没提交」。
        # 演练的腿按「成功」回报：它该做的都做了，失败的只有「没发出去」这件事本身。
        return LegOutcome(
            symbol=symbol,
            quantity=plan_leg.quantity,
            price=float(price),
            ok=True,
            execution=_EXEC_DRY_RUN,
            order_type=str(order_type),
            note=f"{price_note}；演练未提交（cid={cid}）",
        )
    try:
        resp = await deps.dispatch(order_data)
    except Exception as exc:  # noqa: BLE001
        return LegOutcome(
            symbol=symbol,
            quantity=plan_leg.quantity,
            price=float(price),
            ok=False,
            note=f"派发异常：{type(exc).__name__}: {exc}",
        )
    resp = resp if isinstance(resp, Mapping) else {}
    status = str(resp.get("status") or "")
    execution = str(resp.get("execution") or "")
    order_id = str(resp.get("order_id") or "")
    if status == "success":
        if execution == _EXEC_DUPLICATE:
            # 撞上本日已用过的幂等号：既有委托**可能是**在途（崩溃重试的原始场景），
            # 也**可能是**终态（此前那一轮被拒，行仍在库里）。两种都不重复下单；
            # 但号必须就此作废、下一轮换号，否则终态那种会永远撞回同一行（HIGH-1）。
            _bump_burned(state, symbol)
            return LegOutcome(
                symbol=symbol,
                quantity=plan_leg.quantity,
                price=float(price),
                ok=True,
                execution=execution,
                order_id=order_id,
                order_type=str(order_type),
                note=f"幂等命中（{cid} 当日已用过：委托在途或已被拒，本轮不重复下单，下一轮换号）",
            )
        _bump_submitted(state, symbol)
        return LegOutcome(
            symbol=symbol,
            quantity=plan_leg.quantity,
            price=float(price),
            ok=True,
            execution=execution,
            order_id=order_id,
            order_type=str(order_type),
            note=f"{price_note}；cid={cid}",
        )
    violations = resp.get("violations") or []
    detail_text = "；".join(
        str(v.get("message") if isinstance(v, Mapping) else v) for v in violations
    )
    if not detail_text:
        # 拒单（引擎被券商拒）没有 violations，拒因在信封顶层 ``message``
        # （派发层从引擎返回值的 ``result.message`` 抬上来的）。不取它，note 就只剩
        # 「提交失败 status=failed execution=direct」——为什么被拒全靠猜。
        detail_text = str(resp.get("message") or "")
    if order_id:
        # 委托行已落库（有 order_id）= 这个号当日**用掉了**：预检/风控在 ``create_order``
        # 之后才拒，行以 REJECTED 留在库里。不作废的话下一轮会算出同一代次、撞回这行，
        # 被派发层当成「幂等命中」的 ok=True（HIGH-1 的死结）。
        _bump_burned(state, symbol)
    return LegOutcome(
        symbol=symbol,
        quantity=plan_leg.quantity,
        price=float(price),
        ok=False,
        execution=execution,
        order_id=order_id,
        order_type=str(order_type),
        note=f"提交失败 status={status} execution={execution} {detail_text}".strip(),
    )


async def _alert_once(
    deps: TrimDeps, state: dict[str, Any], key: str, title: str, content: str
) -> None:
    """同一成因**送达过**一次即不再喊（60s 一拍的任务不许把同一条消息刷成噪声）。

    去重键**只在送达之后才记**：``publish_notification`` 这类实现失败时**返回 False
    而不抛**（``notification_publisher`` 全部失败分支都只 warning + ``return False``），
    先记键会让一次通知服务抖动把这条风险告警**整日吞掉**——而它正是「该减仓却减不动」
    唯一的主动信号。送达失败时保持未记，下一拍（≤60s）自然重试；永久性故障表现为
    每拍一条 ERROR，那正是运维该看到的（面板 ``/risk/trim`` 侧的状态键不受影响，
    它每轮都照写）。
    """
    if deps.dry_run:
        return  # 演练不喊人：dry-run 的「失败」是替身拒发，不是真事故
    alerted = set(state.get("alerted") or ())
    if key in alerted:
        return
    delivered = False
    try:
        delivered = bool(
            await deps.notify(
                deps.user_id, title, content, "error", tenant_id=deps.tenant_id
            )
        )
    except Exception as exc:  # noqa: BLE001 告警失败不影响已做的动作
        logger.error("[LeverageTrim] 告警发送异常（%s）: %s", key, exc)
    if not delivered:
        logger.error("[LeverageTrim] 告警未送达（%s），下一拍重试: %s", key, title)
        return
    alerted.add(key)
    state["alerted"] = sorted(alerted)
