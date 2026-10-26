# -*- coding: utf-8 -*-
"""ACP 简化形态引擎（纯 numpy，无第三方依赖，供 tradingview.AutoChartPatterns 调用）

Auto Chart Patterns [Trendoscope®]（https://cn.tradingview.com/script/WZ8B1FIW-Auto-Chart-Patterns-Trendoscope/）
的形态扫描核心存放在 Trendoscope 私有库（ZigzagLite / abstractchartpatterns / basechartpatterns）中，
本地仅有入口脚本，无法 1:1 还原。本模块实现自研简化版检测：

    枢轴(由外部 zigzag 生成, 峰 +1 / 谷 -1；支持百分比阈值

或 ZigzagLite 风格 length/depth 两种) → 枚举相邻枢轴窗口(窗口枢轴数 4..max_pivots)
    → 上边界线 = 窗口内首尾两个高点的连线;  下边界线 = 窗口内首尾两个低点的连线
    → 校验: ① 两侧中间的同类枢轴到各自边界线的相对偏差 <= error_ratio
            ② 窗口内全部枢轴落在两条边界线夹成的通道内(容差 error_ratio)
            ③ 两线在窗口内不交叉(近端/远端带距均 > 0)
    → 按两侧线斜率方向 + 带距收/扩/平行 分类出 通道/楔形/三角 13 类之一
    → 若干最近、互不重叠的形态各自渲染出: 高点线 / 低点线 / 中间折线

输出三条线语义(用户约定):
    - top_line    高点线: 形态窗口内"上边界线"(首尾高点连线)在 [start..end] 上的值, 其余 np.nan
    - bottom_line 低点线: 形态窗口内"下边界线"(首尾低点连线)在 [start..end] 上的值, 其余 np.nan
    - mid_line    中间折线: 窗口内枢轴按时间顺序依次相连的折线, 其余 np.nan
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class EngineParams:
    """检测参数(默认对齐入口脚本常用取值并允许自调)"""
    min_pivots: int = 4            # 参与识别的窗口最少枢轴数(2高+2低)
    max_pivots: int = 6            # 窗口最多枢轴数(原 numberOfPivots=5/6)
    error_ratio: float = 0.02      # 中间同类枢轴到边界线 / 通道夹持的相对容差
    flat_ratio: float = 0.2        # 边界线判"平/倾斜"的整段相对波动阈值(原 flatThreshold=20)
    conv_eps: float = 0.05         # 判定两线收/扩/平行 的带距变化相对阈值
    allow_channels: bool = True
    allow_wedges: bool = True
    allow_triangles: bool = True
    max_patterns: int = 6          # 最多保留的最近形态数
    avoid_overlap: bool = True     # 形态时间窗不与已接受形态重叠


@dataclass
class PivotNode:
    bar: int
    price: float
    kind: int          # +1 峰(高点) / -1 谷(低点)


# 形态中/英文名(便于文档与日志), 分类 token 用于测试断言
NAME_UPCH = 'Ascending Channel'
NAME_DNCH = 'Descending Channel'
NAME_RGCH = 'Ranging Channel'
NAME_RWE = 'Rising Wedge (Expanding)'
NAME_RWC = 'Rising Wedge (Contracting)'
NAME_FWE = 'Falling Wedge (Expanding)'
NAME_FWC = 'Falling Wedge (Contracting)'
NAME_CT = 'Converging Triangle'
NAME_DT = 'Diverging Triangle'
NAME_RTE = 'Ascending Triangle (Expanding)'
NAME_RTC = 'Ascending Triangle (Contracting)'
NAME_FTE = 'Descending Triangle (Expanding)'
NAME_FTC = 'Descending Triangle (Contracting)'


@dataclass
class Pattern:
    kind: str
    start: int                       # 形态起始 bar(4 个锚点中最早的 bar)
    end: int                         # 形态结束 bar(4 个锚点中最晚的 bar)
    top: Tuple[int, float, int, float]          # (bar1, p1, bar2, p2) 上边界锚点
    bottom: Tuple[int, float, int, float]       # (bar1, p1, bar2, p2) 下边界锚点
    mid: List[Tuple[int, float]]                # 中间折线节点 [(bar, price), ...]
    error: float                     # 两侧中间同类枢轴的最大相对偏差(评分用)
    pivot_count: int


def length_depth_pivots(high, low, length: int = 8, depth: int = 55) -> np.ndarray:
    """ZigzagLite 风格的枢轴：按 **bar 窗口**而不是百分比阈值。

    * ``length``: 确认窗口。``high[i]`` 是 ``[i-length, i+length]`` 内的最大值
      → 高点候选（低点同理，用 ``low``）；
    * ``depth`` : 同类型枢轴之间的最小 bar 间隔，且方向必须交替（与
      :func:`minibt.zigzag.PeakValleyPivots` 的输出语义一致）。

    返回与输入等长的 ``int8`` 数组：``1`` 峰 / ``-1`` 谷 / ``0`` 非枢轴。
    对应原码 ``zg.Zigzag.new(length, depth, level)``（默认 8 / 55）。
    """
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    n = int(high.shape[0])
    out = np.zeros(n, dtype=np.int8)
    length = max(1, int(length))
    depth = max(0, int(depth))
    if n < 3:
        return out

    window = 2 * length + 1
    # center=True：前后各看 length 根（与原码"枢轴右侧也要确认"一致）
    roll_max = (pd.Series(high).rolling(window, center=True, min_periods=1)
                .max().to_numpy())
    roll_min = (pd.Series(low).rolling(window, center=True, min_periods=1)
                .min().to_numpy())
    is_high = high >= roll_max - 1e-12
    is_low = low <= roll_min + 1e-12

    kept: List[PivotNode] = []          # 交替的枢轴序列
    for i in range(n):
        if is_high[i] and is_low[i]:
            continue                     # 同一根既是高又是低：跳过（罕见）
        if is_high[i]:
            kind, price = 1, float(high[i])
        elif is_low[i]:
            kind, price = -1, float(low[i])
        else:
            continue
        if not kept:
            kept.append(PivotNode(bar=i, price=price, kind=kind))
            continue
        last = kept[-1]
        if kind == last.kind:
            # 同向：更极端就替换（保持"最后一个枢轴"是最值）
            more_extreme = (price > last.price) if kind == 1 else (price < last.price)
            if more_extreme:
                kept[-1] = PivotNode(bar=i, price=price, kind=kind)
            continue
        # 反向：间隔不够就与上一个同类枢轴"二选一"（取更极端的那个）
        if i - last.bar >= depth:
            kept.append(PivotNode(bar=i, price=price, kind=kind))
            continue
        if len(kept) >= 2:
            prev = kept[-2]
            better = ((kind == 1 and price > prev.price)
                      or (kind == -1 and price < prev.price))
            if better:
                kept[-1] = PivotNode(bar=i, price=price, kind=kind)
        # 只有一个枢轴、且间隔不足 -> 丢弃当前候选

    for node in kept:
        out[node.bar] = node.kind
    return out


def line_val(ln: Tuple[int, float, int, float], x) -> float:
    """两点式线段在 bar=x 处的值(线性)"""
    b1, p1, b2, p2 = ln
    if b2 <= b1:
        return p1
    return p1 + (p2 - p1) * (x - b1) / (b2 - b1)


def _dir_tag(ln, flat_ratio: float, band0: float) -> int:
    """线整体方向: 1 上升 / -1 下降 / 0 走平。

    以窗口近端带宽 band0 为参照: 线锚点整段位移 |Δp| > flat_ratio * band0 才判倾斜。
    (相对于绝对价格, 带宽是"通道方向是否显著"的更合理尺度)
    """
    b1, p1, b2, p2 = ln
    if b2 <= b1:
        return 0
    move = p2 - p1
    if move > flat_ratio * band0:
        return 1
    if move < -flat_ratio * band0:
        return -1
    return 0


def _fit_ok(pts, ln, err_ratio):
    """同侧中间同类枢轴是否都贴近该边界线(相对偏差 <= err_ratio);
    pts 为按序同类枢轴(PivotNode), ln 由其首尾两点定义。"""
    maxdev = 0.0
    for node in pts[1:-1]:
        dev = abs(line_val(ln, node.bar) - node.price) / node.price
        maxdev = max(maxdev, dev)
        if dev > err_ratio:
            return None
    return maxdev


def _classify(top, bottom, params) -> Optional[str]:
    """按两线方向 + 带距演变(未来端收窄=converging)给出 13 类之一, 不符合返回 None"""
    x0 = min(top[0], bottom[0])
    x1 = max(top[2], bottom[2])
    w0 = line_val(top, x0) - line_val(bottom, x0)
    w1 = line_val(top, x1) - line_val(bottom, x1)
    if w0 <= 0 or w1 <= 0:                      # 窗口内两线相交/穿越
        return None
    if w1 < w0 * (1 - params.conv_eps):
        conv = 'c'
    elif w1 > w0 * (1 + params.conv_eps):
        conv = 'd'
    else:
        conv = 'p'

    tdir = _dir_tag(top, params.flat_ratio, w0)
    bdir = _dir_tag(bottom, params.flat_ratio, w0)

    pair = (tdir, bdir, conv)
    table = {
        (0, 0, 'p'): NAME_RGCH,
        (1, 1, 'p'): NAME_UPCH, (1, 1, 'c'): NAME_RWC, (1, 1, 'd'): NAME_RWE,
        (-1, -1, 'p'): NAME_DNCH, (-1, -1, 'c'): NAME_FWC, (-1, -1, 'd'): NAME_FWE,
        (0, 1, 'c'): NAME_RTC, (0, 1, 'd'): NAME_RTE,
        (-1, 0, 'c'): NAME_FTC, (-1, 0, 'd'): NAME_FTE,
        (1, -1, 'c'): NAME_CT, (1, -1, 'd'): NAME_DT,
        (-1, 1, 'c'): NAME_CT, (-1, 1, 'd'): NAME_DT,
    }
    return table.get(pair)


def _allowed(kind: str, params) -> bool:
    if kind.endswith('Channel'):
        return params.allow_channels
    if 'Wedge' in kind:
        return params.allow_wedges
    if 'Triangle' in kind:
        return params.allow_triangles
    return True


def _nodes_from_pivots(src, piv) -> List[PivotNode]:
    nodes = []
    for i, k in enumerate(piv):
        if k != 0:
            nodes.append(PivotNode(bar=i, price=float(src[i]), kind=int(k)))
    return nodes


def _candidate(nodes: List[PivotNode], params) -> Optional[Pattern]:
    """对单个相邻枢轴窗口判定形态; 上/下边界锚点为窗口内首尾高点/低点"""
    highs = [n for n in nodes if n.kind == 1]
    lows = [n for n in nodes if n.kind == -1]
    if len(highs) < 2 or len(lows) < 2:
        return None
    top = (highs[0].bar, highs[0].price, highs[-1].bar, highs[-1].price)
    bottom = (lows[0].bar, lows[0].price, lows[-1].bar, lows[-1].price)

    # ① 两侧中间同类枢轴贴合各自边界线
    e_t = _fit_ok(highs, top, params.error_ratio)
    if e_t is None:
        return None
    e_b = _fit_ok(lows, bottom, params.error_ratio)
    if e_b is None:
        return None
    err = max(e_t, e_b)

    # ② 全部枢轴夹在两线之间(容差), 且两线在窗口内不交叉
    for n in nodes:
        tol = params.error_ratio * n.price
        lo = line_val(bottom, n.bar) - tol
        hi = line_val(top, n.bar) + tol
        if not (lo <= n.price <= hi):
            return None

    kind = _classify(top, bottom, params)
    if kind is None or not _allowed(kind, params):
        return None

    start = min(top[0], bottom[0])
    end = max(top[2], bottom[2])
    mid = [(n.bar, n.price) for n in nodes]
    return Pattern(kind, start, end, top, bottom, mid, err, len(nodes))


def scan(src, piv, params: EngineParams = None) -> List[Pattern]:
    """全局扫描: 返回按“距当前最近优先、互不重叠”保留的最多 max_patterns 个形态"""
    if params is None:
        params = EngineParams()
    src = np.asarray(src, dtype=np.float64)
    nodes = _nodes_from_pivots(src, np.asarray(piv))
    n = len(nodes)
    if n < params.min_pivots:
        return []

    patterns: List[Pattern] = []

    def _overlaps(p: Pattern) -> bool:
        return any(not (p.end < q.start or p.start > q.end) for q in patterns)

    # 从最新枢轴往回遍历窗口, 保证优先保留最近的形态
    for b in range(n - 1, params.min_pivots - 2, -1):
        for a in range(max(0, b - params.max_pivots + 1), b - params.min_pivots + 2):
            cand = _candidate(nodes[a:b + 1], params)
            if cand is None:
                continue
            if params.avoid_overlap and _overlaps(cand):
                continue
            patterns.append(cand)
            if len(patterns) >= params.max_patterns:
                return patterns
    return patterns


#: 13 类形态名（顺序固定，供"文本钩子"用 code 解码 —— 钩子里拿不到 OHLCV）
KIND_NAMES: List[str] = (
    NAME_UPCH, NAME_DNCH, NAME_RGCH,
    NAME_RWC, NAME_RWE, NAME_FWC, NAME_FWE,
    NAME_RTC, NAME_RTE, NAME_FTC, NAME_FTE, NAME_CT, NAME_DT,
)


def kind_code(kind: str) -> int:
    """形态名 -> 1..13 的编号（0 表示未知）。"""
    try:
        return KIND_NAMES.index(kind) + 1
    except ValueError:
        return 0


def breakout_labels(close, patterns: List[Pattern],
                    max_scan: int = 250) -> List[Tuple[int, int, str]]:
    """形态的**突破点**标签：返回 ``[(bar, direction, kind)]``。

    价格（close）首次收在通道上/下边界**之外**的那根 bar 就是突破点 —— 原码的
    ``Pattern Label``（``showPatternLabel``）就画在那里，文字是形态名称（如
    ``Ascending Channel`` / ``Rising Wedge (Contracting)``）。

    * ``direction``：``1`` 向上突破（标在上边界外侧）、``-1`` 向下（下边界外侧）、
      ``0`` 还没突破 —— 此时退回形态确认点 ``end``，保证标签不会丢；
    * 形态确认点之后最多向后找 ``max_scan`` 根，找不到就算"未突破"。
    """
    close = np.asarray(close, dtype=np.float64)
    n = int(close.shape[0])
    out: List[Tuple[int, int, str]] = []
    for pat in patterns:
        bar, direction = min(pat.end, max(n - 1, 0)), 0
        for x in range(bar, min(n, bar + max_scan)):
            price = close[x]
            if not np.isfinite(price):
                continue
            if price > line_val(pat.top, x):
                bar, direction = x, 1
                break
            if price < line_val(pat.bottom, x):
                bar, direction = x, -1
                break
        out.append((bar, direction, pat.kind))
    return out


def render(size: int, patterns: List[Pattern]):
    """渲染三条线: 高点线 / 低点线 / 中间折线(其余保持 np.nan)"""
    top_line = np.full(size, np.nan, dtype=np.float64)
    bottom_line = np.full(size, np.nan, dtype=np.float64)
    mid_line = np.full(size, np.nan, dtype=np.float64)
    for pat in patterns:
        for x in range(pat.start, pat.end + 1):
            top_line[x] = line_val(pat.top, x)
            bottom_line[x] = line_val(pat.bottom, x)
        # 中间折线: 窗口枢轴依时间相连
        m = pat.mid
        for i in range(len(m) - 1):
            (x1, y1), (x2, y2) = m[i], m[i + 1]
            for x in range(x1, x2 + 1):
                if x2 > x1:
                    mid_line[x] = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
                else:
                    mid_line[x] = y1
    return top_line, bottom_line, mid_line
