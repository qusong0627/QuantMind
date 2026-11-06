"""统计派 · IC 加权合成因子（移植 Tier 3 / T-S3-04）——构造与表达式。

**口径（先说死）**：``composite = Σ ŵ_i · z_i``，``ŵ_i = RankIC_i(h*) / Σ|RankIC_j(h*)|``
（符号保留，Σ|ŵ| = 1），``z_i`` = 分量表达式的**逐 symbol 滚动 z 分数**
（默认窗口 60，min_periods=1 的 Mean/Std，与 qlib 算子语义一致）。

为什么是滚动 z 而不是截面 z：qlib 0.9.7 **没有截面算子**（``Rank`` 是滚动百分位，
``ZScore`` 不存在——容器内实测）。合成因子的 ``factor_code`` 必须以**单行 qlib
表达式**落库（回测/物化链路消费它），所以量纲统一只能用双侧同式的滚动 z。
代价：合成因子**不做截面中性化**（中性化是离线回归，表达式复现不了）——
这份代价写进落库 metadata，不藏。

**权重定点纪律**：权重归一后取 6 位小数，**表达式与 pandas 用同一份舍入值**
（一侧全精度一侧截断 = 等价性对拍永远修不好的假绿）。舍入到 0 的分量整条剔除
（表达式里它不复存在，pandas 侧也不许留）——两轮仍剔不干净（IC 量级跨 6 个
数量级）就返回 None：宁可不合成，不发一个「表达式里少了一项」的假货。

**min_history** = 全体分量 min_history 的最大值 + (z 窗 − 1)：z 窗满员的第一个
下标。引擎取数暖机按 ``MAX_MIN_HISTORY + z_window`` 外扩，保证挖掘窗第一天起
z 窗就是满的（对照候选：它们的 min_history 直接 ≤ 暖机，从第一天就满）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import candidates as _candidates

#: 合成因子的候选 key（落库身份用；不注册进候选目录——它不是被挖出来的）
COMPOSITE_KEY = "ic_weighted_composite"
COMPOSITE_NAME = "IC 加权合成因子"
COMPOSITE_FAMILY = "composite"
#: 滚动 z 默认窗口（60 ≈ 一个季度的交易日）
DEFAULT_Z_WINDOW = 60
#: 权重定点小数位（表达式文本与 pandas 数值同一份）
WEIGHT_DECIMALS = 6
#: 舍入后 |w| 小于此值的分量视为 0（剔除，不参与表达式与计算）
MIN_ABS_WEIGHT = 0.5 * 10**-WEIGHT_DECIMALS


@dataclass(frozen=True)
class CompositePlan:
    """一份合成方案（纯数据 + 可复算的 pandas 闭包）。"""

    components: tuple[_candidates.Candidate, ...]
    weights: tuple[float, ...]  # 定点后权重（与 expression 同值）
    rank_ics: tuple[float, ...]  # 分量在目标周期 h* 上的 RankIC（权重来源）
    horizon: int
    z_window: int
    expression: str  # 单行 qlib 表达式（factor_code）
    min_history: int

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.components)

    def candidate(self) -> _candidates.Candidate:
        """合成因子 → :class:`candidates.Candidate`（compute 与表达式同式）。"""
        plan = self

        def _compute(frame: pd.DataFrame) -> pd.Series:
            total: pd.Series | None = None
            for component, weight in zip(plan.components, plan.weights, strict=True):
                values = component.compute(frame).astype(float)
                mean = values.rolling(plan.z_window, min_periods=1).mean()
                std = values.rolling(plan.z_window, min_periods=1).std()
                term = weight * (values - mean) / std
                total = term if total is None else total + term
            assert total is not None  # 构造时已保证 ≥ 2 个分量
            return total.rename(COMPOSITE_KEY)

        return _candidates.Candidate(
            key=COMPOSITE_KEY,
            name=COMPOSITE_NAME,
            family=COMPOSITE_FAMILY,
            formulation=(
                f"Σ w_i·zscore(f_i,{plan.z_window})，w = RankIC({plan.horizon}) 归一"
            ),
            source="Tier 3：入选因子按目标周期 RankIC 加权合成",
            expression=plan.expression,
            min_history=plan.min_history,
            compute=_compute,
        )


def _normalized_weights(rank_ics: Sequence[float]) -> tuple[float, ...] | None:
    """|RankIC| → Σ|w| = 1 的 6 位定点权重（被舍入到 0 的分量返回 0.0）。

    先归一再舍入、把舍到 0 的分量整条置 0、重归一——最多三轮取定点
    （正常数据一轮收敛）。三轮不等说明 IC 量级病态，返回 None。
    """
    weights = [float(v) for v in rank_ics]
    for _ in range(3):
        total = sum(abs(w) for w in weights)
        if not np.isfinite(total) or total <= 0:
            return None
        updated = [
            0.0
            if abs(w / total) < MIN_ABS_WEIGHT
            else round(w / total, WEIGHT_DECIMALS)
            for w in weights
        ]
        if updated == weights:
            return tuple(updated)
        weights = updated
    return None


def _z_term(expression: str, weight: float, z_window: int) -> str:
    """单个分量项：``±w*(expr-Mean(expr,W))/Std(expr,W)``（无空格，qlib 白名单安全）。"""
    body = f"({expression}-Mean({expression},{z_window}))/Std({expression},{z_window})"
    return f"{weight:+.{WEIGHT_DECIMALS}f}*{body}"


def build_composite_plan(
    components: Sequence[tuple[_candidates.Candidate, float]],
    *,
    horizon: int,
    z_window: int = DEFAULT_Z_WINDOW,
) -> CompositePlan | None:
    """(分量候选, 目标周期 RankIC) 序列 → 合成方案；不足/退化 → ``None``。

    h* 的选择（哪个周期当加权口径）由引擎负责——那是跨候选的决策；本函数
    只处理「给定的这些分量与权重能不能合成」。
    """
    if z_window < 2:
        raise ValueError(f"z_window 至少为 2（收到 {z_window!r}）")
    usable = [(c, float(ic)) for c, ic in components if np.isfinite(ic) and ic != 0]
    if len(usable) < 2:
        return None
    weights = _normalized_weights([ic for _, ic in usable])
    if weights is None:
        return None
    kept = [
        (candidate, weight, ic)
        for (candidate, ic), weight in zip(usable, weights, strict=True)
        if weight != 0.0
    ]
    if len(kept) < 2:
        return None
    expression = "".join(
        _z_term(candidate.expression, weight, z_window)
        for candidate, weight, _ic in kept
    )
    if expression.startswith("+"):
        expression = expression[1:]
    return CompositePlan(
        components=tuple(candidate for candidate, _, _ in kept),
        weights=tuple(weight for _, weight, _ in kept),
        rank_ics=tuple(ic for _, _, ic in kept),
        horizon=int(horizon),
        z_window=int(z_window),
        expression=expression,
        min_history=max(c.min_history for c, _, _ in kept) + (z_window - 1),
    )
