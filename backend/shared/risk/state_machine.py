"""风控状态机（T-RC-01）：NORMAL → CAUTION → RESTRICT → HALT（纯函数）。

迁移语义（`docs/风险控制体系_设计方案.md` §四）：
- **升级即时**：信号（halt/restrict/caution）取最严重者直达目标态（滞环与确认在下游驱动）；
- **降级需人工确认且信号已解除**：每次只降一级（`manual_confirm=True`），HALT 恢复另需双人复核
  （复核流程在 RC-05，本层只暴露 `manual_confirm` 语义）；
- 自动动作：仓位系数（0.95/0.6/0.4/0）与"只卖不买"（RESTRICT/HALT 禁买）。

状态持久化（Redis 热态 + PG 迁移历史）与动作执行（全撤等）为 RC-02/03 接线层职责；
本模块保持纯函数、可回放。
"""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

STATES = ("NORMAL", "CAUTION", "RESTRICT", "HALT")
_SEVERITY = {s: i for i, s in enumerate(STATES)}

DEFAULT_POSITION_CAP = {"NORMAL": 0.95, "CAUTION": 0.60, "RESTRICT": 0.40, "HALT": 0.0}


def infer_target(*, halt: bool = False, restrict: bool = False, caution: bool = False) -> str:
    """信号 → 目标状态（取最严重者）。"""
    if halt:
        return "HALT"
    if restrict:
        return "RESTRICT"
    if caution:
        return "CAUTION"
    return "NORMAL"


def next_state(
    current: str,
    *,
    halt: bool = False,
    restrict: bool = False,
    caution: bool = False,
    manual_confirm: bool = False,
) -> tuple[str, str]:
    """状态迁移（唯一实现）。返回 (新状态, 原因)。"""
    cur = current if current in _SEVERITY else "NORMAL"
    target = infer_target(halt=halt, restrict=restrict, caution=caution)
    if _SEVERITY[target] > _SEVERITY[cur]:
        return target, f"信号升级（{cur}→{target}）"
    if _SEVERITY[target] < _SEVERITY[cur]:
        if manual_confirm:
            nxt = STATES[_SEVERITY[cur] - 1]
            return nxt, f"人工确认降级（{cur}→{nxt}）"
        return cur, "条件解除但需人工确认方可降级"
    return cur, "维持"


def allows_buy(state: str) -> bool:
    """RESTRICT（只卖不买）与 HALT（全停）禁买。"""
    return state not in ("RESTRICT", "HALT")


def position_cap_pct(state: str, params: Mapping[str, Any] | None = None) -> float:
    """总仓位上限系数（可配置覆盖）。"""
    table = dict(DEFAULT_POSITION_CAP)
    if params:
        for k, v in params.items():
            if k in table:
                try:
                    table[k] = float(v)
                except (TypeError, ValueError):
                    continue
    return float(table.get(state, table["NORMAL"]))
