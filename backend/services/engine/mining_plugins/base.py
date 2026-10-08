"""mining_plugins 基础契约：指标描述符 / 评估上下文 / 评估器 Protocol。

描述符是前后端共同的指标词汇表——前端 ``services-v2/metricRegistry.ts`` 的本地
默认表与金样 ``backend/tests/fixtures/miningMetricsGolden.json`` 的 registry 段
同源，改动必须三处同步（金样、本包、前端表），两侧测试都会对金样断言。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import pandas as pd


@dataclass(frozen=True)
class MetricDescriptor:
    """单个指标的展示契约。

    key：metadata_json 内路径（``quality.pfs`` 为嵌套路径）或表字段名；
    group：prediction（预测力）| robustness（稳健性）| trading（交易/成本）
           | return（毛收益）| pool | combo（后两者为池/组合页预留）；
    unit：ratio 原样小数 | pct 百分数 | score 评分 | days 天数 | count 计数；
    better：higher 越大越好 | lower 越小越好 | none 无方向（仅展示）。
    """

    key: str
    label: str
    group: str
    unit: str
    better: str
    precision: int = 4
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_cost_rate() -> float:
    from .config import get_cost_rate

    return get_cost_rate()


@dataclass(frozen=True)
class EvalContext:
    """评估输入：已对齐清洗后的因子/收益长表。

    ``paired`` 列固定为 datetime / symbol / factor / ret，全部有限值
    （清洗由调用方完成——两条回测路径本来就要清一遍，不重复实现）。
    """

    paired: pd.DataFrame
    cost_rate: float = field(default_factory=_default_cost_rate)
    factor_id: str = ""
    market: str = ""
    universe: str = ""


class EvaluatorPlugin(Protocol):
    name: str
    descriptors: tuple[MetricDescriptor, ...]

    def evaluate(self, ctx: EvalContext) -> dict[str, float | None]: ...


# ── 物化门禁（P1）────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateDescriptor:
    """单个门禁的展示与策略契约。

    default_mode：soft 失败只记录（用户裁决的默认）；hard 失败即拒
    （materializer 走 ``rejected_gate``）。既有 |ρ|≥0.9 值级查重**不**在本
    插件集里，它保持原实现原语义（硬拒）。
    """

    key: str
    label: str
    default_mode: str = "soft"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateContext:
    """门禁输入：因子身份 + 扁平化指标 + 池内分位。

    ``metrics`` 的键与回测 metadata 口径一致（pfs / rre / ic / icir /
    ann_turnover / ann_return_net / ...），由 materializer 装配。值缺失一律
    None——门禁对 None 判 skipped，**绝不按 0 判**。
    """

    factor_id: str = ""
    market: str = ""
    universe: str = ""
    metrics: Mapping[str, float | None] = field(default_factory=dict)
    pool_ic_pct: float | None = None  # IC 在池内的分位 ∈ [0,1]（None=池太小/无池）


@dataclass(frozen=True)
class GateOutcome:
    """单门禁判定出口（runner 填充/覆盖 mode 与 label 后落 manifest 与 metadata）。"""

    key: str
    label: str = ""
    mode: str = "soft"
    status: str = "pass"  # pass | fail | skipped
    message: str = ""
    observed: float | None = None
    threshold: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateDecision:
    """整组判定：``rejected`` 只因 hard 门禁失败而真。"""

    rejected: bool
    outcomes: tuple[GateOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rejected": self.rejected,
            "gates": [o.to_dict() for o in self.outcomes],
        }


class GatePlugin(Protocol):
    name: str
    descriptor: GateDescriptor
    default_threshold: float

    def check(self, ctx: GateContext, threshold: float) -> GateOutcome: ...
