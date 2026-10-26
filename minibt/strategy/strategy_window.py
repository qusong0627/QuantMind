"""P1 骨架：纯 pylightcharts 的策略窗口（bokeh 式布局）。

    ┌──────────────── 页面顶栏 Window.page_topbar() ────────────────┐
    │ 策略：[ CCI ] [ MA ] [ Breakout ]      ← 固定不变，一眼全见     │
    ├──────────────── 图表顶栏 chart.topbar ────────────────────────┤
    │ 合约：[ pp2601 ] [ pp2605 ] [ 回测指标 ] [ 交易记录 ]           │
    │        ↑ 随上层策略动态重建（SwitcherWidget.update_items）      │
    ├───────────────────────────────────────────────────────────────┤
    │ 主图 = 策略的第一个 K 线                                       │
    │ 副图 = 策略其余 K 线 → 权益曲线 → 回撤曲线 → 每笔盈亏（散点）    │
    │        （回测指标页：所有 pane 等高；表格浮在右上角、顶栏下方）  │
    └───────────────────────────────────────────────────────────────┘

* **回测指标**页：策略的每条 K 线各占一个 pane，之后依次是权益、回撤、盈亏散点；
  所有 pane 等高（`BacktestPage.equalise()`），回测数据表固定在**右上角**、并且
  被推到两条顶栏**下面**（不会盖住顶栏）。
* **交易记录**页：居中的分页表格。
* 回测数据**按策略分别登记**（`set_backtest(strategy=..., ...)`），切策略时三个页面
  自动换成该策略的数据。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from base64 import b64decode
import pathlib
import re

from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from pylightcharts import Chart

try:                                       # 作为包的一部分导入（bt.py 用这个）
    from .pages import BacktestPage, ReportPage, StatsTable, TradeRecordsPage
    from ..utils import user_dir as _user_dir
    from ..utils import ensure_user_dir as _ensure_user_dir
except ImportError:                        # 直接按文件路径跑（演示/自测）
    _user_dir = _ensure_user_dir = None
    import importlib.util as _ilu
    import pathlib as _pl

    _spec = _ilu.spec_from_file_location(
        'minibt_pages', _pl.Path(__file__).with_name('pages.py'))
    _pages = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_pages)
    BacktestPage = _pages.BacktestPage
    StatsTable = _pages.StatsTable
    TradeRecordsPage = _pages.TradeRecordsPage
    ReportPage = _pages.ReportPage

#: 第二个顶栏里的两个信息页
INFO_PAGES = ('回测结果', '指标记录')

#: 图表左上角「K 线信息 + 指标标签」的**默认字号**（px）✓
#: （以前是 12 / 库默认 11，用户反馈“字体过小”✗ → 默认改成 **13** ✓；
#: 顶栏「设置 → 字号」仍可随时改 ✓）。
LEGEND_FONT_SIZE = 13


# ---------------------------------------------------------------- 用户设置
#: 用户设置目录：默认存到用户主目录（可写、不随包升级被覆盖）；
#: 可用环境变量 ``MINIBT_SETTINGS`` 覆盖位置。旧版在包内的 setting.json
#: 会自动迁移过来。画线单独一份文件（可能很大，主题/字体变动不应重写它）。
def _resolve_settings_dir() -> pathlib.Path:
    # 与 ``utils.user_dir()`` 保持一致（日志/优化/报告/图表都放同一目录）
    if _user_dir is not None:
        try:
            return pathlib.Path(_user_dir())
        except Exception:
            pass
    env = os.environ.get('MINIBT_SETTINGS')
    if env:
        path = pathlib.Path(env).expanduser()
        return path if path.suffix == '' else path.parent
    return pathlib.Path.home() / '.minibt'


_SETTINGS_DIR = _resolve_settings_dir()
#: 主题/字体等小设置
_SETTINGS_FILE = _SETTINGS_DIR / 'setting.json'
#: 画线单独存一份（可能很大）
_DRAWINGS_FILE = _SETTINGS_DIR / 'drawings.json'
#: 旧版位置（包内，且画线和设置同文件），首次运行时会迁移一次
_LEGACY_SETTINGS_FILE = pathlib.Path(
    __file__).resolve().parent / 'setting.json'
#: 默认设置（theme_color=None 表示用 pylightcharts 主题内置色）
_DEFAULT_SETTINGS = {'theme_color': None, 'font_size': LEGEND_FONT_SIZE,
                     'screenshot_path': ''}


def _read_json_file(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_file(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding='utf-8')


def _ensure_settings_migrated() -> None:
    """把旧版包内的 setting.json（含 drawings）迁移到用户目录，只做一次。"""
    try:
        if not _LEGACY_SETTINGS_FILE.exists():
            return
        if _SETTINGS_FILE.resolve() == _LEGACY_SETTINGS_FILE.resolve():
            return
        if not _SETTINGS_FILE.exists():
            _write_json_file(_SETTINGS_FILE, _read_json_file(
                _LEGACY_SETTINGS_FILE))
        if not _DRAWINGS_FILE.exists():
            legacy = _read_json_file(_LEGACY_SETTINGS_FILE)
            if legacy.get('drawings'):
                _write_json_file(_DRAWINGS_FILE,
                                 dict(legacy.get('drawings') or {}))
    except Exception as error:
        print('[strategy_window] 迁移设置文件失败:', error)


def _read_settings_file() -> dict:
    """读设置文件（只包含 default / user；画线在 drawings.json）。"""
    _ensure_settings_migrated()
    data = _read_json_file(_SETTINGS_FILE)
    data.pop('drawings', None)      # 旧格式残留
    return data


def _write_settings_file(data: dict) -> None:
    data = {k: v for k, v in data.items() if k != 'drawings'}
    _write_json_file(_SETTINGS_FILE, data)


def _read_drawings_file() -> dict:
    _ensure_settings_migrated()
    return _read_json_file(_DRAWINGS_FILE)


def _write_drawings_file(drawings: dict) -> None:
    _write_json_file(_DRAWINGS_FILE, dict(drawings or {}))


def _load_settings() -> dict:
    """返回 {'default': {...}, 'user': {...}}（user 优先，缺项用默认）。"""
    raw = _read_settings_file()
    default = {**_DEFAULT_SETTINGS, **(raw.get('default') or {})}
    user = {**default, **(raw.get('user') or {})}
    return {'default': default, 'user': user}


def _save_user_settings(**settings) -> None:
    """保存用户设置（保留 default / drawings 两段）。"""
    raw = _read_settings_file()
    raw.setdefault('default', dict(_DEFAULT_SETTINGS))
    user = {**_DEFAULT_SETTINGS, **(raw.get('default') or {}),
            **(raw.get('user') or {})}
    user.update(settings)
    raw['user'] = user
    _write_settings_file(raw)


def _restore_default_settings() -> None:
    """恢复默认：把 user 段重置回 default（保留 drawings）。"""
    raw = _read_settings_file()
    raw.setdefault('default', dict(_DEFAULT_SETTINGS))
    raw['user'] = dict(raw['default'])
    _write_settings_file(raw)


#: 「截图」的页面合成脚本（方案 A，不引第三方库）。
#:
#: 引擎的 `takeScreenshot()` 只含各个 pane，DOM 覆盖层（图例、顶栏、回测指标 /
#: 交易记录的 HTML 表格）不在里面。这里把图表容器整棵子树 + 顶栏重绘到同一张
#: 画布上：
#:
#:   * 背景：每个元素的 `getComputedStyle().backgroundColor`；
#:   * 文字：按**直接文本节点**取（不能只看叶子 —— 图例的名称/数值在多层
#:     span 里），用真实的 font-size/family/weight/color/textAlign 排布；
#:   * 边框：只画 TD/TH（表格），避免嵌套重复描边；
#:   * 图标：`<svg>` 里的 path / circle / line / polyline 用 `Path2D` 描出来
#:     （图例的眼睛、删除 X 就是这么画的）；
#:   * 完全落在区域外的元素跳过，所以大表格也只画可见部分。
#:
#: `CHART` 会替换成 `window.<chart.id>`，`WITH_TOPBAR` 替换成 true/false。
_SCREENSHOT_JS = """
;(() => {
    const host = CHART.div || document.body;
    const shot = CHART.chart.takeScreenshot();
    const chartRect = host.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const transparent = (value) => !value || value === 'transparent'
        || value === 'rgba(0, 0, 0, 0)';
    const NONE = new Set(['none', '', null, undefined]);

    const collect = (selectors) => {
        const found = [];
        for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') {
                    continue;
                }
                const box = node.getBoundingClientRect();
                if (box.width < 0.5 || box.height < 0.5) continue;
                found.push(node);
            }
        }
        return found;
    };
    const panels = collect(['[data-pylightcharts-panel]']);
    const legend = collect(['div.legend', '[class*="legend"]']);
    const topbars = collect(WITH_TOPBAR ? ['div.topbar'] : []);
    const nodes = panels.concat(legend, topbars);

    let left = chartRect.left;
    let top = chartRect.top;
    let right = chartRect.right;
    let bottom = chartRect.bottom;
    for (const node of nodes) {
        const box = node.getBoundingClientRect();
        left = Math.min(left, box.left);
        top = Math.min(top, box.top);
        right = Math.max(right, box.right);
        bottom = Math.max(bottom, box.bottom);
    }
    const width = Math.max(right - left, 1);
    const height = Math.max(bottom - top, 1);

    const out = document.createElement('canvas');
    out.width = Math.max(1, Math.round(width * ratio));
    out.height = Math.max(1, Math.round(height * ratio));
    const ctx = out.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.drawImage(shot, chartRect.left - left, chartRect.top - top,
                  chartRect.width, chartRect.height);

    const paint = (node) => {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        if (style.opacity === '0') return;
        const box = node.getBoundingClientRect();
        if (box.width < 0.5 || box.height < 0.5) return;
        const x = box.left - left;
        const y = box.top - top;
        if (x > width || y > height
            || x + box.width < 0 || y + box.height < 0) return;

        if (!transparent(style.backgroundColor)) {
            ctx.fillStyle = style.backgroundColor;
            ctx.fillRect(x, y, box.width, box.height);
        }
        const tag = node.tagName;
        if (tag === 'TD' || tag === 'TH') {
            const line = parseFloat(style.borderTopWidth) || 0;
            if (line > 0 && style.borderTopStyle !== 'none') {
                ctx.strokeStyle = style.borderTopColor;
                ctx.lineWidth = line;
                ctx.strokeRect(x + line / 2, y + line / 2,
                               Math.max(box.width - line, 0),
                               Math.max(box.height - line, 0));
            }
        }

        // 文字：逐个**文本节点**取它自己的矩形（Range），这样图标/色块与
        // 文字之间是布局里的真实间距，不会把文字画到父容器左边缘压住色块。
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'left';
        const size = parseFloat(style.fontSize) || 12;
        ctx.font = (style.fontWeight || '400') + ' ' + size + 'px '
            + style.fontFamily;
        ctx.fillStyle = style.color;
        for (const child of node.childNodes) {
            if (child.nodeType !== 3) continue;
            const content = (child.textContent || '').trim();
            if (!content) continue;
            const range = document.createRange();
            range.selectNodeContents(child);
            const spot = range.getBoundingClientRect();
            if (spot.width < 0.5 && spot.height < 0.5) continue;
            ctx.fillText(content, spot.left - left, spot.top - top + spot.height / 2);
        }
        for (const child of node.children) paint(child);
    };

    paint(host);
    for (const node of panels) paint(node);
    for (const node of topbars) paint(node);
    return out.toDataURL('image/png');
})()
"""


def make_frame(rows: int = 300, seed: int = 3,
               start: float = 6000.0) -> pd.DataFrame:
    """演示/测试用的随机 K 线。"""
    rng = np.random.default_rng(seed)
    index = pd.date_range('2024-01-01', periods=rows, freq='D')
    close = start * np.exp(np.cumsum(rng.normal(0.0004, 0.012, rows)))
    open_ = close * (1 + rng.normal(0, 0.004, rows))
    return pd.DataFrame({
        'time': index, 'open': open_.round(2),
        'high': (np.maximum(open_, close) * 1.008).round(2),
        'low': (np.minimum(open_, close) * 0.992).round(2),
        'close': close.round(2),
        'volume': np.abs(rng.normal(900, 200, rows)).round(),
    })


def _in_notebook() -> bool:
    """是否**真的**在 Jupyter/IPython kernel 里（而不是普通脚本）。

    不能只看"能不能 import IPython"：在普通脚本里 ``display(IFrame(...))``
    不会报错，只会打印一个 ``<IPython.lib.display.IFrame object at ...>``
    —— 既没有嵌图，也没有开浏览器。所以必须确认是 ZMQInteractiveShell。
    """
    try:
        from IPython import get_ipython
        shell = get_ipython()
        return (shell is not None
                and shell.__class__.__name__ == 'ZMQInteractiveShell')
    except Exception:
        return False


class StrategyWindow:
    """一个窗口里：策略顶栏 + 动态合约顶栏 + K 线/回测页面。"""

    def __init__(self, strategies: Sequence[str],
                 contracts: Callable[[str], Sequence[str]],
                 width: int = 1280, height: int = 860,
                 title: str = 'minibt - 策略窗口',
                 loader: Optional[Callable[[str, str], pd.DataFrame]] = None,
                 on_info: Optional[Callable[['StrategyWindow'], None]] = None,
                 panes: Optional[Dict[str, Sequence[str]]] = None,
                 overlays: Optional[Dict[str, Sequence[str]]] = None,
                 info_pages: bool = True,
                 browser: bool = False, live: bool = False,
                 port: int = 0, jupyter: bool = False,
                 extra_settings: Optional[
                     List[tuple]] = None,
                 theme: str = 'dark',
                 toolbox: bool = False,
                 grid: bool = False,
                 y_margin: Optional[tuple] = None,
                 bars: str = 'both',
                 live_tools: bool = False):
        self.strategies = list(strategies)
        self.contracts_of = contracts
        self.loader = loader or self._default_loader
        self.on_info = on_info
        #: Jupyter 单元格内联模式
        self._jupyter = bool(jupyter)
        #: 初始是否显示画线工具（由策略 toolbox / config.toolbox 传入）
        self.toolbox_initial = bool(toolbox)
        #: 是否显示网格线（由策略 config.grid 传入，默认关闭）
        self._grid = bool(grid)
        #: 页面顶栏会显示策略名 → 图例里**不再重复**策略名 ✗；
        #: `bars != 'both'`（页面顶栏被隐藏，如 btplot）时保留 —— 否则图表上
        #: 就完全没有策略名了 ✓
        self._strategy_in_legend = str(bars) != 'both'
        #: 价格轴上下余量 ``(top, bottom)``（0..1，也可传单个 float）；
        #: 不传时由 ``resolve_y_margin`` 给默认值（``DEFAULT_Y_MARGIN`` = **0.1** ✓）
        from ..utils import normalise_y_margin
        self._y_margin = normalise_y_margin(y_margin)
        #: 「设置」菜单里模式专属的项：``[(文字, 回调), ...]``
        #: （回测 → 重置；实时 → 断开连接）
        self._extra_settings = list(extra_settings or [])
        self._settings_handlers = {label: func
                                   for label, func in self._extra_settings}
        self._live = bool(live)
        self._browser = bool(browser)
        #: 主题切换后的回调（RealtimeChart 用它重设指标颜色）
        self.on_theme: Optional[Callable[[], None]] = None
        self.panes = dict(panes or {})
        self.overlays = dict(overlays or {})
        #: 是否带"回测结果 / 回测指标 交易记录"两个信息页（实时图不需要）
        self.info_pages = bool(info_pages)
        self.strategy = self.strategies[0] if self.strategies else ''
        self.contract = ''
        #: 最近一次显示的**合约**。信息页里 `self.contract` 是页面名（如
        #: "回测结果"），不能拿它判断"主图上现在是哪个合约"
        self._kline_contract: str = ''
        self.pane_series: List = []          # 指标副图的系列
        #: [(指标名, 窗口期), ...] —— 与 `pane_series` 一一对应
        self.pane_specs: List = []
        self.overlay_series: List = []       # 主图叠加指标（画在 K 线上）
        #: [(指标名, 窗口期), ...] —— 与 `overlay_series` 一一对应
        self.overlay_specs: List = []
        self.contract_panes: List = []       # 回测指标页里"其余 K 线"的系列
        #: 累计登记（只增不减）：清理时用它兜底，确保不留下孤儿 pane
        self._all_contract_panes: List = []
        self.backtests: Dict[str, Dict[str, Any]] = {}
        self.backtest: Optional[BacktestPage] = None
        self.report: Optional[ReportPage] = None     # 回测指标+交易记录 HTML 报表
        self.info_page = INFO_PAGES[0]
        self._info_shown = False
        self._equalised = False               # 下次进回测页重新设高度比
        #: 程序性调用 `topbar[...].set(...)` / `update_items(...)` 时置真：
        #: 这些都会**异步**触发回调，不挡住就会来回重建（画面不停闪）
        self._switching = False
        #: 每个页面（合约 / 信息页）离开前记住的可见 K 线根数
        self._view_ranges: Dict[str, int] = {}
        #: 切合约/重建期间收到的「可视范围变化」事件要忽略（那是过渡态 ✗）：
        #: 计数式锁，允许嵌套（外层 RealtimeChart 与这里可能都锁一层 ✓）
        self._view_lock_depth: int = 0
        #: JS 侧 ``subscribeVisibleLogicalRangeChange`` 是否已接好（幂等 ✓）
        self._view_tracking: bool = False
        #: 当前主题名与用户选的主题色（顶栏按下色 / 十字线标签背景）
        #  由策略 ``config.theme`` / ``self.theme`` 传入；默认深色。
        self._theme_name: str = ('light'
                                 if str(theme).strip().lower().startswith('light')
                                 else 'dark')
        _settings = _load_settings()
        self._accent: Optional[str] = _settings['user'].get('theme_color')
        self._font_size: int = int(
            _settings['user'].get('font_size', LEGEND_FONT_SIZE)
            or LEGEND_FONT_SIZE)
        #: 截图保存目录（空 = 未设置，点「截图」时先弹设置）
        self._screenshot_path: str = str(
            _settings['user'].get('screenshot_path') or '')
        #: 截图是否连两条顶栏一起截（图例始终包含）
        self.screenshot_with_topbar: bool = False

        if jupyter:
            # Jupyter：用**本地 SSE 服务**承载（而不是静态 JupyterChart）——
            # 只有 live 页面才有 JS→Python 的回调通道，两个顶栏/页面/按钮才生效；
            # 配合 `_show_in_jupyter` 的 width='100%' iframe 自适应窗口宽度。
            from pylightcharts import BrowserChart
            self.chart = BrowserChart(width=width, height=height,
                                      live=True, port=port)
            # 兜底：有些环境拖动 iframe 边界不会在子窗口触发 `resize`，
            # 用 ResizeObserver 主动 reLayout，保证图表跟着 iframe 一起缩放。
            try:
                self.chart.run_script(
                    "if (!window.__pl_resize_obs__) {"
                    " window.__pl_resize_obs__ = new ResizeObserver(function(){"
                    "   try { Lib.Handler.reLayoutAll(); } catch(e){} });"
                    " window.__pl_resize_obs__.observe(document.documentElement);"
                    " window.__pl_resize_obs__.observe(document.body);"
                    "}")
            except Exception as error:
                print('[strategy_window] 注入 resize observer 失败:', error)
        elif browser:
            # 网页版：静态（回测）或 live=True（实时/回放，SSE 推送）
            from pylightcharts import BrowserChart
            self.chart = BrowserChart(width=width, height=height,
                                      live=live, port=port)
        else:
            self.chart = Chart(width=width, height=height, title=title)
        # 放宽时间轴的“最小柱间距”：默认 0.5px 会使命中（键盘/滚轮）缩小时最多只能
        # 看到 宽/0.5 根 K 线（例如 1000px → 2000 根），10,000 根的分钟线就永远框不全。
        # 调小之后 `fit()` 之外的缩放也能覆盇全部数据。
        try:
            # right_offset：最后一根 K 线与右边缘留 20 根的距离（10~30 都行）
            self.chart.time_scale(right_offset=20, min_bar_spacing=0.005)
        except Exception as error:
            print('[strategy_window] time_scale(min_bar_spacing) 失败:', error)
        self.chart.legend(visible=True, font_size=self._font_size,
                          text=self.strategy if self._strategy_in_legend else '')
        # 初始主题（策略 config/self.theme，默认 dark）；放在加载数据之前，
        # 避免对空数据再做一次成交量重发（之前的顺序会让 set_strategy 先 set
        # 全量数据，主题再重发一次成交量）。
        self.chart.win.theme(self._theme_name)
        # 用户保存过的主题色优先于默认
        if self._accent:
            self.chart.win.theme(
                self._theme_name, active_background=self._accent,
                crosshair_label=self._accent)
        self._apply_grid()          # 网格线（config.grid，默认关闭）
        # 上层：策略（页面顶栏，固定不变）+ 右上角「画线工具/主题/设置」
        self.page_bar = self.chart.win.page_topbar()
        self.page_bar.switcher('strategy', tuple(self.strategies) or ('-',),
                               align='left', func=self._on_strategy)
        # 实时图专属按钮（买卖盘 / 二级报价）—— 放在「画线工具」**左边** ✓
        # 回调由 RealtimeChart 注入（``on_toggle_order_book`` / ``on_toggle_depth``）
        self.on_toggle_order_book = None
        self.on_toggle_depth = None
        if live_tools:
            self.page_bar.button('orderbook', '买卖盘', toggle=True,
                                 align='right', func=self._on_order_book)
            self.page_bar.button('depth', '二级报价', toggle=True,
                                 align='right', func=self._on_depth)
        self.page_bar.button('drawing', '画线工具', toggle=True, align='right',
                             func=self._on_drawing)
        self.page_bar.menu('theme', ('深色', '浅色'),
                           default='深色' if self._theme_name == 'dark' else '浅色',
                           align='right', func=self._on_theme)
        self._base_settings_items = [
            '主题色', '截图路径设置', '清空当前画线',
            '清空所有画线', '字体变大', '字体变小', '恢复默认']
        #: 「设置」菜单完整项（基础项 + 模式专属项）
        self._settings_items = self._base_settings_items + [
            label for label, _ in self._extra_settings]
        self.page_bar.menu('settings', tuple(self._settings_items),
                           default='设置', align='right',
                           func=self._on_settings_menu)
        self.page_bar.button('shot', '截图', align='right',
                             func=self._on_screenshot)
        self._set_button_tooltip('drawing', '显示 / 隐藏画线工具', '画线工具')
        self._set_button_tooltip('theme', '切换深色 / 浅色主题',
                                 '深色' if self._theme_name == 'dark' else '浅色')
        self._set_button_tooltip('settings', '主题色、截图路径、字体等设置', '设置')
        self._set_button_tooltip('shot', '截图当前页面（Ctrl+S）', '截图')
        try:                                  # 快捷键：Ctrl+S 截图
            self.chart.hotkey('ctrl', 's', self._on_screenshot)
        except Exception as error:
            print('[截图] 注册快捷键失败:', error)
        # 下层：当前策略的合约 + 两个信息页（随策略动态重建）
        self.chart.topbar.switcher('contract', tuple(self._bar_items()),
                                   align='left', func=self._on_contract)
        self._setup_toolbox()                # 画线工具（只作用于 K 线图）
        self._apply_topbar_gap()             # 顶栏控件间距 5px
        self.set_strategy(self.strategy)
        self._apply_font()                    # 应用用户保存的字体大小
        # 顶栏显隐：'both'（默认）/ 'chart'（只留图表顶栏）/ 'none'（都不要）。
        # 隐藏后必须 `Lib.Handler.reLayoutAll()` 重新按“窗口高 - 可见顶栏高”
        # 算图表高度，否则图表仍按“带顶栏”的高度排布，底部会留 1~2 个顶栏的空隙。
        if bars in ('chart', 'none'):
            try:
                self.page_bar._create()
                self.page_bar.run_script(
                    f"{self.page_bar.id}._div.style.display = 'none'")
            except Exception as error:
                print('[strategy_window] 隐藏页面顶栏失败:', error)
        if bars == 'none':
            try:
                self.chart.topbar._create()
                self.chart.topbar.run_script(
                    f"{self.chart.topbar.id}._div.style.display = 'none'")
            except Exception as error:
                print('[strategy_window] 隐藏图表顶栏失败:', error)
        if bars != 'both':
            try:
                # 等一帧让 display:none 生效后再重排
                self.chart.win.run_script(
                    'requestAnimationFrame(function () {'
                    '  try { Lib.Handler.reLayoutAll(); } catch (e) {}'
                    '});')
            except Exception as error:
                print('[strategy_window] 顶栏隐藏后重排失败:', error)

    # ------------------------------------------------------------------ 数据
    @staticmethod
    def _default_loader(strategy: str, contract: str) -> pd.DataFrame:
        return make_frame(seed=abs(hash((strategy, contract))) % 97 + 1)

    def _bar_items(self) -> List[str]:
        """第二个顶栏的项：**当前策略**的合约（+ 信息页，若启用且有回测数据）。"""
        items = [*self.contracts_of(self.strategy)]
        if self.info_pages and self._has_backtest():
            items += list(INFO_PAGES)
        return items

    def _has_backtest(self, strategy: Optional[str] = None) -> bool:
        """当前策略是否有回测数据（有才显示两个信息页）。

        有非空交易记录**或**权益曲线**真的变化过**才算有。

        ★ 无 ``next`` / 不交易的策略（纯指标，如 ``RollingVolumeProfile``）回测
        循环照跑，会得到一条**恒定等于期初资金**的权益曲线 —— 那不是“回测
        数据”，不该因此多出「回测结果 / 指标记录」两页 ✗（用户反馈 ``vp.py``
        的四个纯 VP 策略都出现了这两页）。
        """
        data = self.backtests.get(strategy or self.strategy)
        if not data:
            return False
        trades = data.get('trades')
        if trades is not None and len(trades) > 0:
            return True
        equity = data.get('equity')
        if equity is None:
            return False
        # 平权益（nunique == 1）= 没交易过 → 不算回测数据
        try:
            column = (equity['value']
                      if 'value' in getattr(equity, 'columns', [])
                      else equity.iloc[:, -1])
            return bool(pd.Series(column).nunique() > 1)
        except Exception:                                        # noqa: BLE001
            return True

    def _refresh_contract_bar(self) -> None:
        """第二个顶栏的项可能变了（信息页从无到有/从有到无），原地重建。

        选项没变就直接返回：回放时 `refresh_every=1` 会每根都调 `set_backtest`，
        每根重建 switcher 会把按钮点没了/选中被重置，看起来就是“切页失败”。
        """
        items = self._bar_items()
        if getattr(self, '_bar_cache', None) == items:
            return
        self._bar_cache = list(items)
        self._switching = True
        try:
            self.chart.topbar['contract'].update_items(*items)
        finally:
            self._switching = False
        if items and self.contract not in items:
            self.set_contract(items[0], force=True)

    # -------------------------------------------------------------- 切换策略
    def set_strategy(self, strategy: str,
                     contract: Optional[str] = None) -> None:
        """切换策略：第二个顶栏换成它的合约（+ 信息页），并加载第一个合约。"""
        if strategy == self.strategy and contract is None and self.contract:
            return
        self.strategy = strategy
        self._switching = True
        try:
            self.chart.topbar['contract'].update_items(*self._bar_items())
            self.chart.legend(
                visible=True,
                font_size=self._font_size,
                text=strategy if self._strategy_in_legend else '')
            self.page_bar['strategy'].set(strategy)   # 策略条在页面顶栏上
        finally:
            self._switching = False
        target = contract or self.contracts_of(strategy)[0]
        self.set_contract(target, force=target == self.contract)

    def set_contract(self, contract: str, force: bool = False) -> None:
        """切换合约或信息页（`force` 忽略"已经在显示"的短路）。

        离开前记住可视范围，回来时恢复（``_view_ranges``）✓；整个重建过程把
        `_view_lock_depth` 锁上，避免过渡态的 JS 范围事件把记录改坏 ✗。
        """
        if contract == self.contract and not force:
            return
        self.install_view_tracking()        # 让 JS 主动回报可视范围 ✓
        self._save_view(self.contract)      # 记住离开前的可视范围
        self._lock_view()
        try:
            self.contract = contract
            if contract not in INFO_PAGES:
                self._kline_contract = contract   # 主图现在显示的是它
            self._switching = True
            try:
                self.chart.topbar['contract'].set(contract)
            finally:
                self._switching = False
            if contract in INFO_PAGES:
                self._hide_toolbox()             # 画线工具只在 K 线图生效
                self._show_info(contract)
                # ★ 回测结果页会建出**一堆副图 pane**（其余合约 + 权益/回撤/盈亏）；
                #   这里如果不重新套一次留白，那些 pane 就还是 LWC 原样的
                #   (0.2, 0.1) ✗（主图是 0.1 → 两边不一致，用户反馈）。
                self._apply_y_margin()
                self._restore_view(contract)    # 回到上次这个页面的可视范围
                return
            self._hide_info()
            self._restore_legend()               # 指标记录页曾把图例隐藏，这里恢复
            self._clear_panes()
            self._clear_overlays()
            self.chart.set(self.loader(self.strategy, contract))
            self._build_overlays()
            self._build_panes()
            self.chart.fit()
            self._reapply_theme()
            self._apply_pane_stretch()          # 主图 : 副图 = 3:1
            # 加载该合约（+周期）的画线
            self.chart.toolbox.load_drawings(contract)
            if self._toolbox_on:
                self._show_toolbox()
            # ★ 放**最后**恢复：上面建 pane / 加载画线都可能扰动视口 ✓
            self._restore_view(contract)        # 有记录就恢复，否则保持 fit()
        finally:
            self._unlock_view()

    # ------------------------------------------------------------ 回测信息页
    def set_backtest(self, stats: Optional[Dict[str, Any]] = None,
                     equity: Any = None, trades: Any = None,
                     *, drawdown: Any = None, pnl: Any = None,
                     strategy: Optional[str] = None,
                     rows_per_page: int = 200) -> 'StrategyWindow':
        """登记**某个策略**的回测结果（缺省当前策略）。

        * `stats`：回测指标表的键值字典（键见 `StatsTable.DEFAULT_ROWS`）
        * `equity` / `drawdown` / `pnl`：权益曲线 / 回撤曲线 / 每笔盈亏（散点）
        * `trades`：交易记录（DataFrame / list[dict] / list[tuple]）
        """
        key = strategy or self.strategy
        self.backtests[key] = {
            'stats': stats, 'equity': equity, 'drawdown': drawdown,
            'pnl': pnl, 'trades': trades, 'rows_per_page': rows_per_page}
        if key == self.strategy:
            self._refresh_contract_bar()     # 信息页从无到有 / 从有到无
        if self._info_shown:
            # 刷新数据：不动用户的缩放位置（否则回放时每根都会 fit 回弹）
            self._show_info(self.info_page, reset_view=False)
        return self

    def _show_info(self, page: str = INFO_PAGES[0],
                   reset_view: bool = True) -> None:
        """显示某个信息页（只显示它，另一页的部件收起）。

        `reset_view=False` 用于“数据刷新”（回放中每根 K 线重算回测时），
        只更新数据、不动用户的缩放位置。
        """
        if self.on_info is not None:
            self.on_info(self)
            return
        data = self.backtests.get(self.strategy)
        if not data:
            print(f'[info] {self.strategy} 还没有回测数据：先调 '
                  'set_backtest(strategy=..., stats=..., equity=..., '
                  'drawdown=..., pnl=..., trades=...)')
            return
        self._info_shown = True
        self.info_page = page
        self._clear_panes()                      # 指标副图让位
        self._clear_overlays()                   # 主图叠加指标也让位
        if page == INFO_PAGES[0]:
            self._reset_main_contract()          # 主图固定第一个合约
            self._build_contract_panes()         # 策略的其余 K 线
            self.chart.show_data()               # 回测页要显示主图 K 线
            self._show_backtest(data)
            if reset_view:
                self.chart.fit()                 # 首次进入时拟合全部数据
            if self.report is not None:
                self.report.hide()               # 回测页不放表格
        else:
            self._clear_contract_panes()         # 表格页：不要 K 线
            self.chart.hide_data()               # 主图 K 线也隐藏（数据保留）
            self._show_trades(data)              # 整页 HTML 报表
        self._reapply_theme()               # 新建的副图 pane 补上主题色
        # 回测结果页保留图例；整页 HTML 报表页不再显示左上角的图例。
        # `legend()` 不带 color 会退回默认灰，所以显式传主题文字色。
        if page == INFO_PAGES[0]:
            theme = getattr(self.chart.win, '_theme', None) or {}
            self.chart.legend(
                visible=True,
                font_size=self._font_size,
                text=(f'{self.strategy} - {page}' if self._strategy_in_legend
                      else page),
                color=theme.get('text', 'rgb(191, 195, 203)'))
        else:
            self.chart.legend(visible=False)

    def _show_backtest(self, data: Dict[str, Any]) -> None:
        """回测页：三块回测图（等高）。指标表在另一个页上，这里不放表格。"""
        if self.backtest is None:
            self.backtest = BacktestPage(self.chart, name='权益')
        backtest = self.backtest              # 用局部变量：关窗时可能被置 None
        backtest.update(data.get('equity'), drawdown=data.get('drawdown'),
                        pnl=data.get('pnl'))
        if getattr(self, '_equalised', False):
            return                            # 高度比已经设过：每根重算会让整页抖
        try:
            backtest.equalise()
            self._equalised = True
        except Exception as error:            # 关窗竞态时别把步进线程带走
            print('[strategy_window] pane 比例设置失败:', error)

    def _show_trades(self, data: Dict[str, Any]) -> None:
        """回测指标 + 交易记录页：整页 HTML 报表（盖住图表画布）。"""
        if self.backtest is not None:
            self.backtest.remove()
            self.backtest = None
        if self.report is None:
            self.report = ReportPage(
                self.chart, stats=data.get('stats'),
                trades=data.get('trades'))
        else:
            self.report.update(stats=data.get('stats'),
                               trades=data.get('trades'))
        self.report.show()

    def _restore_legend(self) -> None:
        """K 线页恢复左上角图例（切到「指标记录」页时 `legend(visible=False)`
        把 JS 的 legend div 隐藏了，切回 K 线必须显式再显示一次）。"""
        try:
            theme = getattr(self.chart.win, '_theme', None) or {}
            self.chart.legend(
                visible=True,
                font_size=self._font_size,
                text=self.strategy if self._strategy_in_legend else '',
                color=theme.get('text', 'rgb(191, 195, 203)'))
        except Exception as error:
            print('[strategy_window] 恢复图例失败:', error)

    def _hide_info(self) -> None:
        """离开信息页：收起回测图与表格、删掉“其余 K 线”的 pane。"""
        if not self._info_shown and not self.backtests:
            return
        self._info_shown = False
        self.chart.show_data()                   # 恢复主图 K 线（曾为了表格页隐藏）
        if self.report is not None:
            self.report.hide()
        if self.backtest is not None:
            self.backtest.remove()
            self.backtest = None
        self._clear_contract_panes()

    # ------------------------------------------------------------- 副图管理
    def _clear_panes(self) -> None:
        for series in self.pane_series:
            series.delete()                      # 空 pane 由引擎自动去掉
        self.pane_series = []

    def _clear_overlays(self) -> None:
        for series in self.overlay_series:
            series.delete()                      # 主图叠加线
        self.overlay_series = []

    def _build_overlays(self) -> None:
        """主图叠加指标（画在 K 线上，共用主图价格轴），如 `MA 30`。"""
        frame = self.chart.candle_data
        self.overlay_specs = []
        for name in self.overlays.get(self.strategy, ()):
            window = self._window_of(name)
            series = self.chart.add_series('Line', name=name, line_width=2)
            if not getattr(frame, 'empty', True) and 'time' in frame.columns:
                series.set(self._indicator_frame(frame, name, window))
            self.overlay_series.append(series)
            self.overlay_specs.append((name, window))

    @staticmethod
    def _window_of(name: str) -> int:
        """指标名里的数字当窗口期（`MA 30` -> 30，`RSI 14` -> 14），否则 20。"""
        found = re.search(r'(\d+)\s*$', str(name))
        return int(found.group(1)) if found else 20

    @staticmethod
    def _indicator(frame: pd.DataFrame, name: str, window: int) -> pd.Series:
        """按指标名算一条**与 K 线等长**的序列（`MA N` 均线 / `RSI N`）。"""
        close = frame['close'].astype(float)
        if 'rsi' in str(name).lower():
            delta = close.diff()
            gain = delta.clip(lower=0.0)
            loss = (-delta).clip(lower=0.0)
            avg_gain = gain.ewm(alpha=1.0 / window, min_periods=1,
                                adjust=False).mean()
            avg_loss = loss.ewm(alpha=1.0 / window, min_periods=1,
                                adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0.0, np.nan)
            return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)
        return close.rolling(window, min_periods=1).mean()

    def _indicator_frame(self, frame: pd.DataFrame, name: str,
                         window: int) -> pd.DataFrame:
        """`_indicator` 的结果带上 `time` 列，可直接 `series.set`。"""
        return pd.DataFrame({'time': frame['time'],
                             name: self._indicator(frame, name, window)})

    def _apply_pane_stretch(self, main: float = 3.0,
                            sub: float = 1.0) -> None:
        """K 线图表：主图 K 线 : 所有副图（含蜡烛图副图指标）= 3:1。"""
        try:
            for index in range(self.chart.pane_count()):
                self.chart.set_pane_stretch(index, main if index == 0 else sub)
        except Exception:
            pass

    def _build_panes(self) -> None:
        """给当前策略建指标副图（每个指标一个 pane + 一条线），如 `RSI 14`。"""
        frame = self.chart.candle_data
        self.pane_specs = []
        for name in self.panes.get(self.strategy, ()):
            window = self._window_of(name)
            pane = self.chart.add_pane()
            series = self.chart.pane_add_series(pane, 'Line', name=name,
                                                line_width=2)
            if not getattr(frame, 'empty', True) and 'time' in frame.columns:
                series.set(self._indicator_frame(frame, name, window))
            # 空图（开窗时还没行情）就先留空，refresh_panes() 会在行情到了后补齐
            self.pane_series.append(series)
            self.pane_specs.append((name, window))

    def refresh_panes(self) -> None:
        """把指标（主图 overlay + 副图 pane）推到最新，且**长度始终等于 K 线**。

        长度不一致（开窗后才来行情、或数据被重新 `set`）就整条重建；
        否则只覆盖最后一根（同一根里的 tick 不新增点）。
        """
        frame = self.chart.candle_data
        if frame is None or frame.empty:
            return
        when = frame['time'].iloc[-1]
        targets = self.overlay_series + self.pane_series
        specs = self.overlay_specs + self.pane_specs
        for series, (name, window) in zip(targets, specs):
            data = getattr(series, 'data', None)
            if data is None or not len(data) or len(data) != len(frame):
                series.set(self._indicator_frame(frame, name, window))
                continue
            series.update(pd.Series(
                {'time': when, name: self._indicator(frame, name, window).iloc[-1]}))

    _TOPBAR_GAP_JS = """
    ;(() => {
        const apply = () => {
            // styles.css 里 `.topbar-button { margin: 4px 10px }`：左右各
            // 10px，两个按钮之间就是 20px —— 真正的"间距很大"来自这里
            document.querySelectorAll('.topbar-button, .topbar-textbox')
                .forEach((el) => {
                    el.style.marginLeft = '0';
                    el.style.marginRight = '0';
                });
            document.querySelectorAll('.topbar-container').forEach((bar) => {
                bar.style.gap = '5px';
                bar.style.columnGap = '5px';
            });
        };
        if (!document.getElementById('pylightcharts-topbar-gap')) {
            const style = document.createElement('style');
            style.id = 'pylightcharts-topbar-gap';
            style.textContent =
                '.topbar-seperator { width: 0; margin: 0; }';
            document.head.appendChild(style);
        }
        apply();
        // 「播放/单步/速度」是图表顶栏建好之后才加的按钮：用观察者保证
        // **任何时候**新增的按钮/容器都被设成同样的间距（否则只有第一
        // 个顶栏生效，第二个还是 20px）
        if (!window.__pylightchartsGapObserver) {
            window.__pylightchartsGapObserver = new MutationObserver(apply);
            window.__pylightchartsGapObserver.observe(
                document.body || document.documentElement, {
                    childList: true, subtree: true,
                });
        }
    })();
    """

    def _apply_topbar_gap(self) -> None:
        """把顶栏控件之间的间距设成 5px（第 1、第 2 个顶栏都生效）。"""
        try:
            self.chart.run_script(self._TOPBAR_GAP_JS.strip())
        except Exception as error:
            print('[strategy_window] 设置顶栏间距失败:', error)

    def _set_progress(self, text: str = '') -> None:
        """在第二个顶栏右侧显示一行文字（回放进度用）。

        顶栏控件没有"纯文本"类型，这里直接往顶栏容器尾部塞一个 div ——
        不改 JS bundle，随主题用 CSS 变量着色。
        """
        try:
            self.chart.run_script(f"""
            ;(() => {{
                const wanted = {json.dumps(text)};
                let node = document.getElementById('pylightcharts-progress');
                if (!node) {{
                    node = document.createElement('div');
                    node.id = 'pylightcharts-progress';
                    node.style.cssText =
                        // 不要用 margin-left:auto —— 它会把前面的「播放/单步/
                        // 速度」推到左边，中间留出一大段空白
                        'padding:0 5px;white-space:nowrap;'
                        + 'font:12px sans-serif;opacity:0.85;'
                        // 不能挡住右侧的切换按钮（否则点不动）
                        + 'pointer-events:none;'
                        + 'color:var(--color,#d1d4dc);';
                    const bars = document.querySelectorAll('.topbar-container');
                    const bar = bars.length ? bars[bars.length - 1]
                                            : document.querySelector('.topbar');
                    if (bar) bar.appendChild(node);
                }}
                node.textContent = wanted;
            }})();
            """)
        except Exception as error:
            print('[strategy_window] 更新进度显示失败:', error)

    def _sweep_orphan_panes(self) -> None:
        """兜底清扫：删掉"名字是合约、却不在主图"的蜡烛系列。

        来回切页时 `contract_panes` 可能被重建过，只按列表删会漏；漏掉的那些
        会留在图上，表现为"多出来一个与某合约一样的 K 线图"。
        """
        contracts = set(self.contracts_of(self.strategy))
        for line in list(getattr(self.chart, '_lines', [])):
            if getattr(line, 'name', '') not in contracts:
                continue
            try:
                if int(line.pane_index) == 0:
                    continue                      # 主图上的那个留着
            except Exception:
                continue
            try:
                line.delete()
            except Exception:
                pass
        self.contract_panes = []
        self._all_contract_panes = []
        self._contract_panes_for = None

    def _main_contract(self) -> str:
        """主图当前显示的是哪个**合约**。

        不能直接用 `self.contract`：信息页里它是页面名（"回测结果"），按它
        过滤"其余合约"时一个都排除不掉 —— 两个合约都建了副图，加上主图就是
        3 个 K 线图（两张还是同一个合约）。
        """
        if self._kline_contract:
            return self._kline_contract
        items = list(self.contracts_of(self.strategy))
        return items[0] if items else ''

    def _reset_main_contract(self) -> None:
        """报告页的主图固定显示**第一个合约**。

        否则主图会留着"上次在 K 线页选中的合约"，回放时 `chart.update()` 更新
        的就是它 —— 表现就是"第一个 K 线图更新的是第二个合约的数据"。
        切回第一个合约时 loader 会同步回放时钟，`index` 只做范围夹取，不会倒退。
        """
        contracts = list(self.contracts_of(self.strategy))
        if not contracts:
            return
        first = contracts[0]
        if self._kline_contract == first:
            return
        self._kline_contract = first
        try:
            self.chart.set(self.loader(self.strategy, first))
        except Exception as error:            # 关窗竞态：别把回放线程带走
            print('[strategy_window] 报告页切回第一个合约失败:', error)

    def _contract_frame(self, contract: str) -> pd.DataFrame:
        """读某合约的 K 线，并把**时间轴按位置对齐到主图** ✓。

        多合约策略里各合约的周期/起始时间往往不同（例：60min 与 300min，或者
        新上市合约只有最后一个月的数据 ✗）——而引擎用的是**全局时间轴**，
        起始更晚的合约就会整体偏右 ✗（用户反馈：“一个策略里第一个合约是主图，
        其它合约的开始位置要同主图对齐”）。

        这里把该合约的 ``time`` 列换成主图**从头开始**的同位置时间戳（根数不足
        就只用前 N 个 ✓），于是它的**第一根就落在主图第一根的位置** ✓、根数也与
        主图一一对应 ✓。

        ⚠ 只在「回测结果页的多合约 pane」用：x 轴的含义变成**主图合约的时间**
        （多合约对比图的通行做法 ✓）。
        """
        frame = self.loader(self.strategy, contract)
        if frame is None or getattr(frame, 'empty', True):
            return frame
        main = getattr(self.chart, 'candle_data', None)
        if main is None or getattr(main, 'empty', True) or \
                'time' not in getattr(main, 'columns', []):
            return frame
        if 'time' not in frame.columns:
            return frame
        times = list(main['time'])[:len(frame)]
        if not times:
            return frame
        out = frame.reset_index(drop=True).copy()
        out.loc[:len(times) - 1, 'time'] = times
        # ★ 让**副图蜡烛的图例行**也能显示成交量：LWC 只会在蜡烛数据里保留
        #   ``customValues``（其它额外列会被引擎丢掉 ✗），所以把 volume 塞进
        #   ``customValues`` ✓ —— 这样图例行就跟主图一样是
        #   ``D | O | H | L | C | V | +pct%`` ✓（用户要求）。
        if 'volume' in out.columns:
            out['customValues'] = [
                None if pd.isna(value) else {'volume': float(value)}
                for value in out['volume']]
        return out

    def _build_contract_panes(self) -> None:
        """回测指标页：主图上那个合约留着，其余合约各占一个 pane。

        合约列表没变时**只推数据**（复用 pane / 序列）：每根回测刷新都删了重建
        会让引擎重算缩放（回测结果页的 K 线因此抖动），而且重建出来的 pane 会被
        追加到末尾（第二个合约的 K 线跑到权益/回撤/盈亏下面）。
        """
        contracts_now = [c for c in self.contracts_of(self.strategy)
                         if c != self._main_contract()]
        if getattr(self, '_contract_panes_for', None) != contracts_now:
            # 只在合约集合变化时清扫（否则每 5 根刷新都重建，会闪）
            self._sweep_orphan_panes()
        if self._sync_contract_panes():
            return
        self._clear_contract_panes()
        # 副图 = 除"主图那个合约"以外的合约，一人一个 pane
        contracts = contracts_now
        for contract in contracts:
            pane = self.chart.add_pane()
            series = self.chart.pane_add_series(pane, 'Candlestick',
                                                name=contract,
                                                price_line_visible=False)
            frame = self._contract_frame(contract)
            series.set(frame)
            series._bars = len(frame)
            self.contract_panes.append(series)
            self._all_contract_panes.append(series)
        self._contract_panes_for = contracts
        self._order_contract_panes()
        self._equalised = False               # pane 变了：高度比要重设

    def _sync_contract_panes(self) -> bool:
        """合约没变：只推新增的 K 线（增量），返回 True 表示已处理。"""
        contracts = [c for c in self.contracts_of(self.strategy)
                     if c != self._main_contract()]
        if not self.contract_panes or getattr(
                self, '_contract_panes_for', None) != contracts:
            return False
        for series, contract in zip(self.contract_panes, contracts):
            frame = self._contract_frame(contract)
            if frame is None or frame.empty:
                continue
            previous = int(getattr(series, '_bars', 0) or 0)
            if len(frame) <= previous:
                series.set(frame)
            else:
                for _, row in frame.iloc[previous:].iterrows():
                    series.update(row)
            series._bars = len(frame)
        return True

    def _order_contract_panes(self) -> None:
        """把合约 K 线 pane 挪到主图之后（报告页的权益/回撤/盈亏排在它们后面）。

        引擎按创建时间排 pane：报告页的 pane 往往建得比第二个合约早，于是新
        合约的 K 线会落到最底下。用 `swapPanes` 逐个归位。
        """
        for offset, series in enumerate(self.contract_panes):
            target = 1 + offset
            try:
                current = int(series.pane_index)
            except Exception:
                continue
            if current == target:
                continue
            try:
                self.chart.swap_panes(current, target)
            except Exception as error:
                print('[strategy_window] K 线 pane 排序失败:', error)

    def _clear_contract_panes(self) -> None:
        """删掉"其余 K 线"的 pane。

        用**累计登记表**兜底：中途 `contract_panes` 可能被重建过，只按它删会
        漏掉早先创建的系列 —— 那些 pane 会留在图上，看起来就是"多出来一个与
        某合约一样的 K 线图"。
        """
        seen = set()
        for series in list(getattr(self, '_all_contract_panes', [])) + \
                list(self.contract_panes):
            if id(series) in seen:      # 两个列表会重叠，别删两次
                continue
            seen.add(id(series))
            try:
                series.delete()
            except Exception:
                pass
        self._all_contract_panes = []
        self.contract_panes = []
        self._contract_panes_for = None

    # ---------------------------------------------------------------- 回调
    def _on_strategy(self, chart) -> None:
        if self._switching:
            return
        widget = self.page_bar['strategy']
        value = widget.value
        if value not in widget.options or value == self.strategy:
            return
        self.set_strategy(value)

    def _on_contract(self, chart) -> None:
        if self._switching:
            return
        widget = self.chart.topbar['contract']
        value = widget.value
        # `update_items()` 之后可能还会飘来旧策略的合约名（不在新选项里），丢掉
        if value not in widget.options or value == self.contract:
            return
        self.set_contract(value)

    def _on_theme(self, win=None) -> None:
        """顶栏「主题」菜单：深色 / 浅色切换整窗配色（保留已选的主题色）。"""
        selected = self.page_bar['theme'].value
        self._theme_name = 'light' if selected == '浅色' else 'dark'
        try:
            if self._accent:
                self.chart.win.theme(self._theme_name,
                                     active_background=self._accent,
                                     crosshair_label=self._accent)
            else:
                self.chart.win.theme(self._theme_name)
        except Exception as error:
            print('[strategy_window] 切换主题失败:', error)
        self._set_button_tooltip('theme', '切换深色 / 浅色主题',
                                 '深色' if self._theme_name == 'dark'
                                 else '浅色')
        if self.on_theme:
            self.on_theme()

    def _change_font(self, delta: int) -> None:
        """字体变大/变小（8~24），并保存到 setting.json。"""
        self._font_size = max(8, min(24, self._font_size + delta))
        self._apply_font()
        _save_user_settings(font_size=self._font_size)

    def _apply_font(self) -> None:
        """应用字体大小：图表轴/图例 + 顶栏/菜单（不动背景色）。"""
        size = self._font_size
        try:
            # 只改 fontSize：不能用 chart.layout()——它的 background_color
            # 默认是 #000000，会把浅色主题的背景重置成黑色
            self.chart.apply_options(layout={'font_size': size})
        except Exception:
            pass
        options = getattr(self.chart, '_legend_options', None)
        if options:
            try:
                self.chart.legend(**{**options, 'font_size': size})
            except Exception:
                pass
        self.chart.run_script(f'''
        ;(() => {{
            let style = document.getElementById('pylightcharts-font-size');
            if (!style) {{
                style = document.createElement('style');
                style.id = 'pylightcharts-font-size';
                document.head.appendChild(style);
            }}
            style.textContent = '.topbar-button, .topbar-textbox-input, '
                + '.topbar-menu button {{ font-size: {size}px !important; }}';
        }})();
        ''')

    def replace_setting(self, old_label: str, new_label: str,
                        callback: Optional[Callable] = None) -> None:
        """替换模式专属菜单项的**文字/回调**（用于「断开连接」↔「重新连接」）。"""
        changed = False
        for index, (label, func) in enumerate(self._extra_settings):
            if label == old_label:
                self._extra_settings[index] = (
                    new_label, callback if callback is not None else func)
                changed = True
                break
        if not changed:
            return
        self._settings_handlers = {label: func
                                   for label, func in self._extra_settings}
        self._settings_items = self._base_settings_items + [
            label for label, _ in self._extra_settings]
        try:
            self.page_bar['settings'].update_items(*self._settings_items)
        except Exception as error:
            print('[strategy_window] 更新设置菜单失败:', error)

    def _on_settings_menu(self, win=None) -> None:
        """顶栏「设置」菜单：主题色 / 清空画线 / 恢复默认。"""
        selected = self.page_bar['settings'].value
        # 点完菜单项后按钮会变成该项文字，把它恢复成「设置」
        try:
            self.page_bar['settings'].run_script(
                f"{self.page_bar['settings'].id}"
                f".updateMenuItems({self._settings_items!r}, '设置')")
        except Exception:
            pass
        if selected == '主题色':
            self._open_color_picker()
        elif selected == '截图路径设置':
            self._prompt_screenshot_path()
        elif selected == '清空当前画线':
            self._clear_current_drawings()
        elif selected == '清空所有画线':
            self._clear_all_drawings()
        elif selected == '字体变大':
            self._change_font(1)
        elif selected == '字体变小':
            self._change_font(-1)
        elif selected == '恢复默认':
            self._restore_default()
        elif selected in self._settings_handlers:
            try:
                self._settings_handlers[selected]()
            except Exception as error:
                print(f'[strategy_window] 设置项 {selected!r} 执行失败:', error)

    def _set_button_tooltip(self, name: str, tooltip: str,
                            label: str = '') -> None:
        """给页面顶栏的控件挂原生 tooltip（title）。

        `label` 是按钮上**显示的文字**（`shot`→截图、`drawing`→画线工具、
        `theme`→深色/浅色、`settings`→设置）。不能只用 `widget.value`：
        按钮控件的 value 是 toggle 状态（True/False），菜单的 value 才是文字。
        所以这里把三样都当候选：显示文字、控件当前 value、键名。

        title **只挂在最近的按钮元素上**（找不到就退回文字节点本身）。
        绝不能往上挂父节点 —— 四个按钮是同一容器的兄弟，挂到共同祖先上
        最后写的那次会盖住全部。菜单文字会变，所以 `_on_theme` 里会再调一次。
        """
        widget = self.page_bar[name]
        candidates = []
        for item in (label, str(getattr(widget, "value", "") or ""), name):
            item = str(item or "").strip()
            if item and item not in candidates:
                candidates.append(item)
        target = widget.id
        self.chart.run_script(f'''
        ;(() => {{
            const wanted = {json.dumps(tooltip)};
            const labels = {json.dumps(candidates)};
            const isButton = (el) => !!el && !!el.classList && (
                el.tagName === "BUTTON"
                || el.classList.contains("topbar-button")
                || el.classList.contains("switcher-button"));
            // 允许前缀：菜单按钮的文字可能是 "深色 v" 这种带箭头的
            const matches = (text) => labels.some((label) => text === label
                || (text.length > label.length && text.startsWith(label)));
            for (const node of document.querySelectorAll("*")) {{
                if (!isButton(node) && node.children.length) continue;
                const text = (node.textContent || "").trim();
                if (!text || !matches(text)) continue;
                node.title = wanted;
                if (!isButton(node)) {{
                    let el = node.parentElement;
                    while (el && el !== document.body && !isButton(el)) {{
                        el = el.parentElement;
                    }}
                    if (isButton(el)) el.title = wanted;
                }}
            }}
            const control = {target};
            const root = (control && (control.div || control._div
                || control.element)) || control;
            if (root && root.querySelectorAll) {{
                for (const tag of root.querySelectorAll(
                        "button, .topbar-button, .switcher-button")) {{
                    tag.title = wanted;
                }}
            }}
        }})();
''')

    def _on_screenshot(self, chart=None) -> None:
        """顶栏「截图」：把当前页面存成 PNG；没设路径时直接弹路径设置。

        `chart=None` 是必需的：顶栏 wrapper 会把 chart 传进来（和
        `_on_theme` / `_on_settings_menu` 的 `win=None` 一样）。
        顶栏按钮偶尔会连发两次（按下 + 状态变化），这里做个去抖，
        免得一秒内落两张几乎一样的图。
        """
        now = time.monotonic()
        if now - getattr(self, '_shot_at', 0.0) < 0.4:
            return
        self._shot_at = now
        if not self._screenshot_path:
            self._prompt_screenshot_path()          # 不弹提示，直接让用户设
            return
        self._take_screenshot(self._screenshot_path)

    def _screenshot_data(self) -> bytes:
        """当前页面截图：pane 合成图 + 页面上的 HTML 面板（表格）。

        引擎的 `takeScreenshot()` 只有各个 pane；图例、顶栏、回测指标 /
        交易记录这些 DOM 覆盖层不在里面。这里用 `_SCREENSHOT_JS` 把它们按真实
        布局重绘到同一张画布（方案 A）。`screenshot_with_topbar=True` 时连两条
        顶栏一起截，否则只带图例。任何一步失败就退回纯图表截图，不影响保存。
        """
        script = (_SCREENSHOT_JS
                  .replace('CHART', f'window.{self.chart.id}')
                  .replace('WITH_TOPBAR',
                           'true' if self.screenshot_with_topbar else 'false'))
        try:
            url = self.chart.win.run_script_and_get(script)
            if url and ',' in str(url):
                return b64decode(str(url).split(',', 1)[1])
        except Exception as error:
            print('[截图] 页面合成失败，退回图表截图:', error)
        return self.chart.screenshot()

    def _take_screenshot(self, directory: str) -> None:
        """当前页面截图写到目录下的时间戳文件（不会互相覆盖）。"""
        target = pathlib.Path(str(directory)).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            name = time.strftime('pylightcharts_%Y%m%d_%H%M%S.png')
            data = self._screenshot_data()
            path = target / name
            path.write_bytes(data)
            print(f'[截图] {len(data)} bytes -> {path}')
        except Exception as error:
            print('[截图] 保存失败:', error)

    def _pick_directory(self):
        """弹系统原生「选择文件夹」对话框。

        返回选中的目录；返回 None 表示用户取消；抛异常表示拿不到原生对话框。

        优先用当前进程里能拿到的 pywebview 窗口（内嵌 Qt 那种情况）；图表窗口
        在子进程里跑时拿不到句柄，就退到**独立进程**弹 Tkinter 的
        `askdirectory()`（Windows 上是系统 shell 对话框）。
        """
        try:
            import webview
            for window in list(getattr(self.chart, 'windows', []) or []):
                picker = getattr(window, 'create_file_dialog', None)
                if picker is None:
                    continue
                dialog = getattr(webview, 'FileDialog', None)
                kind = (dialog.FOLDER if dialog is not None
                        else webview.FOLDER_DIALOG)
                picked = picker(kind)
                return str(picked[0]) if picked else None
        except ImportError:
            pass
        except Exception as error:
            print('[截图] pywebview 目录对话框不可用:', error)

        result = subprocess.run([sys.executable, '-c', _TK_PICK_DIR],
                                capture_output=True, text=True, timeout=900)
        if result.returncode != 0:                 # 没有 tkinter 之类
            raise RuntimeError(result.stderr.strip()
                               or '没有可用的目录选择对话框')
        # 正常退出但没选（点了取消/关掉窗口）-> None，什么都不做
        lines = [line for line in (result.stdout or '').splitlines() if line]
        return lines[-1].strip() if lines else None

    def _apply_screenshot_path(self, value: str) -> None:
        """保存路径（空 = 清除）并立刻截一张（路径为空则什么都不做）。"""
        value = str(value or '').strip()
        self._screenshot_path = value
        _save_user_settings(screenshot_path=value)
        if value:
            self._take_screenshot(value)

    def _prompt_screenshot_path(self) -> None:
        """让用户选截图目录：优先系统原生对话框，不可用时退回页面内输入框。"""
        try:
            chosen = self._pick_directory()
        except Exception as error:
            print('[截图] 原生目录选择不可用，改用输入框:', error)
            self._prompt_screenshot_path_input()
            return
        if not chosen:
            return                        # 用户取消，不截图也不改设置
        self._apply_screenshot_path(chosen)

    def _prompt_screenshot_path_input(self) -> None:
        """页面内输入框（无原生窗口时的兜底）：回车/确定即保存并立刻截一张。"""
        if 'screenshotPath' not in self.chart.win.handlers:
            self.chart.win.handlers['screenshotPath'] = self._on_screenshot_path
        self.chart.run_script("""
        ;(() => {
            let old = document.getElementById('pylightcharts-shot-path');
            if (old) old.remove();
            const box = document.createElement('div');
            box.id = 'pylightcharts-shot-path';
            box.style.cssText =
                'position:fixed;left:50%;top:64px;transform:translateX(-50%);'
                + 'z-index:99999;display:flex;gap:8px;align-items:center;'
                + 'padding:8px 12px;border:1px solid var(--border-color,#888);'
                + 'border-radius:6px;background:var(--bg,#fff);'
                + 'color:var(--text,#000);font:13px sans-serif;';
            const label = document.createElement('span');
            label.textContent = '截图路径';
            const input = document.createElement('input');
            input.type = 'text';
            input.placeholder = '例如 D:/shot 或 /home/me/shot';
            input.style.cssText = 'width:300px;padding:3px 6px;font:12px monospace;';
            const ok = document.createElement('button');
            ok.textContent = '确定';
            ok.style.cssText = 'padding:3px 10px;cursor:pointer;';
            const cancel = document.createElement('button');
            cancel.textContent = '取消';
            cancel.style.cssText = 'padding:3px 10px;cursor:pointer;';
            // 误点 / 不想设路径时：ESC 或「取消」直接退出，不改设置也不截图 ✓
            const close = () => {
                document.removeEventListener('keydown', onKey, true);
                box.remove();
            };
            const done = () => {
                close();
                window.callbackFunction('screenshotPath_~_' + input.value.trim());
            };
            const onKey = (event) => {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    event.stopPropagation();
                    close();
                } else if (event.key === 'Enter') {
                    event.preventDefault();
                    done();
                }
            };
            ok.onclick = done;
            cancel.onclick = close;
            // capture 阶段监听：焦点不在输入框（或图上）也能 ESC 退出 ✓
            document.addEventListener('keydown', onKey, true);
            box.appendChild(label);
            box.appendChild(input);
            box.appendChild(ok);
            box.appendChild(cancel);
            document.body.appendChild(box);
            input.focus();
        })();
        """)

    def _on_screenshot_path(self, payload) -> None:
        """页面内输入框的回调：记住路径并立刻截一张（复用同一套逻辑）。"""
        value = str(payload or '')
        if '~_' in value:
            value = value.split('~_', 1)[1]
        self._apply_screenshot_path(value)

    def _open_color_picker(self) -> None:
        """显示一个可见的颜色选择块，用户点它弹原生选色器（可靠）。"""
        if 'themeColor' not in self.chart.win.handlers:
            self.chart.win.handlers['themeColor'] = self._on_accent_color
        self.chart.run_script('''
        ;(() => {
            let input = document.getElementById('pylightcharts-accent-picker');
            if (!input) {
                input = document.createElement('input');
                input.type = 'color';
                input.id = 'pylightcharts-accent-picker';
                input.title = '选择主题色（点我）';
                input.style.cssText = 'position:fixed;top:56px;right:16px;z-index:99999;'
                    + 'width:26px;height:26px;padding:0;'
                    + 'border:1px solid var(--border-color);border-radius:4px;'
                    + 'cursor:pointer;background:transparent;';
                document.body.appendChild(input);
                input.addEventListener('change', () => {
                    window.callbackFunction('themeColor_~_' + input.value);
                    input.style.display = 'none';
                });
            }
            input.value = '';              // 重置，避免同色不触发 change
            input.style.display = 'block';
            input.click();                 // 尽量直接弹出；被拦截时用户点这个色块
        })();
        ''')

    def _on_accent_color(self, color: str) -> None:
        """颜色选择器回调：把顶栏按下色 + 十字线标签背景换成主题色。"""
        self._apply_accent(color)

    def _apply_accent(self, color: str) -> None:
        self._accent = color
        try:
            self.chart.win.theme(self._theme_name,
                                 active_background=color,
                                 crosshair_label=color)
        except Exception as error:
            print('[strategy_window] 应用主题色失败:', error)
        _save_user_settings(theme_color=color)

    def _restore_default(self) -> None:
        """恢复默认设置（主题色 + 字体大小回到默认）。"""
        self._accent = None
        self._font_size = int(_DEFAULT_SETTINGS.get('font_size',
                                                        LEGEND_FONT_SIZE))
        try:
            self.chart.win.theme(self._theme_name)
        except Exception as error:
            print('[strategy_window] 恢复默认失败:', error)
        self._apply_font()
        _restore_default_settings()
        if self.on_theme:
            self.on_theme()

    def _reapply_theme(self) -> None:
        """新建的副图 pane 不会自动继承整窗主题，重建后补上它们的价格轴颜色。

        只重放“新 pane 缺的那部分”（价格轴文字/边框），**不**重放整个
        `_apply_theme`：那会重发成交量、蜡烛色等，回放时每次刷新回测都会把
        历史成交量 `setData` 重刷一遍（表现为清空/闪没）。
        """
        theme = getattr(self.chart.win, '_theme', None)
        self._apply_y_margin()      # 价格轴余量与主题无关，先应用（新 pane 也套上）
        if not theme:
            return
        try:
            for index in range(1, max(1, self.chart._pane_count)):
                for scale_id in ('right', 'left'):
                    self.chart.pane_price_scale(index, scale_id).apply_options(
                        text_color=theme['text'],
                        border_color=theme['scale_border'])
        except Exception as error:
            print('[strategy_window] 重新应用主题失败:', error)
        self._apply_grid()          # 主题切换后重设网格颜色

    def _apply_y_margin(self) -> None:
        """把 ``config.y_margin`` 落到**每个 pane 的价格轴**（主图 + 各指标副图）。

        lightweight-charts 的价格轴默认 ``scaleMargins={top:0.2, bottom:0.1}``
        —— 价格只占面板高度的 70%，上下看着很空、图表被压。这里默认改成上下
        各 **10%**（``DEFAULT_Y_MARGIN``）；``y_margin=(0.2, 0.1)`` 可回到 LWC 原样，
        ``y_margin=0.0`` 铺满。只作用于 pylightcharts（bokeh 另有留白逻辑）。
        """
        margins = getattr(self, '_y_margin', None)
        if not margins:
            return
        top, bottom = margins
        # 用 python 侧的 pane 计数：`pane_count()` 在窗口已加载时会做一次**同步
        # eval_js**，夹在成批脚本中间发出去容易和排队中的脚本交错（曾出现
        # `applyOptions` 打到一个已被删掉的系列上 -> LWC `Value is null`）。
        try:
            count = int(getattr(self.chart, '_pane_count', 0) or 0)
        except Exception:
            return
        for index in range(max(1, count)):
            for scale_id in ('right', 'left'):
                try:
                    self.chart.pane_price_scale(index, scale_id).apply_options(
                        scale_margins={'top': top, 'bottom': bottom})
                except Exception:
                    pass

    def _apply_grid(self) -> None:
        """网格线：由策略 ``config.grid`` 控制（默认不显示）。"""
        try:
            dark = self._theme_name == 'dark'
            self.chart.grid(
                vert_enabled=self._grid, horz_enabled=self._grid,
                color=('rgba(90, 90, 90, 0.45)' if dark
                       else 'rgba(200, 200, 200, 0.65)'))
        except Exception as error:
            print('[strategy_window] 设置网格失败:', error)

    # ------------------------------------------------------------- 画线工具
    def _setup_toolbox(self) -> None:
        """画线工具：只在 K 线图生效，画线按「合约+周期」存到 setting.json。"""
        from pylightcharts.toolbox import ToolBox
        self._drawings_path = _DRAWINGS_FILE      # 画线单独一份文件
        # 初始是否显示：由调用方通过 `toolbox_initial` 传进来（回放读策略的
        # `Config.toolbox` / `self.toolbox = True`），默认隐藏
        self._toolbox_on = bool(getattr(self, 'toolbox_initial', False))
        self.chart.toolbox = ToolBox(self.chart)
        self.chart.toolbox.save_drawings_under(self.chart.topbar['contract'])
        # 画线每次变化都会回调 save_drawings；这里接管它，额外落盘
        self.chart.win.handlers[f'save_drawings{self.chart.id}'] = self._on_save_drawings
        self._load_drawings()
        # 延迟到页面加载后执行 —— 和 createToolBox 同一批次时，前面任一语句
        # 报错会把这条也带崩（画线工具就默认显示出来了）。
        if self._toolbox_on:
            self._show_toolbox(run_last=True)
        else:
            self._hide_toolbox(run_last=True)

    def set_toolbox_visible(self, on: bool) -> None:
        """设置画图工具初始显示/隐藏，并让顶栏「画线工具」按钮的按下状态一致。

        按钮的"按下"效果是引擎用**内联样式**画的
        （`style.backgroundColor = var(--active-bg-color)`），改 `widget.value`
        不会触发它，所以这里照它的画法直接设一遍。
        """
        self._toolbox_on = bool(on)
        try:
            self.page_bar['drawing'].value = self._toolbox_on
        except Exception:
            pass
        try:
            self.chart.run_script(f"""
            ;(() => {{
                const on = {json.dumps(bool(self._toolbox_on))};
                for (const node of document.querySelectorAll('.topbar-button')) {{
                    if ((node.textContent || '').trim() !== '画线工具') continue;
                    node.style.backgroundColor =
                        on ? 'var(--active-bg-color)' : '';
                    node.style.color = on ? 'var(--active-color)' : '';
                }}
            }})();
            """)
        except Exception as error:
            print('[strategy_window] 设置按钮按下状态失败:', error)
        try:
            if self._toolbox_on:
                self._show_toolbox(run_last=True)
            else:
                self._hide_toolbox(run_last=True)
        except Exception as error:
            print('[strategy_window] 设置画图工具失败:', error)

    def _on_order_book(self, win=None) -> None:
        """顶栏「买卖盘」开关 → 回调 RealtimeChart ✓。"""
        state = bool(self.page_bar['orderbook'].value)
        if self.on_toggle_order_book:
            self.on_toggle_order_book(state)

    def _on_depth(self, win=None) -> None:
        """顶栏「二级报价」开关 → 回调 RealtimeChart ✓。"""
        state = bool(self.page_bar['depth'].value)
        if self.on_toggle_depth:
            self.on_toggle_depth(state)

    def _on_drawing(self, win=None) -> None:
        """「画线工具」开关：显示/隐藏工具箱（只在 K 线图）。"""
        self._toolbox_on = bool(self.page_bar['drawing'].value)
        if self._toolbox_on and self.contract not in INFO_PAGES:
            self._show_toolbox()
        else:
            self._hide_toolbox()

    def _show_toolbox(self, run_last: bool = False) -> None:
        self._apply_toolbox_visibility(True, run_last=run_last)

    def _hide_toolbox(self, run_last: bool = False) -> None:
        self._apply_toolbox_visibility(False, run_last=run_last)

    def _apply_toolbox_visibility(self, on: bool,
                                  run_last: bool = False) -> None:
        """显示/隐藏画线工具（**自适应 toolbox 尚未创建** ✓）。

        ⚠ 以前直接写 ``if (chart.toolBox) …display = 'none'``：`toolBox` 还没建好时
        这条就是**空操作、而且不会再重试** ✗ → 之后工具箱被建出来（默认是可见的 ✗）
        就一直显示着 ✗ —— 这就是用户反馈的「toolbox 默认 False，但有时初始化就
        显示出来」（只是时序不同）。

        现在：先试一次；没有 `toolBox` 就**轮询**到它出现（最多 ~10s ✓），
        保证「初始是否显示」永远跟着 ``toolbox`` 的取值 ✓。
        """
        display = 'flex' if on else 'none'
        self.chart.run_script(
            f'''; (function () {{
                var c = {self.chart.id};
                var apply = function () {{
                    if (c && c.toolBox && c.toolBox.div) {{
                        c.toolBox.div.style.display = {json.dumps(display)};
                        return true;
                    }}
                    return false;
                }};
                if (apply()) {{ return; }}
                var tries = 0;
                var timer = setInterval(function () {{
                    if (apply() || ++tries > 100) {{ clearInterval(timer); }}
                }}, 100);
            }})();''',
            run_last=run_last)

    def _on_save_drawings(self, drawings: str) -> None:
        """画线变化回调：更新内存 dict 并写回 drawings.json。"""
        self.chart.toolbox._save_drawings(drawings)
        self._save_drawings()

    def _load_drawings(self) -> None:
        """从 drawings.json 读入所有合约的画线。"""
        self.chart.toolbox.drawings = _read_drawings_file()

    def _save_drawings(self) -> None:
        """把当前所有画线写回 drawings.json（和主题/字体分开，互不重写）。"""
        try:
            _write_drawings_file(self.chart.toolbox.drawings)
        except Exception as error:
            print('[strategy_window] 保存画线失败:', error)

    def _clear_current_drawings(self) -> None:
        """清空当前合约（+周期）的画线。"""
        key = self.contract
        if key in INFO_PAGES:
            return
        self.chart.toolbox.drawings.pop(key, None)
        self.chart.run_script(
            f"if ({self.chart.id}.toolBox) {self.chart.id}.toolBox.clearDrawings()")
        self._save_drawings()

    def _clear_all_drawings(self) -> None:
        """清空所有合约的画线。"""
        self.chart.toolbox.drawings = {}
        self.chart.run_script(
            f"if ({self.chart.id}.toolBox) {self.chart.id}.toolBox.clearDrawings()")
        self._save_drawings()

    # ------------------------------------------------------ 可视范围记忆
    def _lock_view(self) -> None:
        """开始切合约：期间 JS 报回来的可视范围一概不记（那是过渡态 ✗）。"""
        self._view_lock_depth += 1

    def _unlock_view(self) -> None:
        self._view_lock_depth = max(0, self._view_lock_depth - 1)

    def install_view_tracking(self) -> None:
        """让 JS 在可视范围变化时**主动回包**（幂等 ✓）。

        以前只在 ``_save_view`` 里同步调 ``get_visible_logical_range()``（阻塞式
        往返）—— 在 pywebview 主线程上很容易读不到/超时，于是什么都存不下来，
        表现为「每次切合约都回到初始视图」✗。改成事件驱动后：用户平移/缩放/``fit()``
        都会推一条 ``minibt_view_range_~_from;;;to`` 回来，存下来**不阻塞** ✓
        """
        if self._view_tracking:
            return
        chart = getattr(self, 'chart', None)
        win = getattr(chart, 'win', None)
        handlers = getattr(win, 'handlers', None)
        if chart is None or not isinstance(handlers, dict):
            return
        try:
            handlers['minibt_view_range'] = self._on_view_range
            chart.run_script(
                f"(function(){{var c={getattr(chart, 'id', 'null')};"
                "if(!c||!c.chart||!c.chart.timeScale)return;"
                "var ts=c.chart.timeScale();"
                "if(ts.__minibtViewHooked)return;ts.__minibtViewHooked=true;"
                "ts.subscribeVisibleLogicalRangeChange(function(r){"
                "if(!r)return;window.callbackFunction('minibt_view_range_~_'"
                "+r.from+';;;'+r.to);});})()")
            self._view_tracking = True
        except Exception as error:                               # noqa: BLE001
            print('[strategy_window] 可视范围跟踪安装失败:', error)

    def _on_view_range(self, from_logical=None, to_logical=None) -> None:
        """JS 回包：把当前合约的可见范围存下来（过渡态/信息页忽略 ✓）。"""
        if self._view_lock_depth > 0:
            return                        # 切合约/重建过程中的过渡态，不记
        contract = str(getattr(self, 'contract', ''))
        if contract in INFO_PAGES:
            return                        # 信息页不记 K 线视口 ✓
        self._remember_view(contract, from_logical, to_logical)

    def _remember_view(self, key: str, from_logical, to_logical) -> None:
        """存 ``(根数, 右端相对最后一根的偏移, 右端绝对值)`` ✓。

        只记根数不够：若用户把最后一根拖离右轴（右端留白），恢复时固定
        ``to = 最后一根`` 会把留白吃掉，表现为“切回来最后一根紧贴右轴”✗。
        偏移取不到（拿不到总根数）时留 ``None``，恢复就按**绝对值**来 ✓。
        """
        try:
            from_, to = float(from_logical), float(to_logical)
        except (TypeError, ValueError):
            return
        if from_ != from_ or to != to:                # NaN
            return
        length = max(1, int(round(to - from_)))
        offset = None
        try:
            total = len(self.chart.candle_data)
            if total > 0:
                offset = int(round(to)) - (total - 1)
        except Exception:                                        # noqa: BLE001
            offset = None
        self._view_ranges[key] = (length, offset, int(round(to)))

    def _save_view(self, key: str = '') -> None:
        """记住当前可见范围（**优先用事件流**，拿不到才同步读一次 ✓）。

        ★ 以前只靠同步调 ``get_visible_logical_range()``（阻塞式往返）—— 在
        pywebview 主线程上很容易读不到/超时 → 什么都存不下来 → 表现为
        「每次切合约都回到初始视图」✗。现在 JS 每次范围变化就主动回包
        （``install_view_tracking``），这里只需要处理“从来没收到过事件”的兜底 ✓
        """
        key = key or self.contract
        if key in self._view_ranges:
            return                        # 事件流已实时记好，不需要阻塞往返 ✓
        # 窗口还没加载/显示时读不到可视范围（会卡在回包队列上），直接跳过
        if not getattr(getattr(self.chart, 'win', None), 'loaded', False):
            return
        try:
            logical = self.chart.get_visible_logical_range()
            if logical and logical.get('to') is not None:
                self._remember_view(key, logical.get('from'), logical.get('to'))
        except Exception:
            pass                      # 读不到时忽略

    def _restore_view(self, key: str = '') -> None:
        """切回某个页面时：显示最近行情，并保持上次的可见根数 + 右端留白。"""
        key = key or self.contract
        saved = self._view_ranges.get(key)
        if not saved:
            return
        # 兼容旧格式：``int``（只存了根数）/ ``(length, offset)`` ✓
        if not isinstance(saved, tuple):
            length, offset, to_abs = int(saved), 0, None
        elif len(saved) >= 3:
            length, offset, to_abs = saved[0], saved[1], saved[2]
        else:
            length, offset = saved
            to_abs = None
        try:
            total = len(self.chart.candle_data)
        except Exception:                                        # noqa: BLE001
            total = 0
        if offset is not None and int(offset) >= 0:
            # 当时就贴在右端（跟随最新）→ 锚定最后一根，新 K 线照常往后走 ✓
            to = max(0, total - 1) + int(offset)
        elif to_abs is not None:
            # 当时翻到了**历史区**（右端离最后一根很远）→ 恢复同一个绝对窗口 ✓
            to = int(to_abs)
        else:
            to = max(0, total - 1) + int(offset or 0)
        try:
            # ★ 用 ``to - length``（而不是 ``to - length + 1``）：``length`` 本来
            #   就是**跨度** ``round(to - from)`` —— 减 1 会让每次切回来都少一根
            #   ✗（原实现的隐性缺陷）
            from_ = max(0, to - int(length))
            self.chart.set_visible_logical_range(from_, to)
        except Exception:
            pass

    # ------------------------------------------------------------------ 运行
    def show(self, block: bool = True) -> None:
        if self._jupyter:
            return self._show_in_jupyter()
        # 浏览器 + live 图表：只有在**真的** notebook 里才用 iframe 内嵌；
        # 普通脚本（如 `python cci.py`）必须开浏览器标签，否则
        # `display(IFrame(...))` 只会打印一个对象、图表就没了。
        if bool(getattr(self, '_browser', False)) and _in_notebook() and \
                bool(getattr(getattr(self, 'chart', None), 'live', False)):
            self._show_in_jupyter()
            return
        self.chart.show(block=block)

    def _show_in_jupyter(self) -> str:
        """Jupyter：启动本地 SSE 服务，用 IFrame 内嵌（宽度自适应单元格）。

        ``width='100%'``：iframe 跟着 Jupyter 窗口/单元格宽度伸缩，页内的
        `Handler.reSize()`（autosize）会随之 resize 图表。
        """
        url = self.chart.show(block=False, open_browser=False)
        if not _in_notebook():
            # 兜底：不在 notebook 里却走到这里（如某些封装直接把 jupyter 传成
            # True）—— 用浏览器打开，保证能看图。
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
            print('[strategy_window] 图表地址:', url)
            return url
        try:
            from IPython.display import IFrame, display
            display(IFrame(url, width='100%',
                           height=self.chart.height or 700))
        except Exception as error:
            print('[strategy_window] Jupyter 内嵌失败（页面地址）:', url, error)
        return url


def demo_backtest(seed: int, trades: int, profit: float, rows: int = 160):
    """造一份差异明显的回测结果（演示用）。"""
    rng = np.random.default_rng(seed)
    times = pd.date_range('2024-01-01', periods=rows, freq='D')
    equity = pd.DataFrame({'time': times,
                           'value': 1e6 + np.cumsum(rng.normal(profit / rows,
                                                               80, rows))})
    return {
        'stats': {'final_profit': profit, 'total_return': profit / 1e6,
                  'total_fee': 0.0, 'profit_ratio': 0.88,
                  'mean_profit': profit / max(trades, 1),
                  'sharpe_ratio': rng.normal(0, 0.2),
                  'max_drawdown': abs(rng.normal(0.02, 0.01)),
                  'var': 0.0, 'risk_return_ratio': rng.normal(0, 0.1),
                  'win_rate': 0.27 if profit < 0 else 0.62,
                  'win_count': int(trades * (0.27 if profit < 0 else 0.62)),
                  'loss_count': int(trades * (0.73 if profit < 0 else 0.38)),
                  'total_trades': trades,
                  'mean_loss': -abs(profit / max(trades, 1)) * 2},
        'equity': equity,
        'pnl': pd.DataFrame({'time': pd.date_range('2024-01-01', periods=trades,
                                                   freq='h'),
                             'value': rng.normal(profit / max(trades, 1), 20,
                                                 trades)}),
        'trades': pd.DataFrame({
            'time': pd.date_range('2024-01-01', periods=trades, freq='h'),
            'direction': ['long' if i % 2 else 'short' for i in range(trades)],
            'price': np.round(6000 + np.cumsum(rng.normal(0, 5, trades)), 1),
            'qty': [1] * trades,
            'pnl': np.round(rng.normal(profit / max(trades, 1), 30, trades), 2),
            'balance': np.round(1e6 + np.cumsum(
                rng.normal(profit / max(trades, 1), 30, trades)), 2)}),
    }


def main() -> None:
    window = StrategyWindow(
        strategies=('CCIStrategy', 'MAStrategy', 'BreakoutStrategy'),
        contracts=lambda strategy: {
            'CCIStrategy': ('DCE.pp2601', 'DCE.pp2605'),
            'MAStrategy': ('SHFE.rb2601',),
            'BreakoutStrategy': ('CZCE.SR601', 'DCE.i2601', 'DCE.m2601'),
        }.get(strategy, ('DCE.pp2601',)),
        panes={'CCIStrategy': ('CCI 20',), 'MAStrategy': ('MA 5',),
               'BreakoutStrategy': ('ATR 14',)})
    # 每个策略各登记一份**不同**的回测结果
    window.set_backtest(strategy='CCIStrategy', **
                        demo_backtest(1, 459, -765.0))
    window.set_backtest(strategy='MAStrategy', **demo_backtest(2, 37, 1234.5))
    window.set_backtest(strategy='BreakoutStrategy',
                        **demo_backtest(3, 96, 5200.0))
    print('[strategy_window] 策略:', window.strategies)
    print('[strategy_window] 当前合约栏:', window.chart.topbar['contract'].options)
    window.show(block=True)


if __name__ == '__main__':
    main()
