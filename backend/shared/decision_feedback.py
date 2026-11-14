"""Agent 近期战绩块：把最近平仓的胜负/盈亏放回提示词，防止重复低质量交易。

数据只来自本 agent 的分账账本（``qm_agent_ledger_roundtrip``）；**不是**收益承诺，
也不拿来调参——只是让模型看到“我最近这样做，结果是什么”。样本 <3 笔时只报事实，
不写结论，避免用两笔交易就改变风格。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _num(value: Any, digits: int = 2, sign: bool = True) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    if f != f or f in (float("inf"), float("-inf")):
        return "—"
    return f"{f:+.{digits}f}" if sign else f"{f:.{digits}f}"


def build_agent_feedback_block(feedback: Mapping[str, Any] | None) -> str:
    """最近 N 笔平仓 → 一段紧凑反馈；无数据返回空串（不占版面）。"""
    if not feedback:
        return ""
    rows: Sequence[Mapping[str, Any]] = feedback.get("rows") or ()
    n = int(feedback.get("n") or 0)
    if n <= 0 or not rows:
        return (
            "【你的近期战绩】暂无已平仓记录——不为凑交易而下单；有优势信号时按预算执行。"
        )
    wins = sum(1 for r in rows if float(r.get("pnl") or 0) > 0)
    avg_pct = sum(float(r.get("pct") or 0) for r in rows) / n
    avg_days = sum(float(r.get("days") or 0) for r in rows) / n
    lines = [
        f"【你的近期战绩】（最近 {n} 笔平仓，只做事实参考）",
        f"- 胜率 {wins}/{n}（{wins / n:.0%}）｜平均收益 {_num(avg_pct, 2)}%"
        f"｜平均持有 {avg_days:.1f} 天",
        "- 最近："
        + "；".join(
            f"{r.get('code') or '—'} {_num(r.get('pct'), 2)}%"
            f"（{float(r.get('days') or 0):.1f} 天）"
            for r in list(rows)[:5]
        ),
    ]
    if n >= 3:
        if wins / n <= 0.4 and avg_pct < 0:
            lines.append(
                "- 读法：近期连续低胜率/负收益——减少低质量交易，不摊平、不报复性加仓；"
                "只做有板块/量价/L2 第二腿的票。"
            )
        elif wins / n >= 0.6 and avg_pct > 0:
            lines.append(
                "- 读法：近期正期望——保持同一套筛选，不要因为连赢就放大单笔风险。"
            )
        else:
            lines.append(
                "- 读法：胜负接近——重点是控制单笔风险与避免重复追高，不是提高频率。"
            )
    return "\n".join(lines)
