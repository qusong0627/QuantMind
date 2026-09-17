"""风控规则注册表（T-RC-01）：规则 = 纯函数 + 参数 + 级别。

规则函数签名 ``fn(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None``：
返回 None=通过；返回 Decision 即记录（REJECT 终止 / WARN 继续 / HALT 触发状态机）。
参数由配置（RC-02 起：PG 版本 + Redis 热副本）注入——**规则本身不读任何配置存储**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable, Mapping

from backend.shared.risk.contracts import Decision, RiskContext

RuleFn = Callable[[RiskContext, Mapping[str, Any]], "Decision | None"]


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    level: str                    # L0..L6
    description: str
    fn: RuleFn
    default_params: Mapping[str, Any] = field(default_factory=dict)
    always_on: bool = False       # 不依赖配置显式列出（如 L0 急停/时段）


_REGISTRY: dict[str, RuleSpec] = {}


def register(spec: RuleSpec) -> RuleSpec:
    if spec.rule_id in _REGISTRY:
        raise ValueError(f"规则重复注册: {spec.rule_id}")
    if spec.level not in ("L0", "L1", "L2", "L3", "L4", "L5", "L6"):
        raise ValueError(f"非法级别 {spec.level}（规则 {spec.rule_id}）")
    _REGISTRY[spec.rule_id] = spec
    return spec


def rule(rule_id: str, level: str, description: str, *, always_on: bool = False, **default_params: Any):
    """装饰器注册（默认参数展开为 default_params）。"""

    def _wrap(fn: RuleFn) -> RuleFn:
        register(
            RuleSpec(
                rule_id=rule_id,
                level=level,
                description=description,
                fn=fn,
                default_params=dict(default_params),
                always_on=always_on,
            )
        )
        return fn

    return _wrap


def get_rule(rule_id: str) -> RuleSpec | None:
    return _REGISTRY.get(rule_id)


def all_rules() -> tuple[RuleSpec, ...]:
    """全部规则，按 (级别, 规则 ID) 稳定排序（判定顺序确定性的一部分）。"""
    return tuple(
        sorted(_REGISTRY.values(), key=lambda s: (s.level, s.rule_id))
    )
