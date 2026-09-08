from __future__ import annotations
from .base import tobtind
from ..utils import TYPE_CHECKING,LineStyle,LineDash

if TYPE_CHECKING:
    from typing_ import *
    from .core import *


class TaLib:
    """
    ## Ta-Lib技术指标计算类
    - 将目标数据转换为minibt内置指标数据，提供TA-Lib库中技术指标的Python接口。
    - 此类封装了TA-Lib的技术指标函数，使其能够与minibt框架无缝集成。

    ### 📘 **文档参考**:
    - API参考：https://www.minibt.cn/minibt_api_reference/talib/
    - 详细文档：https://www.bookstack.cn/read/talib-zh/README.md

    ### 主要特性：
    - 支持TA-Lib的所有技术指标类别
    - 自动处理数据格式转换
    - 提供统一的参数接口
    - 返回minibt兼容的IndSeries或IndFrame格式

    ### 使用示例：
    ```python
    # 从数据源创建TaLib实例
    ta = TaLib(data)

    # 从策略调用指标
    self.kline.talib

    # 计算希尔伯特变换-主导周期
    ht_period = ta.HT_DCPERIOD()
    ht_period = self.kline.close.talib.HT_DCPERIOD()
    ht_period = self.kline.close.HT_DCPERIOD()

    # 计算移动平均线
    sma = ta.SMA(length=20)
    sma = self.kline.close.talib.SMA(length=20)
    sma = self.kline.close.SMA(length=20)

    # 计算相对强弱指数
    rsi = ta.RSI(length=14)
    rsi = self.kline.close.talib.RSI(length=14)
    rsi = self.kline.close.RSI(length=14)
    ```

    ### 参数：
        data: 输入数据，可以是pandas Series或DataFrame格式

    ### 属性：
        _df: 存储输入数据的内部属性

    ### 方法：
    所有TA-Lib技术指标方法，按功能分类：
    - 周期指标 (Cycle Indicator Functions)
    - 价格变换 (Price Transform)
    - 动量指标 (Momentum Indicators)
    - 波动率指标 (Volatility Indicators)
    - 成交量指标 (Volume Indicators)
    - 趋势指标 (Trend Indicators)
    - 统计函数 (Statistic Functions)
    - 数学变换 (Math Transform)
        - 数学运算符 (Math Operators)

    ### 注意：
    - 使用前需要确保已安装TA-Lib库
    - 输入数据应包含所需的OHLCV列
    - 返回值会自动转换为minibt的IndSeries或IndFrame格式
    - 所有指标方法都支持**kwargs参数传递额外设置

    ### 版本要求：
    - Python 3.7+
    - TA-Lib 0.4.0+
    - minibt 兼容版本
    """

    _perfixes: str = "talib_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    # Cycle Indicator Functions
    @tobtind(category='Cycle Indicator Functions', lib='talib')
    def HT_DCPERIOD(self, **kwargs) -> IndSeries:
        """## HT_DCPERIOD - Hilbert Transform - Dominant Cycle Period
        名称: 希尔伯特变换-主导周期
        - Hilbert Transform - Dominant Cycle Period (HT_DCPERIOD) 通过希尔伯特变换
        - 计算价格数据的主导周期，用于识别市场的主要循环周期。

        应用场景：
        - 识别市场的周期性波动
        - 确定趋势转换的时间窗口
        - 配合其他周期指标进行多时间框架分析

        计算原理：
        - 使用希尔伯特变换对价格序列进行信号处理，提取其中的周期性成分，
        - 通过相位分析确定主导周期长度。

        参数：
            **kwargs: 额外参数，可传递minibt特定的设置参数

        注意：
            实例包括列：close (收盘价)

        返回值：
            IndSeries: 主导周期计算结果，每个值表示对应时间点的主导周期长度

        示例：
        ```python
        # 计算主导周期
        dominant_period = self.kline.close.HT_DCPERIOD()

        # 识别长周期和短周期
        long_cycle = dominant_period > 50
        short_cycle = dominant_period < 20

        # 周期转换信号
        cycle_turning = dominant_period.diff() > 5
        ```
        """
        ...

    @tobtind(category='Cycle Indicator Functions', lib='talib')
    def HT_DCPHASE(self, **kwargs) -> IndSeries:
        """## HT_DCPHASE - Hilbert Transform - Dominant Cycle Phase
        名称: 希尔伯特变换-主导循环阶段
        - Hilbert Transform - Dominant Cycle Phase (HT_DCPHASE) 通过希尔伯特变换
        - 计算价格数据在当前主导循环中的相位位置，用于判断循环周期中的具体位置。

        应用场景：
        - 判断市场在主导循环中的具体位置（早期、中期、晚期）
        - 识别循环周期的转折点和极端位置
        - 配合HT_DCPERIOD进行循环周期的相位分析

        计算原理：
        - 基于希尔伯特变换计算的主导循环，确定价格在循环中的相位角度
        - 相位值在-180°到+180°之间变化，表示循环的相对位置

        参数：
            **kwargs: 额外参数，可传递minibt特定的设置参数

        注意：
            实例包括列：close (收盘价)

        返回值：
            IndSeries: 主导循环相位计算结果，每个值表示对应时间点的相位角度

        示例：
        ```python
        # 计算主导循环相位
        phase = self.kline.close.HT_DCPHASE()

        # 识别循环转折点（相位接近0°）
        cycle_turning = (phase.abs() < 30) & (phase.diff().abs() > 60)

        # 判断循环阶段
        early_cycle = phase.between(-180, -90)
        mid_cycle = phase.between(-90, 90)
        late_cycle = phase.between(90, 180)
        ```
        """
        ...

    @tobtind(lines=["inphase", "quadrature"], category='Cycle Indicator Functions', lib='talib')
    def HT_PHASOR(self, **kwargs) -> IndFrame:
        """## HT_PHASOR - Hilbert Transform - Phasor Components
        名称: 希尔伯特变换-相量分量
        - Hilbert Transform - Phasor Components (HT_PHASOR) 通过希尔伯特变换
        - 将价格序列分解为同相和正交分量，用于分析价格波的构成。

        应用场景：
        - 分析价格波动的相位特征
        - 识别市场能量的积累和释放
        - 作为其他希尔伯特变换指标的基础组件

        计算原理：
        - 将价格序列视为复平面上的向量
        - 通过希尔伯特变换计算向量的实部（同相分量）和虚部（正交分量）
        - 同相分量表示与原始信号同步的部分，正交分量表示相位偏移90°的部分

        参数：
            **kwargs: 额外参数，可传递minibt特定的设置参数

        注意：
            实例包括列：close (收盘价)

        返回值：
            IndFrame: 包含两列的DataFrame：
            - inphase: 同相分量，与原始价格序列相位相同
            - quadrature: 正交分量，与原始价格序列相位相差90°

        示例：
        ```python
        # 计算相量分量
        phasor = self.kline.close.HT_PHASOR()

        # 计算振幅（波动强度）
        amplitude = np.sqrt(phasor.inphase**2 + phasor.quadrature**2)

        # 计算相位角度
        phase_angle = np.arctan2(phasor.quadrature, phasor.inphase)

        # 检测相位一致性
        phase_alignment = phasor.inphase > 0
        ```
        """
        ...

    @tobtind(lines=["sine", "leadsine"], category='Cycle Indicator Functions', lib='talib')
    def HT_SINE(self, **kwargs) -> IndFrame:
        """## HT_SINE - Hilbert Transform - Sine Wave
        名称: 希尔伯特变换-正弦波
        - Hilbert Transform - Sine Wave (HT_SINE) 通过希尔伯特变换
        - 生成与主导循环同步的正弦波和超前正弦波，用于识别循环极值点。

        应用场景：
        - 识别循环周期的高低点和转折信号
        - 生成循环周期的买卖信号
        - 判断趋势的可持续性和反转概率

        计算原理：
        - 基于HT_PHASOR计算的结果，生成正弦波表示
        - sine: 与主导循环同步的正弦波
        - leadsine: 超前主导循环的正弦波（相位提前）
        - 两条线的交叉点通常表示循环的转折点

        参数：
            **kwargs: 额外参数，可传递minibt特定的设置参数

        注意：
            实例包括列：close (收盘价)

        返回值：
            IndFrame: 包含两列的DataFrame：
            - sine: 正弦波，与主导循环同步
            - leadsine: 超前正弦波，相位提前于主导循环

        示例：
        ```python
        # 计算正弦波指标
        sine_wave = self.kline.close.HT_SINE()

        # 识别黄金交叉（买入信号）
        buy_signal = sine_wave.sine > sine_wave.leadsine

        # 识别死亡交叉（卖出信号）
        sell_signal = sine_wave.sine < sine_wave.leadsine

        # 判断循环极值（正弦波接近±1）
        cycle_high = sine_wave.sine > 0.8
        cycle_low = sine_wave.sine < -0.8
        ```
        """
        ...

    @tobtind(category='Cycle Indicator Functions', lib='talib')
    def HT_TRENDMODE(self, **kwargs) -> IndSeries:
        """## HT_TRENDMODE - Hilbert Transform - Trend vs Cycle Mode
        名称: 希尔伯特变换-趋势与周期模式
        - Hilbert Transform - Trend vs Cycle Mode (HT_TRENDMODE) 通过希尔伯特变换
        - 判断当前市场处于趋势模式还是周期模式，用于选择合适的交易策略。

        应用场景：
        - 判断市场状态（趋势市或震荡市）
        - 切换交易策略（趋势跟踪或波段操作）
        - 风险管理（不同市场模式下的仓位调整）

        计算原理：
        - 通过希尔伯特变换分析价格序列的频谱特性
        - 检测价格波动中是否存在显著的主导循环周期
        - 输出1表示存在显著周期（震荡市），0表示趋势市

        参数：
            **kwargs: 额外参数，可传递minibt特定的设置参数

        注意：
            实例包括列：close (收盘价)

        返回值：
            IndSeries: 模式判断结果，返回值为整数：
            - 1: 周期模式（存在显著主导循环，适合波段操作）
            - 0: 趋势模式（无明显主导循环，适合趋势跟踪）

        示例：
        ```python
        # 判断市场模式
        market_mode = self.kline.close.HT_TRENDMODE()

        # 趋势跟踪策略
        trend_signal = (market_mode == 0) & (self.kline.close > self.kline.close.SMA(20))

        # 波段操作策略
        cycle_signal = (market_mode == 1) & (self.kline.close.HT_SINE().sine < -0.5)

        # 风险控制：在模式切换时减少仓位
        mode_change = market_mode.diff() != 0
        ```
        """
        ...

    # Math Operator Functions
    @tobtind(category="Math Operator Functions", lib='talib')
    def ADD(self,b:pd.Series=None, **kwargs) -> IndSeries:
        """ADD - Vector Arithmetic Add
        名称:向量加法运算

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 向量加法运算结果
        """
        ...

    @tobtind(category="Math Operator Functions", lib='talib')
    def DIV(self,b:pd.Series=None, **kwargs) -> IndSeries:
        """DIV - Vector Arithmetic Div
        名称:向量除法运算

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 向量除法运算结果
        """
        ...

    @tobtind(overlap=True, category="Math Operator Functions", lib='talib')
    def MAX(self, timeperiod=30, **kwargs) -> IndSeries:
        """MAX - Highest value over a specified period
        名称:周期内最大值

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 周期内最大值计算结果
        """
        ...

    @tobtind(category="Math Operator Functions", lib='talib')
    def MAXINDEX(self, timeperiod=30, **kwargs) -> IndSeries:
        """MAXINDEX - Index of highest value over a specified period
        名称:周期内最大值的索引

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 周期内最大值的索引结果
        """
        ...

    @tobtind(overlap=True, category="Math Operator Functions", lib='talib')
    def MIN(self, timeperiod=30, **kwargs) -> IndSeries:
        """MIN - Lowest value over a specified period
        名称:周期内最小值

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 周期内最小值计算结果
        """
        ...

    @tobtind(category="Math Operator Functions", lib='talib')
    def MININDEX(self, timeperiod=30, **kwargs) -> IndSeries:
        """MININDEX - Index of lowest value over a specified period
        名称:周期内最小值的索引

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 周期内最小值的索引结果
        """
        ...

    @tobtind(lines=["tmin", "tmax"], overlap=True, category="Math Operator Functions", lib='talib')
    def MINMAX(self, timeperiod=30, **kwargs) -> IndFrame:
        """MINMAX - Lowest and highest values over a specified period
        名称:周期内最小值和最大值

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndFrame:tmin,tmax
        """
        ...

    @tobtind(lines=["tminidx", "tmaxidx"], category="Math Operator Functions", lib='talib')
    def MINMAXINDEX(self, timeperiod=30, **kwargs) -> IndFrame:
        """MINMAXINDEX - Indexes of lowest and highest values over a specified period
        名称:周期内最小值和最大值的索引

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndFrame:tminidx, tmaxidx
        """
        ...

    @tobtind(category="Math Operator Functions", lib='talib')
    def MULT(self,b:pd.Series=None, **kwargs) -> IndSeries:
        """MULT - Vector Arithmetic Mult
        名称:向量乘法运算

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 向量乘法运算结果
        """
        ...

    @tobtind(category="Math Operator Functions", lib='talib')
    def SUB(self,b:pd.Series=None, **kwargs) -> IndSeries:   
        """SUB - Vector Arithmetic Substraction
        名称:向量减法运算

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 向量减法运算结果
        """
        ...

    @tobtind(category="Math Operator Functions", lib='talib')
    def SUM(self, timeperiod=30, **kwargs) -> IndSeries:
        """SUM - Summation
        名称:周期内求和

        Args:
            timeperiod: 时间周期，默认值为30
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 周期内求和计算结果
        """
        ...

    # Math Transform Functions
    @tobtind(category="Math Transform Functions", lib='talib')
    def ACOS(self, **kwargs) -> IndSeries:
        """ACOS - Vector Trigonometric ACos
        名称:反余弦函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 反余弦函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def ASIN(self, **kwargs) -> IndSeries:
        """ASIN - Vector Trigonometric ASin
        名称:反正弦函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 反正弦函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def ATAN(self, **kwargs) -> IndSeries:
        """ATAN - Vector Trigonometric ATan
        名称:反正切函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 反正切函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def CEIL(self, **kwargs) -> IndSeries:
        """CEIL - Vector Ceil
        名称:向上取整函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 向上取整计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def COS(self, **kwargs) -> IndSeries:
        """COS - Vector Trigonometric Cos
        名称:余弦函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 余弦函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def COSH(self, **kwargs) -> IndSeries:
        """COSH - Vector Trigonometric Cosh
        名称:双曲余弦函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 双曲余弦函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def EXP(self, **kwargs) -> IndSeries:
        """EXP - Vector Arithmetic Exp
        名称:指数函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 指数函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def FLOOR(self, **kwargs) -> IndSeries:
        """FLOOR - Vector Floor
        名称:向下取整函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 向下取整计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def LN(self, **kwargs) -> IndSeries:
        """LN - Vector Log Natural
        名称:自然对数函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 自然对数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def LOG10(self, **kwargs) -> IndSeries:
        """LOG10 - Vector Log10
        名称:10底对数函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 10底对数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def SIN(self, **kwargs) -> IndSeries:
        """SIN - Vector Trigonometric Sin
        名称:正弦函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 正弦函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def SINH(self, **kwargs) -> IndSeries:
        """SINH - Vector Trigonometric Sinh
        名称:双曲正弦函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 双曲正弦函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def SQRT(self, **kwargs) -> IndSeries:
        """SQRT - Vector Square Root
        名称:平方根函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 平方根计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def TAN(self, **kwargs) -> IndSeries:
        """TAN - Vector Trigonometric Tan
        名称:正切函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 正切函数计算结果
        """
        ...

    @tobtind(category="Math Transform Functions", lib='talib')
    def TANH(self, **kwargs) -> IndSeries:
        """TANH - Vector Trigonometric Tanh
        名称:双曲正切函数

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 双曲正切函数计算结果
        """
        ...

    # Momentum Indicator Functions
    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ADX(self, timeperiod=14, **kwargs) -> IndSeries:
        """ADX - Average Directional Movement Index
        名称:平均趋向指数

        Args:
            timeperiod: 时间周期，默认值为14
            **kwargs: 额外参数

        NOTE:
            实例包括列：high, low, close

        Returns:
            IndSeries: 平均趋向指数计算结果
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ADXR(self, timeperiod=14, **kwargs) -> IndSeries:
        """ADXR- Average Directional Movement Index Rating
        名称:平均趋向指数的趋向指数

        Args:
            timeperiod: 时间周期，默认值为14
            **kwargs: 额外参数

        NOTE:
            实例包括列：high, low, close

        Returns:
            IndSeries: 平均趋向指数的趋向指数计算结果
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def APO(self, fastperiod=12, slowperiod=26, matype=0, **kwargs) -> IndSeries:
        """APO - Absolute Price Oscillator
        名称:绝对价格振荡器

        Args:
            fastperiod: 快速周期，默认值为12
            slowperiod: 慢速周期，默认值为26
            matype: 移动平均类型，默认值为0
            **kwargs: 额外参数

        NOTE:
            实例包括列：close

        Returns:
            IndSeries: 绝对价格振荡器计算结果
        """
        ...

    @tobtind(lines=["aroondown", "aroonup"], category="Momentum Indicator Functions", lib='talib')
    def AROON(self, timeperiod=14, **kwargs) -> IndFrame:
        """AROON - Aroon
        名称:阿隆指标

        Args:
            timeperiod: 时间周期，默认值为14
            **kwargs: 额外参数

        NOTE:
            实例包括列：high, low

        Returns:
            IndFrame:aroondown, aroonup
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def AROONOSC(self, timeperiod=14, **kwargs) -> IndSeries:
        """AROONOSC - Aroon Oscillator
        名称:阿隆振荡

        Args:
            timeperiod: 时间周期，默认值为14
            **kwargs: 额外参数

        NOTE:
            实例包括列：high, low

        Returns:
            IndSeries: 阿隆振荡计算结果
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def BOP(self, **kwargs) -> IndSeries:
        """BOP - Balance Of Power
        名称:均势指标

        Args:
            **kwargs: 额外参数

        NOTE:
            实例包括列：open,high, low,close

        Returns:
            IndSeries: 均势指标计算结果
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def CCI(self, timeperiod=14, **kwargs) -> IndSeries:
        """CCI - Commodity Channel Index
        名称:顺势指标

        Args:
            timeperiod: 时间周期，默认值为14
            **kwargs: 额外参数

        NOTE:
            实例包括列：high, low,close

        Returns:
            IndSeries: 顺势指标计算结果
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def CMO(self, timeperiod=14, **kwargs) -> IndSeries:
        """CMO - Chande Momentum Oscillator 钱德动量摆动指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 钱德动量摆动指标序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def DX(self, timeperiod=14, **kwargs) -> IndSeries:
        """DX - Directional Movement Index 动向指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 动向指标序列
        """
        ...

    @tobtind(lines=["dif", "dem", "histogram"], linestyle=dict(histogram=LineStyle(line_dash=LineDash.vbar)),
             category="Momentum Indicator Functions", lib='talib')
    def MACD(self, fastperiod=12, slowperiod=26, signalperiod=9, **kwargs) -> IndFrame:
        """MACD - Moving Average Convergence/Divergence 平滑异同移动平均线
        ---
        Args:
            fastperiod: 快速周期，默认12
            slowperiod: 慢速周期，默认26
            signalperiod: 信号周期，默认9
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndFrame: dif, dem, histogram
        """
        ...

    @tobtind(lines=["dif", "dem", "histogram"], linestyle=dict(histogram=LineStyle(line_dash=LineDash.vbar)),
             category="Momentum Indicator Functions", lib='talib')
    def MACDEXT(self, fastperiod=12, fastmatype=0, slowperiod=26, slowmatype=0, signalperiod=9, signalmatype=0, **kwargs) -> IndFrame:
        """MACDEXT - MACD with controllable MA type 平滑异同移动平均线(可控制移动平均算法)
        ---
        Args:
            fastperiod: 快速周期，默认12
            fastmatype: 快速移动平均类型，默认0
            slowperiod: 慢速周期，默认26
            slowmatype: 慢速移动平均类型，默认0
            signalperiod: 信号周期，默认9
            signalmatype: 信号移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndFrame: dif, dem, histogram
        """
        ...

    @tobtind(lines=["dif", "dem", "histogram"], linestyle=dict(histogram=LineStyle(line_dash=LineDash.vbar)),
             category="Momentum Indicator Functions", lib='talib')
    def MACDFIX(self, signalperiod=9, **kwargs) -> IndFrame:
        """MACDFIX - Moving Average Convergence/Divergence Fix 12/26 平滑异同移动平均线(固定快慢均线周期为12/26)
        ---
        Args:
            signalperiod: 信号周期，默认9
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndFrame: dif, dem, histogram
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def MFI(self, timeperiod=14, **kwargs) -> IndSeries:
        """MFI - Money Flow Index 资金流量指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close, volume
        Returns:
            IndSeries: 资金流量指标序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def MINUS_DI(self, timeperiod=14, **kwargs) -> IndSeries:
        """MINUS_DI - Minus Directional Indicator 下降动向值
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 下降动向值序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def MINUS_DM(self, timeperiod=14, **kwargs) -> IndSeries:
        """MINUS_DM - Minus Directional Movement 下降动向变动值
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 下降动向变动值序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def MOM(self, timeperiod=10, **kwargs) -> IndSeries:
        """MOM - Momentum 动量指标
        ---
        Args:
            timeperiod: 时间周期，默认10
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 动量指标序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def PLUS_DI(self, timeperiod=14, **kwargs) -> IndSeries:
        """PLUS_DI - Plus Directional Indicator 上升动向值
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 上升动向值序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def PLUS_DM(self, timeperiod=14, **kwargs) -> IndSeries:
        """PLUS_DM - Plus Directional Movement 上升动向变动值
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 上升动向变动值序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def PPO(self, fastperiod=12, slowperiod=26, matype=0, **kwargs) -> IndSeries:
        """PPO - Percentage Price Oscillator 价格震荡百分比指数
        ---
        Args:
            fastperiod: 快速周期，默认12
            slowperiod: 慢速周期，默认26
            matype: 移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 价格震荡百分比指数序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ROC(self, timeperiod=10, **kwargs) -> IndSeries:
        """ROC - Rate of change 变动率指标
        ---
        Args:
            timeperiod: 时间周期，默认10
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 变动率指标序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ROCP(self, timeperiod=10, **kwargs) -> IndSeries:
        """ROCP - Rate of change Percentage 百分比变动率
        ---
        Args:
            timeperiod: 时间周期，默认10
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 百分比变动率序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ROCR(self, timeperiod=10, **kwargs) -> IndSeries:
        """ROCR - Rate of change ratio 变动率比值
        ---
        Args:
            timeperiod: 时间周期，默认10
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 变动率比值序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ROCR100(self, timeperiod=10, **kwargs) -> IndSeries:
        """ROCR100 - Rate of change ratio 100 scale 100倍变动率比值
        ---
        Args:
            timeperiod: 时间周期，默认10
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 100倍变动率比值序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def RSI(self, timeperiod=14, **kwargs) -> IndSeries:
        """RSI - Relative Strength Index 相对强弱指数
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 相对强弱指数序列
        """
        ...

    @tobtind(lines=["slowk", "slowd"], category="Momentum Indicator Functions", lib='talib')
    def STOCH(self, fastk_period=5, slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0, **kwargs) -> IndFrame:
        """STOCH - Stochastic 随机指标(KD)
        ---
        Args:
            fastk_period: 快速K周期，默认5
            slowk_period: 慢速K周期，默认3
            slowk_matype: 慢速K移动平均类型，默认0
            slowd_period: 慢速D周期，默认3
            slowd_matype: 慢速D移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndFrame: slowk, slowd
        """
        ...

    @tobtind(lines=["fastk", "fastd"], category="Momentum Indicator Functions", lib='talib')
    def STOCHF(self, fastk_period=5, fastd_period=3, fastd_matype=0, **kwargs) -> IndFrame:
        """STOCHF - Stochastic Fast 快速随机指标
        ---
        Args:
            fastk_period: 快速K周期，默认5
            fastd_period: 快速D周期，默认3
            fastd_matype: 快速D移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndFrame: fastk, fastd
        """
        ...

    @tobtind(lines=["fastk", "fastd"], category="Momentum Indicator Functions", lib='talib')
    def STOCHRSI(self, timeperiod=14, fastk_period=5, fastd_period=3, fastd_matype=0, **kwargs) -> IndFrame:
        """STOCHRSI - Stochastic Relative Strength Index 随机相对强弱指数
        ---
        Args:
            timeperiod: RSI周期，默认14
            fastk_period: 快速K周期，默认5
            fastd_period: 快速D周期，默认3
            fastd_matype: 快速D移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndFrame: fastk, fastd
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def TRIX(self, timeperiod=30, **kwargs) -> IndSeries:
        """TRIX - 1-day Rate-Of-Change of a Triple Smooth EMA 三重平滑指数移动平均变动率
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三重平滑指数移动平均变动率序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def ULTOSC(self, timeperiod1=7, timeperiod2=14, timeperiod3=28, **kwargs) -> IndSeries:
        """ULTOSC - Ultimate Oscillator 终极波动指标
        ---
        Args:
            timeperiod1: 短周期，默认7
            timeperiod2: 中周期，默认14
            timeperiod3: 长周期，默认28
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 终极波动指标序列
        """
        ...

    @tobtind(category="Momentum Indicator Functions", lib='talib')
    def WILLR(self, timeperiod=14, **kwargs) -> IndSeries:
        """WILLR - Williams' %R 威廉指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 威廉指标序列
        """
        ...

    # Overlap Studies Functions
    @tobtind(lines=["upperband", "middleband", "lowerband"], overlap=True, category="overlap", lib='talib')
    def BBANDS(self, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0, **kwargs) -> IndFrame:
        """BBANDS - Bollinger Bands 布林线指标
        ---
        Args:
            timeperiod: 时间周期，默认5
            nbdevup: 上轨标准差倍数，默认2
            nbdevdn: 下轨标准差倍数，默认2
            matype: 移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndFrame: upperband, middleband, lowerband
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def DEMA(self, timeperiod=30, **kwargs) -> IndSeries:
        """DEMA - Double Exponential Moving Average 双指数移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 双指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def EMA(self, timeperiod=30, **kwargs) -> IndSeries:
        """EMA - Exponential Moving Average 指数移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def HT_TRENDLINE(self, **kwargs) -> IndSeries:
        """HT_TRENDLINE - Hilbert Transform - Instantaneous Trendline 希尔伯特瞬时趋势线
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 希尔伯特瞬时趋势线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def KAMA(self, timeperiod=30, **kwargs) -> IndSeries:
        """KAMA - Kaufman Adaptive Moving Average 考夫曼自适应移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 考夫曼自适应移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def MA(self, timeperiod=30, matype=0, **kwargs) -> IndSeries:
        """MA - Moving Average 移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            matype: 移动平均类型，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 移动平均线序列
        """
        ...

    @tobtind(lines=["mama_slow", "mama_fast"], overlap=True, category="overlap", lib='talib')
    def MAMA(self, fastlimit=0, slowlimit=0, **kwargs) -> IndFrame:
        """MAMA - MESA Adaptive Moving Average MESA自适应移动平均线
        ---
        Args:
            fastlimit: 快速限制，默认0
            slowlimit: 慢速限制，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndFrame: mama_slow, mama_fast
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def MAVP(self, periods: float = 14., minperiod: int = 2, maxperiod: int = 30, matype=0, **kwargs) -> IndSeries:
        """MAVP - Moving Average with Variable Period 可变周期移动平均线
        ---
        Args:
            periods: 周期，默认14
            minperiod: 最小周期，默认2
            maxperiod: 最大周期，默认30
            matype: 移动平均类型 (0=SMA, 1=EMA, 2=WMA, 3=DEMA, 4=TEMA, 5=TRIMA, 6=KAMA, 7=MAMA, 8=T3)

        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def MIDPOINT(self, timeperiod=14, **kwargs) -> IndSeries:
        """MIDPOINT - MidPoint over period 周期中点指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 周期中点指标序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def MIDPRICE(self, timeperiod=14, **kwargs) -> IndSeries:
        """MIDPRICE - Midpoint Price over period 周期中点价格指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 周期中点价格指标序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def SAR(self, acceleration=0, maximum=0, **kwargs) -> IndSeries:
        """SAR - Parabolic SAR 抛物线指标
        ---
        Args:
            acceleration: 加速度，默认0
            maximum: 最大值，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 抛物线指标序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def SAREXT(self, startvalue=0, offsetonreverse=0, accelerationinitlong=0, accelerationlong=0, accelerationmaxlong=0, accelerationinitshort=0, accelerationshort=0, accelerationmaxshort=0, **kwargs) -> IndSeries:
        """SAREXT - Parabolic SAR - Extended 扩展抛物线指标
        ---
        Args:
            startvalue: 起始值，默认0
            offsetonreverse: 反转偏移，默认0
            accelerationinitlong: 多头初始加速度，默认0
            accelerationlong: 多头加速度，默认0
            accelerationmaxlong: 多头最大加速度，默认0
            accelerationinitshort: 空头初始加速度，默认0
            accelerationshort: 空头加速度，默认0
            accelerationmaxshort: 空头最大加速度，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 扩展抛物线指标序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def SMA(self, timeperiod=30, **kwargs) -> IndSeries:
        """SMA - Simple Moving Average 简单移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 简单移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def T3(self, timeperiod=5, vfactor=0, **kwargs) -> IndSeries:
        """T3 - Triple Exponential Moving Average (T3) 三重指数移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认5
            vfactor: 体积因子，默认0
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三重指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def TEMA(self, timeperiod=30, **kwargs) -> IndSeries:
        """TEMA - Triple Exponential Moving Average 三重指数移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三重指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def TRIMA(self, timeperiod=30, **kwargs) -> IndSeries:
        """TRIMA - Triangular Moving Average 三角形移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三角形移动平均线序列
        """
        ...

    @tobtind(overlap=True, category="overlap", lib='talib')
    def WMA(close, timeperiod=30, **kwargs) -> IndSeries:
        """WMA - Weighted Moving Average 加权移动平均线
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 加权移动平均线序列
        """
        ...

    # Pattern Recognition Functions 形态识别
    @tobtind(category="Pattern Recognition Functions", lib='talib')
    def PatternRecognition(self, name: PRFNameTtype_ = "CDL2CROWS", penetration=0, **kwargs) -> IndSeries:
        """Pattern Recognition Functions 形态识别指标
        ---
        Args:
            name: 形态名称，默认"CDL2CROWS"（两只乌鸦）
            penetration: 穿透程度，默认0
            **kwargs: 额外参数

        ## name参考
        >>> "CDL2CROWS"两只乌鸦 , "CDL3BLACKCROWS"三只乌鸦 , "CDL3INSIDE"三内部上涨和下跌,
            "CDL3LINESTRIKE"三线打击, "CDL3OUTSIDE"三外部上涨和下跌, "CDL3STARSINSOUTH"南方三星,
            "CDL3WHITESOLDIERS"三个白兵, "CDLABANDONEDBABY"弃婴, "CDLADVANCEBLOCK"大敌当前,
            "CDLBELTHOLD"捉腰带线, "CDLBREAKAWAY"脱离, "CDLCLOSINGMARUBOZU"收盘缺影线,
            "CDLCONCEALBABYSWALL"藏婴吞没, "CDLCOUNTERATTACK"反击线, "CDLDARKCLOUDCOVER"乌云压顶,
            "CDLDOJI"十字, "CDLDOJISTAR"十字星, "CDLDRAGONFLYDOJI"蜻蜓十字/T形十字, "CDLENGULFING"吞噬模式,
            "CDLEVENINGDOJISTAR"十字暮星, "CDLEVENINGSTAR"暮星, "CDLGAPSIDESIDEWHITE"向上/下跳空并列阳线 ,
            "CDLGRAVESTONEDOJI"墓碑十字/倒T十字, "CDLHAMMER"锤头, "CDLHANGINGMAN"上吊线, "CDLHARAMI"母子线 ,
            "CDLHARAMICROSS"十字孕线, "CDLHIGHWAVE"风高浪大线, "CDLHIKKAKE"陷阱, "CDLHIKKAKEMOD"修正陷阱,
            "CDLHOMINGPIGEON"家鸽, "CDLIDENTICAL3CROWS"三胞胎乌鸦, "CDLINNECK"颈内线,
            "CDLINVERTEDHAMMER"倒锤头, "CDLKICKING"反冲形态, "CDLKICKINGBYLENGTH"由较长缺影线决定的反冲形态,
            "CDLLADDERBOTTOM"梯底, "CDLLONGLEGGEDDOJI"长脚十字, "CDLLONGLINE"长蜡烛,
            "CDLMARUBOZU"光头光脚/缺影线, "CDLMATCHINGLOW"相同低价, "CDLMATHOLD"铺垫, "CDLMORNINGDOJISTAR"十字晨星 ,
            "CDLMORNINGSTAR"晨星, "CDLONNECK"颈上线, "CDLPIERCING"刺透形态, "CDLRICKSHAWMAN"黄包车夫,
            "CDLRISEFALL3METHODS"上升/下降三法, "CDLSEPARATINGLINES"分离线, "CDLSHOOTINGSTAR"射击之星,
            "CDLSHORTLINE"短蜡烛, "CDLSPINNINGTOP"纺锤, "CDLSTALLEDPATTERN"停顿形态,
            "CDLSTICKSANDWICH"条形三明治 , "CDLTAKURI"探水竿, "CDLTASUKIGAP"跳空并列阴阳线, "CDLTHRUSTING"插入,
            "CDLTRISTAR"三星, "CDLUNIQUE3RIVER"奇特三河床, "CDLUPSIDEGAP2CROWS"向上跳空的两只乌鸦,
            "CDLXSIDEGAP3METHODS"上升/下降跳空三法

        NOTE:
            实例包括列：open, high, low, close

        Returns:
            IndSeries: 形态识别指标序列
        """
        ...

    # Price Transform Functions
    @tobtind(overlap=True, category="Price Transform Functions", lib='talib')
    def AVGPRICE(self, **kwargs) -> IndSeries:
        """AVGPRICE - Average Price 平均价格指标
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：open, high, low, close
        Returns:
            IndSeries: 平均价格指标序列
        """
        ...

    @tobtind(overlap=True, category="Price Transform Functions", lib='talib')
    def MEDPRICE(self, **kwargs) -> IndSeries:
        """MEDPRICE - Median Price 中位数价格指标
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 中位数价格指标序列
        """
        ...

    @tobtind(overlap=True, category="Price Transform Functions", lib='talib')
    def TYPPRICE(self, **kwargs) -> IndSeries:
        """TYPPRICE - Typical Price 代表性价格指标
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 代表性价格指标序列
        """
        ...

    @tobtind(overlap=True, category="Price Transform Functions", lib='talib')
    def WCLPRICE(self, **kwargs) -> IndSeries:
        """WCLPRICE - Weighted Close Price 加权收盘价指标
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 加权收盘价指标序列
        """
        ...

    # Statistic Functions 统计学指标
    @tobtind(category="Price Transform Functions", lib='talib')
    def BETA(self, timeperiod=5, **kwargs) -> IndSeries:
        """BETA - Beta β系数
        ---
        Args:
            timeperiod: 时间周期，默认5
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: β系数序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def CORREL(self, timeperiod=30, **kwargs) -> IndSeries:
        """CORREL - Pearson's Correlation Coefficient (r) 皮尔逊相关系数
        ---
        Args:
            timeperiod: 时间周期，默认30
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low
        Returns:
            IndSeries: 皮尔逊相关系数序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def LINEARREG(self, timeperiod=14, **kwargs) -> IndSeries:
        """LINEARREG - Linear Regression 线性回归指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 线性回归指标序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def LINEARREG_ANGLE(self, timeperiod=14, **kwargs) -> IndSeries:
        """LINEARREG_ANGLE - Linear Regression Angle 线性回归角度指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 线性回归角度指标序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def LINEARREG_INTERCEPT(self, timeperiod=14, **kwargs) -> IndSeries:
        """LINEARREG_INTERCEPT - Linear Regression Intercept 线性回归截距指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 线性回归截距指标序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def LINEARREG_SLOPE(self, timeperiod=14, **kwargs) -> IndSeries:
        """LINEARREG_SLOPE - Linear Regression Slope 线性回归斜率指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 线性回归斜率指标序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def STDDEV(self, timeperiod=5, nbdev=1, **kwargs) -> IndSeries:
        """STDDEV - Standard Deviation 标准偏差指标
        ---
        Args:
            timeperiod: 时间周期，默认5
            nbdev: 偏差倍数，默认1
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 标准偏差指标序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def TSF(self, timeperiod=14, **kwargs) -> IndSeries:
        """TSF - Time Series Forecast 时间序列预测指标
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 时间序列预测指标序列
        """
        ...

    @tobtind(category="Price Transform Functions", lib='talib')
    def VAR(self, timeperiod=5, nbdev=1, **kwargs) -> IndSeries:
        """VAR - VAR 方差指标
        ---
        Args:
            timeperiod: 时间周期，默认5
            nbdev: 偏差倍数，默认1
            **kwargs: 额外参数
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 方差指标序列
        """
        ...

    # Volatility Indicator Functions 波动率指标函数
    @tobtind(category="Volatility Indicator Functions", lib='talib')
    def ATR(self, timeperiod=14, **kwargs) -> IndSeries:
        """ATR - Average True Range 真实波动幅度均值
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 真实波动幅度均值序列
        """
        ...

    @tobtind(category="Volatility Indicator Functions", lib='talib')
    def NATR(self, timeperiod=14, **kwargs) -> IndSeries:
        """NATR - Normalized Average True Range 归一化波动幅度均值
        ---
        Args:
            timeperiod: 时间周期，默认14
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 归一化波动幅度均值序列
        """
        ...

    @tobtind(category="Volatility Indicator Functions", lib='talib')
    def TRANGE(self, **kwargs) -> IndSeries:
        """TRANGE - True Range 真实波动范围
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close
        Returns:
            IndSeries: 真实波动范围序列
        """
        ...

    # Volume Indicators 成交量指标
    @tobtind(category="Volume Indicators Functions", lib='talib')
    def AD(self, **kwargs) -> IndSeries:
        """AD - Chaikin A/D Line 累积/派发线
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close, volume
        Returns:
            IndSeries: 累积/派发线序列
        """
        ...

    @tobtind(category="Volume Indicators Functions", lib='talib')
    def ADOSC(self, fastperiod=3, slowperiod=10, **kwargs) -> IndSeries:
        """ADOSC - Chaikin A/D Oscillator Chaikin震荡指标
        ---
        Args:
            fastperiod: 快速周期，默认3
            slowperiod: 慢速周期，默认10
            **kwargs: 额外参数
        NOTE:
            实例包括列：high, low, close, volume
        Returns:
            IndSeries: Chaikin震荡指标序列
        """
        ...

    @tobtind(category="Volume Indicators Functions", lib='talib')
    def OBV(self, **kwargs) -> IndSeries:
        """OBV - On Balance Volume 能量潮指标
        ---
        Args:
            **kwargs: 额外参数
        NOTE:
            实例包括列：close, volume
        Returns:
            IndSeries: 能量潮指标序列
        """
        ...
        
        
    NON_KLINE_INDICATORS = ["ADD","DIV","MULT","SUB","CEIL","FLOOR"]
