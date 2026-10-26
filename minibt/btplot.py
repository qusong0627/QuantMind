from __future__ import annotations
from bokeh.io import reset_output
from .utils import np, Callable, TYPE_CHECKING, FILED, Literal, pd, text_width_pt, resolve_signal_text_style, signal_text_below, user_dir
from .constant import to_bokeh_marker
from bokeh.embed import file_html
from bokeh.resources import INLINE, CDN
from copy import deepcopy
import warnings
from bokeh.palettes import Category10
import os
import bokeh.colors.named as bcn
from itertools import cycle
from functools import partial
from bokeh.transform import factor_cmap
from bokeh.models import (
    CrosshairTool,
    CustomJS,
    CustomJSTickFormatter,
    CustomJSTicker,
    FixedTicker,
    NoOverlap,
    ColumnDataSource,
    NumeralTickFormatter,
    Span,
    HoverTool,
    Range1d,
    WheelZoomTool,
    LabelSet,
    Legend,
    Band,
)
from bokeh.events import MouseLeave
from bokeh.layouts import column
from bokeh.io import show
from bokeh.plotting import figure as _figure
from bokeh.models.glyphs import VBar
setattr(_figure, '_main_ohlc', False)
setattr(_figure, '_candles', False)
warnings.filterwarnings("ignore", category=UserWarning)
reset_output()
if TYPE_CHECKING:
    from indicators import KLine, Line, IndSeries, IndFrame
# 检测是否在 Jupyter Notebook/Lab 中运行（新版 Jupyter 不再设置 JPY_INTERRUPT_EVENT）
try:
    from IPython import get_ipython
    _shell = get_ipython()
    IS_JUPYTER_NOTEBOOK = _shell is not None and _shell.__class__.__name__ == 'ZMQInteractiveShell'
except (ImportError, AttributeError):
    IS_JUPYTER_NOTEBOOK = False
panel_CUSTOM_CSS = """
:host {
    height: 100%;
}
.bk-Column {
    display: flex;
    flex-direction: column;
    height: 100vh;
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

#: 全屏页面样式：根节点边距归零、禁止滚动条（`btplot.py` / `strategy/bokeh_plot.py` 共用）
FULLSCREEN_PAGE_CSS = """
html, body { height: 100%; margin: 0; padding: 0; overflow: hidden; }
.bk-root { height: 100% !important; padding: 0 !important; margin: 0 !important; }
"""


def fullscreen_figure_css(weight: int) -> str:
    """单张图在全屏下的高度权重（主图 4、资金曲线 2、副图 1）。

    `panel_CUSTOM_CSS` 里的 `.bk-Column > .xxx` 选不中（bokeh 3.x 布局在 shadow
    DOM 里），所以每张图用**自己的** stylesheet 写 `:host { flex }`。
    """
    return (":host { flex: %d 1 0 !important; height: auto !important; "
            "min-height: 28px !important; }" % weight)

#: bokeh 的 tooltip 是挂在 <body> 下的 ``.bk-Tooltip``（CSS 变量控制深/浅色），
#: 与 ``strategy/bokeh_plot.py`` 保持一致，见那里的说明。
DARK_TOOLTIP_CSS = """
.bk-Tooltip {
    --tooltip-color: #1a1a1a !important;
    --tooltip-text: #e6e6e6 !important;
    --tooltip-border: #555555 !important;
}
.bk-Tooltip .bk-tooltip-row-label { color: #4dabf7; }
"""
LIGHT_TOOLTIP_CSS = """
.bk-Tooltip {
    --tooltip-color: #ffffff !important;
    --tooltip-text: #18191d !important;
    --tooltip-border: #cccccc !important;
}
"""
TOOLTIP_PIN_JS = """
(function () {
  if (window.__minibt_tip_pin__) { return; }
  window.__minibt_tip_pin__ = true;
  var plot = null;
  function isPlot(node) {
    if (!node || !node.classList) { return false; }
    return node.classList.contains('bk-Figure') ||
      node.classList.contains('bk-plot') ||
      node.classList.contains('bk-plot-wrapper');
  }
  function findPlot(e) {
    var path = (e.composedPath && e.composedPath()) || [];
    for (var i = 0; i < path.length; i++) {
      if (isPlot(path[i])) { return path[i]; }
    }
    var node = e.target;
    while (node && node !== document) {
      if (isPlot(node)) { return node; }
      node = node.parentElement;
    }
    return null;
  }
  function pin() {
    if (!plot) { return; }
    var tips = document.querySelectorAll('.bk-Tooltip');
    for (var i = 0; i < tips.length; i++) {
      var tip = tips[i];
      var cs = getComputedStyle(tip);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') { continue; }
      var r = plot.getBoundingClientRect();
      tip.style.setProperty('left', (r.left + 10) + 'px', 'important');
      tip.style.setProperty('top', (r.top + 10) + 'px', 'important');
      tip.style.setProperty('transform', 'none', 'important');
    }
  }
  document.addEventListener('mousemove', function (e) {
    var p = findPlot(e);
    if (p) { plot = p; }
    requestAnimationFrame(pin);
  }, true);
})();
"""

#: 每张图左上角的“指标数值标签”（像 pylightcharts）：顶部一行是 K 线字段，
#: 下面每行一条指标线（色块 + 名称 + 当前值）。挂到每张图自己的 shadow root 里，
#: 由 hover 回调 ``OVERLAY_UPDATE_JS`` 更新。
OVERLAY_SETUP_JS = r"""
(function () {
  if (window.__minibt_overlay_js__) { return; }
  window.__minibt_overlay_js__ = true;
  window.__minibt_fig_index__ = window.__minibt_fig_index__ || {};
  function buildOverlay(v) {
    var m = v.model, el = v.el;
    if (!m || !el || !el.classList) { return; }
    var root = el.shadowRoot || el;
    if (root.querySelector && root.querySelector('.minibt-ol')) { return; }
    var rows = [], seen = {};
    var isCandle = /candle-chart/.test(el.className || '');
    var hasOHLC = false;
    for (var j = 0; j < m.renderers.length; j++) {
      var r = m.renderers[j], g = r.glyph, d = r.data_source && r.data_source.data;
      if (!g) { continue; }
      if (d && d.open && d.high && d.low && d.close && d.volume) { hasOHLC = true; }
      var field = null;
      if (g.type === 'Line' && g.y && g.y.field) { field = g.y.field; }
      else if (g.type === 'VBar') {
        // 柱状指标（如 MACD 柱）：top/bottom 里一个是零轴
        // （'zeros' / '*_base'），真正的数值列是另一个
        var tf = g.top && g.top.field ? String(g.top.field) : null;
        var bf = g.bottom && g.bottom.field ? String(g.bottom.field) : null;
        var isBase = function (x) {
          return !x || x === 'zeros' || x === 'zero' || /_base$/.test(x);
        };
        field = !isBase(tf) ? tf : bf;
      }
      if (!field || seen[field]) { continue; }
      if (field === 'open' || field === 'high' || field === 'low' ||
          field === 'close' || field === 'volume' ||
          field.indexOf('vol_ov') === 0 || /(^|_)zeros?$/.test(field)) { continue; }
      seen[field] = 1;
      var color = (g.line_color && g.line_color.value) ||
                  (g.fill_color && g.fill_color.value) || '#8892a6';
      rows.push({kind: 'line', label: field, field: field, color: color,
                 renderer: r});
    }
    if (isCandle && hasOHLC) {
      rows.unshift({kind: 'kline',
                    fields: ['datetime', 'open', 'high', 'low', 'close', 'volume']});
    }
    // 即使没有指标行也把 overlay/标签建出来（底部图可能没行但要显示时间标签）
    var ol = document.createElement('div');
    ol.className = 'minibt-ol';
    // K 线信息行与各指标标签用同一字号（原来是 11px，偏小；改成 12px）
    ol.style.cssText = 'position:absolute;left:4px;top:3px;z-index:30;' +
      'font:12px/1.5 monospace;pointer-events:none;white-space:nowrap;';
    ol.style.color = window.__minibt_dark__ ? '#e6e6e6' : '#18191d';
    var vals = [];
    for (var k = 0; k < rows.length; k++) {
      var rd = document.createElement('div');
      if (rows[k].kind === 'line') {
        var sw = document.createElement('span');
        sw.textContent = '\u25AC';
        sw.style.color = rows[k].color;
        rd.appendChild(sw);
        var nm = document.createElement('span');
        nm.textContent = ' ' + rows[k].label + ': ';
        rd.appendChild(nm);
        // 点指标行 = 隐藏/显示对应指标线（K 线行不参与）
        rd.style.pointerEvents = 'auto';
        rd.style.cursor = 'pointer';
        rd.title = '点击隐藏 / 显示该指标';
        (function (renderer, rowEl) {
          rowEl.addEventListener('click', function (ev) {
            ev.stopPropagation();
            renderer.visible = !renderer.visible;
            rowEl.style.opacity = renderer.visible ? '1' : '0.4';
          });
        })(rows[k].renderer, rd);
      }
      var vv = document.createElement('span');
      vv.textContent = '';
      rd.appendChild(vv);
      vals.push(vv);
      ol.appendChild(rd);
    }
    el.style.position = 'relative';
    root.appendChild(ol);
    // 跟踪标签：底部 X 轴的时间、右侧 Y 轴的数值
    var _bg, _fg;
    if (window.__minibt_accent__) {
      _bg = window.__minibt_accent__; _fg = '#ffffff';
    } else if (window.__minibt_dark__) {
      _bg = '#ffffff'; _fg = '#000000';
    } else {
      _bg = '#000000'; _fg = '#ffffff';
    }
    function _mk(cls, extra) {
      var d = document.createElement('div');
      d.className = cls;
      d.style.cssText = 'position:absolute;display:none;z-index:31;' +
        'font:12px monospace;padding:0 4px;border-radius:2px;' +
        'pointer-events:none;white-space:nowrap;' + extra;
      d.style.background = _bg; d.style.color = _fg;
      root.appendChild(d);
      return d;
    }
    var xlab = _mk('minibt-xlabel', 'transform:translateX(-50%);');
    var ylab = _mk('minibt-ylabel', 'transform:translateY(-50%);');
    window.__minibt_fig_index__[m.id] = {
      ol: ol, rows: rows, vals: vals, el: el, xlab: xlab, ylab: ylab};
  }
  window.__minibt_mouse__ = window.__minibt_mouse__ || {x: 0, y: 0};
  document.addEventListener('mousemove', function (e) {
    window.__minibt_mouse__.x = e.clientX;
    window.__minibt_mouse__.y = e.clientY;
  }, true);
  function visit(v) {
    if (!v || !v.model || !v.model.type) { return; }
    if (v.model.type === 'Figure') {
      buildOverlay(v);
      if (!window.__minibt_all_figs__) { window.__minibt_all_figs__ = []; }
      if (window.__minibt_all_figs__.indexOf(v.model) < 0) {
        window.__minibt_all_figs__.push(v.model);
      }
    }
    if (v.model.type === 'Tabs') {
      if (!window.__minibt_all_tabs__) { window.__minibt_all_tabs__ = []; }
      if (window.__minibt_all_tabs__.indexOf(v.model) < 0) {
        window.__minibt_all_tabs__.push(v.model);
      }
    }
    var cv = v.child_views;
    if (cv && cv.forEach) { cv.forEach(visit); }
    var vs = v.views;
    if (vs && vs.forEach) { vs.forEach(visit); }
  }
  function ready() {
    if (!window.Bokeh || !Bokeh.index) { return setTimeout(ready, 200); }
    for (var id in Bokeh.index) { visit(Bokeh.index[id]); }
    setTimeout(ready, 1000);   // 重新扫（切换 tab/新建图时补建）
  }
  ready();
})();
"""

#: 深色/浅色主题切换（客户端改 bokeh 模型颜色；与 bokeh 回测图一致）。
#: 需要 `OVERLAY_SETUP_JS` 先收集了 `__minibt_all_figs__` / `__minibt_all_tabs__`。
THEME_TOGGLE_JS = r"""
(function () {
  if (window.__minibt_theme_js__) { return; }
  window.__minibt_theme_js__ = true;
  function eachAxis(fig, fn) {
    ['below', 'left', 'right', 'above'].forEach(function (side) {
      var arr = fig[side] || [];
      for (var i = 0; i < arr.length; i++) {
        var r = arr[i];
        if (r && r.type && /Axis/.test(r.type)) { fn(r); }
      }
    });
  }
  function styleFig(fig, dark) {
    try {
      fig.background_fill_color = dark ? '#1a1a1a' : '#ffffff';
      fig.border_fill_color = dark ? '#1a1a1a' : '#ffffff';
      fig.outline_line_color = dark ? '#404040' : '#cccccc';
      eachAxis(fig, function (ax) {
        ax.major_label_text_color = dark ? '#cccccc' : '#333333';
        ax.axis_line_color = dark ? '#666666' : '#aaaaaa';
        ax.major_tick_line_color = dark ? '#666666' : '#aaaaaa';
      });
      (fig.center || []).forEach(function (r) {
        if (r && r.type && String(r.type).indexOf('Grid') >= 0 && r.grid_line_color) {
          r.grid_line_color = dark ? '#333333' : '#e5e5e5';
        }
      });
    } catch (e) {}
  }
  window.__minibt_toggle_theme__ = function () {
    window.__minibt_dark__ = !window.__minibt_dark__;
    var dark = window.__minibt_dark__;
    var figs = window.__minibt_all_figs__ || [];
    for (var i = 0; i < figs.length; i++) { styleFig(figs[i], dark); }
    (window.__minibt_all_tabs__ || []).forEach(function (t) {
      try { t.css_classes = dark ? ['dark-tabs'] : []; } catch (e) {}
      try { t.background = dark ? '#000000' : '#ffffff'; } catch (e) {}
    });
    document.body.style.background = dark ? '#000' : '#fff';
    document.body.style.color = dark ? '#ddd' : '#333';
  };
})();
"""

#: hover 回调：按当前鼠标位置刷新对应的数值标签，并把十字线联动到所有图
#: （``fig_id`` / ``spans`` 由 CustomJS args 传入）
OVERLAY_UPDATE_JS = r"""
(function () {
  // 联动十字线：把同一合约的每张图的竖直 Span 都移到当前 x
  var xx = cb_data && cb_data.geometry ? cb_data.geometry.x : null;
  if (xx !== null && typeof spans !== 'undefined' && spans) {
    for (var si = 0; si < spans.length; si++) {
      spans[si].location = xx;
      spans[si].visible = true;
    }
  }
  var e = window.__minibt_fig_index__ && window.__minibt_fig_index__[fig_id];
  if (!e || !e.rows) { return; }
  var d = cb_data && cb_data.renderer && cb_data.renderer.data_source &&
          cb_data.renderer.data_source.data;
  if (!d || !d['index']) { return; }
  var n = d['index'].length;
  if (!n) { return; }
  var x = cb_data.geometry ? cb_data.geometry.x : 0;
  var i = Math.round(x);
  if (i < 0) { i = 0; }
  if (i >= n) { i = n - 1; }
  function fmt(val) {
    if (val === null || val === undefined || (typeof val === 'number' && isNaN(val))) {
      return '--';
    }
    return (typeof val === 'number') ? val.toFixed(2) : String(val);
  }
  for (var k = 0; k < e.rows.length; k++) {
    var row = e.rows[k], span = e.vals[k];
    if (row.kind === 'kline') {
      var dt = d['datetime'] ? d['datetime'][i] : null;
      var ds = (dt === null || dt === undefined) ? '' :
        new Date(Number(dt)).toLocaleString();
      // 最前面补上鼠标的 X / Y 坐标
      var gk = cb_data.geometry;
      var yv = (gk && gk.y !== null && gk.y !== undefined) ? gk.y : null;
      span.textContent =
        'X ' + i + '  Y ' + ((yv === null) ? '--' :
          (typeof yv === 'number' ? yv.toFixed(2) : yv)) + '  ' +
        ds + '  O ' + fmt(d['open'][i]) + '  H ' + fmt(d['high'][i]) +
        '  L ' + fmt(d['low'][i]) + '  C ' + fmt(d['close'][i]) +
        '  V ' + fmt(d['volume'][i]);
    } else {
      var col = d[row.field];
      span.textContent = fmt(col ? col[i] : null);
    }
  }
  // 时间标签固定在**最后一张图**底部；数值标签跟着鼠标所在图
  try {
    var g = cb_data.geometry;
    var mo = window.__minibt_mouse__ || {x: 0, y: 0};
    var ex = (typeof xfig_id !== 'undefined' && xfig_id &&
              window.__minibt_fig_index__[xfig_id]) || e;
    var xr = ex.el ? ex.el.getBoundingClientRect() : null;
    if (xr && ex.xlab && g && g.x !== null && g.x !== undefined) {
      var t = d['datetime'] ? d['datetime'][i] : null;
      ex.xlab.textContent = (t === null || t === undefined) ? '' :
        new Date(Number(t)).toLocaleString();
      ex.xlab.style.left = (mo.x - xr.left) + 'px';
      ex.xlab.style.top = (xr.height - 20) + 'px';
      ex.xlab.style.display = 'block';
    }
    var rect = e.el ? e.el.getBoundingClientRect() : null;
    if (rect && e.ylab && g && g.y !== null && g.y !== undefined) {
      e.ylab.textContent = (typeof g.y === 'number') ? g.y.toFixed(2) : String(g.y);
      e.ylab.style.top = (mo.y - rect.top) + 'px';
      e.ylab.style.right = '0px';
      e.ylab.style.display = 'block';
    }
  } catch (err) {}
})();
"""

#: X 轴按 index 取 datetime，格式化为时间刻度（``dt`` 是数据源的 datetime 列）
_TIME_TICK_JS = r"""
const idx = Math.round(tick);
const t = dt[idx];
if (t === undefined || t === null) { return ''; }
let ms = Number(t);
if (ms < 1e12) { ms *= 1000; }
const d = new Date(ms);
const p = (n) => (n < 10 ? '0' + n : '' + n);
return d.getFullYear() + '/' + p(d.getMonth() + 1) + '/' + p(d.getDate()) +
       ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
"""

#: 按**可见窗口**动态生成时间刻度（配合 ``CustomJSTicker.major_code``）。
#: 以前用 ``FixedTicker(range(0, N, 10))``（1 万根 = 1000 个刻度），缩放后要么
#: 标签重叠、要么被 ``NoOverlap`` 抽得太少；改成按 ``cb_data.start/end`` 选步长，
#: 可见范围内始终给出 ~8 个不重叠的刻度。
_TIME_TICKER_JS = r"""
const s = (cb_data && cb_data.start != null) ? cb_data.start : 0;
const e = (cb_data && cb_data.end != null) ? cb_data.end : s + 1;
const lo = Math.round(Math.min(s, e));
const hi = Math.round(Math.max(s, e));
const span = Math.max(1, hi - lo);
const targets = [1, 2, 5, 10, 20, 50, 100, 200, 500,
                 1000, 2000, 5000, 10000, 20000];
let step = targets[targets.length - 1];
for (let i = 0; i < targets.length; i++) {
    if (span / targets[i] <= 8) { step = targets[i]; break; }
}
const out = [];
for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) { out.push(v); }
return out;
"""


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
    # 数值改到左上角标签（OVERLAY_*）：主图/指标 hover 不再弹 tooltip，
    # 但**交易信号**（vline=False）的 tooltip 保留。
    hover = HoverTool(
        point_policy='follow_mouse',
        renderers=renderers, formatters=formatters,
        tooltips=None if vline else tooltips,
        mode='vline' if vline else 'mouse')
    hover.name = 'minibt_overlay'   # 之后统一挂 fig_id/spans/xfig_id 回调
    fig.add_tools(hover)


def new_bokeh_figure(height=300) -> Callable:
    return partial(
        _figure,
        x_axis_type='linear',
        height=height,
        sizing_mode='stretch_width',
        width_policy='max',
        y_axis_location='right',       # 所有 Y 轴放右侧
        tools="xpan,xwheel_zoom,box_zoom,undo,redo,reset,save",  # ,crosshair
        active_drag='xpan',
        active_scroll='xwheel_zoom')


def new_indicator_figure(new_bokeh_figure: Callable, fig_ohlc: _figure, height=80, **kwargs) -> _figure:
    height = int(height) if height and height > 10 else 80
    fig = new_bokeh_figure(height)(x_range=fig_ohlc.x_range,
                                   active_scroll='xwheel_zoom',
                                   active_drag='xpan',
                                   **kwargs)
    fig.xaxis.visible = False
    fig.yaxis.minor_tick_line_color = None
    return fig


def search_index(ls: list[list], name) -> tuple[int] | None:
    for i, ls1 in enumerate(ls):
        for j, ls2 in enumerate(ls1):
            if ls2 == name:
                return i, j
    return None


def colorgen():
    yield from cycle(Category10[10])


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
    某行存在 NaN(例如 QQEMod 的 vbar 信号柱在无信号处恒为 NaN)整行极值
    即为 NaN, 前端会把副图 y_range 设成 NaN, 表现为缩放/拖动后副图空白,
    点击图例切换显隐后又恢复。
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
    #   极值(见 indicator_h/indicator_l 只统计 isplot=True 的列) -> 柱子直接落到
    #   可视范围之外 -> 用户看到的是“只有指标线, 右端根本没有 VP 图”。
    #   修法: 档位数在 Python 侧就是已知的, 故直接给一根**固定 Range1d**
    #   (-0.5 .. nb-0.5) 挂到 extra_y_ranges, hbar 用 y_range_name 指过去 ——
    #   不需要任何 JS 镜像(这也正是之前“A 方案”失败的原因: 那次要去镜像主图的
    #   DataRange1d, 而这次是自建的固定 range), autoscale 也只改 fig.y_range,
    #   不会碰 extra_y_ranges。
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


class _BtplotStrategy:
    """``btplot(gui='pylightcharts')`` 用的临时策略壳。

    把 `btplot` 拿到的 **K 线 + 指标**原样塞进“策略”接口
    （``_btklinedataset`` / ``_btindicatordataset`` / ``config``），
    直接复用 `RealtimeChart` → `StrategyWindow` 那套**已经配好**的顶栏 /
    页面 / 主题 / 网格 / 工具栏，不再自己重建图表。
    """
    _strategy_replay = False
    _is_live_trading = False
    _isoptimize = False
    rl = False
    min_start_length = 0

    def __init__(self, kline, indicator, indicators, config, name):
        self.sname = name
        self.config = config
        key = (f'{getattr(kline, "symbol", "k")}_'
               f'{getattr(kline, "cycle", "0")}')
        self._btklinedataset = {key: kline}
        self._btindicatordataset = {
            f'{getattr(ind, "ind_name", "ind")}_{i}': ind
            for i, ind in enumerate(indicators)}

    @property
    def theme(self) -> str:
        return getattr(self.config, 'theme', 'dark')

    def _get_plot_datas(self):
        return None


def _find_btplot_kline(indicator, df, indicators):
    """从 `btplot` 的参数里找 K 线对象（优先 `df`，其次指标的源链）。"""
    if hasattr(df, 'symbol'):
        return df
    for ind in [indicator] + list(indicators):
        src = getattr(ind, '_dataset', None)
        src = getattr(src, 'source_object', None) if src is not None else None
        if src is not None and hasattr(src, 'symbol'):
            return src
    return None


def _reset_kline_stop_lines(kline) -> None:
    """清掉 K 线上**上一次回测**留下的止损/止盈线。

    K 线（``LocalDatas.xxx.kline``）在同一个 notebook / 进程里是**共享对象**：
    ``btplot`` 看某个带 ``step`` 的指标时跑过一次回测，止损线就存进了
    ``kline._klinesetting.stop_lines``；下一次用**同一个 K 线**看别的指标时，
    ``extract_minibt_indicators`` 的 ``_extract_stop_line_specs`` 会把它当成
    **这次**的止损线画出来 —— ``stop_price`` / ``target_price`` 泄漏到别的指标
    图上 ✗。这里在出图前清空；只有本次指标自己带 ``step`` 跑回测时才会重新
    生成 ✓。
    """
    reset = getattr(kline, 'reset_stop_lines', None)
    if callable(reset):
        reset()
        return
    setting = getattr(kline, '_klinesetting', None)
    if setting is None:
        return
    setting.stop_lines = None
    # 回测状态也一起清：否则上一次的 `stop` / `isstop` 会让这次回测以为已经停了
    setting.stop = None
    setting.isstop = False


def _make_step_strategy(kline, indicators, config, name, trading=None):
    """建一个**真正的** `Strategy`：用传入的 K 线 + 带 `step` 的指标跑一遍回测。

    对应 `tutorials/indicator_step.py`，但要跑出 `profits` / `trade_history`
    才能让「回测结果 / 指标记录」两页有数据。

    ⚠ 关键：指标必须**作为属性赋值**（``setattr``）才能让 `Strategy.__setattr__`
    登记到 ``_btindicatordataset``；只调 ``ind.step()`` 不赋值的话，图表端拿不到
    任何指标，表现为 pylightcharts 模式下**指标与信号一片空白**。

    ``indicators`` 为全部要**上图**的指标，``trading`` 为其中要在 ``next()`` 里
    调 ``step()`` 的（默认同 ``indicators``）。
    """
    from .strategy.strategy import Strategy

    all_inds: list = []
    for _ind in indicators:                 # 按对象去重（同一个指标不能登记两次）
        if not any(_ind is _x for _x in all_inds):
            all_inds.append(_ind)
    trading_inds = list(all_inds if trading is None else trading)

    class _BtplotStep(Strategy):
        def __init__(self):
            self.sname = name
            self.kline = kline                 # 登记 K 线
            self._btplot_inds = []
            used: set = set()
            for i, ind in enumerate(all_inds):
                iname = getattr(ind, 'ind_name', None) or f'btplot_ind_{i}'
                if iname in used:              # 同名指标加后缀，避免相互覆盖
                    iname = f'{iname}_{i}'
                used.add(iname)
                setattr(self, iname, ind)      # ★ 触发登记到 _btindicatordataset
            self._btplot_inds = trading_inds

        def next(self):
            for ind in self._btplot_inds:
                ind.step()

    _BtplotStep.config = config   # 类体内不能写 `config = config`（会遮蔽外层名）
    # ★ `_sid` 默认 0，而 `strategy_instances[sid]` 是**按注册顺序**取值的：
    #   之前跑过任何 `_sid=0` 的策略（如测试里的 `S`、或上一次 btplot 回测）时，
    #   新建的 btplot 壳会与它撞号 —— 指标的 `strategy_instance` 会指回旧策略，
    #   `btindex` 停在旧策略的最后一根（跑到 10 000 根的 `pp2609_60` 后再跑
    #   8 964 根的 `test` 就 `IndexError` / 图上指标一片空白 ✗）。
    #   用当前注册数量当 `_sid`，保证 `strategy_instances[sid] is self` ✓。
    from .utils import Base
    try:
        sid = len(Base._strategy_instances)
    except Exception:                                            # noqa: BLE001
        sid = 0
    return _BtplotStep(_sid=sid)


_BTP_GUI_ALIASES = {
    'pylightcharts': 'pylightcharts',
    'light_chart': 'pylightcharts',
    'pylightcharts_web': 'pylightcharts_web',
    'pylightchartsweb': 'pylightcharts_web',
    'browser': 'pylightcharts_web',
    'jupyter': 'jupyter',
    'notebook': 'jupyter',
    'bokeh': 'bokeh',
}


def _resolve_btplot_gui(gui: str | None = None) -> str:
    """归一化 ``btplot`` 的 ``gui``（**默认 pylightcharts**）。

    * ``None`` / 空串 / ``'pylightcharts'`` / ``'light_chart'`` → ``'pylightcharts'``
    * ``'pylightcharts_web'`` / ``'browser'`` → ``'pylightcharts_web'``（浏览器）
    * ``'jupyter'`` / ``'notebook'`` → ``'jupyter'``（Notebook 内联）
    * ``'bokeh'`` → ``'bokeh'``（**只有显式指定才走 bokeh**）

    未知取值直接报错，避免"传错了却悄悄用另一个引擎"。
    """
    value = getattr(gui, 'value', gui)     # Gui.Pylightcharts -> 'pylightcharts'
    if value is None or not str(value).strip():
        return 'pylightcharts'
    key = str(value).strip().lower()
    if key not in _BTP_GUI_ALIASES:
        raise ValueError(
            f"未知的 btplot gui={gui!r}；可选 'pylightcharts'（默认）/ "
            "'bokeh' / 'pylightcharts_web'（= 'browser'）/ 'jupyter'")
    return _BTP_GUI_ALIASES[key]


def _btplot_pylightcharts(indicator, df, indicators, extra_klines, *,
                          black_style: bool, grid: bool, toolbox: bool,
                          browser: bool, inline: bool = False,
                          info_pages: bool = False,
                          y_margin: float | tuple | None = None,
                          **kwargs) -> None:
    """用 pylightcharts 看 `btplot` 的图（``gui='pylightcharts'``）。

    做法：把 K 线 + 指标塞进一个**临时策略**，然后走
    `RealtimeChart` / `StrategyWindow`（顶栏 / 页面 / 主题 / 网格 / 工具栏都已配好）。
    """
    from .utils import Config
    from .strategy.realtime import RealtimeChart
    kline = _find_btplot_kline(indicator, df, indicators)
    if kline is None:
        print('[btplot] pylightcharts 模式找不到 K 线对象，跳过')
        return
    # ★ K 线是共享对象：先清掉上一次回测留下的 stop_price/target_price，
    #   否则看别的指标时会把它们当成本次的止损线画出来（见函数说明）✓
    _reset_kline_stop_lines(kline)
    name = getattr(indicator, 'ind_name', None) or 'indicator'
    cfg = Config(theme='dark' if black_style else 'light',
                 grid=bool(grid), toolbox=bool(toolbox),
                 bokeh_fullscreen=False)
    # 指标**自身定义了 step**（交易逻辑，见 tutorials/indicator_step.py）→ 当回测：
    # 跑一遍回测（拿到 profits / trade_history）+ 保留图表顶栏 + 两个信息页；
    # 否则是纯图表：两个顶栏都不要，只显示 K 线页。
    # 注意：`IndicatorsBase.step` 是不做事的基类，只有实例 `__dict__` 里有 step
    # 才是指标自己定义的（`BtIndicator.__new__` 里 `setattr(indicator, 'step', ...)`）。
    # btplot 是**静态快速出图**：
    #   inline（display='auto' 且 Jupyter / 'inline'）→ Jupyter 内联；
    #   否则外部打开：web gui → 浏览器；native gui → pylightcharts 窗口
    #   （Jupyter 里原生窗口会 asyncio 报错，则回退浏览器）。
    _jupyter = bool(IS_JUPYTER_NOTEBOOK)
    if inline:
        browser, jupyter = False, True
    else:
        jupyter = False
        if not browser and _jupyter:
            browser = True
    # btplot 内部已从指标往上收集了源链（`indicators` 含 `indicator` 本身），
    # 这里按对象去重，避免同一指标被登记两次（图上画两遗）。
    _chain = []
    for _ind in [indicator] + list(indicators):
        if not any(_ind is _x for _x in _chain):
            _chain.append(_ind)
    trading = [ind for ind in _chain if 'step' in getattr(ind, '__dict__', {})]
    has_trade = bool(trading)
    if has_trade:
        strat = _make_step_strategy(kline, _chain, cfg, name, trading=trading)
        try:
            strat._start_strategy_run()
        except Exception as error:
            print('[btplot] pylightcharts 回测失败:', error)
    else:
        strat = _BtplotStrategy(kline, indicator, list(indicators), cfg, name)
    bars = 'chart' if has_trade else 'none'
    chart = RealtimeChart([strat], browser=browser, jupyter=jupyter,
                          title=f'pylightcharts - 指标图：{name}',
                          info_pages=bool(has_trade or info_pages),
                          realtime=False, theme=cfg.theme, bars=bars,
                          live=False, y_margin=y_margin)
    if has_trade:
        try:
            chart.set_backtest_from_strategy(strat)
        except Exception as error:
            print('[btplot] pylightcharts 登记回测结果失败:', error)
    # 脚本里必须**阻塞**到窗口关闭：原生窗口由子进程渲染，子进程随父进程一起
    # 结束 —— 立刻返回会让界面"一闪而过"（Jupyter 内联 / 网页版不需要等待）。
    if inline or browser:
        chart.show(block=False)
        return
    try:
        chart.show(block=True)
    except KeyboardInterrupt:
        print('[btplot] 已停止（Ctrl+C），图表窗口已关闭')


def _apply_legend(kwargs: dict, visible: bool, label: str) -> dict:
    """按指标级 `legend` 开关决定要不要给 bokeh 传 `legend_label` ✓。

    ⚠ bokeh 要求 `legend_label` **必须是字符串** ✗（传 `None` 会
    `ValueError: legend_label value must be a string` ✗），
    所以「不显示图例」的正确做法是**整个键都不传** ✓ —— 这正是本函数做的事。
    """
    if visible:
        kwargs['legend_label'] = label
    else:
        kwargs.pop('legend_label', None)
    return kwargs


def btplot(indicator: KLine | Line | IndSeries | IndFrame,
           include: Literal["all", "last"] = "all",
           black_style: bool | None = None,
           open_browser: bool = False,
           plot_cwd: str = "",
           plot_name: str = "",
           save_plot: bool = False,
           signal_profit_result: dict | None = None,
           volume_mode: str = 'overlay',
           theme: str | None = None,
           toolbox: bool | None = None,
           grid: bool | None = None,
           y_margin: float | tuple | None = None,
           gui: str | None = None,
           display: str | None = None,
           **kwargs) -> None:
    """
    Like much of GUI code everywhere, this is a mess.

    ``theme``：``'light'``（默认）/ ``'dark'``，与 pylightcharts 图表同名同义；
    旧的 ``black_style`` 仍兼容（给了它就优先，视为 ``theme`` 的布尔写法）；
    默认 ``theme=None`` 表示"未设置"，按库默认（浅色）走，也跟随指标所属策略
    （``self.theme`` / ``config.theme``，二者默认也是浅色）；VS Code 扩展会改用
    VS Code 的深浅色主题。

    ``gui``：**默认就是 pylightcharts**（与 ``Bt.run()`` 的回测/回放/实时一致）；
    只有显式传 ``'bokeh'`` 才走 bokeh。可用取值：
      * ``None`` / ``'pylightcharts'``（= ``'light_chart'``）：pylightcharts 原生窗口；
      * ``'pylightcharts_web'``（= ``'browser'``）：浏览器（本地 SSE）；
      * ``'jupyter'``（= ``'notebook'``）：Notebook 内联（不阻塞 kernel）；
      * ``'bokeh'``：bokeh 交互图 —— ``save_plot`` / ``plot_name`` / ``plot_cwd``
        **只对它生效**（用 pylightcharts 时传了会打印一条提示）。

    ``grid`` / ``toolbox``：网格与工具栏/工具，bokeh 与 pylightcharts 共用；
    ``None`` 时回退到指标所属策略的 ``config.grid`` / ``config.toolbox``（默认 False）。

    ``y_margin``：价格轴上下留白（`scaleMargins`，**面板高度**的比例），只作用于
    `gui='pylightcharts'` / `'pylightcharts_web'`；``None``（默认）用
    `minibt.utils.DEFAULT_Y_MARGIN`（0.03），``(0.2, 0.1)`` 回到
    lightweight-charts 原样，``0.0`` 铺满。

    ``display``（推荐，优先于 ``open_browser``）：
      * ``'auto'``（默认）：Jupyter 里**内联**显示，脚本里**外部**打开；
      * ``'inline'``：强制内联（Jupyter）；
      * ``'external'``：强制外部打开（浏览器 / pylightcharts 窗口）。
    旧的 ``open_browser`` 仍兼容：``True`` = ``'external'``，``False`` = ``'auto'``。
    """
    global IS_JUPYTER_NOTEBOOK
    # —— 解析 display / open_browser（open_browser 兼容旧写法）——
    if display is None:
        display = 'external' if open_browser else 'auto'
    display = str(display).strip().lower()
    _jupyter = bool(IS_JUPYTER_NOTEBOOK)
    _inline = (display in ('inline', 'notebook')
               or (display == 'auto' and _jupyter))
    _external = not _inline
    open_browser = _external          # 兼容下面的 bokeh 分支
    dtype = [_type in type(indicator).__name__ for _type in [
        "KLine", "Line", "IndSeries", "IndFrame"]]
    assert any(dtype), print(f"只支持minibt框架内置指标数据图表,指标数据格式为{type(indicator)}")
    from .utils import (resolve_theme, resolve_grid, resolve_toolbox,
                        resolve_fullscreen)
    black_style = resolve_theme(theme, black_style, indicator) == 'dark'
    _strategy = getattr(indicator, 'strategy_instance', None)
    # grid / toolbox：显式参数优先，None 时回退策略 config（默认 False）
    show_grid = resolve_grid(_strategy) if grid is None else bool(grid)
    show_toolbar = (resolve_toolbox(_strategy) if toolbox is None
                    else bool(toolbox))
    # 全屏：config.bokeh_fullscreen 为 None 时 Jupyter→False、浏览器→True
    fullscreen = resolve_fullscreen(
        _strategy,
        jupyter=bool(IS_JUPYTER_NOTEBOOK) and not open_browser)
    base_dir = indicator._base_dir
    black_color = "white" if black_style else "black"
    white_color = "black" if black_style else "white"
    COLORS = [bcn.tomato, bcn.lime]
    inc_cmap = factor_cmap('inc', COLORS, ['0', '1'])
    new_colors = {'bear': bcn.tomato, 'bull': bcn.lime}
    BAR_WIDTH = .8  # K宽度
    NBSP = '\N{NBSP}' * 4
    lines_setting = dict(line_dash='solid', line_width=1.3)
    with open(f'{base_dir}/strategy/autoscale_cb.js', encoding='utf-8') as _f:
        _AUTOSCALE_JS_CALLBACK = _f.read()
    indicators: list[KLine, Line, IndSeries, IndFrame] = []
    is_kline = dtype[0]
    df = None
    if is_kline:
        df = indicator
    else:
        indicators.append(indicator)
    cond = True
    if indicators:
        while cond and len(indicators) < 100:
            try:
                src = indicators[-1]._dataset.source_object
                if src is None:
                    cond = False
                    break
                if "KLine" in type(src).__name__:
                    df = src
                    break
                else:
                    indicators.append(src)
            except Exception:
                cond = False
                break

        if include == "last":
            indicators = indicators[:1]

    if kwargs:
        indicator = indicator(**kwargs)

    extra_klines: list[pd.DataFrame] = kwargs.get("kline", [])
    if isinstance(extra_klines, pd.DataFrame):
        extra_klines = [extra_klines,]

    if df is not None:
        extra_klines.insert(0, df)

    extra_klines = [k for k in extra_klines if isinstance(k, pd.DataFrame) and all(col in k.columns for col in [
        'open', 'high', 'low', 'close'])]

    if not extra_klines:
        print("未找到KLine数据源，使用pandas原生plot绘图")
        print("提示：可通过 kline 参数显式指定K线数据，如 btplot(indicator, kline=kline_data)")
        indicator.pandas_object.plot()
        return
    df = extra_klines[0]
    extra_klines = extra_klines[1:]

    # ---- 默认走 pylightcharts 看图模式；只有显式 gui='bokeh' 才用 bokeh ----
    _gui = _resolve_btplot_gui(gui)
    if _gui == 'pylightcharts' and not _inline:
        # 缺 main 保护的脚本里原生窗口起不来（spawn 会重跑入口脚本）→ 自动换网页版
        from .utils import main_guard_missing
        if main_guard_missing():
            print('[btplot] 入口脚本缺少 if __name__ == "__main__": 保护 —— '
                  'Windows/macOS 的原生窗口起不来，已自动改用浏览器模式'
                  '（gui="pylightcharts_web"）。')
            _gui = 'pylightcharts_web'
    if _gui != 'bokeh':
        if save_plot or plot_name or plot_cwd:
            print("[btplot] 提示：save_plot / plot_name / plot_cwd 只对 "
                  "gui='bokeh' 生效（当前默认引擎是 pylightcharts）。")
        return _btplot_pylightcharts(
            indicator, df, indicators, extra_klines,
            black_style=black_style, grid=show_grid,
            toolbox=show_toolbar, y_margin=y_margin,
            browser=_gui == 'pylightcharts_web',
            inline=True if _gui == 'jupyter' else _inline)

    df_columns = df.columns
    is_datetime = "datetime" in df_columns
    is_volume = "volume" in df_columns
    # [修改] 成交量已**固定叠加到主 K 线图**底部(见 ohlc_bars 之后那段),
    #   所以默认**不再单开高度 60 的独立成交量副图**(它不便于对照 K 线)。
    #   想恢复原来的独立成交量副图: 把 is_volume_pane 改成 True 即可。
    # [开关] 由 volume_mode 决定（'sub' / 'both' 时显示独立成交量副图）
    #   原代码: is_volume_pane = True      # [改回] 恢复原来的独立成交量副图
    is_volume_pane = volume_mode in ('sub', 'both')
    _df = df.copy()[FILED.ALL]
    index = _df.index
    pad = (index[-1] - index[0]) / 20
    if "volume" in df_columns:
        _df['volume5'] = _df.volume.rolling(5).mean()
        _df['volume10'] = _df.volume.rolling(10).mean()
    _df["inc"] = (_df.close >=
                  _df.open).values.astype(np.uint8).astype(str)
    _df["Low"] = _df[['low', 'high']].min(1)
    _df["High"] = _df[['low', 'high']].max(1)
    # 影线拆两条（单条 high->low 会穿过实体）：实体顶->high、low->实体底
    _df["wick_top"] = _df[['open', 'close']].max(1)
    _df["wick_bottom"] = _df[['open', 'close']].min(1)
    CDS = ColumnDataSource(_df)
    #
    # K线图
    fig_ohlc: _figure = new_bokeh_figure(df.height)(
        x_range=Range1d(index[0], index[-1],
                        min_interval=10,
                        bounds=(index[0] - pad,
                                index[-1] + pad)) if index.size > 1 else None)
    fig_ohlc.css_classes = ["candle-chart"]
    _colors = df.candle_style
    if _colors:
        _COLORS = list(_colors.values())
        inc_cmap = factor_cmap('inc', _COLORS, ['0', '1'])
    # 上下影线（两条：实体顶->high、实体底->low）
    fig_ohlc.segment('index', 'wick_top', 'index', 'high',
                     source=CDS, color=black_color)
    fig_ohlc.segment('index', 'wick_bottom', 'index', 'low',
                     source=CDS, color=black_color)
    # # 实体线
    ohlc_bars = fig_ohlc.vbar('index', BAR_WIDTH, 'open', 'close', source=CDS,
                              line_color=black_color, fill_color=inc_cmap)

    # ==================================================================
    # [新增] 成交量**固定叠加到主 K 线图**底部(不再单开高度 50 的副图)
    #   做法: 给价格图挂一个副 Y 轴 range, 量程 = 最大成交量 / VOL_FRAC,
    #   于是最高量柱恰好在底部占 VOL_FRAC 高度, 其余按比例缩在下面;
    #   副 range 映射整个绘图区高度且不受价格 y 轴缩放影响 -> 永远贴着底边
    #   (即 TradingView 的 Volume overlay 效果)。
    # ⚠ 必须给主 Y 轴限定拟合范围: DataRange1d 在前端的 auto_ranged_renderers()
    #   不区分 range 名字, 会把成交量量级(1e5~1e6)算进价格轴 -> 价格轴被撑坏。
    # [开关] 由 volume_mode 决定（'overlay' / 'both' 时开启）
    #   原代码: SHOW_VOL_OVERLAY = False   # [改回] 主图成交量叠加已停用
    SHOW_VOL_OVERLAY = volume_mode in ('overlay', 'both')
    if SHOW_VOL_OVERLAY and 'volume' in CDS.column_names:
        _vol = np.nan_to_num(np.asarray(CDS.data['volume'], dtype=np.float64))
        if _vol.size and _vol.max() > 0:
            # ★ 成交量用**自己的 CDS**:
            #   JS 刷新它时不会触碰主数据源 -> 价格 y 轴不会重新拟合 ->
            #   既没有反馈死循环(成交量不停下移), 也**不需要干预 y_range.renderers**
            #   (之前限制了拟合 -> 价格轴范围失真)。
            from bokeh.models import (ColumnDataSource as _VPCDS,
                                      CustomJS as _VPCJS)
            _F = 0.20
            # 成交量分布极偏态：线性归一化会让绝大多数柱矮到看不见；
            # 用**幂缩放**（不截断）：最高柱占主图 20%，小成交量也能看得见
            _vmax = float(_vol.max()) or 1.0
            _vratio = np.power(np.clip(_vol / _vmax, 0.0, 1.0), 0.30)
            # ★ 初始基线 = **视口下沿**（优先取当前价格 y_range, 它已含 padding）,
            #   取不到就退化为整段价格的 [min,max] 再补 5% padding。
            #   关键: 柱体因此**始终落在可视价格范围内** -> DataRange1d 自动拟合出的
            #   范围等于 K 线自身范围, 成交量**不会把价格轴撑宽**（VP 同理）。
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
                inc=list(CDS.data.get('inc', ['1'] * len(_vol))),
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
            fig_ohlc.x_range.js_on_change('start', _vol_js)
            fig_ohlc.x_range.js_on_change('end', _vol_js)
            # [改] 归一化量已经在**独立 CDS** `_vol_cds` 里, 这里**不再**往主数据源加列
            #   （原代码: CDS.add(_vol / _vmax, "vol_ov_ratio")）—— 否则 JS 每次刷新都会改动
            #   主数据源, 触发价格轴重新拟合(反馈死循环)。
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
    ]
    if is_volume:
        ohlc_tooltips.append(('Volume', '@volume{0,0}'))

    spanstyle = df.span_style
    for ohlc_span in spanstyle:
        fig_ohlc.add_layout(Span(**ohlc_span))

    # 先绘制主力外的其它K线图（作为副图蜡烛图）
    indicator_figs = []
    indicator_h: list[str] = []
    indicator_l: list[str] = []

    if extra_klines:
        for i, extra_k in enumerate(extra_klines):
            ek_columns = extra_k.columns
            if not all(col in ek_columns for col in ['open', 'high', 'low', 'close']):
                continue
            ek_name = getattr(extra_k, 'ind_name', f'kline_{i + 2}')
            prefix = ek_name + '_'

            # 将 OHLC 数据加入主 CDS（共享 index）
            ek_df = extra_k[['open', 'high', 'low', 'close']].copy()
            for col in ['open', 'high', 'low', 'close']:
                CDS.add(ek_df[col].values, prefix + col)
            # 影线拆两条（单条 high->low 会穿过实体）
            CDS.add(ek_df[['open', 'close']].max(1).values, prefix + 'wick_top')
            CDS.add(ek_df[['open', 'close']].min(1).values, prefix + 'wick_bottom')

            # 涨跌染色
            inc_vals = (ek_df.close >= ek_df.open).values.astype(
                np.uint8).astype(str)
            CDS.add(inc_vals, prefix + 'inc')
            ek_colors = getattr(extra_k, 'candle_style', None)
            ek_inc_cmap = factor_cmap(
                prefix + 'inc', list(ek_colors.values()), ['0', '1']) if ek_colors else inc_cmap

            # 创建副图（共享 x_range）
            ek_height = getattr(extra_k, 'height', 150)
            fig_ek = new_indicator_figure(
                new_bokeh_figure, fig_ohlc, ek_height)

            # 上下影线（两条：实体顶->high、实体底->low）
            fig_ek.segment('index', prefix + 'wick_top', 'index', prefix + 'high',
                           source=CDS, color=black_color)
            fig_ek.segment('index', prefix + 'wick_bottom', 'index', prefix + 'low',
                           source=CDS, color=black_color)
            # 实体线
            ek_bars = fig_ek.vbar('index', BAR_WIDTH, prefix + 'open', prefix + 'close',
                                  source=CDS, line_color=black_color, fill_color=ek_inc_cmap)

            # 提示
            ek_tooltips = [
                ('x, y', NBSP.join(('$index', '$y{0,0.0[0000]}'))),
                ('OHLC', NBSP.join((
                    f'@{prefix}open{{0,0.0[0000]}}',
                    f'@{prefix}high{{0,0.0[0000]}}',
                    f'@{prefix}low{{0,0.0[0000]}}',
                    f'@{prefix}close{{0,0.0[0000]}}',
                ))),
            ]
            set_tooltips(fig_ek, ek_tooltips, vline=True, renderers=[
                         ek_bars], if_datetime=is_datetime)

            fig_ek.yaxis.axis_label = ek_name
            fig_ek.yaxis.axis_label_text_color = black_color
            if fig_ohlc._main_ohlc:
                fig_ek.yaxis.visible = False
            else:
                fig_ek.yaxis.visible = True

            indicator_figs.append(fig_ek)
            # 自动缩放支持
            CDS.add(extra_k['high'].values, f"{ek_name}_h")
            CDS.add(extra_k['low'].values, f"{ek_name}_l")
            indicator_h.append(f"{ek_name}_h")
            indicator_l.append(f"{ek_name}_l")

    for ind in indicators:
        orig_ind = ind  # 保存原始IndFrame引用（后续ind会被覆盖）
        plot_id, isplot, name, lines, _lines, ind_name, is_overlay, category, indicator, doubles, plotinfo, span, _signal, bandstyle = ind._get_plot_datas(
            ind.ind_name)
        ind_name = ind.ind_name
        indicator_candles_index = []
        ohlc_colors = colorgen()
        ic = 0
        lineinfo: dict = plotinfo.get('linestyle', {})
        # 指标级图例开关（`ind.legend = False` / `class Xxx(BtIndicator): legend = False` ✓）：
        # bokeh 的 `legend_label=None` = **不生成图例项** ✓，与 pylightcharts 侧同步生效 ✓
        legend_visible = bool(plotinfo.get('legend', True))
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
                            new_bokeh_figure, fig_ohlc, plotinfo.get('height', 150))
                        indicator_figs.append(fig)
                        _mulit_ind = len(
                            indicator[ids].shape) > 1
                        CDS.add(
                            _nan_reduce_axis1(indicator[ids][:, np.arange(len(isplot[ids]))[isplot[ids]]], 'max') if _mulit_ind else indicator[ids], f"{legend_label}_h")
                        CDS.add(
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
                                CDS.add(ind, source_name)
                                tooltips.append(
                                    f"@{source_name}{'{'}0,0.0[0000]{'}'}")
                                _lineinfo = deepcopy(
                                    lines_setting)
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
                                if is_overlay[ids]:
                                    fig.line(
                                        'index', source_name, source=CDS,
                                        **_apply_legend(_lineinfo, legend_visible, source_name))
                                else:
                                    if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                        if_vbar = True
                                        # [修改] 原代码把柱底硬编码为 0:
                                        #     if "zeros" not in CDS.column_names:
                                        #         CDS.add([0.,]*len(ind), "zeros")
                                        #     _line_inc = np.where(ind > 0., 1, 0)...
                                        #     r = fig.vbar('index', BAR_WIDTH, 'zeros', source_name, ...)
                                        #   现支持逐线柱底基线 vbar_base(默认 None -> 0.0,
                                        #   与旧行为完全一致), 使"围绕中轴振荡"的柱状指标
                                        #   (如 LuxAlgo 市场结构振荡器的周期柱)能画对位置;
                                        #   上下行配色也改为按"数值 vs 基线"判定
                                        #   (base=0.0 时等价于原来的 ind > 0)。
                                        # 此处 _lineinfo 已被清洗掉 vbar_base,
                                        # 故直接从该线的 LineStyle 对象读取。
                                        _vbase = lineinfo[_lines_name].get('vbar_base', None)
                                        _vbase = 0.0 if _vbase is None else float(_vbase)
                                        _base_col = f"{_lines_name}_base"
                                        if _base_col not in CDS.column_names:
                                            CDS.add(
                                                [_vbase, ] * len(ind), _base_col)
                                        _line_inc = np.where(ind > _vbase, 1, 0).astype(
                                            np.uint8).astype(str).tolist()
                                        CDS.add(
                                            _line_inc, f"{_lines_name}_inc")
                                        _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                        if _line_inc_cmap is None:
                                            _line_inc_cmap = factor_cmap(
                                                f"{_lines_name}_inc", COLORS, ['0', '1'])
                                        r = fig.vbar('index', BAR_WIDTH, _base_col, source_name, source=CDS,
                                                     line_color='black', fill_color=_line_inc_cmap)
                                    else:
                                        r = fig.line(
                                            'index', source_name, source=CDS,
                                            **_apply_legend(_lineinfo, legend_visible, source_name))
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
                            fig.yaxis.axis_label = legend_label if legend_visible else ''
                            fig.yaxis.axis_label_text_color = black_color
                            if fig_ohlc._main_ohlc:
                                fig.yaxis.visible = False
                            else:
                                fig.yaxis.visible = True
                            if len(lines) == 1:
                                fig.legend.glyph_width = 0
                        # 两条线之间填充背景色(每个分组只添加一次, 不能放进逐线循环)
                        band_lines = _lines[ids]  # 原始列名(无前缀)
                        band_source_lines = lines[ids]  # 数据源列名(带前缀)
                        if len(band_lines) >= 2:
                            for band_dict in bandstyle:
                                _bd = dict(band_dict)
                                lower_name = _bd.get('lower', '')
                                upper_name = _bd.get('upper', '')
                                if lower_name in band_lines and upper_name in band_lines and all([isplot[ids][band_lines.index(lower_name)], isplot[ids][band_lines.index(upper_name)]]):
                                    # 将原始列名替换为数据源中带前缀的列名
                                    _bd['lower'] = band_source_lines[band_lines.index(
                                        lower_name)]
                                    _bd['upper'] = band_source_lines[band_lines.index(
                                        upper_name)]
                                    from .strategy.bokeh_plot import add_band_breaks
                                    add_band_breaks(fig, CDS, 'index', _bd)
        else:
            # [修正] Volume Profile 指标(category='vp')的列全是
            #   isplot=False(分布数据不该走通用折线分支), 若仍用
            #   `if any(isplot):` 则整个 VP 专门分支都不会执行 -> VP 画不出来。
            #   故补一个 `or category == 'vp'`。
            if any(isplot) or category == 'vp':
                # if any(isplot):
                is_candles = category == 'candles'
                if is_candles and len(lines) < 4:
                    is_candles = False
                # [新增] Volume Profile 指标(category == 'vp'):
                #   next() 返回 (K线根数, 档数) 矩阵, 画成"横向成交量分布"
                #   (右端=0刻度、向左生长、贴绘图区右缘 —— 见 _add_volume_profile)
                is_vp = (category == 'vp')
                tooltips = []
                colors = cycle(
                    ohlc_colors if is_overlay else colorgen())
                legend_label = name  # 初始化命名的名称
                if is_vp:                       # Volume Profile
                    # overlap=True -> 价格主图(y=价格档); False -> 副图(y=档位序号)
                    if is_overlay:
                        fig = fig_ohlc
                        _vp_y = 'price'
                    else:
                        fig = new_indicator_figure(
                            new_bokeh_figure, fig_ohlc, 150)
                        indicator_figs.append(fig)
                        _vp_y = 'index'
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
                        # [清理] 原来写成 `if _cn not in CDS.column_names if hasattr(CDS, 'column_names') else True:`
                        #   嵌套条件表达式（等价于 (a) if (b) else True），易读性差、
                        #   也掩盖了“CDS 可能不存在”的风险（bokeh_plot.py 里就因为写成 `CDS`
                        #   而抛过 NameError）。改成直白写法。
                        if hasattr(CDS, 'column_names') and _cn not in CDS.column_names:
                            CDS.add(_ind2[:, _li], _cn)
                        _lp = plotinfo.get('linestyle', {}).get(_nm[_li], None) if isinstance(plotinfo.get('linestyle'), dict) else None
                        _lkw = dict(line_width=1.3)
                        if _lp is not None:
                            _lkw['line_color'] = getattr(_lp, 'line_color', None)
                            _lkw['line_width'] = getattr(_lp, 'line_width', 1.3) or 1.3
                        _lkw = {k: v for k, v in _lkw.items() if v is not None}
                        fig.line('index', _cn, source=CDS, **_lkw)
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
                    _c0, _c1, _pbv = None, None, None
                    # ★ [BUG 修复] linestyle['vp0'] / ['vp_poc'] 里既可能是 LineStyle
                    #   对象, 也可能是**颜色字符串**(框架归一化后)。原代码只做
                    #   getattr(x, 'line_color'), 遇到字符串就拿不到颜色 -> _c0/_c1
                    #   恒为 None -> 指标端设的 vp_color / vp_poc_color 全部失效
                    #   (永远显示默认蓝/橙)。这里两种形式都兼容。
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
                    # ★ VPStyle（**只有 category='vp' 会读**）：颜色 / 透明度 /
                    #   满量程柱长 / 档高比例 / 档区窗口 / 右侧成交量刻度。
                    #   与 pylightcharts 用**同一个**取值器（utils.vp_style_values）
                    try:
                        from minibt.utils import vp_style_values
                        _vps = vp_style_values(plotinfo.get('vpstyle'))
                    except Exception:
                        _vps = {}
                    if _vps:
                        _c0 = _pick_vp_color(_vps.get('bar_color')) or _c0
                        _c1 = _pick_vp_color(_vps.get('poc_color')) or _c1
                        if _vps.get('price_bars'):
                            _pbv = _vps['price_bars']
                    try:
                        # ★ [BUG 修复] 必须把返回的渲染器接住赋给 r：
                        #   纯 VP 指标没有折线列(_lidx 空) -> 后面 `renderers=[r]`
                        #   会用到 r, 而 r 只在画线/柱的分支里被赋值 ->
                        #   UnboundLocalError: cannot access local variable 'r'。
                        #   _add_volume_profile 返回的正是 VP 的 HBar 渲染器。
                        r = _add_volume_profile(fig, CDS, _ind2[:, _vidx], df, ic,
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
                        ic += 1             # 占一个序号, 保证列名唯一
                    except Exception as _e:  # 画不出就跳过, 但不静默
                        import sys as _sys
                        print(f"[volume_profile] 绘制失败: {type(_e).__name__}: {_e}",
                              file=_sys.stderr)
                elif is_overlay and not is_candles:  # 主图叠加
                    fig = fig_ohlc
                elif is_candles:  # 副图是蜡烛图
                    indicator_candles_index.append(ic)
                    assert len(lines) >= 4
                    lines = list(
                        map(lambda x: x.lower(), lines))
                    filed_index = []
                    missing_index = []
                    for ii, file in enumerate(FILED.OHLC):
                        is_missing = True
                        for n in lines:
                            if file in n:
                                filed_index.append(
                                    lines.index(n))
                                is_missing = False
                        else:
                            if is_missing:
                                missing_index.append(ii)
                    assert not missing_index, f"数据中缺失{[FILED.OHLC[ii] for ii in missing_index]}字段"
                    for ie in filed_index:
                        CDS.add(indicator[:,
                                          ie], lines[ie])
                    # 副图蜡烛按“指标自身”的开/收涨跌着色(不跟随主K线的 inc)
                    _ohlc_inc_field = f"candle_inc_{ic}"
                    _ohlc_open = np.asarray(indicator[:, filed_index[0]],
                                            dtype=np.float64)
                    _ohlc_close = np.asarray(indicator[:, filed_index[3]],
                                             dtype=np.float64)
                    _ohlc_inc = (_ohlc_close >= _ohlc_open
                                 ).astype(np.uint8).astype(str).tolist()
                    CDS.add(_ohlc_inc, _ohlc_inc_field)
                    # 影线拆两条（单条 high->low 会穿过实体）
                    CDS.add(np.maximum(_ohlc_open, _ohlc_close),
                            f"candle_wtop_{ic}")
                    CDS.add(np.minimum(_ohlc_open, _ohlc_close),
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
                    # [修改] 支持“主图双蜡烛”: 当蜡烛型指标 overlap=True 时,
                    #   把它的蜡烛画到**主图** fig_ohlc 上(而不是另开副图),
                    #   并故意用更窄的柱宽(BAR_WIDTH/2), 以免完全盖住主 K 线;
                    #   overlap=False 的蜡烛指标仍按原逻辑单独开副图。
                    if is_overlay:
                        fig_ohlc_ = fig_ohlc
                        _candle_w = BAR_WIDTH / 2
                    else:
                        fig_ohlc_ = new_indicator_figure(
                            new_bokeh_figure, fig_ohlc, plotinfo.get('height', 100))
                        _candle_w = BAR_WIDTH
                    fig_ohlc_.segment(
                        'index', f"candle_wtop_{ic}", 'index', lines[filed_index[1]], source=CDS, color=black_color)
                    fig_ohlc_.segment(
                        'index', f"candle_wbot_{ic}", 'index', lines[filed_index[2]], source=CDS, color=black_color)
                    ohlc_bars_ = fig_ohlc_.vbar('index', _candle_w, lines[filed_index[0]], lines[filed_index[3]], source=CDS,
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
                        fig_ohlc_.yaxis.axis_label = ind_name if legend_visible else ''
                        fig_ohlc_.yaxis.axis_label_text_color = black_color
                        if fig_ohlc._main_ohlc:
                            fig_ohlc_.yaxis.visible = False
                        else:
                            fig_ohlc_.yaxis.visible = True

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
                                CDS.add(ind,
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
                                fig_ohlc_.line(
                                    'index', source_name, source=CDS,
                                    **_apply_legend(_lineinfo, legend_visible, source_name))
                                ohlc_tooltips_.append(
                                    (_lines_name, NBSP.join(tuple(tooltips))))
                    set_tooltips(fig_ohlc_, ohlc_tooltips_,
                                 vline=True, renderers=[ohlc_bars_])
                    # [修改] 主图叠加时不往 indicator_figs 里加(它就是主图 fig_ohlc)
                    if not is_overlay:
                        indicator_figs.append(fig_ohlc_)
                    ic += 1
                    _mulit_ind = len(indicator.shape) > 1
                    CDS.add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'max') if _mulit_ind else indicator,
                            f"{legend_label}_h")
                    CDS.add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'min') if _mulit_ind else indicator,
                            f"{legend_label}_l")
                    indicator_h.append(f"{legend_label}_h")
                    indicator_l.append(f"{legend_label}_l")

                else:
                    __fig = fig_ohlc
                    fig = new_indicator_figure(
                        new_bokeh_figure, __fig, plotinfo.get('height', 150))
                    indicator_figs.append(fig)
                    ic += 1
                    _mulit_ind = len(indicator.shape) > 1
                    CDS.add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'max') if _mulit_ind else indicator,
                            f"{legend_label}_h")
                    CDS.add(_nan_reduce_axis1(indicator[:, np.arange(len(isplot))[isplot]], 'min') if _mulit_ind else indicator,
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
                            CDS.add(ind,
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
                                    'index', source_name, source=CDS,
                                    **_apply_legend(_lineinfo, legend_visible, source_name))
                            else:
                                if lineinfo and _lines_name in lineinfo and lineinfo[_lines_name].get('line_dash', None) == 'vbar':
                                    if_vbar = True
                                    if "zeros" not in CDS.column_names:
                                        CDS.add(
                                            [0.,]*len(ind), "zeros")
                                    _line_inc = np.where(ind > 0., 1, 0).astype(
                                        np.uint8).astype(str).tolist()
                                    CDS.add(
                                        _line_inc, f"{_lines_name}_inc")
                                    _line_inc_cmap = lineinfo[_lines_name]["line_color"]
                                    if _line_inc_cmap is None:
                                        _line_inc_cmap = factor_cmap(
                                            f"{_lines_name}_inc", COLORS, ['0', '1'])
                                    r = fig.vbar('index', BAR_WIDTH, 'zeros', source_name, source=CDS,
                                                 line_color='black', fill_color=_line_inc_cmap)
                                else:
                                    r = fig.line(
                                        'index', source_name, source=CDS,
                                        **_apply_legend(_lineinfo, legend_visible, source_name))
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
                        fig.yaxis.axis_label = ind_name if legend_visible else ''
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
                        from .strategy.bokeh_plot import add_band_breaks
                        add_band_breaks(fig, CDS, 'index', _bd)
        if signal_info:
            # 逐点信号标签文本: {信号key: 长度=bar数 的 object 数组, 有文本=str, 无=np.nan}
            signal_texts_map: dict = plotinfo.get(
                'signal_texts', {}) or {}
            signal_style = resolve_signal_text_style(
                signal_texts_map, black_style)
            for k, v in signal_info.items():
                signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = list(
                    v.values())

                if signalshow:
                    signaldata: np.ndarray
                    islabel = isinstance(signallabel, dict)
                    pertexts = None
                    label_text = None
                    if islabel:
                        label_text = signallabel.pop("text", k)
                    # 获取信号数据：优先从_lines索引，找不到则从原始IndFrame的pandas_object获取
                    found = False
                    if doubles:
                        idx = search_index(_lines, k)
                        if idx is not None:
                            index1, index2 = idx
                            signaldata = indicator[index1][:, index2]
                            found = True
                    else:
                        if k in _lines:
                            signaldata = indicator[:, _lines.index(k)]
                            found = True
                    if not found:
                        # 从原始IndFrame引用获取信号数据
                        try:
                            if orig_ind is not None and hasattr(orig_ind, 'pandas_object') and k in orig_ind.pandas_object.columns:
                                signaldata = np.array(
                                    orig_ind.pandas_object[k].values, dtype=np.float64)
                                found = True
                            elif orig_ind is not None and k in orig_ind.columns:
                                signaldata = np.array(
                                    orig_ind[k].values, dtype=np.float64)
                                found = True
                            else:
                                continue
                        except Exception:
                            continue
                    signal_index = np.argwhere(
                        signaldata > 0)[:, 0]
                    # 锚点：K 线列（low/high）→ 任意指标线（vidya_up）→
                    # 退回信号线自身；与 bokeh_plot / pylightcharts 同口径。
                    from .strategy.bokeh_plot import _signal_anchor_prices
                    price_data = _signal_anchor_prices(
                        df, indicator, _lines, signalkey, signaldata, doubles)
                    if signaloverlap:
                        signal_fig = fig_ohlc
                    else:
                        signal_fig = fig
                    signal_price = price_data[signaldata > 0]
                    signal_datetime = df.datetime.values[signaldata > 0]
                    # 逐点文本: 与数值信号线同索引对齐, 文本为 nan 的信号点跳过
                    pertexts = signal_texts_map.get(k)
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
                    #   signal_source_ -> "没有文本的信号点连 marker 也一起被剔除",
                    #   与上方注释声明的"文本为 nan 的信号点跳过"(只跳文本)不符,
                    #   也让"有文本信号"与"仅有标记信号"在图上无法区分。
                    #   改为: marker 恒用全量信号点(signal_*),
                    #   文本另走 text_* 三件套(可被 _valid 子集化)。
                    text_index = signal_index
                    text_price = signal_price
                    text_datetime = signal_datetime
                    if pertexts is not None:
                        pertexts = np.asarray(
                            pertexts[signaldata > 0], dtype=object)
                        _valid = ~pd.isna(pertexts)
                        if not _valid.any():
                            pertexts = None
                        elif not _valid.all():
                            pertexts = pertexts[_valid]
                            text_index = signal_index[_valid]
                            text_price = signal_price[_valid]
                            text_datetime = signal_datetime[_valid]
                    # 文字标签: 逐点文本优先(无需先 set_label), 否则回退统一 label 配置
                    # `signallabel` 是**原始** SignalLabel 字典（带 text/size/style/
                    # color + 一整套规范文本样式键）：先 `_label_text_style` 归一化
                    # 成规范键，再 `_bokeh_text_options` 翻译成 bokeh 参数 ——
                    # 直接把原字典喂给 LabelSet 会因为 `size`/`offset_y` 这类键报
                    # `unexpected attribute`。
                    label_kw = {}
                    if islabel:
                        from .strategy.bokeh_plot import _bokeh_text_options
                        from .utils import _label_text_style
                        label_kw = _bokeh_text_options(
                            _label_text_style(signallabel))
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
                        max_lines = max((len(r)
                                        for r in split_rows), default=1)
                        pertext_lines = [
                            [r[li] if li < len(
                                r) else None for r in split_rows]
                            for li in range(max_lines)]
                        # [修改] 原代码:
                        #     signal_source_ = ColumnDataSource(dict(
                        #         index=signal_index,
                        #         datetime=signal_datetime,
                        #         price=signal_price,
                        #         size=[float(signalsize), ] *
                        #         len(signal_index),
                        #         text=[str(t) for t in pertexts],
                        #         x_offset=[-0.5 * text_width_pt(str(t), font_size, is_buy)
                        #                   for t in pertexts],
                        #     ))
                        #   它把文本与 marker 塞进同一个 CDS。现在 text/x_offset
                        #   列长度 = 有文本的点数, 与全量 marker 点数不一致
                        #   (bokeh 不校验列长, 不等长的列不会报错、只会在前端静默
                        #    少画/错位), 必须拆成独立的 label_source_ 供 LabelSet 用。
                        label_source_ = ColumnDataSource(dict(
                            index=text_index,
                            datetime=text_datetime,
                            price=text_price,
                            size=[float(signalsize), ] *
                            len(text_index),
                            text=[str(t) for t in pertexts],
                            x_offset=[-0.5 * text_width_pt(str(t), font_size, is_buy)
                                      for t in pertexts],
                        ))
                    elif islabel:
                        pertext_lines = None
                        # [修改] 原为 signal_source_ = ; 拆开以免 scatter 被塞入
                        #   仅文本需要的 text 列(marker 用上面的全量 signal_source_)。
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

                    # [修改] 原代码: marker=signalmarker
                    #   signalmarker 可能是 Markers 枚举里的 lightweight_charts 专用名
                    #   (arrow_up/arrow_down), 或 TradingView 风格的 label_up/label_down;
                    #   bokeh 不认这些名字 -> E-1001 BAD_COLUMN_NAME 且标记被静默丢弃。
                    #   统一经 to_bokeh_marker 收敛(见 minibt/constant.py)。
                    r = signal_fig.scatter(x='index', y='price', source=signal_source_, fill_color=signalcolor,
                                           marker=to_bokeh_marker(signalmarker), line_color='black', size="size")
                    if pertext_lines is not None and len(pertext_lines) > 1:
                        # --- 多行文本: 每行一个 LabelSet, 逐行错开一个行距 ---
                        # 低点锚点向下展开, 高点锚点向上展开(均背离 K 线)
                        if islabel:
                            # 已配置统一 label 样式: 保留其主题背景衬托
                            label_kw.update(
                                dict(background_fill_alpha=0.5,
                                     background_fill_color="white" if black_style else "black"))
                        else:
                            if signal_style:
                                # 同上面那处：规范键必须先过翻译
                                # （`offset_x/offset_y` -> bokeh `x_offset/
                                # `y_offset`，`line_height`/`padding` 丢掉）
                                from .strategy.bokeh_plot import _bokeh_text_options
                                label_kw.update(
                                    _bokeh_text_options(dict(signal_style)))
                            else:
                                # 逐点默认文本: 灰底+边框衬块(与面板拉开层次)
                                if black_style:
                                    label_kw.update(dict(
                                        text_color="white",
                                        background_fill_color="#333333",
                                        background_fill_alpha=0.7,
                                        border_line_color="#888888",
                                        border_line_alpha=0.8))
                                else:
                                    label_kw.update(dict(
                                        text_color="black",
                                        background_fill_color="#EAEAEA",
                                        background_fill_alpha=0.8,
                                        border_line_color="#999999",
                                        border_line_alpha=0.8))
                        y0 = float(label_kw.get("y_offset", 0.))
                        _fs2 = str(label_kw.get(
                            "text_font_size", "10pt")).replace("pt", "")
                        try:
                            font_size2 = float(_fs2)
                        except (TypeError, ValueError):
                            font_size2 = 10.
                        line_gap = font_size2 * 1.3
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
                                        r[3], font_size2, is_buy) for r in keep],
                                )),
                                **_lkw,
                            )
                            signal_fig.add_layout(labels)
                    elif pertexts is not None or islabel:
                        # --- 单行文本: 用 LabelSet 添加文字标签 ---
                        if islabel:
                            # 已配置统一 label 样式: 保留其主题背景衬托
                            label_kw.update(
                                dict(background_fill_alpha=0.5,
                                     background_fill_color="white" if black_style else "black"))
                        else:
                            # 逐点文本: signal_style(含按主题解析的预设/用户自定义)优先。
                            # 必须过一遍 `_bokeh_text_options`：规范键里的
                            # `offset_x/offset_y` 要翻成 bokeh 的 `x_offset/
                            # y_offset`，`line_height`/`padding` 这些
                            # pylightcharts 专属键要丢掉，否则 LabelSet 直接
                            # 报 `unexpected attribute`。
                            if signal_style:
                                from .strategy.bokeh_plot import _bokeh_text_options
                                label_kw.update(
                                    _bokeh_text_options(dict(signal_style)))
                            else:
                                # 逐点默认文本: 灰底+边框衬块(与面板拉开层次)
                                if black_style:
                                    label_kw.update(dict(
                                        text_color="white",
                                        background_fill_color="#333333",
                                        background_fill_alpha=0.7,
                                        border_line_color="#888888",
                                        border_line_alpha=0.8))
                                else:
                                    label_kw.update(dict(
                                        text_color="black",
                                        background_fill_color="#EAEAEA",
                                        background_fill_alpha=0.8,
                                        border_line_color="#999999",
                                        border_line_alpha=0.8))
                        if xoff_col:
                            # 逐点模式: 水平偏移用数据列(每点自适应宽度居中)
                            label_kw['x_offset'] = 'x_offset'
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

    set_tooltips(fig_ohlc, ohlc_tooltips,
                 vline=True, renderers=[ohlc_bars], if_datetime=is_datetime)
    fig_ohlc.yaxis.axis_label = f"{df.ind_name}"
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
                          indicator_h=indicator_h, indicator_l=indicator_l, source=CDS)
    plots: list[_figure] = [fig_ohlc]
    fig_volume = None          # overlay 模式下不建成交量副图
    if is_volume_pane:
        # 成交量
        fig_volume = new_indicator_figure(
            new_bokeh_figure, fig_ohlc, y_axis_label="volume", height=60)
        fig_volume.css_classes = ["volume-chart"]
        if fig_ohlc._main_ohlc:
            fig_volume.yaxis.visible = False
        else:
            fig_volume.yaxis.visible = True
        # X 轴统一在下面 `for f in plots:` 里处理（只保留最下面一张的时间轴）
        r_volume = fig_volume.vbar(
            'index', BAR_WIDTH, 'volume', source=CDS, color=inc_cmap)
        colors = cycle(colorgen())
        r_volume5 = fig_volume.line('index', 'volume5', source=CDS,
                                    legend_label='volume5', line_color=next(colors),
                                    line_width=1.3)
        r_volume10 = fig_volume.line('index', 'volume10', source=CDS,
                                     legend_label='volume10', line_color=next(colors),
                                     line_width=1.3)
        set_tooltips(fig_volume, [
                    ('volume', '@volume{0.00}'), ('volume5', '@volume5{0.00}'), ('volume10', '@volume10{0.00}'),], renderers=[r_volume])
        fig_volume.yaxis.formatter = NumeralTickFormatter(
            format="0 a")  # format="0"
        fig_volume.yaxis.axis_label_text_color = black_color

        plots.append(fig_volume)
    if indicator_figs:
        plots += indicator_figs
    # 缩放/平移时按**可见 x 范围**自动调整 Y 轴（量价 / 各副图）。
    # 之前这段在 `if is_volume_pane:` 里，成交量换成主图叠加后就不再注册，
    # 导致 Y 轴一直用全段最低/最高，不随缩放变化。
    if fig_volume is not None:
        custom_js_args.update(volume_range=fig_volume.y_range)
    fig_ohlc.x_range.js_on_change('end', CustomJS(args=custom_js_args,
                                                  code=_AUTOSCALE_JS_CALLBACK))
    if signal_profit_result:
        profit_figs = _build_profit_figures(
            signal_profit_result, new_bokeh_figure, fig_ohlc, black_color, black_style)
        plots = profit_figs + plots
    layout = column(
        children=plots,
        sizing_mode='stretch_both' if fullscreen else 'stretch_width',
        css_classes=["dynamic-column"],
        # 只有全屏时才用 100vh 的 flex CSS
        stylesheets=[panel_CUSTOM_CSS if fullscreen else ""],
    )

    linked_crosshair = CrosshairTool(
        dimensions='both', line_color=black_color)

    # 联动十字线 + 最下面一张图（时间轴画在它上面）
    _vis_plots = [f for f in plots if getattr(f, 'visible', True)]
    _xfig = _vis_plots[-1] if _vis_plots else (plots[-1] if plots else None)
    _xfig_id = getattr(_xfig, 'id', None) if _xfig is not None else None
    fig_spans: list = []

    for f in plots:
        # 全屏：给每张图自己的 shadow CSS 写 flex 权重（主图 4 / 资金 2 / 副图 1）
        if fullscreen:
            _cls = f.css_classes or []
            _w = 4 if 'candle-chart' in _cls else (2 if 'value-chart' in _cls else 1)
            f.stylesheets = list(f.stylesheets or []) + [fullscreen_figure_css(_w)]
        # 原生图例隐藏：左上角的“带颜色线 + 名称 + 当前值”由 OVERLAY_* 替代
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
            f.legend.click_policy = 'hide'  # 'mute'

        f.min_border_left = 0
        f.min_border_top = 0  # 3
        f.min_border_bottom = 6
        f.min_border_right = 10
        f.outline_line_color = '#666666'
        # 工具栏：由 btplot(toolbar=...) 控制（默认不开启）
        f.toolbar_location = 'right' if show_toolbar else None
        # Y 轴标题一律去掉，只留刻度
        f.yaxis.axis_label = None
        # 网格线：由 config.grid 控制（默认不显示）
        f.xgrid.visible = show_grid
        f.ygrid.visible = show_grid
        # 联动十字线的竖直 Span（hover 时各图一起移动）
        _sp = Span(dimension='height', location=0,
                   line_color=black_color, line_width=1,
                   line_alpha=0.6, visible=False)
        f.add_layout(_sp)
        fig_spans.append(_sp)
        # X 轴只保留最下面一张
        f.xaxis.visible = (f is _xfig)

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
            if fig_volume is not None and f == fig_volume:
                f.background_fill_alpha = 0.5  # 半透明效果
                f.border_fill_alpha = 0.5
        f.add_tools(linked_crosshair)
        wheelzoom_tool = next(
            wz for wz in f.tools if isinstance(wz, WheelZoomTool))
        wheelzoom_tool.maintain_focus = False
        if f._main_ohlc:
            f.yaxis.visible = False
            f.tools.visible = False

    # X 轴时间刻度：按 index 查 datetime 列，按**可见窗口**动态生成（~8 个）
    try:
        _dt_col = CDS.data.get('datetime')
    except Exception:
        _dt_col = None
    if _dt_col is not None:
        _fmt = CustomJSTickFormatter(args=dict(dt=_dt_col), code=_TIME_TICK_JS)
        _tickr = CustomJSTicker(args=dict(dt=_dt_col),
                                major_code=_TIME_TICKER_JS)
        for f in plots:
            try:
                f.xaxis[0].formatter = _fmt
                f.xaxis[0].ticker = _tickr
                f.xaxis[0].major_label_policy = NoOverlap()
            except Exception:
                ...

    # 统一给每张图的 hover 回调带上 spans（十字线联动）+ xfig_id；离开时隐藏标签
    for f in plots:
        for _tool in getattr(f, 'tools', []):
            if (isinstance(_tool, HoverTool)
                    and getattr(_tool, 'name', '') == 'minibt_overlay'):
                _tool.callback = CustomJS(
                    args=dict(fig_id=f.id, spans=fig_spans, xfig_id=_xfig_id),
                    code=OVERLAY_UPDATE_JS)
        try:
            f.js_on_event(MouseLeave, CustomJS(
                args=dict(spans=fig_spans, fig_id=f.id, xfig_id=_xfig_id),
                code=('for (var i = 0; i < spans.length; i++) '
                      '{ spans[i].visible = false; }\n'
                      'var idx = window.__minibt_fig_index__ || {};\n'
                      'var e = idx[fig_id];\n'
                      'if (e && e.ylab) { e.ylab.style.display = "none"; }\n'
                      'var ex = idx[xfig_id] || e;\n'
                      'if (ex && ex.xlab) { ex.xlab.style.display = "none"; }')))
        except Exception:
            ...

    if open_browser:
        IS_JUPYTER_NOTEBOOK = False
    # tooltip 主题色 + 钉到子图左上角（同 strategy/bokeh_plot.py）
    _tooltip_css = DARK_TOOLTIP_CSS if black_style else LIGHT_TOOLTIP_CSS
    # 保存图表
    if plot_name and isinstance(plot_name, str):
        if not plot_name.endswith('.html'):
            plot_name += '_btplot.html'
    else:
        plot_name = 'btplot.html'
    plot_cwd = plot_cwd if isinstance(
        plot_cwd, str) and plot_cwd else os.path.join(user_dir(), "plots")
    if not os.path.exists(plot_cwd):
        os.makedirs(plot_cwd, exist_ok=True)
    FileName = os.path.join(plot_cwd, plot_name)
    # 直接生成 HTML 并注入 tooltip 主题色 / 钉位脚本。
    # 注意：不能用 `bokeh.io.show()` —— 它走 `save()`，实测不会带上
    # `INLINE.css_raw` / `js_raw`（所以以前 tooltip 主题/钉位不会生效）。
    # 用 INLINE（内嵌 bokeh JS/CSS）而不是 CDN，页面自包含、离线也能打开
    html = file_html(layout, INLINE, "Bt Plot")
    # 注入到真正的 `</body>` 前（第一个 `</head>` 可能在 bokeh 内联 JS 的字符串里）
    # pylightcharts 用户设置的主题色（跟踪标签背景）；未设置则 None
    try:
        from .strategy.strategy_window import _load_settings
        _accent = _load_settings()['user'].get('theme_color')
    except Exception:
        _accent = None
    import json as _json
    # 全屏时把页面根节点边距归零 + 禁止滚动条（否则底部会留一条间隙 / 出滚动条）
    if fullscreen:
        _tooltip_css = _tooltip_css + FULLSCREEN_PAGE_CSS
    _inject = (f'<style>{_tooltip_css}</style>'
               f'<script>window.__minibt_dark__={str(bool(black_style)).lower()};'
               f'window.__minibt_accent__={_json.dumps(_accent) if _accent else "null"};</script>'
               f'<script>{TOOLTIP_PIN_JS}</script>'
               f'<script>{OVERLAY_SETUP_JS}</script>')
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
                           f'minibt_btplot_{os.getpid()}.html')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(html)
        webbrowser.open('file://' + os.path.abspath(tmp))


def _build_profit_figures(result: dict, new_bokeh_figure: Callable,
                          fig_ohlc: _figure, black_color: str,
                          black_style: bool) -> list:
    """
    ## 构建信号收益子图（嵌入btplot顶部）

    根据 signal_profit 计算结果创建与主图共享x轴的单一收益曲线图。
    多头信号(正值)和空头信号(负值)的收益合并在一张图中显示。

    Args:
        result: signal_profit 返回的收益数据字典
        new_bokeh_figure: bokeh figure 工厂函数
        fig_ohlc: 主K线图（用于共享x_range）
        black_color: 黑色主题文本颜色
        black_style: 是否为黑色主题

    Returns:
        list: bokeh figures 列表，按顺序插入btplot顶部
    """
    bars = result.get('bars', [1])
    cum_profit = result.get('cum_profit', [])
    signal_directions = result.get('signal_directions', [])

    if not isinstance(cum_profit, (list, tuple)) or len(cum_profit) == 0:
        return []

    profit_figs = []

    fig_profit = new_indicator_figure(new_bokeh_figure, fig_ohlc, height=150,
                                      y_axis_label='Profit')
    fig_profit.css_classes = ["profit-chart"]
    fig_profit.add_tools(HoverTool(
        tooltips=[('Bar Index', '@x'), ('Profit', '@y{0.0000}')],
        mode='vline'))

    colors = Category10[max(3, len(bars))]
    for k, bar_val in enumerate(bars):
        curve = cum_profit[k]
        valid = ~np.isnan(curve)
        if not np.any(valid):
            continue
        x = np.arange(len(curve))[valid]
        y = curve[valid]
        cds = ColumnDataSource(data={'x': x, 'y': y})
        color = colors[k % len(colors)]
        # 初始资金(1.0)与收益曲线之间的色带
        fig_profit.add_layout(Band(
            base='x',
            lower=1.0,
            upper='y',
            source=cds,
            fill_color=color,
            fill_alpha=0.12,
            line_width=0,
            level='underlay',
        ))
        fig_profit.line('x', 'y', source=cds,
                        legend_label=f'bar={bar_val}',
                        color=color, line_width=2)

    # 如果有多空混合信号，添加零线
    if len(set(signal_directions)) > 1:
        fig_profit.add_layout(Span(location=1.0, dimension='width',
                                   line_color='gray', line_dash='dashed', line_width=1))

    fig_profit.legend.click_policy = 'hide'
    fig_profit.add_layout(Legend(), 'right')
    fig_profit.yaxis.formatter = NumeralTickFormatter(format='0.0000')
    fig_profit.yaxis.axis_label_text_color = black_color
    profit_figs.append(fig_profit)

    return profit_figs


def signal_profit_plot(result: dict, indicator: 'IndFrame | IndSeries',
                       open_browser: bool = False,
                       save_plot: bool = True,
                       plot_cwd: str = "",
                       plot_name: str = "") -> None:
    """
    ## 绘制信号收益图表（嵌入btplot顶部）

    根据 signal_profit 计算结果，调用 btplot 并将收益曲线嵌入顶部。
    如果只有一个信号，绘制单个收益图；如果有两个信号（多空），绘制两个收益图。

    Args:
        result: signal_profit 返回的收益数据字典
        indicator: 调用 signal_profit 的 IndFrame 或 IndSeries 实例
        open_browser: 是否在浏览器中打开
        save_plot: 是否保存HTML文件
        plot_cwd: 保存目录
        plot_name: 文件名
    """
    btplot(indicator,
           open_browser=open_browser,
           save_plot=save_plot,
           plot_cwd=plot_cwd,
           plot_name=plot_name,
           signal_profit_result=result)
