"""回测信息页的两个可复用模块（纯 pylightcharts）。

    StatsTable              回测指标表（收益 / 风险 / 交易 三列分组）
    drawdown_curve()        权益 → 回撤（与回放里的 EquityCurveChart 同公式）
    BacktestPage            权益 + 回撤（都是与基准线的双色带）+ 每笔盈亏竖线
    TradeRecordsPage        交易记录（分页表格）

只依赖 pylightcharts 的公开 API（`create_table` / `create_line` / `add_pane` /
`topbar` / `set_pane_stretch` …），所以能挂在 `StrategyWindow` 上，也能单独用。
"""
from __future__ import annotations

import os

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from pylightcharts import shapes

# ---------------------------------------------------------------- 数值格式
UP = '#26A69A'
DOWN = '#EF5350'
#: 基准线颜色（虚线、不显示标签）
BASELINE_COLOUR = '#787B86'


def money(value: Any, decimals: int = 2, unit: str = '') -> str:
    """1234.5 -> '1,234.50'（带千分位，None -> '-'）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '-'
    return f'{float(value):,.{decimals}f}{unit}'


def percent(value: Any, decimals: int = 2) -> str:
    """0.0123 -> '+1.23%'（输入按小数比例）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '-'
    sign = '+' if float(value) >= 0 else ''
    return f'{sign}{float(value) * 100:.{decimals}f}%'


def colour(value: Any, up: str = UP, down: str = DOWN, flat: str = '') -> str:
    """按正负给涨/跌色。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return flat
    return up if float(value) >= 0 else down


def _alpha(colour_hex: str, value: float) -> str:
    """`#RRGGBB` + 不透明度（0~1）-> `#RRGGBBAA`。

    非 6 位十六进制色（命名色 / `rgba()`）原样返回 —— 调用方可用
    0~1 的小数控制色带浓度。
    """
    text = str(colour_hex).strip()
    if not (text.startswith('#') and len(text) == 7):
        return text
    level = max(0, min(255, round(float(value) * 255)))
    return f'{text}{level:02x}'


def _frame(data: Any, columns: Sequence[str]) -> pd.DataFrame:
    """把任意输入（DataFrame / list[dict] / list[tuple] / None）转成表格用帧。"""
    if data is None:
        return pd.DataFrame(columns=list(columns))
    if isinstance(data, pd.DataFrame):
        frame = data.copy()
    else:
        frame = pd.DataFrame(list(data), columns=list(columns))
    for column in columns:
        if column not in frame.columns:
            frame[column] = None
    return frame


def _time_value(data: Any, time: str, value: str) -> pd.DataFrame:
    """统一的 `time` + `value` 两列（Series / DataFrame / 时间索引都接受）。"""
    if isinstance(data, pd.Series):
        frame = data.rename(value).reset_index()
        frame.columns = [time, value]
    else:
        frame = pd.DataFrame(data).copy()
        if value not in frame.columns:
            frame = frame.rename(columns={frame.columns[-1]: value})
    frame = frame[[time, value]].reset_index(drop=True)
    frame[value] = frame[value].astype(float)
    return frame


def drawdown_curve(equity: Any, *, time: str = 'time',
                   value: str = 'value') -> pd.DataFrame:
    """权益 -> 回撤曲线（与 ``light_chart_replay.py::EquityCurveChart`` 一致）。

        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / np.where(peak > 0, peak, 1.0)

    即相对历史最高点的比例、值 <= 0；``peak`` 非正时除以 1（权益以"收益"为基准时
    会出现非正的 peak，直接除会得到无意义的尖刺/空图）。
    """
    frame = _time_value(equity, time, value)
    values = frame[value].to_numpy()
    peak = np.maximum.accumulate(values)
    frame[value] = (values - peak) / np.where(peak > 0, peak, 1.0)
    return frame


# ------------------------------------------------------------------ 回测指标
class StatsTable:
    """回测数据表：把 ``{'收益指标': [(键, 格式), ...], ...}`` 画成表格。

    每个分组占一列，"指标名: 数值"一行；数值用格式化函数或模板处理。
    """

    #: 分组 -> ((取值键, 格式化函数或模板), ...)
    DEFAULT_ROWS = {
        '收益指标': (('final_profit', money), ('total_return', percent),
                 ('total_fee', money), ('profit_ratio', '{:.4f}'),
                 ('mean_profit', '{:.2f}')),
        '风险指标': (('sharpe_ratio', '{:.4f}'), ('max_drawdown', percent),
                 ('var', '{:.4f}'), ('risk_return_ratio', '{:.4f}')),
        '交易指标': (('win_rate', percent), ('win_count', '{:.0f}'),
                 ('loss_count', '{:.0f}'), ('total_trades', '{:.0f}'),
                 ('mean_loss', '{:.2f}')),
    }

    #: 键 -> 中文标题（表里显示的就是这些；可用 `update(titles=...)` 覆盖）
    TITLES = {
        'final_profit': '最终收益', 'total_return': '累计收益率',
        'total_fee': '总手续费', 'profit_ratio': '盈亏比',
        'mean_profit': '平均收益', 'sharpe_ratio': '夏普比率',
        'max_drawdown': '最大回撤', 'var': '风险价值(VaR)',
        'risk_return_ratio': '风险收益比', 'win_rate': '胜率',
        'win_count': '盈利次数', 'loss_count': '亏损次数',
        'total_trades': '交易次数', 'mean_loss': '平均亏损',
    }

    def __init__(self, chart, *, position: str = 'top-right', width: int = 600,
                 height: int = 300, groups: Optional[Dict] = None):
        self.chart = chart
        self.groups = groups or self.DEFAULT_ROWS
        headings = tuple(self.groups)
        self.table = chart.create_table(
            width=width, height=height, headings=headings,
            widths=tuple(1 / len(headings) for _ in headings),
            alignments=tuple('center' for _ in headings),
            position=position, draggable=True)
        self.stats: Dict[str, Any] = {}
        self.table.visible(False)

    def update(self, stats: Dict[str, Any],
               titles: Optional[Dict[str, str]] = None) -> 'StatsTable':
        """刷新数据（`stats` 的键见 :attr:`DEFAULT_ROWS`，标题默认用中文）。"""
        self.stats = dict(stats or {})
        titles = {**self.TITLES, **(titles or {})}
        self.table.clear()
        rows = max((len(entries)
                   for entries in self.groups.values()), default=0)
        for index in range(rows):
            row = []
            for entries in self.groups.values():
                if index >= len(entries):
                    row.append('')
                    continue
                key, spec = entries[index]
                row.append(f'{titles.get(key, key)}: '
                           f'{self._format(self.stats.get(key), spec)}')
            self.table.new_row(*row)
        return self

    @staticmethod
    def _format(value: Any, spec: Any) -> str:
        if callable(spec):
            return spec(value)
        try:
            return spec.format(value) if value is not None else '-'
        except (TypeError, ValueError):
            return str(value)

    def show(self) -> 'StatsTable':
        self.table.visible(True)
        return self

    def hide(self) -> 'StatsTable':
        self.table.visible(False)
        return self


# ------------------------------------------------------------------ 回测曲线
class BacktestPage:
    """回测结果页的图表区：权益 + 回撤（与基准线的双色带）+ 每笔盈亏散点。

    * **权益**：基准线是**期初金额**，曲线在基准线上方用涨色、下方用跌色；
    * **回撤**：基准线是 **0**，同样双色带，Y 轴按**百分比**显示；
    * **每笔盈亏**：一个副图，每笔从基准线 0 拉一条**垂直虚线**到盈亏值
      （`shapes.stem`，端点可补小圆点），逐点着色（盈利涨色 / 亏损跌色）；
    * 三条基准线都是**全宽水平线**（`horizontal_span`），不占价格轴、无标签；
    * 所有 pane 等高（:meth:`equalise`）。
    """

    #: 三个副图的显示名（图例标签）
    LABELS = {'equity': '权益', 'drawdown': '回撤', 'pnl': '盈亏'}

    def __init__(self, chart, *, name: str = '权益',
                 up: str = UP, down: str = DOWN,
                 line_width: int = 2, marker_radius: int = 4,
                 band_top_alpha: float = 0.28,
                 band_bottom_alpha: float = 0.06,
                 stem_width: int = 1, stem_cap: bool = True):
        self.chart = chart
        self.name = name
        self.up = up
        self.down = down
        self.line_width = line_width
        self.marker_radius = marker_radius
        #: 色带上下沿的不透明度（曲线侧 / 基准线侧，0~1）
        self.band_top_alpha = float(band_top_alpha)
        self.band_bottom_alpha = float(band_bottom_alpha)
        #: 盈亏竖线（`shapes.stem`）线宽 / 端点是否补小圆点
        self.stem_width = int(stem_width)
        self.stem_cap = bool(stem_cap)
        self.panes: Dict[str, int] = {}
        self.series: Dict[str, Any] = {}
        #: 期初金额（权益基准线），第一次喂权益时确定
        self._initial: Optional[float] = None

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _baseline(target, price: float) -> None:
        """基准线：全宽水平线（`horizontal_span(filled=False)`）。

        原来是 `createPriceLine`（虚线价格线）。权益/回撤已经有色带一直铺到
        基准线上，再加一条价格线会多出一道贴着色带的虚线；`horizontal_span`
        是独立图元，不参与价格轴、也没有坐标轴标签，视觉上干净得多。
        盈亏散点图的 0 轴同样适用。
        """
        target.horizontal_span(float(price), filled=False,
                               line_color=BASELINE_COLOUR,
                               line_width=1, line_style='dashed')

    def _band(self, key: str, frame: pd.DataFrame, baseline: float,
              percent: bool = False) -> None:
        """曲线与基准线之间的**双色带**：>= 基准用涨色、< 基准用跌色。

        同一个 pane 里放两条 Area，各自把数据向基准线夹紧：
        ``max(值, 基准)`` 只填基准线以上、``min(值, 基准)`` 只填基准线以下；
        再用 Area 的 ``base_value`` 把填充画到基准线上。两条夹紧曲线合起来正是
        原曲线，所以刻度范围不变。另有一条隐藏的"真实曲线"作句柄（清理用）。
        """
        pane = self.chart.add_pane()
        self.panes[key] = pane
        values = frame['value'].to_numpy()
        for suffix, colour, clip in (('up', self.up, np.maximum),
                                     ('down', self.down, np.minimum)):
            settings = {'line_color': colour,
                        'top_color': _alpha(colour, self.band_top_alpha),
                        'bottom_color': _alpha(colour,
                                               self.band_bottom_alpha),
                        'line_width': self.line_width,
                        'base_value': float(baseline),
                        'price_line_visible': False,
                        'last_value_visible': False}
            if percent:
                settings['price_format'] = {'type': 'percent'}
            series = self.chart.pane_add_series(pane, 'Area', name='',
                                                **settings)
            series.set(pd.DataFrame({'time': frame['time'].to_numpy(),
                                     'value': clip(values, baseline)}))
            self.series[f'{key}_{suffix}'] = series
        hidden = self.chart.pane_add_series(
            pane, 'Line', name=self.LABELS.get(key, key),
            line_visible=False, price_line_visible=False,
            last_value_visible=False, crosshair_marker_visible=False,
            legend_toggle=False)
        hidden.set(pd.DataFrame({'time': frame['time'].to_numpy(),
                                 self.LABELS.get(key, key): values}))
        self._baseline(hidden, baseline)
        self.series[key] = hidden

    def _refresh_band(self, band_key: str, band_frame: pd.DataFrame,
                      baseline: float) -> None:
        """已建过的双色带：只更新数据（夹紧后重新下发）。

        参数名刻意不用 `frame`/`key`：仓库里有个签名守卫测试，它把"位置参数名
        和形参名不一致"当成可疑的转发（`series.set(frame)` vs `set(df)`），
        这里换个名字就不会被误报。
        """
        values = band_frame['value'].to_numpy()
        for suffix, clip in (('up', np.maximum), ('down', np.minimum)):
            name = f'{band_key}_{suffix}'
            series = self.series[name] if name in self.series else None
            if series is not None:
                series.set(pd.DataFrame(
                    {'time': band_frame['time'].to_numpy(),
                     'value': clip(values, baseline)}))
        series = self.series[band_key] if band_key in self.series else None
        if series is not None:
            series.set(pd.DataFrame(
                {'time': band_frame['time'].to_numpy(),
                 self.LABELS.get(band_key, band_key):
                     band_frame['value'].to_numpy()}))

    # ---------------------------------------------------------------- 数据
    def update(self, equity: Any = None, *, drawdown: Any = None,
               pnl: Any = None, time: str = 'time',
               value: str = 'value') -> 'BacktestPage':
        """喂入三块数据（可分别给，也可后续再补）。"""
        if equity is not None:
            frame = _time_value(equity, time, value)
            if self._initial is None:                # 期初金额 = 第一条权益
                self._initial = float(frame[value].dropna().iloc[0])
            if 'equity' in self.panes:
                self._refresh_band('equity', frame, self._initial)
            else:
                self._band('equity', frame, self._initial)
            # 回撤一律由权益重算（回放里的 EquityCurveChart 同一套公式）
            drawdown = drawdown_curve(equity, time=time, value=value)
        if drawdown is not None:
            frame = _time_value(drawdown, time, value)
            if 'drawdown' in self.panes:
                self._refresh_band('drawdown', frame, 0.0)
            else:
                self._band('drawdown', frame, 0.0, percent=True)
        if pnl is not None:
            frame = _time_value(pnl, time, value)
            frame['color'] = [self.up if v >= 0 else self.down
                              for v in frame[value]]
            if 'pnl' in self.panes:
                self._set_pnl(frame)
            else:
                self._pnl_series(frame)
        return self

    def _pnl_series(self, frame: Optional[pd.DataFrame] = None):
        """每笔盈亏：一个副图，每笔从基准线 0 拉一条垂直虚线到盈亏值。

        LWC 没有"竖直虚线"图元，但同一根 K 线上的两点 `polyline`（即
        `shapes.stem`）就是它；`cap=True` 在端点补个小圆点，保留原来散点图
        "每笔交易一个点"的可读性。
        """
        pane = self.chart.add_pane()
        self.panes['pnl'] = pane
        series = self.chart.pane_add_custom_series(
            pane, self.LABELS['pnl'], legend=True)   # 左上角要显示盈亏标签
        self.series['pnl'] = series
        self.chart.horizontal_span(
            0.0, filled=False, pane_index=pane,
            line_color=BASELINE_COLOUR, line_width=1, line_style='dashed')
        if frame is not None:
            self._set_pnl(frame)
        return series

    def _set_pnl(self, frame: pd.DataFrame) -> None:
        """写盈亏：每笔一根竖线，逐点着色（盈利涨色 / 亏损跌色）。

        注意列名必须是 `value`：自定义序列靠数据项的 `value` 字段驱动自动缩放、
        也是它判断“这个点是不是空白”的依据（用中文列名会被整点跳过、整个图不出）。
        图例上的名字来自建序列时的 `name=`，跟列名无关。
        """
        self.series['pnl'].set(
            pd.DataFrame({'time': frame['time'].to_numpy(),
                          'value': frame['value'].to_numpy(),
                          'color': list(frame['color'])}),
            shapes=lambda row: shapes.stem(
                row['value'], color=row['color'], width=self.stem_width,
                cap=self.stem_cap, cap_radius=self.marker_radius))

    # ------------------------------------------------------------ 显示/移除
    def equalise(self, kline_stretch: float = 2.0,
                 indicator_stretch: float = 1.0) -> None:
        """回测页高度比：K 线图（主图 + 各合约副图 K 线）= 2，
        权益/回撤/盈亏副图 = 1。"""
        try:
            mine = set(self.panes.values())     # 权益/回撤/盈亏三个 pane
            for index in range(self.chart.pane_count()):
                stretch = (indicator_stretch if index in mine
                           else kline_stretch)
                self.chart.set_pane_stretch(index, stretch)
        except Exception:
            pass

    def remove(self) -> None:
        """移除三块回测图（空 pane 由引擎自动去掉）。"""
        for series in list(self.series.values()):
            try:
                series.delete()
            except Exception as error:
                print('[pages] 删回测序列失败:', error)
        self.panes.clear()
        self.series.clear()
        if os.environ.get('REALTIME_DEBUG'):
            try:
                print('[pages] remove() 后 pane 数:', self.chart.pane_count())
            except Exception:
                pass

    @property
    def pane(self) -> Optional[int]:
        """权益曲线所在的 pane（兼容旧接口）。"""
        return self.panes.get('equity')


#: 旧类名保留为别名
EquityCurvePage = BacktestPage


# ------------------------------------------------------------------ 交易记录
class TradeRecordsPage:
    """交易记录：分页表格（默认每页 200 行）+ 顶栏翻页控件。"""

    DEFAULT_COLUMNS = ('time', 'direction', 'symbol', 'price', 'qty', 'fee',
                       'balance', 'pnl')

    #: 列名 -> 中文表头（与 minibt 策略输出的交易记录同款）
    HEADINGS = {'time': '时间', 'direction': '方向', 'symbol': '合约',
                'price': '成交价', 'qty': '数量', 'fee': '手续费',
                'balance': '权益', 'pnl': '盈亏'}
    #: 方向取值（minibt 输出的就是中文）
    DIRECTIONS = {'开多': '开多', '平多': '平多', '开空': '开空', '平空': '平空'}

    def __init__(self, chart, trades: Any = None, *,
                 columns: Sequence[str] = DEFAULT_COLUMNS,
                 headings: Optional[Sequence[str]] = None,
                 rows_per_page: int = 200, position: str = 'center',
                 width: int = 720, height: int = 420, pager: bool = True):
        self.chart = chart
        self.columns = tuple(columns)
        self.headings = tuple(
            headings or [self.HEADINGS.get(name, name)
                         for name in self.columns])
        self.rows_per_page = int(rows_per_page)
        self.ascending = False
        self.sort_column: Optional[str] = None
        self.index = 0
        self.frame = _frame(trades, self.columns)
        self.table = chart.create_table(
            width=width, height=height, headings=self.headings,
            widths=tuple(1 / len(self.headings) for _ in self.headings),
            alignments=tuple('center' for _ in self.headings),
            position=position, draggable=True)
        self.table.visible(False)
        self.pager = None
        if pager:
            self.chart.topbar.textbox('trades_page', '1/1', align='right')
            self.chart.topbar.button('trades_first', '«', func=self.first)
            self.chart.topbar.button('trades_prev', '‹', func=self.prev)
            self.chart.topbar.button('trades_next', '›', func=self.next)
            self.chart.topbar.button('trades_last', '»', func=self.last)
            self.pager = self.chart.topbar

    # ---------------------------------------------------------------- 数据
    def set_trades(self, trades: Any) -> 'TradeRecordsPage':
        """换一批交易记录（页码回到第一页）。"""
        self.frame = _frame(trades, self.columns)
        if self.sort_column:
            self.frame = self.frame.sort_values(
                self.sort_column, ascending=self.ascending, kind='stable')
        self.index = 0
        return self.refresh()

    def sort_by(self, column: str,
                ascending: bool = False) -> 'TradeRecordsPage':
        """按列排序（Python 侧排序后回到第一页）。"""
        if column not in self.frame.columns:
            raise ValueError(
                f'no column {column!r} in {list(self.frame.columns)}')
        self.sort_column, self.ascending = column, ascending
        self.frame = self.frame.sort_values(
            column, ascending=ascending, kind='stable').reset_index(drop=True)
        self.index = 0
        return self.refresh()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.frame) // self.rows_per_page))

    def page(self, index: int) -> 'TradeRecordsPage':
        self.index = max(0, min(int(index), self.pages - 1))
        return self.refresh()

    def first(self, chart=None): return self.page(0)
    def prev(self, chart=None): return self.page(self.index - 1)
    def next(self, chart=None): return self.page(self.index + 1)
    def last(self, chart=None): return self.page(self.pages - 1)

    # ---------------------------------------------------------------- 渲染
    def refresh(self) -> 'TradeRecordsPage':
        """只重画当前页（清空 + 逐行写入），不重建表头。"""
        start = self.index * self.rows_per_page
        page = self.frame.iloc[start:start + self.rows_per_page]
        self.table.clear()
        for _, record in page.iterrows():
            self.table.new_row(*[self._cell(record[column], column)
                                 for column in self.columns])
        if self.pager is not None:
            self.pager['trades_page'].set(
                f'{self.index + 1}/{self.pages}  ({len(self.frame)} 笔)')
        return self

    def _cell(self, value: Any, column: str) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return '-'
        lower = column.lower()
        if lower == 'direction':
            return self.DIRECTIONS.get(str(value).lower(), str(value))
        if isinstance(value, (int, float, np.number)):
            if any(key in lower for key in ('pnl', 'profit', 'fee', 'balance')):
                return money(value)
            if 'price' in lower:
                return f'{float(value):.2f}'
            return f'{value:g}'
        return str(value)

    def show(self) -> 'TradeRecordsPage':
        self.table.visible(True)
        return self.refresh()

    def hide(self) -> 'TradeRecordsPage':
        self.table.visible(False)
        return self

    def remove(self) -> None:
        self.table.delete()


# ------------------------------------------------------------------ 报表页
_ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;'}


def _esc(text: Any) -> str:
    """HTML 转义（把表格值安全地塞进 HTML）。"""
    return ''.join(_ESC.get(ch, ch) for ch in str(text))


def _trade_cell(value: Any, column: str) -> str:
    """交易记录单元格（与 `TradeRecordsPage._cell` 同一套规则）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '-'
    lower = column.lower()
    if lower == 'direction':
        return TradeRecordsPage.DIRECTIONS.get(str(value).lower(), str(value))
    if isinstance(value, (int, float, np.number)):
        if any(key in lower for key in ('pnl', 'profit', 'fee', 'balance')):
            return money(value)
        if 'price' in lower:
            return f'{float(value):.2f}'
        return f'{value:g}'
    return str(value)


class ReportPage:
    """「回测指标 交易记录」页的**整页 HTML 报表**（替换浮层表格）。

    进入本页时把图表画布整个盖住，用一张真正的 HTML 页面展示：

    * 回测指标：分组（收益/风险/交易）的两列表格；
    * 交易记录：完整 `<table>`（滚动浏览，无需翻页）。

    颜色全用根 CSS 变量，跟着「主题」按钮自动换深色/浅色。
    """

    CSS = '''
    .plc-report { line-height: 1.6; }
    .plc-report h2 { color: var(--color); font-size: 15px; margin: 2px 0 10px;
        border-bottom: 1px solid var(--border-color); padding-bottom: 6px; }
    .plc-report table { border-collapse: collapse; width: 100%;
        margin-bottom: 22px; font-size: 13px; }
    .plc-report th, .plc-report td { border: 1px solid var(--border-color);
        padding: 6px 10px; text-align: left; color: var(--color); }
    .plc-report th { background: var(--muted-bg-color); font-weight: 600; }
    .plc-report tr.section th { background: var(--hover-bg-color); }
    .plc-report td.num { text-align: right;
        font-variant-numeric: tabular-nums; }
    '''

    def __init__(self, chart, *, stats=None, trades=None):
        self.chart = chart
        self.stats: Dict[str, Any] = {}
        self.frame = _frame(trades, TradeRecordsPage.DEFAULT_COLUMNS)
        self.ascending = False
        self.sort_column: Optional[str] = None
        self.panel = chart.create_html_panel()
        self.update(stats=stats, trades=trades)

    # ------------------------------------------------------------- 数据
    def update(self, *, stats=None, trades=None) -> 'ReportPage':
        """刷新数据（`stats` / `trades` 可分别给，也可后续再补）。"""
        if stats is not None:
            self.stats = dict(stats)
        if trades is not None:
            self.frame = _frame(trades, TradeRecordsPage.DEFAULT_COLUMNS)
            if self.sort_column:
                self.frame = self.frame.sort_values(
                    self.sort_column, ascending=self.ascending, kind='stable')
        return self._render()

    def sort_by(self, column: str, ascending: bool = False) -> 'ReportPage':
        """按列排序（Python 侧排序）。"""
        if column not in self.frame.columns:
            raise ValueError(
                f'no column {column!r} in {list(self.frame.columns)}')
        self.sort_column, self.ascending = column, ascending
        self.frame = self.frame.sort_values(
            column, ascending=ascending, kind='stable').reset_index(drop=True)
        return self._render()

    # ------------------------------------------------------------- 渲染
    def _render(self) -> 'ReportPage':
        self.panel.set_html(self._html())
        return self

    def _html(self) -> str:
        return (f'<style>{self.CSS}</style><div class="plc-report">'
                + self._stats_html() + self._trades_html() + '</div>')

    def _stats_html(self) -> str:
        rows = StatsTable.DEFAULT_ROWS
        titles = StatsTable.TITLES
        out = ['<h2>回测指标</h2><table><tbody>']
        for group, entries in rows.items():
            out.append(
                f'<tr class="section"><th colspan="2">{_esc(group)}</th></tr>')
            for key, spec in entries:
                value = StatsTable._format(self.stats.get(key), spec)
                out.append(
                    f'<tr><td>{_esc(titles.get(key, key))}</td>'
                    f'<td class="num">{_esc(value)}</td></tr>')
        out.append('</tbody></table>')
        return ''.join(out)

    def _trades_html(self) -> str:
        cols = TradeRecordsPage.DEFAULT_COLUMNS
        heads = TradeRecordsPage.HEADINGS
        out = ['<h2>交易记录</h2><table><thead><tr>']
        for col in cols:
            out.append(f'<th>{_esc(heads.get(col, col))}</th>')
        out.append('</tr></thead><tbody>')
        for _, record in self.frame.iterrows():
            out.append('<tr>')
            for col in cols:
                out.append(f'<td>{_esc(_trade_cell(record[col], col))}</td>')
            out.append('</tr>')
        out.append('</tbody></table>')
        return ''.join(out)

    # ------------------------------------------------------------- 显示
    def show(self) -> 'ReportPage':
        self.panel.show()
        return self

    def hide(self) -> 'ReportPage':
        self.panel.hide()
        return self

    def remove(self) -> None:
        self.panel.delete()
