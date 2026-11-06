"""FM-DSL 求值器：宽面（index=交易日升序, columns=标的）pandas 实现。

语义口径（本仓唯一实现，测试钉死）：

- 单序列滚动算子 ``min_periods=1``（与候选库/qlib 口径一致）；``Std`` ddof=1；
- **满窗类**（min_periods=n）：``WMA``、``Corr``、``Cov``、``Slope``、``Resi``、
  ``Rsquare``——统计量在小窗上退化（2 点回归残差恒 0、R² 恒 1），会给暖机段
  伪造信号；
- ``EMA`` α=2/(n+1) adjust=False；``WMA`` 权重 1..n（最新最大）；
- ``TsRank`` = 窗内末值平均秩 ÷ 有效根数（pct）；``TsArgMax`` = 距窗内最大值
  的根数（0=当根，并列取最早一次出现）；``CsRank`` = 当日截面 pct 秩；
- ``IfElse`` 条件 >0 为真（NaN→假）；比较/逻辑算子输出 1.0/0.0；
  ``Log`` 自然对数（≤0→NaN）；``SignedPower`` = sign(x)·|x|^p；
- 顶层出口 ±inf→NaN（inf 若留在截面里会排到第一名）。

标量（Num 子树）与宽面可自由混合；引用字段必须是同形宽面，否则 ValueError。
"""

from __future__ import annotations

import warnings
from typing import Any
from collections.abc import Callable, Mapping

import numpy as np
import pandas as pd

from .dsl import OPERATORS, Call, Feature, Node, Num, window_arg_index


def evaluate(node: Node, features: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """对 AST 求值，返回与输入字段同形的宽面。"""
    with np.errstate(all="ignore"):  # 除零/全 NaN 窗走 numpy 语义（inf/nan），不刷警告
        result = _Evaluator(features).eval(node)
    if isinstance(result, pd.DataFrame):
        return result.replace([np.inf, -np.inf], np.nan)
    # 纯标量表达式（不引用任何字段）——拿着它算不了截面因子
    value = float(np.asarray(result, dtype=float))
    if np.isinf(value):
        value = np.nan
    raise ValueError(f"表达式未产出宽面（结果 {value!r}）——因子必须引用至少一个字段")


def missing_implementations() -> list[str]:
    """注册表里没有求值实现的算子（导入自检 / 测试用）。"""
    return sorted(set(OPERATORS) - set(_HANDLERS))


# --------------------------------------------------------------------------
# 求值辅助
# --------------------------------------------------------------------------

_FrameOrNum = pd.DataFrame | np.float64


def _num(value: Any) -> _FrameOrNum:
    """标量统一成 np.float64（numpy 语义：除零得 inf 不抛异常）。"""
    if isinstance(value, pd.DataFrame):
        return value
    return np.float64(value)


def _to_float(value: Any) -> _FrameOrNum:
    if isinstance(value, pd.DataFrame):
        return value.astype(float)
    return np.float64(float(value))


def _log(value: _FrameOrNum) -> _FrameOrNum:
    if isinstance(value, pd.DataFrame):
        return np.log(value.where(value > 0))
    return np.log(value) if value > 0 else np.float64(np.nan)


def _rolling_sum(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    return frame.rolling(n, min_periods=n).sum()


def _pos_series(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(np.arange(len(frame), dtype=float), index=frame.index)


def _ols_parts(frame: pd.DataFrame, n: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (A, T)：A=Σx；T=Σ t_local·x（t_local=0..n−1，末根 = t_local n−1）。

    T = Σ r·x − (t−n+1)·Σx（r=全局面行号，t=输出行号）——一次 rolling-sum
    技巧覆盖全部窗口，避免逐行 polyfit。"""
    pos = _pos_series(frame)
    a = _rolling_sum(frame, n)
    c = frame.mul(pos, axis=0).rolling(n, min_periods=n).sum()
    offset = pos - (n - 1)  # = t − n + 1
    return a, c.sub(a.mul(offset, axis=0))


def _slope(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    a, t_lin = _ols_parts(frame, n)
    i_bar = (n - 1) / 2
    s_ii = n * (n * n - 1) / 12
    return (t_lin - i_bar * a) / s_ii


def _resi(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    a, t_lin = _ols_parts(frame, n)
    i_bar = (n - 1) / 2
    s_ii = n * (n * n - 1) / 12
    slope = (t_lin - i_bar * a) / s_ii
    y_hat = a / n + slope * i_bar  # 回归线在末根的拟合值
    return frame - y_hat


def _rsquare(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    a, t_lin = _ols_parts(frame, n)
    i_bar = (n - 1) / 2
    s_ii = n * (n * n - 1) / 12
    s_xx = (frame * frame).rolling(n, min_periods=n).sum() - a * a / n
    num = (t_lin - i_bar * a) ** 2
    den = s_ii * s_xx
    with np.errstate(all="ignore"):
        value = (num / den).clip(0.0, 1.0)
    return value


def _wma(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    a, t_lin = _ols_parts(frame, n)
    # 权重 1..n（最新最大）：Σ(t_local+1)·x = T + A
    return (t_lin + a) / (n * (n + 1) / 2)


def _ts_argmax(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    """距窗内最大值的根数（0=当根；并列取最早一次出现）。顶部补 n−1 行 NaN
    使滑动窗天然退化为不完整窗（min_periods=1 口径）。"""
    arr = np.asarray(frame.to_numpy(dtype=float), dtype=float)
    pad = np.full((n - 1, arr.shape[1]), np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(np.vstack([pad, arr]), n, axis=0)
    with np.errstate(all="ignore"):
        # nanmax 对全 NaN 窗走 warnings 模块（np.errstate 管不住），暖机段每个
        # 全 NaN 格都会刷 RuntimeWarning——run 一次几万条，显式静音
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            maxes = np.nanmax(windows, axis=-1)
        first = (windows == maxes[:, :, None]).argmax(axis=-1).astype(float)
    bars = (n - 1) - first
    bars[np.isnan(maxes)] = np.nan  # 全 NaN 窗
    return pd.DataFrame(bars, index=frame.index, columns=frame.columns)


def _ew(fn: Callable[..., Any]) -> Callable[[_Evaluator, Call], Any]:
    def handler(ev: _Evaluator, node: Call) -> Any:
        a = _num(ev.eval(node.args[0]))
        b = _num(ev.eval(node.args[1]))
        return fn(a, b)

    return handler


def _unary(
    fn: Callable[[_FrameOrNum], _FrameOrNum],
) -> Callable[[_Evaluator, Call], Any]:
    def handler(ev: _Evaluator, node: Call) -> Any:
        return fn(_num(ev.eval(node.args[0])))

    return handler


def _series_window(fn: Callable[[pd.DataFrame, int], pd.DataFrame]):
    def handler(ev: _Evaluator, node: Call) -> pd.DataFrame:
        frame = ev.require_frame(node.args[0], node.op)
        return fn(frame, ev.window(node, node.op))

    return handler


def _pair_window(fn: Callable[[pd.DataFrame, pd.DataFrame, int], pd.DataFrame]):
    def handler(ev: _Evaluator, node: Call) -> pd.DataFrame:
        x = ev.require_frame(node.args[0], node.op)
        y = ev.require_frame(node.args[1], node.op)
        return fn(x, y, ev.window(node, node.op))

    return handler


def _ifelse(ev: _Evaluator, node: Call) -> Any:
    cond = ev.eval(node.args[0])
    a = ev.eval(node.args[1])
    b = ev.eval(node.args[2])
    if not any(isinstance(v, pd.DataFrame) for v in (cond, a, b)):
        return np.where(_num(cond) > 0, _num(a), _num(b))
    reference = next(v for v in (cond, a, b) if isinstance(v, pd.DataFrame))

    def broadcast(value: Any) -> Any:
        if isinstance(value, pd.DataFrame):
            return value
        blank = np.full(reference.shape, float(value))
        return pd.DataFrame(blank, index=reference.index, columns=reference.columns)

    truth = broadcast(cond) > 0 if isinstance(cond, pd.DataFrame) else _num(cond) > 0
    return broadcast(a).where(truth, broadcast(b))


_HANDLERS: dict[str, Callable[[_Evaluator, Call], Any]] = {
    "Abs": _unary(np.abs),
    "Sign": _unary(np.sign),
    "Neg": _unary(np.negative),
    "Log": _unary(_log),
    "CsRank": lambda ev, node: ev.require_frame(node.args[0], node.op).rank(
        axis=1, pct=True
    ),
    "Add": _ew(lambda a, b: a + b),
    "Sub": _ew(lambda a, b: a - b),
    "Mul": _ew(lambda a, b: a * b),
    "Div": _ew(lambda a, b: a / b),
    "Eq": _ew(lambda a, b: _to_float(a == b)),
    "Greater": _ew(lambda a, b: _to_float(a > b)),
    "Less": _ew(lambda a, b: _to_float(a < b)),
    "Max2": _ew(np.maximum),
    "Min2": _ew(np.minimum),
    "And": _ew(lambda a, b: _to_float((a > 0) & (b > 0))),
    "Or": _ew(lambda a, b: _to_float((a > 0) | (b > 0))),
    "SignedPower": _ew(lambda a, b: np.sign(a) * np.abs(a) ** b),
    "Delay": _series_window(lambda x, n: x.shift(n)),
    "Delta": _series_window(lambda x, n: x - x.shift(n)),
    "EMA": _series_window(lambda x, n: x.ewm(alpha=2 / (n + 1), adjust=False).mean()),
    "Mean": _series_window(lambda x, n: x.rolling(n, min_periods=1).mean()),
    "SMA": _series_window(lambda x, n: x.rolling(n, min_periods=1).mean()),
    "Med": _series_window(lambda x, n: x.rolling(n, min_periods=1).median()),
    "Std": _series_window(lambda x, n: x.rolling(n, min_periods=1).std(ddof=1)),
    "Skew": _series_window(lambda x, n: x.rolling(n, min_periods=1).skew()),
    "Kurt": _series_window(lambda x, n: x.rolling(n, min_periods=1).kurt()),
    "Min": _series_window(lambda x, n: x.rolling(n, min_periods=1).min()),
    "TsMin": _series_window(lambda x, n: x.rolling(n, min_periods=1).min()),
    "Max": _series_window(lambda x, n: x.rolling(n, min_periods=1).max()),
    "TsMax": _series_window(lambda x, n: x.rolling(n, min_periods=1).max()),
    "TsRank": _series_window(lambda x, n: x.rolling(n, min_periods=1).rank(pct=True)),
    "TsArgMax": _series_window(_ts_argmax),
    "WMA": _series_window(_wma),
    "Slope": _series_window(_slope),
    "Resi": _series_window(_resi),
    "Rsquare": _series_window(_rsquare),
    "Corr": _pair_window(
        lambda x, y, n: x.rolling(n, min_periods=n).corr(y),
    ),
    "Cov": _pair_window(
        lambda x, y, n: x.rolling(n, min_periods=n).cov(y),
    ),
    "IfElse": _ifelse,
}


class _Evaluator:
    def __init__(self, features: Mapping[str, pd.DataFrame]):
        self._features = features
        self._shape: tuple[int, int] | None = None

    def eval(self, node: Node) -> Any:
        if isinstance(node, Num):
            return np.float64(node.value)
        if isinstance(node, Feature):
            return self.frame(node.name)
        handler = _HANDLERS.get(node.op)
        if handler is None:  # 解析器注册了但求值器没实现——导入自检会先炸
            raise ValueError(f"算子 {node.op} 没有求值实现")
        return handler(self, node)

    def frame(self, name: str) -> pd.DataFrame:
        if name not in self._features:
            raise KeyError(
                f"缺少字段 {name}（求值器收到：{', '.join(sorted(self._features))}）"
            )
        frame = self._features[name]
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(f"字段 {name} 必须是宽面 DataFrame")
        if self._shape is None:
            self._shape = frame.shape
        elif frame.shape != self._shape:
            raise ValueError(
                f"字段 {name} 形状 {frame.shape} 与既有 {self._shape} 不同形"
                "——所有字段必须是同形宽面"
            )
        return frame

    def require_frame(self, node: Node, op: str) -> pd.DataFrame:
        value = self.eval(node)
        if not isinstance(value, pd.DataFrame):
            raise ValueError(f"算子 {op} 需要宽面（DataFrame）序列输入，收到标量")
        return value

    def window(self, node: Call, op: str) -> int:
        index = window_arg_index(OPERATORS[op])
        assert index is not None
        literal = node.args[index]
        assert isinstance(literal, Num)
        return int(literal.value)
