#!/usr/bin/env python3
"""因子 DSL 引擎：值模型 / 词法语法 / 算子求值（口径唯一事实源）。

本模块由 ``data/factor_defs/_lookahead_test.py`` **原样搬运**而来（行为零变化），
供「无未来函数」截断不变性体检与后续因子表达式求值复用。

值模型
------
日频 ``df.index`` 为 DatetimeIndex；日内为 ``(time, minute)`` MultiIndex，
level 0 与日频对齐，二元运算经 ``_align`` / ``_bcast`` 自动广播。
标量广播依赖模块级栅格 ``_REF``，**每次 ``Evaluator.eval()`` 入口重新绑定**：
多个 Evaluator 共存时若只在 ``__init__`` 里绑定，后构造的会永久改掉前一个的
栅格（详见 ``eval`` 内注释）。

算子口径
--------
能复用平台实现的**一律复用** ``backend/scripts/alpha_library_factors.py``（口径唯一），
平台没有的按 SPEC §3 补齐、日内族按 SPEC §3.4 补齐。该脚本不是包模块，故经
``_ensure_scripts_on_path()`` 把它所在目录放上 ``sys.path`` 后再延迟导入
（与 ``backend/scripts/factor_factory.py`` 同法）。

用法::

    from backend.shared.factor_dsl import Evaluator, Node, ParseError, parse

    ast = parse("Z(MEAN(c, 20) / REF(c, 5))")
    value = Evaluator(frames, ind_onehot, minute_ret).eval(ast)
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── alpha_library_factors 的延迟导入助手 ──────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _ensure_scripts_on_path() -> None:
    """把 ``backend/scripts`` 放上 ``sys.path``（幂等，重复调用无害）。

    ``alpha_library_factors`` 是脚本目录里的**非包模块**，只能按顶层模块名导入。
    真正用到它是在 ``Evaluator._build_ops``，故到那里再调本助手再 import。
    """
    p = str(_SCRIPTS_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


# ══════════════════════════════════════════════════════════════════════════
# 1. 值模型：日频 / 日内两种 DataFrame，二元运算自动对齐
# ══════════════════════════════════════════════════════════════════════════


class Node:
    """DSL 求值结果。

    - 日频：``df.index`` 为 DatetimeIndex
    - 日内：``df.index`` 为 MultiIndex ``(time, minute)``，level 0 与日频对齐
    """

    __slots__ = ("df",)

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df

    @property
    def is_intraday(self) -> bool:
        return self.df.index.nlevels > 1

    def days(self) -> pd.DatetimeIndex:
        """返回该值覆盖的交易日（level 0）。"""
        idx = self.df.index
        return idx.get_level_values(0).unique() if self.is_intraday else idx

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kind = "intraday" if self.is_intraday else "daily"
        return f"<Node {kind} {self.df.shape}>"


_REF: Node | None = None  # 标量广播所用的栅格（Evaluator 初始化时设置）


def _node(x, index=None) -> Node:
    """标量 → 广播成 Node；Node 原样返回。``index`` 省略时用参考（日频）栅格。"""
    if isinstance(x, Node):
        return x
    r = _REF.df
    idx = r.index if index is None else index
    return Node(pd.DataFrame(float(x), index=idx, columns=r.columns))


def _bcast(x, index) -> pd.DataFrame:
    """把标量 / 日频值贴到目标栅格上；日频值贴到分钟栅格时按 level-0 广播。"""
    if isinstance(x, Node) and x.df.index.equals(index):
        return x.df
    n = _node(x, index)
    if n.df.index.equals(index):
        return n.df
    if index.nlevels > 1 and n.df.index.nlevels == 1:
        y = n.df.reindex(index.get_level_values(0))
        y.index = index
        return y
    return n.df.reindex(index)


def _align(a: Node, b: Node):
    """把日频广播到日内的分钟栅格上；返回两个可直接运算的 DataFrame。"""
    if a.is_intraday == b.is_intraday:
        return a.df, b.df
    if a.is_intraday:
        y = b.df.reindex(a.df.index.get_level_values(0))
        y.index = a.df.index
        return a.df, y
    x = a.df.reindex(b.df.index.get_level_values(0))
    x.index = b.df.index
    return x, b.df


def _binop(op):
    def f(a, b):
        x, y = _align(_node(a), _node(b))
        return Node(op(x, y))
    return f


_add = _binop(lambda x, y: x + y)
_sub = _binop(lambda x, y: x - y)
_mul = _binop(lambda x, y: x * y)
_div = _binop(lambda x, y: x / y)
_gt = _binop(lambda x, y: x > y)
_lt = _binop(lambda x, y: x < y)
_ge = _binop(lambda x, y: x >= y)
_le = _binop(lambda x, y: x <= y)
_eq = _binop(lambda x, y: x == y)
_ne = _binop(lambda x, y: x != y)
_and = _binop(lambda x, y: x & y)
_or = _binop(lambda x, y: x | y)


def _pos(a: Node) -> Node:
    return _node(a)


def _neg(a: Node) -> Node:
    return Node(-_node(a).df)


def _not(a: Node) -> Node:
    return Node(~_node(a).df.astype(bool))


# ══════════════════════════════════════════════════════════════════════════
# 2. 词法 + 语法分析：DSL → AST
#     不复用 Python ast —— 公式里 `AND` / `OR` 是**中缀**写法，
#     Python 会当成语法错误（`and` 才是关键字）。
# ══════════════════════════════════════════════════════════════════════════

TOKEN_RE = re.compile(
    r"""
      (?P<num>\d+\.?\d*(?:[eE][-+]?\d+)?)
    | (?P<str>"[^"]*")
    | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<op>>=|<=|==|!=|>|<|\+|\-|\*|/|\(|\)|,|\.)
    """,
    re.VERBOSE,
)


class ParseError(ValueError):
    pass


def tokenize(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = TOKEN_RE.match(expr, pos)
        if not m:
            raise ParseError(f"无法识别的字符 {expr[pos]!r} @{pos}")
        out.append((m.lastgroup, m.group()))
        pos = m.end()
    return out


class Parser:
    """递归下降。优先级（低→高）：OR < AND < NOT < 比较 < 加减 < 乘除 < 一元 < 后缀。"""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.toks = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        t = self.peek()
        if t is None:
            raise ParseError("表达式意外结束")
        self.i += 1
        return t

    def expect(self, kind: str, val: str | None = None) -> tuple[str, str]:
        t = self.take()
        if t[0] != kind or (val is not None and t[1] != val):
            raise ParseError(f"期望 {val or kind}，实为 {t[1]!r}")
        return t

    def parse(self):
        e = self.or_expr()
        if self.peek() is not None:
            raise ParseError(f"多余记号 {self.peek()[1]!r}")
        return e

    def or_expr(self):
        e = self.and_expr()
        while (t := self.peek()) and t[1] == "OR":
            self.take()
            e = ("or", e, self.and_expr())
        return e

    def and_expr(self):
        e = self.not_expr()
        while (t := self.peek()) and t[1] == "AND":
            self.take()
            e = ("and", e, self.not_expr())
        return e

    def not_expr(self):
        t = self.peek()
        if t and t[1] == "NOT":
            self.take()
            return ("not", self.not_expr())
        return self.cmp_expr()

    def cmp_expr(self):
        e = self.add_expr()
        t = self.peek()
        if t and t[1] in (">", "<", ">=", "<=", "==", "!="):
            op = self.take()[1]
            return ("cmp", op, e, self.add_expr())
        return e

    def add_expr(self):
        e = self.mul_expr()
        while (t := self.peek()) and t[1] in ("+", "-"):
            op = self.take()[1]
            e = ("bin", op, e, self.mul_expr())
        return e

    def mul_expr(self):
        e = self.unary_expr()
        while (t := self.peek()) and t[1] in ("*", "/"):
            op = self.take()[1]
            e = ("bin", op, e, self.unary_expr())
        return e

    def unary_expr(self):
        t = self.peek()
        if t and t[1] == "-":
            self.take()
            return ("neg", self.unary_expr())
        if t and t[1] == "+":
            self.take()
            return self.unary_expr()
        return self.postfix_expr()

    def postfix_expr(self):
        e = self.atom()
        while (t := self.peek()) and t[0] == "op" and t[1] == ".":
            self.take()
            attr = self.expect("name")[1]
            e = ("attr", e, attr)
        return e

    def atom(self):
        t = self.take()
        if t[0] == "num":
            return ("num", float(t[1]))
        if t[0] == "str":
            return ("str", t[1][1:-1])
        if t[0] == "name":
            if (n := self.peek()) and n[0] == "op" and n[1] == "(":
                self.take()
                args = []
                if (n2 := self.peek()) and n2[1] != ")":
                    args.append(self.or_expr())
                    while (c := self.peek()) and c[1] == ",":
                        self.take()
                        args.append(self.or_expr())
                self.expect("op", ")")
                return ("call", t[1], args)
            return ("name", t[1])
        if t[1] == "(":
            e = self.or_expr()
            self.expect("op", ")")
            return e
        raise ParseError(f"意外的记号 {t[1]!r}")


def parse(expr: str):
    return Parser(tokenize(expr)).parse()


# ══════════════════════════════════════════════════════════════════════════
# 3. 算子实现
#     能复用平台实现的**一律复用** alpha_library_factors（口径唯一），
#     平台没有的按 SPEC §3 补齐；日内族按 SPEC §3.4 补齐。
# ══════════════════════════════════════════════════════════════════════════


class Evaluator:
    def __init__(self, frames: dict[str, Node], ind_onehot: np.ndarray | None,
                 minute_ret: Node | None) -> None:
        self.frames = frames
        self.ind_onehot = ind_onehot
        self.minute_ret = minute_ret
        # IN() 降级告警的去重位：降级与否在构造期就定死、整轮恒定，故每条公式都喊
        # 只会淹日志（合成臂 2603 条公式实测刷 276 行），按原因保留首次即可 ——
        # 生产上真正要警惕的是「一条都没喊」，而那不是靠刷屏能解决的。
        self._ind_warned: set[str] = set()
        # 输入帧的逐日切分只建一次（见 `_split` 的说明）
        self._splits = {id(n.df): self._make_split(n.df)
                        for n in frames.values() if n.is_intraday}
        self.ops = self._build_ops()

    # ── 算子表 ────────────────────────────────────────────────────────
    def _build_ops(self) -> dict:
        _ensure_scripts_on_path()
        import alpha_library_factors as alf  # noqa: PLC0415

        def W(fn):
            """把 (DataFrame, ...) 形态的平台算子包成 Node→Node。

            整数值的浮点实参（窗口 / 阶数）统一转 int —— 公式里写作 `240`，
            解析后是 240.0，而 pandas 的 rolling/min_periods 只收 int。
            """
            def g(*args):
                vals = []
                for a in args:
                    if isinstance(a, Node):
                        vals.append(a.df)
                    elif isinstance(a, float) and a.is_integer():
                        vals.append(int(a))
                    else:
                        vals.append(a)
                out = fn(*vals)
                if isinstance(out, tuple):
                    return tuple(Node(o) for o in out)
                return Node(out)
            return g

        def sx(fn):
            """逐元素标量函数（只作用于数值，保留 NaN）。

            实参可能是**字面量**（`SQRT(2)`、`3-2*SQRT(2)` 里的 `SQRT(2)`），
            此时没有 `.df`；按参考栅格广播成标量帧再往后走。
            """
            def g(a):
                if not isinstance(a, Node):
                    return _node(fn(float(a)))
                return Node(fn(a.df))
            return g

        ops = {
            # ── 平台既有实现（口径以此为准）──────────────────────
            "MEAN": W(alf.MEAN), "SUM": W(alf.SUM), "STD": W(alf.STD),
            "PROD": W(alf.PROD), "MAX": W(alf.MAX), "MIN": W(alf.MIN),
            "TSRANK": W(alf.TSRANK), "TSARGMAX": W(alf.TSARGMAX),
            "TSARGMIN": W(alf.TSARGMIN), "HIGHDAY": W(alf.HIGHDAY),
            "LOWDAY": W(alf.LOWDAY), "DECAY": W(alf.DECAY), "QTL": W(alf.QTL),
            "CORR": W(alf.CORR), "COV": W(alf.COV), "REG": W(alf.REG),
            "EWMA": W(alf.EWMA), "EMA_SPAN": W(alf.EMA_SPAN),
            "R": W(alf.R), "SCALE": W(alf.SCALE), "RANK_TS": W(alf.RTS),
            # ── SPEC §3.1/3.2 平台未单独实现的 ───────────────────
            "Z": lambda a: Node((a.df - a.df.mean(axis=1).values[:, None])
                                / a.df.std(axis=1).values[:, None]),
            "WINSOR_Z": self._winsor_z,
            "DELTA": lambda a, n: Node(a.df - a.df.shift(_int(n))),
            "REF": lambda a, n: Node(a.df.shift(_int(n))),
            "IN": self._ind_neutral,
            # ── SPEC §3.3 逐元素 ─────────────────────────────────
            "ABS": sx(np.abs), "SIGN": sx(np.sign),
            "LOG": sx(np.log), "SQRT": sx(np.sqrt),
            # 幂的两侧都可能是表达式（WQ101 里 `A ** B` 的 B 就是一条因子式），
            # 故不能 `float(p)`，走 _binop 按栅格对齐逐元素算。
            "POWER": _binop(np.power),
            "MAX2": _binop(np.maximum), "MIN2": _binop(np.minimum),
            "IF": self._if,
            "AND": _and, "OR": _or, "NOT": _not,
            # ── SPEC §3.4 日内族 ─────────────────────────────────
            "INTRADAY_SUM": self._intra("sum"),
            "INTRADAY_MEAN": self._intra("mean"),
            "INTRADAY_STD": self._intra("std"),
            "INTRADAY_MAX": self._intra("max"),
            "INTRADAY_MIN": self._intra("min"),
            "INTRADAY_SKEW": self._intra("skew"),
            "INTRADAY_KURT": self._intra("kurt"),
            "INTRADAY_ENT": self._intra_ent,
            "INTRADAY_CORR": self._intra_corr,
            "INTRADAY_SLOPE": self._intra_slope,
            "INTRADAY_RANK": self._intra_rank,
            "SEG": self._seg,
            "REALIZED_VOL": self._realized_vol,
        }
        return ops

    # ── 条件选择 ──────────────────────────────────────────────────────
    @staticmethod
    def _if(c, a, b) -> Node:
        """两个分支都先贴到**条件**的栅格上 —— 分支可能是标量（如 `IF(x>y, 1, 0)`
        里的 1/0），若默认广播到日频栅格、而条件是分钟栅格，`.where` 会因索引
        层级不匹配直接报错（实测踩到）。"""
        cond = _bool(c)
        return Node(_bcast(a, cond.index).where(cond, _bcast(b, cond.index)))

    # ── 截面算子补充 ──────────────────────────────────────────────────
    def _winsor_z(self, a: Node, p) -> Node:
        df = a.df
        lo = df.quantile(float(p), axis=1)
        hi = df.quantile(1 - float(p), axis=1)
        clip = df.clip(lower=lo, upper=hi, axis=0)
        return Node((clip - clip.mean(axis=1).values[:, None])
                    / clip.std(axis=1).values[:, None])

    def _ind_neutral(self, a: Node) -> Node:
        """行业中性化：委托平台 ``alf.IN``（列序必须与 ``ind_onehot`` 行序一致）。

        ⚠️ 两处降级都会**静默**返回原值 —— 在测试臂上是合理的（合成数据没有行业），
        在生产上却意味着「中性化没生效但因子照样算出来」。故一律打 warning：
        拿它算 IC 的时候，一条没告警的日志就是「这些 IN() 因子确实中性化过」的凭据。
        告警按原因去重（见 ``_warn_once``）：降级在构造期就定死，喊一次足够。
        """
        import alpha_library_factors as alf  # noqa: PLC0415
        oh = self.ind_onehot
        if oh is None:
            self._warn_once("no_onehot", "IN(): 未提供 ind_onehot，行业中性化退化为原值直通")
            return Node(a.df)
        if len(oh) != a.df.shape[1]:  # 行业映射与当前列不匹配时跳过中性化
            self._warn_once(
                f"shape:{len(oh)}x{a.df.shape[1]}",
                f"IN(): ind_onehot 行数 {len(oh)} ≠ 因子列数 {a.df.shape[1]}，"
                "行业中性化退化为原值直通",
            )
            return Node(a.df)
        return Node(alf.IN(a.df, oh))

    def _warn_once(self, key: str, msg: str) -> None:
        """同一原因的 IN() 降级只告警一次。

        去重是 **per-Evaluator 实例**的：降级与否在构造期就定死，单个实例内恒定，
        但一轮测试可能同时存在多个 Evaluator（截断不变性的 ``ev_full`` / ``ev_cut``），
        故一轮下来的告警条数 = **实例数 × 原因数**，不是恒为 1（实测：2603 条公式
        的体检跑 2 个实例 → 2 条，取代原来的 276 条）。
        """
        if key not in self._ind_warned:
            self._ind_warned.add(key)
            logger.warning(msg)

    # ── 日内算子 ──────────────────────────────────────────────────────
    # 逐日分组但走 **numpy 整数位置切片**，不用 groupby.apply，也不用逐日 .loc：
    #   · groupby.apply 在返回 DataFrame 时会做「归约还是映射」的推断，列数 >1 抛 ambiguous
    #   · 逐日 .loc[[k]] 是 O(每日一次索引查找)，2603 条 × 2 遍实测要跑十几分钟
    #
    # ⚠️ 只缓存**输入帧**（`self._splits`，__init__ 时建一次）。曾经按 `id()` 缓存
    # 所有见过的帧，结果每条公式的中间结果都被永久留一份 `to_numpy()` 副本，
    # 900 条日内公式 × 2 臂直接把内存吃到 OOM（实测 24.7GB 被内核杀掉）。
    # 输入帧才是真正被反复复用的（每条分钟公式都要切它），中间帧基本一次性。

    def _make_split(self, df: pd.DataFrame) -> tuple:
        lvl0 = df.index.get_level_values(0)
        # index 按日有序（构造时即如此），故 return_index 给出的 starts 可直接切分
        uniq, starts, counts = np.unique(lvl0.to_numpy(), return_index=True,
                                         return_counts=True)
        return (df, uniq, starts, counts, df.to_numpy(dtype=float),
                np.asarray(df.columns))

    def _split(self, df: pd.DataFrame) -> tuple:
        hit = self._splits.get(id(df))
        if hit is not None and hit[0] is df:
            return hit
        return self._make_split(df)  # 中间帧不缓存，算完即弃

    def _by_day(self, df: pd.DataFrame, fn) -> pd.DataFrame:
        """对每个交易日切出分钟子阵 (m, n_sym)，套 fn 得到「每日一横截面」。"""
        _, uniq, starts, counts, vals, cols = self._split(df)
        if len(vals) == 0:  # 空帧（如 SEG 选出的时段一根 bar 都没有）
            raise ParseError("日内序列为空：SEG 时段与分钟时点无交集")
        rows = [fn(vals[s:s + c]) for s, c in zip(starts, counts, strict=True)]
        return pd.DataFrame(np.vstack(rows), index=pd.DatetimeIndex(uniq), columns=cols)

    def _intra(self, how: str):
        red = {
            "sum": np.nansum, "mean": np.nanmean, "max": np.nanmax, "min": np.nanmin,
            # ddof=1 与平台 STD 口径一致（alf.STD 用 rolling.std 默认样本标准差）
            "std": lambda m, axis=0: np.nanstd(m, axis=axis, ddof=1),
            "skew": lambda m, axis=0: pd.DataFrame(m).skew().to_numpy(),
            "kurt": lambda m, axis=0: pd.DataFrame(m).kurt().to_numpy(),
        }[how]

        def g(a: Node) -> Node:
            if not a.is_intraday:
                raise ParseError("INTRADAY_* 的入参不是分钟序列")
            return Node(self._by_day(a.df, lambda m: red(m, axis=0)))
        return g

    def _intra_ent(self, a: Node, bins) -> Node:
        b = int(bins)

        def ent(m: np.ndarray) -> np.ndarray:
            out = np.full(m.shape[1], np.nan)
            for j in range(m.shape[1]):
                v = m[:, j]
                v = v[np.isfinite(v)]
                if v.size == 0:
                    continue
                lo, hi = float(v.min()), float(v.max())
                # 取值退化（整天一个常数）⇒ 分布无信息，熵按 0 记。
                # 直接把常数喂给 np.histogram 会抛
                # 「Too many bins for data range」（numpy 2 起是硬错误）。
                if not (hi - lo > 1e-12 * max(1.0, abs(lo), abs(hi))):
                    out[j] = 0.0
                    continue
                h, _ = np.histogram(v, bins=b, range=(lo, hi))
                p = h[h > 0] / h.sum()
                out[j] = -(p * np.log(p)).sum()
            return out

        return Node(self._by_day(a.df, ent))

    def _intra_corr(self, x: Node, y: Node) -> Node:
        X, Y = _align(x, y)
        sx, sy = self._split(X), self._split(Y)

        def corr(mx: np.ndarray, my: np.ndarray) -> np.ndarray:
            mx = mx - np.nanmean(mx, axis=0)
            my = my - np.nanmean(my, axis=0)
            num = np.nansum(mx * my, axis=0)
            den = np.sqrt(np.nansum(mx * mx, axis=0) * np.nansum(my * my, axis=0))
            with np.errstate(invalid="ignore", divide="ignore"):
                return num / den

        _, uniq, starts, counts, vx, cols = sx
        vy = sy[4]
        return Node(pd.DataFrame(
            np.vstack([corr(vx[s:s + c], vy[s:s + c])
                       for s, c in zip(starts, counts, strict=True)]),
            index=pd.DatetimeIndex(uniq), columns=cols))

    def _intra_slope(self, a: Node) -> Node:
        def slope(m: np.ndarray) -> np.ndarray:
            t = np.arange(len(m), dtype=float)
            t -= t.mean()
            return np.nansum(m * t[:, None], axis=0) / (t * t).sum()

        return Node(self._by_day(a.df, slope))

    def _intra_rank(self, a: Node) -> Node:
        """末值在当日分钟序列中的分位。"""
        def last_pct(m: np.ndarray) -> np.ndarray:
            last = m[-1]
            less = (m < last).sum(axis=0)
            eq = (m == last).sum(axis=0)
            return (less + (eq + 1.0) / 2.0) / len(m)
        return Node(self._by_day(a.df, last_pct))

    def _seg(self, a: Node, rng) -> Node:
        """取日内时段（闭开区间 `"HH:MM-HH:MM"`）。"""
        lo, hi = str(rng).split("-")
        hm = _hhmm(a.df.index.get_level_values(1))
        return Node(a.df[(hm >= lo) & (hm < hi)])

    def _realized_vol(self, w) -> Node:
        if self.minute_ret is None:
            raise ParseError("REALIZED_VOL 需要分钟收益序列")
        sq = self.minute_ret.df ** 2
        daily = sq.groupby(level=0).sum()
        return Node(np.sqrt(daily).rolling(int(w), min_periods=int(w)).sum())

    # ── AST 求值 ──────────────────────────────────────────────────────
    def eval(self, node):
        # ⚠️ `_REF` 是标量广播用的栅格，模块级变量。**必须每次求值前重新绑定**：
        # 本测试同时存在「全量」「截断」两个 Evaluator，若只在 __init__ 里赋值，
        # 后构造的那个会永久性地把前一个的标量栅格改掉 —— 实测后果是
        # 全量臂的 `x > 0` 拿截断臂的日历来对齐，报
        # `Can only compare identically-labeled ...`，
        # 或悄悄算出 14 条假「违反截断不变性」。绑定是常数开销。
        global _REF
        _REF = self.frames["__ref__"]
        kind = node[0]
        if kind == "num":
            return node[1]  # 标量直通：窗口/阶数/分位等实参必须是裸数值
        if kind == "str":
            return node[1]
        if kind == "name":
            nm = node[1]
            if nm == "nan":
                return float("nan")
            if nm == "inf":
                return float("inf")
            if nm not in self.frames:
                raise ParseError(f"缺变量 frame: {nm}")
            return self.frames[nm]
        if kind == "neg":
            return _neg(self.eval(node[1]))
        if kind == "not":
            return _not(self.eval(node[1]))
        if kind == "bin":
            op = node[1]
            a, b = self.eval(node[2]), self.eval(node[3])
            return {"+": _add, "-": _sub, "*": _mul, "/": _div}[op](a, b)
        if kind == "cmp":
            op = node[1]
            a, b = self.eval(node[2]), self.eval(node[3])
            return {">": _gt, "<": _lt, ">=": _ge, "<=": _le,
                    "==": _eq, "!=": _ne}[op](a, b)
        if kind == "and":
            return _and(self.eval(node[1]), self.eval(node[2]))
        if kind == "or":
            return _or(self.eval(node[1]), self.eval(node[2]))
        if kind == "attr":
            base = self.eval(node[1])
            attr = node[2]
            if not isinstance(base, tuple):
                raise ParseError(f".{attr} 作用在非 REG 结果上")
            return base[{"beta": 0, "r2": 1, "resid": 2}[attr]]
        if kind == "call":
            name = node[1]
            if name not in self.ops:
                raise ParseError(f"未实现算子: {name}")
            args = [self.eval(a) for a in node[2]]
            return self.ops[name](*args)
        raise ParseError(f"未知 AST 节点 {kind}")

    # ── 标量广播所用的栅格 ────────────────────────────────────────────
    def _ref(self) -> Node:
        return self.frames["__ref__"]

    def _ref_index(self):
        return self._ref().df.index

    def _ref_cols(self):
        return self._ref().df.columns


_HHMM_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _hhmm(vals) -> np.ndarray:
    """分钟层级 → 规整的 `'HH:MM'` 字符串数组（可直接做字典序比较）。

    pandas 3 已移除 `Index.strftime`，且层级可能是 `datetime.time`、
    `'09:30'`、`'2020-01-02 09:30:00'` 多种形态，统一用正则抠出 HH:MM。
    """
    if isinstance(vals, pd.DatetimeIndex):
        return pd.Series(vals).dt.strftime("%H:%M").to_numpy()
    out = []
    for s in np.asarray(vals).astype(str):
        m = _HHMM_RE.search(s)
        out.append(f"{int(m.group(1)):02d}:{m.group(2)}" if m else s)
    return np.asarray(out, dtype=object)


def _bool(n) -> pd.DataFrame:
    d = n.df if isinstance(n, Node) else n
    return d.astype(bool)


def _int(n) -> int:
    """窗口 / 滞后阶数：接受 240 或 240.0，拒绝负数由 _validate 静态守卫先拦。"""
    return int(n.df.iloc[0, 0]) if isinstance(n, Node) else int(n)
