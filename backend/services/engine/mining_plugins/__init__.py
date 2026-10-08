"""因子挖掘插件包（机构级评估体系基座）。

家族：
- evaluators：单因子回测评估器（RRE／换手扣成本），回测完成时由
  ``routers/alpha_agent.py`` 两条回测路径统一调用 ``evaluate_paired()``；
- gates：物化门禁（软告警默认），``rd_mined_materialize`` 经 ``run_gates()``
  调用，结果落 manifest 与 factor metadata；
- pool_scoring：池检索打分纯函数（``pool_service`` 组装数据后调用）。

注册表仿 ``rd_agent/market_adapters`` 的「_registry + register_*」先例；
新增指标 = 新文件 + 注册调用，不回改调用方。
"""

from __future__ import annotations

import pandas as pd

from . import config as _config
from . import evaluators as _evaluators  # noqa: F401 — 导入触发内置评估器注册
from . import gates as _gates  # noqa: F401 — 导入触发内置门禁注册
from . import registry as _registry
from .base import (
    EvalContext,
    EvaluatorPlugin,
    GateContext,
    GateDecision,
    GateOutcome,
    GatePlugin,
    MetricDescriptor,
)

__all__ = [
    "EvalContext",
    "EvaluatorPlugin",
    "GateContext",
    "GateDecision",
    "GateOutcome",
    "GatePlugin",
    "MetricDescriptor",
    "evaluate_paired",
    "get_evaluator_names",
    "get_gate_names",
    "list_descriptors",
    "list_gate_descriptors",
    "run_gates",
]


def evaluate_paired(
    paired: pd.DataFrame,
    *,
    market: str = "",
    universe: str = "",
    factor_id: str = "",
    cost_rate: float | None = None,
    enabled: set[str] | None = None,
) -> dict[str, float | None]:
    """门面：构造 ctx → 按配置启用的评估器链执行 → {metric_key: value|None}。

    两条回测路径（qlib / h5 子进程）共用此入口。``cost_rate=None`` 取
    ``config.get_cost_rate()``（env > yaml > COST_RATE 三级）；``enabled=None``
    取配置启用的内置评估器全集。单插件故障降级为 None，不抛异常。
    """
    if cost_rate is None:
        cost_rate = _config.get_cost_rate()
    if enabled is None:
        enabled = _config.get_enabled_names(_registry.evaluator_names())
    ctx = EvalContext(
        paired=paired,
        cost_rate=cost_rate,
        factor_id=factor_id,
        market=market,
        universe=universe,
    )
    return _registry.run_evaluators(ctx, enabled=set(enabled))


def list_descriptors() -> list[dict]:
    """全部指标描述符（含 descriptor-only 的既有指标），注册顺序。"""
    return _registry.list_descriptors()


def get_evaluator_names() -> list[str]:
    return _registry.evaluator_names()


def run_gates(ctx: GateContext) -> GateDecision:
    """门面：物化门禁判定（软告警默认，hard 失败才 ``rejected``）。"""
    return _registry.run_gates(ctx)


def get_gate_names() -> list[str]:
    return _registry.gate_names()


def list_gate_descriptors() -> list[dict]:
    """门禁展示契约（含默认阈值），前端「门禁状态」面用。"""
    return _registry.list_gate_descriptors()
