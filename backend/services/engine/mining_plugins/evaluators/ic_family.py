"""IC 族指标描述符（descriptor-only）。

数值由回测路径既有实现产生（``_vectorized_daily_spearman_ic`` → 表字段
ic_value/rank_ic + metadata 的 icir/rank_icir/n_obs），此处只登记展示契约，
不做计算——另起一份实现就会与回测口径漂移。
"""

from __future__ import annotations

from ..base import MetricDescriptor
from ..registry import register_descriptors

register_descriptors(
    (
        MetricDescriptor(
            key="ic",
            label="IC（日均）",
            group="prediction",
            unit="ratio",
            better="higher",
            precision=4,
            description="因子值与次日收益的日度截面相关系数均值",
        ),
        MetricDescriptor(
            key="rank_ic",
            label="Rank IC（日中位）",
            group="prediction",
            unit="ratio",
            better="higher",
            precision=4,
            description="日度截面 Spearman 秩相关的中位数",
        ),
        MetricDescriptor(
            key="icir",
            label="ICIR",
            group="prediction",
            unit="ratio",
            better="higher",
            precision=3,
            description="日均 IC ÷ 日度 IC 标准差",
        ),
        MetricDescriptor(
            key="rank_icir",
            label="Rank ICIR",
            group="prediction",
            unit="ratio",
            better="higher",
            precision=3,
            description="日均 Rank IC ÷ 其标准差",
        ),
        MetricDescriptor(
            key="n_obs",
            label="有效天数",
            group="prediction",
            unit="days",
            better="none",
            precision=0,
            description="参与 IC 计算的交易日数",
        ),
    )
)
