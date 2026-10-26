"""bokeh 实时图表（纯 bokeh server、内存更新）。

与回放（``bokeh_replay.py``）共用 ``bokeh_plot.py`` 的构建器与 ``live_sources``：

* 图表默认设置来自策略自身（``resolve_theme`` / ``grid`` / ``toolbox`` /
  ``volume_mode`` / ``fullscreen``），但**不建权益曲线**（``with_equity=False``），
  页面只有「策略 / 合约」两级。
* 数据来自天勤 ``TqApi``：:class:`StrategyLiveFeed` 用 ``wait_update`` + 策略
  ``_execute_live_trading`` 推进，然后**重新用 ``bokeh_plot.plot`` 的同一套数据
  管线**算一遍并同步到已挂载的 CDS（``bokeh_plot.sync_sources``）——不落盘、
  不起子进程，也不会出现手工 stream/patch 的列长 / 索引错位。

入口：``Bt(live=True).run(gui=Gui.Bokeh)`` → ``bt.py::_tq_real``。测试（无天勤）
可传 ``feed_factory`` 换成任意带 ``bind(reg)`` / ``tick()`` 的源；框架内置
:class:`MockLiveFeed`（本地数据逐根揭开，无天勤）供演示 / 自测：

```python
run_live(strategys, feed_factory=lambda s, reg: MockLiveFeed(s, reg, start_bars=300))
```
"""
from __future__ import annotations

from . import bokeh_plot as _bp
from .bokeh_replay import add_page_root, close_apis, quiet_bokeh_logging

#: 天勤账户里要显示的字段：(key, 中文名, 格式化字符串)
_ACCOUNT_FIELDS = (
    ('balance', '权益', '{:,.2f}'),
    ('available', '可用', '{:,.2f}'),
    ('margin', '保证金', '{:,.2f}'),
    ('float_profit', '浮动盈亏', '{:+,.2f}'),
    ('risk_ratio', '风险度', '{:.2%}'),
)


class StrategyLiveFeed:
    """天勤实时数据 → bokeh CDS。

    ``tick()`` 返回 ``False`` 表示「应该停止」（连接断开且不再重连）；其余情况
    返回 ``True``（继续）。
    """

    def __init__(self, strategys, *, period_ms: int = 1000,
                 stall_seconds: float = 0.5, plot_kwargs: dict | None = None,
                 reconnect: bool = True, window: int = 300):
        self.strategys = list(strategys)
        self.period_ms = int(period_ms)
        self.stall_seconds = float(stall_seconds)
        self.plot_kwargs = dict(plot_kwargs or {})
        self.reconnect = bool(reconnect)
        self.window = max(10, int(window))
        self.reg: dict | None = None
        self.ticks = 0        # 真正推送图表（_push）的次数
        self.updates = 0      # wait_update 返回（含账户/委托变动）的次数

    # ---------------- 生命周期 ----------------
    def bind(self, reg: dict) -> None:
        self.reg = reg

    def _api(self):
        return getattr(self.strategys[0], 'api', None) if self.strategys else None

    def account_text(self) -> str:
        """天勤账户信息一行（没有天勤 / 取不到时返回空串）。"""
        try:
            acc = self._api().get_account()
        except Exception:
            return ''
        parts = []
        for key, label, fmt in _ACCOUNT_FIELDS:
            try:
                parts.append(f'{label} {fmt.format(float(acc[key]))}')
            except Exception:
                continue
        return '　'.join(parts)

    def _market_changed(self, strategy) -> bool:
        """任一订阅的 K 线 ``tq_object`` 有变化 → True（同 ``RealtimeChart``）。"""
        try:
            api = self._api()
            if api is None or not hasattr(api, 'is_changing'):
                return True
            dataset = getattr(strategy, '_btklinedataset', None)
            if not dataset:
                return True
            for kline in dataset.values():
                tq = getattr(getattr(kline, '_dataset', None), 'tq_object', None)
                if tq is not None and api.is_changing(tq):
                    return True
            return False
        except Exception:
            return True

    def tick(self) -> bool:
        if not self.reg:
            return True
        if not self.strategys:
            return False
        strategy = self.strategys[0]
        try:
            is_open = strategy.wait_update(self.stall_seconds)
        except Exception as error:                       # 天勤断线/关闭
            print('[bokeh_realtime] wait_update 出错:', error)
            return bool(self.reconnect)
        if not is_open:
            return True
        try:
            changed = False
            for item in self.strategys:
                _changed = self._market_changed(item)
                changed = changed or _changed
                # step_only=True 时只推进交易/账户，不重算 K 线/指标
                item._execute_live_trading(step_only=not _changed)
            self.updates += 1
            # 行情没变（只有账户/委托变动）时不重建整张图 —— plot() 要重新构
            # 建整个 bokeh 模型树（实测 ~300ms），只是把“刷新次数”抬高而图没变。
            if changed:
                self._push()
                self.ticks += 1
        except Exception as error:
            import traceback
            traceback.print_exc()
            print('[bokeh_realtime] 推进/刷新出错:', error)
        return True

    def _push(self) -> None:
        """用与静态图**同一套**数据管线重算，再同步到已挂载的 CDS。"""
        src: dict = {}
        # `plot_kwargs` 里可能已经带了 `with_equity`（`build_live_app` 会写入
        # False）—— 再额外传一个同名关键字就会报
        # "got multiple values for keyword argument 'with_equity'"
        kwargs = dict(self.plot_kwargs)
        kwargs.setdefault('with_equity', False)   # 实时不画权益曲线
        _bp.plot(self.strategys, live_sources=src, build_only=True, **kwargs)
        if _bp.sync_sources(self.reg, src) < 0:
            print('[bokeh_realtime] CDS 数量不一致，跳过本次刷新')
            return
        self._follow_x()

    def _latest_index(self):
        full = (self.reg or {}).get('full') or {}
        best = None
        for cds in (self.reg or {}).get('cds', []):
            idx = (full.get(cds.id) or {}).get('index')
            if idx is None or not len(idx):
                continue
            v = int(idx[-1])
            if best is None or v > best:
                best = v
        return best

    def _follow_x(self) -> None:
        """x 轴跟随最新：只在“右边缘已贴近最新”时移动，用户拖到历史不被打扰。

        例外：数据长度**变短**时（实盘 ``_preload`` 截断会把 index 空间从
        0..999 重置成 0..199）窗口会整个落到数据右边，这时必须跟回去；
        否则主图 K 线被挤出可视区，而**不参与同步**的成交量源还在旧范围内，
        看起来就是“其他全没了、只剩成交量”。
        """
        last = self._latest_index()
        if last is None:
            return
        tol = max(3.0, self.window * 0.08)
        pad = max(1, int(self.window * 0.02))
        for xr in (self.reg or {}).get('x_ranges', []):
            try:
                end = float(xr.end)
                start = float(xr.start)
            except Exception:
                continue
            if not (abs(end - last) <= tol or last < start):
                continue                      # 用户拖到历史：别打扰
            try:
                xr.start = max(0, last - self.window + 1)
                xr.end = last + pad
            except Exception:
                pass


class MockLiveFeed:
    """本地数据模拟 tick（无天勤）——用于演示 / 自测 bokeh 实时图。

    和 :class:`StrategyLiveFeed` 不同，这里不调 ``TqApi``：策略一开始就用
    **全量本地数据**把指标 / 交易 / 权益算好（``bokeh_plot.plot(build_only=True)``），
    ``tick()`` 只是按 ``index`` **逐根揭开**（``bokeh_plot.reveal_sources``）并把
    视口右移，看上去就和真实时间一样逐根跑；顶部的“账户信息”也按当前 bar 的
    累计盈亏撮出来。用法::

        build_live_app(strategys, feed_factory=MockLiveFeed, start_bars=300,
                       period_ms=500)
        # 或 bt.run(gui=Gui.Bokeh, feed_factory=MockLiveFeed, start_bars=300)
    """

    def __init__(self, strategys, reg: dict | None = None, *,
                 start_bars: int = 300, window: int = 300,
                 period_ms: int = 1000, speed: int = 1,
                 total: int | None = None):
        self.strategys = list(strategys)
        self.reg = None
        self.start_bars = max(1, int(start_bars))
        self.window = max(10, int(window))
        self.period_ms = int(period_ms)
        self.speed = max(1, int(speed))
        self.ticks = 0
        self.updates = 0
        self.pos = 0
        self.total = int(total) if total else 0
        if reg is not None:
            self.bind(reg)

    # ---------------- 生命周期 ----------------
    def bind(self, reg: dict) -> None:
        self.reg = reg
        if not self.total:
            self.total = self._total_bars(reg) or self.start_bars
        self.pos = min(self.start_bars, self.total)
        _bp.reveal_sources(reg, self.pos - 1)
        self._follow_x(force=True)

    @staticmethod
    def _total_bars(reg: dict) -> int:
        best = 0
        for cds in (reg or {}).get('cds', []):
            full = ((reg or {}).get('full') or {}).get(cds.id) or {}
            idx = full.get('index')
            if idx is not None and len(idx):
                best = max(best, int(idx[-1]))
        return best + 1 if best else 0

    def account_text(self) -> str:
        """按**当前 bar**的盈亏撮一行“账户信息”（非真实账户）。"""
        if not self.strategys:
            return ''
        strategy = self.strategys[0]
        equity = floating = None
        try:
            # strategy._results 是“每合约一个 DataFrame”的 list
            results = getattr(strategy, '_results', None) or []
            trades = results[0] if results else None
            if trades is not None and len(trades):
                i = min(self.pos, len(trades)) - 1
                if i >= 0:
                    equity = float(trades['total_profit'].iloc[i])
                    if 'float_profits' in trades:
                        floating = float(trades['float_profits'].iloc[i])
        except Exception:
            equity = floating = None
        if equity is None:
            equity = float(getattr(getattr(strategy, 'config', None),
                                   'value', 1_000_000.) or 1_000_000.)
        parts = [f'权益 {equity:,.2f}']
        if floating is not None:
            parts.append(f'浮动盈亏 {floating:+,.2f}')
        parts.append(f'进度 {self.pos}/{self.total}')
        return '　'.join(parts)

    def tick(self) -> bool:
        if not self.reg:
            return True
        if self.pos >= self.total:
            return True
        self.pos = min(self.total, self.pos + self.speed)
        _bp.reveal_sources(self.reg, self.pos - 1)
        self._follow_x()
        self.ticks += 1
        self.updates += 1
        return True

    def _follow_x(self, force: bool = False) -> None:
        last = self.pos - 1
        tol = max(3.0, self.window * 0.08)
        pad = max(1, int(self.window * 0.02))
        for xr in (self.reg or {}).get('x_ranges', []):
            try:
                if force or abs(float(xr.end) - last) <= tol:
                    xr.start = max(0, last - self.window + 1)
                    xr.end = last + pad
            except Exception:
                pass


def _feed_status(feed) -> str:
    n = getattr(feed, 'ticks', None)
    m = getattr(feed, 'updates', None)
    if isinstance(n, int) and isinstance(m, int):
        return f'已刷新 {n} 次（行情 {m} tick）'
    return f'已刷新 {n} 次' if isinstance(n, int) else ''


def _apply_preload(strategys) -> None:
    """把实盘 KLine 截到 ``_preload`` 根（与后续 tick 的 `_inplace_values` 一致）。

    实盘 ``history_length > data_length`` 时，底层 KLine 一开始是**全量**
    （如 1000），第一次行情变化才会被 `_inplace_values` 截到 ``preload``（如
    200）。如果首帧按全量建图，index 空间是 0..999；之后变成 0..199，而 x 轴
    窗口还停在旧范围尾部 —— 整张图就空白了（只有不参与同步的成交量源还在，
    看起来像“只剩成交量”）。所以首帧就统一按 preload 长度建图。
    """
    for strategy in strategys:
        klines = getattr(strategy, '_btklinedataset', None) or {}
        try:
            klines = list(klines.values())
        except Exception:
            continue
        for kline in klines:
            try:
                if getattr(kline, '_preload', None):
                    kline._inplace_values()
            except Exception:
                pass


def build_live_app(strategys, *, period_ms: int = 1000,
                   plot_kwargs: dict | None = None, feed_factory=None,
                   **feed_kwargs):
    """构建 bokeh 实时 ``Application``。

    ``feed_factory(strategys, reg) -> feed``（须实现 ``tick()``）；不传则用
    :class:`StrategyLiveFeed`（天勤）。
    """
    from bokeh.application import Application
    from bokeh.application.handlers.function import FunctionHandler
    from bokeh.layouts import row
    from bokeh.models import PreText, Spacer

    plot_kwargs = dict(plot_kwargs or {})
    plot_kwargs['with_equity'] = False

    def make_document(doc):
        reg: dict = {}
        _apply_preload(strategys)          # 首帧就按 preload 长度建图
        tabs = _bp.plot(strategys, live_sources=reg, build_only=True,
                        **plot_kwargs)
        if feed_factory is not None:
            feed = feed_factory(strategys, reg)
        else:
            feed = StrategyLiveFeed(strategys, period_ms=period_ms,
                                    plot_kwargs=plot_kwargs, **feed_kwargs)
            feed.bind(reg)

        # 更新由天勤 `api.wait_update` 驱动，不需要手动暂停按钮；
        # 顶部改为**账户信息**（天勤 get_account 的权益/可用/保证金/盈亏/风险度）。
        state = {'stopped': False}
        acct = PreText(text='', height=22, width_policy='max',
                       styles={'font-size': '12px', 'margin': '0',
                               'white-space': 'nowrap', 'overflow': 'hidden'})
        info = PreText(text='', height=22, width_policy='min',
                       styles={'font-size': '12px', 'margin': '0',
                               'padding-right': '8px'})

        def _account_text():
            fn = getattr(feed, 'account_text', None)
            if callable(fn):
                try:
                    return fn() or ''
                except Exception:
                    return ''
            return ''

        def _update_info():
            acct.text = _account_text()
            parts = ['已停止'] if state['stopped'] else ['运行中']
            extra = _feed_status(feed)
            if extra:
                parts.append(extra)
            info.text = '　'.join(parts)

        _update_info()
        controls = row(acct, Spacer(width_policy='max'), info,
                       width_policy='max')
        # 账户文本框与第一个顶栏（tabs）之间加一条细分隔线
        add_page_root(doc, controls, tabs, reg, separator=True)

        def tick():
            if state['stopped']:
                return
            try:
                alive = feed.tick()
            except Exception as error:
                print('[bokeh_realtime] tick 出错:', error)
                return
            _update_info()
            if alive is False:
                state['stopped'] = True
                _update_info()

        doc.add_periodic_callback(tick, int(period_ms))

    return Application(FunctionHandler(make_document))


def run_live(strategys, *, port: int | None = None, period_ms: int = 1000,
             plot_kwargs: dict | None = None, feed_factory=None, show: bool = True,
             session_token_expiration: int = 86400, **feed_kwargs) -> None:
    """启动 bokeh 实时 server（阻塞，直到 Ctrl+C / IOLoop 退出）。"""
    from random import randint

    from bokeh.server.server import Server
    from tornado.ioloop import IOLoop

    quiet_bokeh_logging()
    if port is None:
        port = randint(1000, 9999)
    app = build_live_app(strategys, period_ms=period_ms, plot_kwargs=plot_kwargs,
                         feed_factory=feed_factory, **feed_kwargs)
    io_loop = IOLoop.current()
    server = Server(applications={'/': app}, io_loop=io_loop, port=port,
                    session_token_expiration=session_token_expiration)
    print(f'【bokeh 实时】 http://localhost:{port}  （Ctrl+C 退出）')
    try:
        server.start()
        if show:
            server.show('/')
        io_loop.start()
    except KeyboardInterrupt:
        print('\n【bokeh 实时】 收到 Ctrl+C，正在关闭天勤连接 ...')
    finally:
        # 先 api.close() 断开天勤，再返回（进程退出）
        close_apis(strategys)
        print('【bokeh 实时】 已关闭天勤连接，成功退出')
