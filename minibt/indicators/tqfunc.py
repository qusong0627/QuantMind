from __future__ import annotations
from .base import tobtind
from ..utils import TYPE_CHECKING
if TYPE_CHECKING:
    from typing_ import *
    from .core import *


class TqFunc:
    """## 天勤序列计算函数类
    - 封装天勤量化(TqSdk)的序列计算函数库，为技术指标和策略开发提供基础数学运算能力。

    ### 📘 **文档参考**:
    - API参考：https://www.minibt.cn/minibt_api_reference/tqfunc/
    - 天勤文档：https://tqsdk-python.readthedocs.io/en/latest/reference/tqsdk.tafunc.html

    ### 核心功能：
    - 序列位移计算：提供时间序列的滞后、超前等位移操作
    - 统计量计算：包含均值、标准差、极值等统计函数
    - 逻辑判断：支持交叉信号、条件计数等逻辑运算
    - 移动平均：多种类型的移动平均计算方法
    - 时间处理：时间格式转换和时间戳处理工具

    ### 使用说明：
    1. 初始化：传入minibt框架兼容的Series或DataFrame数据对象
    >>> # 类调用
        data = IndFrame(...)  # 包含OHLCV等基础字段的minibt数据对象
        tqfunc = TqFunc(data)
        # 通过指标调用
        self.kline.close.tqfunc

    2. 函数调用：直接调用对应函数方法，支持参数自定义
    >>> prev_close = close.tqfunc.ref(length=1)        # 获取前一期收盘价
        ma_20 = close.tqfunc.ma(length=20)             # 20周期简单移动平均


    ### 技术特点：
    - 天勤兼容：基于天勤官方函数库，确保计算准确性
    - 序列优化：针对金融时间序列数据特殊优化
    - 向量计算：支持批量数据处理，计算效率高
    - 边界处理：自动处理数据边界和缺失值情况
    - 类型安全：确保输入输出数据格式一致性
    """
    _perfixes: str = "tqfunc_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @tobtind(lib='tqfunc')
    def ref(self, length=10, **kwargs) -> IndSeries:
        """
        ## 序列位移函数 - 计算序列的滞后值，获取指定周期前的数据

        功能：
            将输入序列向右平移指定周期数，实现滞后计算，用于获取历史数据参考

        应用场景：
            - 计算价格变化率（当前价 vs 前期价）
            - 构建动量指标
            - 计算技术指标的信号线
            - 数据预处理和特征工程

        计算原理：
            ref(series, n)[i] = series[i - n]
            将序列向右移动n个位置，前n个位置填充NaN

        参数：
            length: 位移周期数，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 当length=0时，返回原序列
            - 当序列长度不足length+1时，返回NaN
            - 默认使用实例初始化时的数据列

        返回值：
            IndSeries: 位移后的序列

        >>> # 示例：
            # 计算涨跌幅 = (当前收盘价 - 前一日收盘价) / 前一日收盘价
            prev_close = close.tqfunc.ref(length=1)
            price_change = (close - prev_close) / prev_close
            # 动量计算（当前价格与N日前价格比较）
            price_10_days_ago = close.tqfunc.ref(length=10)
            momentum_10 = close - price_10_days_ago
        """
        ...

    @tobtind(lib='tqfunc')
    def std(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 标准差计算 - 求序列每n个周期的标准差

        功能：
            计算滚动窗口内的标准差，衡量数据的离散程度

        应用场景：
            - 波动率测量和风险评估
            - 布林带宽度计算
            - 数据异常检测
            - 技术指标的稳定性评估

        计算原理：
            std(x, n) = sqrt(sum((x - mean(x, n))^2) / (n - 1))
            计算无偏估计的标准差

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 使用n-1作为分母（样本标准差）
            - 窗口长度不足时返回NaN
            - 对异常值比平均绝对偏差更敏感

        返回值：
            IndSeries: 标准差序列

        >>> 示例：
            # 计算价格波动率
            price_volatility = close.tqfunc.std(length=20)
            # 构建布林带
            middle_band = close.tqfunc.ma(length=20)
            std_dev = close.tqfunc.std(length=20)
            upper_band = middle_band + 2 * std_dev
            lower_band = middle_band - 2 * std_dev
            # 波动率突破策略
            low_vol_period = price_volatility < price_volatility.tqfunc.ma(length=50)
            high_vol_period = price_volatility > price_volatility.tqfunc.ma(length=50)
        """

    @tobtind(overlap=True, lib='tqfunc')
    def ma(self, length: int = 10, **kwargs) -> IndSeries:
        """
        简单移动平均函数 - 计算序列的简单移动平均值

        功能：
            计算滚动窗口内的算术平均值，是最基础的平滑和趋势识别工具

        应用场景：
            - 价格趋势识别和确认
            - 支撑阻力位构建
            - 均线交叉策略
            - 数据平滑和噪音过滤

        计算原理：
            ma(x, n) = (x₁ + x₂ + ... + xₙ) / n
            计算窗口内所有值的简单算术平均

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 所有历史数据权重相等
            - 对价格突变的反应较慢
            - 窗口长度不足时返回NaN

        返回值：
            IndSeries: 简单移动平均值序列

        >>> 示例：
            # 计算基础移动平均线
            ma_20 = close.tqfunc.ma(length=20)
            ma_50 = close.tqfunc.ma(length=50)
            # 均线交叉策略
            golden_cross = ma_20.tqfunc.crossup(ma_50)
            death_cross = ma_20.tqfunc.crossdown(ma_50)
            # 价格与均线关系分析
            above_ma = close > ma_20
            below_ma = close < ma_20
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def sma(self, n: int = 10, m: int = 2, **kwargs) -> IndSeries:
        """
        ## 扩展指数加权移动平均函数 - 计算序列的扩展指数加权移动平均值

        功能：
            结合简单移动平均和指数移动平均的特点，提供可调节的平滑程度

        应用场景：
            - 需要调节平滑程度的技术指标
            - 自适应趋势跟踪系统
            - 自定义滤波器设计
            - 多时间框架分析

        计算原理：
            sma(x, n, m) = sma(x, n, m).shift(1) × (n - m)/n + x(n) × m/n
            递归计算，结合历史值和当前值的加权平均

        参数：
            n: 计算周期，默认10
            m: 平滑系数，默认2
            **kwargs: 额外参数，可指定其他序列

        注意：
            - n必须大于m
            - m值越大，对近期数据权重越高
            - 提供了介于SMA和EMA之间的平滑特性

        返回值：
            IndSeries: 扩展指数加权移动平均值序列

        >>> 示例：
            # 计算扩展指数移动平均
            sma_fast = close.tqfunc.sma(n=5, m=3)   # 较快响应
            sma_slow = close.tqfunc.sma(n=20, m=5)  # 较慢响应
            # 自适应趋势系统
            trend_strength = ...  # 趋势强度指标
            adaptive_sma = close.tqfunc.sma(n=10, m=trend_strength)
            # 多参数组合分析
            for m_val in [1, 2, 3]:
                sma_IndSeries = close.tqfunc.sma(n=10, m=m_val)
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def ema(self, length: int = 10, **kwargs) -> IndSeries:
        """
        指数加权移动平均函数 - 计算序列的指数加权移动平均值

        功能：
            给予近期数据更高权重，更快响应价格变化，减少滞后性

        应用场景：
            - 快速趋势识别
            - 短线交易信号
            - 动量指标计算
            - 实时交易系统

        计算原理：
            ema(x, n) = 2/(n+1) × x + (1 - 2/(n+1)) × ema(x, n).shift(1)
            指数衰减权重，近期数据影响更大

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 对近期价格变化更敏感
            - 滞后性小于简单移动平均
            - 需要足够的初始数据建立稳定值

        返回值：
            IndSeries: 指数加权移动平均值序列

        >>> 示例：
            # 计算指数移动平均线
            ema_12 = close.tqfunc.ema(length=12)
            ema_26 = close.tqfunc.ema(length=26)
            # MACD指标计算
            macd_line = ema_12 - ema_26
            signal_line = macd_line.tqfunc.ema(length=9)
            # 快速趋势识别
            price_above_ema = close > ema_12
            ema_rising = ema_12 > ema_12.tq.ref(1)
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def ema2(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 线性加权移动平均函数 - 计算序列的线性加权移动平均值

        功能：
            使用线性递减权重，平衡响应速度和平滑程度

        应用场景：
            - 需要线性权重衰减的技术分析
            - 平衡滞后性和噪音过滤
            - 传统技术指标计算
            - 多权重系统设计

        计算原理：
            ema2(x, n) = [n·x₀ + (n-1)·x₁ + ... + 1·xₙ₋₁] / [n + (n-1) + ... + 1]
            使用线性递减的权重系数

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 权重线性递减，最近数据权重最大
            - 平滑程度介于SMA和EMA之间
            - 窗口长度不足时返回NaN

        返回值：
            IndSeries: 线性加权移动平均值序列

        >>> 示例：
            # 计算线性加权移动平均
            wma_20 = close.tqfunc.ema2(length=20)
            # 与其它移动平均比较
            sma_20 = close.tqfunc.ma(length=20)
            ema_20 = close.tqfunc.ema(length=20)
            # 构建WMA通道
            upper_wma = high.tqfunc.ema2(length=20)
            lower_wma = low.tqfunc.ema2(length=20)
            # 线性加权动量
            wma_momentum = wma_20 - wma_20.tq.ref(1)
        """
        ...

    @tobtind(lib='tqfunc')
    def crossup(self, b=None, **kwargs) -> IndSeries:
        """
        ## 向上穿越判断 - 判断序列a是否从下向上穿越序列b

        功能：
            检测序列a从下方穿越序列b的时点，生成金叉信号

        应用场景：
            - 均线金叉信号识别
            - 指标突破判断
            - 趋势反转确认
            - 买入信号生成

        计算原理：
            crossup(a, b)[i] = 1 如果 a[i] > b[i] 且 a[i-1] <= b[i-1]，否则为0
            严格判断向上穿越的时点

        参数：
            b: 被穿越的序列b
            **kwargs: 额外参数

        注意：
            - 返回布尔序列（1表示穿越发生，0表示未发生）
            - 只在穿越发生的时点返回1
            - 需要两个序列长度一致

        返回值：
            IndSeries: 向上穿越标志序列（1/0）

        >>> 示例：
            # 均线金叉信号
            ma_fast = close.tqfunc.ma(close, length=5)
            ma_slow = close.tqfunc.ma(close, length=20)
            golden_cross = ma_fast.tqfunc.crossup(ma_slow)
            # 价格突破阻力位
            resistance = high.tqfunc.hhv(length=20)
            breakout_signal = close.tqfunc.crossup(resistance)
            # 指标突破信号
            rsi_signal = rsi.tqfunc.crossup(30)  # RSI从超卖区域向上突破
        """
        ...

    @tobtind(lib='tqfunc')
    def crossdown(self, b=None, **kwargs) -> IndSeries:
        """
        ## 向下穿越判断 - 判断序列a是否从上向下穿越序列b

        功能：
            检测序列a从上方穿越序列b的时点，生成死叉信号

        应用场景：
            - 均线死叉信号识别
            - 支撑位跌破判断
            - 趋势反转确认
            - 卖出信号生成

        计算原理：
            crossdown(a, b)[i] = 1 如果 a[i] < b[i] 且 a[i-1] >= b[i-1]，否则为0
            严格判断向下穿越的时点

        参数：
            b: 被穿越的序列b
            **kwargs: 额外参数

        注意：
            - 返回布尔序列（1表示穿越发生，0表示未发生）
            - 只在穿越发生的时点返回1
            - 需要两个序列长度一致

        返回值：
            IndSeries: 向下穿越标志序列（1/0）

        >>> 示例：
            # 均线死叉信号
            ma_fast = close.tqfunc.ma(length=5)
            ma_slow = close.tqfunc.ma(length=20)
            death_cross = tqfunc.crossdown(ma_fast, ma_slow)
            # 价格跌破支撑位
            support = low.tqfunc.llv(length=20)
            breakdown_signal = close.tqfunc.crossdown(support)
            # 指标跌破信号
            rsi_signal = rsi.tqfunc.crossdown(70)  # RSI从超买区域向下跌破
        """
        ...

    @tobtind(lib='tqfunc')
    def count(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 条件计数统计 - 统计指定周期内满足条件的次数

        功能：
            计算滚动窗口内条件成立的次数，用于频率统计

        应用场景：
            - 统计信号出现的频率
            - 计算胜率和盈亏比
            - 条件发生的密度分析
            - 策略信号的强度评估

        计算原理：
            count(cond, n)[i] = 在[i-n+1, i]区间内cond为真的次数
            滑动窗口内的条件计数

        参数：
            self: 条件表达式
            length: 统计周期数，默认10
            **kwargs: 额外参数

        注意：
            - length=0时从第一个有效值开始统计
            - 条件cond应为布尔序列
            - 返回整数类型的计数序列

        返回值：
            IndSeries: 条件成立次数序列

        >>> 示例：
            # 统计近期上涨天数
            up_days = (close > close.tqfunc.ref(1)).tqfunc.count(length=10)
            # 计算技术指标信号的频率
            rsi_oversold = rsi < 30
            oversold_frequency = rsi_oversold.tqfunc.count(length=20)
            # 策略信号强度评估
            buy_signals = ...  # 买入信号条件
            signal_density = buy_signals.tqfunc.count(length=10)
            high_frequency_signal = signal_density >= 3  # 10期内至少3次信号
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def trma(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 三角移动平均 - 求序列的n周期三角移动平均值

        功能：
            计算双重平滑的移动平均，提供更平滑的趋势信号

        应用场景：
            - 低噪音趋势识别
            - 过滤市场短期波动
            - 长期投资决策参考
            - 趋势质量评估

        计算原理：
            trma(x, n) = ma(ma(x, n), n)
            对原始序列计算移动平均，再对结果计算移动平均

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 相当于两次简单移动平均
            - 比单次移动平均更平滑
            - 滞后性比单次移动平均更大

        返回值：
            IndSeries: 三角移动平均值序列

        >>> 示例：
            # 计算三角移动平均趋势
            trma_20 = close.tqfunc.trma(length=20)
            # 与简单移动平均比较
            sma_20 = close.tqfunc.ma(length=20)
            trma_20 = close.tqfunc.trma(length=20)
            # 三角移动平均通道
            upper_trma = high.tqfunc.trma(length=20)
            lower_trma = low.tqfunc.trma(length=20)
            middle_trma = close.tqfunc.trma(length=20)
            # 趋势确认系统
            price_above_trma = close > trma_20
            trma_rising = trma_20 > trma_20.tq.ref(1)
            confirmed_uptrend = price_above_trma & trma_rising
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def harmean(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 调和平均值函数 - 计算序列在指定周期内的调和平均值

        功能：
            计算滚动窗口内的调和平均值，对极值敏感，适用于比率数据的平均计算

        应用场景：
            - 计算价格比率的平均值
            - 投资组合的调和平均收益
            - 速度和时间相关指标的平均
            - 对异常值敏感的数据分析

        计算原理：
            harmean(x, n) = n / (1/x₁ + 1/x₂ + ... + 1/xₙ)
            计算窗口内各值倒数的算术平均值的倒数

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 调和平均值总是小于等于算术平均值
            - 对极值敏感，零值会导致计算错误
            - 窗口长度不足时返回NaN

        返回值：
            IndSeries: 调和平均值序列

        >>> 示例：
            # 计算价格调和平均趋势
            harmonic_mean = close.tqfunc.harmean(length=20)
            # 计算收益率调和平均
            returns = (close - close.tqfunc.ref(1)) / close.tq.ref(1)
            harmonic_return = (returns+1).tqfunc.harmean(length=10) - 1
            # 构建调和平均通道
            upper_harmonic = high.tqfunc.harmean(length=20)
            lower_harmonic = low.tqfunc.harmean(length=20)
        """
        ...

    @tobtind(lib='tqfunc')
    def numpow(self, n: int = 5, m: int = 2, **kwargs) -> IndSeries:
        """
        ## 自然数幂方和函数 - 计算序列的自然数幂方加权和

        功能：
            使用自然数的幂次作为权重，计算序列的加权和，用于特殊的技术指标计算

        应用场景：
            - 自定义技术指标计算
            - 特殊加权移动平均
            - 数学变换和特征工程
            - 高级滤波器的设计

        计算原理：
            numpow(x, n, m) = nᵐ·x₀ + (n-1)ᵐ·x₁ + ... + 1ᵐ·xₙ₋₁
            使用递减的自然数幂次作为权重系数

        参数：
            n: 自然数周期，默认5
            m: 幂次指数，默认2
            **kwargs: 额外参数，可指定其他序列

        注意：
            - n必须为正整数
            - m可以为任意实数
            - 序列长度不足时返回NaN

        返回值：
            IndSeries: 自然数幂方加权和序列

        >>> 示例：
            # 计算二次幂加权移动和
            quad_weighted_sum = close.tqfunc.numpow(n=5, m=2)
            # 构建自定义指标
            custom_indicator = close.tqfunc.numpow(n=10, m=1.5)
            # 特殊滤波处理
            filtered_signal = price_series.tqfunc.numpow(n=8, m=0.5)
        """
        ...

    @tobtind(lib='tqfunc')
    def abs(self, **kwargs) -> IndSeries:
        """
        ## 绝对值函数 - 计算序列中每个元素的绝对值

        功能：
            对输入序列中的每个元素取绝对值，将负值转换为正值

        应用场景：
            - 计算价格波动的绝对幅度
            - 处理收益率数据的正负波动
            - 技术指标中需要正值计算的场景
            - 距离和偏差的绝对值计算

        计算原理：
            abs(x)[i] = |x[i]|
            对序列中每个元素取绝对值

        参数：
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 默认使用实例初始化时的数据列
            - 对复数类型数据，返回复数的模

        返回值：
            IndSeries: 绝对值序列

        >>> 示例：
            # 计算价格与均线的绝对偏差
            price_deviation = close - ma_20
            abs_deviation = price_deviation.tqfunc.abs()
            # 计算日收益率绝对幅度
            daily_return = (close - close.tqfunc.ref(length=1)) / close.tqfunc.ref(length=1)
            abs_return = daily_return.tqfunc.abs()
        """
        ...

    @tobtind(lib='tqfunc')
    def min(self, b=None, **kwargs) -> IndSeries:
        """
        ## 最小值函数 - 获取两个序列中对应位置的最小值

        功能：
            比较两个序列对应位置的值，返回较小值组成的新序列

        应用场景：
            - 计算价格通道的下轨
            - 寻找支撑位和阻力位
            - 风险管理中的止损计算
            - 多指标信号取保守值

        计算原理：
            min(a, b)[i] = min(a[i], b[i])
            逐元素比较两个序列，取较小值

        参数：
            b: 第二个比较序列
            **kwargs: 额外参数

        注意：
            - 当只有一个序列时，默认与实例数据比较
            - 序列长度不一致时，按位置对应，缺失位置返回NaN

        返回值：
            IndSeries: 最小值序列

        >>> 示例：
            # 计算真实波幅的最小部分
            true_range = high.tqfunc.max(close.tqfunc.ref(1)) - low.tqfunc.min(close.tqfunc.ref(1))
            # 构建价格通道下轨（取最低价和支撑位中的较小值）
            support_level = ...  # 计算支撑位
            channel_lower = low.tqfunc.min(support_level)
        """
        ...

    @tobtind(lib='tqfunc')
    def max(self, b=None, **kwargs) -> IndSeries:
        """
        ## 最大值函数 - 获取两个序列中对应位置的最大值

        功能：
            比较两个序列对应位置的值，返回较大值组成的新序列

        应用场景：
            - 计算价格通道的上轨
            - 寻找突破位和阻力位
            - 止盈目标位计算
            - 多指标信号取激进值

        计算原理：
            max(a, b)[i] = max(a[i], b[i])
            逐元素比较两个序列，取较大值

        参数：
            b: 第二个比较序列
            **kwargs: 额外参数

        注意：
            - 当只有一个序列时，默认与实例数据比较
            - 序列长度不一致时，按位置对应，缺失位置返回NaN

        返回值：
            IndSeries: 最大值序列

        >>> 示例：
            # 计算真实波幅的最大部分
            true_range = high.tqfunc.max(close.tqfunc.ref(1)) - low.tqfunc.min(close.tqfunc.ref(1))
            # 构建价格通道上轨（取最高价和阻力位中的较大值）
            resistance_level = ...  # 计算阻力位
            channel_upper = high.tqfunc.max(resistance_level)
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def median(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 中位数函数 - 计算序列在指定周期内的中位数值

        功能：
            计算滚动窗口内的中位数，反映数据的中心趋势，对异常值不敏感

        应用场景：
            - 构建稳健的价格趋势指标
            - 异常值过滤和数据处理
            - 替代移动平均的稳健中心度量
            - 统计学稳健分析

        计算原理：
            median(x, n)[i] = 排序后窗口内中间位置的值
            对每个位置的n周期窗口内数据排序，取中间值

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 当n为偶数时，取中间两个数的平均值
            - 窗口长度不足时返回NaN
            - 对异常值的敏感度低于算术平均值

        返回值：
            IndSeries: 中位数序列

        >>> 示例：
            # 计算价格中位数趋势
            price_median = close.tqfunc.median(length=20)
            # 构建中位数通道
            upper_median = high.tqfunc.median(length=20)
            lower_median = low.tqfunc.median(length=20)
        """
        ...

    @tobtind(lib='tqfunc')
    def exist(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 条件存在判断 - 判断指定周期内是否存在满足条件的时点

        功能：
            检查在最近的n个周期内，是否至少有一个周期满足给定条件

        应用场景：
            - 确认技术信号的有效性
            - 趋势确认和过滤
            - 模式识别的存在性验证
            - 交易信号的二次确认

        计算原理：
            exist(cond, n)[i] = 1 如果在[i-n+1, i]区间内至少有一个cond为真，否则为0
            滑动窗口内条件成立的布尔判断

        参数：
            self: 条件表达式
            length: 检查周期数，默认10
            **kwargs: 额外参数

        注意：
            - 返回布尔序列（1表示存在，0表示不存在）
            - 条件cond应为布尔序列
            - length=0时从第一个有效值开始检查

        返回值：
            IndSeries: 条件存在标志序列（1/0）

        >>> 示例：
            # 确认近期是否出现过金叉信号
            golden_cross = ma_5.tqfunc.crossup(ma_20)
            recent_golden_cross = golden_cross.tqfunc.exist(length=10)
            # 检查近期是否有突破行为
            breakout = close > high.tqfunc.ref(1)  # 突破前高
            recent_breakout = breakout.tqfunc.exist(length=5)
        """
        ...

    @tobtind(lib='tqfunc')
    def every(self, length: int = 3, **kwargs) -> IndSeries:
        """
        ## 持续满足判断 - 判断指定周期内是否持续满足条件

        功能：
            检查在最近的n个周期内，是否每个周期都满足给定条件

        应用场景：
            - 确认趋势的持续性
            - 过滤假突破信号
            - 确认指标的稳定状态
            - 连续信号验证

        计算原理：
            every(cond, n)[i] = 1 如果在[i-n+1, i]区间内所有cond都为真，否则为0
            滑动窗口内条件持续成立的布尔判断

        参数：
            self: 条件表达式
            length: 检查周期数，默认3
            **kwargs: 额外参数

        注意：
            - 返回布尔序列（1表示持续满足，0表示不满足）
            - 条件cond应为布尔序列
            - length=0时从第一个有效值开始检查

        返回值：
            IndSeries: 持续满足标志序列（1/0）

        >>> 示例：
            # 确认连续上涨趋势
            consecutive_up = (close > close.tqfunc.ref(1)).tqfunc.every(length=3)
            # 确认均线多头排列的持续性
            ma_alignment = (ma_5 > ma_10) & (ma_10 > ma_20)
            stable_trend = ma_alignment.tqfunc.every(length=5)
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def hhv(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 周期最高值 - 计算序列在指定周期内的最高值

        功能：
            计算滚动窗口内的最大值，用于识别阻力位和突破点

        应用场景：
            - 构建布林带和其他通道指标的上轨
            - 识别价格阻力位
            - 计算突破交易的参考水平
            - 波动率测量和极值分析

        计算原理：
            hhv(x, n)[i] = max(x[i-n+1], x[i-n+2], ..., x[i])
            滑动窗口内取最大值

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 窗口长度不足时返回NaN
            - 常用于构建动态支撑阻力位
            - 对价格数据的极值敏感

        返回值：
            IndSeries: 周期最高值序列

        >>> 示例：
            # 计算唐奇安通道上轨
            donchian_upper = high.tqfunc.hhv(length=20)
            # 识别近期阻力位
            recent_resistance = high.tqfunc.hhv(length=10)
        """
        ...

    @tobtind(overlap=True, lib='tqfunc')
    def llv(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 周期最低值 - 计算序列在指定周期内的最低值

        功能：
            计算滚动窗口内的最小值，用于识别支撑位和破位点

        应用场景：
            - 构建布林带和其他通道指标的下轨
            - 识别价格支撑位
            - 计算抄底交易的参考水平
            - 风险管理中的止损设置

        计算原理：
            llv(x, n)[i] = min(x[i-n+1], x[i-n+2], ..., x[i])
            滑动窗口内取最小值

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 窗口长度不足时返回NaN
            - 常用于构建动态支撑阻力位
            - 对价格数据的极值敏感

        返回值：
            IndSeries: 周期最低值序列

        >>> 示例：
            # 计算唐奇安通道下轨
            donchian_lower = low.tqfunc.llv(length=20)
            # 识别近期支撑位
            recent_support = low.tqfunc.llv(length=10)
        """
        ...

    @tobtind(lib='tqfunc')
    def avedev(self, length: int = 10, **kwargs) -> IndSeries:
        """
        ## 平均绝对偏差 - 计算序列在周期内的平均绝对偏差

        功能：
            测量数据点与均值的平均距离，反映数据的离散程度

        应用场景：
            - 波动率测量和风险评估
            - 异常值检测
            - 数据稳定性分析
            - 技术指标的可靠性评估

        计算原理：
            avedev(x, n)[i] = sum(|x[j] - mean(x, n)|) / n, j从i-n+1到i
            计算窗口内各点与均值的绝对偏差的平均值

        参数：
            length: 计算周期，默认10
            **kwargs: 额外参数，可指定其他序列

        注意：
            - 比标准差对异常值更稳健
            - 窗口长度不足时返回NaN
            - 反映数据的平均波动幅度

        返回值：
            IndSeries: 平均绝对偏差序列

        >>> 示例：
            # 计算价格波动性
            price_volatility = close.tqfunc.avedev(length=20)
            # 检测异常波动
            normal_volatility = price_volatility.tqfunc.ma(length=50)
            abnormal_move = price_volatility > normal_volatility * 2
        """
        ...

    @tobtind(lib='tqfunc')
    def barlast(self, **kwargs) -> IndSeries:
        """
        ## 条件间隔计数 - 计算从上一次条件成立到当前的周期数

        ### 功能：
            统计从最近一次条件成立位置到当前位置的周期间隔

        ### 应用场景：
        - 信号出现后的时间跟踪
        - 事件驱动的策略计时
        - 条件持续时间的监控
        - 交易信号的冷却期判断

        ### 计算原理：
            对每个位置，计算从最近一次cond为True到当前位置的周期数

        ### 参数：
            self: 条件表达式序列
            **kwargs: 额外参数

        ### 注意：
            - 条件成立时返回0，表示当前周期条件成立
            - 如果从未成立过，返回全0数组
            - 条件序列应为布尔类型

        ### 返回值：
            IndSeries: 间隔周期数序列

        ### 使用案例:
        >>> # 计算自上一次金叉以来的周期数
        >>> golden_cross = kline.close.sma(5).tqfunc.crossup(kline.close.sma(20))
        >>> bars_since_golden = golden_cross.tqfunc.barlast()
        >>> 
        >>> # 计算自上一次价格突破布林带上轨以来的周期数
        >>> bb_upper = kline.talib.BBANDS(nbdevup=2)[0]
        >>> price_break_upper = kline.close > bb_upper
        >>> bars_since_break = price_break_upper.tqfunc.barlast()
        >>> 
        >>> # 判断距离上一次条件成立是否超过10周期
        >>> def trend_change_strategy(self):
        >>>     ma_fast = self.data.close.sma(5)
        >>>     ma_slow = self.data.close.sma(20)
        >>>     golden_cross = ma_fast.tqfunc.crossup(ma_slow)
        >>>     bars_since_cross = golden_cross.tqfunc.barlast()
        >>>     
        >>>     if bars_since_cross.new == 0:
        >>>         return "刚刚发生金叉，信号新鲜"
        >>>     elif bars_since_cross.new > 10:
        >>>         return f"距离上一次金叉已经{bars_since_cross.new}周期，信号可能过时"
        >>>     else:
        >>>         return f"距离上一次金叉{bars_since_cross.new}周期"
        >>> 
        >>> # 多条件组合使用
        >>> def multi_condition_timing(self):
        >>>     # 条件1：RSI超卖后恢复
        >>>     rsi = self.data.talib.RSI(timeperiod=14)
        >>>     rsi_oversold = rsi < 30
        >>>     bars_since_oversold = rsi_oversold.tqfunc.barlast()
        >>>     
        >>>     # 条件2：价格突破20日高点
        >>>     price_break_high = self.data.close > self.data.high.rolling(20).max()
        >>>     bars_since_break_high = price_break_high.tqfunc.barlast()
        >>>     
        >>>     # 策略：在RSI超卖后5-10周期内，且价格刚刚突破20日高点时买入
        >>>     if 5 <= bars_since_oversold.new <= 10 and bars_since_break_high.new == 0:
        >>>         return "符合买入时机：RSI超卖后恢复中，价格突破近期高点"
        """
        ...

    @tobtind(lib='tqfunc')
    def cum_counts(self, **kwargs) -> IndSeries:
        """
        ## 连续条件计数 - 统计连续满足条件的周期数

        功能：
            计算当前连续满足条件的周期数量，用于识别连续模式

        应用场景：
            - 计算连续上涨/下跌天数
            - 统计最大连续盈利/亏损
            - 识别趋势的持续性
            - 条件连续性的监控

        计算原理：
            对每个位置，计算从当前位置向前连续满足条件的周期数

        参数：
            self: 条件表达式序列
            **kwargs: 额外参数

        注意：
            - 条件不满足时计数重置为0
            - 条件满足时计数递增
            - 可用于计算各种连续模式

        返回值：
            IndSeries: 连续满足条件的计数序列

        >>> 示例：
            # 计算连续上涨天数
            up_day = close > close.tqfunc.ref(1)
            consecutive_up_days = up_day.tqfunc.cum_counts()
            # 统计连续盈利交易
            profitable_trade = trade_pnl > 0
            winning_streak = profitable_trade.tqfunc.cum_counts()
            # 识别超买超卖的持续性
            overbought = rsi > 70
            consecutive_overbought = overbought.tqfunc.cum_counts()
            extreme_overbought = consecutive_overbought >= 3  # 连续3期超买
        """
        ...

    @tobtind(lib='tqfunc')
    def time_to_ns_timestamp(self, **kwargs) -> IndSeries:
        """
        ## 纳秒时间戳转换 - 将时间转换为纳秒级时间戳

        功能：
            将各种格式的时间数据转换为整数类型的纳秒级时间戳

        应用场景：
            - 高频数据的时间精确记录
            - 事件顺序的精确排序
            - 跨系统时间同步
            - 性能分析和时间间隔测量

        计算原理：
            将输入时间转换为从1970-01-01 00:00:00开始的纳秒数

        参数：
            **kwargs: 额外参数

        注意：
            - 支持datetime对象、字符串、pandas时间戳等格式
            - 返回整数类型的纳秒时间戳
            - 精度为纳秒级，适用于高频交易场景

        返回值：
            IndSeries: 纳秒时间戳序列

        >>> 示例：
            # 转换当前时间为纳秒时间戳
            ns_ts = context.current_dt.tqfunc.time_to_ns_timestamp()
            # 计算事件时间间隔（纳秒）
            event1_ts = event1_time.tqfunc.time_to_ns_timestamp()
            event2_ts = event2_time.tqfunc.time_to_ns_timestamp()
            time_diff_ns = event2_ts - event1_ts
        """
        ...

    @tobtind(lib='tqfunc')
    def time_to_s_timestamp(self, **kwargs) -> IndSeries:
        """
        ## 秒级时间戳转换 - 将时间转换为秒级时间戳

        功能：
            将各种格式的时间数据转换为整数类型的秒级时间戳

        应用场景：
            - 低频数据的时间记录
            - 跨日数据的时间对齐
            - 策略逻辑中的时间判断
            - 数据存储和传输的时间标准化

        计算原理：
            将输入时间转换为从1970-01-01 00:00:00开始的秒数

        参数：
            **kwargs: 额外参数

        注意：
            - 支持datetime对象、字符串、pandas时间戳等格式
            - 返回整数类型的秒级时间戳
            - 精度为秒级，适用于日线、小时线等低频数据

        返回值：
            IndSeries: 秒级时间戳序列

        >>> 示例：
            # 转换当前时间为秒级时间戳
            s_ts = current_dt.tqfunc.time_to_s_timestamp()
            # 计算日线数据的时间戳
            daily_timestamp = daily_data.tqfunc.time_to_s_timestamp()
        """
        ...

    @tobtind(lib='tqfunc')
    def time_to_str(self, **kwargs) -> IndSeries:
        """
        ## 时间字符串转换 - 将时间转换为标准格式字符串

        功能：
            将各种格式的时间数据转换为%Y-%m-%d %H:%M:%S.%f格式的字符串

        应用场景：
            - 数据展示和日志输出
            - 报告生成和时间格式化
            - 跨系统数据交换
            - 时间信息的可视化

        计算原理：
            将输入时间转换为标准化的字符串格式

        参数：
            **kwargs: 额外参数

        注意：
            - 支持datetime对象、时间戳、字符串等格式
            - 返回标准格式的时间字符串
            - 格式为：年-月-日 时:分:秒.微秒

        返回值：
            IndSeries: 格式化时间字符串序列

        >>> 示例：
            # 转换当前时间为标准字符串格式
            time_str = current_dt.tqfunc.time_to_str()
            # 生成交易记录的时间戳
            trade_time_str = trade_timestamp.tqfunc.time_to_str()
        """
        ...

    @tobtind(lib='tqfunc')
    def time_to_datetime(self, **kwargs) -> IndSeries:
        """
        ## datetime对象转换 - 将时间转换为datetime对象

        功能：
            将各种格式的时间数据转换为Python datetime对象

        应用场景：
            - 时间运算和日期操作
            - 工作日计算和假期判断
            - 时间序列的高级处理
            - 与其他Python时间库的交互

        计算原理：
            将输入时间转换为datetime.datetime对象

        参数：
            **kwargs: 额外参数

        注意：
            - 支持字符串、时间戳、其他时间对象等格式
            - 返回Python标准datetime对象
            - 便于进行日期运算和时间操作

        返回值：
            IndSeries: datetime对象序列

        >>> 示例：
            # 转换时间戳为datetime对象
            dt_obj = timestamp.tqfunc.time_to_datetime()
            # 计算交易日
            trade_date = current_dttqfunc.time_to_datetime().date()
        """
        ...
        
    NON_KLINE_INDICATORS = ["crossup","crossdown","count","numpow","abs","min","max",
                            "exist","every","barlast","cum_counts","time_to_ns_timestamp",
                            "time_to_s_timestamp","time_to_str","time_to_datetime"]
