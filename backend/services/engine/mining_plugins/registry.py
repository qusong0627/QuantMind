"""插件注册表：descriptor 注册 + evaluator 注册 + 执行（异常隔离）。

仿 ``rd_agent/market_adapters`` 的「_registry + register_*」先例；gate / pool_scoring
家族 P1 落地时在此扩展。模块级默认实例由 ``mining_plugins/__init__`` 导入各 family
时完成注册；测试可用 ``PluginRegistry()`` 拿干净实例，互不污染。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from .base import (
    EvalContext,
    EvaluatorPlugin,
    GateContext,
    GateDecision,
    GateOutcome,
    GatePlugin,
    MetricDescriptor,
)

logger = logging.getLogger(__name__)


class PluginRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, MetricDescriptor] = {}
        self._evaluators: dict[str, EvaluatorPlugin] = {}
        self._gates: dict[str, GatePlugin] = {}

    def register_descriptor(self, desc: MetricDescriptor) -> MetricDescriptor:
        """登记展示契约；key 撞名直接报错（静默覆盖会让前端显示与数据脱节）。"""
        if desc.key in self._descriptors:
            raise ValueError(f"指标 key 重复注册: {desc.key}")
        self._descriptors[desc.key] = desc
        return desc

    def register_descriptors(
        self, descs: tuple[MetricDescriptor, ...] | list[MetricDescriptor]
    ) -> None:
        for desc in descs:
            self.register_descriptor(desc)

    def register_evaluator(self, plugin: EvaluatorPlugin) -> EvaluatorPlugin:
        if plugin.name in self._evaluators:
            raise ValueError(f"评估器重名注册: {plugin.name}")
        self.register_descriptors(plugin.descriptors)
        self._evaluators[plugin.name] = plugin
        return plugin

    def evaluator_names(self) -> list[str]:
        return list(self._evaluators)

    def list_descriptors(self) -> list[dict]:
        """按注册顺序输出（顺序即前端展示顺序）。"""
        return [d.to_dict() for d in self._descriptors.values()]

    def run_evaluators(
        self, ctx: EvalContext, enabled: set[str] | None = None
    ) -> dict[str, float | None]:
        """执行启用的评估器。

        单个插件异常 → 其全部键落 None + 告警，不拖垮其余插件、不中断回测
        （指标是可加层，回测本身必须完成）。``enabled=None`` 表示全部启用。
        """
        out: dict[str, float | None] = {}
        for name, plugin in self._evaluators.items():
            if enabled is not None and name not in enabled:
                continue
            keys = [d.key for d in plugin.descriptors]
            try:
                result = plugin.evaluate(ctx) or {}
            except Exception as exc:  # noqa: BLE001 — 插件故障降级，不中断回测
                logger.warning("mining evaluator %s 失败: %s", name, exc, exc_info=True)
                result = {}
            for key in keys:
                value = result.get(key)
                try:
                    out[key] = float(value) if value is not None else None
                except (TypeError, ValueError):
                    logger.warning(
                        "mining evaluator %s 产出非法值 %s=%r", name, key, value
                    )
                    out[key] = None
        return out

    # ── gates（物化门禁，P1）──────────────────────────────────────
    def register_gate(self, plugin: GatePlugin) -> GatePlugin:
        if plugin.name in self._gates:
            raise ValueError(f"门禁重名注册: {plugin.name}")
        self._gates[plugin.name] = plugin
        return plugin

    def gate_names(self) -> list[str]:
        return list(self._gates)

    def get_gate(self, name: str) -> GatePlugin | None:
        return self._gates.get(name)

    def list_gate_descriptors(self) -> list[dict]:
        """门禁展示契约（含默认阈值），供前端「门禁状态」面渲染。"""
        return [
            {**p.descriptor.to_dict(), "default_threshold": float(p.default_threshold)}
            for p in self._gates.values()
        ]

    def run_gates(self, ctx: GateContext) -> GateDecision:
        """执行全部启用门禁；``rejected`` 只因 **hard 且 fail** 而真。

        与评估器同哲学：门禁异常 → skipped + 告警，不中断物化流程——hard
        拦的是「指标差」，不是「门禁坏了」。逐门禁的 enabled/mode/threshold
        走 ``config.get_gate_settings``（yaml > 默认，env 全局覆盖）。
        """
        from .config import get_gate_settings

        outcomes: list[GateOutcome] = []
        rejected = False
        for name, plugin in self._gates.items():
            settings = get_gate_settings(name, plugin.descriptor)
            if not settings["enabled"]:
                continue
            mode = settings["mode"]
            threshold = settings["threshold"]
            if threshold is None:
                threshold = float(plugin.default_threshold)
            try:
                outcome = plugin.check(ctx, threshold)
            except Exception as exc:  # noqa: BLE001 — 门禁故障降级，不拦物化
                logger.warning("mining gate %s 失败: %s", name, exc, exc_info=True)
                outcome = GateOutcome(
                    key=name,
                    label=plugin.descriptor.label,
                    status="skipped",
                    message=f"门禁执行异常：{exc}",
                    threshold=threshold,
                )
            outcome = replace(
                outcome,
                key=outcome.key or name,
                label=outcome.label or plugin.descriptor.label,
                mode=mode,
            )
            outcomes.append(outcome)
            if outcome.status == "fail" and mode == "hard":
                rejected = True
        return GateDecision(rejected=rejected, outcomes=tuple(outcomes))


_DEFAULT = PluginRegistry()


def get_registry() -> PluginRegistry:
    return _DEFAULT


def register_descriptor(desc: MetricDescriptor) -> MetricDescriptor:
    return _DEFAULT.register_descriptor(desc)


def register_descriptors(
    descs: tuple[MetricDescriptor, ...] | list[MetricDescriptor],
) -> None:
    _DEFAULT.register_descriptors(descs)


def register_evaluator(plugin: EvaluatorPlugin) -> EvaluatorPlugin:
    return _DEFAULT.register_evaluator(plugin)


def evaluator_names() -> list[str]:
    return _DEFAULT.evaluator_names()


def list_descriptors() -> list[dict]:
    return _DEFAULT.list_descriptors()


def run_evaluators(
    ctx: EvalContext, enabled: set[str] | None = None
) -> dict[str, float | None]:
    return _DEFAULT.run_evaluators(ctx, enabled)


def register_gate(plugin: GatePlugin) -> GatePlugin:
    return _DEFAULT.register_gate(plugin)


def gate_names() -> list[str]:
    return _DEFAULT.gate_names()


def get_gate(name: str) -> GatePlugin | None:
    return _DEFAULT.get_gate(name)


def list_gate_descriptors() -> list[dict]:
    return _DEFAULT.list_gate_descriptors()


def run_gates(ctx: GateContext) -> GateDecision:
    return _DEFAULT.run_gates(ctx)
