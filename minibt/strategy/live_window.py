"""实时图表：纯 pylightcharts 的实时行情窗口（**没有**回测页）。

与静态/回放窗口同一套顶栏与 pane，但只关心"行情在跑"：

    ┌────────── 页面顶栏 ──────────┐
    │ 策略: [ A ] [ B ]             │
    ├────────── 图表顶栏 ──────────┤
    │ 合约: [pp2609_60 ▾]           │
    │ 实时: [跟随最新] [回到最新]   12:31 6900.0   │
    ├──────────────────────────────┤
    │ K 线 + 指标副图（pane）                    │
    └──────────────────────────────┘

用法::

    window = LiveWindow(strategies=('MACrossStrategy',),
                        contracts=lambda s: ('pp2609_60',),
                        loader=my_loader, initial_bars=800)
    # 数据源（天勤回调 / 任意行情）喂进来：
    window.on_tick(time, price)                 # 盘中 tick：合并到当前 K 线
    window.on_bar(row)                          # 收线：追加一根完整 K 线
    window.on_bars(frame)                       # 一次补一批（回补/断线重连）
    window.run()                                # 阻塞显示
    # 或者自己管窗口：window.show(block=False) / window.chart…

设计要点：

* **数据入口统一成"喂一根/一批"**，不关心数据来自天勤、CSV 还是回放；
* **跟随最新**默认打开：每次更新把视口带到最后（`scroll_to_real_time`），
  用户自己滚动查看历史时点一下开关就能停住，`回到最新` 一键拉回；
* tick 走 `chart.update_from_tick(...)`（仓库里已验证的"合并高开低收"路径，
  时间戳单位也由它统一处理，避免 1970 问题）；收线走 `chart.update(bar)`；
* 传 `history_loader(count, before)` 时自动接上 pylightcharts 的
  `infinite_history`：向左滚动会自己补更早的数据；
* 顶栏可切策略/合约（切换时会重新 `set` 该策略合约的数据）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence, Union, TYPE_CHECKING

from jsonschema import TypeChecker
import pandas as pd

from minibt.strategy.strategy_window import StrategyWindow

if TYPE_CHECKING:
    from .strategy import Strategy


class LiveWindow:
    """实时行情窗口：顶栏（策略 / 合约）+ 实时控制 + K 线与指标 pane。"""

    def __init__(self, strategies: Sequence[str],
                 contracts: Callable[[str], Sequence[str]],
                 loader: Callable[[str, str], pd.DataFrame],
                 *, initial_bars: int = 800,
                 width: int = 1280, height: int = 880,
                 title: str = 'pylightcharts - 实时图表',
                 panes: Optional[Dict[str, Sequence[str]]] = None,
                 overlays: Optional[Dict[str, Sequence[str]]] = None,
                 follow: bool = True,
                 history_loader: Optional[Callable[[int, Any], pd.DataFrame]] = None,
                 theme: str = 'dark',
                 toolbox: bool = False,
                 grid: bool = False):
        self.strategies = list(strategies)
        self.contracts_of = contracts
        self.loader = loader
        self.initial_bars = int(initial_bars)
        self.follow = bool(follow)
        self.history_loader = history_loader
        #: 复用一个"没有信息页"的策略窗口：顶栏、pane、主题、键盘都是同一套
        self.window = StrategyWindow(self.strategies, contracts, width=width,
                                     height=height, title=title, loader=loader,
                                     panes=panes, overlays=overlays,
                                     info_pages=False, theme=theme,
                                     toolbox=toolbox, grid=grid)
        self.chart = self.window.chart
        self.last_price: Optional[float] = None
        self.last_time: Any = None
        self._switching = False
        self._build_controls()
        self._prepare_initial()
        self._watch_range()
        if history_loader is not None:
            self._attach_history()

    # ---------------------------------------------------------------- 控制
    def _build_controls(self) -> None:
        bar = self.chart.topbar
        bar.button('live_follow', '跟随最新', toggle=True, func=self._on_follow)
        bar.button('live_now', '回到最新', func=lambda chart: self.jump_to_now())
        bar.textbox('live_status', '等待行情', align='right')

    def _on_follow(self, chart) -> None:
        self.follow = bool(self.chart.topbar['live_follow'].value)
        if self.follow:
            self.jump_to_now()

    def jump_to_now(self) -> None:
        """把视口拉回最新（`End` 键、`回到最新` 按钮都是它）。"""
        try:
            self.chart.run_script(
                f'{self.chart.id}.timeScale.scrollToRealTime()')
        except Exception as error:
            print('[live] 回到最新失败:', error)

    def _watch_range(self) -> None:
        """用户自己滚走看历史时自动停掉"跟随最新"（再点开关恢复）。

        用图表的 `events.range_change`（就是 `barsInLogicalRange().barsAfter`）：
        右边还剩很多根说明人已经滚到左边去了 -> 暂停跟随。
        """
        try:
            self.chart.events.range_change += self._on_range
        except Exception as error:
            print('[live] 监听可视范围失败:', error)

    def _on_range(self, chart, bars_before=None, bars_after=None) -> None:
        if bars_after is None or not self.follow:
            return
        try:
            if float(bars_after) > 3:            # 视口已经离最新很远
                self.follow = False
                self._switching = True
                try:
                    self.chart.topbar['live_follow'].set('')
                except Exception:
                    pass                         # 按钮只是视觉提示，失败不影响
                finally:
                    self._switching = False
                print('[live] 你已滚离最新，自动暂停跟随（点「回到最新」恢复）')
        except (TypeError, ValueError):
            pass

    def cleanup(self) -> None:
        """收尾：停跟随、摘下范围监听（窗口销毁前调）。"""
        self.follow = False
        try:
            self.chart.events.range_change.unsubscribe()
        except Exception:
            pass

    def _status(self) -> None:
        text = '等待行情'
        if self.last_price is not None:
            text = f'{self.last_time}  {self.last_price:g}'
        self.chart.topbar['live_status'].set(text)

    # ---------------------------------------------------------------- 数据
    def _prepare_initial(self) -> None:
        frame = self.loader(self.window.strategy, self.window.contract)
        self.window.loader = self.loader
        self.chart.set(frame.tail(self.initial_bars).reset_index(drop=True))
        if frame is not None and len(frame):
            last = frame.iloc[-1]
            self.last_price = float(last.get('close', last.get('price')))
            self.last_time = last.get('time')
        self.chart.fit()
        # 上面把 K 线收窄到 `initial_bars` 了，指标得跟着重建，否则均线/RSI
        # 还停在 full 数据上（比 K 线长一大截）。
        self.window.refresh_panes()
        self._status()

    def _attach_history(self) -> None:
        """可选：向左滚动时自动补更早的数据（pylightcharts 的无限历史）。"""
        try:
            self.window.infinite_history = self.chart.infinite_history(
                self.history_loader, page=max(self.initial_bars, 200),
                threshold=50)
        except Exception as error:
            print('[live] 挂无限历史失败:', error)

    def _stale(self, time: Any) -> bool:
        """比最后一根更早的数据直接丢掉：LWC 会报 "Cannot update oldest data"。"""
        if time is None or self.last_time is None:
            return False
        try:
            if pd.Timestamp(time) < pd.Timestamp(self.last_time):
                if not getattr(self, '_stale_warned', False):
                    print(f'[live] 忽略早于当前的数据（{time} < {self.last_time}），'
                          '实时图只接受"向前"的行情')
                    self._stale_warned = True
                return True
        except Exception:
            return False
        return False

    def on_tick(self, time: Any, price: float,
                volume: Optional[float] = None,
                cumulative_volume: bool = False) -> None:
        """盘中 tick：合并进当前 K 线（高/低/收 + 可选成交量）。

        :param cumulative_volume: 成交量是"当日累计"口径时传 True（天勤就是这样），
            仓库里的 `update_from_tick` 会取增量而不是直接覆盖。
        """
        if self._stale(time):
            return
        row = {'time': time, 'price': float(price)}
        if volume is not None:
            row['volume'] = float(volume)
        self.chart.update_from_tick(pd.Series(row), cumulative_volume)
        self._after_update(time, price)

    def on_bar(self, row: Union[pd.Series, Dict[str, Any]],
               volume: Optional[float] = None) -> None:
        """一根完整 K 线（收线后调用）：追加到图表末尾。"""
        if isinstance(row, dict):
            row = pd.Series(row)
        else:
            row = row.copy()
        if self._stale(row.get('time')):
            return
        if volume is not None:
            row['volume'] = float(volume)
        self.chart.update(row)
        price = row.get('close', row.get('value'))
        self._after_update(row.get('time'),
                           None if price is None else float(price))

    def on_bars(self, frame: pd.DataFrame) -> None:
        """一次补一批（回补 / 断线重连）：逐根 update，保持增量。"""
        for _, row in frame.iterrows():
            self.on_bar(row)

    def _after_update(self, time: Any, price: Optional[float]) -> None:
        self.last_time = time if time is not None else self.last_time
        self.last_price = price if price is not None else self.last_price
        try:
            self.window.refresh_panes()      # 指标副图跟着行情往前走
        except Exception as error:
            print('[live] 指标刷新失败:', error)
        self._status()
        if self.follow:
            self.jump_to_now()

    # ------------------------------------------------------------------ 运行
    def run(self) -> None:
        self.chart.show(block=True)

    def show(self, block: bool = False) -> None:
        self.chart.show(block=block)
