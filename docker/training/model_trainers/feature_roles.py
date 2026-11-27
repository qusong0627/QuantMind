"""特征角色（纯函数，不 import 第三方库）。

行业编码列由 `data/loading.py` 映射为非负整数（缺失映射到 max+1，绝不 -1），
树模型必须按类别处理，否则数值序数会产生假序关系。预处理已在截面变换中
排除这些列（`data/splits.py::_prepare_arrays` 的 `_exclude`），编码保持原值。
"""

from __future__ import annotations

from typing import Iterable

#: 行业编码列（与 CatBoost 训练器的 cat_features 口径一致）。
CATEGORICAL_FEATURES: tuple[str, ...] = ("ind_code_l1", "ind_code_l2")


def categorical_feature_names(features: Iterable[str]) -> list[str]:
    """返回 features 中需要按类别处理的列名（保持输入顺序，去重）。"""
    wanted = set(CATEGORICAL_FEATURES)
    out: list[str] = []
    for feature in features:
        name = str(feature)
        if name in wanted and name not in out:
            out.append(name)
    return out
