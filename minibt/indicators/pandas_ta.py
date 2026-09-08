from __future__ import annotations

from .base import tobtind
from ..utils import TYPE_CHECKING, np, LineStyle, LineDash

if TYPE_CHECKING:
    from typing_ import *
    from .core import *

class PandasTa:
    """## pandas_ta指标指引
    - pandas_ta 指标适配类，用于将 pandas_ta 库中的技术指标计算结果转换为框架内置的指标数据类型（IndSeries/IndFrame）

    ### 📘 **API文档参考**:
    - https://www.minibt.cn/minibt_api_reference/pandasta/

    ### 核心功能：
    - 封装 pandas_ta 库的各类技术指标，提供统一的调用接口
    - 通过 @tobtind 装饰器自动处理指标参数校验、计算逻辑调用和返回值转换，确保输出为框架兼容的 IndSeries 或 IndFrame
    - 支持多维度技术分析场景，覆盖蜡烛图形态、趋势跟踪、动量判断、波动率计算等量化交易核心需求
    - 内置指标分类体系，便于按业务场景快速定位和调用目标指标

    ### 指标分类与包含列表：
    该类支持的指标按功能划分为以下 9 大类，具体包含指标如下：

    **1. 蜡烛图分析（Candles）**
       - 功能：蜡烛图形态识别、特殊蜡烛图转换（如布林带K线、Z评分标准化蜡烛图）
       - 包含指标：cdl_pattern（蜡烛图形态识别）、cdl_z（Z评分标准化蜡烛图）、ha（Heikin-Ashi布林带K线）

    **2. 周期分析（Cycles）**
       - 功能：识别市场价格的周期性规律，辅助判断趋势转折节点
       - 包含指标：ebsw（周期检测指标）

    **3. 动量指标（Momentum）**
       - 功能：衡量价格变化的速度和力度，判断趋势强度与潜在反转
       - 包含指标：ao、apo、bias、bop、brar、cci、cfo、cg、cmo、coppock、cti、er、eri、fisher、
       - inertia、kdj、kst、macd、mom、pgo、ppo、psl、pvo、qqe、roc、rsi、rsx、rvgi、slope、smi、
       - squeeze、squeeze_pro、stc、stoch、stochrsi、td_seq、trix、tsi、uo、willr

    **4. 重叠指标（Overlap）**
       - 功能：通过价格平滑、均线拟合等方式，凸显价格趋势方向
       - 包含指标：alma、dema、ema、fwma、hilo、hl2、hlc3、hma、ichimoku、jma、kama、linreg、
       - mcgd、midpoint、midprice、ohlc4、pwma、rma、sinwma、sma、ssf、supertrend、swma、t3、
       - tema、trima、vidya、vwap、vwma、wcp、wma、zlma

    **5. 收益指标（Performance）**
       - 功能：计算资产的收益情况，量化投资回报表现
       - 包含指标：log_return（对数收益）、percent_return（百分比收益）

    **6. 统计指标（Statistics）**
       - 功能：基于统计方法分析价格分布特征、离散程度等
       - 包含指标：entropy（熵值）、kurtosis（峰度）、mad（平均绝对偏差）、median（中位数）、
       - quantile（分位数）、skew（偏度）、stdev（标准差）、tos_stdevall（全维度标准差）、
       - variance（方差）、zscore（Z评分）

    **7. 趋势指标（Trend）**
       - 功能：识别和确认价格趋势方向、强度及持续时间
       - 包含指标：adx、amat、aroon、chop、cksp、decay、decreasing（下跌趋势）、dpo、
       - increasing（上涨趋势）、long_run（长期趋势）、psar、qstick、short_run（短期趋势）、
       - tsignals（趋势信号）、ttm_trend、vhf、vortex、xsignals（扩展趋势信号）

    **8. 波动率指标（Volatility）**
       - 功能：衡量价格波动的剧烈程度，评估市场风险
       - 包含指标：aberration、accbands、atr（平均真实波幅）、bbands（布林带）、
       - donchian（唐奇安通道）、hwc、kc（肯特纳通道）、massi、natr（归一化平均真实波幅）、
       - pdist、rvi、thermo、true_range（真实波幅）、ui

    **9. 成交量指标（Volume）**
       - 功能：结合成交量数据分析资金流向，辅助判断价格走势的有效性
       - 包含指标：ad（积累/派发指标）、adosc（震荡指标）、aobv（绝对OBV）、cmf（资金流向指数）、
       - efi（资金效率指标）、eom（资金流动指数）、kvo（成交量震荡指标）、mfi（资金流量指标）、
       - nvi（负成交量指数）、obv（能量潮指标）、pvi（正成交量指数）、pvol（价格成交量指标）、
       - pvr（价格成交量比率）、pvt（价格成交量趋势）


    ### 使用说明：
    1. 初始化：
    - 传入框架支持的 IndSeries 或 IndFrame 数据对象（需包含指标计算所需的基础字段，如 open、high、low、close、volume 等）
    >>> data = IndFrame(...)  # 框架内置数据对象（含OHLCV等基础字段）
    >>> ta = PandasTa(data)

    2. 指标调用：
    - 直接调用对应指标方法，传入必要参数（默认参数已适配常见场景，可按需调整）
    >>> #示例1：识别十字星蜡烛图形态
    >>> #返回框架内置IndFrame，含十字星形态识别结果
    >>> doji_result = self.data.cdl_pattern(name="doji")
    >>> #示例2：计算Heikin-Ashi布林带K线
    >>> ha_candles = self.data.ha()  # 返回框架内置IndFrame，含HA蜡烛图的open、high、low、close字段
    >>> #示例3：计算14期RSI动量指标
    >>> rsi_14 = self.data.close.rsi(length=14)  # 返回框架内置IndSeries，含14期RSI值

    3. 返回值特性：
    - 所有方法返回框架内置的 IndSeries 或 IndFrame 类型，可直接用于后续策略逻辑（如信号生成、风险控制），无需额外类型转换


    ### 注意事项：
    - 部分指标需特定基础字段（如成交量指标需 volume 字段），调用前确保输入数据包含所需字段
    - 指标参数（如 length 周期）可通过方法参数调整，未指定时使用 pandas_ta 默认值
    - 可通过 @tobtind 装饰器的 kwargs 参数配置填充缺失值（fillna）、数据偏移（offset）等辅助功能
    """
    _perfixes: str = "pta_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @tobtind(lines=None, lib='pta')
    def cum(self, length=10, **kwargs) -> IndSeries:
        """
        滚动累积和 (Rolling Cumulative Sum)
        ---------
            计算指定长度的滚动窗口内的累积和。

        计算方法:
        ---------
            >>> pd.Series.rolling(length).sum()

        参数:
        ---------
        >>> length (int): 滚动窗口长度. 默认: 10
            **kwargs: 其他参数

        返回:
        ---------
        >>> IndSeries: 滚动累积和序列

        使用案例:
        ---------
        >>> # 计算价格的滚动累积和
        >>> cum_sum = self.data.cum(length=10)
        >>>
        >>> # 计算成交量的滚动累积和
        >>> volume_cum = self.data.volume.cum(length=20)
        >>>
        >>> def cumulative_analysis(self):
        >>>     # 使用10周期累积和分析价格强度
        >>>     price_cum = self.data.close.cum(length=10)
        >>>     if price_cum.new > price_cum.prev:
        >>>         return "10周期价格累积和上升，显示买盘强劲"
        >>>
        >>> # 多周期累积和比较
        >>> def multi_period_cumulative(self):
        >>>     short_cum = self.data.close.cum(length=5)   # 短期累积和
        >>>     long_cum = self.data.close.cum(length=20)   # 长期累积和
        >>>
        >>>     # 短期累积和超过长期累积和
        >>>     if short_cum.new > long_cum.new:
        >>>         return "短期买盘力量超过长期"
        >>>
        >>> # 结合价格位置分析
        >>> def cumulative_with_price_level(self):
        >>>     cum_10 = self.data.close.cum(length=10)
        >>>     # 累积和在高位且价格创新高
        >>>     if (cum_10.new > cum_10.ema(period=20).new and
        >>>         self.data.close.new > self.data.close.rolling(20).max()):
        >>>         return "累积和强劲且价格突破，趋势延续"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def ZeroDivision(self, b=1., dtype=np.float64, fill_value=0.0, handle_inf=True, **kwargs) -> IndFrame | IndSeries:
        """
        ## 零除：安全除法保护 (Safe Division Protection)
        ---------
            处理除法运算中的异常情况，包括除零和无穷大问题。
            当分母为零或无效时，使用指定的默认值替代，确保计算的稳定性。

        参数:
        ---------
        - b (float | array-like): 除数，可以是标量或与当前序列相同形状的序列。默认: 1.0
        - dtype (type): 输出数据的数值类型。默认: np.float64
        - fill_value (float): 当除数为零或无效时返回的填充值。默认: 0.0
        - handle_inf (bool): 是否处理无穷大情况。当为True时，分母为无穷大也会被视为无效。
                          默认: True
        - **kwargs: 其他传递给底层计算函数的参数

        返回:
        ---------
        IndFrame  | IndSeries: 经过安全除法保护处理后的序列或数据框

        使用案例:
        ---------
        >>> # 基础除法保护
        >>> result = self.data.close.ZeroDivision(b=0, fill_value=1.0)
        >>>
        >>> # 价格变动率计算保护
        >>> price_change = self.data.close.diff()
        >>> protected_ratio = price_change.ZeroDivision(fill_value=0) / self.data.close.prev
        >>>
        >>> # 保护技术指标计算，使用中性值填充
        >>> def protected_rsi(self):
        >>>     rsi = self.data.rsi(length=14)
        >>>     # 零除或无效值时返回中性值50
        >>>     protected_rsi = rsi.ZeroDivision(b=50, fill_value=50)
        >>>     return protected_rsi
        >>>
        >>> # 成交量比率计算保护，避免零除和无穷大
        >>> def volume_ratio_protection(self):
        >>>     volume_ma = self.data.volume.ema(period=20)
        >>>     # 同时保护分子和分母，使用1作为默认值
        >>>     volume_ratio = self.data.volume.ZeroDivision(b=1, fill_value=1) / volume_ma.ZeroDivision(b=1, fill_value=1)
        >>>     return volume_ratio
        >>>
        >>> # 高级保护：处理无穷大情况
        >>> def advanced_protection(self):
        >>>     # 在可能产生无穷大的计算中使用handle_inf=True
        >>>     sensitive_calc = self.data.roc(period=10)
        >>>     protected_calc = sensitive_calc.ZeroDivision(
        >>>         b=0,
        >>>         fill_value=np.nan,
        >>>         handle_inf=True
        >>>     )
        >>>     return protected_calc
        >>>
        >>> # 多指标组合计算保护
        >>> def multi_indicator_protection(self):
        >>>     bb = self.data.bbands(length=20)
        >>>     kc = self.data.kc(length=20)
        >>>
        >>>     # 保护布林带百分比计算
        >>>     bb_percent_protected = bb.bb_percent.ZeroDivision(b=0.5, fill_value=0.5)
        >>>
        >>>     # 保护通道宽度比率计算
        >>>     bb_width = bb.bb_upper - bb.bb_lower
        >>>     kc_width = kc.kc_upper - kc.kc_lower
        >>>     width_ratio = bb_width.ZeroDivision(b=1, fill_value=1) / kc_width.ZeroDivision(b=1, fill_value=1)
        >>>
        >>>     return width_ratio
        >>>
        >>> # 条件性保护策略
        >>> def conditional_protection(self):
        >>>     rsi = self.data.rsi(length=14)
        >>>
        >>>     # 根据不同技术状态使用不同的保护策略
        >>>     if rsi.new > 70:
        >>>         # 超买区域使用保守值
        >>>         protected_value = rsi.ZeroDivision(b=80, fill_value=80)
        >>>     elif rsi.new < 30:
        >>>         # 超卖区域使用积极值
        >>>         protected_value = rsi.ZeroDivision(b=20, fill_value=20)
        >>>     else:
        >>>         # 中性区域使用平衡值
        >>>         protected_value = rsi.ZeroDivision(b=50, fill_value=50)
        >>>
        >>>     return protected_value
        >>>
        >>> # 自定义数据类型保护
        >>> def custom_dtype_protection(self):
        >>>     # 使用单精度浮点数以节省内存
        >>>     protected_values = self.data.volume.ZeroDivision(
        >>>         b=0,
        >>>         fill_value=0,
        >>>         dtype=np.float32
        >>>     )
        >>>     return protected_values
        """
        ...

    @tobtind(lines=None, lib='pta')
    def __strategy(self, *args, **kwargs):
        """不可用"""
        ...

    # Public DataFrame Methods: Indicators and Utilities
    # Candles
    @tobtind(lib='pta')
    def cdl_pattern(self, name="2crows", scalar=None, offset=0, **kwargs) -> IndFrame:
        """
        蜡烛图形态识别 (Candle Pattern)
        ---------
            所有蜡烛图形态的包装函数。

        注意:
        ---------
            不可用于replay

        参数:
        ---------
        >>> name: (str | Sequence[str]): 形态名称
        ["2crows", "3blackcrows", "3inside", "3linestrike", "3outside", "3starsinsouth",
        "3whitesoldiers", "abandonedbaby", "advanceblock", "belthold", "breakaway",
        "closingmarubozu", "concealbabyswall", "counterattack", "darkcloudcover", "doji",
        "dojistar", "dragonflydoji", "engulfing", "eveningdojistar", "eveningstar",
        "gapsidesidewhite", "gravestonedoji", "hammer", "hangingman", "harami",
        "haramicross", "highwave", "hikkake", "hikkakemod", "homingpigeon",
        "identical3crows", "inneck", "inside", "invertedhammer", "kicking", "kickingbylength",
        "ladderbottom", "longleggeddoji", "longline", "marubozu", "matchinglow", "mathold",
        "morningdojistar", "morningstar", "onneck", "piercing", "rickshawman",
        "risefall3methods", "separatinglines", "shootingstar", "shortline", "spinningtop",
        "stalledpattern", "sticksandwich", "takuri", "tasukigap", "thrusting", "tristar",
        "unique3river", "upsidegap2crows", "xsidegap3methods"]
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含多个蜡烛图形态列的数据框

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 获取所有蜡烛图形态
        >>> patterns = self.data.cdl_pattern(name="all")
        >>>
        >>> # 在策略中使用特定形态
        >>> def check_bullish_patterns(self):
        >>>     patterns = self.data.cdl_pattern(name=["hammer", "piercing", "morningstar"])
        >>>     # 检查最近的锤子线形态
        >>>     if patterns.cdl_hammer.new== 100:
        >>>         self.data.buy()
        >>>
        >>> # 结合多个形态确认信号
        >>> def confirm_reversal(self):
        >>>     patterns = self.data.cdl_pattern(name=["doji", "engulfing", "hammer"])
        >>>     current = patterns.new
        >>>     prev = patterns.prev
        >>>
        >>>     # 当出现多个看涨形态时确认反转
        >>>     bullish_signals = 0
        >>>     if current.cdl_doji.new == 100: bullish_signals += 1
        >>>     if current.cdl_engulfing.new == 100: bullish_signals += 1
        >>>     if current.cdl_hammer.new == 100: bullish_signals += 1
        >>>
        >>>     if bullish_signals >= 2:
        >>>         return "强烈看涨信号"
        """
        ...

    @tobtind(lines=['open', 'high', 'low', 'close'], overlap=False, category="candles", lib='pta')
    def cdl_z(self, length=30, full=False, ddof=1, offset=None, **kwargs) -> IndFrame:
        """
        Z分数标准化蜡烛图 (Candle Type: Z)
        ---------
            使用滚动Z分数标准化OHLC蜡烛图。

        数据来源:
        ---------
            Kevin Johnson

        计算方法:
        ---------
            >>> length=30, full=False, ddof=1
            Z = ZSCORE
            open  = Z( open, length, ddof)
            high  = Z( high, length, ddof)
            low   = Z(  low, length, ddof)
            close = Z(close, length, ddof)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            full (bool): 默认：False
            ddof (int): 乘数. 默认：1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> naive (bool, 可选): 如果为True，在长度小于其高低范围百分比时预填充潜在的Doji
                默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含open, high, low, close列的数据框

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算Z分数标准化蜡烛图
        >>> z_candles = self.data.cdl_z()
        >>> open_z, high_z, low_z, close_z = z_candles
        >>>
        >>> # 使用Z分数检测异常价格行为
        >>> def detect_anomalies(self):
        >>>     z_candles = self.data.cdl_z(length=20)
        >>>     # 当Z分数超过2个标准差时发出信号
        >>>     if abs(z_candles.close_z.new) > 2:
        >>>         if z_candles.close_z.new > 0:
        >>>             self.data.sell()  # 价格异常偏高
        >>>         else:
        >>>             self.data.buy()   # 价格异常偏低
        """
        ...

    @tobtind(lines=['open', 'high', 'low', 'close'], overlap=False, category="candles", lib='pta')
    def ha(self, length=0, offset=0, **kwargs) -> IndFrame:
        """
        平均K线图 (Heikin Ashi Candles)
        ---------

        - 平均K线图技术通过平均价格数据来创建日本蜡烛图，过滤市场噪音。
        - 平均K线图由本间宗久在18世纪开发，与标准蜡烛图有一些共同特征，
        - 但在创建每根蜡烛时使用的值不同。与使用开盘价、最高价、最低价
        - 和收盘价的标准蜡烛图不同，平均K线图技术使用基于两周期平均值的
        - 修正公式。这使得图表外观更平滑，更容易发现趋势和反转，
        - 但也会掩盖缺口和一些价格数据。

        数据来源:
        ---------
            https://www.investopedia.com/terms/h/heikinashi.asp

        计算方法:
        ---------
        >>> HA_OPEN[0] = (open[0] + close[0]) / 2
            HA_CLOSE = (open[0] + high[0] + low[0] + close[0]) / 4
            for i > 1 in df.index:
                HA_OPEN = (HA_OPEN[i−1] + HA_CLOSE[i−1]) / 2
            HA_HIGH = MAX(HA_OPEN, HA_HIGH, HA_CLOSE)
            HA_LOW = MIN(HA_OPEN, HA_LOW, HA_CLOSE)

        - 使用一个周期创建第一个平均K线蜡烛，使用上述公式。
        - 例如，使用最高价、最低价、开盘价和收盘价创建第一个HA收盘价。
        - 使用开盘价和收盘价创建第一个HA开盘价。
        - 该周期的最高价将是第一个HA最高价，最低价将是第一个HA最低价。
        - 计算出第一个HA后，现在可以根据公式继续计算后续的HA蜡烛。

        参数:
        ---------
        >>> length (int): 周期. 默认: 0
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含open, high, low, close列的数据框

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 基础使用方法
        >>> self.ha = self.data.ha()
        >>> open, high, low, close = self.ha
        >>> # 在策略中作为趋势过滤条件
        >>> # 当HA收盘价连续上涨时考虑买入
        >>> if self.ha.close.new > self.ha.close.prev > self.ha.close.sndprev:
        >>>     self.data.buy()
        """
        ...

    @tobtind(lines=['open', 'high', 'low', 'close'], overlap=False, category="candles", lib='pta')
    def lrc(self, length=11, **kwargs) -> IndFrame:
        """
        线性回归蜡烛图 (Linear Regression Candles)
        ---------
            利用线性回归增强交易中的图表解读能力。
            在交易世界中，准确解读图表对于做出明智决策至关重要。在众多可用的工具和技术中，
            线性回归因其简单性和有效性而脱颖而出。

            线性回归是一种基本的统计方法，通过将直线拟合到数据点来帮助交易者识别证券价格的潜在趋势。
            这条线称为回归线，代表了未来价格走势的最佳估计，更清晰地展示了趋势的方向、强度和波动性。

            通过减少价格数据中的噪音，线性回归使趋势和反转更容易被发现，为技术分析和交易策略开发提供了坚实的基础。

        参数:
        ---------
        >>> length (int, 可选): 回归周期长度. 默认: 11

        返回:
        ---------
        >>> IndFrame: 包含线性回归计算后的OHLC数据

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算线性回归蜡烛图
        >>> lrc_data = self.data.lrc(length=14)
        >>>
        >>> # 使用线性回归蜡烛图判断趋势方向
        >>> def trend_direction(self):
        >>>     lrc_data = self.data.lrc(length=14)
        >>>     # 当线性回归收盘价连续上涨时确认上升趋势
        >>>     if lrc_data.close.new > lrc_data.close.prev > lrc_data.close.sndprev:
        >>>         return "上升趋势"
        >>>     elif lrc_data.close.new < lrc_data.close.prev < lrc_data.close.sndprev:
        >>>         return "下降趋势"
        >>>     else:
        >>>         return "震荡趋势"
        >>>
        >>> # 结合线性回归蜡烛图与其他指标
        >>> def enhanced_trend_strategy(self):
        >>>     lrc_data = self.data.lrc(length=14)
        >>>     # 当线性回归收盘价上穿其移动平均线时买入
        >>>     lrc_ma = lrc_data.close.ema(period=5)
        >>>     if lrc_data.close.new > lrc_ma.new and lrc_data.close.prev <= lrc_ma.prev:
        >>>         self.data.buy()
        """
        ...

    @tobtind(lines=None, lib='pta')
    def ebsw(self, length=40, bars=10, offset=0, **kwargs) -> IndSeries:
        """
        更优正弦波 (Even Better SineWave, EBSW) *测试版*
        ---------
        - 用于测量市场周期并使用低通滤波器去除噪音。
        - 输出信号限制在-1到1之间，检测到的趋势最大长度受周期参数限制。

        数据来源:
        ---------
        - https://www.prorealcode.com/prorealtime-indicators/even-better-sinewave/
        - J.F.Ehlers 'Cycle Analytics for Traders', 2014

        计算方法:
        ---------
            refer to 'sources' or implementation

        参数:
        ---------
        >>> length (int): 最大周期/趋势周期。值在40-48之间效果最佳，最小值: 39. 默认: 40
            bars (int): 低通滤波周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算更优正弦波
        >>> ebsw_signal = self.data.ebsw(length=40, bars=10)
        >>>
        >>> # 使用EBSW识别市场周期阶段
        >>> def detect_cycle_phase(self):
        >>>     ebsw = self.data.ebsw()
        >>>     current_value = ebsw.new
        >>>
        >>>     if current_value > 0.5:
        >>>         return "强势上升周期"
        >>>     elif current_value < -0.5:
        >>>         return "强势下降周期"
        >>>     elif current_value > 0:
        >>>         return "弱势上升周期"
        >>>     else:
        >>>         return "弱势下降周期"
        >>>
        >>> # 结合EBSW与其他周期指标
        >>> def cycle_confirmation_strategy(self):
        >>>     ebsw = self.data.ebsw(length=44)
        >>>     # 当EBSW从负转正时考虑买入
        >>>     if ebsw.new > 0 and ebsw.prev <= 0:
        >>>         self.data.buy()
        """
        ...

    @tobtind(lines=None, lib='pta')
    def ao(self, fast=5, slow=34, offset=0, **kwargs) -> IndSeries:
        """
        动量震荡指标 (Awesome Oscillator, AO)
        ---------
            用于衡量证券的动量，通常用于确认趋势或预期可能的反转。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Awesome_Oscillator_(AO)
        - https://www.ifcm.co.uk/ntx-indicators/awesome-oscillator

        计算方法:
        ---------
            median = (high + low) / 2
            AO = SMA(median, fast) - SMA(median, slow)

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 5
            slow (int): 慢周期. 默认: 34
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算动量震荡指标
        >>> ao_IndSeries = self.data.ao()
        >>>
        >>> # 使用AO识别买卖信号
        >>> def ao_signals(self):
        >>>     ao = self.data.ao(fast=5, slow=34)
        >>>     # 碟形买入信号：连续三个柱状线在零轴下方，且中间柱状线最低
        >>>     if (ao.sndprev < 0 and ao.prev < ao.sndprev and ao.new > ao.prev):
        >>>         return "碟形买入信号"
        >>>     # 穿越零轴买入信号
        >>>     elif ao.new > 0 and ao.prev <= 0:
        >>>         return "零轴上方买入信号"
        >>>
        >>> # AO与价格背离分析
        >>> def ao_divergence(self):
        >>>     ao = self.data.ao()
        >>>     # 价格创新低但AO未创新低 - 看涨背离
        >>>     if (self.data.low.new < self.data.low.prev and
        >>>         ao.new > ao.prev):
        >>>         return "看涨背离"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def apo(self, fast=12, slow=26, mamode='sma', offset=0, **kwargs) -> IndSeries:
        """
        绝对价格震荡指标 (Absolute Price Oscillator, APO)
        ---------
        - 用于衡量证券的动量，是两个不同周期指数移动平均线的差值。
        - 注意：APO与MACD线是等价的。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/xtrader-help/x-study/technical-indicator-definitions/absolute-price-oscillator-apo/

        计算方法:
        ---------
            APO = SMA(close, fast) - SMA(close, slow)

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 12
            slow (int): 慢周期. 默认: 26
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算绝对价格震荡指标
        >>> apo_IndSeries = self.data.apo()
        >>>
        >>> # 使用APO识别动量变化
        >>> def apo_momentum_strategy(self):
        >>>     apo = self.data.apo(fast=12, slow=26)
        >>>     # APO上穿零轴 - 买入信号
        >>>     if apo.new > 0 and apo.prev <= 0:
        >>>         self.data.buy()
        >>>     # APO下穿零轴 - 卖出信号
        >>>     elif apo.new < 0 and apo.prev >= 0:
        >>>         self.data.sell()
        >>>
        >>> # APO与价格背离分析
        >>> def apo_divergence_analysis(self):
        >>>     apo = self.data.apo()
        >>>     # 价格创新高但APO未创新高 - 看跌背离
        >>>     if (self.data.high.new > self.data.high.prev and
        >>>         apo.new < apo.prev):
        >>>         return "看跌背离信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def bias(self, length=26, mamode="sma", offset=0, **kwargs) -> IndSeries:
        """
        乖离率指标 (Bias, BIAS)
        ---------
            衡量价格与移动平均线之间的偏离程度。

        数据来源:
        ---------
            基于网络资源定义，由Github用户homily在issue #46中请求添加

        计算方法:
        ---------
        >>> BIAS = (close - MA(close, length)) / MA(close, length)
                  = (close / MA(close, length)) - 1

        参数:
        ---------
        >>> length (int): 周期. 默认: 26
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算乖离率
        >>> bias_IndSeries = self.data.bias(length=20)
        >>>
        >>> # 使用乖离率识别超买超卖
        >>> def bias_overbought_oversold(self):
        >>>     bias = self.data.bias(length=20)
        >>>     # 乖离率超过5% - 超买区域
        >>>     if bias.new > 0.05:
        >>>         return "超买信号"
        >>>     # 乖离率低于-5% - 超卖区域
        >>>     elif bias.new < -0.05:
        >>>         return "超卖信号"
        >>>
        >>> # 乖离率回归策略
        >>> def bias_reversion_strategy(self):
        >>>     bias = self.data.bias(length=26)
        >>>     # 当乖离率从极端值回归时交易
        >>>     if bias.new < -0.08 and bias.prev <= -0.08:
        >>>         self.data.buy()  # 严重超卖后买入
        >>>     elif bias.new > 0.08 and bias.prev >= 0.08:
        >>>         self.data.sell()  # 严重超买后卖出
        """
        ...

    @tobtind(lines=None, lib='pta')
    def bop(self, scalar=1, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        多空均衡指标 (Balance of Power, BOP)
        ---------
            衡量买方与卖方的市场力量对比。

        数据来源:
        ---------
            http://www.worden.com/TeleChartHelp/Content/Indicators/Balance_of_Power.htm

        计算方法:
        ---------
        >>> BOP = scalar * (close - open) / (high - low)

        参数:
        ---------
        >>> scalar (float): 放大倍数. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算多空均衡指标
        >>> bop_IndSeries = self.data.bop()
        >>>
        >>> # 使用BOP识别买卖压力
        >>> def bop_pressure_analysis(self):
        >>>     bop = self.data.bop()
        >>>     # BOP大于0.5 - 强烈买入压力
        >>>     if bop.new > 0.5:
        >>>         return "强烈买入压力"
        >>>     # BOP小于-0.5 - 强烈卖出压力
        >>>     elif bop.new < -0.5:
        >>>         return "强烈卖出压力"
        >>>
        >>> # BOP与价格行为结合
        >>> def bop_confirmation_strategy(self):
        >>>     bop = self.data.bop(scalar=1)
        >>>     # 阳线且BOP为正 - 确认上涨动力
        >>>     if (self.data.close.new > self.data.open.new and
        >>>         bop.new > 0):
        >>>         self.data.buy()
        >>>     # 阴线且BOP为负 - 确认下跌动力
        >>>     elif (self.data.close.new < self.data.open.new and
        >>>           bop.new < 0):
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=['ar', 'br'], lib='pta')
    def brar(self, length=26, scalar=100, drift=1, offset=0, **kwargs) -> IndFrame:
        """
        情绪指标 (BRAR)
        ---------
        - BR和AR指标，用于衡量市场买卖情绪强度。
        - AR指标反映市场当前交易日的买卖气势，BR指标反映市场前一日收盘价与当前交易日价格的关系。

        数据来源:
        ---------
            基于网络资源定义，由Github用户homily在issue #46中请求添加

        计算方法:
        ---------
        >>> HO_Diff = high - open
            OL_Diff = open - low
            HCY = high - close[-1]
            CYL = close[-1] - low
            HCY[HCY < 0] = 0
            CYL[CYL < 0] = 0
            AR = scalar * SUM(HO, length) / SUM(OL, length)
            BR = scalar * SUM(HCY, length) / SUM(CYL, length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 26
            scalar (float): 放大倍数. 默认: 100
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含ar, br列的数据框

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算BRAR指标
        >>> brar_data = self.data.brar(length=26)
        >>> ar, br = brar_data.ar, brar_data.br
        >>>
        >>> # 使用BRAR识别市场情绪
        >>> def market_sentiment_analysis(self):
        >>>     brar = self.data.brar()
        >>>     # AR > 150 且 BR > 150 - 市场过热
        >>>     if brar.ar.new > 150 and brar.br.new > 150:
        >>>         return "市场过热，注意风险"
        >>>     # AR < 50 且 BR < 50 - 市场过冷
        >>>     elif brar.ar.new < 50 and brar.br.new < 50:
        >>>         return "市场过冷，可能反弹"
        >>>
        >>> # BRAR背离分析
        >>> def brar_divergence_strategy(self):
        >>>     brar = self.data.brar(length=26)
        >>>     # 价格创新高但BR未创新高 - 顶背离
        >>>     if (self.data.high.new > self.data.high.prev and
        >>>         brar.br.new < brar.br.prev):
        >>>         return "BR顶背离，卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def cci(self, length=14, c=0.015, offset=0, **kwargs) -> IndSeries:
        """
        商品通道指数 (Commodity Channel Index, CCI)
        ---------
            动量震荡指标，主要用于识别相对于均值的超买超卖水平。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Commodity_Channel_Index_(CCI)

        计算方法:
        ---------
        >>> tp = typical_price = hlc3 = (high + low + close) / 3
            mean_tp = SMA(tp, length)
            mad_tp = MAD(tp, length)
            CCI = (tp - mean_tp) / (c * mad_tp)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            c (float): 缩放常数. 默认: 0.015
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算CCI指标
        >>> cci_IndSeries = self.data.cci(length=14)
        >>>
        >>> # 使用CCI识别超买超卖
        >>> def cci_trading_signals(self):
        >>>     cci = self.data.cci(length=20)
        >>>     # CCI > 100 - 超买区域
        >>>     if cci.new > 100:
        >>>         self.data.sell()
        >>>     # CCI < -100 - 超卖区域
        >>>     elif cci.new < -100:
        >>>         self.data.buy()
        >>>
        >>> # CCI趋势确认
        >>> def cci_trend_confirmation(self):
        >>>     cci = self.data.cci(length=14)
        >>>     # CCI从超卖区域上穿-100 - 买入信号
        >>>     if cci.new > -100 and cci.prev <= -100:
        >>>         return "CCI买入信号"
        >>>     # CCI从超买区域下穿100 - 卖出信号
        >>>     elif cci.new < 100 and cci.prev >= 100:
        >>>         return "CCI卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def cfo(self, length=9, scalar=100., drift=1, offset=0, **kwargs) -> IndSeries:
        """
        钱德预测震荡指标 (Chande Forecast Oscillator, CFO)
        ---------
            计算实际价格与时间序列预测（线性回归线的端点）之间的百分比差异。

        数据来源:
        ---------
            https://www.fmlabs.com/reference/default.htm?url=ForecastOscillator.htm

        计算方法:
        ---------
        >>> CFO = scalar * (close - LINERREG(length, tdf=True)) / close

        参数:
        ---------
        >>> length (int): 周期. 默认: 9
            scalar (float): 放大倍数. 默认: 100
            drift (int): 短期周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算CFO指标
        >>> cfo_IndSeries = self.data.cfo(length=9)
        >>>
        >>> # 使用CFO识别预测偏差
        >>> def cfo_prediction_analysis(self):
        >>>     cfo = self.data.cfo(length=14)
        >>>     # CFO > 5 - 价格高于预测值
        >>>     if cfo.new > 5:
        >>>         return "价格强势，高于预测"
        >>>     # CFO < -5 - 价格低于预测值
        >>>     elif cfo.new < -5:
        >>>         return "价格弱势，低于预测"
        >>>
        >>> # CFO与价格行为结合
        >>> def cfo_reversion_strategy(self):
        >>>     cfo = self.data.cfo(length=9)
        >>>     # CFO从极端正值回归 - 卖出机会
        >>>     if cfo.new < 10 and cfo.prev >= 10:
        >>>         self.data.sell()
        >>>     # CFO从极端负值回归 - 买入机会
        >>>     elif cfo.new > -10 and cfo.prev <= -10:
        >>>         self.data.buy()
        """
        ...

    @tobtind(lines=None, lib='pta')
    def cg(self, length=10, offset=0, **kwargs) -> IndSeries:
        """
        重心指标 (Center of Gravity, CG)
        ---------
            John Ehlers开发的重心指标，试图在显示零滞后和平滑的同时识别转折点。

        数据来源:
        ---------
            http://www.mesasoftware.com/papers/TheCGOscillator.pdf

        计算方法:
        ---------
            参考原始论文实现

        参数:
        ---------
        >>> length (int): 周期长度. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算重心指标
        >>> cg_IndSeries = self.data.cg(length=10)
        >>>
        >>> # 使用CG识别转折点
        >>> def cg_turning_points(self):
        >>>     cg = self.data.cg(length=10)
        >>>     # CG指标上穿其移动平均线 - 买入信号
        >>>     cg_ma = cg.ema(period=5)
        >>>     if cg.new > cg_ma.new and cg.prev <= cg_ma.prev:
        >>>         return "CG买入信号"
        >>>     # CG指标下穿其移动平均线 - 卖出信号
        >>>     elif cg.new < cg_ma.new and cg.prev >= cg_ma.prev:
        >>>         return "CG卖出信号"
        >>>
        >>> # CG指标与价格背离
        >>> def cg_divergence_analysis(self):
        >>>     cg = self.data.cg(length=10)
        >>>     # 价格创新低但CG指标未创新低 - 看涨背离
        >>>     if (self.data.close.new < self.data.close.prev and
        >>>         cg.new > cg.prev):
        >>>         return "CG看涨背离"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def cmo(self, length=14, scalar=100., talib=True, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        钱德动量震荡指标 (Chande Momentum Oscillator, CMO)
        ---------
            试图捕捉资产的动量，超买水平在50，超卖水平在-50。

        数据来源:
        ---------
        - https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/chande-momentum-oscillator-cmo/
        - https://www.tradingview.com/script/hdrf0fXV-Variable-Index-Dynamic-Average-VIDYA/

        计算方法:
        ---------
        >>> CMO = scalar * (PSUM - NSUM) / (PSUM + NSUM)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            scalar (float): 放大倍数. 默认: 100
            drift (int): 短期周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> talib (bool): 如果为True且安装了TA-Lib，使用TA-Lib的实现。否则使用EMA版本。默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算CMO指标
        >>> cmo_IndSeries = self.data.cmo(length=14)
        >>>
        >>> # 使用CMO识别超买超卖
        >>> def cmo_trading_signals(self):
        >>>     cmo = self.data.cmo(length=14)
        >>>     # CMO > 50 - 超买，卖出信号
        >>>     if cmo.new > 50:
        >>>         self.data.sell()
        >>>     # CMO < -50 - 超卖，买入信号
        >>>     elif cmo.new < -50:
        >>>         self.data.buy()
        >>>
        >>> # CMO动量确认
        >>> def cmo_momentum_confirmation(self):
        >>>     cmo = self.data.cmo(length=14)
        >>>     # CMO从负值区域上穿0轴 - 动量转强
        >>>     if cmo.new > 0 and cmo.prev <= 0:
        >>>         return "动量转强信号"
        >>>     # CMO从正值区域下穿0轴 - 动量转弱
        >>>     elif cmo.new < 0 and cmo.prev >= 0:
        >>>         return "动量转弱信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def coppock(self, length=10, fast=11, slow=14, offset=0, **kwargs) -> IndSeries:
        """
        科波克曲线 (Coppock Curve)
        ---------
        - 动量指标，最初称为"趋势模型"，设计用于月度时间尺度。
        - 虽然设计用于月度使用，但可以在相同周期内进行日线计算。

        数据来源:
        ---------
            https://en.wikipedia.org/wiki/Coppock_curve

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            MAD = Mean Absolute Deviation
            tp = typical_price = hlc3 = (high + low + close) / 3
            mean_tp = SMA(tp, length)
            mad_tp = MAD(tp, length)
            CCI = (tp - mean_tp) / (c * mad_tp)

        参数:
        ---------
        >>> length (int): WMA周期. 默认: 10
            fast (int): 快速ROC周期. 默认: 11
            slow (int): 慢速ROC周期. 默认: 14
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算科波克曲线
        >>> coppock_IndSeries = self.data.coppock()
        >>>
        >>> # 使用科波克曲线识别长期买卖点
        >>> def coppock_trading_signals(self):
        >>>     cop = self.data.coppock(length=10, fast=11, slow=14)
        >>>     # 科波克曲线上穿零轴 - 长期买入信号
        >>>     if cop.new > 0 and cop.prev <= 0:
        >>>         return "科波克长期买入信号"
        >>>     # 科波克曲线下穿零轴 - 长期卖出信号
        >>>     elif cop.new < 0 and cop.prev >= 0:
        >>>         return "科波克长期卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def cti(self, length=12, offset=0, **kwargs) -> IndSeries:
        """
        相关趋势指标 (Correlation Trend Indicator, CTI)
        ---------
        - John Ehler在2020年创建的震荡指标。
        - 根据价格在该范围内跟随正斜率或负斜率直线的接近程度分配值，值范围从-1到1。

        参数:
        ---------
        >>> length (int): 周期. 默认: 12
            offset (int): 结果偏移周期数. 默认: 0

        返回:
        ---------
        >>> IndSeries: CTI值序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算相关趋势指标
        >>> cti_IndSeries = self.data.cti(length=12)
        >>>
        >>> # 使用CTI识别趋势强度
        >>> def cti_trend_strength(self):
        >>>     cti = self.data.cti(length=12)
        >>>     # CTI > 0.8 - 强烈上升趋势
        >>>     if cti.new > 0.8:
        >>>         return "强烈上升趋势"
        >>>     # CTI < -0.8 - 强烈下降趋势
        >>>     elif cti.new < -0.8:
        >>>         return "强烈下降趋势"
        >>>     # CTI接近0 - 震荡市场
        >>>     elif abs(cti.new) < 0.2:
        >>>         return "震荡市场"
        """
        ...

    @tobtind(lines=['dmp', 'dmn'], lib='pta')
    def dm(self, length=14, mamode="rma", talib=True, drift=1, offset=0, **kwargs) -> IndFrame:
        """
        方向运动指标 (Directional Movement, DM)
        ---------
        - J. Welles Wilder在1978年开发，试图确定资产价格的移动方向。
        - 比较前期高点和低点以产生两个序列：+DM和-DM。

        数据来源:
        ---------
        - https://www.tradingview.com/pine-script-reference/#fun_dmi
        - https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=24&Name=Directional_Movement_Index

        计算方法:
        ---------
        >>> up = high - high.shift(drift)
            dn = low.shift(drift) - low
            pos_ = ((up > dn) & (up > 0)) * up
            neg_ = ((dn > up) & (dn > 0)) * dn
            pos_ = pos_.apply(zero)
            neg_ = neg_.apply(zero)
            pos = ma(mamode, pos_, length=length)
            neg = ma(mamode, neg_, length=length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            mamode (str): 移动平均模式，参考MaType. 默认: 'rma'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        返回:
        ---------
        >>> IndFrame: 包含dmp(+DM)和dmn(-DM)列的数据框

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算方向运动指标
        >>> dm_data = self.data.dm(length=14)
        >>> dmp, dmn = dm_data.dmp, dm_data.dmn
        >>>
        >>> # 使用DM识别趋势方向
        >>> def dm_trend_direction(self):
        >>>     dm = self.data.dm(length=14)
        >>>     # +DM > -DM - 上升趋势占主导
        >>>     if dm.dmp.new > dm.dmn.new:
        >>>         return "上升趋势"
        >>>     # -DM > +DM - 下降趋势占主导
        >>>     elif dm.dmn.new > dm.dmp.new:
        >>>         return "下降趋势"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def er(self, length=14, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        效率比率 (Efficiency Ratio, ER)
        ---------
        - Perry J. Kaufman发明并在其著作"New Trading Systems and Methods"中提出。
        - 旨在衡量市场噪音或波动性。

        数据来源:
        ---------
            https://help.tc2000.com/m/69404/l/749623-kaufman-efficiency-ratio

        计算方法:
        ---------
        >>> ABS = Absolute Value
            EMA = Exponential Moving Average
            abs_diff = ABS(close.diff(length))
            volatility = ABS(close.diff(1))
            ER = abs_diff / SUM(volatility, length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算效率比率
        >>> er_IndSeries = self.data.er(length=14)
        >>>
        >>> # 使用ER识别趋势效率
        >>> def er_trend_efficiency(self):
        >>>     er = self.data.er(length=14)
        >>>     # ER > 0.5 - 高效趋势市场
        >>>     if er.new > 0.5:
        >>>         return "高效趋势，适合趋势跟踪"
        >>>     # ER < 0.2 - 低效震荡市场
        >>>     elif er.new < 0.2:
        >>>         return "低效震荡，适合均值回归"
        """
        ...

    @tobtind(lines=['bullp', 'bearp'], lib='pta')
    def eri(self, length=14, offset=0, **kwargs) -> IndFrame:
        """
        艾尔德射线指标 (Elder Ray Index, ERI)
        ---------
        - 包含牛市力量和熊市力量，用于观察价格并了解市场背后的强度。
        - 牛市力量衡量市场中买方将价格推高至平均共识价值之上的能力。
        - 熊市力量衡量卖方将价格拉低至平均共识价值之下的能力。

        数据来源:
        ---------
            https://admiralmarkets.com/education/articles/forex-indicators/bears-and-bulls-power-indicator

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            BULLPOWER = high - EMA(close, length)
            BEARPOWER = low - EMA(close, length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含bullp和bearp列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算艾尔德射线指标
        >>> eri_data = self.data.eri(length=14)
        >>> bullp, bearp = eri_data.bullp, eri_data.bearp
        >>>
        >>> # 使用ERI识别买卖力量
        >>> def eri_power_analysis(self):
        >>>     eri = self.data.eri(length=13)
        >>>     # 牛市力量为正且熊市力量为负 - 强烈看涨
        >>>     if eri.bullp.new > 0 and eri.bearp.new < 0:
        >>>         return "强烈看涨信号"
        >>>     # 牛市力量为负且熊市力量为正 - 强烈看跌
        >>>     elif eri.bullp.new < 0 and eri.bearp.new > 0:
        >>>         return "强烈看跌信号"
        """
        ...

    @tobtind(lines=['fisher', 'fishers'], lib='pta')
    def fisher(self, length=9, signal=1, offset=0, **kwargs) -> IndFrame:
        """
        费希尔变换 (Fisher Transform, FISHT)
        ---------
        - 通过在一定周期内标准化价格来识别重要的价格反转。
        - 当两条线交叉时，建议反转信号。

        数据来源:
        ---------
            TradingView (相关性 >99%)

        计算方法:
        ---------
        >>> HL2 = hl2(high, low)
            HHL2 = HL2.rolling(length).max()
            LHL2 = HL2.rolling(length).min()
            HLR = HHL2 - LHL2
            HLR[HLR < 0.001] = 0.001
            position = ((HL2 - LHL2) / HLR) - 0.5
            v = 0
            m = high.size
            FISHER = [np.nan for _ in range(0, length - 1)] + [0]
            for i in range(length, m):
                v = 0.66 * position[i] + 0.67 * v
                if v < -0.99: v = -0.999
                if v >  0.99: v =  0.999
                FISHER.append(0.5 * (nplog((1 + v) / (1 - v)) + FISHER[i - 1]))
            SIGNAL = FISHER.shift(signal)

        参数:
        ---------
        >>> length (int): 费希尔周期. 默认: 9
            signal (int): 费希尔信号周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含fisher和fishers列的数据框

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算费希尔变换
        >>> fisher_data = self.data.fisher(length=9)
        >>> fisher, fishers = fisher_data.fisher, fisher_data.fishers
        >>>
        >>> # 使用费希尔变换识别反转点
        >>> def fisher_reversal_signals(self):
        >>>     fish = self.data.fisher(length=9)
        >>>     # 费希尔线上穿信号线 - 买入信号
        >>>     if fish.fisher.new > fish.fishers.new and fish.fisher.prev <= fish.fishers.prev:
        >>>         return "费希尔买入信号"
        >>>     # 费希尔线下穿信号线 - 卖出信号
        >>>     elif fish.fisher.new < fish.fishers.new and fish.fisher.prev >= fish.fishers.prev:
        >>>         return "费希尔卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def inertia(self, length=20, rvi_length=14, scalar=100., refined=False, thirds=False, mamode="ema", drift=1, offset=0, **kwargs) -> IndSeries:
        """
        惯性指标 (Inertia, INERTIA)
        ---------
        - Donald Dorsey开发并在1995年9月的文章中介绍。
        - 是通过最小二乘移动平均平滑的相对活力指数。
        - 当值大于50时为正惯性，否则为负惯性。

        数据来源:
        ---------
            https://www.investopedia.com/terms/r/relative_vigor_index.asp

        计算方法:
        ---------
        >>> LSQRMA = Least Squares Moving Average
            INERTIA = LSQRMA(RVI(length), ma_length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 20
            rvi_length (int): RVI周期. 默认: 14
            refined (bool): 使用'精炼'计算. 默认: False
            thirds (bool): 使用'三分之一'计算. 默认: False
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'ema'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, high, low

        使用案例:
        ---------
        >>> # 计算惯性指标
        >>> inertia_IndSeries = self.data.inertia(length=20)
        >>>
        >>> # 使用惯性指标判断趋势持续性
        >>> def inertia_trend_persistence(self):
        >>>     inertia = self.data.inertia(length=20)
        >>>     # 惯性 > 50 - 正惯性，上升趋势持续
        >>>     if inertia.new > 50:
        >>>         return "正惯性，上升趋势"
        >>>     # 惯性 < 50 - 负惯性，下降趋势持续
        >>>     elif inertia.new < 50:
        >>>         return "负惯性，下降趋势"
        """
        ...

    @tobtind(lines=['k', 'd', 'j'], lib='pta')
    def kdj(self, length=9, signal=3, offset=0, **kwargs) -> IndFrame:
        """
        KDJ指标 (KDJ)
        ---------
        - KDJ指标实际上是慢速随机指标的一种衍生形式，
        - 主要区别在于多了一条称为J线的线。
        - J线代表%D值与%K值的背离。

        数据来源:
        ---------
        - https://www.prorealcode.com/prorealtime-indicators/kdj/
        - https://docs.anychart.com/Stock_Charts/Technical_Indicators/Mathematical_Description#kdj

        计算方法:
        ---------
        >>> LL = low for last 9 periods
            HH = high for last 9 periods
            FAST_K = 100 * (close - LL) / (HH - LL)
            K = RMA(FAST_K, signal)
            D = RMA(K, signal)
            J = 3K - 2D

        参数:
        ---------
        >>> length (int): 周期. 默认: 9
            signal (int): 信号周期. 默认: 3
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含k、d和j列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算KDJ指标
        >>> kdj_data = self.data.kdj(length=9)
        >>> k, d, j = kdj_data.k, kdj_data.d, kdj_data.j
        >>>
        >>> # 使用KDJ识别超买超卖
        >>> def kdj_trading_signals(self):
        >>>     kdj = self.data.kdj(length=9)
        >>>     # K < 20 且 D < 20 - 超卖区域
        >>>     if kdj.k.new < 20 and kdj.d.new < 20:
        >>>         return "KDJ超卖，买入机会"
        >>>     # K > 80 且 D > 80 - 超买区域
        >>>     elif kdj.k.new > 80 and kdj.d.new > 80:
        >>>         return "KDJ超买，卖出机会"
        >>>
        >>> # KDJ金叉死叉信号
        >>> def kdj_cross_signals(self):
        >>>     kdj = self.data.kdj(length=9)
        >>>     # K线上穿D线 - 金叉买入
        >>>     if kdj.k.new > kdj.d.new and kdj.k.prev <= kdj.d.prev:
        >>>         return "KDJ金叉买入信号"
        >>>     # K线下穿D线 - 死叉卖出
        >>>     elif kdj.k.new < kdj.d.new and kdj.k.prev >= kdj.d.prev:
        >>>         return "KDJ死叉卖出信号"
        """
        ...

    @tobtind(lines=['kst', 'ksts'], lib='pta')
    def kst(self, roc1=10, roc2=15, roc3=20, roc4=30, sma1=10, sma2=10, sma3=10, sma4=15, signal=9, drift=1, offset=0, **kwargs) -> IndFrame:
        """
        确然指标 (Know Sure Thing, KST)
        ---------
            基于动量的震荡指标，基于ROC计算。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Know_Sure_Thing_(KST)
        - https://www.incrediblecharts.com/indicators/kst.php

        计算方法:
        ---------
        >>> ROC = Rate of Change
            SMA = Simple Moving Average
            rocsma1 = SMA(ROC(close, roc1), sma1)
            rocsma2 = SMA(ROC(close, roc2), sma2)
            rocsma3 = SMA(ROC(close, roc3), sma3)
            rocsma4 = SMA(ROC(close, roc4), sma4)
            KST = 100 * (rocsma1 + 2 * rocsma2 + 3 * rocsma3 + 4 * rocsma4)
            KST_Signal = SMA(KST, signal)

        参数:
        ---------
        >>> roc1 (int): ROC1周期. 默认: 10
            roc2 (int): ROC2周期. 默认: 15
            roc3 (int): ROC3周期. 默认: 20
            roc4 (int): ROC4周期. 默认: 30
            sma1 (int): SMA1周期. 默认: 10
            sma2 (int): SMA2周期. 默认: 10
            sma3 (int): SMA3周期. 默认: 10
            sma4 (int): SMA4周期. 默认: 15
            signal (int): 信号周期. 默认: 9
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含kst和ksts列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算确然指标
        >>> kst_data = self.data.kst()
        >>> kst, ksts = kst_data.kst, kst_data.ksts
        >>>
        >>> # 使用KST识别长期趋势
        >>> def kst_trend_signals(self):
        >>>     kst = self.data.kst()
        >>>     # KST上穿信号线 - 长期买入信号
        >>>     if kst.kst.new > kst.ksts.new and kst.kst.prev <= kst.ksts.prev:
        >>>         return "KST长期买入信号"
        >>>     # KST下穿信号线 - 长期卖出信号
        >>>     elif kst.kst.new < kst.ksts.new and kst.kst.prev >= kst.ksts.prev:
        >>>         return "KST长期卖出信号"
        """
        ...

    @tobtind(lines=['macdx', 'macdh', 'macds'], lib='pta', linestyle=dict(macdh=LineStyle(line_dash=LineDash.vbar)))
    def macd(self, fast=12, slow=26, signal=9, talib=True, offset=0, **kwargs) -> IndFrame:
        """
        指数平滑移动平均线 (Moving Average Convergence Divergence, MACD)
        ---------
        - 用于识别证券趋势的流行指标。
        - 虽然APO和MACD是相同的计算，但MACD还返回另外两个序列：信号线和柱状图。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/MACD_(Moving_Average_Convergence/Divergence)
        - AS模式: https://tr.tradingview.com/script/YFlKXHnP/

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            MACD = EMA(close, fast) - EMA(close, slow)
            Signal = EMA(MACD, signal)
            Histogram = MACD - Signal
            if asmode:
                MACD = MACD - Signal
                Signal = EMA(MACD, signal)
                Histogram = MACD - Signal

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 12
            slow (int): 慢周期. 默认: 26
            signal (int): 信号周期. 默认: 9
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> asmode (bool, 可选): 为True时启用MACD的AS版本. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含macdx、macdh、macds列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算MACD指标
        >>> macd_data = self.data.macd(fast=12, slow=26, signal=9)
        >>> macd_line, signal_line, histogram = macd_data.macdx, macd_data.macds, macd_data.macdh
        >>>
        >>> # 使用MACD金叉死叉信号
        >>> def macd_cross_signals(self):
        >>>     macd = self.data.macd()
        >>>     # MACD线上穿信号线 - 金叉买入
        >>>     if macd.macdx.new > macd.macds.new and macd.macdx.prev <= macd.macds.prev:
        >>>         self.data.buy()
        >>>     # MACD线下穿信号线 - 死叉卖出
        >>>     elif macd.macdx.new < macd.macds.new and macd.macdx.prev >= macd.macds.prev:
        >>>         self.data.sell()
        >>>
        >>> # MACD柱状图分析
        >>> def macd_histogram_analysis(self):
        >>>     macd = self.data.macd()
        >>>     # 柱状图由负转正 - 动能转强
        >>>     if macd.macdh.new > 0 and macd.macdh.prev <= 0:
        >>>         return "MACD动能转强"
        >>>     # 柱状图由正转负 - 动能转弱
        >>>     elif macd.macdh.new < 0 and macd.macdh.prev >= 0:
        >>>         return "MACD动能转弱"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def mom(self, length=10, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        动量指标 (Momentum, MOM)
        ---------
            用于衡量证券运动速度（或强度）的指标，或简单地说是价格的变化。

        数据来源:
        ---------
            http://www.onlinetradingconcepts.com/TechnicalAnalysis/Momentum.html

        计算方法:
        ---------
        >>> MOM = close.diff(length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算动量指标
        >>> mom_IndSeries = self.data.mom(length=10)
        >>>
        >>> # 使用动量指标识别价格加速度
        >>> def momentum_acceleration(self):
        >>>     mom = self.data.mom(length=10)
        >>>     # 动量转正 - 上涨加速度
        >>>     if mom.new > 0 and mom.prev <= 0:
        >>>         return "上涨动量启动"
        >>>     # 动量转负 - 下跌加速度
        >>>     elif mom.new < 0 and mom.prev >= 0:
        >>>         return "下跌动量启动"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def pgo(self, length=14, offset=0, **kwargs) -> IndSeries:
        """
        相当好震荡指标 (Pretty Good Oscillator, PGO)
        ---------
        - Mark Johnson创建的指标，用于衡量当前收盘价与其N日简单移动平均线的距离，
        - 以类似期间的平均真实波幅表示。
        - Johnson的方法是用它作为长期交易的突破系统。
        - 大于3.0做多，小于-3.0做空。

        数据来源:
        ---------
            https://library.tradingtechnologies.com/trade/chrt-ti-pretty-good-oscillator.html

        计算方法:
        ---------
        >>> ATR = Average True Range
            SMA = Simple Moving Average
            EMA = Exponential Moving Average
            PGO = (close - SMA(close, length)) / \
                   EMA(ATR(high, low, close, length), length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算相当好震荡指标
        >>> pgo_IndSeries = self.data.pgo(length=14)
        >>>
        >>> # 使用PGO识别突破信号
        >>> def pgo_breakout_signals(self):
        >>>     pgo = self.data.pgo(length=14)
        >>>     # PGO > 3.0 - 强烈买入信号
        >>>     if pgo.new > 3.0:
        >>>         self.data.buy()
        >>>     # PGO < -3.0 - 强烈卖出信号
        >>>     elif pgo.new < -3.0:
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=['ppo', 'ppoh', 'ppos'], lib='pta')
    def ppo(self, fast=12, slow=26, signal=9, scalar=100., mamode="sma", talib=True, **kwargs) -> IndFrame:
        """
        百分比价格震荡指标 (Percentage Price Oscillator, PPO)
        ---------
            在衡量动量方面与MACD类似。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/MACD_(Moving_Average_Convergence/Divergence)

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            EMA = Exponential Moving Average
            fast_sma = SMA(close, fast)
            slow_sma = SMA(close, slow)
            PPO = 100 * (fast_sma - slow_sma) / slow_sma
            Signal = EMA(PPO, signal)
            Histogram = PPO - Signal

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 12
            slow (int): 慢周期. 默认: 26
            signal (int): 信号周期. 默认: 9
            scalar (float): 放大倍数. 默认: 100
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含ppo、ppoh、ppos列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算百分比价格震荡指标
        >>> ppo_data = self.data.ppo(fast=12, slow=26, signal=9)
        >>> ppo, signal, histogram = ppo_data.ppo, ppo_data.ppos, ppo_data.ppoh
        >>>
        >>> # 使用PPO识别动量变化
        >>> def ppo_momentum_strategy(self):
        >>>     ppo = self.data.ppo()
        >>>     # PPO上穿零轴 - 买入信号
        >>>     if ppo.ppo.new > 0 and ppo.ppo.prev <= 0:
        >>>         self.data.buy()
        >>>     # PPO下穿零轴 - 卖出信号
        >>>     elif ppo.ppo.new < 0 and ppo.ppo.prev >= 0:
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=None, lib='pta')
    def psl(self, length=12, scalar=100., drift=1, offset=0, **kwargs) -> IndSeries:
        """
        心理线 (Psychological Line, PSL)
        ---------
        - 震荡型指标，比较上涨周期数与总周期数的比例。
        - 换句话说，它是在给定期间内收盘价高于前一根K线的K线百分比。

        数据来源:
        ---------
            https://www.quantshare.com/item-851-psychological-line

        计算方法:
        ---------
        >>> IF NOT open:
                DIFF = SIGN(close - close[drift])
            ELSE:
                DIFF = SIGN(close - open)
            DIFF.fillna(0)
            DIFF[DIFF <= 0] = 0
            PSL = scalar * SUM(DIFF, length) / length

        参数:
        ---------
        >>> length (int): 周期. 默认: 12
            scalar (float): 放大倍数. 默认: 100
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算心理线
        >>> psl_IndSeries = self.data.psl(length=12)
        >>>
        >>> # 使用PSL识别市场情绪
        >>> def psl_market_sentiment(self):
        >>>     psl = self.data.psl(length=12)
        >>>     # PSL > 75 - 市场过度乐观
        >>>     if psl.new > 75:
        >>>         return "市场过度乐观，注意风险"
        >>>     # PSL < 25 - 市场过度悲观
        >>>     elif psl.new < 25:
        >>>         return "市场过度悲观，可能反弹"
        """
        ...

    @tobtind(lines=['pvo', 'pvoh', 'pvos'], overlap=False, lib='pta')
    def pvo(self, fast=12, slow=26, signal=9, scalar=100., offset=0, **kwargs) -> IndFrame:
        """
        百分比成交量震荡指标 (Percentage Volume Oscillator, PVO)
        ---------
            成交量的动量震荡指标。

        数据来源:
        ---------
            https://www.fmlabs.com/reference/default.htm?url=PVO.htm

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            PVO = (EMA(volume, fast) - EMA(volume, slow)) / EMA(volume, slow)
            Signal = EMA(PVO, signal)
            Histogram = PVO - Signal

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 12
            slow (int): 慢周期. 默认: 26
            signal (int): 信号周期. 默认: 9
            scalar (float): 放大倍数. 默认: 100
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含pvo、pvoh、pvos列的数据框

        所需数据字段:
        ---------
        >>> volume

        使用案例:
        ---------
        >>> # 计算百分比成交量震荡指标
        >>> pvo_data = self.data.pvo(fast=12, slow=26, signal=9)
        >>> pvo, signal, histogram = pvo_data.pvo, pvo_data.pvos, pvo_data.pvoh
        >>>
        >>> # 使用PVO确认价格趋势
        >>> def pvo_volume_confirmation(self):
        >>>     pvo = self.data.pvo()
        >>>     # 价格上涨且PVO为正 - 量价齐升
        >>>     if (self.data.close.new > self.data.close.prev and
        >>>         pvo.pvo.new > 0):
        >>>         return "量价齐升，趋势健康"
        >>>     # 价格下跌且PVO为负 - 量价齐跌
        >>>     elif (self.data.close.new < self.data.close.prev and
        >>>           pvo.pvo.new < 0):
        >>>         return "量价齐跌，趋势延续"
        """
        ...

    @tobtind(lines=['qqe_', 'rsi_ma', 'qqel', 'qqes'], lib='pta')
    def qqe(self, length=14, smooth=5, factor=4.236, mamode="sma", drift=1, offset=0, **kwargs) -> IndFrame:
        """
        量化定性估计指标 (Quantitative Qualitative Estimation, QQE)
        ---------
        - 类似于SuperTrend，但使用带有上下带的平滑RSI。
        - 当平滑RSI交叉前一个上带时确定多头趋势，
        - 当平滑RSI交叉前一个下带时确定空头趋势。

        数据来源:
        ---------
        - https://www.tradingview.com/script/IYfA9R2k-QQE-MT4/
        - https://www.tradingpedia.com/forex-trading-indicators/quantitative-qualitative-estimation
        - https://www.prorealcode.com/prorealtime-indicators/qqe-quantitative-qualitative-estimation/

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> length (int): RSI周期. 默认: 14
            smooth (int): RSI平滑周期. 默认: 5
            factor (float): QQE因子. 默认: 4.236
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含qqe_ ,rsi_ma ,qqel ,qqes列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算QQE指标
        >>> qqe_data = self.data.qqe(length=14, smooth=5)
        >>> qqe, rsi_ma, qqel, qqes = qqe_data.qqe, qqe_data.rsi_ma, qqe_data.qqel, qqe_data.qqes
        >>>
        >>> # 使用QQE识别趋势转换
        >>> def qqe_trend_signals(self):
        >>>     qqe = self.data.qqe(length=14)
        >>>     # QQE上穿上轨 - 多头趋势开始
        >>>     if qqe.qqe.new > qqe.qqel.new and qqe.qqe.prev <= qqe.qqel.prev:
        >>>         return "QQE多头信号"
        >>>     # QQE下穿下轨 - 空头趋势开始
        >>>     elif qqe.qqe.new < qqe.qqes.new and qqe.qqe.prev >= qqe.qqes.prev:
        >>>         return "QQE空头信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def roc(self, length=10, scalar=100., talib=True, offset=0, **kwargs) -> IndSeries:
        """
        变动率指标 (Rate of Change, ROC)
        ---------
        - 也称为动量指标（容易混淆）。
        - 是一个纯粹的动量震荡指标，衡量价格与'n'（或长度）周期前价格的百分比变化。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Rate_of_Change_(ROC)

        计算方法:
        ---------
        >>> MOM = Momentum
            ROC = 100 * MOM(close, length) / close.shift(length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            scalar (float): 放大倍数. 默认: 100
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算变动率指标
        >>> roc_IndSeries = self.data.roc(length=10)
        >>>
        >>> # 使用ROC识别动量强度
        >>> def roc_momentum_strength(self):
        >>>     roc = self.data.roc(length=10)
        >>>     # ROC > 10 - 强烈上涨动量
        >>>     if roc.new > 10:
        >>>         return "强烈上涨动量"
        >>>     # ROC < -10 - 强烈下跌动量
        >>>     elif roc.new < -10:
        >>>         return "强烈下跌动量"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def rsi(self, length=14, scalar=100., talib=True, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        相对强弱指数 (Relative Strength Index, RSI)
        ---------
            流行的动量震荡指标，用于衡量定向价格运动的速度和幅度。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Relative_Strength_Index_(RSI)

        计算方法:
        ---------
        >>> ABS = Absolute Value
            RMA = Rolling Moving Average
            diff = close.diff(drift)
            positive = diff if diff > 0 else 0
            negative = diff if diff < 0 else 0
            pos_avg = RMA(positive, length)
            neg_avg = ABS(RMA(negative, length))
            RSI = scalar * pos_avg / (pos_avg + neg_avg)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            scalar (float): 放大倍数. 默认: 100
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算RSI指标
        >>> rsi_IndSeries = self.data.rsi(length=14)
        >>>
        >>> # 使用RSI识别超买超卖
        >>> def rsi_overbought_oversold(self):
        >>>     rsi = self.data.rsi(length=14)
        >>>     # RSI > 70 - 超买区域
        >>>     if rsi.new > 70:
        >>>         return "RSI超买，注意回调"
        >>>     # RSI < 30 - 超卖区域
        >>>     elif rsi.new < 30:
        >>>         return "RSI超卖，可能反弹"
        >>>
        >>> # RSI背离分析
        >>> def rsi_divergence_analysis(self):
        >>>     rsi = self.data.rsi(length=14)
        >>>     # 价格创新低但RSI未创新低 - 看涨背离
        >>>     if (self.data.close.new < self.data.close.prev and
        >>>         rsi.new > rsi.prev):
        >>>         return "RSI看涨背离"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def rsx(self, length=14, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        相对强弱扩展指标 (Relative Strength Xtra, RSX)
        ---------
        - 基于流行的RSI指标，受Jurik Research工作的启发。
        - 这个增强版的RSI减少了噪音，提供了更清晰、略有延迟的动量和价格运动速度洞察。

        数据来源:
        ---------
        - http://www.jurikres.com/catalog1/ms_rsx.htm
        - https://www.prorealcode.com/prorealtime-indicators/jurik-rsx/

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算RSX指标
        >>> rsx_IndSeries = self.data.rsx(length=14)
        >>>
        >>> # 使用RSX替代传统RSI
        >>> def rsx_trading_signals(self):
        >>>     rsx = self.data.rsx(length=14)
        >>>     # RSX > 70 - 超买信号
        >>>     if rsx.new > 70:
        >>>         self.data.sell()
        >>>     # RSX < 30 - 超卖信号
        >>>     elif rsx.new < 30:
        >>>         self.data.buy()
        """
        ...

    @tobtind(lines=['rvgi', 'rvgs'], lib='pta')
    def rvgi(self, length=14, swma_length=4, offset=0, **kwargs) -> IndFrame:
        """
        相对活力指数 (Relative Vigor Index, RVGI)
        ---------
        - 试图衡量趋势相对于其收盘价在其交易区间的强度。
        - 基于这样的信念：在上升趋势中倾向于收盘高于开盘价，
        - 在下降趋势中倾向于收盘低于开盘价。

        数据来源:
        ---------
            https://www.investopedia.com/terms/r/relative_vigor_index.asp

        计算方法:
        ---------
        >>> SWMA = Symmetrically Weighted Moving Average
            numerator = SUM(SWMA(close - open, swma_length), length)
            denominator = SUM(SWMA(high - low, swma_length), length)
            RVGI = numerator / denominator

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            swma_length (int): 周期. 默认: 4
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含rvgi、rvgs列的数据框

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算相对活力指数
        >>> rvgi_data = self.data.rvgi(length=14)
        >>> rvgi, signal = rvgi_data.rvgi, rvgi_data.rvgs
        >>>
        >>> # 使用RVGI识别趋势强度
        >>> def rvgi_trend_strength(self):
        >>>     rvgi = self.data.rvgi(length=14)
        >>>     # RVGI上穿信号线 - 看涨信号
        >>>     if rvgi.rvgi.new > rvgi.rvgs.new and rvgi.rvgi.prev <= rvgi.rvgs.prev:
        >>>         return "RVGI看涨信号"
        >>>     # RVGI下穿信号线 - 看跌信号
        >>>     elif rvgi.rvgi.new < rvgi.rvgs.new and rvgi.rvgi.prev >= rvgi.rvgs.prev:
        >>>         return "RVGI看跌信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def slope(self, length=10, as_angle=False, to_degrees=False, vertical=None, offset=0, **kwargs) -> IndSeries:
        """
        斜率指标 (Slope)
        ---------
            返回长度为n的序列的斜率。可以将斜率转换为角度。

        数据来源:
        ---------
            代数基础

        计算方法:
        ---------
        >>> slope = close.diff(length) / length
            if as_angle:
                slope = slope.apply(atan)
                if to_degrees:
                    slope *= 180 / PI

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> as_angle (bool, 可选): 将斜率转换为角度. 默认: False
            to_degrees (bool, 可选): 将斜率角度转换为度数. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算价格斜率
        >>> slope_IndSeries = self.data.slope(length=10)
        >>>
        >>> # 使用斜率识别趋势强度
        >>> def slope_trend_strength(self):
        >>>     slope = self.data.slope(length=10, as_angle=True, to_degrees=True)
        >>>     # 斜率 > 45度 - 强烈上升趋势
        >>>     if slope.new > 45:
        >>>         return "强烈上升趋势"
        >>>     # 斜率 < -45度 - 强烈下降趋势
        >>>     elif slope.new < -45:
        >>>         return "强烈下降趋势"
        """
        ...

    @tobtind(lines=['smi', 'smis', 'smios'], lib='pta')
    def smi(self, fast=5, slow=20, signal=5, scalar=1., offset=0, **kwargs) -> IndFrame:
        """
        SMI遍历指标 (SMI Ergodic Indicator)
        ---------
        - 与William Blau开发的真实强度指数(TSI)相同，但SMI包含信号线。
        - SMI使用价格减去前一个价格的双重移动平均线。
        - 当上穿零轴时趋势看涨，下穿零轴时趋势看跌。

        数据来源:
        ---------
        - https://www.motivewave.com/studies/smi_ergodic_indicator.htm
        - https://www.tradingview.com/script/Xh5Q0une-SMI-Ergodic-Oscillator/
        - https://www.tradingview.com/script/cwrgy4fw-SMIIO/

        计算方法:
        ---------
        >>> TSI = True Strength Index
            EMA = Exponential Moving Average
            ERG = TSI(close, fast, slow)
            Signal = EMA(ERG, signal)
            OSC = ERG - Signal

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 5
            slow (int): 慢周期. 默认: 20
            signal (int): 信号周期. 默认: 5
            scalar (float): 放大倍数. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含smi、smis、smios列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算SMI遍历指标
        >>> smi_data = self.data.smi(fast=5, slow=20, signal=5)
        >>> smi, signal, oscillator = smi_data.smi, smi_data.smis, smi_data.smios
        >>>
        >>> # 使用SMI识别趋势转换
        >>> def smi_trend_signals(self):
        >>>     smi = self.data.smi()
        >>>     # SMI上穿零轴 - 看涨信号
        >>>     if smi.smi.new > 0 and smi.smi.prev <= 0:
        >>>         return "SMI看涨信号"
        >>>     # SMI下穿零轴 - 看跌信号
        >>>     elif smi.smi.new < 0 and smi.smi.prev >= 0:
        >>>         return "SMI看跌信号"
        """
        ...

    @tobtind(lines=['sqz_on', 'sqz_off', 'sqz_no'], lib="pta")
    def squeeze(self, bb_length=20, bb_std=2., kc_length=20, kc_scalar=1.5,
                mom_length=12, mom_smooth=6, use_tr=True, mamode="sma", offset=0, **kwargs) -> IndFrame:
        """
        挤压指标 (Squeeze)
        ---------
        - John Carter的"TTM Squeeze"指标的扩展版本。
        - 试图捕捉布林带和凯尔特纳通道之间的关系。
        - 当波动性增加时，带之间的距离也会增加，反之亦然。

        数据来源:
        ---------
        - https://usethinkscript.com/threads/john-carters-squeeze-pro-indicator-for-thinkorswim-free.4021/
        - https://www.tradingview.com/script/TAAt6eRX-Squeeze-PRO-Indicator-Makit0/

        计算方法:
        ---------
        >>> BB = Bollinger Bands
            KC = Keltner Channels
            MOM = Momentum
            SMA = Simple Moving Average
            EMA = Exponential Moving Average
            TR = True Range
            RANGE = TR(high, low, close) if using_tr else high - low
            BB_LOW, BB_MID, BB_HIGH = BB(close, bb_length, std=bb_std)
            KC_LOW, KC_MID, KC_HIGH = KC(high, low, close, kc_length, kc_scalar, TR)
            MOMO = MOM(close, mom_length)
            SQZ = EMA(MOMO, mom_smooth) if mamode == "ema" else SMA(MOMO, mom_smooth)
            SQZ_ON = (BB_LOW > KC_LOW) and (BB_HIGH < KC_HIGH)
            SQZ_OFF = (BB_LOW < KC_LOW) and (BB_HIGH > KC_HIGH)
            NO_SQZ = !SQZ_ON and !SQZ_OFF

        参数:
        ---------
        >>> bb_length (int): 布林带周期. 默认: 20
            bb_std (float): 布林带标准差. 默认: 2
            kc_length (int): 凯尔特纳通道周期. 默认: 20
            kc_scalar (float): 凯尔特纳通道乘数. 默认: 1.5
            mom_length (int): 动量周期. 默认: 12
            mom_smooth (int): 动量平滑周期. 默认: 6
            mamode (str): 仅"ema"或"sma". 默认: "sma"
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> tr (bool, 可选): 对凯尔特纳通道使用真实波幅. 默认: True
            asint (bool, 可选): 使用整数而不是布尔值. 默认: True
            detailed (bool, 可选): 返回SQZ的附加变体用于可视化. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含sqz_on、sqz_off、sqz_no列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算挤压指标
        >>> squeeze_data = self.data.squeeze()
        >>> sqz_on, sqz_off, sqz_no = squeeze_data.sqz_on, squeeze_data.sqz_off, squeeze_data.sqz_no
        >>> 
        >>> # 使用挤压指标识别突破机会
        >>> def squeeze_breakout_opportunity(self):
        >>>     sqz = self.data.squeeze()
        >>>     # 从挤压状态释放 - 潜在突破信号
        >>>     if sqz.sqz_on.prev == 1 and sqz.sqz_off.new == 1:
        >>>         return "挤压释放，关注突破"
        """
        ...

    @tobtind(lines=['sqzpro', 'sqz_onwide', 'sqz_onnormal', 'sqz_onnarrow', 'sqzpro_off', 'sqzpro_no'], lib="pta")
    def squeeze_pro(self, bb_length=20, bb_std=2., kc_length=20,
                    kc_scalar_wide=2., kc_scalar_normal=1.5,
                    kc_scalar_narrow=1., mom_length=12, mom_smooth=6,
                    use_tr=True, mamode="sma", offset=0, **kwargs) -> IndFrame:
        """
        专业挤压指标 (Squeeze PRO)
        ---------
        - 基于John Carter的"TTM Squeeze"指标。
        - 使用三个不同宽度的凯尔特纳通道来提供更详细的挤压状态信息。

        数据来源:
        ---------
        - https://tradestation.tradingappstore.com/products/TTMSqueeze
        - https://www.tradingview.com/scripts/lazybear/
        - https://tlc.thinkorswim.com/center/reference/Tech-Indicators/studies-library/T-U/TTM-Squeeze

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> bb_length (int): 布林带周期. 默认: 20
            bb_std (float): 布林带标准差. 默认: 2
            kc_length (int): 凯尔特纳通道周期. 默认: 20
            kc_scalar_wide (float): 宽凯尔特纳通道乘数. 默认: 2
            kc_scalar_normal (float): 正常凯尔特纳通道乘数. 默认: 1.5
            kc_scalar_narrow (float): 窄凯尔特纳通道乘数. 默认: 1
            mom_length (int): 动量周期. 默认: 12
            mom_smooth (int): 动量平滑周期. 默认: 6
            mamode (str): 仅"ema"或"sma". 默认: "sma"
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> tr (bool, 可选): 对凯尔特纳通道使用真实波幅. 默认: True
            asint (bool, 可选): 使用整数而不是布尔值. 默认: True
            detailed (bool, 可选): 返回SQZ的附加变体用于可视化. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含sqzpro、sqz_onwide、sqz_onnormal、sqz_onnarrow、sqzpro_off、sqzpro_no列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算专业挤压指标
        >>> squeeze_pro_data = self.data.squeeze_pro()
        >>> 
        >>> # 使用专业挤压指标进行多级分析
        >>> def squeeze_pro_multi_level_analysis(self):
        >>>     sqz_pro = self.data.squeeze_pro()
        >>>     # 在窄通道中挤压 - 最强压缩状态
        >>>     if sqz_pro.sqz_onnarrow.new == 1:
        >>>         return "最强挤压状态，密切关注突破"
        >>>     # 在宽通道中挤压 - 较弱压缩状态
        >>>     elif sqz_pro.sqz_onwide.new == 1:
        >>>         return "较弱挤压状态，可能继续震荡"
        """
        ...

    @tobtind(lines=['stc', 'stcmacd', 'stcstoch'], lib='pta')
    def stc(self, tclength=10, fast=12, slow=26, factor=0.5, offset=0, **kwargs) -> IndFrame:
        """
        沙夫趋势周期 (Schaff Trend Cycle, STC)
        ---------
            流行MACD指标的演进，包含两个级联的随机计算和额外的平滑。

        数据来源:
        ---------
            https://www.prorealcode.com/prorealtime-indicators/schaff-trend-cycle2/

        计算方法:
        ---------
        >>> STCmacd = Moving Average Convergance/Divergance or Oscillator
            STCstoch = Intermediate Stochastic of MACD/Osc.
            2nd Stochastic including filtering with results in the
            STC = Schaff Trend Cycle

        参数:
        ---------
        >>> tclength (int): 沙夫趋势周期信号线长度. 默认: 10
            fast (int): 快周期. 默认: 12
            slow (int): 慢周期. 默认: 26
            factor (float): 最后随机计算的平滑因子. 默认: 0.5
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> ma1: 外部提供的第一移动平均线（与ma2结合使用时必需）
            ma2: 外部提供的第二移动平均线（与ma1结合使用时必需）
            osc: 外部提供的震荡指标
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含stc、stcmacd、stcstoch列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算沙夫趋势周期
        >>> stc_data = self.data.stc(tclength=10, fast=12, slow=26)
        >>> stc, stcmacd, stcstoch = stc_data.stc, stc_data.stcmacd, stc_data.stcstoch
        >>> 
        >>> # 使用STC识别趋势转换
        >>> def stc_trend_reversal(self):
        >>>     stc = self.data.stc()
        >>>     # STC上穿25 - 看涨信号
        >>>     if stc.stc.new > 25 and stc.stc.prev <= 25:
        >>>         return "STC看涨信号"
        >>>     # STC下穿75 - 看跌信号
        >>>     elif stc.stc.new < 75 and stc.stc.prev >= 75:
        >>>         return "STC看跌信号"
        """
        ...

    @tobtind(lines=['stochs', 'stoch_k', 'stoch_d'], lib='pta')
    def stoch(self, k=14, d=3, smooth_k=3, mamode="sma", offset=0, **kwargs) -> IndFrame:
        """
        随机指标 (Stochastic Oscillator)
        ---------
        - George Lane在1950年代开发。
        - 是一个范围限制的震荡指标，有两条在0和100之间移动的线。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Stochastic_(STOCH)
        - https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=332&Name=KD_-_Slow

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            LL = low for last k periods
            HH = high for last k periods
            STOCH = 100 * (close - LL) / (HH - LL)
            STOCHk = SMA(STOCH, smooth_k)
            STOCHd = SMA(FASTK, d)

        参数:
        ---------
        >>> k (int): 快速%K周期. 默认: 14
            d (int): 慢速%K周期. 默认: 3
            smooth_k (int): 慢速%D周期. 默认: 3
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含stochs、stoch_k、stoch_d列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算随机指标
        >>> stoch_data = self.data.stoch(k=14, d=3)
        >>> stoch, k_line, d_line = stoch_data.stochs, stoch_data.stoch_k, stoch_data.stoch_d
        >>> 
        >>> # 使用随机指标识别超买超卖
        >>> def stoch_overbought_oversold(self):
        >>>     stoch = self.data.stoch()
        >>>     # %K < 20 且 %D < 20 - 超卖区域
        >>>     if stoch.stoch_k.new < 20 and stoch.stoch_d.new < 20:
        >>>         return "随机指标超卖"
        >>>     # %K > 80 且 %D > 80 - 超买区域
        >>>     elif stoch.stoch_k.new > 80 and stoch.stoch_d.new > 80:
        >>>         return "随机指标超买"
        """
        ...

    @tobtind(lines=['stochrsi_k', 'stochrsi_d'], lib='pta')
    def stochrsi(self, length=14, rsi_length=14, k=3, d=3, mamode="sma", offset=0, **kwargs) -> IndFrame:
        """
        随机相对强弱指数 (Stochastic RSI)
        ---------
        - Tushar Chande和Stanley Kroll创建。
        - 是一个范围限制的震荡指标，有两条在0和100之间移动的线。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Stochastic_(STOCH)

        计算方法:
        ---------
        >>> RSI = Relative Strength Index
            SMA = Simple Moving Average
            RSI = RSI(high, low, close, rsi_length)
            LL = lowest RSI for last rsi_length periods
            HH = highest RSI for last rsi_length periods
            STOCHRSI = 100 * (RSI - LL) / (HH - LL)
            STOCHRSIk = SMA(STOCHRSI, k)
            STOCHRSId = SMA(STOCHRSIk, d)

        参数:
        ---------
        >>> length (int): 随机RSI周期. 默认: 14
            rsi_length (int): RSI周期. 默认: 14
            k (int): 快速%K周期. 默认: 3
            d (int): 慢速%K周期. 默认: 3
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含stochrsi_k、stochrsi_d列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算随机RSI
        >>> stochrsi_data = self.data.stochrsi(length=14)
        >>> stochrsi_k, stochrsi_d = stochrsi_data.stochrsi_k, stochrsi_data.stochrsi_d
        >>> 
        >>> # 使用随机RSI识别超买超卖
        >>> def stochrsi_extremes(self):
        >>>     stochrsi = self.data.stochrsi()
        >>>     # 随机RSI < 0.2 - 超卖
        >>>     if stochrsi.stochrsi_k.new < 0.2:
        >>>         return "随机RSI超卖"
        >>>     # 随机RSI > 0.8 - 超买
        >>>     elif stochrsi.stochrsi_k.new > 0.8:
        >>>         return "随机RSI超买"
        """
        ...

    # @tobtind(lines=['td_seq_up', 'td_seq_dn'], lib='pta')
    # def td_seq(self, asint=False, offset=0, show_all=True, **kwargs) -> IndFrame:
    #     """
    #     汤姆·迪马克序列指标 (TD Sequential)
    #     ---------
    #         试图识别上升趋势或下降趋势耗尽并反转的价格点。

    #     数据来源:
    #     ---------
    #         https://tradetrekker.wordpress.com/tdsequential/

    #     计算方法:
    #     ---------
    #     - 将当前收盘价与4天前的价格进行比较，最多13天。
    #     - 对于连续上升或下降的价格序列，显示第6天到第9天的值。

    #     参数:
    #     ---------
    #     >>> asint (bool): 如果为True，用0填充nas并将类型更改为int. 默认: False
    #         offset (int): 结果偏移周期数. 默认: 0

    #     可选参数:
    #     ---------
    #     >>> show_all (bool): 显示1-13。如果设置为False，显示6-9. 默认: True
    #         fillna (value, 可选): pd.DataFrame.fillna(value)

    #     返回:
    #     ---------
    #     >>> IndFrame: 包含td_seq_up、td_seq_dn列的数据框

    #     所需数据字段:
    #     ---------
    #     >>> close

    #     使用案例:
    #     ---------
    #     >>> # 计算汤姆·迪马克序列
    #     >>> td_seq_data = self.data.td_seq()
    #     >>> td_seq_up, td_seq_dn = td_seq_data.td_seq_up, td_seq_data.td_seq_dn
    #     >>> 
    #     >>> # 使用TD序列识别反转点
    #     >>> def td_sequential_reversal(self):
    #     >>>     td_seq = self.data.td_seq()
    #     >>>     # 完成9-13-9序列 - 强烈反转信号
    #     >>>     if td_seq.td_seq_up.new == 13:
    #     >>>         return "TD序列卖出信号"
    #     >>>     elif td_seq.td_seq_dn.new == 13:
    #     >>>         return "TD序列买入信号"
    #     """
    #     ...

    @tobtind(lines=['trix', 'trixs'], lib='pta')
    def trix(self, length=18, signal=9, scalar=100., drift=1, offset=0, **kwargs) -> IndFrame:
        """
        三重指数平滑平均线 (TRIX)
        ---------
            动量震荡指标，用于识别背离。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/TRIX

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            ROC = Rate of Change
            ema1 = EMA(close, length)
            ema2 = EMA(ema1, length)
            ema3 = EMA(ema2, length)
            TRIX = 100 * ROC(ema3, drift)

        参数:
        ---------
        >>> length (int): 周期. 默认: 18
            signal (int): 信号周期. 默认: 9
            scalar (float): 放大倍数. 默认: 100
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含trix、trixs列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算TRIX指标
        >>> trix_data = self.data.trix(length=18, signal=9)
        >>> trix, signal = trix_data.trix, trix_data.trixs
        >>> 
        >>> # 使用TRIX识别趋势变化
        >>> def trix_trend_change(self):
        >>>     trix = self.data.trix()
        >>>     # TRIX上穿零轴 - 看涨信号
        >>>     if trix.trix.new > 0 and trix.trix.prev <= 0:
        >>>         return "TRIX看涨信号"
        >>>     # TRIX下穿零轴 - 看跌信号
        >>>     elif trix.trix.new < 0 and trix.trix.prev >= 0:
        >>>         return "TRIX看跌信号"
        """
        ...

    @tobtind(lines=['tsir', 'tsis'], lib='pta')
    def tsi(self, fast=13, slow=25, signal=13, scalar=100., mamode="ema", drift=1, offset=0, **kwargs) -> IndFrame:
        """
        真实强度指数 (True Strength Index, TSI)
        ---------
            动量指标，用于识别趋势方向的短期波动以及确定超买和超卖条件。

        数据来源:
        ---------
            https://www.investopedia.com/terms/t/tsi.asp

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            diff = close.diff(drift)
            slow_ema = EMA(diff, slow)
            fast_slow_ema = EMA(slow_ema, slow)
            abs_diff_slow_ema = absolute_diff_ema = EMA(ABS(diff), slow)
            abema = abs_diff_fast_slow_ema = EMA(abs_diff_slow_ema, fast)
            TSI = scalar * fast_slow_ema / abema
            Signal = EMA(TSI, signal)

        参数:
        ---------
        >>> fast (int): 快周期. 默认: 13
            slow (int): 慢周期. 默认: 25
            signal (int): 信号周期. 默认: 13
            scalar (float): 放大倍数. 默认: 100
            mamode (str): TSI信号线的移动平均模式，参考```help(ta.ma)```. 默认: 'ema'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含tsir、tsis列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算真实强度指数
        >>> tsi_data = self.data.tsi(fast=13, slow=25, signal=13)
        >>> tsi, signal = tsi_data.tsir, tsi_data.tsis
        >>> 
        >>> # 使用TSI识别动量变化
        >>> def tsi_momentum_signals(self):
        >>>     tsi = self.data.tsi()
        >>>     # TSI上穿信号线 - 买入信号
        >>>     if tsi.tsir.new > tsi.tsis.new and tsi.tsir.prev <= tsi.tsis.prev:
        >>>         self.data.buy()
        >>>     # TSI下穿信号线 - 卖出信号
        >>>     elif tsi.tsir.new < tsi.tsis.new and tsi.tsir.prev >= tsi.tsis.prev:
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=None, lib='pta')
    def uo(self, fast=7, medium=14, slow=28, fast_w=4., medium_w=2.,
           slow_w=1., talib=True, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        终极震荡指标 (Ultimate Oscillator, UO)
        ---------
            三个不同周期的动量指标。试图修正错误的背离交易信号。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Ultimate_Oscillator_(UO)

        计算方法:
        ---------
        >>> min_low_or_pc  = close.shift(drift).combine(low, min)
            max_high_or_pc = close.shift(drift).combine(high, max)
            bp = buying pressure = close - min_low_or_pc
            tr = true range = max_high_or_pc - min_low_or_pc
            fast_avg = SUM(bp, fast) / SUM(tr, fast)
            medium_avg = SUM(bp, medium) / SUM(tr, medium)
            slow_avg = SUM(bp, slow) / SUM(tr, slow)
            total_weight = fast_w + medium_w + slow_w
            weights = (fast_w * fast_avg) + (medium_w * medium_avg) + (slow_w * slow_avg)
            UO = 100 * weights / total_weight

        参数:
        ---------
        >>> fast (int): 快速周期. 默认: 7
            medium (int): 中速周期. 默认: 14
            slow (int): 慢速周期. 默认: 28
            fast_w (float): 快速权重. 默认: 4.0
            medium_w (float): 中速权重. 默认: 2.0
            slow_w (float): 慢速权重. 默认: 1.0
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算终极震荡指标
        >>> uo_IndSeries = self.data.uo(fast=7, medium=14, slow=28)
        >>> 
        >>> # 使用UO识别买卖信号
        >>> def uo_trading_signals(self):
        >>>     uo = self.data.uo()
        >>>     # UO > 70 - 超买区域
        >>>     if uo.new > 70:
        >>>         return "UO超买信号"
        >>>     # UO < 30 - 超卖区域
        >>>     elif uo.new < 30:
        >>>         return "UO超卖信号"
        >>>     # UO背离分析
        >>>     if (self.data.close.new > self.data.close.prev and 
        >>>         uo.new < uo.prev):
        >>>         return "UO看跌背离"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def willr(self, length=14, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        威廉指标 (William's Percent R, WILLR)
        ---------
            类似于RSI的动量震荡指标，试图识别超买和超卖条件。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Williams_%25R_(%25R)

        计算方法:
        ---------
        >>> LL = low.rolling(length).min()
            HH = high.rolling(length).max()
            WILLR = 100 * ((close - LL) / (HH - LL) - 1)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算威廉指标
        >>> willr_IndSeries = self.data.willr(length=14)
        >>> 
        >>> # 使用威廉指标识别极端行情
        >>> def willr_extreme_conditions(self):
        >>>     willr = self.data.willr(length=14)
        >>>     # WILLR < -80 - 强烈超卖
        >>>     if willr.new < -80:
        >>>         return "威廉指标强烈超卖"
        >>>     # WILLR > -20 - 强烈超买
        >>>     elif willr.new > -20:
        >>>         return "威廉指标强烈超买"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def alma(self, length=10, sigma=6., distribution_offset=0.85, offset=0, **kwargs) -> IndSeries:
        """
        阿诺德移动平均线 (Arnaud Legoux Moving Average, ALMA)
        ---------
        - 使用正态（高斯）分布曲线的移动平均线。
        - 可以调节指标的平滑度和高灵敏度。

        数据来源:
        ---------
            https://www.prorealcode.com/prorealtime-indicators/alma-arnaud-legoux-moving-average/

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> length (int): 周期，窗口大小. 默认: 10
            sigma (float): 平滑值. 默认: 6.0
            distribution_offset (float): 分布偏移值，最小值0（更平滑），最大值1（更敏感）. 默认: 0.85
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算ALMA移动平均线
        >>> alma_IndSeries = self.data.alma(length=20, sigma=6, distribution_offset=0.85)
        >>> 
        >>> # 使用ALMA作为动态支撑阻力
        >>> def alma_support_resistance(self):
        >>>     alma = self.data.alma(length=20)
        >>>     # 价格上穿ALMA - 看涨信号
        >>>     if self.data.close.new > alma.new and self.data.close.prev <= alma.prev:
        >>>         return "价格上穿ALMA，看涨"
        >>>     # 价格下穿ALMA - 看跌信号
        >>>     elif self.data.close.new < alma.new and self.data.close.prev >= alma.prev:
        >>>         return "价格下穿ALMA，看跌"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def dema(self, length=10, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        双指数移动平均线 (Double Exponential Moving Average, DEMA)
        ---------
            试图提供比普通指数移动平均线(EMA)更平滑且延迟更小的平均值。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/double-exponential-moving-average-dema/

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            ema1 = EMA(close, length)
            ema2 = EMA(ema1, length)
            DEMA = 2 * ema1 - ema2

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算DEMA移动平均线
        >>> dema_IndSeries = self.data.dema(length=20)
        >>> 
        >>> # 使用DEMA识别趋势
        >>> def dema_trend_identification(self):
        >>>     dema = self.data.dema(length=20)
        >>>     # 价格在DEMA上方 - 上升趋势
        >>>     if self.data.close.new > dema.new:
        >>>         return "价格在DEMA上方，上升趋势"
        >>>     # 价格在DEMA下方 - 下降趋势
        >>>     elif self.data.close.new < dema.new:
        >>>         return "价格在DEMA下方，下降趋势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def ema(self, length=10, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        指数移动平均线 (Exponential Moving Average, EMA)
        ---------
            与简单移动平均线(SMA)相比更敏感的移动平均线。

        数据来源:
        ---------
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:moving_averages
        - https://www.investopedia.com/ask/answers/122314/what-exponential-moving-average-ema-formula-and-how-ema-calculated.asp

        计算方法:
        ---------
        >>> if sma:
                sma_nth = close[0:length].sum() / length
                close[:length - 1] = np.NaN
                close.iloc[length - 1] = sma_nth
            EMA = close.ewm(span=length, adjust=adjust).mean()

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool, 可选): 默认: False
            sma (bool, 可选): 如果为True，使用SMA作为初始值. 默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算EMA移动平均线
        >>> ema_IndSeries = self.data.ema(length=20)
        >>> 
        >>> # 使用EMA交叉策略
        >>> def ema_crossover_strategy(self):
        >>>     ema_fast = self.data.ema(length=10)
        >>>     ema_slow = self.data.ema(length=20)
        >>>     # 快速EMA上穿慢速EMA - 金叉买入
        >>>     if (ema_fast.new > ema_slow.new and 
        >>>         ema_fast.prev <= ema_slow.prev):
        >>>         self.data.buy()
        >>>     # 快速EMA下穿慢速EMA - 死叉卖出
        >>>     elif (ema_fast.new < ema_slow.new and 
        >>>           ema_fast.prev >= ema_slow.prev):
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def fwma(self, length=10, asc=True, offset=0, **kwargs) -> IndSeries:
        """
        斐波那契加权移动平均线 (Fibonacci's Weighted Moving Average, FWMA)
        ---------
            类似于加权移动平均线(WMA)，权重基于斐波那契数列。

        数据来源:
        ---------
            Kevin Johnson

        计算方法:
        ---------
        >>> fibs = utils.fibonacci(length - 1)
            FWMA = close.rolling(length)_.apply(weights(fibs), raw=True)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            asc (bool): 近期值权重更大. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算斐波那契加权移动平均线
        >>> fwma_IndSeries = self.data.fwma(length=13)
        >>> 
        >>> # 使用FWMA作为动态支撑位
        >>> def fwma_support_level(self):
        >>>     fwma = self.data.fwma(length=13)
        >>>     # 价格回踩FWMA获得支撑 - 买入机会
        >>>     if (abs(self.data.close.new - fwma.new) / fwma.new < 0.01 and 
        >>>         self.data.close.new > fwma.new):
        >>>         return "价格在FWMA获得支撑"
        """
        ...

    @tobtind(lines=['hilo', 'hilol', 'hilos'], overlap=True, lib='pta')
    def hilo(self, high_length=13, low_length=21, mamode="sma", offset=0, **kwargs) -> IndFrame:
        """
        甘氏高低激活器 (Gann HiLo Activator)
        ---------
        - Robert Krausz在1998年创建。
        - 基于移动平均线的趋势指标，由两个不同的简单移动平均线组成。

        数据来源:
        ---------
        - https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=447&Name=Gann_HiLo_Activator
        - https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/simple-moving-average-sma/
        - https://www.tradingview.com/script/XNQSLIYb-Gann-High-Low/

        计算方法:
        ---------
        >>> if "ema":
                high_ma = EMA(high, high_length)
                low_ma = EMA(low, low_length)
            elif "hma":
                high_ma = HMA(high, high_length)
                low_ma = HMA(low, low_length)
            else: # "sma"
                high_ma = SMA(high, high_length)
                low_ma = SMA(low, low_length)
            hilo = Series(np.nan, index=close.index)
            for i in range(1, m):
                if close.iloc[i] > high_ma.iloc[i - 1]:
                    hilo.iloc[i] = low_ma.iloc[i]
                elif close.iloc[i] < low_ma.iloc[i - 1]:
                    hilo.iloc[i] = high_ma.iloc[i]
                else:
                    hilo.iloc[i] = hilo.iloc[i - 1]

        参数:
        ---------
        >>> high_length (int): 高点周期. 默认: 13
            low_length (int): 低点周期. 默认: 21
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool): 默认: True
            presma (bool, 可选): 如果为True，使用SMA作为初始值
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含hilo、hilol、hilos列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算甘氏高低激活器
        >>> hilo_data = self.data.hilo(high_length=13, low_length=21)
        >>> hilo, hilol, hilos = hilo_data.hilo, hilo_data.hilol, hilo_data.hilos
        >>> 
        >>> # 使用HiLo识别趋势转换
        >>> def hilo_trend_change(self):
        >>>     hilo = self.data.hilo()
        >>>     # HiLo从上升转为下降 - 趋势转弱
        >>>     if hilo.hilo.new < hilo.hilo.prev and hilo.hilo.prev >= hilo.hilo.sndprev:
        >>>         return "HiLo趋势转弱"
        >>>     # HiLo从下降转为上升 - 趋势转强
        >>>     elif hilo.hilo.new > hilo.hilo.prev and hilo.hilo.prev <= hilo.hilo.sndprev:
        >>>         return "HiLo趋势转强"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def hl2(self, offset=0, **kwargs) -> IndSeries:
        """
        高低中点指标 (HL2)
        ---------
            最高价和最低价的平均值。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算高低中点
        >>> hl2_IndSeries = self.data.hl2()
        >>> 
        >>> # 使用HL2作为参考价格
        >>> def hl2_reference_price(self):
        >>>     hl2 = self.data.hl2()
        >>>     # 收盘价高于HL2 - 偏强势
        >>>     if self.data.close.new > hl2.new:
        >>>         return "价格偏强势"
        >>>     # 收盘价低于HL2 - 偏弱势
        >>>     elif self.data.close.new < hl2.new:
        >>>         return "价格偏弱势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def hlc3(self, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        典型价格指标 (HLC3)
        ---------
            最高价、最低价和收盘价的平均值。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算典型价格
        >>> hlc3_IndSeries = self.data.hlc3()
        >>> 
        >>> # 使用HLC3计算其他指标
        >>> def hlc3_based_indicators(self):
        >>>     hlc3 = self.data.hlc3()
        >>>     # 使用HLC3计算移动平均线
        >>>     hlc3_ma = hlc3.ema(period=20)
        >>>     return hlc3_ma
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def hma(self, length=10, offset=0, **kwargs) -> IndSeries:
        """
        赫尔移动平均线 (Hull Moving Average, HMA)
        ---------
            试图减少或消除移动平均线中的延迟。

        数据来源:
        ---------
            https://alanhull.com/hull-moving-average

        计算方法:
        ---------
        >>> WMA = Weighted Moving Average
            half_length = int(0.5 * length)
            sqrt_length = int(sqrt(length))
            wmaf = WMA(close, half_length)
            wmas = WMA(close, length)
            HMA = WMA(2 * wmaf - wmas, sqrt_length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算赫尔移动平均线
        >>> hma_IndSeries = self.data.hma(length=20)
        >>> 
        >>> # 使用HMA识别快速趋势变化
        >>> def hma_fast_trend(self):
        >>>     hma = self.data.hma(length=20)
        >>>     # HMA快速上升 - 强烈上升趋势
        >>>     if hma.new > hma.prev > hma.sndprev:
        >>>         return "HMA强烈上升趋势"
        >>>     # HMA快速下降 - 强烈下降趋势
        >>>     elif hma.new < hma.prev < hma.sndprev:
        >>>         return "HMA强烈下降趋势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def hwma(self, na=0.2, nb=0.1, nc=0.1, offset=0, **kwargs) -> IndSeries:
        """
        霍尔特-温特斯移动平均线 (Holt-Winter Moving Average, HWMA)
        ---------
            霍尔特-温特斯方法的三参数移动平均线，三个参数应选择以获得预测。

        数据来源:
        ---------
            https://www.mql5.com/en/code/20856

        计算方法:
        ---------
        >>> HWMA[i] = F[i] + V[i] + 0.5 * A[i]
            where..
            F[i] = (1-na) * (F[i-1] + V[i-1] + 0.5 * A[i-1]) + na * Price[i]
            V[i] = (1-nb) * (V[i-1] + A[i-1]) + nb * (F[i] - F[i-1])
            A[i] = (1-nc) * A[i-1] + nc * (V[i] - V[i-1])

        参数:
        ---------
        >>> na (float): 平滑序列参数 (0到1). 默认: 0.2
            nb (float): 趋势参数 (0到1). 默认: 0.1
            nc (float): 季节性参数 (0到1). 默认: 0.1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算霍尔特-温特斯移动平均线
        >>> hwma_IndSeries = self.data.hwma(na=0.2, nb=0.1, nc=0.1)
        >>> 
        >>> # 使用HWMA进行价格预测
        >>> def hwma_price_forecast(self):
        >>>     hwma = self.data.hwma(na=0.2, nb=0.1, nc=0.1)
        >>>     # HWMA向上 - 看涨预测
        >>>     if hwma.new > hwma.prev:
        >>>         return "HWMA看涨预测"
        >>>     # HWMA向下 - 看跌预测
        >>>     elif hwma.new < hwma.prev:
        >>>         return "HWMA看跌预测"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def jma(self, length=7, phase=0., offset=0, **kwargs) -> IndSeries:
        """
        茹拉克移动平均线 (Jurik Moving Average, JMA)
        ---------
        - 试图消除噪音以看到"真实"的基础活动。
        - 具有极低的延迟，非常平滑，对市场缺口反应灵敏。

        数据来源:
        ---------
        - https://c.mql5.com/forextsd/forum/164/jurik_1.pdf
        - https://www.prorealcode.com/prorealtime-indicators/jurik-volatility-bands/

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 7
            phase (float): 平均值轻重程度 [-100, 100]. 默认: 0
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算茹拉克移动平均线
        >>> jma_IndSeries = self.data.jma(length=7, phase=0)
        >>> 
        >>> # 使用JMA识别低延迟趋势
        >>> def jma_low_lag_trend(self):
        >>>     jma = self.data.jma(length=7, phase=0)
        >>>     # JMA快速响应价格变化
        >>>     jma_ma = jma.ema(period=5)
        >>>     # JMA上穿其移动平均线 - 快速买入信号
        >>>     if jma.new > jma_ma.new and jma.prev <= jma_ma.prev:
        >>>         return "JMA快速买入信号"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def kama(self, length=10, fast=2, slow=30, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        考夫曼自适应移动平均线 (Kaufman's Adaptive Moving Average, KAMA)
        ---------
        - Perry Kaufman开发，旨在考虑市场噪音或波动性。
        - 当价格波动相对较小且噪音较低时，KAMA将紧密跟随价格。
        - 当价格波动扩大时，KAMA将调整并以更大距离跟随价格。

        数据来源:
        ---------
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:kaufman_s_adaptive_moving_average
        - https://www.tradingview.com/script/wZGOIz9r-REPOST-Indicators-3-Different-Adaptive-Moving-Averages/

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            fast (int): 快速移动平均周期. 默认: 2
            slow (int): 慢速移动平均周期. 默认: 30
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算考夫曼自适应移动平均线
        >>> kama_IndSeries = self.data.kama(length=10, fast=2, slow=30)
        >>> 
        >>> # 使用KAMA适应不同市场环境
        >>> def kama_adaptive_strategy(self):
        >>>     kama = self.data.kama(length=10)
        >>>     # 价格在KAMA上方 - 上升趋势
        >>>     if self.data.close.new > kama.new:
        >>>         return "KAMA上升趋势"
        >>>     # 价格在KAMA下方 - 下降趋势
        >>>     elif self.data.close.new < kama.new:
        >>>         return "KAMA下降趋势"
        """
        ...

    @tobtind(lines=['spana', 'spanb', 'tenkan_sen', 'kijun_sen', 'chikou_span'], overlap=True, lib="pta")
    def ichimoku(self, tenkan=9, kijun=26, senkou=52, include_chikou=True, offset=0, **kwargs) -> IndFrame:
        """
        一目均衡表 (Ichimoku Kinkō Hyō)
        ---------
            二战前开发作为金融市场的预测模型。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/ichimoku-ich/

        计算方法:
        ---------
        >>> MIDPRICE = Midprice
            TENKAN_SEN = MIDPRICE(high, low, close, length=tenkan)
            KIJUN_SEN = MIDPRICE(high, low, close, length=kijun)
            CHIKOU_SPAN = close.shift(-kijun)
            SPAN_A = 0.5 * (TENKAN_SEN + KIJUN_SEN)
            SPAN_A = SPAN_A.shift(kijun)
            SPAN_B = MIDPRICE(high, low, close, length=senkou)
            SPAN_B = SPAN_B.shift(kijun)

        参数:
        ---------
        >>> tenkan (int): 转换线周期. 默认: 9
            kijun (int): 基准线周期. 默认: 26
            senkou (int): 先行带周期. 默认: 52
            include_chikou (bool): 是否包含迟行带. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含spana、spanb、tenkan_sen、kijun_sen、chikou_span列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算一目均衡表
        >>> ichimoku_data = self.data.ichimoku(tenkan=9, kijun=26, senkou=52)
        >>> span_a, span_b, tenkan, kijun, chikou = ichimoku_data.spana, ichimoku_data.spanb, ichimoku_data.tenkan_sen, ichimoku_data.kijun_sen, ichimoku_data.chikou_span
        >>> 
        >>> # 使用一目均衡表识别趋势和信号
        >>> def ichimoku_trading_signals(self):
        >>>     ichi = self.data.ichimoku()
        >>>     # 价格在云层上方 - 看涨
        >>>     if (self.data.close.new > ichi.spana.new and 
        >>>         self.data.close.new > ichi.spanb.new):
        >>>         return "价格在云层上方，看涨"
        >>>     # 转换线上穿基准线 - 金叉买入
        >>>     if (ichi.tenkan_sen.new > ichi.kijun_sen.new and 
        >>>         ichi.tenkan_sen.prev <= ichi.kijun_sen.prev):
        >>>         return "转换线金叉，买入信号"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def linreg(self, length=10, offset=0, **kwargs) -> IndSeries:
        """
        线性回归移动平均线 (Linear Regression Moving Average, LINREG)
        ---------
            标准线性回归的简化版本。LINREG是单变量的滚动回归。

        数据来源:
        ---------
            TA Lib

        计算方法:
        ---------
        >>> x = [1, 2, ..., n]
            x_sum = 0.5 * length * (length + 1)
            x2_sum = length * (length + 1) * (2 * length + 1) / 6
            divisor = length * x2_sum - x_sum * x_sum
            lr(IndSeries):
                y_sum = IndSeries.sum()
                y2_sum = (IndSeries* IndSeries).sum()
                xy_sum = (x * IndSeries).sum()
                m = (length * xy_sum - x_sum * y_sum) / divisor
                b = (y_sum * x2_sum - x_sum * xy_sum) / divisor
                return m * (length - 1) + b
            linreg = close.rolling(length).apply(lr)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> angle (bool, 可选): 如果为True，返回斜率的弧度角度. 默认: False
            degrees (bool, 可选): 如果为True，返回斜率的度数角度. 默认: False
            intercept (bool, 可选): 如果为True，返回截距. 默认: False
            r (bool, 可选): 如果为True，返回相关系数'r'. 默认: False
            slope (bool, 可选): 如果为True，返回斜率. 默认: False
            tsf (bool, 可选): 如果为True，返回时间序列预测值. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算线性回归移动平均线
        >>> linreg_IndSeries = self.data.linreg(length=20)
        >>> 
        >>> # 使用线性回归预测价格
        >>> def linreg_price_prediction(self):
        >>>     linreg = self.data.linreg(length=20, tsf=True)
        >>>     # 线性回归向上 - 看涨预期
        >>>     if linreg.new > linreg.prev:
        >>>         return "线性回归看涨预期"
        >>>     # 线性回归向下 - 看跌预期
        >>>     elif linreg.new < linreg.prev:
        >>>         return "线性回归看跌预期"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def mcgd(self, length=10, offset=0, c=1., **kwargs) -> IndSeries:
        """
        麦金利动态指标 (McGinley Dynamic Indicator)
        ---------
        - 看起来像移动平均线，但实际上是一种价格平滑机制，
        - 可最大限度地减少价格分离、价格锯齿，并更紧密地贴合价格。

        数据来源:
        ---------
            https://www.investopedia.com/articles/forex/09/mcginley-dynamic-indicator.asp

        计算方法:
        ---------
        >>> def mcg_(IndSeries):
                denom = (constant * length * (IndSeries.iloc[1] / IndSeries.iloc[0]) ** 4)
                IndSeries.iloc[1] = (
                    IndSeries.iloc[0] + ((IndSeries.iloc[1] - IndSeries.iloc[0]) / denom))
                return IndSeries.iloc[1]
            mcg_cell = close[0:].rolling(2, min_periods=2).apply(mcg_, raw=False)
            mcg_ds = close[:1].append(mcg_cell[1:])

        参数:
        ---------
        >>> length (int): 指标周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0
            c (float): 分母乘数，有时设置为0.6. 默认: 1

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算麦金利动态指标
        >>> mcgd_IndSeries = self.data.mcgd(length=10, c=1)
        >>> 
        >>> # 使用麦金利动态指标识别平滑趋势
        >>> def mcgd_smooth_trend(self):
        >>>     mcgd = self.data.mcgd(length=10)
        >>>     # 麦金利动态指标平滑跟随价格
        >>>     if abs(mcgd.new - self.data.close.new) / self.data.close.new < 0.01:
        >>>         return "麦金利紧密跟随价格"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def midpoint(self, length=2, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        中点指标 (Midpoint)
        ---------
            价格范围的中点。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算价格中点
        >>> midpoint_IndSeries = self.data.midpoint(length=2)
        >>> 
        >>> # 使用中点作为参考水平
        >>> def midpoint_reference(self):
        >>>     midpoint = self.data.midpoint(length=2)
        >>>     return f"当前价格中点: {midpoint.new}"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def midprice(self, length=2, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        中间价格指标 (Midprice)
        ---------
            最高价和最低价的中间点。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算中间价格
        >>> midprice_IndSeries = self.data.midprice(length=2)
        >>> 
        >>> # 使用中间价格分析市场平衡点
        >>> def midprice_balance_point(self):
        >>>     midprice = self.data.midprice(length=2)
        >>>     # 收盘价接近中间价格 - 市场平衡
        >>>     if abs(self.data.close.new - midprice.new) / midprice.new < 0.005:
        >>>         return "市场处于平衡状态"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def ohlc4(self, offset=0, **kwargs) -> IndSeries:
        """
        OHLC4指标
        ---------
            开盘价、最高价、最低价和收盘价的平均值。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算OHLC4
        >>> ohlc4_IndSeries = self.data.ohlc4()
        >>> 
        >>> # 使用OHLC4作为综合价格参考
        >>> def ohlc4_comprehensive_price(self):
        >>>     ohlc4 = self.data.ohlc4()
        >>>     return f"综合价格水平: {ohlc4.new}"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def pwma(self, length=10, asc=True, offset=0, **kwargs) -> IndSeries:
        """
        帕斯卡加权移动平均线 (Pascal's Weighted Moving Average, PWMA)
        ---------
            类似于对称三角窗口，但PWMA的权重基于帕斯卡三角形。

        数据来源:
        ---------
            Kevin Johnson

        计算方法:
        ---------
        >>> def weights(w):
                def _compute(x):
                    return np.dot(w * x)
                return _compute
            triangle = utils.pascals_triangle(length + 1)
            PWMA = close.rolling(length)_.apply(weights(triangle), raw=True)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            asc (bool): 近期值权重更大. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算帕斯卡加权移动平均线
        >>> pwma_IndSeries = self.data.pwma(length=10)
        >>> 
        >>> # 使用PWMA作为平滑趋势线
        >>> def pwma_smooth_trend(self):
        >>>     pwma = self.data.pwma(length=10)
        >>>     # PWMA连续上升 - 稳定上升趋势
        >>>     if pwma.new > pwma.prev > pwma.sndprev:
        >>>         return "PWMA稳定上升趋势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def ma(self, name: str = "sma", length: int = 10, **kwargs) -> IndSeries:
        """
        移动平均线工具函数 (MA Utility)
        ---------
            简化的移动平均线选择工具函数。

        可用移动平均线类型:
        >>> dema, ema, fwma, hma, linreg, midpoint, pwma, rma,
            sinwma, sma, swma, t3, tema, trima, vidya, wma, zlma

        参数:
        ---------
        >>> name (str): 移动平均线类型名称. 默认: "sma"
            length (int): 周期. 默认: 10

        可选参数:
        ---------
        >>> 所选移动平均线类型可能需要的任何额外参数

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 使用MA工具函数计算不同移动平均线
        >>> ema8 = self.data.ma("ema", length=8)
        >>> sma50 = self.data.ma("sma", length=50)
        >>> pwma10 = self.data.ma("pwma", length=10, asc=False)
        >>> 
        >>> # 多移动平均线策略
        >>> def multi_ma_strategy(self):
        >>>     ema_fast = self.data.ma("ema", length=10)
        >>>     ema_slow = self.data.ma("ema", length=20)
        >>>     sma_long = self.data.ma("sma", length=50)
        >>>     
        >>>     # 多移动平均线多头排列
        >>>     if (ema_fast.new > ema_slow.new > sma_long.new and
        >>>         ema_fast.prev <= ema_slow.prev <= sma_long.prev):
        >>>         return "多头排列形成"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def rma(self, length=10, offset=0, **kwargs) -> IndSeries:
        """
        怀尔德移动平均线 (wildeR's Moving Average, RMA)
        ---------
            简单来说就是修改了alpha = 1 / length的指数移动平均线(EMA)。

        数据来源:
        ---------
        - https://tlc.thinkorswim.com/center/reference/Tech-Indicators/studies-library/V-Z/WildersSmoothing
        - https://www.incrediblecharts.com/indicators/wilder_moving_average.php

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            alpha = 1 / length
            RMA = EMA(close, alpha=alpha)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算怀尔德移动平均线
        >>> rma_IndSeries = self.data.rma(length=14)
        >>> 
        >>> # 使用RMA作为RSI计算的基础
        >>> def rsi_based_on_rma(self):
        >>>     rma = self.data.rma(length=14)
        >>>     # RMA通常用于RSI计算
        >>>     return "RMA常用于RSI指标计算"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def sinwma(self, length=10, offset=0, **kwargs) -> IndSeries:
        """
        正弦加权移动平均线 (Sine Weighted Moving Average, SWMA)
        ---------
            使用正弦周期的加权平均。平均值的中项具有最高的权重。

        数据来源:
        ---------
        - https://www.tradingview.com/script/6MWFvnPO-Sine-Weighted-Moving-Average/
        - 作者: Everget (https://www.tradingview.com/u/everget/)

        计算方法:
        ---------
        >>> def weights(w):
                def _compute(x):
                    return np.dot(w * x)
                return _compute
            sines = Series([sin((i + 1) * pi / (length + 1))
                           for i in range(0, length)])
            w = sines / sines.sum()
            SINWMA = close.rolling(length, min_periods=length).apply(
                weights(w), raw=True)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算正弦加权移动平均线
        >>> sinwma_IndSeries = self.data.sinwma(length=10)
        >>> 
        >>> # 使用正弦加权移动平均线
        >>> def sinwma_smooth_trend(self):
        >>>     sinwma = self.data.sinwma(length=10)
        >>>     # 正弦加权移动平均线提供平滑的趋势线
        >>>     return f"正弦加权移动平均: {sinwma.new}"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def sma(self, length=10, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        简单移动平均线 (Simple Moving Average, SMA)
        ---------
            经典的移动平均线，是n个周期内等权重的平均值。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/simple-moving-average-sma/

        计算方法:
        ---------
        >>> SMA = SUM(close, length) / length

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool): 默认: True
            presma (bool, 可选): 如果为True，使用SMA作为初始值
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算简单移动平均线
        >>> sma_IndSeries = self.data.sma(length=20)
        >>> 
        >>> # 使用SMA识别基本趋势
        >>> def sma_basic_trend(self):
        >>>     sma = self.data.sma(length=20)
        >>>     # 价格在SMA上方 - 基本上升趋势
        >>>     if self.data.close.new > sma.new:
        >>>         return "价格在SMA上方，上升趋势"
        >>>     # 价格在SMA下方 - 基本下降趋势
        >>>     elif self.data.close.new < sma.new:
        >>>         return "价格在SMA下方，下降趋势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def ssf(self, length=10, poles=2, offset=0, **kwargs) -> IndSeries:
        """
        埃勒超级平滑滤波器 (Ehler's Super Smoother Filter, SSF) © 2013
        ---------
            John F. Ehlers的解决方案，旨在减少延迟并消除航空航天模拟滤波器设计研究中的混叠噪声。

        数据来源:
        ---------
        - http://www.stockspotter.com/files/PredictiveIndicators.pdf
        - https://www.tradingview.com/script/VdJy0yBJ-Ehlers-Super-Smoother-Filter/
        - https://www.mql5.com/en/code/588
        - https://www.mql5.com/en/code/589

        计算方法:
        ---------
            参考源代码或上述数据来源

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            poles (int): 使用的极点数，2或3. 默认: 2
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算埃勒超级平滑滤波器
        >>> ssf_IndSeries = self.data.ssf(length=10, poles=2)
        >>> 
        >>> # 使用SSF进行噪声过滤
        >>> def ssf_noise_filtering(self):
        >>>     ssf = self.data.ssf(length=10, poles=2)
        >>>     # SSF提供平滑的价格序列
        >>>     return "SSF有效过滤市场噪声"
        """
        ...

    @tobtind(lines=['trend', 'dir', 'long', 'short'], overlap=dict(trend=False, dir=False, long=True, short=True), lib='pta')
    def supertrend(self, length=7, multiplier=3., offset=0, **kwargs) -> IndFrame:
        """
        超级趋势指标 (Supertrend)
        ---------
            重叠指标。用于帮助识别趋势方向、设置止损、识别支撑和阻力、和/或生成买卖信号。

        数据来源:
        ---------
            http://www.freebsensetips.com/blog/detail/7/What-is-supertrend-indicator-its-calculation

        计算方法:
        ---------
        >>> MID = multiplier * ATR
            LOWERBAND = HL2 - MID
            UPPERBAND = HL2 + MID
            if UPPERBAND[i] < FINAL_UPPERBAND[i-1] and close[i-1] > FINAL_UPPERBAND[i-1]:
                FINAL_UPPERBAND[i] = UPPERBAND[i]
            else:
                FINAL_UPPERBAND[i] = FINAL_UPPERBAND[i-1])
            if LOWERBAND[i] > FINAL_LOWERBAND[i-1] and close[i-1] < FINAL_LOWERBAND[i-1]:
                FINAL_LOWERBAND[i] = LOWERBAND[i]
            else:
                FINAL_LOWERBAND[i] = FINAL_LOWERBAND[i-1])
            if close[i] <= FINAL_UPPERBAND[i]:
                SUPERTREND[i] = FINAL_UPPERBAND[i]
            else:
                SUPERTREND[i] = FINAL_LOWERBAND[i]

        参数:
        ---------
        >>> length (int): ATR计算周期. 默认: 7
            multiplier (float): 上下带到中距离的系数. 默认: 3.0
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含trend、dir、long、short列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算超级趋势指标
        >>> supertrend_data = self.data.supertrend(length=7, multiplier=3)
        >>> trend, direction, long, short = supertrend_data.trend, supertrend_data.dir, supertrend_data.long, supertrend_data.short
        >>> 
        >>> # 使用超级趋势识别趋势方向
        >>> def supertrend_trend_direction(self):
        >>>     st = self.data.supertrend()
        >>>     # 趋势方向为1 - 上升趋势
        >>>     if st.dir.new == 1:
        >>>         return "超级趋势显示上升趋势"
        >>>     # 趋势方向为-1 - 下降趋势
        >>>     elif st.dir.new == -1:
        >>>         return "超级趋势显示下降趋势"
        >>> 
        >>> # 使用超级趋势生成交易信号
        >>> def supertrend_trading_signals(self):
        >>>     st = self.data.supertrend()
        >>>     # 趋势从下降转为上升 - 买入信号
        >>>     if st.dir.new == 1 and st.dir.prev == -1:
        >>>         self.data.buy()
        >>>     # 趋势从上升转为下降 - 卖出信号
        >>>     elif st.dir.new == -1 and st.dir.prev == 1:
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def swma(self, length=10, asc=True, offset=0, **kwargs) -> IndSeries:
        """
        对称加权移动平均线 (Symmetric Weighted Moving Average, SWMA)
        ---------
            权重基于对称三角形的加权移动平均线。

        数据来源:
        ---------
            https://www.tradingview.com/study-script-reference/#fun_swma

        计算方法:
        ---------
        >>> def weights(w):
                def _compute(x):
                    return np.dot(w * x)
                return _compute
            triangle = utils.symmetric_triangle(length - 1)
            SWMA = close.rolling(length)_.apply(weights(triangle), raw=True)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            asc (bool): 近期值权重更大. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算对称加权移动平均线
        >>> swma_IndSeries = self.data.swma(length=10)
        >>> 
        >>> # 使用SWMA作为趋势确认
        >>> def swma_trend_confirmation(self):
        >>>     swma = self.data.swma(length=10)
        >>>     # SWMA连续上升 - 确认上升趋势
        >>>     if swma.new > swma.prev > swma.sndprev:
        >>>         return "SWMA确认上升趋势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def t3(self, length=10, a=0.7, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        蒂姆·蒂尔森T3移动平均线 (Tim Tillson's T3 Moving Average)
        ---------
            被认为相对于其他移动平均线更平滑且响应更快的移动平均线。

        数据来源:
        ---------
            http://www.binarytribune.com/forex-trading-indicators/t3-moving-average-indicator/

        计算方法:
        ---------
        >>> c1 = -a^3
            c2 = 3a^2 + 3a^3 = 3a^2 * (1 + a)
            c3 = -6a^2 - 3a - 3a^3
            c4 = a^3 + 3a^2 + 3a + 1
            ema1 = EMA(close, length)
            ema2 = EMA(ema1, length)
            ema3 = EMA(ema2, length)
            ema4 = EMA(ema3, length)
            ema5 = EMA(ema4, length)
            ema6 = EMA(ema5, length)
            T3 = c1 * ema6 + c2 * ema5 + c3 * ema4 + c4 * ema3

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            a (float): 0 < a < 1. 默认: 0.7
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool): 默认: True
            presma (bool, 可选): 如果为True，使用SMA作为初始值
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算T3移动平均线
        >>> t3_IndSeries = self.data.t3(length=10, a=0.7)
        >>> 
        >>> # 使用T3识别平滑趋势
        >>> def t3_smooth_trend(self):
        >>>     t3 = self.data.t3(length=10)
        >>>     # T3提供平滑且响应迅速的趋势线
        >>>     return "T3移动平均线平滑且响应迅速"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def tema(self, length=10, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        三重指数移动平均线 (Triple Exponential Moving Average, TEMA)
        ---------
            延迟较小的指数移动平均线。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/triple-exponential-moving-average-tema/

        计算方法:
        ---------
        >>> EMA = Exponential Moving Average
            ema1 = EMA(close, length)
            ema2 = EMA(ema1, length)
            ema3 = EMA(ema2, length)
            TEMA = 3 * (ema1 - ema2) + ema3

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool): 默认: True
            presma (bool, 可选): 如果为True，使用SMA作为初始值
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算三重指数移动平均线
        >>> tema_IndSeries = self.data.tema(length=10)
        >>> 
        >>> # 使用TEMA减少延迟
        >>> def tema_low_lag(self):
        >>>     tema = self.data.tema(length=10)
        >>>     ema = self.data.ema(length=10)
        >>>     # TEMA比EMA响应更快
        >>>     return "TEMA比传统EMA延迟更小"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def trima(self, length=10, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        三角移动平均线 (Triangular Moving Average, TRIMA)
        ---------
            权重形状为三角形且最大权重在周期中间的加权移动平均线。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/triangular-moving-average-trima/

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            half_length = round(0.5 * (length + 1))
            SMA1 = SMA(close, half_length)
            TRIMA = SMA(SMA1, half_length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool): 默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算三角移动平均线
        >>> trima_IndSeries = self.data.trima(length=10)
        >>> 
        >>> # 使用TRIMA作为平滑趋势线
        >>> def trima_smooth_trend(self):
        >>>     trima = self.data.trima(length=20)
        >>>     # TRIMA提供非常平滑的趋势线
        >>>     return "TRIMA提供平滑的趋势视图"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def vidya(self, length=14, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        可变指数动态平均线 (Variable Index Dynamic Average, VIDYA)
        ---------
        - Tushar Chande开发。类似于指数移动平均线，但具有动态调整的回溯周期，
        - 依赖于通过钱德动量震荡指标(CMO)衡量的相对价格波动性。
        - 当波动性高时，VIDYA对价格变化反应更快。

        数据来源:
        ---------
        - https://www.tradingview.com/script/hdrf0fXV-Variable-Index-Dynamic-Average-VIDYA/
        - https://www.perfecttrendsystem.com/blog_mt4_2/en/vidya-indicator-for-mt4

        计算方法:
        ---------
        >>> if sma:
                sma_nth = close[0:length].sum() / length
                close[:length - 1] = np.NaN
                close.iloc[length - 1] = sma_nth
            EMA = close.ewm(span=length, adjust=adjust).mean()

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> adjust (bool, 可选): 使用EMA计算的调整选项. 默认: False
            sma (bool, 可选): 如果为True，使用SMA作为EMA计算的初始值. 默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算可变指数动态平均线
        >>> vidya_IndSeries = self.data.vidya(length=14)
        >>> 
        >>> # 使用VIDYA适应市场波动性
        >>> def vidya_volatility_adaptive(self):
        >>>     vidya = self.data.vidya(length=14)
        >>>     # VIDYA在高波动市场中反应更快
        >>>     return "VIDYA根据波动性自动调整灵敏度"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def __vwap(self, anchor="D", offset=0, **kwargs) -> IndSeries:
        """ 指标针对日线数据，成交量索引为日期类型，不适合低频交易，已移除
        成交量加权平均价格 (Volume Weighted Average Price, VWAP)
        ---------
        - 通过成交量衡量的平均典型价格。
        - 通常与日内图表一起使用以识别总体方向。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Volume_Weighted_Average_Price_(VWAP)
        - https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/volume-weighted-average-price-vwap/
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:vwap_intraday

        计算方法:
        ---------
        >>> tp = typical_price = hlc3(high, low, close)
            tpv = tp * volume
            VWAP = tpv.cumsum() / volume.cumsum()

        参数:
        ---------
        >>> anchor (str): VWAP锚定方式。根据索引值，将实现各种时间序列偏移别名.
                    默认: "D" (日线)
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close, volume

        使用案例:
        ---------
        >>> # 计算VWAP
        >>> vwap_IndSeries = self.data.vwap(anchor="D")
        >>> 
        >>> # 使用VWAP识别日内趋势
        >>> def vwap_intraday_trend(self):
        >>>     vwap = self.data.vwap(anchor="D")
        >>>     # 价格在VWAP上方 - 日内偏强
        >>>     if self.data.close.new > vwap.new:
        >>>         return "价格在VWAP上方，日内偏强"
        >>>     # 价格在VWAP下方 - 日内偏弱
        >>>     elif self.data.close.new < vwap.new:
        >>>         return "价格在VWAP下方，日内偏弱"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def vwma(self, length=10, offset=0, **kwargs) -> IndSeries:
        """
        成交量加权移动平均线 (Volume Weighted Moving Average, VWMA)
        ---------
            成交量加权的移动平均线。

        数据来源:
        ---------
            https://www.motivewave.com/studies/volume_weighted_moving_average.htm

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            pv = close * volume
            VWMA = SMA(pv, length) / SMA(volume, length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算成交量加权移动平均线
        >>> vwma_IndSeries = self.data.vwma(length=20)
        >>> 
        >>> # 使用VWMA确认成交量支撑
        >>> def vwma_volume_confirmation(self):
        >>>     vwma = self.data.vwma(length=20)
        >>>     # 价格上涨且成交量放大 - 量价配合良好
        >>>     if (self.data.close.new > vwma.new and 
        >>>         self.data.volume.new > self.data.volume.ema(period=20).new):
        >>>         return "量价配合良好，趋势健康"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def wcp(self, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        加权收盘价 (Weighted Closing Price, WCP)
        ---------
            给定最高价、最低价和双倍收盘价的加权价格。

        数据来源:
        ---------
            https://www.fmlabs.com/reference/default.htm?url=WeightedCloses.htm

        计算方法:
        ---------
        >>> WCP = (2 * close + high + low) / 4

        参数:
        ---------
        >>> offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算加权收盘价
        >>> wcp_IndSeries = self.data.wcp()
        >>> 
        >>> # 使用WCP作为更准确的价格代表
        >>> def wcp_accurate_price(self):
        >>>     wcp = self.data.wcp()
        >>>     # WCP比简单收盘价更能代表当日交易区间
        >>>     return f"加权收盘价: {wcp.new}"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def wma(self, length=10, asc=True, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        加权移动平均线 (Weighted Moving Average, WMA)
        ---------
            权重线性增加且最新数据具有最大权重的加权移动平均线。

        数据来源:
        ---------
            https://en.wikipedia.org/wiki/Moving_average#Weighted_moving_average

        计算方法:
        ---------
        >>> total_weight = 0.5 * length * (length + 1)
            weights_ = [1, 2, ..., length + 1]  # 升序
            weights = weights if asc else weights[::-1]
            def linear_weights(w):
                def _compute(x):
                    return (w * x).sum() / total_weight
                return _compute
            WMA = close.rolling(length)_.apply(linear_weights(weights), raw=True)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            asc (bool): 近期值权重更大. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算加权移动平均线
        >>> wma_IndSeries = self.data.wma(length=20)
        >>> 
        >>> # 使用WMA作为趋势确认
        >>> def wma_trend_confirmation(self):
        >>>     wma = self.data.wma(length=20)
        >>>     # WMA对近期价格更敏感
        >>>     if wma.new > wma.prev:
        >>>         return "WMA显示上升趋势"
        >>>     elif wma.new < wma.prev:
        >>>         return "WMA显示下降趋势"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def zlma(self, length=10, mamode="ema", offset=0, **kwargs) -> IndSeries:
        """
        零延迟移动平均线 (Zero Lag Moving Average, ZLMA)
        ---------
        - 试图消除与移动平均线相关的延迟。
        - 这是由John Ehler和Ric Way创建的适应版本。

        数据来源:
        ---------
            https://en.wikipedia.org/wiki/Zero_lag_exponential_moving_average

        计算方法:
        ---------
        >>> lag = int(0.5 * (length - 1))
            SOURCE = 2 * close - close.shift(lag)
            ZLMA = MA(kind=mamode, SOURCE, length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            mamode (str): 选项: 'ema', 'hma', 'sma', 'wma'. 默认: 'ema'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算零延迟移动平均线
        >>> zlma_IndSeries = self.data.zlma(length=10, mamode="ema")
        >>> 
        >>> # 使用ZLMA减少延迟
        >>> def zlma_low_lag(self):
        >>>     zlma = self.data.zlma(length=10)
        >>>     ema = self.data.ema(length=10)
        >>>     # ZLMA比传统EMA延迟更小
        >>>     return "ZLMA有效减少移动平均线延迟"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def log_return(self, length=20, cumulative=False, offset=0, **kwargs) -> IndSeries:
        """
        对数收益率 (Log Return)
        ---------
            计算序列的对数收益率。

        数据来源:
        ---------
            https://stackoverflow.com/questions/31287552/logarithmic-returns-in-pandas-IndFrame

        计算方法:
        ---------
        >>> LOGRET = log( close.diff(periods=length) )
            CUMLOGRET = LOGRET.cumsum() if cumulative

        参数:
        ---------
        >>> length (int): 周期. 默认: 20
            cumulative (bool): 如果为True，返回累积收益率. 默认: False
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算对数收益率
        >>> log_return_IndSeries = self.data.log_return(length=1)
        >>> 
        >>> # 使用对数收益率分析价格变化
        >>> def log_return_analysis(self):
        >>>     log_ret = self.data.log_return(length=1)
        >>>     # 正对数收益率 - 价格上涨
        >>>     if log_ret.new > 0:
        >>>         return "正对数收益率，价格上涨"
        >>>     # 负对数收益率 - 价格下跌
        >>>     elif log_ret.new < 0:
        >>>         return "负对数收益率，价格下跌"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def percent_return(self, length=20, cumulative=False, offset=0, **kwargs) -> IndSeries:
        """
        百分比收益率 (Percent Return)
        ---------
            计算序列的百分比收益率。

        数据来源:
        ---------
            https://stackoverflow.com/questions/31287552/logarithmic-returns-in-pandas-IndFrame

        计算方法:
        ---------
        >>> PCTRET = close.pct_change(length)
            CUMPCTRET = PCTRET.cumsum() if cumulative

        参数:
        ---------
        >>> length (int): 周期. 默认: 20
            cumulative (bool): 如果为True，返回累积收益率. 默认: False
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算百分比收益率
        >>> percent_return_IndSeries = self.data.percent_return(length=1)
        >>> 
        >>> # 使用百分比收益率评估投资表现
        >>> def investment_performance(self):
        >>>     pct_ret = self.data.percent_return(length=1)
        >>>     # 单日收益率超过5% - 大幅波动
        >>>     if abs(pct_ret.new) > 0.05:
        >>>         return "价格大幅波动"
        >>>     # 累积收益率分析
        >>>     cum_ret = self.data.percent_return(length=20, cumulative=True)
        >>>     if cum_ret.new > 0.1:
        >>>         return "20日累积收益率超过10%"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def entropy(self, length=10, base=2., offset=0, **kwargs) -> IndSeries:
        """
        信息熵 (Entropy)
        ---------
        - Claude Shannon在1948年引入，熵衡量数据的不可预测性，
        - 或等效地，其平均信息量。

        数据来源:
        ---------
            https://en.wikipedia.org/wiki/Entropy_(information_theory)

        计算方法:
        ---------
        >>> P = close / SUM(close, length)
            E = SUM(-P * npLog(P) / npLog(base), length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            base (float): 对数底数. 默认: 2
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算信息熵
        >>> entropy_series = self.data.entropy(length=10, base=2)
        >>> 
        >>> # 使用熵衡量市场不确定性
        >>> def market_uncertainty(self):
        >>>     entropy = self.data.entropy(length=10)
        >>>     # 高熵值 - 市场不确定性高
        >>>     if entropy.new > 0.8:
        >>>         return "市场不确定性较高"
        >>>     # 低熵值 - 市场趋势明确
        >>>     elif entropy.new < 0.3:
        >>>         return "市场趋势相对明确"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def kurtosis_(self, length=30, offset=0, **kwargs) -> IndSeries:
        """
        滚动峰度 (Rolling Kurtosis)
        ---------
            衡量价格分布尾部厚度的统计指标。

        计算方法:
        ---------
        >>> KURTOSIS = close.rolling(length).kurt()

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算滚动峰度
        >>> kurtosis_IndSeries = self.data.kurtosis_(length=30)
        >>> 
        >>> # 使用峰度识别极端价格行为
        >>> def extreme_price_behavior(self):
        >>>     kurtosis = self.data.kurtosis_(length=30)
        >>>     # 高峰度 - 厚尾分布，极端事件概率高
        >>>     if kurtosis.new > 3:
        >>>         return "高峰度，警惕极端价格波动"
        >>>     # 低峰度 - 薄尾分布，价格相对稳定
        >>>     elif kurtosis.new < 0:
        >>>         return "低峰度，价格分布相对稳定"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def mad(self, length=30, offset=0, **kwargs) -> IndSeries:
        """
        滚动平均绝对偏差 (Rolling Mean Absolute Deviation)
        ---------
            衡量价格相对于其移动平均线的平均偏离程度。

        计算方法:
        ---------
        >>> mad = close.rolling(length).mad()

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算平均绝对偏差
        >>> mad_IndSeries = self.data.mad(length=30)
        >>> 
        >>> # 使用MAD衡量价格波动性
        >>> def price_volatility_mad(self):
        >>>     mad = self.data.mad(length=30)
        >>>     # MAD值高 - 价格波动大
        >>>     if mad.new > mad.ema(period=20).new * 1.5:
        >>>         return "价格波动性较高"
        >>>     # MAD值低 - 价格相对稳定
        >>>     elif mad.new < mad.ema(period=20).new * 0.5:
        >>>         return "价格相对稳定"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def median_(self, length=30, offset=0, **kwargs) -> IndSeries:
        """
        滚动中位数 (Rolling Median)
        ---------
        - 'n'个周期内的滚动中位数。简单移动平均线的兄弟指标。

        ## NOTE:
        - 与pandas的median方法同名称,改为median_

        数据来源:
        ---------
            https://www.incrediblecharts.com/indicators/median_price.php

        计算方法:
        ---------
        >>> MEDIAN = close.rolling(length).median()

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算滚动中位数
        >>> median_IndSeries = self.data.median(length=30)
        >>> 
        >>> # 使用中位数识别价格中心趋势
        >>> def median_central_tendency(self):
        >>>     median = self.data.median_(length=30)
        >>>     # 价格在中位数上方 - 偏强势
        >>>     if self.data.close.new > median.new:
        >>>         return "价格在中位数上方"
        >>>     # 价格在中位数下方 - 偏弱势
        >>>     elif self.data.close.new < median.new:
        >>>         return "价格在中位数下方"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def quantile_(self, length=30, q=0.5, offset=0, **kwargs) -> IndSeries:
        """
        滚动分位数 (Rolling Quantile)
        ---------
            计算价格在指定周期内的分位数水平。

        计算方法:
        ---------
        >>> QUANTILE = close.rolling(length).quantile(q)

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            q (float): 分位数 (0到1之间). 默认: 0.5
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算不同分位数
        >>> quantile_50 = self.data.quantile_(length=30, q=0.5)  # 中位数
        >>> quantile_25 = self.data.quantile_(length=30, q=0.25) # 下四分位数
        >>> quantile_75 = self.data.quantile_(length=30, q=0.75) # 上四分位数
        >>> 
        >>> # 使用分位数识别价格位置
        >>> def price_position_analysis(self):
        >>>     q25 = self.data.quantile_(length=30, q=0.25)
        >>>     q75 = self.data.quantile_(length=30, q=0.75)
        >>>     # 价格在下四分位数以下 - 相对低位
        >>>     if self.data.close.new < q25.new:
        >>>         return "价格处于相对低位"
        >>>     # 价格在上四分位数以上 - 相对高位
        >>>     elif self.data.close.new > q75.new:
        >>>         return "价格处于相对高位"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def skew_(self, length=30, offset=0, **kwargs) -> IndSeries:
        """
        滚动偏度 (Rolling Skew)
        ---------
            衡量价格分布不对称性的统计指标。

        计算方法:
        ---------
        >>> SKEW = close.rolling(length).skew()

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算滚动偏度
        >>> skew_IndSeries = self.data.skew_(length=30)
        >>> 
        >>> # 使用偏度分析价格分布特征
        >>> def price_distribution_skew(self):
        >>>     skew = self.data.skew_(length=30)
        >>>     # 正偏度 - 右偏分布，极端高价可能性更高
        >>>     if skew.new > 0.5:
        >>>         return "价格分布右偏，警惕极端高价"
        >>>     # 负偏度 - 左偏分布，极端低价可能性更高
        >>>     elif skew.new < -0.5:
        >>>         return "价格分布左偏，警惕极端低价"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def stdev(self, length=30, ddof=1, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        滚动标准差 (Rolling Standard Deviation)
        ---------
            衡量价格波动性的常用统计指标。

        计算方法:
        ---------
        >>> VAR = Variance
            STDEV = variance(close, length).apply(np.sqrt)

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            ddof (int): 自由度差值。计算中使用的除数是N - ddof，
                        其中N表示元素数量. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算滚动标准差
        >>> stdev_IndSeries = self.data.stdev(length=30)
        >>> 
        >>> # 使用标准差衡量波动性
        >>> def volatility_measurement(self):
        >>>     stdev = self.data.stdev(length=30)
        >>>     # 标准差突增 - 波动性增加
        >>>     if stdev.new > stdev.ema(period=20).new * 1.5:
        >>>         return "市场波动性显著增加"
        >>>     # 标准差较低 - 市场平静
        >>>     elif stdev.new < stdev.ema(period=20).new * 0.7:
        >>>         return "市场波动性较低"
        """
        ...

    @tobtind(lines=['toslr', 'tosl1', 'tosu1', 'tosl2', 'tosu2', 'tosl3', 'tosu3'],overlap=True,  lib='pta')
    def tos_stdevall(self, length=30, stds=[1, 2, 3], ddof=1, offset=0, **kwargs) -> IndFrame:
        """
        TD Ameritrade Think or Swim 全标准差通道 (TOS_STDEV)
        ---------
        - TD Ameritrade Think or Swim 全标准差指标的重现，
        - 返回整个图表数据的标准差或由长度参数定义的最近柱线区间的标准差。

        数据来源:
        ---------
            https://tlc.thinkorswim.com/center/reference/thinkScript/Functions/Statistical/StDevAll

        计算方法:
        ---------
            LR = Linear Regression
            STDEV = Standard Deviation
            LR = LR(close, length)
            STDEV = STDEV(close, length, ddof)
            for level in stds:
                LOWER = LR - level * STDEV
                UPPER = LR + level * STDEV

        参数:
        ---------
        >>> length (int): 从当前柱线开始的柱线数. 默认: 30
            stds (list): 标准差倍数列表，从中心线性回归线开始递增. 默认: [1,2,3]
            ddof (int): 自由度差值。计算中使用的除数是N - ddof，
                        其中N表示元素数量. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含toslr、tosl1、tosu1、tosl2、tosu2、tosl3、tosu3列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算TOS标准差通道
        >>> tos_data = self.data.tos_stdevall(length=30, stds=[1, 2, 3])
        >>> lr, lower1, upper1, lower2, upper2, lower3, upper3 = tos_data.toslr, tos_data.tosl1, tos_data.tosu1, tos_data.tosl2, tos_data.tosu2, tos_data.tosl3, tos_data.tosu3
        >>> 
        >>> # 使用TOS通道识别价格位置
        >>> def tos_channel_analysis(self):
        >>>     tos = self.data.tos_stdevall(length=30)
        >>>     # 价格在2倍标准差上方 - 极端高位
        >>>     if self.data.close.new > tos.tosu2.new:
        >>>         return "价格在2倍标准差上方，极端高位"
        >>>     # 价格在2倍标准差下方 - 极端低位
        >>>     elif self.data.close.new < tos.tosl2.new:
        >>>         return "价格在2倍标准差下方，极端低位"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def variance(self, length=30, ddof=1, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        滚动方差 (Rolling Variance)
        ---------
            衡量价格离散程度的统计指标。

        计算方法:
        ---------
        >>> VARIANCE = close.rolling(length).var()

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            ddof (int): 自由度差值。计算中使用的除数是N - ddof，
                        其中N表示元素数量. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算滚动方差
        >>> variance_IndSeries = self.data.variance(length=30)
        >>> 
        >>> # 使用方差分析价格离散程度
        >>> def price_dispersion(self):
        >>>     variance = self.data.variance(length=30)
        >>>     # 高方差 - 价格离散度高，波动大
        >>>     if variance.new > variance.ema(period=20).new * 2:
        >>>         return "价格离散度高，市场不稳定"
        >>>     # 低方差 - 价格聚集度高，市场稳定
        >>>     elif variance.new < variance.ema(period=20).new * 0.5:
        >>>         return "价格聚集度高，市场相对稳定"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def zscore(self, length=30, std=1., offset=0, **kwargs) -> IndSeries:
        """
        Z分数标准化 (Rolling Z Score)
        ---------
            衡量价格相对于其移动平均线的标准差位置。

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            STDEV = Standard Deviation
            std = std * STDEV(close, length)
            mean = SMA(close, length)
            ZSCORE = (close - mean) / std

        参数:
        ---------
        >>> length (int): 周期. 默认: 30
            std (float): 标准差倍数. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算Z分数
        >>> zscore_IndSeries = self.data.zscore(length=30, std=1)
        >>> 
        >>> # 使用Z分数识别异常价格
        >>> def zscore_anomaly_detection(self):
        >>>     zscore = self.data.zscore(length=30)
        >>>     # Z分数 > 2 - 价格异常偏高
        >>>     if zscore.new > 2:
        >>>         return "Z分数显示价格异常偏高"
        >>>     # Z分数 < -2 - 价格异常偏低
        >>>     elif zscore.new < -2:
        >>>         return "Z分数显示价格异常偏低"
        """
        ...

    @tobtind(lines=['adxx', 'adxr','dmp', 'dmn'], lib='pta')
    def adx(self, length=14, lensig=14, scalar=100, mamode="rma", drift=1, offset=0, **kwargs) -> IndFrame:
        """
        平均趋向指数 (Average Directional Movement, ADX)
        ---------
            旨在通过测量单一方向的移动量来量化趋势强度。

        数据来源:
        ---------
        - https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/average-directional-movement-adx/
        - TA Lib 相关性: >99%

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            lensig (int): 信号长度。类似于TradingView的默认ADX. 默认: length
            scalar (float): 放大倍数. 默认: 100
            mamode (str): 移动平均模式，参考```help(ta.ma)```. 默认: 'rma'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含adxx、adxr、dmp、dmn列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算ADX指标
        >>> adx_data = self.data.adx(length=14, lensig=14)
        >>> adx, plus_di, minus_di = adx_data.adxx, adx_data.dmp, adx_data.dmn
        >>> 
        >>> # 使用ADX判断趋势强度
        >>> def adx_trend_strength(self):
        >>>     adx = self.data.adx()
        >>>     # ADX > 25 - 趋势强劲
        >>>     if adx.adxx.new > 25:
        >>>         return "ADX显示趋势强劲"
        >>>     # ADX < 20 - 趋势疲弱或震荡
        >>>     elif adx.adxx.new < 20:
        >>>         return "ADX显示趋势疲弱或震荡"
        >>> 
        >>> # 使用+DI和-DI判断趋势方向
        >>> def di_trend_direction(self):
        >>>     adx = self.data.adx()
        >>>     # +DI > -DI - 上升趋势占主导
        >>>     if adx.dmp.new > adx.dmn.new:
        >>>         return "+DI > -DI，上升趋势"
        >>>     # -DI > +DI - 下降趋势占主导
        >>>     elif adx.dmn.new > adx.dmp.new:
        >>>         return "-DI > +DI，下降趋势"
        """
        ...

    @tobtind(lines=['amatl', 'amats'], overlap=True, lib='pta')
    def amat(self, fast=8, slow=21, lookback=2, mamode="ema", offset=0, **kwargs) -> IndFrame:
        """
        阿彻移动平均趋势 (Archer Moving Averages Trends, AMAT)
        ---------
            基于移动平均线的趋势识别指标。

        返回:
        ---------
        >>> IndFrame: 包含amatl、amats列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        >>> # 计算AMAT指标
        >>> amat_data = self.data.amat(fast=8, slow=21, lookback=2)
        >>> amat_long, amat_short = amat_data.amatl, amat_data.amats
        >>> 
        >>> # 使用AMAT识别趋势转换
        >>> def amat_trend_signals(self):
        >>>     amat = self.data.amat()
        >>>     # 长期信号转正 - 长期看涨
        >>>     if amat.amatl.new == 1 and amat.amatl.prev == 0:
        >>>         return "AMAT长期看涨信号"
        >>>     # 短期信号转正 - 短期看涨
        >>>     if amat.amats.new == 1 and amat.amats.prev == 0:
        >>>         return "AMAT短期看涨信号"
        """
        ...

    @tobtind(lines=['aroon_up', 'aroon_down', 'aroon_osc'], lib='pta')
    def aroon(self, length=14, scalar=100, talib=True, offset=0, **kwargs) -> IndFrame:
        """
        阿隆指标和震荡器 (Aroon & Aroon Oscillator)
        ---------
            试图识别证券是否处于趋势中以及趋势强度。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Aroon
        - https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/aroon-ar/

        计算方法:
        ---------
            recent_maximum_index(x): return int(np.argmax(x[::-1]))
            recent_minimum_index(x): return int(np.argmin(x[::-1]))
            periods_from_hh = high.rolling(length + 1).apply(recent_maximum_index, raw=True)
            AROON_UP = scalar * (1 - (periods_from_hh / length))
            periods_from_ll = low.rolling(length + 1).apply(recent_minimum_index, raw=True)
            AROON_DN = scalar * (1 - (periods_from_ll / length))
            AROON_OSC = AROON_UP - AROON_DN

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            scalar (float): 放大倍数. 默认: 100
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含aroon_up、aroon_down、aroon_osc列的数据框

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算阿隆指标
        >>> aroon_data = self.data.aroon(length=14)
        >>> aroon_up, aroon_down, aroon_osc = aroon_data.aroon_up, aroon_data.aroon_down, aroon_data.aroon_osc
        >>> 
        >>> # 使用阿隆指标识别趋势强度
        >>> def aroon_trend_strength(self):
        >>>     aroon = self.data.aroon(length=14)
        >>>     # 阿隆上线 > 70 且 阿隆下线 < 30 - 强烈上升趋势
        >>>     if aroon.aroon_up.new > 70 and aroon.aroon_down.new < 30:
        >>>         return "阿隆指标显示强烈上升趋势"
        >>>     # 阿隆下线 > 70 且 阿隆上线 < 30 - 强烈下降趋势
        >>>     elif aroon.aroon_down.new > 70 and aroon.aroon_up.new < 30:
        >>>         return "阿隆指标显示强烈下降趋势"
        >>> 
        >>> # 使用阿隆震荡器
        >>> def aroon_oscillator_signals(self):
        >>>     aroon = self.data.aroon(length=14)
        >>>     # 阿隆震荡器上穿零轴 - 买入信号
        >>>     if aroon.aroon_osc.new > 0 and aroon.aroon_osc.prev <= 0:
        >>>         return "阿隆震荡器买入信号"
        >>>     # 阿隆震荡器下穿零轴 - 卖出信号
        >>>     elif aroon.aroon_osc.new < 0 and aroon.aroon_osc.prev >= 0:
        >>>         return "阿隆震荡器卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def chop(self, length=14, atr_length=1., ln=False, scalar=100, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        震荡指数 (Choppiness Index, CHOP)
        ---------
        - 澳大利亚商品交易员E.W. Dreiss创建，旨在确定市场是否处于震荡状态
        - （横盘交易）或非震荡状态（在任一方向的趋势内交易）。
        - 值接近100表示标的更震荡，值接近0表示标的处于趋势中。

        数据来源:
        ---------
        - https://www.tradingview.com/scripts/choppinessindex/
        - https://www.motivewave.com/studies/choppiness_index.htm

        计算方法:
        ---------
        >>> HH = high.rolling(length).max()
            LL = low.rolling(length).min()
            ATR_SUM = SUM(ATR(drift), length)
            CHOP = scalar * (LOG10(ATR_SUM) - LOG10(HH - LL))
            CHOP /= LOG10(length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            atr_length (int): ATR长度. 默认: 1
            ln (bool): 如果为True，使用ln否则使用log10. 默认: False
            scalar (float): 放大倍数. 默认: 100
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算震荡指数
        >>> chop_IndSeries = self.data.chop(length=14)
        >>> 
        >>> # 使用震荡指数识别市场状态
        >>> def market_choppiness_analysis(self):
        >>>     chop = self.data.chop(length=14)
        >>>     # CHOP > 61.8 - 市场震荡，避免趋势策略
        >>>     if chop.new > 61.8:
        >>>         return "市场高度震荡，适合震荡策略"
        >>>     # CHOP < 38.2 - 市场趋势明显，适合趋势策略
        >>>     elif chop.new < 38.2:
        >>>         return "市场趋势明显，适合趋势策略"
        """
        ...

    @tobtind(lines=['cksp_long', 'cksp_short'], overlap=True, lib='pta')
    def cksp(self, p=10, x=3, q=20, tvmode=True, offset=0, **kwargs) -> IndFrame:
        """
        钱德-克罗尔止损 (Chande Kroll Stop, CKSP)
        ---------
        - Tushar Chande和Stanley Kroll在他们的著作"The New Technical Trader"中提出。
        - 这是一个趋势跟踪指标，通过计算最近市场波动性的平均真实波幅来识别止损位。

        数据来源:
        ---------
        - https://www.multicharts.com/discussion/viewtopic.php?t=48914
        - "The New Technical Trader", Wikey 1st ed. ISBN 9780471597803, page 95

        计算方法:
        ---------
        >>> ATR = Average True Range
            LS0 = high.rolling(p).max() - x * ATR(length=p)
            LS = LS0.rolling(q).max()
            SS0 = high.rolling(p).min() + x * ATR(length=p)
            SS = SS0.rolling(q).min()

        参数:
        ---------
        >>> p (int): ATR和第一个止损周期. 默认: 10
            x (float): ATR乘数. 默认: TV模式为1，否则为3
            q (int): 第二个止损周期. 默认: TV模式为9，否则为20
            tvmode (bool): Trading View或书籍实现模式. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含cksp_long和cksp_short列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算钱德-克罗尔止损
        >>> cksp_data = self.data.cksp(p=10, x=3, q=20)
        >>> cksp_long, cksp_short = cksp_data.cksp_long, cksp_data.cksp_short
        >>> 
        >>> # 使用CKSP设置动态止损
        >>> def cksp_stop_loss(self):
        >>>     cksp = self.data.cksp()
        >>>     # 多头头寸使用多头止损位
        >>>     if self.position.is_long:
        >>>         stop_loss = cksp.cksp_long.new
        >>>         return f"多头止损位: {stop_loss}"
        >>>     # 空头头寸使用空头止损位
        >>>     elif self.position.is_short:
        >>>         stop_loss = cksp.cksp_short.new
        >>>         return f"空头止损位: {stop_loss}"
        """
        ...

    @tobtind(lines=None, overlap=True, lib='pta')
    def decay(self, kind="exponential", length=5, mode="linear", offset=0, **kwargs) -> IndSeries:
        """
        衰减指标 (Decay)
        ---------
        - 从先前的信号（如交叉）向前创建衰减。默认为"线性"。
        - 指数衰减可选为"exponential"或"exp"。

        数据来源:
        ---------
            https://tulipindicators.org/decay

        计算方法:
        ---------
        >>> if mode == "exponential" or mode == "exp":
                max(close, close[-1] - exp(-length), 0)
            else:
                max(close, close[-1] - (1 / length), 0)

        参数:
        ---------
        >>> length (int): 周期. 默认: 5
            mode (str): 如果为'exp'则为"指数"衰减. 默认: 'linear'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算衰减指标
        >>> decay_series = self.data.decay(length=5, mode="linear")
        >>> 
        >>> # 使用衰减指标平滑信号
        >>> def decay_signal_smoothing(self):
        >>>     decay = self.data.decay(length=5, mode="exponential")
        >>>     # 衰减指标可用于平滑交易信号
        >>>     return "衰减指标帮助平滑信号波动"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def decreasing(self, length=1, strict=False, asint=True, percent=None, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        下降序列检测 (Decreasing)
        ---------
        - 如果序列在一个周期内下降，返回True，否则返回False。
        - 如果strict为True，则检查序列是否在该周期内持续下降。

        计算方法:
        ---------
        >>> if strict:
                decreasing = all(i > j for i, j in zip(close[-length:], close[1:]))
            else:
                decreasing = close.diff(length) < 0
            if asint:
                decreasing = decreasing.astype(int)

        参数:
        ---------
        >>> length (int): 周期. 默认: 1
            strict (bool): 如果为True，检查序列是否在该周期内持续下降. 默认: False
            percent (float): 百分比作为整数. 默认: None
            asint (bool): 返回二进制值. 默认: True
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 检测下降序列
        >>> decreasing_IndSeries = self.data.decreasing(length=3, strict=True)
        >>> 
        >>> # 使用下降检测识别趋势
        >>> def decreasing_trend_detection(self):
        >>>     decreasing = self.data.decreasing(length=3, strict=True)
        >>>     # 连续3日严格下降 - 强烈下降趋势
        >>>     if decreasing.new == 1:
        >>>         return "检测到连续下降趋势"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def dpo(self, length=20, centered=True, offset=0, **kwargs) -> IndSeries:
        """
        去趋势价格震荡指标 (Detrend Price Oscillator, DPO)
        ---------
            旨在从价格中去除趋势，使其更容易识别周期。

        数据来源:
        ---------
        - https://www.tradingview.com/scripts/detrendedpriceoscillator/
        - https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/dpo
        - http://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:detrended_price_osci

        计算方法:
        ---------
        >>> SMA = Simple Moving Average
            t = int(0.5 * length) + 1
            DPO = close.shift(t) - SMA(close, length)
            if centered:
                DPO = DPO.shift(-t)

        参数:
        ---------
        >>> length (int): 周期. 默认: 20
            centered (bool): 将dpo向后移动int(0.5 * length) + 1. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算去趋势价格震荡指标
        >>> dpo_IndSeries = self.data.dpo(length=20, centered=True)
        >>> 
        >>> # 使用DPO识别周期波动
        >>> def dpo_cycle_analysis(self):
        >>>     dpo = self.data.dpo(length=20)
        >>>     # DPO上穿零轴 - 周期上升阶段
        >>>     if dpo.new > 0 and dpo.prev <= 0:
        >>>         return "DPO显示周期上升阶段"
        >>>     # DPO下穿零轴 - 周期下降阶段
        >>>     elif dpo.new < 0 and dpo.prev >= 0:
        >>>         return "DPO显示周期下降阶段"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def increasing(self, length=1, strict=False, asint=True, percent=None, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        上升序列检测 (Increasing)
        ---------
        - 如果序列在一个周期内上升，返回True，否则返回False。
        - 如果strict为True，则检查序列是否在该周期内持续上升。

        计算方法:
        ---------
        >>> if strict:
                increasing = all(i < j for i, j in zip(close[-length:], close[1:]))
            else:
                increasing = close.diff(length) > 0
            if asint:
                increasing = increasing.astype(int)

        参数:
        ---------
        >>> length (int): 周期. 默认: 1
            strict (bool): 如果为True，检查序列是否在该周期内持续上升. 默认: False
            percent (float): 百分比作为整数. 默认: None
            asint (bool): 返回二进制值. 默认: True
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 检测上升序列
        >>> increasing_IndSeries = self.data.increasing(length=3, strict=True)
        >>> 
        >>> # 使用上升检测识别趋势
        >>> def increasing_trend_detection(self):
        >>>     increasing = self.data.increasing(length=3, strict=True)
        >>>     # 连续3日严格上升 - 强烈上升趋势
        >>>     if increasing.new == 1:
        >>>         return "检测到连续上升趋势"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def long_run(self, fast=None, slow=None, length=2, offset=0, **kwargs) -> IndSeries:
        """
        长期运行指标 (Long Run)
        ---------
            长期趋势识别指标。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        使用案例:
        >>> # 计算长期运行指标
        >>> long_run_IndSeries = self.data.long_run(length=2)
        >>> 
        >>> # 使用长期运行指标
        >>> def long_run_trend(self):
        >>>     long_run = self.data.long_run(length=2)
        >>>     return f"长期运行指标: {long_run.new}"
        """
        ...

    @tobtind(lines=['psarl', 'psars', 'psaraf', 'psarr'],overlap=dict(psarl=True, psars=True, psaraf=False, psarr=False), lib='pta')
    def psar(self, af0=0.02, af=0.02, max_af=0.2, offset=0, **kwargs) -> IndFrame:
        """
        抛物线转向指标 (Parabolic Stop and Reverse, PSAR)
        ---------
        - J. Wells Wilder开发，用于确定趋势方向及其潜在的价格反转。
        - PSAR使用称为"SAR"的跟踪止损和反转方法来识别可能的入场和出场点。

        数据来源:
        ---------
        - https://www.tradingview.com/pine-script-reference/#fun_sar
        - https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=66&Name=Parabolic

        计算方法:
        ---------
            参考源代码实现

        参数:
        ---------
        >>> af0 (float): 初始加速因子. 默认: 0.02
            af (float): 加速因子. 默认: 0.02
            max_af (float): 最大加速因子. 默认: 0.2
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含psarl、psars、psaraf、psarr列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算抛物线转向指标
        >>> psar_data = self.data.psar(af0=0.02, af=0.02, max_af=0.2)
        >>> psar_long, psar_short, psar_af, psar_reversal = psar_data.psarl, psar_data.psars, psar_data.psaraf, psar_data.psarr
        >>> 
        >>> # 使用PSAR识别趋势反转
        >>> def psar_trend_reversal(self):
        >>>     psar = self.data.psar()
        >>>     # PSAR点从价格下方转到上方 - 趋势反转卖出信号
        >>>     if psar.psarr.new == 1:
        >>>         return "PSAR趋势反转信号"
        >>> 
        >>> # 使用PSAR作为动态止损
        >>> def psar_stop_loss(self):
        >>>     psar = self.data.psar()
        >>>     # 多头头寸使用PSAR多头止损
        >>>     if self.position.is_long:
        >>>         stop_loss = psar.psarl.new
        >>>         return f"PSAR多头止损: {stop_loss}"
        >>>     # 空头头寸使用PSAR空头止损
        >>>     elif self.position.is_short:
        >>>         stop_loss = psar.psars.new
        >>>         return f"PSAR空头止损: {stop_loss}"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def qstick(self, length=10, ma="sma", offset=0, **kwargs) -> IndSeries:
        """
        Q棒指标 (Q Stick)
        ---------
            Tushar Chande开发，试图量化和识别蜡烛图中的趋势。

        数据来源:
        ---------
            https://library.tradingtechnologies.com/trade/chrt-ti-qstick.html

        计算方法:
        ---------
        >>> xMA是其中之一: sma (默认), dema, ema, hma, rma
            qstick = xMA(close - open, length)

        参数:
        ---------
        >>> length (int): 周期. 默认: 10
            ma (str): 使用的移动平均线类型. 默认: 'sma'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, close

        使用案例:
        ---------
        >>> # 计算Q棒指标
        >>> qstick_IndSeries = self.data.qstick(length=10, ma="sma")
        >>> 
        >>> # 使用Q棒指标识别买卖压力
        >>> def qstick_pressure_analysis(self):
        >>>     qstick = self.data.qstick(length=10)
        >>>     # Q棒 > 0 - 买方压力占主导
        >>>     if qstick.new > 0:
        >>>         return "Q棒显示买方压力"
        >>>     # Q棒 < 0 - 卖方压力占主导
        >>>     elif qstick.new < 0:
        >>>         return "Q棒显示卖方压力"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def short_run(self, slow:pd.Series=None, length=2, offset=0, **kwargs) -> IndSeries:
        """
        短期运行指标 (Short Run)
        ---------
            短期趋势识别指标。

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        使用案例:
        >>> # 计算短期运行指标
        >>> short_run_IndSeries = self.data.short_run()
        >>> 
        >>> # 使用短期运行指标
        >>> def short_run_trend(self):
        >>>     short_run = self.data.short_run()
        >>>     return f"短期运行指标: {short_run.new}"
        """
        ...

    @tobtind(lines=['ts_trends', 'ts_trades', 'ts_entries', 'ts_exits'], lib='pta')
    def tsignals(self, asbool=None, trend_reset=None, trend_offset=0,
                 offset=0, **kwargs) -> IndFrame:
        """
        趋势信号 (Trend Signals)
        ---------
            给定一个趋势，趋势信号返回趋势、交易、入场和出场作为布尔整数。

        数据来源:
        ---------
            Kevin Johnson

        计算方法:
        ---------
        >>> trades = trends.diff().shift(trade_offset).fillna(0).astype(int)
            entries = (trades > 0).astype(int)
            exits = (trades < 0).abs().astype(int)

        参数:
        ---------
        >>> trend (pd.Series): 趋势序列。趋势可以是布尔值或0和1的整数序列
            asbool (bool): 如果为True，将趋势、入场和出场列转换为布尔值. 默认: False
            trend_reset (value): 用于识别趋势是否已结束的值. 默认: 0
            trend_offset (int): 用于移动交易入场/出场的值。回测使用1，实盘使用0. 默认: 0
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含ts_trends、ts_trades、ts_entries、ts_exits列的数据框

        使用案例:
        ---------
        >>> # 使用趋势信号
        >>> # 当收盘价大于50日移动平均线时生成信号
        >>> trend_condition = self.data.close > self.data.sma(length=50)
        >>> signals = self.data.tsignals(trend=trend_condition, asbool=False)
        >>> 
        >>> # 获取交易信号
        >>> def get_trading_signals(self):
        >>>     trend = self.data.ema(length=8) > self.data.ema(length=21)
        >>>     signals = self.data.tsignals(trend=trend, asbool=True)
        >>>     # 入场信号
        >>>     if signals.ts_entries.new:
        >>>         self.data.buy()
        >>>     # 出场信号
        >>>     elif signals.ts_exits.new:
        >>>         self.data.sell()
        """
        ...

    @tobtind(lines=['ttm_trend',], lib='pta')
    def ttm_trend(self, length=6, offset=0, **kwargs) -> IndSeries:
        """
        TTM趋势指标 (TTM Trend)
        ---------
        - 来自John Carter的著作"Mastering the Trade"，将柱状图绘制为绿色或红色。
        - 检查价格是否高于或低于前5根柱的平均价格。

        数据来源:
        ---------
            https://www.prorealcode.com/prorealtime-indicators/ttm-trend-price/

        计算方法:
        ---------
        >>> averageprice = (((high[5]+low[5])/2)+((high[4]+low[4])/2)+((high[3]+low[3])/2)+(
                (high[2]+low[2])/2)+((high[1]+low[1])/2)+((high[6]+low[6])/2)) / 6
            if close > averageprice:
                drawcandle(open,high,low,close) coloured(0,255,0)
            if close < averageprice:
                drawcandle(open,high,low,close) coloured(255,0,0)

        参数:
        ---------
        >>> length (int): 周期. 默认: 6
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 包含ttm_trend列的数据序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算TTM趋势指标
        >>> ttm_trend_data = self.data.ttm_trend(length=6)
        >>> 
        >>> # 使用TTM趋势识别颜色变化
        >>> def ttm_trend_color_change(self):
        >>>     ttm = self.data.ttm_trend(length=6)
        >>>     # 趋势从红色转为绿色 - 买入信号
        >>>     if ttm.new == 1 and ttm.prev == 0:
        >>>         return "TTM趋势转为绿色，买入信号"
        >>>     # 趋势从绿色转为红色 - 卖出信号
        >>>     elif ttm.new == 0 and ttm.prev == 1:
        >>>         return "TTM趋势转为红色，卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def vhf(self, length=28, drift=None, offset=0, **kwargs) -> IndSeries:
        """
        垂直水平过滤器 (Vertical Horizontal Filter, VHF)
        ---------
            Adam White创建，用于识别趋势市场和震荡市场。

        数据来源:
        ---------
            https://www.incrediblecharts.com/indicators/vertical_horizontal_filter.php

        计算方法:
        ---------
        >>> HCP = Highest Close Price in Period
            LCP = Lowest Close Price in Period
            Change = abs(Ct - Ct-1)
            VHF = (HCP - LCP) / RollingSum[length] of Change

        参数:
        ---------
        >>> length (int): 周期长度. 默认: 28
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算垂直水平过滤器
        >>> vhf_IndSeries = self.data.vhf(length=28)
        >>> 
        >>> # 使用VHF识别市场状态
        >>> def vhf_market_state(self):
        >>>     vhf = self.data.vhf(length=28)
        >>>     # VHF > 0.4 - 趋势市场
        >>>     if vhf.new > 0.4:
        >>>         return "VHF显示趋势市场"
        >>>     # VHF < 0.2 - 震荡市场
        >>>     elif vhf.new < 0.2:
        >>>         return "VHF显示震荡市场"
        """
        ...

    @tobtind(lines=['vip', 'vim'], lib='pta')
    def vortex(self, length=14, drift=1, offset=0, **kwargs) -> IndFrame:
        """
        涡旋指标 (Vortex)
        ---------
            两个捕捉正负趋势运动的震荡器。

        数据来源:
        ---------
            https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:vortex_indicator

        计算方法:
        ---------
        >>> TR = True Range
            SMA = Simple Moving Average
            tr = TR(high, low, close)
            tr_sum = tr.rolling(length).sum()
            vmp = (high - low.shift(drift)).abs()
            vmn = (low - high.shift(drift)).abs()
            VIP = vmp.rolling(length).sum() / tr_sum
            VIM = vmn.rolling(length).sum() / tr_sum

        参数:
        ---------
        >>> length (int): 周期. 默认: 14
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含vip和vim列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算涡旋指标
        >>> vortex_data = self.data.vortex(length=14)
        >>> vip, vim = vortex_data.vip, vortex_data.vim
        >>> 
        >>> # 使用涡旋指标识别趋势
        >>> def vortex_trend_direction(self):
        >>>     vortex = self.data.vortex(length=14)
        >>>     # VIP > VIM - 上升趋势
        >>>     if vortex.vip.new > vortex.vim.new:
        >>>         return "涡旋指标显示上升趋势"
        >>>     # VIM > VIP - 下降趋势
        >>>     elif vortex.vim.new > vortex.vip.new:
        >>>         return "涡旋指标显示下降趋势"
        """
        ...

    @tobtind(lines=['xs_long', 'xs_short'], lib='pta')
    def xsignals(self, xa:pd.Series=None, xb:pd.Series=None, above=None, long=None, asbool=None, trend_reset=None,
                 trend_offset=0, offset=0, **kwargs) -> IndFrame:
        """
        交叉信号 (Cross Signals, XSIGNALS)
        ---------
            为信号交叉返回趋势信号(TSIGNALS)结果。

        数据来源:
        ---------
            Kevin Johnson

        计算方法:
        ---------
        >>> trades = trends.diff().shift(trade_offset).fillna(0).astype(int)
            entries = (trades > 0).astype(int)
            exits = (trades < 0).abs().astype(int)

        参数:
        ---------
        >>> signal (pd.Series): 信号序列
            xa (float): 第一个交叉阈值
            xb (float): 第二个交叉阈值
            above (bool): 当信号首先上穿'xa'然后下穿'xb'时。如果为False，则当信号首先下穿'xa'然后上穿'xb'时. 默认: True
            long (bool): 将多头趋势传递给tsignals的趋势参数。如果为False，则将空头趋势传递给tsignals的趋势参数. 默认: True
            asbool (bool): 如果为True，将趋势、入场和出场列转换为布尔值. 默认: False
            trend_reset (value): 用于识别趋势是否已结束的值. 默认: 0
            trend_offset (int): 用于移动交易入场/出场的值. 默认: 0
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含xs_long、xs_short列的数据框

        使用案例:
        ---------
        >>> # 使用交叉信号
        >>> rsi = self.data.rsi(length=14)
        >>> # 当RSI上穿20然后下穿80时返回信号
        >>> signals1 = self.data.xsignals(signal=rsi, xa=20, xb=80, above=True)
        >>> # 当RSI下穿20然后上穿80时返回信号
        >>> signals2 = self.data.xsignals(signal=rsi, xa=20, xb=80, above=False)
        >>> 
        >>> # 使用交叉信号进行交易
        >>> def cross_signal_trading(self):
        >>>     rsi = self.data.rsi(length=14)
        >>>     signals = self.data.xsignals(signal=rsi, xa=30, xb=70, above=True)
        >>>     # 多头入场信号
        >>>     if signals.xs_long.new == 1:
        >>>         self.data.buy()
        >>>     # 空头入场信号
        >>>     elif signals.xs_short.new == 1:
        >>>         self.data.sell()
        """
        ...

    @tobtind(lib='pta')
    def above(self, b=None, asint=True, offset=0, **kwargs) -> IndSeries:
        """
        序列a大于或等于序列b或数值
        ---------
            判断序列a是否大于或等于序列b或数值。

        参数:
        ---------
        >>> b (Series | float | int): 序列b或数值
            asint (bool, 可选): 是否转为整数. 默认: True
            offset (int, 可选): 数据偏移. 默认: 0

        返回:
        ---------
        >>> IndSeries: 布尔序列或整数序列

        使用案例:
        >>> # 判断价格是否在移动平均线之上
        >>> above_condition = self.data.above(b=self.data.sma(length=20))
        >>> 
        >>> # 判断价格是否在特定水平之上
        >>> def above_resistance(self):
        >>>     above_res = self.data.above(b=100)
        >>>     if above_res.new:
        >>>         return "价格突破阻力位"
        """
        ...

    @tobtind(lib='pta')
    def below(self, b=None, asint=True, offset=0, **kwargs) -> IndSeries:
        """
        序列a小于或等于序列b或数值
        ---------
            判断序列a是否小于或等于序列b或数值。

        参数:
        ---------
        >>> b (Series | float | int): 序列b或数值
            asint (bool, 可选): 是否转为整数. 默认: True
            offset (int, 可选): 数据偏移. 默认: 0

        返回:
        ---------
        >>> IndSeries: 布尔序列或整数序列

        使用案例:
        >>> # 判断价格是否在移动平均线之下
        >>> below_condition = self.data.below(b=self.data.sma(length=20))
        >>> 
        >>> # 判断价格是否在特定水平之下
        >>> def below_support(self):
        >>>     below_sup = self.data.below(b=50)
        >>>     if below_sup.new:
        >>>         return "价格跌破支撑位"
        """
        ...

    @tobtind(lib='pta')
    def cross(self, b=None, above=True, asint=True, offset=0, **kwargs) -> IndSeries:
        """
        序列a上穿序列b或数值
        ---------
            判断序列a是否上穿序列b或数值。

        参数:
        ---------
        >>> b (pd.Series): 序列b或数值
            above (bool): 上穿方向. 默认: True
            asint (bool, 可选): 是否转为整数. 默认: True
            offset (int, 可选): 数据偏移. 默认: 0

        返回:
        ---------
        >>> IndSeries: 布尔序列或整数序列

        使用案例:
        >>> # 判断快速EMA是否上穿慢速EMA
        >>> cross_condition = self.data.ema(length=10).cross(b=self.data.ema(length=20))
        >>> 
        >>> # 使用交叉信号
        >>> def golden_cross(self):
        >>>     cross = self.data.ema(length=10).cross(b=self.data.ema(length=50))
        >>>     if cross.new:
        >>>         return "金叉信号出现"
        """
        ...

    @tobtind(lib='pta')
    def cross_up(self, b=None, asint=True, offset=0, **kwargs) -> IndSeries:
        """
        序列a上穿序列b或数值
        ---------
            判断序列a是否上穿序列b或数值。

        参数:
        ---------
        >>> b (pd.Series): 序列b或数值
            asint (bool, 可选): 是否转为整数. 默认: True
            offset (int, 可选): 数据偏移. 默认: 0

        返回:
        ---------
        >>> IndSeries: 布尔序列或整数序列

        使用案例:
        >>> # 判断价格是否上穿移动平均线
        >>> cross_up_condition = self.data.cross_up(b=self.data.sma(length=20))
        >>> 
        >>> # 使用上穿信号
        >>> def breakout_signal(self):
        >>>     breakout = self.data.cross_up(b=self.data.high.rolling(20).max())
        >>>     if breakout.new:
        >>>         return "价格突破20日高点"
        """
        ...

    @tobtind(lib='pta')
    def cross_down(self, b=None, asint=True, offset=0, **kwargs) -> IndSeries:
        """
        下穿信号 (Cross Down)
        ---------
            检测序列a是否下穿序列b，或序列a是否下穿一个数值。

        数据来源:
        ---------
            技术分析基础

        计算方法:
        ---------
            >>> Condition: (a[i-1] > b[i-1]) and (a[i] < b[i])

        参数:
        ---------
        >>> b (pd.Series, float, int): 比较序列或数值. 默认: None
            asint (bool, 可选): 是否将结果转为整数(1/0). 默认: True
            offset (int, 可选): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 下穿信号序列，1表示下穿发生，0表示未发生

        所需数据字段:
        ---------
        >>> 序列a (通常是价格序列如close) 和 序列b (比较序列)

        使用案例:
        ---------
        >>> # 检测价格下穿移动平均线
        >>> cross_down_signal = self.data.cross_down(b=self.data.sma(length=20))
        >>> 
        >>> # 使用下穿信号生成卖出信号
        >>> def cross_down_sell_signal(self):
        >>>     if cross_down_signal.new == 1:
        >>>         self.data.sell()
        >>>         return "价格下穿移动平均线，卖出信号"
        """
        ...

    @tobtind(lines=['aber_zg', 'aber_sg', 'aber_xg', 'aber_atr'],overlap=dict(aber_zg=True,aber_sg=True,aber_xg=True,aber_atr=False), lib='pta')
    def aberration(self, length=5, atr_length=15, offset=0, **kwargs) -> IndFrame:
        """
        偏差通道指标 (Aberration)
        ---------
            类似于凯尔特纳通道的波动性指标，用于识别价格突破和趋势变化。

        数据来源:
        ---------
            基于网络资源实现，由Github用户homily请求添加

        计算方法:
        ---------
        >>> Default Inputs: length=5, atr_length=15
            ATR = Average True Range
            SMA = Simple Moving Average
            JG = TP = HLC3(high, low, close)
            ZG = SMA(JG, length)
            SG = ZG + ATR
            XG = ZG - ATR

        参数:
        ---------
        >>> length (int): 中轨计算周期. 默认: 5
            atr_length (int): ATR计算周期. 默认: 15
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含aber_zg(中轨), aber_sg(上轨), aber_xg(下轨), aber_atr(ATR)列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算偏差通道指标
        >>> zg, sg, xg, atr = self.data.aberration(length=5, atr_length=15)
        >>> 
        >>> # 使用偏差通道识别突破
        >>> def aberration_breakout_signal(self):
        >>>     zg, sg, xg, atr = self.data.aberration()
        >>>     if self.data.close.new > sg.new:
        >>>         return "价格突破偏差通道上轨，看涨突破"
        >>>     elif self.data.close.new < xg.new:
        >>>         return "价格跌破偏差通道下轨，看跌突破"
        """
        ...

    @tobtind(lines=['acc_lower', 'acc_mid', 'acc_upper'],overlap=True, lib='pta')
    def accbands(self, length=10, c=4, drift=1, mamode="sma", offset=0, **kwargs) -> IndFrame:
        """
        加速带指标 (Acceleration Bands, ACCBANDS)
        ---------
            Price Headley创建的加速带，在简单移动平均线周围绘制上下包络带。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/acceleration-bands-abands/

        计算方法:
        ---------
        >>> Default Inputs: length=10, c=4
            EMA = Exponential Moving Average
            SMA = Simple Moving Average
            HL_RATIO = c * (high - low) / (high + low)
            LOW = low * (1 - HL_RATIO)
            HIGH = high * (1 + HL_RATIO)
            if mamode == 'ema':
                LOWER = EMA(LOW, length)
                MID = EMA(close, length)
                UPPER = EMA(HIGH, length)
            else:
                LOWER = SMA(LOW, length)
                MID = SMA(close, length)
                UPPER = SMA(HIGH, length)

        参数:
        ---------
        >>> length (int): 移动平均周期. 默认: 10
            c (int): 高低价比率乘数. 默认: 4
            mamode (str): 移动平均模式. 默认: 'sma'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含acc_lower(下轨), acc_mid(中轨), acc_upper(上轨)列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算加速带指标
        >>> lower, mid, upper = self.data.accbands(length=10, c=4)
        >>> 
        >>> # 使用加速带识别趋势加速
        >>> def accbands_trend_acceleration(self):
        >>>     lower, mid, upper = self.data.accbands()
        >>>     if self.data.close.new > upper.new:
        >>>         return "价格突破加速带上轨，加速上涨信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def atr(self, length=14, mamode="rma", talib=True, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        平均真实波幅 (Average True Range, ATR)
        ---------
            用于衡量波动性，特别是由跳空或涨跌停引起的波动性。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Average_True_Range_(ATR)

        计算方法:
        ---------
        >>> Default Inputs: length=14, drift=1, percent=False
            EMA = Exponential Moving Average
            SMA = Simple Moving Average
            WMA = Weighted Moving Average
            RMA = WildeR's Moving Average
            TR = True Range
            tr = TR(high, low, close, drift)
            if mamode == 'ema':
                ATR = EMA(tr, length)
            elif mamode == 'sma':
                ATR = SMA(tr, length)
            elif mamode == 'wma':
                ATR = WMA(tr, length)
            else:
                ATR = RMA(tr, length)
            if percent:
                ATR *= 100 / close

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 14
            mamode (str): 移动平均模式. 默认: 'rma'
            talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> percent (bool, 可选): 是否以百分比形式返回. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算平均真实波幅
        >>> atr_IndSeries = self.data.atr(length=14)
        >>> 
        >>> # 使用ATR设置止损
        >>> def atr_stop_loss(self):
        >>>     atr = self.data.atr(length=14)
        >>>     stop_loss_distance = 2 * atr.new
        >>>     return f"建议止损距离: {stop_loss_distance:.2f}"
        """
        ...

    @tobtind(lines=['bb_lower', 'bb_mid', 'bb_upper', 'bb_width', 'bb_percent'], overlap=dict(bb_lower=True,bb_mid=True,bb_upper=True,bb_width=False,bb_percent=False), lib='pta')
    def bbands(self, length=10, std=2., ddof=0, mamode="sma", talib=True, offset=0, **kwargs) -> IndFrame:
        """
        布林带 (Bollinger Bands, BBANDS)
        ---------
            John Bollinger开发的流行波动性指标。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Bollinger_Bands_(BB)

        计算方法:
        ---------
        >>> Default Inputs: length=5, std=2, mamode="sma", ddof=0
            EMA = Exponential Moving Average
            SMA = Simple Moving Average
            STDEV = Standard Deviation
            stdev = STDEV(close, length, ddof)
            if mamode == "ema":
                MID = EMA(close, length)
            else:
                MID = SMA(close, length)
            LOWER = MID - std * stdev
            UPPER = MID + std * stdev
            BANDWIDTH = 100 * (UPPER - LOWER) / MID
            PERCENT = (close - LOWER) / (UPPER - LOWER)

        参数:
        ---------
        >>> length (int): 移动平均周期. 默认: 5
            std (float): 标准差乘数. 默认: 2.0
            ddof (int): 标准差计算中的自由度. 默认: 0
            mamode (str): 移动平均模式. 默认: 'sma'
            talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含bb_lower(下轨), bb_mid(中轨), bb_upper(上轨), 
                      bb_width(带宽), bb_percent(百分比位置)列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算布林带
        >>> lower, mid, upper, width, percent = self.data.bbands(length=20, std=2)
        >>> 
        >>> # 使用布林带识别超买超卖
        >>> def bbands_overbought_oversold(self):
        >>>     lower, mid, upper, width, percent = self.data.bbands()
        >>>     if self.data.close.new >= upper.new:
        >>>         return "价格触及布林带上轨，可能超买"
        >>>     elif self.data.close.new <= lower.new:
        >>>         return "价格触及布林带下轨，可能超卖"
        """
        ...

    @tobtind(lines=['dc_lower', 'dc_mid', 'dc_upper'],overlap=True, lib='pta')
    def donchian(self, lower_length=20, upper_length=20, offset=0, **kwargs) -> IndFrame:
        """
        唐奇安通道 (Donchian Channels, DC)
        ---------
            用于衡量波动性，基于指定周期内的最高价和最低价。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Donchian_Channels_(DC)

        计算方法:
        ---------
        >>> Default Inputs: lower_length=upper_length=20
            LOWER = low.rolling(lower_length).min()
            UPPER = high.rolling(upper_length).max()
            MID = 0.5 * (LOWER + UPPER)

        参数:
        ---------
        >>> lower_length (int): 下轨计算周期(最低价). 默认: 20
            upper_length (int): 上轨计算周期(最高价). 默认: 20
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含dc_lower(下轨), dc_mid(中轨), dc_upper(上轨)列的数据框

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算唐奇安通道
        >>> lower, mid, upper = self.data.donchian(lower_length=20, upper_length=20)
        >>> 
        >>> # 使用唐奇安通道识别突破
        >>> def donchian_breakout(self):
        >>>     lower, mid, upper = self.data.donchian()
        >>>     if self.data.close.new > upper.new:
        >>>         return "价格突破唐奇安通道上轨，看涨突破"
        >>>     elif self.data.close.new < lower.new:
        >>>         return "价格跌破唐奇安通道下轨，看跌突破"
        """
        ...

    @tobtind(lines=['hwc', 'hwc_upper', 'hwc_lower','hwc_width','hwc_pctwidth'], overlap=dict(hwc=True,hwc_upper=True,hwc_lower=True,hwc_width=False,hwc_pctwidth=False), lib='pta')
    def hwc(self, na=0.2, nb=0.1, nc=0.1, nd=0.1, scalar=1., channel_eval=False, offset=0, **kwargs) -> IndFrame:
        """
        霍尔特-温特斯通道 (Holt-Winter Channel, HWC)
        ---------
            基于HWMA的通道指标，使用霍尔特-温特斯方法计算。

        数据来源:
        ---------
            https://www.mql5.com/en/code/20857

        计算方法:
        ---------
        >>> HWMA[i] = F[i] + V[i] + 0.5 * A[i]
            where..
            F[i] = (1-na) * (F[i-1] + V[i-1] + 0.5 * A[i-1]) + na * Price[i]
            V[i] = (1-nb) * (V[i-1] + A[i-1]) + nb * (F[i] - F[i-1])
            A[i] = (1-nc) * A[i-1] + nc * (V[i] - V[i-1])
            Top = HWMA + Multiplier * StDt
            Bottom = HWMA - Multiplier * StDt
            where..
            StDt[i] = Sqrt(Var[i-1])
            Var[i] = (1-d) * Var[i-1] + nD * (Price[i-1] - HWMA[i-1]) * (Price[i-1] - HWMA[i-1])

        参数:
        ---------
        >>> na (float): 平滑序列参数 (0到1). 默认: 0.2
            nb (float): 趋势参数 (0到1). 默认: 0.1
            nc (float): 季节性参数 (0到1). 默认: 0.1
            nd (float): 通道方程参数 (0到1). 默认: 0.1
            scalar (float): 通道宽度乘数. 默认: 1.0
            channel_eval (bool): 是否返回宽度和百分比位置. 默认: False
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含hwc(中轨), hwc_upper(上轨), hwc_lower(下轨)列的数据框

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算霍尔特-温特斯通道
        >>> hwc, upper, lower = self.data.hwc(na=0.2, nb=0.1, nc=0.1, nd=0.1)
        >>> 
        >>> # 使用HWC通道进行交易
        >>> def hwc_trading_signals(self):
        >>>     hwc, upper, lower = self.data.hwc()
        >>>     if self.data.close.new > upper.new:
        >>>         return "价格突破HWC上轨，买入信号"
        >>>     elif self.data.close.new < lower.new:
        >>>         return "价格跌破HWC下轨，卖出信号"
        """
        ...

    @tobtind(lines=['kc_lower', 'kc_basis', 'kc_upper'], overlap=True,lib='pta')
    def kc(self, length=20, scalar=2., mamode="ema", offset=0, **kwargs) -> IndFrame:
        """
        凯尔特纳通道 (Keltner Channels, KC)
        ---------
            流行的波动性指标，类似于布林带和唐奇安通道。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Keltner_Channels_(KC)

        计算方法:
        ---------
        >>> Default Inputs: length=20, scalar=2, mamode=None, tr=True
            TR = True Range
            SMA = Simple Moving Average
            EMA = Exponential Moving Average
            if tr:
                RANGE = TR(high, low, close)
            else:
                RANGE = high - low
            if mamode == "ema":
                BASIS = sma(close, length)
                BAND = sma(RANGE, length)
            elif mamode == "sma":
                BASIS = sma(close, length)
                BAND = sma(RANGE, length)
            LOWER = BASIS - scalar * BAND
            UPPER = BASIS + scalar * BAND

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 20
            scalar (float): 通道宽度乘数. 默认: 2.0
            mamode (str): 移动平均模式. 默认: 'ema'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> tr (bool): 是否使用真实波幅计算. 默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含kc_lower(下轨), kc_basis(中轨), kc_upper(上轨)列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算凯尔特纳通道
        >>> lower, basis, upper = self.data.kc(length=20, scalar=2)
        >>> 
        >>> # 使用凯尔特纳通道识别趋势
        >>> def kc_trend_identification(self):
        >>>     lower, basis, upper = self.data.kc()
        >>>     if self.data.close.new > basis.new:
        >>>         return "价格在凯尔特纳通道中轨上方，上升趋势"
        >>>     elif self.data.close.new < basis.new:
        >>>         return "价格在凯尔特纳通道中轨下方，下降趋势"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def massi(self, fast=9, slow=25, offset=0, **kwargs) -> IndSeries:
        """
        质量指数 (Mass Index, MASSI)
        ---------
            非定向波动性指标，利用高低价范围识别基于范围扩张的趋势反转。

        数据来源:
        ---------
            https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:mass_index

        计算方法:
        ---------
        >>> Default Inputs: fast=9, slow=25
            EMA = Exponential Moving Average
            hl = high - low
            hl_ema1 = EMA(hl, fast)
            hl_ema2 = EMA(hl_ema1, fast)
            hl_ratio = hl_ema1 / hl_ema2
            MASSI = SUM(hl_ratio, slow)

        参数:
        ---------
        >>> fast (int): 快速周期. 默认: 9
            slow (int): 慢速周期. 默认: 25
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算质量指数
        >>> massi_IndSeries = self.data.massi(fast=9, slow=25)
        >>> 
        >>> # 使用质量指数识别反转
        >>> def massi_reversal_signal(self):
        >>>     massi = self.data.massi()
        >>>     if massi.new > 27:
        >>>         return "质量指数高于27，可能出现趋势反转"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def natr(self, length=20, scalar=100., mamode="ema", talib=True, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        标准化平均真实波幅 (Normalized Average True Range, NATR)
        ---------
            试图标准化平均真实波幅。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/normalized-average-true-range-natr/

        计算方法:
        ---------
        >>> Default Inputs: length=20
            ATR = Average True Range
            NATR = (100 / close) * ATR(high, low, close)

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 20
            scalar (float): 放大倍数. 默认: 100.0
            mamode (str): 移动平均模式. 默认: 'ema'
            talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算标准化ATR
        >>> natr_IndSeries = self.data.natr(length=20)
        >>> 
        >>> # 使用NATR进行跨品种比较
        >>> def natr_cross_instrument_comparison(self):
        >>>     natr = self.data.natr(length=20)
        >>>     return f"标准化波动率: {natr.new:.2f}%"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def pdist(self, drift=10, offset=0, **kwargs) -> IndSeries:
        """
        价格距离指标 (Price Distance, PDIST)
        ---------
            衡量价格运动所覆盖的"距离"。

        数据来源:
        ---------
            https://www.prorealcode.com/prorealtime-indicators/pricedistance/

        计算方法:
        ---------
        >>> Default Inputs: drift=1
            PDIST = 2(high - low) - ABS(close - open) + ABS(open - close[drift])

        参数:
        ---------
        >>> drift (int): 差异周期. 默认: 10
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 计算价格距离指标
        >>> pdist_IndSeries = self.data.pdist(drift=10)
        >>> 
        >>> # 使用价格距离分析市场活跃度
        >>> def pdist_market_activity(self):
        >>>     pdist = self.data.pdist(drift=10)
        >>>     if pdist.new > pdist.ema(period=20).new:
        >>>         return "价格距离扩大，市场活跃度增加"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def rvi(self, length=14, scalar=100., refined=False, thirds=False, mamode="ema",
            drift=1, offset=0, **kwargs) -> IndSeries:
        """
        相对波动指数 (Relative Volatility Index, RVI)
        ---------
            1993年创建，1995年修订。
            不像RSI基于价格方向累加价格变化，RVI基于价格方向累加标准差。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Relative_Volatility_Index_(RVI)

        计算方法:
        ---------
        >>> Default Inputs: length=14, scalar=100, refined=None, thirds=None
            EMA = Exponential Moving Average
            STDEV = Standard Deviation
            UP = STDEV(src, length) IF src.diff() > 0 ELSE 0
            DOWN = STDEV(src, length) IF src.diff() <= 0 ELSE 0
            UPSUM = EMA(UP, length)
            DOWNSUM = EMA(DOWN, length)
            RVI = scalar * (UPSUM / (UPSUM + DOWNSUM))

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 14
            scalar (float): 放大倍数. 默认: 100.0
            refined (bool): 使用'精炼'计算，即RVI(high)和RVI(low)的平均值. 默认: False
            thirds (bool): 最高价、最低价和收盘价的平均值. 默认: False
            mamode (str): 移动平均模式. 默认: 'ema'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算相对波动指数
        >>> rvi_IndSeries = self.data.rvi(length=14)
        >>> 
        >>> # 使用RVI识别波动性趋势
        >>> def rvi_volatility_trend(self):
        >>>     rvi = self.data.rvi(length=14)
        >>>     if rvi.new > 50:
        >>>         return "RVI高于50，波动性偏向上升"
        >>>     elif rvi.new < 50:
        >>>         return "RVI低于50，波动性偏向下降"
        """
        ...

    @tobtind(lines=['thermo', 'thermo_ma', 'thermo_long', 'thermo_short'],overlap=dict(thermo=True,thermo_ma=True,thermo_long=False,thermo_short=False), lib='pta')
    def thermo(self, length=20, long=2., short=0.5, mamode="ema", drift=1, offset=0, **kwargs) -> IndFrame:
        """
        埃尔德温度计 (Elder's Thermometer, THERMO)
        ---------
            衡量价格波动性。

        数据来源:
        ---------
        - https://www.motivewave.com/studies/elders_thermometer.htm
        - https://www.tradingview.com/script/HqvTuEMW-Elder-s-Market-Thermometer-LazyBear/

        计算方法:
        ---------
        >>> Default Inputs: length=20, drift=1, mamode=EMA, long=2, short=0.5
            EMA = Exponential Moving Average
            thermoL = (low.shift(drift) - low).abs()
            thermoH = (high - high.shift(drift)).abs()
            thermo = np.where(thermoH > thermoL, thermoH, thermoL)
            thermo_ma = ema(thermo, length)
            thermo_long = thermo < (thermo_ma * long)
            thermo_short = thermo > (thermo_ma * short)
            thermo_long = thermo_long.astype(int)
            thermo_short = thermo_short.astype(int)

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 20
            long (float): 买入因子. 默认: 2.0
            short (float): 卖出因子. 默认: 0.5
            mamode (str): 移动平均模式. 默认: 'ema'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含thermo(温度计), thermo_ma(移动平均), 
                      thermo_long(多头信号), thermo_short(空头信号)列的数据框

        所需数据字段:
        ---------
        >>> high, low

        使用案例:
        ---------
        >>> # 计算埃尔德温度计
        >>> thermo, thermo_ma, thermo_long, thermo_short = self.data.thermo(length=20)
        >>> 
        >>> # 使用温度计识别市场状态
        >>> def thermo_market_state(self):
        >>>     thermo, thermo_ma, thermo_long, thermo_short = self.data.thermo()
        >>>     if thermo_long.new == 1:
        >>>         return "温度计显示低波动，适合买入"
        >>>     elif thermo_short.new == 1:
        >>>         return "温度计显示高波动，注意风险"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def true_range(self, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        真实波幅 (True Range)
        ---------
            将经典波幅（最高价减最低价）扩展到包括可能的跳空情况。

        数据来源:
        ---------
            https://www.macroption.com/true-range/

        计算方法:
        ---------
        >>> Default Inputs: drift=1
            ABS = Absolute Value
            prev_close = close.shift(drift)
            TRUE_RANGE = ABS([high - low, high - prev_close, low - prev_close])

        参数:
        ---------
        >>> drift (int): 偏移周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 计算真实波幅
        >>> tr_IndSeries = self.data.true_range(drift=1)
        >>> 
        >>> # 使用真实波幅分析波动性
        >>> def true_range_volatility(self):
        >>>     tr = self.data.true_range()
        >>>     return f"当前真实波幅: {tr.new:.2f}"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def ui(self, length=14, scalar=100., offset=0, **kwargs) -> IndSeries:
        """
        溃疡指数 (Ulcer Index, UI)
        ---------
        - Peter Martin开发的溃疡指数使用二次均值衡量下行波动性，
        - 具有强调大幅回撤的效果。

        数据来源:
        ---------
        - https://library.tradingtechnologies.com/trade/chrt-ti-ulcer-index.html
        - https://en.wikipedia.org/wiki/Ulcer_index
        - http://www.tangotools.com/ui/ui.htm

        计算方法:
        ---------
        >>> Default Inputs: length=14, scalar=100
            HC = Highest Close
            SMA = Simple Moving Average
            HCN = HC(close, length)
            DOWNSIDE = scalar * (close - HCN) / HCN
            if kwargs["everget"]:
                UI = SQRT(SMA(DOWNSIDE^2, length) / length)
            else:
                UI = SQRT(SUM(DOWNSIDE^2, length) / length)

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 14
            scalar (float): 放大倍数. 默认: 100.0
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> everget (bool, 可选): 使用TradingView的Everget的SMA计算而不是SUM计算. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close

        使用案例:
        ---------
        >>> # 计算溃疡指数
        >>> ui_IndSeries = self.data.ui(length=14)
        >>> 
        >>> # 使用溃疡指数评估风险
        >>> def ui_risk_assessment(self):
        >>>     ui = self.data.ui(length=14)
        >>>     if ui.new < 5:
        >>>         return "溃疡指数较低，风险可控"
        >>>     elif ui.new > 10:
        >>>         return "溃疡指数较高，注意下行风险"
        """
        ...

    # Volume
    @tobtind(lines=None, lib='pta')
    def ad(self, open_=None, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        累积/派发线 (Accumulation/Distribution, AD)
        ---------
            利用收盘价相对于其高低价范围的位置与成交量，然后进行累积。

        数据来源:
        ---------
            https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/accumulationdistribution-ad/

        计算方法:
        ---------
        >>> CUM = Cumulative Sum
            if 'open':
                AD = close - open
            else:
                AD = 2 * close - high - low
            hl_range = high - low
            AD = AD * volume / hl_range
            AD = CUM(AD)

        参数:
        ---------
        >>> open_ (pd.Series): 开盘价序列. 默认: None
            talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, high, low, close, volume

        使用案例:
        ---------
        >>> # 计算累积/派发线
        >>> ad_IndSeries = self.data.ad()
        >>> 
        >>> # 使用AD线分析资金流向
        >>> def ad_money_flow(self):
        >>>     ad = self.data.ad()
        >>>     if ad.new > ad.prev:
        >>>         return "AD线上涨，资金在累积"
        >>>     elif ad.new < ad.prev:
        >>>         return "AD线下跌，资金在派发"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def adosc(self, open_=None, fast=12, slow=26, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        累积/派发震荡指标或蔡金震荡指标 (Accumulation/Distribution Oscillator or Chaikin Oscillator)
        ---------
            利用累积/派发线，类似于MACD或APO的处理方式。

        数据来源:
        ---------
            https://www.investopedia.com/articles/active-trading/031914/understanding-chaikin-oscillator.asp

        计算方法:
        ---------
        >>> Default Inputs: fast=12, slow=26
            AD = Accum/Dist
            ad = AD(high, low, close, open)
            fast_ad = EMA(ad, fast)
            slow_ad = EMA(ad, slow)
            ADOSC = fast_ad - slow_ad

        参数:
        ---------
        >>> open_ (pd.Series): 开盘价序列. 默认: None
            fast (int): 快速周期. 默认: 12
            slow (int): 慢速周期. 默认: 26
            talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, high, low, close, volume

        使用案例:
        ---------
        >>> # 计算蔡金震荡指标
        >>> adosc_IndSeries = self.data.adosc(fast=12, slow=26)
        >>> 
        >>> # 使用蔡金震荡指标识别买卖点
        >>> def adosc_trading_signals(self):
        >>>     adosc = self.data.adosc()
        >>>     if adosc.new > 0 and adosc.prev <= 0:
        >>>         return "蔡金震荡指标上穿零轴，买入信号"
        >>>     elif adosc.new < 0 and adosc.prev >= 0:
        >>>         return "蔡金震荡指标下穿零轴，卖出信号"
        """
        ...

    @tobtind(lines=['obv_min', 'obv_max', 'obv_maf', 'obv_mas', 'obv_long', 'obv_short'], lib='pta')
    def aobv(self, fast=4, slow=12, max_lookback=2, min_lookback=2, mamode="ema", offset=0, **kwargs) -> IndFrame:
        """
        阿切尔能量潮指标 (Archer On Balance Volume, AOBV)
        ---------
            基于能量潮(OBV)的增强指标，包含多个信号线。

        参数:
        ---------
        >>> fast (int): 快速移动平均周期. 默认: 4
            slow (int): 慢速移动平均周期. 默认: 12
            max_lookback (int): 最大值回溯周期. 默认: 2
            min_lookback (int): 最小值回溯周期. 默认: 2
            mamode (str): 移动平均模式. 默认: 'ema'
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含obv_min(最小值), obv_max(最大值), obv_maf(快速移动平均),
                      obv_mas(慢速移动平均), obv_long(多头信号), obv_short(空头信号)列的数据框

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算阿切尔能量潮指标
        >>> obv_min, obv_max, obv_maf, obv_mas, obv_long, obv_short = self.data.aobv(fast=4, slow=12)
        >>> 
        >>> # 使用AOBV进行交易决策
        >>> def aobv_trading_decision(self):
        >>>     obv_min, obv_max, obv_maf, obv_mas, obv_long, obv_short = self.data.aobv()
        >>>     if obv_long.new == 1:
        >>>         return "AOBV发出多头信号"
        >>>     elif obv_short.new == 1:
        >>>         return "AOBV发出空头信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def cmf(self, open_=None, length=20, offset=0, **kwargs) -> IndSeries:
        """
        蔡金资金流 (Chaikin Money Flow, CMF)
        ---------
            衡量特定时期内资金流量的多少，与累积/派发线结合使用。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Chaikin_Money_Flow_(CMF)
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:chaikin_money_flow_cmf

        计算方法:
        ---------
        >>> Default Inputs: length=20
            if 'open':
                ad = close - open
            else:
                ad = 2 * close - high - low
            hl_range = high - low
            ad = ad * volume / hl_range
            CMF = SUM(ad, length) / SUM(volume, length)

        参数:
        ---------
        >>> open_ (pd.Series): 开盘价序列. 默认: None
            length (int): 计算周期. 默认: 20
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> open, high, low, close, volume

        使用案例:
        ---------
        >>> # 计算蔡金资金流
        >>> cmf_IndSeries = self.data.cmf(length=20)
        >>> 
        >>> # 使用CMF分析资金流向强度
        >>> def cmf_money_flow_strength(self):
        >>>     cmf = self.data.cmf(length=20)
        >>>     if cmf.new > 0.1:
        >>>         return "CMF大于0.1，强势资金流入"
        >>>     elif cmf.new < -0.1:
        >>>         return "CMF小于-0.1，强势资金流出"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def efi(self, length=13, mamode="ema", drift=1, offset=0, **kwargs) -> IndSeries:
        """
        埃尔德力量指数 (Elder's Force Index, EFI)
        ---------
            使用价格和成交量衡量价格运动背后的力量，以及潜在的反转和价格修正。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Elder%27s_Force_Index_(EFI)
        - https://www.motivewave.com/studies/elders_force_index.htm

        计算方法:
        ---------
        >>> Default Inputs: length=20, drift=1, mamode=None
            EMA = Exponential Moving Average
            SMA = Simple Moving Average
            pv_diff = close.diff(drift) * volume
            if mamode == 'sma':
                EFI = SMA(pv_diff, length)
            else:
                EFI = EMA(pv_diff, length)

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 13
            mamode (str): 移动平均模式. 默认: 'ema'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算埃尔德力量指数
        >>> efi_IndSeries = self.data.efi(length=13)
        >>> 
        >>> # 使用EFI识别趋势强度
        >>> def efi_trend_strength(self):
        >>>     efi = self.data.efi(length=13)
        >>>     if efi.new > 0:
        >>>         return "EFI为正，上升趋势力量强劲"
        >>>     elif efi.new < 0:
        >>>         return "EFI为负，下降趋势力量强劲"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def eom(self, length=14, divisor=100, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        简易波动指标 (Ease of Movement, EOM)
        ---------
            基于成交量的震荡指标，旨在衡量价格和成交量在零线上下波动的关系。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/Ease_of_Movement_(EOM)
        - https://www.motivewave.com/studies/ease_of_movement.htm
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:ease_of_movement_emv

        计算方法:
        ---------
        >>> Default Inputs: length=14, divisor=100000000, drift=1
            SMA = Simple Moving Average
            hl_range = high - low
            distance = 0.5 * (high - high.shift(drift) + low - low.shift(drift))
            box_ratio = (volume / divisor) / hl_range
            eom = distance / box_ratio
            EOM = SMA(eom, length)

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 14
            divisor (int): 成交量除数. 默认: 100
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close, volume

        使用案例:
        ---------
        >>> # 计算简易波动指标
        >>> eom_IndSeries = self.data.eom(length=14, divisor=100)
        >>> 
        >>> # 使用EOM识别趋势强度
        >>> def eom_trend_strength(self):
        >>>     eom = self.data.eom(length=14)
        >>>     if eom.new > 0:
        >>>         return "EOM为正，上升趋势容易形成"
        >>>     elif eom.new < 0:
        >>>         return "EOM为负，下降趋势容易形成"
        """
        ...

    @tobtind(lines=['kvo', 'kvos'], lib='pta')
    def kvo(self, fast=34, slow=55, signal=13, mamode="ema", drift=1, offset=0, **kwargs) -> IndFrame:
        """
        克林格成交量震荡指标 (Klinger Volume Oscillator, KVO)
        ---------
            Stephen J. Klinger开发，旨在通过比较成交量和价格来预测市场中的价格反转。

        数据来源:
        ---------
        - https://www.investopedia.com/terms/k/klingeroscillator.asp
        - https://www.daytrading.com/klinger-volume-oscillator

        计算方法:
        ---------
        >>> Default Inputs: fast=34, slow=55, signal=13, drift=1
            EMA = Exponential Moving Average
            SV = volume * signed_IndSeries(HLC3, 1)
            KVO = EMA(SV, fast) - EMA(SV, slow)
            Signal = EMA(KVO, signal)

        参数:
        ---------
        >>> fast (int): 快速周期. 默认: 34
            slow (int): 慢速周期. 默认: 55
            signal (int): 信号周期. 默认: 13
            mamode (str): 移动平均模式. 默认: 'ema'
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含kvo(主指标), kvos(信号线)列的数据框

        所需数据字段:
        ---------
        >>> high, low, close, volume

        使用案例:
        ---------
        >>> # 计算克林格成交量震荡指标
        >>> kvo, kvos = self.data.kvo(fast=34, slow=55, signal=13)
        >>> 
        >>> # 使用KVO识别买卖信号
        >>> def kvo_trading_signals(self):
        >>>     kvo, kvos = self.data.kvo()
        >>>     if kvo.new > kvos.new and kvo.prev <= kvos.prev:
        >>>         return "KVO上穿信号线，买入信号"
        >>>     elif kvo.new < kvos.new and kvo.prev >= kvos.prev:
        >>>         return "KVO下穿信号线，卖出信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def mfi(self, length=14, talib=True, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        资金流量指数 (Money Flow Index, MFI)
        ---------
            震荡指标，通过利用价格和成交量来衡量买卖压力。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Money_Flow_(MFI)

        计算方法:
        ---------
        >>> Default Inputs: length=14, drift=1
            tp = typical_price = hlc3 = (high + low + close) / 3
            rmf = raw_money_flow = tp * volume
            pmf = pos_money_flow = SUM(rmf, length) if tp.diff(drift) > 0 else 0
            nmf = neg_money_flow = SUM(rmf, length) if tp.diff(drift) < 0 else 0
            MFR = money_flow_ratio = pmf / nmf
            MFI = money_flow_index = 100 * pmf / (pmf + nmf)

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 14
            talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> high, low, close, volume

        使用案例:
        ---------
        >>> # 计算资金流量指数
        >>> mfi_IndSeries = self.data.mfi(length=14)
        >>> 
        >>> # 使用MFI识别超买超卖
        >>> def mfi_overbought_oversold(self):
        >>>     mfi = self.data.mfi(length=14)
        >>>     if mfi.new > 80:
        >>>         return "MFI大于80，超买状态"
        >>>     elif mfi.new < 20:
        >>>         return "MFI小于20，超卖状态"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def nvi(self, length=13, initial=1000, offset=0, **kwargs) -> IndSeries:
        """
        负成交量指数 (Negative Volume Index, NVI)
        ---------
            累积指标，使用成交量变化来尝试识别聪明资金活跃的位置。

        数据来源:
        ---------
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:negative_volume_inde
        - https://www.motivewave.com/studies/negative_volume_index.htm

        计算方法:
        ---------
        >>> Default Inputs: length=1, initial=1000
            ROC = Rate of Change
            roc = ROC(close, length)
            signed_volume = signed_IndSeries(volume, initial=1)
            nvi = signed_volume[signed_volume < 0].abs() * roc_
            nvi.fillna(0, inplace=True)
            nvi.iloc[0]= initial
            nvi = nvi.cumsum()

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 13
            initial (int): 初始值. 默认: 1000
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算负成交量指数
        >>> nvi_IndSeries = self.data.nvi(length=13, initial=1000)
        >>> 
        >>> # 使用NVI识别聪明资金行为
        >>> def nvi_smart_money(self):
        >>>     nvi = self.data.nvi(length=13)
        >>>     if nvi.new > nvi.ema(period=20).new:
        >>>         return "NVI上升，聪明资金可能在累积"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def obv(self, talib=True, offset=0, **kwargs) -> IndSeries:
        """
        能量潮指标 (On Balance Volume, OBV)
        ---------
            累积指标，用于衡量买卖压力。

        数据来源:
        ---------
        - https://www.tradingview.com/wiki/On_Balance_Volume_(OBV)
        - https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/on-balance-volume-obv/
        - https://www.motivewave.com/studies/on_balance_volume.htm

        计算方法:
        ---------
        >>> signed_volume = signed_IndSeries(close, initial=1) * volume
            obv = signed_volume.cumsum()

        参数:
        ---------
        >>> talib (bool): 如果安装了TA Lib且talib为True，返回TA Lib版本. 默认: True
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算能量潮指标
        >>> obv_IndSeries = self.data.obv()
        >>> 
        >>> # 使用OBV确认价格趋势
        >>> def obv_confirmation(self):
        >>>     obv = self.data.obv()
        >>>     if self.data.close.new > self.data.close.prev and obv.new > obv.prev:
        >>>         return "价格上涨且OBV上升，趋势确认"
        >>>     elif self.data.close.new < self.data.close.prev and obv.new < obv.prev:
        >>>         return "价格下跌且OBV下降，趋势确认"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def pvi(self, length=13, initial=1000, offset=0, **kwargs) -> IndSeries:
        """
        正成交量指数 (Positive Volume Index, PVI)
        ---------
        - 累积指标，使用成交量变化来尝试识别聪明资金活跃的位置。
        - 与NVI结合使用。

        数据来源:
        ---------
            https://www.investopedia.com/terms/p/pvi.asp

        计算方法:
        ---------
        >>> Default Inputs: length=1, initial=1000
            ROC = Rate of Change
            roc = ROC(close, length)
            signed_volume = signed_IndSeries(volume, initial=1)
            pvi = signed_volume[signed_volume > 0].abs() * roc_
            pvi.fillna(0, inplace=True)
            pvi.iloc[0]= initial
            pvi = pvi.cumsum()

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 13
            initial (int): 初始值. 默认: 1000
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算正成交量指数
        >>> pvi_IndSeries = self.data.pvi(length=13, initial=1000)
        >>> 
        >>> # 使用PVI和NVI综合分析
        >>> def pvi_nvi_analysis(self):
        >>>     pvi = self.data.pvi()
        >>>     nvi = self.data.nvi()
        >>>     if pvi.new > pvi.prev and nvi.new > nvi.prev:
        >>>         return "PVI和NVI同时上升，强烈看涨信号"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def pvol(self, offset=0, **kwargs) -> IndSeries:
        """
        价格-成交量指标 (Price-Volume, PVOL)
        ---------
            返回价格和成交量的乘积序列。

        计算方法:
        ---------
        >>> if signed:
                pvol = signed_IndSeries(close, 1) * close * volume
            else:
                pvol = close * volume

        参数:
        ---------
        >>> offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> signed (bool): 保持收盘价差异的符号. 默认: True
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算价格-成交量指标
        >>> pvol_IndSeries = self.data.pvol()
        >>> 
        >>> # 使用PVOL分析成交金额
        >>> def pvol_analysis(self):
        >>>     pvol = self.data.pvol()
        >>>     return f"当前成交金额: {pvol.new:.2f}"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def pvr(self, **kwargs) -> IndSeries:
        """
        价格成交量排名 (Price Volume Rank)
        ---------
        - Anthony J. Macek开发，描述在1994年6月的《股票与商品技术分析》杂志文章中。
        - 基本解释是当PV排名低于2.5时买入，高于2.5时卖出。

        数据来源:
        ---------
            https://www.fmlabs.com/reference/default.htm?url=PVrank.htm

        计算方法:
        ---------
        >>> return 1 if 'close change' >= 0 and 'volume change' >= 0
            return 2 if 'close change' >= 0 and 'volume change' < 0
            return 3 if 'close change' < 0 and 'volume change' >= 0
            return 4 if 'close change' < 0 and 'volume change' < 0

        参数:
        ---------
        >>> 无额外参数

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算价格成交量排名
        >>> pvr_IndSeries = self.data.pvr()
        >>> 
        >>> # 使用PVR进行交易决策
        >>> def pvr_trading_decision(self):
        >>>     pvr = self.data.pvr()
        >>>     if pvr.new < 2.5:
        >>>         return "PVR低于2.5，考虑买入"
        >>>     elif pvr.new > 2.5:
        >>>         return "PVR高于2.5，考虑卖出"
        """
        ...

    @tobtind(lines=None, lib='pta')
    def pvt(self, drift=1, offset=0, **kwargs) -> IndSeries:
        """
        价量趋势指标 (Price-Volume Trend, PVT)
        ---------
            利用变动率和成交量及其累积值来确定资金流。

        数据来源:
        ---------
            https://www.tradingview.com/wiki/Price_Volume_Trend_(PVT)

        计算方法:
        ---------
        >>> Default Inputs: drift=1
            ROC = Rate of Change
            pv = ROC(close, drift) * volume
            PVT = pv.cumsum()

        参数:
        ---------
        >>> drift (int): 差异周期. 默认: 1
            offset (int): 结果偏移周期数. 默认: 0

        可选参数:
        ---------
        >>> fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndSeries: 生成的新特征序列

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算价量趋势指标
        >>> pvt_IndSeries = self.data.pvt(drift=1)
        >>> 
        >>> # 使用PVT分析资金流向
        >>> def pvt_money_flow(self):
        >>>     pvt = self.data.pvt()
        >>>     if pvt.new > pvt.prev:
        >>>         return "PVT上升，资金流入"
        >>>     elif pvt.new < pvt.prev:
        >>>         return "PVT下降，资金流出"
        """
        ...

    @tobtind(lines=['low_price', 'mean_price', 'high_price', 'pos_volume', 'neg_volume', 'total_volume'],
             overlap=dict(low_price=True,mean_price=True,high_price=True,pos_volume=False,neg_volume=False,total_volume=False),lib='pta')
    def vp(self, width=10, **kwargs) -> IndFrame:
        """
        成交量分布图 (Volume Profile, VP)
        ---------
        - 通过将价格划分为范围来计算成交量分布图。
        - 注意：未计算价值区域。

        数据来源:
        ---------
        - https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:volume_by_price
        - https://www.tradingview.com/wiki/Volume_Profile
        - http://www.ranchodinero.com/volume-tpo-essentials/
        - https://www.tradingtechnologies.com/blog/2013/05/15/volume-at-price/

        计算方法:
        ---------
        >>> Default Inputs: width=10
            vp = pd.concat([close, pos_volume, neg_volume], axis=1)
            if sort_close:
                vp_ranges = cut(vp[close_col], width)
                result = ({range_left, mean_close, range_right, pos_volume, neg_volume} foreach range in vp_ranges
            else:
                vp_ranges = np.array_split(vp, width)
                result = ({low_close, mean_close, high_close, pos_volume, neg_volume} foreach range in vp_ranges
            vpdf = pd.DataFrame(result)
            vpdf['total_volume'] = vpdf['pos_volume'] + vpdf['neg_volume']

        参数:
        ---------
        >>> width (int): 将价格分布到的范围数量. 默认: 10

        可选参数:
        ---------
        >>> sort_close (bool, 可选): 在分割为范围之前是否按收盘价排序. 默认: False
            fillna (value, 可选): pd.DataFrame.fillna(value)
            fill_method (value, 可选): 填充方法类型

        返回:
        ---------
        >>> IndFrame: 包含low_price(最低价), mean_price(平均价), high_price(最高价),
                      pos_volume(正成交量), neg_volume(负成交量), total_volume(总成交量)列的数据框

        所需数据字段:
        ---------
        >>> close, volume

        使用案例:
        ---------
        >>> # 计算成交量分布图
        >>> low_price, mean_price, high_price, pos_volume, neg_volume, total_volume = self.data.vp(width=10)
        >>> 
        >>> # 使用VP分析支撑阻力
        >>> def vp_support_resistance(self):
        >>>     low_price, mean_price, high_price, pos_volume, neg_volume, total_volume = self.data.vp()
        >>>     # 寻找高成交量区域作为支撑阻力
        >>>     max_volume_idx = total_volume.idxmax()
        >>>     return f"高成交量区域在价格 {mean_price[max_volume_idx]:.2f} 附近"
        """
        ...

    @tobtind(lines=None, lib="pta")
    def line_trhend(self, period: int = 1, **kwargs) -> IndSeries:
        """
        指标线趋势判断 (Indicator Line Trend)
        ---------
        - 与前period个数据对比，判断当前值的趋势方向。
        - 比前值大为1，持平为0，小为-1。

        参数:
        ---------
        >>> period (int): 对比周期. 默认: 1
            **kwargs: 其他参数

        返回:
        ---------
        >>> IndSeries: 趋势方向序列 (1: 上升, 0: 持平, -1: 下降)

        使用案例:
        ---------
        >>> # 计算收盘价趋势
        >>> close_trend = self.data.line_trhend(period=1)
        >>> 
        >>> # 计算移动平均线趋势
        >>> ema_trend = self.data.ema(length=20).line_trhend(period=2)
        >>> 
        >>> def trend_analysis(self):
        >>>     # 分析RSI趋势
        >>>     rsi = self.data.rsi(length=14)
        >>>     rsi_trend = rsi.line_trhend(period=3)
        >>>     
        >>>     if rsi_trend.new == 1:
        >>>         return "RSI趋势向上"
        >>>     elif rsi_trend.new == -1:
        >>>         return "RSI趋势向下"
        >>>     else:
        >>>         return "RSI趋势持平"
        >>> 
        >>> # 多周期趋势分析
        >>> def multi_period_trend(self):
        >>>     short_trend = self.data.close.line_trhend(period=1)  # 短期趋势
        >>>     medium_trend = self.data.close.line_trhend(period=5)  # 中期趋势
        >>>     long_trend = self.data.close.line_trhend(period=10)  # 长期趋势
        >>>     
        >>>     # 多重时间框架趋势一致
        >>>     if short_trend.new == 1 and medium_trend.new == 1 and long_trend.new == 1:
        >>>         return "多重时间框架均显示上升趋势"
        """
        ...

    @tobtind(lines=['open', 'high', 'low', 'close'], category='candles')
    def abc(self, lim: float = 5., **kwargs) -> IndFrame:
        """
        ABC模式识别 (ABC Pattern Recognition)
        ---------
            识别K线图中的ABC模式。

        参数:
        ---------
        >>> lim (float): 限制参数. 默认: 5.0
            **kwargs: 其他参数

        返回:
        ---------
        >>> IndFrame: 包含open(开盘价), high(最高价), low(最低价), close(收盘价)列的数据框

        所需数据字段:
        ---------
        >>> open, high, low, close

        使用案例:
        ---------
        >>> # 识别ABC模式
        >>> open, high, low, close = self.data.abc(lim=5.0)
        >>> 
        >>> def abc_pattern_strategy(self):
        >>>     # 获取ABC模式数据
        >>>     open, high, low, close = self.data.abc(lim=5.0)
        >>>     
        >>>     # 分析ABC模式特征
        >>>     # 这里可以添加具体的ABC模式识别逻辑
        >>>     # 例如：寻找特定的高低点序列
        >>>     
        >>>     return "ABC模式分析完成"
        >>> 
        >>> # 结合其他指标使用
        >>> def abc_with_volume(self):
        >>>     abc_data = self.data.abc(lim=5.0)
        >>>     volume_ma = self.data.volume.ema(period=20)
        >>>     
        >>>     # 在ABC模式形成时成交量放大
        >>>     if abc_data.close.new > abc_data.close.prev and \
        >>>        self.data.volume.new > volume_ma.new:
        >>>         return "ABC模式伴随放量，信号增强"
        """
        ...

    @tobtind(lines=['thrend', 'line'])
    def insidebar(self, length: int = 10, **kwargs) -> IndFrame:
        """
        内包线模式识别 (Inside Bar Pattern Recognition)
        ---------
        - 识别K线图中的内包线模式。内包线是指当前K线的最高价和最低价
        - 完全包含在前一根K线的价格范围内。

        参数:
        ---------
        >>> length (int): 计算周期. 默认: 10
            **kwargs: 其他参数

        返回:
        ---------
        >>> IndFrame: 包含thrend(趋势), line(线)列的数据框

        所需数据字段:
        ---------
        >>> high, low, close

        使用案例:
        ---------
        >>> # 识别内包线模式
        >>> thrend, line = self.data.insidebar(length=10)
        >>> 
        >>> def insidebar_trading(self):
        >>>     # 获取内包线信号
        >>>     thrend, line = self.data.insidebar(length=10)
        >>>     
        >>>     # 内包线通常表示市场犹豫，可能预示着突破
        >>>     if thrend.new == 1:
        >>>         return "内包线显示看涨趋势"
        >>>     elif thrend.new == -1:
        >>>         return "内包线显示看跌趋势"
        >>> 
        >>> # 内包线突破策略
        >>> def insidebar_breakout(self):
        >>>     thrend, line = self.data.insidebar()
        >>>     
        >>>     # 内包线后的突破
        >>>     if thrend.new == 1 and self.data.close.new > self.data.high.prev:
        >>>         return "内包线后向上突破，买入信号"
        >>>     elif thrend.new == -1 and self.data.close.new < self.data.low.prev:
        >>>         return "内包线后向下跌破，卖出信号"
        >>> 
        >>> # 结合成交量确认
        >>> def insidebar_volume_confirmation(self):
        >>>     thrend, line = self.data.insidebar()
        >>>     
        >>>     # 内包线成交量萎缩，突破时放量
        >>>     if (thrend.new != 0 and 
        >>>         self.data.volume.new < self.data.volume.ema(period=20).new and
        >>>         self.data.volume.prev > self.data.volume.ema(period=20).prev):
        >>>         return "内包线缩量，突破放量，信号可靠"
        """
        ...
    
    # 2026-5-17 更新

    @tobtind(lines=None, category="candles", lib='pta')
    def cdl_doji(self, length: int = 10, factor: int | float = 100, scalar: int | float = 100, asint: bool = True, offset: int = 0, **kwargs) -> IndSeries:
        """
        十字星形态 (Doji)
        ---------
            尝试识别"十字星"蜡烛，其长度小于前10根K线高低范围平均值的10%。

        所需数据字段:
        ---------
            open, high, low, close

        数据来源:
        ---------
            TA Lib: https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_CDLDOJI.c

        参数:
        ---------
        - length (int): 周期. 默认: 10
        - factor (float): 十字星值因子. 默认: 100
        - scalar (float): 标量. 默认: 100
        - asint (bool): 返回整数. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - naive (bool): 预填充可能的十字星，实体小于高低范围的factor百分比. 默认: False
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        使用案例:
        ---------
        >>> # 识别十字星形态
        >>> doji = self.data.cdl_doji()
        >>> if doji.new != 0:
        >>>     return "检测到十字星，可能出现反转信号"
        """
        ...

    @tobtind(lines=None, category="candles", lib='pta')
    def cdl_inside(self, asbool: bool = False, scalar: int | float = 100, offset: int = 0, **kwargs) -> IndSeries:
        """
        内含线形态 (Inside Bar)
        ---------
            尝试识别"内含"蜡烛，即比前一根蜡烛更小的K线。

        所需数据字段:
        ---------
            open, high, low, close

        数据来源:
        ---------
            TA Lib: https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_CDL3INSIDE.c
            TradingView: https://www.tradingview.com/script/IyIGN1WO-Inside-Bar/

        参数:
        ---------
        - asbool (bool): 返回布尔值. 默认: False
        - scalar (float): 标量. 默认: 100
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        使用案例:
        ---------
        >>> # 识别内含线形态
        >>> inside = self.data.cdl_inside()
        >>> if inside.new != 0:
        >>>     return "检测到内含线，市场可能盘整"
        """
        ...
    
    @tobtind(lines=None, overlap=False, category="candles", lib='pta')
    def cdl(self, name: str | list[str] | PRFNameTtype = "2crows", scalar: int | float = 100, offset: int = 0, **kwargs) -> IndFrame:
        """
        蜡烛图形态识别 (Candle Pattern)
        ---------
            TA Lib蜡烛图形态的包装函数，支持所有标准K线形态识别。

        所需数据字段:
        ---------
            open, high, low, close

        数据来源:
        ---------
            TA Lib: https://ta-lib.org

        参数:
        ---------
        - name (str | list[str]): 形态名称或名称列表. 默认: "all"
        - scalar (float): 标量. 默认: 100
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 形态列

        警告:
        ---------
            需要安装 TA Lib

        使用案例:
        ---------
        >>> # 识别所有蜡烛图形态
        >>> patterns = self.data.cdl()
        >>> for col in patterns.columns:
        >>>     if patterns[col].new != 0:
        >>>         print(f"检测到形态: {col}")
        """
        ...


    @tobtind(lines=None, category="cycles")
    def reflex(self, length: int = 20, smooth: int = 20, alpha: int | float = 0.04, pi: int | float = 3.14159, sqrt2: int | float = 1.414, offset: int = 0, **kwargs) -> IndSeries:
        """
        迟滞降低周期指标 (Reflex)
        ---------
            John F. Ehlers开发的周期指标，旨在减少延迟。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            rengel8: https://github.com/rengel8
            traders.com: http://traders.com/Documentation/FEEDbk_docs/2020/02/TradersTips.html
            prorealcode: https://www.prorealcode.com/prorealtime-indicators/reflex-and-trendflex-indicators-john-f-ehlers/

        参数:
        ---------
        - length (int): 周期. 默认: 20
        - smooth (int): 超平滑周期. 默认: 20
        - alpha (float): 差值和的Alpha权重. 默认: 0.04
        - pi (float): Ehlers截断值. 默认: 3.14159
        - sqrt2 (float): Ehlers截断值. 默认: 1.414
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        注意:
        ---------
            John F. Ehlers在2020年2月TASC杂志的文章"Reflex: A New Zero-Lag Indicator"中介绍了两个指标。
            Reflex是一个减少延迟的周期指标。Reflex/Trendflex都是振荡器，相互补充，
            分别侧重于周期和趋势。
        """
        ...


    @tobtind(lines=None, category="momentum", lib='pta')
    def crsi(self, rsi_length: int = 3, streak_length: int = 2, rank_length: int = 100, scalar: int | float = 100, talib: bool = True, drift: int = 1, offset: int = 0, **kwargs) -> IndSeries:
        """
        康纳斯相对强弱指数 (Connors RSI)
        ---------
            该指标尝试识别"超买"或"超卖"条件下的动量和潜在反转。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            alvarezquanttrading: https://alvarezquanttrading.com/blog/connorsrsi-analysis/
            tradingview: https://www.tradingview.com/support/solutions/43000502017-connors-rsi-crsi/
            Connors, L., Alvarez, C., & Radtke, M. (2012). "An Introduction to ConnorsRSI"

        参数:
        ---------
        - rsi_length (int): RSI周期. 默认: 3
        - streak_length (int): 连胜 RSI 周期. 默认: 2
        - rank_length (int): 百分比排名周期. 默认: 100
        - scalar (float): 标量. 默认: 100
        - talib (bool): 使用TA Lib. 默认: True
        - drift (int): 差分值. 默认: 1
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列
        """
        ...

    @tobtind(lines=['exhc_Long', 'exhc_Short'], category="momentum", lib='pta')
    def exhc(self, length: int = 4, cap: int = 13, asint: bool = False, show_all: bool = False, nozeros: bool = False, offset: int = 0, **kwargs) -> IndFrame:
        """
        衰竭计数 (Exhaustion Count)
        ---------
            该指标尝试识别上涨/下跌衰竭。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            demark: https://demark.com
            practicaltechnicalanalysis: http://practicaltechnicalanalysis.blogspot.com/2013/01/tom-demark-sequential.html

        参数:
        ---------
        - length (int): 周期. 默认: 4
        - cap (int): 计数上限，设为0则无上限. 默认: 13
        - show_all (bool): 显示1-13计数，设为False则显示6-9. 默认: True
        - asint (bool): 返回整数. 默认: False
        - nozeros (bool): 用NaN替换零. 默认: False
        - offset (int): 偏移. 默认: 0

        返回:
        ---------
        >>> IndFrame: 2列 ['exhc_Long', 'exhc_Short']

        注意:
        ---------
            类似于TD Sequential指标
        """
        ...

    @tobtind(lines=["smchv","smcbf","smcbi","smcbp","smctf","smcti","smctp"], category="momentum", lib='pta')
    def smc(self, abr_length: int = 14, close_length: int = 50, vol_length: int = 20, percent: int = 5, vol_ratio: int | float = 1.5, asint: bool = True, mamode: str = "sma", talib: bool = True, offset: int = 0, **kwargs) -> IndFrame:
        """
        聪明钱概念 (Smart Money Concept)
        ---------
            该指标结合多种技术来识别可能表明"聪明钱"行为的重大波动。
            使用蜡烛图形态、移动平均线和失衡计算。

        所需数据字段:
        ---------
            open, high, low, close, volume

        数据来源:
        ---------
            TradingView: https://www.tradingview.com/script/CnB3fSph-Smart-Money-Concepts-LuxAlgo/

        参数:
        ---------
        - abr_length (int): ABR周期. 默认: 14
        - close_length (int): 收盘价MA周期. 默认: 50
        - vol_length (int): 波动率周期. 默认: 20
        - percent (int): 影线超过实体的百分比. 默认: 5
        - vol_ratio (float): 波动率比率上限. 默认: 1.5
        - asint (bool): 返回整数. 默认: True
        - mamode (str): 移动平均线模式. 默认: "sma"
        - talib (bool): 使用TA Lib. 默认: True
        - offset (int): 偏移. 默认: 0

        返回:
        ---------
        >>> IndFrame: 7列 ['smchv','smcbf','smcbi','smcbp','smctf','smcti','smctp']
        """
        ...
    
    @tobtind(lines=['stochf_k', 'stochf_d'], category="momentum", lib='pta')
    def stochf(self, k: int = 14, d: int = 3, mamode: str = "sma", talib: bool = True, offset: int = 0, **kwargs) -> IndFrame:
        """
        快速随机指标 (Fast Stochastic)
        ---------
            George Lane在1950年代开发的指标，与STOCH类似但波动性更大。

        所需数据字段:
        ---------
            high, low, close

        数据来源:
        ---------
            corporatefinanceinstitute: https://corporatefinanceinstitute.com/resources/knowledge/trading-investing/fast-stochastic-indicator/
            sierrachart: https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=333&Name=KD_-_Fast

        参数:
        ---------
        - k (int): 快速%K周期. 默认: 14
        - d (int): 慢速%D周期. 默认: 3
        - mamode (str): 移动平均线模式. 默认: "sma"
        - talib (bool): 使用TA Lib. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 2列 ['stochf_k', 'stochf_d']
        """
        ...

    @tobtind(lines=['tmox', 'tmos', 'tmom','tmoms'], category="momentum", lib='pta')
    def tmo(self, tmo_length: int = 14, calc_length: int = 5, smooth_length: int = 3, momentum: bool = False, normalize: bool = False, exclusive: bool = False, mamode: str = "ema", offset: int = 0, **kwargs) -> IndFrame:
        """
        真实动量振荡器 (True Momentum Oscillator)
        ---------
            该指标尝试量化动量。

        所需数据字段:
        ---------
            open, close

        数据来源:
        ---------
            TradingView: https://www.tradingview.com/script/VRwDppqd-True-Momentum-Oscillator/

        参数:
        ---------
        - tmo_length (int): TMO周期. 默认: 14
        - calc_length (int): 初始MA周期. 默认: 5
        - smooth_length (int): 主信号和平滑信号MA周期. 默认: 3
        - mamode (str): 移动平均线模式. 默认: "ema"
        - momentum (bool): 计算主和平滑动量. 默认: False
        - normalize (bool): 归一化. 默认: False
        - exclusive (bool): 独占周期. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 4列 ['tmox', 'tmos', 'tmom','tmoms']
        """
        ...

    @tobtind(lines=['alligator_jaw', 'alligator_teeth', 'alligator_lips'], overlap=True, category="overlap", lib='pta')
    def alligator(self, jaw: int = 13, teeth: int = 8, lips: int = 5, talib: bool = True, offset: int = 0, **kwargs) -> IndFrame:
        """
        比尔威廉姆斯鳄鱼指标 (Bill Williams Alligator)
        ---------
            Bill Williams开发的趋势识别指标。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            sierrachart: https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=175&Name=Bill_Williams_Alligator
            TradingView: https://www.tradingview.com/scripts/alligator/

        参数:
        ---------
        - jaw (int): 下颌周期. 默认: 13
        - teeth (int): 牙齿周期. 默认: 8
        - lips (int): 嘴唇周期. 默认: 5
        - talib (bool): 使用TA Lib. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 3列 ['alligator_jaw', 'alligator_teeth', 'alligator_lips']

        注意:
        ---------
            Williams认为外汇市场只有15%到30%的时间处于趋势中，
            其余时间为盘整。受分形几何启发，输出类似于鳄鱼张嘴和闭嘴。
            它由三条线组成：Jaw(下颌)、Teeth(牙齿)和Lips(嘴唇)，周期各不相同。
        """
        ...
    
    @tobtind(lines=["mama_slow", "mama_fast"], overlap=True, category="overlap", lib='pta')
    def mama(self, fastlimit: int | float = 0.5, slowlimit: int | float = 0.05, prenan: int = 3, talib: bool = True, offset: int = 0, **kwargs) -> IndFrame:
        """
        MESA自适应移动平均线 (MESA Adaptive Moving Average)
        ---------
            又称"移动平均线之王"，由John Ehlers开发，使用Hilbert Transform Discriminator来适应波动性。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            traders.com: http://traders.com/documentation/feedbk_docs/2014/01/traderstips.html
            TradingView: https://www.tradingview.com/script/foQxLbU3-Ehlers-MESA-Adaptive-Moving-Average-LazyBear/

        参数:
        ---------
        - fastlimit (float): 快速限制. 默认: 0.5
        - slowlimit (float): 慢速限制. 默认: 0.05
        - prenan (int): 预填充数量. TV-LB为3, Ehler's为6, TA Lib为32. 默认: 3
        - talib (bool): 使用TA Lib. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 2列 ['mama_slow','mama_fast']

        提示:
        ---------
            同时输出FAMA(快速自适应移动平均线)
        """
        ...
    
    @tobtind(lines=["pivots_trad_d_p", "pivots_trad_d_s1", "pivots_trad_d_s2", "pivots_trad_d_s3", 
            "pivots_trad_d_s4", "pivots_trad_d_r1", "pivots_trad_d_r2", "pivots_trad_d_r3", "pivots_trad_d_r4"], overlap=True, category="overlap", lib='pta')
    def pivots(self, method: str = 'traditional', anchor: str = 'D', **kwargs) -> IndFrame:
        """
        枢轴点 (Pivot Points)
        ---------
            枢轴点尝试识别支撑和阻力位。有多种计算方法，最常用的是Traditional方法。
            其他方法包括：Camarilla、Classic、Demark、Fibonacci和Woodie。

        所需数据字段:
        ---------
            open, high, low, close

        数据来源:
        ---------
            sierrachart: https://www.sierrachart.com/index.php?page=doc/PivotPoints.html

        参数:
        ---------
        - method (str): 枢轴点方法. 默认: 'traditional'
        - anchor (str): 锚定周期. 默认: 'D'

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 3、7或9列 [pivots_trad_d_p, pivots_trad_d_s1, pivots_trad_d_s2, pivots_trad_d_s3, 
            pivots_trad_d_s4, pivots_trad_d_r1, pivots_trad_d_r2, pivots_trad_d_r3, pivots_trad_d_r4]

        注意:
        ---------
            支持Pandas偏移别名
        """
        ...

    @tobtind(lines=None, overlap=True, category="overlap", lib='pta')
    def smma(self, length: int = 10, mamode: str = "sma", talib: bool = True, offset: int = 0, **kwargs) -> IndSeries:
        """
        平滑移动平均线 (Smoothed Moving Average)
        ---------
            该指标尝试确认趋势并识别支撑和阻力区域。与减少延迟不同，它侧重于减少噪音。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            sierrachart: https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=173&Name=Moving_Average_-_Smoothed
            TradingView: https://www.tradingview.com/scripts/smma/

        参数:
        ---------
        - length (int): 周期. 默认: 10
        - mamode (str): 移动平均线模式. 默认: "sma"
        - talib (bool): 使用TA Lib. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        注意:
        ---------
            是Bill Williams Alligator指标的核心组成部分
        """
        ...

    @tobtind(lines=None, overlap=True, category="overlap", lib='pta')
    def ssf3(self, length: int = 20, pi: int | float = 3.14159, sqrt3: int | float = 1.732, offset: int = 0, **kwargs) -> IndSeries:
        """
        Ehlers三极点超平滑滤波器 (Ehlers's 3 Pole Super Smoother Filter)
        ---------
            John F. Ehlers于2013年开发的(递归)数字滤波器，尝试减少延迟和消除混叠。此版本有两个极点。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            mql5: https://www.mql5.com/en/code/589
            TradingView: https://www.tradingview.com/script/VdJy0yBJ-Ehlers-Super-Smoother-Filter/

        参数:
        ---------
        - length (int): 周期. 默认: 20
        - pi (float): PI值，Ehlers截断值. 默认: 3.14159
        - sqrt3 (float): sqrt(3)值，Ehlers截断值. 默认: 1.732
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        注意:
        ---------
            TradingView上Everget的计算使用: pi=np.pi, sqrt2=np.sqrt(2)
        """
        ...

    @tobtind(lines=["dd","dd_pct","dd_log"], category="performance", lib='pta')
    def drawdown(self, offset: int = 0, **kwargs) -> IndFrame:
        """
        回撤 (Drawdown)
        ---------
            该指标追踪特定时期内从峰值到谷值的下降。通常用峰值与随后谷值之间的百分比表示。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            investopedia: https://www.investopedia.com/terms/d/drawdown.asp

        参数:
        ---------
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 3列
        """
        ...
    
    @tobtind(lines=["alphat","alphatl"], overlap=True, category="trend", lib='pta')
    def alphatrend(self, src: str = "close", length: int = 14, multiplier: int | float = 1, threshold: int | float = 50, lag: int = 2, mamode: str = "sma", talib: bool = True, offset: int = 0, **kwargs) -> IndFrame:
        """
        Alpha趋势指标 (Alpha Trend)
        ---------
            该指标尝试过滤横向波动以获得准确的信号。

        所需数据字段:
        ---------
            open, high, low, close, volume

        数据来源:
        ---------
            OnlyFibonacci: https://github.com/OnlyFibonacci/AlgoSeyri/blob/main/alphaTrendIndicator.py
            TradingView: https://www.tradingview.com/script/o50NYLAZ-AlphaTrend/

        参数:
        ---------
        - src (str): 数据源，可选"open"、"high"、"low"或"close". 默认: "close"
        - length (int): ATR、MFI或RSI周期. 默认: 14
        - multiplier (float): 追踪ATR倍数. 默认: 1
        - threshold (float): 动量阈值. 默认: 50
        - lag (int): 主趋势滞后周期. 默认: 2
        - mamode (str): 移动平均线模式. 默认: "sma"
        - talib (bool): 使用TA Lib. 默认: True
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 2列 ["alphat","alphatl"]
        """
        ...

    @tobtind(lines=None, category="trend", lib='pta')
    def ht_trendline(self, talib: bool = True, prenan: int = 63, offset: int = 0, **kwargs) -> IndSeries:
        """
        Hilbert变换趋势线 (Hilbert Transform TrendLine)
        ---------
            该指标使用Hilbert Transform来平滑值。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            John F Ehlers's "Rocket Science for Traders"
            mql5: https://c.mql5.com/forextsd/forum/59/023inst.pdf
            TA-Lib: https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_HT_TRENDLINE.c

        参数:
        ---------
        - talib (bool): 使用TA Lib. 默认: True
        - prenan (int): 预填充数量. Ehlers's为6或12, TALib为63. 默认: 63
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        警告:
        ---------
            TA-Lib相关系数: 0.9979308363057683
        """
        ...

    @tobtind(lines=['rwi_h', 'rwi_l'], category="trend", lib='pta')
    def rwi(self, length: int = 14, mamode: str = "rma", talib: bool = True, drift: int = 1, offset: int = 0, **kwargs) -> IndFrame:
        """
        随机游走指数 (Random Walk Index)
        ---------
            该指标尝试识别趋势和随机游走之间的差异。

        所需数据字段:
        ---------
            high, low, close

        数据来源:
        ---------
            technicalindicators: https://www.technicalindicators.net/indicators-technical-analysis/168-rwi-random-walk-index

        参数:
        ---------
        - length (int): 周期. 默认: 14
        - mamode (str): 移动平均线模式. 默认: "rma"
        - talib (bool): 使用TA Lib. 默认: True
        - drift (int): 差分值. 默认: 1
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 2列 ['rwi_h', 'rwi_l']
        """
        ...
    
    @tobtind(lines=None, category="trend", lib='pta')
    def trendflex(self, length: int = 20, smooth: int = 20, alpha: int | float = 0.04, pi: int | float = 3.14159, sqrt2: int | float = 1.414, offset: int = 0, **kwargs) -> IndSeries:
        """
        趋势弹性指标 (Trendflex)
        ---------
            John F. Ehlers开发的趋势指标，与"reflex"指标互补。

        所需数据字段:
        ---------
            close

        数据来源:
        ---------
            rengel8: https://github.com/rengel8
            prorealcode: https://www.prorealcode.com/prorealtime-indicators/reflex-and-trendflex-indicators-john-f-ehlers/
            traders: http://traders.com/Documentation/FEEDbk_docs/2020/02/TradersTips.html

        参数:
        ---------
        - length (int): 周期. 默认: 20
        - smooth (int): 超平滑周期. 默认: 20
        - alpha (float): Alpha权重. 默认: 0.04
        - pi (float): Ehlers截断值. 默认: 3.14159
        - sqrt2 (float): Ehlers截断值. 默认: 1.414
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        注意:
        ---------
            John F. Ehlers在2020年2月TASC杂志的文章"Reflex: A New Zero-Lag Indicator"中介绍了两个指标。
            Trendflex是一个与Reflex互补的振荡器，分别侧重于趋势和周期。
        """
        ...


    @tobtind(lines=["zigzags", "zigzagv","zigzagd"], category="trend", lib='pta')
    def zigzag(self, legs: int = 10, deviation: int | float = 5, backtest: bool = False, offset: int = 0, **kwargs) -> IndFrame:
        """
        锯齿形指标 (Zigzag)
        ---------
            该指标尝试过滤较小的波动，同时识别趋势方向。它不预测未来趋势，但能识别摆动高点和低点。

        所需数据字段:
        ---------
            high, low, close

        数据来源:
        ---------
            stockcharts: https://school.stockcharts.com/doku.php?id=technical_indicators:zigzag
            TradingView: https://www.tradingview.com/support/solutions/43000591664-zig-zag/

        参数:
        ---------
        - legs (int): 腿数 (> 2). 默认: 10
        - deviation (float): 反转偏差百分比. 默认: 5
        - backtest (bool): 回测模式. 默认: False
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 3列 ["zigzags", "zigzagv","zigzagd"]

        注意:
        ---------
            偏差: 当deviation=10时，显示大于10%的波动
            回测模式: 确保DataFrame适合回测

        警告:
        ---------
            序列反转将创建新行
        """
        ...


    @tobtind(lines=["xsa","xsb"], category="utils", lib='pta')
    def signals(self, xa: int | float = None, xb: int | float = None, cross_values: bool = None, xseries=None, xseries_a=None, xseries_b=None, cross_series: bool = None, offset: int = 0, **kwargs) -> IndFrame:
        """
        信号检测器 (Signals)
        ---------
            多功能信号检测器，判断指标是否上穿/下穿指定值或序列。

        所需数据字段:
        ---------
            无需特定字段

        参数:
        ---------
        - xa (IntFloat): 上穿值
        - xb (IntFloat): 下穿值
        - cross_values (bool): 检查是否穿越数值
        - xseries: 交叉序列
        - xseries_a: 上穿序列
        - xseries_b: 下穿序列
        - cross_series (bool): 检查是否穿越xseries
        - offset (int): 偏移. 默认: 0

        返回:
        ---------
        >>> IndFrame: 2列

        使用案例:
        ---------
            参考 er, macd, rsi, rsx 的使用示例
        """
        ...

    @tobtind(lines=None,overlap=True, category="volatility", lib='pta')
    def atrts(self, length: int = 14, ma_length: int = 20, k: int | float = 3, mamode: str = "ema", talib: bool = True, drift: int = 1, offset: int = 0, **kwargs) -> IndSeries:
        """
        ATR追踪止损 (ATR Trailing Stop)
        ---------
            该指标尝试识别多头和空头的退出点。使用可缩放的ATR结合移动平均线来确定趋势。

        所需数据字段:
        ---------
            high, low, close

        数据来源:
        ---------
            motivewave: https://www.motivewave.com/studies/atr_trailing_stops.htm

        参数:
        ---------
        - length (int): 周期. 默认: 14
        - ma_length (int): MA长度. 默认: 20
        - k (int): ATR倍数. 默认: 3
        - mamode (str): 移动平均线模式. 默认: "ema"
        - talib (bool): 使用TA Lib. 默认: True
        - drift (int): 差分值. 默认: 1
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - percent (bool): 返回百分比. 默认: False
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列
        """
        ...


    @tobtind(lines=["chandelier_long", "chandelier_short", "chandelier_direction"],
             overlap=dict(chandelier_long=True, chandelier_short=True, chandelier_direction=False),category="volatility", lib='pta')
    def chandelier_exit(self, high_length: int = 22, low_length: int = 22, atr_length: int = 14, multiplier: int | float = 2., mamode: str = "rma", talib: bool = True, use_close: bool = False, drift: int = 1, offset: int = 0, **kwargs) -> IndFrame:
        """
        吊灯止损 (Chandelier Exit)
        ---------
            该指标尝试识别基于ATR的追踪止损。

        所需数据字段:
        ---------
            high, low, close

        数据来源:
        ---------
            stockcharts: https://school.stockcharts.com/doku.php?id=technical_indicators:chandelier_exit
            TradingView: https://in.tradingview.com/scripts/chandelier/

        参数:
        ---------
        - high_length (int): 最高价周期. 默认: 22
        - low_length (int): 最低价周期. 默认: 22
        - atr_length (int): ATR长度. 默认: 14
        - multiplier (float): 上下轨标量. 默认: 2.0
        - mamode (str): 移动平均线模式. 默认: "rma"
        - talib (bool): 使用TA Lib. 默认: True
        - use_close (bool): 使用max(high_length, low_length)作为close. 默认: False
        - drift (int): 差分值. 默认: 1
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 3列 ["chandelier_long", "chandelier_short", "chandelier_direction"]
        """
        ...


    @tobtind(lines=["tsv","tsvs","tsvr"], category="volume", lib="pta")
    def tsv(self, length: int = 18, signal: int = 10, mamode: str = "sma", drift: int = 1, offset: int = 0, **kwargs) -> IndFrame:
        """
        时间段成交量 (Time Segmented Volume)
        ---------
            Worden Brothers Inc.开发的指标，尝试量化不同时间段的价格和成交量中的资金流动；
            类似于OBV指标。

        所需数据字段:
        ---------
            close, volume

        数据来源:
        ---------
            tc2000: https://help.tc2000.com/m/69404/l/747088-time-segmented-volume
            TradingView: https://www.tradingview.com/script/6GR4ht9X-Time-Segmented-Volume/
            usethinkscript: https://usethinkscript.com/threads/time-segmented-volume-for-thinkorswim.519/

        参数:
        ---------
        - length (int): 周期. 默认: 18
        - signal (int): 信号周期. 默认: 10
        - mamode (str): 移动平均线模式. 默认: "sma"
        - drift (int): 差分值. 默认: 1
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndFrame: 3列 ["tsv", "tsvs", "tsvr"]

        注意:
        ---------
            零线称为基准线
            当穿越基准线时产生入场和出场信号
        """
        ...

    @tobtind(lines=None, category="volume", lib='pta')
    def vhm(self, length: int = 610, std_length: int = 610, mamode: str = "sma", offset: int = 0, **kwargs) -> IndSeries:
        """
        成交量热力图 (Volume Heatmap)
        ---------
            该指标尝试量化指定长度周期的成交量趋势强度。

        所需数据字段:
        ---------
            volume

        数据来源:
        ---------
            TradingView: https://www.tradingview.com/script/unWex8N4-Heatmap-Volume-xdecow/

        参数:
        ---------
        - length (int): 周期. 默认: 610
        - std_length (int): 标准差周期. 默认: 610
        - mamode (str): 均值MA. 默认: "sma"
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries: 1列

        信号说明:
        ---------
            - 极冷: vhm <= -0.5
            - 冷: -0.5 < vhm <= 1.0
            - 中: 1.0 < vhm <= 2.5
            - 热: 2.5 < vhm <= 4.0
            - 极热: vhm >= 4
        """
        ...

    @tobtind(lines=None, category="volume", lib='pta')
    def vwap(self, anchor: str = "D", bands: list = [], offset: int = 0, **kwargs) -> IndSeries:
        """
        成交量加权平均价 (Volume Weighted Average Price)
        ---------
            该指标计算成交量加权平均价格。

        所需数据字段:
        ---------
            high, low, close, volume

        数据来源:
        ---------
            TradingView: https://www.tradingview.com/wiki/Volume_Weighted_Average_Prict_(VWAP)
            Trading Technologies: https://www.tradingtechnologies.com/help/x-study/technical-indicator-definitions/volume-weighted-average-price-vwap/
            Stockcharts: https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:vwap_intraday
            Sierra Chart: https://www.sierrachart.com/index.php?page=doc/StudiesReference.php&ID=108

        参数:
        ---------
        - anchor (str): VWAP锚点. 默认: "D"
        - bands (list): 正偏差列表. 默认: []
        - offset (int): 偏移. 默认: 0

        其他参数:
        ---------
        - fillna (value): 填充NaN值

        返回:
        ---------
        >>> IndSeries | IndFrame: 当设置bands时返回DataFrame，否则返回Series

        注意:
        ---------
            常用于日内图表识别总体方向
            根据索引值实现各种Pandas时间序列偏移别名

        提示:
        ---------
            负偏差会自动计算
        """
        ...

    NON_KLINE_INDICATORS = ["ZeroDivision","short_run","tsignals","xsignals","above","below",
                            "cross","cross_up","cross_down","line_trhend","pivots","zigzag",
                            "signals","vwap"]