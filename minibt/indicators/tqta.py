from __future__ import annotations
from .base import tobtind
from ..utils import TYPE_CHECKING, LineStyle, LineDash
if TYPE_CHECKING:
    from typing_ import *
    from .core import *

class TqTa:
    """## 天勤技术指标计算类
    - 基于天勤量化(TqSdk)的技术指标库封装，提供专业的技术分析指标计算。
    - 支持移动平均、振荡指标、趋势指标、量价指标等各类技术分析工具。

    ### 📘 **文档参考**:
    - API参考：https://www.minibt.cn/minibt_api_reference/tqfunc/
    - 天勤文档：https://tqsdk-python.readthedocs.io/en/latest/reference/tqsdk.ta.html

    ### 核心功能分类：
    - 趋势指标：MA, EMA, MACD, 布林带等
    - 振荡指标：RSI, KDJ, WR, CCI, BIAS等  
    - 量价指标：OBV, VWAP, 成交量比率等
    - 统计指标：标准差、相关系数、回归分析等
    - 形态识别：高低点、支撑阻力等

    ### 使用示例：
    >>> data = IndFrame  # 包含OHLCV数据的minibt数据对象
    >>> tqta = TqTa(data)   # data数据必须包含指标计算时用到的字段
    >>> self.kline.close.tqta
    >>> 
    >>> # 趋势指标
    >>> ma_20 = close.tqta.ma(20)     # 指定close列计算20周期简单移动平均
    >>> ema_12 = close.tqta.ema(12)   # 12周期指数移动平均
    >>> macd_diff, macd_dea, macd_hist = tqta.macd()  # MACD指标
    >>> 
    >>> # 振荡指标
    >>> rsi_14 = tqta.rsi(14)         # 14周期RSI
    >>> k, d, j = tqta.kdj()          # KDJ随机指标
    >>> 
    >>> # 量价指标
    >>> obv_line = tqta.obv()         # 能量潮指标

    ### 技术特点：
    - 专业准确：基于天勤官方指标算法，确保计算准确性
    - 性能优化：针对金融时间序列数据进行算法优化
    - 完整兼容：与minibt数据框架无缝集成
    - 边界处理：自动处理数据边界和缺失值
    - 多周期支持：支持不同时间周期的指标计算
    """
    _perfixes: str = "tqta_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    # tqta:天勤指标
    @tobtind(lib="tqta")
    def ATR(self, n=14, **kwargs) -> IndFrame:
        """
        ## 平均真实波幅指标 - 衡量价格波动性的重要技术指标

        功能：
            计算资产在一定周期内的价格波动范围，反映市场波动剧烈程度

        应用场景：
            - 波动率测量和风险评估
            - 止损止盈位设置参考
            - 突破交易信号确认
            - 仓位管理和风险控制

        计算原理：
        >>> 真实波幅(TR)取以下三者最大值：
            1. 当日最高价 - 当日最低价
            2. |当日最高价 - 前日收盘价|
            3. |当日最低价 - 前日收盘价|
            平均真实波幅(ATR) = TR的N周期简单移动平均

        参数：
            n: 计算周期，默认14，常用周期为14
            **kwargs: 额外参数

        注意：
            - ATR值本身没有上下限，数值大小与价格和波动性相关
            - 适用于不同时间周期的分析
            - 可作为动态止损止盈的参考依据

        返回值：
            IndFrame: 包含"tr"(真实波幅)和"atr"(平均真实波幅)两列

        所需数据字段：
            `high`,`low`,`close`

        >>> 示例：
            # 使用ATR设置动态止损
            atr_data = close.tqta.ATR(n=14)
            stop_loss = close - 2 * atr_data.atr
            take_profit = close + 3 * atr_data.atr
            # 波动率过滤
            high_volatility = atr_data.atr > atr_data.atr.tqta.ma(length=20)
        """
        # hlc,tr,atr
        ...

    @tobtind(lib="tqta")
    def BIAS(self, n=6, **kwargs) -> IndSeries:
        """
        ## 乖离率指标 - 衡量价格与移动平均线偏离程度的动量指标

        功能：
            计算收盘价与移动平均线之间的百分比偏离，识别超买超卖状态

        应用场景：
            - 趋势反转预警
            - 超买超卖区域识别
            - 均值回归策略构建
            - 价格极端状态判断

        计算原理：
        >>> BIAS = (收盘价 - N周期移动平均价) / N周期移动平均价 × 100%
            正值表示价格在均线上方，负值表示在均线下方

        参数：
            n: 移动平均周期，默认6，常用周期有6、12、24
            **kwargs: 额外参数

        注意：
            - 不同市场、不同品种的乖离率阈值需要调整
            - 在强势趋势中可能出现持续超买/超卖
            - 建议结合其他指标共同使用

        返回值：
            IndSeries: 乖离率值序列，单位为百分比

        所需数据字段：
            `close`

        >>> 示例：
            # 乖离率超买超卖判断
            bias_6 = close.tqta.BIAS(n=6)
            over_bought = bias_6 > 5    # 6日乖离率大于5%视为超买
            over_sold = bias_6 < -5     # 6日乖离率小于-5%视为超卖

            # 多周期乖离率组合
            bias_short = close.tqta.BIAS(n=6)
            bias_long = close.tqta.BIAS(n=24)
            bias_divergence = bias_short - bias_long
        """
        # c
        ...

    @tobtind(lines=["mid", "top", "bottom"], overlap=True, lib="tqta")
    def BOLL(self, n=26, p=2, **kwargs) -> IndFrame:
        """
        ## 布林带指标 - 基于标准差的价格通道分析工具

        功能：
            构建动态价格通道，识别价格相对位置和波动性变化

        应用场景：
            - 支撑阻力位识别
            - 波动率突破信号
            - 趋势强度和持续性判断
            - 价格极端状态识别

        计算原理：
        >>> 中轨 = N周期简单移动平均
            标准差 = N周期收盘价标准差
            上轨 = 中轨 + P × 标准差
            下轨 = 中轨 - P × 标准差

        参数：
            n: 计算周期，默认26
            p: 标准差倍数，默认2，决定通道宽度
            **kwargs: 额外参数

        注意：
            - 价格触及布林带上轨不一定卖出，触及下轨不一定买入
            - 布林带收窄往往预示重大价格变动
            - 结合价格与布林带相对位置判断趋势强度

        返回值：
            IndFrame: 包含"mid"(中轨)、"top"(上轨)、"bottom"(下轨)三列

        所需数据字段：
            `close`

        >>> 示例：
            # 布林带突破策略
            boll = close.tqta.BOLL(n=20, p=2)
            upper_break = close > boll.top      # 上轨突破
            lower_break = close < boll.bottom   # 下轨突破
            # 布林带收窄识别
            band_width = (boll.top - boll.bottom) / boll.mid
            narrow_band = band_width < band_width.tqta.ma(length=20)
        """
        # c,mid,top,bottom
        ...

    @tobtind(lines=["atr_","pdi", "mdi", "adx", "adxr"], lib="tqta")
    def DMI(self, n=14, m=6, **kwargs) -> IndFrame:
        """
        ## 动向指标系统 - 综合趋势强度和方向的分析工具

        功能：
            通过+DI、-DI、ADX等多维度分析趋势方向、强度和持续性

        应用场景：
            - 趋势方向确认
            - 趋势强度量化
            - 买卖信号生成
            - 趋势转换预警

        计算原理：
        >>> +DM = 当日最高价 - 前日最高价（正值）
            -DM = 前日最低价 - 当日最低价（正值）
            TR = 真实波幅
            +DI = (+DM的N周期平滑 / TR的N周期平滑) × 100
            -DI = (-DM的N周期平滑 / TR的N周期平滑) × 100
            DX = |(+DI - -DI)| / (+DI + -DI) × 100
            ADX = DX的M周期平滑移动平均

        参数：
            n: 主要计算周期，默认14
            m: ADX平滑周期，默认6
            **kwargs: 额外参数

        注意：
            - +DI上穿-DI为买入信号，下穿为卖出信号
            - ADX高于25表示趋势明显，低于20表示盘整
            - ADXR用于评估ADX的可靠性

        返回值：
            IndFrame: 包含"atr"、"pdi"、"mdi"、"adx"、"adxr"五列

        所需数据字段：
            `high`,`low``close`

        示例：
            # DMI趋势判断
            dmi = self.kline.tqta.DMI(n=14, m=6)
            strong_trend = dmi.adx > 25          # 强趋势
            weak_trend = dmi.adx < 20           # 弱趋势/盘整
            buy_signal = dmi.pdi.tqta.crossup(dmi.mdi)  # 买入信号
            sell_signal = dmi.pdi.tqta.crossdown(dmi.mdi) # 卖出信号
        """
        # hlc,atr,pdi,mdi,adx,adxr
        ...

    @tobtind(lines=["k", "d", "j"], lib="tqta")
    def KDJ(self, n=9, m1=3, m2=3, **kwargs) -> IndFrame:
        """
        ## 随机指标 - 动量振荡器，识别超买超卖和背离信号

        功能：
            通过价格在给定周期内相对位置分析市场动量变化

        应用场景：
            - 超买超卖区域识别
            - 背离分析
            - 短线买卖时机把握
            - 趋势转换预警

        计算原理：
        >>> RSV = (收盘价 - N日内最低价) / (N日内最高价 - N日内最低价) × 100
            K = RSV的M1周期简单移动平均
            D = K的M2周期简单移动平均
            J = 3 × K - 2 × D

        参数：
        >>> n: RSV计算周期，默认9
            m1: K值平滑周期，默认3
            m2: D值平滑周期，默认3
            **kwargs: 额外参数

        注意：
            - K、D值在80以上为超买区，20以下为超卖区
            - J值反应更敏感，可提前预警
            - 金叉死叉结合位置判断更有效
            - 背离信号具有较高可靠性

        返回值：
            IndFrame: 包含"k"、"d"、"j"三列

        所需数据字段：
            `high`,`low``close`

        >>> # 示例：
            # KDJ超买超卖判断
            kdj = self.kline.tqta.KDJ(n=9, m1=3, m2=3)
            over_bought = (kdj.k > 80) & (kdj.d > 80)
            over_sold = (kdj.k < 20) & (kdj.d < 20)
            # KDJ金叉死叉
            golden_cross = kdj.k.tqta.crossup(kdj.d)
            death_cross = kdj.k.tqta.crossdown(kdj.d)
        """
        # hlc,k,d,j
        ...

    @tobtind(lines=["dif", "dea", "bar"], lib="tqta", linestyle=dict(diff=LineStyle(line_dash=LineDash.vbar)))
    def MACD(self, short=12, long=26, m=9, **kwargs) -> IndFrame:
        """
        ## 指数平滑异同移动平均线 - 经典的趋势动量指标

        功能：
            通过快慢均线离差分析趋势方向和动量变化

        应用场景：
            - 趋势方向判断
            - 买卖信号生成
            - 背离分析
            - 动量强度评估

        计算原理：
        >>> DIF = 12日EMA - 26日EMA
            DEA = DIF的9日EMA
            MACD柱 = (DIF - DEA) × 2

        参数：
            short: 快线周期，默认12
            long: 慢线周期，默认26
            m: 信号线周期，默认9
            **kwargs: 额外参数

        注意：
            - DIF上穿DEA为金叉买入信号
            - DIF下穿DEA为死叉卖出信号
            - 零轴上方为多头市场，下方为空头市场
            - 柱状线颜色变化反映动量增减

        返回值：
            IndFrame: 包含"dif"(DIF)、"dea"(DEA)、"bar"(MACD柱)三列

        所需数据字段：
            `close`

        >>> # 示例：
            # MACD基础信号
            macd = self.close.tqta.MACD(short=12, long=26, m=9)
            bull_market = macd.diff > 0                    # 多头市场
            golden_cross = macd.diff.tqta.crossup(macd.dea)  # 金叉
            death_cross = macd.diff.tqta.crossdown(macd.dea) # 死叉
            # MACD柱状线分析
            momentum_increasing = macd.bar > macd.bar.tqfunc.ref(length=1)
        """
        # c,diff,dea,bar
        ...

    @tobtind(lib="tqta")
    def SAR(self, n=4, step=0.02, max=0.2, **kwargs) -> IndSeries:
        """
        抛物线停损指标 - 趋势跟踪和停损点设置工具

        功能：
            提供动态的停损点和趋势转换信号，适用于趋势跟踪策略

        应用场景：
            - 趋势方向判断
            - 动态止损位设置
            - 趋势转换预警
            - 长线持仓管理

        计算原理：
        >>> 基于极值点和加速因子动态计算停损点
            上升趋势：SAR = 前日SAR + AF × (前日最高价 - 前日SAR)
            下降趋势：SAR = 前日SAR + AF × (前日最低价 - 前日SAR)
            AF从step开始，每创新高/新低增加step，直到达到max

        参数：
            n: 初始周期，默认4
            step: 步长/加速因子，默认0.02
            max: 最大加速因子，默认0.2
            **kwargs: 额外参数

        注意：
            - 价格在SAR之上为上升趋势，之下为下降趋势
            - SAR点翻转即为买卖信号
            - 在震荡市中可能产生频繁假信号
            - 适合趋势明显的市场环境

        返回值：
            IndSeries: SAR值序列

        所需数据字段：
            `open`,`high`,`low`,`close`

        >>> 示例：
            # SAR趋势判断
            sar = self.kline.tqta.SAR(n=4, step=0.02, max=0.2)
            uptrend = close > sar                    # 上升趋势
            downtrend = close < sar                  # 下降趋势
            # SAR翻转信号
            buy_signal = (close.tqfunc.ref(length=1) < sar.tqfunc.ref(length=1)) & (close > sar)
            sell_signal = (close.tqfunc.ref(length=1) > sar.tqfunc.ref(length=1)) & (close < sar)
        """
        # ohlc
        ...

    @tobtind(lib="tqta")
    def WR(self, n=14, **kwargs) -> IndSeries:
        """
        ## 威廉指标 - 超买超卖振荡器，测量价格相对位置

        功能：
            分析价格在给定周期内相对高低位置，识别极端状态

        应用场景：
            - 超买超卖区域识别
            - 短期反转点预测
            - 市场极端情绪判断
            - 结合其他指标确认信号

        计算原理：
        >>> WR = (N日内最高价 - 当日收盘价) / (N日内最高价 - N日内最低价) × (-100)
            数值在0到-100之间，0为超卖，-100为超买

        参数：
            n: 计算周期，默认14
            **kwargs: 额外参数

        注意：
            - 传统用法：低于-80超买，高于-20超卖
            - 可结合价格行为过滤假信号
            - 在强势趋势中可能出现指标钝化
            - 多周期WR组合使用效果更好

        返回值：
            IndSeries: WR值序列，范围为-100到0

        所需数据字段：
            `high`,`low`,`close`

        >>> #示例：
            # WR超买超卖判断
            wr = self.kline.tqta.WR(n=14)
            over_bought = wr < -80      # 超买区域
            over_sold = wr > -20        # 超卖区域
            # WR背离分析
            price_new_high = close == close.tqfunc.hhv(length=20)
            wr_new_low = wr == wr.tqfunc.llv(length=20)
            bearish_divergence = price_new_high & wr_new_low  # 顶背离
        """
        # hlc
        ...

    @tobtind(lib="tqta")
    def RSI(self, n=7, **kwargs) -> IndSeries:
        """
        ## 相对强弱指标 - 动量振荡器，衡量价格变动速度和幅度

        功能：
            通过比较一定时期内上涨和下跌幅度评估买卖力量对比

        应用场景：
            - 超买超卖状态识别
            - 背离分析
            - 趋势强度评估
            - 买卖时机选择

        计算原理：
        >>> RSI = 100 - 100 / (1 + RS)
            RS = N日内上涨幅度平均值 / N日内下跌幅度平均值

        参数：
            n: 计算周期，默认7，常用周期有6、12、24
            **kwargs: 额外参数

        注意：
            - 传统用法：70以上超买，30以下超卖
            - 可调整阈值适应不同市场特性
            - 背离信号具有较高预测价值
            - 在强势趋势中可能长时间停留在超买/超卖区

        返回值：
            IndSeries: RSI值序列，范围0-100

        所需数据字段：
            `close`

        >>> # 示例：
            # RSI超买超卖判断
            rsi = self.close.tqta.RSI(n=14)
            over_bought = rsi > 70      # 超买
            over_sold = rsi < 30        # 超卖
            # RSI背离检测
            price_lower_low = close < close.tqfunc.ref(length=1)
            rsi_higher_low = rsi > rsi.tqfunc.ref(length=1)
            bullish_divergence = price_lower_low & rsi_higher_low  # 底背离
        """
        # c
        ...

    @tobtind(lib="tqta")
    def ASI(self, **kwargs) -> IndSeries:
        """
        ## 振动升降指标 - 精确定价指标，减少跳空缺口影响

        功能：
            通过复杂计算消除跳空影响，更准确反映价格真实走势

        应用场景：
            - 趋势方向精确认定
            - 突破信号验证
            - 价格真实性判断
            - 结合其他指标提高准确性

        计算原理：
        >>> ASI累计计算每个交易日的振动值
            基于开盘、最高、最低、收盘价和前一交易日价格
            通过复杂公式计算当日SI值并累计得到ASI

        参数：
            **kwargs: 额外参数

        注意：
            - ASI领先或同步于价格走势
            - 突破前高前低时ASI信号更可靠
            - 与OBV类似但计算更复杂
            - 适合判断价格走势的真实性

        返回值：
            IndSeries: ASI值序列

        所需数据字段：
            `open`,`high`,`low`,`close`

        >>> # 示例：
            # ASI突破信号
            asi = self.kline.tqta.ASI()
            price_break_high = close > close.tqfunc.hhv(length=20)
            asi_break_high = asi > asi.tqfunc.hhv(length=20)
            valid_breakout = price_break_high & asi_break_high  # 有效突破
            # ASI趋势确认
            asi_uptrend = asi > asi.tqfunc.ma(length=20)
            asi_downtrend = asi < asi.tqfunc.ma(length=20)
        """
        # ohlc
        ...

    @tobtind(lib="tqta")
    def VR(self, n=26, **kwargs) -> IndSeries:
        """
        ## 容量比率指标 - 量价关系分析工具，衡量成交量与价格关系

        功能：
            通过成交量变化分析资金流向和市场情绪，识别主力资金动向

        应用场景：
            - 量价背离分析
            - 资金流向判断
            - 趋势确认和反转预警
            - 超买超卖区域识别

        计算原理：
        >>> VR = (N日内上涨日成交量总和 + N日内平盘日成交量总和/2) / 
                 (N日内下跌日成交量总和 + N日内平盘日成交量总和/2) × 100
            反映多空双方力量对比

        参数：
            n: 计算周期，默认26
            **kwargs: 额外参数

        注意：
            - VR在40-70为低价区，80-150为安全区，160-450为获利区，450以上为警戒区
            - 低VR值配合价格底部往往是买入时机
            - 高VR值配合价格顶部需警惕反转
            - 与价格走势背离时信号更可靠

        返回值：
            IndSeries: VR值序列

        所需数据字段：
            `close`, `volume`

        >>> 示例：
            # VR超买超卖判断
            vr = self.kline.tqta.VR(n=26)
            oversold_area = vr < 70          # 低价区
            safe_area = (vr >= 80) & (vr <= 150)  # 安全区
            profit_area = (vr >= 160) & (vr <= 450) # 获利区
            warning_area = vr > 450          # 警戒区
            # VR与价格背离
            price_new_high = close == close.tqfunc.hhv(length=20)
            vr_divergence = vr < vr.tqfunc.ref(length=1)
            top_divergence = price_new_high & vr_divergence
        """
        # cv
        ...

    @tobtind(lines=["ar", "br"], lib="tqta")
    def ARBR(self, n=26, **kwargs) -> IndFrame:
        """
        ## 人气意愿指标系统 - 综合反映市场多空力量对比

        功能：
            AR衡量市场人气，BR反映买卖意愿，共同分析市场情绪

        应用场景：
            - 市场情绪判断
            - 多空力量对比分析
            - 趋势转换预警
            - 超买超卖识别

        计算原理：
        >>> AR = (N日内(最高价 - 开盘价)之和 / N日内(开盘价 - 最低价)之和) × 100
            BR = (N日内(当日最高价 - 前日收盘价)之和 / N日内(前日收盘价 - 当日最低价)之和) × 100

        参数：
            n: 计算周期，默认26
            **kwargs: 额外参数

        注意：
            - AR在80-120为盘整区，150以上超买，70以下超卖
            - BR在70-150为盘整区，300以上超买，50以下超卖
            - AR、BR同时急升预示趋势强劲
            - BR急剧下降而AR平稳时可能见底

        返回值：
            IndFrame: 包含"ar"(人气指标)、"br"(意愿指标)两列

        所需数据字段：
            `open`, `high`, `low`, `close`

        >>> 示例：
            # ARBR超买超卖判断
            arbr = self.kline.tqta.ARBR(n=26)
            ar_overbought = arbr.ar > 150
            ar_oversold = arbr.ar < 70
            br_overbought = arbr.br > 300
            br_oversold = arbr.br < 50
            # ARBR同步分析
            strong_uptrend = (arbr.ar > 100) & (arbr.br > 100)
            strong_downtrend = (arbr.ar < 100) & (arbr.br < 100)
        """
        # ohlc ,ar,br
        ...

    @tobtind(lines=["ddd", "ama"], lib="tqta")
    def DMA(self, short=10, long=50, m=10, **kwargs) -> IndFrame:
        """
        ## 平行线差指标 - 基于移动平均线差值的趋势分析工具

        功能：
            通过长短周期均线差值分析趋势方向和强度

        应用场景：
            - 趋势方向判断
            - 买卖信号生成
            - 趋势强度量化
            - 均线系统优化

        计算原理：
        >>> DDD = 短期移动平均 - 长期移动平均
            AMA = DDD的M周期移动平均

        参数：
            short: 短期周期，默认10
            long: 长期周期，默认50
            m: 平滑周期，默认10
            **kwargs: 额外参数

        注意：
            - DDD上穿AMA为金叉买入信号
            - DDD下穿AMA为死叉卖出信号
            - DDD在零轴上方为多头市场
            - 配合价格走势使用效果更好

        返回值：
            IndFrame: 包含"ddd"(均线差值)、"ama"(差值均线)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # DMA基础信号
            dma = self.close.tqta.DMA(short=10, long=50, m=10)
            bull_market = dma.ddd > 0                    # 多头市场
            golden_cross = dma.ddd.tqta.crossup(dma.ama)  # 金叉
            death_cross = dma.ddd.tqta.crossdown(dma.ama) # 死叉
            # DMA趋势强度
            trend_strength = dma.ddd.abs() / close.tqfunc.ma(length=long)
            strong_trend = trend_strength > 0.05
        """
        # c,ddd,ama
        ...

    @tobtind(lines=["ma1", "ma2"], overlap=True, lib="tqta")
    def EXPMA(self, p1=5, p2=10, **kwargs) -> IndFrame:
        """
        ## 指数加权移动平均线组合 - 对近期价格赋予更高权重的均线系统

        功能：
            提供对价格变化更敏感的移动平均线，减少滞后性

        应用场景：
            - 趋势方向早期识别
            - 短线交易信号
            - 动态支撑阻力位
            - 均线交叉策略

        计算原理：
        >>> EMA = α × 当日收盘价 + (1 - α) × 前日EMA
            α = 2 / (N + 1)

        参数：
            p1: 短期EMA周期，默认5
            p2: 长期EMA周期，默认10
            **kwargs: 额外参数

        注意：
            - 短期EMA上穿长期EMA为买入信号
            - 对价格变化反应比SMA更敏感
            - 在震荡市中可能产生较多假信号
            - 适合趋势明显的市场环境

        返回值：
            IndFrame: 包含"ma1"(短期EMA)、"ma2"(长期EMA)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # EXPMA交叉策略
            expma = self.close.tqta.EXPMA(p1=5, p2=10)
            fast_above_slow = expma.ma1 > expma.ma2      # 快线在慢线上方
            buy_signal = expma.ma1.tqfunc.crossup(expma.ma2) # 金叉买入
            sell_signal = expma.ma1.tqfunc.crossdown(expma.ma2) # 死叉卖出
            # 价格与EXPMA关系
            support_level = expma.ma1.tqfunc.min(expma.ma2)  # 支撑位
            resistance_level = expma.ma1.tqfunc.max(expma.ma2) # 阻力位
        """
        # c,ma1,ma2
        ...

    @tobtind(lines=["cr", "crma"], lib="tqta")
    def CR(self, n=26, m=5, **kwargs) -> IndFrame:
        """
        ## 能量指标 - 反映价格动量和市场人气的综合指标

        功能：
            通过中间价与前一交易日比较分析多空力量对比

        应用场景：
            - 市场能量判断
            - 趋势强度分析
            - 买卖时机选择
            - 价格动量评估

        计算原理：
        >>> CR = (N日内(当日最高价+最低价)/2 - 前一日中间价的正值之和) / 
                 (N日内(前一日中间价 - 当日最高价+最低价)/2的正值之和) × 100
            CRMA = CR的M周期移动平均

        参数：
            n: CR计算周期，默认26
            m: CRMA平滑周期，默认5
            **kwargs: 额外参数

        注意：
            - CR在100附近表示多空平衡
            - CR急升表示能量聚集，可能突破
            - CR与价格顶背离是卖出信号
            - 配合ARBR使用效果更好

        返回值：
            IndFrame: 包含"cr"(能量指标)、"crma"(CR均线)两列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # CR能量分析
            cr_data = self.kline.tqta.CR(n=26, m=5)
            energy_accumulation = cr_data.cr > 150          # 能量聚集
            energy_dispersion = cr_data.cr < 50            # 能量分散
            balance_area = (cr_data.cr >= 80) & (cr_data.cr <= 120) # 多空平衡
            # CR与价格背离
            price_high = close == close.tqfunc.hhv(length=20)
            cr_low = cr_data.cr < cr_data.cr.tqfunc.ref(length=1)
            bearish_divergence = price_high & cr_low
        """
        # hlc,cr,crma
        ...

    @tobtind(lib="tqta")
    def CCI(self, n=14, **kwargs) -> IndSeries:
        """
        ## 顺势指标 - 测量价格偏离统计平均程度的振荡器

        功能：
            识别超买超卖状态和趋势转换点，适用于商品和股票市场

        应用场景：
            - 超买超卖判断
            - 趋势转换预警
            - 极端价格状态识别
            - 短线交易时机选择

        计算原理：
        >>> 典型价格 = (最高价 + 最低价 + 收盘价) / 3
            CCI = (典型价格 - N期典型价格移动平均) / (0.015 × N期典型价格平均绝对偏差)

        参数：
            n: 计算周期，默认14
            **kwargs: 额外参数

        注意：
            - CCI在+100以上为超买区，-100以下为超卖区
            - +100以上回落为卖出信号，-100以下回升为买入信号
            - 在强势趋势中可能长时间停留在超买/超卖区
            - 适合短线交易和极端状态识别

        返回值：
            IndSeries: CCI值序列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # CCI超买超卖信号
            cci = self.kline.tqta.CCI(n=14)
            overbought = cci > 100                    # 超买区域
            oversold = cci < -100                     # 超卖区域
            buy_signal = cci.tqfunc.crossup(-100)        # 从超卖区回升
            sell_signal = cci.tqfunc.crossdown(100)      # 从超买区回落
            # CCI趋势强度
            strong_uptrend = cci > 0
            strong_downtrend = cci < 0
        """
        # hlc
        ...

    @tobtind(lib="tqta")
    def OBV(self, **kwargs) -> IndSeries:
        """
        ## 能量潮指标 - 通过成交量变动预测价格变动的先行指标

        功能：
            将成交量数量化，制成趋势线，配合价格趋势判断量价关系

        应用场景：
            - 量价关系分析
            - 趋势确认
            - 背离分析
            - 资金流向判断

        计算原理：
        >>> 如果当日收盘价 > 前日收盘价，则OBV = 前日OBV + 当日成交量
            如果当日收盘价 < 前日收盘价，则OBV = 前日OBV - 当日成交量
            如果当日收盘价 = 前日收盘价，则OBV = 前日OBV

        参数：
            **kwargs: 额外参数

        注意：
            - OBV与价格同步上升为健康上涨
            - OBV与价格顶背离是卖出信号
            - OBV突破前高确认价格上涨
            - 适合中长期趋势分析

        返回值：
            IndSeries: OBV值序列

        所需数据字段：
            `close`, `volume`

        >>> # 示例：
            # OBV趋势分析
            obv = self.kline.tqta.OBV()
            obv_uptrend = obv > obv.tqfunc.ma(length=20)        # OBV上升趋势
            obv_downtrend = obv < obv.tqfunc.ma(length=20)      # OBV下降趋势
            # OBV背离检测
            price_new_high = close == close.tqfunc.hhv(length=20)
            obv_divergence = obv < obv.tqfunc.hhv(length=20)
            top_divergence = price_new_high & obv_divergence  # 顶背离
            # OBV突破确认
            obv_breakout = obv > obv.tqfunc.hhv(length=20)      # OBV突破前高
        """
        # cv
        ...

    @tobtind(lines=["ah", "al", "nh", "nl"], lib="tqta")
    def CDP(self, n=3, **kwargs) -> IndFrame:
        """
        ## 逆势操作指标 - 短线交易的反向操作工具

        功能：
            为短线交易者提供支撑阻力位参考，适合震荡市操作

        应用场景：
            - 短线支撑阻力位识别
            - 日内交易点位选择
            - 震荡市高抛低吸
            - 突破交易确认

        计算原理：
        >>> CDP = (前日最高 + 前日最低 + 前日收盘 × 2) / 4
            AH = CDP + (前日最高 - 前日最低)
            NH = CDP × 2 - 前日最低
            NL = CDP × 2 - 前日最高
            AL = CDP - (前日最高 - 前日最低)

        参数：
            n: 参考周期，默认3
            **kwargs: 额外参数

        注意：
            - 价格在NL和NH之间震荡时适合反向操作
            - 突破AH或AL可能形成趋势
            - 适合短线日内交易
            - 在趋势明显的市场中效果较差

        返回值：
            IndFrame: 包含"ah"(最高值)、"al"(最低值)、"nh"(近高值)、"nl"(近低值)四列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # CDP交易区间判断
            cdp = self.kline.tqta.CDP(n=3)
            in_trading_range = (close > cdp.nl) & (close < cdp.nh)  # 震荡区间
            breakout_up = close > cdp.ah                              # 向上突破
            breakout_down = close < cdp.al                            # 向下跌破
            # CDP短线交易策略
            buy_zone = close <= cdp.nl        # 买入区域
            sell_zone = close >= cdp.nh       # 卖出区域
            stop_loss_long = cdp.al          # 多头止损
            stop_loss_short = cdp.ah         # 空头止损
        """
        # hlc,ah,al,nh,nl
        ...

    @tobtind(lines=["mah", "mal", "mac"], lib="tqta")
    def HCL(self, n=10, **kwargs) -> IndFrame:
        """
        ## 均线通道指标 - 基于高、低、收盘价的移动平均通道系统

        功能：
            构建价格波动通道，识别趋势方向和波动范围

        应用场景：
            - 趋势方向判断
            - 波动范围测量
            - 支撑阻力位构建
            - 突破交易信号

        计算原理：
        >>> MAH = 最高价的N周期移动平均
            MAL = 最低价的N周期移动平均  
            MAC = 收盘价的N周期移动平均

        参数：
            n: 移动平均周期，默认10
            **kwargs: 额外参数

        注意：
            - 价格在MAH和MAL之间波动为震荡市
            - 突破MAH为强势上涨信号
            - 跌破MAL为强势下跌信号
            - MAC代表趋势方向

        返回值：
            IndFrame: 包含"mah"(最高价均线)、"mal"(最低价均线)、"mac"(收盘价均线)三列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # HCL通道分析
            hcl = tqta.HCL(n=10)
            in_channel = (close >= hcl.mal) & (close <= hcl.mah)  # 通道内震荡
            breakout_up = close > hcl.mah                           # 向上突破
            breakdown = close < hcl.mal                             # 向下跌破
            # 趋势方向判断
            uptrend = (hcl.mac > hcl.mac.tqfunc.ref(length=1)) & 
                     (hcl.mah > hcl.mah.tqfunc.ref(length=1))
            downtrend = (hcl.mac < hcl.mac.tqfunc.ref(length=1)) & 
                       (hcl.mal < hcl.mal.tqfunc.ref(length=1))
        """
        # hlc,mah,mal,mac
        ...

    @tobtind(lines=["upper", "lower"], overlay=True, lib="tqta")
    def ENV(self, n=14, k=6, **kwargs) -> IndFrame:
        """
        ## 包络线指标 - 基于移动平均线的动态通道系统

        功能：
            在移动平均线上下构建固定百分比的通道，识别超买超卖

        应用场景：
            - 动态支撑阻力位
            - 超买超卖识别
            - 趋势跟踪
            - 回归均值策略

        计算原理：
        >>> 中线 = 收盘价的N周期移动平均
            上轨 = 中线 × (1 + K%)
            下轨 = 中线 × (1 - K%)

        参数：
            n: 移动平均周期，默认14
            k: 通道宽度参数，默认6，表示6%
            **kwargs: 额外参数

        注意：
            - 价格触及上轨可能回调，触及下轨可能反弹
            - 在趋势市中价格可能沿通道运行
            - 通道宽度需要根据波动性调整
            - 适合均值回归策略

        返回值：
            IndFrame: 包含"upper"(上轨)、"lower"(下轨)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # ENV通道交易策略
            env = self.close.tqta.ENV(n=14, k=6)
            overbought = close > env.upper                  # 触及上轨超买
            oversold = close < env.lower                    # 触及下轨超卖
            middle_line = (env.upper + env.lower) / 2    # 通道中线
            # 回归均值策略
            buy_signal = (close < env.lower) & 
                        (close.tqfunc.ref(length=1) >= env.lower.tqfunc.ref(length=1))
            sell_signal = (close > env.upper) & 
                         (close.tqfunc.ref(length=1) <= env.upper.tqfunc.ref(length=1))
        """
        # c,upper,lower
        ...

    @tobtind(lines=["wr_", "mr", "sr", "ws", "ms", "ss"], lib="tqta")
    def MIKE(self, n=12, **kwargs) -> IndFrame:
        """
        ## 麦克指标 - 压力支撑分析系统，提供多级支撑阻力位

        功能：
            通过复杂计算提供六条不同级别的支撑阻力线，辅助判断价格运行区间

        应用场景：
            - 多级支撑阻力位识别
            - 价格目标位预测
            - 突破交易确认
            - 区间震荡交易

        计算原理：
        >>> 基于典型价格和价格波动幅度计算六个不同级别的支撑阻力位：
            WR(初级压力)、MR(中级压力)、SR(强力压力)
            WS(初级支撑)、MS(中级支撑)、SS(强力支撑)

        参数：
            n: 计算周期，默认12
            **kwargs: 额外参数

        注意：
            - 价格在WS-WR之间为正常波动区间
            - 突破WR可能向MR、SR运行
            - 跌破WS可能向MS、SS运行
            - 适合中短线交易和仓位管理

        返回值：
            IndFrame: 包含"wr_"(初级压力)、"mr"(中级压力)、"sr"(强力压力)、
                      "ws"(初级支撑)、"ms"(中级支撑)、"ss"(强力支撑)六列

        所需数据字段：
            `high`, `low`, `close`

        >>> 示例：
            # MIKE支撑阻力分析
            mike = tqta.MIKE(n=12)
            normal_range = (close >= mike.ws) & (close <= mike.wr)  # 正常区间
            strong_resistance = close > mike.sr                       # 强力阻力区
            strong_support = close < mike.ss                          # 强力支撑区
            # 突破交易信号
            break_resistance = (close.tqfunc.ref(length=1) <= mike.wr) & (close > mike.wr)
            break_support = (close.tqfunc.ref(length=1) >= mike.ws) & (close < mike.ws)
        """
        # hlc,wr,mr,sr,ws,ms,ss
        ...

    @tobtind(lib="tqta")
    def PUBU(self, m=4, **kwargs) -> IndSeries:
        """
        ## 瀑布线指标 - 非线性移动平均系统，过滤价格噪音

        功能：
            通过特殊的移动平均计算方法，减少价格波动干扰，突出趋势方向

        应用场景：
            - 趋势方向过滤
            - 买卖信号生成
            - 价格噪音消除
            - 趋势跟踪策略

        计算原理：
        >>> 基于收盘价计算特殊的移动平均线，算法相对复杂，
            旨在平滑价格波动，突出主要趋势方向

        参数：
            m: 计算周期，默认4
            **kwargs: 额外参数

        注意：
            - 瀑布线上涨为多头趋势，下跌为空头趋势
            - 价格在瀑布线上方运行为强势
            - 适合趋势明显的市场环境
            - 在震荡市中可能产生假信号

        返回值：
            IndSeries: 瀑布线值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # 瀑布线趋势判断
            pubu = self.close.tqta.PUBU(m=4)
            uptrend = pubu > pubu.tqfunc.ref(length=1)          # 上升趋势
            downtrend = pubu < pubu.tqfunc.ref(length=1)        # 下降趋势

            # 价格与瀑布线关系
            strong_bull = close > pubu                       # 强势多头
            weak_bull = (close > pubu) & (close < close.tqfunc.ref(length=1))  # 弱势多头
            price_breakout = (close.tqfunc.ref(length=1) <= pubu) & (close > pubu)  # 价格突破
        """
        # c
        ...

    @tobtind(lib="tqta")
    def BBI(self, n1=3, n2=6, n3=12, n4=24, **kwargs) -> IndSeries:
        """
        ## 多空指数 - 多周期移动平均综合指标

        功能：
            综合不同时间周期的移动平均线，提供更全面的趋势判断

        应用场景：
            - 多周期趋势综合分析
            - 买卖点确认
            - 趋势强度评估
            - 均线系统简化

        计算原理：
        >>> BBI = (3日均价 + 6日均价 + 12日均价 + 24日均价) / 4
            综合短、中、长期移动平均线的优势

        参数：
            n1: 短期周期，默认3
            n2: 中短期周期，默认6
            n3: 中期周期，默认12
            n4: 长期周期，默认24
            **kwargs: 额外参数

        注意：
            - 价格在BBI上方为多头市场
            - BBI上升角度反映趋势强度
            - 可作为其他指标的参考基准
            - 适合各类时间周期的分析

        返回值：
            IndSeries: BBI值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # BBI多空判断
            bbi = self.close.tqta.BBI(n1=3, n2=6, n3=12, n4=24)
            bull_market = close > bbi                         # 多头市场
            bear_market = close < bbi                         # 空头市场
            # BBI趋势强度
            bbi_trend_strength = (bbi - bbi.tqfunc.ref(length=5)) / bbi.tqfunc.ref(length=5)
            strong_trend = bbi_trend_strength.abs() > 0.02     # 强势趋势
            # BBI突破信号
            break_bull = (close.tqfunc.ref(length=1) <= bbi) & (close > bbi)    # 向上突破
            break_bear = (close.tqfunc.ref(length=1) >= bbi) & (close < bbi)    # 向下跌破
        """
        # c
        ...

    @tobtind(lines=["b", "d"], overlay=True, lib="tqta")
    def DKX(self, m=10, **kwargs) -> IndFrame:
        """
        ## 多空线指标 - 综合价格和成交量的多空力量分析

        功能：
            通过复杂算法综合价格和成交量信息，判断多空力量对比

        应用场景：
            - 多空力量对比分析
            - 趋势方向确认
            - 买卖时机选择
            - 量价关系验证

        计算原理：
        >>> 基于开盘、最高、最低、收盘价和中间价计算多空线，
            再计算其移动平均作为参考线

        参数：
            m: 移动平均周期，默认10
            **kwargs: 额外参数

        注意：
            - 多空线上穿其均线为买入信号
            - 多空线下穿其均线为卖出信号
            - 两者同步上升为强势多头
            - 适合中短线交易

        返回值：
            IndFrame: 包含"b"(多空线)、"d"(多空线均线)两列

        所需数据字段：
            `open`, `high`, `low`, `close`

        >>> # 示例：
            # DKX多空信号
            dkx = self.kline.tqta.DKX(m=10)
            bull_signal = dkx.b.tqfunc.crossup(dkx.d)      # 多头信号
            bear_signal = dkx.b.tqfunc.crossdown(dkx.d)    # 空头信号
            # 多空力量强度
            strong_bull = (dkx.b > dkx.d) & (dkx.b > dkx.b.tqfunc.ref( length=1))
            strong_bear = (dkx.b < dkx.d) & (dkx.b < dkx.b.tqfunc.ref( length=1))
            # DKX与价格背离
            price_high = close == close.tqfunc.hhv(length=20)
            dkx_low = dkx.b < dkx.b.tqfunc.ref( length=1)
            top_divergence = price_high & dkx_low
        """
        # ohlc,b,d
        ...

    @tobtind(lines=["bbiboll", "upr", "dwn"], overlay=True, lib="tqta")
    def BBIBOLL(self, n=10, m=3, **kwargs) -> IndFrame:
        """
        ## 多空布林线 - BBI与布林带结合的趋势通道系统

        功能：
            在多空指数基础上构建布林通道，提供趋势和波动性双重分析

        应用场景：
            - 趋势通道分析
            - 波动率测量
            - 超买超卖判断
            - 突破交易信号

        计算原理：
        >>> BBIBOLL = BBI多空指数
            UPR = BBIBOLL + M × BBIBOLL的N周期标准差
            DWN = BBIBOLL - M × BBIBOLL的N周期标准差

        参数：
            n: BBI计算周期参数，默认10
            m: 标准差倍数，默认3
            **kwargs: 额外参数

        注意：
            - 价格在通道内运行为震荡市
            - 突破上轨可能继续上涨，跌破下轨可能继续下跌
            - 通道收窄预示重大价格变动
            - 适合趋势跟踪和突破策略

        返回值：
            IndFrame: 包含"bbiboll"(多空布林线)、"upr"(压力线)、"dwn"(支撑线)三列

        所需数据字段：
            `close`

        >>> # 示例：
            # BBIBOLL通道分析
            bbiboll = self.kline.close.tqta.BBIBOLL(n=10, m=3)
            in_channel = (close >= bbiboll.dwn) & (close <= bbiboll.upr)  # 通道内
            breakout_up = close > bbiboll.upr                               # 向上突破
            breakdown = close < bbiboll.dwn                                 # 向下跌破
            # 通道宽度分析
            channel_width = (bbiboll.upr - bbiboll.dwn) / bbiboll.bbiboll
            narrow_channel = channel_width < channel_width.tqfunc.ma(length=20)   # 通道收窄
            wide_channel = channel_width > channel_width.tqfunc.ma(length=20)     # 通道扩张
        """
        # close,bbiboll,upr,dwn
        ...

    @tobtind(lines=["adtm", "adtmma"], lib="tqta")
    def ADTM(self, n=23, m=8, **kwargs) -> IndFrame:
        """
        ## 动态买卖气指标 - 衡量市场动态买卖力量的振荡器

        功能：
            通过价格在区间内的相对位置分析买卖力量动态变化

        应用场景：
            - 买卖力量对比
            - 超买超卖判断
            - 趋势转换预警
            - 短线交易时机

        计算原理：
        >>> 基于开盘价与价格区间的关系计算动态买卖气，
            再计算其移动平均作为参考

        参数：
            n: 主要计算周期，默认23
            m: 移动平均周期，默认8
            **kwargs: 额外参数

        注意：
            - ADTM在0轴上方为买方主导
            - ADTM在0轴下方为卖方主导
            - 上穿0轴为买入信号，下穿0轴为卖出信号
            - 适合震荡市和反转交易

        返回值：
            IndFrame: 包含"adtm"(动态买卖气)、"adtmma"(买卖气均线)两列

        所需数据字段：
            `open`, `high`, `low`

        >>> # 示例：
            # ADTM多空判断
            adtm = self.kline.tqta.ADTM(n=23, m=8)
            buyer_dominant = adtm.adtm > 0                 # 买方主导
            seller_dominant = adtm.adtm < 0                # 卖方主导
            # ADTM交易信号
            buy_signal = adtm.adtm.tqta.crossup(0)          # 买入信号
            sell_signal = adtm.adtm.tqta.crossdown(0)       # 卖出信号
            # ADTM与均线关系
            strong_buy = (adtm.adtm > adtm.adtmma) & (adtm.adtm > 0)
            strong_sell = (adtm.adtm < adtm.adtmma) & (adtm.adtm < 0)
        """
        # ohl,adtm,adtmma
        ...

    @tobtind(lines=["b36", "b612"], lib="tqta")
    def B3612(self, **kwargs) -> IndFrame:
        """
        ## 三减六日乖离率 - 短期均线乖离分析系统

        功能：
            通过不同周期移动平均线的乖离关系分析短期趋势动量

        应用场景：
            - 短期趋势动量分析
            - 均线系统优化
            - 买卖时机选择
            - 趋势强度评估

        计算原理：
        >>> B36 = 3日移动平均 - 6日移动平均
            B612 = 6日移动平均 - 12日移动平均
            反映不同周期均线之间的偏离程度

        参数：
            **kwargs: 额外参数

        注意：
            - B36反映极短期动量变化
            - B612反映短期趋势方向
            - 两者同向为趋势确认
            - 适合短线交易和趋势确认

        返回值：
            IndFrame: 包含"b36"(3-6日乖离)、"b612"(6-12日乖离)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # B3612动量分析
            b3612 = self.kline.close.tqta.B3612()
            short_momentum = b3612.b36 > 0                 # 短期动量向上
            medium_trend = b3612.b612 > 0                  # 中期趋势向上
            # 多周期协同
            strong_uptrend = (b3612.b36 > 0) & (b3612.b612 > 0)  # 强势上涨
            trend_reversal = (b3612.b36 > 0) & (b3612.b612 < 0)  # 趋势转换
            # 乖离率极端值
            extreme_bull = b3612.b36 > b3612.b36.tqfunc.hhv(length=20) * 0.8
            extreme_bear = b3612.b36 < b3612.b36.tqfunc.llv(length=20) * 0.8
        """
        # c,b36,b612
        ...

    @tobtind(lines=["dbcd", "mm"], lib="tqta")
    def DBCD(self, n=5, m=16, t=76, **kwargs) -> IndFrame:
        """
        ## 异同离差乖离率 - 乖离率的优化版本，减少噪音干扰

        功能：
            通过复杂的乖离率计算和平滑处理，提供更稳定的趋势信号

        应用场景：
            - 趋势方向过滤
            - 买卖信号生成
            - 价格偏离度分析
            - 中长期趋势判断

        计算原理：
            基于BIAS乖离率进行多级计算和平滑处理，
            得到更稳定的DBCD指标及其移动平均

        参数：
            n: BIAS计算周期，默认5
            m: 第一次平滑周期，默认16
            t: 第二次平滑周期，默认76
            **kwargs: 额外参数

        注意：
            - DBCD上穿其均线为买入信号
            - DBCD下穿其均线为卖出信号
            - 指标波动较小，信号相对稳定
            - 适合中长线趋势跟踪

        返回值：
            IndFrame: 包含"dbcd"(异同离差乖离率)、"mm"(乖离率均线)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # DBCD趋势信号
            dbcd = self.kline.close.tqta.DBCD(n=5, m=16, t=76)
            buy_signal = dbcd.dbcd.tqfunc.crossup(dbcd.mm)  # 买入信号
            sell_signal = dbcd.dbcd.tqfunc.crossdown(dbcd.mm) # 卖出信号
            # DBCD趋势强度
            uptrend_strength = dbcd.dbcd - dbcd.mm       # 上升趋势强度
            downtrend_strength = dbcd.mm - dbcd.dbcd     # 下降趋势强度
            # 零轴分析
            above_zero = dbcd.dbcd > 0                      # 零轴上方
            below_zero = dbcd.dbcd < 0                      # 零轴下方
        """
        # c,dbcd,mm
        ...

    @tobtind(lines=["ddi", "addi", "ad_"], lib="tqta")
    def DDI(self, n=13, n1=30, m=10, m1=5, **kwargs) -> IndFrame:
        """
        ## 方向标准离差指数 - 趋势方向和波动性综合指标

        功能：
            通过价格波动方向和幅度分析趋势强度和持续性

        应用场景：
            - 趋势方向确认
            - 波动性分析
            - 买卖信号生成
            - 趋势强度量化

        计算原理：
            基于最高最低价计算方向离差，通过多级平滑得到DDI、ADDI、AD等指标

        参数：
            n: 方向计算周期，默认13
            n1: 离差计算周期，默认30
            m: 第一次平滑周期，默认10
            m1: 第二次平滑周期，默认5
            **kwargs: 额外参数

        注意：
            - DDI反映短期趋势方向
            - ADDI反映中期趋势强度
            - AD确认趋势持续性
            - 三者同步为趋势确认

        返回值：
            IndFrame: 包含"ddi"(方向离差)、"addi"(加权平均)、"ad_"(移动平均)三列

        所需数据字段：
            `high`, `low`

        >>> # 示例：
            # DDI趋势系统
            ddi = self.kline.tqta.DDI(n=13, n1=30, m=10, m1=5)
            trend_confirmed = (ddi.ddi > 0) & (ddi.addi > 0) & (ddi.ad > 0)  # 趋势确认
            # DDI强度分级
            strong_uptrend = ddi.ddi > ddi.ddi.tqfunc.hhv(length=20) * 0.7
            weak_uptrend = (ddi.ddi > 0) & (ddi.ddi < ddi.ddi.tqfunc.ma(length=10))
            # 趋势转换信号
            trend_turn_up = (ddi.ddi.tqfunc.ref(length=1) <= 0) & (ddi.ddi > 0)
            trend_turn_down = (ddi.ddi.tqfunc.ref(length=1) >= 0) & (ddi.ddi < 0)
        """
        # hl,ddi,addi,ad
        ...

    @tobtind(lib="tqta")
    def KD(self, n=9, m1=3, m2=3, **kwargs) -> IndFrame:
        """
        ## 随机指标(KD) - KDJ指标的简化版本，去除J值

        功能：
            通过价格在周期内相对位置分析市场动量和超买超卖状态

        应用场景：
            - 超买超卖判断
            - 短线买卖时机
            - 背离分析
            - 趋势转换预警

        计算原理：
        >>> RSV = (收盘价 - N日内最低价) / (N日内最高价 - N日内最低价) × 100
            K = RSV的M1周期简单移动平均
            D = K的M2周期简单移动平均

        参数：
            n: RSV计算周期，默认9
            m1: K值平滑周期，默认3
            m2: D值平滑周期，默认3
            **kwargs: 额外参数

        注意：
            - K、D值在80以上为超买区，20以下为超卖区
            - K线上穿D线为金叉买入信号
            - K线下穿D线为死叉卖出信号
            - 背离信号可靠性较高

        返回值：
            IndFrame: 包含"k"(K值)、"d"(D值)两列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # KD超买超卖判断
            kd = self.kline.tqta.KD(n=9, m1=3, m2=3)
            overbought = (kd.k > 80) & (kd.d > 80)      # 超买区域
            oversold = (kd.k < 20) & (kd.d < 20)        # 超卖区域
            # KD交易信号
            golden_cross = kd.k.tqfunc.crossup(kd.d)       # 金叉买入
            death_cross = kd.k.tqfunc.crossdown(kd.d)      # 死叉卖出
            # KD位置分析
            bull_zone = (kd.k > 50) & (kd.d > 50)       # 多头区域
            bear_zone = (kd.k < 50) & (kd.d < 50)       # 空头区域
        """
        # hlc,k,d
        ...

    @tobtind(lib="tqta")
    def LWR(self, n=9, m=3, **kwargs) -> IndSeries:
        """
        ## 威廉指标(LWR) - 反向威廉指标，与WR指标计算方式相反

        功能：
            通过价格在周期内相对位置分析市场动量和超买超卖状态，数值范围与WR相反

        应用场景：
            - 超买超卖判断
            - 短线买卖时机
            - 背离分析
            - 趋势转换预警

        计算原理：
        >>> LWR = (N日内最低价 - 当日收盘价) / (N日内最高价 - N日内最低价) × (-100)
            数值在0到-100之间，但方向与WR指标相反

        参数：
            n: 计算周期，默认9
            m: 平滑周期，默认3
            **kwargs: 额外参数

        注意：
            - LWR在-20以下为超买区，-80以上为超卖区
            - 与传统WR指标数值方向相反
            - 可结合其他指标确认信号
            - 在强势趋势中可能出现指标钝化

        返回值：
            IndSeries: LWR值序列，范围为-100到0

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # LWR超买超卖判断
            lwr = self.kline.tqta.LWR(n=9, m=3)
            overbought = lwr < -20      # 超买区域
            oversold = lwr > -80        # 超卖区域
            # LWR背离分析
            price_new_high = close == close.tqfunc.hhv(length=20)
            lwr_new_low = lwr < lwr.tqfunc.llv(length=20)
            bearish_divergence = price_new_high & lwr_new_low  # 顶背离
        """
        # hlc
        ...

    @tobtind(lib="tqta")
    def MASS(self, n1=9, n2=25, **kwargs) -> IndSeries:
        """
        ## 梅斯线指标 - 价格波动幅度和强度测量工具

        功能：
            通过高低价区间分析价格波动幅度，识别趋势转折点

        应用场景：
            - 趋势转折预警
            - 波动性爆发识别
            - 突破信号确认
            - 价格极端状态判断

        计算原理：
            基于最高最低价区间计算波动幅度，通过两次指数移动平均得到MASS线
            反映价格波动的强度和频率

        参数：
            n1: 第一次EMA周期，默认9
            n2: 第二次EMA周期，默认25
            **kwargs: 额外参数

        注意：
            - MASS高于27后回落为趋势转折信号
            - MASS线急剧上升预示波动性增加
            - 适合识别价格波动的极端状态
            - 常与其他趋势指标结合使用

        返回值：
            IndSeries: MASS值序列

        所需数据字段：
            `high`, `low`

        >>> # 示例：
            # MASS趋势转折信号
            mass = self.kline.tqta.MASS(n1=9, n2=25)
            reversal_signal = (mass.tqfunc.ref(length=1) > 27) & (mass <= 27)  # 转折信号
            # 波动性分析
            high_volatility = mass > mass.tqfunc.ma(length=20)                # 高波动期
            low_volatility = mass < mass.tqfunc.ma(length=20)                 # 低波动期
            # MASS突破预警
            mass_breakout = mass > mass.tqfunc.hhv(length=20)                 # 波动性爆发
        """
        # hl
        ...

    @tobtind(lib="tqta")
    def MFI(self, n=14, **kwargs) -> IndSeries:
        """
        ## 资金流量指标 - 带成交量的相对强弱指标

        功能：
            结合价格和成交量分析资金流向，识别超买超卖状态

        应用场景：
            - 资金流向分析
            - 超买超卖判断
            - 背离分析
            - 量价关系验证

        计算原理：
        >>> MFI计算方式类似RSI，但加入了成交量因素
            典型价格 = (最高 + 最低 + 收盘) / 3
            资金流 = 典型价格 × 成交量
            通过正负资金流比率计算MFI

        参数：
            n: 计算周期，默认14
            **kwargs: 额外参数

        注意：
            - MFI在80以上为超买区，20以下为超卖区
            - 与价格背离时信号更可靠
            - 反映资金的实际流入流出
            - 适合中短线交易分析

        返回值：
            IndSeries: MFI值序列，范围0-100

        所需数据字段：
            `high`, `low`, `close`, `volume`

        >>> # 示例：
            # MFI超买超卖判断
            mfi = self.kline.tqta.MFI(n=14)
            overbought = mfi > 80                    # 超买
            oversold = mfi < 20                      # 超卖
            # MFI资金流向
            money_inflow = mfi > 50                  # 资金流入
            money_outflow = mfi < 50                 # 资金流出
            # MFI与价格背离
            price_high = close == close.tqfunc.hhv(length=20)
            mfi_low = mfi < mfi.tqfunc.ref(length=1)
            top_divergence = price_high & mfi_low    # 顶背离
        """
        # hlcv
        ...

    @tobtind(lines=["a", "mi"], lib="tqta")
    def MI(self, n=12, **kwargs) -> IndFrame:
        """
        # 动量指标 - 价格变动速率和方向分析

        功能：
            测量价格变动速度和幅度，识别趋势动量和转折点

        应用场景：
            - 趋势动量分析
            - 买卖时机选择
            - 趋势强度评估
            - 反转信号预警

        计算原理：
        >>> A = 当日收盘价 - N日前收盘价
            MI = A的平滑处理值
            反映价格在N周期内的变动动量

        参数：
            n: 计算周期，默认12
            **kwargs: 额外参数

        注意：
            - MI为正表示上升动量，为负表示下降动量
            - MI上穿零轴为买入信号，下穿零轴为卖出信号
            - 动量极值往往预示趋势转折
            - 适合趋势跟踪和动量策略

        返回值：
            IndFrame: 包含"a"(价格差值)、"mi"(动量指标)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # MI动量分析
            mi = tqta.MI(n=12)
            positive_momentum = mi.mi > 0                  # 正动量
            negative_momentum = mi.mi < 0                  # 负动量
            # MI交易信号
            buy_signal = mi.mi.tqfunc.crossup(0)              # 上穿零轴买入
            sell_signal = mi.mi.tqfunc.crossdown(0)           # 下穿零轴卖出
            # 动量极值识别
            momentum_extreme = mi.mi.abs() > mi.mi.abs().tqfunc.ma(length=20) * 2
        """
        # c,a,mi
        ...

    @tobtind(lines=["dif", "micd"], lib="tqta")
    def MICD(self, n=3, n1=10, n2=20, **kwargs) -> IndFrame:
        """
        ## 异同离差动力指数 - 动量指标的MACD版本

        功能：
            在动量指标基础上进行离差分析，提供更稳定的动量信号

        应用场景：
            - 动量趋势分析
            - 买卖信号生成
            - 趋势转换预警
            - 动量强度量化

        计算原理：
            基于动量指标进行多周期离差计算
            DIF = 短期动量 - 长期动量
            MICD = DIF的平滑移动平均
            类似MACD但对动量指标进行计算

        参数：
            n: 动量计算周期，默认3
            n1: 短期周期，默认10
            n2: 长期周期，默认20
            **kwargs: 额外参数

        注意：
            - DIF上穿MICD为金叉买入信号
            - DIF下穿MICD为死叉卖出信号
            - 零轴上方为多头动量，下方为空头动量
            - 适合中短线动量交易

        返回值：
            IndFrame: 包含"dif"(离差值)、"micd"(异同离差动力指数)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # MICD动量信号
            micd = self.kline.close.tqta.MICD(n=3, n1=10, n2=20)
            bull_momentum = micd.dif > 0                    # 多头动量
            golden_cross = micd.dif.tqfunc.crossup(micd.micd)  # 金叉
            death_cross = micd.dif.tqfunc.crossdown(micd.micd) # 死叉
            # 动量强度分析
            momentum_strength = (micd.dif - micd.micd).abs()
            strong_momentum = momentum_strength > momentum_strength.tqfunc.ma(length=20)
        """
        # c,dif,micd
        ...

    @tobtind(lines=["mtm", "mtmma"], lib="tqta")
    def MTM(self, n=6, n1=6, **kwargs) -> IndFrame:
        """
        ## 动量指标(MTM) - 经典的价格动量振荡器

        功能：
            测量价格变化速率，识别趋势动量和超买超卖状态

        应用场景：
            - 趋势动量分析
            - 超买超卖判断
            - 背离分析
            - 买卖时机选择

        计算原理：
        >>> MTM = 当日收盘价 - N日前收盘价
            MTMMA = MTM的M周期简单移动平均
            反映价格在N周期内的变动动量

        参数：
            n: 动量计算周期，默认6
            n1: 移动平均周期，默认6
            **kwargs: 额外参数

        注意：
            - MTM上穿零轴为买入信号，下穿零轴为卖出信号
            - MTM与价格顶背离是卖出信号
            - MTM与价格底背离是买入信号
            - 适合各类时间周期的分析

        返回值：
            IndFrame: 包含"mtm"(动量值)、"mtmma"(动量均线)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # MTM动量分析
            mtm = self.kline.close.tqta.MTM(n=6, n1=6)
            positive_momentum = mtm.mtm > 0                # 正动量
            momentum_cross = mtm.mtm.tqfunc.crossup(mtm.mtmma)  # 动量金叉
            # MTM背离分析
            price_new_high = close == close.tqfunc.hhv(length=20)
            mtm_lower_high = mtm.mtm < mtm.mtm.tqfunc.ref(length=1)
            bearish_divergence = price_new_high & mtm_lower_high  # 顶背离
            # MTM超买超卖
            overbought = mtm.mtm > mtm.mtm.tqfunc.hhv(length=20) * 0.8
            oversold = mtm.mtm < mtm.mtm.tqfunc.llv(length=20) * 0.8
        """
        # c,mtm,mtmma
        ...

    @tobtind(lib="tqta")
    def PRICEOSC(self, long=26, short=12, **kwargs) -> IndSeries:
        """
        ## 价格震荡指数 - 长短周期移动平均离差指标

        功能：
            通过长短周期移动平均线的离差分析价格动量和趋势方向

        应用场景：
            - 趋势方向判断
            - 动量强度分析
            - 买卖信号生成
            - 趋势转换预警

        计算原理：
            PRICEOSC = (短期移动平均 - 长期移动平均) / 长期移动平均 × 100
            反映长短周期均线的相对位置关系

        参数：
            long: 长期周期，默认26
            short: 短期周期，默认12
            **kwargs: 额外参数

        注意：
            - PRICEOSC上穿零轴为买入信号
            - PRICEOSC下穿零轴为卖出信号
            - 数值大小反映趋势强度
            - 适合趋势跟踪和动量策略

        返回值：
            IndSeries: 价格震荡指数序列，单位为百分比

        所需数据字段：
            `close`

        >>> # 示例：
            # PRICEOSC趋势判断
            priceosc = self.kline.close.tqta.PRICEOSC(long=26, short=12)
            bull_market = priceosc > 0                        # 多头市场
            bear_market = priceosc < 0                        # 空头市场
            # PRICEOSC交易信号
            buy_signal = priceosc.tqta.crossup(0)              # 上穿零轴买入
            sell_signal = priceosc.tqta.crossdown(0)           # 下穿零轴卖出
            # 趋势强度分析
            trend_strength = priceosc.abs()
            strong_trend = trend_strength > trend_strength.tqfunc.ma(length=20)
        """
        # close
        ...

    @tobtind(lines=["psy", "psyma"], lib="tqta")
    def PSY(self, n=12, m=6, **kwargs) -> IndFrame:
        """
        ## 心理线指标 - 投资者情绪和心理状态测量工具

        功能：
            通过上涨天数比率分析市场心理状态，识别超买超卖

        应用场景：
            - 市场情绪分析
            - 超买超卖判断
            - 趋势转换预警
            - 投资者心理测量

        计算原理：
            PSY = (N日内上涨天数 / N) × 100
            PSYMA = PSY的M周期简单移动平均
            反映投资者在N周期内的心理状态

        参数：
            n: 计算周期，默认12
            m: 移动平均周期，默认6
            **kwargs: 额外参数

        注意：
            - PSY在75以上为超买区，25以下为超卖区
            - PSY与价格背离时信号更可靠
            - 反映市场集体心理状态
            - 适合逆向投资策略

        返回值：
            IndFrame: 包含"psy"(心理线)、"psyma"(心理线均线)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # PSY心理状态分析
            psy = tqta.PSY(n=12, m=6)
            over_optimistic = psy.psy > 75                 # 过度乐观
            over_pessimistic = psy.psy < 25                # 过度悲观
            # PSY交易信号
            buy_zone = (psy.psy < 25) & (psy.psy.tqfunc.ref(length=1) >= 25)
            sell_zone = (psy.psy > 75) & (psy.psy.tqfunc.ref(length=1) <= 75)
            # PSY与均线关系
            sentiment_improving = psy.psy > psy.psyma   # 情绪改善
            sentiment_deteriorating = psy.psy < psy.psyma # 情绪恶化
        """
        # c,psy,psyma
        ...

    @tobtind(lines=["qhl5", "qhl10"], lib="tqta")
    def QHLSR(self, **kwargs) -> IndFrame:
        """
        ## 阻力指标 - 量价关系阻力分析系统

        功能：
            通过价格和成交量关系分析市场阻力和支撑水平

        应用场景：
            - 阻力支撑位识别
            - 量价关系分析
            - 突破交易确认
            - 市场强度评估

        计算原理：
            基于最高、最低、收盘价和成交量计算阻力系数
            QHL5: 5日阻力系数
            QHL10: 10日阻力系数
            反映价格在成交量配合下的阻力程度

        参数：
            **kwargs: 额外参数

        注意：
            - QHL值越高表示阻力越大
            - QHL值接近1表示强阻力，接近0表示弱阻力
            - 可结合价格位置判断阻力有效性
            - 适合突破交易和区间交易

        返回值：
            IndFrame: 包含"qhl5"(5日阻力)、"qhl10"(10日阻力)两列

        所需数据字段：
            `high`, `low`, `close`, `volume`

        >>> # 示例：
            # QHLSR阻力分析
            qhlsr = self.kline.tqta.QHLSR()
            strong_resistance = qhlsr.qhl5 > 0.8           # 强阻力
            weak_resistance = qhlsr.qhl5 < 0.2             # 弱阻力
            # 阻力变化分析
            resistance_increasing = qhlsr.qhl5 > qhlsr.qhl5.tqfunc.ref(length=1)
            resistance_decreasing = qhlsr.qhl5 < qhlsr.qhl5.tqfunc.ref(length=1)
            # 多周期阻力对比
            short_term_stronger = qhlsr.qhl5 > qhlsr.qhl10  # 短期阻力更强
        """
        # hlcv,qhl5,qhl10
        ...

    @tobtind(lib="tqta")
    def RC(self, n=50, **kwargs) -> IndSeries:
        """
        ## 变化率指数 - 价格变动速率标准化指标

        功能：
            测量价格变化速率，并进行标准化处理，便于跨品种比较

        应用场景：
            - 价格变动速率分析
            - 趋势强度比较
            - 动量策略构建
            - 跨品种分析

        计算原理：
            RC = 当日收盘价 / N日前收盘价
            反映价格在N周期内的变化比率

        参数：
            n: 计算周期，默认50
            **kwargs: 额外参数

        注意：
            - RC大于1表示上涨，小于1表示下跌
            - RC值大小反映变动幅度
            - 便于不同品种间的动量比较
            - 适合动量投资和趋势跟踪

        返回值：
            IndSeries: 变化率指数序列

        所需数据字段：
            `close`

        >>> # 示例：
            # RC变化率分析
            rc = self.kline.close.tqta.RC(n=50)
            price_up = rc > 1                                # 价格上涨
            price_down = rc < 1                              # 价格下跌
            # 变动幅度分析
            strong_rise = rc > 1.1                           # 强势上涨
            strong_fall = rc < 0.9                           # 强势下跌
            # 动量比较
            momentum_rank = rc.rank(ascending=False)         # 动量排名
            top_momentum = momentum_rank <= 10               # 前10动量
        """
        # c
        ...

    @tobtind(lines=["dif", "rccd"], lib="tqta")
    def RCCD(self, n=10, n1=21, n2=28, **kwargs) -> IndFrame:
        """
        ## 异同离差变化率指数 - RC指标的MACD版本

        功能：
            在变化率指标基础上进行离差分析，提供更稳定的变化率信号

        应用场景：
            - 变化率趋势分析
            - 买卖信号生成
            - 动量转换预警
            - 跨周期变化率比较

        计算原理：
            基于变化率指标进行多周期离差计算
            DIF = 短期变化率 - 长期变化率
            RCCD = DIF的平滑移动平均
            类似MACD但对变化率指标进行计算

        参数：
            n: 基础变化率周期，默认10
            n1: 短期周期，默认21
            n2: 长期周期，默认28
            **kwargs: 额外参数

        注意：
            - DIF上穿RCCD为金叉买入信号
            - DIF下穿RCCD为死叉卖出信号
            - 零轴上方为正向变化率，下方为负向变化率
            - 适合中长线趋势分析

        返回值：
            IndFrame: 包含"dif"(离差值)、"rccd"(异同离差变化率指数)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # RCCD变化率信号
            rccd = self.close.tqta.RCCD(n=10, n1=21, n2=28)
            positive_change = rccd.dif > 0                  # 正向变化
            golden_cross = rccd.dif.tqfunc.crossup(rccd.rccd)  # 金叉
            death_cross = rccd.dif.tqfunc.crossdown(rccd.rccd) # 死叉
            # 变化率强度分析
            change_strength = (rccd.dif - rccd.rccd).abs()
            strong_change = change_strength > change_strength.tqfunc.ma(length=20)
        """
        # c,dif,rccd
        ...

    @tobtind(lines=["roc_", "rocma"], lib="tqta")
    def ROC(self, n=24, m=20, **kwargs) -> IndFrame:
        """
        ## 变动速率指标 - 价格变化百分比动量振荡器

        功能：
            测量价格变化的百分比速率，识别动量极值和转折点

        应用场景：
            - 动量强度分析
            - 超买超卖判断
            - 趋势转换预警
            - 买卖时机选择

        计算原理：
            ROC = (当日收盘价 - N日前收盘价) / N日前收盘价 × 100
            ROCMA = ROC的M周期简单移动平均
            反映价格在N周期内的百分比变化率

        参数：
            n: 计算周期，默认24
            m: 移动平均周期，默认20
            **kwargs: 额外参数

        注意：
            - ROC上穿零轴为买入信号，下穿零轴为卖出信号
            - ROC极值往往预示趋势转折
            - 与价格背离时信号更可靠
            - 适合各类时间周期的动量分析

        返回值：
            IndFrame: 包含"roc_"(变动速率)、"rocma"(变动速率均线)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # ROC动量分析
            roc = tqta.ROC(n=24, m=20)
            positive_momentum = roc.roc > 0                # 正动量
            momentum_cross = tqta.crossup(roc.roc, roc.rocma)  # 动量金叉
            # ROC超买超卖
            overbought = roc.roc > roc.roc.tqfunc.hhv(length=20) * 0.8
            oversold = roc.roc < roc.roc.tqfunc.llv(length=20) * 0.8
            # ROC背离分析
            price_new_high = close == close.tqfunc.hhv(length=20)
            roc_lower_high = roc.roc < roc.roc.tqfunc.ref(length=1)
            bearish_divergence = price_new_high & roc_lower_high  # 顶背离
        """
        # c,roc,rocma
        ...

    @tobtind(lines=["k", "d"], lib="tqta")
    def SLOWKD(self, n=9, m1=3, m2=3, m3=3, **kwargs) -> IndFrame:
        """
        ## 慢速随机指标 - KDJ指标的平滑版本，减少信号噪音

        功能：
            通过多次平滑处理减少KD指标的波动，提供更稳定的买卖信号

        应用场景：
            - 超买超卖判断
            - 趋势转换确认
            - 买卖信号过滤
            - 中长线交易时机

        计算原理：
            在标准KD计算基础上进行多次平滑处理
            经过m1、m2、m3三次平滑得到最终的K、D值
            信号更稳定但响应更慢

        参数：
            n: RSV计算周期，默认9
            m1: 第一次平滑周期，默认3
            m2: 第二次平滑周期，默认3
            m3: 第三次平滑周期，默认3
            **kwargs: 额外参数

        注意：
            - K、D值在80以上为超买区，20以下为超卖区
            - 信号比标准KD更稳定但滞后
            - 适合中长线趋势跟踪
            - 减少震荡市中的假信号

        返回值：
            IndFrame: 包含"k"(慢速K值)、"d"(慢速D值)两列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # SLOWKD超买超卖判断
            slowkd = self.kline.tqta.SLOWKD(n=9, m1=3, m2=3, m3=3)
            overbought = (slowkd.k > 80) & (slowkd.d > 80)  # 超买
            oversold = (slowkd.k < 20) & (slowkd.d < 20)    # 超卖
            # SLOWKD交易信号
            golden_cross = slowkd.k.tqfunc.crossup(slowkd.d)   # 金叉
            death_cross = slowkd.k.tqfunc.crossdown(slowkd.d)  # 死叉
            # 趋势区域判断
            bull_zone = (slowkd.k > 50) & (slowkd.d > 50)   # 多头区域
            bear_zone = (slowkd.k < 50) & (slowkd.d < 50)   # 空头区域
        """
        # hlc,k,d
        ...

    @tobtind(lines=["srdm", "asrdm"], lib="tqta")
    def SRDM(self, n=30, **kwargs) -> IndFrame:
        """
        ## 动向速度比率 - 价格变动速度和方向综合指标

        功能：
            综合分析价格变动速度和方向，识别趋势强度和持续性

        应用场景：
            - 趋势速度分析
            - 买卖力量对比
            - 趋势持续性判断
            - 动量强度量化

        计算原理：
            基于价格变动计算动向速度比率
            SRDM: 原始动向速度值
            ASRDM: SRDM的加权移动平均
            反映价格变动的速度和方向特征

        参数：
            n: 计算周期，默认30
            **kwargs: 额外参数

        注意：
            - SRDM值反映变动速度，ASRDM反映速度趋势
            - 两者同向为趋势确认
            - 数值大小反映变动强度
            - 适合趋势跟踪和动量策略

        返回值：
            IndFrame: 包含"srdm"(动向速度比率)、"asrdm"(加权平均)两列

        所需数据字段：
            `high`, `low`, `close`

        >>> # 示例：
            # SRDM趋势分析
            srdm = tqta.SRDM(n=30)
            fast_movement = srdm.srdm > srdm.srdm.tqfunc.ma(length=20)  # 快速变动
            trend_confirmed = (srdm.srdm > 0) & (srdm.asrdm > 0)     # 上升趋势确认
            # 速度变化分析
            accelerating = srdm.srdm > srdm.srdm.tqfunc.ref(length=1)   # 加速
            decelerating = srdm.srdm < srdm.srdm.tqfunc.ref(length=1)   # 减速
        """
        # hlc,srdm,asrdm
        ...

    @tobtind(lines=["a", "mi"], lib="tqta")
    def SRMI(self, n=9, **kwargs) -> IndFrame:
        """
        ## MI修正指标 - 动量指标的优化版本

        功能：
            对传统动量指标进行修正，提供更平滑和稳定的动量信号

        应用场景：
            - 动量趋势分析
            - 买卖时机选择
            - 趋势强度评估
            - 反转信号预警

        计算原理：
            在传统动量指标基础上进行修正和平滑处理
            A: 原始动量值
            MI: 修正后的动量指标
            减少噪音干扰，提高信号质量

        参数：
            n: 计算周期，默认9
            **kwargs: 额外参数

        注意：
            - MI上穿零轴为买入信号，下穿零轴为卖出信号
            - 比传统动量指标更平滑
            - 适合中短线趋势分析
            - 减少虚假信号

        返回值：
            IndFrame: 包含"a"(原始动量值)、"mi"(修正动量指标)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # SRMI动量分析
            srmi = tqta.SRMI(n=9)
            positive_momentum = srmi.mi > 0                 # 正动量
            momentum_turn = srmi.mi.tqfunc.crossup(0)          # 动量转正
            # 动量强度分级
            strong_momentum = srmi.mi > srmi.mi.tqfunc.ma(length=20)
            weak_momentum = srmi.mi < srmi.mi.tqfunc.ma(length=20)
            # 原始与修正对比
            signal_improvement = (srmi.mi - srmi.a).abs() < 0.1  # 信号改善
        """
        # c,a,mi
        ...

    @tobtind(lines=["b", "d"], lib="tqta")
    def ZDZB(self, n1=50, n2=5, n3=20, **kwargs) -> IndFrame:
        """
        ## 筑底指标 - 底部形成和反转识别工具

        功能：
            识别价格底部形成过程，预警趋势反转机会

        应用场景：
            - 底部形态识别
            - 趋势反转预警
            - 买入时机选择
            - 支撑位确认

        计算原理：
            基于价格相对位置计算筑底信号
            B: 短期筑底信号
            D: 长期筑底信号
            反映价格在底部区域的相对强度

        参数：
            n1: 基础计算周期，默认50
            n2: 短期信号周期，默认5
            n3: 长期信号周期，默认20
            **kwargs: 额外参数

        注意：
            - B上穿D为底部确认信号
            - 指标值上升表示筑底过程
            - 适合抄底和反转交易
            - 需结合价格形态确认

        返回值：
            IndFrame: 包含"b"(短期筑底信号)、"d"(长期筑底信号)两列

        所需数据字段：
            `close`

        >>> # 示例：
            # ZDZB底部信号
            zdzb = self.kline.close.tqta.ZDZB(n1=50, n2=5, n3=20)
            bottom_formation = zdzb.b > zdzb.b.tqfunc.ref(length=1)  # 筑底进行中
            bottom_confirmed = zdzb.b.tqfunc.crossup(zdzb.d)         # 底部确认
            # 筑底强度分析
            strong_bottom = (zdzb.b > 1) & (zdzb.d > 1)           # 强势筑底
            weak_bottom = (zdzb.b < 1) & (zdzb.d < 1)             # 弱势筑底
        """
        # c,b,d
        ...

    @tobtind(lib="tqta")
    def DPO(self, **kwargs) -> IndSeries:
        """
        ## 区间震荡线 - 价格与移动平均线的周期性偏离分析

        功能：
            消除长期趋势影响，专注于中短期价格波动分析

        应用场景：
            - 区间震荡识别
            - 买卖时机选择
            - 周期波动分析
            - 趋势过滤

        计算原理：
            DPO = 收盘价 - (N/2+1)日前移动平均价
            通过减去移动平均消除长期趋势，突出周期性波动

        参数：
            **kwargs: 额外参数

        注意：
            - DPO上穿零轴为买入信号
            - DPO下穿零轴为卖出信号
            - 适合震荡市交易
            - 在趋势市中效果有限

        返回值：
            IndSeries: DPO值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # DPO震荡分析
            dpo = self.kline.close.tqta.DPO()
            in_oscillation = dpo.abs() < dpo.tqfunc.std(length=20)          # 震荡区间
            breakout_signal = dpo > dpo.tqfunc.hhv(length=20)              # 突破信号
            # DPO交易信号
            buy_oscillation = (dpo.tqfunc.ref(length=1) < 0) & (dpo > 0)   # 震荡买入
            sell_oscillation = (dpo.tqfunc.ref(length=1) > 0) & (dpo < 0)  # 震荡卖出
        """
        # c
        ...

    @tobtind(lines=["lon", "ma1"], lib="tqta")
    def LON(self, **kwargs) -> IndFrame:
        """
        ## 长线指标 - 综合量价关系的长线趋势分析系统

        功能：
            结合价格和成交量分析长线趋势方向和强度

        应用场景：
            - 长线趋势判断
            - 资金流向分析
            - 趋势持续性评估
            - 长线买卖时机

        计算原理：
            基于价格和成交量计算长线趋势指标
            LON: 长线指标值
            MA1: LON的10周期移动平均
            反映长线资金流向和趋势强度

        参数：
            **kwargs: 额外参数

        注意：
            - LON上穿MA1为长线买入信号
            - LON下穿MA1为长线卖出信号
            - 适合长线投资和趋势跟踪
            - 信号稳定但响应较慢

        返回值：
            IndFrame: 包含"lon"(长线指标)、"ma1"(指标均线)两列

        所需数据字段：
            `high`, `low`, `close`, `volume`

        >>> # 示例：
            # LON长线趋势
            lon = self.kline.tqta.LON()
            long_term_bull = lon.lon > lon.ma1                    # 长线多头
            golden_cross_long = lon.lon.tqfunc.crossup(lon.ma1)      # 长线金叉
            # 长线趋势强度
            strong_uptrend = (lon.lon > 0) & (lon.ma1 > 0)        # 强势上涨
            trend_strength = (lon.lon - lon.ma1) / lon.ma1.abs()
        """
        # hlcv,lon,ma1
        ...

    @tobtind(lines=["short", "ma1"], lib="tqta")
    def SHORT(self, **kwargs) -> IndFrame:
        """
        ## 短线指标 - 综合量价关系的短线交易系统

        功能：
            结合价格和成交量分析短线交易机会和买卖时机

        应用场景：
            - 短线交易信号
            - 日内买卖时机
            - 资金短期流向
            - 短线趋势判断

        计算原理：
            基于价格和成交量计算短线交易指标
            SHORT: 短线指标值
            MA1: SHORT的10周期移动平均
            反映短线资金流向和交易机会

        参数：
            **kwargs: 额外参数

        注意：
            - SHORT上穿MA1为短线买入信号
            - SHORT下穿MA1为短线卖出信号
            - 适合短线交易和日内操作
            - 信号敏感但可能有噪音

        返回值：
            IndFrame: 包含"short"(短线指标)、"ma1"(指标均线)两列

        所需数据字段：
            `high`, `low`, `close`, `volume`

        >>> 示例：
            # SHORT短线交易
            short = self.kline.tqta.SHORT()
            short_term_bull = short.short > short.ma1             # 短线多头
            golden_cross_short = short.short.tqfunc.crossup(short.ma1) # 短线金叉
            # 短线交易信号过滤
            strong_signal = (short.short - short.ma1).abs() > short.short.tqfunc.std(length=20)
            weak_signal = (short.short - short.ma1).abs() < short.short.tqfunc.std(length=20)
        """
        # hlcv,short,ma1
        ...

    @tobtind(lines=["mv1", "mv2"], lib="tqta")
    def MV(self, n=10, m=20, **kwargs) -> IndFrame:
        """
        ## 均量线指标 - 成交量移动平均分析系统

        功能：
            通过成交量的移动平均分析资金流向和活跃度变化

        应用场景：
            - 成交量趋势分析
            - 资金活跃度判断
            - 量价关系验证
            - 突破确认

        计算原理：
            MV1 = 成交量的N周期简单移动平均
            MV2 = 成交量的M周期简单移动平均
            反映不同周期下的平均成交量水平

        参数：
            n: 短期均量周期，默认10
            m: 长期均量周期，默认20
            **kwargs: 额外参数

        注意：
            - MV1上穿MV2为量能金叉
            - MV1下穿MV2为量能死叉
            - 量价配合时信号更可靠
            - 适合各类时间周期的量能分析

        返回值：
            IndFrame: 包含"mv1"(短期均量)、"mv2"(长期均量)两列

        所需数据字段：
            `volume`

        >>> # 示例：
            # MV量能分析
            mv = tqta.MV(n=10, m=20)
            volume_increasing = mv.mv1 > mv.mv2                  # 量能增加
            golden_cross_volume = mv.mv1.tqfunc.crossup(mv.mv2)      # 量能金叉
            # 量价配合分析
            price_up_volume_up = (close > close.tqfunc.ref(length=1)) & (mv.mv1 > mv.mv1.tqfunc.ref(length=1))
            price_up_volume_down = (close > close.tqfunc.ref(length=1)) & (mv.mv1 < mv.mv1.tqfunc.ref(length=1))
        """
        # v,mv1,mv2
        ...

    @tobtind(lines=["a", "b", "e"], lib="tqta")
    def WAD(self, n=10, m=30, **kwargs) -> IndFrame:
        """
        ## 威廉多空力度线 - 威廉指标与量价结合的多空力量分析

        功能：
            结合价格位置和量价关系分析多空双方力量对比

        应用场景：
            - 多空力量对比分析
            - 趋势方向确认
            - 买卖信号生成
            - 量价关系验证

        计算原理：
            基于威廉指标和量价关系计算多空力度
            A/D: 原始多空力度值
            B: A/D的N周期加权移动平均
            E: A/D的M周期加权移动平均
            综合反映多空力量变化

        参数：
            n: 短期平滑周期，默认10
            m: 长期平滑周期，默认30
            **kwargs: 额外参数

        注意：
            - A/D值反映当日多空力度
            - B上穿E为多头信号
            - B下穿E为空头信号
            - 适合趋势确认和力量分析

        返回值：
            IndFrame: 包含"a"(多空力度)、"b"(短期均线)、"e"(长期均线)三列

        所需数据字段：
            `high`, `low`, `close`

        >>> 示例：
            # WAD多空分析
            wad = tqta.WAD(n=10, m=30)
            bull_power = wad.a > 0                          # 多头力量
            golden_cross_wad = wad.b.tqta.crossup(wad.e)  # 多空金叉
            death_cross_wad = wad.b.tqta.crossdown(wad.e) # 多空死叉

            # 多空力量强度
            strong_bull = (wad.b > wad.e) & (wad.a > wad.a.tqfunc.ma(length=10))
            strong_bear = (wad.b < wad.e) & (wad.a < wad.a.tqfunc.ma(length=10))
        """
        # hlc,a,b,e
        ...

    @tobtind(lib="tqta")
    def AD(self, **kwargs) -> IndSeries:
        """
        ## 累积/派发指标 - 资金流向和积累分布分析

        功能：
            通过价格和成交量关系分析资金累积和派发过程

        应用场景：
            - 资金流向分析
            - 趋势确认
            - 背离分析
            - 机构资金动向

        计算原理：
            AD = 前日AD + 当日资金流
            当日资金流 = [(收盘价-最低价)-(最高价-收盘价)] / (最高价-最低价) × 成交量
            反映资金的累积和派发过程

        参数：
            **kwargs: 额外参数

        注意：
            - AD上升表示资金累积，下降表示资金派发
            - 与价格背离时信号更可靠
            - 适合中长线资金流向分析
            - 反映机构资金动向

        返回值：
            IndSeries: AD值序列

        所需数据字段：
            `high`, `low`, `close`, `volume`

        >>> # 示例：
            # AD资金流向分析
            ad = self.kline.tqta.AD()
            accumulation = ad > ad.tqfunc.ref(length=1)           # 资金累积
            distribution = ad < ad.tqfunc.ref(length=1)           # 资金派发
            # AD与价格背离
            price_new_high = close == close.tqfunc.hhv(length=20)
            ad_lower_high = ad < ad.tqfunc.ref(length=1)
            bearish_divergence = price_new_high & ad_lower_high  # 顶背离
        """
        # hlcv
        ...

    @tobtind(lib="tqta")
    def CCL(self, close_oi=None, **kwargs) -> IndSeries:
        """
        ## 持仓异动指标 - 期货市场持仓变化分析

        功能：
            分析期货合约持仓量变化，识别主力资金动向

        应用场景：
            - 期货持仓分析
            - 主力资金动向
            - 多空力量判断
            - 趋势确认

        计算原理：
            基于持仓量变化计算持仓异动
            返回字符串标识：'多头增仓'、'多头减仓'、'空头增仓'、'空头减仓'等
            反映期货市场的资金流向

        参数：
            close_oi: 收盘持仓量数据
            **kwargs: 额外参数

        注意：
            - 需要持仓量数据支持
            - 反映期货市场特有信息
            - 适合期货品种分析
            - 结合价格走势更有效

        返回值：
            IndSeries: 持仓异动标识序列

        所需数据字段：
            `close` (需要持仓量数据配合)

        >>> # 示例：
            # CCL持仓分析
            ccl = self.kline.close.tqta.CCL()
            long_increase = ccl == '多头增仓'                   # 多头增仓
            short_increase = ccl == '空头增仓'                  # 空头增仓
            long_decrease = ccl == '多头减仓'                   # 多头减仓
            short_decrease = ccl == '空头减仓'                  # 空头减仓
        """
        # c
        ...

    @tobtind(lines=["vol", "opid"], lib="tqta")
    def __CJL(self, close_oi=None, **kwargs) -> IndFrame:
        """
        ## 成交持仓分析 - 期货市场成交和持仓量数据

        功能：
            提供期货市场的成交量和持仓量基础数据

        应用场景：
            - 成交量分析
            - 持仓量分析
            - 量价关系研究
            - 市场活跃度判断

        计算原理：
            VOL: 当日成交量
            OPID: 当日持仓量
            提供期货市场的基础成交持仓数据

        参数：
            close_oi: 收盘持仓量数据
            **kwargs: 额外参数

        注意：
            - 需要期货合约的成交持仓数据
            - 成交量反映市场活跃度
            - 持仓量反映资金沉淀
            - 适合期货市场分析

        返回值：
            IndFrame: 包含"vol"(成交量)、"opid"(持仓量)两列

        所需数据字段：
            `volume` (需要持仓量数据配合)

        >>> # 示例：
            # CJL量仓分析
            cjl = tqta.CJL()
            high_volume = cjl.vol > cjl.vol.tqfunc.ma(length=20)  # 高成交量
            oi_increase = cjl.opid > cjl.opid.tqfunc.ref(length=1) # 持仓增加
            # 量仓配合分析
            volume_oi_rise = (cjl.vol > cjl.vol.tqfunc.ref(length=1)) & (cjl.opid > cjl.opid.tqfunc.ref(length=1))
        """
        # v,vol,opid
        ...

    @tobtind(lib="tqta")
    def __OPI(self, close_oi=None, **kwargs) -> IndSeries:
        """移除
        ## 持仓量指标 - 期货市场未平仓合约数量

        功能：
            分析期货市场持仓量变化，反映资金沉淀和市场情绪

        应用场景：
            - 资金流向分析
            - 市场情绪判断
            - 趋势强度评估
            - 风险控制

        计算原理：
            OPI = 当日未平仓合约数量
            反映期货市场的资金沉淀和投资者持仓情况

        参数：
            close_oi: 收盘持仓量数据
            **kwargs: 额外参数

        注意：
            - 需要期货合约持仓量数据
            - 持仓量增加表示资金流入
            - 持仓量减少表示资金流出
            - 反映市场参与度

        返回值：
            IndSeries: 持仓量序列

        所需数据字段：
            (需要持仓量数据)

        >>> 示例：
            # OPI持仓分析
            opi = self.kline.volume.tqta.OPI()
            oi_uptrend = opi > opi.tqfunc.ma(length=10)           # 持仓上升趋势
            new_high_oi = opi == opi.tqfunc.hhv(length=20)        # 持仓创新高
            capital_inflow = opi > opi.tqfunc.ref(length=1)       # 资金流入
        """
        ...

    @tobtind(lib="tqta")
    def PVT(self, **kwargs) -> IndSeries:
        """
        ## 价量趋势指数 - 价格与成交量的协同变化分析

        功能：
            通过价格变化与成交量的乘积分析趋势强度

        应用场景：
            - 量价关系分析
            - 趋势确认
            - 买卖信号生成
            - 资金流向判断

        计算原理：
            PVT = 前日PVT + (当日收盘价-前日收盘价)/前日收盘价 × 当日成交量
            反映价格变动与成交量的协同变化

        参数：
            **kwargs: 额外参数

        注意：
            - PVT上升表示量价配合良好
            - 与价格背离时预警趋势转换
            - 适合趋势确认分析
            - 反映资金推动力度

        返回值：
            IndSeries: PVT值序列

        所需数据字段：
            `close`, `volume`

        >>> # 示例：
            # PVT量价分析
            pvt = self.kline.tqta.PVT()
            healthy_uptrend = (close > close.tqfunc.ref(length=1)) & (pvt > pvt.tqfunc.ref(length=1))
            weak_uptrend = (close > close.tqfunc.ref(length=1)) & (pvt < pvt.tqfunc.ref(length=1))
            # PVT趋势信号
            pvt_breakout = pvt > pvt.tqfunc.hhv(length=20)        # PVT突破
            pvt_breakdown = pvt < pvt.tqfunc.llv(length=20)       # PVT跌破
        """
        # cv
        ...

    @tobtind(lib="tqta")
    def VOSC(self, short=12, long=26, **kwargs) -> IndSeries:
        """
        ## 成交量振荡器 - 成交量移动平均离差分析

        功能：
            通过长短周期成交量均线离差分析量能变化

        应用场景：
            - 量能变化分析
            - 买卖信号确认
            - 突破有效性验证
            - 资金活跃度判断

        计算原理：
            VOSC = (短期成交量均线 - 长期成交量均线) / 长期成交量均线 × 100
            反映成交量能的变化幅度

        参数：
            short: 短期周期，默认12
            long: 长期周期，默认26
            **kwargs: 额外参数

        注意：
            - VOSC上穿零轴为量能金叉
            - VOSC下穿零轴为量能死叉
            - 数值大小反映量能变化强度
            - 适合量价配合分析

        返回值：
            IndSeries: VOSC值序列，单位为百分比

        所需数据字段：
            `volume`

        >>> # 示例：
            # VOSC量能分析
            vosc = self.kline.volume.tqta.VOSC(short=12, long=26)
            volume_increase = vosc > 0                         # 量能增加
            volume_surge = vosc > vosc.tqfunc.hhv(length=20)      # 量能激增
            # 量价配合
            price_volume_confirmation = (close > close.tqfunc.ref(length=1)) & (vosc > 0)
            price_volume_divergence = (close > close.tqfunc.ref(length=1)) & (vosc < 0)
        """
        # v
        ...

    @tobtind(lib="tqta")
    def VROC(self, n=12, **kwargs) -> IndSeries:
        """
        ## 成交量变动速率 - 成交量变化百分比分析

        功能：
            测量成交量变化的百分比速率，分析量能动量

        应用场景：
            - 量能动量分析
            - 突破确认
            - 资金流入流出判断
            - 市场活跃度变化

        计算原理：
            VROC = (当日成交量 - N日前成交量) / N日前成交量 × 100
            反映成交量在N周期内的百分比变化率

        参数：
            n: 计算周期，默认12
            **kwargs: 额外参数

        注意：
            - VROC上穿零轴为量能转强
            - VROC下穿零轴为量能转弱
            - 极值往往预示重大变化
            - 适合各类时间周期的量能分析

        返回值：
            IndSeries: VROC值序列，单位为百分比

        所需数据字段：
            `volume`

        >>> # 示例：
            # VROC量能动量
            vroc = self.kline.volume.tqta.VROC(n=12)
            volume_momentum_up = vroc > 0                      # 量能动量向上
            volume_surge_signal = vroc > vroc.tqfunc.hhv(length=20) * 0.8  # 量能激增
            # 量能转换信号
            volume_turn_positive = (ta.ref(vroc, length=1) <= 0) & (vroc > 0)
            volume_turn_negative = (ta.ref(vroc, length=1) >= 0) & (vroc < 0)
        """
        # v
        ...

    @tobtind(lib="tqta")
    def VRSI(self, n=6, **kwargs) -> IndSeries:
        """
        ## 成交量相对强弱指标 - 成交量动量的RSI版本

        功能：
            通过成交量变化分析量能动量的强弱状态

        应用场景：
            - 量能动量分析
            - 超买超卖判断
            - 背离分析
            - 量价关系验证

        计算原理：
            计算方式类似RSI，但基于成交量数据
            VRSI = 100 - 100 / (1 + RS)
            RS = N日内成交量上涨平均值 / N日内成交量下跌平均值

        参数：
            n: 计算周期，默认6
            **kwargs: 额外参数

        注意：
            - VRSI在70以上为量能超买
            - VRSI在30以下为量能超卖
            - 与价格RSI结合使用效果更好
            - 适合量能动量分析

        返回值：
            IndSeries: VRSI值序列，范围0-100

        所需数据字段：
            `volume`

        >>> # 示例：
            # VRSI量能分析
            vrsi = self.kline.volume.tqta.VRSI(n=6)
            volume_overbought = vrsi > 70                     # 量能超买
            volume_oversold = vrsi < 30                       # 量能超卖
            # 量价RSI配合
            price_rsi = self.kline.close.tqta.RSI(n=6)
            healthy_volume = (price_rsi > 50) & (vrsi > 50)   # 健康量价
            weak_volume = (price_rsi > 50) & (vrsi < 50)      # 量价背离
        """
        # v
        ...

    @tobtind(lib="tqta")
    def WVAD(self, **kwargs) -> IndSeries:
        """
        ## 威廉变异离散量 - 威廉指标与成交量结合的分析工具

        功能：
            结合威廉指标和成交量分析资金流向和市场强度

        应用场景：
            - 资金流向分析
            - 市场强度判断
            - 买卖信号生成
            - 趋势确认

        计算原理：
            基于开盘、最高、最低、收盘价和成交量计算
            WVAD = (收盘价-开盘价) / (最高价-最低价) × 成交量
            综合反映价格位置和成交量信息

        参数：
            **kwargs: 额外参数

        注意：
            - WVAD为正表示资金流入
            - WVAD为负表示资金流出
            - 数值大小反映资金流向强度
            - 适合短线资金流向分析

        返回值：
            IndSeries: WVAD值序列

        所需数据字段：
            `open`, `high`, `low`, `close`, `volume`

        >>> # 示例：
            # WVAD资金流向
            wvad = self.kline.tqta.WVAD()
            capital_inflow = wvad > 0                         # 资金流入
            capital_outflow = wvad < 0                        # 资金流出
            # 资金流向强度
            strong_inflow = wvad > wvad.tqfunc.ma(length=20)     # 强势流入
            strong_outflow = wvad < wvad.tqfunc.ma(length=20)    # 强势流出
            # WVAD突破信号
            wvad_breakout = wvad > wvad.tqfunc.hhv(length=20)    # 资金流入突破
        """
        # ohlcv
        ...

    @tobtind(overlap=True, lib="tqta")
    def MA(self, n=30, **kwargs) -> IndSeries:
        """
        ## 简单移动平均线 - 最基础的价格趋势平滑工具

        功能：
            计算指定周期内收盘价的算术平均值，消除短期波动，显示长期趋势

        应用场景：
            - 趋势方向识别
            - 支撑阻力位构建
            - 均线交叉策略
            - 价格与均线关系分析

        计算原理：
            MA = (P₁ + P₂ + ... + Pₙ) / n
            其中P为收盘价，n为计算周期
            所有历史数据权重相等

        参数：
            n: 移动平均周期，默认30
            **kwargs: 额外参数

        注意：
            - 对价格变化的反应相对滞后
            - 周期越长，平滑效果越明显但滞后性越大
            - 适合趋势明显的市场环境
            - 常作为其他技术指标的基础

        返回值：
            IndSeries: 简单移动平均值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # MA趋势分析
            ma = self.kline.close.tqta.MA(n=30)
            price_above_ma = close > ma                        # 价格在均线上方
            price_below_ma = close < ma                        # 价格在均线下方
            ma_uptrend = ma > ma.tqfunc.ref(length=1)             # 均线上升趋势
            # 多周期MA系统
            ma_short = self.kline.close.tqta.MA(n=10)
            ma_long = self.kline.close.tqta.MA(n=30)
            golden_cross = ma_short.tqfunc.crossup(ma_long)       # 金叉信号
            death_cross = ma_short.tqfunc.crossdown(ma_long)      # 死叉信号
        """
        # c
        ...

    @tobtind(overlap=True, lib="tqta")
    def SMA(self, n=5, m=2, **kwargs) -> IndSeries:
        """
        ## 扩展指数加权移动平均 - 可调节权重的平滑移动平均

        功能：
            提供可自定义权重的指数加权移动平均，平衡近期和远期数据的重要性

        应用场景：
            - 自定义平滑程度的需求
            - 特定权重模式的趋势分析
            - 交易系统优化
            - 技术指标定制

        计算原理：
            SMA = (Pₙ × m + 前日SMA × (n - m)) / n
            其中m为权重系数，n为周期
            允许调节近期数据的权重比例

        参数：
            n: 计算周期，默认5
            m: 权重系数，默认2
            **kwargs: 额外参数

        注意：
            - m值越大，近期数据权重越高
            - 当m=1时退化为简单移动平均
            - 适合需要定制化平滑程度的场景
            - 平衡响应速度和平滑效果

        返回值：
            IndSeries: 扩展指数加权移动平均值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # SMA定制化分析
            sma_fast = self.kline.close.tqta.SMA(n=5, m=3)                       # 快速SMA
            sma_slow = self.kline.close.tqta.SMA(n=10, m=2)                      # 慢速SMA
            custom_signal = sma_fast.tqfunc.crossup(sma_slow)    # 定制信号
            # 权重优化
            high_weight = self.kline.close.tqta.SMA(n=5, m=4)                    # 高权重近期数据
            low_weight = self.kline.close.tqta.SMA(n=5, m=1)                     # 低权重近期数据
            weight_effect = high_weight - low_weight          # 权重影响
        """
        # c
        ...

    @tobtind(overlap=True, lib="tqta")
    def EMA(self, n=10, **kwargs) -> IndSeries:
        """
        ## 指数加权移动平均 - 对近期价格赋予更高权重的移动平均

        功能：
            通过指数加权方式强调近期价格，减少滞后性，更快反应趋势变化

        应用场景：
            - 快速趋势识别
            - 短线交易信号
            - 动态支撑阻力
            - 与其他指标配合使用

        计算原理：
            EMA = α × 当日收盘价 + (1 - α) × 前日EMA
            α = 2 / (n + 1)
            近期价格权重较高，远期价格权重指数衰减

        参数：
            n: 计算周期，默认10
            **kwargs: 额外参数

        注意：
            - 对价格变化比SMA更敏感
            - 在震荡市中可能产生较多假信号
            - 适合趋势跟踪策略
            - 常作为MACD等指标的计算基础

        返回值：
            IndSeries: 指数加权移动平均值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # EMA趋势系统
            ema_fast = self.kline.close.tqta.EMA(n=12)                           # 快速EMA
            ema_slow = self.kline.close.tqta.EMA(n=26)                           # 慢速EMA
            ema_golden_cross = ema_fast.tqfunc.crossup(ema_slow) # EMA金叉
            ema_death_cross = ema_fast.tqfunc.crossdown(ema_slow) # EMA死叉
            # EMA支撑阻力
            dynamic_support = self.kline.close.tqta.EMA(n=20)                    # 动态支撑
            dynamic_resistance = self.kline.tqta.EMA(n=50)                 # 动态阻力
            support_test = close <= dynamic_support           # 测试支撑
        """
        # c
        ...

    @tobtind(overlap=True, lib="tqta")
    def EMA2(self, n=10, **kwargs) -> IndSeries:
        """
        ## 线性加权移动平均 - 按时间线性加权的移动平均

        功能：
            采用线性递减权重，近期数据权重线性增加，平衡响应和平滑效果

        应用场景：
            - 平衡型的趋势分析
            - 需要线性权重的交易系统
            - 技术指标优化
            - 多时间框架分析

        计算原理：
            WMA = (P₁ × 1 + P₂ × 2 + ... + Pₙ × n) / (1 + 2 + ... + n)
            权重随时间线性递增，近期数据权重更高

        参数：
            n: 计算周期，默认10
            **kwargs: 额外参数

        注意：
            - 权重线性递增，比EMA更平缓
            - 滞后性介于SMA和EMA之间
            - 适合需要平衡响应的场景
            - 在趋势转换时表现稳定

        返回值：
            IndSeries: 线性加权移动平均值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # WMA多周期分析
            wma_short = self.kline.close.tqta.EMA2(n=10)                         # 短期WMA
            wma_long = self.kline.close.tqta.EMA2(n=30)                          # 长期WMA
            wma_cross = wma_short.tqfunc.crossup(wma_long)       # WMA金叉
            # 不同MA类型比较
            sma_20 = self.kline.close.tqta.MA(n=20)
            ema_20 = self.kline.close.tqta.EMA(n=20)
            wma_20 = self.kline.close.tqta.EMA2(n=20)
            ma_comparison = (wma_20 - sma_20) / sma_20        # MA差异比较
        """
        # c
        ...

    @tobtind(overlap=True, lib="tqta")
    def TRMA(self, n=10, **kwargs) -> IndSeries:
        """
        ## 三角移动平均线 - 双重平滑的移动平均变体

        功能：
            通过两次平均计算提供更平滑的趋势线，减少噪音干扰

        应用场景：
            - 极平滑趋势识别
            - 长期投资分析
            - 过滤市场噪音
            - 重大趋势确认

        计算原理：
            先计算n周期简单移动平均
            再对SMA结果进行n/2周期简单移动平均
            实现双重平滑效果

        参数：
            n: 计算周期，默认10
            **kwargs: 额外参数

        注意：
            - 滞后性明显大于其他移动平均
            - 适合长期趋势分析
            - 几乎完全过滤短期波动
            - 在趋势明显的市场中效果最佳

        返回值：
            IndSeries: 三角移动平均值序列

        所需数据字段：
            `close`

        >>> # 示例：
            # TRMA长期趋势
            trma = self.kline.close.tqta.TRMA(n=20)                              # 三角移动平均
            long_term_trend = trma > trma.tqfunc.ref(length=5)   # 长期趋势向上
            major_turn = (trma.tqfunc.ref(length=2) < trma.tqfunc.ref(length=1)) & (trma < trma.tqfunc.ref(length=1))
            # TRMA与其他MA对比
            trma_smooth = self.kline.close.tqta.TRMA(n=20)
            sma_smooth = self.kline.close.tqta.MA(n=20)
            smoothness_advantage = (trma_smooth - trma_smooth.tqfunc.ref(1)).abs() < abs(sma_smooth - sma_smooth.tqfunc.ref(1))
        """
        # c
        ...
