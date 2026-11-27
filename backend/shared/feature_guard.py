"""训练特征名的前视泄漏护栏（提交 API 与训练容器共用）。

背景：
- `factor_selection` / 手工选列 / 外部因子库都可能把标签列或远期收益列带进
  `features`。`label`、`label_return_h5`、`fwd_ret_*` 这类列在 T 日就包含
  T+N 的信息，一旦进模型就是未来函数。
- 训练加载器对非直读因子源会剔除 `return_{n}d`（`data/loading.py` 的
  `_LEAKY_RETURN_COLS`）；但直读因子源保留该列，语义取决于因子源自身口径，
  本模块无法判定，故只做告警不做阻断。

口径（fail-closed 分级）：
- HARD：`label` 家族、`fwd/future/forward/next` 收益 → 提交即 422、训练入口即
  RuntimeError。宁可误伤，不可泄漏。
- SOFT：`return_{n}d` → 只告警；由加载器的源类型判定最终去留。

本模块必须保持纯函数（不 import 训练/API 依赖）。放在 `backend/shared/` 顶层
而不是 `backend/shared/training/`，是为了让训练容器导入时不触发
`training/__init__.py` 的 fastapi/pydantic 依赖。
"""

from __future__ import annotations

import re
from typing import Iterable

#: 硬阻断：任何以 label 开头的列（label / label_return / label_vol / label_rank ...）
_HARD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^label($|_)", re.IGNORECASE),
    re.compile(r"^(fwd|future|forward)_?ret", re.IGNORECASE),
    re.compile(r"^(fwd|future|forward)_?\d*d_?ret", re.IGNORECASE),
    re.compile(r"^(next|future)_\d*d?_?ret", re.IGNORECASE),
)

#: 软告警：不同因子源里可能是过去收益（合法）也可能是远期收益（泄漏）
_SOFT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^return_\d+d$", re.IGNORECASE),
)


def find_hard_label_leaks(features: Iterable[str]) -> list[str]:
    """返回命中硬阻断规则的特征名（保持输入顺序，去重）。"""
    out: list[str] = []
    for raw in features:
        name = str(raw or "").strip()
        if not name or name in out:
            continue
        if any(p.search(name) for p in _HARD_PATTERNS):
            out.append(name)
    return out


def find_soft_return_features(features: Iterable[str]) -> list[str]:
    """返回命中软告警规则的特征名（`return_{n}d`）。"""
    out: list[str] = []
    for raw in features:
        name = str(raw or "").strip()
        if not name or name in out:
            continue
        if any(p.search(name) for p in _SOFT_PATTERNS):
            out.append(name)
    return out


def describe_leaks(leaks: Iterable[str]) -> str:
    """统一的错误文案片段（API 422 与训练 RuntimeError 共用）。"""
    names = list(leaks)
    return (
        "特征包含标签/远期收益列（前视泄漏）："
        + ", ".join(names)
        + "。请从 features 中移除；若确为过去收益，请改用不含 label/fwd/future/next 的列名。"
    )
