"""候选信号一键推送的**纯逻辑**（不碰 DB / Redis / OrderRouter）。

推送这条链上「算错了也不报错」的地方集中在这里，所以单独成模块逐条单测：
默认股数怎么算、整手怎么归、风控裁定怎么读、镜像回执怎么翻成「成功/排队/未执行」。

四条纪律（每条对应一个真实的错法）：

1. **默认股数是算出来的，不是猜的**。`可用资金 × position_score ÷ 价格` 再整手归一；
   `position_score <= 0` 表示引擎明确「不入场」，此时**必须给 0 股并说清原因**，
   不能退化成「那就买 1 手」—— 那正是把「不建议」执行成了「建议」。
2. **整手归一只发生在自动算量上**。用户手填的数量原样保留、另行报告违规，
   悄悄改掉用户输入的数等同于伪造回执。
3. **风控裁定要分环境闸与标的闸**。`l0.*`（时段/急停/时钟/配置）是全局的，
   它拒单不代表这只票有问题；混在一起讲会让用户在名单里找一只根本不存在的风险股。
4. **镜像回执只有 `ok`/`submitted` 是成功**。`queued` 是「还没发」，`skipped` 是
   「压根没发」，`duplicate` 是「早就发过了」—— 三者与成功在界面上必须可区分，
   否则用户会在「已推送」的错觉里错过一整天的行情。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from backend.services.live_trading.services.lot_rules import (
    BOARD_BJ,
    BOARD_STAR,
    DEFAULT_LOT,
    STAR_MIN_LOT,
    board_display,
    describe_violation,
    resolve_board,
)

# 环境闸（全局，与标的无关）：L0 是时段/急停/时钟/配置四类
ENVIRONMENT_PREFIX = "l0."

# 镜像回执分类：只有前两类算成功
_MIRROR_SUCCESS = frozenset({"ok", "submitted"})
_MIRROR_QUEUED = frozenset({"queued"})
_MIRROR_DUPLICATE = frozenset({"duplicate"})
_MIRROR_SKIPPED = frozenset({"skipped"})
# 其余（failed / error / 未知）一律归失败


@dataclass(frozen=True)
class QuantityPlan:
    """一笔腿的股数裁定。

    ``note`` 是**给人看的说明**（整手对齐/资金来源），``problem`` 是**必须阻断的违规**。
    两者分开是刻意的：整手对齐后照常下单，而违规必须让确认按钮点不动 ——
    合成一个字段的话，「科创板按 1 股递增」和「科创板不足 200 股」会长得一样。
    """

    quantity: float
    source: str  # auto | manual | blocked
    note: str = ""
    problem: str = ""

    @property
    def executable(self) -> bool:
        return self.source != "blocked" and not self.problem and self.quantity > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "source": self.source,
            "note": self.note,
            "problem": self.problem,
            "executable": self.executable,
        }


def classify_decisions(
    decisions: list[dict[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """按规则前缀把决策拆成「环境闸」与「标的/账户闸」两组。

    推送确认面板要分栏呈现：`l0.session` 说「现在不是交易时段」是对全场的结论，
    不该出现在某一只票的风险栏里 —— 那会让用户去查一只票，而问题不在它身上。
    """
    env: list[dict[str, Any]] = []
    subject: list[dict[str, Any]] = []
    for d in decisions or []:
        rid = str((d or {}).get("rule_id") or "")
        (env if rid.startswith(ENVIRONMENT_PREFIX) else subject).append(d)
    return {"environment": env, "subject": subject}


def align_buy_quantity(symbol: str, raw: float) -> tuple[float, str]:
    """买入数量整手归一，返回 ``(数量, 说明)``；买不起 1 手时返回 ``(0, 原因)``。

    与 ``lot_rules.describe_violation`` 的判据保持同源（同一份 board/最小手数常量），
    避免「算出来的数」与「校验说违规」互相打架。
    """
    qty = float(raw or 0)
    if qty <= 0:
        return 0.0, ""
    board = resolve_board(symbol)
    if board == BOARD_STAR:
        aligned = float(math.floor(qty))
        if aligned < STAR_MIN_LOT:
            return 0.0, f"科创板买入最少 {STAR_MIN_LOT} 股（可买 {qty:g} 股不足）"
        note = "" if aligned == qty else f"科创板按 1 股递增（{qty:g}→{aligned:g}）"
        return aligned, note
    if board == BOARD_BJ:
        aligned = float(math.floor(qty))
        if aligned < DEFAULT_LOT:
            return 0.0, f"北交所买入最少 {DEFAULT_LOT} 股（可买 {qty:g} 股不足）"
        note = "" if aligned == qty else f"北交所按 1 股递增（{qty:g}→{aligned:g}）"
        return aligned, note
    aligned = float(int(qty // DEFAULT_LOT) * DEFAULT_LOT)
    if aligned < DEFAULT_LOT:
        return 0.0, (
            f"{board_display(board)}买入最少 {DEFAULT_LOT} 股"
            f"（可买 {qty:g} 股不足 1 手）"
        )
    note = "" if aligned == qty else f"整手对齐（{qty:g}→{aligned:g}）"
    return aligned, note


def plan_quantity(
    *,
    symbol: str,
    side: str,
    price: float | None,
    position_score: float | None,
    available_cash: float | None,
    available_position: float | None = None,
    override: float | None = None,
) -> QuantityPlan:
    """算这一笔的股数。``source ∈ auto|manual|blocked``。

    * **买入 · 自动**：``可用资金 × position_score ÷ 价格`` 后整手归一。
      ``position_score`` 缺失或 ≤0 → 0 股 + ``problem``（引擎说「不入场」，
      不是「那就买 1 手」—— 那等于把「不建议」执行成了「建议」）。
    * **卖出 · 自动**：整仓卖出（数量 = 可用持仓）。
    * **手填**：原样采用，只做违反项体检（``describe_violation``），绝不静默改数 ——
      悄悄改掉用户输入的数等同于伪造回执。
    """
    side_raw = str(side or "").strip().lower()
    px = float(price or 0)

    if override is not None:
        qty = float(override)
        if qty != int(qty):
            return QuantityPlan(
                qty, "manual", problem=f"数量必须为整数股，当前 {qty:g}"
            )
        if qty <= 0:
            return QuantityPlan(qty, "blocked", problem="手填数量必须大于 0")
        return QuantityPlan(
            qty, "manual", problem=describe_violation(symbol, side_raw, qty) or ""
        )

    if side_raw == "sell":
        can = float(available_position or 0)
        if can <= 0:
            return QuantityPlan(
                0.0, "blocked", problem="无可用持仓（T+1 锁定或未持有）"
            )
        return QuantityPlan(can, "auto", note="整仓卖出")

    # 买入 · 自动
    if px <= 0:
        return QuantityPlan(0.0, "blocked", problem="无有效价格，无法计算股数")
    if position_score is None:
        return QuantityPlan(
            0.0, "blocked", problem="该信号日无仓位信号（未推理或缺失基准）"
        )
    score = float(position_score)
    if score <= 0:
        return QuantityPlan(
            0.0, "blocked", problem=f"仓位信号为 {score:g}（引擎不入场）"
        )
    cash = float(available_cash or 0)
    if cash <= 0:
        return QuantityPlan(0.0, "blocked", problem="账户可用资金为 0")
    raw_qty = cash * score / px
    qty, note = align_buy_quantity(symbol, raw_qty)
    if qty <= 0:
        return QuantityPlan(0.0, "blocked", problem=note or "可用资金不足 1 手")
    basis = f"可用资金 {cash:,.0f} × 仓位 {score:.0%} ÷ {px:.2f}"
    return QuantityPlan(qty, "auto", note=f"{note}；{basis}" if note else basis)


def batch_scale(available_cash: float | None, needs: list[float]) -> float:
    """整批资金约束下的**统一缩放系数**（1.0 = 不必缩放）。

    半凯利仓位分是「占**总权益**的比例」，逐笔各按全量可用资金算量会**系统性超配**：
    实测 3 只候选、可用 48.9 万、仓位分 0.849 → 逐笔算出 41.5 万×3 = 124.5 万，
    是可用资金的 2.5 倍。多出来的单子由账户层「可用资金不足」逐笔拒掉，
    表现为「一键推送 3 只，成交 1 只，另两只莫名失败」——量算错了，却报成下单失败。

    所以整批**等比例缩量**到可用资金以内：保持各腿的相对权重（缩量前就是同一套
    仓位分的比例），只改绝对规模。返回的系数由调用方乘到**自动算出的**数量上；
    手填数量一律不乘（纪律 2：绝不悄悄改用户输入的数）。
    """
    total = 0.0
    for x in needs or []:
        try:
            v = float(x or 0.0)
        except (TypeError, ValueError):  # pragma: no cover - 调用方保证数值
            continue
        if v > 0:
            total += v
    cash = float(available_cash or 0.0)
    if total <= 0 or cash <= 0 or total <= cash:
        return 1.0
    return cash / total


def scale_note(
    factor: float, *, before: float | None = None, after: float | None = None
) -> str:
    """缩量说明（挂在腿的 ``note`` 上，用户要能看出数为什么变小了）。

    带上「缩量前→缩量后」的实际股数：只写系数的话，用户拿 note 里的整手对齐数字
    去核对会发现对不上（那句说的是缩量前的量），看起来像系统自己改错了数。
    """
    base = f"按批次资金约束缩量 ×{factor:.4f}"
    if before and after and before != after:
        return f"{base}（{before:g}→{after:g} 股）"
    return base


def apply_batch_scale(
    legs: list[dict[str, Any]], available_cash: float | None, side: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把整批**自动算出的**量等比例缩到可用资金以内，返回 ``(新腿列表, budget)``。

    只动三处，其余原样透传：

    * **手填量不缩**（纪律 2）：用户明确输入的数被悄悄改掉等于伪造回执；
    * **已阻断的腿不缩**：它本来就不花钱，缩它只会让这一行的数量与原因对不上；
    * **note 里的整手对齐要重写**：那句说的是**缩放前**的数，留着用户一核对就以为算错了。

    不缩量时原样返回入参（连复制都省了）；缩量时返回新列表，不就地改调用方的数据。
    """
    if str(side or "").strip().lower() != "buy" or not legs:
        return legs, {"applied": False, "factor": 1.0}

    # 合计需求含手填的腿（它们同样要花钱），但只缩自动算出来的
    needs = [
        float(x.get("amount") or 0)
        for x in legs
        if x.get("executable") and x.get("source") in ("auto", "manual")
    ]
    factor = batch_scale(available_cash, needs)
    planned = round(sum(needs), 2)
    cash = float(available_cash or 0.0)
    budget: dict[str, Any] = {
        "available_cash": round(cash, 2),
        "planned_amount": planned,
        "factor": round(factor, 6),
        "applied": factor < 1.0,
        "note": "",
    }
    if factor >= 1.0:
        return legs, budget

    scaled_legs: list[dict[str, Any]] = []
    for leg in legs:
        if leg.get("source") != "auto" or not leg.get("executable"):
            scaled_legs.append(leg)
            continue
        before = float(leg.get("quantity") or 0)
        scaled, why = align_buy_quantity(str(leg.get("symbol") or ""), before * factor)
        if scaled <= 0:
            scaled_legs.append(
                {
                    **leg,
                    "quantity": 0.0,
                    "source": "blocked",
                    "executable": False,
                    "blocked_by": "quantity",
                    "amount": 0.0,
                    "problem": f"批次资金约束后不足 1 手（{why or '可用资金不足'}）",
                }
            )
            continue
        # 原 note 形如「{整手对齐}；{资金来源}」：只留资金来源（资金来源句不含「；」）
        parts = [p for p in str(leg.get("note") or "").split("；") if p]
        head = parts[-1] if parts else ""
        tail = scale_note(factor, before=before, after=scaled)
        scaled_legs.append(
            {
                **leg,
                "quantity": scaled,
                "amount": round(float(leg.get("price") or 0.0) * scaled, 2),
                "note": f"{head}；{tail}" if head else tail,
            }
        )
    budget["note"] = (
        f"本批自动算量合计 ¥{planned:,.0f} 超出可用资金 ¥{cash:,.0f}，"
        f"已按 ×{factor:.4f} 等比例缩量"
    )
    return scaled_legs, budget


def mirror_outcome_class(status: str | None) -> str:
    """镜像回执 → 粗分类：``success | queued | duplicate | skipped | failed``。

    **未知状态归 failed**（fail-closed）：未来若镜像新增了状态词，宁可显示成失败让
    用户自己看一眼，也不能默认落进「成功」—— 真钱路径上，把没发出去说成发出去了，
    比说成失败要贵得多。
    """
    s = str(status or "").strip().lower()
    if s in _MIRROR_SUCCESS:
        return "success"
    if s in _MIRROR_QUEUED:
        return "queued"
    if s in _MIRROR_DUPLICATE:
        return "duplicate"
    if s in _MIRROR_SKIPPED:
        return "skipped"
    return "failed"


def summarize_legs(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """确认面板底部那行「本次将执行 N 笔（勾除 M）· 预计金额 ¥X」。

    只统计**下单那一刻确实会发出去**的腿：被排除/被风控/数量为 0 的不计入 N，
    但要计入 blocked 单独报出来，否则「选了 10 只只有 3 只有数」看起来像丢了 7 只。
    """
    executable = [x for x in legs if x.get("executable")]
    blocked = [x for x in legs if not x.get("executable")]
    amount = 0.0
    for x in executable:
        amount += float(x.get("amount") or 0)
    return {
        "total": len(legs),
        "executable": len(executable),
        "blocked": len(blocked),
        "est_amount": round(amount, 2),
    }
