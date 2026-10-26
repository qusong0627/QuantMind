"""天勤（tqsdk）实时行情 -> :class:`LiveWindow` 的接线（tick + 收线）。

参考 `minibt/strategy/light_chart.py::kline_update` 与 `tutorials/live_chart.py`
的做法，把天勤的 kline 序列接到实时图上：

* **同一根 K 线还在动** → `LiveWindow.on_tick(time, price, volume)` →
  `update_from_tick`（合并高/低/收，成交量按累计口径给）；
* **换到新的一根** → 上一根已经定型 → `LiveWindow.on_bar(完整 OHLCV)` →
  `update()` 追加；
* 指标副图由 `LiveWindow` 自己 `refresh_panes()` 跟着更新（K 线和指标都是实时的）。

本模块**不 import tqsdk**：`api`、`klines` 都由调用方传进来，所以没装天勤也能
import / 测试（`python minibt/strategy/tq_live.py` 会用模拟数据跑一遍）。

典型用法（tqsdk 官方循环）::

    from tqsdk import TqApi, TqAuth
    api = TqApi(auth=TqAuth("账号", "密码"))
    klines = {"DCE.pp2601": api.get_kline_serial("DCE.pp2601", 60, data_length=800)}

    window = LiveWindow(strategies=('TianQinStrategy',),
                        contracts=lambda s: tuple(klines),
                        loader=lambda s, c: klines_to_frame(klines[c]),
                        initial_bars=600)
    bridge = TqLiveBridge(window, api, klines)
    window.show(block=False)                 # 非阻塞，自己在下面跑行情循环
    while api.wait_update():
        bridge.update()
    api.close()
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

import pandas as pd

#: 天勤 kline 序列里我们需要的列（不同版本大小写略有差别，统一成小写）
COLUMNS = ('datetime', 'open', 'high', 'low', 'close', 'volume')


def klines_to_frame(kline: pd.DataFrame,
                    *, duration: int = 60) -> pd.DataFrame:
    """天勤 kline 序列 -> `LiveWindow` 需要的 OHLCV 表（time 用秒）。

    天勤的 `datetime` 是**纳秒**整型（且带时区偏移），这里按 `duration` 对齐成
    一根一根的 K 线时间；`LiveWindow` 自己会把时间统一成秒（`_single_datetime_format`）。
    """
    frame = kline.rename(columns={c: c.lower() for c in kline.columns})
    sort_key = 'datetime' if 'datetime' in frame.columns else frame.columns[0]
    frame = frame[[c for c in COLUMNS if c in frame.columns]].copy()
    frame = frame.rename(columns={'datetime': 'time'})
    if pd.api.types.is_numeric_dtype(frame['time']):
        # 纳秒 -> 秒（天勤常见 1e18 量级；其它量级交给 pandas 推断）
        unit = 'ns' if abs(float(frame['time'].iloc[-1])) >= 1e17 else None
        frame['time'] = pd.to_datetime(frame['time'], unit=unit)
    return frame.reset_index(drop=True)


class TqLiveBridge:
    """把天勤的行情循环接到 :class:`LiveWindow`。

    :param window: 已经建好的 `LiveWindow`
    :param api: tqsdk 的 `TqApi`；只用到 `api.is_changing(obj, key)`，没装天勤时
        可以传任意实现了它的对象（测试就是这么做的）
    :param klines: ``{合约: 天勤 kline DataFrame}``
    :param duration: K 线周期（秒），用于把 tick 时间对齐到 K 线
    :param log: 日志回调（默认 print）
    """

    def __init__(self, window, api: Any, klines: Dict[str, pd.DataFrame],
                 *, duration: int = 60,
                 log: Optional[Callable[[str], None]] = None):
        self.window = window
        self.api = api
        self.klines = klines
        self.duration = int(duration)
        self.log = log or (lambda message: print('[tq]', message))
        #: 每个合约上一次"已定型"的 K 线时间，用来判断有没有换新根
        self._confirmed: Dict[str, Any] = {}
        self.ticks = 0

    # ---------------------------------------------------------------- 内部
    def _is_changing(self, row: Any, key: str) -> bool:
        """`api.is_changing` 的薄封装（没有这个方法时按"总是变化"处理）。"""
        try:
            return bool(self.api.is_changing(row, key))
        except Exception:
            return True

    def _row(self, kline: pd.DataFrame) -> pd.Series:
        return kline.iloc[-1]

    # ---------------------------------------------------------------- 行情
    def update(self, contract: Optional[str] = None) -> None:
        """在 `api.wait_update()` 之后调用一次：把变动推给图表。

        不传 `contract` 时只看**当前显示**的那个合约（图表上正在看哪个就喂哪个），
        其它合约的数据留着，切过去时 `update(那份合约)` 即可。
        """
        contract = contract or self.window.window.contract
        kline = self.klines.get(contract)
        if kline is None or not len(kline):
            return
        row = self._row(kline)
        when = window_time(row.get('datetime', row.get('time')))
        previous = self._confirmed.get(contract)

        if previous is None:                     # 第一次：把已定型的最后一根补上
            self._confirmed[contract] = when
            self.window.on_bar(_to_window_row(row, when))
            return
        if when != previous:                     # 换新根：上一根定型，追加它
            self._confirmed[contract] = when
            self.window.on_bar(_to_window_row(row, when))
            return
        if self._is_changing(row, 'close'):      # 同一根还在动：tick 合并
            self.window.on_tick(when, float(row['close']),
                                volume=_volume_delta(kline))  # 天勤给的是**本根**累计量
        self.ticks += 1

    def log_status(self) -> None:
        self.log(f'ticks={self.ticks} last={self.window.last_time} '
                 f'{self.window.last_price}')


def _volume_delta(kline: pd.DataFrame) -> float:
    """天勤 kline 的 `volume` 是**本根**（当根 K 线）的累计成交量。

    `LiveWindow.on_tick` 走 `update_from_tick`，同一根里是"覆盖"语义，所以直接把
    这个值传进去就是对的（不会把整天成交量加进去）。若哪天改用"当日累计"口径，
    再传 `cumulative_volume=True` 让它累加。
    """
    if 'volume' not in kline.columns or len(kline) < 1:
        return 0.0
    volume = float(kline['volume'].iloc[-1])
    return volume


def window_time(value: Any) -> Any:
    """天勤的 `datetime`（纳秒整型）-> 和 :func:`klines_to_frame` 一致的秒级时间。

    两处必须用同一口径：历史用 `klines_to_frame`（秒），实时若直接塞纳秒，
    LWC 会认为时间倒流（`Cannot update oldest data`）。
    """
    if value is None or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return pd.to_datetime(value)
    unit = 'ns' if abs(number) >= 1e17 else None
    return pd.to_datetime(number, unit=unit)


def _to_window_row(row: pd.Series, when: Any = None) -> pd.Series:
    """天勤的一行 -> `LiveWindow.on_bar` 需要的行（time/open/high/low/close/volume）。"""
    data = {str(key).lower(): value for key, value in row.items()}
    data['time'] = when if when is not None else window_time(data.get('datetime'))
    return pd.Series(data)


# --------------------------------------------------------------- 模拟演示 --
class _FakeApi:
    """没装天勤时的替身：只实现 `is_changing`。"""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.step = 0

    def is_changing(self, row, key: str) -> bool:
        return True


def _demo() -> None:
    """没有天勤账号也能跑：用本地真实 CSV 模拟"tick 在动 + 换新根"。"""
    import pathlib

    from minibt.strategy.live_window import LiveWindow

    path = (pathlib.Path(__file__).resolve().parents[1] / 'data' / 'test'
            / 'pp2609_60.csv')
    frame = pd.read_csv(path)
    frame = frame.rename(columns={c: c.lower() for c in frame.columns})
    frame['time'] = pd.to_datetime(frame['time']
                                   if 'time' in frame.columns else frame['datetime'])
    frame = frame[['time', 'open', 'high', 'low', 'close', 'volume']]
    history, coming = frame.iloc[:-200], frame.iloc[-200:]
    print('[demo] 行情:', len(frame), '根 | 模拟放', len(coming), '根')

    window = LiveWindow(strategies=('TianQinStrategy',),
                        contracts=lambda strategy: ('DCE.pp2601',),
                        loader=lambda strategy, contract: history,
                        initial_bars=600,
                        panes={'TianQinStrategy': ('MA 30',)})
    # 天勤风格：kline 序列里最后一根不断被改写；这里用一个长度固定的表来模拟
    buffer = coming.iloc[[0]].copy()
    api = _FakeApi(buffer)
    bridge = TqLiveBridge(window, api, {'DCE.pp2601': buffer})
    for index in range(len(coming)):
        row = coming.iloc[index]
        # 天勤里"当前这根"会一直被改写：先模拟几次同根的 tick（close 在动）
        for _ in range(3):
            buffer.loc[buffer.index[-1]] = row
            bridge.update()          # 同一根 + close 变化 -> on_tick
        # 换到新的一根：把这一根追加进序列（上一根已经定型）
        buffer.loc[len(buffer)] = row
        bridge.update()              # 时间变了 -> on_bar
        if index % 40 == 0:
            bridge.log_status()
        time.sleep(0.02)
    print('[demo] 结束:', len(window.chart.candle_data), '根 |',
          window.chart.topbar['live_status'].value)


if __name__ == '__main__':
    _demo()
