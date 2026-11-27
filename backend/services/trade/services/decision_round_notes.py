"""决策轮**执行段取数**：summary 形状归一 + 摘要 ``▸`` 要点行。**纯函数、零 IO**。

从 ``decision_round`` 拆出（2026-10-15）：该文件顶在 800 行体量预算上，而
「执行段返回值怎么读」与「摘要要说什么」都是**读侧**关注点——编排层只负责调用。
本模块是**叶子**：只 import 标准库，不 import 任何 ``decision_round_*`` 模块，
也不碰 IO（``round → notes`` 单向，与 ``blocks``/``feeds`` 同层纪律）。

两条纪律随迁（原在 ``decision_round`` 里，逐字搬来）：

* :func:`exec_summary`：``summary`` 方法/映射都收——只认方法的话，一个带 dict 的
  实现会让这一轮以 ``legs=0/submitted=0`` 收尾：状态键显示「ok、没动腿」，而真单
  已经出去了。「取不到」与「真的是 0」必须分开。
* :func:`round_highlights`：摘要/告警 ``▸`` 段的数据源（用户 2026-10-15 点名
  「通知了、不给建议吗」）。数据全取本轮真件（决策/腿/回执/否决），取不到的段
  就省——绝不替模型编理由。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)


def exec_summary(outcome: Any) -> tuple[str, dict[str, Any]]:
    """执行段结果 → ``(aborted, summary)``。

    契约是 ``outcome.summary()``（ExecutionOutcome 的方法），但**映射也收**——只认
    方法的话，一个带 ``summary`` dict 的实现会让这一轮以 ``legs=0/submitted=0`` 收尾：
    状态键显示「ok、没动腿」，而真单已经出去了。「取不到」与「真的是 0」在这里必须
    分开，否则状态键会说谎。
    """
    src = getattr(outcome, "summary", None)
    if callable(src):
        summary = dict(src() or {})
    elif isinstance(src, Mapping):
        summary = dict(src)
    else:
        summary = {}
    return str(getattr(outcome, "aborted", "") or ""), summary


# ── 本轮要点（▸ 段）：摘要/告警面的人话行 ───────────────────────────────
#: 条数上限：QQ 是卡片不是报表——20 条腿全量排开会把要人处理的那条挤出视线。
_HIGHLIGHT_LIMIT = 5
#: 单条要点里理由/拦因/失败文案的截断长度（要点是**一句**话）。
_HIGHLIGHT_TEXT_MAX = 36


def round_highlights(decisions: Sequence[Any], outcome: Any) -> list[str]:
    """本轮执行要点的人话行（摘要 ▸ 段的数据源）。**纯函数、绝不抛**。

    每条 = ``方向 标的 股数 @价（一句理由或终态）``：决策给标的/理由，腿给股数与
    限价，回执给终态（提交/失败/幂等重复），否决给拦因；**取不到的段就省**——
    绝不替模型编理由（同 ``exec_summary`` 纪律）。hold 与三无（无腿、无回执、
    无否决）决策不出行：它们没有「本来要做」可言，条数在「决策 N」里。

    「绝不抛」是硬约束：本函数在 OK 轮的 ``try`` 块内被调用，漏出去会把一轮跑好
    的结果改写成 error（补跑槽整轮重来 = 真钱再下一遍）。
    """
    try:
        plan = getattr(outcome, "plan", None)
        legs = tuple(getattr(plan, "legs", ()) or ())
        vetoes = tuple(getattr(plan, "vetoes", ()) or ())
        receipts = tuple(getattr(outcome, "receipts", ()) or ())
        leg_by = {getattr(leg, "index", -1): leg for leg in legs}
        veto_by = {getattr(veto, "index", -1): veto for veto in vetoes}
        rec_by: dict[int, Any] = {}
        for rec in receipts:
            rec_by[getattr(rec, "index", -1)] = rec
        money: list[str] = []
        guards: list[str] = []
        for idx, d in enumerate(decisions or ()):
            action = str(getattr(d, "action", "") or "").lower()
            leg, veto, rec = leg_by.get(idx), veto_by.get(idx), rec_by.get(idx)
            label = _highlight_label(d) or _highlight_text(
                getattr(rec or leg or veto, "symbol", "")
            )
            if not label:
                continue  # 名字与代码都取不到：宁可不出行，不编一个「?」
            if action == "watch":
                stops = [
                    f"{word} {px}"
                    for key, word in (
                        ("stop_loss", "止损"),
                        ("take_profit", "止盈"),
                        ("move_stop", "移动止"),
                    )
                    if (px := _highlight_price(getattr(d, key, None)))
                ]
                guards.append(
                    f"守望 {label}（{'，'.join(stops)}）"
                    if stops
                    else _with_reason(f"守望 {label}", d)
                )
                continue
            if action not in ("buy", "sell"):
                continue
            if leg is None and veto is None and rec is None:
                continue  # 三无（不可执行/noop）：不出行
            word = "买入" if action == "buy" else "卖出"
            src = rec if rec is not None else leg
            qty = _highlight_qty(getattr(src, "quantity", None))
            px = _highlight_price(getattr(src, "limit_price", None))
            sizing = f" {qty}股" if qty else ""
            if px:
                sizing += f" @¥{px}"
            managed = "〔人工托管〕" if str(getattr(leg, "owner", "") or "") else ""
            money.append(
                f"{word} {label}{sizing}{_highlight_extra(rec, veto, leg, d)}{managed}"
            )
        lines = money + guards
        if len(lines) > _HIGHLIGHT_LIMIT:
            rolled = len(lines) - _HIGHLIGHT_LIMIT
            lines = [*lines[:_HIGHLIGHT_LIMIT], f"…另有 {rolled} 条（见审计行）"]
        return lines
    except Exception as exc:  # noqa: BLE001 摘要面不许带走一轮
        logger.warning("[DecisionRound] 要点提炼失败（摘要将不带 ▸ 段）: %s", exc)
        return []


def _highlight_label(d: Any) -> str:
    """``名称(代码)``（缺哪半补哪半；补不齐才退化为单体）。两半都缺返回空串。

    代码统一显示形态（A股前缀式/港股 ``NNNN.HK``/美股 ticker）；只有一半时
    用另一半经 ``stock_display_name`` 补齐（2026-10-16：模型偶尔只给 name 或
    只给 code，旧实现让裸码上卡）。纯函数、绝不抛。
    """
    name = _highlight_text(getattr(d, "name", ""))
    code = _highlight_text(getattr(d, "code", ""))
    if name and code:
        return f"{name}({_display_code(code)})"
    if code:
        try:
            from backend.shared.stock_display_name import display_code, resolve

            shown = display_code(code) or code
            resolved = resolve(code)
            return f"{resolved}({shown})" if resolved else shown
        except Exception:  # noqa: BLE001 - 要点面绝不因名字解析失败
            return code
    return name


def _display_code(code: str) -> str:
    """代码 → 显示形态（``stock_display_name``）；失败回原样，绝不丢身份。"""
    try:
        from backend.shared.stock_display_name import display_code

        return display_code(code) or code
    except Exception:  # noqa: BLE001
        return code


def _highlight_text(value: Any, *, limit: int = _HIGHLIGHT_TEXT_MAX) -> str:
    """折叠空白并截断——要点行是**一行**一句话，换行/超长会把卡片顶爆。"""
    one = " ".join(str(value or "").split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def _highlight_qty(qty: Any) -> str:
    """股数（整手取整）；取不到/非正数返回空串（调用点省这一段，不写 0 股）。"""
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value <= 0:
        return ""
    return f"{value:.0f}" if value.is_integer() else f"{value:g}"


def _highlight_price(px: Any) -> str:
    """限价两位小数；取不到/非正数返回空串（不写 @0.00 这种假价）。"""
    try:
        value = float(px)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value <= 0:
        return ""
    return f"{value:.2f}"


def _with_reason(line: str, d: Any) -> str:
    reason = _highlight_text(getattr(d, "reason", ""))
    return f"{line}（{reason}）" if reason else line


def _highlight_extra(rec: Any, veto: Any, leg: Any, d: Any) -> str:
    """行尾括号段，优先级：回执终态 > 否决拦因 > 「未发出」。

    提交成功且非重复时回落到**决策理由**——那正是用户要的「一句理由」。
    """
    if rec is not None:
        if bool(getattr(rec, "duplicate", False)):
            return "（此前轮已在途，未重复提交）"
        if bool(getattr(rec, "success", False)):
            reason = _highlight_text(getattr(d, "reason", ""))
            return f"（{reason}）" if reason else ""
        msg = _highlight_text(getattr(rec, "message", ""))
        return f"（失败：{msg}）" if msg else "（失败）"
    if veto is not None:
        rule = _highlight_text(getattr(veto, "rule", ""), limit=24)
        why = _highlight_text(getattr(veto, "reason", ""))
        if rule and why:
            return f"（被拦 {rule}：{why}）"
        return (
            f"（被拦 {rule}）" if rule else (f"（被拦：{why}）" if why else "（被拦）")
        )
    if leg is not None:  # 有腿无回执：本轮一张没发（aborted）或提交面没走到
        return "（未发出）"
    return ""
