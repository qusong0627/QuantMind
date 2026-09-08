import joblib
from bokeh.core.properties import String  # 用 String 存储 ISO 字符串（JSON 兼容）
from bokeh.document import Document
import argparse
import os
import warnings
from datetime import datetime
from random import randint
from ast import literal_eval
from typing import Callable
from bokeh.application.handlers.function import FunctionHandler
from bokeh.application import Application
from bokeh.server.server import Server
from tornado.ioloop import IOLoop
from bokeh.palettes import Category10
from bokeh.events import PointEvent, Tap
import bokeh.colors.named as bcn
from itertools import cycle
from functools import partial
from bokeh.models import Tabs
from bokeh.transform import factor_cmap
import numpy as np
import pandas as pd
from bokeh.models import (
    CrosshairTool,
    CustomJS,
    ColumnDataSource,
    NumeralTickFormatter,
    Span,
    HoverTool,
    Range1d,
    WheelZoomTool,
    PreText,
    Button,
    LabelSet,
    DataTable,
    TableColumn,
    StringFormatter,
    Toggle,
    Spacer,
    Div,
)
from copy import deepcopy
from bokeh.layouts import gridplot, column, row
from bokeh.plotting import figure as _figure
from bokeh.models.glyphs import VBar
setattr(_figure, '_main_ohlc', False)
try:  # 版本API有变
    from bokeh.models import TabPanel as Panel
except:
    from bokeh.models import Panel
warnings.filterwarnings('ignore')
FILED = ['open', 'high', 'low', 'close']
_FILED = ['datetime', 'open', 'high', 'low', 'close', 'volume']
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DARK_TABS_CSS = """
    :host(.dark-tabs) {
        background-color: #1a1a1a;
        border-color: #333;
    }
    :host(.dark-tabs) .bk-tabs-header {
        background-color: #333;
    }
    :host(.dark-tabs) .bk-tab {
        color: #ccc;
        border-color: #666;
    }
    :host(.dark-tabs) .bk-active {
        background-color: #1a1a1a;
        color: #fff;
    }
    """
panel_CUSTOM_CSS = """
:host {
    height: 100%;
}
.bk-Column {
    display: flex;
    flex-direction: column;
    height: 100%;  /* 从100vh改为100% */
}
.bk-Column > * {
    flex: 1 1 auto;
}
.bk-Column > .candle-chart {
    flex: 4 1 auto;  /* 主K线图占比 */
}
.bk-Column > .volume-chart {
    flex: 0.8 1 auto;  /* 成交量图占比 */
}
.bk-Column > .value-chart {
    flex: 1.5 1 auto;  /* 资金曲线占比 */
}
"""

# 在现有的样式定义部分添加
# 更新行情面板CSS样式
QUOTES_PANEL_CSS = """
:host {
    background-color: #2a2a2a;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 5px;
}
.bk-data-table {
    font-size: 12px;
    height: 100% !important;
}
.bk-cell {
    text-align: center;
}
.bk-header-row .bk-cell {
    font-weight: bold;
    background-color: #333;
    color: #cccccc;
}
/* 隐藏复选框列 */
.bk-cell.select-row {
    display: none !important;
}
.bk-header-column .bk-cell.select-row {
    display: none !important;
}
/* 选中行样式 */
.bk-slick-row.bk-selected {
    background-color: #ff9900 !important;
    color: #000000 !important;
    font-weight: bold;
}
.bk-slick-cell.bk-selected {
    background-color: #ff9900 !important;
    color: #000000 !important;
}
/* 鼠标悬停效果 */
.bk-slick-row:hover {
    background-color: #3a3a3a !important;
    cursor: pointer;
}
"""

LIGHT_QUOTES_CSS = """
:host {
    background-color: #f5f5f5;
    border: 1px solid #ddd;
}
.bk-header-row .bk-cell {
    background-color: #e0e0e0;
    color: #333333;
}
/* 隐藏复选框列 */
.bk-cell.select-row {
    display: none !important;
}
.bk-header-column .bk-cell.select-row {
    display: none !important;
}
/* 选中行样式 */
.bk-slick-row.bk-selected {
    background-color: #007acc !important;
    color: #ffffff !important;
    font-weight: bold;
}
.bk-slick-cell.bk-selected {
    background-color: #007acc !important;
    color: #ffffff !important;
}
/* 鼠标悬停效果 */
.bk-slick-row:hover {
    background-color: #e8f4ff !important;
    cursor: pointer;
}
"""

# 定义自定义子类（必须在使用前定义）


class CustomColumnDataSource(ColumnDataSource):
    # 显式声明 current_datetime 为 Bokeh 支持的 DateTime 类型
    current_datetime = String(
        default="",
        help="存储 ISO 格式时间字符串（统一对比标准）"
    )


parser = argparse.ArgumentParser()
parser.add_argument('--black_style', '-bs',
                    type=literal_eval, default=False)
parser.add_argument('--plot_width', '-pw', type=int, default=0)
parser.add_argument('--period_milliseconds', '-pm', type=int, default=1000)
parser.add_argument("--click_policy", '-cp', type=str, default='hide')
parser.add_argument('--live', '-lv', type=int, default=0)
parser.add_argument('--update_length', '-ul', type=int, default=10)

args = parser.parse_args()
ispoint = True
# 新增：控制更新状态的全局变量
update_step = 1    # 默认向前更新1根K线
current_strategy_idx, current_contract_idx = 0, 0


def ffillnan(arr: np.ndarray) -> np.ndarray:
    if len(arr.shape) > 1:
        arr = pd.DataFrame(arr)
    else:
        arr = pd.Series(arr)
    arr.ffill(inplace=True)
    arr.bfill(inplace=True)
    return arr.values


# 先导入必要的库


def storeData(data, filename='exampleJoblib'):
    try:
        # joblib.dump 直接写入文件，无需手动打开文件句柄（更简洁）
        joblib.dump(data, filename)
    except Exception as e:  # 建议捕获具体异常，而非空except
        # 可添加异常提示，方便调试
        # print(f"保存数据失败：{str(e)}")
        return None


def loadData(filename='exampleJoblib'):
    try:
        # joblib.load 直接读取文件
        db = joblib.load(filename)
        return db
    except Exception as e:
        # print(f"加载数据失败：{str(e)}")
        return None


def on_mouse_move(event: PointEvent):
    global ispoint
    ispoint = False if ispoint else True


def set_tooltips(fig: _figure, tooltips=(), vline=True, renderers=(), if_datetime: bool = True, if_date=True, mouse: bool = False, callback=None):
    tooltips = list(tooltips)
    renderers = list(renderers)

    if if_datetime:
        formatters = {'@datetime': 'datetime'}
        if if_date:
            tooltips += [("Datetime", "@datetime{%Y-%m-%d}")]
        else:
            tooltips += [("Datetime", "@datetime{%Y-%m-%d %H:%M:%S}")]
    else:
        formatters = {}
    # tooltips = [("Date", "@datetime")] + tooltips
    if callback:
        hover_tool = HoverTool(
            point_policy='follow_mouse',
            renderers=renderers, formatters=formatters,
            tooltips=tooltips, mode='vline' if vline else 'mouse',
            callback=callback)
    else:
        hover_tool = HoverTool(
            point_policy='follow_mouse',
            renderers=renderers, formatters=formatters,
            tooltips=tooltips, mode='vline' if vline else 'mouse')
    fig.add_tools(hover_tool)
    if mouse:
        fig.on_event(Tap, on_mouse_move)


def new_bokeh_figure(plot_width, height=300) -> Callable:
    return partial(
        _figure,
        x_axis_type='linear',
        width_policy='max',
        width=plot_width,
        height=height,
        tools="xpan,xwheel_zoom,box_zoom,undo,redo,reset,save",  # ,crosshair
        active_drag='xpan',
        active_scroll='xwheel_zoom',
        toolbar_location=None,)
    # output_backend="webgl")


def new_bokeh_figure_main(plot_width, height=150) -> Callable:
    return partial(
        _figure,
        x_axis_type='linear',
        width_policy='max',
        width=plot_width,
        height=height,
        tools="xpan,xwheel_zoom",  # ,crosshair
        active_drag='xpan',
        active_scroll='xwheel_zoom',
        toolbar_location=None,)
    # output_backend="webgl")


def new_indicator_figure(new_bokeh_figure: partial, fig_ohlc: _figure, plot_width, height=80, **kwargs) -> _figure:
    # kwargs.setdefault('height', height)
    height = int(height) if height and height > 10 else 80
    fig = new_bokeh_figure(plot_width, height)(x_range=fig_ohlc.x_range,
                                               active_scroll='xwheel_zoom',
                                               active_drag='xpan',
                                               **kwargs)
    fig.xaxis.visible = False
    fig.yaxis.minor_tick_line_color = None
    return fig


def search_index(ls: list[list], name) -> tuple[int]:
    for i, ls1 in enumerate(ls):
        for j, ls2 in enumerate(ls1):
            if ls2 == name:
                return i, j
    assert False, "找不到索引"


def colorgen():
    yield from cycle(Category10[10])


def callback_strategy(attr, old, new):
    global strategy_id
    strategy_id = new


def callback_strategy(attr, old, new):
    global data_index
    data_index = new


strategy_id: int = 0
data_index: int = 0
live = int(args.live)
init_datas_dir = f"{BASE_DIR}/init_datas" if live else f"{BASE_DIR}/replay/init_datas"
trade_datas_dir = f"{BASE_DIR}/trade_datas" if live else f"{BASE_DIR}/replay/trade_datas"
update_datas_dir = f"{BASE_DIR}/update_datas" if live else f"{BASE_DIR}/replay/update_datas"
account_info_dir = f"{BASE_DIR}/account_info" if live else f"{BASE_DIR}/replay/account_info"
pause_status_dir = f"{BASE_DIR}/pause_status" if live else f"{BASE_DIR}/replay/pause_status"
id_dir = f"{BASE_DIR}/id" if live else f"{BASE_DIR}/replay/id"
storeData((current_strategy_idx, current_contract_idx), id_dir)
storeData(None, update_datas_dir)
init_datas = loadData(init_datas_dir)
account_info = loadData(account_info_dir)
black_style = args.black_style
plot_width = None  # args.plot_width
period_milliseconds = args.period_milliseconds
click_policy = args.click_policy
updatelength = args.update_length

source: list[list[ColumnDataSource]] = None
trade_source: list[list[ColumnDataSource]] = None


def make_document(doc: Document):
    global black_style, plot_width, period_milliseconds, click_policy, updatelength, current_strategy_idx, current_contract_idx
    if not hasattr(doc, 'plot_update_state'):
        doc.plot_update_state = {}  # 格式：{(i,j): bool} 标记是否允许X轴更新
    # 在文档级别存储状态
    if not hasattr(doc, 'is_paused'):
        doc.is_paused = False

    black_color, white_color = black_style and (
        "white", "black") or ("black", "white")
    # K线颜色
    COLORS = [bcn.tomato, bcn.lime]
    inc_cmap = factor_cmap('inc', COLORS, ['0', '1'])
    lines_setting = dict(line_dash='solid', line_width=1.3)
    BAR_WIDTH = .8  # K宽度
    NBSP = '\N{NBSP}' * 4
    pad = 2000
    trade_signal = False

    new_colors = {'bear': bcn.tomato, 'bull': bcn.lime}
    with open(f'{BASE_DIR}/autoscale_cb.js', encoding='utf-8') as _f:
        _AUTOSCALE_JS_CALLBACK = _f.read()
    ts: list[Tabs] = []
    snum = len(init_datas)
    long_source: list[list] = [[] for _ in range(snum)]
    short_source: list[list] = [[] for _ in range(snum)]
    long_flat_source: list[list] = [[] for _ in range(snum)]
    short_flat_source: list[list] = [[] for _ in range(snum)]
    long_segment_source: list[list] = [[] for _ in range(snum)]
    short_segment_source: list[list] = [[] for _ in range(snum)]
    ohlc_extreme_values: list[list] = [[] for _ in range(snum)]  # 范围数据
    pre_fig_x_range = [[] for _ in range(snum)]
    pre_fig_y_range = [[] for _ in range(snum)]
    symbols = [[] for _ in range(snum)]
    cycles = [[] for _ in range(snum)]
    source: list[list[ColumnDataSource]] = [[]
                                            for _ in range(snum)]
    trade_source: list[list[dict[str, ColumnDataSource]]] = [[]
                                                             for _ in range(snum)]
    ts: list[Tabs] = []
    signal_ind_data_source: list[list[dict]] = [[] for i in range(snum)]
    signal_ind_datatime_source: list[list[dict]] = [[] for i in range(snum)]
    all_data_plots: list[list[list[_figure]]] = []
    spans: list[list[list[Span]]] = [[] for _ in range(snum)]
    symbol_multi_cycle: list[list[list[int]]] = [[]
                                                 for _ in range(snum)]
    fig_ohlc_list: list[dict] = [{} for _ in range(snum)]
    ohlc_span: list[list[list[Span]]] = [[] for _ in range(snum)]
    snames: list[str] = []
    instruments: list[list[str]] = []
    lines_setting = dict(line_dash='solid', line_width=1.3)
    for i, (sname, datas, inds, btind_info) in enumerate(init_datas):
        snames.append(sname)
        panel = []
        all_plots = []
        instruments.append([])
        for j, df in enumerate(datas):
            df: pd.DataFrame
            df_length = len(df)
            signal_ind_data_source[i].append(dict())
            signal_ind_datatime_source[i].append(dict())
            symbols[i].append(df.symbol.iloc[0])
            cycles[i].append(df.duration.iloc[0])
            df = df[_FILED]
            df['volume5'] = df.volume.rolling(5).mean()
            df['volume10'] = df.volume.rolling(10).mean()
            df["inc"] = (df.close >=
                         df.open).values.astype(np.uint8).astype(str)
            df["Low"] = df[['low', 'high']].min(1)
            df["High"] = df[['low', 'high']].max(1)
            source[i].append(ColumnDataSource(df))
            # index = np.arange(len(df))
            # K线图
            # 计算初始Y轴范围，使用Range1d避免DataRange1d自动缩放导致的跳动
            _y_min = df['low'].min() * 0.998
            _y_max = df['high'].max() * 1.002
            if btind_info[j]["ismain"] and j != 0:
                fig_ohlc: _figure = new_bokeh_figure_main(plot_width, btind_info[j].get('height', 150))(
                    x_range=Range1d(0, len(df)-1,
                                    min_interval=10,
                                    bounds=(0 - pad,
                                            len(df)-1 + pad)),
                    y_range=Range1d(_y_min, _y_max))
                fig_ohlc._main_ohlc = True
            else:
                fig_ohlc: _figure = new_bokeh_figure(plot_width, btind_info[j].get('height', 300))(
                    x_range=Range1d(0, len(df)-1,
                                    min_interval=10,
                                    bounds=(0 - pad,
                                            len(df)-1 + pad)),
                    y_range=Range1d(_y_min, _y_max))
            fig_ohlc.css_classes = ["candle-chart"]
            _colors = btind_info[j].get('candlestyle', new_colors)
            if _colors and j == 0:
                _COLORS = list(_colors.values())
                inc_cmap = factor_cmap('inc', _COLORS, ['0', '1'])
            # 上下影线
            fig_ohlc.segment('index', 'high', 'index',
                             'low', source=source[i][j], color=black_color, line_width=1.2)
            # 实体线
            ohlc_bars = fig_ohlc.vbar('index', BAR_WIDTH, 'open', 'close', source=source[i][j],
                                      line_color=black_color, fill_color=inc_cmap)
            # 提示格式
            ohlc_tooltips = [
                ('x, y', NBSP.join(('$index',
                                    '$y{0,0.0[0000]}'))),
                ('OHLC', NBSP.join(('@open{0,0.0[0000]}',
                                    '@high{0,0.0[0000]}',
                                    '@low{0,0.0[0000]}',
                                    '@close{0,0.0[0000]}'))),
                ('Volume', '@volume{0,0}')]

            #
            spanstyle = btind_info[j].get("spanstyle", [])
            for ohlc_spanstyle in spanstyle:
                fig_ohlc.add_layout(Span(**ohlc_spanstyle))

            pos = 1

            if pos:
                span_color = 'red' if pos > 0. else 'green'
                for spanstyle_ in spanstyle:
                    bt_span = Span(**spanstyle_)
                    ohlc_span[i].append(bt_span)
                    fig_ohlc.add_layout(bt_span)
            else:
                bt_span = Span(location=0., dimension='width',
                               line_color='#666666', line_dash='dashed',
                               line_width=.8)
                bt_span.visible = False
                ohlc_span[i].append(bt_span)
                fig_ohlc.add_layout(bt_span)

            # 指标数据
            indicators_data = inds[j]
            indicator_candles_index = []
            indicator_figs = []
            indicator_h: list[str] = []
            indicator_l: list[str] = []
            
            # 获取价格数据长度，确保指标数据与其一致
            price_data_length = len(df)
            
            if indicators_data:
                ohlc_colors = colorgen()
                ic = 0
                for isplot, name, names, __lines, ind_name, is_overlay, category, indicator, doubles, plotinfo, span, _signal in indicators_data:
                    lineinfo = plotinfo.get('linestyle', {})
                    datainfo = plotinfo.get('source', "")
                    signal_info: dict = plotinfo.get('signalstyle', {})
                    if doubles:
                        _doubles_fig = []
                        for ids in range(2):
                            if any(isplot[ids]):
                                is_candles = category[ids] == 'candles'
                                tooltips = []
                                colors = cycle([next(ohlc_colors)]
                                               if is_overlay[ids] else colorgen())
                                legend_label = name[ids]  # 初始化命名的名称
                                if is_overlay[ids] and not is_candles:  # 主图叠加
                                    fig = fig_ohlc
                                else:
                                    fig = new_indicator_figure(
                                        new_bokeh_figure, fig_ohlc, plot_width, plotinfo.get('height', 150))
                                    indicator_figs.append(fig)
                                    _indicator = ffillnan(indicator[ids])
                                    _mulit_ind = len(
                                        _indicator.shape) > 1
                                    # 确保指标数据长度与价格数据一致
                                    _indicator = _indicator[:price_data_length]
                                    source[i][j].add(
                                        np.max(_indicator[:, np.arange(len(isplot[ids]))[isplot[ids]]], axis=1).tolist() if _mulit_ind else _indicator.tolist(), f"{ind_name[ids]}_h")
                                    source[i][j].add(
                                        np.min(_indicator[:, np.arange(len(isplot[ids]))[isplot[ids]]], axis=1).tolist() if _mulit_ind else _indicator.tolist(), f"{ind_name[ids]}_l")
                                    indicator_h.append(
                                        f"{legend_label}_h")
                                    indicator_l.append(
                                        f"{legend_label}_l")
                                    ic += 1
                                _doubles_fig.append(fig)

                                if not is_candles:
                                    if_vbar = False
                                    # 确保指标数据长度与价格数据一致
                                    _indicator_data = indicator[ids][:price_data_length]
                                    for jx in range(_indicator_data.shape[1]):
                                        if isplot[ids][jx]:
                                            _lines_name = __lines[ids][jx]
                                            ind = _indicator_data[:, jx]
                                            color = next(colors)
                                            source_name = names[ids][jx]
                                            if ind.dtype == bool:
                                                ind = ind.astype(int)
                                            source[i][j].add(
                                                ind.tolist(), source_name)
                                            tooltips.append(
                                                f"@{source_name}{'{'}0,0.0[0000]{'}'}")
                                            _lineinfo = deepcopy(
                                                lines_setting)
                                            if _lines_name in lineinfo:
                                                _lineinfo = {
                                                    **_lineinfo, **(lineinfo[_lines_name])}
                                            if _lineinfo.get("line_color", None) is None:
                                                _lineinfo.update(
                                                    dict(line_color=color))
                                            if "price_line" in _lineinfo:
                                                del _lineinfo["price_line"]
                                            if "price_label" in _lineinfo:
                                                del _lineinfo["price_label"]
                                            if is_overlay[ids]:
                                                fig.line(
                                                    'index', source_name, source=source[i][j],
                                                    legend_label=source_name, **_lineinfo)

                                            else:
                                                if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                                    if_vbar = True
                                                    if "zeros" not in source[i][j].column_names:
                                                        source[i][j].add(
                                                            [0.,]*len(ind), "zeros")
                                                    _line_inc = np.where(ind > 0., 1, 0).astype(
                                                        np.uint8).astype(str).tolist()
                                                    source[i][j].add(
                                                        _line_inc, f"{_lines_name}_inc")
                                                    _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                                    if _line_inc_cmap is None:
                                                        _line_inc_cmap = factor_cmap(
                                                            f"{_lines_name}_inc", COLORS, ['0', '1'])
                                                    r = fig.vbar('index', BAR_WIDTH, 'zeros', source_name, source=source[i][j],
                                                                 line_color='black', fill_color=_line_inc_cmap)
                                                else:
                                                    r = fig.line(
                                                        'index', source_name, source=source[i][j],
                                                        legend_label=source_name, **_lineinfo)

                                    else:
                                        if if_vbar:
                                            renderers = fig.renderers.copy()
                                            fig.renderers = list(
                                                sorted(renderers, key=lambda x: not isinstance(x.glyph, VBar)))

                                        if span:
                                            for ind_span in span:
                                                if np.isnan(ind_span["location"]) and not all(is_overlay):
                                                    ind = ind.astype(
                                                        np.float32)
                                                    mean = ind[~np.isnan(
                                                        ind)].mean()
                                                    if not np.isnan(mean) and (abs(mean) < .1 or
                                                                               round(abs(mean), 1) == .5 or
                                                                               round(abs(mean), -1) in (50, 100, 200)):
                                                        fig.add_layout(Span(location=float(mean), dimension='width',
                                                                            line_color='#666666', line_dash='dashed',
                                                                            line_width=.8))
                                                else:
                                                    fig.add_layout(
                                                        Span(**ind_span))
                                        else:
                                            ind = ind.astype(np.float32)
                                            mean = ind[~np.isnan(
                                                ind)].mean()
                                            if not np.isnan(mean) and (abs(mean) < .1 or
                                                                       round(abs(mean), 1) == .5 or
                                                                       round(abs(mean), -1) in (50, 100, 200)):
                                                fig.add_layout(Span(location=float(mean), dimension='width',
                                                                    line_color='#666666', line_dash='dashed',
                                                                    line_width=.8))
                                    if is_overlay[ids]:
                                        ohlc_tooltips.append(
                                            (legend_label, NBSP.join(tuple(tooltips))))
                                    else:

                                        set_tooltips(
                                            fig, [(legend_label, NBSP.join(tooltips))], vline=True, renderers=[r])
                                        fig.yaxis.axis_label = legend_label
                                        fig.yaxis.axis_label_text_color = 'white' if black_style else 'black'
                                        if fig_ohlc._main_ohlc:
                                            fig.yaxis.visible = False
                                        else:
                                            fig.yaxis.visible = True
                                        if len(names) == 1:
                                            fig.legend.glyph_width = 0
                    else:
                        if any(isplot):
                            is_candles = category == 'candles'
                            if is_candles and len(names) < 4:
                                is_candles = False
                            tooltips = []
                            colors = cycle([next(ohlc_colors)]
                                           if is_overlay else colorgen())
                            legend_label = name  # 初始化命名的名称
                            if is_overlay and not is_candles:  # 主图叠加
                                if datainfo in fig_ohlc_list[i]:  # 副图
                                    fig = fig_ohlc_list[i].get(
                                        datainfo)
                                else:
                                    fig = fig_ohlc
                            elif is_candles:  # 副图是蜡烛图

                                indicator_candles_index.append(ic)
                                assert len(names) >= 4
                                names = list(
                                    map(lambda x: x.lower(), names))
                                # 按open,high,low,volume进行排序
                                filed_index = []
                                missing_index = []
                                for ii, file in enumerate(FILED):
                                    is_missing = True
                                    for n in names:
                                        if file in n:
                                            filed_index.append(
                                                names.index(n))
                                            is_missing = False
                                    else:
                                        if is_missing:
                                            missing_index.append(ii)
                                assert not missing_index, f"数据中缺失{[FILED[ii] for ii in missing_index]}字段"
                                for ie in filed_index:
                                    source[i][j].add(
                                        indicator[:, ie].tolist(), names[ie])
                                index = np.arange(indicator.shape[0])
                                fig_ohlc_ = new_indicator_figure(
                                    new_bokeh_figure, fig_ohlc, plot_width, plotinfo.get('height', 100))
                                fig_ohlc_.segment('index', names[filed_index[1]], 'index', names[filed_index[2]],
                                                  source=source[i][j], color=black_color)
                                ohlc_bars_ = fig_ohlc_.vbar('index', BAR_WIDTH, names[filed_index[0]], names[filed_index[3]], source=source[i][j],
                                                            line_color=black_color, fill_color=inc_cmap)
                                ohlc_tooltips_ = [
                                    ('x, y', NBSP.join(('$index',
                                                        '$y{0,0.0[0000]}'))),
                                    ('OHLC', NBSP.join((f"@{names[filed_index[0]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{names[filed_index[1]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{names[filed_index[2]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{names[filed_index[3]]}{'{'}0,0.0[0000]{'}'}")))]
                                for lj in range(len(names)):
                                    if lj not in filed_index:
                                        if isplot[lj]:
                                            tooltips = []
                                            _lines_name = __lines[lj]
                                            ind = indicator[:, lj]
                                            color = next(colors)
                                            source_name = names[lj]
                                            if ind.dtype == bool:
                                                ind = ind.astype(int)
                                            source[i][j].add(ind,
                                                             source_name)
                                            tooltips.append(
                                                f"@{source_name}{'{'}0,0.0[0000]{'}'}")
                                            _lineinfo = deepcopy(lines_setting)
                                            if _lines_name in lineinfo:
                                                _lineinfo = {
                                                    **_lineinfo, **lineinfo[_lines_name]}
                                            if _lineinfo.get("line_color", None) is None:
                                                _lineinfo.update(
                                                    dict(line_color=color))
                                            if "price_line" in _lineinfo:
                                                del _lineinfo["price_line"]
                                            if "price_label" in _lineinfo:
                                                del _lineinfo["price_label"]
                                            # if is_overlay:
                                            fig_ohlc_.line(
                                                'index', source_name, source=source[i][j],
                                                legend_label=source_name, **_lineinfo)
                                            ohlc_tooltips_.append(
                                                (_lines_name, NBSP.join(tuple(tooltips))))

                                set_tooltips(
                                    fig_ohlc_, ohlc_tooltips_, vline=True, renderers=[ohlc_bars_])
                                fig_ohlc_.yaxis.axis_label = ind_name
                                fig_ohlc_.yaxis.axis_label_text_color = black_color
                                if fig_ohlc._main_ohlc:
                                    fig_ohlc_.yaxis.visible = False
                                else:
                                    fig_ohlc_.yaxis.visible = True
                                indicator_figs.append(fig_ohlc_)
                                fig_ohlc_list[i].update(
                                    {ind_name: fig_ohlc_})
                                ic += 1
                                _indicator = ffillnan(indicator)
                                # 确保指标数据长度与价格数据一致
                                _indicator = _indicator[:price_data_length]
                                _mulit_ind = len(
                                    _indicator.shape) > 1
                                source[i][j].add(
                                    np.max(_indicator[:, np.arange(len(isplot))[isplot]], axis=1).tolist() if _mulit_ind else _indicator.tolist(), f"{legend_label}_h")
                                source[i][j].add(
                                    np.min(_indicator[:, np.arange(len(isplot))[isplot]], axis=1).tolist() if _mulit_ind else _indicator.tolist(), f"{legend_label}_l")
                                indicator_h.append(
                                    f"{legend_label}_h")
                                indicator_l.append(
                                    f"{legend_label}_l")
                            else:
                                if datainfo in fig_ohlc_list[i]:  # 副图
                                    __fig = fig_ohlc_list[i].get(
                                        datainfo)
                                else:
                                    __fig = fig_ohlc
                                fig = new_indicator_figure(
                                    new_bokeh_figure, __fig, plot_width, plotinfo.get('height', 150))
                                indicator_figs.append(fig)
                                ic += 1
                                _indicator = ffillnan(indicator)
                                # 确保指标数据长度与价格数据一致
                                _indicator = _indicator[:price_data_length]
                                _mulit_ind = len(
                                    _indicator.shape) > 1
                                source[i][j].add(
                                    np.max(_indicator[:, np.arange(len(isplot))[isplot]], axis=1).tolist() if _mulit_ind else _indicator.tolist(), f"{legend_label}_h")
                                source[i][j].add(
                                    np.min(_indicator[:, np.arange(len(isplot))[isplot]], axis=1).tolist() if _mulit_ind else _indicator.tolist(), f"{legend_label}_l")
                                indicator_h.append(
                                    f"{legend_label}_h")
                                indicator_l.append(
                                    f"{legend_label}_l")

                            if not is_candles:
                                if_vbar = False
                                # 确保指标数据长度与价格数据一致
                                _indicator_data = indicator[:price_data_length]
                                for jx in range(len(isplot)):
                                    if isplot[jx]:
                                        _lines_name = __lines[jx]
                                        ind = _indicator_data[:, jx]
                                        color = next(colors)
                                        source_name = names[jx]
                                        if ind.dtype == bool:
                                            ind = ind.astype(np.float64)
                                        source[i][j].add(
                                            ind.tolist(), source_name)
                                        tooltips.append(
                                            f"@{source_name}{'{'}0,0.0[0000]{'}'}")
                                        _lineinfo = deepcopy(lines_setting)

                                        if _lines_name in lineinfo:
                                            _lineinfo = {
                                                **_lineinfo, **(lineinfo[_lines_name])}
                                        if _lineinfo.get("line_color", None) is None:
                                            _lineinfo.update(
                                                dict(line_color=color))
                                        if "price_line" in _lineinfo:
                                            del _lineinfo["price_line"]
                                        if "price_label" in _lineinfo:
                                            del _lineinfo["price_label"]
                                        if is_overlay:
                                            fig.line(
                                                'index', source_name, source=source[i][j],
                                                legend_label=source_name, **_lineinfo)
                                        else:
                                            if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                                if_vbar = True
                                                if "zeros" not in source[i][j].column_names:
                                                    source[i][j].add(
                                                        [0.,]*len(ind), "zeros")

                                                _line_inc = np.where(ind > 0., 1, 0).astype(
                                                    np.uint8).astype(str).tolist()
                                                source[i][j].add(
                                                    _line_inc, f"{_lines_name}_inc")
                                                _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                                if _line_inc_cmap is None:
                                                    _line_inc_cmap = factor_cmap(
                                                        f"{_lines_name}_inc", COLORS, ['0', '1'])
                                                r = fig.vbar('index', BAR_WIDTH, 'zeros', source_name, source=source[i][j],
                                                             line_color='black', fill_color=_line_inc_cmap)
                                            else:
                                                r = fig.line(
                                                    'index', source_name, source=source[i][j],
                                                    legend_label=source_name, **_lineinfo)
                                    if if_vbar:
                                        renderers = fig.renderers.copy()
                                        fig.renderers = list(
                                            sorted(renderers, key=lambda x: not isinstance(x.glyph, VBar)))
                                    if span:
                                        for ind_span in span:
                                            if np.isnan(ind_span["location"]) and not is_overlay if isinstance(is_overlay, bool) else not all(is_overlay):
                                                ind = ind.astype(np.float32)
                                                mean = ind[~np.isnan(
                                                    ind)].mean()
                                                if not np.isnan(mean) and (abs(mean) < .1 or
                                                                           round(abs(mean), 1) == .5 or
                                                                           round(abs(mean), -1) in (50, 100, 200)):
                                                    fig.add_layout(Span(location=float(mean), dimension='width',
                                                                        line_color='#666666', line_dash='dashed',
                                                                        line_width=.8))
                                            else:
                                                fig.add_layout(
                                                    Span(**ind_span))
                                    else:
                                        ind = ind.astype(np.float32)
                                        non_nan_ind = ind[~np.isnan(ind)]
                                        mean = non_nan_ind.mean() if len(non_nan_ind) > 0 else np.nan
                                        if not np.isnan(mean) and (abs(mean) < .1 or
                                                                   round(abs(mean), 1) == .5 or
                                                                   round(abs(mean), -1) in (50, 100, 200)):
                                            fig.add_layout(Span(location=float(mean), dimension='width',
                                                                line_color='#666666', line_dash='dashed',
                                                                line_width=.8))

                                if is_overlay:
                                    ohlc_tooltips.append(
                                        (ind_name, NBSP.join(tuple(tooltips))))
                                else:

                                    set_tooltips(
                                        fig, [(legend_label, NBSP.join(tooltips))], vline=True, renderers=[r])
                                    fig.yaxis.axis_label = ind_name
                                    fig.yaxis.axis_label_text_color = 'white' if black_style else 'black'
                                    # If the sole indicator line on this figure,
                                    # have the legend only contain text without the glyph
                                    if len(names) == 1:
                                        fig.legend.glyph_width = 0
                                    if fig_ohlc._main_ohlc:
                                        fig.yaxis.visible = False
                                    else:
                                        fig.yaxis.visible = True

                    if signal_info:
                        for k, v in signal_info.items():
                            signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = list(
                                v.values())
                            if signalshow:
                                signaldata: np.ndarray
                                islabel = isinstance(signallabel, dict)
                                if islabel:
                                    label_text = signallabel.pop("text", k)
                                if doubles:
                                    index1, index2 = search_index(
                                        __lines, k)
                                    signaldata = indicator[index1][:, index2]
                                else:
                                    signaldata = indicator[:, __lines.index(
                                        k)]
                                signal_index = np.argwhere(
                                    signaldata > 0)[:, 0]
                                if signaloverlap:
                                    # 修复：确保 price_data 长度与 signaldata 一致（指标数据已截断到 min_start_length）
                                    price_data = df[signalkey].values[:len(signaldata)]
                                    signal_fig = fig_ohlc
                                else:
                                    signal_fig = fig
                                    try:
                                        if doubles:
                                            index1, index2 = search_index(
                                                __lines, signalkey)
                                            price_data = indicator[index1][:, index2]
                                        else:
                                            price_data = indicator[:, __lines.index(
                                                signalkey)]
                                    except:
                                        price_data = signaldata  # .copy()
                                signal_price = price_data[signaldata > 0]
                                signal_datetime = df.datetime.values[signaldata > 0]
                                if islabel:
                                    signal_source_ = ColumnDataSource(dict(
                                        index=signal_index,
                                        datetime=signal_datetime,
                                        price=signal_price,
                                        size=[float(signalsize),] *
                                        len(signal_index),
                                        text=[label_text] *
                                        len(signal_index),  # 标签文字列表
                                    ))
                                else:
                                    signal_source_ = ColumnDataSource(dict(
                                        index=signal_index,
                                        datetime=signal_datetime,
                                        price=signal_price,
                                        size=[float(signalsize),] *
                                        len(signal_index),
                                    ))
                                ind_key = doubles and name[0] or name
                                signal_ind_data_source[i][j].update(
                                    {f"{ind_key}{i}_{k}": signal_source_})
                                signal_ind_datatime_source[i][j].update(
                                    {f"{ind_key}{i}_{k}": np.datetime64(datetime.now()), "index": signal_index[-1]})

                                r = signal_fig.scatter(x='index', y='price', source=signal_source_, fill_color=signalcolor,
                                                       marker=signalmarker, line_color='black', size="size")
                                if islabel:
                                    # --- 新增：用 LabelSet 添加文字标签 ---
                                    signallabel.update(dict(
                                        background_fill_alpha=0.1, background_fill_color="white" if black_style else "black"))
                                    labels = LabelSet(
                                        x='index',        # 标签 x 坐标（与散点 x 一致）
                                        y='price',        # 标签 y 坐标（与散点 y 一致）
                                        text='text',      # 标签文字来源（数据源的 text 字段）
                                        source=signal_source_,  # 共享散点的数据源
                                        **signallabel,
                                    )
                                    signal_fig.add_layout(labels)  # 将标签添加到图形中
                                tooltips = [(k, "@price{0.00}"),]
                                set_tooltips(signal_fig, tooltips,
                                             vline=False, renderers=[r,])

            set_tooltips(fig_ohlc, ohlc_tooltips,
                         vline=True, renderers=[ohlc_bars], mouse=True)
            fig_ohlc.yaxis.axis_label = f"{symbols[i][-1]}"
            fig_ohlc.yaxis.axis_label_text_color = black_color
            custom_js_args = dict(ohlc_range=fig_ohlc.y_range, indicator_range=[indicator_figs[_ic].y_range for _ic in range(len(indicator_figs))],
                                  indicator_h=indicator_h, indicator_l=indicator_l, source=source[i][j])
            # 成交量
            fig_volume = new_indicator_figure(
                new_bokeh_figure, fig_ohlc, plot_width, y_axis_label="volume", height=60)
            fig_volume.css_classes = ["volume-chart"]
            fig_volume.xaxis.formatter = fig_ohlc.xaxis[0].formatter
            if fig_ohlc._main_ohlc:
                fig_volume.yaxis.visible = False
            else:
                fig_volume.yaxis.visible = True
            fig_volume.xaxis.visible = True
            fig_ohlc.xaxis.visible = False  # Show only Volume's xaxis
            r_volume = fig_volume.vbar(
                'index', BAR_WIDTH, 'volume', source=source[i][j], color=inc_cmap)
            colors = cycle(colorgen())
            r_volume5 = fig_volume.line('index', 'volume5', source=source[i][j],
                                        legend_label='volume5', line_color=next(colors),
                                        line_width=1.3)
            r_volume10 = fig_volume.line('index', 'volume10', source=source[i][j],
                                         legend_label='volume10', line_color=next(colors),
                                         line_width=1.3)
            set_tooltips(fig_volume, [
                        ('volume', '@volume{0.00}'), ('volume5', '@volume5{0.00}'), ('volume10', '@volume10{0.00}'),], renderers=[r_volume])
            fig_volume.yaxis.formatter = NumeralTickFormatter(
                format="0 a")
            fig_volume.yaxis.axis_label_text_color = black_color

            custom_js_args.update(volume_range=fig_volume.y_range)
            # 回放模式下禁用JS自动缩放，避免Y轴随X轴滚动不断跳动
            if live:
                fig_ohlc.x_range.js_on_change('end', CustomJS(args=custom_js_args,
                                                              code=_AUTOSCALE_JS_CALLBACK))

            # 主图交易信号
            if j == 0:
                if trade_signal:
                    r1 = fig_ohlc.scatter(x='index', y='price', source=long_source[i][j], fill_color=COLORS[0],
                                            marker='triangle', line_color='black', size='size')
                    r2 = fig_ohlc.scatter(x='index', y='price', source=short_source[i][j], fill_color=COLORS[1],
                                            marker='inverted_triangle', line_color='black', size='size')
                    r3 = fig_ohlc.scatter(x='index', y='price', source=long_flat_source[i][j], fill_color=COLORS[0],
                                            marker='inverted_triangle', line_color='black', size='size')
                    r4 = fig_ohlc.scatter(x='index', y='price', source=short_flat_source[i][j], fill_color=COLORS[1],
                                            marker='triangle', line_color='black', size='size')
                    tooltips = [
                        ("position", "@pos{0,0}"), ("price", "@price{0.00}")]
                    set_tooltips(fig_ohlc, tooltips,
                                 vline=False, renderers=[r1,])
                    set_tooltips(fig_ohlc, tooltips,
                                 vline=False, renderers=[r2,])
                    set_tooltips(fig_ohlc, tooltips + [("P/L", "@profit{0.00}")],
                                 vline=False, renderers=[r3,])
                    set_tooltips(fig_ohlc, tooltips + [("P/L", "@profit{0.00}")],
                                 vline=False, renderers=[r4,])
                    fig_ohlc.segment(x0='index', y0='price', x1='flat_index', y1='flat_price',
                                        source=long_segment_source[i][j], color='yellow' if black_style else "blue", line_width=3, line_dash="4 4")
                    fig_ohlc.segment(x0='index', y0='price', x1='flat_index', y1='flat_price',
                                        source=short_segment_source[i][j], color='yellow' if black_style else "blue", line_width=3, line_dash="4 4")

            # figs_ohlc[i].append(fig_ohlc)
            plots = [fig_ohlc, fig_volume]+indicator_figs
            all_plots.append(plots)

            linked_crosshair = CrosshairTool(
                dimensions='both', line_color=black_color)
            for f in plots:
                if f.legend:
                    f.legend.nrows = 1
                    f.legend.label_height = 6
                    f.legend.visible = True
                    f.legend.location = 'top_left'
                    f.legend.border_line_width = 0
                    # f.legend.border_line_color = '#333333'
                    f.legend.padding = 1
                    f.legend.spacing = 0
                    f.legend.margin = 0
                    f.legend.label_text_font_size = '8pt'
                    f.legend.label_text_line_height = 1.2
                    # "hide"  # "mute"  #
                    f.legend.click_policy = click_policy

                f.min_border_left = 0
                f.min_border_top = 0  # 3
                f.min_border_bottom = 6
                f.min_border_right = 10
                f.outline_line_color = '#666666'

                if black_style:
                    # 图表全局样式
                    f.background_fill_color = "#1a1a1a"  # 更柔和的深灰色
                    f.border_fill_color = "#1a1a1a"
                    f.outline_line_color = "#404040"  # 边框线颜色

                    # 坐标轴样式
                    f.xaxis.major_label_text_color = "#cccccc"
                    f.xaxis.axis_label_text_color = "#cccccc"
                    f.xaxis.major_tick_line_color = "#666666"
                    f.xaxis.minor_tick_line_color = "#444444"
                    f.xaxis.axis_line_color = "#666666"

                    f.yaxis.major_label_text_color = "#cccccc"
                    f.yaxis.axis_label_text_color = "#cccccc"
                    f.yaxis.major_tick_line_color = "#666666"
                    f.yaxis.minor_tick_line_color = "#444444"
                    f.yaxis.axis_line_color = "#666666"

                    # 网格线样式
                    f.xgrid.grid_line_color = "#333333"
                    f.xgrid.grid_line_alpha = 0.3
                    f.ygrid.grid_line_color = "#333333"
                    f.ygrid.grid_line_alpha = 0.3

                    # 图例样式
                    f.legend.background_fill_color = "#333333"
                    f.legend.background_fill_alpha = 0.7
                    f.legend.label_text_color = "#ffffff"
                    f.legend.border_line_color = "#555555"

                    # 标题样式（如果图表有标题）
                    if f.title:
                        f.title.text_color = "#ffffff"
                        f.title.text_font_style = "bold"

                    # 成交量图特殊处理
                    if f == fig_volume:
                        f.background_fill_alpha = 0.5  # 半透明效果
                        f.border_fill_alpha = 0.5
                f.add_tools(linked_crosshair)
                wheelzoom_tool = next(
                    wz for wz in f.tools if isinstance(wz, WheelZoomTool))
                wheelzoom_tool.maintain_focus = False
                if f._main_ohlc:
                    f.yaxis.visible = False
                    f.tools.visible = False

            kwargs = dict(
                ncols=1,
                toolbar_location='right',
                sizing_mode='stretch_both',  # ✅ 统一在此处定义
                toolbar_options=dict(logo=None),
                merge_tools=True
            )
        ismain = [info["ismain"] for info in btind_info]
        ismain = ismain[1:] if len(ismain) > 1 else [False,]

        if any(ismain):
            _ip = 0
            _all_plots = [_p for _ip, _p in enumerate(
                all_plots[1:]) if not ismain[_ip]]
            first_plot = all_plots[0]
            row_plots = []
            _panel_name = [symbols[i][0], str(cycles[i][0]),]
            for _ismain, _ps in list(zip(ismain, all_plots[1:])):
                if _ismain:
                    _ip += 1
                    figs = gridplot(
                        _ps,
                        **kwargs
                    )
                    row_plots.append(figs)
                    if _panel_name[0] != symbols[i][_ip]:
                        _panel_name.append(symbols[i][_ip])
                    _panel_name.append(str(cycles[i][_ip]))
            controls = row(*row_plots, width_policy='max')
            figs = gridplot(
                first_plot,
                **kwargs
            )
            _lay = column(controls, figs, width_policy='max',
                          sizing_mode='stretch_both')
            panel.append(
                Panel(child=_lay, title='_'.join(_panel_name)))  # name_))
            all_plots = _all_plots
        all_data_plots.append(all_plots)
        if all_plots:
            _df_length = int(df_length/2)
            for ips, _ps in enumerate(all_plots):
                _df = datas[ips]
                _ps[0].x_range.update(
                    start=_df_length, end=df_length+100)
                _ps[0].y_range.update(
                    start=_df.low.iloc[-_df_length:].min()*0.998, end=_df.high.iloc[-_df_length:].max()*1.002)
                layout = column(
                    _ps,
                    sizing_mode='stretch_both',
                    css_classes=["dynamic-column"],
                    stylesheets=[panel_CUSTOM_CSS],
                )
                panel.append(
                    Panel(
                        child=layout, title=f"{symbols[i][ips]}_{cycles[i][ips]}"))
                instruments[i].append(f"{symbols[i][ips]}_{cycles[i][ips]}")

                pre_fig_x_range[i].append(
                    [_ps[0].x_range.start, _ps[0].x_range.end])
                pre_fig_y_range[i].append(
                    [_ps[0].y_range.start, _ps[0].y_range.end])
        ts.append(Tabs(tabs=panel,
                  width=plot_width if plot_width else None, width_policy='max',
                  sizing_mode='stretch_both',
                       css_classes=["dark-tabs"] if black_style else [],
                       stylesheets=[DARK_TABS_CSS] if black_style else []
                       ))

    div = PreText(text='账户信息',
                  height=30, styles={'text-align': 'center'})

    pause_btn = Button(
        label="暂停更新",
        height=30,
        width=80,
        button_type="warning",
        styles={'text-align': 'center'}
    )

    tabs = Tabs(tabs=[Panel(child=t, title=snames[it]) for it, t in enumerate(ts)],
                background=white_color, width=plot_width if plot_width else None, width_policy='max',
                sizing_mode='stretch_both',
                css_classes=["dark-tabs"] if black_style else [],
                stylesheets=[DARK_TABS_CSS] if black_style else [])

    separator_color = "#666" if black_style else "#ccc"
    border_style = f'1px solid {separator_color}'

    # 事件回调函数

    def update_table_selection(strategy_idx, contract_idx):
        """更新表格选中状态"""
        global current_strategy_idx, current_contract_idx
        if strategy_idx < len(instruments) and contract_idx < len(instruments[strategy_idx]):
            selected_instrument = instruments[strategy_idx][contract_idx]
            if selected_instrument in instruments_flat:
                flat_index = instruments_flat.index(selected_instrument)
                # 更新表格选中状态（不触发事件循环）
                quotes_table.source.selected.indices = [flat_index]
                current_strategy_idx, current_contract_idx = strategy_idx, contract_idx
                storeData((strategy_idx, contract_idx), id_dir)

    def on_contract_tab_selected(attr, old, new):
        """当合约Tab被选中时的回调函数"""
        if new is not None:
            # 获取当前激活的策略索引
            current_strategy_idx = tabs.active
            if current_strategy_idx is not None and current_strategy_idx < len(ts):
                # 获取当前策略下激活的合约索引
                current_contract_idx = new
                update_table_selection(
                    current_strategy_idx, current_contract_idx)

    def on_strategy_tab_selected(attr, old, new):
        """当策略Tab被选中时的回调函数"""
        if new is not None:
            # 获取当前策略下激活的合约索引
            current_strategy_idx = new
            if current_strategy_idx < len(ts):
                current_contract_idx = ts[current_strategy_idx].active
                if current_contract_idx is not None:
                    update_table_selection(
                        current_strategy_idx, current_contract_idx)

    # 为每个策略的合约Tabs绑定选择事件
    for strategy_idx, contract_tabs in enumerate(ts):
        contract_tabs.on_change('active', on_contract_tab_selected)

    # 为策略Tabs绑定选择事件
    tabs.on_change('active', on_strategy_tab_selected)

   # 在创建其他组件后添加行情表格
    # 创建合约名称到策略和合约索引的映射
    instrument_mapping = {}
    for strategy_idx, strategy_instruments in enumerate(instruments):
        for instrument_idx, instrument_name in enumerate(strategy_instruments):
            instrument_mapping[instrument_name] = (
                strategy_idx, instrument_idx)

    # 在创建其他组件后添加行情表格
    # 定义表格列
    columns = [
        TableColumn(field="合约中文名", title="合约名称",
                    formatter=StringFormatter(text_align="center")),
    ]
    instruments_flat = np.array(instruments).flatten().tolist()
    instruments_source = ColumnDataSource(
        dict({"合约中文名": instruments_flat}))

    # 创建数据表格
    quotes_table = DataTable(
        source=instruments_source,
        columns=columns,
        width=150,
        height_policy="max",
        editable=False,
        sortable=True,
        reorderable=True,
        selectable="checkbox",
        index_position=None,
        css_classes=["quotes-panel"],
        stylesheets=[QUOTES_PANEL_CSS] if black_style else [LIGHT_QUOTES_CSS]
    )
    # 默认勾选第一行，如果数据源中有数据
    if instruments_source.data:
        instruments_source.selected.indices = [0]
    # storeData((current_strategy_idx, current_contract_idx), id_dir)
    # 为行情表格添加点击事件 - 实现单选逻辑

    def on_instrument_selected(attr, old, new):
        """当合约被选中时的回调函数"""
        global current_strategy_idx, current_contract_idx
        # 实现单选逻辑
        if new:
            # 如果选择了多个，只保留最后一个
            if len(new) > 1:
                selected_index = new[-1]  # 取最后一个
                quotes_table.source.selected.indices = [selected_index]
                return  # 直接返回，等待下一次回调

            selected_index = new[0]
            if selected_index < len(instruments_flat):
                selected_instrument = instruments_flat[selected_index]

                # 切换到对应的策略和合约tab
                if selected_instrument in instrument_mapping:
                    strategy_idx, instrument_idx = instrument_mapping[selected_instrument]
                    # 切换到对应的策略tab
                    tabs.active = strategy_idx
                    # 切换到对应策略下的合约tab
                    ts[strategy_idx].active = instrument_idx
                    current_strategy_idx, current_contract_idx = strategy_idx, instrument_idx
                    storeData((strategy_idx, instrument_idx), id_dir)

    # 绑定选择事件
    quotes_table.source.selected.on_change('indices', on_instrument_selected)

    # 创建显示/隐藏按钮
    quotes_toggle = Toggle(
        label="📈 合约面板",
        button_type="primary",
        width=100,
        height=30,
        active=True  # 默认显示
    )

    # 添加显示隐藏控制
    toggle_js_simple = """
    // 直接控制行情表格的可见性
    if (cb_obj.active) {
        quotes_table.visible = true;
    } else {
        quotes_table.visible = false;
    }
    """

    quotes_toggle.js_on_click(
        CustomJS(args={'quotes_table': quotes_table}, code=toggle_js_simple))

    # 修改顶部行布局
    if live:
        toprow = row(
            div,  # PreText靠左
            Spacer(width_policy='max'),  # 中间弹性空白
            quotes_toggle,  # 合约面板按钮靠右
            spacing=10,
            width_policy="max",
            height=40,
            styles={'border-bottom': border_style, 'padding-bottom': '5px'}
        )
    else:
        toprow = row(
            div,  # PreText靠左
            Spacer(width_policy='max'),  # 中间弹性空白
            pause_btn,  # 暂停按钮
            quotes_toggle,  # 合约面板按钮靠右
            spacing=10,
            width_policy="max",
            height=40,
            styles={'border-bottom': border_style, 'padding-bottom': '5px'}
        )

    # 创建主内容区域（图表 + 合约面板）
    main_content = column(
        toprow,  # 顶部按钮行

        # 添加分隔线
        Div(
            text=f"""<hr style="margin: 5px 0; border: none; border-top: {border_style}; opacity: 0.5;">""",
            height=10,
            sizing_mode='stretch_width'
        ),

        row(
            column(
                tabs,
                sizing_mode='stretch_both',
                width_policy='max',
                styles={
                    'border': border_style,
                    'border-radius': '4px',
                    'padding': '5px',
                    'background-color': '#1a1a1a' if black_style else '#f9f9f9'
                }
            ),

            quotes_table,  # 合约表格
            sizing_mode='stretch_both',
            styles={
                'border': border_style,
                'border-radius': '4px',
                'padding': '5px',
                'background-color': '#1a1a1a' if black_style else '#f9f9f9'
            },),

        sizing_mode='stretch_both',
        styles={
            'border': border_style,
            'border-radius': '6px',
            'padding': '8px',
            'background-color': '#1e1e1e' if black_style else '#ffffff'
        }
    )

    _lay = main_content

    # ---------------------- 核心：暂停状态直接用文件管理 ----------------------
    def get_pause_status() -> bool:
        """读取暂停状态（文件中'1'=暂停，'0'=运行）"""
        if live:
            return False
        try:

            with open(pause_status_dir, 'r') as f:
                pause_status = f.read().strip()
                return pause_status == '1'
        except Exception as e:
            print(f"读取暂停状态失败: {e}，默认运行")
            return False

    def set_pause_status(paused: bool):
        """设置暂停状态到文件"""
        try:
            with open(pause_status_dir, 'w') as f:
                f.write('1' if paused else '0')
            return True
        except Exception as e:
            print(f"设置暂停状态失败: {e}")
            return False

    # ---------------------- 初始化与UI更新 ----------------------
    def update_button_states():
        """更新暂停按钮状态"""
        is_paused = get_pause_status()
        if is_paused:
            pause_btn.label = "取消暂停"
            pause_btn.button_type = "success"
            print("【UI更新】 暂停模式")
        else:
            pause_btn.label = "暂停更新"
            pause_btn.button_type = "warning"
            print("【UI更新】 运行模式")

    def initialize_state():
        """初始化状态（直接读文件）"""
        is_paused = get_pause_status()
        # print(f"初始化状态: {'暂停' if is_paused else '运行'}")
        update_button_states()

    if not live:
        # 立即初始化
        initialize_state()

    # ---------------------- 简化版暂停切换（直接操作文件，无异步/HTTP） ----------------------
    def toggle_pause():
        """切换暂停状态（同步操作，速度极快）"""
        # 读取当前状态
        current_paused = get_pause_status()
        # 切换状态并写入文件
        new_paused = not current_paused
        set_pause_status(new_paused)
        # 立即更新UI
        update_button_states()

    if live:
        def periodic_update():
            """主更新循环（仅保留自动周期更新）"""
            global current_strategy_idx, current_contract_idx
            try:
                # 暂停时不更新
                if get_pause_status():
                    return

                # 数据更新逻辑（保留原有）
                update_datas = loadData(update_datas_dir)
                trade_datas = loadData(trade_datas_dir)
                account_info = loadData(account_info_dir)
                if update_datas:
                    for i, update_data in update_datas:
                        if live and i != current_strategy_idx:
                            continue
                        if update_data:
                            for j, data in enumerate(update_data):
                                if live and j != current_contract_idx or not data:
                                    continue
                                _source_data = source[i][j].data
                                # 修复：在回放模式下，使用与图表数据长度一致的数据
                                # 获取当前图表数据的长度
                                source_length = len(_source_data['high']) if 'high' in _source_data else len(_source_data.get('index', []))
                                # 使用与图表数据长度一致的数据，避免Y轴范围跳动
                                data_length = len(data["high"])
                                if source_length > 0 and data_length > source_length:
                                    # 数据长度不一致，取与图表数据对应的最后一条
                                    current_high = data["high"][-source_length]
                                    current_low = data["low"][-source_length]
                                else:
                                    current_high = data["high"][-1]
                                    current_low = data["low"][-1]
                                update_datetime = data['datetime']
                                source_datetime = _source_data['datetime']

                                # if live:
                                # ========== 核心修改：按新K线判断逻辑重构live模式 ==========
                                source_last_dt = source_datetime[-1]
                                isupdate_datetime = update_datetime[update_datetime >
                                                                    source_last_dt]
                                bar = len(isupdate_datetime)
                                has_new_kline = bar > 0

                                # 更新交易信号
                                # 更新交易信号
                                for signalkey, signalsource in signal_ind_data_source[i][j].items():
                                    if signalkey in data:
                                        signalsource_data = signalsource.data  # 图表数据
                                        # 最新数据
                                        signalinfo = data.pop(signalkey)
                                        # 图表时间
                                        signal_datetime = signalsource_data["datetime"]
                                        # 图表最后时间
                                        last_signal_datetime = signal_datetime[-1]
                                        # 当最新数据最后时间与信号最后时间相同时，取之前的时间
                                        current_datetime = signalinfo["datetime"]
                                        is_update_datetime = current_datetime > last_signal_datetime  # 判断最新数据是否有更新
                                        has_new_signal = is_update_datetime.any()  # 只要有一个条件成立，则有信号更新

                                        if has_new_signal:
                                            stream_sdata = {}
                                            # 最新数据的时间与图表最新时间的时间差
                                            add_datetime = current_datetime[is_update_datetime]
                                            sbar = len(add_datetime)
                                            for k in signalsource_data.keys():
                                                # 取更新数据的最后一条作为新K线值
                                                stream_sdata[k] = signalinfo[k][-sbar:]
                                            signalsource.stream(
                                                stream_sdata, rollover=None)
                                        elif last_signal_datetime == signalinfo["datetime"][-1]:
                                            patch_data = {}
                                            index = [
                                                len(signalinfo["index"])-1,]
                                            for k in signalsource_data.keys():
                                                patch_data[k] = list(
                                                    zip(index, signalinfo[k][-1:]))
                                            signalsource.patch(patch_data)
                                        elif last_signal_datetime > signalinfo["datetime"][-1]:
                                            signalsource.data = signalinfo

                                if has_new_kline:
                                    # source[i][j].data = data
                                    stream_data = {}  # 用于增量数据追加
                                    for k, _ in source[i][j].data.items():
                                        value = data[k]
                                        # 取更新数据的最后一条作为新K线值
                                        stream_data[k] = value[-bar:]

                                    # current_max_length = len(
                                    #     source[i][j].data['index'])
                                    # stream_data['index'] = np.arange(
                                    #     current_max_length, current_max_length+bar)
                                    # print("*"*100)
                                    # print("前",
                                    #       source[i][j].data["index"][-updatelength:], stream_data["index"])
                                    source[i][j].stream(
                                        stream_data, rollover=None)
                                else:
                                    # ---------- 无新K线：直接patch最后一根K线 ----------
                                    patch_data = {}  # 用于存量数据修改
                                    # 定位最后一根K线的索引
                                    target_patch_length = len(
                                        source_datetime)
                                    # print(source[i][j].data["index"]
                                    #       [-updatelength-1:], range(target_patch_length-len(data["close"])-1, target_patch_length))
                                    for k, _ in source[i][j].data.items():
                                        # if k != 'index':
                                        value = data[k][-10:]
                                        patch_data[k] = list(
                                            zip(range(target_patch_length-len(value), target_patch_length), value))
                                    # print("*"*100)
                                    # print("前", source[i][j].data["index"][-updatelength:], range(
                                    #     target_patch_length-len(value), target_patch_length))
                                    # 直接执行patch，无多余判断
                                    if patch_data:
                                        source[i][j].patch(patch_data)

                                # 计算全部数据的极值用于Y轴范围（回放模式下基于全部数据，避免跳动）
                                if not live:
                                    _src = source[i][j].data
                                    all_high = np.max(_src["high"]) * 1.002
                                    all_low = np.min(_src["low"]) * 0.998

                                for fig_idx, fig in enumerate(all_data_plots[i][j]):
                                    if has_new_kline and ispoint and fig_idx == 0:
                                        fig.x_range.update(start=fig.x_range.start+1,
                                                           end=fig.x_range.end+1)
                                    # Y轴范围只更新主图（K线图），指标图保持自己的范围
                                    if fig_idx == 0:
                                        if live:
                                            # 实盘模式：根据当前K线动态扩展
                                            if current_high > pre_fig_y_range[i][j][1]:
                                                fig.y_range.update(
                                                    end=current_high*1.002)
                                            elif current_low < pre_fig_y_range[i][j][0]:
                                                fig.y_range.update(
                                                    start=current_low*0.998)
                                        else:
                                            # 回放模式：只在极值变化时才更新Y轴范围，避免不必要的重绘
                                            y_start, y_end = fig.y_range.start, fig.y_range.end
                                            # 使用容差比较避免浮点数精度问题
                                            if abs(y_start - all_low) > 0.01 or abs(y_end - all_high) > 0.01:
                                                print(f"[DEBUG] Y轴更新: i={i}, j={j}, fig_idx={fig_idx}, old=({y_start:.2f}, {y_end:.2f}), new=({all_low:.2f}, {all_high:.2f})")
                                                fig.y_range.update(
                                                    start=all_low, end=all_high)
                                # pre_fig_x_range[i][j] = [
                                #     all_data_plots[i][j][0].x_range.start, all_data_plots[i][j][0].x_range.end]
                                pre_fig_y_range[i][j] = [
                                    all_data_plots[i][j][0].y_range.start, all_data_plots[i][j][0].y_range.end]

                    storeData(None, update_datas_dir)

                if trade_datas:
                    for i, trade_source_ in trade_datas:
                        if i != current_strategy_idx:
                            continue
                        if trade_source_:
                            for j, (pos, price) in enumerate(trade_source_):
                                if j != current_contract_idx:
                                    continue
                                _span = ohlc_span[i][j]
                                if price > 0. and _span.location != price:
                                    if pos:
                                        if not _span.visible:
                                            _span.visible = True
                                        _span.location = float(price)
                                        _span.line_color = 'green' if pos > 0 else 'red'
                                    else:
                                        _span.location = 0.
                                        _span.visible = False
                    storeData(None, trade_datas_dir)

                if account_info:
                    div.update(text=account_info[current_strategy_idx])
                    storeData(None, account_info_dir)

            except Exception as e:
                print(f"周期更新错误: {e}")
                import traceback
                traceback.print_exc()

    else:

        # ---------------------- 周期更新（移除手动更新逻辑） ----------------------
        def periodic_update():
            """主更新循环（仅保留自动周期更新）"""
            global current_strategy_idx, current_contract_idx
            try:
                # 暂停时不更新
                if get_pause_status():
                    return

                # 数据更新逻辑（保留原有）
                update_datas = loadData(update_datas_dir)
                trade_datas = loadData(trade_datas_dir)
                account_info = loadData(account_info_dir)
                if update_datas:
                    for i, update_data in update_datas:
                        if live and i != current_strategy_idx:
                            continue
                        if update_data:
                            for j, data in enumerate(update_data):
                                if live and j != current_contract_idx or not data:
                                    continue
                                _source_data = source[i][j].data
                                # 修复：在回放模式下，使用与图表数据长度一致的数据
                                # 获取当前图表数据的长度
                                source_length = len(_source_data['high']) if 'high' in _source_data else len(_source_data.get('index', []))
                                # 使用与图表数据长度一致的数据，避免Y轴范围跳动
                                data_length = len(data["high"])
                                if source_length > 0 and data_length > source_length:
                                    # 数据长度不一致，取与图表数据对应的最后一条
                                    current_high = data["high"][-source_length]
                                    current_low = data["low"][-source_length]
                                else:
                                    current_high = data["high"][-1]
                                    current_low = data["low"][-1]
                                update_datetime = data['datetime']
                                source_datetime = _source_data['datetime']

                                # if live:
                                # ========== 核心修改：按新K线判断逻辑重构live模式 ==========
                                source_last_dt = source_datetime[-1]
                                isupdate_datetime = update_datetime[update_datetime >
                                                                    source_last_dt]
                                bar = len(isupdate_datetime)
                                has_new_kline = bar > 0

                                # 更新交易信号
                                if live and has_new_kline:
                                    for signalkey, signalsource in signal_ind_data_source[i][j].items():
                                        if signalkey in data:
                                            signalsource_data = signalsource.data  # 图表数据
                                            # 最新数据
                                            signalinfo = data.pop(signalkey)
                                            # 图表时间
                                            signal_datetime = signalsource_data["datetime"]
                                            # 图表最后时间
                                            last_signal_datetime = signal_datetime[-1]
                                            # 当最新数据最后时间与信号最后时间相同时，取之前的时间

                                            next_datas = {}
                                            if signalinfo["datetime"][-1] >= source[i][j].data["datetime"][-1]:
                                                for k in signalsource_data.keys():
                                                    if k != "index":
                                                        next_datas[k] = signalinfo[k][:-1]
                                                signalinfo = next_datas

                                            current_datetime = signalinfo["datetime"]
                                            is_update_datetime = current_datetime > last_signal_datetime  # 判断最新数据是否有更新
                                            has_new_signal = is_update_datetime.any()  # 只要有一个条件成立，则有信号更新

                                            if has_new_signal:
                                                stream_sdata = {}
                                                # 最新数据的时间与图表最新时间的时间差
                                                add_datetime = current_datetime[is_update_datetime]
                                                print(
                                                    add_datetime, source[i][j].data["datetime"][-1])
                                                sbar = len(add_datetime)
                                                for k in signalsource_data.keys():
                                                    if k != 'index':
                                                        # 取更新数据的最后一条作为新K线值
                                                        stream_sdata[k] = signalinfo[k][-sbar:]
                                                stream_sdata["index"] = source[i][j].data["index"][np.isin(
                                                    source[i][j].data["datetime"], add_datetime)]
                                                print(
                                                    stream_sdata, source[i][j].data["index"][-10])
                                                signalsource.stream(
                                                    stream_sdata, rollover=None)

                                if has_new_kline:
                                    stream_data = {}  # 用于增量数据追加
                                    for k, _ in source[i][j].data.items():
                                        if k != 'index':
                                            value = data[k]
                                            # 取更新数据的最后一条作为新K线值
                                            stream_data[k] = value[-bar:]

                                    current_max_length = len(
                                        source[i][j].data['index'])
                                    stream_data['index'] = np.arange(
                                        current_max_length, current_max_length+bar)
                                    # print("*"*100)
                                    # print("前",
                                    #       source[i][j].data["index"][-updatelength:], stream_data["index"])
                                    source[i][j].stream(
                                        stream_data, rollover=None)
                                    # print("后",
                                    #       source[i][j].data["index"][-updatelength:], stream_data["index"])
                                    # 追加后更新源数据引用（确保后续patch倒数第二根）

                                else:
                                    # ---------- 无新K线：直接patch最后一根K线 ----------
                                    patch_data = {}  # 用于存量数据修改
                                    # 定位最后一根K线的索引
                                    target_patch_length = len(
                                        source_datetime)
                                    # print(source[i][j].data["index"]
                                    #       [-updatelength-1:], range(target_patch_length-len(data["close"])-1, target_patch_length))
                                    for k, _ in source[i][j].data.items():
                                        if k != 'index':
                                            value = data[k]
                                            patch_data[k] = list(
                                                zip(range(target_patch_length-len(value), target_patch_length), value))
                                    # print("*"*100)
                                    # print("前", source[i][j].data["index"][-updatelength:], range(
                                    #     target_patch_length-len(value), target_patch_length))
                                    # 直接执行patch，无多余判断
                                    if patch_data:
                                        source[i][j].patch(patch_data)

                                # 更新交易信号
                                for signalkey, signalsource in signal_ind_data_source[i][j].items():
                                    if signalkey in data:
                                        signalsource_data = signalsource.data  # 图表数据
                                        # 最新数据
                                        signalinfo = data.pop(signalkey)
                                        # 图表时间
                                        signal_datetime = signalsource_data["datetime"]
                                        # 图表最后时间
                                        last_signal_datetime = signal_datetime[-1]
                                        # 当最新数据最后时间与信号最后时间相同时，取之前的时间
                                        current_datetime = signalinfo["datetime"]
                                        is_update_datetime = current_datetime > last_signal_datetime  # 判断最新数据是否有更新
                                        has_new_signal = is_update_datetime.any()  # 只要有一个条件成立，则有信号更新

                                        if has_new_signal:
                                            stream_sdata = {}
                                            # 最新数据的时间与图表最新时间的时间差
                                            add_datetime = current_datetime[is_update_datetime]
                                            sbar = len(add_datetime)
                                            for k in signalsource_data.keys():
                                                if k != 'index':
                                                    # 取更新数据的最后一条作为新K线值
                                                    stream_sdata[k] = signalinfo[k][-sbar:]
                                            # 修复：优先使用 signalinfo 中的 index 字段，确保索引对齐
                                            if "index" in signalinfo:
                                                stream_sdata["index"] = signalinfo["index"][-sbar:]
                                            else:
                                                # 兼容旧逻辑：从K线数据中通过datetime匹配获取索引
                                                stream_sdata["index"] = source[i][j].data["index"][np.isin(
                                                    source[i][j].data["datetime"], add_datetime)]
                                            signalsource.stream(
                                                stream_sdata, rollover=None)

                                # 计算全部数据的极值用于Y轴范围（回放模式下基于全部数据，避免跳动）
                                if not live:
                                    _src = source[i][j].data
                                    all_high = np.max(_src["high"]) * 1.002
                                    all_low = np.min(_src["low"]) * 0.998

                                for fig_idx, fig in enumerate(all_data_plots[i][j]):
                                    if has_new_kline and ispoint and fig_idx == 0:
                                        fig.x_range.update(start=fig.x_range.start+1,
                                                           end=fig.x_range.end+1)
                                    # Y轴范围只更新主图（K线图），指标图保持自己的范围
                                    if fig_idx == 0:
                                        if live:
                                            # 实盘模式：根据当前K线动态扩展
                                            if current_high > pre_fig_y_range[i][j][1]:
                                                fig.y_range.update(
                                                    end=current_high*1.002)
                                            elif current_low < pre_fig_y_range[i][j][0]:
                                                fig.y_range.update(
                                                    start=current_low*0.998)
                                        else:
                                            # 回放模式：只在极值变化时才更新Y轴范围，避免不必要的重绘
                                            y_start, y_end = fig.y_range.start, fig.y_range.end
                                            # 使用容差比较避免浮点数精度问题
                                            if abs(y_start - all_low) > 0.01 or abs(y_end - all_high) > 0.01:
                                                fig.y_range.update(
                                                    start=all_low, end=all_high)
                                pre_fig_y_range[i][j] = [
                                    all_data_plots[i][j][0].y_range.start, all_data_plots[i][j][0].y_range.end]

                    storeData(None, update_datas_dir)

                if trade_datas:
                    for i, trade_source_ in trade_datas:
                        if i != current_strategy_idx:
                            continue
                        if trade_source_:
                            for j, (pos, price) in enumerate(trade_source_):
                                if j != current_contract_idx:
                                    continue
                                _span = ohlc_span[i][j]
                                if price > 0. and _span.location != price:
                                    if pos:
                                        if not _span.visible:
                                            _span.visible = True
                                        _span.location = float(price)
                                        _span.line_color = 'green' if pos > 0 else 'red'
                                    else:
                                        _span.location = 0.
                                        _span.visible = False
                    storeData(None, trade_datas_dir)

                if account_info:
                    div.update(text=account_info[current_strategy_idx])
                    storeData(None, account_info_dir)

            except Exception as e:
                print(f"周期更新错误: {e}")
                import traceback
                traceback.print_exc()

    # ---------------------- 绑定与启动 ----------------------
    pause_btn.on_click(toggle_pause)

    doc.add_root(_lay)
    doc.add_periodic_callback(periodic_update, period_milliseconds)


if __name__ == "__main__":
    apps = {'/': Application(FunctionHandler(make_document))}
    io_loop = IOLoop.current()
    port = randint(1000, 9999)
    server = Server(applications=apps, io_loop=io_loop, port=port)
    text = "【实时图表】" if live else "【策略回放】"
    print(f"{text} localhost:{port} (快捷打开 ctrl + 左击)")
    print(f"{text} ctrl + c 退出")
    print(f"{text} 双击K线图可暂停X轴向右更新，双击后可恢复！")
    server.start()
    server.show('/')
    io_loop.start()
