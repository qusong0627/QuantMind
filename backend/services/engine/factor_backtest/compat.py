"""T-FB-02 因子代码列依赖静态提取与跨市场兼容性分类。

「因子 × 市场适配矩阵」的前置闸门：只用**静态**判据（AST 里的 ``$`` 列 token
是否 ⊆ 市场列集）在跑批之前就能给出 ``data_unsupported``（富化列在非 CN 不
存在），免烧算力；判不穿的动态写法归 ``unknown`` 交由实跑裁决，绝不猜。

实现纪律：
- 列 token 一律从**字符串常量**里取（``df["$close"]`` 的下标键是规范写法），
  ``$`` 变量名/注释/docstring 都算不上依赖；
- ``"$" + name``、``f"$turn_{n}"``、``"$%s" % x`` 这类拼接/格式化判
  ``dynamic``——token 集不完整时宁可 unknown 也不误杀/误放；
- 缺列证据（静态 token ⊄ 市场列集）比动态疑点更硬：即便代码里另有动态写法，
  也判 ``data_unsupported``。
"""

from __future__ import annotations

import ast
import re
from typing import NamedTuple

#: 五市场 qlib bin 实测基础列（2026-10-09 ``ls features/<标的>/``：
#: US/HK/BC/FUTURES 与 CN 均为 amount/close/factor/high/low/open/volume）。
BASE_COLUMNS: frozenset[str] = frozenset(
    {"$open", "$high", "$low", "$close", "$volume", "$amount", "$factor"}
)

#: CN 挖掘同源富化契约 = 39 列（``daily_pv_all.h5`` 实测列头，2026-10-09）。
#: 这是因子**挖掘时看到的那套列**——CN 目标市场的可用列集以它为准（bin 里的
#: ``change`` 不在挖掘契约内，因子从未依赖它，也不据此判 portable）。
CN_MINING_COLUMNS: frozenset[str] = frozenset(
    {
        "$adx_14",
        "$amount",
        "$atr_14",
        "$bb_pos",
        "$bb_width",
        "$beta_20",
        "$bp",
        "$chip_profit_20",
        "$close",
        "$concept_hot",
        "$div_yield",
        "$ep",
        "$factor",
        "$float_mv",
        "$high",
        "$idio_vol_20",
        "$ind_strength",
        "$low",
        "$macd_hist",
        "$maxdd_20",
        "$mfi_14",
        "$netflow_20",
        "$netflow_5",
        "$np_growth",
        "$np_ttm",
        "$obv_slope",
        "$open",
        "$parkinson_20",
        "$pb",
        "$pe_ttm",
        "$peg",
        "$ps_ttm",
        "$roe",
        "$rsi_14",
        "$total_mv",
        "$turn_20",
        "$turn_5",
        "$turn_z_20",
        "$volume",
    }
)

#: 列 token：``$`` 后必须紧跟标识符首字符。``"$%s"``、``"${k}"``、裸 ``"$"``
#: 都取不出 token——由「含 $ 却无 token」规则判 dynamic。
_TOKEN_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")

_DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class TokenScan(NamedTuple):
    """静态扫描结果：确定取到的列 token 集 + 是否存在判不穿的动态写法。"""

    values: frozenset[str]
    dynamic: bool


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """收集 docstring 常量节点的 id（首个语句为字符串常量的模块/函数/类）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _is_pure_const_expr(node: ast.AST) -> bool:
    """表达式是否由字符串/数字常量经 ``+`` 拼成（可当静态字面量看）。"""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_pure_const_expr(node.left) and _is_pure_const_expr(node.right)
    return False


def _dynamic_fragment_ids(tree: ast.AST) -> tuple[set[int], bool]:
    """收集「参与非纯字面量拼接/格式化」的字符串常量 id。

    - ``"$" + name`` / ``"$turn_" + str(n)``：字符串常量**直接**与非常量表达式
      拼接——半截 token 会误导分类，该常量判 dynamic 且不计入 token；
      注意判据只看**直接操作数**：``df["$close"] + df["$volume"]`` 两侧都是
      下标表达式（其中的 ``$`` 是下标键、不是拼接片段），不得误伤；
    - ``f"$turn_{n}"``：JoinedStr 含插值且字面量部分带 ``$``——判 dynamic；
      ``f"$close"``（无插值）不在此列，仍是静态 token。
    """
    ids: set[int] = set()
    dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for side, other in ((node.left, node.right), (node.right, node.left)):
                if (
                    isinstance(side, ast.Constant)
                    and isinstance(side.value, str)
                    and "$" in side.value
                    and not _is_pure_const_expr(other)
                ):
                    ids.add(id(side))
                    dynamic = True
        elif isinstance(node, ast.JoinedStr):
            if not any(isinstance(v, ast.FormattedValue) for v in node.values):
                continue
            for v in node.values:
                if (
                    isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                    and "$" in v.value
                ):
                    ids.add(id(v))
                    dynamic = True
    return ids, dynamic


def extract_column_tokens(code: str) -> TokenScan:
    """从因子代码静态提取 ``$`` 列 token 集与动态写法标记。

    Raises:
        ValueError: 代码语法错误（无法建 AST；调用方归 ``unknown``）。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"因子代码语法错误: {e.msg} (line {e.lineno})") from e

    excluded = _docstring_constant_ids(tree)
    frag_ids, dynamic = _dynamic_fragment_ids(tree)
    excluded |= frag_ids

    values: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in excluded or "$" not in node.value:
            continue
        found = _TOKEN_RE.findall(node.value)
        if found:
            values.update(found)
        else:
            # 含 $ 却取不出 token（"$" / "$%s_5" / "${k}"）——判不穿，归动态
            dynamic = True
    return TokenScan(frozenset(values), dynamic)


def classify_factor(code: str, market_columns: frozenset[str] | set[str]) -> dict:
    """按目标市场列集给因子代码定兼容性档。

    Returns: ``{"status", "tokens", "missing", "dynamic", "reason"}``
        - ``portable``：静态 token ⊆ 市场列集 → 可跑；
        - ``data_unsupported``：静态缺列（missing 清单）→ 免跑登记；
        - ``unknown``：语法错误 / 动态写法 / 无列引用 → 照跑裁决。
    """
    try:
        scan = extract_column_tokens(code)
    except ValueError:
        return {
            "status": "unknown",
            "tokens": [],
            "missing": [],
            "dynamic": False,
            "reason": "syntax_error",
        }

    tokens = sorted(scan.values)
    missing = sorted(scan.values - set(market_columns))
    base = {"tokens": tokens, "missing": missing, "dynamic": scan.dynamic}
    if missing:
        return {**base, "status": "data_unsupported", "reason": "missing_columns"}
    if scan.dynamic:
        return {**base, "status": "unknown", "reason": "dynamic_column_reference"}
    if not tokens:
        return {**base, "status": "unknown", "reason": "no_column_reference"}
    return {**base, "status": "portable", "reason": None}
