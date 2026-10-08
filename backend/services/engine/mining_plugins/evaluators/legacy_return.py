"""毛收益族描述符（descriptor-only）。

annual_return / sharpe_ratio / max_drawdown 由回测路径既有实现写入表字段，
此处只登记展示契约，不做计算。
"""

from __future__ import annotations

from ..base import MetricDescriptor
from ..registry import register_descriptors

register_descriptors(
    (
        MetricDescriptor(
            key="annual_return",
            label="年化收益（毛）",
            group="return",
            unit="pct",
            better="higher",
            precision=2,
            description="多头组合年化收益（未扣成本）",
        ),
        MetricDescriptor(
            key="sharpe_ratio",
            label="夏普（毛）",
            group="return",
            unit="ratio",
            better="higher",
            precision=3,
            description="毛收益年化夏普",
        ),
        MetricDescriptor(
            key="max_drawdown",
            label="最大回撤（毛）",
            group="return",
            unit="pct",
            better="lower",
            precision=2,
            description="毛净值最大回撤",
        ),
    )
)
