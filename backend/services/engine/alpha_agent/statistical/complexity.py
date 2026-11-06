"""统计派 · 表达式复杂度与简单性因子（T-FE-05）。

用途：``selection_score`` 乘简单性因子——同 IC/稳定性的候选，表达式越
简越靠前；同分再按结构简单性取简（``engine._rank_eligible``）。

**口径边界（唯一实现，勿在别处重写）**：统计派候选的 ``expression`` 是
qlib 中缀表达式（``Ref($close,5)/$close-1``），FM-DSL（``factorminer.dsl``，
FactorMiner 附录 D 的纯函数形式语法）**解析不了中缀算术**——此处字符串级
计数是范围受限的口径：纯函数、无 eval（既往教训：检索式因子库拿 eval()
当求值器 = RCE）、幂等、不依赖求值器。FM 因子的 AST 级复杂度另有单源
（``factorminer.dsl.complexity``，T-FM-01 产出），两条口径不互相冒充。

计数规则（逐字符记账，金样据此核对）：
- ``op_count`` = 函数调用数（标识符紧跟 ``(``）+ 中缀算子数（``- + * / < >``；
  科学计数法 ``1e-8`` 先剔除，其符号不是算子；多字符运算符按构成字符计——
  ``**``→2、``>=``/``<=``→1、``==``→0，当前候选无此用法）；
- ``literal_count`` = 数值字面量数（含科学计数法/小数）；前缀是词字符或点的
  数字串不算（``$turn_1``/``Ref20`` 的数字是标识符的一部分，不是参数）。

**输入域**：统计派候选注册表（``candidates.py``）内的表达式——固定数十条、
每条数百字符内（实测真实表达式约 100 字符、计数 42µs）。计数正则在超长不
匹配段上是二次扫描，未按不可信长文本设计；若表达式未来变成用户可提供，
先换线性扫描再复用本口径。

简单性因子 = ``1 / (1 + op_count × log1p(literal_count))``——参数空间
（字面量数）越大、算子越多，过拟合税越高；**literal=0 时恰为 1.0**：
没有参数空间就没有参数过拟合税，其结构复杂度由「同分取简」排序条款
兜底（不是漏罚，是分工）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = ["ExpressionComplexity", "expression_complexity", "simplicity_factor"]

#: 函数调用：标识符（字母/下划线开头）紧跟可选的空白与左括号。
_CALL_RE = re.compile(r"[A-Za-z_]\w*\s*\(")

#: 科学计数法整体（先把其中的 +/- 剔出中缀计数）。
_SCI_RE = re.compile(r"\d+(?:\.\d+)?[eE][+-]?\d+")

#: 数值字面量（含小数与科学计数法）。前缀是词字符或点（``turn_1``、``Ref20``）
#: 的数字串属标识符，不计——否则未来候选引用这类字段名会静默多缴参数税。
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")

#: 中缀算子（在剔除科学计数法后的文本上计数）。
_INFIX_RE = re.compile(r"[-+*/<>]")


@dataclass(frozen=True)
class ExpressionComplexity:
    """qlib 中缀表达式的结构复杂度（计数级，非 AST 级）。"""

    op_count: int
    literal_count: int


def expression_complexity(expr: str) -> ExpressionComplexity:
    """表达式字符串 → 复杂度计数。纯函数；永不抛（计数退化也有确定值）。"""
    stripped = _SCI_RE.sub(" ", str(expr))
    op_count = len(_CALL_RE.findall(stripped)) + len(_INFIX_RE.findall(stripped))
    literal_count = len(_NUM_RE.findall(str(expr)))
    return ExpressionComplexity(op_count=op_count, literal_count=literal_count)


def simplicity_factor(op_count: int, literal_count: int) -> float:
    """简单性因子 ∈ (0, 1]：1/(1 + op × log1p(lit))。

    - 有参数空间（lit ≥ 1）时对 op 严格递减；
    - 有算子（op ≥ 1）时对 lit 严格递减；
    - lit = 0 → 恰为 1.0（无参数空间，不缴参数税）。
    """
    return 1.0 / (1.0 + op_count * math.log1p(literal_count))
