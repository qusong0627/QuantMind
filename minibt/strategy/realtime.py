"""实时图表（重构版）：只接受 minibt 策略，K 线 + 指标全部来自策略。

参考 `light_chart.py::Chart._init_chart_data`：数据不外部传入，直接从

* ``strategy._btklinedataset``   -> K 线（``{symbol}_{cycle}``，``v._dataset.tq_object``）
* ``strategy._btindicatordataset`` -> 指标（``v._get_plot_datas(k)`` + ``v.pandas_object``）

里取。指标线按 ``overlaps`` 分主图（叠加在 K 线上）/ 副图（各占一个原生 pane）。

本版先做**初始化 + 静态加载**，实时更新（tick/换根）下一步再接。

    chart = RealtimeChart([strategy1, strategy2])
    chart.show()
"""
from __future__ import annotations

import inspect
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

import numpy as np
import pandas as pd

from pylightcharts import shapes

from minibt.strategy import indicator_menu
from minibt.strategy.live_tools import LiveTools
from minibt.strategy.strategy_window import INFO_PAGES, StrategyWindow

_DEBUG = bool(os.environ.get('REALTIME_DEBUG'))

#: 图表用的 K 线列（与 minibt 的 FILED.TALL 一致）
_KLINE_COLS = ('time', 'open', 'high', 'low', 'close', 'volume')

#: 实时图表开局先放出多少根 K 线（其余历史向左滚动时再加载）
DEFAULT_PRELOAD = 200

#: 指标默认颜色：深色主题（黑底）用亮色
COLORS_DARK = ['#e91e63', '#4caf50', '#ff9800', '#2196f3', '#9c27b0',
               '#00bcd4', '#ffeb3b', '#8bc34a', '#03a9f4', '#ff5722',
               '#3f51b5', '#f44336']
#: 指标默认颜色：浅色主题（白底）用深色
COLORS_LIGHT = ['#c2185b', '#2e7d32', '#e65100', '#1565c0', '#6a1b9a',
                '#00838f', '#f9a825', '#558b2f', '#0277bd', '#d84315',
                '#283593', '#c62828']


def palette_for(chart) -> List[str]:
    """按图表当前主题背景选深/浅两组指标默认颜色。"""
    theme = getattr(getattr(chart, 'win', None), '_theme', None) or {}
    return COLORS_DARK if _is_dark(theme.get('background', '#000000')) \
        else COLORS_LIGHT


#: 线型名 -> lightweight-charts 的 LineStyle 枚举值
#: （0=solid 1=dotted 2=dashed 3=large_dashed 4=sparse_dotted）
#: minibt 的 dotdash/dashdot 引擎里没有，用最接近的虚线/大虚线代替，
#: 否则会被当成未知值退化成实线。
_LINE_STYLE = {'solid': 0, 'dotted': 1, 'dashed': 2,
               'large_dashed': 3, 'sparse_dotted': 4,
               'dotdash': 2, 'dashdot': 3}
_LINE_STYLE_WARNED: set = set()


#: 回测结果页上仍然保留的指标（止损/止盈线）——交易信号和曲线放一起
#: 才看得清回测过程，其余指标在信息页上清掉
INFO_KEEP_SOURCES = ('stop_lines',)
INFO_KEEP_NAMES = ('stop_price', 'target_price')


def line_style_value(style: str) -> int:
    """minibt 的线型名 -> lightweight-charts 的 LineStyle 整数值。

    LWC 只有 5 种（solid/dotted/dashed/large_dashed/sparse_dotted）；minibt 的
    ``dotdash``/``dashdot`` 没有对应，映射到最接近的虚线。未知名字退回实线，
    但只提示一次（不再静默变成实线）。
    """
    key = str(style).strip().lower()
    if key in _LINE_STYLE:
        return _LINE_STYLE[key]
    if key not in _LINE_STYLE_WARNED:
        _LINE_STYLE_WARNED.add(key)
        print(f'[realtime] 未知线型 {style!r}，按实线处理；'
              f'可用：{sorted(_LINE_STYLE)}')
    return 0


def _is_dark(color: str) -> bool:
    """背景色是不是深色（用来选深/浅两组指标颜色）。"""
    value = str(color).lstrip('#')
    if len(value) == 3:
        value = ''.join(c * 2 for c in value)
    try:
        red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return True
    return (0.299 * red + 0.587 * green + 0.114 * blue) < 128


def _resolve_legend(value, name: Optional[str] = None,
                    index: Optional[int] = None) -> bool:
    """把 ``legend`` 解析成**这一条线**要不要图例行。

    与 ``isplot`` 同形，三种写法都支持 ✓：

    * ``bool`` → 该指标**所有**线统一开关（老行为 ✓）；
    * ``dict`` → 按**线名**：``legend = dict(zero=False)`` 只隐藏 zero 那一行，
      ``cci`` 照常显示 ✓；
    * ``list`` / ``tuple`` → 按**线序**（与 ``isplot`` 位置一致 ✓）。

    解析不出来时保持**显示**（老行为，不会意外藏掉标签 ✗）。
    """
    if isinstance(value, dict):
        if name is None:
            return True
        return bool(value.get(name, True))
    if isinstance(value, (list, tuple)):
        if index is None or index < 0 or index >= len(value):
            return True
        return bool(value[index])
    return bool(value)


def indicator_legend_visible(owner, source: str,
                             name: Optional[str] = None,
                             index: Optional[int] = None) -> bool:
    """**指标级图例开关**：``ind.legend = False`` / 类属性 ``legend = False`` ✓。

    指标对象按 ``spec.source`` **现取**（`strategy._btindicatordataset[key]` ✓）——
    实盘里每次 ``__call__`` 都会重建指标对象，捕获实例会拿到旧的 ✗。

    ``owner`` 可以是 ``IndicatorManager`` 或 ``RealtimeChart`` ✓（两者的
    ``strategy`` 属性名不一样，所以候选都试一遍 ✓）。取不到 / 没有该属性 →
    ``True``（保持老行为：显示图例 ✓）。
    """
    owners = [owner]
    for attr in ('strategy', 'strategy_instance', '_strategy'):
        candidate = getattr(owner, attr, None)
        if candidate is not None:
            owners.append(candidate)
    for item in owners:
        try:
            dataset = getattr(item, '_btindicatordataset', None)
            if isinstance(dataset, dict) and source in dataset:
                indicator = dataset[source]
                if indicator is not None:
                    return _resolve_legend(getattr(indicator, 'legend', True),
                                           name, index)
        except Exception:                                        # noqa: BLE001
            continue
    return True


@dataclass
class IndicatorSpec:
    """一条指标线。

    :param name: 显示名（图例行）
    :param overlap: True=主图叠加，False=副图
    :param values: 与 K 线等长的值（构建时一次性 set）
    :param kind: Line / Histogram
    :param get_last: 实时更新时取"最后一个值"（下一步用）
    :param get_values: 实时更新时**重新解析**整条线的最新值。实盘里策略每次
        `__call__` 都会重建指标对象并覆盖 `_btindicatordataset[key]`，捕获
        dataset 实例会拿到旧对象（永远停在初始化那一刻），所以必须按 key
        现取。
    :param up_color/down_color: 柱状图（Histogram）上下行颜色，`update_last`
        更新单点时要把颜色一起发下去，否则退化成序列默认色。
    :param color_conditions: 指标线的 `color_by` 规则现取用的条件数组，
        `[(mask, color, else_color), ...]`，更新单点时据此重算该点颜色。
    """
    name: str
    overlap: bool
    values: Optional[pd.Series] = None
    kind: str = 'Line'
    color: Optional[str] = None
    line_width: int = 2
    line_style: str = 'solid'
    price_line: bool = False
    price_label: bool = False
    vbar_base: Optional[float] = None   # vbar 柱底基线（柱子从哪画起）
    #: 柱体上下行配色的分界（`LineStyle.vbar_color_base`）；None = 跟随
    #: `vbar_base`（再默认 0）
    vbar_color_base: Optional[float] = None
    #: 柱体上下行配色（来自 `LineStyle.vbar_up_color/vbar_down_color`）；
    #: None = 跟主图 K 线色一致（由 `apply_histogram_colors` 补）
    vbar_up_color: Optional[str] = None
    vbar_down_color: Optional[str] = None
    source: str = ''           # 来自哪个指标（`_btindicatordataset` 的 key）
    get_last: Optional[Callable[[], float]] = None
    get_values: Optional[Callable[[], Optional[np.ndarray]]] = None
    up_color: Optional[str] = None
    down_color: Optional[str] = None
    #: 蜡烛型指标（``kind='Candlestick'``）：``{'open': 列名, 'high': …, 'low': …, 'close': …}``
    #: （列名是 ``v.pandas_object`` 里的列）；同一指标的其它线仍走普通 ``Line``。
    ohlc: Optional[dict] = None
    #: 蜡烛涨/跌配色（``CandleStyle`` 的 ``{'bull':…, 'bear':…}``）
    candlestyle: Optional[dict] = None
    #: 蜡烛型指标：实时重取 OHLC（``DataFrame``，列 open/high/low/close）
    get_ohlc: Optional[Callable[[], Optional[pd.DataFrame]]] = None
    #: 隐藏线：仅为承载 ``fill_between`` 而建（不进图例、不画线）—— 色带的
    #: 上下沿往往是 ``isplot=False`` 的“仅数据”列（如 HemaTrendLevels 的
    #: ``bull_top``/``bull_bottom``），没有它们 `apply_bands` 拿不到 series。
    hidden: bool = False
    #: 是否显示这条线所属**指标**的图例标签（指标级 `legend = False` ✓）。
    #: `hidden=True` 是「仅为承载色带而建的隐藏线」；这个字段是「指标自己说
    #: 不显示图例」✗——两者都会让序列不进图例 ✓。
    legend: bool = True
    color_conditions: List[tuple] = field(default_factory=list)
    #: 无限历史用：全量指标值（按 epoch 秒索引）。实盘策略只重算 ``preload``
    #: 根，历史部分的指标值在这里缓存，不要每次 tick 重算 10000 根。
    cached_series: Optional[pd.Series] = None
    #: 这个 `color` 是 `IndicatorManager.build()` **自动分配的调色板色**（不是
    #: 策略里写的 `line_color`）。柱体配色要靠它区分“用户显式单色”与“默认色”。
    palette_color: bool = False
    #: 指标标签齿轮菜单的「参数」页：``[{'name','value','type'}]``；
    #: ``None`` / 空 = 不显示参数页（minibt 拿不到指标计算函数签名时）。
    menu_params: Optional[list] = None
    #: ---- 滚动 Volume Profile（``category == 'vp'``）----
    #: ``(K线根数, 档数)`` 的分布矩阵（**列 = 等宽价格档**，与
    #: `RollingVolumeProfile` / `VolumeProfileMA` 的 ``pandas_object`` 一致）。
    profile: Optional[np.ndarray] = None
    #: 横条颜色 / POC（量最大那一档）颜色（``linestyle['vp0']`` / ``['vp_poc']``）
    vp_color: Optional[str] = None
    vp_poc_color: Optional[str] = None
    #: 档区用**多少根** K 线的 ``[min(low), max(high)]`` 确定（滚动，逐根不同）
    vp_price_bars: Optional[int] = None
    #: 满量程柱长 = 当前**可视跨度** × ``vp_frac``（与 bokeh 的 0.30 一致）
    vp_frac: float = 0.30
    #: VP 的 y：``'price'``=价格档中心（主图）/``'index'``=档位序号（副图）
    vp_y_mode: str = 'price'
    #: 规范化后的 `VPStyle`（柱色 / POC 色 / 透明度 / 描边 / 满量程比例 /
    #: 档高比例 / 档区窗口 / 右侧成交量刻度）——`category='vp'` 专用。
    #: 见 `minibt/utils.py::VPStyle`，bokeh 侧读同一个值。
    vpstyle: Optional[dict] = None
    #: 实时重取 VP 分布矩阵（列 = 价格档）。实盘里策略每次 `__call__` 都会用
    #: 新对象覆盖 `_btindicatordataset[key]`，所以必须按 key 现取，不能只用
    #: 构建时的 `profile`。
    get_profile: Optional[Callable[[], Optional[np.ndarray]]] = None
    #: 鼠标 CSS x 存在 ``window`` 上的属性名。**数据一变就清成 null** ——
    #: 优先级：实时更新 > 鼠标跟随（与指标数值表格一致）。
    vp_xkey: Optional[str] = None


def _candle_options(candlestyle) -> dict:
    """``CandleStyle``（``{'bull':…, 'bear':…}``）→ LWC Candlestick 的涨/跌色。"""
    opts: dict = {}
    if isinstance(candlestyle, dict):
        bull = candlestyle.get('bull')
        bear = candlestyle.get('bear')
        if bull:
            opts.update({'up_color': bull, 'border_up_color': bull,
                         'wick_up_color': bull})
        if bear:
            opts.update({'down_color': bear, 'border_down_color': bear,
                         'wick_down_color': bear})
    return opts


def _color_options(kind: str, color: str, width: int = 2,
                   style: str = 'solid', price_line: bool = False,
                   price_label: bool = False) -> dict:
    """指标线的样式选项（来自 PlotInfo.linestyle）。"""
    options = {'line_width': width,
               'price_line_visible': price_line,
               'last_value_visible': price_label}
    if kind == 'Area':
        options.update({'line_color': color, 'top_color': color + '55',
                        'bottom_color': color + '00'})
    else:
        options['color'] = color
        if kind == 'Line':
            options['line_style'] = line_style_value(style)
    return options


def _apply_color(series, kind: str, color: str) -> None:
    """事后改色（用于没有重建序列的场景）。"""
    try:
        options = _color_options(kind, color)
        if kind != 'Line':
            options.pop('line_style', None)
        series.style(**options)
    except Exception:
        pass


# ---------------------------------------------------------------- 策略数据
#: minibt 的 Markers 名 -> lightweight-charts 的 MARKER_SHAPE
#: （LWC 只有 arrow_up / arrow_down / circle / square）
_MARKER_SHAPE = {
    'arrow_up': 'arrow_up', 'arrow_down': 'arrow_down',
    'circle': 'circle', 'square': 'square',
    'triangle': 'arrow_up', 'triangle_dot': 'arrow_up',
    'inverted_triangle': 'arrow_down',
    'asterisk': 'circle', 'cross': 'circle', 'circle_cross': 'circle',
    'circle_dot': 'circle', 'circle_x': 'circle', 'circle_y': 'circle',
    'dash': 'square', 'dot': 'circle',
}
_MARKER_SHAPE_WARNED: set = set()


def marker_shape_value(shape: str) -> str:
    """minibt 标记名 -> LWC 标记形状（未知的退回 circle 并只提示一次）。"""
    key = str(shape).strip().lower()
    if key in _MARKER_SHAPE:
        return _MARKER_SHAPE[key]
    if key not in _MARKER_SHAPE_WARNED:
        _MARKER_SHAPE_WARNED.add(key)
        print(f'[realtime] 未知标记 {shape!r}，按 circle 处理；'
              f'可用：{sorted(_MARKER_SHAPE)}')
    return 'circle'


def marker_size_value(size) -> float:
    """minibt 的 size（默认 12）-> LWC 的 size（引擎默认 1）。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return 1.0
    if value <= 0:
        return 1.0
    return max(0.5, min(2.0, value / 12.0))


def _style_get(style, key: str, default=None):
    """SignalStyle 既可能是对象也可能是 dict。"""
    if style is None:
        return default
    if isinstance(style, dict):
        return style.get(key, default)
    return getattr(style, key, default)


#: 信号锚点写成这些名字 = 锚在 K 线自身（贴 low/high）；其余名字按**指标线**解析
_KLINE_ANCHORS = ('open', 'high', 'low', 'close', 'hl2', 'hlc3', 'ohlc4',
                  'volume', 'time')


def _unpack_signal(item):
    """信号项兼容 3 / 4 / 5 元组：``(name, values, style[, anchor[, pane]])``。"""
    item = tuple(item)
    return (item[0], item[1], item[2],
            item[3] if len(item) > 3 else None,
            item[4] if len(item) > 4 else 0)


def extract_minibt_signals(strategy, plot_id: int, panes=None) -> list:
    """信号线 -> ``[(line_name, values, style, anchor, pane), ...]``。

    `PlotInfo.signallines`（如 ``['long_signal', 'short_signal']``）
    + `signalstyle[line]`（`SignalStyle`: color/marker/size/label/show）。

    取值和指标线一样走 `_get_plot_datas()[8]`（即当前的 `v.values`），
    **不读 `pandas_object`**：实盘每次重算可能只有 `values` 是新的，
    信号如果读缓存就会“上一 tick 的信号一直不消”。
    """
    out = []
    for key, dataset in getattr(strategy, '_btindicatordataset', {}).items():
        try:
            if getattr(dataset, 'plot_id', plot_id) != plot_id:
                continue
            plotinfo = getattr(dataset, '_plotinfo', None)
            styles = getattr(plotinfo, 'signalstyle', None) or {}
            names = list(getattr(plotinfo, 'signallines', None) or [])
            if not names and hasattr(styles, 'keys'):
                names = [str(k) for k in styles.keys()]
            if not names:
                continue
            plot = dataset._get_plot_datas(key)
            lines = list(plot[4] or [])          # `_lines`：原始列名
            # 多分组指标（部分线主图、部分副图）时 ``plot[8]`` 是 list-of-array，
            # 直接 ``np.asarray`` 会因形状不一致报错；此时退回 ``pandas_object``。
            indicators = None
            flat = False
            try:
                indicators = np.asarray(plot[8], dtype=float)
                if indicators.ndim == 1:
                    indicators = indicators.reshape(-1, 1)
                flat = bool(lines) and all(isinstance(x, str) for x in lines)
            except (ValueError, TypeError):
                indicators = None
            for col in names:
                style = styles.get(col) if hasattr(styles, 'get') else None
                if _style_get(style, 'show', True) is False:
                    continue
                values = None
                if flat and col in lines and lines.index(col) < indicators.shape[1]:
                    values = indicators[:, lines.index(col)]
                else:
                    # 多分组 / 少见结构：退回 pandas_object（兼容旧逻辑）
                    values = _values(getattr(dataset, 'pandas_object', None),
                                     col, 0, 1)
                if values is None:
                    continue
                # 锚点：名字是 K 线的 low/high 等就保持"贴 K 线"，否则按
                # **指标线**解析（bokeh 那边 overlap=False 的支路同义）——
                # 于是 ``SignalStyle('vidya_up', ...)`` 能把箭头画在 vidya_up 上。
                anchor = None
                anchor_key = str(_style_get(style, 'key', None) or '').strip()
                if anchor_key and anchor_key.lower() not in _KLINE_ANCHORS:
                    if (flat and anchor_key in lines
                            and lines.index(anchor_key) < indicators.shape[1]):
                        anchor = np.asarray(
                            indicators[:, lines.index(anchor_key)], dtype=float)
                    else:
                        # 多分组指标（plot[8] 不是规整二维数组，如 VIDYA）
                        # 走和**取值**同一条兜底：从 pandas_object 按列名取，
                        # 保证锚点与信号值同源、长度一致。
                        anchor = _values(dataset.pandas_object, anchor_key, 0, 1)
                        anchor = (None if anchor is None
                                  else np.asarray(anchor, dtype=float))
                # 画在哪个 pane：`SignalStyle(..., overlap=False)` = 副图，
                # 跟指标本体（同 `source`）用同一个 pane —— 例如 QQE 的
                # `qqe_slow` 在副图，信号也应该跟着去副图；
                # `overlap=True`（默认）= 主图 pane 0。
                pane = 0
                if _style_get(style, 'overlap', True) is False:
                    pane = int((panes or {}).get(key, 0) or 0)
                out.append((col, np.asarray(values, dtype=float),
                            style or {}, anchor, pane))
        except Exception as error:
            print(f'[realtime] 解析信号 {key} 失败:', error)
    return out


def _marker_time(value):
    """marker 的时间：统一成和 K 线数据一样的**整数秒**。

    pylightcharts 的数据点是整数秒；marker 若给浮点，缩放时可能对不上
    （表现就是"缩放了 marker 就不见，fit 才出现"）。
    """
    try:
        stamp = pd.Timestamp(value)
        if stamp.tz is not None:
            return int(stamp.timestamp())
        return int(stamp.value // 10 ** 9)
    except Exception:
        return value


class SignalTextMap(dict):
    """`{信号线: 文本数组}` + **解析好的逐点文本样式**（``.style``）。

    样式由 :func:`minibt.utils.resolve_signal_text_style` 按主题解析后挂在这里，
    于是 bokeh 与 pylightcharts 两个渲染端读的是**同一份**样式（键名也同一套）。
    """

    style: dict = {}


def extract_minibt_signal_texts(strategy, plot_id: int,
                                black_style: bool = False) -> SignalTextMap:
    """从策略提取逐点信号文本（`PlotInfo.signal_texts`）。

    minibt 侧可以是一个 ``signal_text`` property 返回 ``{线名: 文本数组}``
    （Bokeh 需要单独的文字 glyph 列，所以那边要写函数）；LWC 的标记自带
    ``text`` 字段，这里只要把数组取出来给标记用就行。
    """
    # 绝对导入：本模块会被测试以“独立文件”方式加载（没有父包）
    from minibt.utils import resolve_signal_text_style
    texts: SignalTextMap = SignalTextMap()
    for key, dataset in getattr(strategy, '_btindicatordataset', {}).items():
        try:
            if getattr(dataset, 'plot_id', plot_id) != plot_id:
                continue
            if getattr(dataset, 'iscandles', False):
                continue
            info = dataset._get_plot_datas(key)[10] or {}
            found = info.get('signal_texts') if hasattr(info, 'get') else None
            if not found:
                continue
            for line, values in dict(found).items():
                texts[str(line)] = list(values)
                if texts.style is SignalTextMap.style:
                    # 指标级样式（signaltext.style）：按图表主题解析成规范 dict
                    texts.style = resolve_signal_text_style(
                        info.get('signal_texts'), black_style)
        except Exception as error:
            print(f'[realtime] 解析信号文本 {key} 失败:', error)
    return texts


def _text_shape_kwargs(style: dict) -> dict:
    """规范样式 → ``shapes.text(...)`` 关键字（**pylightcharts 侧唯一映射点**）。

    键名在两处不同：minibt 用 ``text_color`` / ``background_fill_color`` 这类
    中性名（也贴近 bokeh），pylightcharts 的 shapes 用 ``color`` /
    ``background_color``。映射只在这里做一次，渲染层不再各自解释样式。
    """
    if not isinstance(style, dict) or not style:
        return {}                       # 逐点列里没样式的行是 NaN，直接忽略
    out: dict = {}
    simple = (('font_size', 'font_size', float),
              ('line_height', 'line_height', float),
              ('text_color', 'color', None),
              ('text_align', 'align', None),
              ('baseline', 'baseline', None),
              ('offset_x', 'offset_x', float),
              ('offset_y', 'offset_y', float),
              ('font_family', 'font_family', None),
              ('font_weight', 'font_weight', None),
              ('font_style', 'font_style', None),
              ('background_fill_color', 'background_color', None),
              ('background_fill_alpha', 'background_alpha', float),
              ('border_line_color', 'border_color', None),
              ('border_line_alpha', 'border_alpha', float),
              ('border_line_width', 'border_width', float),
              ('padding', 'padding', float))
    for key, shape_key, cast in simple:
        value = style.get(key)
        if value is None or value == '':
            continue
        out[shape_key] = cast(value) if cast else value
    return out


def _signal_payload(frame, name, values, style, texts=None, anchor=None):
    """一条信号线 -> ``(data, make_shapes, color)``。

    逻辑和旧 `apply_signals` 一致：只在“上一根无信号、这一根有信号”的跳变点
    画一个标记。因为每次都从**最新**的信号值重算，所以 tick 更新时：

    * 当前根从 0 变非 0 -> 自动多出一个点；
    * 当前根从非 0 变 0 -> 该点自然消失；
    * 换根后重算最后两根，收盘价修正导致的信号增/减也会跟着变。

    计时用 `frame` 的 time/high/low/close。没有信号点时返回空 DataFrame。
    """
    if frame is None or frame.empty:
        return pd.DataFrame(), None, None
    glyph = str(_style_get(style, 'marker', None) or 'circle')
    color = _style_get(style, 'color', None) or '#2196F3'
    anchor_name = str(_style_get(style, 'key', None) or '').lower()
    # ``anchor`` 非 None = 锚在**指标线**上（如 vidya_up）：标记就贴在线的价格上，
    # 不再按 low/high 上下偏移。
    on_line = anchor is not None
    below = (glyph in ('triangle', 'arrow_up') if on_line else
             ('low' in anchor_name) or (not anchor_name and
                                        glyph in ('triangle', 'arrow_up')))
    try:                                     # SignalStyle.size 默认 12 -> 半径 6
        size = max(3, min(9, round(
            float(_style_get(style, 'size', 12)) / 2)))
    except (TypeError, ValueError):
        size = 6
    # 尖头类图形：锚点=尖端，直接贴住低/高价；其余：锚点=中心，挪到 K 线外
    tipped = glyph in ('triangle', 'inverted_triangle', 'arrow_up',
                       'arrow_down')
    offset = 0.0 if on_line else (
        (2.0 if below else -2.0) if tipped else (
            (size + 4.0) if below else -(size + 4.0)))
    # 信号文本：优先逐点数组（signal_texts），否则用 SignalStyle.label 的固定文字
    line_texts = (texts or {}).get(name)
    label = _style_get(style, 'label', None)
    fixed_text = getattr(label, 'text', None)
    if not fixed_text and isinstance(label, str):
        fixed_text = label               # label 也允许直接写字符串
    from minibt.utils import _label_text_style, normalise_text_item
    # 样式（两个引擎共用一套键）: 主题预设 + 指标级( texts.style ) → 逐线 label
    text_style: dict = dict(getattr(texts, 'style', None) or {})
    text_style.update(_label_text_style(label))
    label_color = _as_color(text_style.get('text_color'), color)
    label_size = int(text_style.get('font_size') or 10)

    times = list(frame['time'])
    n = len(times)

    def column(key: str) -> np.ndarray:
        if key in frame.columns:
            return np.asarray(frame[key], dtype=float)
        return np.full(n, np.nan)

    highs, lows, closes = column('high'), column('low'), column('close')
    rows = []
    last = 0.0
    for i in range(min(n, len(values))):
        value = float(values[i])
        if not np.isfinite(value):
            # NaN 视作"这一根没有信号"：否则稀疏信号（如 VIDYA 的趋势箭头，
            # 事件那根有值、其余全 NaN）只有第一个事件能触发，后面的全被
            # 上一次的非 0 值卡住。
            last = 0.0
            continue
        if value != 0 and last == 0:
            if on_line and i < len(anchor) and np.isfinite(anchor[i]):
                # 锚到指标线：直接用它在该 bar 的价格
                base = float(anchor[i])
            else:
                base = lows[i] if below else highs[i]
            high_i = highs[i] if np.isfinite(highs[i]) else base
            low_i = lows[i] if np.isfinite(lows[i]) else base
            if not np.isfinite(base):
                base = closes[i] if np.isfinite(closes[i]) else value
            # value 只用来让自动缩放给标记留位置：贴 K 线时往外再抻一点，
            # 锚指标线时就是线价本身（线本来就在可见范围内）。
            margin = max(abs(float(high_i) - float(low_i)) * 1.0, 1e-6)
            tip = (float(base) if on_line else
                   (float(base) - margin if below else float(base) + margin))
            rows.append({'time': times[i], 'value': tip,
                         'base': float(base)})
            text = fixed_text
            point_style: dict = {}
            if line_texts is not None and i < len(line_texts):
                # 逐点元素可以是 str，也可以是 {'text': ..., 'font_size': ...}
                point_text, point_style = normalise_text_item(line_texts[i])
                if point_text:
                    text = point_text
            if text:
                text_price = (float(base) - margin * 0.5 if below
                              else float(base) + margin * 0.5)
                rows[-1]['text'] = str(text)
                rows[-1]['text_price'] = text_price
                if point_style:
                    merged = dict(text_style)
                    merged.update(point_style)
                    rows[-1]['text_style'] = merged
        last = value

    def make_shapes(row, _glyph=glyph, _color=color, _offset=offset,
                    _size=size, _below=below, _lcolor=label_color,
                    _lsize=label_size):
        # 像素图元：offset/size 都是**像素**，放大缩小都不变形
        glyphs = [shapes.marker(float(row['base']), marker=_glyph,
                                offset=_offset, size=_size, color=_color)]
        if row.get('text'):
            point_style = row.get('text_style')
            kwargs = _text_shape_kwargs(
                point_style if isinstance(point_style, dict) else text_style)
            kwargs.setdefault('color', _lcolor)
            kwargs.setdefault('font_size', _lsize)
            kwargs.setdefault('baseline', 'top' if _below else 'bottom')
            glyphs.append(shapes.text(
                float(row['text_price']), str(row['text']), **kwargs))
        return glyphs

    return (pd.DataFrame(rows) if rows else pd.DataFrame()), make_shapes, color


def apply_signals(chart, frame, signals, texts=None) -> list:
    """把信号画成**自定义序列的三角**（不用 LWC v5 的 marker primitive）。

    v5 的标记是挂在序列上的 primitive，**不计入自动缩放**：`belowBar` 的标记
    一放大就落到可见价格范围外被裁掉（只有上方的还在）。改用 custom series 后
    `priceValueBuilder` 会把箭头尖端算进价格范围，缩放/平移都跟随。

    :return: 建好的自定义信号序列列表；切策略/合约时要拿它 `delete()`，
        否则上一个合约的信号会一直留在图上。
    """
    made: list = []
    if not signals or frame is None or frame.empty:
        return made
    for item in signals:
        name, values, style, anchor, pane = _unpack_signal(item)
        data, make_shapes, color = _signal_payload(frame, name, values,
                                                   style, texts, anchor)
        try:
            series = chart.add_custom_series(name=name, pane_index=pane,
                                             color=color)
            series.set(data, shapes=make_shapes)
            made.append(series)
        except Exception as error:
            print('[realtime] 画信号失败:', error)
    return made


class SignalManager:
    """交易信号的自定义序列：创建 / 实时更新 / 清理。

    与 `apply_signals` 的区别是它会**复用**已建好的序列：每次更新只
    `series.set(...)` 重发该信号线的全部点（信号很稀疏，代价很小），
    这样 tick 里信号出现/消失、换根时最后两根被收盘价修正，都会立刻反映。

    `align` 与 `IndicatorManager` 同口径：``'head'`` = 图表显示**最前面** n 根
    （回放：逐根放出的 prefix），``'tail'`` = 显示**最新** n 根（实时/回测）。
    取错一边信号就会整体错位。
    """

    def __init__(self, chart, align: str = 'head'):
        self.chart = chart
        self.align = align
        self.series: dict = {}          # 信号线名 -> CustomSeries
        self._panes: dict = {}          # 信号线名 -> pane_index（变了好重建）

    def build(self, frame, signals, texts=None) -> None:
        self.clear()
        self.update(frame, signals, texts)

    def update(self, frame, signals, texts=None) -> None:
        active = set()
        n = 0 if frame is None else len(frame)
        for item in (signals or []):
            name, values, style, anchor, pane = _unpack_signal(item)
            active.add(name)
            # 与 IndicatorManager 同口径取 n 个值：回放取最前面 n 个
            # （align='head'），实时/回测取最后 n 个（align='tail'）。
            # 取错一边信号就会和 K 线错位（回放里表现为整体右移）。
            values = np.asarray(values)
            if n:
                values = (values[-n:] if getattr(self, 'align', 'head') == 'tail'
                          else values[:n])
            if anchor is not None:      # 锚点数组要和信号值**同口径**切
                anchor = np.asarray(anchor, dtype=float)
                if n:
                    anchor = (anchor[-n:]
                              if getattr(self, 'align', 'head') == 'tail'
                              else anchor[:n])
            data, make_shapes, color = _signal_payload(frame, name, values,
                                                       style, texts, anchor)
            series = self.series.get(name)
            if series is not None and self._panes.get(name) != pane:
                # pane 变了（例如 `SignalStyle(overlap=...)` 改了）→ 重建
                self._delete(self.series.pop(name))
                series = None
            if series is None:
                if data is None or data.empty:
                    continue            # 现在没信号，等出现再建
                try:
                    series = self.chart.add_custom_series(
                        name=name, pane_index=pane, color=color)
                except Exception as error:
                    print('[realtime] 画信号失败:', error)
                    continue
                self.series[name] = series
                self._panes[name] = pane
            try:
                series.set(data, shapes=make_shapes)
            except Exception as error:
                print('[realtime] 更新信号失败:', error)
        # 信号线本身没了（换指标）就把对应序列删掉
        for name in [k for k in self.series if k not in active]:
            self._delete(self.series.pop(name))

    def clear(self) -> None:
        for series in self.series.values():
            self._delete(series)
        self.series = {}
        self._panes = {}

    @staticmethod
    def _delete(series) -> None:
        try:
            series.delete()
        except Exception:
            pass


# ------------------------------------------------ 回测结果页的装饰（信号 + 停止线）
def _contract_index(strategy, contract: str) -> int:
    """合约名 -> `plot_id`（不在列表里退回 0，等价旧的回退行为）。"""
    contracts = strategy_contracts(strategy)
    return contracts.index(contract) if contract in contracts else 0


def _align_values(values, n: int, align: str = 'head') -> np.ndarray:
    """把一条**与全量等长**的值对齐到当前视图长度 `n`。

    ``'head'`` = 视图是全量的前 n 根（回放）；``'tail'`` = 视图是全量的后 n
    根（实时 / MiniQT 截断）。取错一边信号/停止线就会整体错位 ✗。
    """
    arr = np.asarray(values, dtype=float).ravel()
    if arr.size == n:
        return arr
    if align == 'tail':
        if arr.size > n:
            return arr[-n:]
        return np.concatenate([np.full(n - arr.size, np.nan), arr])
    if arr.size > n:
        return arr[:n]
    return np.concatenate([arr, np.full(n - arr.size, np.nan)])


def draw_stop_line(chart, spec, frame: pd.DataFrame, pane: int,
                   color: Optional[str] = None, align: str = 'head',
                   single_gap: bool = False):
    """把一条止损/止盈线画到指定 pane；**全 NaN 的线直接跳过**。

    :param single_gap: 用"单序列 + 透明缺口"渲染 NaN 断点（MiniQT/Qt 上止损线
        可能有几十段 NaN，默认一段一个内部序列会把 JS 渲染线程拖死 ✗）。
        **必须在 `set()` 之前**打开 —— `set()` 里就会把缺口点染透明 ✓。
    """
    values = getattr(spec, 'values', None)
    if values is None:
        return None
    arr = np.asarray(values, dtype=float).ravel()
    if not np.isfinite(arr).any():
        return None                         # 全 NaN：不显示（用户要求）
    arr = _align_values(arr, len(frame), align)
    try:
        options = _color_options(spec.kind, color or spec.color,
                                 spec.line_width, spec.line_style,
                                 spec.price_line, spec.price_label)
        series = chart.add_series(
            spec.kind, name=spec.name, pane_index=pane,
            legend=bool(getattr(spec, 'legend', True)), **options)
        if single_gap:
            func = getattr(series, 'prefer_single_gap', None)
            if callable(func):
                try:
                    func()
                except Exception:                            # noqa: BLE001
                    pass
        series.set(pd.DataFrame({'time': list(frame['time']),
                                 spec.name: arr}))
        return series
    except Exception as error:
        print('[realtime] 画止损线失败:', error)
        return None


def draw_contract_info(chart, strategy, contract: str, frame: pd.DataFrame,
                       pane: int, *, palette=(), theme_dark: bool = True,
                       panes=None, align: str = 'head',
                       single_gap: bool = False) -> list:
    """回测结果页：在 `pane` 上画**该合约自己的**交易信号 + 止损/止盈线。

    返回新建的序列列表（调用方负责在切页/重建时 `delete()`）。其它指标一律
    不画 —— 副图已有权益 / 回撤 / 盈亏，信号 + 停止线足够看清在哪里买卖。
    主图第一个合约与其余合约的副图都走这一个入口。

    :param align: 信号/停止线与全量等长时怎么对齐到 `frame`：``'head'``（回放，
        视图 = 前 n 根）/ ``'tail'``（实时 / MiniQT 截断，视图 = 后 n 根）✓。
    """
    made: list = []
    if frame is None or getattr(frame, 'empty', True):
        return made
    plot_id = _contract_index(strategy, contract)
    n = len(frame)
    # 1) 交易信号：强制画在该合约自己的 pane 上
    signals = [(name, _align_values(values, n, align), style,
                (None if anchor is None
                 else _align_values(anchor, n, align)), pane)
               for (name, values, style, anchor, _pane)
               in extract_minibt_signals(strategy, plot_id, panes)]
    try:
        texts = extract_minibt_signal_texts(strategy, plot_id, theme_dark)
        made.extend(apply_signals(chart, frame, signals, texts))
    except Exception as error:
        print('[realtime] 画交易信号失败:', error)
    # 2) 止损/止盈线（stop_price / target_price）；全 NaN 的跳过
    colors = list(palette or [])
    offset = 0
    for spec in extract_minibt_indicators(strategy, plot_id):
        if getattr(spec, 'source', '') != 'stop_lines':
            continue
        color = spec.color
        if color is None and colors:
            color = colors[offset % len(colors)]
            offset += 1
        series = draw_stop_line(chart, spec, frame, pane, color, align,
                                single_gap=single_gap)
        if series is not None:
            made.append(series)
    return made


# ------------------------------------------------ 入场/离场连线（复刻 bokeh）
#: 四类交易标记：``(key, side, is_entry)``。颜色/形状/大小从 `TradeStyle` 取
#: （``config.tradestyle``，默认多头入场红上三角、空头入场绿下三角、离场红/绿圆）。
TRADE_MARKER_KEYS = (
    ('long_entry', 'long', True),
    ('short_entry', 'short', True),
    ('long_exit', 'long', False),
    ('short_exit', 'short', False),
)
#: 入场→离场连线颜色（`TradeStyle.line_color=None` 时的主题默认：
#: 深色主题黄、浅色主题蓝，与 bokeh 一致）
TRADE_LINE_DARK = '#ffeb3b'
TRADE_LINE_LIGHT = '#2962ff'


def _trade_marker_size(size) -> int:
    """bokeh 口径的标记尺寸 -> pylightcharts 标记半径（默认 12->10、9->7）。"""
    try:
        return max(3, min(20, int(round(float(size) * 0.8))))
    except (TypeError, ValueError):
        return 7


def _trade_line_style(value) -> str:
    """minibt `LineDash` -> `shapes.polyline(style=...)` 认的线型名。"""
    text = str(getattr(value, 'value', value) or '').strip().lower()
    return {'dotdash': 'large_dashed', 'dashdot': 'large_dashed',
            'vbar': 'solid'}.get(text, text or 'dashed')


def extract_trade_lines(strategy, plot_id: int, frame: pd.DataFrame,
                        align: str = 'head') -> list:
    """从 ``strategy._results[plot_id]`` 提取每笔交易（复刻 ``get_trades_source``）。

    只需 ``positions``（>0 多、<0 空、0 平）+ ``cum_profits``；返回::

        [{'side': 'long'/'short', 'entry_index', 'exit_index',
          'entry_price', 'exit_price', 'profit'}, ...]

    入场/离场价取**该根的收盘价**（与 bokeh 画出来的入场标记一致）。

    :param align: `frame` 与全量 `positions` 的对齐方式：``'head'`` = frame 是
        前 n 根（回放）/ ``'tail'`` = frame 是后 n 根（实时 / MiniQT 截断）。
    """
    results = getattr(strategy, '_results', None)
    if not results or plot_id < 0 or plot_id >= len(results):
        return []
    result = results[plot_id]
    if result is None or 'positions' not in getattr(result, 'columns', []):
        return []
    if frame is None or getattr(frame, 'empty', True) \
            or 'close' not in frame.columns:
        return []
    positions = np.asarray(result['positions'], dtype=float).ravel()
    cum = (np.asarray(result['cum_profits'], dtype=float).ravel()
           if 'cum_profits' in result.columns
           else np.zeros(positions.size, dtype=float))
    closes = np.asarray(frame['close'], dtype=float).ravel()
    # 全量 positions 与 frame（可能是最后 n 根）对齐
    pair = int(min(positions.size, cum.size))
    n = int(min(pair, closes.size))
    if align == 'tail':
        positions = positions[pair - n:]
        cum = cum[pair - n:]
    out: list = []
    side: Optional[str] = None
    entry_i = 0
    entry_cp = 0.0
    pre_pos = 0.0
    for i in range(n):
        pos = positions[i]
        cp = cum[i]
        # 平仓（回 0）或反手（多头↔空头）：先收掉上一笔
        closing = (pos == 0 and pre_pos != 0) or \
                  (pos != 0 and pre_pos != 0 and pos != pre_pos)
        if closing and side is not None:
            out.append({
                'side': side,
                'entry_index': int(entry_i), 'exit_index': int(i),
                'entry_price': float(closes[entry_i]),
                'exit_price': float(closes[i]),
                'profit': float(cp - entry_cp),
            })
            side = None
        # 新开仓（从 0 或反手）
        if pos != 0 and pos != pre_pos:
            side = 'long' if pos > 0 else 'short'
            entry_i = i
            # 基准取**开仓前**一根的累计盈亏：`cum` 在开仓这根已经减过开仓
            # 手续费，用 `cum[i]` 会把这笔手续费抵消掉 ✗
            entry_cp = cum[i - 1] if i > 0 else cp
        pre_pos = pos
    return out


def draw_trade_lines(chart, strategy, contract: str, frame: pd.DataFrame,
                     pane: int, *, theme_dark: bool = True,
                     legend: bool = False, align: str = 'head') -> list:
    """在 `pane` 上画该合约的**入场→离场连线 + 四类交易标记**（复刻 bokeh）。

    * 连线：一笔交易一根虚线（深色主题黄 / 浅色主题蓝，宽 3）；
    * 多头入场/空头入场/多头离场/空头离场各自单独一条自定义序列，形状与尺寸
      都与**指标信号**区分开；
    * 与 ``_signal_payload`` 同理用 ``low``/``high`` 把入场/离场价纳入自动缩放。

    返回新建的序列列表（调用方负责在切页/重建时 ``delete()`` ✓）。
    """
    made: list = []
    if frame is None or getattr(frame, 'empty', True):
        return made
    trades = extract_trade_lines(
        strategy, _contract_index(strategy, contract), frame, align)
    if not trades:
        return made
    from minibt.utils import resolve_tradestyle
    style = resolve_tradestyle(strategy)
    if not getattr(style, 'show', True):
        return made
    times = list(frame['time'])
    n = len(times)
    line_color = _as_color(style.line_color) or (
        TRADE_LINE_DARK if theme_dark else TRADE_LINE_LIGHT)
    line_width = max(1, int(round(float(style.line_width or 3))))
    line_style = _trade_line_style(style.line_dash)
    # 1) 入场→离场虚线：一笔一条 polyline，挂在**入场那根**上
    line_rows = []
    for trade in trades:
        i, j = int(trade['entry_index']), int(trade['exit_index'])
        if not (0 <= i < n and 0 <= j < n):
            continue
        entry, exit_ = float(trade['entry_price']), float(trade['exit_price'])
        line_rows.append({
            'time': times[i], 'value': entry,
            'low': min(entry, exit_), 'high': max(entry, exit_),
            '_offset': j - i, '_exit_price': exit_,
        })
    if line_rows:
        df = pd.DataFrame(line_rows)

        def _line_shapes(row, _color=line_color, _width=line_width,
                         _style=line_style):
            return [shapes.polyline(
                [(0.0, float(row['value'])),
                 (float(row['_offset']), float(row['_exit_price']))],
                color=_color, width=_width, style=_style)]
        try:
            series = chart.add_custom_series(
                name='交易连线', pane_index=pane, legend=legend)
            series.set(df, shapes=_line_shapes)
            made.append(series)
        except Exception as error:
            print('[realtime] 画交易连线失败:', error)
    # 2) 四类交易标记（颜色/形状/大小从 TradeStyle 取 ✓）
    for key, side, is_entry in TRADE_MARKER_KEYS:
        marker_style = getattr(style, key, None)
        marker = str(getattr(marker_style, 'marker', 'circle') or 'circle')
        color = _as_color(getattr(marker_style, 'color', None), '#2196f3')
        size = _trade_marker_size(getattr(marker_style, 'size', 12))
        rows = []
        for trade in trades:
            if trade['side'] != side:
                continue
            idx = int(trade['entry_index'] if is_entry
                      else trade['exit_index'])
            if not (0 <= idx < n):
                continue
            rows.append({'time': times[idx],
                         'value': float(trade['entry_price'] if is_entry
                                        else trade['exit_price'])})
        if not rows:
            continue
        df = pd.DataFrame(rows)

        def _marker_shapes(row, _m=marker, _c=color, _s=size):
            return [shapes.marker(float(row['value']), marker=_m,
                                  size=_s, color=_c)]
        try:
            series = chart.add_custom_series(
                name=f'交易-{key}', pane_index=pane, legend=legend)
            series.set(df, shapes=_marker_shapes)
            made.append(series)
        except Exception as error:
            print('[realtime] 画交易标记失败:', error)
    return made


def _as_color(value, default=None):
    """minibt 的 Colors（str 枚举）/ 字符串 -> 普通颜色字符串。"""
    if value is None or value == '':
        return default
    return str(getattr(value, 'value', value))


def _rgba(color: str, alpha: float) -> str:
    """`#rrggbb` / `rgb(...)` -> `rgba(..., alpha)`（minibt 的 fill_alpha）。"""
    text = str(color).strip()
    if text.startswith('#') and len(text) in (4, 7):
        if len(text) == 4:
            text = '#' + ''.join(c * 2 for c in text[1:])
        red, green, blue = (int(text[i:i + 2], 16) for i in (1, 3, 5))
        return f'rgba({red}, {green}, {blue}, {alpha})'
    if text.startswith('rgb('):
        return text.replace('rgb(', 'rgba(').replace(')', f', {alpha})')
    return text


_SPAN_DASH = {
    'solid': 'solid', 'dashed': 'dashed', 'dotted': 'dotted',
    'large_dashed': 'large_dashed', 'sparse_dotted': 'sparse_dotted',
    'dotdash': 'dashed', 'dashdot': 'large_dashed',
}


def _iter_style(raw) -> list:
    """PlotInfo 里的样式集合：`SpanList`/`BandList`/单个对象/字典都行。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        return list(raw.values())
    vars_ = getattr(raw, 'vars', None)
    if isinstance(vars_, dict):
        return list(vars_.values())
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return [raw]


def extract_minibt_spans(strategy, plot_id: int) -> list:
    """从策略提取水平线（`PlotInfo.spanstyle`）。

    ``SpanStyle(location, line_color, line_dash, line_width)`` 里 `location` 是
    **价格**，所以直接画成全宽水平线（`horizontal_span(..., filled=False)`）。
    """
    spans: List[dict] = []
    for key, dataset in getattr(strategy, '_btindicatordataset', {}).items():
        try:
            if getattr(dataset, 'plot_id', plot_id) != plot_id:
                continue
            if getattr(dataset, 'iscandles', False):
                continue
            for item in _iter_style(dataset._get_plot_datas(key)[11]):
                location = _style_get(item, 'location', None)
                try:
                    price = float(location)
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(price):
                    continue
                dash = str(_style_get(item, 'line_dash', 'dashed'))
                spans.append({
                    'source': key,
                    'price': price,
                    'color': _as_color(_style_get(item, 'line_color', None),
                                       '#666666'),
                    'style': _SPAN_DASH.get(dash.strip().lower(), 'dashed'),
                    'width': max(1, int(_style_get(item, 'line_width', 1) or 1)),
                })
        except Exception as error:
            print(f'[realtime] 解析水平线 {key} 失败:', error)
    return spans


def extract_minibt_bands(strategy, plot_id: int) -> list:
    """从策略提取“两条线之间填充”（`PlotInfo.bandstyle`）。

    ``BandStyle(lower='ma5', upper='ma20', fill_color=..., fill_alpha=...)``
    里的 lower/upper 是**指标线名**（= 指标 DataFrame 的列名），所以填充要等
    指标线建好后再 `upper.fill_between(lower)`。
    """
    bands: List[dict] = []
    for key, dataset in getattr(strategy, '_btindicatordataset', {}).items():
        try:
            if getattr(dataset, 'plot_id', plot_id) != plot_id:
                continue
            if getattr(dataset, 'iscandles', False):
                continue
            for item in _iter_style(dataset._get_plot_datas(key)[13]):
                lower = _style_get(item, 'lower', None)
                upper = _style_get(item, 'upper', None)
                if not lower or not upper:
                    continue
                try:
                    alpha = float(_style_get(item, 'fill_alpha', 0.03))
                except (TypeError, ValueError):
                    alpha = 0.03
                color = _as_color(_style_get(item, 'fill_color', None),
                                  '#4682B4')
                bands.append({
                    'source': key, 'lower': str(lower), 'upper': str(upper),
                    'color': color,
                    'alpha': max(0.0, min(1.0, alpha)),
                    'line_color': _as_color(
                        _style_get(item, 'line_color', None), None),
                })
        except Exception as error:
            print(f'[realtime] 解析色带 {key} 失败:', error)
    return bands


def apply_spans(chart, spans, manager=None) -> list:
    """把 `SpanStyle` 画成全宽水平线，返回对象列表（便于清理）。"""
    made = []
    for span in spans or []:
        try:
            target = manager.pane_series(span['source']) if manager else None
            if target is None:
                target = chart
            made.append(target.horizontal_span(
                span['price'], filled=False, line_color=span['color'],
                line_width=span['width'], line_style=span['style']))
        except Exception as error:
            print('[realtime] 画水平线失败:', error)
    return made


def _call_table_rows(rows, dataset, index, live_values=None):
    """调用表格行生成器（`TableStyle.rows` 或指标类上的 `table_rows`）。

    * 普通函数：``rows(indicator)`` / ``rows(indicator, index)`` /
      ``rows(indicator, index, values)``；
    * **绑定方法**（`dataset.table_rows`，`self` 已绑定）：参数只是
      ``(index)`` / ``(index, values)`` / 空参；
    * ``values`` 是 ``{线名: 最新整条值}``（实盘每次重算指标对象，闭包里
      的 ``indicator`` 可能是旧的，读 ``values`` 才拿得到最新值）。
    """
    if not callable(rows):
        return rows
    bound = inspect.ismethod(rows)
    try:
        parameters = list(inspect.signature(rows).parameters.values())
        positional = [p for p in parameters
                      if p.kind in (p.POSITIONAL_ONLY,
                                    p.POSITIONAL_OR_KEYWORD)]
        variadic = any(p.kind == p.VAR_POSITIONAL for p in parameters)
        if bound:                      # self 已绑定，不再传 dataset
            if variadic or len(positional) >= 2:
                return rows(index, live_values)
            if positional:
                return rows(index)
            return rows()
        if variadic or len(positional) >= 3:
            return rows(dataset, index, live_values)
        if variadic or len(positional) >= 2:
            return rows(dataset, index)
    except (TypeError, ValueError):
        pass
    return rows(dataset)


def _align_table_values(values, length, align):
    """把一条线对齐到**当前图表长度**（回放用 head、实时用 tail）。

    回放时策略是在**全量**数据上跑的，指标值也是全量；表格不能取 ``[-1]``
    （那是最后一根，不是已放出的那根），要按图表长度切片。
    """
    values = np.asarray(values, dtype=float).ravel()
    length = int(length or 0)
    if length <= 0 or values.size == 0:
        return values
    if align == 'tail':
        if values.size >= length:
            return values[-length:]
        return np.concatenate([np.full(length - values.size, np.nan), values])
    if values.size >= length:
        return values[:length]
    return np.concatenate([values, np.full(length - values.size, np.nan)])


def _table_live_values(strategy, key, length=None,
                       align: str = 'head') -> Optional[dict]:
    """按 key 现取该指标**所有线**的最新整条值（含 ``isplot=False`` 的数据列）。

    和 `_indicator_values` 同一口径：每次从 ``strategy._btindicatordataset``
    重新取，避免拿到初始化时的旧值。``length`` / ``align`` 会把每条线对齐到
    当前图表长度（回放 = 前 N 根、实时 = 最后 N 根）。
    """
    if not key:
        return None
    try:
        dataset = (getattr(strategy, '_btindicatordataset', {}) or {}).get(key)
        if dataset is None:
            return None
        raw = dataset._get_plot_datas(key)[8]
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        arr = np.asarray(raw, dtype=float)
        names = list(getattr(dataset, 'lines', None) or [])
        if arr.ndim == 0 or arr.size == 0 or not names:
            return None
        if arr.ndim == 1:
            values = {str(names[0]): arr}
        else:
            values = {str(name): arr[:, i]
                      for i, name in enumerate(names) if i < arr.shape[1]}
        if length is not None:
            values = {name: _align_table_values(v, length, align)
                      for name, v in values.items()}
        return values
    except Exception:
        return None


def _table_headings(spec) -> tuple:
    """列名必须**互不相同**：``Row`` 是 ``{heading: value}`` 的 dict，
    两个空串会互相覆盖；没给 / 数量不对就自动用空白列名。"""
    rows = spec.get('rows') or []
    ncol = len(rows[0]) if rows and isinstance(rows[0], (list, tuple)) else 0
    provided = tuple(spec.get('headings') or ())
    if ncol and len(provided) == ncol and len(set(provided)) == ncol:
        return provided
    return tuple(' ' * (i + 1) for i in range(ncol))


def _fill_indicator_table(table, rows) -> None:
    """填表：行数/列名一致时**就地改单元格**（不 ``clear()``）。

    ``clear()`` + 逐行重建每一下都是一次 JS 调用，而且中间会先“空表”再逐行
    长出 —— 鼠标快速移动 / 回放推进时看着就是“一闪一闪”。就地
    ``row[heading] = value`` 只发 ``updateCell``，表格一直在线。
    """
    rows = [list(r) for r in (rows or ())]
    existing = list(table.values())
    headings = list(getattr(table, 'headings', ()) or ())
    if rows and len(existing) == len(rows) and headings \
            and all(len(r) == len(headings) for r in rows):
        try:
            for row, values in zip(existing, rows):
                if list(row.keys()) != headings:
                    raise ValueError('表格列变了')
                for heading, value in zip(headings, values):
                    row[heading] = value
            table.visible(True)
            return
        except Exception:
            pass
    table.clear()
    for row in rows:
        table.new_row(*row)
    table.visible(True)


def _apply_table_sections(table, spec) -> None:
    """写 header / footer（``table.header(1); table.header[0] = '...'``）。"""
    for which in ('header', 'footer'):
        text = spec.get(which)
        made = table.__dict__.setdefault('_made_sections', set())
        if text is None and which not in made:
            continue
        section = getattr(table, which, None)
        if section is None:
            continue
        if which not in made:
            section(1)
            made.add(which)
        try:
            section[0] = '' if text is None else str(text)
        except Exception:
            pass


def _indicator_table_times(chart):
    frame = getattr(chart, 'candle_data', None)
    if frame is None or 'time' not in getattr(frame, 'columns', ()):
        return None
    try:
        return np.asarray(frame['time'], dtype=np.int64)
    except Exception:
        return None


def _crosshair_index(chart, time_value):
    """十字光标时间（epoch 秒）-> 图表里的 bar 下标；不在 K 线上返回 None。

    **每次现取** `candle_data['time']`：回放/实时图表会不断变长，用缓存的话
    光标移到新放出的那几根上会找不到 -> 表格不跟随。
    """
    if not isinstance(time_value, (int, float)):
        return None
    times = _indicator_table_times(chart)
    if times is None or len(times) == 0:
        return None
    key = int(time_value)
    position = int(np.searchsorted(times, key))
    if 0 <= position < len(times) and int(times[position]) == key:
        return position
    return None


def apply_indicator_tables(chart, strategy, contract: str,
                           tables: Optional[dict] = None,
                           plot_id: Optional[int] = None,
                           align: str = 'head') -> dict:
    """把指标声明的统计表格画成 pylightcharts 浮动表格。

    ``align`` 与 `IndicatorManager.align` 同义：回放/'head' = 图表显示**前 N 根**
    （指标值要取 ``values[:N]``）、实时/'tail' = 最后 N 根 —— 表格的值必须
    按它对齐，否则回放里会一直显示**全量最后一根**的值（不跟 K 线走）。

    两种来源：

    * **指标类上的 ``TableStyle``**（推荐，像 ``LineStyle`` 一样写在类体里）
      -> ``PlotInfo.tablestyle``；``rows`` 是 ``[(标签, 值), ...]`` 或
      ``callable(indicator[, index]) -> rows``；
    * 旧机制：``kline._indicator_tables``（保持兼容）。

    * 默认 ``draggable=True``（可拖动），``margin_x`` / ``margin_y`` 控制离
      “该角最近的两条边”的间距，``header`` / ``footer`` 是表格标题 / 脚注；
    * ``live=True`` 时表格**跟随十字光标**：鼠标在哪根就显示哪根的值，移出
      K 线回到最后一根（用户自己的 crosshair 回调会被本函数覆盖，见
      :data:`_indicator_table_crosshair`）；
    * 同名表格**复用**（``clear()`` + 重填），指标不再声明则隐藏。

    目前只在 pylightcharts 引擎落地（bokeh 侧忽略）。
    """
    tables = dict(tables or {})
    specs: dict = {}

    # (A) 指标类上声明的 TableStyle -> PlotInfo.tablestyle
    for key, dataset in (getattr(strategy, '_btindicatordataset',
                                 {}) or {}).items():
        try:
            if (plot_id is not None
                    and getattr(dataset, 'plot_id', plot_id) != plot_id):
                continue
            plotinfo = getattr(dataset, '_plotinfo', None)
            style = (getattr(plotinfo, 'tablestyle', None)
                     if plotinfo is not None else None)
            if style is None or getattr(style, 'show', True) is False:
                continue
            rows = getattr(style, 'rows', None)
            if not rows:
                # 类上覆写的 `table_rows()`（已绑到 IndFrame；默认 None）；
                # 绑定方法，_call_table_rows 不会再传 dataset。
                rows = getattr(plotinfo, 'table_rows', None)
            specs[str(key)] = {
                'key': str(key),
                'position': getattr(style, 'position', 'top-right'),
                'headings': tuple(getattr(style, 'headings', ()) or ()),
                'rows': rows,
                'dataset': dataset,
                'live': bool(getattr(style, 'live', True)),
                'width': getattr(style, 'width', 0.24),
                'height': getattr(style, 'height', 0.24),
                'draggable': getattr(style, 'draggable', True),
                'margin_x': getattr(style, 'margin_x', 0) or 0,
                'margin_y': getattr(style, 'margin_y', 0) or 0,
                'header': getattr(style, 'header', None),
                'footer': getattr(style, 'footer', None),
                'background_color': getattr(style, 'background_color', None),
                'border_color': getattr(style, 'border_color', None),
            }
        except Exception as error:
            print(f'[realtime] 读取指标表格 {key} 失败:', error)

    # (B) 旧机制：KLine._indicator_tables
    for _, kline in (getattr(strategy, '_btklinedataset', {}) or {}).items():
        try:
            if f'{kline.symbol}_{kline.cycle}' != contract:
                continue
            for key, spec in (getattr(kline, '_indicator_tables',
                                      None) or {}).items():
                specs.setdefault(str(key), spec)
        except Exception:
            continue

    # 现在已经不声明的 -> 隐藏
    for key in list(tables):
        if key not in specs:
            try:
                tables[key].visible(False)
            except Exception:
                pass
            tables.pop(key, None)

    frame = getattr(chart, 'candle_data', None)
    frame_length = 0 if frame is None else len(frame)
    for key, spec in specs.items():
        try:
            live = _table_live_values(strategy, spec.get('key'),
                                      frame_length, align)
            rows = [list(r) for r in
                    (_call_table_rows(spec.get('rows'), spec.get('dataset'),
                                      None, live) or [])]
            if not rows:
                continue
            headings = _table_headings({**spec, 'rows': rows})
            table = tables.get(key)
            if table is None:
                options = dict(
                    width=spec.get('width', 0.24),
                    height=spec.get('height', 0.24),
                    headings=headings,
                    widths=tuple(spec.get('widths')
                                 or [1.0 / len(headings)] * len(headings)),
                    alignments=tuple(spec.get('alignments')
                                     or ['left'] * len(headings)),
                    position=str(spec.get('position', 'top-right')),
                    draggable=bool(spec.get('draggable', True)),
                    margin_x=spec.get('margin_x', 0) or 0,
                    margin_y=spec.get('margin_y', 0) or 0)
                if spec.get('background_color'):
                    options['background_color'] = spec['background_color']
                if spec.get('border_color'):
                    options['border_color'] = spec['border_color']
                table = chart.create_table(**options)
                tables[key] = table
            spec['table'] = table
            _fill_indicator_table(table, rows)
            _apply_table_sections(table, spec)
        except Exception as error:
            print(f'[realtime] 指标表格 {key} 绘制失败:', error)

    def refresh(index=None):
        """重填所有 ``live`` 表格（``index=None`` = 图表最后一根）。"""
        view = getattr(chart, 'candle_data', None)
        length = 0 if view is None else len(view)
        for key, spec in specs.items():
            table = spec.get('table')
            if table is None or not callable(spec.get('rows')):
                continue
            try:
                live = _table_live_values(strategy, spec.get('key'),
                                          length, align)
                rows = [list(r) for r in (_call_table_rows(
                    spec['rows'], spec.get('dataset'), index, live) or [])]
                if rows:
                    _fill_indicator_table(table, rows)
                    _apply_table_sections(table, spec)
            except Exception as error:
                print(f'[realtime] 指标表格 {key} 刷新失败:', error)

    chart._indicator_table_specs = specs
    chart._refresh_indicator_tables = refresh
    _wire_indicator_table_crosshair(chart, specs, refresh)
    return tables


def _wire_indicator_table_crosshair(chart, specs, refresh) -> None:
    """把 ``live`` 表格接到十字光标上（移出 K 线回最后一根）。"""
    emitter = getattr(getattr(chart, 'events', None), 'crosshair_move', None)
    if emitter is None:
        return
    live = any(spec.get('live') and callable(spec.get('rows'))
               and spec.get('table') is not None for spec in specs.values())
    if not live:
        if getattr(chart, '_indicator_table_crosshair', None) is not None:
            try:
                emitter.unsubscribe()
            except Exception:
                pass
            chart._indicator_table_crosshair = None
        return

    chart._indicator_table_times = _indicator_table_times(chart)

    def handler(_chart, time_value, price):
        index = _crosshair_index(chart, time_value)
        chart._indicator_table_cursor = index
        refresh(index)

    chart.events.crosshair_move += handler
    chart._indicator_table_crosshair = handler


def apply_bands(manager, bands) -> None:
    """把色带画到对应指标线上（`upper.fill_between(lower)`）。"""
    if not bands or manager is None:
        return
    for band in bands:
        try:
            upper = manager.series_named(band['source'], band['upper'])
            lower = manager.series_named(band['source'], band['lower'])
            if upper is None or lower is None:
                if _DEBUG:
                    print(f"[realtime] 色带 {band['lower']}~"
                          f"{band['upper']} 找不到指标线，跳过")
                continue
            options = {'color': band['color'], 'opacity': band.get('alpha')}
            if band['line_color']:
                options['line_color'] = band['line_color']
            upper.fill_between(lower, **options)
        except Exception as error:
            print(f"[realtime] 画色带 {band['lower']}~{band['upper']} 失败:",
                  error)


def extract_minibt_candlestyle(strategy, contract: str) -> dict:
    """从 K 线数据集取蜡烛涨跌配色（`PlotInfo.candlestyle`）。

    minibt 只在用户**显式设过** ``bull_color`` / ``bear_color`` 时才置
    ``_light_chart_candles_{up,down}_color``，所以这里是“策略说要什么颜色”的
    准确信号；没设过的部分返回空字典，交给主题默认值。
    """
    for _, kline in (getattr(strategy, '_btklinedataset', {}) or {}).items():
        try:
            if f'{kline.symbol}_{kline.cycle}' != contract:
                continue
            style = {}
            if getattr(kline, '_light_chart_candles_up_color', False):
                color = _as_color(getattr(kline, 'bull_color', None), None)
                if color:
                    style['up'] = color
            if getattr(kline, '_light_chart_candles_down_color', False):
                color = _as_color(getattr(kline, 'bear_color', None), None)
                if color:
                    style['down'] = color
            if _DEBUG and style:
                print(f'[realtime] candlestyle {contract}: {style}')
            return style
        except Exception as error:
            print(f'[realtime] 解析 K 线配色失败: {error}')
    return {}


def apply_candlestyle(chart, style) -> None:
    """把策略的涨跌配色应用到主图蜡烛（没指定的部分保持主题色）。"""
    if not style:
        return
    options = {}
    up, down = style.get('up'), style.get('down')
    if up:
        options.update(up_color=up, wick_up_color=up, border_up_color=up)
    if down:
        options.update(down_color=down, wick_down_color=down,
                       border_down_color=down)
    if not options:
        return
    try:
        chart.candle_style(**options)
    except Exception as error:
        print('[realtime] 应用 K 线配色失败:', error)


def strategy_candle_colors(strategy, contract: str) -> tuple:
    """策略给 K 线的涨跌配色 -> ``(up, down)``。

    优先级：显式 `bull_color`/`bear_color`（`extract_minibt_candlestyle`）
    > K 线 `color_by(...)` 规则里的 `color`/`else_color` > 默认色。旧 Qt 版
    用 `btcolors.bull_color/bear_color`，新版策略常用 `color_by`，这里都兼容。
    """
    style = extract_minibt_candlestyle(strategy, contract)
    up, down = style.get('up'), style.get('down')
    for _, kline in (getattr(strategy, '_btklinedataset', {}) or {}).items():
        try:
            if f'{kline.symbol}_{kline.cycle}' != contract:
                continue
            rules = list(kline.__dict__.get('_color_rules') or [])
            plotinfo = getattr(kline, '_plotinfo', None)
            for rule in getattr(plotinfo, 'color_rules', []) or []:
                if rule not in rules:
                    rules.append(rule)
        except Exception:
            continue
        for rule in rules:
            color = _as_color(rule.get('color'), None)
            else_color = _as_color(rule.get('else_color'), None)
            if color and not up:
                up = color
            if else_color and not down:
                down = else_color
        break
    return up, down


def apply_histogram_colors(manager, strategy, contract: str,
                           rows: int, frame=None) -> None:
    """柱状指标（`LineDash.vbar`）按“值 vs `vbar_base`”上下行分色。

    颜色优先级（`LineStyle`）：

    1. `vbar_up_color` / `vbar_down_color`（显式上下行色，最高）
    2. `line_color`（显式单色 -> 整段单色，不分上下行）
    3. 跟主图 K 线涨/跌色（`candlestyle` / `color_by`）
    4. pylightcharts 默认涨/跌色（``#26a69a`` / ``#ef5350``）

    分界位置用 `vbar_color_base`（未设则跟随柱底 `vbar_base`，再默认 0）。
    颜色落到 pylightcharts 的 `color_by`（给每个点加 `color` 列，LWC 的
    Histogram 自己取用）；同时把色记到 spec，供 `update_last` 单点更新一起发。
    """
    specs = [(s, sp) for s, sp in zip(manager.series, manager.specs)
             if sp.kind == 'Histogram']
    if not specs:
        return
    up, down = strategy_candle_colors(strategy, contract)
    up = up or '#26a69a'
    down = down or '#ef5350'
    for series, spec in specs:
        explicit_up = _as_color(getattr(spec, 'vbar_up_color', None), None)
        explicit_down = _as_color(getattr(spec, 'vbar_down_color', None), None)
        # 只有**策略里显式写的** `line_color` 才当单色；`build()` 自动分配的
        # 调色板色不算（否则所有柱体都会被当成单色，上下行配色全失效——
        # 这个 bug 曾让 macd / rsi 的柱子都变成一种颜色）
        single = (None if getattr(spec, 'palette_color', False)
                  else _as_color(getattr(spec, 'color', None), None))
        # 显式 `line_color` 且没给上下行色 -> 单色（用系列自身颜色，不再逐点配色）
        if single and not (explicit_up or explicit_down):
            spec.up_color = spec.down_color = single
            continue
        spec_up = explicit_up or up
        spec_down = explicit_down or down
        try:
            if frame is not None and hasattr(manager, 'view_values'):
                values = manager.view_values(spec, frame)
            elif hasattr(manager, 'spec_values'):
                values = manager.spec_values(spec, rows)
            elif spec.values is not None:
                values = np.asarray(spec.values, dtype=float).ravel()[:rows]
            else:
                values = None
            if values is None:
                continue
            values = np.asarray(values, dtype=float).ravel()
            base = 0.0 if spec.vbar_base is None else float(spec.vbar_base)
            color_base = getattr(spec, 'vbar_color_base', None)
            color_base = (base if color_base is None
                          else float(color_base))
            spec.up_color, spec.down_color = spec_up, spec_down
            series.color_by(pd.Series(values > color_base),
                            spec_up, else_color=spec_down)
        except Exception as error:
            print(f'[realtime] 柱状图配色 {spec.name} 失败:', error)


#: 已经提醒过的 color_by 失败信息（同一条只提醒一次，避免实时刷屏）
_COLOR_BY_WARNED: set = set()


def apply_color_rules(chart, manager, strategy, contract: str) -> None:
    """把 `KLine.color_by(...)` / `IndSeries.color_by(...)` 的规则落到图上。

    * K 线（`_btklinedataset` 的 PlotInfo）-> `chart.color_by(...)`：整根变色；
    * 指标线（`_btindicatordataset` 的 PlotInfo）-> 对应序列的 `color_by`。

    规则里的条件若是数组/Series（策略里通常是全量的），会按“图表当前长度”
    从头部切片——与指标值的切法一致（回放时逐步放出、实时时同步增长）。
    """
    def make_condition(rule):
        condition = rule.get('condition')
        # 注意：minibt 的 Series/指标定义了 __call__，所以 callable() 会是 True，
        # 必须先按"数据"判断，否则会被当成回调去调用。
        if isinstance(condition, (pd.Series, pd.Index, np.ndarray,
                                  list, tuple)):
            mask = np.asarray(condition).ravel()
            if getattr(manager, 'align', 'head') == 'tail':
                def _tail(frame, _m=mask):
                    """条件覆盖**最后 len(mask) 根**（实盘策略只留 preload 根）。

                    图可能因为「无限历史」向左多加载了老 K 线（401/601 根），
                    这时按**尾部时间**对齐；没被条件覆盖的老 K 线交回给调用方
                    处理（见 `apply`：把它们排除在 `else_color` 之外，保持原色）。
                    """
                    n = len(frame)
                    if len(_m) >= n:
                        return pd.Series(_m[-n:])
                    if 'time' not in getattr(frame, 'columns', ()):
                        return pd.Series(_m)
                    times = np.asarray(frame['time'])[-len(_m):]
                    return pd.Series(_m, index=times)

                # 告诉 `apply`：前 k 根是“没被条件覆盖的老 K 线”
                _tail.uncovered = lambda n, _c=len(mask): max(0, n - _c)
                return _tail
            offset = int(getattr(manager, 'offset', 0) or 0)
            return lambda frame, _m=mask, _o=offset: pd.Series(
                _m[_o:_o + len(frame)])
        if callable(condition):
            return condition
        return None

    def rules_of(obj):
        found = list(obj.__dict__.get('_color_rules') or [])
        try:
            for rule in getattr(obj._plotinfo, 'color_rules', []):
                if rule not in found:
                    found.append(rule)
        except Exception:
            pass
        return found

    def apply(target, rule, label):
        condition = make_condition(rule)
        if condition is None:
            return
        else_color = rule.get('else_color')
        # 「无限历史」向左多加载的老 K 线不在条件覆盖范围内：应该**不套色**
        # （沿用 K 线自己的颜色），否则它们会被 `else_color` 统一刷成另一种色
        # （cci 就是整段变红）。`else_color` 支持逐行数组，所以未覆盖那段给
        # None、覆盖那段给配置的颜色。
        uncovered_of = getattr(condition, 'uncovered', None)
        if uncovered_of is not None and isinstance(else_color, str):
            try:
                total = len(target._color_frame())
            except Exception:
                total = 0
            head = int(uncovered_of(total))
            if total and head > 0:
                else_color = [None] * head + [else_color] * (total - head)
        try:
            target.color_by(condition, rule.get('color'), else_color,
                            **rule.get('options', {}))
        except Exception as error:
            # 同一条错只提醒一次：实时每根行情都会重算，否则会刷屏
            key = f'{label}:{error}'
            if key not in _COLOR_BY_WARNED:
                _COLOR_BY_WARNED.add(key)
                print(f'[realtime] {label} color_by 失败:', error)

    for _, kline in (getattr(strategy, '_btklinedataset', {}) or {}).items():
        try:
            if f'{kline.symbol}_{kline.cycle}' != contract:
                continue
            rules = rules_of(kline)
        except Exception:
            continue
        for rule in rules:
            apply(chart, rule, 'K 线')
        break

    datasets = getattr(strategy, '_btindicatordataset', {}) or {}
    for series, spec in zip(manager.series, manager.specs):
        dataset = datasets.get(getattr(spec, 'source', ''))
        if dataset is None:
            continue
        rules = rules_of(dataset)
        conditions = []
        for rule in rules:
            raw = rule.get('condition')
            if isinstance(raw, (pd.Series, pd.Index, np.ndarray,
                                list, tuple)):
                conditions.append((np.asarray(raw).ravel(),
                                   _as_color(rule.get('color'), None),
                                   _as_color(rule.get('else_color'), None)))
            apply(series, rule, f'指标 {spec.name}')
        # 记下来，实时更新单点时重算该点颜色（否则最后一点会掉色）
        spec.color_conditions = conditions


def strategy_watermark(strategy, contract: str) -> Optional[str]:
    """当前合约要画的水印文本（``None`` = 不画）。

    * ``config.watermark = False``（策略里可写 ``self.config.watermark = False``，
      见 :func:`minibt.utils.resolve_watermark`）：**全部合约都不画水印**；
    * 否则：策略里给该 K 线显式设过 ``.watermark = '文本'``（或
      ``WaterMark(...)``）就用它的文本；没设过则自动用**合约自身名称**
      （等价于 ``self.data.watermark = f"{self.data.symbol}"``）。
    """
    from ..utils import resolve_watermark
    if not resolve_watermark(strategy):
        return None
    for v in getattr(strategy, '_btklinedataset', {}).values():
        if f'{v.symbol}_{v.cycle}' != contract:
            continue
        flag = getattr(v, 'watermark', None)
        if isinstance(flag, str) and flag:
            return flag                      # 显式设的文本
        vars_ = getattr(flag, 'vars', None)
        if isinstance(vars_, dict):
            text = vars_.get('text') or vars_.get('content')
            if text:
                return str(text)
        return f'{getattr(v, "symbol", "") or getattr(v, "sname", "")}'
    return None


def apply_watermark(chart, text: Optional[str]) -> None:
    """给图表加水印（失败不影响图表）。"""
    if not text:
        return
    try:
        chart.watermark(text)
    except Exception as error:
        print('[realtime] 水印失败:', error)


def strategy_contracts(strategy) -> List[str]:
    """策略的 K 线列表：``[{symbol}_{cycle}, ...]``（按 `_btklinedataset` 顺序）。"""
    return [f'{v.symbol}_{v.cycle}'
            for v in getattr(strategy, '_btklinedataset', {}).values()]


def strategy_preload(strategy, contract: str) -> Optional[int]:
    """开局先放出多少根：取 `get_kline` 里记下的 preload；没显式设置返回 None
    （回测/未启用无限历史时表示“全部一次显示”）。"""
    for v in (getattr(strategy, '_btklinedataset', {}) or {}).values():
        if f'{v.symbol}_{v.cycle}' != contract:
            continue
        try:
            value = int(getattr(v, '_preload', None))
            if value >= 10:
                return value
        except (TypeError, ValueError):
            pass
    return None


def strategy_kline_frame(strategy, contract: str,
                         tail: Optional[int] = None) -> pd.DataFrame:
    """取某个 K 线（``{symbol}_{cycle}``）的 OHLCV 表。

    数据源优先 ``v._dataset.tq_object``（实盘 = 天勤 DataFrame）；拿不到时退回
    ``v.pandas_object`` / ``v.kline_object``（回测/其它模式）。

    ``tail``：只要最后这几根（tick 每轮都推图时用）—— 先切尾再 copy/转时间，
    避免每个 tick 都对整段 ``datetime`` 做一次 ``pd.to_datetime``。
    """
    for v in getattr(strategy, '_btklinedataset', {}).values():
        if f'{v.symbol}_{v.cycle}' != contract:
            continue
        df = None
        for source in (getattr(v, '_dataset', None), v):
            for attr in ('tq_object', 'pandas_object', 'kline_object'):
                obj = getattr(source, attr, None)
                if isinstance(obj, pd.DataFrame):
                    if tail and len(obj) > int(tail):
                        obj = obj.iloc[-int(tail):]      # 先切尾再 copy
                    df = obj.copy()
                    break
            if df is not None:
                break
        if df is None:
            return pd.DataFrame()
        if 'datetime' in df.columns and 'time' not in df.columns:
            df['time'] = pd.to_datetime(df['datetime'])
        cols = [c for c in _KLINE_COLS if c in df.columns]
        return df[cols].reset_index(drop=True)
    return pd.DataFrame()


def _as_list(value, n: int) -> List[bool]:
    if isinstance(value, dict):               # isplot 有时是 {线名: bool}
        values = [bool(v) for v in value.values()]
        return values + [True] * max(0, n - len(values))
    if isinstance(value, (list, tuple)):
        values = [bool(v) for v in value]
        return values + [True] * max(0, n - len(values))
    return [bool(value)] * n


def _values(indicator, col: str, line_idx: int, n_lines: int):
    """从 `v.pandas_object` 取一条线的值。

    兼容：多列 DataFrame（按列名取）、单列 DataFrame（按位置取）、
    Series / 1-D 数组（一维指标）、2-D 数组（按列位置取）。
    """
    if indicator is None:
        return None
    if isinstance(indicator, pd.DataFrame):
        if col in indicator.columns:
            return indicator[col]
        if n_lines == 1 and indicator.shape[1] >= 1:
            return indicator.iloc[:, 0]
        if line_idx < indicator.shape[1]:
            return indicator.iloc[:, line_idx]
        return None
    try:
        arr = np.asarray(indicator)
    except Exception:
        return None
    if arr.ndim == 1:
        return pd.Series(arr) if line_idx == 0 else None
    if arr.ndim == 2 and line_idx < arr.shape[1]:
        return pd.Series(arr[:, line_idx])
    return None


def _indicator_values(strategy, key: str, group_idx: int, line_idx: int):
    """`get_values`：实时更新时**按 key 现取**该指标线的最新整条值。

    和 `light_chart.py::indicator_update` 一样，从 `_get_plot_datas(k)` 的
    `indicators`（即当前 `v.values`）取，而不是读可能已缓存的 `pandas_object`。
    关键：实盘每次跑策略都会重建指标对象（`_btindicatordataset[key]` 被替换），
    所以这里必须每次从 `strategy._btindicatordataset` 重新取，不能把 dataset
    实例闭包进来，否则永远拿到初始化时的旧值。
    """
    def get_values():
        try:
            datasets = getattr(strategy, '_btindicatordataset', None) or {}
            dataset = datasets.get(key)
            if dataset is None:
                return None
            indicators = dataset._get_plot_datas(key)[8]
            if isinstance(indicators, (list, tuple)):
                arr = indicators[group_idx]
            else:
                arr = indicators
            arr = np.asarray(arr, dtype=float)
            if arr.ndim == 0 or len(arr) == 0:
                return None
            if arr.ndim == 1:
                return arr
            if line_idx < arr.shape[1]:
                return arr[:, line_idx]
            return None
        except Exception:
            return None
    return get_values


def _indicator_last(strategy, key: str, group_idx: int, line_idx: int):
    """`get_last`：实时更新时重取该指标线的最新值（兼容旧接口）。"""
    get_values = _indicator_values(strategy, key, group_idx, line_idx)

    def get_last():
        values = get_values()
        if values is None or not len(values):
            return float('nan')
        return float(values[-1])
    return get_last


def _spec_point_color(spec: 'IndicatorSpec', value: float, n: int,
                      idx: int, base: int = 0) -> Optional[str]:
    """实时更新单点时，算出该点的颜色（柱状图 / `color_by` 规则）。

    * Histogram：值 > 基线用 `up_color`，否则 `down_color`；
    * 有 `color_by` 规则：按顺序叠加，规则命中取规则色，否则若给了
      `else_color` 则取它（与 pylightcharts `color_by` 语义一致）。

    :param n: 当前值的根数（保留兼容，不再据此定位）
    :param idx: 位置（负数从末尾算）
    :param base: 保留兼容

    实盘策略只算 ``preload`` 根，而条件数组是全量的；两边都是**尾部对齐**，
    所以直接用负索引 `mask[idx]` 取（`idx=-1` 就是最后一根）。
    """
    if spec.kind == 'Histogram':
        base = getattr(spec, 'vbar_color_base', None)
        base = spec.vbar_base if base is None else base
        vbase = 0.0 if base is None else float(base)
        return spec.up_color if value > vbase else spec.down_color
    color = None
    for mask, rule_color, else_color in spec.color_conditions or []:
        try:
            flag = bool(mask[idx])
        except Exception:
            continue
        if flag:
            color = rule_color
        elif else_color is not None:
            color = else_color
    return color


def _ohlc_getter(strategy, key, ohlc):
    """``get_ohlc``：按 key 现取该蜡烛指标的 OHLC（DataFrame）。"""
    def get_ohlc():
        try:
            dataset = (getattr(strategy, '_btindicatordataset', None)
                       or {}).get(key)
            if dataset is None:
                return None
            data = getattr(dataset, 'pandas_object', None)
            if data is None or not set(ohlc.values()) <= set(data.columns):
                return None
            return pd.DataFrame({'open': data[ohlc['open']],
                                 'high': data[ohlc['high']],
                                 'low': data[ohlc['low']],
                                 'close': data[ohlc['close']]})
        except Exception:
            return None
    return get_ohlc


def _candle_specs(strategy, key, dataset) -> List[IndicatorSpec]:
    """蜡烛型指标（``category='candles'``）→ 一条 ``Candlestick`` + 其余线。

    之前这里直接 ``continue``（"蜡烛图类指标先跳过"），所以 pylightcharts 下**蜡烛
    副图整条不画**；而 bokeh 侧是能画的。现在：

    * 前 4 条（open/high/low/close）出一条 ``kind='Candlestick'``；
    * 同一指标里其余可画线（如 CDV 的 ``ctl`` / ``sma1`` / ``sma2`` / ``ema1`` /
      ``ema2``）按**同一个 ``overlap`` 与 ``source``** 追加 —— 副图蜡烛时它们靠
      ``source`` 落到**同一个副图**（``IndicatorManager.build`` 按 source 分 pane）。
    """
    try:
        data = dataset._get_plot_datas(key)
    except Exception as error:
        print(f'[realtime] 解析蜡烛指标 {key} 失败:', error)
        return []
    (pid, isplot, name, lines, _lines, ind_names, overlaps, categorys,
     indicator, doubles, plotinfo, span, signal) = data[:13]
    frame = getattr(dataset, 'pandas_object', None)
    if frame is None:
        return []
    low = [str(x).lower() for x in _lines]
    try:
        oi, hi, li, ci = (low.index('open'), low.index('high'),
                          low.index('low'), low.index('close'))
    except ValueError:
        return []
    idx = {'open': oi, 'high': hi, 'low': li, 'close': ci}
    ohlc_cols = {k: _lines[i] for k, i in idx.items()}
    # `ohlc` 存**值**（Series，便于 build 时按 frame 长度对齐）；
    # `ohlc_cols`（列名）交给 get_ohlc 用于实时重取。
    ohlc = {k: _values(frame, _lines[i], i, len(_lines))
            for k, i in idx.items()}
    overlap = bool(overlaps)
    info = (plotinfo or {}).get('linestyle', {}) if plotinfo else {}
    specs = [IndicatorSpec(
        name=ind_names or name, overlap=overlap, kind='Candlestick',
        ohlc=ohlc, source=key,
        candlestyle=(plotinfo or {}).get('candlestyle', None),
        legend=bool((plotinfo or {}).get('legend', True)),
        get_ohlc=_ohlc_getter(strategy, key, ohlc_cols))]
    for i, col in enumerate(_lines):
        if i in (oi, hi, li, ci):
            continue
        # ★ 不能再按 ``isplot[i]`` 跳过：CDV 这类指标的 ``ctl`` / 均线是**分模式**
        #   出现的（蜡烛模式：均线有数据、``ctl`` 全 NaN；线模式反之），而
        #   ``isplot`` 是静态类属性，表达不了“哪个模式”。以前直接 ``not isplot[i]``
        #   跳过，线模式的 ``ctl`` 就永远不画 ✗。现在改成“**有数据就画**”：
        #   全 NaN 的（另一种模式的线）标 ``hidden``，不画也不占图例 ✓。
        values = _values(frame, col, i, len(_lines))
        try:
            has_data = bool(np.isfinite(
                np.asarray(values, dtype=float)).any())
        except Exception:                                        # noqa: BLE001
            has_data = True
        st = info.get(col) or {}
        style = st.get('line_dash', 'solid')
        specs.append(IndicatorSpec(
            name=col, overlap=overlap,
            values=values,
            color=st.get('line_color'),
            line_width=int(st.get('line_width', 1) or 1),
            line_style=style,
            kind='Histogram' if style == 'vbar' else 'Line',
            source=key,
            hidden=not has_data,
            get_last=_indicator_last(strategy, key, 0, i),
            get_values=_indicator_values(strategy, key, 0, i)))
    return specs


def _stop_line_dataset_ids(strategy) -> set:
    """`_btindicatordataset` 里哪些对象其实是 K 线的 `stop_lines`。

    ``base.py`` 把每条 K 线的止损线都登记在**同一个 key**（``'stop_lines'``）
    下，多合约时后一个合约会直接覆盖前一个 ✗。所以这些对象不能按
    `_btindicatordataset` 遍历（拿到的永远是最后一个合约），要按合约从
    `_btklinedataset` 现取 —— 这里先把它们的 id 记下来，遍历时跳过。
    """
    ids: set = set()
    for kline in (getattr(strategy, '_btklinedataset', {}) or {}).values():
        setting = getattr(kline, '_klinesetting', None)
        frame = getattr(setting, 'stop_lines', None) if setting is not None else None
        if frame is not None:
            ids.add(id(frame))
    return ids


def _extract_stop_line_specs(strategy, plot_id: int) -> List['IndicatorSpec']:
    """某一合约的止损/止盈线（`KLine.stop_lines` 的 stop_price/target_price）。

    * 直接按 `plot_id` 从 `_btklinedataset` 取，避免多合约互相覆盖；
    * **全 NaN 的线直接跳过**（如某合约没触发过止损/止盈）——用户要求“全 nan
      可以不显示”，否则空的序列会拖着一个图例行和自动缩放的空区间。
    """
    klines = list((getattr(strategy, '_btklinedataset', {}) or {}).values())
    if plot_id < 0 or plot_id >= len(klines):
        return []
    setting = getattr(klines[plot_id], '_klinesetting', None)
    frame = getattr(setting, 'stop_lines', None) if setting is not None else None
    if frame is None:
        return []
    try:
        data = frame._get_plot_datas('stop_lines')
    except Exception as error:                 # 数据还没建好 / 结构异常
        print('[realtime] 解析止损线失败:', error)
        return []
    (pid, isplot, name, lines, _lines, ind_names, overlaps, categorys,
     indicators, doubles, plotinfo, span, signal) = data[:13]
    plots = _as_list(isplot, len(_lines))
    if not any(plots):
        return []
    indicator = getattr(frame, 'pandas_object', None)
    lineinfo = (plotinfo or {}).get('linestyle', {}) if plotinfo else {}
    overlap = (bool(overlaps) if not isinstance(overlaps, (list, tuple))
               else bool(overlaps[-1]))
    specs: List[IndicatorSpec] = []
    for line_idx, col in enumerate(_lines):
        if line_idx >= len(plots) or not plots[line_idx]:
            continue
        values = _values(indicator, col, line_idx, len(_lines))
        if values is None:
            continue
        arr = np.asarray(values, dtype=float)
        if not np.isfinite(arr).any():
            continue                            # 全 NaN：不显示
        info = (lineinfo.get(col) or {}) if lineinfo else {}
        style = info.get('line_dash', 'solid')

        def _get_values(_frame=frame, _col=col, _idx=line_idx,
                        _n=len(_lines)):
            """实时重取**这个合约**的止损线（不是被覆盖的那个）。"""
            obj = getattr(_frame, 'pandas_object', None)
            found = _values(obj, _col, _idx, _n)
            return None if found is None else np.asarray(found, dtype=float)

        def _get_last(_get=_get_values):
            whole = _get()
            if whole is None or not len(whole):
                return float('nan')
            return float(whole[-1])

        specs.append(IndicatorSpec(
            name=col, overlap=overlap, values=values,
            color=info.get('line_color'),
            line_width=int(info.get('line_width', 2) or 2),
            line_style=style,
            price_line=bool(info.get('price_line', False)),
            price_label=bool(info.get('price_label', False)),
            kind='Histogram' if style == 'vbar' else 'Line',
            source='stop_lines',
            get_values=_get_values, get_last=_get_last,
            legend=indicator_legend_visible(strategy, 'stop_lines')))
    return specs


def _pane_zorder(spec) -> int:
    """同一 pane 内的绘制层级：**小的先建 = 在底层** ✓。

    LWC 里同一 pane 的序列是**按创建顺序**堆的（先建的在下层 ✗）—— 所以
    「柱体线 + 指标线」混合的副图（macd 这类）必须**先建柱体** ✓，否则柱体会
    把先建的指标线盖住（用户反馈：macdh 挡住了 macdx ✗）。
    """
    kind = str(getattr(spec, 'kind', 'Line') or 'Line')
    if kind == 'Histogram':
        return 0                      # 柱体在最底层 ✓
    return 1                          # 指标线 / 蜡烛 / 其它都压在上面 ✓


def extract_minibt_indicators(strategy, plot_id: int) -> List[IndicatorSpec]:
    """从策略提取某合约(`plot_id`)的指标线；`overlaps` 决定主/副图。

    与 `light_chart.py::_init_chart_data` 一致：
    * ``_get_plot_datas(k)`` -> ``(... lines, _lines, ind_names, overlaps,
      ... _ind_plotinfo ...)``；
    * 线条数值在 ``v.pandas_object[col]``（col 取自 ``_lines``）；
    * ``_ind_plotinfo['linestyle'][col]['line_dash'] == 'vbar'`` -> 柱状图。

    止损/止盈线单独按合约从 `_btklinedataset` 取（见
    :func:`_extract_stop_line_specs`），不走上面的遍历。
    """
    specs: List[IndicatorSpec] = []
    stop_ids = _stop_line_dataset_ids(strategy)
    for key, dataset in getattr(strategy, '_btindicatordataset', {}).items():
        try:
            if getattr(dataset, 'plot_id', plot_id) != plot_id:
                continue
            if id(dataset) in stop_ids:
                continue                        # 止损线按合约单独取（防覆盖）
            if getattr(dataset, 'iscandles', False):
                # 蜡烛型指标：出蜡烛副图（或主图叠加）+ 同图其它线，不再直接跳过
                specs.extend(_candle_specs(strategy, key, dataset))
                continue
            (pid, isplot, name, lines, _lines, ind_names, overlaps, categorys,
             indicators, doubles, plotinfo, span, signal) = \
                dataset._get_plot_datas(key)[:13]
            indicator = getattr(dataset, 'pandas_object', None)
            lineinfo = (plotinfo or {}).get('linestyle', {}) if plotinfo else {}
            if _DEBUG:
                print(f'[realtime] {key}: plot_id={pid} isplot={isplot} '
                      f'overlaps={overlaps} lines={_lines} doubles={doubles} '
                      f'obj={type(indicator).__name__} '
                      f'cols={list(getattr(indicator, "columns", []))}')

            _is_vp = str(categorys) == 'vp'
            if _is_vp:
                # 滚动 Volume Profile：`vp0..vpN` 是**等宽价格档**的分布矩阵（不
                # 是普通指标线），走单独的 `CustomSeries` 渲染分支。
                # VolumeProfileMA 的 `ma` 列 `isplot=True`，下面仍按普通线建；
                # RollingVolumeProfile 的 `isplot` 全 False，下面会 continue。
                _vpspec = _vp_spec(strategy, key, indicator, overlaps, lineinfo,
                                   plotinfo)
                if _vpspec is not None:
                    specs.append(_vpspec)

            if doubles:                        # 多分组：部分主图、部分副图
                for group_idx in range(2):
                    group_lines = _lines[group_idx]
                    if not any(_as_list(isplot[group_idx], len(group_lines))):
                        continue
                    _append_group(specs, strategy, key, group_idx, indicator,
                                  lineinfo, bool(overlaps[group_idx]),
                                  group_lines,
                                  _as_list(isplot[group_idx], len(group_lines)))
            else:                              # 单分组
                if not any(_as_list(isplot, len(_lines))):
                    continue
                _append_group(specs, strategy, key, 0, indicator, lineinfo,
                              bool(overlaps), _lines,
                              _as_list(isplot, len(_lines)))
            # ---- 色带的上下沿（常常是 isplot=False 的“仅数据”列）也要建**隐藏**
            #   序列，否则 `apply_bands` 的 `fill_between` 找不到 series（这正是
            #   HemaTrendLevels 的支撑/阻力带、均线渐变带看不见的原因）。
            try:
                _bands = _iter_style(dataset._get_plot_datas(key)[13])
            except Exception:
                _bands = []
            if _bands:
                # 逐线 overlap 的指标（如 MarketStructureOscillator：结构走主图、
                # 振荡器 + 色带走副图）要按**所在分组**的 overlap 来建，
                # 不能直接用 `bool(overlaps)`（列表恒为真 -> 色带会被画到主图、
                # 把 K 线压扁）。
                _grp: dict = {}
                if doubles:
                    for _gi, _grp_lines in enumerate(_lines):
                        _ov = (bool(overlaps[_gi])
                               if isinstance(overlaps, (list, tuple))
                               else bool(overlaps))
                        for _c in _grp_lines:
                            _grp[str(_c)] = _ov
                _default_ov = (bool(overlaps) if not isinstance(
                    overlaps, (list, tuple)) else bool(overlaps[-1]))
                _have = {s.name for s in specs
                         if getattr(s, 'source', '') == key}
                for item in _bands:
                    for _col in (_style_get(item, 'lower', None),
                                 _style_get(item, 'upper', None)):
                        _col = str(_col or '')
                        if not _col or _col in _have:
                            continue
                        _have.add(_col)
                        specs.append(IndicatorSpec(
                            name=_col,
                            overlap=_grp.get(_col, _default_ov),
                            values=_values(indicator, _col, 0, 1),
                            source=key, hidden=True))
        except Exception as error:
            print(f'[realtime] 解析指标 {key} 失败:', error)
    # 最后补上该合约自己的止损/止盈线（全 NaN 的已跳过）
    specs.extend(_extract_stop_line_specs(strategy, plot_id))
    return specs


def _indicator_profile(strategy, key):
    """按指标 key 现取 VP 分布矩阵（列 = 价格档）。

    与 `_indicator_values` 同理：实盘里策略每次 `__call__` 都会重建指标对象并
    覆盖 `_btindicatordataset[key]`，捕获 dataset 实例会拿到旧对象。
    """
    def _get() -> Optional[np.ndarray]:
        dataset = getattr(strategy, '_btindicatordataset', {}).get(key)
        obj = getattr(dataset, 'pandas_object', None)
        if obj is None or not hasattr(obj, 'columns'):
            return None
        cols = [c for c in obj.columns
                if str(c).startswith('vp') and str(c)[2:].isdigit()]
        if not cols:
            return None
        cols.sort(key=lambda c: int(str(c)[2:]))
        try:
            return np.asarray(obj[cols].values, dtype=np.float64)
        except Exception:
            return None
    return _get


def _vp_spec(strategy, key, indicator, overlaps, lineinfo,
             plotinfo=None) -> Optional[IndicatorSpec]:
    """``category == 'vp'`` 的指标 -> 一条 ``kind='VolumeProfile'`` 的 spec。

    数据契约（与 ``bokeh_plot._add_volume_profile`` / ``btplot`` 完全一致）：

    * ``indicator``（``dataset.pandas_object``）的**列**是等宽价格档
      ``vp0..vpN-1``（不是时间序列），行才是 K 线；
    * 档区 = 该根往前 ``price_bars`` 根的 ``[min(low), max(high)]``
      （**逐根滚动**，所以每根的价格位置不同）；
    * **样式**优先取 ``plotinfo['vpstyle']``（`minibt/utils.py::VPStyle`，
      只有 `category='vp'` 才生效），再回退到 ``linestyle['vp0']`` /
      ``linestyle['vp_poc']``（兼容旧写法）。
    """
    if indicator is None or not hasattr(indicator, 'columns'):
        return None
    cols = [c for c in indicator.columns
            if str(c).startswith('vp') and str(c)[2:].isdigit()]
    if not cols:
        return None
    cols.sort(key=lambda c: int(str(c)[2:]))
    try:
        matrix = np.asarray(indicator[cols].values, dtype=np.float64)
    except Exception:
        return None

    def _color(name: str, default: str) -> str:
        style = lineinfo.get(name) if isinstance(lineinfo, dict) else None
        if hasattr(style, 'get'):
            return _as_color(style.get('line_color'), default)
        if isinstance(style, dict):
            return _as_color(style.get('line_color'), default)
        return default

    vpstyle = vp_style_values(
        (plotinfo or {}).get('vpstyle') if hasattr(plotinfo, 'get') else None)
    price_bars = vpstyle.get('price_bars')
    if price_bars is None:
        price_bars = lineinfo.get('vp_price_bars') if isinstance(lineinfo, dict) else None
    try:
        price_bars = int(price_bars) if price_bars else None
    except Exception:
        price_bars = None
    if isinstance(overlaps, (list, tuple)):
        overlap = bool(overlaps[0]) if overlaps else True
    else:
        overlap = bool(overlaps)
    return IndicatorSpec(
        name=str(key),
        overlap=overlap,
        kind='VolumeProfile',
        color=vpstyle.get('bar_color') or _color('vp0', '#3498db'),
        vp_color=vpstyle.get('bar_color') or _color('vp0', '#3498db'),
        vp_poc_color=(vpstyle.get('poc_color')
                      or _color('vp_poc', '#f39c12')),
        vp_price_bars=price_bars,
        vp_frac=float(vpstyle.get('profile_frac') or 0.30),
        vp_y_mode='price' if overlap else 'index',
        vpstyle=vpstyle,
        profile=matrix,
        get_profile=_indicator_profile(strategy, key),
        source=key)


#: `VPStyle`（`category='vp'` 专用）的默认值与取值器在 `minibt/utils.py`，
#: 这里 re-export 一下，保持 `minibt.strategy.realtime.vp_style_values` 可用。
from minibt.utils import VP_STYLE_DEFAULTS, vp_style_values  # noqa: E402,F401


#: VP 的 `rendererDraw` JS 模板。
#:   ★ 与 bokeh 一致：**位置固定在可视范围最右端**（`anchor = 最右可见 bar`），
#:     只有**数值**跟随鼠标（画鼠标所在那根的分布）—— 所以鼠标左右移动时柱体
#:     长度变化、整体不左右跑；鼠标不在图上时回落到最右那根的分布。
#:   ★ y 取鼠标那根自己的档区（`row.lo/hi`），所以分布会落在那一根的价位上。
#:   每帧重算（拖动/缩放自动跟随），不需要订阅任何 range 事件。
_VP_DRAW_JS = """
const bars = view.visibleBars();
if (bars.length) {
  const first = bars[0], lastVis = bars[bars.length - 1];
  const band = Math.max(Math.abs(lastVis.x - first.x), 1) * %(frac)s;
  // 数值跟随鼠标：十字线监听（纯 JS）把鼠标 CSS x 存在 window 上，
  // 这里按 `bar.x`（也是 CSS 像素）挑最近的一根 —— 不靠时间换算。
  // ★ 优先级：**实时更新 > 鼠标跟随**。数据一变（`series.set/update`）
  //   Python 侧会先把 `window[xkey]` 清成 null，于是那一帧画的是**最新**
  //   一根；鼠标再动一下才重新跟随。
  let hovered = lastVis;
  const hx = window.%(xkey)s;
  if (hx !== null && hx !== undefined) {
    let best = null, bd = Infinity;
    for (const b of bars) {
      const d = Math.abs(b.x - hx);
      if (d < bd) { bd = d; best = b; }
    }
    if (best) hovered = best;
  }
  const row = hovered.originalData;
  let mx = 0;
  for (let j = 0; j < %(nb)d; j++) {
    const q = row['vp' + j] || 0;
    if (q > mx) mx = q;
  }
  if (mx > 0) {
    let base = 0, bw = 1;
    if (%(index_mode)s) {
      base = 0; bw = 1;
    } else {
      const lo = row.lo, hi = row.hi;
      base = lo; bw = (isFinite(lo) && isFinite(hi) && hi > lo) ? (hi - lo) / %(nb)d : NaN;
    }
    if (isFinite(bw) && bw > 0) {
      target.useBitmapCoordinateSpace(scope => {
        const ctx = scope.context;
        const h = scope.horizontalPixelRatio;
        const v = scope.verticalPixelRatio;
        // ★ 0 刻度 = **绘图区最右端**（`scope.mediaSize.width`），也就是可视
        //   X 轴的右端 —— 与主图**成交量柱**的最右边缘对齐（成交量柱也是
        //   按整根 bar 的宽度铺到视口右边）。以前用“最后一根 K 线的中心”
        //   （`lastVis.x`）会差半根 bar，看着没贴到右边缘。
        const anchor = scope.mediaSize.width;
        ctx.globalAlpha = %(alpha)s;
        for (let j = 0; j < %(nb)d; j++) {
          const q = row['vp' + j] || 0;
          if (!(q > 0)) continue;
          const w = q / %(vmax)s * band;
          const y0 = priceConverter(base + j * bw);
          const y1 = priceConverter(base + (j + 1) * bw);
          if (!isFinite(y0) || !isFinite(y1)) continue;
          ctx.fillStyle = (%(poc_cond)s) ? %(poc)s : %(bar)s;
          const bx = (anchor - w) * h;
          const by = Math.min(y0, y1) * v;
          const bw2 = w * h;
          const bh = Math.abs(y1 - y0) * v * %(bin_ratio)s;
          ctx.fillRect(bx, by, bw2, bh);
          if (%(line_width)s > 0) {
            ctx.strokeStyle = %(line_color)s;
            ctx.lineWidth = %(line_width)s * h;
            ctx.strokeRect(bx, by, bw2, bh);
          }
        }
      });
    }
  }
}
"""


def _vp_draw_js(nb: int, style, vmax_js: str,
                index_mode: bool, xkey: str = '__minibtVpX') -> str:
    """生成 VP 的 ``rendererDraw`` JS。

    :param style: `VPStyle` / dict / None（经 :func:`vp_style_values` 规范化）
    :param vmax_js: JS 表达式（通常是 ``row.vmax``）：量程做成**逐行数据列**
        而不是烘焙常量，实时新数据把量做大时柱长比例才能跟着变。
    :param index_mode: 副图模式（y 用档位序号而不是价格档中心）
    :param xkey: 存鼠标 CSS x 的 ``window`` 属性名
    """
    st = vp_style_values(style)
    line_color = st.get('line_color')
    return _VP_DRAW_JS % {
        'nb': int(nb), 'frac': repr(float(st['profile_frac'] or 0.30)),
        'vmax': str(vmax_js),
        'bar': json.dumps(str(st['bar_color'] or '#3498db')),
        'poc': json.dumps(str(st['poc_color'] or '#f39c12')),
        'poc_cond': '(q >= mx)' if st.get('highlight_poc', True) else 'false',
        'alpha': repr(float(st['fill_alpha'] if st['fill_alpha'] is not None else 0.9)),
        'bin_ratio': repr(float(st['bin_ratio'] if st['bin_ratio'] else 0.88)),
        'line_color': json.dumps(str(line_color)) if line_color else 'null',
        'line_width': repr(float(st['line_width'] or 0.0)),
        'index_mode': 'true' if index_mode else 'false',
        'xkey': str(xkey),
    }


def _vp_price_js(nb: int, index_mode: bool) -> str:
    """VP 的 ``priceValueBuilder``：决定自动缩放取哪些价。"""
    if index_mode:
        return f'return [0, {int(nb)}];'
    return 'return (row.lo === row.hi) ? null : [row.lo, row.hi];'


def _vp_price_bands(frame, price_bars, nb: int):
    """每根 K 线自己的档区 ``[lo_i, hi_i]``（滚动 ``price_bars`` 根的 low/high）。

    与指标端 ``RollingVolumeProfile.next()`` 同一规则、也与 bokeh 的
    ``_lo_arr``/``_hi_arr`` 一致，保证横条的价格位置对得上。
    """
    if frame is None or 'low' not in getattr(frame, 'columns', []) \
            or 'high' not in getattr(frame, 'columns', []):
        return None, None
    lows = pd.to_numeric(frame['low'], errors='coerce')
    highs = pd.to_numeric(frame['high'], errors='coerce')
    try:
        window = int(price_bars) if price_bars else 0
    except Exception:
        window = 0
    if 0 < window <= len(frame):
        lo = lows.rolling(window, min_periods=1).min().to_numpy(dtype=float)
        hi = highs.rolling(window, min_periods=1).max().to_numpy(dtype=float)
    else:
        lo = np.full(len(frame), float(np.nanmin(lows)))
        hi = np.full(len(frame), float(np.nanmax(highs)))
    if not np.isfinite(lo).any() or not np.isfinite(hi).any():
        return None, None
    return lo, hi


def _append_group(specs, strategy, key, group_idx, indicator, lineinfo,
                  overlap, lines, plots):
    for line_idx, col in enumerate(lines):
        if not plots[line_idx]:
            continue
        series = _values(indicator, col, line_idx, len(lines))
        info = (lineinfo.get(col) or {}) if lineinfo else {}
        style = info.get('line_dash', 'solid')
        specs.append(IndicatorSpec(
            name=col, overlap=overlap, values=series,
            color=info.get('line_color'),
            line_width=int(info.get('line_width', 2) or 2),
            line_style=style,
            price_line=bool(info.get('price_line', False)),
            price_label=bool(info.get('price_label', False)),
            kind='Histogram' if style == 'vbar' else 'Line',
            vbar_base=info.get('vbar_base'),
            vbar_color_base=info.get('vbar_color_base'),
            vbar_up_color=info.get('vbar_up_color'),
            vbar_down_color=info.get('vbar_down_color'),
            source=key,
            # ★ 指标级图例开关：**建 spec 时就带进去** ✓，
            #   `IndicatorManager.build` 再原样传给
            #   `add_series/pane_add_series(..., legend=spec.legend)` ✓
            #   ``legend`` 支持 bool / dict（按线名）/ list（按线序）✓ ——
            #   例：``legend = dict(zero=False)`` 只藏掉零轴那一行
            legend=indicator_legend_visible(strategy, key, col, line_idx),
            get_last=_indicator_last(strategy, key, group_idx, line_idx),
            get_values=_indicator_values(strategy, key, group_idx, line_idx)))



def strategy_backtest_dict(strategy, limit: Optional[int] = None,
                           contract: Optional[str] = None,
                           limit_trades: bool = False) -> dict:
    """从 minibt 策略拼出 `set_backtest` 需要的字典（可按 limit 截断）。

    * 权益：``strategy.profits``（否则 ``_results[0]['total_profit']``），截到 limit 根；
    * 回撤：按回撤公式算；
    * 交易/盈亏：minibt logger 的 ``trade_history``，取不到就用 positions 推；
      ``limit_trades=True`` 时只取已放出时间段内的。
    """
    results = getattr(strategy, '_results', None)
    result = results[0] if results else None
    profits = getattr(strategy, 'profits', None)
    if profits is None and result is not None and 'total_profit' in result:
        profits = result['total_profit']
    if profits is None:
        return {}
    full_values = np.asarray(getattr(profits, 'values', profits), dtype=float)
    if full_values.size == 0:
        return {}

    try:
        contracts = strategy_contracts(strategy)
        name = contract or (contracts[0] if contracts else None)
        if contracts and name not in contracts:
            # ★ 多策略登记时，调用方可能传了**别个策略的合约**（如
            #   `self.window.contract`）：在这个策略的数据集里找不到 →
            #   `strategy_kline_frame` 返回空 → 下面 `len(frame) >= full_values`
            #   不成立 → 权益/回撤退回 `pd.date_range('2020-01-01')` 的
            #   假时间轴，整页与 K 线完全错位 ✗。用这个策略自己的第一个
            #   合约兑底，至少时间轴是对的 ✓。
            name = contracts[0]
        frame = strategy_kline_frame(strategy, name)
    except Exception:
        frame = None
    if frame is not None and len(frame) >= full_values.size:
        full_times = list(frame['time'].iloc[:full_values.size])
    else:
        full_times = list(pd.date_range('2020-01-01',
                                        periods=full_values.size, freq='min'))
    # 曲线：截到已放出根数（回放时跟着 K 线长度）
    values = full_values[:limit] if limit else full_values
    times = full_times[:values.size]
    limit_time = full_times[values.size - 1] if (limit and limit_trades) else None
    peak = np.maximum.accumulate(values)
    drawdown_values = (values - peak) / np.where(peak > 0, peak, 1.0)
    # 交易/盈亏：logger 有真实记录就用它（本来就含全部合约）；否则按**每个
    # 合约**从 positions 推，合并后按时间排序（多合约时间可能不一致 ✓）
    records, pnl_full = RealtimeChart._trades_from_log(strategy, limit_time)
    if records:
        pnl_full = pd.DataFrame(pnl_full) if pnl_full else None
    else:
        records, pnl_full = RealtimeChart._extract_all_contract_trades(
            strategy, results, full_values, full_times, limit_time)
    # 盈亏散点只保留已放出时间段内的
    if pnl_full is not None and limit:
        pnl = pnl_full[
            pd.to_datetime(pnl_full['time']) <= pd.Timestamp(times[-1])]
        pnl = pnl.reset_index(drop=True)
    else:
        pnl = pnl_full
    full_peak = np.maximum.accumulate(full_values)
    full_dd = (full_values - full_peak) / np.where(full_peak > 0, full_peak, 1.0)
    if _DEBUG:
        try:
            from minibt.logger import get_logger
            _n = len(getattr(get_logger(), 'trade_history', None) or [])
        except Exception:
            _n = -1
        print(f'[realtime] strategy={type(strategy).__name__} '
              f'trade_history={_n} trades={0 if not records else len(records)} '
              f'equity={values.size}/{full_values.size} '
              f'pnl={0 if pnl is None else len(pnl)}')
    stats = RealtimeChart._extract_stats(strategy, full_values, full_dd,
                                         pnl_full)
    out = {'stats': stats,
           'equity': pd.DataFrame({'time': times, 'value': values}),
           'drawdown': pd.DataFrame({'time': times, 'value': drawdown_values}),
           'pnl': pnl, 'trades': records}
    return out


class IndicatorManager:
    """主图 overlay + 副图 pane 的指标线。"""

    def __init__(self, chart, legend_menu: bool = True):
        self.chart = chart
        self.specs: List[IndicatorSpec] = []
        self.series: List[Any] = []
        self.panes: dict = {}              # source -> pane_index（信号跟着它走）
        #: 是否给指标行挂**图例齿轮菜单**（参数 / 线型 / 线宽 / 线颜色）：
        #: ``False`` → 只保留图例行与眼睛开关，不生成齿轮 ✓。
        #: **回放时关掉** —— 回测结果是按当前指标参数跑出来的，在回放图里
        #: 改参数没有意义（也和已录下的信号/成交对不上 ✗）。
        self.legend_menu: bool = bool(legend_menu)
        #: 用户在图例齿轮菜单里改过的视觉设置：(source, name) -> {字段: 值}。
        #: 图表重建会换出新 spec，靠它把用户的选择套回去 ✓。
        self.style_overrides: dict = {}
        #: 菜单「参数」页确认后的重算回调（由 RealtimeChart 注入）。
        self.menu_handler = None
        #: 视图和数据怎么对齐：
        #: * ``'head'``（回放）—— 图表放出的是全量数据的**前** N 根；
        #: * ``'tail'``（实盘/无限历史）—— 图表显示的总是**后** N 根。
        self.align = 'head'
        #: `'head'` 对齐时可再指定起点（回放一般不用）；`'tail'` 忽略
        self.offset = 0

    def spec_for(self, series) -> Optional[IndicatorSpec]:
        """序列 → 对应的 ``IndicatorSpec``（图例菜单回调用）。"""
        for item, spec in zip(self.series, self.specs):
            if item is series:
                return spec
        return None

    def _wire_menu(self, spec: IndicatorSpec, series, legend: bool) -> None:
        """给一条指标线挂图例齿轮菜单 + 固定图例行（仅带图例的线 ✓）。

        ``self.legend_menu is False``（回放）时只固定图例行，**不挂齿轮** ✓。
        """
        if not legend or series is None:
            return
        indicator_menu.pin_row(series)
        if not self.legend_menu:
            return
        indicator_menu.enable_menu(series, indicator_menu.build_config(spec),
                                   self._on_menu)

    def _on_menu(self, series, kind: str, payload: dict) -> None:
        """图例菜单回调：视觉设置写回 spec；参数交给重算回调 ✓。"""
        spec = self.spec_for(series)
        if spec is None:
            return
        if kind == 'params':
            if self.menu_handler is not None:
                self.menu_handler(spec, payload.get('values') or {})
            return
        if not indicator_menu.apply_visual(spec, kind, payload):
            return
        values = indicator_menu.override_values(spec, kind, payload)
        if values:
            self.style_overrides.setdefault(
                indicator_menu.override_key(spec), {}).update(values)
        # ★ 光改 spec + 记 ``style_overrides`` 要等**下一次重建**才生效 ✗（用户反馈：
        #   “选完没反应，重载其中一个指标后所有设置又生效了”）。这里立刻用
        #   ``series.set_line_style/width/color`` 同步到图上（图例小方块也跟着
        #   变 ✓）—— 和 miniqt ``_apply_indicator_styles`` 同一套做法 ✓
        self.apply_visual_now(series, kind, payload)

    def apply_visual_now(self, series, kind: str, payload: dict) -> int:
        """把菜单选择**立刻**写到该序列上（返回成功应用的项数 ✓）。

        对应 pylightcharts 的 ``set_line_style`` / ``set_line_width`` /
        ``set_line_color`` / ``set_price_visible``（它们会顺带刷新图例行 ✓）。
        当前后端没有这些 API 时安静跳过（老行为）✓。
        """
        value = payload.get('value')
        calls: list = []
        if kind == 'style':
            calls.append(('set_line_style', value))
        elif kind == 'width':
            try:
                calls.append(('set_line_width', int(value)))
            except (TypeError, ValueError):
                return 0
        elif kind == 'color':
            calls.append(('set_line_color', value))
        elif kind == 'price':
            # ★ 参数名是 **snake**（`set_price_visible('price_line'/'price_label')`✓）
            #   —— 传 camelCase 会被它静默忽略 ✗
            calls.append(('set_price_visible',
                          ('price_line', bool(payload.get('priceLine')))))
            calls.append(('set_price_visible',
                          ('price_label', bool(payload.get('priceLabel')))))
        done = 0
        for name, arg in calls:
            func = getattr(series, name, None)
            if not callable(func):
                continue
            try:
                if isinstance(arg, tuple):
                    func(arg[0], arg[1])
                else:
                    func(arg)
                done += 1
            except Exception as error:                           # noqa: BLE001
                print(f'[realtime] {name} 失败: {error!r}')
        return done

    def series_named(self, source: str, name: str) -> Any:
        """按“来源 key + 线名”找指标线序列（画色带用）。"""
        for series, spec in zip(self.series, self.specs):
            if spec.name == name and (not source or spec.source == source):
                return series
        return None

    def pane_series(self, source: str) -> Any:
        """该指标的第一条序列（把水平线放到同一个 pane）。"""
        for series, spec in zip(self.series, self.specs):
            if not source or spec.source == source:
                return series
        return None

    @staticmethod
    def _frame_seconds(frame: pd.DataFrame) -> pd.Index:
        """一帧数据的时间 -> epoch 秒 Index（和图表 `candle_data` 一致）。

        ⚠ 必须容忍 **object 列**：图表 mirror 在第一次 ``update()`` 之后，
        ``time`` 列会因为写进 float（如 ``1760712900.0``）而退化成 ``object``
        ✗ —— 那时 ``is_numeric_dtype`` 为 False，直接走 ``pd.to_datetime(…)``
        会把“秒”当 **纳秒** 解释（结果全是 `1`）✗ → 按时间 reindex 全落空
        → 指标/信号整段变 NaN（“交易信号突然消失、此后不再显示”✗）。
        所以先试 **按数值当秒** ✓，不行再当日期字符串解析 ✓。
        """
        times = frame['time']
        # ① 真正的 datetime64：先转秒 ✓（必须放在最前面，否则会被
        #    ``pd.to_numeric`` 转成**纳秒**✗）
        if pd.api.types.is_datetime64_any_dtype(times):
            return pd.Index(times.astype('int64') // 10 ** 9)
        # ② 纯数值（int/float）：本来就是 epoch 秒 ✓
        if pd.api.types.is_numeric_dtype(times):
            return pd.Index(np.asarray(times, dtype='int64'))
        # ③ object 列：里可能是“秒”（mirror 写入 float 后退化 ✗）
        #    也可能是日期字符串 ✓ —— 先试按数值当秒，不行再当日期解析 ✓
        try:
            numeric = pd.to_numeric(times, errors='coerce')
            if len(numeric) and bool(numeric.notna().all()):
                return pd.Index(np.asarray(numeric, dtype='int64'))
        except Exception:                                        # noqa: BLE001
            pass
        return pd.Index(pd.to_datetime(times).astype('int64') // 10 ** 9)

    def live_values(self, spec: IndicatorSpec) -> Optional[np.ndarray]:
        """该指标线**当前实盘**整条值（按 key 现取，取不到返回 ``None``）。

        ⚠ **不再退回建图时的快照** ``spec.values``：那是**初始化那一瞬**的值，
        拿它去填后面新来的 K 线就会写出一片**常数**（“初始化时的最后一根到
        最新一根前全部是固定值”✗）。宁可返回 ``None`` → 调用方保留图上已有的
        值 / 留空，也不写错值 ✓
        """
        if spec.get_values is not None:
            try:
                values = spec.get_values()
            except Exception:                                    # noqa: BLE001
                return None
            if values is None:
                return None
        elif spec.values is not None:
            values = np.asarray(spec.values, dtype=float)
        else:
            return None
        return np.asarray(values, dtype=float).ravel()

    def _align(self, values: Optional[np.ndarray],
               n: int) -> Optional[np.ndarray]:
        if values is None:
            return None
        if n is None:
            return values
        n = int(n)
        if self.align == 'tail':
            if len(values) >= n:
                return values[-n:]
            return np.concatenate(
                [np.full(n - len(values), np.nan), values])
        offset = int(getattr(self, 'offset', 0) or 0)
        if offset:
            values = values[offset:]
        if len(values) >= n:
            return values[:n]
        return np.concatenate([values, np.full(n - len(values), np.nan)])

    def spec_values(self, spec: IndicatorSpec,
                    n: Optional[int] = None) -> Optional[np.ndarray]:
        """该指标线当前整条值（优先按 key 现取，退回构建时快照）。"""
        return self._align(self.live_values(spec), n)

    def view_values(self, spec: IndicatorSpec,
                    frame: pd.DataFrame) -> Optional[np.ndarray]:
        """按 `frame` 的时间取该指标线的值。

        有全量缓存（`spec.cached_series`）时**按时间对齐**：实盘策略只重算
        `preload` 根，历史部分的指标值在这里取。没有缓存就退回实时值尾部对齐。
        """
        if frame is None or frame.empty:
            return None
        n = len(frame)
        cached = spec.cached_series
        out = None
        if cached is not None and len(cached):
            times = self._frame_seconds(frame)
            try:
                out = np.asarray(cached.reindex(times).to_numpy(dtype=float),
                                 dtype=float)
            except Exception:
                out = None
        if out is None:
            return self._align(self.live_values(spec), n)
        # ★ 全量缓存是**开局建好就冻结**的：之后新来的 K 线不在它的索引里 →
        #   ``reindex`` 取出来是 NaN → 图上右侧就是一截空的（“切换策略后数据
        #   缺失”✗）。这里用**活值尾部对齐**把缓存没覆盖到的时间戳补上；
        #   活值也缺的（历史段）保持 NaN，仍由无限历史负责 ✓
        miss = ~np.isfinite(out)
        if miss.any():
            live = self._align(self.live_values(spec), n)
            if live is not None and len(live) == n:
                out = np.where(miss, live, out)
        return out

    def build(self, frame: pd.DataFrame, specs: List[IndicatorSpec],
              palette: Optional[Sequence[str]] = None) -> None:
        self.clear()
        # ★ 同一 pane 内**先建柱体、再建指标线**（先建的在下层 ✗）——
        #   否则 macdh 会把先建的 macdx 盖住（用户反馈 ✗）。
        #   用**稳定排序**（同类保持原顺序 ✓），并同时写回 ``self.specs``：
        #   下面 ``update_last`` / ``reslice`` / ``apply_*`` 都是按这两个列表
        #   同下标 zip 的 ✓，两边顺序必须完全一致 ✓。
        specs = sorted(specs, key=_pane_zorder)
        self.specs = specs
        colors = list(palette or [])
        panes: dict = {}                      # source -> pane：每个指标一个副图
        self.panes = panes                    # 信号（overlap=False）也去同一个 pane
        for index, spec in enumerate(specs):
            indicator_menu.apply_overrides(spec, self.style_overrides)
            if colors and spec.color is None:
                spec.color = colors[index % len(colors)]
                spec.palette_color = True     # 调色板分配，不算“用户显式 line_color”
            # 蜡烛型指标：一条 Candlestick（副图 / 主图叠加），其余线走普通分支
            if spec.kind == 'Candlestick' and spec.ohlc:
                candle = self._build_candle(spec, panes, frame)
                self.series.append(candle)
                self._wire_menu(spec, candle,
                                bool(getattr(spec, 'legend', True)))
                continue
            # 滚动 Volume Profile：一条 CustomSeries，`rendererDraw` 里画横条
            if spec.kind == 'VolumeProfile' and spec.profile is not None:
                self.series.append(self._build_vp(spec, panes, frame))
                continue
            # 颜色/线型必须在建序列时传入：图例刷色是建行时从
            # series.options() 读的，事后 applyOptions 不会更新图例小方块
            options = (_color_options(spec.kind, spec.color, spec.line_width,
                                      spec.line_style, spec.price_line,
                                      spec.price_label)
                       if spec.color else {})
            # 柱体（Histogram）的**柱底基线**：LWC 的 series 选项叫 ``base``。
            #   以前 `vbar_base` 只用于“值 vs 基线”判上下行分色，没传给序列，
            #   所以柱子永远从 0 开始（如 MarketStructureOscillator 的 cycle
            #   应该绕 50 振荡）。
            if spec.kind == 'Histogram' and spec.vbar_base is not None:
                options['base'] = float(spec.vbar_base)
            _legend = (not getattr(spec, 'hidden', False)
                       and bool(getattr(spec, 'legend', True))
                       and indicator_legend_visible(self,
                                                    getattr(spec, 'source', '')))
            if spec.overlap:
                series = self.chart.add_series(spec.kind, name=spec.name,
                                               pane_index=0, legend=_legend,
                                               **options)
            else:
                # 按 `source`（指标）分副图：不同指标量级不同（CCI 的 ±100 与
                # MACD 的 ±几），挤在一个价格轴上会互相压成直线。同一指标的多条
                # 线（如 macdx/macdh/macds）仍然共用一个副图。
                pane = panes.get(spec.source)
                if pane is None:
                    pane = self.chart.add_pane()
                    panes[spec.source] = pane
                series = self.chart.pane_add_series(pane, spec.kind,
                                                    name=spec.name,
                                                    legend=_legend, **options)
            if not _legend:                    # 隐藏线：不画线、不进图例
                try:
                    series.applyOptions({'lineVisible': False})
                except Exception:
                    pass
            # 指标值：有历史缓存时按时间对齐，否则实时值尾部对齐（长了截断、
            # 短了补 NaN），否则 set() 会因长度不一致失败。
            n = len(frame)
            values = self.view_values(spec, frame)
            if values is not None and 'time' in frame.columns:
                if _DEBUG:
                    print(f'[realtime] 指标线 {spec.name}: nan='
                          f'{int(pd.isna(values).sum())} n={n} '
                          f'overlap={spec.overlap} kind={spec.kind}')
                series.set(pd.DataFrame({'time': list(frame['time']),
                                         spec.name: values}))
            self.series.append(series)
            self._wire_menu(spec, series, _legend)

    def _build_candle(self, spec: 'IndicatorSpec', panes: dict,
                      frame: pd.DataFrame):
        """建蜡烛序列（副图 / 主图叠加）并灌入 OHLC。"""
        options = _candle_options(spec.candlestyle)
        if spec.overlap:
            series = self.chart.add_series('Candlestick', name=spec.name,
                                           pane_index=0, **options)
        else:
            pane = panes.get(spec.source)
            if pane is None:
                pane = self.chart.add_pane()
                panes[spec.source] = pane
            series = self.chart.pane_add_series(pane, 'Candlestick',
                                                name=spec.name, **options)
        df = self._candle_frame(spec, frame)
        if df is not None and not df.empty:
            series.set(df)
        return series

    def _candle_frame(self, spec: 'IndicatorSpec',
                      frame: pd.DataFrame) -> Optional[pd.DataFrame]:
        """按当前 ``frame`` 长度拼出蜡烛的 ``time/open/high/low/close``。"""
        if frame is None or 'time' not in getattr(frame, 'columns', []):
            return None
        n = len(frame)
        data = {'time': list(frame['time'])}
        live = None
        if spec.get_ohlc is not None:
            try:
                live = spec.get_ohlc()
            except Exception:
                live = None
        for key in ('open', 'high', 'low', 'close'):
            arr = None
            if live is not None and key in live:
                try:
                    arr = np.asarray(live[key], dtype=float)
                except Exception:
                    arr = None
            if arr is None and spec.ohlc and spec.ohlc.get(key) is not None:
                arr = np.asarray(spec.ohlc[key], dtype=float)
            if arr is None:
                arr = np.full(n, np.nan)
            data[key] = self._align(arr, n)
        return pd.DataFrame(data)

    def _build_vp(self, spec: 'IndicatorSpec', panes: dict,
                  frame: pd.DataFrame):
        """建滚动 Volume Profile 的 ``CustomSeries``（与 bokeh 同一几何约定）。

        每个 K 线一根点：行里带 ``lo``/``hi``（该根的滚动档区）、``vmax``
        （量程）与 ``vp0..vpN-1``（该根的分布）。真正的绘制在 ``rendererDraw``
        里 —— 每帧取**鼠标最近的那根**作锚点（鼠标不在图上则回落到最右可见根）、
        按当前可视跨度算柱长，所以拖动/缩放/移动鼠标都自动跟随。
        """
        nb = int(spec.profile.shape[1])
        index_mode = spec.vp_y_mode == 'index'
        if spec.overlap:
            pane = 0
        else:
            pane = panes.get(spec.source)
            if pane is None:
                pane = self.chart.add_pane()
                panes[spec.source] = pane
        prof = np.asarray(spec.profile, dtype=np.float64)
        tag = abs(hash((str(spec.source), str(spec.name), nb, index_mode))) & 0xFFFFFF
        draw = f'minibt_vp_draw_{tag:x}'
        prices = f'minibt_vp_price_{tag:x}'
        empty = f'minibt_vp_empty_{tag:x}'
        hover = f'minibt_vp_hover_{tag:x}'
        xkey = f'__minibtVpX_{tag:x}'
        self.chart.register_js_callback(
            draw, ['target', 'priceConverter', 'view'],
            _vp_draw_js(nb, spec.vpstyle, 'row.vmax', index_mode, xkey))
        self.chart.register_js_callback(
            prices, ['row', 'view'], _vp_price_js(nb, index_mode))
        self.chart.register_js_callback(empty, ['data', 'view'],
                                        'return false;')
        series = self.chart.add_custom_series(
            name=spec.name, pane_index=pane, legend=False,
            color=spec.vp_color or '#3498db',
            spec={'rendererDraw': draw, 'priceValueBuilder': prices,
                  'isWhitespace': empty,
                  'defaultOptions': {'lastValueVisible': False,
                                     'priceLineVisible': False}})
        df = self._vp_frame(spec, frame)
        if df is not None and not df.empty:
            try:
                series.set(df, format_cols=False)
            except Exception as error:
                print('[realtime] 画 Volume Profile 失败:', error)
        # 跟随鼠标：注册一个**纯 JS** 的十字线监听（不回调 Python，避免每次
        # 移动都走一遍 webview），把鼠标的 CSS x 存到 `window[xkey]`；
        # `rendererDraw` 每帧按它挑最近的那根 K 线。鼠标移出图表时
        # `param.point` 为空 -> 置 null -> 自动回落到“最右可见根”。
        #
        # ★ 必须顺手 `applyOptions({})` 强制重绘：实测 lightweight-charts
        #   的十字线移动**不会**重新调用自定义序列的 `rendererDraw`
        #   （它只重绘十字线/坐标轴层），不主动重绘的话分布图永远停在
        #   鼠标进入前那一次的画面。`applyOptions({})` 是最便宜的重绘触发
        #   （实测每次 +1 次 draw，且不改任何可见选项），比 `setData` 小得多。
        chart_id = getattr(self.chart, 'id', '') or ''
        series_id = getattr(series, 'id', '') or ''
        spec.vp_xkey = xkey
        if chart_id and series_id:
            try:
                self.chart.run_script(
                    f'if (window.{hover}) {chart_id}.removeCrosshairListener('
                    f'window.{hover});\n'
                    f'window.{hover} = (param) => {{\n'
                    f'  window.{xkey} = (param && param.point) ? '
                    f'param.point.x : null;\n'
                    f'  try {{ {series_id}.applyOptions({{}}); }} catch (e) {{}}\n'
                    f'}};\n'
                    f'{chart_id}.addCrosshairListener(window.{hover});')
            except Exception:
                pass
        return series

    def _vp_matrix(self, spec: 'IndicatorSpec') -> Optional[np.ndarray]:
        """取 VP 矩阵：优先现取（实时），拿不到则回退构建时缓存的。"""
        if spec.get_profile is not None:
            try:
                live = spec.get_profile()
            except Exception:
                live = None
            if live is not None and getattr(live, 'size', 0):
                return live
        return spec.profile

    def _clear_vp_hover(self, spec: 'IndicatorSpec') -> None:
        """数据一变就把鼠标跟随坐标清掉。

        优先级：**实时更新 > 鼠标跟随**（与指标数值表格一致）—— 新数据到达时
        分布图先显示**最新**一根，鼠标再动一下才重新跟随；否则鼠标停在老 K 线上
        不动时，分布图会一直卡在那根上，看着像“实时不更新”。
        """
        xkey = getattr(spec, 'vp_xkey', None)
        if not xkey:
            return
        try:
            self.chart.run_script(f'window.{xkey} = null;')
        except Exception:
            pass

    def _vp_frame(self, spec: 'IndicatorSpec',
                  frame: pd.DataFrame,
                  matrix: Optional[np.ndarray] = None) -> Optional[pd.DataFrame]:
        """按当前 ``frame`` 长度拼出 VP 的逐根数据（``time/lo/hi/vmax/vp*``）。"""
        if frame is None or 'time' not in getattr(frame, 'columns', []):
            return None
        prof = np.asarray(self._vp_matrix(spec) if matrix is None else matrix,
                          dtype=np.float64)
        n = len(frame)
        if prof.shape[0] != n:
            # 长度对不上（回放/切片）：尾部对齐，缺的补 NaN
            if prof.shape[0] > n:
                prof = prof[-n:]
            else:
                pad = np.full((n - prof.shape[0], prof.shape[1]), np.nan)
                prof = np.vstack([pad, prof])
        lo, hi = _vp_price_bands(frame, spec.vp_price_bars, prof.shape[1])
        # 量程固定（整段最大量）改成**逐行列**：实时新数据把量做大时柱长比例
        # 才能跟着变；同一帧内所有根共用同一个值，跨时刻仍然可比。
        finite = prof[np.isfinite(prof)]
        vmax = float(np.nanmax(finite)) if finite.size else 0.0
        data = {'time': list(frame['time'])}
        data['vmax'] = np.full(n, vmax if vmax > 0 else 1.0)
        data['lo'] = np.full(n, np.nan) if lo is None else lo
        data['hi'] = np.full(n, np.nan) if hi is None else hi
        for j in range(prof.shape[1]):
            data[f'vp{j}'] = prof[:, j]
        return pd.DataFrame(data)

    def reslice(self, frame: pd.DataFrame,
                offset: Optional[int] = None,
                previous: Optional[int] = None) -> None:
        """K 线变长后，把指标值按新视图重新切片并 set。

        :param offset: `'head'` 对齐时的起点（回放一般不用）。
        :param previous: 上一次的长度。给了、并且只增长时走**逐点 update()** ——
            整段 `setData` 会让 LWC 重算价格轴并播一次缩放动画，回放逐根推进
            时表现为 K 线主图"先放大再缩回"的抖动。长度跳变仍整段 set。
        """
        if offset is not None:
            self.offset = int(offset or 0)
        if not self.series or frame is None or frame.empty:
            return
        if 'time' not in frame.columns:
            return
        times = list(frame['time'])
        n = len(times)
        first = 0 if previous is None else int(previous)
        incremental = 0 < first < n
        for series, spec in zip(self.series, self.specs):
            # 蜡烛型：整段 set（不是逐点 update）
            if spec.kind == 'Candlestick' and spec.ohlc:
                try:
                    df = self._candle_frame(spec, frame)
                    if df is not None and not df.empty:
                        series.set(df)
                except Exception:
                    pass
                continue
            # Volume Profile：同样整段 set（逐根带自己的档区，不能逐点推）。
            # ★ 先清鼠标跟随：**实时更新优先于鼠标跟随**。
            if spec.kind == 'VolumeProfile':
                try:
                    self._clear_vp_hover(spec)
                    df = self._vp_frame(spec, frame)
                    if df is not None and not df.empty:
                        series.set(df, format_cols=False)
                except Exception:
                    pass
                continue
            values = self.view_values(spec, frame)
            if values is None:
                continue
            if incremental:
                try:
                    for index in range(first, n):
                        point = {'time': times[index],
                                 spec.name: values[index]}
                        # 柱体：逐点 update 也要带颜色，否则新柱子会掉回系列
                        # 默认色（回放时看着就是“macd / rsi 变回 line_color”）
                        if spec.kind == 'Histogram':
                            color = _spec_point_color(spec, values[index],
                                                      n, index)
                            if color:
                                point['color'] = color
                        series.update(pd.Series(point, dtype=object))
                    continue
                except Exception:
                    pass                        # 退化成整段 set
            series.set(pd.DataFrame({'time': times, spec.name: values}))
            if spec.kind == 'Histogram' and spec.up_color and spec.down_color:
                # 整段 set 会丢掉逐点颜色：按静态配色重新刷一遍
                base = getattr(spec, 'vbar_color_base', None)
                base = spec.vbar_base if base is None else base
                vbase = 0.0 if base is None else float(base)
                try:
                    series.color_by(pd.Series(np.asarray(values) > vbase),
                                    spec.up_color,
                                    else_color=spec.down_color)
                except Exception:
                    pass

    def recolor(self, palette: Sequence[str]) -> None:
        """主题切换时重设所有指标线颜色。"""
        colors = list(palette or [])
        if not colors:
            return
        for index, (series, spec) in enumerate(zip(self.series, self.specs)):
            spec.color = colors[index % len(colors)]
            _apply_color(series, spec.kind, spec.color)

    def clear(self) -> None:
        for series in self.series:
            series.delete()
        self.series = []
        self.specs = []
        self.panes = {}

    def keep_only(self, sources=(), names=()) -> None:
        """只保留指定来源/名称的指标线，其余删除（回测结果页用）。

        删了线之后 pane 会变空，引擎会把空 pane 一起收走，所以不会
        留下一条空白带。
        """
        kept_series, kept_specs = [], []
        for series, spec in zip(self.series, self.specs):
            if spec.source in sources or spec.name in names:
                kept_series.append(series)
                kept_specs.append(spec)
                continue
            try:
                series.delete()
            except Exception:
                pass
        self.series = kept_series
        self.specs = kept_specs

    @staticmethod
    def _push_point(series, fields: dict, historical: bool) -> None:
        """把一个点推给序列；``historical=True`` 时带 ``historical_update`` ✓。

        旧后端 / 自实现序列没有这个参数时退回普通 ``update``（老行为 ✓）。

        ⚠ 实参一律用 ``pd.Series(...)`` 这种**调用表达式**（而不是变量名）：
        ``test_lwc_compat`` 的位置转发检查会把「变量名 = 形参名」当成约定，
        变量名不匹配就报错 ✗（minibt 里其它 ``series.update(...)`` 也都这么写 ✓）。
        """
        if historical:
            try:
                series.update(pd.Series(fields, dtype=object),
                              historical_update=True)
                return
            except TypeError:
                pass
        series.update(pd.Series(fields, dtype=object))

    def update_last(self, when: Any, offset: int = 0,
                    historical: bool = False) -> None:
        """tick / 换根后把某个指标点推到最新。

        :param when: 该点的时间
        :param offset: 距视图末尾的距离（0=最新一根；换根时用 1 回填上一根）。
        :param historical: 回填**旧根** → 给引擎传 ``historicalUpdate=True`` ✓
            （传上旧根却漏这个参数会被 LWC 静默丢掉 ✗）。
        每次现取整条值（`spec_values`），并把柱状图 / `color_by` 的**逐点颜色**
        一起发下去，否则最后一点会退化成序列默认色。
        """
        for series, spec in zip(self.series, self.specs):
            # Volume Profile：整段刷新（分布随新数据变），并清鼠标跟随
            if spec.kind == 'VolumeProfile':
                frame = getattr(self.chart, 'candle_data', None)
                try:
                    self._clear_vp_hover(spec)
                    df = self._vp_frame(spec, frame)
                    if df is not None and not df.empty:
                        series.set(df, format_cols=False)
                except Exception:
                    pass
                continue
            # ★ 蜡烛型指标（``ha`` / CDV …）：必须推一根 **OHLC** ——
            #   Candlestick 序列收不到单值 ``value``，下面那句 ``series.update``
            #   会被引擎忽略（甚至把 `_last_bar` 污染成 `{time,value}`）→
            #   新 K 线永远不上副图蜡烛 ✗（用户反馈“副图 ha 从打开就不更新”）。
            #   值索引与线一致用 ``-1 - offset``，**时间用 ``when``**（当前这根）
            #   —— 不能拿 ``chart.candle_data`` 反查：换根回填时它是**上一根**，
            #   算出来的时间会比蜡烛序列最后一根还旧 → 引擎报
            #   “Cannot update oldest data” ✗。
            if spec.kind == 'Candlestick':
                idx = -1 - int(offset)
                ohlc = None
                if spec.get_ohlc is not None:
                    try:
                        ohlc = spec.get_ohlc()
                    except Exception:
                        ohlc = None
                if ohlc is None:
                    ohlc = spec.ohlc
                if ohlc is None:
                    continue
                try:
                    # ⚠ 不能用 ``if not ohlc`` —— ``get_ohlc()`` 返回的是 DataFrame，
                    #   ``not df`` 会抛 “truth value of a DataFrame is ambiguous” ✗
                    if len(ohlc) == 0:
                        continue
                except TypeError:
                    continue
                try:
                    bar = {key: float(np.asarray(ohlc[key],
                                                 dtype=float)[idx])
                           for key in ('open', 'high', 'low', 'close')}
                except Exception:
                    continue
                if not np.isfinite(bar['close']):
                    continue
                self._push_point(series, {'time': when, **bar}, historical)
                continue
            values = self.spec_values(spec)
            if values is None or len(values) <= offset:
                continue
            idx = -1 - offset
            value = float(values[idx])
            if not np.isfinite(value):
                continue              # 该点还是空（如未触发的止损线），不用推
            point = {'time': when, 'value': value}
            color = _spec_point_color(spec, value, len(values), idx)
            if color:
                point['color'] = color
            # dtype=object：否则 time 是 Timestamp 时 pandas 会把整行强转成
            # datetime64，把 value 变成 NaT（NaN 指标线就会在这报错）
            self._push_point(series, point, historical)


class RealtimeHistory:
    """实时图表的无限历史：向左滚到边缘时，从全量快照里再放出一批更早的 K 线。

    不用 pylightcharts 的 `InfiniteHistory` 是因为它把“已加载 frame”缓存在自己
    身上，而实时每来一根新 K 线走的是 `chart.update()`，它感知不到；下次向前
    翻页时会把新 K 线弄丢。这里每次都拿 `chart.candle_data` 现算，实时/翻页
    可以共存。
    """

    def __init__(self, chart, loader, page: int = 200, threshold: int = 20,
                 on_load=None):
        self.chart = chart
        self.loader = loader
        self.page = max(1, int(page))
        self.threshold = max(1, int(threshold))
        self.on_load = on_load
        self.exhausted = False
        self.requests = 0
        self._loading = False
        self._subscribed = False
        self.start()

    def __repr__(self) -> str:
        return (f'<RealtimeHistory page={self.page} '
                f'threshold={self.threshold} requests={self.requests} '
                f'exhausted={self.exhausted}>')

    def start(self) -> 'RealtimeHistory':
        if not self._subscribed:
            self.chart.events.range_change += self._on_range
            self._subscribed = True
        return self

    def stop(self) -> 'RealtimeHistory':
        if self._subscribed:
            self.chart.events.range_change.unsubscribe()
            self._subscribed = False
        return self

    def _on_range(self, chart, bars_before, bars_after):
        self.check(bars_before)

    def check(self, bars_before) -> int:
        """靠近左边缘（`bars_before <= threshold`）就拉一批。"""
        if bars_before is None or self.exhausted or self._loading:
            return 0
        if bars_before > self.threshold:
            return 0
        count = max(self.page, int(self.threshold - bars_before) + 1)
        return self.load(count)

    def load(self, count: Optional[int] = None) -> int:
        """:return: 新增了多少根（0 = 没有了 / 已在加载）。"""
        if self._loading or self.exhausted:
            return 0
        count = int(count or self.page)
        view = self.chart.candle_data
        if view is None or view.empty:
            return 0
        earliest = self._earliest(view)
        self._loading = True
        try:
            incoming = self.loader(count, earliest)
        finally:
            self._loading = False
        if incoming is None or len(incoming) == 0:
            self.exhausted = True
            return 0
        combined = self._combine(view, incoming)
        if combined is None:
            return 0
        added = len(combined) - len(view)
        if added <= 0:
            return 0
        self.chart.set(combined, keep_drawings=True)
        self.requests += 1
        if self.on_load is not None:
            self.on_load(self, added)
        return added

    @staticmethod
    def _as_datetime(series: pd.Series) -> pd.Series:
        """引擎里的 `time` 是 epoch 秒；加载器给的是 Timestamp。统一成 Timestamp。"""
        if pd.api.types.is_datetime64_any_dtype(series):
            return series
        return pd.to_datetime(series, unit='s')

    @classmethod
    def _earliest(cls, view: pd.DataFrame):
        return pd.Timestamp(cls._as_datetime(view['time']).iloc[0])

    @classmethod
    def _combine(cls, view: pd.DataFrame, incoming: pd.DataFrame):
        current = view.copy()
        current['time'] = cls._as_datetime(current['time'])
        older = incoming.copy()
        older['time'] = cls._as_datetime(older['time'])
        known = set(current['time'])
        fresh = older[~older['time'].isin(known)]
        if fresh.empty:
            return None
        combined = pd.concat([older, current], ignore_index=True)
        combined = combined.drop_duplicates(subset='time', keep='last')
        return combined.sort_values('time').reset_index(drop=True)


class RealtimeChart:
    """实时图表：只接受 minibt 策略。"""

    def __init__(self, strategies: Sequence[Any], *,
                 width: int = 1280, height: int = 880,
                 title: str | None = None,
                 browser: bool = False, port: int = 0,
                 info_pages: bool = False,
                 push_interval_ms: int = 0,
                 jupyter: bool = False,
                 live: bool = True,
                 realtime: bool = False,
                 theme: str | None = None,
                 y_margin: float | tuple | None = None,
                 bars: str = 'both',
                 trade_signal: bool = True):
        self.strategies = list(strategies)
        #: 是否在 K 线上画**真实成交**的入场/离场连线（与 bokeh 的 trade_signal 同义）
        self._trade_signal = bool(trade_signal)
        # 窗口标题：调用方没给就按模式取一个（实时图 / 静态图；回测展示与
        # btplot 会显式传自己的标题）。写死"实时图表"会让静态图标题不对。
        if not title:
            title = ('pylightcharts - 实时图表' if live
                     else 'pylightcharts - 静态图表')
        # 图表主题：显式 theme > 策略 self.theme > config.theme，默认 'dark'
        from ..utils import resolve_theme, resolve_toolbox
        theme = resolve_theme(
            theme=theme,
            strategy=self.strategies[0] if self.strategies else None)
        self._theme_name = theme
        # 初始画线工具：与 bokeh 工具栏统一用 toolbox
        toolbox = resolve_toolbox(
            self.strategies[0] if self.strategies else None)
        # 网格线：与 bokeh 共用 config.grid（默认关闭）
        from ..utils import resolve_grid
        grid = resolve_grid(
            self.strategies[0] if self.strategies else None)
        # 价格轴上下余量：**显式传参**才生效（不传 = lightweight-charts 默认）
        from ..utils import normalise_y_margin
        y_margin = normalise_y_margin(y_margin)
        #: 图表推送节流（毫秒）：策略每 tick 照跑，但推给 webview 最多这个频率；
        #: 换根（新 K 线）时立即推，不会拖。0 = 不节流。
        self._push_interval = max(0, int(push_interval_ms)) / 1000.0
        self._last_push = 0.0
        # minibt 策略实例没有 sname，退回类名（重名时加序号）
        names: List[str] = []
        for index, strategy in enumerate(self.strategies):
            name = getattr(strategy, 'sname', None) or \
                type(strategy).__name__
            if name in names:
                name = f'{name}#{index}'
            names.append(name)
        self._by_name = dict(zip(names, self.strategies))
        #: 合约 -> 该合约的全量 K 线快照（无限历史从这里向前翻页）
        self._full_frames: dict = {}

        def contracts_by_name(name: str) -> List[str]:
            return strategy_contracts(self._by_name[name])

        def klines_by_name(name: str, contract: str) -> pd.DataFrame:
            """给窗口的 loader：**全量**存起来，只先返回最后一小段（preload）。"""
            strategy = self._by_name[name]
            full = strategy_kline_frame(strategy, contract)
            self._full_frames[contract] = full
            preload = strategy_preload(strategy, contract)
            if (preload is None or full is None or full.empty
                    or len(full) <= preload):
                return full
            return full.tail(preload).reset_index(drop=True)

        # 复用 StrategyWindow 的面板：无回测页、无实时控件
        # 「设置」菜单里的模式专属项：实时 → 断开连接；回测展示 → 重置
        extra_settings = ([('断开连接', self.disconnect)] if realtime
                          else [('重置', self.reset)])
        self.window = StrategyWindow(names, contracts_by_name, width=width,
                                     height=height, title=title,
                                     loader=klines_by_name,
                                     info_pages=info_pages,
                                     browser=browser, live=live, port=port,
                                     jupyter=jupyter,
                                     extra_settings=extra_settings,
                                     theme=theme, toolbox=toolbox, grid=grid,
                                     y_margin=y_margin,
                                     bars=bars,
                                     live_tools=bool(realtime))
        self.chart = self.window.chart
        self._jupyter = bool(jupyter)
        self._realtime = bool(realtime)
        # 实时图专属工具（买卖盘 / 持仓线 / 二级报价 + 简易下单）
        self.live_tools = LiveTools(self)
        if self._realtime:
            self.window.on_toggle_order_book = \
                self.live_tools.toggle_order_book
            self.window.on_toggle_depth = self.live_tools.toggle_depth
        #: 点「断开连接」后置 True：feed 循环直接返回，图表保持最后一帧
        self._disconnected = False
        #: 重新登录天勤用的工厂（从当前 TqApi 的 auth/account 推导）
        self._api_factory = None
        self._period_ms = 100
        self._loop_thread = None
        if self._realtime:
            try:
                self._api_factory = self._build_api_factory()
            except Exception as error:
                print('[realtime] 构建天勤重连工厂失败:', error)
        self.indicators = IndicatorManager(self.chart)
        self.indicators.align = 'tail'      # 实时图表显示最新的 N 根
        # 指标图例菜单「参数」页确认 → 重算该指标（见 _on_indicator_params）
        self.indicators.menu_handler = self._on_indicator_params
        #: 用户在图例「参数」页改过的指标参数：{(合约, source): {参数: 值}}。
        #: 实盘每拍策略会用原始参数重建指标，靠它跨重建保留用户选择 ✓。
        self._param_overrides: dict = {}
        # 信号同口径（回放用默认的 head）
        self.signal_manager = SignalManager(self.chart, align='tail')
        #: 回测结果页的装饰序列（**每个合约**自己的止损/止盈线 + 交易信号）。
        #: 该页不走 signal_manager：信号要能画到任意合约的 pane 上，
        #: 而 signal_manager 固定挂在主图上。
        self._info_overlays: list = []
        #: 合约页上的入场/离场连线 + 交易标记（与指标线分开记账 ✓）
        self._trade_series: list = []
        self._history = None                # 无限历史控制器
        self._last_time: Any = None
        # 后台 feed 线程与主线程（切合约/切主题）共享 chart/指标，必须串行，
        # 否则“合约还没加载完又切下一个”会把两边的 series/pane 交叉，越切越卡。
        self._lock = threading.RLock()
        self._switching = False
        #: 无限历史用：全量指标值缓存，key=(合约, 指标key, 线名)，value=按时间索引的 Series
        self._value_cache: dict = {}

        # 合约切换后重建指标
        original = self.window.set_contract

        def set_contract(contract: str, force: bool = False) -> None:
            with self._lock:
                self._switching = True
                # ★ 整个重建期间锁住可视范围记录：过渡态的 JS 范围事件不记 ✗
                lock_view = getattr(self.window, '_lock_view', None)
                unlock_view = getattr(self.window, '_unlock_view', None)
                if callable(lock_view):
                    lock_view()
                try:
                    # 先停掉上一个合约的无限历史：它订阅着 range_change，
                    # 下面 original() 里的 chart.set() 会触发回调，若不先停，
                    # 旧控制器会用旧合约数据把新合约的图重设一遍。
                    self._stop_history()
                    # ★ 切合约/切策略后必须清掉“上一根 K 线时间”的记忆：
                    #   否则新合约/新策略的时间可能**更早** → 下一拍 _refresh_market
                    #   走 on_tick → chart.update(旧时间) 被 LWC 静默拒绝 ✗
                    #   → K 线再也追不上（右侧一截没有 K 线/成交量 ✗）。
                    #   清掉后，下一拍只记录新时间（图表已由 original() 里的
                    #   chart.set(全量 frame) 填好 ✓），再往后就正常追价 ✓
                    self._last_time = None
                    # 进/出信息页都先清掉上一轮的装饰序列
                    # （合约页的入场/离场连线 + 信息页的信号 / 止损线）
                    self._clear_info_overlays()
                    self._clear_trade_lines()
                    if contract in INFO_PAGES:
                        # 先删水平线/信号（它们挂在指标线上，指标线删掉后
                        # detachPrimitive 就找不到 series 了）
                        self._clear_spans()
                        self._clear_signals()
                        # 指标表格是浮动 DOM，不属于任何序列 —— 信息页上要手动隐藏
                        self._hide_indicator_tables()
                        # 回测结果页只画「交易信号 + 止损/止盈线」，其它指标
                        # 一律清掉；各合约的止损线由 _apply_info_decorations
                        # 按合约重画（不再靠 keep_only 留下一个合约的）
                        self.indicators.clear()
                    original(contract, force=force)
                    if contract not in INFO_PAGES:
                        # 关键：这里用 fit=False —— 上面 original() 已经在
                        # chart.set() 后 fit() 并 _restore_view() 恢复了“上次离开该
                        # 图表时的可视范围”；若再 fit() 就会把它冲掉，表现为
                        # 切回图表时可视范围回到初始（全部 K 线）。
                        self._rebuild_indicators(fit=False)
                        self._setup_history()
                        self.live_tools.on_contract_changed()
                        # ★ 再恢复一次（放最后）：建 pane / 无限历史都不该改视口 ✓
                        restore = getattr(self.window, '_restore_view', None)
                        if callable(restore):
                            restore(contract)
                    else:
                        self._apply_info_decorations()
                finally:
                    if callable(unlock_view):
                        unlock_view()
                    self._switching = False

        self.window.set_contract = set_contract
        # 主题切换后重设指标颜色
        self.window.on_theme = self.recolor
        self._build_value_caches()
        self._rebuild_indicators()
        self._setup_history()

    # ------------------------------------------------------------ 设置菜单
    def reset(self) -> None:
        """「重置」：回到程序启动时的初始状态（第一个策略 + 第一个合约 + fit）。

        切回第一个策略的第一个合约，重建 K 线/指标/回测页，并重新拟合视图。
        """
        try:
            name = self.window.strategies[0]
            contracts = self.window.contracts_of(name)
            if not contracts:
                return
            first = contracts[0]
            # 置空当前合约，让 set_strategy 不因“同一合约”而短路
            self.window.contract = ''
            self.window.set_strategy(name, contract=first)
            try:
                self.chart.fit()
            except Exception:
                pass
        except Exception as error:
            print('[realtime] 重置失败:', error)

    def _first_api(self):
        """当前策略共用的 TqApi（取第一个非空的）。"""
        for strategy in self.strategies:
            api = (getattr(strategy, 'api', None)
                   or getattr(strategy, '_api', None))
            if api is not None:
                return api
        return None

    def _api_factory_from(self, api):
        if api is None:
            return None
        auth_obj = getattr(api, '_auth', None)
        user = getattr(auth_obj, '_user_name', None)
        password = getattr(auth_obj, '_password', None)
        account = getattr(api, '_account', None)
        account_cls = type(account).__name__ if account is not None else ''
        broker = getattr(account, '_broker_id', None)
        account_id = getattr(account, '_account_id', None)
        account_pwd = getattr(account, '_password', None)

        def factory():
            from tqsdk import TqApi, TqAuth, TqKq, TqAccount
            auth = TqAuth(user, password) if user else None
            if account_cls == 'TqAccount' and broker and account_id:
                acc = TqAccount(broker, account_id, account_pwd)
            else:
                acc = TqKq()
            return TqApi(acc, auth=auth)
        return factory

    def _build_api_factory(self):
        return self._api_factory_from(self._first_api())

    def _rebind_api(self, new_api) -> None:
        """把新 TqApi 接到每个策略，并重新订阅其 K 线/tick。"""
        for strategy in self.strategies:
            try:
                strategy._api = new_api
            except Exception:
                pass
            try:
                strategy._account = new_api.get_account()
            except Exception:
                pass
            for kline in (getattr(strategy, '_btklinedataset', {}) or {}).values():
                try:
                    dataset = kline._dataset
                    old = getattr(dataset, 'tq_object', None)
                    length = (len(old) if old is not None
                              else int(getattr(kline, '_preload', 0) or 0)
                              or 300)
                    dataset.tq_object = new_api.get_kline_serial(
                        kline.symbol, kline.cycle, int(length))
                    dataset.tq_tick = new_api.get_tick_serial(kline.symbol)
                except Exception as error:
                    print(f'[realtime] 重新订阅 '
                          f'{getattr(kline, "symbol", "?")} 失败:', error)

    def disconnect(self) -> None:
        """「断开连接」：关闭天勤连接、停止更新，菜单变「重新连接」。"""
        if self._disconnected:
            return
        self._disconnected = True
        # 关之前先把重连信息抓下来
        if self._api_factory is None:
            try:
                self._api_factory = self._api_factory_from(self._first_api())
            except Exception as error:
                print('[realtime] 保存天勤认证信息失败:', error)
        try:
            api = self._first_api()
            if api is not None and hasattr(api, 'close'):
                api.close()
        except Exception as error:
            print('[realtime] 关闭天勤连接失败:', error)
        try:
            self.window.replace_setting('断开连接', '重新连接', self.reconnect)
        except Exception:
            pass
        print('[realtime] 已断开天勤连接，图表停止更新（静态）')

    def reconnect(self) -> None:
        """「重新连接」：重新登录天勤并恢复更新。"""
        if not self._disconnected:
            return
        if self._api_factory is None:
            print('[realtime] 无法重新连接：没有可用的天勤认证信息')
            return
        try:
            new_api = self._api_factory()
        except Exception as error:
            print('[realtime] 重新登录天勤失败:', error)
            return
        self._rebind_api(new_api)
        self._disconnected = False
        self._last_time = None          # 用新数据的最后一根重建基准
        # 用新连接的数据重建图表
        try:
            self.window.set_contract(self.window.contract, force=True)
        except Exception as error:
            print('[realtime] 重新连接后刷新图表失败:', error)
        # 原来的 feed 线程已退出，重开一个
        try:
            import threading
            if self._loop_thread is None or not self._loop_thread.is_alive():
                self._loop_thread = threading.Thread(
                    target=self._feed_loop, args=(self._period_ms,),
                    daemon=True, name='realtime-feed')
                self._loop_thread.start()
        except Exception as error:
            print('[realtime] 重启实时更新失败:', error)
        try:
            self.window.replace_setting('重新连接', '断开连接', self.disconnect)
        except Exception:
            pass
        print('[realtime] 已重新登录天勤，恢复更新')

    def _feed_loop(self, period_ms: int) -> None:
        """实时循环：``wait_update`` 阻塞到有新数据（最多 ``period_ms``），
        数据一到就**立即**推图。

        原来这里是 ``feed(timeout=1)`` + ``sleep(period_ms)``：数据来了还
        要多等最多 ``period_ms``（默认 **1000ms**）才推，图表看起来就"慢半拍"。
        现在把 ``period_ms`` 作为 ``wait_update`` 的超时（不浪费 CPU），
        循环本身只睡一小会儿防空转。
        """
        import time
        interval = max(0.02, period_ms / 1000.0)
        while (getattr(self.chart, 'is_alive', True)
               and not self._disconnected):
            try:
                self.feed(timeout=interval)
            except Exception as error:
                print('[realtime] feed 异常:', error)
            time.sleep(0.02)          # wait_update 立即返回时别空转

    # ---------------------------------------------------------------- 内部
    def _strategy_obj(self):
        return self._by_name.get(self.window.strategy)

    def _contract_plot_id(self, contract: str) -> int:
        contracts = strategy_contracts(self._strategy_obj())
        return contracts.index(contract) if contract in contracts else 0

    def set_backtest_from_strategy(self, strategy: Any) -> None:
        """从 minibt 策略提取回测结果喂给“回测结果 / 指标记录”页。

        ★ 多策略时必须按**这个策略自己的名字 + 自己的合约**登记：以前这里用
        `self.window.contract` / `self.window.strategy`（**当前显示**的那个），
        循环登记第二个策略时会把它算到第一个策略名下（覆盖前一个），并拿第一个
        策略的合约去第二个策略的数据集里找（找不到 → 权益/回撤退回 2020 的
        假时间 → 与 K 线完全错位 ✗）。
        """
        name = next((n for n, s in self._by_name.items() if s is strategy),
                    getattr(strategy, 'sname', None)
                    or type(strategy).__name__)
        data = strategy_backtest_dict(strategy, contract=self.window.contract)
        if data:
            self.window.set_backtest(strategy=name, **data)

    @staticmethod
    def _trades_from_log(strategy, limit_time=None):
        """从 minibt logger 的 `trade_history` 取真实交易记录。

        `_record_trade` 存的是 ``{strategy_name, contract, timestamp, operation,
        price, quantity, pnl, capital, fee}`` —— 直接映射成信息页的列。
        ``limit_time`` 时只取不晚于该时间的记录（回放用）。
        """
        try:
            from minibt.logger import get_logger
            history = list(getattr(get_logger(), 'trade_history', None) or [])
        except Exception:
            return [], []
        name = getattr(strategy, 'sname', None) or type(strategy).__name__
        records, pnl_rows = [], []
        for trade in history:
            if str(trade.get('strategy_name')) != str(name):
                continue
            if (limit_time is not None
                    and str(trade.get('timestamp')) > str(limit_time)):
                continue
            pnl_value = trade.get('pnl')
            records.append({
                'time': trade.get('timestamp'),
                'direction': trade.get('operation'),
                'symbol': trade.get('contract'),
                'price': trade.get('price'),
                'qty': trade.get('quantity'),
                'fee': trade.get('fee', 0.0),
                'balance': trade.get('capital'),
                'pnl': pnl_value,
            })
            if pnl_value is not None:
                pnl_rows.append({'time': trade.get('timestamp'),
                                 'value': float(pnl_value)})
        return records, pnl_rows

    @staticmethod
    def _extract_trades(strategy, result, values, times, limit_time=None,
                        cum_values=None, symbol=None):
        """交易记录：优先用 minibt logger 的真实记录，否则从 positions 推。

        :param values: ``balance``（权益）列用的**账号权益**曲线；
        :param cum_values: ``pnl`` 用的**该合约**累计盈亏（``cum_profits``）。
            不传时退回 ``values``（单合约时两者同义 ✓）；多合约时**必须**按
            合约分别传，否则每一笔的盈亏会把别的合约的盈亏也算进来 ✗。
        :param symbol: 这些记录归属的合约名；不传时取第一个合约（旧的单合约行为）。
        """
        records, pnl_rows = RealtimeChart._trades_from_log(strategy, limit_time)
        if records:
            return records, (pd.DataFrame(pnl_rows) if pnl_rows else None)
        if result is None or 'positions' not in result.columns:
            return None, None
        if symbol is None:
            # 从 positions 推的记录以前只来自 ``_results[0]``（第一个合约）——
            # “合约”列就填它（多合约时由调用方按合约逐个传 symbol ✓）。
            try:
                symbol = str(strategy_contracts(strategy)[0])
            except Exception:
                symbol = str(getattr(strategy, 'sname', '') or '')
        positions = np.asarray(result['positions'], dtype=float)
        profits = (np.asarray(cum_values, dtype=float).ravel()
                   if cum_values is not None
                   else np.asarray(values, dtype=float).ravel())
        n = min(len(positions), len(profits), len(times))
        # 价格列：用该合约 K 线的**收盘价**（原来用的是 `values[i]`＝权益，
        # 表格里“价格”会显示成上百万的权益值）。`times`/`values` 与该合约
        # K 线逐根对齐，所以直接用同一下标即可。
        closes = None
        try:
            _frame = strategy_kline_frame(strategy, symbol)
            if _frame is not None and len(_frame) >= n:
                closes = np.asarray(_frame['close'].astype(float))[:n]
        except Exception:
            closes = None

        def _price(i, fallback):
            if closes is not None and 0 <= i < closes.size \
                    and np.isfinite(closes[i]):
                return float(closes[i])
            return float(fallback)

        def _balance(i):
            if i < len(values):
                return float(values[i])
            return float(profits[i])

        records, pnl_rows = [], []
        entry_value, entry_pos = profits[0], positions[0]
        fee = 0.0
        if 'total_fee' in result.columns:
            try:
                fee = float(result['total_fee'].iloc[-1]) / max(len(values), 1)
            except Exception:
                fee = 0.0
        for i in range(1, n):
            prev, cur = positions[i - 1], positions[i]
            # 盈亏散点：**每一根累计盈亏有变化**就画一个点，值 = 这一根的变化 ——
            # 开仓那根就是 ``-手续费``（用户要求：0 轴下方 -1.0 有个圆点 ✓）；
            # 而且「盘中来回」（开+平同一根、期末净仓位没变）也不会漏（cum 变化
            # 仍能捕获）✓。``kind`` 标记事件类型：统计口径只认 ``close`` ✓
            diff = float(profits[i] - profits[i - 1])
            if abs(diff) > 1e-12:
                if cur != 0 and (prev == 0 or abs(cur) > abs(prev)):
                    kind = 'open'
                elif prev != 0 and (cur == 0 or abs(cur) < abs(prev)):
                    kind = 'close'
                elif prev * cur < 0:
                    kind = 'close'          # 反手：按“平旧仓”归类
                else:
                    kind = 'other'
                pnl_rows.append({'time': times[i], 'value': diff, 'kind': kind})
            if cur == entry_pos:
                continue
            closing = abs(cur) < abs(entry_pos) or cur * entry_pos < 0
            if closing:
                pnl_value = float(profits[i] - entry_value)
                records.append({'time': times[i],
                                'direction': '平多' if entry_pos > 0 else '平空',
                                'symbol': symbol,
                                'price': _price(i, entry_value),
                                'qty': abs(entry_pos), 'fee': fee,
                                'balance': _balance(i),
                                'pnl': pnl_value})
                # 平仓后基准更新（此时已空仓）—— 下一笔从平仓后的权益起算 ✓
                entry_value = profits[i]
            else:
                # 开/加仓：**保留开仓前的基准**（"entry_value" 不重置）——
                # 这样交易记录里的本笔盈亏仍含开仓手续费 ✓
                records.append({'time': times[i],
                                'direction': '开多' if cur > 0 else '开空',
                                'symbol': symbol,
                                'price': _price(i, profits[i]),
                                'qty': abs(cur), 'fee': fee,
                                'balance': _balance(i), 'pnl': None})
            entry_pos = cur
        pnl = pd.DataFrame(pnl_rows) if pnl_rows else None
        return (records or None), pnl

    @staticmethod
    def _extract_all_contract_trades(strategy, results, values, times,
                                     limit_time=None):
        """**多合约**：每个合约各自从 positions 推交易记录，合并后按时间排序。

        * 以前只取 ``results[0]`` —— 第二个及以后合约的交易记录全丢 ✗；
        * 盈亏用**该合约**的 ``cum_profits`` 差值（账号权益是几个合约共享的，
          直接拿权益差值算会把别的合约的盈亏算进来 ✗）；
        * 两个合约的时间可能不一致 → 合并后按时间重新排序（盈亏散点也一起排）✓。
        """
        contracts = strategy_contracts(strategy)
        records: list = []
        pnl_frames: list = []
        for plot_id, result in enumerate(results or []):
            contract = (contracts[plot_id]
                        if plot_id < len(contracts) else '')
            try:
                frame = strategy_kline_frame(strategy, contract)
                ctimes = list(frame['time'])
            except Exception:
                ctimes = times
            cum = (result['cum_profits']
                   if 'cum_profits' in getattr(result, 'columns', [])
                   else None)
            recs, pnl_df = RealtimeChart._extract_trades(
                strategy, result, values, ctimes, limit_time,
                cum_values=cum, symbol=contract)
            if recs:
                records.extend(recs)
            if pnl_df is not None and len(pnl_df):
                pnl_frames.append(pnl_df)
        if records:
            records.sort(key=lambda r: pd.Timestamp(r.get('time')))
        if pnl_frames:
            pnl = (pd.concat(pnl_frames, ignore_index=True)
                   .sort_values('time').reset_index(drop=True))
        else:
            pnl = None
        return (records or None), pnl

    @staticmethod
    def _extract_stats(strategy, values, drawdown_values, pnl):
        """根据权益/交易现算回测指标（键与 StatsTable.DEFAULT_ROWS 一致）。"""
        final_profit = float(values[-1] - values[0])
        # 散点含「开仓（-手续费）」和「平仓」两类事件；统计要把它们**配对成整笔**
        # （开仓事件 + 它后面的平仓事件），否则每笔会少算开仓手续费，和打印报表
        # `_compute_backtest_metrics` 的 avg_return/avg_win/avg_loss 对不上 ✗。
        trade_pnls = []
        if pnl is not None and 'kind' in getattr(pnl, 'columns', []):
            pending = 0.0
            for kind, value in zip(pnl['kind'], pnl['value']):
                v = float(value)
                if kind == 'open':
                    pending += v              # 开/加仓：累计手续费（及偏差）
                elif kind == 'close':
                    trade_pnls.append(pending + v)   # 整笔 = 开 + 平
                    pending = 0.0
            pnl_values = np.asarray(trade_pnls, dtype=float)
        else:
            pnl_values = (np.asarray(pnl['value'], dtype=float)
                          if pnl is not None and len(pnl) else np.array([]))
        wins = pnl_values[pnl_values > 0] if pnl_values.size else np.array([])
        losses = pnl_values[pnl_values <= 0] if pnl_values.size else np.array([])
        mean = float(pnl_values.mean()) if pnl_values.size else 0.0
        std = float(pnl_values.std()) if pnl_values.size else 0.0
        total_return = (float(values[-1] / values[0] - 1.0)
                        if values[0] else 0.0)
        # 手续费：取 `_results[0]['total_fee']` 的累计值；拿不到就 0。
        # （原来写的是 `float(getattr(strategy, '_results', None) and 0.0)` ——
        #   `_results` 为空列表时 `[] and 0.0` 就是 `[]`，`float([])` 直接抛，
        #   导致整个「回测结果 / 指标记录」登记失败。）
        total_fee = 0.0
        try:
            results = getattr(strategy, '_results', None) or []
            result = results[0] if len(results) else None
            if result is not None and 'total_fee' in result.columns:
                total_fee = float(result['total_fee'].iloc[-1])
        except Exception:
            total_fee = 0.0
        stats = {
            'final_profit': final_profit,
            'total_return': total_return,
            'total_fee': total_fee,
            'profit_ratio': float(wins.sum() / abs(losses.sum())
                                  if losses.size and losses.sum() else 0.0),
            'mean_profit': mean,
            'sharpe_ratio': float(mean / std * np.sqrt(pnl_values.size)
                                  if std else 0.0),
            'max_drawdown': float(-drawdown_values.min()
                                  if drawdown_values.size else 0.0),
            'var': float(np.percentile(pnl_values, 5)
                         if pnl_values.size else 0.0),
            'risk_return_ratio': float(mean / std if std else 0.0),
            'win_rate': float(wins.size / pnl_values.size
                              if pnl_values.size else 0.0),
            'win_count': int(wins.size), 'loss_count': int(losses.size),
            'total_trades': int(pnl_values.size),
            'mean_loss': float(losses.mean()) if losses.size else 0.0,
        }
        results = getattr(strategy, '_results', None)
        if results and 'total_fee' in getattr(results[0], 'columns', []):
            try:
                stats['total_fee'] = float(results[0]['total_fee'].iloc[-1])
            except Exception:
                pass
        return stats

    def _apply_param_overrides(self, strategy) -> None:
        """把用户在图例「参数」页改过的参数套回当前合约的指标。

        实盘策略每拍都用**原始参数**重建 ``_btindicatordataset``，所以要跨重建
        重新套用；参数与当前一致时跳过（静态图第二次重建就无需再算 ✓）。
        """
        overrides = getattr(self, '_param_overrides', None)
        if not overrides:
            return
        datasets = getattr(strategy, '_btindicatordataset', None)
        if not datasets:
            return
        contract = self.window.contract
        changed = False
        for (key_contract, source), params in list(overrides.items()):
            if key_contract != contract:
                continue
            dataset = datasets.get(source)
            if dataset is None:
                continue
            current = dict(getattr(dataset, 'params', None) or {})
            if indicator_menu.params_equal(current, params):
                continue
            new = indicator_menu.recompute_indicator(dataset, params)
            if new is None:
                print(f'[realtime] 参数覆盖重算失败: {source}')
                continue
            try:
                if hasattr(datasets, 'add_data'):
                    datasets.add_data(source, new)
                else:
                    datasets[source] = new
                changed = True
            except Exception as error:                           # noqa: BLE001
                print(f'[realtime] 参数覆盖替换失败: {error!r}')
        if changed:
            try:
                self._value_cache.clear()
            except Exception:                                    # noqa: BLE001
                pass

    def _on_indicator_params(self, spec, values: dict) -> None:
        """指标图例菜单「参数」页确认 → 记录参数并重算重画。

        仅在 ``config.indicator_recompute = True`` 时参数页才出现。参数记到
        ``_param_overrides``（跨重建保留），实际重算交给
        :meth:`_apply_param_overrides`，避免重复计算 ✓。
        """
        strategy = self._strategy_obj()
        datasets = getattr(strategy, '_btindicatordataset', None) or {}
        source = getattr(spec, 'source', '')
        dataset = datasets.get(source)
        if dataset is None:
            print(f'[realtime] 找不到指标 {source!r}，无法重算参数')
            return
        current = dict(getattr(dataset, 'params', None) or {})
        # ★ 只接受**标量**改动：菜单回传的是字符串，KLine / DataFrame 这类对比
        #   合约参数必须原样保留（否则 sym1 被 str(kline) 覆盖 → 背离线全没了 ✗）
        current.update(indicator_menu.sanitize_params(dataset, values or {}))
        self._param_overrides[(self.window.contract, source)] = current
        try:
            self._value_cache.clear()
        except Exception:                                        # noqa: BLE001
            pass
        with self._lock:
            self._rebuild_indicators(fit=False)
        print(f'[realtime] 指标 {getattr(spec, "name", "")!r} 参数已更新并重算 ✓')

    def _rebuild_indicators(self, fit: bool = True) -> None:
        frame = self.chart.candle_data
        if frame is None or frame.empty:
            return
        # 关键顺序：水平线是挂在**指标线**上的 primitive，必须先 detach，
        # 再让 `indicators.build()` 删掉旧指标线；否则 detachPrimitive 会
        # 指向一个已经删掉的 `series`（主题切换时就会刷一堆 resolve 报错）。
        self._clear_spans()
        self._clear_signals()               # 换策略/合约：先删干净上一个合约的信号
        self._clear_trade_lines()           # 真实成交的入场/离场连线（下面重画）
        strategy = self._strategy_obj()
        # 用户改过的指标参数 → 套回（实盘每拍重建，必须重新套用 ✓）
        self._apply_param_overrides(strategy)
        contract = self.window.contract
        name = self.window.strategy
        plot_id = self._contract_plot_id(contract)
        specs = extract_minibt_indicators(strategy, plot_id)
        # config.indicator_recompute 开启：给每条 spec 带上「参数」页数据
        if indicator_menu.recompute_enabled(strategy):
            _datasets = getattr(strategy, '_btindicatordataset', {}) or {}
            for spec in specs:
                dataset = _datasets.get(spec.source)
                if dataset is not None:
                    spec.menu_params = indicator_menu.params_for_dataset(
                        dataset)
        # 接上全量指标值缓存（实盘策略只算 preload 根，历史部分靠它）
        missing = 0
        for spec in specs:
            spec.cached_series = self._value_cache.get(
                (name, contract, spec.source, spec.name))
            if spec.cached_series is None:
                missing += 1
        # ★ 缓存接不上 → 历史段只能拿“活值”尾部对齐 → 图上只剩最近几根 ✗。
        #   一个 策略/合约 组合只提示一次，方便定位“历史指标线不见了” ✓
        if missing:
            warned = getattr(self, '_cache_warned', None)
            if warned is None:
                warned = self._cache_warned = set()
            if (name, contract) not in warned:
                warned.add((name, contract))
                print(f'[realtime] 指标历史缓存缺失 {missing}/{len(specs)} 条'
                      f'（{name} / {contract}）'
                      f'→ 历史指标线会只剩最近几根 ✗')
        self.indicators.build(frame, specs, self.palette())
        indicator_menu.enable_collapse(self.chart)
        apply_bands(self.indicators,
                    extract_minibt_bands(strategy, plot_id))
        apply_histogram_colors(self.indicators, strategy,
                               contract, len(frame), frame=frame)
        apply_color_rules(self.chart, self.indicators, strategy,
                          self.window.contract)
        self.spans = apply_spans(
            self.chart, extract_minibt_spans(strategy, plot_id),
            self.indicators)
        # 指标自带的统计表格（如 MachineLearningRSI 的左上角表）
        self._indicator_tables = apply_indicator_tables(
            self.chart, strategy, contract,
            getattr(self, '_indicator_tables', None), plot_id=plot_id,
            align=getattr(self.indicators, 'align', 'head'))
        if fit:
            self.chart.fit()
        self.window._apply_pane_stretch()     # 主图 : 副图 = 3:1
        apply_watermark(
            self.chart, strategy_watermark(strategy, self.window.contract))
        apply_candlestyle(
            self.chart,
            extract_minibt_candlestyle(strategy, self.window.contract))
        try:
            self.chart.clear_markers()
        except Exception:
            pass
        self.signal_manager.build(
            frame, extract_minibt_signals(strategy, plot_id,
                                          self.indicators.panes),
            extract_minibt_signal_texts(
                strategy, plot_id,
                getattr(self, '_theme_name', 'dark') == 'dark'))
        # 真实成交的**入场→离场连线 + 四类交易标记**（复刻 bokeh）
        if getattr(self, '_trade_signal', False):
            self._trade_series = draw_trade_lines(
                self.chart, strategy, contract, frame, 0,
                theme_dark=getattr(self, '_theme_name', 'dark') == 'dark')
        # 新建的 legend 行 / 副图 pane 不会自动继承整窗主题：重建完再补一次
        # （否则浅色主题下左上角指标标签会变灰）。
        self.window._reapply_theme()
        self._reapply_legend_color()

    def _legend_visible(self, source: str) -> bool:
        """指标的图例开关：``ind.legend = False`` / 类属性 ``legend = False`` ✓。

        指标对象按 ``spec.source`` **现取**（实盘里每次 ``__call__`` 都会重建指标
        对象，捕获实例会拿到旧的 ✗）。取不到 / 没有该属性 → ``True``（老行为 ✓）。
        """
        return indicator_legend_visible(self, source)

    def _reapply_legend_color(self) -> None:
        """把主题的文字色重新应用到图例（新建的图例行默认是灰的）。"""
        theme = getattr(getattr(self.chart, 'win', None), '_theme', None)
        if not theme:
            return
        options = getattr(self.chart, '_legend_options', None)
        if not options:
            return
        try:
            self.chart.legend(**{**options, 'color': theme.get('text', '')})
        except Exception:
            pass

    def _clear_spans(self) -> None:
        """删掉上一次画的水平线（`_rebuild_indicators` 会重建）。"""
        for span in getattr(self, 'spans', []):
            try:
                span.delete()
            except Exception:
                pass
        self.spans = []

    def _clear_signals(self) -> None:
        """删掉上一次画的交易信号（自定义序列）。

        信号不再用 LWC 的 marker primitive，而是 `add_custom_series` 建的序列；
        切策略/合约/信息页时如果不主动删，旧合约的信号会一直留在图上。
        """
        self.signal_manager.clear()

    def _refresh_signals(self) -> None:
        """用**最新**的信号值重画所有信号（tick 出现/消失、换根最后两根修正）。

        用 `chart.candle_data`（当前视图，末尾就是最新一根）配对时间；
        `SignalManager.update` 会取信号值的最后 n 个，保证与视图对齐。
        """
        strategy = self._strategy_obj()
        contract = self.window.contract
        if strategy is None or contract in INFO_PAGES:
            return
        frame = self.chart.candle_data
        if frame is None or frame.empty:
            return
        plot_id = self._contract_plot_id(contract)
        times = IndicatorManager._frame_seconds(frame)
        name = self.window.strategy
        signals = []
        for col, values, style, anchor, pane, *_rest in extract_minibt_signals(
                strategy, plot_id, self.indicators.panes):
            cached = self._value_cache.get((name, contract, 'signal', col))
            if cached is not None and len(cached):
                try:
                    got = cached.reindex(times).to_numpy(dtype=float)
                    if not np.isfinite(got).any():
                        # ★ 缓存按时间一个都对不上（时间轴不一致 / 缓存未覆盖）→
                        #   退回**活值**尾部对齐 ✓。否则整条信号会变 NaN，
                        #   `set(空)` 把标记/文本整段清掉且**再也回不来** ✗
                        got = _align_values(values, len(frame), 'tail')
                    values = got
                except Exception:
                    values = np.asarray(values, dtype=float).ravel()
            signals.append((col, values, style, anchor, pane))
        self.signal_manager.update(
            frame, signals,
            extract_minibt_signal_texts(
                strategy, plot_id,
                getattr(self, '_theme_name', 'dark') == 'dark'))

    def _hide_indicator_tables(self) -> None:
        """隐藏所有指标表格（回测结果/指标记录页没有指标线）。"""
        for table in (getattr(self, '_indicator_tables', None) or {}).values():
            try:
                table.visible(False)
            except Exception:
                pass
        if getattr(self.chart, '_indicator_table_crosshair', None) is not None:
            try:
                self.chart.events.crosshair_move.unsubscribe()
            except Exception:
                pass
            self.chart._indicator_table_crosshair = None

    def _refresh_indicator_tables(self) -> None:
        """实时/回放推进后刷新指标表格（`TableStyle.live` 表格显示最新值）。

        表格内容由 `apply_indicator_tables` 存的刷新闭包重建；光标在最后一根
        或已移出 K 线时显示最新值，停在某根时保持显示那根的值（`_indicator_table_cursor`）。
        """
        refresh = getattr(self.chart, '_refresh_indicator_tables', None)
        if refresh is None:
            return
        try:
            refresh(getattr(self.chart, '_indicator_table_cursor', None))
        except Exception as error:
            print('[realtime] 指标表格刷新失败:', error)

    # ------------------------------------------------------ 无限历史
    def _setup_history(self) -> None:
        """给当前合约装一个“向左滚动加载更早历史”的控制器。

        全量 K 线放在 `self._full_frames[contract]`（`get_kline` 底层取了
        10000 根），窗口开局只放出 `preload` 根。每拉一批就把 K 线、指标、
        色带/颜色和信号一起对齐到新视图。
        """
        self._stop_history()
        contract = self.window.contract
        if contract in INFO_PAGES:
            return
        full = self._full_frames.get(contract)
        view = self.chart.candle_data
        if full is None or full.empty or view is None or view.empty:
            return
        if len(full) <= len(view):          # 全量已经全部放出 -> 没有更早的了
            return
        strategy = self._strategy_obj()

        def load(count, before=None):
            data = self._full_frames.get(contract, full)
            if before is not None:
                data = data[data['time'] < pd.Timestamp(before)]
            return data.tail(int(count))

        try:
            self._history = RealtimeHistory(
                self.chart, load,
                page=max(50, strategy_preload(strategy, contract)
                         or DEFAULT_PRELOAD),
                threshold=20, on_load=self._on_history_load)
        except Exception as error:
            print('[realtime] 无限历史启动失败:', error)

    def _stop_history(self) -> None:
        """停掉当前无限历史控制器（切合约/切页面时先调）。"""
        history = getattr(self, '_history', None)
        if history is not None:
            try:
                history.stop()
            except Exception:
                pass
        self._history = None

    def _build_value_caches(self) -> None:
        """开局（所有策略 KLine 还是全量）把全量指标值按时间缓存下来。

        实盘 `_inplace_values` 之后策略只保留 `preload` 根（否则 10000 根 ×
        多策略每 tick 重算要命），历史部分的指标值就靠这份缓存；否则无限
        历史向前翻页时老 K 线的指标会是 NaN。
        """
        for name, strategy in self._by_name.items():
            contracts = strategy_contracts(strategy)
            for plot_id, contract in enumerate(contracts):
                full = self._full_frames.get(contract)
                if full is None or full.empty:
                    try:
                        full = strategy_kline_frame(strategy, contract)
                    except Exception:
                        full = None
                    self._full_frames[contract] = full
                if full is None or full.empty:
                    continue
                times = IndicatorManager._frame_seconds(full)
                for spec in extract_minibt_indicators(strategy, plot_id):
                    if spec.get_values is None:    # 蜡烛型指标没有单条值
                        continue
                    values = spec.get_values()
                    if values is None:
                        continue
                    values = np.asarray(values, dtype=float).ravel()
                    # ★ 长度不等**不能直接丢**：指标比 K 线短（预热窗口 / 参数
                    #   窗口 / 止挽线只覆盖部分根）时，以前 ``continue`` → 这条线
                    #   的历史永远进不了缓存 → 实时重建时 ``cached_series=None``，
                    #   只画最近的活值（“历史指标线全不见了”✗）。前补 NaN 对齐 ✓
                    if len(values) < len(times):
                        values = np.concatenate(
                            [np.full(len(times) - len(values), np.nan),
                             values])
                    elif len(values) > len(times):
                        values = values[-len(times):]
                    self._value_cache[
                        (name, contract, spec.source, spec.name)] = \
                        pd.Series(values, index=times)
                for col, values, _style, *_rest in extract_minibt_signals(
                        strategy, plot_id):
                    values = np.asarray(values, dtype=float).ravel()
                    if len(values) < len(times):
                        values = np.concatenate(
                            [np.full(len(times) - len(values), np.nan),
                             values])
                    elif len(values) > len(times):
                        values = values[-len(times):]
                    self._value_cache[
                        (name, contract, 'signal', col)] = \
                        pd.Series(values, index=times)

    def _sync_history_cache(self) -> None:
        """把当前合约的**最新实盘值**按时间写回全量缓存（尾部，可**追加** ✓）。"""
        strategy = self._strategy_obj()
        contract = self.window.contract
        if strategy is None or contract in INFO_PAGES:
            return
        frame = strategy_kline_frame(strategy, contract)
        if frame is None or frame.empty:
            return
        times = IndicatorManager._frame_seconds(frame)
        name = self.window.strategy
        self._probe_indicator_values(times, name, contract)
        for spec in self.indicators.specs:
            cached = spec.cached_series
            if cached is None:
                continue
            live = self.indicators.live_values(spec)
            if live is None or not len(live):
                continue
            self._write_cache_tail((name, contract, spec.source, spec.name),
                                   cached, times, live)
        # 信号值也按时间写回缓存（历史部分的信号不能缺）
        plot_id = self._contract_plot_id(contract)
        for col, values, _style, *_rest in extract_minibt_signals(
                strategy, plot_id):
            key = (name, contract, 'signal', col)
            cached = self._value_cache.get(key)
            if cached is None:
                continue
            self._write_cache_tail(key, cached, times,
                                   np.asarray(values, dtype=float).ravel())

    def _probe_indicator_values(self, times, name, contract) -> None:
        """换根时打一行探针：**活值**与**缓存**的尾部都要看一眼。

        用来一眼分清“新 K 线指标值是常数”的根因在哪边：

        * ``活值尾部`` 本身就是同一个数 → **策略侧**的实时值没跟上（重算窗口 /
          增量重算的问题），图表侧救不了 ✗；
        * ``活值尾部`` 正常但 ``缓存尾部`` 是常数 → 图表侧的写回/对齐错了 ✗。
        """
        try:
            count = len(times)
            if getattr(self, '_probe_bar_count', None) == count:
                return                       # 只在**换根**时打（tick 不打 ✓）
            self._probe_bar_count = count
            spec = next((s for s in self.indicators.specs
                         if not getattr(s, 'hidden', False)), None)
            if spec is None:
                return
            live = self.indicators.live_values(spec)
            cached = getattr(spec, 'cached_series', None)

            def _tail(arr):
                if arr is None:
                    return '-'
                arr = np.asarray(arr, dtype=float).ravel()[-5:]
                return [None if not np.isfinite(v) else round(float(v), 4)
                        for v in arr]

            print(f'[realtime] 指标探针 {name}/{contract} '
                  f'{spec.source}.{spec.name} bars={count} '
                  f'live_len={0 if live is None else len(live)} '
                  f'cache_len={0 if cached is None else len(cached)}')
            print(f'[realtime]   活值尾部  = {_tail(live)}')
            print('[realtime]   缓存尾部  = '
                  f'{_tail(None if cached is None else cached.to_numpy())}')
        except Exception:                                        # noqa: BLE001
            pass

    def _write_cache_tail(self, key, cached, times, live) -> None:
        """把 live 值写回缓存尾部；**比缓存更晚的时间戳就追加**（保持升序 ✓）。

        ⚠ 以前只写“缓存里**已经存在**”的标签（怕 ``.loc[新标签] =`` 把索引
        撑乱 ✗），而缓存是**开局建好就冻结**的（``_build_value_caches`` 只调
        一次）—— 于是**新 K 线永远进不了缓存**：切换策略 / 向前翻页后，
        ``reindex`` 取那些新时间段全是 NaN，表现为「右侧数据缺失」✗（用户反馈）。

        现在只追加**比缓存最后一根更晚**的时间戳（更早的属于历史，仍交给无限
        历史 ✓）：既不打乱顺序也不重复，已存在的标签照旧原地更新 ✓。
        """
        n = min(len(live), len(times))
        if n <= 0:
            return
        labels = pd.Index(times)[-n:]
        values = np.asarray(live, dtype=float).ravel()[-n:]
        try:
            known = labels.isin(cached.index)
            if known.any():
                cached.loc[labels[known]] = values[known]
            fresh = labels[~known]
            if len(fresh) and len(cached.index):
                fresh = fresh[fresh > cached.index[-1]]     # 只追加更晚的 ✓
            if not len(fresh):
                return
            extra = pd.Series(values[~known][-len(fresh):], index=fresh)
            merged = pd.concat([cached, extra])
            merged = merged[~merged.index.duplicated(keep='last')]
            self._value_cache[key] = merged
            # 已经建好的 spec 拿的是**旧对象**，也要换过来（否则下一拍还是旧值 ✗）
            for spec in self.indicators.specs:
                if getattr(spec, 'cached_series', None) is cached and \
                        (spec.source, spec.name) == key[-2:]:
                    spec.cached_series = merged
        except Exception:                                        # noqa: BLE001
            pass

    def _on_history_load(self, history, added: int) -> None:
        """向左补了一批历史：K 线由 `InfiniteHistory` 自己 set 好了，
        这里把 minibt 指标/颜色/色带/信号也对齐到新视图。"""
        with self._lock:
            self._sync_history_view(added)

    def _sync_history_view(self, added: int) -> None:
        view = self.chart.candle_data
        if view is None or view.empty:
            return
        # 图表显示的总是最新的 len(view) 根，所以指标取尾部对齐
        self.indicators.reslice(view)
        strategy = self._strategy_obj()
        contract = self.window.contract
        apply_histogram_colors(self.indicators, strategy, contract,
                               len(view), frame=view)
        apply_color_rules(self.chart, self.indicators, strategy, contract)
        self._refresh_signals()
        self._refresh_indicator_tables()

    def palette(self) -> List[str]:
        """按当前主题选深/浅两组指标默认颜色。"""
        return palette_for(self.chart)

    def recolor(self) -> None:
        """主题切换后重设指标颜色（重建序列，图例小方块也会跟着变）。"""
        if self.window.contract in INFO_PAGES:
            # 信息页只保留「信号 + 止损/止盈线」，整组按新主题重建一次
            self._apply_info_decorations()
            return
        with self._lock:
            self._rebuild_indicators(fit=False)

    # ------------------------------------------------ 回测结果页的装饰
    def _clear_trade_lines(self) -> None:
        """删掉合约页上的入场/离场连线 + 交易标记。"""
        for series in getattr(self, '_trade_series', []):
            try:
                series.delete()
            except Exception:
                pass
        self._trade_series = []

    def _clear_info_overlays(self) -> None:
        """删掉回测结果页的装饰序列（各合约的信号 + 止损/止盈线）。"""
        for series in getattr(self, '_info_overlays', []):
            try:
                series.delete()
            except Exception:
                pass
        self._info_overlays = []

    def _decorate_contract_info(self, strategy, contract: str, frame,
                                pane: int, palette=()) -> None:
        """给某个合约的 K 线画上**它自己**的交易信号 + 止损/止盈线。

        `pane` 就是该合约 K 线所在的 pane（主图第一个合约为 0，其余合约各自
        的副图 pane）—— 信号/停止线必须画回同一个 pane，否则会跑到别的
        价格轴上。具体画法见模块级 :func:`draw_contract_info`（回放窗口共用）。
        """
        dark = getattr(self, '_theme_name', 'dark') == 'dark'
        self._info_overlays.extend(draw_contract_info(
            self.chart, strategy, contract, frame, pane,
            palette=palette, theme_dark=dark,
            panes=self.indicators.panes))
        # 真实成交的入场/离场连线（每个合约都画它自己那份 ✓）
        if getattr(self, '_trade_signal', False):
            self._info_overlays.extend(draw_trade_lines(
                self.chart, strategy, contract, frame, pane,
                theme_dark=dark))

    def _apply_info_decorations(self) -> None:
        """回测结果页：每根 K 线只画它自己的**交易信号 + 止损/止盈线**。

        其它指标一律不画（副图已有权益 / 回撤 / 盈亏，信号 + 停止线足够看清
        在哪里买卖）。之前只给主图（第一个合约）画、且用页面名当合约导致
        `plot_id` 恒为 0，所以第二个合约既没有信号也没有止损线。
        """
        strategy = self._strategy_obj()
        if strategy is None:
            return
        contracts = strategy_contracts(strategy)
        if not contracts:
            return
        self._clear_info_overlays()
        palette = self.palette()
        main = self.window._main_contract() or contracts[0]
        try:
            apply_candlestyle(
                self.chart, extract_minibt_candlestyle(strategy, main))
            apply_watermark(
                self.chart, strategy_watermark(strategy, main))
        except Exception as error:
            print('[realtime] 信息页 K 线装饰失败:', error)
        # 主图 = 第一个合约
        try:
            self._decorate_contract_info(strategy, main,
                                         self.chart.candle_data, 0, palette)
            # 其余合约各画到自己的 pane 上
            for series, contract in zip(
                    self.window.contract_panes,
                    getattr(self.window, '_contract_panes_for', None) or ()):
                if contract not in contracts:
                    continue
                frame = getattr(series, 'data', None)
                if frame is None or len(frame) == 0:
                    frame = self.window.loader(strategy, contract)
                self._decorate_contract_info(strategy, contract, frame,
                                             int(series.pane_index), palette)
        except Exception as error:
            print('[realtime] 信息页装饰应用失败:', error)

    # ---------------------------------------------------------------- 行情
    def on_tick(self, time: Any, price: float,
                volume: Optional[float] = None,
                indicator_offset: int = 0,
                push_indicators: bool = True) -> None:
        row = {'time': time, 'price': float(price)}
        if volume is not None:
            row['volume'] = float(volume)
        self.chart.update_from_tick(pd.Series(row))
        self._last_time = time
        if push_indicators:
            self.indicators.update_last(time, offset=indicator_offset)

    def on_bar(self, row: Any, indicator_offset: int = 0,
               push_indicators: bool = True,
               historical: bool = False) -> None:
        """把一根 K 线 + 指标点推给图表。

        :param historical: 这根是**已经在图上的旧根**（换根时回填上一根：它的
            收盘价/指标值要用“最后一拍”重算的值改写 ✓）。必须把
            ``historical_update=True`` 透传给引擎，否则 LWC 的 ``update``
            只接受最后一根，旧根会被**静默丢掉** ✗。
        """
        if isinstance(row, dict):
            row = pd.Series(row)
        try:
            self.chart.update(row, historical_update=historical)
        except TypeError:                     # 旧后端没有这个参数 ✓
            self.chart.update(row)
        self._last_time = row.get('time')
        if push_indicators:
            self.indicators.update_last(self._last_time,
                                        offset=indicator_offset,
                                        historical=historical)

    # ------------------------------------------------------------ 实时更新
    def feed(self, timeout: float = 1) -> None:
        """一次实时更新：``wait_update`` -> 跑策略（重算指标）-> 推进 K 线/指标。

        参考 `light_chart.py`：
        * `kline_loop`：`wait_update(1)` + `kline_update()`；
        * `indicator_loop`：`self.strategy()` + `indicator_update()`。
        这里合并成一次：先 `wait_update`，再**调用策略**（实盘模式 = ``_execute_live_trading()``，
        里面会 ``_strategy_init()`` 重算指标），最后推 K 线 + 指标。
        """
        strategy = self._strategy_obj()
        if strategy is None:
            return
        if self._disconnected:    # 已断开天勤连接：不再拉取/重算
            return
        if self._switching:        # 切合约/切主题中，先别动图表
            return
        try:
            is_open = strategy.wait_update(timeout)
        except Exception as error:
            print('[realtime] wait_update 失败:', error)
            return
        if not is_open:
            return        # 收盘/无新数据（等价 indicator_loop 的 if self._is_update）
        # 与主线程的切合约/切主题串行：两边都会删/建 series、发脚本，
        # 并发跑会把 series/pane 交叉，越快切越卡。
        with self._lock:
            if self._switching:
                return
            # 变更检测：`wait_update` 返回可能只是账户/委托变了，行情没动。
            # 只对行情真的变了的策略重算指标 + 跑 step（没变的重算也是白算）。
            current = self._strategy_obj()
            current_changed = False
            for item in self.strategies:
                changed = self._market_changed(item)
                if changed and item is current:
                    current_changed = True
                if not changed and _DEBUG:
                    print('[realtime] 行情未变，只跑 step:',
                          getattr(item, 'sname', '') or type(item).__name__)
                try:
                    # 行情没变：跳过 `_inplace_values` + 指标重算，但仍跑
                    # `step()` 做风控/下单（C：拆分实盘主方法）。
                    if getattr(item, '_is_live_trading', False):
                        item(step_only=not changed)
                    else:
                        item()
                except Exception as error:
                    print('[realtime] 策略执行异常:', error)
            # K 线**每轮都推**（与行情有没有变、与节流都无关）：价格线要跟手。
            # 指标/信号昂贵，只在行情真的变了时才算/推（`_market_changed`）。
            if current_changed:
                self._refresh_market()
                self._sync_history_cache()
            elif self._should_push():
                self._refresh_market(push_indicators=False)
            # 实时工具（买卖盘 / 持仓线 / 二级报价）：每拍刷新（只刷显示中的 ✓）
            # ⚠ 测试 / 非实时构造可能没建 ``live_tools``，别直接属性访问 ✗
            live_tools = getattr(self, 'live_tools', None)
            if live_tools is not None:
                live_tools.refresh()

    @staticmethod
    def _market_changed(strategy) -> bool:
        """天勤：本策略任一 K 线在最近一次 `wait_update` 里变了吗？

        返回 False 就跳过 `_inplace_values` + 整轮指标重算（账户/委托变动
        不应触发重算）。拿不到天勤 API / `is_changing` 时返回 True，
        保持原来的“每次都算”行为。
        """
        try:
            api = getattr(strategy, 'api', None) or getattr(
                strategy, '_api', None)
            is_changing = getattr(api, 'is_changing', None)
            klines = getattr(strategy, '_btklinedataset', None) or {}
        except Exception:
            return True
        if is_changing is None or not klines:
            return True
        for kline in klines.values():
            tq = getattr(getattr(kline, '_dataset', None), 'tq_object', None)
            if tq is None:
                return True
            try:
                if is_changing(tq):
                    return True
            except Exception:
                return True
        return False

    def _should_push(self) -> bool:
        """行情没变时是否还要推一次 K 线（默认**不限速**：每轮都推）。

        ``push_interval_ms`` 默认 **0** = 不节流（K 线要跟手）；想降桥接压力
        可以显式给个毫秒数（如 100），那时只限 K 线，指标仍只在行情变了时算。
        """
        import time as _time
        now = _time.monotonic()
        if self._push_interval <= 0 or (now - self._last_push) >= self._push_interval:
            self._last_push = now
            return True
        if self._pending_new_bar():
            self._last_push = now
            return True
        return False

    def _pending_new_bar(self) -> bool:
        """当前合约是否已经有比图表更新的新 K 线（有就立即推，不等节流）。"""
        if self._last_time is None:
            return True
        strategy = self._strategy_obj()
        contract = self.window.contract
        for kline in (getattr(strategy, '_btklinedataset', {}) or {}).values():
            if f'{kline.symbol}_{kline.cycle}' != contract:
                continue
            tq = getattr(getattr(kline, '_dataset', None), 'tq_object', None)
            if tq is None or not len(tq):
                return True
            try:
                # 天勤 `datetime` 是纳秒整数；`pd.Timestamp(int)` 按纳秒解析，
                # 与 `strategy_kline_frame` 里的 `pd.to_datetime(datetime)` 一致。
                when = pd.Timestamp(int(tq['datetime'].iloc[-1]))
            except Exception:
                return True
            return when > pd.Timestamp(self._last_time)
        return True

    def _refresh_market(self, push_indicators: bool = True) -> None:
        """把当前合约的最新行情推给图表（等价 kline_update）。

        换根时：**上一根**（上次那根形成中的 K 线）用最新计算值重新回填，
        再把从它之后到最新一根之间的**所有新根**逐根追加。这样即使策略算
        指标比较慢、一次 feed 跳过了好几根，K 线和指标也不会漏根/错位。
        tick（还在同一根里）则只更新最后一根。

        ``push_indicators=False``：只推 K 线，不算/不推指标与信号 —— K 线现在
        **每轮都推**（价格线要跟手），指标/信号贵，只在行情真变了时做。
        """
        contract = self.window.contract
        if contract in INFO_PAGES:
            return
        # tick：只取最后一根（整段 pd.to_datetime 很贵，而 K 线每轮都推）；
        # 换根/首次：取全段，保证漏掉的多根能补齐。
        tail = 1 if not self._pending_new_bar() else None
        frame = strategy_kline_frame(self._strategy_obj(), contract,
                                     tail=tail)
        if frame is None or frame.empty:
            return
        last = frame.iloc[-1]
        when = pd.Timestamp(last['time'])
        if self._last_time is None:
            self._last_time = when
            return
        previous = pd.Timestamp(self._last_time)
        if when > previous:
            times = pd.to_datetime(frame['time'])
            new_bars = frame[times > previous]
            if new_bars.empty:
                # 时间变了但定位不到（极端边界）：至少把最新一根推上去
                self.on_bar(last, push_indicators=push_indicators)
                if push_indicators:
                    self._refresh_signals()
                return
            count = len(new_bars)
            # 上一根刚收线：它的指标要用倒数第 count+1 个值回填，
            # 不能拿最新的值（否则上一根会被写成新根的值）。
            prev_rows = frame[times <= previous]
            if not prev_rows.empty:
                self.on_bar(prev_rows.iloc[-1], indicator_offset=count,
                            push_indicators=push_indicators,
                            historical=True)      # ★ 回填旧根（要带 historicalUpdate ✓）
            # 新根从旧到新逐根追加：最旧的 offset=count-1，最新的 offset=0
            for j in range(count):
                self.on_bar(new_bars.iloc[j], indicator_offset=count - 1 - j,
                            push_indicators=push_indicators)
        else:
            self.on_tick(last['time'], float(last['close']),
                         volume=float(last.get('volume', 0) or 0),
                         push_indicators=push_indicators)
        # 信号跟着最新行情实时增/删（含换根时最后一根/多根重新计算）
        if push_indicators:
            self._refresh_signals()
            self._refresh_indicator_tables()

    # ---------------------------------------------------------------- 运行
    def run(self, period_ms: int = 100) -> None:
        """显示窗口（或网页服务），并在后台线程里跑实时更新循环。"""
        import os
        import threading
        import time
        self._period_ms = int(period_ms)

        if self._jupyter:
            # Jupyter：启动本地 SSE 服务 + iframe，不阻塞、不要 watchdog
            # （否则窗口一异常会把整个 kernel os._exit 掉）。
            self.window.show(block=False)
            self._loop_thread = threading.Thread(
                target=self._feed_loop, args=(self._period_ms,),
                daemon=True, name='realtime-feed')
            self._loop_thread.start()
            return

        self.chart.show(block=False)

        def watchdog() -> None:
            """窗口一关就强制退出（浏览器/原生都可能卡住主线程）。"""
            while True:
                time.sleep(0.5)
                if not getattr(self.chart, 'is_alive', True):
                    time.sleep(1.0)
                    print('[realtime] 窗口已关闭，退出')
                    os._exit(0)

        self._loop_thread = threading.Thread(
            target=self._feed_loop, args=(self._period_ms,),
            daemon=True, name='realtime-feed')
        self._loop_thread.start()
        threading.Thread(target=watchdog, daemon=True,
                         name='realtime-watchdog').start()
        try:
            self.chart.show(block=True)
        except KeyboardInterrupt:
            pass

    def show(self, block: bool = False) -> None:
        self.window.show(block=block)
