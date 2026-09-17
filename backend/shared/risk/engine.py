"""规则引擎核心（T-RC-01）：唯一判定入口 `RiskGateCore.evaluate`（纯函数）。

语义：
- 规则按 (level, rule_id) 稳定序执行；未在 config 显式启用的规则跳过（``always_on`` 除外）；
- 任一 REJECT 或 HALT → ``passed=False``（HALT 另置 ``halt=True`` 供状态机迁移）；
- 任何规则自身异常 → 该规则产出 REJECT（**fail-closed**，绝不静默放行）；
- 裁决携带 ``config_version``（审计：拦截记录可回放复盘）。
"""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

from backend.shared.risk.contracts import (
    ACTION_HALT,
    ACTION_REJECT,
    Decision,
    RiskContext,
    RiskVerdict,
)
from backend.shared.risk.registry import RuleSpec, all_rules
import backend.shared.risk.builtin_rules  # noqa: F401 — 导入即注册内置规则

__all__ = ["RiskGateCore"]


class RiskGateCore:
    """纯函数规则引擎（无 IO；配置注入；可注入规则集便于测试）。"""

    def __init__(self, specs: tuple[RuleSpec, ...] | None = None) -> None:
        self._specs: tuple[RuleSpec, ...] = specs if specs is not None else all_rules()

    @property
    def rules(self) -> tuple[RuleSpec, ...]:
        return self._specs

    def evaluate(
        self,
        ctx: RiskContext,
        config: Mapping[str, Mapping[str, Any] | None] | None = None,
        *,
        version: int = 0,
    ) -> RiskVerdict:
        cfg = config or {}
        decisions: list[Decision] = []
        checked: list[str] = []
        rejected = False
        halted = False
        for spec in self._specs:
            entry = cfg.get(spec.rule_id)
            if entry is None and not spec.always_on:
                continue
            params: dict[str, Any] = dict(spec.default_params)
            if isinstance(entry, Mapping):
                params.update(entry)
            checked.append(spec.rule_id)
            try:
                d = spec.fn(ctx, params)
            except Exception as exc:  # noqa: BLE001 - fail-closed：规则异常=拒
                d = Decision(
                    rule_id=spec.rule_id,
                    level=spec.level,
                    action=ACTION_REJECT,
                    reason=f"规则执行异常（fail-closed）: {type(exc).__name__}",
                    evidence={"error": str(exc)[:200]},
                )
            if d is None:
                continue
            decisions.append(d)
            if d.action == ACTION_REJECT:
                rejected = True
            elif d.action == ACTION_HALT:
                halted = True
        return RiskVerdict(
            passed=not (rejected or halted),
            halt=halted,
            decisions=tuple(decisions),
            config_version=int(version),
            checked_rules=tuple(checked),
        )
