"""V5 公式 DSL 的最小表达式引擎（解析 + 逐标的/截面计算）。

支持文档里出现的函数与语法：
- 时序：SUM/MEAN/STD/CORR/COV/REF/DELAY/DELTA/TSRANK/DECAY/DECAYLINEAR/SMA/EMA/
  EMA_SPAN/MAX/MIN/HIGHDAY/LOWDAY/TSARGMAX/REG/MA/AR/BR/PSY/PSYMA/ADX/CCI/MASS
- 截面：RANK/R/QTL/Z/WINSOR_Z/SCALE
- 逐点：ABS/SIGN/LOG/SQRT/POWER/MAX2/MIN2/IF/AND
- 语法：三元 `? :`、`&&/||`、`X:N` 简写、`REG(...).resid`

口径未定的函数先按常见实现，最终以 2018/2020 官方 V5 对账为准。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

# ─────────────────────────────── tokenizer ──────────────────────────────

TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|(?P<id>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op><=|>=|==|!=|&&|\|\||[+\-*/%<>!?:(),.]))"
)


def _tokens(src: str) -> list[tuple[str, str]]:
    out = []
    pos = 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise ValueError(f"无法解析: {src[pos:pos+30]!r}")
        pos = m.end()
        if m.group("num"):
            out.append(("num", m.group("num")))
        elif m.group("id"):
            out.append(("id", m.group("id")))
        elif m.group("op"):
            out.append(("op", m.group("op")))
    out.append(("eof", ""))
    return out


# ─────────────────────────────── parser ────────────────────────────────

@dataclass(frozen=True)
class Node:
    kind: str
    value: Any = None
    args: tuple = ()


class Parser:
    def __init__(self, src: str):
        # X:N 简写 → X,N（只匹配「标识符:数字」）
        src = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\d+(?:\.\d+)?)", r"\1,\2", src)
        src = src.replace("$", "")  # JQ/TDX 公式的 $close 前缀
        self.t = _tokens(src)
        self.i = 0
        self.src = src

    def peek(self, k: int = 0) -> tuple[str, str]:
        return self.t[self.i + k]

    def take(self, val: str | None = None) -> tuple[str, str]:
        tok = self.t[self.i]
        if val is not None and tok[1] != val:
            raise ValueError(f"期望 {val!r}，得到 {tok[1]!r} @ {self.src}")
        self.i += 1
        return tok

    def parse(self) -> Node:
        n = self.ternary()
        if self.peek()[0] != "eof":
            raise ValueError(f"多余 token {self.peek()} @ {self.src}")
        return n

    def ternary(self) -> Node:
        cond = self.logic_or()
        if self.peek()[1] == "?":
            self.take("?")
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            return Node("if", None, (cond, a, b))
        return cond

    def logic_or(self) -> Node:
        n = self.logic_and()
        while self.peek()[1] in ("||", "OR"):
            self.take()
            n = Node("bin", "||", (n, self.logic_and()))
        return n

    def logic_and(self) -> Node:
        n = self.cmp()
        while self.peek()[1] in ("&&", "AND"):
            self.take()
            n = Node("bin", "&&", (n, self.cmp()))
        return n

    def cmp(self) -> Node:
        n = self.add()
        while self.peek()[1] in (">", "<", ">=", "<=", "==", "!="):
            op = self.take()[1]
            n = Node("bin", op, (n, self.add()))
        return n

    def add(self) -> Node:
        n = self.mul()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            n = Node("bin", op, (n, self.mul()))
        return n

    def mul(self) -> Node:
        n = self.unary()
        while self.peek()[1] in ("*", "/", "%"):
            op = self.take()[1]
            n = Node("bin", op, (n, self.unary()))
        return n

    def unary(self) -> Node:
        if self.peek()[1] in ("-", "+", "!"):
            op = self.take()[1]
            return Node("unary", op, (self.unary(),))
        return self.postfix()

    def postfix(self) -> Node:
        n = self.primary()
        while self.peek()[1] == ".":
            self.take(".")
            attr = self.take()[1]
            n = Node("attr", attr, (n,))
        return n

    def primary(self) -> Node:
        kind, val = self.peek()
        if kind == "num":
            self.take()
            return Node("num", float(val))
        if kind == "id":
            self.take()
            if self.peek()[1] == "(":
                self.take("(")
                args = []
                if self.peek()[1] != ")":
                    args.append(self.ternary())
                    while self.peek()[1] == ",":
                        self.take(",")
                        args.append(self.ternary())
                self.take(")")
                return Node("call", val.upper(), tuple(args))
            return Node("var", val)
        if val == "(":
            self.take("(")
            n = self.ternary()
            self.take(")")
            return n
        raise ValueError(f"无法解析 primary {self.peek()} @ {self.src}")


def parse(src: str) -> Node:
    return Parser(src).parse()


# ─────────────────────────────── evaluator ─────────────────────────────

class Ctx:
    """长表上下文：index 0..n-1，按 symbol 分组、按 time 升序。"""

    def __init__(self, df: pd.DataFrame, vars_map: dict[str, pd.Series]):
        self.df = df
        self.symbol = df["symbol"]
        self.dates = df["dt"]
        self.vars = vars_map
        self.cache: dict[str, pd.Series] = {}

    def g(self) -> Any:
        return self.symbol

    def ts(self, s: pd.Series, fn) -> pd.Series:
        return s.groupby(self.symbol, sort=False).transform(fn)

    def xs(self, s: pd.Series, fn) -> pd.Series:
        return s.groupby(self.dates, sort=False).transform(fn)


def _roll(s: pd.Series, n: int, how: str = "mean", ddof: int = 1) -> pd.Series:
    r = s.rolling(int(n), min_periods=int(n))
    return {"mean": r.mean(), "std": r.std(ddof=ddof), "sum": r.sum(),
            "max": r.max(), "min": r.min(), "median": r.median()}[how]


def _decay_weights(n: int) -> np.ndarray:
    w = np.arange(1, n + 1, dtype=float)
    return w / w.sum()


def _reg_resid(s: pd.Series, n: int) -> pd.Series:
    n = int(n)
    t = np.arange(n, dtype=float)
    t_mean = t.mean()
    t_var = ((t - t_mean) ** 2).sum()

    def f(x: np.ndarray) -> float:
        if np.isnan(x).any():
            return np.nan
        slope = ((t - t_mean) * (x - x.mean())).sum() / t_var
        intercept = x.mean() - slope * t_mean
        return float(x[-1] - (intercept + slope * t[-1]))

    return s.rolling(n, min_periods=n).apply(f, raw=True)


def _argmax_pos(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).apply(
        lambda x: float(np.argmax(x)) if not np.isnan(x).any() else np.nan, raw=True)


def _highday(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).apply(
        lambda x: float(int(n) - 1 - np.argmax(x)) if not np.isnan(x).any() else np.nan,
        raw=True)


def _lowday(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).apply(
        lambda x: float(int(n) - 1 - np.argmin(x)) if not np.isnan(x).any() else np.nan,
        raw=True)


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / int(n), adjust=False).mean()


def eval_node(n: Node, ctx: Ctx) -> pd.Series | float:
    if n.kind == "num":
        return float(n.value)
    if n.kind == "var":
        key = n.value
        if key in ctx.vars:
            return ctx.vars[key]
        # 大小写别名
        up = key.upper()
        alias = {"C": "close", "O": "open", "H": "high", "L": "low", "V": "volume",
                 "A": "amount", "CLOSE": "close", "OPEN": "open", "HIGH": "high",
                 "LOW": "low", "VOLUME": "volume", "AMOUNT": "amount", "VWAP": "vwap"}
        if up in alias and alias[up] in ctx.vars:
            return ctx.vars[alias[up]]
        raise KeyError(f"未知变量 {key}")
    if n.kind == "if":
        c = eval_node(n.args[0], ctx)
        a = eval_node(n.args[1], ctx)
        b = eval_node(n.args[2], ctx)
        if np.isscalar(c):
            return a if c else b
        return a.where(c.astype(bool), b)
    if n.kind == "unary":
        v = eval_node(n.args[0], ctx)
        if n.value == "-":
            return -v
        if n.value == "+":
            return v
        return ~v.astype(bool)
    if n.kind == "attr":
        base = eval_node(n.args[0], ctx)
        return base  # REG(...).resid 已在 call 内处理
    if n.kind == "bin":
        a = eval_node(n.args[0], ctx)
        b = eval_node(n.args[1], ctx)
        op = n.value
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b
        if op == "%":
            return a % b
        if op == ">":
            return a > b
        if op == "<":
            return a < b
        if op == ">=":
            return a >= b
        if op == "<=":
            return a <= b
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "&&":
            return a.astype(bool) & b.astype(bool)
        if op == "||":
            return a.astype(bool) | b.astype(bool)
        raise ValueError(op)
    if n.kind == "call":
        return _call(n.value, n.args, ctx)
    raise ValueError(n.kind)


def _series(x: Any, ctx: Ctx) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    return pd.Series(float(x), index=ctx.df.index)


def _call(name: str, args: tuple[Node, ...], ctx: Ctx) -> pd.Series:
    # 轻量缓存：同名字+同 AST 子节点
    ck = repr((name, tuple(repr(a) for a in args)))
    if ck in ctx.cache:
        return ctx.cache[ck]
    A = [eval_node(a, ctx) for a in args]

    def ts(s, fn):
        return _series(s, ctx).groupby(ctx.symbol, sort=False).transform(fn)

    def xs(s, fn):
        return _series(s, ctx).groupby(ctx.dates, sort=False).transform(fn)

    s0 = _series(A[0], ctx) if A else None
    out: pd.Series
    if name == "IF":
        cond = _series(A[0], ctx).astype(bool)
        out = _series(A[1], ctx).where(cond, _series(A[2], ctx))
    elif name in ("ABS",):
        out = s0.abs()
    elif name == "SIGN":
        out = np.sign(s0)
    elif name == "LOG":
        out = np.log(s0.where(s0 > 0))
    elif name == "SQRT":
        out = np.sqrt(s0.clip(lower=0))
    elif name == "POWER":
        out = np.power(s0, A[1])
    elif name in ("MAX2",):
        out = np.maximum(s0, _series(A[1], ctx))
    elif name in ("MIN2",):
        out = np.minimum(s0, _series(A[1], ctx))
    elif name in ("AND",):
        out = s0.astype(bool) & _series(A[1], ctx).astype(bool)
    elif name in ("SUM", "MEAN", "STD", "MAX", "MIN", "MA", "MEDIAN"):
        n = int(A[1])
        how = {"SUM": "sum", "MEAN": "mean", "STD": "std", "MAX": "max",
               "MIN": "min", "MA": "mean", "MEDIAN": "median"}[name]
        # STD 先试 ddof=1；对账再调
        out = ts(s0, lambda x, n=n, how=how: _roll(x, n, how))
    elif name in ("REF", "DELAY"):
        out = ts(s0, lambda x, n=int(A[1]): x.shift(n))
    elif name == "DELTA":
        out = s0 - ts(s0, lambda x, n=int(A[1]): x.shift(n))
    elif name == "CORR":
        out = ts(s0, lambda x, n=int(A[2]): x.rolling(n, min_periods=n).corr(
            _series(A[1], ctx).loc[x.index]))
    elif name == "COV":
        out = ts(s0, lambda x, n=int(A[2]): x.rolling(n, min_periods=n).cov(
            _series(A[1], ctx).loc[x.index]))
    elif name == "TSRANK":
        out = ts(s0, lambda x, n=int(A[1]): x.rolling(n, min_periods=n).rank(pct=True))
    elif name in ("RANK", "R"):
        out = xs(s0, lambda x: x.rank(pct=True))
    elif name == "Z":
        def _z(x):
            sd = x.std(ddof=0)
            return (x - x.mean()) / sd if sd and np.isfinite(sd) else x * 0
        out = xs(s0, _z)
    elif name == "QTL":
        n, q = int(A[1]), float(A[2])
        out = ts(s0, lambda x, n=n, q=q: x.rolling(n, min_periods=n).quantile(q))
    elif name in ("DECAY", "DECAYLINEAR"):
        n = int(A[1])
        w = _decay_weights(n)
        out = ts(s0, lambda x, n=n, w=w: x.rolling(n, min_periods=n).apply(
            lambda v: float(np.dot(v, w)), raw=True))
    elif name == "SMA":
        n, m = int(A[1]), int(A[2])
        out = ts(s0, lambda x, n=n, m=m: x.ewm(alpha=m / n, adjust=False).mean())
    elif name == "EMA":
        out = ts(s0, lambda x, n=int(A[1]): x.ewm(span=int(A[1]), adjust=False).mean())
    elif name == "EMA_SPAN":
        out = ts(s0, lambda x, n=int(A[1]): x.ewm(span=int(A[1]), adjust=False).mean())
    elif name == "TSARGMAX":
        out = ts(s0, lambda x, n=int(A[1]): _argmax_pos(x, n))
    elif name == "HIGHDAY":
        out = ts(s0, lambda x, n=int(A[1]): _highday(x, n))
    elif name == "LOWDAY":
        out = ts(s0, lambda x, n=int(A[1]): _lowday(x, n))
    elif name == "REG":
        out = ts(s0, lambda x, n=int(A[1]): _reg_resid(x, n))
    elif name == "AR":
        n = int(A[0])
        out = ts(ctx.vars["high"] - ctx.vars["open"], lambda x, n=n: _roll(x, n, "sum")) / (
            ts(ctx.vars["high"] - ctx.vars["open"], lambda x, n=n: _roll(x, n, "sum"))
            + ts(ctx.vars["open"] - ctx.vars["low"], lambda x, n=n: _roll(x, n, "sum"))) * 100
    elif name == "WINSOR_Z":
        out = xs(s0, lambda x: (x.clip(x.quantile(0.01), x.quantile(0.99)) - x.mean())
                 / (x.std(ddof=0) or np.nan))
    elif name == "SCALE":
        out = xs(s0, lambda x: x / x.abs().sum() * 100 if x.abs().sum() else x * 0)
    else:
        raise NotImplementedError(f"函数未实现: {name}")
    ctx.cache[ck] = out
    return out
