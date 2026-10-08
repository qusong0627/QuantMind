"""PFS 族描述符（descriptor-only）。

数值由既有实现产生：qlib 路径 ``_compute_pfs_quality``（alpha_agent.py）与
H5 子进程脚本内联块，都走 ``backend.shared.factor_quality``（与训练侧筛选同公式），
此处只登记展示契约，不做计算。
"""

from __future__ import annotations

from ..base import MetricDescriptor
from ..registry import register_descriptors

register_descriptors(
    (
        MetricDescriptor(
            key="quality.pfs",
            label="PFS（扰动保真度）",
            group="robustness",
            unit="score",
            better="higher",
            precision=4,
            description="截面加噪后排序保持率，<0.9 预警",
        ),
        MetricDescriptor(
            key="quality.pfs_gauss",
            label="PFS-Gauss",
            group="robustness",
            unit="score",
            better="higher",
            precision=4,
            description="高斯噪声扰动下的 PFS",
        ),
        MetricDescriptor(
            key="quality.pfs_t",
            label="PFS-T",
            group="robustness",
            unit="score",
            better="higher",
            precision=4,
            description="t 分布噪声扰动下的 PFS",
        ),
    )
)
