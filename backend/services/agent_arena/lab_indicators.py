"""回测时捕获策略真正算过的指标序列，供 K 线图叠加/副图显示。

为什么不静态解析源码：指标参数常来自 ``input()``、可能被界面覆盖、也可能算在 hlc3 上，
静态解析只能猜。这里在**运行时**给 ``pynecore.lib.ta`` 打桩，按「调用点」记录每根 bar
的返回值——两条链路都适用：

- 内置模板：API 进程内同步回测，直接捕获；
- Pine 库策略：宿主沙箱回测时捕获，结果落 ``report.json``（容器绝不执行模型代码）。

只留指标类调用；信号/工具类（crossover / barssince / change …）跳过——成交点已经有
markers，再画一堆布尔序列只是噪音。

放主图还是副图**不看函数名，看输入是谁**：``ta.highest(rsi, 14)`` 名字在叠加表里，值域
却是 RSI 的 0-100（中位数 ~50，恰好落在价格的 ±4 倍护栏内），按名字会被画到价格图上。
pynecore 传给 ``ta.*`` 的是当前 bar 的浮点数（不是 Series 对象），所以认亲只能比数值：
哪个调用点的输出和本调用的第一个实参逐位相同，就跟它同格（链式一路走到根）。
副图内部再按**量级**拆：同格两条线纵轴范围差超过 4 倍时，小的那条会被压成直线。

捕获必须是**行为透明**的：打桩只是包一层记录后原样返回，实测同一策略打桩前后
成交笔数与净收益逐位相同（见 tests/test_lab_indicators.py）。
"""
from __future__ import annotations

import functools
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# 报告体积闸：一条序列 ≈ 每根 K 线一个数（2596 根 ≈ 23KB），留太多会把 report.json 撑爆。
# 实测 183 份报告：多数只有 1-4 条，p95 = 16，最长的 32 条；旧上限 8/6 曾把 10 份叠加线、
# 20 份副图线悄悄截掉（0975 的 RSI 区间高低点就这么消失过）。24/24 对现有报告零截断，
# 最坏情况也只是 ~1.1MB 指标数据。
MAX_OVERLAYS = 24
MAX_PANES = 24
MAX_DECIMALS = 4

# 同一格副图里的线量级最多差这么多倍，否则小的那条会被压成贴着 0 的直线。
PANE_SPLIT_RATIO = 4.0

# 画在价格主图上（与价格同量纲）
OVERLAYS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ema": ("EMA", ("line",)),
    "sma": ("MA", ("line",)),
    "wma": ("WMA", ("line",)),
    "rma": ("RMA", ("line",)),
    "hma": ("HMA", ("line",)),
    "swma": ("SWMA", ("line",)),
    "alma": ("ALMA", ("line",)),
    "vwap": ("VWAP", ("line", "line", "line")),
    "vwma": ("VWMA", ("line",)),
    "bb": ("布林带", ("line", "line", "line")),
    "kc": ("肯特纳通道", ("line", "line", "line")),
    "sar": ("SAR", ("line",)),
    "supertrend": ("SuperTrend", ("line",)),
    "highest": ("区间高点", ("line",)),
    "lowest": ("区间低点", ("line",)),
    "linreg": ("线性回归", ("line",)),
}

# 独立副图（与价格不同量纲）
PANES: dict[str, tuple[str, tuple[str, ...]]] = {
    "macd": ("MACD", ("line", "line", "hist")),
    "rsi": ("RSI", ("line",)),
    "stoch": ("KDJ/Stoch", ("line",)),
    "dmi": ("DMI", ("line", "line", "line")),
    "atr": ("ATR", ("line",)),
    "cci": ("CCI", ("line",)),
    "mfi": ("MFI", ("line",)),
    "wpr": ("Williams %R", ("line",)),
    "obv": ("OBV", ("line",)),
    "roc": ("ROC", ("line",)),
    "mom": ("动量", ("line",)),
    "cmo": ("CMO", ("line",)),
    "tsi": ("TSI", ("line",)),
    "bbw": ("布林带宽", ("line",)),
    "kcw": ("肯特纳宽", ("line",)),
    "stdev": ("标准差", ("line",)),
    "dev": ("平均偏离", ("line",)),
    "variance": ("方差", ("line",)),
    "percentrank": ("百分位", ("line",)),
    "accdist": ("A/D", ("line",)),
    "pvt": ("PVT", ("line",)),
    "wad": ("WAD", ("line",)),
    "nvi": ("NVI", ("line",)),
    "pvi": ("PVI", ("line",)),
    "wvad": ("WVAD", ("line",)),
    "correlation": ("相关性", ("line",)),
}

# 副图默认排序（越小越先显示）；没列到的排在后面
PANE_ORDER = ("macd", "rsi", "stoch", "dmi", "atr", "cci", "mfi", "wpr", "obv")

# 叠加线必须与价格同量纲：成交量的均线（3e7）画进主图会把价格压成一条直线。
# 量级对不上（中位数偏离价格中位数超过这个倍数）的，降级到副图。
PRICE_RATIO_LO = 0.25
PRICE_RATIO_HI = 4.0

# 多返回值各列的名字（只列常见几个，其余用「名1/名2」）
SUB_LABELS: dict[str, tuple[str, ...]] = {
    "macd": ("DIF", "DEA", "柱"),
    "bb": ("中轨", "上轨", "下轨"),
    "kc": ("中轨", "上轨", "下轨"),
    "vwap": ("VWAP", "上轨", "下轨"),
    "dmi": ("DI+", "DI-", "ADX"),
}

def _num(v: Any) -> float | int | None:
    """NA / nan / bool → None；数值四舍五入到 MAX_DECIMALS 位。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return round(f, MAX_DECIMALS)
    return None


# ---------- 打桩 ----------


@contextmanager
def capture(script_path: Path) -> Iterator[dict]:
    """跑脚本期间记录 ``ta.*`` 的返回值。

    产物结构：``{key: {"name", "line", "by_bar": {bar: [(实参, 返回值)]}}}``。
    实参用于给序列起名（``EMA(50)``）——源码里读不到的值（函数参数、被覆盖的
    input）这里拿到的是**实际值**。同一行调多次（如三个周期的 ``ta.highest``）
    按 bar 内的调用次序区分。
    """
    import pynecore.lib.ta as ta
    from pynecore.core.script_runner import ScriptRunner

    want = str(Path(script_path).resolve())
    state: dict = {"bar": 0, "hits": {}}
    saved: list[tuple[Any, str, Any]] = []

    def record(name: str, args: tuple, value: Any) -> None:
        fr = sys._getframe(2)          # record ← 包装层 ← 脚本
        fn = fr.f_code.co_filename
        if fn != want:
            try:
                if str(Path(fn).resolve()) != want:
                    return
            except OSError:
                return
        key = f"{name}@{fr.f_lineno}@{fr.f_lasti}"
        hit = state["hits"].setdefault(
            key, {"name": name, "line": fr.f_lineno, "by_bar": {}})
        hit["by_bar"].setdefault(state["bar"], []).append((args, value))

    def wrap(name: str, fn: Any) -> Any:
        # @overload 分派器要走 __pyne_bind__ 这条路，否则会绕过锚点、让调用点共享
        # 状态（三个周期的 highest 会互相污染）。绑定后的可调用对象再包一层记录。
        if hasattr(fn, "__pyne_bind__"):
            orig_bind = fn.__pyne_bind__

            def bind(pin=None):
                bound = orig_bind(pin)

                def rec(*a, **kw):
                    r = bound(*a, **kw)
                    record(name, a, r)
                    return r
                try:
                    return functools.wraps(bound)(rec)
                except (AttributeError, TypeError):
                    return rec
            wrapper = functools.wraps(fn)(lambda *a, **kw: fn(*a, **kw))
            wrapper.__pyne_bind__ = bind
            return wrapper

        @functools.wraps(fn)
        def inner(*a, **kw):
            r = fn(*a, **kw)
            record(name, a, r)
            return r
        return inner

    for name in dir(ta):
        if name.startswith("_"):
            continue
        fn = getattr(ta, name)
        if not callable(fn) or getattr(fn, "__module__", "") != ta.__name__:
            continue
        if name not in OVERLAYS and name not in PANES:
            continue                   # 信号/工具类不打桩，省开销
        saved.append((ta, name, fn))
        setattr(ta, name, wrap(name, fn))

    orig_run_iter = ScriptRunner.run_iter

    def run_iter(self):
        it = orig_run_iter(self)
        state["bar"] = 0
        while True:
            try:
                r = next(it)
            except StopIteration:
                return
            yield r
            state["bar"] += 1

    ScriptRunner.run_iter = run_iter
    try:
        yield state["hits"]
    finally:
        ScriptRunner.run_iter = orig_run_iter
        for obj, name, fn in saved:
            setattr(obj, name, fn)


# ---------- 组装 ----------


def _stable_args(calls: list[tuple]) -> str:
    """捕获到的实参里挑「每根 bar 都一样且是整数」的 → 标签后缀 ``(12,26,9)``。

    价格、成交量这些每根都变，自然被排除；周期/倍数/开关则稳定可读。
    比解析源码可靠：源码里的 ``length`` 可能来自函数参数或被界面覆盖的 input。
    """
    if not calls:
        return ""
    n = min(len(a) for a, _ in calls)
    nums: list[str] = []
    for i in range(n):
        vals = [a[i] for a, _ in calls]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in vals):
            continue
        if any(float(v) != float(vals[0]) for v in vals):
            continue
        if float(vals[0]) != int(float(vals[0])):
            continue
        nums.append(str(int(float(vals[0]))))
    return f"({','.join(nums)})" if nums else ""


LINEAGE_MIN_BARS = 10   # 至少这么多根 bar 逐位相同，才认「这根线是它算出来的」


def _source_value(args: tuple) -> float | None:
    """本调用的「源」当前值：第一个数值实参。

    pynecore 会给一部分函数在实参最前面塞一个状态对象（list，如 ema/rsi/sma/macd
    的 ``[state, src, length]``），真正的源值在它后面；``lowest/highest`` 则是
    直接的 ``[src, length]``。0975 实录：``ta.sma(stoch_rsi_k, 3)`` 的源值被状态对象
    挡住，认不出上游 → StochRSI 的 0-100 线画到了价格主图上。
    """
    for v in args:
        if isinstance(v, list):
            continue                   # 注入的状态对象
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            f = float(v)
            return f if f == f else None   # na 不当来源
        return None
    return None


def _arg_by_bar(calls: dict[int, list], ci: int) -> dict[int, float]:
    """某调用次序的源值逐 bar 序列。

    pynecore 传给 ``ta.*`` 的是**当前 bar 的浮点数**（不是 Series 对象，见
    ``ta.highest`` 的 ``source: PyneFloat``），所以对象身份认不了亲——只能比数值。
    """
    out: dict[int, float] = {}
    for bar, lst in calls.items():
        if ci >= len(lst) or not lst[ci][0]:
            continue
        f = _source_value(lst[ci][0])
        if f is not None:
            out[bar] = f
    return out


def _out_by_bar(calls: dict[int, list], ci: int, cj: int) -> dict[int, float]:
    """某调用次序第 cj 列返回值的逐 bar 值（多返回值按列拆开比）。"""
    out: dict[int, float] = {}
    for bar, lst in calls.items():
        if ci >= len(lst):
            continue
        v = lst[ci][1]
        if isinstance(v, tuple):
            v = v[cj] if cj < len(v) else None
        elif cj:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        f = float(v)
        if f == f:
            out[bar] = f
    return out


def _width(calls: dict[int, list], ci: int) -> int:
    for lst in calls.values():
        if ci < len(lst):
            v = lst[ci][1]
            return len(v) if isinstance(v, tuple) else 1
    return 1


def _upstream(src: dict[int, float], hits: dict, self_key: str) -> tuple[str, int] | None:
    """谁的输出序列和本调用的第一个实参逐位相同——那就是它的输入来源。"""
    if len(src) < LINEAGE_MIN_BARS or len(set(src.values())) < 2:
        return None                    # 太短或恒定值：不认，免得撞上巧合
    for key, hit in hits.items():
        if key == self_key:
            continue
        calls = hit["by_bar"]
        n_calls = max((len(v) for v in calls.values()), default=0)
        for ci in range(n_calls):
            for cj in range(_width(calls, ci)):
                out = _out_by_bar(calls, ci, cj)
                common = src.keys() & out.keys()
                if len(common) >= LINEAGE_MIN_BARS and all(
                        out[b] == src[b] for b in common):
                    return key, ci
    return None


def _root(key: str, ci: int, hits: dict, memo: dict) -> tuple[str, int]:
    """沿「输入链」走到最上游的调用点——决定这条线归主图还是副图、跟谁同格。

    函数名不可靠的典型：`ta.highest(rsi, 14)` 名字在 OVERLAYS 里，值域却是 RSI 的
    0-100（中位数 ~50，恰好在价格的 0.25~4 倍护栏内）→ 会被画到价格主图上。
    谁算出来的比算什么更重要；链式（lowest(ema(rsi,9),5)）要一路走到根。

    返回 (调用点键, 该调用点内的第几次调用)：同一行写三个 ``ta.sma`` 时，
    它们是三个独立的根，不能因为同一行就当成一格。
    """
    mkey = (key, ci)
    if mkey in memo:
        return memo[mkey]
    memo[mkey] = (key, ci)          # 环（正常策略不会出现）自己兜底
    hit = hits.get(key) or {}
    up = _upstream(_arg_by_bar(hit.get("by_bar") or {}, ci), hits, key)
    if up:
        memo[mkey] = _root(up[0], up[1], hits, memo)
    return memo[mkey]


def _series(name: str, key: str, calls: dict[int, list], bars: int,
            hits: dict, memo: dict) -> list[dict]:
    """一个调用点 → 若干条序列（多返回值/一行多次调用都拆开）。"""
    cn, kinds = OVERLAYS.get(name) or PANES[name]
    subs = SUB_LABELS.get(name, ())
    n_calls = max((len(v) for v in calls.values()), default=0)
    out: list[dict] = []
    for ci in range(n_calls):
        pairs = [lst[ci] for _, lst in sorted(calls.items()) if ci < len(lst)]
        suffix = _stable_args(pairs)
        root_key, root_ci = _root(key, ci, hits, memo)
        root = f"{root_key}#{root_ci}"     # 分格用：跟随输入链，同根必同格
        for cj, kind in enumerate(kinds):
            vals: list[float | int | None] = [None] * bars
            for bar, lst in calls.items():
                if ci >= len(lst) or bar >= bars:
                    continue
                v = lst[ci][1]
                if isinstance(v, tuple):
                    v = v[cj] if cj < len(v) else None
                elif cj:
                    v = None
                vals[bar] = _num(v)
            if len({v for v in vals if v is not None}) < 2:
                continue               # 全空或恒定值：画上去没有信息
            label = cn
            if len(kinds) > 1:
                label += " " + (subs[cj] if cj < len(subs) else str(cj + 1))
                if n_calls > 1:
                    label += f"#{ci + 1}"
            elif n_calls > 1:
                label += str(ci + 1)
            out.append({"key": f"{key}:{ci}:{cj}", "root": root,
                        "root_name": (hits.get(root_key) or {}).get("name") or name,
                        "label": label + suffix, "kind": kind, "values": vals})
    return out


def _median(vals: list) -> float | None:
    xs = sorted(v for v in vals if v is not None)
    return xs[len(xs) // 2] if xs else None


def _mag(vals: list) -> float | None:
    """这条线在图上能撑多大：非空值的绝对值最大值（纵轴范围由它决定）。"""
    xs = [abs(v) for v in vals if v is not None]
    return max(xs) if xs else None


def _same_scale(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    if a == 0 or b == 0:
        return a == b
    lo, hi = (a, b) if a <= b else (b, a)
    return hi / lo <= PANE_SPLIT_RATIO


def _group_panes(panes: list[dict]) -> list[dict]:
    """给副图线分格：同一条推导链（同根）共一格，格内量级相差超过 4 倍的再拆开。

    拆：同一行的三个 ``ta.sma`` 可能是完全不同量纲的线（0541 实录：中位数
    0.0036 / 0.031 / 2.14），挤在一格时小的两条会被压成贴着 0 的直线。
    不并：跨调用点合并会把不相干的线塞进同一个纵轴（RSI 和 MACD 叠在一起，
    看着是一格其实各说各话）——一个指标一格是平台的通用口径。
    """
    slots: dict[str, list] = {}
    for s in panes:                    # 同根的线在排序后相邻
        slots.setdefault(s["root"], []).append(s)
    for root, ss in slots.items():
        mags: list[float | None] = []      # 每个桶的量级代表（首条线）
        bucket: list[int] = []
        for s in ss:
            m = _mag(s["values"])
            bi = next((i for i, b in enumerate(mags) if _same_scale(b, m)), None)
            if bi is None:
                mags.append(m)
                bi = len(mags) - 1
            bucket.append(bi)
        for s, bi in zip(ss, bucket):
            s["group"] = root if len(mags) == 1 else f"{root}#{bi}"
    return panes


def build(hits: dict, bars: int, price_ref: float | None = None) -> dict:
    """捕获结果 → ``{"bars", "overlays": [...], "panes": [...]}``（喂给 K 线图）。

    ``price_ref`` 是标的的价格中位数（收盘价），用来判断一条「叠加线」是不是真的
    与价格同量纲——``ta.sma(volume, 22)`` 也是 sma，画进主图就毁掉整张图。
    """
    overlays: list[dict] = []
    panes: list[dict] = []
    seen_vals: set[tuple] = set()
    root_memo: dict = {}

    for key, hit in sorted(hits.items(), key=lambda kv: (kv[1]["line"], kv[0])):
        name = hit["name"]
        if name not in OVERLAYS and name not in PANES:
            continue
        for s in _series(name, key, hit["by_bar"], bars, hits, root_memo):
            sig = tuple(s["values"])
            if sig in seen_vals:       # 同一根线的两份拷贝（如 vwap 三列全等）
                continue
            seen_vals.add(sig)
            overlay = s["root_name"] in OVERLAYS
            if overlay and price_ref:
                m = _median(s["values"])
                if m is None or not price_ref * PRICE_RATIO_LO <= m <= price_ref * PRICE_RATIO_HI:
                    overlay = False    # 不同量纲：降级到副图，别压垮价格轴
            (overlays if overlay else panes).append(s)

    # 按「格」排序而不是按自身调用点：跟随上游的线排在父旁边（组内按源码行号，
    # 父在前子在后），截断时先牺牲无关的
    panes.sort(key=lambda s: (PANE_ORDER.index(s["root_name"])
                              if s["root_name"] in PANE_ORDER else 99,
                              s["root"], int(s["key"].split("@")[1]), s["key"]))
    _group_panes(panes)
    for s in overlays + panes:
        s.pop("root", None)
        s.pop("root_name", None)
    return {"bars": bars,
            "overlays": overlays[:MAX_OVERLAYS],
            "panes": panes[:MAX_PANES]}
