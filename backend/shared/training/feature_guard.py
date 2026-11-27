"""兼容转发：实现已移至 `backend.shared.feature_guard`。

移动原因：`backend/shared/training/__init__.py` 会 import fastapi/pydantic，
训练容器（只装训练依赖）导入本护栏时会被包初始化拖挂；顶层 shared 包的
`__init__` 是空 docstring，训练端与 API 端都能零成本导入。
"""

from backend.shared.feature_guard import (
    describe_leaks,
    find_hard_label_leaks,
    find_soft_return_features,
)

__all__ = [
    "describe_leaks",
    "find_hard_label_leaks",
    "find_soft_return_features",
]
