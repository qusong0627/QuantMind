"""FM-DSL：FactorMiner 论文（arXiv:2602.14670）附录 D 表达式语法的解析器。

**白名单制、禁 eval**（既往教训：检索式因子库把 eval() 当求值器 = RCE）。
算子/字段/窗口/数值全部在解析期校验，产出的 AST 是冻结 dataclass。

语法（39 算子，附录 D 全库实测清单）：

- 一元 ``Abs CsRank Log Neg Sign``（CsRank 为截面秩，其余逐元素）；
- 逐元素二元 ``Add And Div Eq Greater Less Max2 Min2 Mul Or Sub``；
- 时间窗 ``Delay Delta EMA Kurt Max Mean Med Min Resi Rsquare Skew Slope
  SMA Std TsArgMax TsMax TsMin TsRank WMA``——第二参必须是正整数字面量；
- 双序列窗 ``Corr(x, y, n)``、``Cov(x, y, n)``；三目 ``IfElse(cond, a, b)``；
- ``SignedPower(x, p)``——p 为（可分数）数值字面量。

字段白名单：``$open $high $low $close $volume $amt $vwap $returns``。

资源上界（解析期校验）：窗口字面量 ≤ ``MAX_WINDOW``、嵌套 ≤ ``MAX_DEPTH``
——避免「解析通过、求值期 MemoryError/RecursionError」逃出 ValueError 契约。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from collections.abc import Iterator

# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Feature:
    name: str


@dataclass(frozen=True)
class Call:
    op: str
    args: tuple[Node, ...]


Node = Num | Feature | Call

#: 落库前缀：因子池 factor_code 里 FM-DSL 表达式的形态（如 ``fm:Neg($close)``）。
#: 与统计派的裸 qlib 表达式区分路由（``routers/alpha_agent._detect_factor_kind``
#: 双判据之一）；两类都进 "expression" kind，但求值器不同——FM 走
#: ``factorminer.runtime`` 的自有白名单求值器，qlib 走 qlib 原生。
FM_CODE_PREFIX = "fm:"

FEATURES: frozenset[str] = frozenset(
    {"$open", "$high", "$low", "$close", "$volume", "$amt", "$vwap", "$returns"}
)


# --------------------------------------------------------------------------
# 算子注册表
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorSpec:
    """kind: unary | elementwise | window | window2 | ternary | power。"""

    arity: int
    kind: str


#: 窗口字面量上界：求值器按 n 分配缓冲（``_ts_argmax`` 的 pad = (n−1)×标的数），
#: 无上界时 ``Mean($close, 999999999)`` 会在求值期 MemoryError 而不是解析期
#: ValueError——本库实测 max_window=60、仓库候选暖机上界 250，1000 远超合理域。
MAX_WINDOW = 1000

#: 嵌套深度上界：递归下降在 ~千层会 RecursionError（非契约内异常）；
#: 本库实测 max depth=9，64 给生成式表达式留足余量。
MAX_DEPTH = 64


_OPS: tuple[tuple[str, int, str], ...] = (
    # (名称, 元数, 类别) —— 与附录 D 实测清单逐一对齐（tests 钉死）
    ("Abs", 1, "unary"),
    ("CsRank", 1, "unary"),
    ("Log", 1, "unary"),
    ("Neg", 1, "unary"),
    ("Sign", 1, "unary"),
    ("Add", 2, "elementwise"),
    ("And", 2, "elementwise"),
    ("Div", 2, "elementwise"),
    ("Eq", 2, "elementwise"),
    ("Greater", 2, "elementwise"),
    ("Less", 2, "elementwise"),
    ("Max2", 2, "elementwise"),
    ("Min2", 2, "elementwise"),
    ("Mul", 2, "elementwise"),
    ("Or", 2, "elementwise"),
    ("Sub", 2, "elementwise"),
    ("Delay", 2, "window"),
    ("Delta", 2, "window"),
    ("EMA", 2, "window"),
    ("Kurt", 2, "window"),
    ("Max", 2, "window"),
    ("Mean", 2, "window"),
    ("Med", 2, "window"),
    ("Min", 2, "window"),
    ("Resi", 2, "window"),
    ("Rsquare", 2, "window"),
    ("Skew", 2, "window"),
    ("Slope", 2, "window"),
    ("SMA", 2, "window"),
    ("Std", 2, "window"),
    ("TsArgMax", 2, "window"),
    ("TsMax", 2, "window"),
    ("TsMin", 2, "window"),
    ("TsRank", 2, "window"),
    ("WMA", 2, "window"),
    ("SignedPower", 2, "power"),
    ("Corr", 3, "window2"),
    ("Cov", 3, "window2"),
    ("IfElse", 3, "ternary"),
)

OPERATORS: dict[str, OperatorSpec] = {
    name: OperatorSpec(arity, kind) for name, arity, kind in _OPS
}


def window_arg_index(spec: OperatorSpec) -> int | None:
    """窗口算子吃哪一号参数：window→1，window2→2，其余 None。"""
    if spec.kind == "window":
        return 1
    if spec.kind == "window2":
        return 2
    return None


# --------------------------------------------------------------------------
# 词法 / 语法
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(\$[A-Za-z][A-Za-z0-9]*)"
    r"|([A-Za-z][A-Za-z0-9]*)"
    r"|(-?\d+\.?\d*(?:[eE][-+]?\d+)?)"
    r"|([(),]))"
)

#: 词法单元：("FEATURE"|"IDENT"|"NUMBER"|"PUNCT", 文本)
Token = tuple[str, str]


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            if not text[pos:].strip():
                break  # 行尾空白
            raise ValueError(f"无法识别的记号（位置 {pos}）：{text[pos : pos + 10]!r}")
        feature, ident, number, punct = match.groups()
        if feature is not None:
            tokens.append(("FEATURE", feature))
        elif ident is not None:
            tokens.append(("IDENT", ident))
        elif number is not None:
            tokens.append(("NUMBER", number))
        else:
            assert punct is not None  # 正则保证四组必有一中
            tokens.append(("PUNCT", punct))
        pos = match.end()
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.tokens = tokenize(text)
        self.pos = 0
        self.depth = 0

    def peek(self) -> tuple[str | None, str | None]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return (None, None)

    def take(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse_expr(self) -> Node:
        kind, value = self.peek()
        if kind == "FEATURE":
            self.take()
            assert value is not None
            if value not in FEATURES:
                raise ValueError(
                    f"未知字段 {value}（允许：{', '.join(sorted(FEATURES))}）"
                )
            return Feature(value)
        if kind == "NUMBER":
            self.take()
            assert value is not None
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"字面量必须是有限数：{value}")
            return Num(number)
        if kind == "IDENT":
            self.take()
            assert value is not None
            spec = OPERATORS.get(value)
            if spec is None:
                raise ValueError(f"未知算子 {value}")
            if self.peek() != ("PUNCT", "("):
                raise ValueError(f"算子 {value} 后必须跟左括号")
            self.take()
            return self._parse_call(value, spec)
        if kind is None:
            raise ValueError("表达式意外结束")
        raise ValueError(f"意外的记号 {value!r}")

    def _parse_call(self, op: str, spec: OperatorSpec) -> Call:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ValueError(f"表达式嵌套过深（超过 {MAX_DEPTH} 层）：{op}(…)")
        args: list[Node] = []
        if self.peek() == ("PUNCT", ")"):
            self.take()
        else:
            while True:
                args.append(self.parse_expr())
                kind, value = self.peek()
                if (kind, value) == ("PUNCT", ","):
                    self.take()
                    continue
                if (kind, value) == ("PUNCT", ")"):
                    self.take()
                    break
                if kind is None:
                    raise ValueError(f"缺少右括号：{op}(…) 未闭合")
                raise ValueError(f"意外的记号 {value!r}（{op} 的参数之间）")
        if len(args) != spec.arity:
            raise ValueError(
                f"算子 {op} 元数不匹配：需要 {spec.arity} 个参数，收到 {len(args)} 个"
            )
        self._validate_args(op, spec, args)
        self.depth -= 1
        return Call(op, tuple(args))

    @staticmethod
    def _validate_args(op: str, spec: OperatorSpec, args: list[Node]) -> None:
        window_index = window_arg_index(spec)
        if window_index is not None:
            window = args[window_index]
            if not isinstance(window, Num):
                raise ValueError(
                    f"算子 {op} 的窗口参数必须是正整数字面量（收到非字面量参数）"
                )
            if not math.isfinite(window.value) or window.value != int(window.value):
                raise ValueError(
                    f"算子 {op} 的窗口参数必须是正整数（收到 {window.value}）"
                )
            if int(window.value) < 1:
                raise ValueError(
                    f"算子 {op} 的窗口参数必须是正整数（收到 {window.value}）"
                )
            if int(window.value) > MAX_WINDOW:
                raise ValueError(
                    f"算子 {op} 的窗口参数超出上限 {MAX_WINDOW}（收到 {window.value}）"
                )
        if spec.kind == "power":
            exponent = args[1]
            if not isinstance(exponent, Num):
                raise ValueError(
                    f"算子 {op} 的指数必须是数值字面量（收到非字面量参数）"
                )


def parse(text: str) -> Node:
    """解析一条 FM-DSL 表达式；一切非法形态在解析期即抛 ``ValueError``。"""
    parser = _Parser(text)
    if not parser.tokens:
        raise ValueError("空表达式")
    node = parser.parse_expr()
    kind, value = parser.peek()
    if kind is not None:
        raise ValueError(f"表达式尾部有多余记号：{value!r}")
    return node


# --------------------------------------------------------------------------
# 静态分析
# --------------------------------------------------------------------------

_RETURNS_MIN_HISTORY = 2  # $returns 需要前一日收盘


def min_history(node: Node) -> int:
    """保守暖机根数：宁多取一根 bar 不欠（欠暖机会让首窗不满、与
    「表达式带扩展历史求值」不等价）。Num→0；$returns→2 其余字段→1；
    窗口算子 = 子节点上界 + n；其余 = 子节点上界。"""
    if isinstance(node, Num):
        return 0
    if isinstance(node, Feature):
        return _RETURNS_MIN_HISTORY if node.name == "$returns" else 1
    spec = OPERATORS[node.op]
    child = max((min_history(arg) for arg in node.args), default=0)
    window_index = window_arg_index(spec)
    if window_index is not None:
        window = node.args[window_index]
        assert isinstance(window, Num)
        return child + int(window.value)
    return child


@dataclass(frozen=True)
class Complexity:
    """AST 级结构复杂度指标——T-FM-06 结构原创性闸专用。

    T-FE-05 统计派选择分的简单性因子**不用**本口径（FM-DSL 解析不了
    qlib 中缀算术），其单源是 ``statistical.complexity``（字符串级计数）。
    """

    node_count: int
    op_count: int
    depth: int  # 根→叶最长链上的节点数（叶子树 = 1）
    max_window: int
    literal_count: int


def complexity(node: Node) -> Complexity:
    node_count = 0
    op_count = 0
    literal_count = 0
    max_window = 0

    def visit(current: Node, depth: int) -> int:
        nonlocal node_count, op_count, literal_count, max_window
        node_count += 1
        if isinstance(current, Num):
            literal_count += 1
            return depth
        if isinstance(current, Feature):
            return depth
        op_count += 1
        spec = OPERATORS[current.op]
        window_index = window_arg_index(spec)
        if window_index is not None:
            window = current.args[window_index]
            assert isinstance(window, Num)
            max_window = max(max_window, int(window.value))
        return max(visit(arg, depth + 1) for arg in current.args)

    depth = visit(node, 1)
    return Complexity(node_count, op_count, depth, max_window, literal_count)


def walk(node: Node) -> Iterator[Node]:
    """前序遍历 AST（先根、后参数）。"""
    yield node
    if isinstance(node, Call):
        for arg in node.args:
            yield from walk(arg)
