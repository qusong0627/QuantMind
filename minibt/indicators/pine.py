from __future__ import annotations

from .base import tobtind
from ..utils import TYPE_CHECKING#, np, LineStyle, LineDash

if TYPE_CHECKING:
    from typing_ import *
    from .core import *
    
class Pine:
    
    _perfixes: str = "pine_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data
    
    @tobtind(overlap=False, lib="pine")
    def normalize(self, lo=0.0, hi=1.0, **kwargs) -> IndSeries:
        """MLExtensions normalize: 扩展窗口历史 min/max 归一化
        所需字段：close"""
        ...
        
    @tobtind(overlap=False, lib="pine")
    def cci(self, n: int = 10,**kwargs) -> IndSeries:
        """Pine ta.cci: (src - sma) / (0.015 * mean|src - sma|)
        所需字段：close"""
        ...
        
    @tobtind(overlap=False, lib="pine")
    def wavetrend(self,n1=10, n2=11,talib: bool | None = None, **kwargs) -> IndSeries:
        """Pine ta.wavetrend: 波形趋势
        所需字段：high,low,close"""
        ...
        
    @tobtind(overlap=True, lib="pine")
    def rma(self, n=20,**kwargs) -> IndSeries:
        """Pine ta.rma: alpha=1/n, 以最初 n 个非NaN值的 SMA 播种, 之后递归
        所需字段：close"""
        ...
        
    @tobtind(overlap=False, lib="pine")
    def adx_wilder(self, n=20,**kwargs) -> IndSeries:
        """MLExtensions 风格的 Wilder ADX (累加器平滑 + rma)
        所需字段：high,low,close"""
        ...
    
    @tobtind(overlap=False, lib="pine")
    def barssince(self,**kwargs) -> IndSeries:
        """Pine ta.barssince: 距上次 cond 为 True 的 bar 数, 从未发生为 NaN
        所需字段：close"""
        ...
        
    @tobtind(overlap=False, lib="pine")
    def rsi(self, n=20,**kwargs) -> IndSeries:
        """Pine ta.rsi: Wilder 平滑 RSI
        所需字段：close"""
        ...
        
    @tobtind(overlap=True, lib="pine")
    def ema(self, n=20,**kwargs) -> IndSeries:
        """Pine ta.ema: Wilder 平滑 EMA
        所需字段：close"""
        ...

    # ==================== Pine ta.* 全量补全 ====================

    # ---- 均线 / 平滑 ----
    @tobtind(overlap=True, lib="pine")
    def sma(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.sma: 简单移动平均。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def wma(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.wma: 线性加权移动平均。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def hma(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.hma: Hull 移动平均。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def vwma(self, length=20, **kwargs) -> IndSeries:
        """Pine ta.vwma: 成交量加权移动平均。所需字段：close, volume"""
        ...

    @tobtind(overlap=True, lib="pine")
    def swma(self, **kwargs) -> IndSeries:
        """Pine ta.swma: 对称加权均线。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def alma(self, length=9, offset=0.85, sigma=6.0, **kwargs) -> IndSeries:
        """Pine ta.alma: Arnaud Legoux 均线。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def linreg(self, length=14, offset=0, **kwargs) -> IndSeries:
        """Pine ta.linreg: 线性回归末端值。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def median(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.median: 滚动中位数。所需字段：close"""
        ...

    # ---- 变换 / 差分 ----
    @tobtind(overlap=False, lib="pine")
    def cum(self, **kwargs) -> IndSeries:
        """Pine ta.cum: 累计和。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def change(self, length=1, **kwargs) -> IndSeries:
        """Pine ta.change: source - source[length]。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def roc(self, length=1, **kwargs) -> IndSeries:
        """Pine ta.roc: 变动率（%）。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def mom(self, length=1, **kwargs) -> IndSeries:
        """Pine ta.mom: 动量。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def cog(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.cog: 重心振荡器。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def range(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.range: highest - lowest。所需字段：close"""
        ...

    # ---- 波动率 / 统计 ----
    @tobtind(overlap=True, lib="pine")
    def tr(self, handle_na=False, **kwargs) -> IndSeries:
        """Pine ta.tr: 真实波幅。所需字段：high, low, close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def atr(self, length=14, handle_na=True, **kwargs) -> IndSeries:
        """Pine ta.atr: rma(tr(true), length)。所需字段：high, low, close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def stdev(self, length=10, biased=True, **kwargs) -> IndSeries:
        """Pine ta.stdev: 标准差。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def variance(self, length=10, biased=True, **kwargs) -> IndSeries:
        """Pine ta.variance: 方差。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def dev(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.dev: 平均绝对偏差。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def correlation(self, b=None, length=10, **kwargs) -> IndSeries:
        """Pine ta.correlation: 滚动皮尔逊相关。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def percentrank(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.percentrank: 百分位排名。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def percentile_nearest_rank(self, length=10, percentage=50.0, **kwargs) -> IndSeries:
        """Pine ta.percentile_nearest_rank: 最近秩百分位值。所需字段：close"""
        ...

    # ---- 极值 ----
    @tobtind(overlap=True, lib="pine")
    def highest(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.highest: 窗口最高值。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def lowest(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.lowest: 窗口最低值。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def highestbars(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.highestbars: 距最高值的 bar 数（0=当前最高）。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def lowestbars(self, length=10, **kwargs) -> IndSeries:
        """Pine ta.lowestbars: 距最低值的 bar 数（0=当前最低）。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def max(self, b=None, **kwargs) -> IndSeries:
        """Pine ta.max: 逐点较大值。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def min(self, b=None, **kwargs) -> IndSeries:
        """Pine ta.min: 逐点较小值。所需字段：close"""
        ...

    # ---- 交叉 / 逻辑 ----
    @tobtind(overlap=False, lib="pine")
    def cross(self, b=None, **kwargs) -> IndSeries:
        """Pine ta.cross: 双向穿越。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def crossover(self, b=None, **kwargs) -> IndSeries:
        """Pine ta.crossover: 上穿。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def crossunder(self, b=None, **kwargs) -> IndSeries:
        """Pine ta.crossunder: 下穿。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def rising(self, length=1, **kwargs) -> IndSeries:
        """Pine ta.rising: 连续上涨。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def falling(self, length=1, **kwargs) -> IndSeries:
        """Pine ta.falling: 连续下跌。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def valuewhen(self, cond=None, occurrence=0, **kwargs) -> IndSeries:
        """Pine ta.valuewhen: 条件成立时的 source（默认 close）。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def pivothigh(self, leftbars=5, rightbars=5, **kwargs) -> IndSeries:
        """Pine ta.pivothigh: 摆动高点。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def pivotlow(self, leftbars=5, rightbars=5, **kwargs) -> IndSeries:
        """Pine ta.pivotlow: 摆动低点。所需字段：close"""
        ...

    # ---- 振荡器 ----
    @tobtind(overlap=False, lib="pine")
    def cmo(self, length=14, **kwargs) -> IndSeries:
        """Pine ta.cmo: Chande 动量振荡器。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def wpr(self, length=14, **kwargs) -> IndSeries:
        """Pine ta.wpr: Williams %R。所需字段：high, low, close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def mfi(self, length=14, **kwargs) -> IndSeries:
        """Pine ta.mfi: 资金流量指标。所需字段：high, low, close, volume"""
        ...

    @tobtind(overlap=False, lib="pine")
    def rci(self, length=14, **kwargs) -> IndSeries:
        """Pine ta.rci: 秩相关指数。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def tsi(self, short_length=13, long_length=25, **kwargs) -> IndSeries:
        """Pine ta.tsi: 真实强度指数。所需字段：close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def accdist(self, **kwargs) -> IndSeries:
        """Pine ta.accdist: 累积/派发线。所需字段：high, low, close, volume"""
        ...

    @tobtind(overlap=True, lib="pine")
    def vwap(self, **kwargs) -> IndSeries:
        """Pine ta.vwap: 成交量加权均价。所需字段：high, low, close, volume"""
        ...

    @tobtind(overlap=False, lib="pine")
    def stoch(self, length=14, **kwargs) -> IndSeries:
        """Pine ta.stoch: 随机指标 %K。所需字段：high, low, close"""
        ...

    @tobtind(overlap=False, lib="pine")
    def bbw(self, length=20, mult=2.0, **kwargs) -> IndSeries:
        """Pine ta.bbw: 布林带宽度。所需字段：close"""
        ...

    # ---- 多输出 ----
    @tobtind(lines=['bb_middle', 'bb_upper', 'bb_lower'], overlap=True, lib="pine")
    def bb(self, length=20, mult=2.0, **kwargs) -> IndFrame:
        """Pine ta.bb: 布林带（mid/upper/lower）。所需字段：close"""
        ...

    @tobtind(lines=['macd', 'signal', 'hist'], overlap=False, lib="pine")
    def macd(self, fast=12, slow=26, signal=9, **kwargs) -> IndFrame:
        """Pine ta.macd: MACD（macd/signal/hist）。所需字段：close"""
        ...

    @tobtind(overlap=True, lib="pine")
    def sar(self, start=0.02, inc=0.02, maximum=0.2, **kwargs) -> IndSeries:
        """Pine ta.sar: 抛物线 SAR。所需字段：high, low"""
        ...

    @tobtind(lines=['plus_di', 'minus_di', 'adx'], overlap=False, lib="pine")
    def dmi(self, di_length=14, adx_length=14, **kwargs) -> IndFrame:
        """Pine ta.dmi: 动向指标（+DI/-DI/ADX）。所需字段：high, low, close"""
        ...
        