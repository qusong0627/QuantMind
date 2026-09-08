from __future__ import annotations
from .base import tobtind
from ..utils import TYPE_CHECKING
if TYPE_CHECKING:
    from typing_ import *
    from .core import *

class TuLip:
    """## Tulip Indicators
    https://tulipindicators.org/"""
    _perfixes: str = "ti_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @tobtind(lib="ti")
    def abs(self, **kwargs) -> IndSeries:
        """## Vector Absolute Value
        https://tulipindicators.org/abs

        计算向量的绝对值。

        Returns:
            IndSeries: 输入序列的绝对值结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def acos(self, **kwargs) -> IndSeries:
        """## Vector Arccosine
        https://tulipindicators.org/acos

        计算向量的反余弦值。

        Returns:
            IndSeries: 输入序列的反余弦结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def ad(self, **kwargs) -> IndSeries:
        """## Accumulation/Distribution Line
        https://tulipindicators.org/ad

        计算累积/派发线指标。

        Returns:
            IndSeries: 累积/派发线的计算结果。

        Note:
            实例包含列：high, low, close, volume
        """
        ...

    @tobtind(lib="ti")
    def add(self, series=None, **kwargs) -> IndSeries:
        """## Vector Addition
        https://tulipindicators.org/add

        实现向量加法运算。

        Args:
            IndSeries (int | float | IndSeries | np.ndarray): 用于加法运算的数值或序列

        Returns:
            IndSeries: 向量加法的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def adosc(self, short_period=10, long_period=10, **kwargs) -> IndSeries:
        """## Accumulation/Distribution Oscillator
        https://tulipindicators.org/adosc

        计算累积/派发震荡指标。

        Args:
            short_period (int): 短期周期，默认值为10
            long_period (int): 长期周期，默认值为10

        Returns:
            IndSeries: 累积/派发震荡指标的计算结果。

        Note:
            实例包含列：high, low, close, volume
        """
        ...

    @tobtind(lib="ti")
    def adx(self, period=10, **kwargs) -> IndSeries:
        """## Average Directional Movement Index
        https://tulipindicators.org/adx

        计算平均趋向指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 平均趋向指标的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def adxr(self, period=10, **kwargs) -> IndSeries:
        """## Average Directional Movement Rating
        https://tulipindicators.org/adxr

        计算平均趋向指标评级。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 平均趋向指标评级的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def ao(self, **kwargs) -> IndSeries:
        """## Awesome Oscillator
        https://tulipindicators.org/ao

        计算动量震荡指标。

        Returns:
            IndSeries: 动量震荡指标的计算结果。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def apo(self, short_period=10, long_period=10, **kwargs) -> IndSeries:
        """## Absolute Price Oscillator
        https://tulipindicators.org/apo

        计算绝对价格震荡指标。

        Args:
            short_period (int): 短期周期，默认值为10
            long_period (int): 长期周期，默认值为10

        Returns:
            IndSeries: 绝对价格震荡指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=["aroondown", "aroonup"], lib="ti")
    def aroon(self, period=10, **kwargs) -> IndFrame:
        """## Aroon
        https://tulipindicators.org/aroon

        计算阿隆指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndFrame: 包含阿隆下行（aroondown）和阿隆上行（aroonup）的结果数据框。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def aroonosc(self, period=10, **kwargs) -> IndSeries:
        """## Aroon Oscillator
        https://tulipindicators.org/aroonosc

        计算阿隆震荡指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 阿隆震荡指标的计算结果。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def asin(self, **kwargs) -> IndSeries:
        """## Vector Arcsine
        https://tulipindicators.org/asin

        计算向量的反正弦值。

        Returns:
            IndSeries: 输入序列的反正弦结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def atan(self, **kwargs) -> IndSeries:
        """## Vector Arctangent
        https://tulipindicators.org/atan

        计算向量的反正切值。

        Returns:
            IndSeries: 输入序列的反正切结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def atr(self, period=10, **kwargs) -> IndSeries:
        """## Average True Range
        https://tulipindicators.org/atr

        计算平均真实波幅。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 平均真实波幅的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def avgprice(self, **kwargs) -> IndSeries:
        """## Average Price
        https://tulipindicators.org/avgprice

        计算平均价格。

        Returns:
            IndSeries: 平均价格的计算结果。

        Note:
            实例包含列：open, high, low, close
        """
        ...

    @tobtind(lines=["lowerband", "middleband", "upperband"], lib="ti")
    def bbands(self, period=10, stddev=1., **kwargs) -> IndFrame:
        """## Bollinger Bands
        https://tulipindicators.org/bbands

        计算布林带指标。

        Args:
            period (int): 计算周期，默认值为10
            stddev (float): 标准差倍数，默认值为1.0

        Returns:
            IndFrame: 包含上轨（upperband）、中轨（middleband）和下轨（lowerband）的结果数据框。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def bop(self, **kwargs) -> IndSeries:
        """## Balance of Power
        https://tulipindicators.org/bop

        计算动力平衡指标。

        Returns:
            IndSeries: 动力平衡指标的计算结果。

        Note:
            实例包含列：open, high, low, close
        """
        ...

    @tobtind(lib="ti")
    def cci(self, period=10, **kwargs) -> IndSeries:
        """## Commodity Channel Index
        https://tulipindicators.org/cci

        计算商品通道指数。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 商品通道指数的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def ceil(self, **kwargs) -> IndSeries:
        """## Vector Ceiling
        https://tulipindicators.org/ceil

        计算向量的向上取整值。

        Returns:
            IndSeries: 输入序列的向上取整结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def cmo(self, period=10, **kwargs) -> IndSeries:
        """## Chande Momentum Oscillator
        https://tulipindicators.org/cmo

        计算钱德动量震荡指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 钱德动量震荡指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def cos(self, **kwargs) -> IndSeries:
        """## Vector Cosine
        https://tulipindicators.org/cos

        计算向量的余弦值。

        Returns:
            IndSeries: 输入序列的余弦结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def cosh(self, **kwargs) -> IndSeries:
        """## Vector Hyperbolic Cosine
        https://tulipindicators.org/cosh

        计算向量的双曲余弦值。

        Returns:
            IndSeries: 输入序列的双曲余弦结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def crossany(self, series=None, **kwargs) -> IndSeries:
        """## Crossany
        https://tulipindicators.org/crossany

        判断向量是否与目标序列交叉。

        Args:
            IndSeries (IndSeries | np.ndarray | int | float): 用于交叉判断的目标序列或数值

        Returns:
            IndSeries: 交叉判断结果（布尔值序列）。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def crossover(self, series=None, **kwargs) -> IndSeries:
        """## Crossover
        https://tulipindicators.org/crossover

        判断向量是否上穿目标序列。

        Args:
            IndSeries (IndSeries | np.ndarray | int | float): 用于上穿判断的目标序列或数值

        Returns:
            IndSeries: 上穿判断结果（布尔值序列）。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def cvi(self, period=10, **kwargs) -> IndSeries:
        """## Chaikins Volatility
        https://tulipindicators.org/cvi

        计算柴金波动率指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 柴金波动率指标的计算结果。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def decay(self, period=10, **kwargs) -> IndSeries:
        """## Linear Decay
        https://tulipindicators.org/decay

        计算线性衰减值。

        Args:
            period (int): 衰减周期，默认值为10

        Returns:
            IndSeries: 线性衰减的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def dema(self, period=10, **kwargs) -> IndSeries:
        """## Double Exponential Moving Average
        https://tulipindicators.org/dema

        计算双指数移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 双指数移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=["plus_di", "minus_di"], lib="ti")
    def di(self, period=10, **kwargs) -> IndFrame:
        """## Directional Indicator
        https://tulipindicators.org/di

        计算趋向指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndFrame: 包含正趋向指标（plus_di）和负趋向指标（minus_di）的结果数据框。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def div(self, series=None, **kwargs) -> IndSeries:
        """## Vector Division
        https://tulipindicators.org/div

        实现向量除法运算。

        Args:
            IndSeries (IndSeries | np.ndarray | int | float): 用于除法运算的数值或序列

        Returns:
            IndSeries: 向量除法的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=['dmp', 'dmn'], lib="ti")
    def dm(self, period=10, **kwargs) -> IndFrame:
        """## Directional Movement
        https://tulipindicators.org/dm

        计算趋向运动值。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndFrame: 包含正趋向运动（dmp）和负趋向运动（dmn）的结果数据框。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def dpo(self, period=10, **kwargs) -> IndSeries:
        """## Detrended Price Oscillator
        https://tulipindicators.org/dpo

        计算去趋势价格震荡指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 去趋势价格震荡指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def dx(self, period=10, **kwargs) -> IndSeries:
        """## Directional Movement Index
        https://tulipindicators.org/dx

        计算趋向运动指数。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 趋向运动指数的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def edecay(self, period=10, **kwargs) -> IndSeries:
        """## Exponential Decay
        https://tulipindicators.org/edecay

        计算指数衰减值。

        Args:
            period (int): 衰减周期，默认值为10

        Returns:
            IndSeries: 指数衰减的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def ema(self, period=10, **kwargs) -> IndSeries:
        """## Exponential Moving Average
        https://tulipindicators.org/ema

        计算指数移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 指数移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def emv(self, **kwargs) -> IndSeries:
        """## Ease of Movement
        https://tulipindicators.org/emv

        计算简易移动指标。

        Returns:
            IndSeries: 简易移动指标的计算结果。

        Note:
            实例包含列：high, low, volume
        """
        ...

    @tobtind(lib="ti")
    def exp(self, **kwargs) -> IndSeries:
        """## Vector Exponential
        https://tulipindicators.org/exp

        计算向量的指数值。

        Returns:
            IndSeries: 输入序列的指数结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=['fisher', 'fishers'], lib="ti")
    def fisher(self, period=10, **kwargs) -> IndFrame:
        """## Fisher Transform
        https://tulipindicators.org/fisher

        计算费希尔变换指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndFrame: 包含费希尔变换值（fisher）和信号值（fishers）的结果数据框。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def floor(self, **kwargs) -> IndSeries:
        """## Vector Floor
        https://tulipindicators.org/floor

        计算向量的向下取整值。

        Returns:
            IndSeries: 输入序列的向下取整结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def fosc(self, period=10, **kwargs) -> IndSeries:
        """## Forecast Oscillator
        https://tulipindicators.org/fosc

        计算预测震荡指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 预测震荡指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def hma(self, period=10, **kwargs) -> IndSeries:
        """## Hull Moving Average
        https://tulipindicators.org/hma

        计算赫尔移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 赫尔移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def kama(self, period=10, **kwargs) -> IndSeries:
        """## Kaufman Adaptive Moving Average
        https://tulipindicators.org/kama

        计算考夫曼自适应移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 考夫曼自适应移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def kvo(self, short_period=10, long_period=10, **kwargs) -> IndSeries:
        """## Klinger Volume Oscillator
        https://tulipindicators.org/kvo

        计算克林格成交量震荡指标。

        Args:
            short_period (int): 短期周期，默认值为10
            long_period (int): 长期周期，默认值为10

        Returns:
            IndSeries: 克林格成交量震荡指标的计算结果。

        Note:
            实例包含列：high, low, close, volume
        """
        ...

    @tobtind(lib="ti")
    def lag(self, period=10, **kwargs) -> IndSeries:
        """## Lag
        https://tulipindicators.org/lag

        计算序列的滞后值。

        Args:
            period (int): 滞后周期，默认值为10

        Returns:
            IndSeries: 滞后处理后的序列结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def linreg(self, period=10, **kwargs) -> IndSeries:
        """## Linear Regression
        https://tulipindicators.org/linreg

        计算线性回归拟合值。

        Args:
            period (int): 回归周期，默认值为10

        Returns:
            IndSeries: 线性回归拟合的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def linregintercept(self, period=10, **kwargs) -> IndSeries:
        """## Linear Regression Intercept
        https://tulipindicators.org/linregintercept

        计算线性回归截距。

        Args:
            period (int): 回归周期，默认值为10

        Returns:
            IndSeries: 线性回归截距的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def linregslope(self, period=10, **kwargs) -> IndSeries:
        """## Linear Regression Slope
        https://tulipindicators.org/linregslope

        计算线性回归斜率。

        Args:
            period (int): 回归周期，默认值为10

        Returns:
            IndSeries: 线性回归斜率的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def ln(self, **kwargs) -> IndSeries:
        """## Vector Natural Log
        https://tulipindicators.org/ln

        计算向量的自然对数。

        Returns:
            IndSeries: 输入序列的自然对数结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def log10(self, **kwargs) -> IndSeries:
        """## Vector Base-10 Log
        https://tulipindicators.org/log10

        计算向量的以10为底的对数。

        Returns:
            IndSeries: 输入序列的10底对数结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=["macdx", "macdh", "macds"], lib="ti")
    def macd(self, short_period=12, long_period=26, signal_period=9, **kwargs) -> IndFrame:
        """## Moving Average Convergence/Divergence
        https://tulipindicators.org/macd

        计算指数平滑异同移动平均线（MACD）。

        Args:
            short_period (int): 短期EMA周期，默认值为12
            long_period (int): 长期EMA周期，默认值为26
            signal_period (int): 信号EMA周期，默认值为9

        Returns:
            IndFrame: 包含MACD线（macdx）、MACD柱状线（macdh）和信号线（macds）的结果数据框。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def marketfi(self, **kwargs) -> IndSeries:
        """## Market Facilitation Index
        https://tulipindicators.org/marketfi

        计算市场便利指数。

        Returns:
            IndSeries: 市场便利指数的计算结果。

        Note:
            实例包含列：high, low, volume
        """
        ...

    @tobtind(lib="ti")
    def mass(self, period=10, **kwargs) -> IndSeries:
        """## Mass Index
        https://tulipindicators.org/mass

        计算质量指数。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 质量指数的计算结果。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def max(self, period=10, **kwargs) -> IndSeries:
        """## Maximum In Period
        https://tulipindicators.org/max

        计算周期内的最大值。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内最大值的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def md(self, period=10, **kwargs) -> IndSeries:
        """## Mean Deviation Over Period
        https://tulipindicators.org/md

        计算周期内的平均偏差。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内平均偏差的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def medprice(self, **kwargs) -> IndSeries:
        """## Median Price
        https://tulipindicators.org/medprice

        计算中位数价格。

        Returns:
            IndSeries: 中位数价格的计算结果。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def mfi(self, period=10, **kwargs) -> IndSeries:
        """## Money Flow Index
        https://tulipindicators.org/mfi

        计算资金流向指数。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 资金流向指数的计算结果。

        Note:
            实例包含列：high, low, close, volume
        """
        ...

    @tobtind(lib="ti")
    def min(self, period=10, **kwargs) -> IndSeries:
        """## Minimum In Period
        https://tulipindicators.org/min

        计算周期内的最小值。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内最小值的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def mom(self, period=10, **kwargs) -> IndSeries:
        """## Momentum
        https://tulipindicators.org/mom

        计算动量指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 动量指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=["msw_sine", "msw_lead"], lib="ti")
    def msw(self, period=10, **kwargs) -> IndFrame:
        """## Mesa Sine Wave
        https://tulipindicators.org/msw

        计算梅萨正弦波指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndFrame: 包含正弦波值（msw_sine）和领先值（msw_lead）的结果数据框。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def mul(self, series=None, **kwargs) -> IndSeries:
        """## Vector Multiplication
        https://tulipindicators.org/mul

        实现向量乘法运算。

        Args:
            IndSeries (IndSeries | np.ndarray | int | float): 用于乘法运算的数值或序列

        Returns:
            IndSeries: 向量乘法的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def natr(self, period=10, **kwargs) -> IndSeries:
        """## Normalized Average True Range
        https://tulipindicators.org/natr

        计算归一化平均真实波幅。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 归一化平均真实波幅的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def nvi(self, **kwargs) -> IndSeries:
        """## Negative Volume Index
        https://tulipindicators.org/nvi

        计算负成交量指数。

        Returns:
            IndSeries: 负成交量指数的计算结果。

        Note:
            实例包含列：close, volume
        """
        ...

    @tobtind(lib="ti")
    def obv(self, **kwargs) -> IndSeries:
        """## On Balance Volume
        https://tulipindicators.org/obv

        计算能量潮指标。

        Returns:
            IndSeries: 能量潮指标的计算结果。

        Note:
            实例包含列：close, volume
        """
        ...

    @tobtind(lib="ti")
    def ppo(self, short_period=10, long_period=10, **kwargs) -> IndSeries:
        """## Percentage Price Oscillator
        https://tulipindicators.org/ppo

        计算百分比价格震荡指标。

        Args:
            short_period (int): 短期周期，默认值为10
            long_period (int): 长期周期，默认值为10

        Returns:
            IndSeries: 百分比价格震荡指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def psar(self, acceleration_factor_step=0.06185, acceleration_factor_maximum=0.6185, **kwargs):
        """## Parabolic SAR
        https://tulipindicators.org/psar

        计算抛物线转向指标（SAR）。

        Args:
            acceleration_factor_step (float): 加速因子步长，默认值为0.06185
            acceleration_factor_maximum (float): 加速因子最大值，默认值为0.6185

        Returns:
            IndSeries: 抛物线转向指标的计算结果。

        Note:
            实例包含列：high, low
        """
        ...

    @tobtind(lib="ti")
    def qstick(self, period=10, **kwargs) -> IndSeries:
        """## Qstick
        https://tulipindicators.org/qstick

        计算Qstick指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: Qstick指标的计算结果。

        Note:
            实例包含列：open, close
        """
        ...

    @tobtind(lib="ti")
    def roc(self, period=10, **kwargs) -> IndSeries:
        """## Rate of Change
        https://tulipindicators.org/roc

        计算变化率指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 变化率指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def rocr(self, period=10, **kwargs) -> IndSeries:
        """## Rate of Change Ratio
        https://tulipindicators.org/rocr

        计算变化率比率指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 变化率比率指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def round(self, **kwargs) -> IndSeries:
        """## Vector Round
        https://tulipindicators.org/round

        计算向量的四舍五入值。

        Returns:
            IndSeries: 输入序列的四舍五入结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def rsi(self, period=10, **kwargs) -> IndSeries:
        """## Relative Strength Index
        https://tulipindicators.org/rsi

        计算相对强弱指数。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 相对强弱指数的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def sin(self, **kwargs) -> IndSeries:
        """## Vector Sine
        https://tulipindicators.org/sin

        计算向量的正弦值。

        Returns:
            IndSeries: 输入序列的正弦结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def sinh(self, **kwargs) -> IndSeries:
        """## Vector Hyperbolic Sine
        https://tulipindicators.org/sinh

        计算向量的双曲正弦值。

        Returns:
            IndSeries: 输入序列的双曲正弦结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def sma(self, period=10, **kwargs) -> IndSeries:
        """## Simple Moving Average
        https://tulipindicators.org/sma

        计算简单移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 简单移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def sqrt(self, **kwargs) -> IndSeries:
        """## Vector Square Root
        https://tulipindicators.org/sqrt

        计算向量的平方根。

        Returns:
            IndSeries: 输入序列的平方根结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def stddev(self, period=10, **kwargs) -> IndSeries:
        """## Standard Deviation Over Period
        https://tulipindicators.org/stddev

        计算周期内的标准差。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内标准差的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def stderr(self, period=10, **kwargs) -> IndSeries:
        """## Standard Error Over Period
        https://tulipindicators.org/stderr

        计算周期内的标准误差。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内标准误差的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lines=["stoch_k", "stoch_d"], lib="ti")
    def stoch(self, pct_k_period=5, pct_k_slowing_period=3, pct_d_period=3, **kwargs) -> IndFrame:
        """## Stochastic Oscillator
        https://tulipindicators.org/stoch

        计算随机震荡指标。

        Args:
            pct_k_period (int): %K周期，默认值为5
            pct_k_slowing_period (int): %K放缓周期，默认值为3
            pct_d_period (int): %D周期，默认值为3

        Returns:
            IndFrame: 包含%K值（stoch_k）和%D值（stoch_d）的结果数据框。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def stochrsi(self, period=10, **kwargs) -> IndSeries:
        """## Stochastic RSI
        https://tulipindicators.org/stochrsi

        计算随机相对强弱指数。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 随机相对强弱指数的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def sub(self, series=None, **kwargs) -> IndSeries:
        """## Vector Subtraction
        https://tulipindicators.org/sub

        实现向量减法运算。

        Args:
            IndSeries (IndSeries | np.ndarray | int | float): 用于减法运算的数值或序列

        Returns:
            IndSeries: 向量减法的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def sum(self, period=10, **kwargs) -> IndSeries:
        """## Sum Over Period
        https://tulipindicators.org/sum

        计算周期内的总和。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内总和的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def tan(self, **kwargs) -> IndSeries:
        """## Vector Tangent
        https://tulipindicators.org/tan

        计算向量的正切值。

        Returns:
            IndSeries: 输入序列的正切结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def tanh(self, **kwargs) -> IndSeries:
        """## Vector Hyperbolic Tangent
        https://tulipindicators.org/tanh

        计算向量的双曲正切值。

        Returns:
            IndSeries: 输入序列的双曲正切结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def tema(self, period=10, **kwargs) -> IndSeries:
        """## Triple Exponential Moving Average
        https://tulipindicators.org/tema

        计算三重指数移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 三重指数移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def todeg(self, **kwargs) -> IndSeries:
        """## Vector Degree Conversion
        https://tulipindicators.org/todeg

        将弧度转换为角度。

        Returns:
            IndSeries: 弧度转角度的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def torad(self, **kwargs) -> IndSeries:
        """## Vector Radian Conversion
        https://tulipindicators.org/torad

        将角度转换为弧度。

        Returns:
            IndSeries: 角度转弧度的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def tr(self, **kwargs) -> IndSeries:
        """## True Range
        https://tulipindicators.org/tr

        计算真实波幅。

        Returns:
            IndSeries: 真实波幅的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def trima(self, period=10, **kwargs) -> IndSeries:
        """## Triangular Moving Average
        https://tulipindicators.org/trima

        计算三角形移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 三角形移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def trix(self, period=10, **kwargs) -> IndSeries:
        """## Trix
        https://tulipindicators.org/trix

        计算三重指数平滑指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 三重指数平滑指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def trunc(self, **kwargs) -> IndSeries:
        """## Vector Truncate
        https://tulipindicators.org/trunc

        计算向量的截断值（向零取整）。

        Returns:
            IndSeries: 输入序列的截断结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def tsf(self, period=10, **kwargs) -> IndSeries:
        """## Time Series Forecast
        https://tulipindicators.org/tsf

        计算时间序列预测值。

        Args:
            period (int): 预测周期，默认值为10

        Returns:
            IndSeries: 时间序列预测的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def typprice(self, **kwargs) -> IndSeries:
        """## Typical Price
        https://tulipindicators.org/typprice

        计算典型价格。

        Returns:
            IndSeries: 典型价格的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def ultosc(self, short_period=2, medium_period=3, long_period=5, **kwargs) -> IndSeries:
        """## Ultimate Oscillator
        https://tulipindicators.org/ultosc

        计算终极震荡指标。

        Args:
            short_period (int): 短期周期，默认值为2
            medium_period (int): 中期周期，默认值为3
            long_period (int): 长期周期，默认值为5

        Returns:
            IndSeries: 终极震荡指标的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def var(self, period=10, **kwargs) -> IndSeries:
        """## Variance Over Period
        https://tulipindicators.org/var

        计算周期内的方差。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 周期内方差的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def vhf(self, period=10, **kwargs) -> IndSeries:
        """## Vertical Horizontal Filter
        https://tulipindicators.org/vhf

        计算垂直水平过滤器指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 垂直水平过滤器指标的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def vidya(self, short_period=5, long_period=10, alpha=0.2, **kwargs) -> IndSeries:
        """## Variable Index Dynamic Average
        https://tulipindicators.org/vidya

        计算可变指数动态平均线。

        Args:
            short_period (int): 短期周期，默认值为5
            long_period (int): 长期周期，默认值为10
            alpha (float): 平滑系数，默认值为0.2

        Returns:
            IndSeries: 可变指数动态平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def volatility(self, period=10, **kwargs) -> IndSeries:
        """## Annualized Historical Volatility
        https://tulipindicators.org/volatility

        计算年化历史波动率。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 年化历史波动率的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def vosc(self, short_period=2, long_period=5, **kwargs) -> IndSeries:
        """## Volume Oscillator
        https://tulipindicators.org/vosc

        计算成交量震荡指标。

        Args:
            short_period (int): 短期周期，默认值为2
            long_period (int): 长期周期，默认值为5

        Returns:
            IndSeries: 成交量震荡指标的计算结果。

        Note:
            实例包含列：volume
        """
        ...

    @tobtind(lib="ti")
    def vwma(self, period=10, **kwargs) -> IndSeries:
        """## Volume Weighted Moving Average
        https://tulipindicators.org/vwma

        计算成交量加权移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 成交量加权移动平均线的计算结果。

        Note:
            实例包含列：close, volume
        """
        ...

    @tobtind(lib="ti")
    def wad(self, **kwargs) -> IndSeries:
        """## Williams Accumulation/Distribution
        https://tulipindicators.org/wad

        计算威廉姆斯累积/派发指标。

        Returns:
            IndSeries: 威廉姆斯累积/派发指标的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def wcprice(self, **kwargs) -> IndSeries:
        """## Weighted Close Price
        https://tulipindicators.org/wcprice

        计算加权收盘价。

        Returns:
            IndSeries: 加权收盘价的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def wilders(self, period=10, **kwargs) -> IndSeries:
        """## Wilders Smoothing
        https://tulipindicators.org/wilders

        计算威尔德平滑值。

        Args:
            period (int): 平滑周期，默认值为10

        Returns:
            IndSeries: 威尔德平滑的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def willr(self, period=10, **kwargs) -> IndSeries:
        """## Williams %R
        https://tulipindicators.org/willr

        计算威廉姆斯%R指标。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 威廉姆斯%R指标的计算结果。

        Note:
            实例包含列：high, low, close
        """
        ...

    @tobtind(lib="ti")
    def wma(self, period=10, **kwargs) -> IndSeries:
        """## Weighted Moving Average
        https://tulipindicators.org/wma

        计算加权移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 加权移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...

    @tobtind(lib="ti")
    def zlema(self, period=10, **kwargs) -> IndSeries:
        """## Zero-Lag Exponential Moving Average
        https://tulipindicators.org/zlema

        计算零滞后指数移动平均线。

        Args:
            period (int): 计算周期，默认值为10

        Returns:
            IndSeries: 零滞后指数移动平均线的计算结果。

        Note:
            实例包含列：close
        """
        ...
