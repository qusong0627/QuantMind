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
from ..utils import (text_width_pt, resolve_signal_text_style,
                     signal_text_below, user_dir, _label_text_style,
                     normalise_text_item)
# [新增] marker 兼容: 把 Markers 里的 lightweight_charts 专用名
#   (arrow_up/arrow_down) 与 label_up/label_down 收敛为 bokeh 可渲染名
from ..constant import to_bokeh_marker
from functools import partial
from bokeh.models import Tabs
from bokeh.transform import factor_cmap
from bokeh.models import (
    CrosshairTool,
    CustomJS,
    CustomJSTickFormatter,
    CustomJSTicker,
    ColumnDataSource,
    FixedTicker,
    NoOverlap,
    NumeralTickFormatter,
    Span,
    HoverTool,
    Range1d,
    WheelZoomTool,
    LabelSet,
    Band,
)
from bokeh.events import MouseLeave
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
        color: #cccccc;
        /* 两个顶栏之间的间隔线（--divider 用的是 --divider-color）
           默认是 #D7D7D8（浅色下发白），深色下改成深一点的灰 */
        --divider-color: #454545;
    }
    :host(.dark-tabs) > .bk-header {
        background-color: #333333;
    }
    :host(.dark-tabs) .bk-header {
        background-color: #333333;
        border-bottom-color: #454545;
    }
    :host(.dark-tabs) .bk-tab {
        color: #cccccc;
        border-color: transparent;
    }
    :host(.dark-tabs) .bk-tab:hover {
        background-color: #3d3d3d;
    }
    :host(.dark-tabs) .bk-tab.bk-active {
        background-color: #1a1a1a;
        color: #ffffff;
        border-color: #555555;
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

#: 深色 / 浅色 tooltip + 钉位脚本与 ``minibt/btplot.py`` 共用（两个 bokeh 绘图
#: 模块保持一致：bokeh 的 tooltip 是挂在 <body> 下的 ``.bk-Tooltip``，只能用
#: 全局 CSS 覆盖它使用的 CSS 变量）。
from ..btplot import (DARK_TOOLTIP_CSS, LIGHT_TOOLTIP_CSS,  # noqa: E402
                      FULLSCREEN_PAGE_CSS as FULLSCREEN_CSS)

#: 全屏布局（主图 : 副图 = 3 : 1，整页铺满，不出现纵向滚动条）。
#: bokeh 3.x 的布局都在 shadow DOM 里，**全局 CSS 穿透不进去**，所以这里拆成
#: 三个片段，通过 ``Model.stylesheets`` 注入到 Tabs / Column / Figure 自己的
#: shadow root（用 ``:host`` 命中元素本身）。
#: 注：`FULLSCREEN_CSS`（页面根节点边距归零 + 禁止滚动条）定义在 `btplot.py`。
#: 外层 Tabs：铺满整个视口（内容区自动 = 视口 - 顶部 tab 高度）
FULLSCREEN_TABS_CSS = ":host { height: 100vh !important; }"
#: 内层 Tabs：只占外层 Tabs 的内容区，不能再是 100vh（否则会多出
#:  外层 tab 的高度 → 出现纵向滚动条）
FULLSCREEN_TABS_INNER_CSS = ":host { height: 100% !important; }"
#: 给承载 figures 的 column 用：竖向 flex、占满 Tabs 内容区
FULLSCREEN_COLUMN_CSS = (
    ":host { display: flex !important; flex-direction: column !important; "
    "height: 100% !important; overflow: hidden !important; }"
)


def _figure_fullscreen_css(weight) -> str:
    """单张图在全屏下的高度权重（主图 3，指标副图 1，成交量副图 0.5）。"""
    return (
        ":host { flex: %s 1 0 !important; height: auto !important; "
        "min-height: 28px !important; }" % weight
    )

#: 左上角数值标签 / 十字线联动 / 时间刻度 JS 与 btplot.py 共用
from ..btplot import (OVERLAY_SETUP_JS, OVERLAY_UPDATE_JS,  # noqa: E402
                      _TIME_TICK_JS, _TIME_TICKER_JS)


#: 右侧 Y 轴固定宽度（bokeh 3.x 的轴画在 canvas 上，刻度数字宽度变化会推动
#: 绘图区左右抖动 —— 给承载右侧轴的 canvas 面板固定最小宽度即可稳定）。
AXIS_WIDTH_CSS = (
    ".bk-CanvasPanel.bk-right { min-width: 68px !important; }"
)


#: 水印 CSS：挂在**主 K 线图 figure 自己的 stylesheets** 上，用灵活的
#: ``:host::after`` —— Bokeh 没有原生水印，这是最省事且自动居中的做法
#: （全屏/缩放/resize 都不用 JS 跟着算坐标）。
_WATERMARK_CSS = """
:host { position: relative; }
:host::after {
    content: %s;
    position: absolute;
    top: 0; right: 0; bottom: 0; left: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 26pt;
    font-weight: 600;
    letter-spacing: 0.06em;
    color: %s;
    white-space: nowrap;
    pointer-events: none;
    z-index: 5;
}
"""


def _watermark_text(strategy, contract: str) -> Optional[str]:
    """该合约要画的水印文本（与 pylightcharts 共用同一套规则）。"""
    try:
        from .realtime import strategy_watermark
        return strategy_watermark(strategy, contract)
    except Exception:
        return None


def _bokeh_text_options(style: dict) -> dict:
    """规范逐点文本样式 → bokeh ``LabelSet`` 关键字（**bokeh 侧唯一映射点**）。

    键名只在 ``minibt.utils`` 定义一套（``SIGNAL_TEXT_STYLE_KEYS``），两个引擎
    各自只做"规范键 → 本引擎参数名"这一次翻译。
    """
    if not isinstance(style, dict) or not style:
        return {}
    out: dict = {}
    if style.get('font_size') is not None:
        out['text_font_size'] = f'{float(style["font_size"]):g}pt'
    if style.get('text_color'):
        out['text_color'] = style['text_color']
    if style.get('font_family'):
        out['text_font'] = style['font_family']
    weight = style.get('font_weight')
    slant = style.get('font_style')
    words = [w for w in (str(weight or ''), str(slant or ''))
             if w and w != 'normal']
    if words:
        out['text_font_style'] = ' '.join(words)
    if style.get('text_align'):
        out['text_align'] = style['text_align']
    # 像素偏移：规范键 -> bokeh 的 `x_offset` / `y_offset`（两者都是屏幕单位）
    if style.get('offset_x') is not None:
        out['x_offset'] = float(style['offset_x'])
    if style.get('offset_y') is not None:
        out['y_offset'] = float(style['offset_y'])
    for key in ('background_fill_color', 'background_fill_alpha',
                'border_line_color', 'border_line_alpha',
                'border_line_width'):
        if style.get(key) is not None:
            out[key] = style[key]
    return out


def _apply_figure_watermark(fig, text, black_style: bool) -> None:
    """给一张 Bokeh 图加水印（叠在图上、居中、不吃鼠标事件）。

    ``text`` 为假（``config.watermark=False``）时啥也不做。
    """
    if not text:
        return
    import json as _json
    color = ('rgba(200, 205, 215, 0.13)' if black_style
             else 'rgba(80, 85, 95, 0.10)')
    css = _WATERMARK_CSS % (_json.dumps(str(text)), color)
    try:
        fig.stylesheets = list(fig.stylesheets or []) + [css]
    except Exception:
        pass


def theme_css_js(black_style: bool, fullscreen: bool = False,
                 accent: Optional[str] = None) -> tuple[str, str]:
    """返回 ``(css_text, setup_js)``。

    * ``css_text``：tooltip 主题色 + 全屏页面 CSS，需要注入到一个 ``<style>``。
    * ``setup_js``：主题全局变量 + 数值标签/十字线 ``OVERLAY_SETUP_JS``。

    静态 HTML 路径用 :func:`_theme_head` 包进 ``<style>/<script>``；bokeh
    **server**（回放）无法后处理 ``<body>``，改用
    ``doc.js_on_event(DocumentReady, CustomJS(...))`` 注入同一份 CSS/JS。
    """
    import json as _json
    css = DARK_TOOLTIP_CSS if black_style else LIGHT_TOOLTIP_CSS
    css += AXIS_WIDTH_CSS
    if fullscreen:
        css += FULLSCREEN_CSS
    accent_js = _json.dumps(accent) if accent else 'null'
    js = (f'window.__minibt_dark__={str(bool(black_style)).lower()};'
          f'window.__minibt_accent__={accent_js};'
          + OVERLAY_SETUP_JS)
    return css, js


def _theme_head(black_style: bool, fullscreen: bool = False,
                accent: Optional[str] = None) -> str:
    """返回需要注入到 HTML ``<head>``（或 Jupyter INLINE）的全局样式/脚本。"""
    css, js = theme_css_js(black_style, fullscreen, accent)
    return f'<style>{css}</style><script>{js}</script>'


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
    # 数值改为左上角标签（OVERLAY_*）：不再弹 tooltip，但 HoverTool 仍用于
    # 捕捉鼠标位置并触发刷新回调（交易信号的 tooltip 由调用处单独创建，保留）。
    hover = HoverTool(
        point_policy='follow_mouse',
        renderers=renderers, formatters=formatters,
        tooltips=None, mode='vline' if vline else 'mouse')
    hover.name = 'minibt_overlay'   # 之后统一挂 fig_id + spans 回调
    try:
        hover.callback = CustomJS(args=dict(fig_id=fig.id),
                                  code=OVERLAY_UPDATE_JS)
    except Exception:
        ...
    fig.add_tools(hover)


def new_bokeh_figure(plot_width, height=300) -> Callable:
    return partial(
        _figure,
        x_axis_type='linear',
        width=plot_width,
        height=height,
        width_policy='max',
        y_axis_location='right',       # 所有 Y 轴放右侧
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
        y_axis_location='right',       # 所有 Y 轴放右侧
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


def _tail_align_indicators(indicators, length):
    """把指标数组对齐到 K 线长度（都取**尾部**）。

    实盘开启「无限历史」（``history_length > data_length``）时，KLine 的
    ``_inplace_values`` 只保留最后 ``_preload`` 根（图表开局先放出的量，如
    200），而 ``get_plot_datas()`` 里的指标数组仍是全量（如 1000）—— 不对齐
    就会在取信号价时报
    ``boolean index did not match indexed array ... size of axis is 200 but
    size of corresponding boolean axis is 1000``。

    ``indicators`` 是 ``get_plot_datas()[2]`` 的一个分组（每项是元组，第 7 项
    才是指标数组）；只有比 K 线**长**时才截尾，短的原样返回。
    """
    if not length or not indicators:
        return indicators
    aligned = []
    for item in indicators:
        if not isinstance(item, (list, tuple)) or len(item) < 8:
            aligned.append(item)
            continue
        array = item[7]
        try:
            if array is not None and hasattr(array, 'shape') \
                    and array.shape[0] > length:
                item = list(item)
                item[7] = array[-length:]
                item = tuple(item)
        except Exception:                     # 形状异常：原样返回，别把图带崩
            pass
        aligned.append(item)
    return aligned


def _span_values(indicator, isplot):
    """取「当前指标里被绘制的那几列」的数值 —— 给自动均值虚线用。

    ★ [BUG 修复] 调用它的那段代码位于 ``for j in range(len(isplot)):`` 的
      **for-else** 子句里：VP 指标（``category='vp'``）的 ``isplot`` 全为 False，
      循环体一次都没执行，但 **for-else 仍会执行** —— 原代码直接用了 ``ind``，
      而它此时还是**上一个指标遗留的变量**（可能是 list），于是
      ``ind.astype(np.float32)`` 抛 ``AttributeError: 'list' object has no
      attribute 'astype'``（纯 VP 副图 ``RollingVolumeProfile(overlap=False)`` 必崩）。
      这里改为按当前指标的“要绘制的列”重新取数，既不崩、均值也才对得上。
    """
    try:
        a = np.asarray(indicator, dtype=np.float64)
        if a.ndim > 1:
            m = np.asarray(isplot, dtype=bool)
            if m.size != a.shape[1]:
                m = np.ones(a.shape[1], dtype=bool)
            if m.any():
                a = a[:, m]
        return a.reshape(-1).astype(np.float32)
    except Exception:
        try:
            return np.asarray(indicator, dtype=np.float32).reshape(-1)
        except Exception:
            return np.array([], dtype=np.float32)



def _nan_reduce_axis1(a, mode: str) -> np.ndarray:
    """按行做 NaN 安全的 max/min(等价 ``np.nanmax`` / ``np.nanmin``),
    但对整行全 NaN 不会触发 RuntimeWarning, 而是返回 NaN。

    框架用该结果生成副图的 ``*_h`` / ``*_l`` 极值列, 供前端 autoscale
    回调按可视区间缩放副图 y 轴。若直接用 ``np.max`` / ``np.min``, 只要
    某行存在 NaN(例如 vbar 信号柱在无信号处恒为 NaN)整行极值即为 NaN,
    前端会把副图 y_range 设成 NaN, 表现为缩放/拖动后副图空白。
    """
    arr = np.asarray(a, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    valid = ~np.isnan(arr)
    fill = -np.inf if mode == 'max' else np.inf
    filled = np.where(valid, arr, fill)
    out = filled.max(axis=1) if mode == 'max' else filled.min(axis=1)
    out[~valid.any(axis=1)] = np.nan
    return out


# ======================================================================
# Volume Profile（`category == 'vp'`）绘制
# ======================================================================
def _vp_interactive(fig, vp_src, indicator, vidx, vmax, band, anchor,
                    bar_color, poc_color, ax_vol=None, vticks=None,
                    profile_frac: float = 0.30,
                    lo_arr=None, hi_arr=None, y_mode: str = 'price'):
    """照 test8.py 的 **ANCHOR_MODE="right_edge"** 给 VP 加交互。

    与 test8 完全一致的几何约定（都在**主轴 index 数据单位**里算, 不换坐标系）::

        span   = x1 - x0                    # 当前**可见**序号跨度(RangesUpdate 自带)
        BAND   = span * PROFILE_FRAC        # 满量程柱长(随缩放自适应)
        anchor = x1                         # ★ right_edge: 锚点=视口右端=成交量 0 刻度
        right  = anchor                     # 柱右端 = 0 刻度
        left   = anchor - v / VMAX * BAND   # 向左生长

    触发源（缺一不可, 且事件必须进 `subscribed_events` 否则前端根本不发）:
    - `MouseMove`    -> 统计"鼠标所在那根 K 线 + 前 LEN-1 根"
    - `MouseLeave`   -> 回到"最右可见 K 线"
    - `RangesUpdate` -> ★ 拖动/缩放主通道(自带 x0/x1), 统计"最右可见 K 线"
    - `x_range` 的 start/end 变化 -> 兜底再对齐一次
    """
    try:
        from bokeh.models import (ColumnDataSource as _VPCDS,
                                  CustomJS as _VPCJS)
        from bokeh.events import MouseMove, MouseLeave, RangesUpdate
        # 注意: 传进来的 indicator **已经是切好的 VP 切片**(调用端做了 _ind2[:, _vidx]),
        #   不能再按 vidx 切一次(否则 IndexError)。vidx 仅作兼容保留。
        prof = np.asarray(indicator)
        if prof.ndim == 1:
            prof = prof.reshape(-1, 1)
        roll = _VPCDS(dict(
            {"x": np.arange(prof.shape[0], dtype=np.float64)},
            **{f"p{j}": prof[:, j] for j in range(prof.shape[1])}))
        code = """
// ★ NB 必须是**档数**(矩阵列数), 不能取 p0.length ——
//   p{j} 是矩阵的一列, 长度 = K线根数! 取错会让循环去访问不存在的
//   p40/p41... -> undefined[i] -> 每次回调抛错 -> 分布永远不更新。
const dt = roll.data["x"], N = dt.length, NB = N_BINS;
const xr = range;
function barAt(t){
  if (t == null || isNaN(t)) return N - 1;
  return Math.max(0, Math.min(N - 1, Math.round(t)));
}
function showBar(i, x0, x1){
  i = Math.max(0, Math.min(N - 1, i));
  const span = (x0 != null && x1 != null && x1 > x0) ? (x1 - x0) : N;
  const BAND = span * FRAC;
  const anchor = (x1 != null && x1 > x0) ? x1 : (N - 1);   // ★ right_edge
  const vol = [], left = [], right = [], col = [];
  let mx = 0;
  for (let j = 0; j < NB; j++){
    const v = roll.data["p" + j][i] || 0;
    vol.push(v); if (v > mx) mx = v;
  }
  // ★ 档区逐根滚动: 锚点那根的档区决定柱子的 y(价格档中心)与 height(档高)。
  //   只改 left/right 而不改 y/height, 鼠标移到别的 K 线时分布就会落在错误的价位。
  if (YMODE === 'price' && LO != null && HI != null){
    const lo = LO[i], hi = HI[i];
    if (isFinite(lo) && isFinite(hi) && hi > lo){
      const bw = (hi - lo) / NB;
      const yv = [], ht = [];
      for (let j = 0; j < NB; j++){
        yv.push(lo + (j + 0.5) * bw);
        ht.push(bw * 0.88);
      }
      src.data["y"] = yv; src.data["height"] = ht;
    }
  }
  for (let j = 0; j < NB; j++){
    right.push(anchor);
    left.push(anchor - vol[j] / VMAX * BAND);
    col.push((mx > 0 && vol[j] >= mx) ? POC : BAR);
  }
  src.data["left"] = left; src.data["right"] = right; src.data["color"] = col;
  src.change.emit();
  if (ax_vol != null && VTICKS != null){
    const tk = [], ov = {};
    for (let k = 0; k < VTICKS.length; k++){
      const x = anchor - VTICKS[k] / VMAX * BAND;
      tk.push(x); ov[x] = VTICKS[k].toLocaleString();
    }
    ax_vol.ticker.ticks = tk;
    ax_vol.major_label_overrides = ov;
  }
}
showBar(barAt(xr.end), xr.start, xr.end);
"""
        args = dict(roll=roll, src=vp_src, VMAX=vmax, FRAC=profile_frac,
                    N_BINS=int(prof.shape[1]),
                    BAR=bar_color, POC=poc_color, range=fig.x_range,
                    ax_vol=ax_vol, VTICKS=vticks,
                    # ★ 逐根档区（与指标端同一规则）: LO/HI 为每根 K 线的
                    #   [min(low), max(high)] 数组, 供 JS 按锚点重算 y/height。
                    LO=(None if lo_arr is None else np.asarray(lo_arr, dtype=np.float64)),
                    HI=(None if hi_arr is None else np.asarray(hi_arr, dtype=np.float64)),
                    YMODE=y_mode)
        cb_init = _VPCJS(args=args, code=code)
        cb_move = _VPCJS(args=args, code=code + "showBar(barAt(cb_obj.x), xr.start, xr.end);")
        cb_leave = _VPCJS(args=args, code=code + "showBar(barAt(xr.end), xr.start, xr.end);")
        cb_rng = _VPCJS(args=args, code=code + "showBar(barAt(cb_obj.x1), cb_obj.x0, cb_obj.x1);")
        fig.js_on_event(MouseMove, cb_move)
        fig.js_on_event(MouseLeave, cb_leave)
        fig.js_on_event(RangesUpdate, cb_rng)          # ★ 拖动/缩放主通道
        for _e in ("mousemove", "mouseleave", "rangesupdate"):
            fig.subscribed_events.add(_e)              # ★ 不订阅前端就不发事件
        for _e in ("start", "end"):                    # 兜底
            fig.x_range.js_on_change(_e, cb_init)
    except Exception as _e:
        import sys as _s
        print(f"[volume_profile] 交互挂载失败: {type(_e).__name__}: {_e}", file=_s.stderr)

def _add_volume_profile(fig, cds, indicator, df, ic,
                        black_color=None, profile_frac: float = 0.30,
                        y_mode: str = 'price', bar_color=None, poc_color=None,
                        vidx=None, price_bars=None, y_range_name=None,
                        show_vol_axis: bool = False,
                        y_tick_labels: bool = True,
                        # ---- `VPStyle`（`category='vp'` 专用，见 utils.VPStyle）----
                        fill_alpha: float = 0.6,
                        bin_ratio: float = 0.88,
                        line_color: str | None = 'white',
                        line_width: float = 0.4,
                        highlight_poc: bool = True):
    """在价格图上叠加**滚动 Volume Profile**（右端=0刻度、向左生长、贴右缘）。

    数据约定（与 `RollingVolumeProfile` 指标端一致）
    -------------------------------------------------
    - `indicator` 形状 `(K线根数, 档数)`；一维时当作"只有 1 档"；
    - **列 = 等宽价格档**，档位区间为 `[min(low), max(high)]`（**同一段 df**）；
    - 第 i 行第 j 列 = 该时刻窗口内落在第 j 档的成交量之和。

    本函数只画**最后一行**（最新时刻）的分布 —— 即 `ANCHOR_MODE="right_edge"`：
      锚点(柱右端/0刻度) = 最后一根 K 线的 index
      满量程柱长        = 数据宽度 × profile_frac
      柱长 = 该档成交量 / 全矩阵最大成交量 × 满量程柱长   （向左生长）
    与 test8 一样: x 用的是**主轴 index 坐标**，与 K 线共用同一套 transform，
    因此不存在"两套坐标系换算"导致的错位。
    """
    prof = np.asarray(indicator, dtype=np.float64)
    if prof.ndim == 1:
        prof = prof.reshape(-1, 1)          # 一维 -> (n, 1)
    if prof.ndim != 2 or prof.shape[0] == 0:
        return None
    n, nb = prof.shape

    # ---- 价格档（与指标端**同一条规则**）----
    #   指标端 RollingVolumeProfile.next() 默认只用**最后 price_bars 根**的
    #   [min(low), max(high)] 定档位（档位数固定，若覆盖整段数据则只有极少数档
    #   落在滚动窗口内 -> 图上只剩 2~3 根柱子）。这里必须用同一条规则，
    #   否则横条的价格位置会与指标算出的分布对不上。
    try:
        _pb = None if price_bars is None else int(price_bars)
    except Exception:
        _pb = None
    if _pb is not None and 0 < _pb < len(df):
        lo = float(np.nanmin(df['low'].values[-_pb:]))
        hi = float(np.nanmax(df['high'].values[-_pb:]))
    else:
        lo = float(np.nanmin(df['low'].values))
        hi = float(np.nanmax(df['high'].values))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None
    edges = np.linspace(lo, hi, nb + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    bin_h = (hi - lo) / nb

    # ---- [新增] 每根 K 线**自己**的档区（滚动, 与指标端 next() 完全同一条规则）----
    #   为什么需要: JS 交互时锚点会落到**任意一根** K 线上, 柱子的 y（价格档中心）
    #   与 height（档高）必须用**那一根**的档区来算 —— 否则鼠标移到早期 K 线时,
    #   分布会被放在错误的价位/用错的档高。
    #   注意 i = 最后一根时该区间恰等于上面用 price_bars 算出的 [lo, hi] ->
    #   初始绘制（= 最后一根的分布）与这里完全一致。
    _lows = np.asarray(df['low'].values, dtype=np.float64)
    _highs = np.asarray(df['high'].values, dtype=np.float64)
    if _pb is not None and 0 < _pb < len(_lows):
        _lo_arr = np.empty(len(_lows), dtype=np.float64)
        _hi_arr = np.empty(len(_lows), dtype=np.float64)
        for _i in range(len(_lows)):
            _a = _i - _pb + 1
            if _a < 0:
                _a = 0
            _lo_arr[_i] = np.nanmin(_lows[_a:_i + 1])
            _hi_arr[_i] = np.nanmax(_highs[_a:_i + 1])
    else:
        _lo_arr = np.full(len(_lows), lo)
        _hi_arr = np.full(len(_lows), hi)

    # y 坐标: 主图用**价格档中心**; 副图(overlap=False)不是价格轴, 改用**档位序号**
    #   -> 画成"分层直方图", 每档一根横条(0 刻度仍在右端)
    if y_mode == 'index':
        y_vals = np.arange(nb, dtype=np.float64)
        bar_h = float(bin_ratio)
    else:
        y_vals = centers
        bar_h = bin_h * float(bin_ratio)

    row = prof[-1]                                    # 最新一根的分布
    vmax = float(np.nanmax(prof)) if np.isfinite(prof).any() else 0.0
    vmax = vmax if vmax > 0 else 1.0                  # 固定量程(跨时刻可比)

    # ---- x: 主轴 index 坐标 ----
    x = np.arange(n, dtype=np.float64)
    anchor = float(x[-1])                             # 锚点 = 右端 = 0 刻度
    band = (float(x[-1]) - float(x[0])) * float(profile_frac)   # 满量程柱长

    left = anchor - np.nan_to_num(row, nan=0.0) / vmax * band
    right = np.full(nb, anchor)
    row_max = np.nanmax(row) if np.isfinite(row).any() else 0.0
    _bar = bar_color or '#3498db'
    _poc = poc_color or '#f39c12'
    if not highlight_poc:
        # 不高亮 POC：整轴一个颜色（`highlight_poc=False`）
        _poc = _bar
    colors = [_poc if v >= row_max else _bar for v in row]

    # ★ [新增] 副图(overlap=False)专用: 给 VP 一个**自己的 y range**。
    #   为什么需要: 副图里往往还有普通指标线(如 RSI, 值域 0~100), 而 VP 的 y 是
    #   **档位序号**(0..nb-1)。两者共用同一个自动纵轴时: 柱子高度 0.88 相对 100
    #   只有 0.9%(细如发丝), 而且框架的 autoscale 回调会把副图纵轴设成指标线的
    #   极值(indicators_h/l 只统计 isplot=True 的列) -> 柱子直接落到可视范围之外
    #   -> 用户看到的是“只有指标线, 右端根本没有 VP 图”。
    #   修法: 档位数在 Python 侧就是已知的, 所以直接给一根**固定 Range1d**
    #   (-0.5 .. nb-0.5) 挂到 extra_y_ranges, hbar 用 y_range_name 指过去 ——
    #   不需要任何 JS 镜像（之前“A 方案”失败是因为要去镜像主图的 DataRange1d;
    #   这里是自建的固定 range，性质完全不同）。autoscale 只改 fig.y_range。
    _yrn = None
    if y_range_name:
        try:
            from bokeh.models import Range1d as _VPRange
            fig.extra_y_ranges[y_range_name] = _VPRange(
                start=-0.5, end=max(nb - 0.5, 0.5))
            _yrn = y_range_name
        except Exception:
            _yrn = None

    # ★ 分布图用**自己的** ColumnDataSource(与 test8.py 一致):
    #   档数(40)与 K 线根数(几千)本来就不同, 塞进同一个源会造成列长不一致
    #   -> bokeh 截断整张图 / 柱子错位 / 出现垂直线。独立源彻底避免。
    from bokeh.models import ColumnDataSource
    # ★ height 也做成**列**（不再是标量）: 档区逐根滚动后, 档高 = (hi_i-lo_i)/nb
    #   会随锚点变, 必须能由 JS 改写。
    _vp_src = ColumnDataSource(dict(y=y_vals, left=left, right=right,
                                    height=np.full(nb, bar_h, dtype=np.float64),
                                    color=colors))
    if y_mode == 'index' and y_tick_labels:
        # 副图的 y 是档位序号 -> 刻度标上该档的价格中心, 便于读数。
        # ★ 是否标刻度由调用处决定(y_tick_labels):
        #   - 副图里**只有 VP**(没有指标线) -> True: 标价格中心, 读者能对上价位;
        #   - 副图里还有指标线(如 RSI) -> False: 不能改 fig.yaxis, 否则会把
        #     指标线自己的纵轴刻度覆盖掉。
        try:
            from bokeh.models import FixedTicker
            _st = max(1, nb // 8)
            _bt = list(range(0, nb, _st))
            fig.yaxis.ticker = FixedTicker(ticks=[float(b) for b in _bt])
            fig.yaxis.major_label_overrides = {
                float(b): f"{centers[b]:.1f}" for b in _bt}
            fig.yaxis.axis_label = "价格档"
        except Exception:
            ...
    # [改回] 原「A 方案」在此为 VP 建独立 y range(extra_y_ranges['vp_y'])
    #   并用 JS 镜像主 y 轴。实测: DataRange1d 的 start/end 在**模型层恒为
    #   null**(真值只在 view 里算), 镜像回调的条件永不成立 -> vp_y 永远停在
    #   整段价格范围 [lo, hi], VP 柱子的价格坐标与 K 线对不上 => 主图上出现
    #   大量"垂直线"。而 VP 的 y 值本来就落在 [min(low), max(high)] 内
    #   (= K 线自身范围), 根本不会撑宽主 Y 轴, 故该方案整体多余, 直接去掉:
    # ★ [更正] 上面"DataRange1d 的 start/end 在**模型层恒为 null**"的说法**不准确**:
    #   bokehjs 的 DataRange1d.update() 确实会在模型层写入 start/end(模型初值 null
    #   只是"还没 update"), 所以"镜像回调条件永不成立"这个理由不成立; 放弃该方案的
    #   真正原因就是后半句 —— VP 的 y 本就 ⊆ K 线范围, 独立 y range 多余。
    #   而 update() 会用**全部数据**覆盖 start/end, 这正是 Y 轴缩放"先跟随可见 K 线、
    #   随即弹回全段范围"的根因, 已在 autoscale_cb.js 里用 have_updated_interactively 修掉。
    #   from bokeh.models import Range1d as _VPRange, CustomJS as _VPMir
    #   _vp_y_rng = _VPRange(start=float(centers[0]), end=float(centers[-1]))
    #   fig.extra_y_ranges['vp_y'] = _vp_y_rng
    #   _mirror = _VPMir(args=dict(a=fig.y_range, b=_vp_y_rng), code="...")
    #   fig.y_range.js_on_change('start', _mirror)
    #   fig.y_range.js_on_change('end', _mirror)
    # ⚠ y_range_name 传 None 会被 bokeh 校验拒绝
    #   (ValueError: expected a value of type str, got None) -> 所以 None 时
    #   **不要**把这个 kwarg 传给 hbar, 否则 overlay 的 VP 会被 try/except 静默跳过。
    _hbar_kw = {} if _yrn is None else {'y_range_name': _yrn}
    r = fig.hbar(y='y', left='left', right='right', height='height',
                 **_hbar_kw,
                 source=_vp_src, fill_color='color',
                 line_color=(line_color or 'white'),
                 line_width=float(line_width or 0.0),
                 line_alpha=(1.0 if line_color else 0.0),
                 alpha=float(fill_alpha if fill_alpha is not None else 0.6))
    # ---- 成交量刻度轴: 位置 = 锚点 - v/vmax*band, 标签仍是 v ----
    _ax, _vt = None, None          # 下面 try 里创建; 失败也要能交互
    # ★ [开关 show_vol_axis] False = **不画**这根“成交量刻度轴”（挂在图顶、
    #   标签“成交量（右→左 递增）”、刻度数字出现在图的右上角）。
    #   为什么要开关：副图(overlap=False)里 VP 是**背景分布**，顶部再挂一根
    #   成交量轴又挤又容易和副图自己的纵轴刻度打架，所以副图默认关掉
    #   （调用处传 show_vol_axis=is_overlay）。价格主图的 VP 仍然保留。
    #   需要恢复：传 show_vol_axis=True 即可 —— 原逻辑一字未删，只是被 if 包住。
    if show_vol_axis:
        try:
            from bokeh.models import LinearAxis, FixedTicker
            _vt = [0.0, vmax * .25, vmax * .5, vmax * .75, vmax]
            _tk = [anchor - _v / vmax * band for _v in _vt]
            _ax = LinearAxis(axis_label="成交量（右→左 递增）",
                             axis_line_color=None, major_tick_line_color=None,
                             minor_tick_line_color=None,
                             major_label_text_font_size="9pt")
            _ax.ticker = FixedTicker(ticks=_tk)
            _ax.major_label_overrides = {float(_t): f"{_v:,.0f}"
                                         for _t, _v in zip(_tk, _vt)}
            fig.add_layout(_ax, "above")
        except Exception:
            ...
    # 让分布柱垫在 K 线下面（渲染顺序 = 加入顺序, 这里重排一下）
    try:
        from bokeh.models.glyphs import HBar
        fig.renderers = sorted(fig.renderers, key=lambda x_: not isinstance(
            x_.glyph, (HBar, VBar)))
    except Exception:
        ...
    _vp_interactive(fig, _vp_src, indicator, vidx, vmax, band, anchor,
                    _bar, _poc, ax_vol=_ax, vticks=_vt,
                    profile_frac=profile_frac,
                    lo_arr=_lo_arr, hi_arr=_hi_arr, y_mode=y_mode)
    try:
        r.tags = ['__vp_excl__']
    except Exception:
        ...
    return r


def reveal_sources(live_sources: dict, current: int) -> None:
    """## 回放：把所有登记的数据源截到 ``index <= current``

    因果指标在回测时已全量算好，回放只需按当前 bar 索引“揭开”已发生的部分，
    因此不重算、不 stream，直接切片赋值（O(N)，几千根很快）。
    """
    full = live_sources.get('full') or {}
    for cds in live_sources.get('cds', []):
        data = full.get(cds.id)
        if not data:
            continue
        idx = data.get('index')
        if idx is None:
            continue
        n = int(np.searchsorted(np.asarray(idx), current, side='right'))
        cds.data = {k: v[:n] for k, v in data.items()}


def renderer_y_extent(fig):
    """从图里各 renderer 的 ``glyph.y/top/upper/base`` 数据源算 y 极值。"""
    lo = hi = None
    for r in getattr(fig, 'renderers', []):
        g = getattr(r, 'glyph', None)
        if g is None:
            continue
        data = getattr(getattr(r, 'data_source', None), 'data', None)
        if not isinstance(data, dict):
            continue
        if 'vol_ov_ratio' in data:
            # 成交量 overlay 的柱子粘在图底部（由视口定位），不参与 y 轴范围；
            # 否则它会把主图 y 轴撑到整段价格区间（6500~7000）而不贴合可见窗口。
            continue
        # y0/y1: segment(上下影线 high/low)、VBar 的 bottom/top；
        # upper/base: Band/其他区间图。全部纳入，避免影线被裁。
        for attr in ('y', 'y0', 'y1', 'top', 'bottom', 'upper', 'base'):
            v = getattr(g, attr, None)
            key = v if isinstance(v, str) else (
                v.get('field') if isinstance(v, dict) else None)
            if not key:
                continue
            arr = data.get(key)
            if arr is None:
                continue
            try:
                a = np.asarray(arr, dtype=float)
            except Exception:
                continue
            a = a[np.isfinite(a)]
            if not a.size:
                continue
            lo = float(a.min()) if lo is None else min(lo, float(a.min()))
            hi = float(a.max()) if hi is None else max(hi, float(a.max()))
    return lo, hi


def y_range_padding(lo, hi, frac_bottom: float = 0.0005,
                    frac_top: float = 0.0005, frac_span: float = 0.06):
    """返回 y 轴可视范围的 ``(pad_lo, pad_hi)``。

    * 主项是**按跨度**的 ``frac_span``；
    * ``frac_bottom``/``frac_top`` 是“按价位”的**极小下限**（跨品种价位用，
      如 0.05%）；
    * 上限 ``span*0.5``，避免余量把 K 线挤扁。

    主图默认 ``frac_span=0.06``（底部与顶部略留余量给下方的信号 marker/文本）；
    指标/成交量等图传 ``frac_bottom=frac_top=0.0, frac_span=0.05``，保持原有行为。
    """
    span = float(hi) - float(lo)
    base = max(abs(float(lo)), abs(float(hi))) or 1.0
    cap = span * 0.5
    pad_lo = min(max(span * float(frac_span), base * float(frac_bottom)), cap)
    pad_hi = min(max(span * float(frac_span), base * float(frac_top)), cap)
    return pad_lo, pad_hi


def freeze_subfigure_ranges(tabs, live_sources: dict, refs=None) -> None:
    """把**所有**图的 y_range 换成固定 ``Range1d``（含主 K 线图）。

    图默认是 ``DataRange1d``，每揭开一帧就重拟合一次 -> 抖。统一锁成固定范围
    后，由 ``bokeh_replay`` 每 N 帧整体刷新一次（主图/副图节奏一致）。

    ``refs`` 可传入 ``plot()`` 已经算好的模型列表（避免重复遍历模型树）。
    """
    from bokeh.models.plots import Plot
    subs: list = []
    for p in (refs if refs is not None else tabs.references()):
        if not isinstance(p, Plot):
            continue
        # 权益曲线**单独**处理（见上方建图时的 ``Range1d`` 与
        # ``bokeh_replay._refresh_equity_ranges``）：这里跳过，不动它的 y 轴。
        if 'value-chart' in (p.css_classes or []):
            continue
        lo, hi = renderer_y_extent(p)
        if lo is None or not (hi > lo):
            continue
        # 只有主 K 线图用「按价格比例」的余量（给信号标记/文本留位置）；
        # 指标/权益/成交量按跨度留即可——否则权益(值 ~1e6)会被 ±1% 压成直线。
        _is_candle = 'candle-chart' in (p.css_classes or [])
        pad_lo, pad_hi = (y_range_padding(lo, hi) if _is_candle
                          else y_range_padding(lo, hi, frac_bottom=0.0,
                                               frac_top=0.0, frac_span=0.05))
        # ★ 必须是**原地改** ``start/end``，不能整只换新 ``Range1d``：
        #   成交量 overlay 的 ``CustomJS`` 在 args 里持有 ``fig_ohlc.y_range`` 这个
        #   对象，换了新对象后 JS 仍指向旧对象 -> 成交量柱不会跟着新范围走
        #   （表现为成交量消失或悬空）。
        try:
            p.y_range.start = lo - pad_lo
            p.y_range.end = hi + pad_hi
        except Exception:
            try:
                p.y_range = Range1d(lo - pad_lo, hi + pad_hi)
            except Exception:
                continue
        subs.append(p)
    live_sources['sub_figs'] = subs


#: ``plot()`` 里 ``id`` 被循环变量遮蔽（``for id, df in enumerate(...)``），
#: 这里存一份内置 ``id`` 备用
_builtin_id = id


def _ordered_models(root) -> list:
    """按模型树的**确定顺序**列出所有 bokeh 模型。

    ``Model.references()`` / ``select()`` 返回的是 **set**，迭代顺序不稳定 ——
    两次完全相同的构建，``ColumnDataSource`` 的顺序都可能不同。实时刷新要靠
    顺序把新算的数据写到已挂载的 CDS 上（见 :func:`sync_sources`），所以这里
    自己按属性声明顺序做一次确定遍历。
    """
    from bokeh.models import Model
    out: list = []
    seen: set = set()

    def walk(value):
        if isinstance(value, Model):
            if id(value) in seen:
                return
            seen.add(id(value))
            out.append(value)
            # ``properties_with_refs()`` 只返回「可能持有其它模型引用」的属性，
            # 遍历时跳过了大量标量属性（title / width / …），比 `properties()`
            # 快 ~4x 且能达同样多的模型（实测模型数一致）。
            try:
                names = list(value.properties_with_refs())
            except Exception:
                try:
                    names = list(value.properties())
                except Exception:
                    return
            for name in names:
                try:
                    walk(getattr(value, name))
                except Exception:
                    continue
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key in value:
                walk(value[key])

    with warnings.catch_warnings():
        # 读属性会触发 bokeh 的 DeprecatedAlias 警告（如 InspectTool.toggleable
        # 已改成 visible，一读就警告）；这里只是在遍历模型树，静音掉。
        warnings.simplefilter('ignore')
        walk(root)
    return out


def sync_sources(dst_reg: dict, src_reg: dict) -> int:
    """把 ``src_reg`` 的全量数据同步到 ``dst_reg`` 已存在的 CDS 上（实时图表用）。

    两个 registry 由**同一次** :func:`plot` 构建流程产生，因此 ``cds`` 的顺序与
    列结构一致；这里按顺序把 ``src_reg['full']`` 的数组直接赋给 ``dst_reg`` 的
    CDS（``.data`` 整体替换，bokeh 一次性推送）。图表模型（figures / x_range）
    保持 ``dst_reg`` 自己的不变。

    返回同步的 CDS 数量；两个 registry 的 CDS 数量不一致时返回 ``-1``（调用方
    自行决定是否重建整个文档）。
    """
    dst = dst_reg.get('cds') or []
    src = src_reg.get('cds') or []
    if len(dst) != len(src):
        return -1
    src_full = src_reg.get('full') or {}
    mismatched = 0
    for d, s in zip(dst, src):
        data = src_full.get(s.id)
        if not data:
            continue
        # 列结构不一致 = 配错源了（CDS 顺序变了）：宁可不同步，也不能把 K 线
        # 写成别的源的数据 —— 那会让所有指标/信号一起消失（成交量源因为不
        # 参与同步而“幸存”，很容易误判成“只有成交量还在”）。
        if set(d.data) != set(data):
            mismatched += 1
            continue
        d.data = dict(data)
    if mismatched:
        print(f'[bokeh_plot] sync_sources: {mismatched} 个 CDS 列结构不一致，'
              '已跳过（CDS 顺序可能发生了变化）')
    dst_reg['full'] = {d.id: (src_full.get(s.id) or {})
                       for d, s in zip(dst, src)}
    return len(dst)


def _trade_str(value, default: str = '') -> str:
    """minibt 的颜色/标记（普通 str 或 Colors/Markers 枚举）-> 普通字符串。"""
    text = str(getattr(value, 'value', value) or '').strip()
    return text or default


def _trade_dash(value) -> str:
    """minibt `LineDash` -> bokeh 认的线型名。"""
    text = str(getattr(value, 'value', value) or '').strip().lower()
    if text in ('solid', 'dashed', 'dotted', 'dotdash', 'dashdot'):
        return text
    if text == 'sparse_dotted':
        return 'dotted'
    return 'dashed'                 # large_dashed / vbar / 未知 -> 普通虚线


def plot(strategys: list[Strategy],
         trade_signal: bool = True,
         black_style: bool | None = None,
         open_browser: bool = False,
         plot_width: int = None,
         plot_cwd: str = "",
         plot_name: str = "",
         save_plot: bool = False,
         volume_mode: str = 'sub',
         theme: str | None = None,
         live_sources: dict | None = None,
         build_only: bool = False,
         reveal_length: int | None = None,
         with_equity: bool = True,
         server_volume: bool = False) -> Tabs:
    """
    Like much of GUI code everywhere, this is a mess.

    ``theme``：``'dark'``（默认）/ ``'light'``，与 pylightcharts 图表同名同义；
    未显式给 ``theme``/``black_style`` 时取 ``strategys[0].theme``（即
    ``config.theme`` / ``self.theme``）。旧的 ``black_style`` 仍兼容。

    回放支持（``bokeh_replay.py`` 用）：

    - ``live_sources``：传入一个 dict，函数会把构建好的所有可更新
      ``ColumnDataSource``（列表 ``cds``）、它们的全量数据 ``full``，以及合约/
      策略名等元信息写入其中，供服务端逐 bar "揭开" 显示。
    - ``build_only=True``：只构建并返回 ``Tabs``，不做 HTML 导出/打开浏览器。
    - ``reveal_length``：若给定，构建后立即把所有带 ``index`` 列的 CDS 截到
      ``index <= reveal_length-1``（回放初始只显示前 N 根）。
    """
    from bokeh.io import reset_output
    reset_output()
    global IS_JUPYTER_NOTEBOOK, _factors_plot

    from ..utils import resolve_theme, resolve_toolbox, resolve_fullscreen
    black_style = resolve_theme(
        theme, black_style, strategys[0] if strategys else None) == 'dark'
    black_color = "white" if black_style else "black"
    white_color = "black" if black_style else "white"
    # 全屏铺满（主图:副图 = 3:1，不出纵向滚动条）：config.bokeh_fullscreen
    # 为 None 时：Jupyter → False，浏览器 → True
    fullscreen = resolve_fullscreen(
        strategys[0] if strategys else None,
        jupyter=bool(IS_JUPYTER_NOTEBOOK) and not open_browser)
    # 工具栏开关与 pylightcharts 画线工具统一用 toolbox（config.toolbox / self.toolbox）
    show_toolbar = resolve_toolbox(strategys[0]) if strategys else False
    # 网格开关（bokeh 与 pylightcharts 共用 config.grid）
    show_grid = bool(getattr(getattr(strategys[0], 'config', None),
                             'grid', False)) if strategys else False
    _layout_sizing = 'stretch_both' if fullscreen else 'scale_width'
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

    # 实时模式没有回测结果（``_results`` 为空）→ 按合约补齐成“空但带列”的结果
    # DataFrame，否则 ``value_source`` / ``*_segment_source`` 不会按合约各建一份，
    # 后面按 ``[ik][id]`` 取就会 IndexError（实时图表只能落地）。
    for _i, _t in enumerate(strategys):
        _n = len(datas[_i]) if _i < len(datas) else 0
        _rs = list(results[_i]) if _i < len(results) else []
        if len(_rs) >= _n:
            continue
        try:
            _idx = list(datas[_i][0].pandas_object.index)
        except Exception:
            _idx = []
        for _ in range(_n - len(_rs)):
            _rs.append(pd.DataFrame(dict(
                positions=np.zeros(len(_idx), dtype=int),
                sizes=np.zeros(len(_idx), dtype=float),
                cum_profits=np.zeros(len(_idx), dtype=float),
                total_profit=np.zeros(len(_idx), dtype=float),
            ), index=_idx))
        results[_i] = _rs

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
            # 实盘「无限历史」：K 线只给 preload 根，而指标仍是全量 -> 先对齐
            indicator_datas[ik][id] = _tail_align_indicators(
                indicator_datas[ik][id], current_length)
            pad = (index[-1] - index[0]) / 20
            df = df[_FILED]
            df['volume5'] = df.volume.rolling(5).mean()
            df['volume10'] = df.volume.rolling(10).mean()
            df["inc"] = (df.close >=
                         df.open).values.astype(np.uint8).astype(str)
            df["Low"] = df[['low', 'high']].min(1)
            df["High"] = df[['low', 'high']].max(1)
            # 影线拆两条（单条 high->low 会穿过实体）：实体顶->high、low->实体底
            df["wick_top"] = df[['open', 'close']].max(1)
            df["wick_bottom"] = df[['open', 'close']].min(1)
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
            # 水印（Config.watermark，默认 True -> 用合约自身名称）
            _apply_figure_watermark(
                fig_ohlc,
                _watermark_text(strategys[ik],
                                f'{symbols[ik][id]}_'
                                f'{symbol_multi_cycle[ik][id]}'),
                black_style)
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
            # 上下影线（两条：实体顶->high、实体底->low）
            fig_ohlc.segment('index', 'wick_top', 'index', 'high',
                             source=source[ik][id], color=black_color)
            fig_ohlc.segment('index', 'wick_bottom', 'index', 'low',
                             source=source[ik][id], color=black_color)
            # # 实体线
            ohlc_bars = fig_ohlc.vbar('index', BAR_WIDTH, 'open', 'close', source=source[ik][id],
                                      line_color=black_color, fill_color=inc_cmap)

            # [新增] 成交量**固定叠加到主 K 线图**底部(与 btplot.py 同款);
            #   副 Y 轴量程 = 最大量/VOL_FRAC -> 最高柱恰好占底部 VOL_FRAC, 不受价格缩放影响。
            # ⚠ 必须给主 Y 轴限定拟合范围, 否则成交量量级会把价格轴撑坏。
            # [开关] 由 Config.volume_mode 决定（'overlay' / 'both' 时开启）
            #   原代码: SHOW_VOL_OVERLAY = False   # [改回] 主图成交量叠加已停用
            SHOW_VOL_OVERLAY = volume_mode in ('overlay', 'both')
            if SHOW_VOL_OVERLAY and 'volume' in source[ik][id].column_names:
                _vol = np.nan_to_num(np.asarray(source[ik][id].data['volume'], dtype=np.float64))
                if _vol.size and _vol.max() > 0:
                    # ★ 成交量用**自己的 CDS**:
                    #   JS 刷新它时不会触碰主数据源 -> 价格 y 轴不会重新拟合 ->
                    #   既没有反馈死循环(成交量不停下移), 也**不需要干预 y_range.renderers**
                    #   (之前限制了拟合 -> 价格轴范围失真)。
                    from bokeh.models import (ColumnDataSource as _VPCDS,
                                      CustomJS as _VPCJS)
                    _F = 0.20
                    # 成交量分布极偏态（中位数≈最大值的 1.7%）：直接线性归一化会让
                    # 绝大多数柱矮到看不见；但**削平**又会把大量柱裁成一条直线。
                    # 所以改用**幂缩放**（p<1）：不截断、最高柱仍占主图 20%，
                    # 同时把小成交量抬高到看得见。
                    _vmax = float(_vol.max()) or 1.0
                    _vratio = np.power(np.clip(_vol / _vmax, 0.0, 1.0), 0.30)
                    # ★ 初始基线 = **视口下沿**（优先取当前价格 y_range，它已含 padding），
                    #   取不到就退化为整段价格的 [min,max] 再补 5% padding。
                    #   关键: 柱体因此**始终落在可视价格范围内** -> DataRange1d 自动拟合
                    #   出来的范围等于 K 线自身范围, 成交量**不会把价格轴撑宽**
                    #   （与 VP 同理: 值域 ⊆ 价格值域）。
                    try:
                        _y0 = float(fig_ohlc.y_range.start)
                        _y1 = float(fig_ohlc.y_range.end)
                    except Exception:
                        _y0 = _y1 = float('nan')
                    if not (_y1 > _y0):
                        _ylo = float(np.nanmin(df['low'].values))
                        _yhi = float(np.nanmax(df['high'].values))
                        _pad = (_yhi - _ylo) * 0.05
                        _y0, _y1 = _ylo - _pad, _yhi + _pad
                    _span = (_y1 - _y0) or 1.0
                    _vol_cds = _VPCDS(dict(
                        index=np.arange(len(_vol), dtype=np.float64),
                        inc=list(source[ik][id].data.get('inc', ['1'] * len(_vol))),
                        vol_ov_bottom=np.full(len(_vol), _y0),      # 视口下沿
                        vol_ov_top=_y0 + _vratio * _span * _F,  # 之后由 JS 按视口刷新
                        vol_ov_ratio=_vratio))
                    _vol_r = fig_ohlc.vbar('index', BAR_WIDTH, 'vol_ov_bottom', 'vol_ov_top',
                                  source=_vol_cds,
                                  fill_color=factor_cmap('inc', COLORS, ['0', '1']),
                                  line_alpha=0, alpha=0.55)
                    try:
                        _vol_r.tags = ['__vp_excl__']
                        # ★ 立刻限定价格 y 轴的拟合对象: 此刻 renderers 里只有 K 线,
                        #   把成交量 overlay 排除(它的 y 基线是整段数据最低点, 会钉死 Y 轴)。
                        try:
                            _keep_r = [r for r in fig_ohlc.renderers if r is not _vol_r]
                            if _keep_r:
                                fig_ohlc.y_range.renderers = _keep_r
                        except Exception as _e:
                            import sys as _s
                            print('[y_range.renderers] 失败:', repr(_e), file=_s.stderr)
                    except Exception:
                        ...
                    # bottom_edge: 可见 Y 范围一变就重算基线 -> 始终贴在下沿
                    # [说明] 此处原本是 `from bokeh.models import CustomJS`。
                    #   函数内出现裸 `CustomJS`/`ColumnDataSource` 导入会把它**绑成局部名**,
                    #   使同函数内**早于它**使用的同名调用(框架自身的 CustomJS)抛
                    #   UnboundLocalError。两者模块顶部已导入(btplot.py 第 17/18 行),
                    #   故此处不再重复导入; 需要别名时用 `Xxx as _Alias` 形式。
                    _vol_js = _VPCJS(args=dict(
                        src=_vol_cds, F=_F, xr=fig_ohlc.x_range,
                        yr=fig_ohlc.y_range,          # ★ 视口下沿 = y_range.start
                        hiA=np.asarray(df['high'].values, dtype=np.float64),
                        loA=np.asarray(df['low'].values, dtype=np.float64)),
                        code="""
// bottom_edge: 成交量柱贴**视口最下端**(TradingView Volume overlay 效果)。
//   基线 base = 价格 y_range.start（= 当前可见范围最底部, 已含 padding）-> 视觉紧贴底边;
//   高度 = 归一化量 × 可见跨度 span × F。
//   ★ 柱子始终落在**可视价格范围内** -> DataRange1d 自动拟合出的范围等于 K 线自身范围,
//     成交量**不会影响 K 线图的可见范围**(与 VP 同理: 值域 ⊆ 价格值域)。
//   y_range 还没就绪时退化为"可见 x 窗口内 K 线的 low/high"。
const r = src.data['vol_ov_ratio'];
if (!r) return;
const a = Math.max(0, Math.round(xr.start));
const b = Math.min(r.length - 1, Math.round(xr.end));
let base = null, span = null;
if (yr != null && yr.start != null && yr.end != null && yr.end > yr.start) {
    base = yr.start;
    span = yr.end - yr.start;
} else if (a <= b) {
    let lo = Infinity, hi = -Infinity;
    for (let i = a; i <= b; i++) {
        if (loA[i] < lo) lo = loA[i];
        if (hiA[i] > hi) hi = hiA[i];
    }
    if (isFinite(lo) && isFinite(hi)) {
        base = lo;
        span = (hi - lo) || 1;
    }
}
if (base == null) return;
const bot = [], top = [];
for (let i = 0; i < r.length; i++) {
    bot.push(base);
    top.push(base + r[i] * span * F);
}
src.data['vol_ov_bottom'] = bot;
src.data['vol_ov_top'] = top;
src.change.emit();
""")
                    if not server_volume:
                        fig_ohlc.x_range.js_on_change('start', _vol_js)
                        fig_ohlc.x_range.js_on_change('end', _vol_js)
                    # [改] 归一化量已经在**独立 CDS** `_vol_cds` 里, 这里**不再**往主数据源
                    #   加列 —— 否则 JS 每次刷新都会改动主数据源, 触发价格轴重新拟合(反馈死循环)。
                    #   原代码: source[ik][id].add(_vol / _vmax, "vol_ov_ratio")
                    try:
                        # ★ 拖动/缩放时重算基线: 成交量**始终贴在可见 Y 范围底部**。
                        #   cb_obj 就是价格 y_range; src 必须指向**独立 CDS**。
                        _vol_js = _VPCJS(args=dict(src=_vol_cds, F=_F), code="""
const r = src.data['vol_ov_ratio'];
const lo = cb_obj.start, hi = cb_obj.end;
if (lo == null || hi == null || !r) return;
const sp = (hi - lo) || 1;
const bot = [], top = [];
for (let i = 0; i < r.length; i++) { bot.push(lo); top.push(lo + r[i] * sp * F); }
src.data['vol_ov_bottom'] = bot;
src.data['vol_ov_top'] = top;
src.change.emit();
""")
                        if not server_volume:
                            fig_ohlc.y_range.js_on_change('start', _vol_js)
                            fig_ohlc.y_range.js_on_change('end', _vol_js)
                    except Exception:
                        ...
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
                for ind_index, (isplot, name, lines, _lines, ind_name, is_overlay, category, indicator, doubles, plotinfo, span, _signal, bandstyle) in enumerate(indicators_data):
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
                                        _nan_reduce_axis1(indicator[ids][:, np.arange(len(isplot[ids]))[isplot[ids]]], 'max') if _mulit_ind else indicator[ids], f"{legend_label}_h")
                                    source[ik][id].add(
                                        _nan_reduce_axis1(indicator[ids][:, np.arange(len(isplot[ids]))[isplot[ids]]], 'min') if _mulit_ind else indicator[ids], f"{legend_label}_l")
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

                                                # [新增] vbar_base 是本框架为柱体线新增的字段(不是 bokeh Line/VBar 的属性),
                                                #   必须像 price_line/price_label 一样在传给 bokeh 前剔除, 否则
                                                #   fig.line(**lineinfo) 会因未知属性报 AttributeError。柱体分支
                                                #   改为直接从 LineStyle 对象读取该值。
                                                _lineinfo.pop("vbar_base", None)
                                                # 同理：vbar_up_color /
                                                #   vbar_down_color 也不是 bokeh 属性
                                                _lineinfo.pop("vbar_up_color", None)
                                                _lineinfo.pop("vbar_down_color", None)
                                                _lineinfo.pop("vbar_color_base", None)
                                            if is_overlay[ids]:
                                                fig.line(
                                                    'index', source_name, source=source[ik][id],
                                                    legend_label=source_name, **_lineinfo)
                                            else:
                                                if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                                    if_vbar = True
                                                    # [修改] 同 btplot.py: 柱底由硬编码 0 改为逐线
                                                    #   vbar_base(默认 None -> 0.0, 保持旧行为),
                                                    #   上下行配色改按"数值 vs 基线"判定。
                                                    # 此处 _lineinfo 已被清洗掉 vbar_base,
                                                    # 故直接从该线的 LineStyle 对象读取。
                                                    _vbase = lineinfo[_lines_name].get('vbar_base', None)
                                                    _vbase = 0.0 if _vbase is None else float(_vbase)
                                                    _base_col = f"{_lines_name}_base"
                                                    if _base_col not in source[ik][id].column_names:
                                                        source[ik][id].add(
                                                            [_vbase, ] * len(ind), _base_col)
                                                    _line_inc = np.where(ind > _vbase, 1, 0).astype(
                                                        np.uint8).astype(str).tolist()
                                                    source[ik][id].add(
                                                        _line_inc, f"{_lines_name}_inc")
                                                    # if "line_color" in lineinfo[_lines_name]:
                                                    # `LineStyle.vbar_up_color` /
                                                    #   `vbar_down_color` 优先；其次 line_color；
                                                    #   都没有则用默认涨/跌调色板
                                                    _up = lineinfo[_lines_name].get(
                                                        "vbar_up_color", None)
                                                    _down = lineinfo[_lines_name].get(
                                                        "vbar_down_color", None)
                                                    if _up or _down:
                                                        _line_inc_cmap = factor_cmap(
                                                            f"{_lines_name}_inc",
                                                            [_down or COLORS[0],
                                                             _up or COLORS[1]], ['0', '1'])
                                                    else:
                                                        _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                                        if _line_inc_cmap is None:
                                                            _line_inc_cmap = factor_cmap(
                                                                f"{_lines_name}_inc", COLORS, ['0', '1'])
                                                    r = fig.vbar('index', BAR_WIDTH, _base_col, source_name, source=source[ik][id],
                                                                 line_color='black', fill_color=_line_inc_cmap)
                                                else:
                                                    r = fig.line(
                                                        'index', source_name, source=source[ik][id],
                                                        legend_label=source_name, **_lineinfo)
                                        # 两条线之间填充背景色
                                        band_lines = _lines[ids]  # 原始列名(无前缀)
                                        band_source_lines = lines[ids]  # 数据源列名(带前缀)
                                        if len(band_lines) >= 2:
                                            for band_dict in bandstyle:
                                                _bd = dict(band_dict)
                                                lower_name = _bd.get('lower', '')
                                                upper_name = _bd.get('upper', '')
                                                if lower_name in band_lines and upper_name in band_lines and all([isplot[ids][band_lines.index(lower_name)], isplot[ids][band_lines.index(upper_name)]]):
                                                    # 将原始列名替换为数据源中带前缀的列名
                                                    _bd['lower'] = band_source_lines[band_lines.index(lower_name)]
                                                    _bd['upper'] = band_source_lines[band_lines.index(upper_name)]
                                                    add_band_breaks(
                                                        fig, source[ik][id],
                                                        'index', _bd)

                                    else:
                                        if if_vbar:
                                            renderers = fig.renderers.copy()
                                            fig.renderers = list(
                                                sorted(renderers, key=lambda x: not isinstance(x.glyph, VBar)))

                                        if span:
                                            for ind_span in span:
                                                if np.isnan(ind_span["location"]) and not all(is_overlay):
                                                    ind = _span_values(indicator, isplot)
                                                    _valid_ind = ind[~np.isnan(
                                                        ind)]
                                                    mean = _valid_ind.mean() if len(_valid_ind) > 0 else np.nan
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
                                            ind = _span_values(indicator, isplot)
                                            _valid_ind = ind[~np.isnan(
                                                ind)]
                                            mean = _valid_ind.mean() if len(_valid_ind) > 0 else np.nan
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
                        # [修正] Volume Profile(category='vp')的列全是 isplot=False,
                        #   若仍用 `if any(isplot):` 则 VP 专门分支不会执行 -> VP 画不出来。
                        if any(isplot) or category == 'vp':
                            # if any(isplot):
                            is_candles = category == 'candles'
                            if is_candles and len(lines) < 4:
                                is_candles = False
                            # [新增] Volume Profile 指标(category == 'vp'):
                            #   next() 返回 (K线根数, 档数) 矩阵, 画成"横向成交量分布"
                            is_vp = (category == 'vp')
                            tooltips = []
                            colors = cycle(
                                ohlc_colors if is_overlay else colorgen())
                            legend_label = name  # 初始化命名的名称
                            if is_vp:                       # Volume Profile
                                # overlap=True -> 画在价格主图(y=价格档);
                                # overlap=False -> 另开副图(y=档位序号, 分层直方图)
                                if is_overlay:
                                    if datainfo in fig_ohlc_list[ik]:
                                        fig = fig_ohlc_list[ik].get(datainfo)
                                    else:
                                        fig = fig_ohlc
                                    _vp_y, _vp_isnew = 'price', False
                                else:
                                    fig = new_indicator_figure(
                                        new_bokeh_figure, fig_ohlc, plot_width,
                                        plotinfo.get('height', 150))
                                    _vp_y, _vp_isnew = 'index', True
                                # [新增] 按 lines 的 **'vp' 前缀**切分:
                                #   前缀之前的列 = 普通指标线(可选, 先按折线画);
                                #   前缀及之后的列 = Volume Profile 数据块。
                                try:
                                    _nm = [str(x) for x in (_lines if _lines is not None else [])]
                                    _cut = next((_i for _i, _x in enumerate(_nm)
                                                 if _x.lower().startswith('vp')), None)
                                except Exception:
                                    _nm, _cut = [], None
                                _ind2 = indicator if np.ndim(indicator) > 1 else indicator.reshape(-1, 1)
                                if _nm and _cut is not None:
                                    _lidx, _vidx = list(range(_cut)), list(range(_cut, len(_nm)))
                                else:
                                    _lidx, _vidx = [], list(range(_ind2.shape[1]))
                                for _i, _li in enumerate(_lidx):
                                    _cn = f"vpl_{ic}_{_i}"
                                    # [BUG 修复] 这里原来写的是 `CDS` —— 本函数（plot）里
                                    #   数据源叫 `source[ik][id]`，根本没有 `CDS` 这个名字；
                                    #   因为 RollingVolumeProfile 没有“普通指标线”列 -> _lidx 为空，
                                    #   这行永不执行所以一直没暴露；换成 VolumeProfileMA（有 'ma' 列）
                                    #   后 _lidx=[0] -> 直接抛 NameError: name 'CDS' is not defined。
                                    #   同时把 `a if c else b` 嵌套写法改成直白的 if。
                                    _src_vp = source[ik][id]
                                    if _cn not in _src_vp.column_names:
                                        _src_vp.add(_ind2[:, _li], _cn)
                                    _lp = plotinfo.get('linestyle', {}).get(_nm[_li], None) if isinstance(plotinfo.get('linestyle'), dict) else None
                                    _lkw = dict(line_width=1.3)
                                    if _lp is not None:
                                        _lkw['line_color'] = getattr(_lp, 'line_color', None)
                                        _lkw['line_width'] = getattr(_lp, 'line_width', 1.3) or 1.3
                                    _lkw = {k: v for k, v in _lkw.items() if v is not None}
                                    fig.line('index', _cn, source=_src_vp, **_lkw)
                                # [按用户要求暂时关闭] VP 图的 tooltips —— 需要时取消下面两行注释即可:

                                #(vp* 的几十档当 tooltip 没有意义)
                                if _lidx:
                                    try:
                                        _tt = [(str(_nm[_li]),
                                                "@vpl_" + str(ic) + "_" + str(_i) + "{0,0.0[0000]}")
                                               for _i, _li in enumerate(_lidx)]
                                        set_tooltips(fig, _tt, vline=True)
                                    except Exception:
                                        ...
                                _c0, _c1 = None, None
                                # ★ [BUG 修复] 同 btplot.py: linestyle['vp0'] 可能是
                                #   颜色字符串而不是 LineStyle -> 原 getattr 拿不到颜色,
                                #   导致 vp_color / vp_poc_color 失效(永远是默认蓝/橙)。
                                def _pick_vp_color(_v):
                                    if _v is None:
                                        return None
                                    if isinstance(_v, str):
                                        return _v
                                    if isinstance(_v, dict):
                                        return _v.get('line_color')
                                    return getattr(_v, 'line_color', None)
                                try:
                                    _lin = plotinfo.get('linestyle') or {}
                                    _c0 = _pick_vp_color(_lin.get(_nm[_vidx[0]]))
                                    _c1 = _pick_vp_color(_lin.get('vp_poc'))
                                    _lpb = _lin.get('vp_price_bars')
                                    _pbv = None if hasattr(_lpb, 'line_color') else _lpb
                                except Exception:
                                    _c0, _c1, _pbv = None, None, None
                                # ★ VPStyle（**只有 category='vp' 会读**）：颜色 /
                                #   透明度 / 满量程柱长 / 档高比例 / 档区窗口 /
                                #   右侧成交量刻度。与 pylightcharts 用**同一个**
                                #   取值器（utils.vp_style_values），两侧一致。
                                try:
                                    from minibt.utils import vp_style_values
                                    _vps = vp_style_values(
                                        plotinfo.get('vpstyle'))
                                except Exception:
                                    _vps = {}
                                if _vps:
                                    _c0 = _pick_vp_color(
                                        _vps.get('bar_color')) or _c0
                                    _c1 = _pick_vp_color(
                                        _vps.get('poc_color')) or _c1
                                    if _vps.get('price_bars'):
                                        _pbv = _vps['price_bars']
                                try:
                                    # ★ [BUG 修复] 接住返回的渲染器赋给 r:
                                    #   纯 VP 指标没有折线列 -> 后面 renderers=[r]
                                    #   会 UnboundLocalError(参见 btplot.py 同处说明)。
                                    r = _add_volume_profile(
                                        fig, source[ik][id], _ind2[:, _vidx], df, ic,
                                        black_color=black_color, y_mode=_vp_y,
                                        bar_color=_c0, poc_color=_c1, vidx=_vidx,
                                        price_bars=_pbv,
                                        # 副图里 VP 用自己的 y range（见函数内说明）
                                        y_range_name=(None if is_overlay else
                                                      f'vp_y{len(fig.extra_y_ranges)}'),
                                        # 顶部那根“成交量刻度轴”默认不画（见函数内开关说明）
                                        show_vol_axis=(bool(is_overlay)
                                                       and bool(_vps.get('show_axis', False))),
                                        # 副图里只有 VP(无指标线)时才把纵轴刻度标成价格中心
                                        y_tick_labels=(len(_lidx) == 0),
                                        # ---- VPStyle（category='vp' 专用）----
                                        profile_frac=float(
                                            _vps.get('profile_frac') or 0.30),
                                        fill_alpha=float(
                                            _vps.get('fill_alpha')
                                            if _vps.get('fill_alpha') is not None
                                            else 0.6),
                                        bin_ratio=float(
                                            _vps.get('bin_ratio') or 0.88),
                                        line_color=_vps.get('line_color'),
                                        line_width=float(
                                            _vps.get('line_width') or 0.0),
                                        highlight_poc=bool(
                                            _vps.get('highlight_poc', True)))
                                    if _vp_isnew:
                                        indicator_figs.append(fig)
                                    ic += 1        # 占一个序号, 保证列名唯一
                                except Exception as _e:      # 画不出就跳过, 但不静默
                                    import sys as _sys
                                    print(f"[volume_profile] 绘制失败: {type(_e).__name__}: {_e}",
                                          file=_sys.stderr)
                            elif is_overlay and not is_candles:  # 主图叠加
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
                                # 副图蜡烛按“指标自身”的开/收涨跌着色(不跟随主K线的 inc)
                                _ohlc_inc_field = f"candle_inc_{ic}"
                                _ohlc_open = np.asarray(indicator[:, filed_index[0]],
                                                        dtype=np.float64)
                                _ohlc_close = np.asarray(indicator[:, filed_index[3]],
                                                         dtype=np.float64)
                                _ohlc_inc = (_ohlc_close >= _ohlc_open
                                             ).astype(np.uint8).astype(str).tolist()
                                source[ik][id].add(_ohlc_inc, _ohlc_inc_field)
                                # 影线拆两条（单条 high->low 会穿过实体）
                                source[ik][id].add(
                                    np.maximum(_ohlc_open, _ohlc_close),
                                    f"candle_wtop_{ic}")
                                source[ik][id].add(
                                    np.minimum(_ohlc_open, _ohlc_close),
                                    f"candle_wbot_{ic}")
                                # [修改] 蜡烛指标优先使用**自身**的 candlestyle(plotinfo.candlestyle);
                                #   未设置时才回退到 _colors(主 K 线配色) / COLORS —— 原代码直接用 _colors,
                                #   导致蜡烛型指标无法自定义涨/跌配色。
                                _cs_ind = plotinfo.get('candlestyle', None)
                                if _cs_ind:
                                    _ohlc_palette = list(_cs_ind.values())
                                elif _colors:
                                    _ohlc_palette = list(_colors.values())
                                else:
                                    _ohlc_palette = COLORS
                                _ohlc_cmap = factor_cmap(
                                    _ohlc_inc_field, _ohlc_palette, ['0', '1'])
                                index = np.arange(indicator.shape[0])
                                pad = (index[-1] - index[0]) / 20
                                # [修改] 支持“主图双蜡烛”: 蜡烛型指标 overlap=True 时,
                                #   把蜡烛画到**主图** fig_ohlc(而不是另开副图),
                                #   并用更窄柱宽(BAR_WIDTH/2)避免完全盖住主 K 线;
                                #   overlap=False 仍按原逻辑单独开副图。
                                if is_overlay:
                                    fig_ohlc_ = fig_ohlc
                                    _candle_w = BAR_WIDTH / 2
                                else:
                                    fig_ohlc_ = new_indicator_figure(
                                        new_bokeh_figure, fig_ohlc, plot_width, plotinfo.get('height', 100))
                                    _candle_w = BAR_WIDTH
                                fig_ohlc_.segment(
                                    'index', f"candle_wtop_{ic}", 'index', lines[filed_index[1]], source=source[ik][id], color=black_color)
                                fig_ohlc_.segment(
                                    'index', f"candle_wbot_{ic}", 'index', lines[filed_index[2]], source=source[ik][id], color=black_color)
                                ohlc_bars_ = fig_ohlc_.vbar('index', _candle_w, lines[filed_index[0]], lines[filed_index[3]], source=source[ik][id],
                                                            line_color=black_color, fill_color=_ohlc_cmap)
                                ohlc_tooltips_ = [
                                    ('x, y', NBSP.join(('$index',
                                                        '$y{0,0.0[0000]}'))),
                                    ('OHLC', NBSP.join((f"@{lines[filed_index[0]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{lines[filed_index[1]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{lines[filed_index[2]]}{'{'}0,0.0[0000]{'}'}",
                                                        f"@{lines[filed_index[3]]}{'{'}0,0.0[0000]{'}'}")))]

                                # [修改] is_overlay 时 fig_ohlc_ 就是主图, 不能改它的 y 轴标题/可见性,
                                #   否则会把主 K 线的 y 轴盖掉; 仅副图蜡烛才按原逻辑设置。
                                if not is_overlay:
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

                                                # [新增] vbar_base 是本框架为柱体线新增的字段(不是 bokeh Line/VBar 的属性),
                                                #   必须像 price_line/price_label 一样在传给 bokeh 前剔除, 否则
                                                #   fig.line(**lineinfo) 会因未知属性报 AttributeError。柱体分支
                                                #   改为直接从 LineStyle 对象读取该值。
                                                _lineinfo.pop("vbar_base", None)
                                                # 同理：vbar_up_color /
                                                #   vbar_down_color 也不是 bokeh 属性
                                                _lineinfo.pop("vbar_up_color", None)
                                                _lineinfo.pop("vbar_down_color", None)
                                                _lineinfo.pop("vbar_color_base", None)
                                            # if is_overlay:
                                            fig_ohlc_.line(
                                                'index', source_name, source=source[ik][id],
                                                legend_label=source_name, **_lineinfo)
                                            ohlc_tooltips_.append(
                                                (_lines_name, NBSP.join(tuple(tooltips))))
                                set_tooltips(fig_ohlc_, ohlc_tooltips_,
                                             vline=True, renderers=[ohlc_bars_])
                                # [修改] 主图叠加时不往 indicator_figs 里加(它就是主图 fig_ohlc)
                                if not is_overlay:
                                    indicator_figs.append(fig_ohlc_)
                                ic += 1
                                _mulit_ind = len(indicator.shape) > 1
                                source[ik][id].add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'max') if _mulit_ind else indicator,
                                                   f"{legend_label}_h")
                                source[ik][id].add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'min') if _mulit_ind else indicator,
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
                                source[ik][id].add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'max') if _mulit_ind else indicator,
                                                   f"{legend_label}_h")
                                source[ik][id].add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'min') if _mulit_ind else indicator,
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

                                            # [新增] vbar_base 是本框架为柱体线新增的字段(不是 bokeh Line/VBar 的属性),
                                            #   必须像 price_line/price_label 一样在传给 bokeh 前剔除, 否则
                                            #   fig.line(**lineinfo) 会因未知属性报 AttributeError。柱体分支
                                            #   改为直接从 LineStyle 对象读取该值。
                                            _lineinfo.pop("vbar_base", None)
                                            # 同理：vbar_up_color /
                                            #   vbar_down_color 也不是 bokeh 属性
                                            _lineinfo.pop("vbar_up_color", None)
                                            _lineinfo.pop("vbar_down_color", None)
                                            _lineinfo.pop("vbar_color_base", None)
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
                                                ind = _span_values(indicator, isplot)
                                                _valid_ind = ind[~np.isnan(
                                                    ind)]
                                                mean = _valid_ind.mean() if len(_valid_ind) > 0 else np.nan
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
                                        ind = _span_values(indicator, isplot)
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
                        # 两条线之间填充背景色
                        if len(_lines) >= 2:
                            for band_dict in bandstyle:
                                _bd = dict(band_dict)
                                lower_name = _bd.get('lower', '')
                                upper_name = _bd.get('upper', '')
                                if lower_name in _lines and upper_name in _lines and all([isplot[_lines.index(lower_name)], isplot[_lines.index(upper_name)]]):
                                    # 将原始列名替换为数据源中带前缀的列名
                                    _bd['lower'] = lines[_lines.index(lower_name)]
                                    _bd['upper'] = lines[_lines.index(upper_name)]
                                    add_band_breaks(
                                        fig, source[ik][id], 'index', _bd)
                    if signal_info:
                        # 逐点信号标签文本: {信号key: 长度=bar数 的 object 数组, 有文本=str, 无=np.nan}
                        signal_texts_map: dict = plotinfo.get(
                            'signal_texts', {}) or {}
                        signal_style = resolve_signal_text_style(
                            signal_texts_map, black_style)
                        signal_ind_data_ = dict(
                            long_signal=None, exitlong_signal=None, short_signal=None, exitshort_signal=None)
                        for k, v in signal_info.items():
                            signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = list(
                                v.values())

                            if signalshow:
                                # 逐线样式（两个引擎共用一套键）：
                                # 主题预设 + 指标级(signaltext.style) + 本线 label
                                line_style = dict(signal_style or {})
                                line_style.update(
                                    _label_text_style(signallabel))
                                signaldata: np.ndarray
                                islabel = isinstance(signallabel, dict)
                                pertexts = None
                                label_text = None
                                if islabel:
                                    # 不原地 pop：signallabel 是共享 dict，pop 掉的
                                    # "text" 会让“从合约0复制信号”那次拿不到文本、
                                    # 退化成信号键名（long_signal/short_signal）
                                    label_text = signallabel.get("text", k)
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
                                    price_data = _signal_anchor_prices(
                                        df, indicator, _lines, signalkey,
                                        signaldata, doubles)
                                    signal_fig = fig_ohlc
                                else:
                                    signal_fig = fig
                                    price_data = _signal_anchor_prices(
                                        df, indicator, _lines, signalkey,
                                        signaldata, doubles)
                                signal_price = price_data[signaldata > 0]
                                signal_datetime = df.datetime.values[signaldata > 0]
                                # 逐点文本: 与数值信号线同索引对齐, 文本为 nan 的信号点跳过
                                pertexts = signal_texts_map.get(k)
                                perstyles = None
                                if pertexts is not None and len(pertexts) != len(signaldata):
                                    # 长度与数值行不一致(数据截断/多周期/切图), 回退统一文本防错位
                                    pertexts = None
                                # [修改] 原代码在这里用 _valid 就地子集化 signal_index /
                                #   signal_price / signal_datetime:
                                #       elif not _valid.all():
                                #           pertexts = pertexts[_valid]
                                #           signal_index = signal_index[_valid]
                                #           signal_price = signal_price[_valid]
                                #           signal_datetime = signal_datetime[_valid]
                                #   但 marker(scatter) 与文本(LabelSet) 共用同一个
                                #   signal_source_ -> "没有文本的信号点连 marker 也一起
                                #   被剔除", 与"文本为 nan 的信号点跳过"(只跳文本)不符。
                                #   改为: marker 恒用全量信号点(signal_*),
                                #   文本另走 text_* 三件套。
                                text_index = signal_index
                                text_price = signal_price
                                text_datetime = signal_datetime
                                if pertexts is not None:
                                    # 逐点元素可以是 str，也可以是
                                    # {'text': ..., 'font_size': ...}：先统一拆成
                                    # "文本数组 + 逐点样式数组"，后续流程照旧
                                    _raw = np.asarray(
                                        pertexts[signaldata > 0], dtype=object)
                                    _split = [normalise_text_item(v)
                                              for v in _raw]
                                    pertexts = np.array(
                                        [(s[0] or np.nan) for s in _split],
                                        dtype=object)
                                    perstyles = np.array(
                                        [s[1] for s in _split], dtype=object)
                                    _valid = ~pd.isna(pertexts)
                                    if not _valid.any():
                                        pertexts = None
                                        perstyles = None
                                    elif not _valid.all():
                                        pertexts = pertexts[_valid]
                                        perstyles = perstyles[_valid]
                                        text_index = signal_index[_valid]
                                        text_price = signal_price[_valid]
                                        text_datetime = signal_datetime[_valid]
                                # 文字标签: 逐点文本优先(无需先 set_label), 否则回退统一 label 配置
                                label_kw = ({kk: vv for kk, vv in signallabel.items()
                                             if kk != 'text'} if islabel else {})
                                xoff_col = False
                                # marker 的数据源: 始终是全量信号点
                                signal_source_ = ColumnDataSource(dict(
                                    index=signal_index,
                                    datetime=signal_datetime,
                                    price=signal_price,
                                    size=[float(signalsize), ] * len(signal_index),
                                ))
                                # 文本的数据源: 默认与 marker 共用(统一文本/无文本场景),
                                # 逐点文本场景下换成只含"有文本点"的子集(label_source_)。
                                label_source_ = signal_source_
                                if pertexts is not None:
                                    # 逐点文本: 支持 "\n" 多行; 水平偏移按各自文字宽度居中
                                    if not islabel:
                                        # 默认文字颜色随主题: 浅色→黑字, 深色→白字
                                        label_kw.update(dict(
                                            text_font_size="10pt",
                                            text_font_style="bold",
                                            text_color="white" if black_style else "black"))
                                    _fs = str(label_kw.get(
                                        "text_font_size", "10pt")).replace("pt", "")
                                    try:
                                        font_size = float(_fs)
                                    except (TypeError, ValueError):
                                        font_size = 10.
                                    # 方向判定: 以价格锚点列高/低决定文字落在信号点哪一侧
                                    # (锚点在低点→文字在下方, 锚点在高点→文字在上方),
                                    # 避免文字覆盖信号点与 K 线。锚点列无法判断时回退信号名。
                                    is_buy = signal_text_below(signalkey, k)
                                    if not islabel:
                                        label_kw.setdefault(
                                            "y_offset", -2.5 * font_size if is_buy else 1.4 * font_size)
                                    label_kw.pop("x_offset", None)
                                    xoff_col = True
                                    # 按 "\n" 拆行; 各点行数可不同, 缺行以 None 标记(渲染时剔除)
                                    split_rows = [str(t).split("\n") for t in pertexts]
                                    max_lines = max((len(r) for r in split_rows), default=1)
                                    pertext_lines = [
                                        [r[li] if li < len(r) else None for r in split_rows]
                                        for li in range(max_lines)]
                                    # [修改] 原代码:
                                    #     signal_source_ = ColumnDataSource(dict(
                                    #         index=signal_index,
                                    #         datetime=signal_datetime,
                                    #         price=signal_price,
                                    #         size=[float(signalsize), ] *
                                    #         len(signal_index),
                                    #         text=[str(t) for t in pertexts],
                                    #         x_offset=[...],
                                    #     ))
                                    #   它把文本与 marker 塞进同一个 CDS。现在 text/x_offset
                                    #   列长度 = 有文本的点数, 与全量 marker 点数不一致
                                    #   (bokeh 不校验列长), 必须拆成独立的 label_source_。
                                    label_source_ = ColumnDataSource(dict(
                                        index=text_index,
                                        datetime=text_datetime,
                                        price=text_price,
                                        size=[float(signalsize), ] *
                                        len(text_index),
                                        text=[str(t) for t in pertexts],
                                        # 多行文本：按**最宽的一行**居中，
                                        # 否则整块文字会偏向一侧
                                        x_offset=[-0.5 * max(
                                            text_width_pt(line, font_size, is_buy)
                                            for line in str(t).split('\n'))
                                            for t in pertexts],
                                    ))
                                elif islabel:
                                    pertext_lines = None
                                    # [修改] 原为 signal_source_ = ; 拆开以免 scatter
                                    #   被塞入仅文本需要的 text 列。
                                    label_source_ = ColumnDataSource(dict(
                                        index=signal_index,
                                        datetime=signal_datetime,
                                        price=signal_price,
                                        size=[float(signalsize),] *
                                        len(signal_index),
                                        text=[label_text] *
                                        len(signal_index),  # 标签文字列表
                                    ))
                                else:
                                    pertext_lines = None
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

                                # [修改] 原代码: marker=signalmarker
                                #   signalmarker 可能是 Markers 枚举里的 lightweight_charts 专用名
                                #   (arrow_up/arrow_down), 或 TradingView 风格的 label_up/label_down;
                                #   bokeh 不认这些名字 -> E-1001 BAD_COLUMN_NAME 且标记被静默丢弃。
                                #   统一经 to_bokeh_marker 收敛(见 minibt/constant.py)。
                                r = signal_fig.scatter(x='index', y='price', source=signal_ind_data_source[ik][id][-1].get(k), fill_color=signalcolor,
                                                       marker=to_bokeh_marker(signalmarker), line_color='black', size="size")
                                if pertext_lines is not None and len(pertext_lines) > 1:
                                    # --- 多行文本: 每行一个 LabelSet, 逐行错开一个行距 ---
                                    # 低点锚点向下展开, 高点锚点向上展开(均背离 K 线)
                                    label_kw.update(
                                        _bokeh_text_options(line_style))
                                    y0 = float(label_kw.get("y_offset", 0.))
                                    y0 += float(
                                        line_style.get('offset_y') or 0.)
                                    _fs2 = str(label_kw.get(
                                        "text_font_size", "10pt")).replace("pt", "")
                                    try:
                                        font_size2 = float(_fs2)
                                    except (TypeError, ValueError):
                                        font_size2 = 10.
                                    line_gap = font_size2 * float(
                                        line_style.get('line_height') or 1.3)
                                    _base_rows = list(zip(text_index, text_price,
                                                          text_datetime))
                                    # 行展开方向: 低点锚点文本在信号点下方, 多行继续向下(负向)
                                    # 展开; 高点锚点文本在信号点上方, 多行继续向上(正向)展开,
                                    # 均背离 K 线, 避免堆叠遮挡信号点与 K 线
                                    _row_dir = -1 if is_buy else 1
                                    for li, line_txts in enumerate(pertext_lines):
                                        keep = [(r[0], r[1], r[2], tx)
                                                for r, tx in zip(_base_rows, line_txts)
                                                if tx]
                                        if not keep:
                                            continue
                                        _lkw = dict(label_kw)
                                        _lkw['y_offset'] = y0 + _row_dir * li * line_gap
                                        _lkw['x_offset'] = 'x_offset'
                                        labels = LabelSet(
                                            x='index',
                                            y='price',
                                            text='text',
                                            source=ColumnDataSource(dict(
                                                index=[r[0] for r in keep],
                                                datetime=[r[2] for r in keep],
                                                price=[r[1] for r in keep],
                                                text=[r[3] for r in keep],
                                                x_offset=[-0.5 * text_width_pt(
                                                    r[3], font_size2, is_buy)
                                                    + float(line_style.get('offset_x') or 0.)
                                                    for r in keep],
                                            )),
                                            **_lkw,
                                        )
                                        signal_fig.add_layout(labels)
                                elif pertexts is not None or islabel:
                                    # --- 单行文本: 用单个 LabelSet 添加文字标签 ---
                                    label_kw.update(
                                        _bokeh_text_options(line_style))
                                    # 逐点样式（单行才支持）：有差异的键改成数据列，
                                    # LabelSet 支持 "属性名 = 列名" 这种写法
                                    if perstyles is not None:
                                        _per_cols = {}
                                        pairs = (
                                            ('font_size', 'text_font_size'),
                                            ('text_color', 'text_color'),
                                            ('background_fill_color',
                                             'background_fill_color'),
                                            ('background_fill_alpha',
                                             'background_fill_alpha'),
                                            ('border_line_color',
                                             'border_line_color'),
                                            ('border_line_alpha',
                                             'border_line_alpha'))
                                        for _key, _opt in pairs:
                                            _vals = [s.get(_key) for s in perstyles
                                                     if isinstance(s, dict)]
                                            if len(_vals) != len(perstyles) \
                                                    or not _vals:
                                                continue
                                            if all(v is None for v in _vals):
                                                continue
                                            if _key == 'font_size':
                                                _col = [
                                                    f'{float(v):g}pt' if v else
                                                    label_kw.get(
                                                        'text_font_size', '10pt')
                                                    for v in _vals]
                                            else:
                                                _col = [
                                                    v if v is not None else
                                                    label_kw.get(_opt)
                                                    for v in _vals]
                                            _per_cols[_opt] = _col
                                        if _per_cols:
                                            label_source_.data.update(_per_cols)
                                            for _opt in _per_cols:
                                                label_kw[_opt] = _opt
                                    if xoff_col:
                                        # 逐点模式: 水平偏移用数据列(每点自适应宽度居中)
                                        label_kw['x_offset'] = 'x_offset'
                                    if label_kw.get('y_offset') is not None:
                                        label_kw['y_offset'] = (
                                            float(label_kw['y_offset'])
                                            + float(line_style.get('offset_y') or 0.))
                                    labels = LabelSet(
                                        x='index',        # 标签 x 坐标（与散点 x 一致）
                                        y='price',        # 标签 y 坐标（与散点 y 一致）
                                        text='text',      # 标签文字来源（数据源的 text 字段）
                                        source=label_source_,  # 文本数据源(逐点文本时为子集)
                                        **label_kw,
                                    )
                                    signal_fig.add_layout(labels)  # 将标签添加到图形中
                                tooltips = [(k, "@price{0.00}"),]
                                set_tooltips(signal_fig, tooltips,
                                             vline=False, renderers=[r,])

            # 如果当前合约**确实有成交**但没有信号标记，才从合约0复制信号标记。
            # 加了“有成交”这个前提：像 `self.data1 = get_kline(...)` 这种只挂了
            # K 线、没指标也没交易的合约，不应该被画上合约0的信号。
            _has_ref_trades = False
            if id > 0:
                try:
                    _tr = results[ik][id]
                    _has_ref_trades = len(_tr) > 0 and bool(
                        np.asarray(_tr.positions).any())
                except Exception:
                    _has_ref_trades = False
            if (id > 0 and trade_signal and _has_ref_trades
                    and not signal_ind_data_source[ik][id]):
                ref_indicators = indicator_datas[ik][0]
                for ref_isplot, ref_name, ref_lines, ref__lines, ref_ind_name, ref_is_overlay, ref_category, ref_indicator, ref_doubles, ref_plotinfo, ref_span, ref__signal, ref_bandstyle in ref_indicators:
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
                            label_text = signallabel.get("text", k)
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
                        # [修改] 原代码: marker=signalmarker
                        #   signalmarker 可能是 Markers 枚举里的 lightweight_charts 专用名
                        #   (arrow_up/arrow_down), 或 TradingView 风格的 label_up/label_down;
                        #   bokeh 不认这些名字 -> E-1001 BAD_COLUMN_NAME 且标记被静默丢弃。
                        #   统一经 to_bokeh_marker 收敛(见 minibt/constant.py)。
                        r = signal_fig.scatter(x='index', y='price', source=signal_source_, fill_color=signalcolor,
                                              marker=to_bokeh_marker(signalmarker), line_color='black', size="size")
                        if islabel:
                            _lbl = {kk: vv for kk, vv in signallabel.items()
                                    if kk != 'text'}
                            _lbl.update(
                                dict(background_fill_alpha=0.1, background_fill_color="white" if black_style else "black"))
                            labels = LabelSet(
                                x='index', y='price', text='text', source=signal_source_, **_lbl)
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

                        # [新增] 把 Volume Profile / 成交量 overlay 排除出价格 y 轴的自动拟合:
            #   它们的 y 值跨越整段数据的价格范围(成交量基线更是常量最低价),
            #   若不排除, y 轴掩码会把主轴拉成全历史范围(横缩后极值仍不对)。
            #   此处所有图层均已建完 -> 除它们之外全部图层照旧参与拟合。
            try:
                _ex = [r for r in fig_ohlc.renderers
                       if getattr(r, 'tags', None) and '__vp_excl__' in r.tags]
                if _ex:
                    fig_ohlc.y_range.renderers = [r for r in fig_ohlc.renderers
                                                  if r not in _ex]
            except Exception:
                ...
            custom_js_args = dict(ohlc_range=fig_ohlc.y_range, indicator_range=[indicator_figs[_ic].y_range for _ic in range(len(indicator_figs))],
                                  indicator_h=indicator_h, indicator_l=indicator_l, source=source[ik][id])

            # 成交量
            # [修改] 成交量已**固定叠加到主 K 线图**(见 ohlc_bars 之后那段),
            #   这里默认不再单独显示高度 60 的成交量副图; 需要时把 show_volume_pane 改 True。
            # [开关] 由 Config.volume_mode 决定（'sub' / 'both' 时显示独立副图）
            #   原代码: show_volume_pane = True    # [改回] 恢复原来的独立成交量副图
            show_volume_pane = volume_mode in ('sub', 'both')
            fig_volume = new_indicator_figure(
                # 成交量副图 = 其它指标副图（默认 80）的一半高度
                new_bokeh_figure, fig_ohlc, plot_width, y_axis_label="volume", height=40)
            fig_volume.css_classes = ["volume-chart"]
            fig_volume.visible = show_volume_pane
            # X 轴统一在下面 `for f in plots:` 里处理：
            #   只保留最下面一张图的时间轴，其它隐藏（共享同一个 x_range）。
            
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
                from ..utils import resolve_tradestyle
                # 每个合约取该策略的 `config.tradestyle`（bokeh 与 pylightcharts 共用 ✓）
                _ts = resolve_tradestyle(strategys[ik])
                if _ts.show:
                    _line_color = (_trade_str(_ts.line_color, '')
                                   or ('yellow' if black_style else 'blue'))
                    _line_width = float(_ts.line_width or 3)
                    _line_dash = _trade_dash(_ts.line_dash)
                    # 多头/空头连线：用虚线连接入场→出场，悬停显示P/L
                    ls = fig_ohlc.segment(x0='index', y0='price', x1='flat_index', y1='flat_price',
                                        source=long_segment_source[ik][id], color=_line_color, line_width=_line_width, line_dash=_line_dash)
                    ss = fig_ohlc.segment(x0='index', y0='price', x1='flat_index', y1='flat_price',
                                        source=short_segment_source[ik][id], color=_line_color, line_width=_line_width, line_dash=_line_dash)
                    # 入场 / 离场标记（颜色 / 形状 / 大小来自 TradeStyle ✓）
                    _le, _se = _ts.long_entry, _ts.short_entry
                    _lx, _sx = _ts.long_exit, _ts.short_exit
                    # 多单入场信号：默认正三角；空单入场：默认倒三角
                    l_entry = fig_ohlc.scatter(x='index', y='price', source=long_segment_source[ik][id],
                                               marker=to_bokeh_marker(_trade_str(_le.marker, 'triangle')),
                                               size=float(_le.size), color=_trade_str(_le.color, 'red'),
                                               fill_color=_trade_str(_le.color, 'red'))
                    s_entry = fig_ohlc.scatter(x='index', y='price', source=short_segment_source[ik][id],
                                               marker=to_bokeh_marker(_trade_str(_se.marker, 'inverted_triangle')),
                                               size=float(_se.size), color=_trade_str(_se.color, 'green'),
                                               fill_color=_trade_str(_se.color, 'green'))
                    # 离场标记（bokeh 原来只画了入场，这里补上 ✓）
                    l_exit = fig_ohlc.scatter(x='index', y='price', source=long_flat_source[ik][id],
                                              marker=to_bokeh_marker(_trade_str(_lx.marker, 'circle')),
                                              size=float(_lx.size), color=_trade_str(_lx.color, 'red'),
                                              fill_color=_trade_str(_lx.color, 'red'))
                    s_exit = fig_ohlc.scatter(x='index', y='price', source=short_flat_source[ik][id],
                                              marker=to_bokeh_marker(_trade_str(_sx.marker, 'circle')),
                                              size=float(_sx.size), color=_trade_str(_sx.color, 'green'),
                                              fill_color=_trade_str(_sx.color, 'green'))
                    # 为 datetime 字段添加格式化器
                    fig_ohlc.add_tools(HoverTool(
                        point_policy='follow_mouse',
                        renderers=[ls, ss, l_entry, s_entry, l_exit, s_exit],
                        formatters={'@entry_datetime': 'datetime', '@exit_datetime': 'datetime'},
                        tooltips=[("方向", "@side"), ("入场时间", "@entry_datetime{%Y-%m-%d %H:%M}"), ("出场时间", "@exit_datetime{%Y-%m-%d %H:%M}"), ("持仓分钟", "@hold_minutes{0}"), ("入场价", "@price{0.00}"), ("出场价", "@flat_price{0.00}"), ("P/L", "@profit{0.00}")],
                        mode='mouse'))
            plots: list[_figure] = [
                fig_ohlc, fig_volume]+indicator_figs
            # 权益曲线（仅第一个合约显示）
            if id == 0 and profit_plot[ik] and with_equity:
                source_key = 'value'
                fig_value = new_indicator_figure(new_bokeh_figure, fig_ohlc, plot_width,
                                                 y_axis_label=source_key,
                                                 height=90)
                fig_value.css_classes = ["value-chart"]
                fig_value.tags = ["profit_plot"]
                fig_value.patch('index', 'level',
                                source=value_source[ik][id],
                                fill_color='#ffffea', line_color='#ffcb66', line_dash='dashed')
                # 初始资金与权益曲线之间的色带（level 为初始资金，value 为权益曲线）
                # 深色背景下 #4dabf7@0.15 太暗看不清：换成对比更强的青绿 + 提高不透明度
                band_value = Band(
                    base='index',
                    lower='level',
                    upper=source_key,
                    source=value_source[ik][id],
                    fill_color='#20c997' if black_style else '#4dabf7',
                    fill_alpha=0.28 if black_style else 0.15,
                    line_width=0,
                    level='underlay',
                )
                fig_value.add_layout(band_value)
                r_value = fig_value.line(
                    'index', source_key, source=value_source[ik][id],
                    legend_label='value',
                    line_color='#4dabf7' if black_style else 'blue',
                    line_width=2, line_alpha=1)
                # ★ 权益曲线**单独**的 y 轴（只按 ``value`` 收紧，不影响其它图）：
                #   权益默认是 ``DataRange1d``，前端自动拟合 + padding 会把曲线压成很小
                #   一截。这里换成固定 ``Range1d``，**只 fit value 本身**（不纳入 level
                #   初始资金参考线，否则参考线离得远会把曲线压扁）；回放侧由
                #   ``bokeh_replay._refresh_equity_ranges`` 每 N 帧跟随刷新。
                try:
                    _vv = np.asarray(value_source[ik][id].data[source_key],
                                     dtype=float)
                    _vv = _vv[np.isfinite(_vv)]
                    if _vv.size:
                        _vlo, _vhi = float(_vv.min()), float(_vv.max())
                        if not (_vhi > _vlo):
                            _vp = max(abs(_vhi) * 0.001, 1.0)
                            _vlo, _vhi = _vlo - _vp, _vhi + _vp
                        _vpad = (_vhi - _vlo) * 0.05
                        fig_value.y_range = Range1d(_vlo - _vpad, _vhi + _vpad)
                except Exception:
                    ...
                # ★ 拖动/缩放 x 轴时，权益 Y 轴跟着**可见窗口**重算
                #   （与其它图缩放时一致）。只挂在**这张权益图**的 x_range 上，
                #   不影响其它图；与回放的 _refresh_equity_ranges 也互不冲突
                #   （都是把 y_range 设成同一个“可见 value 范围 + 5%”）。
                try:
                    from bokeh.models import CustomJS as _EQCJS
                    _eq_xr = fig_value.x_range
                    _eq_cb = _EQCJS(
                        args=dict(src=value_source[ik][id], key=source_key,
                                  xr=_eq_xr, yr=fig_value.y_range),
                        code="""
const v = src.data[key], ix = src.data['index'];
if (!v || !ix) return;
const a = Math.round(xr.start), b = Math.round(xr.end);
let lo = Infinity, hi = -Infinity;
for (let i = 0; i < ix.length; i++) {
    const x = ix[i];
    if (x < a || x > b) continue;
    if (v[i] < lo) lo = v[i];
    if (v[i] > hi) hi = v[i];
}
if (!isFinite(lo) || !isFinite(hi)) return;
if (hi <= lo) { const p = Math.max(Math.abs(hi) * 0.001, 1.0); lo -= p; hi += p; }
const pad = (hi - lo) * 0.05;
yr.start = lo - pad;
yr.end = hi + pad;
""")
                    _eq_xr.js_on_change('start', _eq_cb)
                    _eq_xr.js_on_change('end', _eq_cb)
                except Exception:
                    ...
                if live_sources is not None:
                    live_sources.setdefault('equity_figs', []).append(
                        (fig_value, value_source[ik][id], source_key))
                tooltip_format = f'@{source_key}{{+0,0.[00]}}'
                tick_format = '0,0.[00]'
                set_tooltips(
                    fig_value, [(source_key, tooltip_format)], renderers=[r_value,])

                fig_value.yaxis.formatter = NumeralTickFormatter(
                    format=tick_format)
                fig_value.yaxis.axis_label = None
                fig_value.yaxis.axis_label_text_color = black_color
                plots.insert(0, fig_value)
            figs_ohlc[ik].append(fig_ohlc)
            linked_crosshair = CrosshairTool(
                dimensions='both', line_color=black_color)

            #: 联动十字线的竖直 Span（同一合约的每张图各一根）
            fig_spans: list = []
            # X 轴画在**最下面一张可见图**上（成交量副图可能是隐藏的）
            _vis_plots = [f for f in plots if getattr(f, 'visible', True)]
            _xfig = _vis_plots[-1] if _vis_plots else (plots[-1] if plots else None)

            for f in plots:
                # 给副图/指标图打上类名（全屏布局靠它做 3:1 高度分配）
                if not (f.css_classes or []):
                    f.css_classes = ["indicator-chart"]
                if fullscreen:
                    f.sizing_mode = 'stretch_both'
                    f.height = None
                    f.width = None
                    # 主图 3 份、副图各 1 份高度（通过 figure 自己的 shadow CSS）
                    if 'candle-chart' in (f.css_classes or []):
                        _w = 3
                    elif 'volume-chart' in (f.css_classes or []):
                        _w = 0.5   # 成交量副图 = 指标副图的一半高度
                    else:
                        _w = 1
                    f.stylesheets = list(f.stylesheets or []) + [
                        _figure_fullscreen_css(_w)]
                # 原生图例隐藏：左上角的“带颜色线 + 名称 + 当前值”由
                # OVERLAY_* 注入的 HTML 数值标签代替（避免两套重复）
                for _leg in (([f.legend] if getattr(f, 'legend', None) else []) +
                             list(getattr(f, 'legends', None) or [])):
                    _leg.visible = False
                if f.legend:
                    f.legend.nrows = 1
                    f.legend.label_height = 6
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
                f.min_border_bottom = 0   # 图与图之间不留空隙
                f.min_border_right = 10
                f.outline_line_color = '#666666'
                # 网格线：由 config.grid 控制（默认不显示）
                f.xgrid.visible = show_grid
                f.ygrid.visible = show_grid
                # Y 轴标题一律去掉，只留刻度（标题太小、要凑近才看得清）
                f.yaxis.axis_label = None
                # toolbox 控制每张图自己的工具栏（放最右，左→右：图 / Y轴 / 工具栏）；
                # 全屏时 figures 直接进 column（无 GridPlot 合并），只能靠这个显/隐
                f.toolbar_location = 'right' if show_toolbar else None

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
                # 所有图（含 ismain 副图）的 Y 轴都显示在右侧
                # 联动十字线：hover 时把每张图的这根竖直 Span 一起移动
                _sp = Span(dimension='height', location=0,
                           line_color=black_color, line_width=1,
                           line_alpha=0.6, visible=False)
                f.add_layout(_sp)
                fig_spans.append(_sp)
                # X 轴：只保留最下面一张（时间坐标），其它隐藏
                f.xaxis.visible = (f is _xfig)

            # X 轴时间刻度：按 index 查 datetime 列
            _dt_col = None
            try:
                _dt_col = source[ik][id].data.get('datetime')
            except Exception:
                _dt_col = None
            if _dt_col is not None:
                _fmt = CustomJSTickFormatter(args=dict(dt=_dt_col),
                                             code=_TIME_TICK_JS)
                # 时间刻度按**可见窗口**动态生成（~8 个不重叠的刻度），
                # 而不是固定 `range(0, N, 10)`（1 万根=1000 个，缩放后要么重叠
                # 要么被 NoOverlap 抽得只剩几个）。
                _tickr = CustomJSTicker(args=dict(dt=_dt_col),
                                        major_code=_TIME_TICKER_JS)
                for f in plots:
                    try:
                        f.xaxis[0].formatter = _fmt
                        f.xaxis[0].ticker = _tickr
                        f.xaxis[0].major_label_policy = NoOverlap()
                    except Exception:
                        ...

            # 统一给每张图的 hover 回调带上 spans（十字线联动）+ 离开时隐藏；
            # xfig_id = 最下面那张图（时间标签固定显示在它的底部）
            _xfig_id = getattr(_xfig, 'id', None) if _xfig is not None else None
            for f in plots:
                for _tool in getattr(f, 'tools', []):
                    if (isinstance(_tool, HoverTool)
                            and getattr(_tool, 'name', '') == 'minibt_overlay'):
                        _tool.callback = CustomJS(
                            args=dict(fig_id=f.id, spans=fig_spans,
                                      xfig_id=_xfig_id),
                            code=OVERLAY_UPDATE_JS)
                try:
                    f.js_on_event(MouseLeave, CustomJS(
                        args=dict(spans=fig_spans, fig_id=f.id,
                                  xfig_id=_xfig_id),
                        code=('for (var i = 0; i < spans.length; i++) '
                              '{ spans[i].visible = false; }\n'
                              'var idx = window.__minibt_fig_index__ || {};\n'
                              'var e = idx[fig_id];\n'
                              'if (e && e.ylab) { e.ylab.style.display = "none"; }\n'
                              'var ex = idx[xfig_id] || e;\n'
                              'if (ex && ex.xlab) { ex.xlab.style.display = "none"; }')))
                except Exception:
                    ...

            kwargs = dict(
                ncols=1,
                # toolbox=True 时才显示工具栏（放最右）
                toolbar_location='right' if show_toolbar else None,
                sizing_mode=_layout_sizing,  # 全屏时 stretch_both，否则 scale_width
                toolbar_options=dict(logo=None),
                merge_tools=True
            )
            all_plots.append(plots)

        ismain = btind_main[ik][1:]

        # 初始化 panels 列表
        panels = []
        # 全屏：figures 要作为 flex column 的**直接子元素**
        # （否则 .bk-Column > .candle-chart 选不中，3:1 不生效）
        _fs_styles = ([panel_CUSTOM_CSS + FULLSCREEN_COLUMN_CSS]
                      if fullscreen else None)

        if any(ismain):
            _ip = 0
            _all_plots = [_p for _ip, _p in enumerate(
                all_plots[1:]) if not ismain[_ip]]
            first_plot = all_plots[0]
            row_plots = []
            row_figs = []
            for _ismain, _plots in list(zip(ismain, all_plots[1:])):
                if _ismain:
                    _ip += 1
                    if fullscreen:
                        row_figs.extend(list(_plots))
                    else:
                        row_plots.append(gridplot(_plots, **kwargs))
            if fullscreen:
                _lay = column(children=row_figs + list(first_plot),
                              width_policy='max', sizing_mode=_layout_sizing,
                              stylesheets=_fs_styles)
            else:
                controls = row(*row_plots, width_policy='max') if row_plots else None
                figs = gridplot(
                    first_plot,
                    ** kwargs
                )
                # 将 ismain=True 的图表合并到主布局
                if controls:
                    _lay = column(controls, figs, width_policy='max',
                                  sizing_mode=_layout_sizing)
                else:
                    _lay = column(figs, width_policy='max',
                                  sizing_mode=_layout_sizing)
            name_ = '_'.join(
                [symbols[ik][0], *(str(symbol_multi_cycle[ik][__ip]) for __ip in range(1, _ip+1))])
            # 将合并后的布局添加到 panels 列表的开头
            panels.append(
                Panel(child=_lay, title=name_))
            # 更新 all_plots 为不包含 ismain=True 的图表
            all_plots = _all_plots
        else:
            # 如果没有 ismain=True 的图表，将第一个图表作为默认面板
            if fullscreen:
                _child = column(children=list(all_plots[0]), width_policy='max',
                                sizing_mode=_layout_sizing,
                                stylesheets=_fs_styles)
            else:
                figs = gridplot(all_plots[0], **kwargs)
                _child = column(figs, width_policy='max',
                                sizing_mode=_layout_sizing)
            panels.append(
                Panel(child=_child,
                      title=f"{symbols[ik][0]}_{symbol_multi_cycle[ik][0]}"))
            all_plots = all_plots[1:]
        # 添加剩余的 ismain=False 的图表
        for _ips, __plots in enumerate(all_plots):
            layout = column(
                children=__plots,
                sizing_mode=_layout_sizing,
                css_classes=["dynamic-column"],
                # 全屏时带上列布局 CSS（竖向 flex + 占满内容区）
                stylesheets=[panel_CUSTOM_CSS + (FULLSCREEN_COLUMN_CSS if fullscreen else "")],
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

        _tabs_styles = ([DARK_TABS_CSS] if black_style else []) + (
            [FULLSCREEN_TABS_INNER_CSS] if fullscreen else [])
        ts.append(Tabs(tabs=panels,
                  width=plot_width if plot_width else None, width_policy='max',
                  sizing_mode=_layout_sizing,
                       css_classes=["dark-tabs"] if black_style else [],
                       stylesheets=_tabs_styles
                       ))
    # 创建策略图表的面板
    # ★ 单策略：外层「策略名」标签页多余（策略名由外部顶栏显示）→ 直接用内层
    #   「合约」标签页，图表上方不再顶一行策略名 ✗；fullscreen 时内层标签页成了
    #   最外层，把它的 100% 换成 100vh（原外层 CSS）✓
    #   （btplot 走的是 ``btplot.py``，不受影响 ✓）
    if len(ts) == 1:
        tabs = ts[0]
        if fullscreen:
            styles = [style for style in (tabs.stylesheets or [])
                      if style is not FULLSCREEN_TABS_INNER_CSS]
            styles.append(FULLSCREEN_TABS_CSS)
            tabs.stylesheets = styles
    else:
        all_panels = [Panel(child=t, title=s_name[i]) for i, t in enumerate(ts)]
        tabs = Tabs(tabs=all_panels,
                    background=white_color, width=plot_width if plot_width else None, width_policy='max',
                    sizing_mode=_layout_sizing,
                    css_classes=["dark-tabs"] if black_style else [],
                    stylesheets=([DARK_TABS_CSS] if black_style else []) + (
                        [FULLSCREEN_TABS_CSS] if fullscreen else [])
                    )
    # ===================== 回放：登记可更新数据源 =====================
    if live_sources is not None:
        # 一次遍历拿到**确定顺序**的模型列表：CDS 配对（sync_sources）、各合约
        # x_range / 主 K 线图、副图 y 轴冻结 都用它 —— 实时刷新时模型树遍历很贵，
        # 之前这里遍历了 3 次（select + references + _ordered_models）。
        _models = _ordered_models(tabs)
        # 主图成交量叠加源**不参与揭开**：它的基线由它自己的 CustomJS 跟着视口
        # Y 范围刷新；若回放也去切开/重写它，会「一次更新一次闪」。所以整段保留，
        # 只让 X 轴窗口推进（视觉上仍然跟着回放走）。
        _cds_list = [c for c in _models
                     if isinstance(c, ColumnDataSource)
                     and isinstance(getattr(c, 'data', None), dict)
                     and 'index' in c.data
                     and 'vol_ov_ratio' not in c.data]
        live_sources['cds'] = _cds_list
        live_sources['full'] = {
            c.id: {k: np.asarray(v) for k, v in c.data.items()}
            for c in _cds_list}
        # 收集各合约的 x_range（共享的只留一个）与主 K 线图（回放时窗口/价格 Y 跟着走）
        try:
            from bokeh.models.plots import Plot as _Plot
            _xr: list = []
            _ohlc: list = []
            for _p in _models:
                if not isinstance(_p, _Plot):
                    continue
                _r = getattr(_p, 'x_range', None)
                if _r is not None and not any(_r is _q for _q in _xr):
                    _xr.append(_r)
                if 'candle-chart' not in (_p.css_classes or []):
                    continue
                _src = None
                for _rr in _p.renderers:
                    _s = getattr(_rr, 'data_source', None)
                    if isinstance(_s, ColumnDataSource) and 'high' in (_s.data or {}):
                        _src = _s
                        break
                if _src is not None:
                    _ohlc.append((_src, _p))
            live_sources['x_ranges'] = _xr
            live_sources['ohlc_figs'] = _ohlc
        except Exception:
            live_sources['x_ranges'] = []
            live_sources['ohlc_figs'] = []
        # 副图（指标/权益/成交量）y 轴固定成整段范围，避免每帧重拟合而抖动
        try:
            freeze_subfigure_ranges(tabs, live_sources, refs=_models)
        except Exception:
            live_sources['sub_figs'] = []
        live_sources['symbols'] = symbols
        live_sources['cycles'] = symbol_multi_cycle
        live_sources['strategy_names'] = s_name
        live_sources['theme'] = black_style
        live_sources['fullscreen'] = fullscreen
        live_sources['volume_mode'] = volume_mode
        live_sources['start_value'] = start_value
        if reveal_length is not None:
            reveal_sources(live_sources, int(reveal_length) - 1)

    if build_only:
        return tabs

    if open_browser:
        IS_JUPYTER_NOTEBOOK = False

    # 保存图表
    if plot_name and isinstance(plot_name, str):
        if not plot_name.endswith('.html'):
            plot_name += 'bokeh_plot.html'
    else:
        plot_name = 'bokeh_plot.html'
    plot_cwd = plot_cwd if isinstance(
        plot_cwd, str) and plot_cwd else os.path.join(user_dir(), "plots")
    if not os.path.exists(plot_cwd):
        os.makedirs(plot_cwd, exist_ok=True)
    FileName = os.path.join(plot_cwd, plot_name)
    # 直接生成 HTML 并注入 tooltip 主题色 / 钉位脚本。
    # 注意：**不能再用 `bokeh.io.show()`** —— 它最终走 `save()`，而实测
    # `INLINE.css_raw` / `js_raw` 并不会写进页面（所以以前 tooltip 主题/钉位
    # 实质上没生效）。这里改为自己生成 HTML、写入临时文件再打开。
    # 用 INLINE（内嵌 bokeh JS/CSS）而不是 CDN，页面自包含、离线也能打开
    html = file_html(tabs, INLINE, "Strategy Plot")
    # 注入到真正的 `</body>` 前（**不能**用第一个 `</head>`：bokeh 内联 JS 里
    # 的字符串也含 `</head>`，会把 bundle 拆坏、整段 JS 变成页面文本）。
    # pylightcharts 里用户设置的主题色（顶栏按下色 / 十字线标签背景）；未设置则 None
    try:
        from .strategy_window import _load_settings
        _accent = _load_settings()['user'].get('theme_color')
    except Exception:
        _accent = None
    _inject = _theme_head(black_style, fullscreen, accent=_accent)
    _bi = html.rfind('</body>')
    if _bi != -1:
        html = html[:_bi] + _inject + html[_bi:]
    else:
        html = html + _inject

    if save_plot:
        with open(FileName, 'w', encoding='utf-8') as f:
            f.write(html)

    # 显示图表
    if IS_JUPYTER_NOTEBOOK:
        from IPython.display import display, HTML
        display(HTML(html))
    else:
        import tempfile
        import webbrowser
        tmp = os.path.join(tempfile.gettempdir(),
                           f'minibt_bokeh_{os.getpid()}.html')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(html)
        webbrowser.open('file://' + os.path.abspath(tmp))

    return tabs


def _signal_anchor_prices(df, indicator, _lines, signalkey, signaldata,
                          doubles):
    """信号锚点价格：K 线列 → 任意指标线 → 退回信号线自身。

    以前只看 ``df[signalkey]``（K 线列），像 ``SignalStyle('vidya_up', ...)``
    这种"锚到某条指标线"的写法会直接 ``KeyError``；现在与 pylightcharts 侧
    同口径（也兼容 ``overlap=True`` 的写法）。
    """
    key = str(signalkey or '').strip()
    if key:
        try:
            if key in getattr(df, 'columns', ()):
                return np.asarray(df[key].values, dtype=float)
        except Exception:
            pass
        try:
            if doubles:
                index1, index2 = search_index(_lines, key)
                return np.asarray(indicator[index1][:, index2], dtype=float)
            return np.asarray(indicator[:, _lines.index(key)], dtype=float)
        except Exception:
            pass
    return np.asarray(signaldata).copy()


def add_band_breaks(fig, source, base, options, *,
                    legend_key: str = 'legend_label'):
    """按 NaN 把色带**拆段**画（bokeh 的 `Band` 会把 NaN 两侧直接连起来）。

    指标列常常是"分段有效"的：VIDYA 的 ``vidya_up`` 在空头段为 ``nan``、
    ``vidya_down`` 在多头段为 ``nan``（`hl2` 则一直有效）。直接用一条 `Band`
    会在 NaN 处横跨填充，看起来就是"两段色带中间被补上了"。

    这里只按**两列都有效**的连续段各画一条 `Band`，NaN 处自然断开；
    没有 NaN 时行为与原代码完全一致（仍然只画一条）。

    :return: 建好的 Band 列表（没有有效段时为空）。
    """
    lower = options.get('lower')
    upper = options.get('upper')
    data = getattr(source, 'data', None) or {}
    if not lower or not upper or base not in data or lower not in data \
            or upper not in data:
        return []
    lows = np.asarray(data.get(lower), dtype=float)
    highs = np.asarray(data.get(upper), dtype=float)
    if lows.size == 0 or highs.size == 0:
        return []
    valid = np.isfinite(lows) & np.isfinite(highs)
    if valid.all():
        band = Band(base=base, source=source, **options)
        fig.add_layout(band)
        return [band]
    base_values = np.asarray(data.get(base))
    edges = np.diff(np.concatenate(([0], valid.view(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    made = []
    for start, stop in zip(starts, ends):
        if stop - start < 2:               # 一个点连不成带
            continue
        piece = ColumnDataSource({
            base: base_values[start:stop],
            lower: lows[start:stop],
            upper: highs[start:stop],
        })
        # 图例标签只给第一段，否则图例里会出现一堆同名条目
        now = options if not made else {
            key: value for key, value in options.items() if key != legend_key}
        band = Band(base=base, source=piece, **now)
        fig.add_layout(band)
        made.append(band)
    return made


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
