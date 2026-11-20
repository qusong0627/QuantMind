"""决策轮结果装配（从 ``decision_round.py`` 拆出，2026-11-20）。

两件事：把执行段/守护段的结果折成 ``RoundResult``，以及轮终 note 的判据。
拆出原因：``decision_round.py`` 顶到 800 行上限（用户规则 common/coding-style.md）。
"""

from __future__ import annotations

from typing import Any

from backend.services.trade.services.decision_round_core import (
    STATUS_ABORTED,
    STATUS_OK,
    RoundResult,
    RoundSlot,
)
from backend.services.trade.services.decision_round_notes import round_highlights


def aborted_round_result(
    *,
    day,
    slot: RoundSlot,
    round_id: str,
    agent: str,
    mode: str,
    decisions: list[Any] | tuple[Any, ...],
    aborted: str,
    outcomes_gap: str | None,
    summary: dict[str, Any],
    outcome: Any,
    watch_result: Any,
    audit_rows: int,
) -> RoundResult:
    """执行段 aborted 且零提交：状态记 aborted（不置 done 键），守护表整组保留。"""
    return RoundResult(
        status=STATUS_ABORTED,
        day=day,
        slot=slot,
        round_id=round_id,
        note=f"{aborted}（守护规则表本轮整组保留）",
        errors=(aborted, outcomes_gap) if outcomes_gap else (aborted,),
        agent=agent,
        mode=mode,
        decisions=len(decisions),
        legs=int(summary.get("legs", 0) or 0),
        audit_rows=audit_rows,
        watch_armed=len(getattr(watch_result, "armed", ()) or ()),
        # 中止轮的要点行同样要给（腿都标「未发出」）：告警旁一句「本来要做什么」，
        # 值班不必翻审计表就知道这轮缺的是什么。
        meta={"highlights": round_highlights(decisions, outcome)},
    )


def ok_round_result(
    *,
    day,
    slot: RoundSlot,
    round_id: str,
    agent: str,
    mode: str,
    decisions: list[Any] | tuple[Any, ...],
    submitted: int,
    summary: dict[str, Any],
    outcome: Any,
    watch_result: Any,
    audit_rows: int,
    outcomes_gap: str | None,
    aborted: str | None,
    pool_rows: list[Any],
    pool_file: str,
    kept_rows: list[Any],
    dropped_rows: list[Any],
    direction: Any,
    direction_text: str,
    in_session: bool,
    quota_total: int,
    quota_used: int,
    ledger: Any,
    virtual_cash: float,
) -> RoundResult:
    """正常轮：轮终 note（空池/非时段/零腿/提交完成）+ 告警判据 meta。"""
    bearish_clear = len(pool_rows) == 0 and "看空" in direction_text
    if aborted:
        note = aborted
    elif bearish_clear:
        note = f"空池（看空清空）：方向「{direction_text}」→ 本轮不建仓"
    elif not in_session:
        note = "非交易时段：未提交腿"
    elif submitted > 0:
        note = "提交完成"
    else:
        note = "提交完成（0 腿）"
    return RoundResult(
        status=STATUS_OK,
        day=day,
        slot=slot,
        round_id=round_id,
        note=note,
        errors=(outcomes_gap,) if outcomes_gap else (),
        agent=agent,
        mode=mode,
        decisions=len(decisions),
        legs=int(summary.get("legs", 0) or 0),
        submitted=submitted,
        failed=int(summary.get("failed", 0) or 0),
        watch_armed=len(getattr(watch_result, "armed", ()) or ()),
        audit_rows=audit_rows,
        meta={
            "vetoes": int(summary.get("vetoes", 0) or 0),
            "noops": int(summary.get("noops", 0) or 0),
            "watches": int(summary.get("watches", 0) or 0),
            "duplicates": int(summary.get("duplicates", 0) or 0),
            # 本轮要点（▸ 段的数据源）：摘要在计数之外要说人话——方向/标的/股数/
            # 理由（2026-10-15 用户点名「通知了、不给建议吗」）。提炼纪律见
            # ``decision_round_notes.round_highlights``；空列表=没可说的事，不是取不到。
            "highlights": round_highlights(decisions, outcome),
            # 执行段自己的注记（如「虚拟现金未建模，未判」）——是说明不是错误，
            # 与 ``errors`` 分开：把 note 混进 errors 会让状态键红得没有意义。
            "plan_notes": list(summary.get("notes") or [])[:5],
            "pool": {
                "file": pool_file,
                # 原始行数（供给面）。**必须显式带出来**：告警侧靠「键在不在」区分
                # 「真的是 0 行」与「取不到」（同 ``exec_summary`` 纪律）。
                "rows": len(pool_rows),
                "shown": len(kept_rows),
                "dropped": len(dropped_rows),
                # 大盘方向文本（看空清空判据；"—"=缺失，绝不编一个方向出来）。
                "direction": direction_text,
                "total_score": getattr(direction, "total_score", None),
            },
            # 告警判据：非交易时段的零腿是**设计内**（盘前规划轮不发腿）。
            "in_session": bool(in_session),
            # 对外状态面（前端/告警）也只暴露授予额度口径：真实账户三数只留在
            # 审计行 ``context_meta.account`` 里，供运维复盘，不进任何模型可见面。
            "quota": {
                "total": quota_total,
                "used": quota_used,
                "remaining": ledger.quota_remaining,
                "cash": virtual_cash,
            },
        },
    )
