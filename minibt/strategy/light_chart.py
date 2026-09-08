# coding:utf-8
# https://github.com/louisnw01/lightweight-charts-python
# https://tradingview.github.io/lightweight-charts/
# https://lightweight-charts-python.readthedocs.io/en/latest/index.html

from __future__ import annotations
from collections import deque
import json
from itertools import cycle
from ..other import FILED, os, pd, partial, FilteredOutputRedirector, sys
from ..utils import Colors as btcolors, OrderedDict, _time,Iterable,TYPE_CHECKING
with FilteredOutputRedirector():
    from qfluentwidgets import (setTheme, Theme, FluentWindow, TransparentToolButton, RoundMenu, Action,
                                FluentIcon, FluentStyleSheet, FluentTitleBar, TableWidget, CaptionLabel,
                                Dialog, SearchLineEdit, ListWidget, PushButton,
                                BodyLabel, DoubleSpinBox, TitleLabel, SwitchButton, PrimaryPushButton,
                                ComboBox,  CardWidget, ScrollArea, ColorDialog,
                                InfoBar, InfoBarPosition, HyperlinkLabel)
from PyQt6.QtWidgets import (QApplication, QSplitter, QWidget, QVBoxLayout, QSizePolicy,
                               QHBoxLayout, QAbstractItemView, QTableWidgetItem, QHeaderView,)
from PyQt6.QtGui import QColor, QFont, QPainter, QIcon, QPen, QGuiApplication
from PyQt6.QtCore import Qt, QSize, QTimer, QUrl, QObject, QThread, QMetaObject, QEvent, pyqtSignal

# 阻止 lightweight_charts 自动加载 PySide6（强制使用 PyQt6）
import builtins as _builtins
_original_import = _builtins.__import__

def _blocked_import(name, *args, **kwargs):
    if name.startswith('PySide6'):
        raise ImportError(f'PySide6 blocked (PyQt6): {name}')
    return _original_import(name, *args, **kwargs)

_builtins.__import__ = _blocked_import

from lightweight_charts.toolbox import ToolBox, json
from lightweight_charts.drawings import HorizontalLine
from lightweight_charts.abstract import Line, Candlestick, AbstractChart
from lightweight_charts import util

# 恢复正常导入
_builtins.__import__ = _original_import
# 延迟导入QtChart类
ChartClass = None
QtChart = None


def get_chart_class():
    """
    延迟导入Chart类
    """
    global ChartClass, QtChart
    if ChartClass is None:
        # 确保QApplication实例存在
        app = QApplication.instance()
        if app is None:
            # 这些设置必须在创建QApplication实例之前调用
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
            app = QApplication([])

        # 先导入PyQt6的QtWebEngineWidgets模块，确保使用的是PyQt6
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtCore import Qt, QObject, pyqtSlot, QUrl, QTimer

        # 定义一个使用PyQt6 QObject的Bridge类
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

        # 手动设置lightweight_charts.widgets模块的using_pyside6变量为False（使用PyQt6）
        import lightweight_charts
        lightweight_charts.using_pyside6 = False

        # 导入lightweight_charts.widgets模块
        import lightweight_charts.widgets

        # 直接使用我们导入的PyQt6模块，而不是让lightweight_charts.widgets模块自己导入
        lightweight_charts.widgets.QWebEngineView = QWebEngineView
        lightweight_charts.widgets.QWebChannel = QWebChannel
        lightweight_charts.widgets.QObject = QObject
        lightweight_charts.widgets.Slot = pyqtSlot
        lightweight_charts.widgets.QUrl = QUrl
        lightweight_charts.widgets.QTimer = QTimer
        lightweight_charts.widgets.Qt = Qt
        lightweight_charts.widgets.Bridge = Bridge

        # 获取Chart类
        ChartClass = lightweight_charts.widgets.QtChart
        QtChart = ChartClass
    return ChartClass


if TYPE_CHECKING:
    from strategy import Strategy
Colors: list[str] = ['fuchsia', 'lime', 'olive', 'blue', 'purple', 'silver', 'teal', 'aqua',
                     'green', 'maroon', 'navy', 'red']

SETTING_FILE_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "setting.json")


def get_default_settings() -> dict:
    """返回默认配置，用于文件不存在/数据损坏时兜底"""
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


def get_colors() -> cycle:
    """指标初始颜色"""
    return cycle(Colors)


class FixedSizeQueue:
    def __init__(self, max_size, value=False, values: Iterable = None):
        self.queue = deque(maxlen=max_size)
        if not (values and isinstance(values, Iterable)):
            values = [False,]*max_size
        self.add_items(list(values))
        if isinstance(value, bool):
            self.add(value)

    def add(self, item) -> FixedSizeQueue:
        """添加单个元素"""
        self.queue.append(item)
        return self

    def add_items(self, items: Iterable) -> FixedSizeQueue:
        """批量添加元素"""
        self.queue.extend(items)
        return self

    def values(self) -> list:
        """获取队列中所有元素（列表形式）"""
        return list(self.queue)

    def clear(self) -> FixedSizeQueue:
        """清空队列"""
        self.queue.clear()
        return self

    @property
    def any(self) -> bool:
        return any(self.queue)


class PriceAlertSettingDialog(Dialog):
    """
    简洁版价格预警设置对话框（基于自定义 Dialog 基类）
    核心功能：启用开关、预警类型、上下破价格设置
    """
    alertSettingsChanged = pyqtSignal(dict)

    def __init__(self, title="", content="", parent: LightChartWindow = None, current_settings: dict = None):
        """
        初始化对话框（调用基类构造方法，后续替换默认布局内容）
        :param parent: 父窗口
        :param current_settings: 现有预警设置（可选）
        """
        super().__init__(title=title, content=content, parent=parent)
        self.linght_chart_window = parent
        self.current_settings = current_settings
        self.setFixedSize(420, 360)
        self.setResizeEnabled(False)
        self._clean_base_widgets()
        self._init_alert_ui()
        self._init_signals()
        self._load_current_settings()

    def _clean_base_widgets(self):
        """
        清理基类默认控件（不修改基类源码，仅在子类中移除无用控件）
        避免默认标题/内容标签与预警设置控件冲突
        """
        # 移除基类默认的 titleLabel 和 contentLabel
        if hasattr(self, 'titleLabel') and self.titleLabel:
            self.textLayout.removeWidget(self.titleLabel)
            self.titleLabel.hide()
            self.titleLabel.deleteLater()

        if hasattr(self, 'contentLabel') and self.contentLabel:
            self.textLayout.removeWidget(self.contentLabel)
            self.contentLabel.hide()
            self.contentLabel.deleteLater()

        # 移除基类默认的 yesButton 和 cancelButton（替换为自定义按钮）
        if hasattr(self, 'yesButton') and self.yesButton:
            self.buttonLayout.removeWidget(self.yesButton)
            self.yesButton.hide()
            self.yesButton.deleteLater()

        if hasattr(self, 'cancelButton') and self.cancelButton:
            self.buttonLayout.removeWidget(self.cancelButton)
            self.cancelButton.hide()
            self.cancelButton.deleteLater()

    def _init_alert_ui(self):
        """
        初始化预警设置UI（复用基类的 textLayout 和 vBoxLayout，无重复布局）
        """
        # 1. 核心设置卡片（承载所有预警功能，添加到基类的 textLayout 中）
        core_card = CardWidget(self)
        card_layout = QVBoxLayout(core_card)
        card_layout.setSpacing(18)
        card_layout.setContentsMargins(20, 20, 20, 20)

        # 1.1 预警启用开关
        self.enable_switch = SwitchButton("启用价格预警", core_card)
        card_layout.addWidget(self.enable_switch)

        # 1.2 预警类型选择
        type_layout = QHBoxLayout()
        type_label = BodyLabel("预警类型：", core_card)
        self.alert_combo = ComboBox(core_card)
        self.alert_combo.addItems(["双向预警", "上破预警", "下破预警"])
        type_layout.addWidget(type_label)
        type_layout.addStretch()
        type_layout.addWidget(self.alert_combo)
        card_layout.addLayout(type_layout)

        # 1.3 上破价格设置
        up_layout = QHBoxLayout()
        up_label = BodyLabel("上破价格：", core_card)
        self.up_spin = DoubleSpinBox(core_card)
        self.up_spin.setRange(0, 1000000)
        self.up_spin.setDecimals(4)
        self.up_spin.setFixedWidth(180)
        up_layout.addWidget(up_label)
        up_layout.addStretch()
        up_layout.addWidget(self.up_spin)
        card_layout.addLayout(up_layout)

        # 1.4 下破价格设置
        down_layout = QHBoxLayout()
        down_label = BodyLabel("下破价格：", core_card)
        self.down_spin = DoubleSpinBox(core_card)
        self.down_spin.setRange(0, 1000000)
        self.down_spin.setDecimals(4)
        self.down_spin.setFixedWidth(180)
        down_layout.addWidget(down_label)
        down_layout.addStretch()
        down_layout.addWidget(self.down_spin)
        card_layout.addLayout(down_layout)

        # 2. 复用基类的 textLayout，添加核心卡片（无新布局，避免冲突）
        self.textLayout.addWidget(core_card, 0, Qt.AlignmentFlag.AlignTop)
        self.textLayout.setSpacing(20)
        self.textLayout.setContentsMargins(24, 24, 24, 24)

        # 3. 复用基类的 buttonGroup 和 buttonLayout，添加自定义按钮
        self.cancel_btn = PushButton("Cancel", self.buttonGroup)
        self.confirm_btn = PrimaryPushButton("OK", self.buttonGroup)
        self.confirm_btn.setIcon(FluentIcon.ACCEPT.icon())

        # 重新布局自定义按钮
        self.buttonLayout.setSpacing(12)
        self.buttonLayout.setContentsMargins(24, 24, 24, 24)
        self.buttonLayout.addStretch()
        self.buttonLayout.addWidget(self.cancel_btn)
        self.buttonLayout.addWidget(self.confirm_btn)

        # 4. 初始状态更新（禁用输入框）
        self._update_widget_status(self.enable_switch.isChecked())

    def _init_signals(self):
        """绑定信号槽（仅关联预警功能所需信号，不改动基类逻辑）"""
        # 启用开关状态变更
        self.enable_switch.checkedChanged.connect(
            self._update_widget_status)
        # 预警类型变更
        self.alert_combo.currentIndexChanged.connect(
            self._on_alert_type_changed)
        # 自定义按钮点击事件
        self.cancel_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self._on_confirm_clicked)

    def _load_current_settings(self):
        """加载现有设置（填充到控件中）"""
        # 启用状态
        self.enable_switch.setChecked(
            self.current_settings.get('enabled', False))
        # 预警类型
        type_map = {'both': 0, 'up': 1, 'down': 2}
        current_type = self.current_settings.get('alert_type', 'both')
        self.alert_combo.setCurrentIndex(type_map.get(current_type, 0))
        # 价格设置
        self.up_spin.setValue(self.current_settings.get('up_price', 0.0))
        self.down_spin.setValue(
            self.current_settings.get('down_price', 0.0))

    def _update_widget_status(self, is_enabled: bool):
        """更新控件启用状态（跟随总开关）"""
        self.alert_combo.setEnabled(is_enabled)
        self.up_spin.setEnabled(is_enabled)
        self.down_spin.setEnabled(is_enabled)

        # 同步更新预警类型对应的输入框状态
        if is_enabled:
            self._on_alert_type_changed(self.alert_combo.currentIndex())

    def _on_alert_type_changed(self, index: int):
        """预警类型变更处理（控制输入框可用性）"""
        if not self.enable_switch.isChecked():
            return

        # 0: 双向预警, 1: 上破预警, 2: 下破预警
        if index == 1:  # 上破预警
            self.up_spin.setEnabled(True)
            self.down_spin.setEnabled(False)
            self.down_spin.setValue(0.0)
        elif index == 2:  # 下破预警
            self.up_spin.setEnabled(False)
            self.up_spin.setValue(0.0)
            self.down_spin.setEnabled(True)
        else:  # 双向预警
            self.up_spin.setEnabled(True)
            self.down_spin.setEnabled(True)

    def _validate_input(self) -> bool:
        """验证输入合法性（简洁校验，只保留核心规则）"""
        if not self.enable_switch.isChecked():
            return True

        up_price = self.up_spin.value()
        down_price = self.down_spin.value()
        alert_index = self.alert_combo.currentIndex()

        # 上破预警校验
        if alert_index == 1 and up_price <= 0:
            self._show_info_bar("警告", "上破价格必须大于0！", "warning")
            return False

        # 下破预警校验
        if alert_index == 2 and down_price <= 0:
            self._show_info_bar("警告", "下破价格必须大于0！", "warning")
            return False

        # 双向预警校验
        if alert_index == 0 and up_price > 0 and down_price > 0 and up_price <= down_price:
            self._show_info_bar("警告", "上破价格必须大于下破价格！", "warning")
            return False

        return True

    def _get_current_settings(self) -> dict:
        """获取当前控件中的设置（组装为字典）"""
        type_map = {0: 'both', 1: 'up', 2: 'down'}
        return {
            'enabled': self.enable_switch.isChecked(),
            'alert_type': type_map.get(self.alert_combo.currentIndex(), 'both'),
            'up_price': self.up_spin.value(),
            'down_price': self.down_spin.value()
        }

    def _on_confirm_clicked(self):
        """确认按钮点击处理（校验→发射信号→关闭对话框）"""
        if not self._validate_input():
            return

        # 获取当前设置并发射信号
        current_settings = self._get_current_settings()
        self.alertSettingsChanged.emit(current_settings)

        # 显示成功提示
        self._show_info_bar("成功", "价格预警设置已保存！", "success")

        # 关闭对话框
        self.accept()

    def _show_info_bar(self, title: str, content: str, info_type: str):
        """
        正确使用自定义 InfoBar 类显示提示框
        :param title: 提示标题
        :param content: 提示内容
        :param info_type: 提示类型（success/warning/info/error）
        """
        # 配置默认参数（与自定义 InfoBar 类匹配）
        duration = 2000
        position = InfoBarPosition.TOP_RIGHT
        orient = Qt.Orientation.Horizontal
        is_closable = True

        # 根据提示类型调用对应的 InfoBar 类方法
        if info_type == "success":
            InfoBar.success(
                title=title,
                content=content,
                orient=orient,
                isClosable=is_closable,
                duration=duration,
                position=position,
                parent=self
            )
        elif info_type == "warning":
            InfoBar.warning(
                title=title,
                content=content,
                orient=orient,
                isClosable=is_closable,
                duration=duration,
                position=position,
                parent=self
            )
        elif info_type == "error":
            InfoBar.error(
                title=title,
                content=content,
                orient=orient,
                isClosable=is_closable,
                duration=duration,
                position=position,
                parent=self
            )
        else:  # 默认 info 类型
            InfoBar.info(
                title=title,
                content=content,
                orient=orient,
                isClosable=is_closable,
                duration=duration,
                position=position,
                parent=self
            )


class CustomTitleBar(FluentTitleBar):
    """自定义标题栏"""

    def __init__(self, parent):
        super().__init__(parent)
        FluentStyleSheet.FLUENT_WINDOW.apply(self, Theme.DARK)
        self.iconLabel.setFixedSize(36, 36)

        self.avatar = TransparentToolButton(
            FluentIcon.CONSTRACT, self)
        self.avatar.setIconSize(QSize(18, 18))
        self.avatar.setFixedHeight(24)
        self.hBoxLayout.insertWidget(2, self.avatar, 0, Qt.AlignmentFlag.AlignRight)
        self.avatar.clicked.connect(parent.setTheme)

    def setIcon(self, icon):
        self.iconLabel.setPixmap(QIcon(icon).pixmap(36, 36))


class MainWindow(FluentWindow):
    """主窗口"""

    def __init__(self, strategies: list[Strategy], period_milliseconds: int = 1000):
        super().__init__()
        self.title_height = 36
        self.theme = Theme.LIGHT
        self.base_dir = strategies[0]._base_dir
        self.initNavigation()
        self.initWindow()
        # 先设置主题，然后再创建LightChartWindow
        self.light_chart_window = LightChartWindow(
            self, strategies, period_milliseconds)
        # 将LightChartWindow添加到主窗口
        self.addSubInterface(self.light_chart_window,
                             FluentIcon.HOME, 'Home')

    def initNavigation(self):
        self.setTitleBar(CustomTitleBar(self))
        self.setMicaEffectEnabled(False)
        self.navigationInterface.setAcrylicEnabled(False)
        self.navigationInterface.hide()

    def initWindow(self):
        self.resize(900, 700)
        self.widgetLayout.setContentsMargins(0, self.title_height, 0, 0)
        self.setTheme()
        screen = QGuiApplication.primaryScreen()
        desktop = screen.availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w//2 - self.width()//2, h//2 - self.height()//2)

    def setTheme(self):
        self.theme = Theme.LIGHT if self.isdark else Theme.DARK
        setTheme(self.theme)
        if hasattr(self, 'light_chart_window') and hasattr(self.light_chart_window, 'chart_window'):
            self.light_chart_window.chart_window.setTheme()
        self._seticon()

    @property
    def isdark(self) -> bool:
        return self.theme == Theme.DARK

    def _seticon(self):
        if self.isdark:
            path = os.path.join(self.base_dir, "data", "minibt_dark.png")
        else:
            path = os.path.join(self.base_dir, "data", "minibt_light.png")

        if os.path.exists(path):
            icon = QIcon(path)
            self.titleBar.setIcon(icon)

    def closeEvent(self, e):
        try:
            if hasattr(self.light_chart_window, 'save_settings'):
                self.light_chart_window.save_settings()
            if hasattr(self.light_chart_window, 'multi_timer'):
                self.light_chart_window.multi_timer.stop()
            if hasattr(self.light_chart_window, 'chart_window') and self.light_chart_window.chart_window and hasattr(self.light_chart_window.chart_window, 'cleanup'):
                self.light_chart_window.chart_window.cleanup()
            if hasattr(self.light_chart_window, 'close_api'):
                self.light_chart_window.close_api()
        except Exception as e:
            print(f"closeEvent执行失败: {e}")
        return super().closeEvent(e)

    def resizeEvent(self, event):
        try:
            """重写窗口大小改变事件，添加防抖处理"""
            super().resizeEvent(event)
            if hasattr(self, 'light_chart_window') and hasattr(self.light_chart_window, 'splitter') and hasattr(self.light_chart_window, 'current_sizes'):
                self.light_chart_window.splitter.setSizes(
                    self.light_chart_window.current_sizes)
        except Exception as e:
            print(f"resizeEvent执行失败: {e}")


class InfoWindow(QWidget):
    """账户信息窗口"""

    def __init__(self, parent: MainWindow = None):
        super().__init__(parent=parent)
        self.hBoxLayout = QHBoxLayout(self)
        self.setFixedHeight(40)
        self.text_label = CaptionLabel("test")
        label_font = QFont()
        label_font.setFamily('Microsoft YaHei')
        label_font.setPointSize(12)
        self.text_label.setFont(label_font)
        self.hBoxLayout.addWidget(self.text_label)
        self.hBoxLayout.addStretch(1)
        self.hBoxLayout.setContentsMargins(10, 0, 10, 0)
        self.hBoxLayout.setSpacing(10)

    def set_account_info(self, text: str = ""):
        self.text_label.setText(text)


class ContractTable(QWidget):
    """合约表格 — 支持按策略sid过滤，内嵌策略选择器"""

    def __init__(self, parent: LightChartWindow = None, strategy_sid: int = None):
        super().__init__(parent)
        self.light_chart_window = parent
        self._all_contracts = parent.contract_dict  # 全量合约
        self._current_sid = strategy_sid
        self.setObjectName("ContractTable")

        # ---- 主布局 ----
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ---- 策略选择器（多策略模式下显示在表格上方） ----
        self.strategy_combo = None
        if len(parent.strategies) > 1:
            selector = QWidget(self)
            sel_layout = QHBoxLayout(selector)
            sel_layout.setContentsMargins(6, 4, 6, 4)
            sel_layout.setSpacing(6)
            sel_label = CaptionLabel("活跃策略:", selector)
            self.strategy_combo = ComboBox(selector)
            strategy_names = [s.__class__.__name__ for s in parent.strategies]
            self.strategy_combo.addItems(strategy_names)
            self.strategy_combo.setCurrentIndex(strategy_sid or 0)
            self.strategy_combo.currentIndexChanged.connect(parent._on_strategy_changed)
            sel_layout.addWidget(sel_label)
            sel_layout.addWidget(self.strategy_combo)
            sel_layout.addStretch()
            layout.addWidget(selector)

        # ---- 内部表格 ----
        self._table = TableWidget(self)
        self._table.setObjectName("ContractInnerTable")
        font = self._table.horizontalHeader().font()
        font.setBold(True)
        self._table.horizontalHeader().setFont(font)
        self._table.adjustSize()
        self._table.verticalHeader().setVisible(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setSortingEnabled(True)
        self._table.setBorderVisible(True)

        multi = len(self.light_chart_window.strategies) > 1
        self._table.setColumnCount(2 if multi else 1)
        self._table.setHorizontalHeaderLabels(
            ['策略', '合约'] if multi else ['合约'])

        self._table.cellDoubleClicked.connect(self.on_click_item)
        layout.addWidget(self._table, stretch=1)

        # 初始填充数据
        self._refresh_table()

    def set_strategy_filter(self, sid: int):
        """按策略sid重新填充合约表格"""
        self._current_sid = sid
        self._refresh_table()

    def _refresh_table(self):
        """按当前过滤条件重新加载合约列表"""
        sid = self._current_sid
        all_contracts = self._all_contracts
        if sid is not None:
            contracts = {k: v for k, v in all_contracts.items() if v == sid}
        else:
            contracts = all_contracts

        self.contracts = contracts
        num = len(contracts)
        self._table.setRowCount(num)
        multi = self._table.columnCount() == 2

        sid_to_name = {s._sid: s.__class__.__name__ for s in self.light_chart_window.strategies}

        for i, contract in enumerate(contracts.keys()):
            item = QTableWidgetItem(contract)
            self._table.setItem(i, self._table.columnCount() - 1, item)
            if multi:
                sname = sid_to_name.get(contracts[contract], str(contracts[contract]))
                self._table.setItem(i, 0, QTableWidgetItem(sname))
            if not i:
                self.light_chart_window.current_contract = contract
                self._table.setCurrentItem(item)
        self.current_row = 0

    def on_click_item(self, row, column):
        """双击事件"""
        if row == self.current_row:
            return
        self.current_row = row
        # 合约名称在最后一列
        col_idx = self._table.columnCount() - 1
        contract = self._table.item(row, col_idx).text()
        self.light_chart_window.current_contract = contract
        sid = self.contracts[contract]
        strategy = self.light_chart_window.strategies[sid]
        self.light_chart_window._active_strategy_sid = sid
        # 同步更新策略下拉框（现在在 ContractTable 内部）
        if self.strategy_combo:
            self.strategy_combo.blockSignals(True)
            self.strategy_combo.setCurrentIndex(sid)
            self.strategy_combo.blockSignals(False)
        self.light_chart_window.replace_chart(strategy)


class SeparatorWidget(QWidget):
    """分隔线组件"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        # 设置分隔线高度
        self.setFixedHeight(1)
        # 根据主题设置分隔线颜色
        self._color = QColor(0, 0, 0, 20)

    def paintEvent(self, e):
        """绘制分隔线"""
        painter = QPainter(self)
        painter.setPen(QPen(self._color, 1))
        # 绘制一条水平直线
        painter.drawLine(0, 0, self.width(), 0)


class LightChartWindow(QWidget):
    """
    整体图表窗口类，负责管理策略图表的显示和交互

    主要功能：
    - 管理多个策略的图表显示
    - 处理合约切换
    - 管理价格预警设置
    - 保存和加载用户设置
    - 处理键盘快捷键
    """
    current_contract: str

    def __init__(self, parent: MainWindow = None, strategies: list[Strategy] = None, period_milliseconds: int = 1000):
        """
        初始化LightChartWindow

        Args:
            parent: 父窗口
            strategies: 策略列表
            period_milliseconds: 更新周期（毫秒）
        """
        super().__init__(parent=parent)
        self.period_milliseconds = period_milliseconds
        self.main_window = parent
        self.strategies = strategies
        self.price_scale_widthes: dict[str, int] = {}
        self.visible_range = {}
        self.setObjectName("LightChartWindow")
        self.vBoxLayout = QVBoxLayout(self)
        self.load_settings()
        # 安装事件过滤器，用于处理键盘快捷键
        self.installEventFilter(self)
        self.__initWidget()

    def __initWidget(self):
        """初始化组件和布局"""
        # 布局基础设置
        self.vBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.vBoxLayout.setSpacing(0)

        # 1. 添加顶部信息栏
        self.info_window = InfoWindow(self)
        self.vBoxLayout.addWidget(self.info_window)

        # 2. 添加水平分割窗口（左侧：合约面板(含策略选择器+表格)，右侧：图表）
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        # ========== 设置QSplitter样式表，和SeparatorWidget视觉一致 ==========
        self.splitter.setStyleSheet("""
            QSplitter {
                background-color: transparent;  /* 透明背景，不影响整体布局 */
            }
            QSplitter::handle:horizontal {
                /* 水平分割线样式（和SeparatorWidget一致） */
                background-color: rgba(0, 0, 0, 20);  /* 颜色和SeparatorWidget完全一致 */
                width: 1px;  /* 宽度和SeparatorWidget高度一致 */
            }
            /* 鼠标悬停时的样式（可选，增加拖动辨识度） */
            QSplitter::handle:horizontal:hover {
                background-color: rgba(0, 0, 0, 150);  /* 悬停时略深，提示可拖动 */
                width: 2px;  /* 悬停时宽度稍大，更容易点击 */
            }
        """)

        # 构建合约字典：key = "symbol_cycle", value = strategy_sid
        self.contract_dict = {}
        for strategy in self.strategies:
            for k, v in strategy._btklinedataset.items():
                self.contract_dict[f"{v.symbol}_{v.cycle}"] = strategy._sid
                if v.symbol not in self.price_alert:
                    self.price_alert[v.symbol] = {}

        # 初始化 current_contract（用于支持多K线策略）
        if self.contract_dict:
            self.current_contract = next(iter(self.contract_dict))
        elif self.strategies:
            first_strategy = self.strategies[0]
            for k, v in first_strategy._btklinedataset.items():
                self.current_contract = f"{v.symbol}_{v.cycle}"
                break

        # 3.1 策略表格 — 初始显示第一个策略的合约列表
        self.strategy_table = ContractTable(self, strategy_sid=0)
        self.strategy_table.setMaximumWidth(300)
        self.strategy_table.setMinimumWidth(150)
        self.chart_container = QWidget(self)
        self.chart_container.setLayout(QVBoxLayout())
        self.chart_container.layout().setContentsMargins(0, 0, 0, 0)
        self.chart_container.layout().setSpacing(0)
        # 3.2 图表窗口 — 多策略模式：不创建 QuoteQTimer（由 MultiStrategyTimer 统一管理）
        self._active_strategy_sid = 0
        self.chart_window = self._create_chart(self.strategies[0], use_qtimer=False)
        self.chart_container.layout().addWidget(self.chart_window.get_webview())
        self.splitter.addWidget(self.strategy_table)
        self.splitter.addWidget(self.chart_container)
        self.splitter.setSizes(self.current_sizes)
        # 添加到主布局，并设置拉伸因子让分割窗口填充剩余空间
        self.vBoxLayout.addWidget(self.splitter, stretch=1)

        # 创建多策略统一调度器（替代每个 Chart 各自的 QuoteQTimer）
        self.multi_timer = MultiStrategyTimer(self)

    def _create_chart(self, strategy: Strategy, use_qtimer: bool = True) -> Chart:
        """创建新图表实例（工厂方法，统一封装）"""
        return Chart(
            self,
            strategy=strategy,
            period_milliseconds=self.period_milliseconds,
            use_qtimer=use_qtimer,
        )

    def _on_strategy_changed(self, index: int):
        """策略选择器变更：切换到对应策略的合约列表和图表"""
        if index < 0 or index >= len(self.strategies):
            return
        strategy = self.strategies[index]
        self._active_strategy_sid = strategy._sid
        # 1. 刷新合约表格为该策略的合约
        self.strategy_table.set_strategy_filter(strategy._sid)
        # 2. 切换到对应图表
        self.replace_chart(strategy)

    def _do_chart_replacement(self, strategy: Strategy):
        """执行图表替换：加载新图表到固定容器"""
        self.chart_window = self._create_chart(strategy)
        new_webview = self.chart_window.get_webview()
        if new_webview:
            self.chart_container.layout().addWidget(new_webview)
        self.current_strategy = strategy
        QApplication.processEvents()

    def replace_chart(self, strategy: Strategy):
        """
        外部调用的策略切换入口（线程安全、校验前置）
        :param strategy: 目标策略对象，为None则不执行切换
        """
        QTimer.singleShot(
            50, lambda: self._start_chart_replacement(strategy))

    def _start_chart_replacement(self, strategy: Strategy):
        """切换流程主控：清理旧资源 -> 创建新图表"""
        # 安全清理旧组件
        self._safe_clear_container()
        # 加载新图表
        self._do_chart_replacement(strategy)

    def _safe_clear_container(self):
        """安全清空图表容器：释放旧组件、清理布局、回收内存"""
        # 1. 清理旧Chart对象资源
        if self.chart_window:
            try:
                # 调用业务层清理方法（定时器、网络、API连接）
                self.chart_window.cleanup()
            except Exception as e:
                print(f"清理旧图表资源失败: {str(e)}")

            # 2. 从布局中移除WebView组件
            old_webview = self.chart_window.get_webview()
            if old_webview and old_webview.parent() == self.chart_container:
                self.chart_container.layout().removeWidget(old_webview)
                old_webview.setParent(None)
                old_webview.deleteLater()

            # 3. 释放图表对象引用
            self.chart_window = None

        # 4. 清空布局所有组件（兜底处理）
        layout = self.chart_container.layout()
        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def close_api(self):
        """关闭天勤API"""
        self.strategies[0].api.close()

    def save_settings(self):
        """
        保存所有配置到 setting.json
        窗口关闭事件中调用此方法
        """
        if not os.path.exists(SETTING_FILE_PATH):
            self.save_default_settings()
            return

        with open(SETTING_FILE_PATH, "r", encoding="utf-8") as f:
            settings: dict = json.load(f)

        current_drawings = self.drawings
        merged_drawings = {
            **settings.get("drawings", {}), **current_drawings}
        settings["drawings"] = {
            k: v for k, v in merged_drawings.items() if v and isinstance(v, (list, dict))}
        current_price_alerts = self.price_alert
        merged_price_alerts = {
            **settings.get("price_alerts", {}), **current_price_alerts}
        settings["price_alerts"] = {k: v for k,
                                    v in merged_price_alerts.items() if v}
        settings["istool"] = self.chart_window.toolbox is not None if self.chart_window else False
        settings["mouse_label_color"] = self.mouse_label_color
        settings["candlestick_colors"]["bear_color"] = self.bear_color
        settings["candlestick_colors"]["bull_color"] = self.bull_color
        settings["splitter_sizes"] = self.splitter.sizes()

        with open(SETTING_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)

    def load_settings(self, default: bool = False):
        """从 setting.json 加载配置，还原到窗口对象"""
        if (not os.path.exists(SETTING_FILE_PATH)) or default:
            settings = get_default_settings()
        else:
            with open(SETTING_FILE_PATH, "r", encoding="utf-8") as f:
                settings: dict = json.load(f)
        self.drawings: dict = settings.get("drawings", {})
        self.drawings = {k: v for k, v in self.drawings.items() if v}
        self.price_alert: dict = settings.get("price_alerts", {})
        self.price_alert = {k: v for k, v in self.price_alert.items() if v}
        self.mouse_label_color: str = settings.get(
            "mouse_label_color", 'rgba(30, 30, 30, 0.9)')
        self.istool: bool = bool(settings.get("istool", False))
        candle_colors: dict = settings.get("candlestick_colors", {})
        self.bear_color: str = candle_colors.get(
            "bear_color", btcolors.bear_color)
        self.bull_color: str = candle_colors.get(
            "bull_color", btcolors.bull_color)
        self.current_sizes = settings.get("splitter_sizes", [220, 1800])

    def save_default_settings(self):
        with open(SETTING_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(get_default_settings(), f,
                      ensure_ascii=False, indent=4)

    def eventFilter(self, obj, event):
        """事件过滤器，处理键盘快捷键"""
        # 只处理键盘按下事件
        if event.type() == QEvent.Type.KeyPress:
            # 获取当前激活的webview
            webview = self.chart_window.get_webview()

            # 确保事件来自webview或者窗口本身
            if obj == webview or obj == self:
                key = event.key()
                # modifiers = event.modifiers()

                # ========== 完善键盘事件映射 ==========
                # 放大：上箭头 或 + 键
                if key == Qt.Key.Key_Up or key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
                    self.chart_window.zoom_in()
                    return True  # 拦截事件，避免传递给其他组件
                # 缩小：下箭头 或 - 键
                elif key == Qt.Key.Key_Down or key == Qt.Key.Key_Minus:
                    self.chart_window.zoom_out()
                    return True
                # 向左平移：左箭头
                elif key == Qt.Key.Key_Left:
                    self.chart_window.pan_left()
                    return True
                # 向右平移：右箭头
                elif key == Qt.Key.Key_Right:
                    self.chart_window.pan_right()
                    return True
                elif key == Qt.Key.Key_Space:
                    self.chart_window.center_on_latest()
                    return True

        # 其他事件交给父类处理
        return super().eventFilter(obj, event)


class CustomToolBox(ToolBox):
    def __init__(self, chart: Chart):
        self.run_script = chart.run_script
        self.id = chart.id
        self.drawings = {}
        self.chart = chart
        self.window = chart.light_chart_window
        self._save_under = self.window
        # 绑定前端保存画线的回调
        chart.win.handlers[f'save_drawings{self.id}'] = self._save_drawings
        # 创建前端工具箱
        self.run_script(f'{self.id}.createToolBox()')

    def load_drawings(self, tag: str):
        """加载历史画线数据"""
        target_drawings = self.window.drawings.get(tag)
        if not target_drawings:
            target_drawings = self.drawings.get(tag)
        if not target_drawings:
            return
        self.run_script(
            f'if ({self.id}.toolBox) {self.id}.toolBox.loadDrawings({json.dumps(target_drawings)})'
        )

    def _save_drawings(self, drawings: str):
        """前端回调：持久化画线数据"""
        if not self._save_under:
            return
        parsed_drawings = json.loads(drawings)
        tag = self._save_under.current_contract
        self.drawings[tag] = parsed_drawings
        self.window.drawings[tag] = parsed_drawings

    def clear_drawings(self):
        """清空当前合约所有画线，保留工具实例"""
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
        # 清理Python端缓存
        tag = self._save_under.current_contract
        self.drawings.pop(tag, None)
        self.window.drawings.pop(tag, None)

    def hide_toolbox(self):
        """
        核心方法：强制隐藏工具箱，屏蔽交互，兼容所有渲染场景
        替代DOM查找/移除，解决元素定位失败问题
        """
        js_script = f"""
        try {{
            // 1. 清空所有画线图形
            if ({self.id}.toolBox) {{
                {self.id}.toolBox.clearDrawings();
            }}
            // 2. 全局样式覆盖：隐藏所有lightweight-charts工具箱控件（通用选择器）
            const style = document.createElement('style');
            style.id = '{self.id}-toolbox-hide-style';
            style.textContent = `
                /* 隐藏工具箱容器、按钮、绘图面板 */
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
            // 移除旧样式，插入新样式
            const oldStyle = document.getElementById('{self.id}-toolbox-hide-style');
            if (oldStyle) oldStyle.remove();
            document.head.appendChild(style);
            
            // 3. 置空JS引用，禁止后续调用
            {self.id}.toolBox = null;
            console.log('画线工具已隐藏并禁用');
        }} catch (e) {{
            console.error('隐藏工具箱失败：', e);
        }}
        """
        self.run_script(js_script)

    def show_toolbox(self):
        """重新显示工具箱，清理隐藏样式（用于重新初始化）"""
        js_script = f"""
        try {{
            // 移除隐藏样式
            const style = document.getElementById('{self.id}-toolbox-hide-style');
            if (style) style.remove();
            console.log('工具箱样式已恢复');
        }} catch (e) {{
            console.error('恢复工具箱样式失败：', e);
        }}
        """
        self.run_script(js_script)

    def cleanup(self):
        """完整资源清理：隐藏工具+清理数据+解绑事件"""
        self.hide_toolbox()
        # 清理Python端事件回调，防止内存泄漏
        handler_key = f'save_drawings{self.id}'
        if hasattr(self.chart.win, 'handlers') and handler_key in self.chart.win.handlers:
            del self.chart.win.handlers[handler_key]
        # 清空所有画线数据
        self.drawings.clear()
        self.window.drawings.clear()


class QuoteQTimer(QObject):
    """支持开盘/收盘状态自动切换的定时器管理类"""
    names: list[str] = ["monitor", "kline", "indicator", "account"]

    def __init__(self, chart: Chart):
        super().__init__()
        self.chart = chart
        self.qtimers: dict[str, QTimer] = OrderedDict()
        self.msecs: dict[str, int] = {}
        self.current_status = chart._is_update
        self.initqtimers()

    def initqtimers(self):
        """初始化定时器配置和实例"""
        self.msecs = dict(zip(self.names, [
            1000, 100, self.chart.period_milliseconds, self.chart.period_milliseconds
        ]))
        self.create_timers()

        # 初始化状态检测定时器（持续检测行情状态）
        self.status_check_timer = QTimer(self.chart.light_chart_window)
        self.status_check_timer.timeout.connect(self.start)
        self._switch_qtimer(self.current_status)
        self.status_check_timer.start(1000)

    def _create_timer(self, name: str):
        """创建单个定时器并绑定回调"""
        timer = QTimer(self.chart.light_chart_window)
        timer.timeout.connect(getattr(self.chart, f"{name}_loop"))
        self.qtimers[name] = timer

    def create_timers(self):
        """批量创建所有定时器"""
        for name in self.names:
            self._create_timer(name)

    def start(self):
        """启动"""
        if QThread.currentThread() != QApplication.instance().thread():
            QMetaObject.invokeMethod(self, "start", Qt.QueuedConnection)
            return
        status = self.chart.update_queue.any
        if status != self.current_status:
            self._switch_qtimer(status)

    def _switch_qtimer(self, status):
        if status:
            self.chart.update_queue.clear()
            self.start_quote()
        else:
            self.start_monitor()

        self.current_status = status

    def start_quote(self):
        """开盘模式：启动行情定时器，停止监视器"""
        # 停止monitor定时器
        self._stop_all_timers()
        # 启动kline/indicator/account定时器
        for name in ["kline", "indicator", "account"]:
            self._start_timer(self.qtimers[name], self.msecs[name])

    def start_monitor(self):
        """收盘模式：停止行情定时器，启动监视器"""
        # 停止kline/indicator/account定时器
        self._stop_all_timers()
        # 启动monitor定时器
        self._start_timer(self.qtimers["monitor"], self.msecs["monitor"])

    def _stop_all_timers(self):
        """批量停止定时器（增加排除项）"""
        if self.qtimers:
            for timer in self.qtimers.values():
                self._stop_timer(timer)

    def _start_timer(self, timer: QTimer, msec: int):
        """安全启动定时器（避免重复启动）"""
        if not timer.isActive():
            timer.start(msec)

    def _stop_timer(self, timer: QTimer):
        """安全停止定时器"""
        if timer.isActive():
            timer.stop()

    def stop(self):
        """资源清理"""
        # 3. 停止定时器运行
        if self.status_check_timer and self.status_check_timer.isActive():
            self.status_check_timer.stop()

        for timer in self.qtimers.values():
            if timer and timer.isActive():
                timer.stop()
        # 1. 先断开所有信号
        if self.status_check_timer:
            try:
                self.status_check_timer.timeout.disconnect(self.start)
            except:
                pass

        # 2. 停止所有定时器
        for name, timer in self.qtimers.items():
            if timer:
                try:
                    timer.timeout.disconnect(
                        getattr(self.chart, f"{name}_loop"))
                except:
                    pass

        QTimer.singleShot(100, lambda: self._final_cleanup())

    def _final_cleanup(self):
        """最终清理"""
        if self.status_check_timer:
            self.status_check_timer.deleteLater()
            self.status_check_timer = None

        for timer in self.qtimers.values():
            if timer:
                timer.deleteLater()
        self.qtimers.clear()
        self.msecs.clear()
        self.deleteLater()
        self.chart = None


class MultiStrategyTimer(QObject):
    """多策略统一定时器 — 一个 QTimer 驱动所有策略的交易逻辑和活跃图表渲染

    设计原则：
      - 交易定时器始终运行（period_ms 间隔）
      - 每次 tick 先 wait_update(1)，根据返回值判断开盘/收盘
      - 只在行情有效时执行策略逻辑和图表渲染
      - 一个策略卡住不影响其他策略（try/except 包裹每个策略）
    """

    def __init__(self, window: LightChartWindow):
        """
        初始化多策略定时器

        Args:
            window: LightChartWindow 实例（持有所有策略和图表）
        """
        super().__init__(window)
        self.window = window
        self.strategies = window.strategies
        self.period_ms = max(window.period_milliseconds, 100)
        self._is_market_open = False  # 行情状态
        self._first_start = True

        # 主循环定时器（始终运行，内部判断开盘/收盘）
        self.trading_timer = QTimer(window)
        self.trading_timer.timeout.connect(self._trading_loop)
        self.trading_timer.start(self.period_ms)

    def _trading_loop(self):
        """统一交易循环：一次 wait_update → 所有策略 step() → 更新活跃图表"""
        try:
            # ---- 第1步：等待行情更新（只需一次，所有策略共用同一 TqApi） ----
            is_open = self.strategies[0].wait_update(1)

            # 更新活跃图表的 update_queue（用于状态标记、旧兼容逻辑）
            active = self.window.chart_window
            if active and hasattr(active, 'update_queue'):
                active.update_queue.add(is_open)
                active._is_update = is_open

            self._is_market_open = is_open

            # 首次启动时也可能需要执行策略初始化
            if self._first_start and is_open:
                self._first_start = False

            if not is_open:
                return  # 收盘/无新数据，跳过策略执行和渲染

            # ---- 第2步：执行所有策略的交易逻辑（隔离、互不干扰） ----
            for strategy in self.strategies:
                try:
                    strategy._execute_live_trading()
                except Exception as e:
                    print(f"[策略 {strategy.__class__.__name__}] 执行异常: {e}")

            # ---- 第3步：更新活跃图表渲染 ----
            if active is not None:
                try:
                    active.kline_update()
                    active.indicator_update()
                    active.account_update()
                except Exception as e:
                    print(f"图表更新异常: {e}")

        except Exception as e:
            print(f"多策略交易循环异常: {e}")

    def stop(self):
        """停止定时器并清理资源"""
        if self.trading_timer and self.trading_timer.isActive():
            self.trading_timer.stop()

        try:
            self.trading_timer.timeout.disconnect(self._trading_loop)
        except Exception:
            pass

        self.trading_timer.deleteLater()
        self.trading_timer = None


class Chart:
    """
    lightweight_charts Chart类，负责单个策略图表的显示和数据更新

    主要功能：
    - 绘制K线图表
    - 显示技术指标
    - 处理实时数据更新
    - 管理交易信号标记
    - 支持画线工具
    - 处理价格预警
    """
    toolbox: CustomToolBox
    position_horizontal_line: HorizontalLine
    chart_indicators: dict[str, dict[str, Line]]
    subcharts: dict[str, AbstractChart]

    def __init__(self, widget: LightChartWindow = None, inner_width: float = 1.0, inner_height: float = 1.0,
                 scale_candles_only: bool = False, strategy: Strategy = None, period_milliseconds: int = 1000,
                 use_qtimer: bool = True):
        """
        初始化Chart实例

        Args:
            widget: LightChartWindow实例
            inner_width: 内部宽度比例
            inner_height: 内部高度比例
            scale_candles_only: 是否只缩放蜡烛图
            strategy: 策略实例
            period_milliseconds: 更新周期（毫秒）
            use_qtimer: 是否创建独立的 QuoteQTimer（多策略模式下为 False，由 MultiStrategyTimer 统一管理）
        """
        toolbox = strategy.toolbox if (
            strategy and widget.istool is None) else widget.istool if widget else False
        # 获取QtChart类
        chart_class = get_chart_class()
        # 创建图表实例，使用None作为第一个参数，避免类型错误
        self.chart = chart_class(
            None, inner_width, inner_height, scale_candles_only, toolbox)
        self.light_chart_window = widget
        self.period_milliseconds = max(period_milliseconds, 100)
        self.strategy = strategy
        self._use_qtimer = use_qtimer
        self.chart_indicators = {}
        self.subcharts = {}
        self.markers = {}
        self.signal_indicators = {}
        self._signal_markers = {}   # (sname, sk, time_key) -> marker_id
        self.toolbox = None
        if toolbox:
            self.toolbox = CustomToolBox(self)
        self._new_bar_event = False
        self._saved_visible_range = None
        self._webview_loadfinished = False
        webview = self.get_webview()
        webview.page().loadFinished.connect(self._on_webview_loaded)
        webview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        webview.customContextMenuRequested.connect(
            self._on_webview_custom_context_menu)
        if strategy:
            self._init_chart_data()

    def get_webview(self):
        """获取webview实例"""
        return self.chart.get_webview()

    def run_script(self, script, run_last=False):
        """运行JavaScript脚本"""
        return self.chart.run_script(script, run_last)

    def set(self, df, keep_drawings=False):
        """设置图表数据"""
        return self.chart.set(df, keep_drawings)

    def update(self, series, _from_tick=False):
        """更新图表数据"""
        return self.chart.update(series, _from_tick)

    def fit(self):
        """调整图表以适应数据"""
        return self.chart.fit()

    def create_line(self, name='', color='rgba(214, 237, 255, 0.6)', style='solid', width=2, price_line=True, price_label=True, price_scale_id=None):
        """创建线条"""
        return self.chart.create_line(name, color, style, width, price_line, price_label, price_scale_id)

    def create_subchart(self, position='left', width=0.5, height=0.5, sync=None, scale_candles_only=False, sync_crosshairs_only=False, toolbox=False):
        """创建子图表"""
        return self.chart.create_subchart(position, width, height, sync, scale_candles_only, sync_crosshairs_only, toolbox)

    def time_scale(self, right_offset=0, min_bar_spacing=0.5, visible=True, time_visible=True, seconds_visible=False, border_visible=True, border_color=None):
        """设置时间轴"""
        return self.chart.time_scale(right_offset, min_bar_spacing, visible, time_visible, seconds_visible, border_visible, border_color)

    def layout(self, background_color='#000000', text_color=None, font_size=None, font_family=None):
        """设置布局"""
        return self.chart.layout(background_color, text_color, font_size, font_family)

    def grid(self, vert_enabled=True, horz_enabled=True, color='rgba(29, 30, 38, 5)', style='solid'):
        """设置网格"""
        return self.chart.grid(vert_enabled, horz_enabled, color, style)

    def crosshair(self, mode='normal', vert_visible=True, vert_width=1, vert_color=None, vert_style='large_dashed', vert_label_background_color='rgb(46, 46, 46)', horz_visible=True, horz_width=1, horz_color=None, horz_style='large_dashed', horz_label_background_color='rgb(55, 55, 55)'):
        """设置十字光标"""
        return self.chart.crosshair(mode, vert_visible, vert_width, vert_color, vert_style, vert_label_background_color, horz_visible, horz_width, horz_color, horz_style, horz_label_background_color)

    def candle_style(self, up_color='rgba(39, 157, 130, 100)', down_color='rgba(200, 97, 100, 100)', wick_visible=True, border_visible=True, border_up_color='', border_down_color='', wick_up_color='', wick_down_color=''):
        """设置蜡烛图样式"""
        return self.chart.candle_style(up_color, down_color, wick_visible, border_visible, border_up_color, border_down_color, wick_up_color, wick_down_color)

    def volume_config(self, scale_margin_top=0.8, scale_margin_bottom=0.0, up_color='rgba(83,141,131,0.8)', down_color='rgba(200,127,130,0.8)'):
        """设置成交量配置"""
        return self.chart.volume_config(scale_margin_top, scale_margin_bottom, up_color, down_color)

    def legend(self, visible=False, ohlc=True, percent=True, lines=True, color='rgb(191, 195, 203)', font_size=11, font_family='Monaco', text='', color_based_on_candle=False):
        """设置图例"""
        return self.chart.legend(visible, ohlc, percent, lines, color, font_size, font_family, text, color_based_on_candle)

    def watermark(self, text, font_size=44, color='rgba(180, 180, 200, 0.5)'):
        """添加水印"""
        return self.chart.watermark(text, font_size, color)

    def spinner(self, visible):
        """设置加载动画"""
        return self.chart.spinner(visible)

    def hotkey(self, modifier_key, keys, func):
        """设置热键"""
        return self.chart.hotkey(modifier_key, keys, func)

    def screenshot(self):
        """获取截图"""
        return self.chart.screenshot()

    def set_visible_range(self, start_time, end_time):
        """设置可见范围"""
        return self.chart.set_visible_range(start_time, end_time)

    def resize(self, width=None, height=None):
        """调整图表大小"""
        return self.chart.resize(width, height)

    def lines(self):
        """获取所有线条"""
        return self.chart.lines()

    def precision(self, precision):
        """设置精度"""
        return self.chart.precision(precision)

    def price_line(self, label_visible=True, line_visible=True, title=''):
        """设置价格线"""
        return self.chart.price_line(label_visible, line_visible, title)

    def clear_markers(self):
        """清除标记"""
        return self.chart.clear_markers()

    def horizontal_line(self, price, color='rgb(122, 146, 202)', width=2, style='solid', text='', axis_label_visible=True, func=None):
        """创建水平线"""
        return self.chart.horizontal_line(price, color, width, style, text, axis_label_visible, func)

    def remove_marker(self, marker_id):
        """移除标记"""
        return self.chart.remove_marker(marker_id)

    def marker(self, time=None, position='below', shape='arrow_up', color='#2196F3', text=''):
        """创建标记"""
        return self.chart.marker(time, position, shape, color, text)

    def marker_list(self, markers):
        """创建多个标记"""
        return self.chart.marker_list(markers)

    def hide_data(self):
        """隐藏数据"""
        return self.chart.hide_data()

    def show_data(self):
        """显示数据"""
        return self.chart.show_data()

    @property
    def id(self):
        """获取图表ID"""
        return self.chart.id

    @property
    def events(self):
        """获取图表事件"""
        return self.chart.events

    @property
    def win(self):
        """获取窗口实例"""
        return self.chart.win

    def set_price_scale_fixed_width(self, chart, width):
        """设置价格刻度固定宽度"""
        return self.chart.set_price_scale_fixed_width(chart, width)

    def set_only_last_chart_xaxis_visible(self):
        """只显示最后一个图表的X轴"""
        return self.chart.set_only_last_chart_xaxis_visible()

    def add_chart_separator_lines(self):
        """添加图表分隔线"""
        return self.chart.add_chart_separator_lines()

    def _restore_visible_range(self):
        """恢复可见范围"""
        return self.chart._restore_visible_range()

    def set_all_charts_crosshair_label_background(self):
        """设置所有图表的十字光标标签背景"""
        return self.chart.set_all_charts_crosshair_label_background()

    def set_watermark(self, **kwargs):
        """设置水印"""
        return self.chart.watermark(**kwargs)

    def setSubChartTheme(self, chart, name):
        """设置子图表主题"""
        return self.chart.setSubChartTheme(chart, name)

    def create_histogram(self, name='', color='rgba(214, 237, 255, 0.6)', price_line=True, price_label=True, price_scale_id=None):
        """创建直方图"""
        return self.chart.create_histogram(name, color, price_line, price_label, price_scale_id)

    def cleanup(self):
        """清理资源"""
        if hasattr(self, 'qtimer') and self.qtimer:
            self.qtimer.stop()
        if hasattr(self, 'toolbox') and self.toolbox:
            self.toolbox.cleanup()

    def on_search(self, text):
        """搜索处理"""
        pass

    def _on_alert_settings_changed(self, settings, is_new=False):
        """预警设置变更处理"""
        pass

    @property
    def price_alert(self):
        """获取价格预警设置"""
        return self.light_chart_window.price_alert.get(self.contract, {})

    def setTheme(self):
        """设置所有chart的样式"""
        charts = [self, *self.subcharts.values()]
        for chart in charts:
            self.setChartTheme(chart)
        if self.water_mark:
            self.set_watermark(**self.water_mark)
        if self._webview_loadfinished:
            self.add_chart_separator_lines()

    def setChartTheme(self, chart=None):
        """设置对应chart的样式"""
        if chart is None:
            chart = self
        from qfluentwidgets import isDarkTheme
        dark = isDarkTheme()
        if dark:
            chart.layout(background_color='rgb(6, 6, 6)', text_color='rgb(249, 249, 249)', font_size=14,
                         font_family='Microsoft YaHei')  # 'Helvetica')
            chart.grid(color="rgb(26, 26, 26)")
            chart.legend(visible=True, font_size=14,
                         color='rgb(249, 249, 249)')
        else:
            chart.layout(background_color='rgb(249, 249, 249)', text_color='rgb(6, 6, 6)', font_size=14,
                         font_family='Microsoft YaHei')  # 'Helvetica')
            chart.grid(color="rgb(229, 229, 229)")
            chart.legend(visible=True, font_size=14,
                         color='rgb(6, 6, 6)')

    # def on_chart_click(self, chart, time, price):
    #    ...

    # def on_range_change(self, chart, bars_before, bars_after):
    #     self._save_current_visible_range()

    def _on_webview_loaded(self, success: bool):
        """WebView加载完成回调（增加图表初始化延迟，避开时序差）"""
        if success:
            QTimer.singleShot(500, self.loadsetting)
            # 多策略模式下不创建独立 QuoteQTimer（由 MultiStrategyTimer 统一管理）
            if self._use_qtimer:
                self.qtimer = QuoteQTimer(self)

    def loadsetting(self):
        """加载基本设置"""
        def handle_result(result):
            if result is not None and result > 0:
                width = int(result)
                self.light_chart_window.price_scale_widthes[self.symbol] = width
            if self.symbol in self.light_chart_window.price_scale_widthes:
                width = self.light_chart_window.price_scale_widthes[
                    self.symbol]
            for _, chart in self.subcharts.items():
                self.set_price_scale_fixed_width(chart, width)
            self.set_only_last_chart_xaxis_visible()
            self.add_chart_separator_lines()
            self._restore_visible_range()
            self.set_all_charts_crosshair_label_background()
            self.events.search += self.on_search
            self._on_alert_settings_changed(self.price_alert, False)

        script = f'''
            (function() {{
                if (typeof {self.id} !== 'undefined' && {self.id}.chart && {self.id}.chart.priceScale) {{
                    return {self.id}.chart.priceScale("right").width();
                }}
                return null;
            }})();
        '''
        self.get_webview().page().runJavaScript(script, handle_result)
        self._webview_loadfinished = True

    def init_update(self) -> bool:
        # 即使开盘中调用天勤api.wait_update函数也可能返回false，必须多次调用，获取初始状态
        count = 0
        is_update = False
        start_time = _time.time()
        while count < 100 or (_time.time() - start_time) < 1.0:
            is_update = self.strategy.wait_update(1)
            count += 1
            if is_update:
                break
        return is_update

    def set_candle_style(self) -> None:
        self.candle_style(up_color=self.light_chart_window.bull_color,
                          down_color=self.light_chart_window.bear_color)

    def set_default_candle_style(self) -> None:
        self.light_chart_window.bull_color = btcolors.bull_color
        self.light_chart_window.bear_color = btcolors.bear_color
        self.candle_style(up_color=btcolors.bull_color,
                          down_color=btcolors.bear_color)

    def _init_chart_data(self):
        """
        初始化图表数据和配置

        主要功能：
        - 加载K线数据
        - 初始化技术指标
        - 创建子图表
        - 设置蜡烛图样式
        - 添加交易信号标记
        """
        self.update_queue = FixedSizeQueue(10, self.init_update())
        self._is_update = self.update_queue.any
        dataset = None
        for i, (k, v) in enumerate(self.strategy._btklinedataset.items()):
            if self.light_chart_window.current_contract == f"{v.symbol}_{v.cycle}":
                dataset = v._dataset
                self.cycle = v.cycle
                self.current_index = i
                self.water_mark = v.watermark
                if v._light_chart_candles_down_color:
                    self.light_chart_window.bear_color = v.bear_color
                if v._light_chart_candles_up_color:
                    self.light_chart_window.bull_color = v.bull_color
                self.volume_multiple = v.volume_multiple
                self.price_tick = v.price_tick
                self.position = v.position
                self.account = v.account
                self.symbol = self.light_chart_window.current_contract
                self.contract = v.symbol
                break

        if dataset is None:
            return False

        self._kline = dataset.tq_object
        self.tick = dataset.tq_tick
        assert self._kline is not None, '实盘获取K线数据失败，K线数据获取与天勤API一致'
        df = self._kline.copy()
        df['time'] = df.datetime+8*3.6e12  # 调整时区
        df = df[FILED.TALL]
        self.set(df, True)
        if self.water_mark is not None:
            self.water_mark = self.water_mark.vars
            self.set_watermark(**self.water_mark)
        self.set_candle_style()
        self.strategy()
        self.account_float_profit = self.account.float_profit
        self.light_chart_window.info_window.set_account_info(
            self.strategy._get_account_info())
        pos = self.position.pos
        price = 0.
        text = ""
        color = btcolors.bear_color
        if pos:
            price = self.position.open_price_long if pos > 0 else self.position.open_price_short
            profit = self.position.float_profit
            text = f"⬆️{profit}" if pos > 0 else f"⬇️{profit}"
            color = btcolors.bear_color if profit > 0 else btcolors.bull_color
        self.position_horizontal_line = self.horizontal_line(
            price, color=color, width=3, style='dashed', text=text)
        colors = get_colors()
        for k, v in self.strategy._btindicatordataset.items():
            if v.plot_id == self.current_index:
                plot_id, isplot, name, lines, _lines, ind_names, overlaps, \
                    categorys, indicators, doubles, _ind_plotinfo, span, _signal = v._get_plot_datas(
                        k)
                lineinfo = _ind_plotinfo.get('linestyle', {})
                signal_info: dict = _ind_plotinfo.get('signalstyle', {})
                indicator = v.pandas_object
                if doubles:
                    ind_dict = dict()
                    for j in range(2):
                        if any(isplot[j]):
                            cache_dict = {}
                            if overlaps[j]:
                                chart = self
                            else:
                                chart = self.create_subchart(
                                    'bottom', sync=True)
                                self.setSubChartTheme(chart, name[j])
                                self.subcharts.update({name[j]: chart})
                            for i, plot in enumerate(isplot[j]):
                                if plot:
                                    col = _lines[j][i]
                                    info = lineinfo[col]
                                    color = info.get(
                                        "line_color", None)
                                    if not color:
                                        color = next(colors)
                                    style = info.get('line_dash', 'solid')
                                    if style != "vbar" and style not in util.LINE_STYLE.__args__:
                                        style = 'solid'
                                    width = info.get("line_width", 2)
                                    price_line = info.get(
                                        "price_line", False)
                                    price_label = info.get(
                                        "price_label", False)

                                    if style == "vbar":
                                        # 创建柱状图
                                        line = chart.create_histogram(
                                            name=col, color=color, price_line=price_line, price_label=price_label)

                                        # 准备数据
                                        hist_data = pd.concat(
                                            [df["time"], indicator[col]], axis=1)

                                        # 检查是否是MACD柱状图，如果是，添加颜色列
                                        if "macdh" in col.lower():
                                            # 为MACD柱状图添加颜色列，正值为红色，负值为绿色
                                            # 绿色
                                            hist_data['color'] = btcolors.bear_color
                                            # 红色
                                            hist_data.loc[hist_data[col] > 0,
                                                          'color'] = btcolors.bull_color

                                        # 设置数据
                                        line.set(hist_data)

                                    else:
                                        line = partial(chart.create_line,
                                                       name=col, color=color, style=style, width=width, price_line=price_line, price_label=price_label)
                                        line.vdata = pd.concat(
                                            [df["time"], indicator[col]], axis=1)
                                    cache_dict[col] = line
                            else:
                                # vbar指标在前其它指标线在后，否则vbar柱体会挡住其它指标线
                                if cache_dict:
                                    new_dict = {}
                                    for _k, _v in cache_dict.items():
                                        if hasattr(_v, "vdata"):
                                            data = _v.vdata
                                            _v = _v()
                                            _v.set(data)
                                        new_dict.update({_k: _v})
                                    ind_dict.update(new_dict)
                    if ind_dict:
                        self.chart_indicators.update({name[0]: ind_dict})
                else:
                    if any(isplot):
                        ind_dict = dict()
                        is_candles = v.iscandles
                        if not v.isMDim:
                            indicator = pd.DataFrame(
                                {_lines[0]: indicator})

                        if is_candles:
                            if set(indicator.columns) != set(FILED.OHLC):
                                print(f"蜡烛图指标列名必须为{FILED.OHLC}")
                                continue
                            chart = self.create_subchart(
                                'bottom', sync=True)
                            self.setSubChartTheme(chart, name)
                            self.subcharts.update({name: chart})
                            candles_df = indicator.copy()
                            candles_df["time"] = df["time"]
                            candles_df = candles_df[FILED.TOHLC]
                            chart.set(candles_df)
                        else:
                            if overlaps:
                                chart = self
                            else:
                                chart = self.create_subchart(
                                    'bottom', sync=True)
                                self.setSubChartTheme(chart, name)
                                self.subcharts.update({name: chart})

                            for i, plot in enumerate(isplot):
                                if plot:
                                    col = _lines[i]
                                    info = lineinfo[col]
                                    color = info.get(
                                        "line_color", None)
                                    if not color:
                                        color = next(colors)
                                    style = info.get('line_dash', 'solid')
                                    if style != "vbar" and style not in util.LINE_STYLE.__args__:
                                        style = 'solid'
                                    width = info.get("line_width", 2)
                                    price_line = info.get(
                                        "price_line", False)
                                    price_label = info.get(
                                        "price_label", False)
                                    if style == "vbar":
                                        # 创建柱状图
                                        line = chart.create_histogram(
                                            name=col, color=color, price_line=price_line, price_label=price_label)

                                        # 准备数据
                                        hist_data = pd.concat(
                                            [df["time"], indicator[col]], axis=1)

                                        # 检查是否是MACD柱状图，如果是，添加颜色列
                                        if "macdh" in col.lower():
                                            # 为MACD柱状图添加颜色列，正值为红色，负值为绿色
                                            # 绿色
                                            hist_data['color'] = btcolors.bear_color
                                            # 红色
                                            hist_data.loc[hist_data[col] > 0,
                                                          'color'] = btcolors.bull_color

                                        # 设置数据
                                        line.set(hist_data)
                                    else:
                                        line = partial(chart.create_line,
                                                       name=col, color=color, style=style, width=width, price_line=price_line, price_label=price_label)
                                        line.vdata = pd.concat(
                                            [df["time"], indicator[col]], axis=1)
                                    ind_dict[col] = line
                            else:
                                if ind_dict:
                                    new_dict = {}
                                    for _k, _v in ind_dict.items():
                                        if hasattr(_v, "vdata"):
                                            data = _v.vdata
                                            _v = _v()
                                            _v.set(data)
                                        new_dict.update({_k: _v})
                                    ind_dict = new_dict
                        if ind_dict:
                            self.chart_indicators.update({name: ind_dict})
                # 交易信号：
                if signal_info:
                    signal_dict = {}
                    last_signal_dict = {}
                    all_markers = []

                    for signalname, signal_config in signal_info.items():
                        signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = list(
                            signal_config.values())
                        signal_series = indicator[signalname]
                        is_buy_signal = any([name in signalname for name in [
                                            "long", "exitshort"]]) or "low" in signalkey.lower()
                        signal_points = signal_series[signal_series > 0]
                        if is_buy_signal:
                            position = 'below'
                            shape = signalmarker if signalmarker in util.MARKER_SHAPE.__args__ else 'arrow_up'
                            color = signalcolor if signalcolor else btcolors.bear_color
                        else:
                            position = 'above'
                            shape = signalmarker if signalmarker in util.MARKER_SHAPE.__args__ else 'arrow_down'
                            color = signalcolor if signalcolor else btcolors.bull_color
                        text = signallabel.get("text", "") if isinstance(
                            signallabel, dict) else ""
                        signalconfig = dict(
                            position=position, shape=shape, color=color, text=text)

                        for idx in signal_points.index:
                            time_value = df.iloc[idx]['time']
                            if isinstance(time_value, (int, float)) and time_value > 1e15:
                                time_value = pd.to_datetime(time_value)
                            all_markers.append(
                                {"time": time_value, **signalconfig})
                            # all_markers.append(
                            #     {"time": time_value, **signalconfig})
                        else:
                            if not signal_series.empty and signal_series.iloc[-1] > 0:
                                last_time_value = df.iloc[-1]['time']
                                if isinstance(last_time_value, (int, float)) and last_time_value > 1e15:
                                    last_time_value = pd.to_datetime(last_time_value)
                                last_signal_dict[signalname] = self.chart._single_datetime_format(
                                    last_time_value)
                                # last_signal_dict[signalname] = self.chart._single_datetime_format(
                                #     df.iloc[-1]['time'])
                        signal_dict.update({signalname: signalconfig})
                    if all_markers:
                        all_markers.sort(key=lambda x: x["time"])
                        self.marker_list(all_markers)
                        if last_signal_dict:
                            count = 0
                            for markername in reversed(self.markers):
                                marker = self.markers[markername]
                                for lsd, lsv in last_signal_dict.items():
                                    if marker["time"] == lsv:
                                        signal_dict[lsd]["signal"] = markername
                                        count += 1
                                if count == len(last_signal_dict):
                                    break

                    self.signal_indicators.update({v.sname: signal_dict})
        self.setChartTheme()
        self._resizes()
        if self.toolbox and hasattr(self, 'symbol'):
            self.toolbox.load_drawings(tag=self.symbol)

    def reset_update_queue(self):
        self.update_queue.clear()
        self.update_queue.add(self.init_update())

    def monitor_loop(self):
        try:
            self.update_queue.add(
                self.strategy.wait_update(1))
        except Exception as e:
            print(f"监视器更新异常：{e}")

    def kline_loop(self):
        """行情更新函数"""
        try:
            self.update_queue.add(self.strategy.wait_update(1))
            self._is_update = self.update_queue.any
            if self._is_update:
                self.kline_update()
        except Exception as e:
            print(f"K线更新异常：{e}")

    def account_loop(self):
        """账户更新函数"""
        try:
            if self._is_update:
                self.account_update()
        except Exception as e:
            print(f"账户更新异常：{e}")

    def indicator_loop(self):
        """循环执行指标计算（不依赖K线更新，独立循环）"""
        try:
            if self._is_update:
                self.strategy()
                self.indicator_update()
        except Exception as e:
            print(f"指标更新异常：{e}")

    def is_changing(self, obj: Candlestick | Line, ns) -> int:
        """判断是否更新,获取最新时间与图表最后更新时间的差转化为需要更新数据的K线数量"""
        return int((ns*1e-9-obj._last_bar["time"])/self.cycle)

    def is_key_changing(self, chart_obj: Candlestick | Line, obj: pd.DataFrame, key: str = "close") -> bool:
        return chart_obj._last_bar[key] != obj[key]

    @property
    def datetime(self) -> pd.Series:
        return self._kline["datetime"].copy()+8*3.6e12

    @property
    def new_datetime(self) -> pd.Series:
        return self._kline["datetime"].iloc[-2:].copy()+8*3.6e12

    @property
    def last_datetime(self) -> int:
        return self._kline["datetime"].iloc[-1]+8*3.6e12

    @property
    def kline(self) -> pd.DataFrame:
        kline = self._kline.copy()
        kline["datetime"] = kline["datetime"]+8*3.6e12
        return kline

    @property
    def new_kline(self) -> pd.DataFrame:
        kline = self._kline.iloc[-2:].copy()
        kline["datetime"] = kline["datetime"]+8*3.6e12
        return kline

    def kline_update(self) -> None:
        """K线更新函数"""
        latest_tick_time = self.last_datetime
        chang = self.is_changing(
            self.chart, latest_tick_time)
        kline = self.new_kline[FILED.ALL]
        # 更新频率高的条件放前面
        if chang == 0:
            series = kline.iloc[-1][FILED.DCV]
            series.index = FILED.TPV
            self.chart.update_from_tick(series)
        elif chang == 1:
            series = kline.iloc[-2][FILED.DCV]
            series.index = FILED.TPV
            self.chart.update_from_tick(series)
            sereis = kline.iloc[-1]
            sereis.index = FILED.TALL
            self.chart.update(sereis)
        else:
            sereis = kline.iloc[-1]
            sereis.index = FILED.TALL
            self.chart.update(sereis)

    def account_update(self):
        pos = self.position.pos
        price = 0.
        if pos:
            price = self.position.open_price_long if pos > 0 else self.position.open_price_short
            profit = self.position.float_profit
            text = f"⬆️{profit}" if pos > 0 else f"⬇️{profit}"
            color = btcolors.bear_color if profit > 0 else btcolors.bull_color
            if self.position_horizontal_line.price != price:
                self.position_horizontal_line.update(price)
            self.position_horizontal_line.options(
                color=color, style='dashed', width=3, text=text)

        if price == 0. and self.position_horizontal_line.price != price:
            self.position_horizontal_line.update(price)
            self.position_horizontal_line.options(
                color=btcolors.bear_color, style='dashed', width=3)
        if self.account.float_profit or self.account_float_profit:
            self.light_chart_window.info_window.set_account_info(
                self.strategy._get_account_info())
            self.account_float_profit = self.account.float_profit

    def indicator_update(self):
        """
        指标更新函数，处理技术指标数据的实时更新

        主要功能：
        - 更新蜡烛图指标
        - 更新普通技术指标线
        - 特别处理MACD柱状图的颜色显示
        - 更新交易信号标记
        """
        datetime = self.new_datetime
        last_time = datetime.iloc[-1]
        for k, v in self.strategy._btindicatordataset.items():
            ischang = False  # 每个 indicator 独立计算 chang, 避免不同指标间复用导致状态错位
            sname = v.sname
            if v.plot_id == self.current_index and (sname in self.chart_indicators or sname in self.subcharts):
                if v.iscandles:
                    chart = self.subcharts[sname]
                    if not ischang:
                        chang = self.is_changing(chart, last_time)
                        ischang = True
                    if chang == 0:
                        series = pd.Series(
                            [last_time, v.iloc[-1][3]], index=FILED.TP)
                        chart.update_from_tick(series)
                    elif chang == 1:
                        series = pd.Series(
                            [datetime.iloc[-2], v.iloc[-2][3]], index=FILED.TP)
                        chart.update_from_tick(series)
                        series = pd.Series(
                            [datetime.iloc[-1], *v.iloc[-1].values], index=FILED.TOHLC)
                        chart.update(series)
                    else:
                        series = pd.Series(
                            [datetime.iloc[-1], *v.iloc[-1].values], index=FILED.TOHLC)
                        chart.update(series)

                else:
                    lines = self.chart_indicators[sname]
                    if v.isMDim:
                        ind = v
                    else:
                        ind = pd.DataFrame({v.lines[0]: v.values})
                    for name in v.lines:
                        if name in lines:
                            line = lines[name]
                            if not ischang:
                                chang = self.is_changing(line, last_time)
                                ischang = True
                            values = ind[name].values
                            # 检查是否是MACD柱状图（vbar类型）
                            if hasattr(line, 'update') and "macdh" in name.lower():
                                # 对于MACD柱状图，使用update方法更新数据
                                if chang == 0 or chang > 1:
                                    # 创建包含颜色信息的Series
                                    value = values[-1]
                                    series = pd.Series(
                                        [datetime.iloc[-1], value],
                                        index=["time", name]
                                    )
                                    # 添加颜色信息：正值为bull_color，负值为bear_color
                                    series['color'] = btcolors.bull_color if value > 0 else btcolors.bear_color
                                    # 使用update方法更新数据
                                    line.update(series)
                                else:
                                    # 对于需要更新两个点的情况
                                    for j in range(-2, 0):
                                        value = values[j]
                                        series = pd.Series(
                                            [datetime.iloc[j], value],
                                            index=["time", name]
                                        )
                                        # 添加颜色信息：正值为bull_color，负值为bear_color
                                        series['color'] = btcolors.bull_color if value > 0 else btcolors.bear_color
                                        # 使用update方法更新数据
                                        line.update(series)
                            else:
                                # 普通指标线的更新逻辑
                                if chang == 0 or chang > 1:
                                    series = pd.Series(
                                        [datetime.iloc[-1], values[-1]], index=["time", name])
                                    line.update(series)
                                else:
                                    for j in range(-2, 0):
                                        series = pd.Series(
                                            [datetime.iloc[j], values[j]], index=["time", name])
                                        line.update(series)
                if sname in self.signal_indicators:
                    signal: dict[str, dict] = self.signal_indicators[sname]
                    for sk, sv in signal.items():
                        # chang == 0: 没有新K线，处理最新一根K线的信号变化
                        # chang == 1: 有1根新K线，处理倒数第二根（上一根已确认）的信号变化
                        # chang > 1: 批量添加多根K线，需要重放 chang-1..-2 之间的所有历史信号
                        seq = range(-2, -2)  # 默认空
                        if chang:
                            if chang == 1:
                                seq = range(-2, -1)  # 仅上一根
                            else:
                                seq = range(-chang, -1)  # 批量新增的所有历史K线
                        else:
                            seq = range(-1, 0)  # 仅最新一根

                        for idx in seq:
                            svalue = v[sk].values[idx]  # 该位置信号的值
                            time_value = datetime.iloc[idx]
                            if isinstance(time_value, (int, float)) and time_value > 1e15:
                                time_value = pd.to_datetime(time_value)

                            # 从 self._signal_markers 中按 (sname, sk, time_key) 查找 marker_id
                            # 不再遍历 self.chart.markers，避免不同 signal 类型之间互相干扰
                            time_key = self.chart._single_datetime_format(time_value)
                            marker_key = (sname, sk, time_key)
                            last_signal = self._signal_markers.pop(marker_key, None)

                            if svalue > 0:
                                # 该位置有信号：若尚未创建过 marker 则创建，否则保留已有 marker
                                if last_signal is None:
                                    last_signal = self.marker(time_value, **sv)
                                self._signal_markers[marker_key] = last_signal
                            else:
                                # 该位置无信号：若有活跃 marker 则删除，并清理映射
                                if last_signal is not None:
                                    self.remove_marker(last_signal)

    def set_watermark(self, text: str, font_size: int = 44, dark: bool = None):
        """设置水印"""
        if dark is None:
            dark = self.light_chart_window.main_window.isdark
        color: str = 'rgba(180, 180, 200, 0.2)' if dark else 'rgba(75, 75, 55, 0.2)'
        self.watermark(text, font_size, color)

    def sub_legend_params(self, name, dark):
        """指标参数显示"""
        return dict(text=name, color='rgb(249, 249, 249)' if dark else 'rgb(6, 6, 6)', lines=True, font_size=14, color_based_on_candle=True)

    def _resizes(self):
        """指标窗口高度设置,主图占3,副图占1"""
        num_sub_chart = len(self.subcharts)
        if num_sub_chart == 0:
            self.resize(1, 1)
            return
        num = round(1./(3+num_sub_chart), 4)
        chart_size = 1.+5e-3
        for _, subchart in self.subcharts.items():
            subchart.resize(1., num)
            chart_size -= num
        self.resize(1., chart_size)

    def setTheme(self):
        """设置所有chart的样式"""
        charts = [self, *self.subcharts.values()]
        for chart in charts:
            self.setChartTheme(chart)
        if self.water_mark:
            self.set_watermark(**self.water_mark)
        if self._webview_loadfinished:
            self.add_chart_separator_lines()

    def setChartTheme(self, chart: AbstractChart = None):
        """设置对应chart的样式"""
        if chart is None:
            chart = self
        dark = self.light_chart_window.main_window.isdark
        if dark:
            chart.layout(background_color='rgb(6, 6, 6)', text_color='rgb(249, 249, 249)', font_size=14,
                         font_family='Microsoft YaHei')  # 'Helvetica')
            chart.grid(color="rgb(26, 26, 26)")
            chart.legend(visible=True, font_size=14,
                         color='rgb(249, 249, 249)')
        else:
            chart.layout(background_color='rgb(249, 249, 249)', text_color='rgb(6, 6, 6)', font_size=14,
                         font_family='Microsoft YaHei')  # 'Helvetica')
            chart.grid(color="rgb(229, 229, 229)")
            chart.legend(visible=True, font_size=14,
                         color='rgb(6, 6, 6)')

    def setSubChartTheme(self, chart: AbstractChart, text: str = ""):
        """设置对应子图的样式"""
        dark = self.light_chart_window.main_window.isdark
        if dark:
            chart.layout(background_color='rgb(6, 6, 6)', text_color='rgb(249, 249, 249)', font_size=14,
                         font_family='Microsoft YaHei')  # 'Helvetica')
            chart.grid(color="rgb(26, 26, 26)")
            chart.legend(True, **self.sub_legend_params(text, dark))
        else:
            chart.layout(background_color='rgb(249, 249, 249)', text_color='rgb(6, 6, 6)', font_size=14,
                         font_family='Microsoft YaHei')  # 'Helvetica')
            chart.grid(color="rgb(229, 229, 229)")
            chart.legend(True, **self.sub_legend_params(text, dark))

    def _set_price_scale_fixed_width(self, chart: AbstractChart, target_width: int = None):
        """
        固定副图价格轴宽度，自动同步主图价格轴宽度
        :param chart: 主图实例（AbstractChart类型），用于获取主图价格轴宽度
        :param target_width: 可选参数，手动指定宽度；若为None，自动同步主图宽度
        :return: 无
        """
        chart.run_script(f'''
            {chart.id}.chart.priceScale("right").applyOptions({{
                minimumWidth: {target_width},
                width: {target_width} ,
                autoScale: false
            }});
        ''')

    def set_price_scale_fixed_width(self, chart: AbstractChart, target_width: int = None):
        """固定副图价格轴宽度（优化容错与性能，避免无效执行）"""
        target_width = target_width if (
            target_width is not None and target_width > 0) else 80

        if not chart or not hasattr(chart, 'id'):
            return

        script = f'''
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
        '''
        chart.run_script(script)

    def set_only_last_chart_xaxis_visible(self):
        """
        遍历所有主图+副图，仅最后一个图表显示X轴时间，其余隐藏X轴
        """
        # 1. 收集所有图表：主图 + 所有副图
        all_charts = [self]  # 主图
        all_charts.extend(self.subcharts.values())  # 所有副图
        last_index = len(all_charts) - 1

        if not all_charts:
            return

        # 2. 遍历所有图表，仅最后一个显示X轴
        for idx, chart in enumerate(all_charts):
            # 仅最后一个图表显示X轴，其余隐藏
            is_visible = idx == last_index
            self._set_chart_xaxis_visible(chart, is_visible)

    def _set_chart_xaxis_visible(self, chart: AbstractChart, visible: bool):
        """
        控制单个图表的X轴显示/隐藏（修正lightweight-charts API调用方式）
        :param chart: 目标图表
        :param visible: True=显示X轴，False=隐藏X轴
        """
        chart.run_script(f'''
            try {{
                var targetChart = {chart.id};
                if (targetChart && targetChart.chart && targetChart.chart.timeScale) {{
                    targetChart.chart.timeScale().applyOptions({{
                        visible: {str(visible).lower()}
                    }});
                }}
            }} catch (e) {{
                console.error("设置图表X轴显示状态失败：", e);
            }}
        ''')

    def add_chart_separator_lines(self):
        """
        通过lightweight-charts的容器结构添加分隔线
        """
        if not self.subcharts:
            return
        dark_theme = self.light_chart_window.main_window.isdark
        border_color = "rgba(180, 180, 180, 0.3)" if dark_theme else "rgba(80, 80, 80, 0.3)"

        # 构建一次性执行的JavaScript脚本
        script = f'''
        (function() {{
            try {{
                console.log("开始添加图表分隔线");
                
                // 查找所有lightweight-charts容器
                var chartContainers = document.querySelectorAll('.tv-lightweight-charts');
                console.log("找到图表容器数量:", chartContainers.length);
                
                if (chartContainers.length === 0) {{
                    console.warn("未找到任何lightweight-charts容器");
                    return;
                }}
                
                // 为每个图表容器添加分隔线
                for (var i = 0; i < chartContainers.length; i++) {{
                    var container = chartContainers[i];
                    var separatorId = 'chart_separator_' + i;
                    
                    // 移除已存在的分隔线
                    var existingSeparator = document.getElementById(separatorId);
                    if (existingSeparator) {{
                        existingSeparator.remove();
                    }}
                    
                    // 创建新的分隔线
                    var separator = document.createElement('div');
                    separator.id = separatorId;
                    separator.style.cssText = `
                        position: absolute;
                        top: 0;
                        left: 0;
                        right: 0;
                        height: 1px;
                        background-color: {border_color};
                        z-index: 1000;
                        pointer-events: none;
                    `;
                    
                    // 确保容器有相对定位
                    container.style.position = 'relative';
                    container.appendChild(separator);
                    
                    console.log("为图表容器", i, "添加分隔线成功");
                }}
                
                console.log("全部分隔线添加完成");
                
            }} catch (error) {{
                console.error("添加分隔线时出错:", error);
            }}
        }})();
        '''
        self.run_script(script)

    def _on_webview_custom_context_menu(self, pos):
        """
        自定义 WebView 右键菜单槽函数（添加图标 + 关于弹窗，适配自定义 Dialog）
        """
        webview = self.get_webview()
        custom_menu = RoundMenu("", webview)

        action_reload = Action("重新加载", parent=custom_menu)
        action_reload.setIcon(FluentIcon.UPDATE.icon())

        action_refresh = Action("适应窗口", parent=custom_menu)
        action_refresh.setIcon(FluentIcon.FIT_PAGE.icon())  # 适配窗口图标

        action_restore_range = Action("恢复窗口", parent=custom_menu)
        action_restore_range.setIcon(
            FluentIcon.BACK_TO_WINDOW.icon())  # 恢复窗口图标

        action_show_latest_kline = Action("最近K线", parent=custom_menu)
        action_show_latest_kline.setIcon(
            FluentIcon.SYNC.icon())
        action_show_latest_kline.setToolTip(
            "快速切换到最新300根K线视图")

        action_clear = Action("清空画线", parent=custom_menu)
        action_clear.setIcon(FluentIcon.BROOM.icon())
        action_tool = Action(
            "关闭画线工具" if self.toolbox else "打开画线工具", parent=custom_menu)
        action_tool.setIcon(FluentIcon.CLEAR_SELECTION.icon(
        ) if self.toolbox else FluentIcon.ERASE_TOOL.icon())

        action_alert = Action("价格预警设置", parent=custom_menu)
        action_alert.setIcon(FluentIcon.MESSAGE.icon())

        action_alert_stats = Action("清除预警", parent=custom_menu)
        action_alert_stats.setIcon(FluentIcon.DELETE.icon())

        mouse_color_action = Action("鼠标标签色", parent=custom_menu)
        mouse_color_action.setIcon(FluentIcon.PALETTE.icon())

        candle_style_meun = RoundMenu("蜡烛图样式", parent=custom_menu)
        candle_style_meun.setIcon(FluentIcon.BRUSH.icon())

        candle_style_bull_action = Action("上涨", parent=candle_style_meun)
        candle_style_bull_action.setIcon(FluentIcon.CARE_UP_SOLID.icon())
        candle_style_bull_action.triggered.connect(
            self.open_candle_style_bull_dialog)

        candle_style_bear_action = Action("下跌", parent=candle_style_meun)
        candle_style_bear_action.setIcon(FluentIcon.CARE_DOWN_SOLID.icon())
        candle_style_bear_action.triggered.connect(
            self.open_candle_style_bear_dialog)

        candle_style_meun.addAction(candle_style_bull_action)
        candle_style_meun.addAction(candle_style_bear_action)

        default_candle_style_action = Action("默认蜡烛图样式", parent=custom_menu)
        default_candle_style_action.setIcon(FluentIcon.LABEL.icon())
        default_candle_style_action.triggered.connect(
            self.set_default_candle_style)

        action_settings = Action("恢复默认设置", parent=custom_menu)
        action_settings.setIcon(FluentIcon.SETTING.icon())

        action_about = Action("关于", parent=custom_menu)
        action_about.setIcon(FluentIcon.INFO.icon())

        # 绑定槽函数
        action_reload.triggered.connect(
            lambda: self.light_chart_window.replace_chart(self.strategy))
        action_refresh.triggered.connect(self._fit_chart)
        action_restore_range.triggered.connect(self._restore_visible_range)
        action_show_latest_kline.triggered.connect(
            self._switch_to_latest_300_kline)

        action_clear.triggered.connect(self._clear_all_drawings)
        action_tool.triggered.connect(self._tool)
        action_alert.triggered.connect(self._show_price_alert_dialog)
        action_alert_stats.triggered.connect(self._reset_price_alert)
        mouse_color_action.triggered.connect(self.open_color_dialog)
        action_about.triggered.connect(self._show_about_dialog)
        action_settings.triggered.connect(self._set_default_setting)

        # 根据状态启用/禁用菜单
        action_restore_range.setEnabled(
            self._saved_visible_range is not None)
        action_clear.setEnabled(
            self.symbol in self.light_chart_window.drawings)
        action_alert_stats.setEnabled(
            self.price_alert.get('enabled', False) and
            (self.price_alert.get('up_price', 0) > 0 or
                self.price_alert.get('down_price', 0) > 0)
        )

        # 组装菜单
        custom_menu.addAction(action_reload)
        custom_menu.addAction(action_show_latest_kline)
        custom_menu.addAction(action_refresh)
        custom_menu.addAction(action_restore_range)
        custom_menu.addSeparator()
        custom_menu.addAction(action_tool)
        custom_menu.addAction(action_clear)
        custom_menu.addSeparator()
        custom_menu.addAction(action_alert)
        custom_menu.addAction(action_alert_stats)
        custom_menu.addSeparator()
        custom_menu.addAction(mouse_color_action)
        custom_menu.addMenu(candle_style_meun)
        custom_menu.addAction(default_candle_style_action)
        custom_menu.addSeparator()
        custom_menu.addAction(action_settings)
        custom_menu.addAction(action_about)

        # 显示菜单
        custom_menu.exec(webview.mapToGlobal(pos))

    def _clear_all_symbol_drawings(self, clear_all: bool = True):
        """
        清空画线
        :param clear_all: True-清空所有合约的画线；False-仅清空当前合约画线
        """
        # 1. 清理前端JS层所有绘图元素
        if self.toolbox:
            # 调用工具箱方法清空前端画布
            self.toolbox.clear_drawings()
            # 清空Python端缓存
            if clear_all:
                # 清空所有合约的绘图数据
                self.toolbox.drawings.clear()
                self.light_chart_window.drawings.clear()
            else:
                # 仅清空当前合约
                current_tag = self.light_chart_window.current_contract
                self.toolbox.drawings.pop(current_tag, None)
                self.light_chart_window.drawings.pop(current_tag, None)
        else:
            # 无工具箱实例时，直接执行JS清空画布
            current_tag = self.light_chart_window.current_contract
            self.run_script(
                f'if ({self.id}.toolBox) {self.id}.toolBox.clearDrawings()'
            )
            if clear_all:
                self.light_chart_window.drawings.clear()
            else:
                self.light_chart_window.drawings.pop(current_tag, None)

        # 2. 强制刷新前端视图，确保画线立即消失
        self.run_script(f"{self.id}.chart.applyOptions({'{'}{'}'});")

        QTimer.singleShot(100, self.light_chart_window.save_settings)

    def _set_default_setting(self):
        """重置为默认设置：清空所有画线+加载默认配置+重置样式+保存配置"""
        self._clear_all_symbol_drawings(clear_all=True)
        self.light_chart_window.load_settings(default=True)
        self.set_all_charts_crosshair_label_background()
        self.remove_toolbox()
        self.set_default_candle_style()
        self.light_chart_window.save_default_settings()
        InfoBar.success(
            title="重置成功",
            content="已恢复默认设置",
            duration=2000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self.light_chart_window
        )

    def init_toolbox(self):
        """
        加载画线工具，无图表重载
        显示工具箱，重建实例，加载历史画线
        """
        # 已存在则直接返回，避免重复创建
        if self.toolbox is not None:
            return
        temp_tool = CustomToolBox(self)
        temp_tool.show_toolbox()
        del temp_tool
        self.toolbox = CustomToolBox(self)
        self.toolbox.load_drawings(tag=self.symbol)
        self.light_chart_window.istool = True

    def remove_toolbox(self):
        """
        删除画线工具：逻辑销毁+隐藏UI，不重载图表
        """
        if self.toolbox is None:
            return
        self.toolbox.cleanup()
        self.toolbox = None
        self.light_chart_window.istool = False

    def _tool(self):
        """
        切换画线工具开关，核心调用入口
        不重新加载图表，无缝切换
        """
        if self.toolbox is None:
            self.init_toolbox()
        else:
            self.remove_toolbox()

    def open_color_dialog(self):
        """打开颜色选择对话框"""
        dialog = ColorDialog(Qt.cyan, "选择鼠标标签颜色", self.light_chart_window)
        if dialog.exec():
            color = dialog.color
            self.light_chart_window.mouse_label_color = f'rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})'
            self.set_all_charts_crosshair_label_background()

    def open_candle_style_bull_dialog(self):
        """打开颜色选择对话框"""
        dialog = ColorDialog(Qt.cyan, "选择K线上涨颜色", self.light_chart_window)
        if dialog.exec():
            color = dialog.color
            self.light_chart_window.bull_color = f'rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})'
            self.set_candle_style()

    def open_candle_style_bear_dialog(self):
        """打开颜色选择对话框"""
        dialog = ColorDialog(Qt.cyan, "选择K线下跌颜色", self.light_chart_window)
        if dialog.exec():
            color = dialog.color
            self.light_chart_window.bear_color = f'rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})'
            self.set_candle_style()

    def _get_latest_minibt_version(self) -> str:
        """从PyPI获取minibt最新版本号，失败则返回本地默认版本"""
        from minibt import __version__ as minibt_version
        default_version = minibt_version
        try:
            import requests
            import json
            # PyPI的JSON接口，直接返回包的最新信息
            url = "https://pypi.org/pypi/minibt/json"
            # 设置超时，避免卡界面
            response = requests.get(url, timeout=5)
            response.raise_for_status()  # 捕获HTTP错误（如404、500）

            # 解析JSON获取最新版本号
            data = json.loads(response.text)
            latest_version = data["info"]["version"]
            return f"v{latest_version}"  # 格式化为 vx.x.x

        except requests.exceptions.RequestException as e:
            # 网络错误（超时、断网、接口不可用）
            print(f"获取最新版本失败（网络问题）：{e}")
            return default_version
        except (KeyError, json.JSONDecodeError) as e:
            # 接口格式变化/解析错误
            print(f"解析版本信息失败：{e}")
            return default_version

    def _show_about_dialog(self):
        """显示“关于”弹窗（适配自定义 Dialog，显示关闭按钮，可正常退出）"""
        about_dialog = Dialog("关于 MiniBt", "MiniBt",
                              parent=self.light_chart_window)
        about_dialog.setFixedSize(450, 420)

        self._clean_about_dialog_default_widgets(about_dialog)
        self._optimize_dialog_default_buttons(about_dialog)

        content_container = QWidget(about_dialog)
        main_layout = QVBoxLayout(content_container)
        main_layout.setSpacing(16)
        main_layout.setContentsMargins(24, 24, 24, 20)

        title_card = CardWidget(content_container)
        title_layout = QVBoxLayout(title_card)
        title_layout.setContentsMargins(20, 16, 20, 16)

        app_title = TitleLabel("MiniBt", title_card)
        latest_version = self._get_latest_minibt_version()
        version_label = BodyLabel(f"版本: {latest_version}", title_card)

        title_layout.addWidget(app_title)
        title_layout.addWidget(version_label)
        main_layout.addWidget(title_card)

        link_card = CardWidget(content_container)
        link_layout = QVBoxLayout(link_card)
        link_layout.setContentsMargins(20, 16, 20, 16)
        link_layout.setSpacing(12)

        # GitHub 仓库
        github_link = HyperlinkLabel(QUrl("https://github.com/MiniBtMaster/minibt"),
                                     "GitHub 仓库", link_card)
        link_layout.addWidget(github_link)

        # PyPI 仓库
        pypi_link = HyperlinkLabel(QUrl("https://pypi.org/project/minibt/"),
                                   "PyPI 仓库", link_card)
        link_layout.addWidget(pypi_link)

        # 项目教程
        tutorial_link = HyperlinkLabel(QUrl("https://www.minibt.cn"),
                                       "项目教程", link_card)
        link_layout.addWidget(tutorial_link)

        # 联系邮箱
        email_label = BodyLabel("联系邮箱: 407841129@qq.com", link_card)
        link_layout.addWidget(email_label)

        main_layout.addWidget(link_card)

        about_dialog.vBoxLayout.insertWidget(
            1, content_container, 1, Qt.AlignmentFlag.AlignTop)

        # 步骤 7：显示对话框
        about_dialog.exec()

    def _switch_to_latest_300_kline(self,):
        """
        新增：切换到最新300根K线视图（核心功能实现）
        """
        length = len(self._kline)
        datetime = self._kline.datetime[[length-300, length-1]]+8*3.6e12
        start, end = datetime.tolist()
        from_ts = pd.to_datetime(start).timestamp()
        to_ts = pd.to_datetime(end).timestamp()
        self.run_script(f'''
        {self.id}.chart.timeScale().setVisibleRange({{
            from: {from_ts},
            to: {to_ts}
        }})
        ''')

    def _clean_about_dialog_default_widgets(self, dialog):
        """
        清理自定义 Dialog 的默认冗余控件（不修改原码，仅临时隐藏）
        避免默认标题/内容标签与自定义内容重叠
        """
        # 隐藏默认标题标签（windowTitleLabel 和 titleLabel）
        if hasattr(dialog, 'windowTitleLabel') and dialog.windowTitleLabel:
            dialog.windowTitleLabel.hide()
        if hasattr(dialog, 'titleLabel') and dialog.titleLabel:
            dialog.titleLabel.hide()

        # 隐藏默认内容标签（contentLabel）
        if hasattr(dialog, 'contentLabel') and dialog.contentLabel:
            dialog.contentLabel.hide()

        # 清理默认 textLayout 的间距，避免影响自定义内容布局
        if hasattr(dialog, 'textLayout'):
            dialog.textLayout.setSpacing(0)
            dialog.textLayout.setContentsMargins(0, 0, 0, 0)

    def _optimize_dialog_default_buttons(self, dialog):
        """
        优化自定义 Dialog 的默认按钮组
        """
        if hasattr(dialog, 'yesButton') and dialog.yesButton:
            dialog.yesButton.setText("关闭")
            dialog.yesButton.setIcon(FluentIcon.CLOSE.icon())
            dialog.yesButton.adjustSize()

        if hasattr(dialog, 'cancelButton') and dialog.cancelButton:
            dialog.cancelButton.hide()

        if hasattr(dialog, 'buttonGroup') and dialog.buttonGroup:
            dialog.buttonGroup.setFixedHeight(81)

    def _fit_chart(self):
        """适应窗口：将所有K线纳入可视范围，并记录之前的范围"""
        self._save_current_visible_range()
        self.fit()

    def _save_current_visible_range(self):
        """
        修复：通过异步回调获取当前可视范围
        """
        def handle_range_result(result):
            """处理JS返回的结果"""
            try:
                if result is not None and isinstance(result, str):
                    from_ts, to_ts = result.split(',')
                    self._saved_visible_range = (
                        float(from_ts), float(to_ts))
                    self.light_chart_window.visible_range[self.symbol] = self._saved_visible_range
                else:
                    self._saved_visible_range = None
            except Exception as e:
                self._saved_visible_range = None

        get_range_script = f'''
            (function() {{
                try {{
                    const chart = {self.id};
                    if (!chart || !chart.chart) {{
                        console.error("图表实例未找到");
                        return null;
                    }}
                    
                    const timeScale = chart.chart.timeScale();
                    if (!timeScale) {{
                        console.error("timeScale未找到");
                        return null;
                    }}
                    
                    const visibleRange = timeScale.getVisibleRange();
                    if (!visibleRange) {{
                        console.error("无法获取可视范围");
                        return null;
                    }}
                    
                    console.log("可视范围:", visibleRange);
                    return visibleRange.from + "," + visibleRange.to;
                    
                }} catch (error) {{
                    console.error("获取可视范围时出错:", error);
                    return null;
                }}
            }})();
        '''
        webview = self.get_webview()
        if webview and webview.page():
            webview.page().runJavaScript(get_range_script, handle_range_result)
        else:
            self._saved_visible_range = None

    def _restore_visible_range(self):
        """
        核心方法：恢复到 fit() 前保存的可视范围（右键菜单「恢复窗口」绑定此方法）
        """
        if not self._saved_visible_range:
            self._saved_visible_range = self.light_chart_window.visible_range.get(
                self.symbol, None)
        if not self._saved_visible_range:
            return
        from_ts, to_ts = self._saved_visible_range
        self.run_script(f'''
        {self.id}.chart.timeScale().setVisibleRange({{
            from: {from_ts},
            to: {to_ts}
        }})
        ''')

    def _clear_all_drawings(self):
        """清空所有画线（基于CustomToolBox的drawings数据）"""
        if not self.toolbox:
            return
        self.run_script(
            f'if ({self.id}.toolBox) {self.id}.toolBox.clearDrawings()')
        current_tag = self.light_chart_window.current_contract
        if current_tag in self.toolbox.drawings:
            del self.toolbox.drawings[current_tag]
        if current_tag in self.light_chart_window.drawings:
            del self.light_chart_window.drawings[current_tag]

    # 事件
    def _show_price_alert_dialog(self):
        """显示价格预警设置对话框 - 使用新版对话框"""
        # 创建对话框
        dialog = PriceAlertSettingDialog(
            "价格预警设置",
            "",
            self.light_chart_window,
            self.price_alert

        )
        # 连接信号
        dialog.alertSettingsChanged.connect(
            self._on_alert_settings_changed)
        # 显示对话框
        if dialog.exec():
            ...

    @property
    def price_alert(self) -> dict:
        return self.light_chart_window.price_alert.get(self.contract, {})

    @price_alert.setter
    def price_alert(self, value):
        if isinstance(value, dict):
            self.light_chart_window.price_alert[self.contract] = value

    def _on_alert_settings_changed(self, settings: dict, showinfo: bool = True):
        """处理预警设置变化"""
        if not settings.get('enabled', False):
            self.price_alert = {}
            return
        if settings:
            up_price = settings.get('up_price', 0)
            down_price = settings.get('down_price', 0)
            if not any([up_price, down_price]):
                self.price_alert = {}
                return
        self.price_alert = settings
        self.set_on_new_bar_event()
        if showinfo:
            alert_type_text = {
                'both': '双向预警',
                'up': '上破预警',
                'down': '下破预警'
            }.get(settings.get('alert_type', 'both'), '双向预警')

            info_text = f"价格预警已启用 [{alert_type_text}]"

            up_price = settings.get('up_price', 0)
            down_price = settings.get('down_price', 0)

            if up_price > 0:
                info_text += f" 上破: {up_price:.4f}"
            if down_price > 0:
                info_text += f" 下破: {down_price:.4f}"

            InfoBar.info("价格预警设置成功", info_text, duration=2000,
                         position=InfoBarPosition.TOP_RIGHT, parent=self.light_chart_window)

    def set_on_new_bar_event(self):
        if not self._new_bar_event:
            self.events.new_bar += self.on_new_bar
            self._new_bar_event = True

    def on_new_bar(self, chart):
        """
        新K线生成回调：实用看盘功能
        1. 监控收盘价是否触发价格预警
        2. 打印最新收盘价（保留4位小数，便于精准监控）
        3. 触发预警时弹出提示并标记K线
        4. 自动重置预警（价格回归区间后）
        """
        if not self.price_alert:
            return
        latest_close = self._kline.iloc[-1].copy()['close']

        # 1. 检查价格预警配置
        alert_config = self.price_alert
        up_price = alert_config['up_price']
        down_price = alert_config['down_price']
        enabled = alert_config['enabled']

        # 2. 上破价格预警（未触发过且配置有效才提示）
        if up_price > 0 and latest_close >= up_price and enabled:
            self._trigger_price_alert(
                "上破预警",
                f"收盘价 {latest_close:.4f} 上破预警价格 {up_price:.4f}！"
            )
        # 3. 下破价格预警（未触发过且配置有效才提示）
        elif down_price > 0 and latest_close <= down_price and enabled:
            self._trigger_price_alert(
                "下破预警",
                f"收盘价 {latest_close:.4f} 下破预警价格 {down_price:.4f}！"
            )

    def _trigger_price_alert(self, title, content, duration=2000, position=InfoBarPosition.TOP_RIGHT):
        InfoBar.info(title, content, duration=duration,
                     position=position, parent=self.light_chart_window)

    def _reset_price_alert(self):
        self.price_alert = {}

    def on_search(self, chart, searched_string):
        """
        合约搜索回调：基于自定义 Dialog 适配，避免 contentWidget 报错
        """
        if not searched_string or not hasattr(self.light_chart_window, 'contract_dict'):
            return

        # 1. 筛选符合条件的合约（不区分大小写，模糊匹配）
        matched_contracts = []
        contract_dict = self.light_chart_window.contract_dict
        search_lower = searched_string.lower()

        for contract in contract_dict.keys():
            if search_lower in contract.lower():
                matched_contracts.append(contract)

        # 2. 无匹配结果，显示优雅提示
        if not matched_contracts:
            InfoBar.warning(
                title="无匹配结果",
                content=f"未找到包含「{searched_string}」的合约",
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self.light_chart_window
            )
            return

        # 3. 单个匹配结果，直接切换合约并提示
        if len(matched_contracts) == 1:
            target_contract = matched_contracts[0]
            self._switch_contract_directly(target_contract)
            InfoBar.success(
                title="切换成功",
                content=f"已快速切换至合约「{target_contract}」",
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self.light_chart_window
            )
            return

        # 4. 多个匹配结果，弹出适配自定义 Dialog 的 Fluent 风格对话框
        self._create_fluent_search_dialog(
            matched_contracts, searched_string)

    def _create_fluent_search_dialog(self, matched_contracts: list[str], searched_string: str):
        """
        创建适配自定义 Dialog 的 Fluent 风格对话框（核心修正：无 contentWidget）
        复用自定义 Dialog 的 vBoxLayout，清理默认文本控件，添加 QFluentWidgets 控件
        """
        dialog_title = f"找到 {len(matched_contracts)} 个匹配合约"
        self.search_dialog = Dialog(
            dialog_title, "", parent=self.light_chart_window)
        self.search_dialog.setFixedSize(450, 400)

        self._clean_custom_dialog_default_widgets()

        content_container = QWidget(self.search_dialog)
        main_layout = QVBoxLayout(content_container)
        main_layout.setSpacing(16)
        main_layout.setContentsMargins(24, 24, 24, 20)

        tip_card = CardWidget(content_container)
        tip_layout = QHBoxLayout(tip_card)
        tip_layout.setContentsMargins(16, 12, 16, 12)
        tip_label = BodyLabel(
            f"关键词：「{searched_string}」，可二次筛选缩小范围", tip_card)
        tip_layout.addWidget(tip_label, 0, Qt.AlignmentFlag.AlignLeft)
        main_layout.addWidget(tip_card)

        self.filter_search = SearchLineEdit(content_container)
        self.filter_search.setPlaceholderText("再次筛选合约（模糊匹配）...")
        self.filter_search.setClearButtonEnabled(True)
        self.filter_search.textChanged.connect(
            lambda text: self._filter_contract_list_fluent(
                text, matched_contracts)
        )
        main_layout.addWidget(self.filter_search)

        scroll_area = ScrollArea(content_container)
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }")

        self.contract_list_fluent = ListWidget(scroll_area)
        self.contract_list_fluent.addItems(matched_contracts)
        self.contract_list_fluent.setSelectionMode(
            ListWidget.SingleSelection)
        self.contract_list_fluent.setCurrentRow(0)
        self.contract_list_fluent.itemDoubleClicked.connect(
            lambda item: self._confirm_contract_selection_fluent(
                item.text())
        )
        scroll_area.setWidget(self.contract_list_fluent)

        scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_layout.addWidget(scroll_area, 1)

        btn_layout = QHBoxLayout()
        cancel_btn = PushButton("取消", content_container,
                                FluentIcon.CLOSE.icon())
        cancel_btn.clicked.connect(self.search_dialog.reject)
        confirm_btn = PrimaryPushButton(
            "确认选择", content_container, FluentIcon.ACCEPT.icon())
        confirm_btn.clicked.connect(
            self._confirm_contract_selection_fluent)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        btn_layout.setSpacing(12)
        main_layout.addLayout(btn_layout)

        self.search_dialog.vBoxLayout.insertWidget(
            1, content_container, 1, Qt.AlignmentFlag.AlignTop)
        self.search_dialog.buttonGroup.hide()

        self.search_dialog.exec()

    def _clean_custom_dialog_default_widgets(self):
        """
        清理自定义 Dialog 的默认控件（不修改原码，仅临时隐藏/移除）
        避免默认标题/内容标签与 QFluentWidgets 控件冲突
        """
        dialog = self.search_dialog
        if hasattr(dialog, 'windowTitleLabel') and dialog.windowTitleLabel:
            dialog.windowTitleLabel.hide()
        if hasattr(dialog, 'titleLabel') and dialog.titleLabel:
            dialog.titleLabel.hide()

        if hasattr(dialog, 'contentLabel') and dialog.contentLabel:
            dialog.contentLabel.hide()

        if hasattr(dialog, 'textLayout'):
            dialog.textLayout.setSpacing(0)
            dialog.textLayout.setContentsMargins(0, 0, 0, 0)

    def _filter_contract_list_fluent(self, filter_text: str, original_contracts: list[str]):
        """实时二次筛选合约列表"""
        self.contract_list_fluent.clear()
        if not filter_text:
            self.contract_list_fluent.addItems(original_contracts)
            self.contract_list_fluent.setCurrentRow(0)
            return

        filter_lower = filter_text.lower()
        filtered_contracts = [
            c for c in original_contracts if filter_lower in c.lower()
        ]
        self.contract_list_fluent.addItems(filtered_contracts)
        if filtered_contracts:
            self.contract_list_fluent.setCurrentRow(0)

    def _confirm_contract_selection_fluent(self, contract_text: str = None):
        """确认合约选择"""
        target_contract = None
        if contract_text:
            target_contract = contract_text
        else:
            current_item = self.contract_list_fluent.currentItem()
            if not current_item:
                InfoBar.warning(
                    title="选择错误",
                    content="请先选择一个合约再确认！",
                    duration=1500,
                    position=InfoBarPosition.TOP_RIGHT,
                    parent=self.search_dialog
                )
                return
            target_contract = current_item.text()

        self._switch_contract_directly(target_contract)
        self.search_dialog.accept()
        InfoBar.success(
            title="切换成功",
            content=f"已切换至合约「{target_contract}」，图表数据正在更新...",
            duration=2000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self.light_chart_window
        )

    def _switch_contract_directly(self, contract: str):
        """直接切换目标合约"""
        light_window = self.light_chart_window
        if contract not in light_window.contract_dict:
            InfoBar.error(
                title="切换失败",
                content=f"合约「{contract}」未配置对应的策略！",
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self.light_chart_window
            )
            return
        light_window.current_contract = contract
        strategy = light_window.strategies[light_window.contract_dict[contract]]
        light_window.replace_chart(strategy)

    def set_crosshair_label_background(self, chart: AbstractChart, bg: str = 'rgba(30, 30, 30, 0.9)'):
        """
        设置十字线移动时X时间轴和Y价格轴标签的背景色
        :param dark: 是否为深色模式，默认使用全局主题设置
        """

        # 生成JS脚本
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
        }} catch (e) {{
            console.error("设置十字线标签背景色失败:", e);
        }}
        '''
        chart.run_script(script)

    def set_all_charts_crosshair_label_background(self):
        """
        为所有图表（主图+副图）设置十字线标签背景色
        :param dark: 是否为深色模式，默认使用全局主题设置
        """
        bg = self.light_chart_window.mouse_label_color
        if bg:
            charts = [self, *self.subcharts.values()]
            for chart in charts:
                self.set_crosshair_label_background(chart, bg)

    def cleanup(self):
        """切换合约时的彻底清理逻辑（核心）"""
        if hasattr(self, 'qtimer') and self.qtimer:
            try:
                self.qtimer.stop()
            except Exception as e:
                print(f"停止定时器时出错: {e}")
            finally:
                self.qtimer = None
        event_names = [
            f'search{self.id}',
            f'save_drawings{self.id}',
            f'click{self.id}',
            f'range_change{self.id}',
            f'new_bar{self.id}'
        ]

        for event_name in event_names:
            try:
                if hasattr(self.win, 'handlers') and event_name in self.win.handlers:
                    del self.win.handlers[event_name]
            except Exception as e:
                print(f"清理事件监听器异常:{e}")
        if hasattr(self, 'events'):
            try:
                for event_name in ['click', 'range_change', 'search', 'new_bar']:
                    if hasattr(self.events, event_name):
                        getattr(self.events, event_name)._callable = []
                        setattr(self.events, event_name, None)

                self.chart.events = None
            except Exception as e:
                print(f"清理 Emitter 事件异常:{e}")

        webview = self.get_webview()
        if webview:
            cleanup_js = f'''
            try {{
                // 1. 销毁图表实例
                if (typeof {self.id} !== 'undefined' && {self.id}.chart) {{
                    // 解绑所有事件
                    {self.id}.chart.unsubscribeClick();
                    {self.id}.chart.unsubscribeCrosshairMove();
                    {self.id}.chart.timeScale().unsubscribeVisibleLogicalRangeChange();
                    
                    // 移除所有系列和子图表
                    while ({self.id}.chart.seriesCount() > 0) {{
                        {self.id}.chart.removeSeries({self.id}.chart.seriesByIndex(0));
                    }}
                    
                    // 销毁图表
                    {self.id}.chart.destroy();
                    {self.id}.chart = null;
                }}
                
                // 2. 销毁工具箱
                if ({self.id}.toolBox) {{
                    {self.id}.toolBox = null;
                }}
                
                // 3. 清空整个图表对象
                {self.id} = null;
                
                // 4. 强制垃圾回收
                if (window.gc) {{ window.gc(); }}
            }} catch (e) {{
                console.error("JS层清理失败:", e);
            }}
            '''
            webview.page().runJavaScript(cleanup_js)
            try:
                # 断开所有信号连接
                webview.page().loadFinished.disconnect(self._on_webview_loaded)
                webview.customContextMenuRequested.disconnect(
                    self._on_webview_custom_context_menu)

                # 停止页面加载
                webview.stop()
                webview.setUrl(QUrl("about:blank"))  # 清空页面

                # 清理页面
                page = webview.page()
                if page:
                    # page.history().clear()
                    page.deleteLater()

                # 设置空页面
                webview.setPage(None)

            except Exception as e:
                print(f"清理 WebView 时出错: {e}")
        for name, subchart in list(self.subcharts.items()):
            try:
                subchart_id = subchart.id
                if hasattr(self.win, 'handlers'):
                    keys_to_delete = []
                    for key in list(self.win.handlers.keys()):
                        if subchart_id in key:
                            keys_to_delete.append(key)
                    for key in keys_to_delete:
                        del self.win.handlers[key]
            except Exception as e:
                print(f"清理子图表 {name} 时出错: {e}")

        self.subcharts.clear()
        self.chart_indicators.clear()
        self.signal_indicators.clear()
        self._kline = None
        self.tick = None
        self.strategy = None
        self.toolbox = None
        self.light_chart_window = None

    def smart_pan(self, direction='left'):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        direction_map = {
            'left': {'scroll_delta': 15, 'range_multiplier': -0.1},
            'right': {'scroll_delta': -15, 'range_multiplier': 0.1}
        }
        if direction not in direction_map:
            return
        delta_config = direction_map[direction]

        js_script = f'''
            (function() {{
                try {{
                    const chart = window['{pure_id}'].chart;
                    if (!chart) return;
                    const timeScale = chart.timeScale();
                    let currentState = {{}};

                    try {{ currentState.scrollPos = timeScale.scrollPosition(); }} catch(e) {{}}
                    try {{
                        const range = timeScale.getVisibleRange();
                        if (range) currentState.visibleRange = {{from: range.from, to: range.to, width: range.to - range.from}};
                    }} catch(e) {{}}

                    if (currentState.scrollPos !== undefined) {{
                        const newPos = currentState.scrollPos + {delta_config['scroll_delta']};
                        timeScale.scrollToPosition(newPos, false);
                    }} else if (currentState.visibleRange) {{
                        const range = currentState.visibleRange;
                        const shift = range.width * {delta_config['range_multiplier']};
                        timeScale.setVisibleRange({{from: range.from + shift, to: range.to + shift}});
                    }} else {{
                        try {{
                            const logicalRange = timeScale.getVisibleLogicalRange();
                            if (logicalRange) {{
                                const barCount = logicalRange.to - logicalRange.from;
                                const shiftBars = Math.max(1, Math.ceil(barCount * 0.1));
                                const directionMultiplier = '{direction}' === 'left' ? 1 : -1;
                                timeScale.setVisibleLogicalRange({{
                                    from: logicalRange.from + shiftBars * directionMultiplier,
                                    to: logicalRange.to + shiftBars * directionMultiplier
                                }});
                            }}
                        }} catch(e) {{}}
                    }}
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def pan_left_final(self):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        js_script = f'''
            (function() {{
                try {{
                    const chart = window['{pure_id}'].chart;
                    const timeScale = chart.timeScale();
                    const visibleRange = timeScale.getVisibleRange();
                    if (visibleRange) {{
                        const viewWidth = visibleRange.to - visibleRange.from;
                        const timeShift = viewWidth * 0.1;
                        timeScale.setVisibleRange({{
                            from: visibleRange.from - timeShift,
                            to: visibleRange.to - timeShift
                        }});
                    }}
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def pan_right_final(self):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        js_script = f'''
            (function() {{
                try {{
                    const chart = window['{pure_id}'].chart;
                    const timeScale = chart.timeScale();
                    const visibleRange = timeScale.getVisibleRange();
                    if (visibleRange) {{
                        const viewWidth = visibleRange.to - visibleRange.from;
                        const timeShift = viewWidth * 0.1;
                        timeScale.setVisibleRange({{
                            from: visibleRange.from + timeShift,
                            to: visibleRange.to + timeShift
                        }});
                    }}
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def pan_with_fixed_bars(self, direction='left'):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        js_script = f'''
            (function() {{
                try {{
                    const chart = window['{pure_id}'].chart;
                    const timeScale = chart.timeScale();
                    const logicalRange = timeScale.getVisibleLogicalRange();
                    if (!logicalRange) return;
                    const currentBars = logicalRange.to - logicalRange.from;
                    const shiftBars = Math.max(1, Math.ceil(currentBars * 0.1));
                    let newFrom, newTo;
                    if ('{direction}' === 'left') {{
                        newFrom = logicalRange.from + shiftBars;
                        newTo = logicalRange.to + shiftBars;
                    }} else {{
                        newFrom = Math.max(0, logicalRange.from - shiftBars);
                        newTo = logicalRange.to - shiftBars;
                    }}
                    if (newTo <= newFrom) newTo = newFrom + 1;
                    timeScale.setVisibleLogicalRange({{from: newFrom, to: newTo}});
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def pan(self, direction='left', mode='time'):
        if direction not in ['left', 'right']:
            return
        if mode == 'time':
            self.pan_left_final() if direction == 'left' else self.pan_right_final()
        elif mode == 'bars':
            self.pan_with_fixed_bars(direction)
        elif mode == 'smart':
            self.smart_pan(direction)

    def pan_left(self):
        self.pan('right', 'smart')

    def pan_right(self):
        self.pan('left', 'smart')

    def zoom_in_wheel(self):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        js_script = f'''
            (function() {{
                try {{
                    const chartInstance = window['{pure_id}'];
                    if (!chartInstance || !chartInstance.chart) return;
                    const chart = chartInstance.chart;
                    const chartElement = chart.chartElement();
                    if (!chartElement) return;
                    const rect = chartElement.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const wheelEvent = new WheelEvent('wheel', {{
                        deltaX: 0, deltaY: -50, deltaMode: WheelEvent.DOM_DELTA_PIXEL,
                        clientX: centerX, clientY: centerY, view: window, bubbles: true, cancelable: true
                    }});
                    chartElement.dispatchEvent(wheelEvent);
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def zoom_out_wheel(self):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        js_script = f'''
            (function() {{
                try {{
                    const chartInstance = window['{pure_id}'];
                    if (!chartInstance || !chartInstance.chart) return;
                    const chart = chartInstance.chart;
                    const chartElement = chart.chartElement();
                    if (!chartElement) return;
                    const rect = chartElement.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const wheelEvent = new WheelEvent('wheel', {{
                        deltaX: 0, deltaY: 50, deltaMode: WheelEvent.DOM_DELTA_PIXEL,
                        clientX: centerX, clientY: centerY, view: window, bubbles: true, cancelable: true
                    }});
                    chartElement.dispatchEvent(wheelEvent);
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def zoom_smart(self, direction='in'):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        delta_config = {'in': {'deltaY': -80, 'factor': 0.9},
                        'out': {'deltaY': 80, 'factor': 1.1}}
        if direction not in delta_config:
            return
        config = delta_config[direction]

        js_script = f'''
            (function() {{
                try {{
                    const chartInstance = window['{pure_id}'];
                    if (!chartInstance || !chartInstance.chart) return;
                    const chart = chartInstance.chart;
                    const chartElement = chart.chartElement();
                    if (!chartElement) return;

                    try {{
                        const rect = chartElement.getBoundingClientRect();
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const wheelEvent = new WheelEvent('wheel', {{
                            deltaX: 0, deltaY: {config['deltaY']}, deltaMode: WheelEvent.DOM_DELTA_PIXEL,
                            clientX: centerX, clientY: centerY, view: window, bubbles: true, cancelable: true
                        }});
                        if (chartElement.dispatchEvent(wheelEvent)) return;
                    }} catch(e) {{}}

                    try {{
                        const timeScale = chart.timeScale();
                        const range = timeScale.getVisibleRange();
                        if (range) {{
                            const currentWidth = range.to - range.from;
                            const newWidth = currentWidth * {config['factor']};
                            const center = (range.from + range.to) / 2;
                            timeScale.setVisibleRange({{from: center - newWidth/2, to: center + newWidth/2}});
                        }}
                    }} catch(e) {{}}
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def zoom_at_position(self, direction='in', x_percent=0.5, y_percent=0.5):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        delta_map = {'in': -100, 'out': 100}
        if direction not in delta_map:
            return
        delta_y = delta_map[direction]

        js_script = f'''
            (function() {{
                try {{
                    const chartInstance = window['{pure_id}'];
                    if (!chartInstance || !chartInstance.chart) return;
                    const chart = chartInstance.chart;
                    const chartElement = chart.chartElement();
                    if (!chartElement) return;
                    const rect = chartElement.getBoundingClientRect();
                    const posX = rect.left + rect.width * {x_percent};
                    const posY = rect.top + rect.height * {y_percent};
                    const wheelEvent = new WheelEvent('wheel', {{
                        deltaX: 0, deltaY: {delta_y}, deltaMode: WheelEvent.DOM_DELTA_PIXEL,
                        clientX: posX, clientY: posY, view: window, bubbles: true, cancelable: true
                    }});
                    chartElement.dispatchEvent(wheelEvent);
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def zoom(self, direction='in', method='wheel', position=None):
        if direction not in ['in', 'out']:
            return
        if method == 'wheel':
            self.zoom_in_wheel() if direction == 'in' else self.zoom_out_wheel()
        elif method == 'smart':
            self.zoom_smart(direction)
        elif method == 'position':
            x, y = position if position else (0.5, 0.5)
            self.zoom_at_position(direction, x, y)

    def zoom_in(self):
        self.zoom('in', 'smart')

    def zoom_out(self):
        self.zoom('out', 'smart')

    def center_on_latest_smart(self):
        pure_id = self.id.replace(
            'window.', '') if 'window.' in self.id else self.id
        js_script = f'''
            (function() {{
                try {{
                    const chartInstance = window['{pure_id}'];
                    if (!chartInstance || !chartInstance.chart) return;
                    const chart = chartInstance.chart;
                    const timeScale = chart.timeScale();
                    const currentRange = timeScale.getVisibleRange();
                    if (!currentRange) {{
                        timeScale.fitContent();
                        return;
                    }}
                    const currentWidth = currentRange.to - currentRange.from;
                    const series = chartInstance.series;
                    let latestTime = 0;
                    if (series && series.data) {{
                        const allData = series.data();
                        if (allData && allData.length > 0) latestTime = allData[allData.length-1].time;
                    }}
                    if (latestTime > 0) {{
                        timeScale.setVisibleRange({{from: latestTime - currentWidth, to: latestTime}});
                    }} else {{
                        timeScale.fitContent();
                    }}
                }} catch(e) {{}}
            }})();
        '''
        self.get_webview().page().runJavaScript(js_script)

    def center_on_latest(self):
        return self.center_on_latest_smart()


def main(strategies, period_milliseconds: int = 1000):
    try:
        # 过滤QFont警告
        import warnings
        warnings.filterwarnings(
            "ignore", message="QFont::setPointSize: Point size <= 0 (-1), must be greater than 0")

        # 使用Qt的日志系统过滤警告
        from PyQt6.QtCore import Qt, QTranslator, qInstallMessageHandler, QtMsgType

        # 自定义消息处理器来过滤警告
        def message_handler(msg_type, context, message):
            # 忽略QFont相关的警告
            if "QFont::setPointSize" in message:
                return
            # 忽略QWidgetWindow相关的警告
            if "QWidgetWindow" in message and "must be a top level window" in message:
                return
            # 其他消息正常输出
            if msg_type == QtMsgType.QtDebugMsg:
                print(f"Debug: {message}")
            elif msg_type == QtMsgType.QtInfoMsg:
                print(f"Info: {message}")
            elif msg_type == QtMsgType.QtWarningMsg:
                print(f"Warning: {message}")
            elif msg_type == QtMsgType.QtCriticalMsg:
                print(f"Critical: {message}")
            elif msg_type == QtMsgType.QtFatalMsg:
                print(f"Fatal: {message}")

        # 安装消息处理器
        qInstallMessageHandler(message_handler)

        # 先导入PyQt6的所有必要模块，确保使用的是PyQt6
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtCore import Qt, QObject, pyqtSlot, QUrl, QTimer

        # 在主线程中创建QApplication实例
        app = QApplication.instance()

        # 如果不存在QApplication实例，才设置HighDpi相关参数并创建实例
        if app is None:
            # 这些设置必须在创建QApplication实例之前调用
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
            app = QApplication(sys.argv)

        # 设置Qt属性以避免QWidgetWindow警告
        app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)

        # 提前加载lightweight_charts.widgets模块，确保在创建任何QWidget实例之前，Qt的模块已经被正确初始化
        import lightweight_charts.widgets

        w = MainWindow(strategies, period_milliseconds)
        w.show()

        # 启动Qt事件循环
        app.exec()
    except Exception as e:
        import traceback
        print(f"程序崩溃，错误信息：{e}")
        traceback.print_exc()
        # 等待用户输入，以便查看错误信息
        input("按回车键退出...")
