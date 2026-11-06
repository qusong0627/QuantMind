"""FactorMiner（arXiv:2602.14670）因子表达式工具包。

- ``dsl`` 白名单制解析器（禁 eval——检索式因子库把 eval() 当求值器的 RCE
  教训见因子挖掘方法论文档）；
- ``evaluator`` 宽面 pandas 求值（本仓唯一语义口径）。
"""

from .dsl import (
    FEATURES,
    OPERATORS,
    Call,
    Complexity,
    Feature,
    Node,
    OperatorSpec,
    complexity,
    min_history,
    parse,
    walk,
    window_arg_index,
)
from .evaluator import evaluate, missing_implementations

__all__ = [
    "FEATURES",
    "OPERATORS",
    "Call",
    "Complexity",
    "Feature",
    "Node",
    "OperatorSpec",
    "complexity",
    "evaluate",
    "min_history",
    "missing_implementations",
    "parse",
    "walk",
    "window_arg_index",
]
