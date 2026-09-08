# coding:utf-8
# light_chart_replay.py - 策略回放模式
# 基于 light_chart.py 改造，支持多策略回放、SegmentedWidget导航
# https://github.com/louisnw01/lightweight-charts-python
# https://tradingview.github.io/lightweight-charts/
# https://lightweight-charts-python.readthedocs.io/en/latest/index.html

from __future__ import annotations
from collections import deque
import json
from itertools import cycle
from ..other import FILED, os, pd, partial, FilteredOutputRedirector, sys, np
from ..utils import Colors as btcolors, OrderedDict, _time, Iterable, TYPE_CHECKING
with FilteredOutputRedirector():
    from qfluentwidgets import (setTheme, Theme, FluentWindow, TransparentToolButton, FluentIcon,
                                FluentStyleSheet, FluentTitleBar, CaptionLabel,RoundMenu,Action,
                                BodyLabel, ComboBox, SegmentedWidget, SingleDirectionScrollArea,
                                PushButton, PrimaryPushButton, Slider, HorizontalSeparator,
                                TableWidget, CardWidget, FlowLayout,FluentIcon as FIF)
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QSizePolicy, QStackedWidget, QTableWidgetItem,
                               QAbstractItemView, QScrollArea, QFrame, QFileDialog,
                               QHeaderView)
from PyQt6.QtGui import QColor, QFont, QIcon, QGuiApplication, QPainter, QPen, QPolygonF
from PyQt6.QtCore import QPointF
from PyQt6.QtCore import Qt, QSize, QTimer, QUrl, QObject, pyqtSignal

# 阻止 lightweight_charts 自动加载 PySide6（强制使用 PyQt6）
import builtins as _builtins
_original_import = _builtins.__import__

def _blocked_import(name, *args, **kwargs):
    if name.startswith('PySide6'):
        raise ImportError(f'PySide6 blocked (PyQt6): {name}')
    return _original_import(name, *args, **kwargs)

_builtins.__import__ = _blocked_import

from lightweight_charts.toolbox import ToolBox, json as lw_json
from lightweight_charts.drawings import HorizontalLine
from lightweight_charts.abstract import Line, Candlestick, AbstractChart, SeriesCommon
from lightweight_charts import util

# 恢复正常导入
_builtins.__import__ = _original_import

# ---- Monkey-patch: NaN 断线处理（多系列分割法）----
# lightweight-charts 的 LineSeries 会跨 NaN 间隙连线，无法通过空白数据点实现断线。
# 此补丁采用多系列分割法：检测 NaN 位置，将数据分割为多个连续片段，
# 每个片段用独立的 Line 系列显示，实现真正的断线效果。
import numpy as _np

_original_series_set = SeriesCommon.set

def _split_df_by_nan(df, value_col='value'):
    """
    将 DataFrame 根据 NaN 值分割成多个连续片段。
    返回：[(片段索引, DataFrame片段), ...]
    """
    if df.empty or value_col not in df.columns:
        return [(0, df)]

    # 找出 NaN 位置
    is_nan = df[value_col].isna()
    segments = []
    segment_start = None

    for i in range(len(df)):
        if not is_nan.iloc[i]:
            # 有效值：如果还没开始片段，开始新片段
            if segment_start is None:
                segment_start = i
        else:
            # NaN 值：如果有正在进行的片段，结束它
            if segment_start is not None:
                segments.append((len(segments), df.iloc[segment_start:i].copy()))
                segment_start = None

    # 处理最后一个片段
    if segment_start is not None:
        segments.append((len(segments), df.iloc[segment_start:].copy()))

    return segments

def _patched_series_set(self, df=None, format_cols=True):
    """补丁版 set()：Line 数据含 NaN 时用多系列分割法实现断线。"""
    # 清理之前的分段线（如果存在）
    if hasattr(self, '_segment_lines') and self._segment_lines:
        for seg_line in self._segment_lines:
            try:
                # 清空分段线的数据
                seg_line.run_script(f'{seg_line.id}.series.setData([]); ')
            except Exception:
                pass
        self._segment_lines = []

    if df is None or df.empty:
        return _original_series_set(self, df, format_cols)

    # 仅处理 Line（不处理 Histogram）
    if not isinstance(self, Line):
        return _original_series_set(self, df, format_cols)

    # 检测值列是否有 NaN
    val_cols = [c for c in df.columns if c != 'time']
    has_nan = val_cols and any(
        str(df[c].dtype).startswith('float') and df[c].isna().any()
        for c in val_cols
    )

    # 无 NaN 或只有一个片段：直接用原方法
    if not has_nan:
        return _original_series_set(self, df, format_cols)

    # 格式化 DataFrame
    if format_cols:
        df_formatted = self._df_datetime_format(df, exclude_lowercase=self.name)
    else:
        df_formatted = df.copy()

    if self.name:
        if self.name not in df_formatted:
            raise NameError(f'No column named "{self.name}".')
        df_formatted = df_formatted.rename(columns={self.name: 'value'})

    # 分割 DataFrame
    segments = _split_df_by_nan(df_formatted, 'value')

    # 如果只有一个片段（全是有效值），用原方法（需先重命名回原列名）
    if len(segments) <= 1:
        self.data = df_formatted.copy()
        self._last_bar = df_formatted.iloc[-1] if len(df_formatted) > 0 else None
        df_renamed = df_formatted.rename(columns={'value': self.name})
        return _original_series_set(self, df_renamed, format_cols=False)

    # 多片段：第一个片段用原 Line，其他片段创建新的 Line 系列
    self._segment_lines = []

    for seg_idx, seg_df in segments:
        if seg_idx == 0:
            # 第一个片段：用原 Line 对象
            self.data = df_formatted.copy()
            self._last_bar = df_formatted.iloc[-1] if len(df_formatted) > 0 else None
            # 修复：将 value 列重命名回原列名，避免 NameError
            seg_df_renamed = seg_df.rename(columns={'value': self.name})
            _original_series_set(self, seg_df_renamed, format_cols=False)
        else:
            # 其他片段：创建新的 Line 系列
            # 使用原 Line 的样式（color），其他参数用默认值
            seg_line = self._chart.create_line(
                name=f"{self.name}_seg{seg_idx}",
                color=self.color if hasattr(self, 'color') else 'rgba(214, 237, 255, 0.6)',
                style='solid',
                width=2,
                price_line=False,
                price_label=False
            )
            # 修复：将 value 列重命名为分段线的列名
            seg_df_renamed = seg_df.rename(columns={'value': seg_line.name})
            # 设置分段数据
            _original_series_set(seg_line, seg_df_renamed, format_cols=False)
            self._segment_lines.append(seg_line)

# SeriesCommon.set = _patched_series_set

# 延迟导入QtChart类
ChartClass = None
QtChart = None

if TYPE_CHECKING:
    from strategy import Strategy

Colors: list[str] = ['fuchsia', 'lime', 'olive', 'blue', 'purple', 'silver', 'teal', 'aqua',
                     'green', 'maroon', 'navy', 'red']


def get_chart_class():
    """延迟导入Chart类"""
    global ChartClass, QtChart
    if ChartClass is None:
        app = QApplication.instance()
        if app is None:
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
            app = QApplication([])

        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtCore import Qt, QObject, pyqtSlot, QUrl, QTimer

        class Bridge(QObject):
            def __init__(self, chart):
                super().__init__()
                self.win = chart.win

            @pyqtSlot(str)
            def callback(self, message):
                from lightweight_charts.util import parse_event_message

                def emit_callback(window, string):
                    func, args = parse_event_message(window, string)
                    import asyncio
                    asyncio.create_task(
                        func(*args)) if asyncio.iscoroutinefunction(func) else func(*args)
                emit_callback(self.win, message)

        import lightweight_charts
        lightweight_charts.using_pyside6 = False
        import lightweight_charts.widgets
        lightweight_charts.widgets.QWebEngineView = QWebEngineView
        lightweight_charts.widgets.QWebChannel = QWebChannel
        lightweight_charts.widgets.QObject = QObject
        lightweight_charts.widgets.Slot = pyqtSlot
        lightweight_charts.widgets.QUrl = QUrl
        lightweight_charts.widgets.QTimer = QTimer
        lightweight_charts.widgets.Qt = Qt
        lightweight_charts.widgets.Bridge = Bridge

        ChartClass = lightweight_charts.widgets.QtChart
        QtChart = ChartClass
    return ChartClass


def get_colors() -> cycle:
    """指标初始颜色"""
    return cycle(Colors)


def get_default_settings() -> dict:
    return {
        "drawings": {},
        "price_alerts": {},
        "istool": False,
        "mouse_label_color": 'rgba(50, 50, 50, 0.8)',
        "candlestick_colors": {
            "bear_color": btcolors.bear_color,
            "bull_color": btcolors.bull_color
        },
        "splitter_sizes": [174, 1868],
    }


class FixedSizeQueue:
    def __init__(self, max_size, value=False, values: Iterable = None):
        self.queue = deque(maxlen=max_size)
        if not (values and isinstance(values, Iterable)):
            values = [False,] * max_size
        self.add_items(list(values))
        if isinstance(value, bool):
            self.add(value)

    def add(self, item) -> FixedSizeQueue:
        self.queue.append(item)
        return self

    def add_items(self, items: Iterable) -> FixedSizeQueue:
        self.queue.extend(items)
        return self

    def values(self) -> list:
        return list(self.queue)

    def clear(self) -> FixedSizeQueue:
        self.queue.clear()
        return self

    @property
    def any(self) -> bool:
        return any(self.queue)


class SeparatorWidget(QWidget):
    """分隔线组件"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setFixedHeight(1)
        self._color = QColor(0, 0, 0, 20)

    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setPen(QPen(self._color, 1))
        painter.drawLine(0, 0, self.width(), 0)


# ============================================================
# ReplayInfoWindow - 含暂停/继续、速度控制、进度条的信息栏
# ============================================================
class ReplayInfoWindow(QWidget):
    """回放信息栏：显示策略信息 + 暂停/继续 + 速度控制 + 进度"""

    # 信号：通知外部暂停/继续状态变化
    pause_toggled = pyqtSignal(bool)       # True=暂停, False=继续
    speed_changed = pyqtSignal(int)        # 新的速度(ms)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setFixedHeight(46)
        self._paused = True  # 初始为暂停/未开始状态
        self._speed_ms = 500  # 默认500ms一根K线

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)

        # 信息文本
        self.info_label = CaptionLabel("回放已暂停")
        font = QFont('Microsoft YaHei', 12)
        self.info_label.setFont(font)
        layout.addWidget(self.info_label)

        layout.addStretch(1)

        # 速度标签
        speed_label = BodyLabel("速度:", self)
        layout.addWidget(speed_label)

        # 速度滑块
        self.speed_slider = Slider(Qt.Orientation.Horizontal, self)
        self.speed_slider.setRange(50, 2000)  # 50ms ~ 2000ms
        self.speed_slider.setValue(self._speed_ms)
        self.speed_slider.setFixedWidth(120)
        self.speed_slider.setSingleStep(50)
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        layout.addWidget(self.speed_slider)

        # 速度数值显示
        self.speed_value_label = BodyLabel(f"{self._speed_ms}ms", self)
        self.speed_value_label.setFixedWidth(50)
        layout.addWidget(self.speed_value_label)

        # 进度文本
        self.progress_label = BodyLabel("0/0", self)
        self.progress_label.setFixedWidth(80)
        layout.addWidget(self.progress_label)

        # 暂停/继续按钮（初始显示播放，因为处于暂停/未开始状态）
        self.pause_btn = PushButton("播放", self)
        self.pause_btn.setFixedWidth(70)
        self.pause_btn.clicked.connect(self._toggle_pause)
        layout.addWidget(self.pause_btn)

    def _on_speed_changed(self, value):
        self._speed_ms = value
        self.speed_value_label.setText(f"{value}ms")
        self.speed_changed.emit(value)

    def _toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self.pause_btn.setText("播放")
            self.info_label.setText("回放已暂停")
        else:
            self.pause_btn.setText("暂停")
            self.info_label.setText("回放中...")
        self.pause_toggled.emit(self._paused)

    def set_paused(self, paused: bool):
        """外部设置暂停状态（如回放完成时自动暂停）"""
        if self._paused != paused:
            self._paused = paused
            if paused:
                self.pause_btn.setText("播放")
                self.info_label.setText("回放已暂停")
            else:
                self.pause_btn.setText("暂停")
                self.info_label.setText("回放中...")

    def set_info(self, text: str):
        self.info_label.setText(text)

    def set_progress(self, current: int, total: int):
        self.progress_label.setText(f"{current}/{total}")

    def set_speed(self, ms: int):
        self._speed_ms = ms
        self.speed_slider.setValue(ms)
        self.speed_value_label.setText(f"{ms}ms")

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def speed_ms(self) -> int:
        return self._speed_ms


# ============================================================
# ReplayTimer - 回放定时器，逐K线推进
# ============================================================
class ReplayTimer(QObject):
    """回放定时器：控制逐K线回放节奏"""

    step_completed = pyqtSignal(int, int)      # (current_index, total_candles)
    replay_finished = pyqtSignal()             # 回放完成

    def __init__(self, replay_window, speed_ms: int = 500):
        """
        Args:
            replay_window: ReplayWindow实例
            speed_ms: 每根K线间隔(ms)
        """
        super().__init__(replay_window)
        self._window = replay_window
        self._speed_ms = speed_ms
        self._paused = True
        self._current_index = 0
        self._total_candles = 0

        self._timer = QTimer(replay_window)
        self._timer.timeout.connect(self._step)

    def reset(self, start_index: int = 0, total_candles: int = 0):
        """重置回放状态"""
        self._current_index = start_index
        self._total_candles = total_candles
        self._paused = True
        self._timer.stop()
        self.step_completed.emit(self._current_index, self._total_candles)

    def start_replay(self):
        """开始/恢复回放"""
        if self._current_index >= self._total_candles:
            return
        self._paused = False
        self._timer.start(self._speed_ms)

    def pause_replay(self):
        """暂停回放"""
        self._paused = True
        self._timer.stop()

    def set_speed(self, ms: int):
        """修改回放速度"""
        self._speed_ms = ms
        if not self._paused:
            self._timer.start(ms)

    def _step(self):
        """单步推进"""
        if self._paused:
            return
        if self._current_index >= self._total_candles:
            self._timer.stop()
            self.replay_finished.emit()
            return

        # 执行一步回放
        try:
            self._window.replay_step(self._current_index)
        except Exception as e:
            print(f"回放步骤异常 (index={self._current_index}): {e}")

        self._current_index += 1
        self.step_completed.emit(self._current_index, self._total_candles)

        if self._current_index >= self._total_candles:
            self._timer.stop()
            self.replay_finished.emit()

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def total_candles(self) -> int:
        return self._total_candles


# ============================================================
# CustomToolBox - 画线工具箱（保持原有逻辑）
# ============================================================
class CustomToolBox(ToolBox):
    def __init__(self, chart):
        self.run_script = chart.run_script
        self.id = chart.id
        self.drawings = {}
        self.chart = chart
        self.window = chart.replay_window
        self._save_under = self.window
        chart.win.handlers[f'save_drawings{self.id}'] = self._save_drawings
        self.run_script(f'{self.id}.createToolBox()')

    def load_drawings(self, tag: str):
        target_drawings = self.window.drawings.get(tag)
        if not target_drawings:
            target_drawings = self.drawings.get(tag)
        if not target_drawings:
            return
        self.run_script(
            f'if ({self.id}.toolBox) {self.id}.toolBox.loadDrawings({json.dumps(target_drawings)})'
        )

    def _save_drawings(self, drawings: str):
        if not self._save_under:
            return
        parsed_drawings = json.loads(drawings)
        tag = self._save_under.current_chart_name
        self.drawings[tag] = parsed_drawings
        self.window.drawings[tag] = parsed_drawings

    def clear_drawings(self):
        js_script = f"""
        try {{
            if ({self.id}.toolBox) {{
                {self.id}.toolBox.clearDrawings();
            }}
        }} catch (e) {{
            console.error('清空画线失败：', e);
        }}
        """
        self.run_script(js_script)
        tag = self._save_under.current_chart_name
        self.drawings.pop(tag, None)
        self.window.drawings.pop(tag, None)

    def hide_toolbox(self):
        js_script = f"""
        try {{
            if ({self.id}.toolBox) {{
                {self.id}.toolBox.clearDrawings();
            }}
            const style = document.createElement('style');
            style.id = '{self.id}-toolbox-hide-style';
            style.textContent = `
                .tv-lightweight-charts-toolbox,
                .tv-toolbox-container,
                [class*="toolbox"],
                [data-toolbox="true"] {{
                    display: none !important;
                    visibility: hidden !important;
                    pointer-events: none !important;
                    opacity: 0 !important;
                }}
            `;
            const oldStyle = document.getElementById('{self.id}-toolbox-hide-style');
            if (oldStyle) oldStyle.remove();
            document.head.appendChild(style);
            {self.id}.toolBox = null;
        }} catch (e) {{
            console.error('隐藏工具箱失败：', e);
        }}
        """
        self.run_script(js_script)

    def show_toolbox(self):
        js_script = f"""
        try {{
            const style = document.getElementById('{self.id}-toolbox-hide-style');
            if (style) style.remove();
        }} catch (e) {{
            console.error('恢复工具箱样式失败：', e);
        }}
        """
        self.run_script(js_script)

    def cleanup(self):
        self.hide_toolbox()
        handler_key = f'save_drawings{self.id}'
        if hasattr(self.chart.win, 'handlers') and handler_key in self.chart.win.handlers:
            del self.chart.win.handlers[handler_key]
        self.drawings.clear()
        self.window.drawings.clear()


# ReplayChart - 适配回放模式的Chart

# ============================================================
# ReplayChart - 适配回放模式的Chart
# ============================================================
class ReplayChart:
    """
    回放模式下的Chart类
    与原始Chart的核心区别：
    1. 不使用实时API (wait_update)
    2. 数据来源于回测结果
    3. 通过replay_step()逐K线推进
    """
    toolbox: CustomToolBox = None
    position_horizontal_line: HorizontalLine = None
    chart_indicators: dict = {}
    subcharts: dict = {}
    markers: dict = {}

    def __init__(self, replay_window=None, contract_key: str = "",
                 kline_df: pd.DataFrame = None, indicators_data: list = None,
                 strategy=None, plot_id: int = 0):
        """
        Args:
            replay_window: ReplayWindow实例
            contract_key: 合约标识 (如 "KQ.m@SHFE.rb_3600")
            kline_df: K线DataFrame (含time列)
            indicators_data: 指标数据列表
            strategy: 策略实例
            plot_id: 该Chart对应的合约索引
        """
        self.replay_window = replay_window
        self.contract_key = contract_key
        self._kline_df = kline_df
        self._indicators_data = indicators_data or []
        self._strategy = strategy
        self._plot_id = plot_id
        self._current_index = 0
        self.display_only = getattr(replay_window, 'display_only', False)  # 纯展示模式标志

        # 创建底层QtChart
        chart_class = get_chart_class()
        self.chart = chart_class(None, 1.0, 1.0, False, False)
        self.light_chart_window = replay_window  # 兼容性
        self.period_milliseconds = 100
        self.strategy = strategy

        self.chart_indicators = {}
        self.subcharts = {}
        self.markers = {}
        self.signal_indicators = {}
        self._signal_markers = {}

        # WebView加载
        webview = self.get_webview()
        webview.page().loadFinished.connect(self._on_webview_loaded)

        if kline_df is not None:
            self._init_chart_data_replay()

    # ---- 委托方法 ----
    def get_webview(self):
        return self.chart.get_webview()

    def run_script(self, script, run_last=False):
        return self.chart.run_script(script, run_last)

    def set(self, df, keep_drawings=False):
        return self.chart.set(df, keep_drawings)

    def update(self, series, _from_tick=False):
        return self.chart.update(series, _from_tick)

    def create_line(self, name='', color='rgba(214, 237, 255, 0.6)', style='solid',
                    width=2, price_line=True, price_label=True, price_scale_id=None):
        return self.chart.create_line(name, color, style, width, price_line, price_label, price_scale_id)

    def create_subchart(self, position='left', width=0.5, height=0.5, sync=None,
                        scale_candles_only=False, sync_crosshairs_only=False, toolbox=False):
        return self.chart.create_subchart(position, width, height, sync,
                                          scale_candles_only, sync_crosshairs_only, toolbox)

    def create_histogram(self, name='', color='rgba(214, 237, 255, 0.6)',
                         price_line=True, price_label=True, price_scale_id=None):
        return self.chart.create_histogram(name, color, price_line, price_label, price_scale_id)

    def marker(self, time=None, position='below', shape='arrow_up', color='#2196F3', text=''):
        return self.chart.marker(time, position, shape, color, text)

    def marker_list(self, markers: list):
        return self.chart.marker_list(markers)

    def remove_marker(self, marker_id):
        return self.chart.remove_marker(marker_id)

    def clear_markers(self):
        return self.chart.clear_markers()

    def horizontal_line(self, price, color='rgb(122, 146, 202)', width=2, style='solid',
                        text='', axis_label_visible=True, func=None):
        return self.chart.horizontal_line(price, color, width, style, text, axis_label_visible, func)

    def candle_style(self, up_color='rgba(39, 157, 130, 100)', down_color='rgba(200, 97, 100, 100)',
                     wick_visible=True, border_visible=True, border_up_color='',
                     border_down_color='', wick_up_color='', wick_down_color=''):
        return self.chart.candle_style(up_color, down_color, wick_visible, border_visible,
                                       border_up_color, border_down_color, wick_up_color, wick_down_color)

    def watermark(self, text, font_size=44, color='rgba(180, 180, 200, 0.5)'):
        return self.chart.watermark(text, font_size, color)

    def layout(self, background_color='#000000', text_color=None, font_size=None, font_family=None):
        return self.chart.layout(background_color, text_color, font_size, font_family)

    def grid(self, vert_enabled=True, horz_enabled=True, color='rgba(29, 30, 38, 5)', style='solid'):
        return self.chart.grid(vert_enabled, horz_enabled, color, style)

    def legend(self, visible=False, ohlc=True, percent=True, lines=True, color='rgb(191, 195, 203)',
               font_size=11, font_family='Monaco', text='', color_based_on_candle=False):
        return self.chart.legend(visible, ohlc, percent, lines, color, font_size, font_family,
                                  text, color_based_on_candle)

    def resize(self, width=None, height=None):
        return self.chart.resize(width, height)

    def spinner(self, visible):
        return self.chart.spinner(visible)

    def fit(self):
        return self.chart.fit()

    def precision(self, precision):
        return self.chart.precision(precision)

    @property
    def id(self):
        return self.chart.id

    @property
    def win(self):
        return self.chart.win

    @property
    def events(self):
        return self.chart.events

    # ---- WebView加载回调 ----
    def _on_webview_loaded(self, success: bool):
        if success:
            QTimer.singleShot(300, self._post_load_setup)

    def _post_load_setup(self):
        """WebView加载完成后设置主题等"""
        dark = self.replay_window.isdark if hasattr(self.replay_window, 'isdark') else False
        self.setChartTheme(dark=dark)
        self._resizes()
        self.set_only_last_chart_xaxis_visible()
        self.add_chart_separator_lines()
        self.set_all_charts_crosshair_label_background()
        self._apply_price_scale_fixed_width()

    # ---- 回放数据初始化 ----
    def _init_chart_data_replay(self):
        """从回放数据初始化图表

        - 回放模式：只加载初始 initial_candles 根K线，后续通过 replay_step 逐根更新
        - 纯展示模式：一次性加载全部K线数据和指标，不再逐K线更新
        """
        initial_candles = self.replay_window.initial_candles
        df = self._kline_df

        # 确保有time列
        if 'time' not in df.columns:
            raise ValueError("K线数据缺少 'time' 列")

        total = len(df)
        if self.display_only:
            # 纯展示模式：加载全部K线数据
            load_len = total
        else:
            load_len = min(initial_candles, total)
        self._current_index = load_len - 1

        # 设置初始K线数据
        init_df = df.iloc[:load_len].copy()
        init_df = init_df[FILED.TALL].reset_index(drop=True)
        self.set(init_df, True)
        self.set_candle_style_default()

        # 水印：策略名称（仅在KLine未关闭水印时显示）
        should_show_watermark = True
        if self._strategy and hasattr(self._strategy, '_btklinedataset'):
            klines = list(self._strategy._btklinedataset.values())
            if self._plot_id < len(klines):
                should_show_watermark = klines[self._plot_id].iswatermark
        if should_show_watermark:
            sname = self._strategy.__class__.__name__ if self._strategy else "Replay"
            self.watermark(f"{sname} | {self.contract_key}")

        # 加载指标
        self._load_indicators(init_df, load_len)

        # 设置主题
        dark = self.replay_window.isdark if hasattr(self.replay_window, 'isdark') else False
        self.setChartTheme(dark=dark)
        self._resizes()

    def set_candle_style_default(self):
        self.candle_style(up_color=btcolors.bull_color, down_color=btcolors.bear_color)

    def _load_indicators(self, df: pd.DataFrame, load_len: int):
        """加载指标线和信号到图表"""
        colors = get_colors()

        for ind_data in self._indicators_data:
            isplot = ind_data.get("isplot", [])
            name = ind_data.get("name", "")
            lines = ind_data.get("lines", [])
            _lines = ind_data.get("_lines", [])
            overlaps = ind_data.get("overlaps", {})
            doubles = ind_data.get("doubles", False)
            plotinfo = ind_data.get("plotinfo", {})
            indicators = ind_data.get("indicators", None)
            signal_info = ind_data.get("signal", {})

            lineinfo = plotinfo.get('linestyle', {})
            # ---- 提前检查：如果所有 isplot 都是 False，直接跳过该指标 ----
            if isinstance(isplot, (list, tuple)):
                if len(isplot) > 0 and not any(isplot):
                    continue
            elif isinstance(isplot, bool) and not isplot:
                continue
            if indicators is None:
                continue

            # 多分组指标：indicators 是 list[ndarray]，每个元素对应一个分组
            if doubles and isinstance(indicators, list):
                # 为每个分组独立转换 numpy → DataFrame（保留完整数据用于回放更新）
                group_dfs_full = []
                for j in range(len(indicators)):
                    group_data = indicators[j]
                    group_lines = _lines[j] if isinstance(
                        _lines, (list, tuple)) and isinstance(_lines[0], (list, tuple)) else _lines
                    if not hasattr(group_data, 'columns'):
                        if hasattr(group_data, 'ndim') and group_data.ndim == 2 \
                                and group_data.shape[1] == len(group_lines):
                            group_df = pd.DataFrame(group_data, columns=group_lines)
                        elif hasattr(group_data, 'ndim') and group_data.ndim == 1:
                            group_df = pd.DataFrame({group_lines[0]: group_data})
                        elif hasattr(group_data, 'ndim') and group_data.ndim == 2 \
                                and group_data.shape[1] == 1:
                            group_df = pd.DataFrame({group_lines[0]: group_data.ravel()})
                        else:
                            group_df = pd.DataFrame({group_lines[0]: group_data})
                    else:
                        group_df = group_data  # 已经是 DataFrame
                    group_dfs_full.append(group_df)

                # 写回完整数据，确保 replay_step / _get_indicator_value 能访问全部K线
                ind_data["indicators"] = group_dfs_full

                # 使用分组数据绘制（仅绘制前 load_len 根）
                ind_dict = {}
                for j in range(len(group_dfs_full)):
                    gj_isplot = isplot[j] if isinstance(isplot, (list, tuple)) and isinstance(isplot[0], (list, tuple)) else isplot
                    if not any(gj_isplot) if isinstance(gj_isplot, (list, tuple)) else not gj_isplot:
                        continue
                    cache_dict = {}
                    gj_overlap = overlaps[j] if isinstance(overlaps, (list, tuple)) else overlaps
                    gj_lines = _lines[j] if isinstance(
                        _lines, (list, tuple)) and isinstance(_lines[0], (list, tuple)) else _lines
                    gj_name = name[j] if isinstance(name, (list, tuple)) else name

                    if gj_overlap:
                        chart = self
                    else:
                        chart = self.create_subchart('bottom', sync=True)
                        key_name = gj_name if isinstance(gj_name, str) else f"{gj_name}_{j}"
                        self.setSubChartTheme(chart, key_name)
                        self.subcharts[key_name] = chart

                    for i, plot in enumerate(gj_isplot if isinstance(gj_isplot, (list, tuple)) else [gj_isplot]):
                        if not plot:
                            continue
                        col = gj_lines[i] if isinstance(gj_lines, (list, tuple)) else gj_lines
                        info = lineinfo.get(col, {})
                        color = info.get("line_color") or next(colors)
                        style = info.get('line_dash', 'solid')
                        if style != "vbar" and style not in util.LINE_STYLE.__args__:
                            style = 'solid'
                        width = info.get("line_width", 2)
                        price_line = info.get("price_line", False)
                        price_label = info.get("price_label", False)

                        indicator = group_dfs_full[j].iloc[:load_len] if hasattr(group_dfs_full[j], 'iloc') else group_dfs_full[j][:load_len]
                        if style == "vbar":
                            line = chart.create_histogram(
                                name=col, color=color, price_line=price_line, price_label=price_label)
                            hist_data = pd.concat([df["time"].reset_index(drop=True),
                                                    indicator[col].reset_index(drop=True)], axis=1)
                            if "macdh" in col.lower():
                                hist_data['color'] = btcolors.bear_color
                                hist_data.loc[hist_data[col] > 0, 'color'] = btcolors.bull_color
                            line.set(hist_data)
                        else:
                            line = chart.create_line(
                                name=col, color=color, style=style, width=width,
                                price_line=price_line, price_label=price_label)
                            line_data = pd.concat([df["time"].reset_index(drop=True),
                                                    indicator[col].reset_index(drop=True)], axis=1)
                            line.set(line_data)
                        cache_dict[col] = line

                    if cache_dict:
                        ind_dict.update(cache_dict)

                if ind_dict:
                    key_name = name[0] if isinstance(name, (list, tuple)) else name
                    self.chart_indicators[key_name] = ind_dict
                continue  # 多分组处理完毕，跳过后续单分组逻辑

            # numpy数组 → DataFrame转换（镜像light_chart.py中v.pandas_object / pd.DataFrame行为）
            if not hasattr(indicators, 'columns'):
                if isinstance(_lines, (list, tuple)) and _lines:
                    if isinstance(_lines[0], (list, tuple)):
                        # doubles: 扁平化嵌套列名
                        col_names = [c for sub in _lines
                                     for c in (sub if isinstance(sub, (list, tuple)) else [sub])]
                    else:
                        col_names = list(_lines)
                else:
                    col_names = None

                if col_names and hasattr(indicators, 'ndim') and indicators.ndim == 2 \
                        and indicators.shape[1] == len(col_names):
                    indicators = pd.DataFrame(indicators, columns=col_names)
                elif col_names and hasattr(indicators, 'ndim') and indicators.ndim == 1:
                    indicators = pd.DataFrame({col_names[0]: indicators})
                elif col_names and hasattr(indicators, 'ndim') and indicators.ndim == 2 and indicators.shape[1] == 1:
                    indicators = pd.DataFrame({col_names[0]: indicators.ravel()})
                elif col_names and isinstance(indicators, list):
                    # Fallback: 普通Python list → DataFrame
                    if len(indicators) > 0 and isinstance(indicators[0], (list, tuple)):
                        indicators = pd.DataFrame(indicators, columns=col_names)
                    else:
                        indicators = pd.DataFrame({col_names[0]: indicators})

                # 写回原始数据，确保 replay_step 中能正确索引
                ind_data["indicators"] = indicators

            # 截取已K线长度的指标
            if hasattr(indicators, 'iloc'):
                indicator = indicators.iloc[:load_len]
            else:
                indicator = indicators[:load_len]

            if doubles:
                # 双面板指标（如MACD）
                ind_dict = {}
                for j in range(2):
                    if not any(isplot[j]) if isinstance(isplot[j], (list, tuple)) else not isplot[j]:
                        continue
                    cache_dict = {}
                    if overlaps[j] if isinstance(overlaps, (list, tuple)) else overlaps:
                        chart = self
                    else:
                        chart = self.create_subchart('bottom', sync=True)
                        self.setSubChartTheme(chart, name[j] if isinstance(name, (list, tuple)) else name)
                        key_name = name[j] if isinstance(name, (list, tuple)) else f"{name}_{j}"
                        self.subcharts[key_name] = chart

                    for i, plot in enumerate(isplot[j] if isinstance(isplot[j], (list, tuple)) else [isplot[j]]):
                        if not plot:
                            continue
                        col = _lines[j][i] if isinstance(_lines[j], (list, tuple)) else _lines[i]
                        info = lineinfo.get(col, {})
                        color = info.get("line_color") or next(colors)
                        style = info.get('line_dash', 'solid')
                        if style != "vbar" and style not in util.LINE_STYLE.__args__:
                            style = 'solid'
                        width = info.get("line_width", 2)
                        price_line = info.get("price_line", False)
                        price_label = info.get("price_label", False)

                        if style == "vbar":
                            line = chart.create_histogram(
                                name=col, color=color, price_line=price_line, price_label=price_label)
                            hist_data = pd.concat([df["time"].reset_index(drop=True),
                                                    indicator[col].reset_index(drop=True)], axis=1)
                            if "macdh" in col.lower():
                                hist_data['color'] = btcolors.bear_color
                                hist_data.loc[hist_data[col] > 0, 'color'] = btcolors.bull_color
                            line.set(hist_data)
                        else:
                            line = chart.create_line(
                                name=col, color=color, style=style, width=width,
                                price_line=price_line, price_label=price_label)
                            line_data = pd.concat([df["time"].reset_index(drop=True),
                                                    indicator[col].reset_index(drop=True)], axis=1)
                            line.set(line_data)
                        cache_dict[col] = line

                    if cache_dict:
                        ind_dict.update(cache_dict)

                if ind_dict:
                    key_name = name[0] if isinstance(name, (list, tuple)) else name
                    self.chart_indicators[key_name] = ind_dict
            else:
                # 单面板指标
                if not any(isplot):
                    continue
                ind_dict = {}
                is_candles = ind_data.get("iscandles", False)

                if is_candles:
                    chart = self.create_subchart('bottom', sync=True)
                    self.setSubChartTheme(chart, name if isinstance(name, str) else name[0])
                    self.subcharts[name] = chart
                    candles_df = indicator.copy()
                    candles_df["time"] = df["time"].reset_index(drop=True)
                    candles_df = candles_df[FILED.TOHLC]
                    chart.set(candles_df)
                else:
                    if overlaps:
                        chart = self
                    else:
                        chart = self.create_subchart('bottom', sync=True)
                        self.setSubChartTheme(chart, name if isinstance(name, str) else name[0])
                        self.subcharts[name] = chart

                    for i, plot in enumerate(isplot):
                        if not plot:
                            continue
                        col = _lines[i]
                        info = lineinfo.get(col, {})
                        color = info.get("line_color") or next(colors)
                        style = info.get('line_dash', 'solid')
                        if style != "vbar" and style not in util.LINE_STYLE.__args__:
                            style = 'solid'
                        width = info.get("line_width", 2)
                        price_line = info.get("price_line", False)
                        price_label = info.get("price_label", False)

                        if style == "vbar":
                            line = chart.create_histogram(
                                name=col, color=color, price_line=price_line, price_label=price_label)
                            hist_data = pd.concat([df["time"].reset_index(drop=True),
                                                    indicator[col].reset_index(drop=True)], axis=1)
                            if "macdh" in col.lower():
                                hist_data['color'] = btcolors.bear_color
                                hist_data.loc[hist_data[col] > 0, 'color'] = btcolors.bull_color
                            line.set(hist_data)
                        else:
                            line = chart.create_line(
                                name=col, color=color, style=style, width=width,
                                price_line=price_line, price_label=price_label)
                            line_data = pd.concat([df["time"].reset_index(drop=True),
                                                    indicator[col].reset_index(drop=True)], axis=1)
                            line.set(line_data)
                        ind_dict[col] = line

                if ind_dict:
                    self.chart_indicators[name] = ind_dict

                # 加载交易信号
                if signal_info:
                    signal_dict = {}
                    last_signal_dict = {}
                    all_markers = []

                    for signalname, signal_config in signal_info.items():
                        signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = \
                            list(signal_config.values())
                        if not signalshow:
                            continue
                        signal_series = indicator[signalname]
                        is_buy = any([n in signalname for n in ["long", "exitshort"]]) or "low" in signalkey.lower()
                        position = 'below' if is_buy else 'above'
                        shape = signalmarker if signalmarker in util.MARKER_SHAPE.__args__ else (
                            'arrow_up' if is_buy else 'arrow_down')
                        color = signalcolor if signalcolor else (btcolors.bear_color if is_buy else btcolors.bull_color)
                        text = signallabel.get("text", "") if isinstance(signallabel, dict) else ""
                        signalconfig = dict(position=position, shape=shape, color=color, text=text)

                        signal_points = signal_series[signal_series > 0]
                        for idx in signal_points.index:
                            time_val = df.iloc[idx]['time']
                            all_markers.append({"time": time_val, **signalconfig})

                        if not signal_series.empty and signal_series.iloc[-1] > 0:
                            last_signal_dict[signalname] = self.chart._single_datetime_format(
                                df.iloc[-1]['time'])

                        signal_dict[signalname] = signalconfig

                    if all_markers:
                        all_markers.sort(key=lambda x: x["time"])
                        self.marker_list(all_markers)
                    self.signal_indicators[name] = signal_dict

    # ---- 回放步进 ----
    def replay_step(self, index: int):
        """将图表推进到指定索引（加载第index根K线及其指标）

        纯展示模式下跳过更新（数据已在初始化时全部加载）。
        """
        if self.display_only:
            return  # 纯展示模式：不逐K线更新
        df = self._kline_df
        if index < 0 or index >= len(df):
            return

        self._current_index = index
        new_row = df.iloc[index]
        time_val = new_row['time']

        # 更新K线
        series = pd.Series(
            [time_val, new_row['open'], new_row['high'],
             new_row['low'], new_row['close'], new_row['volume']],
            index=FILED.TALL
        )
        self.chart.update(series)

        # 更新指标线
        for sname, lines_dict in self.chart_indicators.items():
            for col_name, line in lines_dict.items():
                try:
                    ind_data = self._get_indicator_value(sname, col_name, index)
                    if ind_data is not None:
                        if hasattr(line, 'update'):
                            s = pd.Series([time_val, ind_data], index=["time", col_name])
                            # MACD柱状图加颜色
                            if "macdh" in col_name.lower():
                                s['color'] = btcolors.bull_color if ind_data > 0 else btcolors.bear_color
                            line.update(s)
                except Exception as e:
                    pass  # 忽略单个指标更新失败

        # 更新副图（蜡烛图类型）
        for sub_name, sub_chart in self.subcharts.items():
            try:
                # 查找对应副图的指标数据
                for ind_data in self._indicators_data:
                    if ind_data.get("name") == sub_name and ind_data.get("iscandles"):
                        indicators = ind_data.get("indicators")
                        if indicators is not None:
                            if isinstance(indicators, list):
                                # 多分组：取第一个分组的数据
                                for gdf in indicators:
                                    if hasattr(gdf, 'iloc'):
                                        row = gdf.iloc[index]
                                        candle_series = pd.Series(
                                            [time_val, row.get('open', 0), row.get('high', 0),
                                             row.get('low', 0), row.get('close', 0)],
                                            index=FILED.TOHLC)
                                        sub_chart.update(candle_series)
                                        break
                            elif hasattr(indicators, 'iloc'):
                                row = indicators.iloc[index]
                                candle_series = pd.Series(
                                    [time_val, row.get('open', 0), row.get('high', 0),
                                     row.get('low', 0), row.get('close', 0)],
                                    index=FILED.TOHLC)
                                sub_chart.update(candle_series)
                        break
            except Exception as e:
                pass

        # 更新信号标记
        for sname, signal_dict in self.signal_indicators.items():
            for sk, sv in signal_dict.items():
                try:
                    ind_data = self._get_indicator_value(sname, sk, index)
                    if ind_data is None:
                        continue
                    time_key = self.chart._single_datetime_format(time_val)
                    marker_key = (sname, sk, time_key)
                    last_signal = self._signal_markers.pop(marker_key, None)

                    if ind_data > 0:
                        if last_signal is None:
                            last_signal = self.marker(time_val, **sv)
                        self._signal_markers[marker_key] = last_signal
                    else:
                        if last_signal is not None:
                            self.remove_marker(last_signal)
                except Exception:
                    pass

    def _get_indicator_value(self, sname: str, col_name: str, index: int):
        """从指标数据中获取指定位置的值"""
        for ind_data in self._indicators_data:
            indicators = ind_data.get("indicators")
            if indicators is None:
                continue
            name = ind_data.get("name", "")
            if isinstance(name, (list, tuple)):
                if sname not in name:
                    continue
            elif sname != name:
                continue

            # 多分组指标：indicators 是 list[DataFrame]，逐个查找
            if isinstance(indicators, list):
                for group_df in indicators:
                    if hasattr(group_df, 'iloc'):
                        try:
                            if col_name in group_df.columns:
                                return group_df.iloc[index][col_name]
                        except (IndexError, KeyError):
                            return None
                    elif hasattr(group_df, '__getitem__'):
                        try:
                            if col_name in group_df:
                                return group_df[col_name][index]
                        except (IndexError, KeyError):
                            return None
            elif hasattr(indicators, 'iloc'):
                try:
                    if col_name in indicators.columns:
                        return indicators.iloc[index][col_name]
                except (IndexError, KeyError):
                    return None
            elif hasattr(indicators, '__getitem__'):
                try:
                    if col_name in indicators:
                        return indicators[col_name][index]
                except (IndexError, KeyError):
                    return None
        return None

    # ---- 主题与布局 ----
    def setChartTheme(self, dark: bool = False):
        if dark:
            self.layout(background_color='rgb(6, 6, 6)', text_color='rgb(249, 249, 249)',
                        font_size=14, font_family='Microsoft YaHei')
            self.grid(color="rgb(26, 26, 26)")
            self.legend(visible=True, font_size=14, color='rgb(249, 249, 249)')
        else:
            self.layout(background_color='rgb(249, 249, 249)', text_color='rgb(6, 6, 6)',
                        font_size=14, font_family='Microsoft YaHei')
            self.grid(color="rgb(229, 229, 229)")
            self.legend(visible=True, font_size=14, color='rgb(6, 6, 6)')

    def setSubChartTheme(self, chart: AbstractChart, text: str = ""):
        dark = self.replay_window.isdark if hasattr(self.replay_window, 'isdark') else False
        if dark:
            chart.layout(background_color='rgb(6, 6, 6)', text_color='rgb(249, 249, 249)',
                         font_size=14, font_family='Microsoft YaHei')
            chart.grid(color="rgb(26, 26, 26)")
            chart.legend(True, text=text, color='rgb(249, 249, 249)', lines=True,
                         font_size=14, color_based_on_candle=True)
        else:
            chart.layout(background_color='rgb(249, 249, 249)', text_color='rgb(6, 6, 6)',
                         font_size=14, font_family='Microsoft YaHei')
            chart.grid(color="rgb(229, 229, 229)")
            chart.legend(True, text=text, color='rgb(6, 6, 6)', lines=True,
                         font_size=14, color_based_on_candle=True)

    def _resizes(self):
        """副图高度分配"""
        num_sub_chart = len(self.subcharts)
        if num_sub_chart == 0:
            self.resize(1, 1)
            return
        num = round(1. / (3 + num_sub_chart), 4)
        chart_size = 1. + 5e-3
        for _, subchart in self.subcharts.items():
            subchart.resize(1., num)
            chart_size -= num
        self.resize(1., chart_size)

    def set_only_last_chart_xaxis_visible(self):
        all_charts = [self] + list(self.subcharts.values())
        last_index = len(all_charts) - 1
        for idx, chart in enumerate(all_charts):
            is_visible = idx == last_index
            chart.run_script(f'''
                try {{
                    var tc = {chart.id};
                    if (tc && tc.chart && tc.chart.timeScale) {{
                        tc.chart.timeScale().applyOptions({{visible: {str(is_visible).lower()}}});
                    }}
                }} catch(e) {{}}
            ''')

    def add_chart_separator_lines(self):
        if not self.subcharts:
            return
        dark = self.replay_window.isdark if hasattr(self.replay_window, 'isdark') else False
        border_color = "rgba(180, 180, 180, 0.3)" if dark else "rgba(80, 80, 80, 0.3)"
        script = f'''
        (function() {{
            try {{
                var containers = document.querySelectorAll('.tv-lightweight-charts');
                for (var i = 0; i < containers.length - 1; i++) {{
                    containers[i].style.borderBottom = '1px solid {border_color}';
                }}
            }} catch(e) {{ console.error('add_chart_separator_lines:', e); }}
        }})();
        '''
        self.run_script(script)

    def _apply_price_scale_fixed_width(self):
        cached = self.replay_window.price_scale_widthes
        width = cached.get(self.contract_key)
        if width is None and cached:
            width = next(iter(cached.values()))
        if width is None:
            width = 80
        for _, chart in self.subcharts.items():
            self.set_price_scale_fixed_width(chart, width)

        def handle_result(result):
            if result is not None and result > 0:
                exact_width = int(result)
                cached[self.contract_key] = exact_width
                for _, chart in self.subcharts.items():
                    self.set_price_scale_fixed_width(chart, exact_width)

        script = f'''
            (function() {{
                if (typeof {self.id} !== 'undefined' && {self.id}.chart && {self.id}.chart.priceScale) {{
                    return {self.id}.chart.priceScale("right").width();
                }}
                return null;
            }})();
        '''
        self.get_webview().page().runJavaScript(script, handle_result)

    def set_price_scale_fixed_width(self, chart, target_width: int = None):
        target_width = target_width if (target_width is not None and target_width > 0) else 80
        if not chart or not hasattr(chart, 'id'):
            return
        chart.run_script(f'''
            try {{
                var targetChart = {chart.id};
                if (targetChart && targetChart.chart && targetChart.chart.priceScale) {{
                    var priceScale = targetChart.chart.priceScale("right");
                    priceScale.applyOptions({{
                        minimumWidth: {target_width},
                        width: {target_width},
                        autoScale: false
                    }});
                }}
            }} catch (e) {{
                console.error("设置副图价格轴宽度失败：", e);
            }}
        ''')

    def set_crosshair_label_background(self, chart: AbstractChart, bg: str = 'rgba(30, 30, 30, 0.9)'):
        script = f'''
        try {{
            const chart = {chart.id}.chart;
            if (chart && chart.applyOptions) {{
                chart.applyOptions({{
                    crosshair: {{
                        vertLine: {{
                            labelBackgroundColor: "{bg}"
                        }},
                        horzLine: {{
                            labelBackgroundColor: "{bg}"
                        }}
                    }}
                }});
            }}
        }} catch(e) {{}}
        '''
        chart.run_script(script)

    def set_all_charts_crosshair_label_background(self):
        settings = get_default_settings()
        bg = settings.get("mouse_label_color", 'rgba(50, 50, 50, 0.8)')
        charts = [self] + list(self.subcharts.values())
        for chart in charts:
            self.set_crosshair_label_background(chart, bg)

    def hide_data(self):
        return self.chart.hide_data()

    def show_data(self):
        return self.chart.show_data()

    def price_line(self, label_visible=True, line_visible=True, title=''):
        return self.chart.price_line(label_visible, line_visible, title)

    def set_watermark(self, **kwargs):
        return self.chart.watermark(**kwargs)

    # ---- 清理 ----
    def cleanup(self):
        if self.toolbox:
            self.toolbox.cleanup()
        self.chart_indicators.clear()
        self.subcharts.clear()
        self.markers.clear()
        self.signal_indicators.clear()
        self._signal_markers.clear()


# ============================================================
# ReplayWindow - 回放主窗口（SegmentedWidget导航）
# ============================================================
class ReplayWindow(QWidget):
    """
    回放主窗口
    布局：
    ┌────────────────────────────────────────┐
    │ ReplayInfoWindow（暂停/继续/速度/进度）   │
    ├────────────────────────────────────────┤
    │ strategySegmentedWidget（策略选择）      │
    ├────────────────────────────────────────┤
    │ chartSegmentedWidget（K线图选择）        │
    ├────────────────────────────────────────┤
    │                                        │
    │ QStackedWidget（图表显示区域）            │
    │                                        │
    └────────────────────────────────────────┘
    """
    current_chart_name: str = ""

    def __init__(self, parent=None, strategies: list[Strategy]= None,
                 initial_candles: int = 300, replay_speed_ms: int = 500,
                 backtest_completed: bool = False, display_only: bool = False,
                 optimization_df_data: list = None,
                 trade_records_by_strategy: dict = None):
        """
        Args:
            parent: 父窗口
            strategies: 策略实例列表
            initial_candles: 初始加载K线数
            replay_speed_ms: 回放速度(ms/根)
            backtest_completed: 是否已在外部完成回测（True则跳过回测，仅收集数据）
            display_only: 是否为纯图表展示模式（隐藏控制栏，全量加载，不逐K线更新）
            optimization_df_data: 优化 trial 数据列表（有数据时显示优化结果表格）
            trade_records_by_strategy: 交易记录 {strategy_idx: [trade_records]} 
        """
        super().__init__(parent=parent)
        self.strategies = strategies or []
        self.initial_candles = initial_candles
        self.replay_speed_ms = replay_speed_ms
        self.main_window = parent
        self.drawings: dict = {}
        self.price_scale_widthes: dict[str, int] = {}
        self.setObjectName("ReplayWindow")
        self.display_only = display_only  # 纯图表展示模式标志
        self.optimization_df_data = optimization_df_data  # 优化数据
        self.trade_records_by_strategy = trade_records_by_strategy or {}  # 交易记录

        # 存储所有Chart实例: {(strategy_idx, contract_key): ReplayChart}
        self.all_charts: dict = {}
        # 策略→合约映射: {strategy_idx: [contract_key, ...]}
        self.strategy_contracts: dict = {}
        # 策略名称
        self.strategy_names: list = []

        self._init_ui()
        if backtest_completed:
            self._collect_backtest_data()
        else:
            self._prepare_backtest_data()
        self._init_all_charts()

        # display_only 模式：隐藏信息栏（暂停/继续/速度控件不需要）

    def _init_ui(self):
        """初始化UI布局"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)


        # 1. 信息栏（display_only 模式隐藏）
        self.info_window = ReplayInfoWindow(self)
        self.info_window.pause_toggled.connect(self._on_pause_toggled)
        self.info_window.speed_changed.connect(self._on_speed_changed)
        layout.addWidget(self.info_window)
        self._info_separator = HorizontalSeparator()
        layout.addWidget(self._info_separator)
        if self.display_only:
            self.info_window.hide()
            self._info_separator.hide()

        # 2. 策略分段控件
        self.strategy_seg_scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Horizontal, parent=self)
        self.strategy_seg = SegmentedWidget(self)
        self.strategy_seg.addItem("strategy_placeholder", "策略")
        self.strategy_seg_scroll.setWidget(self.strategy_seg)
        self.strategy_seg_scroll.enableTransparentBackground()
        self.strategy_seg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strategy_seg_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.strategy_seg.adjustSize()
        self.strategy_seg_scroll.setFixedHeight(self.strategy_seg.height() + 4)
        layout.addWidget(self.strategy_seg_scroll)
        layout.addWidget(HorizontalSeparator())

        # 3. 图表分段控件（高度与策略分段控件保持一致，避免右侧出现滚动条）
        self.chart_seg_scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Horizontal, parent=self)
        self.chart_seg = SegmentedWidget(self)
        self.chart_seg.setFixedHeight(self.strategy_seg.height())
        self.chart_seg.addItem("chart_placeholder", "K线图")
        self.chart_seg_scroll.setWidget(self.chart_seg)
        self.chart_seg_scroll.enableTransparentBackground()
        self.chart_seg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chart_seg_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.chart_seg.adjustSize()
        self.chart_seg_scroll.setFixedHeight(self.strategy_seg_scroll.height())
        layout.addWidget(self.chart_seg_scroll)
        layout.addWidget(HorizontalSeparator())

        # 4. 图表堆叠容器
        self.chart_stack = QStackedWidget(self)
        layout.addWidget(self.chart_stack, stretch=1)

        # 5. 回放定时器
        self.replay_timer = ReplayTimer(self, self.replay_speed_ms)
        self.replay_timer.step_completed.connect(self._on_step_completed)
        self.replay_timer.replay_finished.connect(self._on_replay_finished)

        # 6. 优化结果表格（仅当有优化数据时创建）
        if self.optimization_df_data and len(self.optimization_df_data) > 0:
            self._init_opt_table()

    def _init_opt_table(self):
        """初始化优化结果表格，添加到 chart_stack + chart_seg"""
        # 创建容器 widget
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 4, 8, 4)
        container_layout.setSpacing(0)
        container.setObjectName("OptTableContainer")

        # 标题
        from qfluentwidgets import SubtitleLabel
        title = SubtitleLabel("参数优化结果")
        container_layout.addWidget(title)
        container_layout.addWidget(HorizontalSeparator())

        # 创建表格
        table = TableWidget(container)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)

        # 从数据中提取列名
        if self.optimization_df_data:
            columns = list(self.optimization_df_data[0].keys())
            table.setColumnCount(len(columns))
            table.setHorizontalHeaderLabels(columns)
            table.setRowCount(len(self.optimization_df_data))

            for row_idx, row_data in enumerate(self.optimization_df_data):
                for col_idx, col_name in enumerate(columns):
                    val = row_data.get(col_name, '')
                    table.setItem(row_idx, col_idx, QTableWidgetItem(str(val)))

        # 右键菜单 - 保存
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._on_opt_table_context_menu)

        # 保存表格引用
        self.opt_table = table
        self._opt_table_container = container

        container_layout.addWidget(table)
        # 添加到 chart_stack（index 0，后续 charts 从 index 1 开始）
        self.chart_stack.addWidget(container)

        # 添加到 chart_seg，用户可点击切换到优化结果表格
        self.chart_seg.insertItem(0, "opt_results", "参数优化",
            onClick=lambda key: self._show_opt_table())
        self.chart_seg.setCurrentItem("opt_results")

    def _show_opt_table(self):
        """切换到优化结果表格视图"""
        if hasattr(self, '_opt_table_container') and self._opt_table_container is not None:
            idx = self.chart_stack.indexOf(self._opt_table_container)
            if idx >= 0:
                self.chart_stack.setCurrentIndex(idx)

    def _show_trade_table(self, s_idx: int):
        """切换到指定策略的交易记录表格"""
        tr_key = f"trades_{s_idx}"
        if hasattr(self, '_trade_tables') and tr_key in self._trade_tables:
            container = self._trade_tables[tr_key]
        else:
            container = self._create_trade_table(s_idx)
        idx = self.chart_stack.indexOf(container)
        if idx >= 0:
            self.chart_stack.setCurrentIndex(idx)

    def _create_trade_table(self, s_idx: int):
        """创建指定策略的交易记录表格 widget"""
        tr_key = f"trades_{s_idx}"
        if not hasattr(self, '_trade_tables'):
            self._trade_tables = {}

        trade_records = self.trade_records_by_strategy.get(s_idx, [])

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 4, 8, 4)
        container_layout.setSpacing(0)
        container.setObjectName(f"TradeTableContainer_{s_idx}")

        from qfluentwidgets import SubtitleLabel
        sname = self.strategy_names[s_idx] if s_idx < len(self.strategy_names) else f"策略{s_idx}"
        title = SubtitleLabel(f"交易记录 - {sname}")
        container_layout.addWidget(title)
        container_layout.addWidget(HorizontalSeparator())

        table = TableWidget(container)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(
            ["序号", "方向", "时间", "价格(leg0)", "价格(1)", "盈亏", "手续费"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        for row, tr in enumerate(trade_records):
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            dir_item = QTableWidgetItem(str(tr.get("direction", "")))
            is_long = "多" in str(tr.get("direction", "")) or "long" in str(
                tr.get("direction", "")).lower()
            dir_item.setForeground(
                QColor('#4CAF50') if is_long else QColor('#F44336'))
            table.setItem(row, 1, dir_item)
            table.setItem(
                row, 2, QTableWidgetItem(str(tr.get("time", ""))))
            table.setItem(
                row, 3, QTableWidgetItem(str(tr.get("price0", ""))))
            table.setItem(
                row, 4, QTableWidgetItem(str(tr.get("price1", ""))))
            pnl = tr.get("pnl", 0)
            pnl_item = QTableWidgetItem(f"{float(pnl):.2f}")
            pnl_item.setForeground(QColor('#4CAF50') if float(
                pnl) >= 0 else QColor('#F44336'))
            table.setItem(row, 5, pnl_item)
            table.setItem(row, 6, QTableWidgetItem(
                f"{float(tr.get('fee', 0)):.2f}"))

        # 右键菜单 - 保存
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, _s_idx=s_idx: self._on_trade_table_context_menu(pos, _s_idx))

        container_layout.addWidget(table)
        self.chart_stack.addWidget(container)
        self._trade_tables[tr_key] = container
        return container

    def _on_trade_table_context_menu(self, pos, s_idx: int):
        """交易记录表格右键菜单：保存到下载目录"""
        table = self._trade_tables.get(f"trades_{s_idx}")
        if table is None:
            return
        # 找到容器内的 TableWidget
        table_widget = table.findChild(TableWidget)
        if table_widget is None:
            return

        menu = RoundMenu()
        menu.addAction(Action(FIF.SAVE, '保存', triggered=lambda: self._save_trade_table_to_file(s_idx)))
        menu.exec(table_widget.viewport().mapToGlobal(pos))

    def _save_trade_table_to_file(self, s_idx: int):
        """保存交易记录表格到系统设置中的下载目录"""
        import csv
        try:
            from miniqt.app.common.config import cfg
            download_dir = cfg.get(cfg.downloadFolder)
        except Exception:
            download_dir = os.path.expanduser("~/Downloads")

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sname = self.strategy_names[s_idx] if s_idx < len(self.strategy_names) else f"策略{s_idx}"
        default_name = f"trade_records_{sname}_{timestamp}.csv"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存交易记录", os.path.join(download_dir, default_name),
            "CSV 文件 (*.csv);;所有文件 (*)"
        )

        if not file_path:
            return

        try:
            table = self._trade_tables.get(f"trades_{s_idx}")
            if table is None:
                return
            table_widget = table.findChild(TableWidget)
            if table_widget is None:
                return

            # 提取表头和数据
            headers = []
            for col in range(table_widget.columnCount()):
                headers.append(table_widget.horizontalHeaderItem(col).text())

            rows = []
            for row in range(table_widget.rowCount()):
                row_data = []
                for col in range(table_widget.columnCount()):
                    item = table_widget.item(row, col)
                    row_data.append(item.text() if item else '')
                rows.append(row_data)

            with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)

            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.success(
                '保存成功',
                f'交易记录已保存到:\n{file_path}',
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self
            )
        except Exception as e:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(
                '保存失败',
                f'无法保存文件:\n{str(e)}',
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self
            )

    def _on_opt_table_context_menu(self, pos):
        """优化表格右键菜单：保存到下载目录"""
        # from PyQt6.QtWidgets import QMenu
        # from PyQt6.QtGui import QAction

        menu =  RoundMenu()
        # save_action = QAction("保存表格到下载目录", self)
        # save_action.triggered.connect(self._save_opt_table_to_file)
        # menu.addAction(save_action)
        menu.addAction(Action(FIF.SAVE,'保存',triggered=self._save_opt_table_to_file))
        menu.exec(self.opt_table.viewport().mapToGlobal(pos))

    def _save_opt_table_to_file(self):
        """保存优化结果表格到系统设置中的下载目录"""
        import csv
        try:
            # 获取下载目录
            from miniqt.app.common.config import cfg
            download_dir = cfg.get(cfg.downloadFolder)
        except Exception:
            download_dir = os.path.expanduser("~/Downloads")

        # 生成默认文件名
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"opt_results_{timestamp}.csv"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存优化结果", os.path.join(download_dir, default_name),
            "CSV 文件 (*.csv);;所有文件 (*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
                if self.optimization_df_data:
                    columns = list(self.optimization_df_data[0].keys())
                    writer = csv.DictWriter(f, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(self.optimization_df_data)

            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.success(
                '保存成功',
                f'优化结果已保存到:\n{file_path}',
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self
            )
        except Exception as e:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(
                '保存失败',
                f'无法保存文件:\n{str(e)}',
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self
            )

    @property
    def isdark(self) -> bool:
        """检测当前是否为深色主题

        优先级：
        1. main_window 的 isdark / _is_dark_theme 属性
        2. qfluentwidgets 全局主题（适用于 miniqt 回测模块嵌入场景）
        """
        if self.main_window:
            # 尝试从父窗口获取主题
            if hasattr(self.main_window, 'isdark'):
                return self.main_window.isdark
            if hasattr(self.main_window, '_is_dark_theme'):
                return self.main_window._is_dark_theme
            # 尝试从祖父窗口获取（miniqt 中 chart_container 的 parent 是 BacktestResultPanel）
            parent = self.main_window.parent()
            if parent:
                if hasattr(parent, 'is_dark_theme'):
                    return parent.is_dark_theme
                if hasattr(parent, '_is_dark_theme'):
                    return parent._is_dark_theme
        # 回退到 qfluentwidgets 全局主题检测
        try:
            from qfluentwidgets import isDarkTheme
            return isDarkTheme()
        except ImportError:
            return False

    def _prepare_backtest_data(self):
        """运行所有策略的回测，收集K线和指标数据"""
        self.strategy_names = []
        self.strategy_contracts = {}
        self._all_backtest_data = {}  # {strategy_idx: {contract_key: {...}}}

        for idx, strategy in enumerate(self.strategies):
            sname = strategy.__class__.__name__
            self.strategy_names.append(sname)

            # 第1步：运行完整回测（正常模式，非replay模式）
            try:
                strategy._strategy_replay = False
                strategy()  # 触发 _start_strategy_run → _execute_core_trading_loop
            except Exception as e:
                print(f"策略 [{sname}] 回测失败: {e}")
                continue

            # 第2步：切换到replay模式，重置回测索引
            strategy._strategy_replay = True
            strategy._btindex = self.initial_candles - 1

            # 第3步：收集K线数据和指标数据
            contracts = {}
            for plot_id, (k, v) in enumerate(strategy._btklinedataset.items()):
                contract_key = f"{v.symbol}_{v.cycle}"
                # 获取完整K线DataFrame
                if hasattr(v, 'pandas_object'):
                    kline_obj = v.pandas_object.copy()
                elif hasattr(v, '_dataset') and hasattr(v._dataset, 'tq_object'):
                    kline_obj = v._dataset.tq_object.copy()
                else:
                    print(f"  → 无法获取合约 [{contract_key}] 的K线数据")
                    continue

                # 构建K线DataFrame (含time列)
                kdf = kline_obj.copy()
                # 兼容 datetime 列可能是 datetime64 或数值类型
                if pd.api.types.is_datetime64_any_dtype(kdf['datetime']):
                    kdf['time'] = kdf['datetime'].astype('int64') + 8 * 3.6e12
                else:
                    kdf['time'] = kdf['datetime'] + 8 * 3.6e12
                kdf = kdf[FILED.TALL].reset_index(drop=True)

                # 收集该plot_id对应的指标数据
                indicators = []
                # 临时禁用_strategy_replay标记，获取完整指标数据（不受min_start_length限制）
                saved_replay_flag = getattr(strategy, '_strategy_replay', False)
                strategy._strategy_replay = False
                try:
                    for ik, iv in strategy._btindicatordataset.items():
                        if iv.plot_id == plot_id:
                            try:
                                pid, isplot, name, lines, _lines, ind_names, overlaps, \
                                    categorys, ind_indicators, doubles, _ind_plotinfo, span, _signal = \
                                    iv._get_plot_datas(ik)
                                indicators.append({
                                    "sname": iv.sname,
                                    "isplot": isplot,
                                    "name": name,
                                    "lines": lines,
                                    "_lines": _lines,
                                    "overlaps": overlaps,
                                    "doubles": doubles,
                                    "plotinfo": dict(_ind_plotinfo) if _ind_plotinfo else {},
                                    "indicators": ind_indicators,
                                    "signal": _signal,
                                    "iscandles": iv.iscandles,
                                })
                            except Exception as e:
                                print(f"  → 指标 [{ik}] 数据收集失败: {e}")
                finally:
                    strategy._strategy_replay = saved_replay_flag

                contracts[contract_key] = {
                    "kline_df": kdf,
                    "indicators": indicators,
                    "plot_id": plot_id,
                }

            self.strategy_contracts[idx] = list(contracts.keys())
            self._all_backtest_data[idx] = contracts
            print(f"策略 [{sname}] 回测完成，共 {len(contracts)} 个合约")

    def _collect_backtest_data(self):
        """收集已完成回测的数据（不重新运行回测，仅从策略中提取K线和指标数据）"""
        self.strategy_names = []
        self.strategy_contracts = {}
        self._all_backtest_data = {}

        for idx, strategy in enumerate(self.strategies):
            sname = strategy.__class__.__name__
            self.strategy_names.append(sname)

            # 收集K线数据和指标数据
            contracts = {}
            for plot_id, (k, v) in enumerate(strategy._btklinedataset.items()):
                contract_key = f"{v.symbol}_{v.cycle}"
                # 获取完整K线DataFrame
                if hasattr(v, 'pandas_object'):
                    kline_obj = v.pandas_object.copy()
                elif hasattr(v, '_dataset') and hasattr(v._dataset, 'tq_object'):
                    kline_obj = v._dataset.tq_object.copy()
                else:
                    print(f"  → 无法获取合约 [{contract_key}] 的K线数据")
                    continue

                # 构建K线DataFrame (含time列)
                kdf = kline_obj.copy()
                # 兼容 datetime 列可能是 datetime64 或数值类型
                if pd.api.types.is_datetime64_any_dtype(kdf['datetime']):
                    kdf['time'] = kdf['datetime'].astype('int64') + 8 * 3.6e12
                else:
                    kdf['time'] = kdf['datetime'] + 8 * 3.6e12
                kdf = kdf[FILED.TALL].reset_index(drop=True)

                # 收集该plot_id对应的指标数据
                indicators = []
                # 临时禁用_strategy_replay标记，获取完整指标数据（不受min_start_length限制）
                saved_replay_flag = getattr(strategy, '_strategy_replay', False)
                strategy._strategy_replay = False
                try:
                    for ik, iv in strategy._btindicatordataset.items():
                        if iv.plot_id == plot_id:
                            try:
                                pid, isplot, name, lines, _lines, ind_names, overlaps, \
                                    categorys, ind_indicators, doubles, _ind_plotinfo, span, _signal = \
                                    iv._get_plot_datas(ik)
                                indicators.append({
                                    "sname": iv.sname,
                                    "isplot": isplot,
                                    "name": name,
                                    "lines": lines,
                                    "_lines": _lines,
                                    "overlaps": overlaps,
                                    "doubles": doubles,
                                    "plotinfo": dict(_ind_plotinfo) if _ind_plotinfo else {},
                                    "indicators": ind_indicators,
                                    "signal": _signal,
                                    "iscandles": iv.iscandles,
                                })
                            except Exception as e:
                                print(f"  → 指标 [{ik}] 数据收集失败: {e}")
                finally:
                    strategy._strategy_replay = saved_replay_flag

                contracts[contract_key] = {
                    "kline_df": kdf,
                    "indicators": indicators,
                    "plot_id": plot_id,
                }

            self.strategy_contracts[idx] = list(contracts.keys())
            self._all_backtest_data[idx] = contracts
            print(f"策略 [{sname}] 数据收集完成，共 {len(contracts)} 个合约")

    def _init_all_charts(self):
        """初始化所有Chart实例（全部预加载）"""
        for s_idx, contracts in self._all_backtest_data.items():
            strategy = self.strategies[s_idx]
            for ck, cdata in contracts.items():
                chart_key = (s_idx, ck)
                try:
                    chart = ReplayChart(
                        replay_window=self,
                        contract_key=ck,
                        kline_df=cdata["kline_df"],
                        indicators_data=cdata["indicators"],
                        strategy=strategy,
                        plot_id=cdata["plot_id"],
                    )
                    self.all_charts[chart_key] = chart
                    self.chart_stack.addWidget(chart.get_webview())
                except Exception as e:
                    print(f"创建Chart失败 [{self.strategy_names[s_idx]}/{ck}]: {e}")

        # 填充SegmentedWidget
        self._fill_strategy_segments()
        self._fill_chart_segments(0)

        # 显示第一个Chart
        if self.all_charts:
            first_key = list(self.all_charts.keys())[0]
            self._show_chart(first_key)

        # 计算总K线数（取所有合约中最多的）
        if not self.display_only:
            max_candles = 0
            for contracts in self._all_backtest_data.values():
                for cdata in contracts.values():
                    max_candles = max(max_candles, len(cdata["kline_df"]))
            self.replay_timer.reset(self.initial_candles, max_candles)
            self.info_window.set_progress(self.initial_candles, max_candles)
            self.info_window.set_info("回放就绪 - 点击暂停按钮开始")

    def _fill_strategy_segments(self):
        """填充策略分段控件"""
        try:
            # 检查控件是否已被删除
            if not self.strategy_seg or not self.strategy_seg_scroll:
                print("[ReplayWindow] 策略分段控件已被删除，跳过填充")
                return
            
            self.strategy_seg.blockSignals(True)
            self.strategy_seg.clear()
            for idx, sname in enumerate(self.strategy_names):
                self.strategy_seg.addItem(
                    routeKey=str(idx),
                    text=sname,
                    onClick=lambda key, _idx=idx: self._on_strategy_selected(_idx)
                )
            if self.strategy_names:
                self.strategy_seg.setCurrentItem("0")
            self.strategy_seg.adjustSize()
            self.strategy_seg_scroll.setFixedHeight(self.strategy_seg.height() + 4)
            self.strategy_seg.blockSignals(False)
        except RuntimeError as e:
            # 捕获 RuntimeError: wrapped C/C++ object has been deleted
            if "has been deleted" in str(e):
                print("[ReplayWindow] 策略分段控件已被删除，跳过填充")
            else:
                raise e
        except Exception as e:
            print(f"[ReplayWindow] 填充策略分段控件失败: {e}")
            import traceback
            traceback.print_exc()
            self.strategy_seg.blockSignals(False)

    def _fill_chart_segments(self, s_idx: int):
        """填充指定策略的图表分段控件"""
        try:
            # 检查控件是否已被删除
            if not self.chart_seg or not self.chart_seg_scroll:
                print("[ReplayWindow] 图表分段控件已被删除，跳过填充")
                return
            
            # 先阻塞信号，防止 clear/setCurrentItem 触发级联切换
            self.chart_seg.blockSignals(True)
            self.chart_seg.clear()
            contracts = self.strategy_contracts.get(s_idx, [])
            for i, ck in enumerate(contracts):
                # 简化合约名显示
                display_name = ck.replace("KQ.m@", "").replace("KQ.i@", "")
                self.chart_seg.addItem(
                    routeKey=ck,
                    text=display_name,
                    onClick=lambda key, _s_idx=s_idx, _ck=ck: self._on_chart_selected(_s_idx, _ck)
                )
            if contracts:
                self.chart_seg.setCurrentItem(contracts[0])
            # 如果当前策略有交易记录，追加 "交易记录" 标签
            if self.trade_records_by_strategy and s_idx in self.trade_records_by_strategy:
                tr_key = f"trades_{s_idx}"
                self.chart_seg.addItem(
                    routeKey=tr_key,
                    text="交易记录",
                    onClick=lambda key, _s_idx=s_idx: self._show_trade_table(_s_idx)
                )
            # 如果存在优化结果表格，追加 "参数优化" 标签
            if hasattr(self, 'opt_table') and self.opt_table is not None:
                self.chart_seg.addItem(
                    routeKey="opt_results",
                    text="参数优化",
                    onClick=lambda key: self._show_opt_table()
                )
            self.chart_seg.adjustSize()
            # 确保 chart 分段控件高度与 strategy 分段控件一致
            self.chart_seg_scroll.setFixedHeight(self.strategy_seg_scroll.height())
            # 恢复信号
            self.chart_seg.blockSignals(False)
            #print(f"[ReplayWindow] _fill_chart_segments OK: s_idx={s_idx}, contracts={contracts}")
        except RuntimeError as e:
            # 捕获 RuntimeError: wrapped C/C++ object has been deleted
            if "has been deleted" in str(e):
                print("[ReplayWindow] 图表分段控件已被删除，跳过填充")
            else:
                raise e
        except Exception as e:
            print(f"[ReplayWindow] 填充图表分段控件失败: {e}")
            import traceback
            traceback.print_exc()
            self.chart_seg.blockSignals(False)

    def _on_strategy_selected(self, s_idx: int):
        """策略分段选择回调"""
        try:
            #print(f"[ReplayWindow] _on_strategy_selected: s_idx={s_idx}, name={self.strategy_names[s_idx] if s_idx < len(self.strategy_names) else '?'}")
            # 先更新 chart_seg（信号已阻塞，不会触发额外 _show_chart）
            self._fill_chart_segments(s_idx)
            # 然后主动切换 chart
            contracts = self.strategy_contracts.get(s_idx, [])
            if contracts:
                self._show_chart((s_idx, contracts[0]))
            else:
                print(f"[ReplayWindow] _on_strategy_selected WARN: s_idx={s_idx} 没有合约数据!")
            # 同步策略分段高亮
            self.strategy_seg.setCurrentItem(str(s_idx))
        except Exception as e:
            print(f"[ReplayWindow] 策略切换异常 (s_idx={s_idx}): {e}")
            import traceback
            traceback.print_exc()

    def _on_chart_selected(self, s_idx: int, ck: str):
        """图表分段选择回调"""
        try:
            self._show_chart((s_idx, ck))
        except Exception as e:
            print(f"[ReplayWindow] 图表切换异常 (s_idx={s_idx}, ck={ck}): {e}")
            import traceback
            traceback.print_exc()

    def _show_chart(self, chart_key: tuple):
        """切换到指定Chart的WebView"""
        s_idx, ck = chart_key
        sname = self.strategy_names[s_idx] if s_idx < len(self.strategy_names) else ""
        #print(f"[ReplayWindow] _show_chart: s_idx={s_idx}, ck={ck}, sname={sname}")

        chart = self.all_charts.get(chart_key)
        if chart is None:
            all_keys = list(self.all_charts.keys())
            print(f"[ReplayWindow] _show_chart FAIL: chart_key={chart_key} 不在 all_charts 中! 已有 keys: {all_keys}")
            return

        webview = chart.get_webview()
        if webview is None:
            print(f"[ReplayWindow] _show_chart FAIL: chart.get_webview() 返回 None! chart_key={chart_key}")
            return

        idx = self.chart_stack.indexOf(webview)
        if idx < 0:
            stack_count = self.chart_stack.count()
            print(f"[ReplayWindow] _show_chart FAIL: webview({id(webview)}) 不在 chart_stack 中! "
                  f"stack_count={stack_count}, webview_parent={webview.parent()}, "
                  f"chart_stack={id(self.chart_stack)}")
            # 尝试直接添加（恢复性修复）
            self.chart_stack.addWidget(webview)
            idx = self.chart_stack.count() - 1
            print(f"[ReplayWindow] _show_chart 恢复: 重新添加 webview, new_idx={idx}")

        # 切换前先确保 webview 可见
        webview.show()
        self.chart_stack.setCurrentIndex(idx)
        # 强制刷新 QStackedWidget
        self.chart_stack.updateGeometry()
        self.chart_stack.update()
        QApplication.processEvents()
        # 调用 _resizes 按比例分配主图和副图高度（主图占3，副图各占1）
        chart._resizes()
        chart.fit()
        # 延迟再执行一次 fit，确保 WebView 布局已稳定
        QTimer.singleShot(100, chart.fit)

        self.current_chart_name = ck
        self.info_window.set_info(f"{sname} | {ck}")
        #print(f"[ReplayWindow] _show_chart OK: 切换到 {sname} | {ck} (stack idx={idx})")

    def _on_pause_toggled(self, paused: bool):
        """暂停/继续切换"""
        if paused:
            self.replay_timer.pause_replay()
        else:
            self.replay_timer.start_replay()

    def _on_speed_changed(self, ms: int):
        """速度变化"""
        self.replay_timer.set_speed(ms)

    def _on_step_completed(self, current: int, total: int):
        """单步完成回调"""
        self.info_window.set_progress(current, total)

    def _on_replay_finished(self):
        """回放完成"""
        self.info_window.set_info("回放完成")
        self.info_window.set_paused(True)

    def replay_step(self, index: int):
        """执行所有Chart的一步回放（仅更新图表显示，不修改策略状态）"""
        # 同步所有策略的_btindex
        for strategy in self.strategies:
            strategy._btindex = index

        # 更新所有Chart到当前索引
        for chart_key, chart in self.all_charts.items():
            try:
                chart.replay_step(index)
            except Exception as e:
                print(f"Chart [{chart_key}] 回放步骤异常: {e}")

    def closeEvent(self, event):
        """关闭清理"""
        self.replay_timer.pause_replay()
        for chart in self.all_charts.values():
            try:
                chart.cleanup()
            except Exception:
                pass
        self.all_charts.clear()
        super().closeEvent(event)


# ============================================================
# EquityCurveChart - 权益曲线自定义绘制组件
# ============================================================
class EquityCurveChart(QWidget):
    """用QPainter绘制权益曲线和回撤曲线"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setMinimumHeight(120)
        self._equity: list[float] = [1.0]
        self._drawdown: list[float] = [0.0]
        self._line_color = QColor('#2196F3')
        self._fill_color = QColor(33, 150, 243, 40)
        self._dd_color = QColor('#F44336')
        self._dd_fill = QColor(244, 67, 54, 30)
        self._bg_color = QColor('#FFFFFF')
        self._grid_color = QColor(0, 0, 0, 20)
        self._text_color = QColor('#333333')
        self._plot_type = 0  # 0=权益, 1=回撤

    def set_data(self, equity: list[float], drawdown: list[float] | None = None):
        """设置权益和回撤数据"""
        if equity and len(equity) > 0:
            self._equity = list(equity)
        if drawdown and len(drawdown) > 0:
            self._drawdown = list(drawdown)
        self.update()

    def set_dark(self, dark: bool):
        """设置暗色主题"""
        if dark:
            self._bg_color = QColor('#1E1E2E')
            self._grid_color = QColor(255, 255, 255, 30)
            self._text_color = QColor('#CDD6F4')
        else:
            self._bg_color = QColor('#FFFFFF')
            self._grid_color = QColor(0, 0, 0, 20)
            self._text_color = QColor('#333333')
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        margin_l, margin_r = 60, 20
        margin_t, margin_b = 15, 35
        pw, ph = w - margin_l - margin_r, h - margin_t - margin_b

        # 背景
        painter.fillRect(0, 0, w, h, self._bg_color)

        # 标题
        title = "权益曲线" if self._plot_type == 0 else "回撤曲线"
        painter.setPen(self._text_color)
        painter.setFont(QFont('Microsoft YaHei', 10, QFont.Weight.Bold))
        painter.drawText(margin_l, margin_t - 2, pw, 18, Qt.AlignmentFlag.AlignLeft, title)

        # 切换按钮
        btn_text = "▼ 回撤" if self._plot_type == 0 else "▲ 权益"
        painter.setPen(QColor('#2196F3'))
        painter.setFont(QFont('Microsoft YaHei', 8))
        painter.drawText(w - margin_r - 60, margin_t - 2, 60, 18, Qt.AlignmentFlag.AlignRight, btn_text)

        data = self._equity if self._plot_type == 0 else self._drawdown
        if not data or len(data) < 1 or pw <= 0 or ph <= 0:
            painter.end()
            return

        # 数据值域
        mn, mx = min(data), max(data)
        if mx == mn:
            mx = mn + 1.0

        # 绘制网格
        painter.setPen(QPen(self._grid_color, 1, Qt.PenStyle.DashLine))
        n_grid = 4
        for i in range(n_grid + 1):
            y = margin_t + int(ph * i / n_grid)
            painter.drawLine(margin_l, y, w - margin_r, y)
            val = mx - (mx - mn) * i / n_grid
            label = f"{val:.2f}"
            painter.setPen(self._text_color)
            painter.setFont(QFont('Consolas', 7))
            painter.drawText(2, y - 7, margin_l - 4, 14, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(QPen(self._grid_color, 1, Qt.PenStyle.DashLine))

        # 零线（仅回撤图）
        if self._plot_type == 1 and mn < 0 < mx:
            zy = margin_t + int(ph * (mx - 0) / (mx - mn))
            painter.setPen(QPen(QColor(128, 128, 128), 1, Qt.PenStyle.SolidLine))
            painter.drawLine(margin_l, zy, w - margin_r, zy)

        # 点→像素变换
        def tx(i: int) -> int: return margin_l + int(pw * i / max(len(data) - 1, 1))
        def ty(v: float) -> int: return margin_t + int(ph * (mx - v) / (mx - mn))

        # 填充区域
        color = self._fill_color if self._plot_type == 0 else self._dd_fill
        poly = QPolygonF()
        poly.append(QPointF(tx(0), margin_t + ph))
        for i, v in enumerate(data):
            poly.append(QPointF(tx(i), ty(v)))
        poly.append(QPointF(tx(len(data) - 1), margin_t + ph))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(poly)

        # 曲线
        line_color = self._line_color if self._plot_type == 0 else self._dd_color
        painter.setPen(QPen(line_color, 2))
        for i in range(1, len(data)):
            painter.drawLine(tx(i - 1), ty(data[i - 1]), tx(i), ty(data[i]))

        # 起/终点标记
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#4CAF50'))
        painter.drawEllipse(tx(0) - 4, ty(data[0]) - 4, 8, 8)
        painter.setBrush(QColor('#F44336'))
        painter.drawEllipse(tx(len(data) - 1) - 4, ty(data[-1]) - 4, 8, 8)

        painter.end()

    def mouseReleaseEvent(self, event):
        """点击切换权益/回撤视图"""
        w = self.width()
        if event.pos().x() > w - 100 and event.pos().y() < 25:
            self._plot_type = 1 - self._plot_type
            self.update()


# ============================================================
# ResultPanel - 回测结果展示面板
# ============================================================
class ResultPanel(QWidget):
    """回测结果分析面板，展示权益曲线、统计指标、成交记录"""

    # 指标分组定义: (key, 显示标题)
    METRIC_GROUPS = {
        "收益指标": [
            ("profit", "最终收益"),
            ("return", "累计收益率"),
            ("total_fee", "总手续费"),
            ("payoff_ratio", "盈亏比"),
            ("avg_return", "平均收益"),
            ("avg_win", "平均盈利"),
        ],
        "风险指标": [
            ("sharpe", "夏普比率"),
            ("drawdown", "最大回撤"),
            ("var", "风险价值(VaR)"),
            ("risk_return", "风险收益比"),
        ],
        "交易指标": [
            ("winrate", "胜率"),
            ("wins", "盈利次数"),
            ("losses", "亏损次数"),
            ("profit_ratio", "收益比率"),
            ("trades", "交易次数"),
            ("avg_loss", "平均亏损"),
        ],
    }

    def __init__(self, parent=None, strategies: list = None):
        super().__init__(parent=parent)
        self.strategies = strategies or []
        self._current_idx = 0
        self._dark = False
        self.setObjectName("ResultPanel")
        self._card_widgets: dict[str, QWidget] = {}
        self._init_ui()
        if self.strategies:
            self._refresh()

    def _init_ui(self):
        # self.setMinimumSize(0, 0)
        self.adjustSize()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # --- 策略选择器 ---
        sel_row = QHBoxLayout()
        sel_label = BodyLabel("策略:", self)
        sel_row.addWidget(sel_label)
        self.strategy_sel = ComboBox(self)
        self.strategy_sel.currentIndexChanged.connect(self._on_strategy_changed)
        sel_row.addWidget(self.strategy_sel, stretch=1)
        sel_row.addStretch()
        layout.addLayout(sel_row)

        # --- 所有指标卡片放在一个 FlowLayout 中 ---
        metrics_container = QWidget(self)
        metrics_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._metrics_flow = FlowLayout(metrics_container, needAni=True, isTight=True)
        self._metrics_flow.setContentsMargins(0, 0, 0, 0)
        self._metrics_flow.setSpacing(8)
        self._metrics_flow.setVerticalSpacing(8)

        for group_name, metrics in self.METRIC_GROUPS.items():
            for key, title in metrics:
                card = self._make_metric_card(key, title, "--")
                self._card_widgets[key] = card
                self._metrics_flow.addWidget(card)

        layout.addWidget(metrics_container)

        # --- 权益曲线图 ---
        self.equity_chart = EquityCurveChart(self)
        self.equity_chart.setMinimumHeight(100)
        layout.addWidget(self.equity_chart, stretch=1)

        # --- 交易明细简表 ---
        self.trade_table = TableWidget(self)
        self.trade_table.setMinimumHeight(100)
        self.trade_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.trade_table.setColumnCount(7)
        self.trade_table.setHorizontalHeaderLabels(["索引", "方向", "开仓时间", "价格", "手数", "盈亏", "手续费"])
        self.trade_table.horizontalHeader().setStretchLastSection(True)
        self.trade_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.trade_table.setAlternatingRowColors(True)
        layout.addWidget(self.trade_table, stretch=1)

    def _make_metric_card(self, key: str, title: str, value: str) -> QWidget:
        """创建单个指标卡片"""
        card = CardWidget(self)
        card.setMinimumWidth(120)
        card.setMaximumWidth(180)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(10, 6, 10, 6)
        cl.setSpacing(4)
        lbl_title = CaptionLabel(title)
        cl.addWidget(lbl_title)
        lbl_val = BodyLabel(value)
        lbl_val.setFont(QFont('Consolas', 13, QFont.Weight.Bold))
        lbl_val.setObjectName(f"card_val_{key}")
        cl.addWidget(lbl_val)
        return card

    def _on_strategy_changed(self, idx: int):
        if idx >= 0 and idx < len(self.strategies):
            self._current_idx = idx
            self._refresh()

    def _refresh(self):
        """刷新当前选中策略的数据"""
        idx = self._current_idx
        if idx >= len(self.strategies):
            return

        s = self.strategies[idx]
        sname = s.__class__.__name__

        # 更新策略选择器
        current_texts = [self.strategy_sel.itemText(i) for i in range(self.strategy_sel.count())]
        expected_names = [st.__class__.__name__ for st in self.strategies]
        if current_texts != expected_names:
            self.strategy_sel.blockSignals(True)
            self.strategy_sel.clear()
            self.strategy_sel.addItems(expected_names)
            self.strategy_sel.setCurrentIndex(idx)
            self.strategy_sel.blockSignals(False)

        try:
            # 获取权益数据
            if hasattr(s, 'profits') and s.profits is not None:
                equity = s.profits.values.tolist() if hasattr(s.profits, 'values') else list(s.profits)
            elif hasattr(s, '_results') and s._results and len(s._results) > 0:
                equity = s._results[0]["total_profit"].values.tolist()
            else:
                equity = []

            # 计算回撤
            if equity and len(equity) > 0:
                peak = np.maximum.accumulate(np.array(equity, dtype=float))
                dd = (np.array(equity, dtype=float) - peak) / np.where(peak > 0, peak, 1.0)
                drawdown = dd.tolist()
            else:
                drawdown = []

            # 统计指标
            equity_arr = np.array(equity, dtype=float) if equity else np.array([1.0])
            initial = s.config.value if hasattr(s, 'config') and hasattr(s.config, 'value') else 1.0
            final = equity_arr[-1]
            ret = (final / initial - 1.0) * 100 if initial > 0 else 0

            # 默认值
            sharpe_val = 0.0
            max_dd = 0.0
            win_rate = 0.0
            profit_val = final - initial
            total_fee = 0.0
            payoff_ratio = 0.0
            avg_return_val = 0.0
            avg_win_val = 0.0
            avg_loss_val = 0.0
            var_val = 0.0
            risk_return = 0.0
            profit_ratio = 0.0
            wins = 0
            losses = 0
            trade_count = 0

            # 尝试从 _results 获取总手续费
            if hasattr(s, '_results') and s._results and len(s._results) > 0:
                try:
                    total_fee = float(s._results[0]["total_fee"].iloc[-1])
                except Exception:
                    total_fee = 0.0

            # 尝试从 _stats 获取详细指标
            if hasattr(s, '_stats') and s._stats is not None:
                st = s._stats
                try:
                    sharpe_val = st.sharpe() or 0
                except Exception:
                    sharpe_val = self._calc_sharpe(equity_arr)
                try:
                    max_dd = st.max_drawdown() * 100 or 0
                except Exception:
                    max_dd = min(drawdown) * 100 if drawdown else 0
                try:
                    win_rate = st.win_rate() * 100 or 0
                except Exception:
                    win_rate = 0.0
                try:
                    profit_val = st.profit() or (final - initial)
                except Exception:
                    profit_val = final - initial
                try:
                    payoff_ratio = st.payoff_ratio() or 0
                except Exception:
                    payoff_ratio = 0.0
                try:
                    avg_return_val = st.avg_return() or 0
                except Exception:
                    avg_return_val = 0.0
                try:
                    avg_win_val = st.avg_win() or 0
                except Exception:
                    avg_win_val = 0.0
                try:
                    avg_loss_val = st.avg_loss() or 0
                except Exception:
                    avg_loss_val = 0.0
                try:
                    var_val = st.value_at_risk() or 0
                except Exception:
                    var_val = 0.0
                try:
                    risk_return = st.risk_return_ratio() or 0
                except Exception:
                    risk_return = 0.0
                try:
                    profit_ratio = st.profit_ratio() or 0
                except Exception:
                    profit_ratio = 0.0
                # 盈利/亏损次数
                try:
                    if st.profit_array is not None:
                        diffs = pd.Series(st.profit_array).diff().fillna(0)
                        wins = int(len(diffs[diffs > 0]))
                        losses = int(len(diffs[diffs < 0]))
                except Exception:
                    wins = 0
                    losses = 0
            else:
                sharpe_val = self._calc_sharpe(equity_arr)
                max_dd = min(drawdown) * 100 if drawdown else 0
                profit_val = final - initial

            # 交易次数
            trade_count = self._count_trades(s)

            # 更新所有卡片
            self._update_card("profit", f"{profit_val:+.2f}")
            self._update_card("return", f"{ret:+.2f}%")
            self._update_card("total_fee", f"{total_fee:.2f}")
            self._update_card("payoff_ratio", f"{payoff_ratio:.4f}")
            self._update_card("avg_return", f"{avg_return_val:.6f}")
            self._update_card("avg_win", f"{avg_win_val:.6f}")
            self._update_card("sharpe", f"{sharpe_val:.4f}")
            self._update_card("drawdown", f"{max_dd:.2f}%")
            self._update_card("var", f"{var_val:.4f}")
            self._update_card("risk_return", f"{risk_return:.4f}")
            self._update_card("winrate", f"{win_rate:.2f}%")
            self._update_card("wins", str(wins))
            self._update_card("losses", str(losses))
            self._update_card("profit_ratio", f"{profit_ratio:.4f}")
            self._update_card("trades", str(trade_count))
            self._update_card("avg_loss", f"{avg_loss_val:.6f}")

            # 刷新权益图表
            self.equity_chart.set_data(equity, drawdown)

            # 刷新交易表
            self._refresh_trade_table(s)

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"刷新结果面板数据失败 [{sname}]: {e}")

    @staticmethod
    def _calc_sharpe(equity: np.ndarray) -> float:
        """简单计算夏普比率（年化）"""
        if len(equity) < 2:
            return 0.0
        returns = np.diff(equity) / (np.abs(equity[:-1]) + 1e-8)
        if np.std(returns) == 0:
            return 0.0
        return float(np.mean(returns) / np.std(returns) * np.sqrt(252))

    @staticmethod
    def _count_trades(strategy) -> int:
        """估算交易次数"""
        cnt = 0
        try:
            if hasattr(strategy, '_results') and strategy._results and len(strategy._results) > 0:
                pos = strategy._results[0]["positions"].values
                # 统计仓位变化次数（从0变非0/非0变0）
                pos_arr = np.array(pos, dtype=float)
                cnt = int(np.sum(np.diff(np.abs(pos_arr) > 0) != 0) + (pos_arr[0] != 0))
        except Exception:
            cnt = 0
        return cnt

    def _refresh_trade_table(self, strategy):
        """刷新交易明细表（简版，提取仓位变化点）"""
        self.trade_table.setRowCount(0)
        try:
            if not hasattr(strategy, '_results') or not strategy._results:
                return
            df = strategy._results[0]
            if len(df) == 0:
                return

            # 获取K线数据，用于取价格和开仓时间
            kline_open = None
            kline_datetime = None
            if hasattr(strategy, '_btklinedataset') and strategy._btklinedataset:
                kline_obj = list(strategy._btklinedataset.values())[0]
                if hasattr(kline_obj, 'pandas_object'):
                    kdf = kline_obj.pandas_object
                    kline_open = kdf['open'].values
                    kline_datetime = kdf['datetime'].values

            pos = df["positions"].values
            pos_arr = np.array(pos, dtype=float)

            # 找到仓位变化点
            signals = []
            prev_pos = 0.0
            open_time = None  # 记录开仓时间
            prev_total_fee = 0.0  # 上一笔累计手续费

            for i in range(len(pos_arr)):
                curr_total_fee = float(df.iloc[i].get("total_fee", 0))

                if prev_pos == 0 and pos_arr[i] != 0:
                    # 开仓
                    dir_str = "多头" if pos_arr[i] > 0 else "空头"
                    price = float(kline_open[i]) if kline_open is not None and i < len(kline_open) else 0
                    open_time = (str(kline_datetime[i]) if kline_datetime is not None and i < len(kline_datetime)
                                 else str(df.index[i]) if hasattr(df, 'index') else f"#{i}")
                    size = abs(float(df.iloc[i].get("sizes", 0)))
                    fee_delta = curr_total_fee - prev_total_fee
                    prev_total_fee = curr_total_fee
                    signals.append((i, dir_str, open_time, price, size, fee_delta))
                elif prev_pos != 0 and pos_arr[i] == 0:
                    # 平仓
                    price = float(kline_open[i]) if kline_open is not None and i < len(kline_open) else 0
                    o_time = open_time or "--"
                    size = abs(float(df.iloc[i].get("sizes", 0)))
                    fee_delta = curr_total_fee - prev_total_fee
                    prev_total_fee = curr_total_fee
                    signals.append((i, "平仓", o_time, price, size, fee_delta))
                    open_time = None
                elif prev_pos != 0 and pos_arr[i] != 0 and np.sign(prev_pos) != np.sign(pos_arr[i]):
                    # 反手
                    dir_str = "多头" if pos_arr[i] > 0 else "空头"
                    price = float(kline_open[i]) if kline_open is not None and i < len(kline_open) else 0
                    o_time = open_time or "--"
                    size = abs(float(df.iloc[i].get("sizes", 0)))
                    fee_delta = curr_total_fee - prev_total_fee
                    prev_total_fee = curr_total_fee
                    signals.append((i, f"反手→{dir_str}", o_time, price, size, fee_delta))
                    # 记录新的开仓时间
                    open_time = (str(kline_datetime[i]) if kline_datetime is not None and i < len(kline_datetime)
                                 else str(df.index[i]) if hasattr(df, 'index') else f"#{i}")
                prev_pos = float(pos_arr[i])

                # 最多500行
                if len(signals) >= 500:
                    break

            total_rows = len(signals) + (1 if signals else 0)
            self.trade_table.setRowCount(total_rows)

            total_size = 0.0
            total_pnl = 0.0
            total_fee = 0.0

            for row, (idx, direction, otime, price, size, fee_delta) in enumerate(signals):
                # 索引
                self.trade_table.setItem(row, 0, QTableWidgetItem(str(idx)))
                # 方向
                dir_item = QTableWidgetItem(direction)
                if "多" in direction:
                    dir_item.setForeground(QColor('#F44336'))
                elif "平" in direction:
                    dir_item.setForeground(QColor('#FF9800'))
                elif "空" in direction or "反手" in direction:
                    dir_item.setForeground(QColor('#4CAF50'))
                self.trade_table.setItem(row, 1, dir_item)
                # 开仓时间
                self.trade_table.setItem(row, 2, QTableWidgetItem(str(otime)))
                # 价格
                self.trade_table.setItem(row, 3, QTableWidgetItem(f"{price:.2f}"))
                # 手数
                self.trade_table.setItem(row, 4, QTableWidgetItem(str(size)))
                # 盈亏（简算）
                pnl = float(df.iloc[idx].get("float_profits", 0)) if idx < len(df) else 0
                pnl_item = QTableWidgetItem(f"{pnl:.2f}")
                pnl_item.setForeground(QColor('#4CAF50') if pnl >= 0 else QColor('#F44336'))
                self.trade_table.setItem(row, 5, pnl_item)
                # 手续费（单笔增量）
                self.trade_table.setItem(row, 6, QTableWidgetItem(f"{fee_delta:.2f}"))

                total_size += size
                total_pnl += pnl
                total_fee += fee_delta

            # 添加合计行
            if signals:
                row = len(signals)
                self.trade_table.setItem(row, 0, QTableWidgetItem("合计"))
                self.trade_table.setItem(row, 1, QTableWidgetItem("-"))
                self.trade_table.setItem(row, 2, QTableWidgetItem("-"))
                self.trade_table.setItem(row, 3, QTableWidgetItem("-"))
                self.trade_table.setItem(row, 4, QTableWidgetItem(f"{total_size:.2f}"))
                pnl_item = QTableWidgetItem(f"{total_pnl:.2f}")
                pnl_item.setForeground(QColor('#4CAF50') if total_pnl >= 0 else QColor('#F44336'))
                self.trade_table.setItem(row, 5, pnl_item)
                self.trade_table.setItem(row, 6, QTableWidgetItem(f"{total_fee:.2f}"))
                self.trade_table.scrollToBottom()

        except Exception:
            pass

    def _update_card(self, key: str, value: str):
        """更新指标卡片值"""
        card = self._card_widgets.get(key)
        if card:
            lbl = card.findChild(BodyLabel, f"card_val_{key}")
            if lbl:
                lbl.setText(value)

    def set_dark(self, dark: bool):
        """设置暗色主题——qfluentwidgets 控件会自动跟随主题，只需更新自定义绘图"""
        self._dark = dark
        self.equity_chart.set_dark(dark)
        # 同步 ResultPanel 背景色，避免 QScrollArea 包裹后出现色差
        bg = QColor(32, 32, 32) if dark else QColor(243, 243, 243)
        self.setStyleSheet(f"#ResultPanel {{ background-color: {bg.name()}; }}")
        self.update()


# ============================================================
# CustomTitleBar - 自定义标题栏
# ============================================================
class CustomTitleBar(FluentTitleBar):
    def __init__(self, parent):
        super().__init__(parent)
        self._parent = parent
        self.iconLabel.setFixedSize(36, 36)
        # 不在此处调用 _update_theme()，由 MainWindow.setTheme() 统一刷新

        self.avatar = TransparentToolButton(FluentIcon.CONSTRACT, self)
        self.avatar.setIconSize(QSize(18, 18))
        self.avatar.setFixedHeight(24)
        self.hBoxLayout.insertWidget(2, self.avatar, 0, Qt.AlignmentFlag.AlignRight)
        self.avatar.clicked.connect(parent.setTheme)
        self._update_theme()

    def _update_theme(self):
        theme = self._parent.theme if hasattr(self._parent, 'theme') else Theme.DARK
        FluentStyleSheet.FLUENT_WINDOW.apply(self, theme)
        # 显式同步按钮颜色，避免初始化时 qproperty 样式表未生效
        # is_dark = theme == Theme.DARK
        # color = QColor(255, 255, 255) if is_dark else QColor(0, 0, 0)
        # for btn in (self.minBtn, self.maxBtn, self.closeBtn):
        #     btn.setNormalColor(color)
        #     btn.setHoverColor(color)
        #     if btn.__class__.__name__ != 'CloseButton':
        #         btn.setPressedColor(color)

    def setIcon(self, icon):
        self.iconLabel.setPixmap(QIcon(icon).pixmap(36, 36))


# ============================================================
# MainWindow - 主窗口（简化版，用于回放）
# ============================================================
class MainWindow(FluentWindow):
    """回放模式主窗口"""

    def __init__(self, strategies: list = None, initial_candles: int = 300,
                 replay_speed_ms: int = 500, backtest_completed: bool = False,
                 display_only: bool = False):
        super().__init__()
        self.title_height = 36
        self.theme = Theme.LIGHT
        self.isdark = False  # 用于Chart主题判断
        self.base_dir = strategies[0]._base_dir if strategies and hasattr(strategies[0], '_base_dir') else ""
        self._display_only = display_only
        self._strategies = strategies
        self.initNavigation()
        self.initWindow()

        # K线图/回放窗口
        self.replay_window = ReplayWindow(
            self, strategies, initial_candles, replay_speed_ms,
            backtest_completed=backtest_completed,
            display_only=display_only)
        chart_title = '图表展示' if display_only else '回放'
        self.addSubInterface(self.replay_window, FluentIcon.HOME, chart_title)

        # 回测结果面板（用 QScrollArea 包裹，防止 FlowLayout/TableWidget 撑大窗口最小高度）
        self.result_panel = ResultPanel(self, strategies=strategies)
        result_scroll = QScrollArea(self)
        result_scroll.setObjectName("result_scroll")
        result_scroll.setWidget(self.result_panel)
        result_scroll.setWidgetResizable(True)
        result_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        result_scroll.setFrameShape(QFrame.Shape.NoFrame)
        result_scroll.viewport().setAutoFillBackground(False)
        self.addSubInterface(result_scroll, FluentIcon.INFO, '回测结果')

        # 同步当前主题到结果面板（initWindow/setTheme 在创建 result_panel 之前已执行）
        self.result_panel.set_dark(self.isdark)

        # 显示导航栏，并在display_only模式下切换到结果面板
        self.navigationInterface.show()
        # 注意：addSubInterface 注册的是 result_scroll(QScrollArea)，不是内部的 result_panel
        #       FluentWindow.switchTo 通过 stackedWidget.indexOf 查找，必须传入被注册的 widget
        self.switchTo(result_scroll if display_only else self.replay_window)

    def initNavigation(self):
        self.setTitleBar(CustomTitleBar(self))
        self.setMicaEffectEnabled(False)
        self.navigationInterface.setAcrylicEnabled(False)
        # 有多个Tab时不隐藏导航栏
        self.navigationInterface.setExpandWidth(160)

    def initWindow(self):
        self.resize(900, 700)
        self.setMinimumSize(800, 500)
        self.widgetLayout.setContentsMargins(0, self.title_height, 0, 0)
        self.setTheme()
        screen = QGuiApplication.primaryScreen()
        desktop = screen.availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

    def showEvent(self, e):
        super().showEvent(e)
        # 强制确保 show 后窗口严格为 900x700，防止 DPI 或子控件影响初始大小
        self.resize(900, 700)

    def setTheme(self):
        self.theme = Theme.LIGHT if self.isdark else Theme.DARK
        self.isdark = not self.isdark
        setTheme(self.theme)
        # 更新标题栏样式
        # tb = self.titleBar
        # if isinstance(tb, CustomTitleBar):
        #     tb._update_theme()
        # 通知图表和结果面板更新主题
        if hasattr(self, 'replay_window'):
            for chart in self.replay_window.all_charts.values():
                if hasattr(chart, 'setChartTheme'):
                    chart.setChartTheme(dark=self.isdark)
                if hasattr(chart, 'setSubChartTheme'):
                    for sub_name, sub_chart in chart.subcharts.items():
                        chart.setSubChartTheme(sub_chart, text=sub_name)
                if hasattr(chart, 'add_chart_separator_lines'):
                    chart.add_chart_separator_lines()
        if hasattr(self, 'result_panel'):
            self.result_panel.set_dark(self.isdark)
        self._seticon()

    def _seticon(self):
        if self.isdark:
            path = os.path.join(self.base_dir, "data", "minibt_dark.png")
        else:
            path = os.path.join(self.base_dir, "data", "minibt_light.png")
        if os.path.exists(path):
            icon = QIcon(path)
            tb = self.titleBar
            if isinstance(tb, CustomTitleBar):
                tb.setIcon(icon)

    def closeEvent(self, e):
        try:
            if hasattr(self.replay_window, 'replay_timer'):
                self.replay_window.replay_timer.pause_replay()
        except Exception:
            pass
        return super().closeEvent(e)


# ============================================================
# main() - 回放入口
# ============================================================
def main(strategies: list, initial_candles: int = 300, replay_speed_ms: int = 500,
         backtest_completed: bool = False, display_only: bool = False):
    """
    回放模式入口

    Args:
        strategies: 策略实例列表 (需已设置好_btklinedataset)
        initial_candles: 初始加载K线数量 (默认300)
        replay_speed_ms: 回放速度，每根K线间隔毫秒 (默认500)
        backtest_completed: 是否已在外部完成回测，True则跳过回测直接收集数据
        display_only: 是否为纯图表展示模式（不逐K线回放，隐藏控制栏，全量加载）
    """
    try:
        import warnings
        warnings.filterwarnings(
            "ignore", message="QFont::setPointSize: Point size <= 0 (-1), must be greater than 0")

        from PyQt6.QtCore import Qt, QTranslator, qInstallMessageHandler, QtMsgType

        def message_handler(msg_type, context, message):
            if "QFont::setPointSize" in message:
                return
            if "QWidgetWindow" in message and "must be a top level window" in message:
                return
            if msg_type == QtMsgType.QtDebugMsg:
                print(f"Debug: {message}")
            elif msg_type == QtMsgType.QtWarningMsg:
                print(f"Warning: {message}")
            elif msg_type == QtMsgType.QtCriticalMsg:
                print(f"Critical: {message}")
            elif msg_type == QtMsgType.QtFatalMsg:
                print(f"Fatal: {message}")

        qInstallMessageHandler(message_handler)

        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtCore import Qt, QObject, pyqtSlot, QUrl, QTimer

        app = QApplication.instance()
        if app is None:
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
            app = QApplication(sys.argv)

        app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
        import lightweight_charts.widgets

        w = MainWindow(strategies, initial_candles, replay_speed_ms,
                       backtest_completed=backtest_completed,
                       display_only=display_only)
        w.show()
        app.exec()

    except Exception as e:
        import traceback
        print(f"回放模式崩溃，错误信息：{e}")
        traceback.print_exc()
        input("按回车键退出...")


if __name__ == "__main__":
    # 测试示例：需要先创建策略实例并传入
    print("请通过 main(strategies, initial_candles, replay_speed_ms) 调用回放模式")
