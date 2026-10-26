"""bokeh 回放图表（纯 bokeh server、内存更新）。

替代旧 ``liveplot/live_plot.py`` 的「策略子进程 + joblib 文件队列 + 子进程读盘」
回放：这里直接在策略所在进程里复用 ``bokeh_plot.py`` 构建的**同一套图表**，然后按
各 ``ColumnDataSource`` 的 ``index`` 列逐 bar「揭开」已发生的数据（不重算、不落盘、
不 stream/patch，因此没有列长不一致 / 索引错位那类 bug）。

入口：``Bt(replay=True).run(gui=Gui.Bokeh)`` → ``bt.py::_strategy_replay_bokeh``
→ ``run_replay(...)``。回放/实时共用 ``bokeh_plot.py`` 的构建器，实时模式只需换一个
「喂数据」的 ``Feed``（阶段二再做）。
"""
from __future__ import annotations

import numpy as np

from . import bokeh_plot as _bp


def close_apis(strategys, verbose: bool = True) -> None:
    """Ctrl+C 退出时先关掉天勤连接（``api.close()``），再退出程序。

    多个策略可能共用同一个 ``TqApi``：按 ``id(api)`` 去重，只关一次。
    回放用的是本地数据，不关心天勤连接，传 ``verbose=False`` 不打印。
    """
    seen = set()
    for s in strategys or []:
        try:
            api = getattr(s, 'api', None)
        except Exception:
            api = None
        if api is None or id(api) in seen:
            continue
        seen.add(id(api))
        try:
            if hasattr(api, 'close'):
                api.close()
                if verbose:
                    print('[bokeh] 已关闭天勤连接')
        except Exception as error:
            if verbose:
                print('[bokeh] 关闭天勤连接出错:', error)


def quiet_bokeh_logging(level: int | None = None) -> None:
    """把 bokeh / tornado 的日志压到 WARNING。

    上层（tqsdk / 用户）常把 root logger 设成 DEBUG/TRACE，bokeh 就会把
    ``Patching attribute ...``、``Running ... job`` 这类 TRACE/DEBUG 全部刷出来。
    回放/实时 server 启动前调一次。
    """
    import logging
    lvl = logging.WARNING if level is None else level
    for _name in ('bokeh', 'bokeh.core', 'bokeh.core.has_props',
                  'bokeh.core.property', 'bokeh.server', 'bokeh.server.tornado',
                  'bokeh.server.contexts', 'bokeh.server.session',
                  'bokeh.application', 'bokeh.document', 'bokeh.client',
                  'bokeh.io', 'tornado', 'tornado.application',
                  'tornado.general'):
        try:
            logging.getLogger(_name).setLevel(lvl)
        except Exception:
            pass
    # 关闭浏览器后，周期回调仍会尝试推送 -> “Failed sending message as connection
    # was closed”（WARNING，无害）。压到 ERROR。
    for _name in ('bokeh.server.views', 'bokeh.server.views.ws',
                  'bokeh.server.views.doc_handler', 'bokeh.server.views.session_handler'):
        try:
            logging.getLogger(_name).setLevel(logging.ERROR)
        except Exception:
            pass


def add_page_root(doc, controls, tabs, reg: dict, separator: bool = False) -> None:
    """回放/实时共用：把「控件行 + Tabs」挂到 doc，并做全屏/主题处理。

    * 页面根是 ``[控件行, Tabs]`` 而不是 Tabs 本身，所以把外层 Tabs 的
      ``:host{height:100vh}`` 覆盖成 ``100%``（否则 100vh + 控件行 > 视口，
      图表被拷扁、下方留一大片空白）。
    * ``separator=True`` 时在控件行与 Tabs 之间插一条细分隔线。
    * bokeh server 无法像静态 HTML 那样后处理 ``<body>``：用
      ``doc.js_on_event(DocumentReady, CustomJS(...))`` 注入 tooltip 主题色 +
      全屏页面 CSS + ``OVERLAY_SETUP_JS``。
    """
    from bokeh.layouts import column
    from bokeh.models import Div
    try:
        _ss = [s for s in list(tabs.stylesheets or [])
               if '100vh' not in str(s)]
        _ss.append(':host{height:100% !important; flex:1 1 auto;}')
        tabs.stylesheets = _ss
    except Exception:
        pass
    children = [controls]
    if separator:
        _line = '#555555' if reg.get('theme') else '#d0d0d0'
        children.append(Div(
            text='', height=1, sizing_mode='stretch_width',
            styles={'background-color': _line, 'margin': '4px 0 2px'}))
    children.append(tabs)
    doc.add_root(column(
        *children, sizing_mode='stretch_both',
        stylesheets=[':host{height:100% !important; display:flex; '
                     'flex-direction:column;}']))
    try:
        import json as _json

        from bokeh.events import DocumentReady
        from bokeh.models import CustomJS
        try:
            from .strategy_window import _load_settings
            _accent = _load_settings()['user'].get('theme_color')
        except Exception:
            _accent = None
        _css, _js = _bp.theme_css_js(bool(reg.get('theme')),
                                     bool(reg.get('fullscreen')), _accent)
        doc.js_on_event(DocumentReady, CustomJS(code=(
            "var _s=document.createElement('style');"
            f"_s.textContent={_json.dumps(_css)};"
            "document.head.appendChild(_s);" + _js)))
    except Exception as error:
        print('[bokeh] 注入主题/全屏 CSS 失败:', error)


def collect_total(reg: dict) -> int:
    """取所有已登记数据源中最大的 ``index``（即回放终点 bar）。"""
    total = 0
    full = reg.get('full') or {}
    for cds in reg.get('cds', []):
        idx = (full.get(cds.id) or {}).get('index')
        if idx is not None and len(idx):
            total = max(total, int(idx[-1]))
    return total


def reveal(reg: dict, current: int, window: int = 300,
           prev: int | None = None, refresh_every: int = 20) -> None:
    """揭开到 ``current``（``index <= current``），并把 x 轴窗口移到当前 bar。

    * ``current`` 是**全局 bar 索引**（各合约共用同一时钟）。
    * ``prev is None``：首次显示最后 ``window`` 根（右侧留一点空白）。
    * 否则：**只把当前可视范围整体平移** ``current-prev`` 步，保留用户缩放/平移后
      的跨度（不再重置「最后一根到右边缘的距离」，拖动/缩放不会被打乱）。
    * Y 轴不每帧重拟合（图都在建图时锁成固定 ``Range1d``）；每 ``refresh_every``
      次揭开，才把**所有图（主图+副图）**的 y 轴按当前可见数据整体刷新一次。
    """
    _bp.reveal_sources(reg, int(current))
    prev = None if prev is None else int(prev)
    cur = int(current)
    for xr in reg.get('x_ranges', []):
        try:
            if prev is None:
                # 首次：显示最后 window 根，右侧留一点空白
                pad = max(1, int(window * 0.02))
                xr.start = max(0, cur - window + 1)
                xr.end = cur + pad
            else:
                # 之后：**只把当前可视范围整体平移** (cur-prev) 步。保留用户缩放/平移
                # 后的跨度，不再把「最后一根到右边缘的距离」重置成固定值。
                shift = cur - prev
                if shift:
                    xr.start = xr.start + shift
                    xr.end = xr.end + shift
        except Exception:
            pass
    if prev is None:
        # 首次揭开：所有图的 y 轴此前是建图时按**整段数据**锁的（范围偏宽，K 线
        # 挤在上半部分）。这里先按“开局可见的那一段”重定一次。
        _refresh_subfigure_ranges(reg)
        _refresh_equity_ranges(reg)
    # 主图/副图 y 轴都已固定，不每帧重拟合；每 refresh_every 次揭开整体刷新一次。
    try:
        _n = int(reg.get('_reveal_n', 0)) + 1
    except Exception:
        _n = 1
    reg['_reveal_n'] = _n
    if int(refresh_every) > 0 and _n % int(refresh_every) == 0:
        _refresh_subfigure_ranges(reg)
        _refresh_equity_ranges(reg)


def _refresh_subfigure_ranges(reg: dict) -> None:
    """把副图 y 轴（已固定为 ``Range1d``）按**当前已揭开数据**刷新一次。"""
    for fig in reg.get('sub_figs') or []:
        # 权益曲线**单独**处理（见 _refresh_equity_ranges），这里不动它。
        if 'value-chart' in (getattr(fig, 'css_classes', None) or []):
            continue
        try:
            lo, hi = _bp.renderer_y_extent(fig)
            if lo is None or not (hi > lo):
                continue
            # 只有主 K 线图用「按价格比例」的余量；其它图（指标/成交量）
            # 只用按跨度的余量，否则权益(值 ~1e6)会被 ±1% 压成一条直线。
            if 'candle-chart' in (fig.css_classes or []):
                pad_lo, pad_hi = _bp.y_range_padding(lo, hi)
            else:
                pad_lo, pad_hi = _bp.y_range_padding(
                    lo, hi, frac_bottom=0.0, frac_top=0.0, frac_span=0.05)
            fig.y_range.start = lo - pad_lo
            fig.y_range.end = hi + pad_hi
        except Exception:
            pass


def _refresh_equity_ranges(reg: dict) -> None:
    """只刷新**权益曲线**的 y 轴（按当前已揭开的 ``value`` 范围收紧）。

    权益曲线在建图时已换成只 fit ``value`` 的固定 ``Range1d``（不含 level
    参考线）；这里每 N 帧按可见窗口重算一次，与其它图的刷新互不影响。
    """
    for item in reg.get('equity_figs') or []:
        try:
            fig, cds, key = item
            v = np.asarray(cds.data.get(key), dtype=float)
            v = v[np.isfinite(v)]
            if not v.size:
                continue
            lo, hi = float(v.min()), float(v.max())
            if not (hi > lo):
                p = max(abs(hi) * 0.001, 1.0)
                lo, hi = lo - p, hi + p
            pad = (hi - lo) * 0.05
            fig.y_range.start = lo - pad
            fig.y_range.end = hi + pad
        except Exception:
            pass


def _autoscale_price_y(reg: dict, current: int) -> None:
    """主 K 线图的价格 Y 范围跟着**可见窗口**走，**带滞回**（避免每 tick 抖动）。

    ``bokeh_plot.py`` 构建时把 y_range 固定成整段的 min/max；回放揭开后若不更新，
    新价格跑出这个固定范围就看不见了。但每 tick 都重算会让 Y 轴不停缩放/K 线
    “呼吸”。这里：

    * 当前范围**放不下**新价 → 扩到能放下（并留 5% 余量）；
    * 当前范围比需要的**宽出 2 倍以上** → 收一次；
    * 其余情况**保持不动**。

    这样 Y 轴只在真正越界/过宽时才动，所有合约都不抖。
    """
    full = reg.get('full') or {}
    for src, fig in reg.get('ohlc_figs', []):
        d = full.get(src.id)
        if not d or 'high' not in d or 'low' not in d:
            continue
        xr = getattr(fig, 'x_range', None)
        lo, hi = 0, int(current)
        if xr is not None:
            lo = max(0, int(np.floor(float(xr.start))))
            hi = min(int(current), int(np.ceil(float(xr.end))))
        if hi <= lo:
            continue
        try:
            low = float(np.nanmin(d['low'][lo:hi + 1]))
            high = float(np.nanmax(d['high'][lo:hi + 1]))
        except Exception:
            continue
        if not (high > low):
            continue
        pad = (high - low) * 0.05
        want_lo, want_hi = low - pad, high + pad
        try:
            yr = fig.y_range
            cur_lo, cur_hi = float(yr.start), float(yr.end)
            if not (cur_hi > cur_lo):
                yr.start, yr.end = want_lo, want_hi
                continue
            cur_span, want_span = cur_hi - cur_lo, want_hi - want_lo
            if want_lo < cur_lo or want_hi > cur_hi:
                yr.start = min(cur_lo, want_lo)
                yr.end = max(cur_hi, want_hi)
            elif want_span > 0 and cur_span > want_span * 2.0:
                yr.start = want_lo
                yr.end = want_hi
        except Exception:
            pass


def build_replay_app(strategys, *, start_bars: int = 300, period_ms: int = 1000,
                     speed: int = 1, window: int = 300,
                     plot_kwargs: dict | None = None):
    """构建 bokeh ``Application``（回放）。

    ``plot_kwargs`` 原样转交 ``bokeh_plot.plot``（``plot_width`` / ``volume_mode`` /
    ``theme`` / ``fullscreen`` 等），因此图表的默认设置完全由策略自身决定。
    """
    from bokeh.application import Application
    from bokeh.application.handlers.function import FunctionHandler
    from bokeh.layouts import column, row
    from bokeh.models import Button, Div, PreText, Select, Spacer

    plot_kwargs = dict(plot_kwargs or {})

    def make_document(doc):
        reg: dict = {}
        tabs = _bp.plot(strategys, live_sources=reg, build_only=True,
                        **plot_kwargs)
        total = collect_total(reg)

        state = {'index': int(start_bars) - 1, 'paused': False,
                 'speed': max(1, int(speed))}
        reveal(reg, state['index'], window)

        # width_policy='min'：只包住文字，由前面的 Spacer 把它顶到最右端
        # （之前是 'max'，占满剩余空间但文字左对齐，看起来偏中间）。
        info = PreText(text='', height=22, width_policy='min',
                       styles={'font-size': '12px', 'margin': '0',
                               'padding-right': '8px'})
        pause_btn = Button(label='暂停', button_type='warning', width=80)
        step_btn = Button(label='单步', width=70)
        # 「速度」标签用**不可用按钮**：高度/基线跟旁边按钮完全一致（Div 很难对齐，
        # 会显得比按钮文字高一点）；再去掉灰化样式，让它看起来就是个标题。
        speed_lbl = Button(label='速度', disabled=True, width=60,
                           styles={'opacity': '1', 'cursor': 'default',
                                   'font-weight': 'normal',
                                   'background-color': 'transparent',
                                   'border': 'none', 'box-shadow': 'none',
                                   'color': 'inherit'})
        speed_sel = Select(value=str(state['speed']),
                           options=[str(v) for v in (1, 2, 5, 10, 20, 50)],
                           width=80)

        def _update_info():
            info.text = (f'回放 {max(0, state["index"])}/{total}　'
                         f'速度 x{state["speed"]}　'
                         f'{"已暂停" if state["paused"] else "运行中"}')

        def _advance(n: int):
            if total <= 0:
                return
            old = state['index']
            state['index'] = min(total, old + max(1, int(n)))
            reveal(reg, state['index'], window, prev=old)
            if state['index'] >= total:
                state['paused'] = True
                pause_btn.label = '继续'

        def _on_pause():
            state['paused'] = not state['paused']
            pause_btn.label = '继续' if state['paused'] else '暂停'
            _update_info()

        def _on_step():
            state['paused'] = True
            pause_btn.label = '继续'
            _advance(1)
            _update_info()

        def _on_speed(attr, old, new):
            try:
                state['speed'] = max(1, int(new))
            except (TypeError, ValueError):
                state['speed'] = 1
            _update_info()

        pause_btn.on_click(_on_pause)
        step_btn.on_click(_on_step)
        speed_sel.on_change('value', _on_speed)
        _update_info()

        controls = row(pause_btn, step_btn, speed_lbl, speed_sel,
                       Spacer(width_policy='max'), info, width_policy='max')
        # 按钮栏与第一个顶栏（tabs）之间加一条细分隔线
        add_page_root(doc, controls, tabs, reg, separator=True)

        def tick():
            if state['paused'] or total <= 0:
                return
            if state['index'] >= total:
                state['paused'] = True
                pause_btn.label = '继续'
                return
            _advance(state['speed'])
            _update_info()

        doc.add_periodic_callback(tick, int(period_ms))

    return Application(FunctionHandler(make_document))


def run_replay(strategys, *, port: int | None = None, period_ms: int = 1000,
               speed: int = 1, start_bars: int | None = None, window: int = 300,
               plot_kwargs: dict | None = None, show: bool = True,
               session_token_expiration: int = 86400) -> None:
    """启动 bokeh 回放 server（阻塞，直到 Ctrl+C / IOLoop 退出）。"""
    from random import randint

    from bokeh.server.server import Server
    from tornado.ioloop import IOLoop

    quiet_bokeh_logging()
    if start_bars is None:
        start_bars = max(
            1, int(getattr(strategys[0], 'min_start_length', 300) or 300))
    if port is None:
        port = randint(1000, 9999)

    app = build_replay_app(strategys, start_bars=int(start_bars),
                           period_ms=period_ms, speed=speed, window=window,
                           plot_kwargs=plot_kwargs)
    io_loop = IOLoop.current()
    server = Server(applications={'/': app}, io_loop=io_loop, port=port,
                    session_token_expiration=session_token_expiration)
    print(f'【bokeh 回放】 http://localhost:{port}  （Ctrl+C 退出）')
    try:
        server.start()
        if show:
            server.show('/')
        io_loop.start()
    except KeyboardInterrupt:
        print('\n【bokeh 回放】 收到 Ctrl+C，成功退出')
    finally:
        # 回放是本地数据，不刷“天勤连接”相关输出
        close_apis(strategys, verbose=False)
