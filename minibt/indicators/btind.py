from __future__ import annotations
from .base import tobtind
from ..utils import Any, pd, TYPE_CHECKING


if TYPE_CHECKING:
    from typing_ import *
    from .core import *



class BtInd:
    """## 自定义指标指引
    - 自定义指标类，用于封装项目中自定义的基础指标计算逻辑，提供框架兼容的指标访问接口

    ### 核心功能：
    - 基于输入的基础数据（IndSeries/IndFrame）提供自定义指标的计算与访问
    - 通过 @tobtind 装饰器将原生数据转换为框架内置的指标数据类型（IndSeries/IndFrame）
    - 支持基础行情数据（开盘价、最高价、最低价等）的直接访问与指标化处理

    ### 使用说明：
    - 1. 初始化：传入框架支持的基础数据对象（需包含对应的原始字段）
       >>> ind = self.data.btind.alerts()"""
    _perfixes: str = "btind_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @tobtind(overlap=False, lib='btind')
    def smoothrng(self, length: int = 14, mult: float = 1., **kwargs) -> IndSeries:
        """
        平滑平均范围指标 (Smoothed Average Range)
        ---------
        - 计算价格在一定周期内的平滑波动范围，用于识别市场的平均波动水平
        - 通常用于构建动态支撑阻力位或波动率相关的交易策略

        计算方法:
        ---------
        >>> 基于特定周期内的价格范围（最高价-最低价）进行平滑处理
            smooth_range = 平滑函数(high - low, length) * mult

        参数:
        ---------
        >>> length (int): 计算平滑范围的周期长度. 默认: 14
            mult (float): 范围乘数，用于调整波动范围大小. 默认: 1.0

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndSeries: 平滑后的价格范围序列

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算平滑范围指标
        >>> smooth_range = self.data.btind.smoothrng(length=14, mult=1.0)
        >>> 
        >>> # 使用平滑范围构建动态通道
        >>> def dynamic_channel_strategy(self):
        >>>     smooth_range = self.data.btind.smoothrng(length=20)
        >>>     upper_band = self.data.close + smooth_range
        >>>     lower_band = self.data.close - smooth_range
        >>>     
        >>>     if self.data.close.new > upper_band.new:
        >>>         return "价格突破上轨，可能回调"
        >>>     elif self.data.close.new < lower_band.new:
        >>>         return "价格跌破下轨，可能反弹"
        """
        ...

    @tobtind(lines=["rngfilt", "dir"], overlap=dict(rngfilt=True, dir=False), lib='btind')
    def rngfilt(self, r: pd.Series = None, **kwargs) -> IndFrame:
        """
        范围过滤指标 (Range Filter)
        ---------
        - 过滤价格波动中的异常值或噪音，提取主要的价格趋势
        - 通过设置范围阈值，排除超出正常波动范围的价格点

        计算方法:
        ---------
        >>> 基于给定的范围阈值r或动态计算的阈值，过滤价格数据
            filtered_price = 当价格在阈值范围内时保留原值，否则使用前值或插值

        参数:
        ---------
        >>> r (pd.Series): 范围阈值序列. 默认: None (使用自动计算的阈值)
            **kwargs: 其他参数

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndFrame: 过滤后的价格序列"rngfilt","dir"

        所需数据字段:
        ---------
        >>> close (或需要过滤的价格序列)

        使用案例:
        ---------
        >>> # 应用范围过滤
        >>> filtered_close = self.data.btind.rngfilt()
        >>> 
        >>> # 使用自定义范围阈值
        >>> threshold = self.data.btind.smoothrng(length=10) * 2
        >>> custom_filtered = self.data.btind.rngfilt(r=threshold)
        >>> 
        >>> def noise_reduction_strategy(self):
        >>>     # 过滤噪音后的价格
        >>>     clean_price = self.data.btind.rngfilt()
        >>>     
        >>>     # 计算过滤后的趋势
        >>>     trend = clean_price.line_trhend(period=5)
        >>>     
        >>>     if trend.new == 1:
        >>>         return "过滤后趋势向上"
        >>>     elif trend.new == -1:
        >>>         return "过滤后趋势向下"
        """
        ...

    @tobtind(lines=['filt', 'hband', 'lband', 'dir'], overlap=dict(filt=True, hband=True, lband=True, dir=False), lib='btind')
    def alerts(self, length: int = 14, mult: float = 2., **kwargs) -> IndFrame:
        """
        警报指标系统 (Alerts Indicator System)
        ---------
        - 综合性的多维度指标，提供过滤后的价格、上下通道和方向信号
        - 常用于趋势识别、突破交易和动态支撑阻力分析

        计算方法:
        ---------
        >>> 基于ATR或类似波动率指标构建动态通道
            filt = 价格的低通滤波或移动平均
            hband = filt + ATR * mult
            lband = filt - ATR * mult
            dir = 方向信号 (1: 向上, -1: 向下, 0: 中性)

        参数:
        ---------
        >>> length (int): 计算基准线的周期长度. 默认: 14
            mult (float): 通道宽度的乘数因子. 默认: 2.0

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndFrame: 包含四列的数据框
            - filt: 过滤后的基准价格
            - hband: 上通道线
            - lband: 下通道线
            - dir: 方向信号 (1, -1, 0)

        所需数据字段:
        ---------
        >>> high, low, close (用于计算波动率和价格)

        使用案例:
        ---------
        >>> # 获取警报指标数据
        >>> filt, hband, lband, dir = self.data.btind.alerts(length=14, mult=2.0)
        >>> 
        >>> # 通道突破策略
        >>> def alert_breakout_strategy(self):
        >>>     alerts_data = self.data.btind.alerts()
        >>>     
        >>>     # 价格突破上通道
        >>>     if self.data.close.new > alerts_data.hband.new:
        >>>         return "价格突破上通道，买入信号"
        >>>     
        >>>     # 价格跌破下通道
        >>>     elif self.data.close.new < alerts_data.lband.new:
        >>>         return "价格跌破下通道，卖出信号"
        >>>     
        >>>     # 方向信号确认
        >>>     if alerts_data.dir.new == 1:
        >>>         return "方向信号向上，多头趋势"
        >>>     elif alerts_data.dir.new == -1:
        >>>         return "方向信号向下，空头趋势"
        """
        ...

    @tobtind(lib='btind')
    def noises_density(self, length: int = 10, **kwargs) -> IndSeries:
        """
        价格噪音密度函数 (Price Noise Density Function)
        ---------
        - 量化价格序列中的噪音水平，衡量市场无序程度
        - 高噪音密度表示市场波动混乱，低噪音密度表示趋势清晰

        计算方法:
        ---------
        >>> 基于一定周期内价格变化的标准差与平均值的比率
            noise_density = std(price_changes) / mean(abs(price_changes))

        参数:
        ---------
        >>> length (int): 计算噪音密度的周期长度. 默认: 10

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndSeries: 噪音密度值序列，值越高表示噪音越大

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算噪音密度
        >>> noise_level = self.data.btind.noises_density(length=10)
        >>> 
        >>> # 噪音过滤策略
        >>> def noise_based_filtering(self):
        >>>     noise = self.data.btind.noises_density(length=14)
        >>>     
        >>>     # 噪音水平过高时减少交易
        >>>     if noise.new > 0.8:
        >>>         return "噪音水平过高，建议观望"
        >>>     
        >>>     # 噪音水平适中时正常交易
        >>>     elif noise.new < 0.3:
        >>>         return "噪音水平低，趋势清晰"
        >>>     
        >>>     return "噪音水平中等"
        >>> 
        >>> # 结合趋势指标使用
        >>> def trend_quality_analysis(self):
        >>>     trend = self.data.ema(length=20).line_trhend(period=3)
        >>>     noise = self.data.btind.noises_density(length=10)
        >>>     
        >>>     if trend.new == 1 and noise.new < 0.4:
        >>>         return "上升趋势且噪音低，趋势质量高"
        """
        ...

    @tobtind(lib='btind')
    def noises_er(self, length: int = 10, **kwargs) -> IndSeries:
        """
        效率比率指标 (Efficiency Ratio)
        ---------
        - 衡量价格变化的效率，表示趋势的强度和连续性
        - 由Perry Kaufman提出，用于自适应移动平均系统
        - 值在0到1之间，越高表示趋势越强，越低表示噪音越多

        计算方法:
        ---------
        >>> direction = abs(close - close[length])
            volatility = sum(abs(close[i] - close[i-1]) for i in range(1, length+1))
            ER = direction / volatility

        参数:
        ---------
        >>> length (int): 计算效率比率的周期长度. 默认: 10

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndSeries: 效率比率序列，范围0-1

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算效率比率
        >>> efficiency = self.data.btind.noises_er(length=10)
        >>> 
        >>> # 自适应移动平均策略
        >>> def adaptive_ma_strategy(self):
        >>>     er = self.data.btind.noises_er(length=10)
        >>>     
        >>>     # 根据效率比率调整均线周期
        >>>     # 高效率时使用短期均线，低效率时使用长期均线
        >>>     if er.new > 0.7:
        >>>         ma = self.data.ema(length=10)
        >>>     elif er.new < 0.3:
        >>>         ma = self.data.ema(length=30)
        >>>     else:
        >>>         ma = self.data.ema(length=20)
        >>>     
        >>>     return f"自适应均线: {ma.new:.2f}"
        >>> 
        >>> # 趋势强度判断
        >>> def trend_strength_analysis(self):
        >>>     er = self.data.btind.noises_er(length=14)
        >>>     
        >>>     if er.new > 0.5:
        >>>         return "趋势强劲"
        >>>     elif er.new > 0.3:
        >>>         return "趋势中等"
        >>>     else:
        >>>         return "趋势疲弱或震荡"
        """
        ...

    @tobtind(lib='btind')
    def noises_fd(self, length: int = 10, **kwargs) -> IndSeries:
        """
        分形维度指标 (Fractal Dimension)
        ---------
        - 量化价格序列的复杂性和不规则程度
        - 用于区分趋势市场和震荡市场
        - 分形维度接近1表示强趋势，接近2表示随机波动

        计算方法:
        ---------
        >>> 基于Higuchi算法或其他分形维度计算方法
            FD = log(L(k)) / log(k) 对于不同的k值

        参数:
        ---------
        >>> length (int): 计算分形维度的窗口长度. 默认: 10

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndSeries: 分形维度序列，通常在1-2之间

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算分形维度
        >>> fractal_dim = self.data.btind.noises_fd(length=10)
        >>> 
        >>> # 市场状态识别
        >>> def market_state_detection(self):
        >>>     fd = self.data.btind.noises_fd(length=14)
        >>>     
        >>>     if fd.new < 1.3:
        >>>         return "强趋势市场"
        >>>     elif fd.new < 1.7:
        >>>         return "中等趋势市场"
        >>>     else:
        >>>         return "震荡市场"
        >>> 
        >>> # 结合其他指标使用
        >>> def fractal_trend_strategy(self):
        >>>     fd = self.data.btind.noises_fd(length=10)
        >>>     er = self.data.btind.noises_er(length=10)
        >>>     
        >>>     # 强趋势且高效率时入场
        >>>     if fd.new < 1.4 and er.new > 0.6:
        >>>         return "强趋势高效率，适合趋势跟踪"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='btind',)
    def kama(self, length=10, fast=2, slow=30, drift=1, offset=0, **kwargs) -> IndFrame | IndSeries:
        """
        考夫曼自适应移动平均 (Kaufman Adaptive Moving Average, KAMA)
        ---------
        - 由Perry Kaufman开发的动态调整速度的移动平均
        - 根据市场噪音水平自动调整平滑系数，在趋势和震荡市中都有良好表现
        - 在趋势市场中反应迅速，在震荡市中过滤噪音

        计算方法:
        ---------
        >>> ER = 效率比率 (方向变化/波动率)
            fast_alpha = 2/(fast+1)
            slow_alpha = 2/(slow+1)
            alpha = (ER * (fast_alpha - slow_alpha) + slow_alpha)^2
            KAMA = KAMA[1] + alpha * (close - KAMA[1])

        参数:
        ---------
        >>> length (int): 计算效率比率的周期. 默认: 10
            fast (int): 快速平滑周期. 默认: 2
            slow (int): 慢速平滑周期. 默认: 30
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndSeries | IndFrame: 自适应移动平均值序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算KAMA
        >>> kama = self.data.btind.kama(length=10, fast=2, slow=30)
        >>> 
        >>> # 自适应趋势跟踪策略
        >>> def kama_trend_strategy(self):
        >>>     kama = self.data.btind.kama()
        >>>     
        >>>     # 价格突破KAMA时入场
        >>>     if self.data.close.new > kama.new and self.data.close.prev <= kama.prev:
        >>>         return "价格突破KAMA，买入信号"
        >>>     elif self.data.close.new < kama.new and self.data.close.prev >= kama.prev:
        >>>         return "价格跌破KAMA，卖出信号"
        >>> 
        >>> # 多周期KAMA系统
        >>> def multi_kama_system(self):
        >>>     kama_fast = self.data.btind.kama(length=5, fast=2, slow=10)
        >>>     kama_slow = self.data.btind.kama(length=20, fast=2, slow=30)
        >>>     
        >>>     # 快线上穿慢线
        >>>     if kama_fast.new > kama_slow.new and kama_fast.prev <= kama_slow.prev:
        >>>         return "快线上穿慢线，金叉买入"
        >>>     elif kama_fast.new < kama_slow.new and kama_fast.prev >= kama_slow.prev:
        >>>         return "快线下穿慢线，死叉卖出"
        """
        ...

    @tobtind(lines=['mama', 'fama'], overlap=dict(mama=True, fama=True), lib='btind',)
    def mama(self, fastlimit: float = 0.6185, slowlimit: float = 0.06185, **kwargs) -> IndFrame:
        """
        MESA自适应移动平均 (MESA Adaptive Moving Average, MAMA)
        ---------
        - 由John Ehlers开发的基于希尔伯特变换的自适应移动平均
        - 自动调整相位以最小化滞后，提供几乎没有滞后的趋势信号
        - 包含MAMA(主自适应移动平均)和FAMA(跟随自适应移动平均)两条线

        计算方法:
        ---------
        >>> 基于希尔伯特变换计算瞬时周期
            使用相位加速度调整平滑系数
            MAMA = 基于相位调整的移动平均
            FAMA = MAMA的进一步平滑版本

        参数:
        ---------
        >>> fastlimit (float): 快速限制参数，控制MAMA的响应速度. 默认: 0.6185
            slowlimit (float): 慢速限制参数，控制FAMA的平滑程度. 默认: 0.06185

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndFrame: 包含两列的数据框
            - mama: MESA自适应移动平均主线
            - fama: 跟随自适应移动平均线

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算MAMA指标
        >>> mama, fama = self.data.btind.mama(fastlimit=0.6185, slowlimit=0.06185)
        >>> 
        >>> # MAMA/FAMA交叉策略
        >>> def mama_cross_strategy(self):
        >>>     mama_data = self.data.btind.mama()
        >>>     
        >>>     # MAMA上穿FAMA
        >>>     if mama_data.mama.new > mama_data.fama.new and mama_data.mama.prev <= mama_data.fama.prev:
        >>>         return "MAMA上穿FAMA，买入信号"
        >>>     
        >>>     # MAMA下穿FAMA
        >>>     if mama_data.mama.new < mama_data.fama.new and mama_data.mama.prev >= mama_data.fama.prev:
        >>>         return "MAMA下穿FAMA，卖出信号"
        >>> 
        >>> # 结合价格位置分析
        >>> def mama_price_position(self):
        >>>     mama_data = self.data.btind.mama()
        >>>     
        >>>     # 价格在MAMA/FAMA之上
        >>>     if self.data.close.new > mama_data.mama.new and self.data.close.new > mama_data.fama.new:
        >>>         return "价格在双线之上，强势多头"
        >>>     
        >>>     # 价格在MAMA/FAMA之下
        >>>     if self.data.close.new < mama_data.mama.new and self.data.close.new < mama_data.fama.new:
        >>>         return "价格在双线之下，弱势空头"
        """
        ...

    @tobtind(lines=['long', 'short', 'thrend'], overlap=dict(long=True, short=True, thrend=False), lib='btind',)
    def pmax(self, length: int = 14, mult: float = 3., mode: MaModeType = 'hma', dev: DevType = "stdev", **kwargs) -> IndFrame:
        """
        PMax指标 (Price Max) - 第一个版本
        ---------
        - 价格最大化指标的第一版本，同时维护多头和空头两条独立的止损线
        - 基于移动平均线和波动率通道构建，提供动态的止损保护和趋势方向判断
        - 主要用于趋势跟踪策略和风险管理

        计算方法:
        ---------
        >>> ma = 指定类型的移动平均线(close, length)
            dev_value = 指定的波动率计算方法(close, length) * mult
            upper_band = ma + dev_value
            lower_band = ma - dev_value
            # 分别维护多头和空头止损线，根据趋势方向动态更新

        参数:
        ---------
        >>> length (int): 计算移动平均线和波动率的周期. 默认: 14
            mult (float): 波动率通道的宽度乘数. 默认: 3.0
            mode (str): 移动平均线类型，支持多种平均算法:
                "dema", "ema", "fwma", "hma", "linreg", "midpoint", "pwma", "rma",
                "sinwma", "sma", "swma", "t3", "tema", "trima", "vidya", "wma", "zlma"
                . 默认: 'hma' (赫尔移动平均)
            dev (str): 波动率计算方法:
                "stdev": 标准差
                "art": 平均真实波幅
                "variance": 方差
                "smoothrng": 平滑范围
                . 默认: "stdev"

        可选参数:
        ---------
        >>> **kwargs: 传递给移动平均线和波动率计算函数的其他参数

        返回:
        ---------
        >>> IndFrame: 包含三列的数据框
            - long: 多头止损线，在多头趋势中动态上移
            - short: 空头止损线，在空头趋势中动态下移
            - thrend: 趋势方向 (1: 多头趋势, -1: 空头趋势)

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算PMax指标
        >>> long_stop, short_stop, trend = self.data.btind.pmax(length=14, mult=3.0)
        >>> 
        >>> # 趋势跟踪止损策略
        >>> def pmax_trend_stop_strategy(self):
        >>>     pmax_data = self.data.btind.pmax()
        >>>     
        >>>     # 多头趋势时，使用long止损线
        >>>     if pmax_data.thrend.new == 1:
        >>>         if self.data.close.new < pmax_data.long.new:
        >>>             return "多头止损触发"
        >>>         return "多头持仓中，止损线: {pmax_data.long.new:.2f}"
        >>>     
        >>>     # 空头趋势时，使用short止损线
        >>>     elif pmax_data.thrend.new == -1:
        >>>         if self.data.close.new > pmax_data.short.new:
        >>>             return "空头止损触发"
        >>>         return "空头持仓中，止损线: {pmax_data.short.new:.2f}"
        >>> 
        >>> # 结合突破信号
        >>> def pmax_breakout_strategy(self):
        >>>     pmax_data = self.data.btind.pmax()
        >>>     
        >>>     # 价格突破上通道且趋势向上
        >>>     if (self.data.close.new > pmax_data.long.new and 
        >>>         pmax_data.thrend.new == 1 and 
        >>>         pmax_data.thrend.prev != 1):
        >>>         return "突破上轨，趋势转多"
        >>>     
        >>>     # 价格跌破下通道且趋势向下
        >>>     if (self.data.close.new < pmax_data.short.new and 
        >>>         pmax_data.thrend.new == -1 and 
        >>>         pmax_data.thrend.prev != -1):
        >>>         return "跌破下轨，趋势转空"
        """
        ...

    @tobtind(lines=['pmax', 'thrend'], overlap=dict(pmax=True, thrend=False), lib='btind',)
    def pmax2(self, length: int = 14, mult: float = 3., mode: MaModeType = 'hma', dev: DevType = "stdev", **kwargs) -> IndFrame:
        """
        PMax指标 (Price Max) - 第二个版本
        ---------
        - 价格最大化指标的第二版本，简化版，合并多头空头止损线为单一的PMax线
        - 根据趋势方向动态切换PMax线的计算方式，提供更简洁的止损参考
        - 适用于需要简化止损逻辑的趋势策略

        计算方法:
        ---------
        >>> ma = 指定类型的移动平均线(close, length)
            dev_value = 指定的波动率计算方法(close, length) * mult
            upper_band = ma + dev_value
            lower_band = ma - dev_value
            # 根据趋势方向选择upper_band或lower_band作为PMax线

        参数:
        ---------
        >>> length (int): 计算移动平均线和波动率的周期. 默认: 14
            mult (float): 波动率通道的宽度乘数. 默认: 3.0
            mode (str): 移动平均线类型，支持多种平均算法:
                "dema", "ema", "fwma", "hma", "linreg", "midpoint", "pwma", "rma",
                "sinwma", "sma", "swma", "t3", "tema", "trima", "vidya", "wma", "zlma"
                . 默认: 'hma' (赫尔移动平均)
            dev (str): 波动率计算方法:
                "stdev": 标准差
                "art": 平均真实波幅
                "variance": 方差
                "smoothrng": 平滑范围
                . 默认: "stdev"

        可选参数:
        ---------
        >>> **kwargs: 传递给移动平均线和波动率计算函数的其他参数

        返回:
        ---------
        >>> IndFrame: 包含两列的数据框
            - pmax: 合并后的PMax止损线，趋势向上时为上轨，趋势向下时为下轨
            - thrend: 趋势方向 (1: 多头趋势, -1: 空头趋势)

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算PMax2指标
        >>> pmax_line, trend = self.data.btind.pmax2(length=14, mult=3.0)
        >>> 
        >>> # 简化止损策略
        >>> def pmax2_simple_stop_strategy(self):
        >>>     pmax_data = self.data.btind.pmax2()
        >>>     
        >>>     # 多头趋势中的止损
        >>>     if pmax_data.thrend.new == 1:
        >>>         if self.data.close.new < pmax_data.pmax.new:
        >>>             return "多头止损触发"
        >>>     
        >>>     # 空头趋势中的止损
        >>>     elif pmax_data.thrend.new == -1:
        >>>         if self.data.close.new > pmax_data.pmax.new:
        >>>             return "空头止损触发"
        >>> 
        >>> # 趋势反转识别
        >>> def pmax2_trend_reversal(self):
        >>>     pmax_data = self.data.btind.pmax2()
        >>>     
        >>>     # 趋势由多转空
        >>>     if pmax_data.thrend.new == -1 and pmax_data.thrend.prev == 1:
        >>>         return "趋势由多转空，注意风险"
        >>>     
        >>>     # 趋势由空转多
        >>>     if pmax_data.thrend.new == 1 and pmax_data.thrend.prev == -1:
        >>>         return "趋势由空转多，关注机会"
        """
        ...

    @tobtind(lines=['pmax', 'thrend'], overlap=dict(pmax=True, thrend=False), lib='btind',)
    def pmax3(self, length: int = 14, mult: float = 3., mode: MaModeType = 'hma', dev: DevType = "stdev", **kwargs) -> IndFrame:
        """
        PMax指标 (Price Max) - 第三个版本
        ---------
        - 价格最大化指标的第三版本，改进版，提供PMax线变化趋势的精细判断
        - 关注PMax线自身的变化方向而非价格趋势，提供更敏感的趋势变化信号
        - 适用于需要早期趋势变化预警的策略

        计算方法:
        ---------
        >>> ma = 指定类型的移动平均线(close, length)
            dev_value = 指定的波动率计算方法(close, length) * mult
            upper_band = ma + dev_value
            lower_band = ma - dev_value
            # PMax线基于复杂的趋势判断逻辑更新，thrend反映PMax线的变化方向

        参数:
        ---------
        >>> length (int): 计算移动平均线和波动率的周期. 默认: 14
            mult (float): 波动率通道的宽度乘数. 默认: 3.0
            mode (str): 移动平均线类型，支持多种平均算法:
                "dema", "ema", "fwma", "hma", "linreg", "midpoint", "pwma", "rma",
                "sinwma", "sma", "swma", "t3", "tema", "trima", "vidya", "wma", "zlma"
                . 默认: 'hma' (赫尔移动平均)
            dev (str): 波动率计算方法:
                "stdev": 标准差
                "art": 平均真实波幅
                "variance": 方差
                "smoothrng": 平滑范围
                . 默认: "stdev"

        可选参数:
        ---------
        >>> **kwargs: 传递给移动平均线和波动率计算函数的其他参数

        返回:
        ---------
        >>> IndFrame: 包含两列的数据框
            - pmax: PMax线，基于改进的逻辑计算
            - thrend: PMax线的变化趋势 (1: PMax线上升, -1: PMax线下降, 0: 不变)

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算PMax3指标
        >>> pmax_line, pmax_trend = self.data.btind.pmax3(length=14, mult=3.0)
        >>> 
        >>> # PMax线趋势变化预警
        >>> def pmax3_trend_change_alert(self):
        >>>     pmax_data = self.data.btind.pmax3()
        >>>     
        >>>     # PMax线开始上升
        >>>     if pmax_data.thrend.new == 1 and pmax_data.thrend.prev != 1:
        >>>         return "PMax线开始上升，可能预示趋势转强"
        >>>     
        >>>     # PMax线开始下降
        >>>     if pmax_data.thrend.new == -1 and pmax_data.thrend.prev != -1:
        >>>         return "PMax线开始下降，可能预示趋势转弱"
        >>> 
        >>> # 结合价格与PMax线关系
        >>> def pmax3_price_relationship(self):
        >>>     pmax_data = self.data.btind.pmax3()
        >>>     
        >>>     # 价格在PMax线上方且PMax线上升
        >>>     if (self.data.close.new > pmax_data.pmax.new and 
        >>>         pmax_data.thrend.new == 1):
        >>>         return "价格在PMax线上方且PMax线上升，强势信号"
        >>>     
        >>>     # 价格在PMax线下方且PMax线下降
        >>>     if (self.data.close.new < pmax_data.pmax.new and 
        >>>         pmax_data.thrend.new == -1):
        >>>         return "价格在PMax线下方且PMax线下降，弱势信号"
        """
        ...

    @tobtind(lib='btind')
    def pv(self, length=10, **kwargs) -> IndSeries:
        """
        价格波动率指标 (Price Volatility)
        ---------
        - 衡量价格在指定周期内的波动率水平
        - 用于评估市场波动程度和风险水平
        - 高波动率可能预示着趋势变化或市场不确定性增加

        计算方法:
        ---------
        >>> 基于价格变化的标准差或其他波动率度量方法
            volatility = 价格变化的波动率度量(close, length)

        参数:
        ---------
        >>> length (int): 计算波动率的周期长度. 默认: 10

        可选参数:
        ---------
        >>> **kwargs: 传递给波动率计算函数的其他参数

        返回:
        ---------
        >>> IndSeries: 价格波动率序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算价格波动率
        >>> volatility = self.data.btind.pv(length=10)
        >>> 
        >>> # 波动率自适应策略
        >>> def volatility_adaptive_strategy(self):
        >>>     vol = self.data.btind.pv(length=20)
        >>>     
        >>>     # 高波动率时降低仓位或增加止损距离
        >>>     if vol.new > 0.02:
        >>>         return "波动率高，建议降低仓位或放宽止损"
        >>>     
        >>>     # 低波动率时正常交易
        >>>     elif vol.new < 0.005:
        >>>         return "波动率低，适合正常交易"
        >>> 
        >>> # 结合趋势判断
        >>> def volatility_trend_combo(self):
        >>>     trend = self.data.ema(20).line_trhend(period=3)
        >>>     vol = self.data.btind.pv(length=10)
        >>>     
        >>>     # 趋势强劲且波动率适中
        >>>     if trend.new == 1 and 0.005 < vol.new < 0.015:
        >>>         return "趋势向上且波动率适中，理想交易环境"
        """
        ...

    @tobtind(lib='btind')
    def realized(self, length: int = 10, **kwargs) -> IndSeries:
        """
        已实现波动率指标 (Realized Volatility)
        ---------
        - 基于历史收益率计算的已实现波动率，反映实际发生的价格波动
        - 与隐含波动率不同，已实现波动率基于实际价格数据计算
        - 用于风险评估、仓位管理和波动率交易策略

        计算方法:
        ---------
        >>> 基于对数收益率的标准差计算
            returns = log(close/close_shift(1))
            realized_vol = std(returns, length) * sqrt(年化因子)

        参数:
        ---------
        >>> length (int): 计算已实现波动率的周期长度. 默认: 10

        可选参数:
        ---------
        >>> **kwargs: 传递给波动率计算函数的其他参数

        返回:
        ---------
        >>> IndSeries: 已实现波动率序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算已实现波动率
        >>> realized_vol = self.data.btind.realized(length=10)
        >>> 
        >>> # 波动率回归策略
        >>> def volatility_regression_strategy(self):
        >>>     vol = self.data.btind.realized(length=20)
        >>>     vol_ma = vol.ema(period=10)
        >>>     
        >>>     # 波动率从高位回归均值
        >>>     if vol.prev > vol_ma.prev * 1.5 and vol.new <= vol_ma.new * 1.5:
        >>>         return "波动率从高位回归，可能适合入场"
        >>> 
        >>> # 风险评估
        >>> def risk_assessment(self):
        >>>     vol = self.data.btind.realized(length=10)
        >>>     
        >>>     if vol.new > 0.03:
        >>>         return "高风险区域，建议谨慎操作"
        >>>     elif vol.new < 0.01:
        >>>         return "低风险区域，适合积极操作"
        """
        ...

    @tobtind(lines=['rshl', 'rslh'], lib='btind')
    def rsrs(self, length: int = 10, method: RSRSMethodType = 'r1', weights=True, **kwargs) -> IndFrame:
        """
        RSRS阻力支撑相对强度指标 (Resistance Support Relative Strength)
        ---------
        - 通过回归分析计算阻力位和支撑位的相对强度
        - 评估市场在阻力位和支撑位附近的表现，预测突破概率
        - 广泛应用于量化投资和算法交易

        计算方法:
        ---------
        >>> 基于最高价和最低价的线性回归分析
            # 不同method对应不同的计算方式
            r1: 标准线性回归斜率
            r2: 加权线性回归斜率
            r3: 改进的回归方法

        参数:
        ---------
        >>> length (int): 回归分析的周期长度. 默认: 10
            method (str): 计算方法:
                'r1': 标准方法
                'r2': 加权方法
                'r3': 改进方法
                . 默认: 'r1'
            weights (bool): 是否使用权重. 默认: True

        可选参数:
        ---------
        >>> **kwargs: 传递给回归分析函数的其他参数

        返回:
        ---------
        >>> IndFrame: 包含两列的数据框
            - rshl: 阻力支撑相对强度 (基于高低价)
            - rslh: 支撑阻力相对强度 (基于低高价)

        所需数据字段:
        ---------
        >>> high, low, volume

        使用案例:
        ---------
        >>> # 计算RSRS指标
        >>> rshl, rslh = self.data.btind.rsrs(length=10, method='r1')
        >>> 
        >>> # 阻力支撑强度分析
        >>> def rsrs_strength_analysis(self):
        >>>     rsrs_data = self.data.btind.rsrs()
        >>>     
        >>>     # 阻力强度高
        >>>     if rsrs_data.rshl.new > 0.8:
        >>>         return "阻力强度高，突破难度大"
        >>>     
        >>>     # 支撑强度高
        >>>     if rsrs_data.rslh.new > 0.8:
        >>>         return "支撑强度高，下跌难度大"
        >>> 
        >>> # 突破概率预测
        >>> def rsrs_breakout_probability(self):
        >>>     rsrs_data = self.data.btind.rsrs()
        >>>     
        >>>     # 阻力弱支撑强，向上突破概率高
        >>>     if rsrs_data.rshl.new < 0.3 and rsrs_data.rslh.new > 0.7:
        >>>         return "向上突破概率高"
        >>>     
        >>>     # 阻力强支撑弱，向下突破概率高
        >>>     if rsrs_data.rshl.new > 0.7 and rsrs_data.rslh.new < 0.3:
        >>>         return "向下突破概率高"
        """
        ...

    @tobtind(overlap=True, lib='btind')
    def savitzky_golay(self, window_length: Any = 10, polyorder: Any = 2, deriv: int = 0, delta: float = 1, axis: int = -1, mode: str = 'interp', cval: float = 0, **kwargs) -> IndSeries:
        """
        Savitzky-Golay滤波平滑器
        ---------
        - 使用Savitzky-Golay滤波器平滑价格序列，保留重要特征的同时去除噪音
        - 在保持信号形状特征的前提下有效过滤高频噪声
        - 广泛应用于信号处理、光谱分析和金融时间序列平滑

        计算方法:
        ---------
        >>> 通过局部多项式回归进行卷积平滑
            y[i] = Σ_{j=-m}^{m} c_j * x[i+j]
            其中c_j是Savitzky-Golay系数

        参数:
        ---------
        >>> window_length (int): 滤波器窗口长度，必须为正奇数. 默认: 10
            polyorder (int): 多项式阶数，必须小于window_length. 默认: 2
            deriv (int): 导数阶数，0表示平滑，>0表示计算导数. 默认: 0
            delta (float): 采样间隔，用于导数计算. 默认: 1.0
            axis (int): 应用滤波器的轴. 默认: -1 (最后一个轴)
            mode (str): 边界处理模式:
                'mirror': 镜像扩展
                'constant': 常数填充
                'nearest': 最近邻扩展
                'wrap': 环绕扩展
                'interp': 内插模式
                . 默认: 'interp'
            cval (float): constant模式下的填充值. 默认: 0.0

        可选参数:
        ---------
        >>> **kwargs: 传递给滤波器的其他参数

        返回:
        ---------
        >>> IndSeries: 平滑后的价格序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 应用Savitzky-Golay滤波
        >>> smoothed = self.data.btind.savitzky_golay(window_length=11, polyorder=3)
        >>> 
        >>> # 噪音过滤策略
        >>> def sg_filter_strategy(self):
        >>>     # 原始价格噪音大
        >>>     raw_trend = self.data.close.line_trhend(period=3)
        >>>     
        >>>     # 平滑后价格
        >>>     smoothed = self.data.btind.savitzky_golay(window_length=9, polyorder=2)
        >>>     smooth_trend = smoothed.line_trhend(period=3)
        >>>     
        >>>     # 平滑后趋势更清晰
        >>>     if smooth_trend.new == 1 and raw_trend.new == 0:
        >>>         return "平滑显示上升趋势，原始数据噪音掩盖"
        >>> 
        >>> # 导数计算（趋势强度）
        >>> def sg_derivative_trend(self):
        >>>     # 计算一阶导数（瞬时斜率）
        >>>     derivative = self.data.btind.savitzky_golay(
        >>>         window_length=11, polyorder=3, deriv=1)
        >>>     
        >>>     if derivative.new > 0:
        >>>         return f"趋势向上，强度: {derivative.new:.4f}"
        >>>     elif derivative.new < 0:
        >>>         return f"趋势向下，强度: {derivative.new:.4f}"
        """
        ...

    @tobtind(lines=['thrend', 'dir', 'long', 'short'], overlap=dict(thrend=False, dir=False, long=True, short=True), lib='btind')
    def supertrend(self, length=14, multiplier=2., weights=2., **Kwargs) -> IndFrame:
        """
        超级趋势指标 (SuperTrend)
        ---------
        - 基于ATR的动态趋势跟踪指标，提供清晰的趋势方向和止损参考
        - 通过上下轨构建趋势通道，价格突破通道时趋势反转
        - 结合趋势方向、止损线和买卖信号于一体

        计算方法:
        ---------
        >>> atr = ATR(high, low, close, length)
            basic_upper = (high + low) / 2 + multiplier * atr
            basic_lower = (high + low) / 2 - multiplier * atr
            # 根据价格与通道关系动态调整上下轨和趋势方向

        参数:
        ---------
        >>> length (int): 计算ATR的周期长度. 默认: 14
            multiplier (float): AR乘数，决定通道宽度. 默认: 2.0
            weights (float): 权重参数，影响通道计算. 默认: 2.0

        可选参数:
        ---------
        >>> **Kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndFrame: 包含四列的数据框
            - thrend: 趋势方向 (1: 上涨, -1: 下跌)
            - dir: 交易方向信号
            - long: 多头止损线
            - short: 空头止损线

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算超级趋势指标
        >>> trend, dir_signal, long_stop, short_stop = self.data.btind.SuperTrend()
        >>> 
        >>> # 超级趋势交易策略
        >>> def supertrend_strategy(self):
        >>>     st_data = self.data.btind.SuperTrend()
        >>>     
        >>>     # 趋势转多信号
        >>>     if st_data.thrend.new == 1 and st_data.thrend.prev == -1:
        >>>         return "趋势转多，买入信号"
        >>>     
        >>>     # 趋势转空信号
        >>>     if st_data.thrend.new == -1 and st_data.thrend.prev == 1:
        >>>         return "趋势转空，卖出信号"
        >>> 
        >>> # 动态止损管理
        >>> def supertrend_stop_management(self):
        >>>     st_data = self.data.btind.SuperTrend()
        >>>     
        >>>     # 多头持仓止损
        >>>     if st_data.thrend.new == 1:
        >>>         return f"多头持仓，动态止损位: {st_data.long.new:.2f}"
        >>>     
        >>>     # 空头持仓止损
        >>>     if st_data.thrend.new == -1:
        >>>         return f"空头持仓，动态止损位: {st_data.short.new:.2f}"
        >>> 
        >>> # 多时间框架确认
        >>> def multi_timeframe_supertrend(self):
        >>>     # 主周期超级趋势
        >>>     main_st = self.data.btind.SuperTrend(length=10, multiplier=3)
        >>>     
        >>>     # 次周期超级趋势
        >>>     # 注意：实际使用中需要获取次周期数据
        >>>     # sub_data = self.data.resample('15min')
        >>>     # sub_st = sub_data.btind.SuperTrend(length=20, multiplier=2)
        >>>     
        >>>     # 双周期共振
        >>>     if main_st.thrend.new == 1:  # and sub_st.thrend.new == 1
        >>>         return "多周期共振向上，强趋势信号"
        """
        ...

    @tobtind(category="overlap", lib='btind')
    def zigzag(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> IndSeries:
        """
        ZigZag指标 - 包含NaN数据版本
        ---------
        - 识别价格序列中的峰值和谷值，过滤微小波动，突出主要趋势转折点
        - 通过设定阈值条件过滤掉不满足幅度要求的转折点，只保留显著的高低点
        - 包含NaN值版本，非转折点的位置填充为NaN，便于绘图时忽略这些点

        计算方法:
        ---------
        >>> 遍历价格序列，当价格相对于前一个转折点的变化超过阈值时，记录新的转折点
            峰值条件: 当前高点相对前一谷值的涨幅 > up_thresh * multiplier
            谷值条件: 当前低点相对前一峰值的跌幅 > down_thresh * multiplier
            非转折点位置填充NaN

        参数:
        ---------
        >>> up_thresh (float): 定义峰值的最小相对涨幅阈值. 默认: 0.0
            down_thresh (float): 定义谷值的最小相对跌幅阈值. 默认: 0.0
            multiplier (float): 阈值乘数，用于调整阈值大小. 默认: 1.0

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: ZigZag转折点序列
            - 1: 表示峰值 (peak)
            - -1: 表示谷值 (valley)
            - NaN: 非转折点位置

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算ZigZag指标
        >>> zigzag_line = self.data.btind.zigzag(up_thresh=0.05, down_thresh=0.05)
        >>> 
        >>> # 趋势转折点识别
        >>> def zigzag_turn_points(self):
        >>>     zigzag = self.data.btind.zigzag(up_thresh=0.03, down_thresh=0.03)
        >>>     
        >>>     # 找到最近的转折点
        >>>     if not np.isnan(zigzag.new):
        >>>         if zigzag.new == 1:
        >>>             return f"峰值转折点，价格: {self.data.high.new:.2f}"
        >>>         elif zigzag.new == -1:
        >>>             return f"谷值转折点，价格: {self.data.low.new:.2f}"
        >>> 
        >>> # 支撑阻力位识别
        >>> def zigzag_support_resistance(self):
        >>>     zigzag = self.data.btind.zigzag(up_thresh=0.05, down_thresh=0.05)
        >>>     
        >>>     # 通过ZigZag识别历史支撑阻力位
        >>>     peaks = self.data.high[zigzag == 1]  # 所有峰值
        >>>     valleys = self.data.low[zigzag == -1]  # 所有谷值
        >>>     
        >>>     if len(peaks) > 0 and len(valleys) > 0:
        >>>         return f"识别到 {len(peaks)} 个阻力位，{len(valleys)} 个支撑位"
        """
        ...

    @tobtind(category="overlap", lib='btind')
    def zigzag_full(self, up_thresh: float = 0.001, down_thresh: float = 0.001, multiplier: float = 1., **kwargs) -> IndSeries:
        """
        ZigZag指标 - 无NaN数据版本
        ---------
        - 识别价格序列中的峰值和谷值，过滤微小波动，突出主要趋势转折点
        - 通过设定阈值条件过滤掉不满足幅度要求的转折点，只保留显著的高低点
        - 无NaN值版本，非转折点的位置填充0，便于后续计算和分析

        计算方法:
        ---------
        >>> 遍历价格序列，当价格相对于前一个转折点的变化超过阈值时，记录新的转折点
            峰值条件: 当前高点相对前一谷值的涨幅 > up_thresh * multiplier
            谷值条件: 当前低点相对前一峰值的跌幅 > down_thresh * multiplier
            非转折点位置填充0

        参数:
        ---------
        >>> up_thresh (float): 定义峰值的最小相对涨幅阈值. 默认: 0.0
            down_thresh (float): 定义谷值的最小相对跌幅阈值. 默认: 0.0
            multiplier (float): 阈值乘数，用于调整阈值大小. 默认: 1.0

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: ZigZag转折点序列
            - 1: 表示峰值 (peak)
            - -1: 表示谷值 (valley)
            - 0: 非转折点位置

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算ZigZag完整序列
        >>> zigzag_full = self.data.btind.zigzag_full(up_thresh=0.05, down_thresh=0.05)
        >>> 
        >>> # 转折点统计分析
        >>> def zigzag_statistics(self):
        >>>     zigzag = self.data.btind.zigzag_full(up_thresh=0.04, down_thresh=0.04)
        >>>     
        >>>     # 计算转折点数量
        >>>     peaks_count = (zigzag == 1).sum()
        >>>     valleys_count = (zigzag == -1).sum()
        >>>     
        >>>     return f"峰值数量: {peaks_count}, 谷值数量: {valleys_count}"
        >>> 
        >>> # 结合其他指标使用
        >>> def zigzag_with_rsi(self):
        >>>     zigzag = self.data.btind.zigzag_full(up_thresh=0.03, down_thresh=0.03)
        >>>     rsi = self.data.rsi(length=14)
        >>>     
        >>>     # 转折点处的RSI值
        >>>     if zigzag.new == 1:  # 峰值
        >>>         return f"峰值处RSI: {rsi.new:.2f}"
        >>>     elif zigzag.new == -1:  # 谷值
        >>>         return f"谷值处RSI: {rsi.new:.2f}"
        """
        ...

    @tobtind(lib='btind')
    def zigzag_modes(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> IndSeries:
        """
        ZigZag趋势模式指标 (ZigZag Trend Modes)
        ---------
        - 将ZigZag转折点转换为连续的趋势模式序列
        - 在谷值和峰值之间标记为上升趋势，在峰值和谷值之间标记为下降趋势
        - 提供平滑的趋势状态序列，便于趋势跟踪和模式识别

        计算方法:
        ---------
        >>> 基于zigzag_full的转折点序列，将趋势模式填充到每个时间点
            在(谷值, 峰值]区间内标记为1 (上升趋势)
            在(峰值, 谷值]区间内标记为-1 (下降趋势)

        参数:
        ---------
        >>> up_thresh (float): 定义峰值的最小相对涨幅阈值. 默认: 0.0
            down_thresh (float): 定义谷值的最小相对跌幅阈值. 默认: 0.0
            multiplier (float): 阈值乘数，用于调整阈值大小. 默认: 1.0

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: 趋势模式序列
            - 1: 上升趋势
            - -1: 下降趋势

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算ZigZag趋势模式
        >>> trend_mode = self.data.btind.zigzag_modes(up_thresh=0.05, down_thresh=0.05)
        >>> 
        >>> # 趋势跟踪策略
        >>> def zigzag_trend_following(self):
        >>>     trend = self.data.btind.zigzag_modes()
        >>>     
        >>>     if trend.new == 1:
        >>>         return "ZigZag显示上升趋势"
        >>>     elif trend.new == -1:
        >>>         return "ZigZag显示下降趋势"
        >>> 
        >>> # 趋势持续时间分析
        >>> def zigzag_trend_duration(self):
        >>>     trend = self.data.btind.zigzag_modes(up_thresh=0.03, down_thresh=0.03)
        >>>     
        >>>     # 计算当前趋势持续时间
        >>>     current_trend_start = np.where(np.diff(trend) != 0)[0][-1] if len(np.where(np.diff(trend) != 0)[0]) > 0 else 0
        >>>     duration = len(trend) - current_trend_start
        >>>     
        >>>     return f"当前趋势已持续 {duration} 根K线"
        """
        ...

    @tobtind(lib='btind')
    def zigzag_returns(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., limit: bool = True, **kwargs) -> IndSeries:
        """
        ZigZag分段收益率指标 (ZigZag Segment Returns)
        ---------
        - 计算ZigZag各分段（转折点到转折点）的收益率
        - 分析各趋势段的盈利能力和幅度，评估市场波动特征
        - 用于策略绩效分析、波动率评估和风险控制

        计算方法:
        ---------
        >>> 基于ZigZag转折点，计算每个趋势段的收益率
            上升段收益率 = (峰值价格 - 谷值价格) / 谷值价格
            下降段收益率 = (谷值价格 - 峰值价格) / 峰值价格
            每个时间点填充所属趋势段的收益率

        参数:
        ---------
        >>> up_thresh (float): 定义峰值的最小相对涨幅阈值. 默认: 0.0
            down_thresh (float): 定义谷值的最小相对跌幅阈值. 默认: 0.0
            multiplier (float): 阈值乘数，用于调整阈值大小. 默认: 1.0
            limit (bool): 是否限制收益率范围. 默认: True

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: 各时间点所属趋势段的收益率序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算ZigZag分段收益率
        >>> segment_returns = self.data.btind.zigzag_returns(up_thresh=0.05, down_thresh=0.05)
        >>> 
        >>> # 收益率统计分析
        >>> def zigzag_returns_analysis(self):
        >>>     returns = self.data.btind.zigzag_returns()
        >>>     
        >>>     # 计算平均收益率
        >>>     avg_return = returns[returns != 0].mean()
        >>>     max_return = returns[returns != 0].max()
        >>>     min_return = returns[returns != 0].min()
        >>>     
        >>>     return f"平均分段收益率: {avg_return:.2%}, 最大: {max_return:.2%}, 最小: {min_return:.2%}"
        >>> 
        >>> # 收益率与波动率关系
        >>> def returns_volatility_relation(self):
        >>>     segment_returns = self.data.btind.zigzag_returns()
        >>>     volatility = self.data.btind.pv(length=10)
        >>>     
        >>>     # 高收益率是否伴随高波动率
        >>>     if abs(segment_returns.new) > 0.1 and volatility.new > 0.02:
        >>>         return "高收益率伴随高波动率"
        """
        ...

    @tobtind(lines=["up", "dn", "bull", "bear", "signal"], overlap=dict(up=True,dn=True,bull=False,bear=False,signal=False), lib="btind")
    def AndeanOsc(self, length: int = 14, signal_length: int = 9, **kwargs) -> IndFrame:
        """
        安第斯振荡器指标 (Andean Oscillator)
        ---------
        - 基于在线趋势分析算法的新型技术指标
        - 分离市场的看涨和看跌成分，提供清晰的多空力量对比
        - 通过信号线识别交易机会，适用于趋势和震荡市场

        计算方法:
        ---------
        >>> 基于开盘价和收盘价计算
            up = 当close > open时的价格变化累积
            dn = 当close < open时的价格变化累积
            bull = up的移动平均
            bear = dn的移动平均
            signal = 信号线的进一步平滑

        参数:
        ---------
        >>> length (int): 主要计算周期. 默认: 14
            signal_length (int): 信号线平滑周期. 默认: 9

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndFrame: 包含五列的数据框
            - up: 上涨成分累积
            - dn: 下跌成分累积
            - bull: 看涨成分（上涨成分平滑）
            - bear: 看跌成分（下跌成分平滑）
            - signal: 信号线

        所需数据字段:
        ---------
        >>> open, close

        使用案例:
        ---------
        >>> # 计算安第斯振荡器
        >>> up, dn, bull, bear, signal = self.data.btind.AndeanOsc(length=14, signal_length=9)
        >>> 
        >>> # 多空力量对比
        >>> def andean_osc_power_comparison(self):
        >>>     ao_data = self.data.btind.AndeanOsc()
        >>>     
        >>>     # 看涨力量占优
        >>>     if ao_data.bull.new > ao_data.bear.new:
        >>>         return "看涨力量占优，多头市场"
        >>>     
        >>>     # 看跌力量占优
        >>>     elif ao_data.bull.new < ao_data.bear.new:
        >>>         return "看跌力量占优，空头市场"
        >>> 
        >>> # 信号线交易策略
        >>> def andean_osc_signal_strategy(self):
        >>>     ao_data = self.data.btind.AndeanOsc()
        >>>     
        >>>     # 看涨成分上穿信号线
        >>>     if (ao_data.bull.new > ao_data.signal.new and 
        >>>         ao_data.bull.prev <= ao_data.signal.prev):
        >>>         return "看涨成分上穿信号线，买入信号"
        >>>     
        >>>     # 看涨成分下穿信号线
        >>>     if (ao_data.bull.new < ao_data.signal.new and 
        >>>         ao_data.bull.prev >= ao_data.signal.prev):
        >>>         return "看涨成分下穿信号线，卖出信号"
        """
        ...

    @tobtind(overlap=True,lib="btind")
    def Coral_Trend_Candles(self, smooth: int = 9., mult: float = .4, **kwargs) -> IndSeries:
        """
        珊瑚趋势蜡烛指标 (Coral Trend Candles)
        ---------
        - 基于价格和波动率计算趋势强度，生成类似K线的趋势蜡烛
        - 提供清晰直观的趋势可视化，帮助识别趋势起始和结束
        - 结合移动平均和波动率调整，适应不同市场环境

        计算方法:
        ---------
        >>> 基于收盘价计算趋势强度
            结合平滑参数和乘数因子，生成趋势蜡烛数值
            正值表示上涨趋势，负值表示下跌趋势，绝对值大小表示趋势强度

        参数:
        ---------
        >>> smooth (int): 平滑参数，影响趋势的平滑程度. 默认: 9.0
            mult (float): 乘数因子，调整趋势强度的灵敏度. 默认: 0.4

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: 珊瑚趋势蜡烛序列，正负值表示趋势方向，绝对值表示强度

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算珊瑚趋势蜡烛
        >>> coral_candles = self.data.btind.Coral_Trend_Candles(smooth=9.0, mult=0.4)
        >>> 
        >>> # 趋势强度分析
        >>> def coral_trend_strength(self):
        >>>     coral = self.data.btind.Coral_Trend_Candles()
        >>>     
        >>>     if coral.new > 0:
        >>>         return f"上涨趋势，强度: {coral.new:.4f}"
        >>>     elif coral.new < 0:
        >>>         return f"下跌趋势，强度: {coral.new:.4f}"
        >>>     else:
        >>>         return "趋势中性"
        >>> 
        >>> # 趋势转换识别
        >>> def coral_trend_transition(self):
        >>>     coral = self.data.btind.Coral_Trend_Candles(smooth=9.0, mult=0.5)
        >>>     
        >>>     # 趋势由负转正
        >>>     if coral.new > 0 and coral.prev <= 0:
        >>>         return "趋势由跌转涨，关注买入机会"
        >>>     
        >>>     # 趋势由正转负
        >>>     if coral.new < 0 and coral.prev >= 0:
        >>>         return "趋势由涨转跌，注意卖出风险"
        """
        ...

    @tobtind(lines=["returns", "mean"], lib="btind")
    def signal_returns_stats(self, close=None, n: int = 1, **kwargs) -> IndFrame:
        """
        信号收益率统计指标 (Signal Returns Statistics)
        ---------
        - 基于交易信号计算后续N天的收益率，并进行统计分析
        - 评估交易信号的盈利能力和稳定性，优化信号参数
        - 提供总收益、最大收益和平均收益等多维度统计

        计算方法:
        ---------
        >>> 对每个信号点（信号为1的位置），计算未来N日的收益率
            returns = (close_future / close_signal - 1) * 100%
            统计所有信号点的收益率分布特征

        参数:
        ---------
        >>> close (IndSeries): 收盘价序列，默认使用当前数据. 默认: None
            n (int): 统计未来收益的天数. 默认: 1

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndFrame: 包含两列的数据框
            - returns: 各信号点的未来收益率
            - mean: 收益率的移动平均

        所需数据字段:
        ---------
        >>> IndSeries (需要与信号序列对齐的收盘价)

        使用案例:
        ---------
        >>> # 计算信号收益率统计
        >>> signal = (self.data.close > self.data.ema(20)).astype(int)  # 生成交易信号
        >>> returns_stats = signal.btind.signal_returns_stats(n=5)
        >>> 
        >>> # 信号绩效评估
        >>> def signal_performance_evaluation(self):
        >>>     # 生成RSI超卖信号
        >>>     rsi = self.data.rsi(length=14)
        >>>     signal = (rsi < 30).astype(int)
        >>>     
        >>>     # 计算信号未来5日收益率
        >>>     stats = signal.btind.signal_returns_stats(n=5)
        >>>     
        >>>     # 评估信号质量
        >>>     avg_return = stats.mean.new
        >>>     win_rate = (stats.returns.new > 0).mean() if len(stats.returns.new) > 0 else 0
        >>>     
        >>>     return f"信号平均收益率: {avg_return:.2%}, 胜率: {win_rate:.2%}"
        >>> 
        >>> # 多信号对比
        >>> def multiple_signals_comparison(self):
        >>>     # 定义多个信号
        >>>     signal1 = (self.data.close > self.data.ema(20)).astype(int)
        >>>     signal2 = (self.data.macd().macd > self.data.macd().macds).astype(int)
        >>>     signal3 = (self.data.rsi(14) > 50).astype(int)
        >>>     
        >>>     # 比较各信号的未来收益
        >>>     returns1 = signal1.btind.signal_returns_stats(n=3).mean.new
        >>>     returns2 = signal2.btind.signal_returns_stats(n=3).mean.new
        >>>     returns3 = signal3.btind.signal_returns_stats(n=3).mean.new
        >>>     
        >>>     return f"信号1收益: {returns1:.2%}, 信号2收益: {returns2:.2%}, 信号3收益: {returns3:.2%}"
        """
        ...

    @tobtind(lines=["up_prob", "sideways_prob", "down_prob"], lib="btind")
    def calculate_trend_probabilities(self, window_length=60,
                                      up_threshold=0.001,
                                      down_threshold=-0.001, **kwargs):
        """
        趋势概率计算指标 (Trend Probability Calculator)
        ---------
        - 基于历史价格变化统计上涨、横盘和下跌的概率
        - 提供市场状态的量化评估，帮助判断当前趋势的可持续性
        - 适用于概率交易、风险评估和市场状态识别

        计算方法:
        ---------
        >>> 在滑动窗口内统计价格变化
            上涨概率 = (变化 > up_threshold)的比例
            下跌概率 = (变化 < down_threshold)的比例
            横盘概率 = 1 - 上涨概率 - 下跌概率

        参数:
        ---------
        >>> window_length (int): 统计窗口长度. 默认: 60
            up_threshold (float): 上涨阈值，价格变化超过此值视为上涨. 默认: 0.001 (0.1%)
            down_threshold (float): 下跌阈值，价格变化低于此值视为下跌. 默认: -0.001 (-0.1%)

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndFrame: 包含三列的数据框
            - up_prob: 上涨概率 (0-1)
            - sideways_prob: 横盘概率 (0-1)
            - down_prob: 下跌概率 (0-1)

        所需数据字段:
        ---------
        >>> close (或需要计算趋势概率的价格序列)

        使用案例:
        ---------
        >>> # 计算趋势概率
        >>> up_prob, sideways_prob, down_prob = self.data.btind.calculate_trend_probabilities()
        >>> 
        >>> # 市场状态判断
        >>> def market_state_judgment(self):
        >>>     probs = self.data.btind.calculate_trend_probabilities(window_length=50)
        >>>     
        >>>     # 上涨主导市场
        >>>     if probs.up_prob.new > 0.6:
        >>>         return f"上涨概率 {probs.up_prob.new:.1%}，市场偏多"
        >>>     
        >>>     # 下跌主导市场
        >>>     elif probs.down_prob.new > 0.6:
        >>>         return f"下跌概率 {probs.down_prob.new:.1%}，市场偏空"
        >>>     
        >>>     # 横盘震荡市场
        >>>     else:
        >>>         return f"横盘概率 {probs.sideways_prob.new:.1%}，市场震荡"
        >>> 
        >>> # 趋势可持续性分析
        >>> def trend_sustainability(self):
        >>>     probs = self.data.btind.calculate_trend_probabilities()
        >>>     
        >>>     # 高上涨概率且趋势一致
        >>>     if probs.up_prob.new > 0.7 and probs.up_prob.prev > 0.6:
        >>>         return "上涨趋势强劲且持续"
        >>>     
        >>>     # 概率转换信号
        >>>     if probs.up_prob.new > probs.down_prob.new and probs.up_prob.prev <= probs.down_prob.prev:
        >>>         return "上涨概率超过下跌概率，趋势可能转换"
        """
        ...

    @tobtind(lines=["gap_ratio", "upper_hl", "lower_hl", "signal"], overlap=dict(gap_ratio=False, upper_hl=True, lower_hl=True, signal=False), lib="btind")
    def gap_ratio(self, length=20, **kwargs) -> IndFrame:
        """
        通道差异系数指标 (Gap Ratio)
        ---------
        - 计算价格在通道内的相对位置，衡量价格与通道上下轨的差异程度
        - 识别价格突破、通道挤压和趋势强度变化
        - 提供上下轨参考线和交易信号，适用于通道突破策略

        计算方法:
        ---------
        >>> 基于一定周期内的最高价和最低价构建通道
            upper_hl = 最高价的移动平均或通道上轨
            lower_hl = 最低价的移动平均或通道下轨
            gap_ratio = (close - lower_hl) / (upper_hl - lower_hl) * 100%
            signal = 基于gap_ratio的交易信号

        参数:
        ---------
        >>> length (int): 计算通道的周期长度. 默认: 20

        可选参数:
        ---------
        >>> **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndFrame: 包含四列的数据框
            - gap_ratio: 通道差异系数 (0-100%)
            - upper_hl: 通道上轨线
            - lower_hl: 通道下轨线
            - signal: 交易信号

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算通道差异系数
        >>> gap_ratio, upper_band, lower_band, signal = self.data.btind.gap_ratio(length=20)
        >>> 
        >>> # 通道突破策略
        >>> def gap_ratio_breakout_strategy(self):
        >>>     gap_data = self.data.btind.gap_ratio()
        >>>     
        >>>     # 价格突破上轨
        >>>     if self.data.close.new > gap_data.upper_hl.new:
        >>>         return "价格突破上轨，强势买入信号"
        >>>     
        >>>     # 价格跌破下轨
        >>>     if self.data.close.new < gap_data.lower_hl.new:
        >>>         return "价格跌破下轨，弱势卖出信号"
        >>> 
        >>> # 通道挤压识别
        >>> def channel_squeeze_detection(self):
        >>>     gap_data = self.data.btind.gap_ratio(length=20)
        >>>     
        >>>     # 通道宽度收缩
        >>>     channel_width = gap_data.upper_hl.new - gap_data.lower_hl.new
        >>>     channel_width_ma = (gap_data.upper_hl - gap_data.lower_hl).ema(period=10).new
        >>>     
        >>>     if channel_width < channel_width_ma * 0.7:
        >>>         return "通道挤压，可能即将突破"
        >>> 
        >>> # 相对位置分析
        >>> def gap_ratio_position_analysis(self):
        >>>     gap_data = self.data.btind.gap_ratio()
        >>>     
        >>>     if gap_data.gap_ratio.new > 80:
        >>>         return "价格接近通道上轨，注意回调风险"
        >>>     elif gap_data.gap_ratio.new < 20:
        >>>         return "价格接近通道下轨，关注反弹机会"
        >>>     else:
        >>>         return f"价格在通道中部，相对位置: {gap_data.gap_ratio.new:.1f}%"
        """
        ...

    @tobtind(lines=["vwap_window"], overlap=dict(vwap_window=True), lib="btind")
    def vwap_window(self, window=20, offset=None, **kwargs):
        """
        滚动窗口VWAP指标 (Rolling Window VWAP)
        ---------
        - 基于固定时间窗口计算成交量加权平均价格，不随时间周期重置
        - 提供动态的近期平均成本参考，反映特定周期内的市场平均成交价格
        - 适合短期交易策略，对近期价格变化更敏感，避免长期累积偏差

        计算方法:
        ---------
        >>> 计算典型价格：tp = (high + low + close) / 3
            计算加权价格：wp = tp * volume
            滚动窗口累加：rolling_wp_sum = wp.rolling(window).sum()
            滚动成交量累加：rolling_volume_sum = volume.rolling(window).sum()
            滚动VWAP = rolling_wp_sum / rolling_volume_sum

        参数:
        ---------
        >>> window (int): 滚动窗口周期长度. 默认: 20
            较小窗口(5-10): 对近期价格更敏感，适合短线交易
            中等窗口(20-50): 平衡敏感度和稳定性，适合日内交易
            较大窗口(100-200): 更平滑的趋势参考，适合长线分析

        可选参数:
        ---------
        >>> offset (int): 结果偏移周期数. 默认: 0
            **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: 滚动窗口VWAP序列

        所需数据字段:
        ---------
        >>> high, low, close, volume

        使用案例:
        ---------
        >>> # 计算20周期滚动VWAP
        >>> vwap_20 = self.data.btind.vwap_window(window=20)
        >>> 
        >>> # 价格突破策略
        >>> def vwap_window_breakout_strategy(self):
        >>>     vwap_20 = self.data.btind.vwap_window(window=20)
        >>>     vwap_50 = self.data.btind.vwap_window(window=50)
        >>>     
        >>>     # 短期VWAP上穿长期VWAP
        >>>     if vwap_20.new > vwap_50.new and vwap_20.prev < vwap_50.prev:
        >>>         return "VWAP金叉，短期趋势转强"
        >>>     
        >>>     # 价格突破VWAP
        >>>     if self.data.close.new > vwap_20.new and self.data.close.prev < vwap_20.prev:
        >>>         return "价格突破VWAP，买入信号"
        >>> 
        >>> # VWAP支撑阻力策略
        >>> def vwap_support_resistance_strategy(self):
        >>>     vwap_short = self.data.btind.vwap_window(window=10)
        >>>     vwap_medium = self.data.btind.vwap_window(window=30)
        >>>     
        >>>     # 判断支撑和阻力
        >>>     if self.data.close.new > vwap_short.new and self.data.close.new > vwap_medium.new:
        >>>         return "价格在VWAP上方，VWAP构成支撑"
        >>>     elif self.data.close.new < vwap_short.new and self.data.close.new < vwap_medium.new:
        >>>         return "价格在VWAP下方，VWAP构成阻力"
        >>> 
        >>> # 多时间框架VWAP分析
        >>> def multi_timeframe_vwap_analysis(self):
        >>>     # 不同窗口的VWAP形成支撑阻力带
        >>>     vwap_fast = self.data.btind.vwap_window(window=5)
        >>>     vwap_slow = self.data.btind.vwap_window(window=20)
        >>>     
        >>>     # 计算VWAP通道宽度
        >>>     vwap_channel_width = abs(vwap_fast.new - vwap_slow.new) / vwap_slow.new * 100
        >>>     
        >>>     if vwap_channel_width < 0.5:
        >>>         return f"VWAP通道收敛({vwap_channel_width:.2f}%)，市场即将选择方向"
        >>>     else:
        >>>         return f"VWAP通道扩张({vwap_channel_width:.2f}%)，趋势延续"
        """
        ...

    @tobtind(lines=["vwap_volume"], overlap=dict(vwap_volume=True), lib="btind")
    def vwap_volume_based(self, volume_quantile=0.25, lookback=100, offset=None, **kwargs):
        """
        成交量分位数VWAP指标 (Volume Quantile VWAP)
        ---------
        - 基于成交量分位数动态调整重置点，实现自适应成交量加权平均价格计算
        - 根据市场实际成交量分布确定VWAP重置阈值，避免固定时间周期重置的局限性
        - 反映按成交量累积的成本分布，更适合成交量分析和高波动市场环境

        计算方法:
        ---------
        >>> 计算典型价格：tp = (high + low + close) / 3
            计算加权价格：wp = tp * volume
            计算动态成交量阈值：基于lookback周期内的volume_quantile分位数
            创建成交量分组：按累计成交量达到阈值时重置分组
            分组内累计计算：vwap = wp.groupby(groups).cumsum() / volume.groupby(groups).cumsum()

        参数:
        ---------
        >>> volume_quantile (float): 成交量分位数阈值 (0-1). 默认: 0.25
            0.10-0.20: 高重置频率，对价格变化敏感，适合高频交易
            0.25-0.35: 中等重置频率，平衡敏感度和稳定性，适合日内交易
            0.40-0.50: 低重置频率，更平滑，适合趋势跟踪
            0.60-0.75: 极低重置频率，适合长期持仓分析
        >>> lookback (int): 计算分位数的回看周期. 默认: 100
            较短周期: 更快适应成交量变化，适合市场结构变化快的环境
            较长周期: 更稳定的阈值，适合趋势明显的市场

        可选参数:
        ---------
        >>> offset (int): 结果偏移周期数. 默认: 0
            **kwargs: 传递给指标计算的其他参数

        返回:
        ---------
        >>> IndSeries: 成交量分位数VWAP序列

        所需数据字段:
        ---------
        >>> high, low, close, volume

        使用案例:
        ---------
        >>> # 计算25%成交量分位数VWAP
        >>> vwap_vol = self.data.btind.vwap_volume_based(volume_quantile=0.25, lookback=100)
        >>> 
        >>> # 成交量驱动策略
        >>> def volume_driven_vwap_strategy(self):
        >>>     vwap_q25 = self.data.btind.vwap_volume_based(volume_quantile=0.25)
        >>>     vwap_q50 = self.data.btind.vwap_volume_based(volume_quantile=0.5)
        >>>     
        >>>     # 小成交量分位VWAP上穿大成交量分位VWAP
        >>>     if vwap_q25.new > vwap_q50.new and vwap_q25.prev < vwap_q50.prev:
        >>>         return "成交量密集区VWAP突破，资金流入信号"
        >>>     
        >>>     # 价格与成交量VWAP的关系
        >>>     if self.data.close.new > vwap_q25.new and self.data.volume.new > self.data.volume.ema(20).new:
        >>>         return "放量突破成交量VWAP，强势确认"
        >>> 
        >>> # 市场结构分析
        >>> def market_structure_analysis(self):
        >>>     # 使用不同分位数VWAP分析市场结构
        >>>     vwap_q20 = self.data.btind.vwap_volume_based(volume_quantile=0.2)
        >>>     vwap_q40 = self.data.btind.vwap_volume_based(volume_quantile=0.4)
        >>>     
        >>>     # 计算VWAP间距
        >>>     vwap_spread = vwap_q40.new - vwap_q20.new
        >>>     avg_spread = (vwap_q40 - vwap_q20).ema(period=20).new
        >>>     
        >>>     if vwap_spread > avg_spread * 1.5:
        >>>         return f"VWAP间距扩大({vwap_spread:.2f})，市场分歧加大"
        >>>     elif vwap_spread < avg_spread * 0.5:
        >>>         return f"VWAP间距收敛({vwap_spread:.2f})，市场趋于一致"
        >>> 
        >>> # 成交量分位数自适应调整
        >>> def adaptive_quantile_strategy(self):
        >>>     # 根据市场波动率动态调整分位数
        >>>     volatility = self.data.close.rolling(20).std() / self.data.close.rolling(20).mean()
        >>>     
        >>>     if volatility.new > 0.02:
        >>>         # 高波动市场，使用更高分位数
        >>>         vwap = self.data.btind.vwap_volume_based(volume_quantile=0.4, lookback=50)
        >>>         return f"高波动市场，使用40%分位数VWAP: {vwap.new:.2f}"
        >>>     else:
        >>>         # 低波动市场，使用标准分位数
        >>>         vwap = self.data.btind.vwap_volume_based(volume_quantile=0.25, lookback=100)
        >>>         return f"低波动市场，使用25%分位数VWAP: {vwap.new:.2f}"
        >>> 
        >>> # 成交量异常检测
        >>> def volume_anomaly_detection(self):
        >>>     vwap_vol = self.data.btind.vwap_volume_based(volume_quantile=0.3)
        >>>     
        >>>     # 价格偏离VWAP但成交量正常
        >>>     price_deviation = abs(self.data.close.new - vwap_vol.new) / vwap_vol.new * 100
        >>>     volume_ratio = self.data.volume.new / self.data.volume.ema(20).new
        >>>     
        >>>     if price_deviation > 2 and volume_ratio < 0.8:
        >>>         return f"价格偏离VWAP{price_deviation:.1f}%但缩量，警惕假突破"
        >>>     elif price_deviation < 1 and volume_ratio > 1.5:
        >>>         return f"价格接近VWAP但放量，关注方向选择"
        """
        ...

    @tobtind(lines=["gaussian"], overlap=dict(gaussian=True), lib="btind")
    def gaussian_smoothed(self, window:int=11,**kwargs)->IndSeries:
        """
        高斯核卷积平滑 (Gaussian Smoothed)
        ---------
        - 基于高斯分布（正态分布）核的加权卷积平滑算法，如同给价格曲线\"磨皮\"
        - 中间权重最大、两边权重逐渐减小，比简单移动平均更自然，不会产生相位偏移
        - 在保留趋势特征的前提下有效过滤短期价格波动噪音，让趋势更加清晰可见

        计算方法:
        ---------
        >>> sigma = window / 6.0
            half = window // 2
            x = np.arange(-half, half + 1)
            kernel = np.exp(-x ** 2 / (2 * sigma ** 2))
            kernel = kernel / kernel.sum()
            # 边界用边缘值填充，卷积后对齐长度
            smoothed = np.convolve(padded, kernel, mode='valid')

        参数:
        ---------
        >>> window (int): 高斯平滑窗口大小，控制平滑程度. 默认: 11
            5-7: 轻度平滑，保留更多细节，适合短线交易
            9-13: 中度平滑，平衡趋势与细节，适合日内交易
            15-21: 深度平滑，趋势更清晰但滞后更大，适合中长线趋势跟踪

        可选参数:
        ---------
        >>> **kwargs: 传递给@tobtind装饰器的其他参数

        返回:
        ---------
        >>> IndSeries: 高斯平滑后的价格序列，长度与原始数据一致

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算高斯平滑价格
        >>> gaussian = self.data.btind.gaussian_smoothed(window=11)
        >>>
        >>> # 平滑价格趋势策略
        >>> gaussian = self.data.btind.gaussian_smoothed(window=11)
        >>> signal = self.data.close.cross_up(gaussian)
        >>>
        >>> # 多窗口平滑对比
        >>> gauss_fast = self.data.btind.gaussian_smoothed(window=7)
        >>> gauss_slow = self.data.btind.gaussian_smoothed(window=21)
        >>>
        >>> # 快线上穿慢线
        >>> gauss_fast.cross_up(gauss_slow)
        >>> # 快线下穿慢线
        >>> gauss_fast.cross_down(gauss_slow)
        """
        ...
    
    @tobtind(overlap=True, lib="btind")
    def varma(self, length: int=10, var_length: int=10,**kwargs) -> IndSeries:
        """VAR (Variable Moving Average) — 基于 CMO 的自适应 EMA"""
        ...
    
    @tobtind(overlap=True, lib="btind")
    def rsswma(self, length: int=10,**kwargs) -> IndSeries:
        """RSS_WMA (Lazy Line) — 三重级联 WMA 平滑器"""
        ...
        
    @tobtind(overlap=True, lib="btind")
    def pivot_high(self, left: int=5, right: int=5,**kwargs) -> IndSeries:
        """Pivot High"""
        ...
    
    @tobtind(overlap=True, lib="btind")
    def pivot_low(self, left: int=5, right: int=5,**kwargs) -> IndSeries:
        """Pivot Low"""
        ...
        
    @tobtind(overlap=False, lib="btind")
    def linreg_slope(self, length: int=10,**kwargs) -> IndSeries:
        """Linreg Slope"""
        ...
        
    @tobtind(overlap=False, lib="btind")
    def vwap_high(self, length: int=10,**kwargs) -> IndSeries:
        """VWAP High"""
        ...
        
    @tobtind(overlap=False, lib="btind")
    def vwap_low(self, length: int=10,**kwargs) -> IndSeries:
        """VWAP Low"""
        ...
    
    
    NON_KLINE_INDICATORS = ["signal_returns_stats","rngfilt"]
