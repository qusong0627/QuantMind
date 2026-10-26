"""回放窗口：纯 pylightcharts 的历史行情回放（对应旧的 light_chart_replay.py）。

第一版聚焦"把数据放出来 + 可控"：

* 复用已完成的静态窗口 :class:`StrategyWindow`（两层顶栏：策略 / 合约；pane 副图；
  回测信息页），所以顶栏、指标、缩放、主题、键盘都是同一套；
* 在图表顶栏上再加一排**回放控制**：`播放/暂停`（开关按钮）、`单步`、
  `速度`（1x/2x/5x/10x 下拉）、`进度`（`已放出/总数  当前时间`）；
* 时间驱动用宿主进程的 `QTimer`（pylightcharts 自己的窗口跑的就是 Qt 事件循环，
  同进程加定时器即可）；
* 每 tick 走**增量更新**（`chart.update(bar)`，一根一根放），不重发全量数据；
* 数据直接从本地 CSV 读（不依赖天勤）：`loader(strategy, contract) -> DataFrame`。

用法::

    window = ReplayWindow(strategies=('MACrossStrategy',),
                          contracts=lambda s: ('pp2609_60',),
                          loader=my_loader, start_bars=300)
    window.run()
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from minibt.strategy.strategy_window import StrategyWindow

#: 速度档位：每 tick 放出几根 K 线（避免把 QTimer 频率开得过高）
SPEEDS = {'1x': 1, '2x': 2, '5x': 5, '10x': 10}
#: 1x 时每根 K 线的间隔（毫秒）
BASE_INTERVAL_MS = 400


#: 初始资金虚线/色带的默认样式（`Config.capital_line=True` 时用它们）
_CAPITAL_COLOR = '#4FC3F7'      # 浅蓝
_CAPITAL_ALPHA = 0.12           # 透明度低一点


class ReplayWindow:
    """历史行情回放：一个 :class:`StrategyWindow` + 回放控制条。"""

    def __init__(self, strategies: Sequence[str],
                 contracts: Callable[[str], Sequence[str]],
                 loader: Callable[[str, str], pd.DataFrame],
                 *, start_bars: int = 300,
                 width: int = 1280, height: float = 0.92,
                 title: str = 'pylightcharts - 策略回放',
                 panes: Optional[Dict[str, Sequence[str]]] = None,
                 backtest: Optional[Callable[[pd.DataFrame], Dict]] = None,
                 refresh_every: int = 25,
                 on_close: Optional[Callable[[], None]] = None,
                 browser: bool = False, port: int = 0,
                 jupyter: bool = False,
                 indicators_of: Optional[Callable[[str, str], list]] = None,
                 theme: str = 'dark',
                 toolbox: bool = False,
                 grid: bool = False,
                 y_margin: Optional[tuple] = None):
        self.start_bars = int(start_bars)
        self.loader = loader
        self._jupyter = bool(jupyter)
        # 从构造起就让 StrategyWindow 只看到前 start_bars 根：否则它先 set 全量
        # 数据，reset() 再重设一遍，窗口要白等一次大序列化
        def initial_loader(strategy: str, contract: str) -> pd.DataFrame:
            frame = loader(strategy, contract).reset_index(drop=True)
            return frame.iloc[:self.start_bars]

        self.window = StrategyWindow(strategies, contracts, width=width,
                                     height=int(880 * height), title=title,
                                     loader=initial_loader, panes=panes,
                                     browser=browser, live=True, port=port,
                                     jupyter=jupyter, theme=theme,
                                     toolbox=toolbox, grid=grid,
                                     y_margin=y_margin)
        self.chart = self.window.chart
        self.on_close = on_close
        self.speed = '1x'
        self.index = 0
        self.frame: Optional[pd.DataFrame] = None
        self.playing = False
        self._timer = None
        self._thread = None
        self._interval = BASE_INTERVAL_MS
        self._key = None
        #: 持有 QApplication 引用：否则它会被 GC 掉，之后 startTimer 会报
        #: "current thread's event dispatcher has already been destroyed"
        self._app = None
        #: `backtest(已放出的行情) -> set_backtest(...) 的那份字典`；
        #: 给了它，回放过程中"回测结果 / 回测指标 交易记录"两页会跟着增长
        self.backtest = backtest
        #: 每放出多少根刷新一次信息页（重算回测是 O(n)，不能每根都做）
        self.refresh_every = int(refresh_every)
        self._refreshed_at = -1
        #: 指标线提供者（`strategy, contract -> [IndicatorSpec]`）；给了就在回放中建指标
        self.indicators_of = indicators_of
        self._indicator_manager = None
        self._signals: list = []          # 兼容旧字段：SignalManager 的序列
        #: 信号管理器：按名字**复用**序列（每根重建会让主图自动缩放重算 -> 抖动）
        self._signal_manager = None
        #: K 线主图上叠加的权益曲线（左刻度）；None = 还没建
        self._equity_series = None
        #: 初始资金水平虚线（左刻度）；None = 还没建
        self._capital_series = None
        #: 权益色带的上下两条 Area（base_value = 初始资金）；None = 还没建
        self._equity_areas: dict = {}
        self._band_len = 0
        #: 是否在 K 线主图上叠加权益曲线：由策略 `Config.equity_overlay`
        #: 控制（默认 False），这里不再单独开关
        #: 双刻度各占的高度：right=(上, 下)、left=(上, 下)
        self.equity_margins = (0.2, 0.2, 0.2, 0.2)
        if indicators_of is not None:
            from minibt.strategy.realtime import IndicatorManager, SignalManager
            # ★ 回放：**所有合约**的指标行不挂图例齿轮 —— 回测结果是按当前
            #   指标参数跑出来的，回放里改参数没有意义（信号/成交已经录好了 ✗）。
            #   图例行 + 眼睛（显/隐）开关仍然保留 ✓
            self._indicator_manager = IndicatorManager(self.chart,
                                                       legend_menu=False)
            self._signal_manager = SignalManager(self.chart)
        self._build_controls()
        self.window._apply_topbar_gap()    # 新建的「播放/单步/速度」也要 5px
        self.reset()
        # 回测结果/指标记录页不显示指标（先清指标，再建回测 pane，避免 pane 索引错位）
        from minibt.strategy.strategy_window import INFO_PAGES
        original_set_contract = self.window.set_contract

        def set_contract(contract: str, force: bool = False) -> None:
            # 页面还没加载完就再点切换：只记下最后一次点击，这次完成后再补切。
            # 直接放行会让两批脚本交错（图表跑到别的页面 / 重复出现）。
            if getattr(self, '_switching_page', False):
                self._pending_contract = contract
                return
            self._switching_page = True
            try:
                _switch(contract, force)
            finally:
                self._switching_page = False
            pending = getattr(self, '_pending_contract', None)
            self._pending_contract = None
            if pending is not None and pending != self.window.contract:
                set_contract(pending)

        def _switch(contract: str, force: bool = False) -> None:
            if contract in INFO_PAGES and self._indicator_manager is not None:
                # 回测结果页保留止损/止盈线（其余指标清掉）——和回测模式的
                # `RealtimeChart` 一致，方便信号与回测曲线对照
                from minibt.strategy.realtime import (INFO_KEEP_NAMES,
                                                      INFO_KEEP_SOURCES)
                self._indicator_manager.keep_only(INFO_KEEP_SOURCES,
                                                  INFO_KEEP_NAMES)
                self._hide_indicator_tables()
            original_set_contract(contract, force=force)
            if contract not in INFO_PAGES:
                self._build_indicators()
            else:
                self._apply_info_decorations()
            self._reset_equity_overlay()       # 换合约/策略：权益曲线重建

        self.window.set_contract = set_contract

    # ---------------------------------------------------------------- 控制
    def _build_controls(self) -> None:
        bar = self.chart.topbar
        # 都靠右：顺序 = 播放 / 单步 / 速度，正好在顶栏右侧"百分比进度"左边
        bar.button('replay_play', '播放', toggle=True, align='right',
                   func=self._on_play)
        bar.button('replay_step', '单步', align='right',
                   func=lambda chart: self.step())
        bar.menu('replay_speed', tuple(SPEEDS), default='1x', align='right',
                 func=self._on_speed)

    def _on_play(self, chart) -> None:
        """播放/暂停（开关按钮：`value` 为 True 表示打开）。"""
        self.play() if self.chart.topbar['replay_play'].value else self.pause()

    def _on_speed(self, chart) -> None:
        self.speed = self.chart.topbar['replay_speed'].value
        if self.playing:
            self.play()                      # 用新间隔重启定时器

    @staticmethod
    def _qt_modules():
        """pylightcharts 实际使用的那个 Qt 绑定（`QTimer` + `QApplication`）。

        不能写死 `from PyQt6...`：如果 pylightcharts 选的是 PySide6，PyQt6 里
        `QApplication.instance()` 会返回 None —— 表现就是"单步能用、播放没反应"。
        这里通过 `pylightcharts.widgets._resolve('QTimer')` 拿到同一个绑定，
        再由它的模块名推出同一套 QtWidgets。
        """
        import sys
        try:
            from pylightcharts.widgets import _resolve
            QTimer = _resolve('QTimer')                  # 与图表同一个绑定
            package = QTimer.__module__.rsplit('.', 1)[0]
            QApplication = sys.modules[f'{package}.QtWidgets'].QApplication
            return QTimer, QApplication
        except Exception:                                # 退路：按 PyQt6 找
            from PyQt6.QtCore import QTimer
            from PyQt6.QtWidgets import QApplication
            return QTimer, QApplication

    def _qt_app(self):
        """当前的 QApplication（没有就返回 None：事件循环还没起来）。"""
        if self._app is not None:
            return self._app
        try:
            _QTimer, QApplication = self._qt_modules()
            self._app = QApplication.instance()
        except Exception as error:
            print('[replay] 找不到 QApplication:', error)
        return self._app

    def play(self) -> None:
        """开始/继续回放。"""
        if not getattr(self.chart, 'is_alive', True):
            self.playing = False            # 窗口已关闭
            return
        if self.frame is None or self.index >= len(self.frame):
            return
        self.playing = True
        interval = max(16, int(BASE_INTERVAL_MS / SPEEDS.get(self.speed, 1)))
        self._interval = interval
        app = self._qt_app()
        if app is not None:
            if self._timer is None:
                # 以 app 为 parent：timer 的生命周期跟着事件循环，也不会被 GC
                QTimer, _QApplication = self._qt_modules()
                self._timer = QTimer(app)
                self._timer.timeout.connect(self.step)
            self._timer.start(interval)
            return
        # pylightcharts 的窗口可能开在**别的进程**里（那时当前进程没有
        # QApplication，Qt 定时器没处挂），退回 stdlib 线程定时器：每 `interval`
        # 毫秒在自己的线程里走一步，JS 更新照常通过桥接排队发出去。
        import threading
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._thread_loop,
                                            name='replay-timer', daemon=True)
            self._thread.start()

    def _thread_loop(self) -> None:
        """没有 QApplication 时的播放循环（stdlib 线程，daemon）。"""
        import time
        while self.playing:
            time.sleep(max(0.016, self._interval / 1000.0))
            if not self.playing:
                break
            if not getattr(self.chart, 'is_alive', True):
                self.playing = False      # 窗口已关：退出线程
                break
            try:
                self.step()
            except Exception as error:          # 出问题别把线程静默带走
                print('[replay] 步进出错，已暂停:', error)
                self.playing = False
                break

    def pause(self) -> None:
        """暂停回放（位置保留，可继续）。"""
        self.playing = False
        if self._timer is not None:
            self._timer.stop()                  # QTimer 路线
        self._thread = None                     # 线程路线：看到 playing=False 自行退出
        self._refresh_backtest(force=True)      # 暂停时把信息页对齐到当前位置

    def step(self) -> None:
        """放出一批 K 线（根数由速度档位决定）。"""
        if not getattr(self.chart, 'is_alive', True):
            self.pause()                     # 窗口已被关闭：停掉定时器/线程
            return
        if self.frame is None:
            return
        count = SPEEDS.get(self.speed, 1)
        for _ in range(count):
            if self.index >= len(self.frame):
                self.pause()
                self.chart.fit()
                return
            bar = self.frame.iloc[self.index]
            self.chart.update(bar)           # 增量：一根一根放
            self.index += 1
        self._reslice_indicators()           # 指标跟着 K 线长度推进
        self._refresh_backtest()
        self._show_progress()

    def reset(self) -> None:
        """回到起点：显示前 `start_bars` 根，其余等着被放出来。"""
        self.pause()
        self._key = (self.window.strategy, self.window.contract)
        frame = self.loader(self.window.strategy, self.window.contract)
        self.frame = frame.reset_index(drop=True)
        self.index = min(self.start_bars, len(self.frame))
        # 窗口后续任何"重发数据"（切策略、切页面、切合约）都只能拿到已经放出的
        # 那一段 —— 否则引擎里会留着全量数据，之后 update() 就会报
        # "Cannot update oldest data"
        self.window.loader = self._slice_loader
        self.chart.set(self.frame.iloc[:self.index])
        self._build_indicators()
        self.chart.fit()
        self._refresh_backtest(force=True)
        self._show_progress()

    def _slice_loader(self, strategy: str, contract: str) -> pd.DataFrame:
        """给窗口用的 loader：只返回已经放出的那 `self.index` 根。

        * **信息页**（contract 是页面名）沿用当前时间线，不参与键比较 ——
          切页面不是换合约，否则会把进度重置回起点，已放出的 K 线整段消失
          （"K 线回退"，且 K 线页与回测结果页不再同步）。
        * 真的换合约时也**不清零进度**：只把 index 夹到该合约的长度范围内，
          多个合约共用同一个回放时钟，切回来不会倒退。
        """
        from minibt.strategy.strategy_window import INFO_PAGES
        key = (strategy, contract)
        if contract in INFO_PAGES and self._key is not None:
            key = self._key                     # 信息页沿用当前时间线
        main = getattr(self.window, '_kline_contract', None)
        if main and contract != main and key != self._key:
            # 副图里的**其余合约**：只给它的数据切片，**不要**改回放时钟。
            # 否则主图接下来会被 `chart.update()` 推成这个合约的 K 线 ——
            # 表现就是“第一个 K 线图更新的是第二个合约的数据”。
            try:
                frame = self.loader(strategy, contract).reset_index(drop=True)
            except Exception:                # 取不到就退回当前时间线
                if self.frame is None:
                    return pd.DataFrame()
                return self.frame.iloc[:self.index]
            return frame.iloc[:min(self.index, len(frame))]
        if key != self._key:
            self._key = key
            frame = self.loader(strategy, contract).reset_index(drop=True)
            self.frame = frame
            self.index = max(self.start_bars, min(self.index, len(frame)))
            self._refreshed_at = -1
            self._show_progress()
        if self.frame is None:
            return pd.DataFrame()
        return self.frame.iloc[:self.index]

    def _build_indicators(self) -> None:
        """在回放数据上重建策略指标（同 RealtimeChart）。"""
        if self._indicator_manager is None or self.indicators_of is None:
            return
        frame = self.chart.candle_data
        if frame is None or frame.empty:
            return
        try:
            specs = self.indicators_of(self.window.strategy, self.window.contract)
            from minibt.strategy.realtime import palette_for, apply_watermark
            self._indicator_manager.build(frame, specs, palette_for(self.chart))
            self.window._apply_pane_stretch()
            strategy = self._strategy_by_name(self.window.strategy)
            if strategy is not None:
                from minibt.strategy.realtime import (
                    apply_bands, apply_candlestyle, apply_color_rules,
                    apply_histogram_colors, apply_indicator_tables,
                    apply_spans,
                    extract_minibt_bands, extract_minibt_candlestyle,
                    extract_minibt_spans, strategy_contracts,
                    strategy_watermark)
                contract = self.window.contract
                try:
                    plot_id = strategy_contracts(strategy).index(contract)
                except ValueError:
                    plot_id = 0
                # 修：回放路径原来漏了柱体配色 / 逐点配色 / 色带 / 水平线
                # （只有实时那边叫了），所以 replay 里 macd 柱子颜色不生效。
                apply_histogram_colors(self._indicator_manager, strategy,
                                       contract, len(frame), frame=frame)
                apply_color_rules(self.chart, self._indicator_manager,
                                  strategy, contract)
                apply_bands(
                    self._indicator_manager,
                    extract_minibt_bands(strategy, plot_id))
                self.spans = apply_spans(
                    self.chart, extract_minibt_spans(strategy, plot_id),
                    self._indicator_manager)
                apply_watermark(self.chart, strategy_watermark(
                    strategy, contract))
                apply_candlestyle(
                    self.chart,
                    extract_minibt_candlestyle(strategy, contract))
                try:
                    self.chart.clear_markers()
                except Exception:
                    pass
                self._refresh_signals(frame)
                # 指标自带的统计表格（同回测/实时）：可拖动 + 跟随十字光标。
                # 回放是“前 N 根”，所以指标值要按 head 对齐（取全量的前 N 个），
                # 否则表格会一直显示全量最后一根的值。
                self._indicator_tables = apply_indicator_tables(
                    self.chart, strategy, contract,
                    getattr(self, '_indicator_tables', None), plot_id=plot_id,
                    align=getattr(self._indicator_manager, 'align', 'head'))
        except Exception as error:
            print('[replay] 建指标失败:', error)

    def _refresh_indicator_tables(self) -> None:
        """回放推进后刷新指标表格（显示已放出最后一根的值）。"""
        refresh = getattr(self.chart, '_refresh_indicator_tables', None)
        if refresh is None:
            return
        try:
            refresh(getattr(self.chart, '_indicator_table_cursor', None))
        except Exception as error:
            print('[replay] 指标表格刷新失败:', error)

    def _hide_indicator_tables(self) -> None:
        """隐藏所有指标表格（回测结果/指标记录页）。"""
        for table in (getattr(self, '_indicator_tables', None) or {}).values():
            try:
                table.visible(False)
            except Exception:
                pass

    # ------------------------------------------------- 策略对象注入（后置）
    @property
    def _strategy_lookup(self):
        return getattr(self, '_lookup_fn', None)

    @_strategy_lookup.setter
    def _strategy_lookup(self, func):
        """注入"策略名 -> 策略对象"的查表函数，并立刻按初始长度补一次指标/信号。

        调用方是先构造窗口、再把策略对象塞进来的；不补这一次的话，初始化时
        主图上既没有交易信号、也没有止损/止盈线，要等第一次播放或切页才出现。
        `_build_indicators()` 是幂等的（内部先 clear 再建），重复调用安全。
        """
        self._lookup_fn = func
        if func is None:
            return
        try:
            self._build_indicators()
        except Exception as error:
            print('[replay] 注入策略后补建指标/信号失败:', error)
        try:
            self._refresh_equity_overlay()      # 开了 Config 才有这条曲线
        except Exception as error:
            print('[replay] 初始化权益曲线失败:', error)
        # 画图工具初始显示：Config.toolbox，或策略里显式 `self.toolbox = ...`
        try:
            strategy = self._strategy_by_name(self.window.strategy)
            config = getattr(strategy, 'config', None)
            explicit = getattr(strategy, '__dict__', {}).get('_toolbox')
            wanted = bool(explicit if explicit is not None
                          else getattr(config, 'toolbox', False))
            self.window.set_toolbox_visible(wanted)
        except Exception as error:
            print('[replay] 初始化画图工具失败:', error)
        self._refresh_capital_overlay()
        self._scroll_to_latest()

    def _strategy_by_name(self, name):
        """策略名 -> 策略对象（回放时不一定有，返回 None 即可）。"""
        loader = getattr(self, '_strategy_lookup', None)
        return loader(name) if loader else None

    def _refresh_signals(self, frame=None) -> None:
        """重画交易信号（唯一的创建入口：先删旧、再画新的）。

        `_build_indicators`、信息页装饰、回放推进都调它，所以"谁建谁删"始终
        是同一处，不会像之前那样攒出好几组重合的信号序列。
        """
        strategy = self._strategy_by_name(self.window.strategy)
        if strategy is None:
            return
        frame = self.chart.candle_data if frame is None else frame
        if frame is None or frame.empty:
            return
        from minibt.strategy.realtime import (
            apply_signals, extract_minibt_signal_texts,
            extract_minibt_signals, strategy_contracts)
        index = 0
        try:
            names = strategy_contracts(strategy)
            if self.window.contract in names:
                index = names.index(self.window.contract)
        except Exception:
            pass
        try:
            if self._signal_manager is not None:
                # 复用序列：同一根信号线只有一个自定义序列对象，逐根 update
                self._signal_manager.update(
                    frame, extract_minibt_signals(strategy, index),
                    extract_minibt_signal_texts(strategy, index))
                self._signals = list(self._signal_manager.series.values())
            else:
                self._clear_signals()
                self._signals = apply_signals(
                    self.chart, frame,
                    extract_minibt_signals(strategy, index),
                    extract_minibt_signal_texts(strategy, index))
        except Exception as error:
            print('[replay] 画信号失败:', error)

    def _apply_info_decorations(self) -> None:
        """信息页的 K 线装饰：K 线配色 / 水印 / 交易信号。

        指标线在信息页上只留止损/止盈，但信号要一直画着 —— 播放时它跟着
        已放出的行情逐根更新，和权益/回撤曲线对照着看才完整。
        """
        frame = self.chart.candle_data
        if frame is None or frame.empty:
            return
        strategy = self._strategy_by_name(self.window.strategy)
        if strategy is not None:
            from minibt.strategy.realtime import (
                apply_candlestyle, apply_watermark,
                extract_minibt_candlestyle, strategy_watermark)
            try:
                apply_candlestyle(
                    self.chart,
                    extract_minibt_candlestyle(strategy,
                                               self.window.contract))
                apply_watermark(
                    self.chart,
                    strategy_watermark(strategy, self.window.contract))
            except Exception as error:
                print('[replay] 信息页装饰失败:', error)
        self._refresh_signals(frame)

    def _clear_signals(self) -> None:
        """删掉上一次画的交易信号（自定义序列），切策略/合约时用。"""
        manager = getattr(self, '_signal_manager', None)
        if manager is not None:
            manager.clear()
            self._signals = []
            return
        for series in getattr(self, '_signals', []):
            try:
                series.delete()
            except Exception:
                pass
        self._signals = []

    def _reslice_indicators(self) -> None:
        """回放推进时把指标值重新切片到最新长度，并重画交易信号。

        信号按"已放出的那一段"重算，所以播放时它会随着实际成交逐根出现，
        止损/止盈线也一起跟着延长。
        """
        if self._indicator_manager is None:
            return
        frame = self.chart.candle_data
        try:
            self._indicator_manager.reslice(
                frame, previous=getattr(self, '_indicator_len', 0))
            self._indicator_len = 0 if frame is None else len(frame)
        except Exception as error:
            print('[replay] 指标推进失败:', error)
        self._refresh_signals()
        self._refresh_indicator_tables()

    def _refresh_equity_overlay(self, data: Optional[dict] = None) -> None:
        """在 K 线主图上叠加权益曲线（左刻度），随回放一起长。

        `add_symbol(..., scale='left')` 会给它一条独立的价格刻度，K 线仍在
        右刻度；再用 `scale_margins` 控制两条刻度各占多少高度。

        只在策略开了 `Config(equity_overlay=True)` 时才画（默认关闭）。
        """
        from minibt.strategy.strategy_window import INFO_PAGES
        if self.window.contract in INFO_PAGES:
            # 回测结果页本来就有权益曲线，双坐标是重复的；而且那里的 pane
            # 重建会换掉主图系列句柄，叠加序列跟着报 null
            self._drop_equity_overlay()
            return
        strategy = self._strategy_by_name(self.window.strategy)
        if not getattr(getattr(strategy, 'config', None),
                       'equity_overlay', False):
            return                      # 策略没开 `Config(equity_overlay=True)`
        if data is None and self.backtest is not None and self.frame is not None:
            released = self.frame.iloc[:self.index] if self.index else self.frame
            try:
                data = self.backtest(released.reset_index(drop=True))
            except Exception:
                data = None
        equity = (data or {}).get('equity')
        if equity is None or len(equity) == 0:
            return
        frame = pd.DataFrame({'time': self._epoch_seconds(equity['time']),
                              '权益': equity['value'].to_numpy()})
        try:
            series = self._equity_series
            if series is None:
                top, bottom, left_top, left_bottom = self.equity_margins
                # 图表级先声明左刻度可见（教程里的写法；per-scale 的那条
                # `add_symbol` 内部也会发，两处都留着更稳）
                try:
                    self.chart.apply_options(leftPriceScale={
                        'visible': True, 'borderVisible': True})
                except Exception:
                    pass
                try:
                    series = self.chart.add_symbol(
                        '权益', frame, kind='line', scale='left',
                        value_column='权益', color='#FFB74D', line_width=2,
                        margins=(left_top, left_bottom),
                        last_value_visible=True)   # 左轴上标出当前权益
                except Exception as error:
                    # 建失败（快速连点切页时偶发）：清掉句柄，下次刷新重建，
                    # 免得后面一直往一个已经不存在的系列上发脚本
                    self._equity_series = None
                    print('[replay] 权益曲线创建失败，稍后重试:', error)
                    return
                self._equity_series = series
                self.chart.scale_margins(right=(top, bottom),
                                         left=(left_top, left_bottom))
                # 双刻度（左=权益、右=K线）的上下余量归 `config` 之外的
                # `y_margin` 管：这里再套一次，免得切回合约后主图跟指标副图
                # 的留白不一致（y_margin 默认 0.03，两边同值，叠加关系不变）
                try:
                    self.window._apply_y_margin()
                except Exception:
                    pass
                self._equity_len = len(frame)
                return
            elif not getattr(self, '_equity_len', 0):
                series.set(frame)                 # 刚换了合约/策略：整段换
            elif len(frame) <= self._equity_len:
                series.set(frame)
            else:
                # 只补新增的点：整段 setData 会让左刻度重算并播缩放动画，
                # 叠加在 K 线主图上就会被看成"图表先放大再缩回"
                for index in range(self._equity_len, len(frame)):
                    row = frame.iloc[index]
                    series.update(pd.Series({'time': row['time'],
                                             '权益': row['权益']}))
            self._equity_len = len(frame)
            # LWC 在某个刻度上最后一个序列被移除时会把刻度一起收走，
            # 这里再显式保证左刻度可见（初始化那一闪就是被收走了）
            try:
                self.chart.get_price_scale('left').apply_options(
                    visible=True, border_visible=True)
            except Exception:
                pass
        except Exception as error:
            print('[replay] 叠加权益曲线失败:', error)

    def _scroll_to_latest(self) -> None:
        """最后一根 **不可见** 时才把视口贴到右端。

        LWC 只在贴右端时自动跟随新数据，所以切合约后如果最后一根在视野外要
        贴过去；但如果它已经在视野内（用户可能特意留了一段右端空白），就
        保持不动，免得覆盖掉 `_restore_view` 刚恢复的位置。

        窗口还没加载完时**直接返回**：这时读 `getVisibleLogicalRange` 会拿不到
        回包（`No reply from the webview`），白等一次超时 —— 首屏启动变慢。
        """
        if not getattr(self.chart.win, 'loaded', False):
            return
        try:
            logical = self.chart.get_visible_logical_range()
            total = len(self.chart.candle_data)
            if (logical and total and logical.get('to') is not None
                    and int(round(logical['to'])) >= total - 1):
                return
            self.chart.scroll_to_real_time(animated=False)
        except Exception as error:
            print('[replay] 视口贴右端失败:', error)

    @staticmethod
    def _with_alpha(colour: str, alpha: float) -> str:
        """`#RRGGBB` + 透明度 -> `#RRGGBBAA`（其它写法原样返回）。"""
        text = str(colour).strip()
        if not (text.startswith('#') and len(text) == 7):
            return text
        return f'{text}{max(0, min(255, round(float(alpha) * 255))):02x}'

    @staticmethod
    def _epoch_seconds(times):
        """时间列 -> epoch 秒（int）。

        权益/色带的数据有时来自 `strategy_backtest_dict` 的 `pd.date_range`
        兜底（Timestamp 对象），直接下发会变成 `[object Object]`，引擎就报
        `Cannot update oldest data`。
        """
        series = pd.Series(list(times))
        if pd.api.types.is_datetime64_any_dtype(series):
            return (series.astype('int64') // 10 ** 9).to_numpy()
        try:
            return np.asarray(series, dtype='int64')
        except (TypeError, ValueError):
            stamps = pd.to_datetime(series)          # 字符串时间
            return (stamps.astype('int64') // 10 ** 9).to_numpy()

    def _refresh_equity_band(self, values, times, capital: float,
                             options: dict) -> None:
        """权益曲线与初始资金之间的色带（两条 Area 夹紧，无 primitive）。"""
        colour = (options.get('color') or options.get('fill_color')
                  or _CAPITAL_COLOR)
        alpha = options.get('opacity', options.get('alpha',
                  options.get('fill_alpha', _CAPITAL_ALPHA)))
        alpha = max(0.0, min(1.0, float(alpha)))
        clips = {'up': np.maximum, 'down': np.minimum}
        for suffix, clip in clips.items():
            series = self._equity_areas.get(suffix)
            data = pd.DataFrame({'time': times,
                                 'value': clip(values, capital)})
            try:
                if series is None:
                    series = self.chart.add_series(
                        'Area', name='', pane_index=0, price_scale_id='left',
                        base_value=float(capital),
                        line_color=colour, line_width=1,
                        top_color=self._with_alpha(colour, alpha),
                        bottom_color=self._with_alpha(colour, alpha * 0.4),
                        price_line_visible=False, last_value_visible=False,
                        legend=False)
                    self._equity_areas[suffix] = series
                    series.set(data)
                    continue
                if len(data) <= getattr(self, '_band_len', 0):
                    series.set(data)
                else:
                    for index in range(getattr(self, '_band_len', 0), len(data)):
                        series.update(pd.Series(
                            {'time': data['time'].iloc[index],
                             'value': data['value'].iloc[index]}))
            except Exception as error:
                print('[replay] 权益色带失败:', error)
        self._band_len = len(values)

    def _drop_equity_band(self) -> None:
        """删掉色带的两条 Area。"""
        for series in list(getattr(self, '_equity_areas', {}).values()):
            try:
                series.delete()
            except Exception:
                pass
        self._equity_areas = {}
        self._band_len = 0

    def _refresh_capital_overlay(self, frame=None) -> None:
        """初始资金水平虚线；与权益曲线同时开启时在两者之间填色带。

        `Config.capital_line` 为 True 用默认样式（浅蓝、透明度低）；为 dict 时
        `{'color': .., 'opacity': ..}`（也接受 fill_color/fill_alpha）决定色带。
        """
        from minibt.strategy.strategy_window import INFO_PAGES
        strategy = self._strategy_by_name(self.window.strategy)
        config = getattr(strategy, 'config', None)
        setting = getattr(config, 'capital_line', False)
        if self.window.contract in INFO_PAGES or not setting:
            self._drop_capital_overlay()
            return
        capital = float(getattr(config, 'value', 0.0) or 0.0)
        options = setting if isinstance(setting, dict) else {}
        data = self.chart.candle_data if frame is None else frame
        if data is None or data.empty:
            return
        flat = pd.DataFrame({'time': self._epoch_seconds(data['time']),
                             '初始资金': [capital] * len(data)})
        try:
            series = self._capital_series
            if series is None:
                # LWC 里刻度要先可见，序列才能挂上去（只开 capital_line、
                # 不叠权益时左刻度还没被声明过）
                try:
                    self.chart.get_price_scale('left').apply_options(
                        visible=True, border_visible=True)
                except Exception:
                    pass
                # 注意 `Line` 的参数名：style/width/price_line/price_label，
                # 用 line_style/line_width/... 会被塞进 **options 直接下发成
                # 非法选项（`lineStyle: "dashed"`），序列就建不出来。
                series = self.chart.add_series(
                    'Line', name='初始资金', price_scale_id='left',
                    color=(options.get('color') or options.get('line_color')
                           or _CAPITAL_COLOR),
                    style='dashed', width=2,
                    price_line=False, price_label=False,
                    legend=False)          # 数值不变，图例里不必出现这一行
                self._capital_series = series
            series.set(flat)
        except Exception as error:
            print('[replay] 初始资金虚线失败:', error)
            return
        self._refresh_capital_band(options)

    def _refresh_capital_band(self, options) -> None:
        """需要时刷新权益色带：`equity_overlay` + `capital_line` 都开启。"""
        config = getattr(self._strategy_by_name(self.window.strategy),
                         'config', None)
        if (self._equity_series is None
                or not getattr(config, 'equity_overlay', False)
                or not getattr(config, 'capital_line', False)):
            self._drop_equity_band()
            return
        equity = self._equity_series.data
        if equity is None or equity.empty:
            return
        # `add_symbol` 会把值列规整成 `value`，自己建的才是 `权益`
        column = '权益' if '权益' in equity.columns else 'value'
        if column not in equity.columns:
            return
        self._refresh_equity_band(
            equity[column].to_numpy(dtype=float),
            self._epoch_seconds(equity['time']),
            float(getattr(config, 'value', 0.0) or 0.0), options)

    def _drop_capital_overlay(self) -> None:
        """删掉初始资金虚线（进信息页 / 参数关掉时）。"""
        series = getattr(self, '_capital_series', None)
        if series is None:
            return
        try:
            series.delete()
        except Exception:
            pass
        self._capital_series = None

    def _drop_equity_overlay(self) -> None:
        """删掉叠加的权益曲线（进信息页时用；回到 K 线页会自动重建）。"""
        series = getattr(self, '_equity_series', None)
        if series is None:
            return
        try:
            series.delete()
        except Exception:
            pass
        self._equity_series = None
        self._equity_len = 0
        try:
            # 建曲线时在图表级声明过 `leftPriceScale.visible=True`，序列删掉后
            # 要把这个声明收回去，否则引擎仍给左轴留一条空白宽度
            self.chart.apply_options(leftPriceScale={'visible': False})
        except Exception:
            pass

    def _reset_equity_overlay(self) -> None:
        """切策略/合约：权益曲线**不删不建**，只标记数据要整段换。

        删掉再建会让它所在的价格刻度（左轴）被引擎收起 —— 初始化时表现为
        "左轴闪一下然后不见了"。序列常驻，下一轮刷新 `set()` 整段数据即可。
        """
        if getattr(self, '_equity_series', None) is None:
            self._refresh_equity_overlay()
        else:
            self._equity_len = 0
            self._drop_equity_band()
            self._refresh_equity_overlay()
        self._refresh_capital_overlay()

    def _refresh_backtest(self, force: bool = False) -> None:
        """按“已放出的行情”刷新回测信息页（节流：每 `refresh_every` 根一次）。"""
        if not getattr(self.chart, 'is_alive', True):
            return                        # 窗口已关：不要再推脚本
        if self.backtest is None or self.frame is None or self.index == 0:
            return
        if not force and self.index - self._refreshed_at < self.refresh_every:
            return
        released = self.frame.iloc[:self.index]
        try:
            data = self.backtest(released.reset_index(drop=True))
        except Exception as error:           # 数据太短/没产生交易：先跳过
            print('[replay] 回测刷新跳过:', error)
            return
        self.window.set_backtest(strategy=self.window.strategy, **data)
        self._refresh_equity_overlay(data)          # K 线主图上的权益曲线
        self._refresh_capital_overlay()             # 初始资金虚线 + 色带
        self._refreshed_at = self.index

    def _set_progress_text(self) -> None:
        """第二个顶栏右侧：`已放出/总数（百分比）`。"""
        total = 0 if self.frame is None else len(self.frame)
        if not total:
            return
        percent = self.index / total * 100.0
        try:
            self.window._set_progress(f'{self.index}/{total}'
                                      f'（{percent:.2f}%）')
        except Exception as error:
            print('[replay] 进度显示失败:', error)

    def _show_progress(self) -> None:
        """更新第二个顶栏右侧的进度：`已放出/总数（百分比）`。"""
        self._set_progress_text()

    # ------------------------------------------------------------------ 运行

    def run(self) -> None:
        import os
        import threading
        import time

        if self._jupyter:
            # Jupyter：启动本地 SSE 服务 + iframe，不阻塞（也不要 watchdog，
            # 否则会把整个 kernel 一起 os._exit 掉）。回放由控件/线程驱动。
            self.window.show(block=False)
            return

        def watchdog() -> None:
            """窗口一关就强制退出：pywebview 的 Windows 消息循环会卡住主线程，
            光靠 show(block=True) 返回 / Ctrl+C 不一定退得掉。"""
            while True:
                time.sleep(0.5)
                if not getattr(self.chart, 'is_alive', True):
                    self.playing = False
                    time.sleep(1.0)
                    print('[replay] 窗口已关闭，退出')
                    os._exit(0)

        threading.Thread(target=watchdog, daemon=True,
                         name='replay-watchdog').start()
        try:
            self.chart.show(block=True)
        except KeyboardInterrupt:
            pass
        finally:
            self.pause()                     # 关窗 / Ctrl+C 后停掉定时器/线程
            try:
                self.chart.exit()            # 销毁窗口，确保进程能退出
            except Exception:
                pass

    def close(self) -> None:
        self.pause()
        if self.on_close is not None:
            self.on_close()
