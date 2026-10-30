"""推理中性化（Neutralization Layer）共享实现。

- ``core``：纯函数（秩回归取残差），零 IO；
- ``controls``：控制变量快照读取（QuantDB 日分区）。

服务编排见 ``backend.services.engine.inference.neutralization_service``。
"""

from .core import (
    DEFAULT_CONTROLS,
    STATUS_DEGRADED,
    STATUS_OK,
    NeutralizationConfig,
    NeutralizationInputError,
    NeutralizationResult,
    neutralize_scores,
)
from .controls import (
    ControlSnapshot,
    ControlsUnavailableError,
    load_controls,
)

__all__ = [
    "DEFAULT_CONTROLS",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "ControlSnapshot",
    "ControlsUnavailableError",
    "NeutralizationConfig",
    "NeutralizationInputError",
    "NeutralizationResult",
    "load_controls",
    "neutralize_scores",
]
