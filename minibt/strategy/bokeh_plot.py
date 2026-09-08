from __future__ import annotations
from bokeh.embed import file_html
from bokeh.resources import INLINE, CDN
from copy import deepcopy
import warnings
from bokeh.palettes import Category10
from .strategy import Strategy
import os
import bokeh.colors.named as bcn
from itertools import cycle
from ..indicators import pd, np, Callable
from functools import partial
from bokeh.models import Tabs
from bokeh.transform import factor_cmap
from bokeh.models import (
    CrosshairTool,
    CustomJS,
    ColumnDataSource,
    NumeralTickFormatter,
    Span,
    HoverTool,
    Range1d,
    WheelZoomTool,
    LabelSet,
)
from bokeh.layouts import gridplot, column, row
from bokeh.io import show
from bokeh.plotting import figure as _figure
from bokeh.models.glyphs import VBar
setattr(_figure, '_main_ohlc', False)
setattr(_figure, '_candles', False)

try:  # 版本API有变
    from bokeh.models import TabPanel as Panel
except:
    from bokeh.models import Panel
warnings.filterwarnings("ignore", category=UserWarning)
# 'JPY_PARENT_PID' in os.environ
# IS_JUPYTER_NOTEBOOK = 'JPY_INTERRUPT_EVENT' in os.environ
try:
    from IPython import get_ipython
    _shell = get_ipython()
    IS_JUPYTER_NOTEBOOK = _shell is not None and _shell.__class__.__name__ == 'ZMQInteractiveShell'
except (ImportError, AttributeError):
    IS_JUPYTER_NOTEBOOK = False
FILED = ['open', 'high', 'low', 'close']
_FILED = ['datetime', 'open', 'high', 'low', 'close', 'volume']
_FILED = ['datetime', 'open', 'high', 'low', 'close', 'volume']
_factors_plot: Callable = None

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
    height: auto;
}
.bk-Column {
    display: flex;
    flex-direction: column;
    height: auto;
}
.bk-Column > * {
    flex: 0 0 auto;
}
"""


def ffillnan(arr: np.ndarray) -> np.ndarray:
    if len(arr.shape) > 1:
        arr = pd.DataFrame(arr)
    else:
        arr = pd.Series(arr)
    arr.ffill(inplace=True)
    arr.bfill(inplace=True)
    return arr.values


def set_tooltips(fig: _figure, tooltips=(), vline=True, renderers=(), if_datetime: bool = True, if_date=False) -> None:
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
    fig.add_tools(HoverTool(
        point_policy='follow_mouse',
        renderers=renderers, formatters=formatters,
        tooltips=tooltips, mode='vline' if vline else 'mouse'),
    )


def new_bokeh_figure(plot_width, height=300) -> Callable:
    return partial(
        _figure,
        x_axis_type='linear',
        width=plot_width,
        height=height,
        width_policy='max',
        tools="xpan,xwheel_zoom,box_zoom,undo,redo,reset,save",  # ,crosshair
        active_drag='xpan',
        active_scroll='xwheel_zoom')


def new_bokeh_figure_main(plot_width, height=150) -> Callable:
    return partial(
        _figure,
        x_axis_type='linear',
        width_policy='max',
        width=plot_width,
        height=height,
        tools="xpan,xwheel_zoom",  # ,crosshair
        active_drag='xpan',
        active_scroll='xwheel_zoom')


def new_indicator_figure(new_bokeh_figure: Callable, fig_ohlc: _figure, plot_width, height=80, **kwargs) -> _figure:
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


def plot(strategys: list[Strategy],
         trade_signal: bool = True,
         black_style: bool = False,
         open_browser: bool = False,
         plot_width: int = None,
         plot_cwd: str = "",
         plot_name: str = "",
         save_plot: bool = False) -> Tabs:
    """
    Like much of GUI code everywhere, this is a mess.
    """
    from bokeh.io import reset_output
    reset_output()
    global IS_JUPYTER_NOTEBOOK, _factors_plot

    black_color = "white" if black_style else "black"
    white_color = "black" if black_style else "white"
    num = len(strategys)
    s_name: list[str] = [t.__class__.__name__ for t in strategys]
    results: list[list[pd.DataFrame]] = [t.get_results()
                                         for t in strategys]  # 回测结果
    
    # 修复：在 replay 模式下，使用_plot_datas 中的 K 线数据（已截断），而不是原始数据
    datas: list[list[pd.DataFrame]] = []
    for t in strategys:
        if getattr(t, '_strategy_replay', False) and hasattr(t, '_plot_datas') and t._plot_datas:
            # 使用_plot_datas 中已截断的 K 线数据
            datas.append(t._plot_datas[1])
        else:
            # 非 replay 模式使用原始数据
            datas.append([data for data in t._btklinedataset.values()])
    
    # ===== DEBUG: 打印数据长度信息 =====
    # print("\n" + "="*60)
    # print("[DEBUG] bokeh_plot.py - plot() 函数数据长度信息")
    # print("="*60)
    for i, s in enumerate(strategys):
        # print(f"\n策略 {i}: {s_name[i]}")
        # print(f"  - min_start_length: {getattr(s, 'min_start_length', 'N/A')}")
        # print(f"  - _strategy_replay: {getattr(s, '_strategy_replay', False)}")
        
        # K 线数据长度
        kline_lengths = [len(d.pandas_object) for d in datas[i]]
        # print(f"  - K 线数据长度 (datas): {kline_lengths}")
        
        # 回测结果长度
        result_lengths = [len(r) for r in results[i]]
        # print(f"  - 回测结果长度 (results): {result_lengths}")
        # 回测结果索引范围
        result_indexes = [(r.index.min(), r.index.max()) for r in results[i]]
        # print(f"  - 回测结果索引范围：{result_indexes}")
        
        # _plot_datas 中的 K 线长度
        if hasattr(s, '_plot_datas') and s._plot_datas:
            plot_kline_lengths = [len(d.pandas_object) if hasattr(d, 'pandas_object') else len(d) for d in s._plot_datas[1]]
            # print(f"  - K 线数据长度 (_plot_datas): {plot_kline_lengths}")
        
        # 指标数据长度
        plot_datas = s.get_plot_datas()
        if len(plot_datas) > 2:
            ind_datas = plot_datas[2]
            for j, ind_data in enumerate(ind_datas):
                if ind_data:
                    for k, ind in enumerate(ind_data):
                        if len(ind) > 8 and hasattr(ind[8], 'shape'):
                            print(f"  - 指标[{j}][{k}] 数据形状: {ind[8].shape}")
    
    # print("="*60 + "\n")
    btind_main = [
        [data.ismain for data in t._btklinedataset.values()] for t in strategys]
    btind_info = [t.get_plot_datas()[3]
                  for t in strategys]
    dir = strategys[0].get_base_dir()  # 路径
    indicator_datas: list[list[list]] = [t.get_plot_datas()[2]
                                         for t in strategys]  # 指标数据indicator
    datas_num: list[int] = [
        t._btklinedataset.num for t in strategys]  # 合约个数
    symbols: list[list[str]] = [
        [data.symbol for data in t._btklinedataset.values()] for t in strategys]  # 合约名称
    symbol_multi_cycle: list[list[int]] = [
        [data.cycle for data in t._btklinedataset.values()] for t in strategys]  # 周期
    start_value: list[float] = [t.config.value for t in strategys]  # 开始回测价值
    profit_plot: list[bool] = [
        t._profit_plot or t.config.profit_plot for t in strategys]
    click_policy: list[str] = [t.config.click_policy for t in strategys]
    # K线颜色
    # COLORS = [BEAR_COLOR, BULL_COLOR]
    COLORS = [bcn.tomato, bcn.lime]
    inc_cmap = factor_cmap('inc', COLORS, ['0', '1'])
    new_colors = {'bear': bcn.tomato, 'bull': bcn.lime}
    BAR_WIDTH = .8  # K宽度
    NBSP = '\N{NBSP}' * 4
    # 'solid', 'dashed', 'dotted', 'dotdash', 'dashdot'

    lines_setting = dict(line_dash='solid', line_width=1.3)

    factors_dfs = [[] for _ in strategys]
    factors_names = [[] for _ in strategys]
    with open(f'{dir}/strategy/autoscale_cb.js', encoding='utf-8') as _f:
        _AUTOSCALE_JS_CALLBACK = _f.read()

    value_source: list[list] = [[] for _ in range(num)]  # 价值数据

    if trade_signal:
        long_source: list[list] = [[] for _ in range(num)]
        short_source: list[list] = [[] for _ in range(num)]
        long_flat_source: list[list] = [[] for _ in range(num)]
        short_flat_source: list[list] = [[] for _ in range(num)]
        long_segment_source: list[list] = [[] for _ in range(num)]
        short_segment_source: list[list] = [[] for _ in range(num)]
    source: list[list[ColumnDataSource]] = [[] for _ in range(num)]  # K线数据
    figs_ohlc: list[list[_figure]] = [[] for _ in range(num)]
    ts: list[Tabs] = []
    fig_ohlc_list = [{} for i in range(num)]  # K线数据
    signal_ind_data_source: list[list[list]] = [[] for i in range(num)]  # K线数据

    for i, rs in enumerate(results):
        for j, trades in enumerate(rs):
            _td = datas[i][j]
            symbol = symbols[i][j]
            if trade_signal:
                _long_source, _short_source, _long_flat_source, _short_flat_source, \
                    _long_segment_source, _short_segment_source = get_trades_source(
                        _td, trades)
                long_source[i].append(_long_source)
                short_source[i].append(_short_source)
                long_flat_source[i].append(_long_flat_source)
                short_flat_source[i].append(_short_flat_source)
                long_segment_source[i].append(_long_segment_source)
                short_segment_source[i].append(_short_segment_source)
            value_source[i].append(ColumnDataSource(dict(
                index=trades.index,
                datetime=_td.datetime.values,
                value=trades.total_profit.values,
                level=np.ones(_td.shape[0])*start_value[i],
            )))

    for ik, _datas in enumerate(datas):
        panel = []
        all_plots = []
        # 用于存储主图表的 x_range 和长度，实现 X 轴联动
        main_x_range = None
        main_length = None
        for id, df in enumerate(_datas):
            factors_dfs[ik].append(getattr(df, "factors_df", None))
            factors_names[ik].append(df.sname)
            df = df.pandas_object
            signal_ind_data_source[ik].append([])
            df.reset_index(drop=True, inplace=True)

            index = df.index
            current_length = len(index)
            pad = (index[-1] - index[0]) / 20
            df = df[_FILED]
            df['volume5'] = df.volume.rolling(5).mean()
            df['volume10'] = df.volume.rolling(10).mean()
            df["inc"] = (df.close >=
                         df.open).values.astype(np.uint8).astype(str)
            df["Low"] = df[['low', 'high']].min(1)
            df["High"] = df[['low', 'high']].max(1)
            source[ik].append(ColumnDataSource(df))

            # K线图
            if btind_main[ik][id] and id != 0:
                # ismain=True 的图表，只有当长度一致时才共享主图表的 x_range 实现 X 轴联动
                if main_x_range is not None and main_length == current_length:
                    x_range = main_x_range
                else:
                    x_range = Range1d(index[0], index[-1],
                                      min_interval=10,
                                      bounds=(index[0] - pad,
                                              index[-1] + pad)) if index.size > 1 else None
                fig_ohlc: _figure = new_bokeh_figure_main(plot_width, btind_info[ik][id].get('height', 150))(
                    x_range=x_range)
                fig_ohlc._main_ohlc = True
            else:
                # 主图表（id=0），创建并保存 x_range 和长度
                x_range = Range1d(index[0], index[-1],
                                  min_interval=10,
                                  bounds=(index[0] - pad,
                                          index[-1] + pad)) if index.size > 1 else None
                if id == 0:
                    main_x_range = x_range
                    main_length = current_length
                fig_ohlc: _figure = new_bokeh_figure(plot_width, btind_info[ik][id].get('height', 300))(
                    x_range=x_range)
            fig_ohlc.css_classes = ["candle-chart"]
            # 显式设置Y轴，确保Y轴范围独立，避免Y轴联动
            # 获取当前K线数据的价格范围
            if index.size > 0:
                price_min = df['low'].min()
                price_max = df['high'].max()
                y_pad = (price_max - price_min) * 0.05  # 添加5%的边距
                fig_ohlc.y_range.start = price_min - y_pad
                fig_ohlc.y_range.end = price_max + y_pad
            _colors = btind_info[ik][id].get('candlestyle', new_colors)
            if _colors and id == 0:
                _COLORS = list(_colors.values())
                inc_cmap = factor_cmap('inc', _COLORS, ['0', '1'])
            # 上下影线
            fig_ohlc.segment('index', 'high', 'index', 'low',
                             source=source[ik][id], color=black_color)
            # # 实体线
            ohlc_bars = fig_ohlc.vbar('index', BAR_WIDTH, 'open', 'close', source=source[ik][id],
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

            spanstyle = btind_info[ik][id].get('spanstyle', [])
            for ohlc_span in spanstyle:
                fig_ohlc.add_layout(Span(**ohlc_span))

            # 指标数据
            indicators_data = indicator_datas[ik][id]
            indicator_candles_index = []
            indicator_figs = []
            indicator_h: list[str] = []
            indicator_l: list[str] = []

            if indicators_data:
                ohlc_colors = colorgen()
                ic = 0
                for ind_index, (isplot, name, lines, _lines, ind_name, is_overlay, category, indicator, doubles, plotinfo, span, _signal) in enumerate(indicators_data):
                    lineinfo: dict = plotinfo.get('linestyle', {})
                    datainfo = plotinfo.get('source', "")
                    signal_info: dict = plotinfo.get('signalstyle', {})

                    if doubles:
                        _doubles_fig = []
                        for ids in range(2):
                            if any(isplot[ids]):
                                is_candles = category[ids] == 'candles'
                                tooltips = []
                                colors = cycle(
                                    ohlc_colors if is_overlay[ids] else colorgen())
                                legend_label = name[ids]  # 初始化命名的名称
                                if is_overlay[ids] and not is_candles:  # 主图叠加
                                    fig = fig_ohlc
                                else:
                                    fig = new_indicator_figure(
                                        new_bokeh_figure, fig_ohlc, plot_width, plotinfo.get('height', 150))
                                    indicator_figs.append(fig)
                                    _mulit_ind = len(
                                        indicator[ids].shape) > 1
                                    source[ik][id].add(
                                        np.max(indicator[ids][:, np.arange(len(isplot[ids]))[isplot[ids]]], axis=1) if _mulit_ind else indicator[ids], f"{legend_label}_h")
                                    source[ik][id].add(
                                        np.min(indicator[ids][:, np.arange(len(isplot[ids]))[isplot[ids]]], axis=1) if _mulit_ind else indicator[ids], f"{legend_label}_l")
                                    indicator_h.append(f"{legend_label}_h")
                                    indicator_l.append(f"{legend_label}_l")
                                    ic += 1
                                _doubles_fig.append(fig)

                                if not is_candles:
                                    if_vbar = False
                                    for j in range(len(isplot[ids])):
                                        if isplot[ids][j]:
                                            _lines_name = _lines[ids][j]
                                            ind = indicator[ids][:, j]
                                            color = next(colors)
                                            source_name = lines[ids][j]
                                            if ind.dtype == bool:
                                                ind = ind.astype(
                                                    np.float64)
                                            source[ik][id].add(ind,
                                                               source_name)
                                            tooltips.append(
                                                f"@{source_name}{'{'}0,0.0[0000]{'}'}")
                                            # tooltips.append(f"@{source_name}{'{'}0,0.0[0000]{'}'}")
                                            _lineinfo = deepcopy(
                                                lines_setting)
                                            if _lines_name in lineinfo:
                                                _lineinfo = {
                                                    **_lineinfo, **lineinfo[_lines_name]}
                                            # if 'line_color' not in _lineinfo:
                                            if _lineinfo.get("line_color", None) is None:
                                                _lineinfo.update(
                                                    dict(line_color=color))
                                            if "price_line" in _lineinfo:
                                                del _lineinfo["price_line"]
                                            if "price_label" in _lineinfo:
                                                del _lineinfo["price_label"]
                                            if is_overlay[ids]:
                                                fig.line(
                                                    'index', source_name, source=source[ik][id],
                                                    legend_label=source_name, **_lineinfo)
                                            else:
                                                if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                                    if_vbar = True
                                                    if "zeros" not in source[ik][id].column_names:
                                                        source[ik][id].add(
                                                            [0.,]*len(ind), "zeros")
                                                    _line_inc = np.where(ind > 0., 1, 0).astype(
                                                        np.uint8).astype(str).tolist()
                                                    source[ik][id].add(
                                                        _line_inc, f"{_lines_name}_inc")
                                                    # if "line_color" in lineinfo[_lines_name]:
                                                    _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                                    if _line_inc_cmap is None:
                                                        _line_inc_cmap = factor_cmap(
                                                            f"{_lines_name}_inc", COLORS, ['0', '1'])
                                                    r = fig.vbar('index', BAR_WIDTH, 'zeros', source_name, source=source[ik][id],
                                                                 line_color='black', fill_color=_line_inc_cmap)
                                                else:
                                                    r = fig.line(
                                                        'index', source_name, source=source[ik][id],
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
                                        fig.yaxis.axis_label_text_color = black_color
                                        if fig_ohlc._main_ohlc:
                                            fig.yaxis.visible = False
                                        else:
                                            fig.yaxis.visible = True
                                        if len(lines) == 1:
                                            fig.legend.glyph_width = 0
                    else:
                        if any(isplot):
                            is_candles = category == 'candles'
                            if is_candles and len(lines) < 4:
                                is_candles = False
                            tooltips = []
                            colors = cycle(
                                ohlc_colors if is_overlay else colorgen())
                            legend_label = name  # 初始化命名的名称
                            if is_overlay and not is_candles:  # 主图叠加
                                if datainfo in fig_ohlc_list[ik]:  # 副图
                                    fig = fig_ohlc_list[ik].get(
                                        datainfo)
                                else:
                                    fig = fig_ohlc
                            elif is_candles:  # 副图是蜡烛图
                                # if any(isplot):
                                indicator_candles_index.append(ic)
                                assert len(lines) >= 4
                                lines = list(
                                    map(lambda x: x.lower(), lines))
                                filed_index = []
                                missing_index = []
                                for ii, file in enumerate(FILED):
                                    is_missing = True
                                    for n in lines:
                                        if file in n:
                                            filed_index.append(
                                                lines.index(n))
                                            is_missing = False
                                    else:
                                        if is_missing:
                                            missing_index.append(ii)
                                assert not missing_index, f"数据中缺失{[FILED[ii] for ii in missing_index]}字段"
                                for ie in filed_index:
                                    source[ik][id].add(indicator[:,
                                                                 ie], lines[ie])
                                index = np.arange(indicator.shape[0])
                                pad = (index[-1] - index[0]) / 20
                                fig_ohlc_ = new_indicator_figure(
                                    new_bokeh_figure, fig_ohlc, plot_width, plotinfo.get('height', 100))
                                fig_ohlc_.segment(
                                    'index', lines[filed_index[1]], 'index', lines[filed_index[2]], source=source[ik][id], color=black_color)
                                ohlc_bars_ = fig_ohlc_.vbar('index', BAR_WIDTH, lines[filed_index[0]], lines[filed_index[3]], source=source[ik][id],
                                                            line_color=black_color, fill_color=inc_cmap)
                                ohlc_tooltips_ = [
                                    ('x, y', NBSP.join(('$index',
                                                        '$y{0,0.0[0000]}'))),
                                    ('OHLC', NBSP.join((f"@{lines[filed_index[0]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{lines[filed_index[1]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{lines[filed_index[2]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{lines[filed_index[3]]}{'{'}0,0.0[0000]{'}'}")))]

                                fig_ohlc_.yaxis.axis_label = ind_name
                                fig_ohlc_.yaxis.axis_label_text_color = black_color
                                if fig_ohlc._main_ohlc:
                                    fig_ohlc_.yaxis.visible = False
                                else:
                                    fig_ohlc_.yaxis.visible = True
                                fig_ohlc_list[ik].update(
                                    {ind_name: fig_ohlc_})

                                for j in range(len(lines)):
                                    if j not in filed_index:
                                        if isplot[j]:
                                            tooltips = []
                                            _lines_name = _lines[j]
                                            ind = indicator[:, j]
                                            color = next(colors)
                                            source_name = lines[j]
                                            if ind.dtype == bool:
                                                ind = ind.astype(int)
                                            source[ik][id].add(ind,
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
                                                'index', source_name, source=source[ik][id],
                                                legend_label=source_name, **_lineinfo)
                                            ohlc_tooltips_.append(
                                                (_lines_name, NBSP.join(tuple(tooltips))))
                                set_tooltips(fig_ohlc_, ohlc_tooltips_,
                                             vline=True, renderers=[ohlc_bars_])
                                indicator_figs.append(fig_ohlc_)
                                ic += 1
                                _mulit_ind = len(indicator.shape) > 1
                                source[ik][id].add(np.max(indicator[:, np.arange(len(isplot))[isplot]], axis=1) if _mulit_ind else indicator,
                                                   f"{legend_label}_h")
                                source[ik][id].add(np.min(indicator[:, np.arange(len(isplot))[isplot]], axis=1) if _mulit_ind else indicator,
                                                   f"{legend_label}_l")
                                indicator_h.append(f"{legend_label}_h")
                                indicator_l.append(f"{legend_label}_l")

                            else:
                                if datainfo in fig_ohlc_list[ik]:  # 副图
                                    __fig = fig_ohlc_list[ik].get(
                                        datainfo)
                                else:
                                    __fig = fig_ohlc
                                fig = new_indicator_figure(
                                    new_bokeh_figure, __fig, plot_width, plotinfo.get('height', 150))
                                indicator_figs.append(fig)
                                ic += 1
                                _mulit_ind = len(indicator.shape) > 1
                                source[ik][id].add(np.max(indicator[:, np.arange(len(isplot))[isplot]], axis=1) if _mulit_ind else indicator,
                                                   f"{legend_label}_h")
                                source[ik][id].add(np.min(indicator[:, np.arange(len(isplot))[isplot]], axis=1) if _mulit_ind else indicator,
                                                   f"{legend_label}_l")
                                indicator_h.append(f"{legend_label}_h")
                                indicator_l.append(f"{legend_label}_l")

                            if not is_candles:
                                if_vbar = False
                                for j in range(len(isplot)):
                                    if isplot[j]:
                                        _lines_name = _lines[j]
                                        ind = indicator[:, j]
                                        color = next(colors)
                                        source_name = lines[j]
                                        if ind.dtype == bool:
                                            ind = ind.astype(int)
                                        source[ik][id].add(ind,
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
                                        if is_overlay:
                                            fig.line(
                                                'index', source_name, source=source[ik][id],
                                                legend_label=source_name, **_lineinfo)
                                        else:
                                            if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                                if_vbar = True
                                                if "zeros" not in source[ik][id].column_names:
                                                    source[ik][id].add(
                                                        [0.,]*len(ind), "zeros")
                                                _line_inc = np.where(ind > 0., 1, 0).astype(
                                                    np.uint8).astype(str).tolist()
                                                source[ik][id].add(
                                                    _line_inc, f"{_lines_name}_inc")
                                                _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                                if _line_inc_cmap is None:
                                                    _line_inc_cmap = factor_cmap(
                                                        f"{_lines_name}_inc", COLORS, ['0', '1'])
                                                r = fig.vbar('index', BAR_WIDTH, 'zeros', source_name, source=source[ik][id],
                                                             line_color='black', fill_color=_line_inc_cmap)
                                            else:
                                                r = fig.line(
                                                    'index', source_name, source=source[ik][id],
                                                    legend_label=source_name, **_lineinfo)

                                else:

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
                                    fig.yaxis.axis_label_text_color = black_color
                                    if len(lines) == 1:
                                        fig.legend.glyph_width = 0
                                    if fig_ohlc._main_ohlc:
                                        fig.yaxis.visible = False
                                    else:
                                        fig.yaxis.visible = True
                    if signal_info:
                        signal_ind_data_ = dict(
                            long_signal=None, exitlong_signal=None, short_signal=None, exitshort_signal=None)
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
                                        _lines, k)
                                    signaldata = indicator[index1][:, index2]
                                else:
                                    signaldata = indicator[:, _lines.index(
                                        k)]
                                signal_index = np.argwhere(
                                    signaldata > 0)[:, 0]
                                if signaloverlap:
                                    price_data = df[signalkey].values
                                    signal_fig = fig_ohlc
                                else:
                                    signal_fig = fig
                                    try:
                                        if doubles:
                                            index1, index2 = search_index(
                                                _lines, signalkey)
                                            price_data = indicator[index1][:, index2]
                                        else:
                                            price_data = indicator[:, _lines.index(
                                                signalkey)]
                                    except:
                                        price_data = signaldata.copy()
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
                                signal_ind_data_.update(
                                    {k: signal_source_})
                                signal_ind_data_source[ik][id].append(
                                    signal_ind_data_)

                                r = signal_fig.scatter(x='index', y='price', source=signal_ind_data_source[ik][id][-1].get(k), fill_color=signalcolor,
                                                       marker=signalmarker, line_color='black', size="size")
                                if islabel:
                                    # --- 新增：用 LabelSet 添加文字标签 ---
                                    signallabel.update(
                                        dict(background_fill_alpha=0.1, background_fill_color="white" if black_style else "black"))
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

            # 如果当前合约有交易但没有信号标记，从合约0复制信号标记
            if id > 0 and trade_signal and not signal_ind_data_source[ik][id]:
                ref_indicators = indicator_datas[ik][0]
                for ref_isplot, ref_name, ref_lines, ref__lines, ref_ind_name, ref_is_overlay, ref_category, ref_indicator, ref_doubles, ref_plotinfo, ref_span, ref__signal in ref_indicators:
                    ref_signal_info = ref_plotinfo.get('signalstyle', {})
                    if not ref_signal_info:
                        continue
                    signal_ind_data_ = dict(
                        long_signal=None, exitlong_signal=None, short_signal=None, exitshort_signal=None)
                    for k, v in ref_signal_info.items():
                        signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = list(
                            v.values())
                        if not signalshow:
                            continue
                        islabel = isinstance(signallabel, dict)
                        if islabel:
                            label_text = signallabel.pop("text", k)
                        # 从合约0获取信号索引
                        ref_source_list = signal_ind_data_source[ik][0]
                        signal_index = None
                        for sd in ref_source_list:
                            if k in sd and sd[k] is not None:
                                signal_index = sd[k].data['index']
                                break
                        if signal_index is None:
                            continue
                        # 使用当前合约的价格数据
                        if signaloverlap:
                            price_data = df[signalkey].values
                            signal_fig = fig_ohlc
                        else:
                            signal_fig = fig
                            try:
                                if ref_doubles:
                                    index1, index2 = search_index(
                                        ref__lines, signalkey)
                                    price_data = ref_indicator[index1][:, index2]
                                else:
                                    price_data = ref_indicator[:, ref__lines.index(
                                        signalkey)]
                            except:
                                price_data = np.zeros(len(signal_index))
                        signal_price = price_data[signal_index.astype(int)]
                        signal_datetime = df.datetime.values[signal_index.astype(int)]
                        if islabel:
                            signal_source_ = ColumnDataSource(dict(
                                index=signal_index,
                                datetime=signal_datetime,
                                price=signal_price,
                                size=[float(signalsize),] *
                                len(signal_index),
                                text=[label_text] *
                                len(signal_index),
                            ))
                        else:
                            signal_source_ = ColumnDataSource(dict(
                                index=signal_index,
                                datetime=signal_datetime,
                                price=signal_price,
                                size=[float(signalsize),] *
                                len(signal_index),
                            ))
                        signal_ind_data_.update({k: signal_source_})
                        r = signal_fig.scatter(x='index', y='price', source=signal_source_, fill_color=signalcolor,
                                              marker=signalmarker, line_color='black', size="size")
                        if islabel:
                            signallabel.update(
                                dict(background_fill_alpha=0.1, background_fill_color="white" if black_style else "black"))
                            labels = LabelSet(
                                x='index', y='price', text='text', source=signal_source_, **signallabel)
                            signal_fig.add_layout(labels)
                        tooltips = [(k, "@price{0.00}"),]
                        set_tooltips(signal_fig, tooltips,
                                     vline=False, renderers=[r,])
                    if any(v is not None for v in signal_ind_data_.values()):
                        signal_ind_data_source[ik][id].append(
                            signal_ind_data_)

            set_tooltips(fig_ohlc, ohlc_tooltips,
                         vline=True, renderers=[ohlc_bars])
            fig_ohlc.yaxis.axis_label = f"{symbols[ik][id]}_{symbol_multi_cycle[ik][id]}"
            fig_ohlc.yaxis.axis_label_text_color = black_color

            custom_js_args = dict(ohlc_range=fig_ohlc.y_range, indicator_range=[indicator_figs[_ic].y_range for _ic in range(len(indicator_figs))],
                                  indicator_h=indicator_h, indicator_l=indicator_l, source=source[ik][id])

            # 成交量
            fig_volume = new_indicator_figure(
                new_bokeh_figure, fig_ohlc, plot_width, y_axis_label="volume", height=60)
            fig_volume.css_classes = ["volume-chart"]
            fig_volume.xaxis.formatter = fig_ohlc.xaxis[0].formatter
            fig_volume.xaxis.visible = True
            fig_ohlc.xaxis.visible = False  # Show only Volume's xaxis
            
            r_volume = fig_volume.vbar(
                'index', BAR_WIDTH, 'volume', source=source[ik][id], color=inc_cmap)
            colors = cycle(colorgen())
            r_volume5 = fig_volume.line('index', 'volume5', source=source[ik][id],
                                        legend_label='volume5', line_color=next(colors),
                                        line_width=1.3)
            r_volume10 = fig_volume.line('index', 'volume10', source=source[ik][id],
                                         legend_label='volume10', line_color=next(colors),
                                         line_width=1.3)
            set_tooltips(fig_volume, [
                        ('volume', '@volume{0.00}'), ('volume5', '@volume5{0.00}'), ('volume10', '@volume10{0.00}'),], renderers=[r_volume])
            fig_volume.yaxis.formatter = NumeralTickFormatter(
                format="0 a")  # format="0"
            fig_volume.yaxis.axis_label_text_color = black_color
            
            if fig_ohlc._main_ohlc:
                fig_volume.yaxis.visible = False
                # 当 ismain=True 时，不添加自动缩放回调，避免影响主图表的 Y 轴
            else:
                fig_volume.yaxis.visible = True
                # 只有非 ismain 的图表才添加自动缩放回调
                custom_js_args.update(volume_range=fig_volume.y_range)
                fig_ohlc.x_range.js_on_change('end', CustomJS(args=custom_js_args,
                                                              code=_AUTOSCALE_JS_CALLBACK))
            # 交易信号线段（每个合约都显示）
            if trade_signal:
                # 多头连线：用虚线连接入场→出场，悬停显示P/L
                ls = fig_ohlc.segment(x0='index', y0='price', x1='flat_index', y1='flat_price',
                                    source=long_segment_source[ik][id], color='yellow' if black_style else "blue", line_width=3, line_dash="4 4")
                ss = fig_ohlc.segment(x0='index', y0='price', x1='flat_index', y1='flat_price',
                                    source=short_segment_source[ik][id], color='yellow' if black_style else "blue", line_width=3, line_dash="4 4")
                # 多单入场信号：正三角
                l_entry = fig_ohlc.scatter(x='index', y='price', source=long_segment_source[ik][id],
                                           marker='triangle', size=12, color='red', fill_color='red')
                # 空单入场信号：倒三角
                s_entry = fig_ohlc.scatter(x='index', y='price', source=short_segment_source[ik][id],
                                           marker='inverted_triangle', size=12, color='green', fill_color='green')
                # 为 datetime 字段添加格式化器
                fig_ohlc.add_tools(HoverTool(
                    point_policy='follow_mouse',
                    renderers=[ls, ss, l_entry, s_entry],
                    formatters={'@entry_datetime': 'datetime', '@exit_datetime': 'datetime'},
                    tooltips=[("方向", "@side"), ("入场时间", "@entry_datetime{%Y-%m-%d %H:%M}"), ("出场时间", "@exit_datetime{%Y-%m-%d %H:%M}"), ("持仓分钟", "@hold_minutes{0}"), ("入场价", "@price{0.00}"), ("出场价", "@flat_price{0.00}"), ("P/L", "@profit{0.00}")],
                    mode='mouse'))
            plots: list[_figure] = [
                fig_ohlc, fig_volume]+indicator_figs
            # 权益曲线（仅第一个合约显示）
            if id == 0 and profit_plot[ik]:
                source_key = 'value'
                fig_value = new_indicator_figure(new_bokeh_figure, fig_ohlc, plot_width,
                                                 y_axis_label=source_key,
                                                 height=90)
                fig_value.css_classes = ["value-chart"]
                fig_value.tags = ["profit_plot"]
                fig_value.patch('index', 'level',
                                source=value_source[ik][id],
                                fill_color='#ffffea', line_color='#ffcb66', line_dash='dashed')
                r_value = fig_value.line(
                    'index', source_key, source=value_source[ik][id], legend_label='value', line_color='blue', line_width=2, line_alpha=1)
                tooltip_format = f'@{source_key}{{+0,0.[00]}}'
                tick_format = '0,0.[00]'
                set_tooltips(
                    fig_value, [(source_key, tooltip_format)], renderers=[r_value,])

                fig_value.yaxis.formatter = NumeralTickFormatter(
                    format=tick_format)
                fig_value.yaxis.axis_label = 'Value'
                fig_value.yaxis.axis_label_text_color = black_color
                plots.insert(0, fig_value)
            figs_ohlc[ik].append(fig_ohlc)
            linked_crosshair = CrosshairTool(
                dimensions='both', line_color=black_color)

            for f in plots:
                if f.legend:
                    f.legend.nrows = 1
                    f.legend.label_height = 6
                    f.legend.visible = True
                    f.legend.location = 'top_left'
                    f.legend.border_line_width = 0
                    f.legend.padding = 1
                    f.legend.spacing = 0
                    f.legend.margin = 0
                    f.legend.label_text_font_size = '8pt'
                    f.legend.label_text_line_height = 1.2
                    f.legend.click_policy = click_policy[ik]

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
                sizing_mode='scale_width',  # 改为scale_width，只横向拉伸，保持纵向高度
                toolbar_options=dict(logo=None),
                merge_tools=True
            )
            all_plots.append(plots)

        ismain = btind_main[ik][1:]

        # 初始化 panels 列表
        panels = []

        if any(ismain):
            _ip = 0
            _all_plots = [_p for _ip, _p in enumerate(
                all_plots[1:]) if not ismain[_ip]]
            first_plot = all_plots[0]
            row_plots = []
            for _ismain, _plots in list(zip(ismain, all_plots[1:])):
                if _ismain:
                    _ip += 1
                    figs = gridplot(
                        _plots,
                        ** kwargs
                    )
                    row_plots.append(figs)
            controls = row(*row_plots, width_policy='max') if row_plots else None
            figs = gridplot(
                first_plot,
                ** kwargs
            )
            # 将 ismain=True 的图表合并到主布局
            if controls:
                _lay = column(controls, figs, width_policy='max',
                              sizing_mode='scale_width')
            else:
                _lay = column(figs, width_policy='max',
                              sizing_mode='scale_width')
            name_ = '_'.join(
                [symbols[ik][0], *(str(symbol_multi_cycle[ik][__ip]) for __ip in range(1, _ip+1))])
            # 将合并后的布局添加到 panels 列表的开头
            panels.append(
                Panel(child=_lay, title=name_))
            # 更新 all_plots 为不包含 ismain=True 的图表
            all_plots = _all_plots
        else:
            # 如果没有 ismain=True 的图表，将第一个图表作为默认面板
            figs = gridplot(
                all_plots[0],
                ** kwargs
            )
            panels.append(
                Panel(child=column(figs, width_policy='max', sizing_mode='scale_width'),
                      title=f"{symbols[ik][0]}_{symbol_multi_cycle[ik][0]}"))
            all_plots = all_plots[1:]
        # 添加剩余的 ismain=False 的图表
        for _ips, __plots in enumerate(all_plots):
            layout = column(
                children=__plots,
                sizing_mode='scale_width',
                css_classes=["dynamic-column"],
                stylesheets=[panel_CUSTOM_CSS],  # 注入自定义CSS
            )

            panels.append(
                Panel(
                    child=layout, title=f"{symbols[ik][_ips + (1 if any(ismain) else 1)]}_{symbol_multi_cycle[ik][_ips + (1 if any(ismain) else 1)]}"))
        # 添加因子分析图表作为新的Panel
        for fi, factors_df in enumerate(factors_dfs[ik]):
            if factors_df is not None:
                if _factors_plot is None:
                    from .factors_plot import factors_plot
                    _factors_plot = factors_plot
                factor_layout = _factors_plot(factors_df)  # 生成因子分析布局
                # 加入当前策略的panels
                panels.append(Panel(child=factor_layout,
                              title=f"{factors_names[ik][fi]}因子分析"))

        ts.append(Tabs(tabs=panels,
                  width=plot_width if plot_width else None, width_policy='max',
                  sizing_mode='scale_width',
                       css_classes=["dark-tabs"] if black_style else [],
                       stylesheets=[DARK_TABS_CSS] if black_style else []
                       ))
    # 创建策略图表的面板
    all_panels = [Panel(child=t, title=s_name[i]) for i, t in enumerate(ts)]
    tabs = Tabs(tabs=all_panels,
                background=white_color, width=plot_width if plot_width else None, width_policy='max',
                sizing_mode='scale_width',
                css_classes=["dark-tabs"] if black_style else [],
                stylesheets=[DARK_TABS_CSS] if black_style else []
                )
    if open_browser:
        IS_JUPYTER_NOTEBOOK = False
    INLINE.css_raw.append(panel_CUSTOM_CSS)  # 确保CSS被包含
    if IS_JUPYTER_NOTEBOOK:
        from bokeh.io import output_notebook
        notebook_handle = True
        open_browser = False
        output_notebook(INLINE)
    else:
        notebook_handle = False
        open_browser = True

    # 保存图表
    if plot_name and isinstance(plot_name, str):
        if not plot_name.endswith('.html'):
            plot_name += 'bokeh_plot.html'
    else:
        plot_name = 'bokeh_plot.html'
    plot_cwd = plot_cwd if isinstance(
        plot_cwd, str) and plot_cwd else os.path.join(os.path.dirname(os.path.abspath(__file__)), "plots")
    if not os.path.exists(plot_cwd):
        os.makedirs(plot_cwd, exist_ok=True)
    FileName = os.path.join(plot_cwd, plot_name)
    html = file_html(tabs, CDN, "Strategy Plot")
    if save_plot or IS_JUPYTER_NOTEBOOK:
        with open(FileName, 'w', encoding='utf-8') as f:
            f.write(html)

    # 显示图表
    try:
        show(tabs, browser=None if open_browser else 'none',
             notebook_handle=notebook_handle)
    except Exception as e:
        if IS_JUPYTER_NOTEBOOK:
            from IPython.display import display, HTML

            # 读取保存的 HTML 文件内容
            with open(FileName, 'r', encoding='utf-8') as f:
                html_content = f.read()

            # 在 Jupyter 中显示 HTML 内容
            display(HTML(html_content))
        else:
            import webbrowser
            # 在默认浏览器中打开 HTML 文件
            webbrowser.open('file://' + os.path.abspath(FileName))

    return tabs


def get_trades_source(data: pd.DataFrame, trades: pd.DataFrame) -> tuple[ColumnDataSource]:
    datetime = data.datetime.values
    high = data.high.values
    low = data.low.values
    close = data.close.values
    position = trades.positions.values
    sizes = trades.sizes.values
    ps = position*sizes
    cum_profit = trades.cum_profits.values
    short_ticks = []
    long_ticks = []
    long_flat_ticks = []
    short_flat_ticks = []
    long_profit = []
    short_profit = []
    pre_pos = 0
    for i, pos_ in enumerate(position):
        cp = cum_profit[i]
        if pos_ == -1 and pre_pos == 0:
            short_ticks.append(i)
            pre_cp = cum_profit[max(i-1, 0)]  # 开仓bar已扣手续费，取前一根bar避免计入
        elif pos_ == 1 and pre_pos == 0:
            long_ticks.append(i)
            pre_cp = cum_profit[max(i-1, 0)]  # 开仓bar已扣手续费，取前一根bar避免计入
        elif pos_ == 0:
            if pre_pos == 1:
                long_flat_ticks.append(i)
                long_profit.append(cp - pre_cp)
            elif pre_pos == -1:
                short_flat_ticks.append(i)
                short_profit.append(cp - pre_cp)
        else:
            if pos_ != pre_pos and pre_pos != 0:
                if pos_ == 1:
                    short_flat_ticks.append(i)
                    short_profit.append(cp - pre_cp)

                    long_ticks.append(i)
                    pre_cp = cp
                else:
                    long_flat_ticks.append(i)
                    long_profit.append(cp - pre_cp)
                    short_ticks.append(i)
                    pre_cp = cp
        pre_pos = pos_

    # long
    long_source = ColumnDataSource(dict(
        index=long_ticks,
        datetime=datetime[long_ticks],
        price=low[long_ticks],
        pos=ps[long_ticks],
        size=[12.,]*len(long_ticks)
    ))
    short_source = ColumnDataSource(dict(
        index=short_ticks,
        datetime=datetime[short_ticks],
        price=high[short_ticks],
        pos=ps[short_ticks],
        size=[12.,]*len(short_ticks)
    ))
    long_flat_source = ColumnDataSource(dict(
        index=long_flat_ticks,
        datetime=datetime[long_flat_ticks],
        price=high[long_flat_ticks],
        pos=ps[long_flat_ticks],
        profit=long_profit,
        size=[12.,]*len(long_flat_ticks)
    ))

    short_flat_source = ColumnDataSource(dict(
        index=short_flat_ticks,
        datetime=datetime[short_flat_ticks],
        price=low[short_flat_ticks],
        pos=ps[short_flat_ticks],
        profit=short_profit,
        size=[12.,]*len(short_flat_ticks)
    ))
    if len(long_ticks) != len(long_flat_ticks):
        long_ticks = long_ticks[:-1]
    if len(short_ticks) != len(short_flat_ticks):
        short_ticks = short_ticks[:-1]

    long_segment_source = ColumnDataSource(dict(
        index=long_ticks,
        price=close[long_ticks],
        flat_index=long_flat_ticks,
        flat_price=close[long_flat_ticks],
        profit=long_profit,
        side=['多头']*len(long_ticks),
        entry_datetime=datetime[long_ticks],
        exit_datetime=datetime[long_flat_ticks],
        hold_minutes=(datetime[long_flat_ticks] - datetime[long_ticks]).astype('timedelta64[m]').astype(float),
    ))
    short_segment_source = ColumnDataSource(dict(
        index=short_ticks,
        price=close[short_ticks],
        flat_index=short_flat_ticks,
        flat_price=close[short_flat_ticks],
        profit=short_profit,
        side=['空头']*len(short_ticks),
        entry_datetime=datetime[short_ticks],
        exit_datetime=datetime[short_flat_ticks],
        hold_minutes=(datetime[short_flat_ticks] - datetime[short_ticks]).astype('timedelta64[m]').astype(float),
    ))

    return long_source, short_source, long_flat_source, short_flat_source, long_segment_source, short_segment_source
