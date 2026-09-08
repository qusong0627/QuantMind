from __future__ import annotations
from .base import tobtind
from ..utils import TYPE_CHECKING



if TYPE_CHECKING:
    from typing_ import *
    from .core import *



class FinTa:
    """finta指标指引"""
    _perfixes: str = "finta_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @tobtind(overlap=True, lib='finta')
    def SMA(self, period: int = 14, **kwargs) -> IndSeries:
        """简单移动平均线 -  pandas中的滚动平均值，又称MA
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 简单移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def SMM(self, period: int = 9, **kwargs) -> IndSeries:
        """简单移动中位数 - 移动平均线的替代指标，对异常值更稳健
        ---
        Args:
            period: 周期，默认9
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 简单移动中位数序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def SSMA(self, period: int = 9, adjust: bool = True, **kwargs) -> IndSeries:
        """平滑简单移动平均线
        ---
        Args:
            period: 周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 平滑简单移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def EMA(self, period: int = 9, adjust: bool = True, **kwargs) -> IndSeries:
        """指数加权移动平均线 - 适用于趋势市场，常与其他指标结合确认走势
        ---
        Args:
            period: 周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 指数加权移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def DEMA(self, period: int = 9, adjust: bool = True, **kwargs) -> IndSeries:
        """双指数移动平均线 - 通过对EMA二次处理减少滞后
        ---
        Args:
            period: 周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 双指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def TEMA(self, period: int = 9, adjust: bool = True, **kwargs) -> IndSeries:
        """三重指数移动平均线 - 通过对EMA三次处理减少滞后
        ---
        Args:
            period: 周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三重指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def TRIMA(self, period: int = 18, **kwargs) -> IndSeries:
        """三角形移动平均线 - 对周期中间价格赋予更高权重
        ---
        Args:
            period: 周期，默认18
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三角形移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def TRIX(self, period: int = 20, adjust: bool = True, **kwargs) -> IndSeries:
        """三重指数移动平均变化率 - 围绕零轴波动，交叉零轴产生买卖信号
        ---
        Args:
            period: 周期，默认20
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 三重指数移动平均变化率序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def __LWMA(self, period: int = 10, **kwargs) -> IndSeries:
        """原函数直接返回raise,移除
        线性加权移动平均线
        ---
        Args:
            period: 周期
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 线性加权移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def VAMA(self, period: int = 8, **kwargs) -> IndSeries:
        """成交量调整移动平均线
        ---
        Args:
            period: 周期，默认8
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries: 成交量调整移动平均线序列
        """
        ...

    @tobtind(lib='finta')
    def __VIDYA(self, period: int = 9, smoothing_period: int = 12, **kwargs) -> IndSeries:
        """原函数直接返回raise，移除
        可变指数动态平均线 - EMA的改进版，平滑因子随价格波动变化
        ---
        Args:
            period: 周期，默认9
            smoothing_period: 平滑周期，默认12
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 可变指数动态平均线序列
        """
        ...

    @tobtind(lib='finta')
    def ER(self, period: int = 10, **kwargs) -> IndSeries:
        """考夫曼效率指标 - 震荡于+100至-100之间，指示趋势方向
        ---
        Args:
            period: 周期，默认10
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 考夫曼效率指标序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def KAMA(self, er: int = 10, ema_fast: int = 2, ema_slow: int = 30, period: int = 20, **kwargs) -> IndSeries:
        """考夫曼自适应移动平均线 - 结合方向与波动率，适应市场变化
        ---
        Args:
            er: 效率周期，默认10
            ema_fast: 快速EMA周期，默认2
            ema_slow: 慢速EMA周期，默认30
            period: 周期，默认20
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 考夫曼自适应移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def ZLEMA(self, period: int = 26, adjust: bool = True, **kwargs) -> IndSeries:
        """零滞后指数移动平均线 - 消除移动平均线固有的滞后性
        ---
        Args:
            period: 周期，默认26
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 零滞后指数移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def WMA(self, period: int = 9, **kwargs) -> IndSeries:
        """加权移动平均线 - 比EMA更注重近期数据，帮助识别趋势
        ---
        Args:
            period: 周期，默认9
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 加权移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def HMA(self, period: int = 16, **kwargs) -> IndSeries:
        """赫尔移动平均线 - 曲线更平滑，滞后性低，适用于中长期交易
        ---
        Args:
            period: 周期，默认16
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 赫尔移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def EVWMA(self, period: int = 20, **kwargs) -> IndSeries:
        """弹性成交量加权移动平均线 - 近似近n期每股平均成交价
        ---
        Args:
            period: 周期，默认20
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries: 弹性成交量加权移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def VWAP(self, **kwargs) -> IndSeries:
        """成交量加权平均价格 - 交易基准指标，计算当日总成交额/总成交量
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries: 成交量加权平均价格序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def SMMA(self, period: int = 42, adjust: bool = True, **kwargs) -> IndSeries:
        """平滑移动平均线 - 近期价格与历史价格权重相等
        ---
        Args:
            period: 周期，默认42
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 平滑移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def __ALMA(self, period: int = 9, sigma: int = 6, offset: int = 0.85, **kwargs) -> IndSeries:
        """原函数直接返回raise，移除
        阿尔诺·勒古克斯移动平均线
        ---
        Args:
            period: 周期，默认9
            sigma: 标准差，默认6
            offset: 偏移量，默认0.85
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 阿尔诺·勒古克斯移动平均线序列
        """

        """dataWindow = _.last(data, period)
        size = _.size(dataWindow)
        m = offset * (size - 1)
        s = size / sigma
        sum = 0
        norm = 0
        for i in [size-1..0] by -1
        coeff = Math.exp(-1 * (i - m) * (i - m) / 2 * s * s)
        sum = sum + dataWindow[i] * coeff
        norm = norm + coeff
        return sum / norm"""
        ...

    @tobtind(overlap=True, lib='finta')
    def __MAMA(self, period: int = 16, **kwargs) -> IndSeries:
        """原函数直接返回raise，移除
        MESA自适应移动平均线
        ---
        Args:
            period: 周期，默认16
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: MESA自适应移动平均线序列
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def FRAMA(self, period: int = 16, batch: int = 10, **kwargs) -> IndSeries:
        """分形自适应移动平均线
        ---
        Args:
            period: 周期，默认16
            batch: 批次大小，默认10
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 分形自适应移动平均线序列
        """
        ...

    @tobtind(lines=["macd", "macdh"], lib='finta')
    def MACD(self, period_fast: int = 12, period_slow: int = 26, signal: int = 9, adjust: bool = True, **kwargs) -> IndFrame:
        """指数平滑异同移动平均线 - 包含MACD线、信号线和MACD差
        ---
        Args:
            period_fast: 快速周期，默认12
            period_slow: 慢速周期，默认26
            signal: 信号周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndFrame:macd,macdh
        """
        ...

    @tobtind(lines=["ppo", "ppos", "ppos"], lib='finta')
    def __PPO(self, period_fast: int = 12, period_slow: int = 26, signal: int = 9, adjust: bool = True, **kwargs) -> IndFrame:
        """有BUG，移除
        价格百分比振荡器 - 类似MACD，以相对值表示移动平均差
        ---
        Args:
            period_fast: 快速周期，默认12
            period_slow: 慢速周期，默认26
            signal: 信号周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndFrame:ppo,ppos,ppos
        """
        ...

    @tobtind(lines=["macd", "macdh"], lib='finta')
    def VW_MACD(self, period_fast: int = 12, period_slow: int = 26, signal: int = 9, adjust: bool = True, **kwargs) -> IndFrame:
        """成交量加权MACD - 基于成交量加权移动平均计算的MACD
        ---
        Args:
            period_fast: 快速周期，默认12
            period_slow: 慢速周期，默认26
            signal: 信号周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:macd,macdh
        """
        ...

    @tobtind(lines=["macd", "macdh"], lib='finta')
    def EV_MACD(self, period_fast: int = 20, period_slow: int = 40, signal: int = 9, adjust: bool = True, **kwargs) -> IndFrame:
        """弹性成交量加权MACD - 基于EVWMA计算的MACD变体
        ---
        Args:
            period_fast: 快速周期，默认20
            period_slow: 慢速周期，默认40
            signal: 信号周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:macd,macdh
        """
        ...

    @tobtind(lib='finta')
    def MOM(self, period: int = 10, **kwargs) -> IndSeries:
        """动量指标 - 固定时间间隔的价格差，围绕零轴波动
        ---
        Args:
            period: 周期，默认10
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 动量指标序列
        """
        ...

    @tobtind(lib='finta')
    def ROC(self, period: int = 12, **kwargs) -> IndSeries:
        """变化率指标 - 衡量价格较n期前的百分比变化
        ---
        Args:
            period: 周期，默认12
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 变化率指标序列
        """
        ...

    @tobtind(lib='finta')
    def VBM(self, roc_period: int = 12, atr_period: int = 26, **kwargs) -> IndSeries:
        """波动率基差动量 - 类似ROC，但除以ATR波动率
        ---
        Args:
            roc_period: ROC周期，默认12
            atr_period: ATR周期，默认26
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 波动率基差动量序列
        """
        ...

    @tobtind(lib='finta')
    def RSI(self, period: int = 14, adjust: bool = True, **kwargs) -> IndSeries:
        """相对强弱指数 - 震荡于0-100之间，70以上超买，30以下超卖
        ---
        Args:
            period: 周期，默认14
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 相对强弱指数序列
        """
        ...

    @tobtind(lib='finta')
    def IFT_RSI(self, rsi_period: int = 5, wma_period: int = 9, **kwargs) -> IndSeries:
        """改进型逆Fisher变换RSI - 交叉±0.5产生交易信号
        ---
        Args:
            rsi_period: RSI周期，默认5
            wma_period: WMA周期，默认9
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 改进型逆Fisher变换RSI序列
        """
        ...

    @tobtind(lib='finta')
    def __SWI(self, period: int = 16, **kwargs) -> IndSeries:
        """原函数直接返回raise，移除
        正弦波指标
        ---
        Args:
            period: 周期，默认16
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 正弦波指标序列
        """
        ...

    @tobtind(lib='finta')
    def DYMI(self, adjust: bool = True, **kwargs) -> IndSeries:
        """动态动量指数 - 可变周期RSI，3-30间波动，更早产生信号
        ---
        Args:
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries: 动态动量指数序列
        """

    @tobtind(lib='finta')
    def TR(self, **kwargs) -> IndSeries:
        """真实波幅 - 取三个价格范围的最大值：当期最高价减当期最低价、当期最高价减前收盘价的绝对值、当期最低价减前收盘价的绝对值
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def ATR(self, period: int = 14, **kwargs) -> IndSeries:
        """平均真实波幅 - 真实波幅的移动平均值
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def SAR(self, af: int = 0.02, amax: int = 0.2, **kwargs) -> IndSeries:
        """停损反转指标 - 随趋势延伸跟踪价格，上涨时在价格下方，下跌时在价格上方，价格突破指标时触发反转信号
        ---
        Args:
            af: 加速因子，默认0.02
            amax: 最大加速因子，默认0.2
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["psar", "psarbull", "psarbear"], overlap=True, lib='finta')
    def PSAR(self, iaf: int = 0.02, maxaf: int = 0.2, **kwargs) -> IndFrame:
        """抛物线停损反转指标 - 用于判断趋势方向和潜在反转，通过跟踪止损点确定买卖点
        ---
        Args:
            iaf: 初始加速因子，默认0.02
            maxaf: 最大加速因子，默认0.2
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:psar,psarbull,psarbear
        """
        ...

    @tobtind(lines=["upper_bb", "middle_band", "lower_bb"], overlap=True, lib='finta')
    def BBANDS(self, period: int = 20, MA: IndSeries = None, std_multiplier: float = 2, **kwargs) -> IndFrame:
        """布林带 - 移动平均线上下的波动率带，波动率基于标准差，波动率增大时带宽扩大，减小时缩小，支持传入自定义移动平均线
        ---
        Args:
            period: 周期，默认20
            MA: 移动平均线序列，默认None
            std_multiplier: 标准差倍数，默认2
        NOTE:
            实例包括列：close
        Returns:
            IndFrame:upper_bb,middle_band,lower_bb
        """
        ...

    @tobtind(lines=["upper_bb", "middle_band", "lower_bb"], lib='finta')
    def MOBO(self, period: int = 10, std_multiplier: float = 0.8, **kwargs) -> IndFrame:
        """MOBO带 - 基于10期0.8倍标准差的波动带，价格突破带时可能预示趋势或价格波动，42%的价格波动（噪音）位于带内
        ---
        Args:
            period: 周期，默认10
            std_multiplier: 标准差倍数，默认0.8
        NOTE:
            实例包括列：close
        Returns:
            IndFrame:upper_bb,middle_band,lower_bb
        """
        ...

    @tobtind(overlap=True, lib='finta')
    def BBWIDTH(self, period: int = 20, MA: IndSeries = None, **kwargs) -> IndSeries:
        """布林带带宽 - 标准化表示布林带的宽度
        ---
        Args:
            period: 周期，默认20
            MA: 移动平均线序列，默认None
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def PERCENT_B(self, period: int = 20, MA: IndSeries = None, **kwargs) -> IndSeries:
        """%b指标 - 基于随机指标公式，显示价格在布林带中的位置，上轨为1，下轨为0
        ---
        Args:
            period: 周期，默认20
            MA: 移动平均线序列，默认None
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...
        import finta

    @tobtind(lines=["up", "down"], lib='finta')
    def KC(self, period: int = 20, atr_period: int = 10, MA: IndSeries = None, kc_mult: float = 2, **kwargs) -> IndFrame:
        """肯特纳通道 - 基于指数移动平均线的波动率通道，用平均真实波幅（ATR）确定带宽，通常为20期EMA上下各2倍ATR，用于识别趋势反转和超买超卖
        ---
        Args:
            period: 周期，默认20
            atr_period: ATR周期，默认10
            MA: 移动平均线序列，默认None
            kc_mult: ATR倍数，默认2
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:up,down
        """
        ...

    @tobtind(lines=["lower", "middle", "upper"], lib='finta')
    def DO(self, upper_period: int = 20, lower_period: int = 5, **kwargs) -> IndFrame:
        """唐奇安通道 - 绘制过去一段时间内的最高价和最低价
        ---
        Args:
            upper_period: 上轨周期，默认20
            lower_period: 下轨周期，默认5
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:lower,middle,upper
        """
        ...

    @tobtind(lines=["diplus", "diminus"], lib='finta')
    def DMI(self, period: int = 14, adjust: bool = True, **kwargs) -> IndFrame:
        """方向移动指数 - 评估价格方向和强度，帮助判断多空方向，对趋势交易策略尤其有用，能区分趋势强弱
        ---
        Args:
            period: 周期，默认14
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:diplus,diminus
        """
        ...

    @tobtind(lib='finta')
    def ADX(self, period: int = 14, adjust: bool = True, **kwargs) -> IndSeries:
        """平均趋向指数 - 仅表示趋势强度，不指示方向，20以下为弱趋势，40以上为强趋势，50以上为极强趋势
        ---
        Args:
            period: 周期，默认14
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["s1", "s2", "s3", "s4", "r1", "r2", "r3", "r4"], lib='finta')
    def PIVOT(self, **kwargs) -> IndFrame:
        """枢轴点 - 重要的支撑和阻力位，通过最高价、最低价和收盘价计算，通常使用前一周期数据计算当前周期枢轴点
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:s1,s2,s3,s4,r1,r2,r3,r4
        """
        ...

    @tobtind(lines=["s1", "s2", "s3", "s4", "r1", "r2", "r3", "r4"], lib='finta')
    def PIVOT_FIB(self, **kwargs) -> IndFrame:
        """斐波那契枢轴点 - 先计算经典枢轴点，再结合前一周期波动范围与斐波那契比例计算支撑和阻力位
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:s1,s2,s3,s4,r1,r2,r3,r4
        """
        ...

    @tobtind(lib='finta')
    def STOCH(self, period: int = 14, **kwargs) -> IndSeries:
        """随机振荡器%K - 动量指标，比较证券收盘价与一定时期内价格范围的关系，可通过调整周期或取移动平均降低灵敏度
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def STOCHD(self, period: int = 3, stoch_period: int = 14, **kwargs) -> IndSeries:
        """随机振荡器%D - %K的3期简单移动平均
        ---
        Args:
            period: %D周期，默认3
            stoch_period: %K周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["STOCHRSI"], lib='finta')
    def STOCHRSI(self, rsi_period: int = 14, stoch_period: int = 14, **kwargs) -> IndSeries:
        """随机相对强弱指数 - 将随机指标公式应用于RSI值，衡量RSI在一定时期高低范围内的水平，震荡于0-1之间
        ---
        Args:
            rsi_period: RSI周期，默认14
            stoch_period: 随机指标周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries:STOCHRSI
        """
        ...

    @tobtind(lib='finta')
    def WILLIAMS(self, period: int = 14, **kwargs) -> IndSeries:
        """威廉指标%R - 显示当前收盘价相对于过去N天高低区间的位置，负值刻度，-100为最低，0为最高
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def UO(self, **kwargs) -> IndSeries:
        """终极振荡器 - 跨三个时间框架捕捉动量，避免单一时间框架振荡器的缺陷
        ---
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def AO(self, slow_period: int = 34, fast_period: int = 5, **kwargs) -> IndSeries:
        """真棒振荡器 - 衡量市场动量，计算34期与5期简单移动平均的差值（基于K线中点而非收盘价），用于确认趋势或预测反转
        ---
        Args:
            slow_period: 慢速周期，默认34
            fast_period: 快速周期，默认5
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def MI(self, period: int = 9, adjust: bool = True, **kwargs) -> IndSeries:
        """质量指数 - 基于高低范围识别趋势反转，无方向偏差的波动率指标，通过范围扩张预示当前趋势反转
        ---
        Args:
            period: 周期，默认9
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def BOP(self, **kwargs) -> IndSeries:
        """功率平衡指标
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["vim", "vip"], lib='finta')
    def VORTEX(self, period: int = 14, **kwargs) -> IndFrame:
        """涡旋指标 - 两条震荡线分别识别正负趋势移动，基于近两期高低点距离计算，距离越长趋势越强
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:vim,vip
        """
        ...

    @tobtind(lines=["k", "signal"], lib='finta')
    def KST(self, r1: int = 10, r2: int = 15, r3: int = 20, r4: int = 30, **kwargs) -> IndFrame:
        """确然指标 - 基于四个时间框架的平滑变化率，可通过背离、超买超卖、信号线交叉等判断信号
        ---
        Args:
            r1: 第一个时间框架，默认10
            r2: 第二个时间框架，默认15
            r3: 第三个时间框架，默认20
            r4: 第四个时间框架，默认30
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:k,signal
        """
        ...

    @tobtind(lib='finta')
    def TSI(self, long: int = 25, short: int = 13, signal: int = 13, adjust: bool = True, **kwargs) -> IndSeries:
        """真实强度指数 - 基于价格变化的双重平滑动量振荡器
        ---
        Args:
            long: 长期周期，默认25
            short: 短期周期，默认13
            signal: 信号周期，默认13
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def TP(self, **kwargs) -> IndSeries:
        """典型价格 - 某一时期内最高价、最低价和收盘价的算术平均值
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def ADL(self, **kwargs) -> IndSeries:
        """累积分布线 - 衡量资金流入流出，与涨跌线不同，用于判断买卖压力或确认趋势强度
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def CHAIKIN(self, adjust: bool = True, **kwargs) -> IndSeries:
        """柴金振荡器 - 计算累积分布线的3期EMA与10期EMA的差值，凸显累积分布线的动量
        ---
        Args:
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def MFI(self, period: int = 14, **kwargs) -> IndSeries:
        """资金流量指数 - 衡量资金流入流出的动量指标，可视为成交量调整后的RSI，超买超卖阈值为80和20
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def OBV(self, **kwargs) -> IndSeries:
        """能量潮指标 - 累积指标，上涨日加成交量，下跌日减成交量，通过与价格背离预测走势或确认趋势
        ---
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def WOBV(self, **kwargs) -> IndSeries:
        """加权能量潮指标 - 考虑价格差异的OBV变体，避免常规OBV中价格小幅波动但成交量大时的剧烈变化
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def VZO(self, period: int = 14, adjust: bool = True, **kwargs) -> IndSeries:
        """成交量震荡指标 - 利用价格、前一期价格和移动平均线计算震荡值，领先指标，基于超买超卖计算买卖信号。5%-40%为上升趋势区，-40%-5%为下降趋势区；40%以上超买，60%以上极度超买；-40%以下超卖，-60%以下极度超卖。
        ---
        Args:
            period: 周期，默认14
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def PZO(self, period: int = 14, adjust: bool = True, **kwargs) -> IndSeries:
        """价格震荡指标 - 仅基于一个条件：若当日收盘价高于昨日收盘价则为正值（看涨），否则为负值（看跌）。
        ---
        Args:
            period: 用于PZO计算的周期，默认14
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def EFI(self, period: int = 13, adjust: bool = True, **kwargs) -> IndSeries:
        """艾尔德力度指数 - 利用价格和成交量评估走势力度或识别潜在转折点。
        ---
        Args:
            period: 周期，默认13
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def CFI(self, adjust: bool = True, **kwargs) -> IndSeries:
        """累积力度指数 - 基于艾尔德力度指数改进而来。
        ---
        Args:
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["bull_power", "bear_power"], lib='finta')
    def EBBP(self, **kwargs) -> IndFrame:
        """艾尔德多空力度指标 - 显示当日最高价和最低价相对于13日指数移动平均线（EMA）的位置。
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:bull_power,bear_power
        """
        ...

    @tobtind(lib='finta')
    def EMV(self, period: int = 14, **kwargs) -> IndSeries:
        """简易波动指标 - 基于成交量的震荡指标，在零线上下波动，用于衡量价格移动的"难易程度"。指标为正时价格上涨较易，为负时价格下跌较易。
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def CCI(self, period: int = 20, constant: float = 0.015, **kwargs) -> IndSeries:
        """商品通道指数 - 多功能指标，用于识别新趋势或警示极端情况，衡量当前价格相对于某段时间平均价格的位置。在零线上下震荡，常规范围为+100至-100；+100以上超买，-100以下超卖，此时价格大概率向合理水平回调。
        ---
        Args:
            period: 考虑的周期数，默认20
            constant: 常数（0.015），确保约70%-80%的CCI值落在-100至+100之间
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def COPP(self, adjust: bool = True, **kwargs) -> IndSeries:
        """科派克曲线 - 动量指标，当指标从负值区间转为正值区间时，发出买入信号。
        ---
        Args:
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["nbfraw", "nsfraw"], lib='finta')
    def BASP(self, period: int = 40, adjust: bool = True, **kwargs):
        """买卖压力指标 - 用于识别买入和卖出压力。
        ---
        Args:
            period: 周期，默认40
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:nbfraw,nsfraw
        """
        ...

    @tobtind(lines=["nbf", "nsf"], lib='finta')
    def BASPN(self, period: int = 40, adjust: bool = True, **kwargs):
        """标准化买卖压力指标
        ---
        Args:
            period: 周期，默认40
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:nbf,nsf
        """
        ...

    @tobtind(lib='finta')
    def CMO(self, period: int = 9, factor: int = 100, adjust: bool = True, **kwargs) -> IndSeries:
        """钱德动量震荡指标 - 由技术分析师Tushar Chande发明的动量指标。通过计算近期上涨总和与下跌总和的差值，再除以该周期内价格总波动，结果在+100至-100之间波动，类似RSI和随机振荡器。
        ---
        Args:
            period: 周期，默认9
            factor: 因子，默认100
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["s", "l"], lib='finta')
    def CHANDELIER(self, short_period: int = 22, long_period: int = 22, k: int = 3, **kwargs) -> IndFrame:
        """吊灯止损指标 - 基于平均真实波幅（ATR）设置跟踪止损。旨在让交易者紧跟趋势，在趋势延续时避免过早离场。通常在下跌趋势中位于价格上方，上涨趋势中位于价格下方。
        ---
        Args:
            short_period: 短期周期，默认22
            long_period: 长期周期，默认22
            k: 倍数，默认3
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:s,l
        """
        ...

    @tobtind(lib='finta')
    def QSTICK(self, period: int = 14, **kwargs) -> IndSeries:
        """Qstick指标 - 通过过去N天的开盘价与收盘价平均差值，显示阴线（下跌）或阳线（上涨）的主导地位。
        ---
        Args:
            period: 周期，默认14
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def __TMF(self, period: int = 21, **kwargs) -> IndSeries:
        """原函数直接返回raise,移除
        特维格资金流量指标 - 由Colin Twiggs发明，是对资金流向指标（CMF）的改进。
        ---
        Args:
            period: 周期，默认21
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["wt1", "wt2"], lib='finta')
    def WTO(self, channel_length: int = 10, average_length: int = 21, adjust: bool = True, **kwargs):
        """波浪趋势震荡指标
        ---
        Args:
            channel_length: 通道长度，默认10
            average_length: 平均长度，默认21
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:wt1,wt2
        """
        ...

    @tobtind(lib='finta')
    def FISH(self, period: int = 10, adjust: bool = True, **kwargs) -> IndSeries:
        """费雪变换指标 - 由John Ehlers提出，假设价格分布呈方波特性。
        ---
        Args:
            period: 周期，默认10
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["tenkan_sen", "kijun_sen", "senkou_span_a", "senkou_span_b", "chikou_span"], overlap=True, lib='finta')
    def ICHIMOKU(self, tenkan_period: int = 9, kijun_period: int = 26, senkou_period: int = 52, chikou_period: int = 26, **kwargs) -> IndFrame:
        """ Ichimoku云图（一目均衡表） - 多功能指标，用于定义支撑位和阻力位、识别趋势方向、衡量动量并提供交易信号，意为"一眼看清的平衡图表"。
        ---
        Args:
            tenkan_period: 转换线周期，默认9
            kijun_period: 基准线周期，默认26
            senkou_period: 先行跨度周期，默认52
            chikou_period: 延迟线周期，默认26
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:tenkan_sen,kijun_sen,senkou_span_a,senkou_span_b,chikou_span
        """
        ...

    @tobtind(lines=["upper_band", "lower_band"], lib='finta')
    def APZ(self, period: int = 21, dev_factor: int = 2, MA: IndSeries = None, adjust: bool = True, **kwargs) -> IndFrame:
        """自适应价格带 - 由Lee Leibfarth开发的基于波动率的指标，以带状形式显示在价格图表上。在无趋势、震荡的市场中尤其有用，帮助交易者识别潜在的市场转折点。
        ---
        Args:
            period: 周期，默认21
            dev_factor: 偏差因子，默认2
            MA: 移动平均线序列，默认None
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:upper_band,lower_band
        """
        ...

    @tobtind(lib='finta')
    def SQZMI(self, period: int = 20, MA: IndSeries = None, **kwargs) -> IndSeries:
        """挤压动量指标 - 用于识别市场盘整期。市场通常处于平静盘整或垂直价格发现状态，识别平静期有助于把握潜在的大波动交易机会。当市场进入"挤压"状态时，可通过整体动量预测方向并等待能量释放。SQZMI['SQZ']为布尔值，True表示处于挤压状态，False表示挤压已释放。
        ---
        Args:
            period: 考虑的周期数，默认20
            MA: 自定义移动平均线序列，默认使用SMA
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def VPT(self, **kwargs) -> IndSeries:
        """量价趋势指标 - 利用价格与前一期价格的差值结合成交量计算得出。若价格与VPT出现看涨背离（VPT上行、价格下行），则存在买入机会；若出现看跌背离（VPT下行、价格上行），则存在卖出机会。
        ---
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def FVE(self, period: int = 22, factor: int = 0.3, **kwargs) -> IndSeries:
        """资金流量指标 - 有两项重要创新：一是同时考虑日内和日间价格行为，二是通过引入价格阈值纳入微小价格变化。
        ---
        Args:
            period: 周期，默认22
            factor: 因子，默认0.3
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def VFI(self, period: int = 130, smoothing_factor: int = 3, factor: int = 0.2, vfactor: int = 2.5, adjust: bool = True, **kwargs) -> IndSeries:
        """成交量流量指标 - 基于价格移动方向跟踪成交量，类似能量潮指标（OBV）。
        ---
        Args:
            period: 用于VFI计算的周期，默认130
            smoothing_factor: 短期移动平均的周期，默认3
            factor: VFI计算的固定缩放因子，默认0.2
            vfactor: VFI计算的最大成交量阈值，默认2.5
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def MSD(self, period: int = 21, **kwargs) -> IndSeries:
        """移动标准差 - 统计术语，衡量数据围绕平均值的离散程度，也是波动率的衡量指标。离散程度越大，标准差越高；价格剧烈变化时标准差显著上升，市场稳定时标准差较低。低标准差通常出现在价格大幅上涨前，分析师普遍认为高波动率伴随主要顶部，低波动率伴随主要底部。
        ---
        Args:
            period: 用于MSD计算的周期，默认21
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def STC(self, period_fast: int = 23, period_slow: int = 50, k_period: int = 10, d_period: int = 3, adjust: bool = True, **kwargs) -> IndSeries:
        """沙夫趋势周期指标 - 可视为MACD的双重平滑随机指标。计算步骤：1. 计算快速周期（23）和慢速周期（50）的EMA，两者差值为MACD；2. 计算MACD的10期随机指标（STOCH_K、STOCH_D）；3. 对STOCH_D进行3期平均得到STC。指标下降表明趋势周期下行，价格倾向于稳定或跟随下行；指标上升表明趋势周期上行，价格倾向于稳定或跟随上行。
        ---
        Args:
            period_fast: 快速周期，默认23
            period_slow: 慢速周期，默认50
            k_period: 随机K周期，默认10
            d_period: 随机D周期，默认3
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：close
        Returns:
            IndSeries
        """
        ...

    @tobtind(lib='finta')
    def EVSTC(self, period_fast: int = 12, period_slow: int = 30, k_period: int = 10, d_period: int = 3, adjust: bool = True, **kwargs) -> IndSeries:
        """改进型沙夫趋势周期指标 - 使用EVWMA MACD进行计算的沙夫趋势周期变体。
        ---
        Args:
            period_fast: 快速周期，默认12
            period_slow: 慢速周期，默认30
            k_period: 随机K周期，默认10
            d_period: 随机D周期，默认3
            adjust: 是否调整，默认True
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndSeries
        """
        ...

    @tobtind(lines=["bearish_fractals", "bullish_fractals"], lib='finta')
    def WILLIAMS_FRACTAL(self, period: int = 2, **kwargs) -> IndFrame:
        """威廉姆斯分形指标 - 识别分形结构的指标。
        ---
        Args:
            period: 极值点前后需满足的高低点数量，默认2
        NOTE:
            实例包括列：open,high,low,close,volume
        Returns:
            IndFrame:bearish_fractals,bullish_fractals
        """
        ...

    @tobtind(lines=["open", "high", "low", "close"], lib='finta')
    def VC(self, period: int = 5, **kwargs) -> IndSeries:
        """计算价值图表（Value Chart）指标，用于标准化价格波动分析

        价值图表通过将价格数据与浮动轴和波动率单位进行标准化处理，
        帮助分析价格在相对波动区间内的位置，消除了绝对价格水平的影响。

        参数:
            period: 计算滚动平均值的窗口大小，默认为5
            **kwargs: 传递给_finta_get_data方法的额外参数，用于数据获取

        NOTE:
            实例包括列：open,high,low,close

        返回:
            IndFrame :open,high,low,close
        """
        ...

    @tobtind(lib='finta')
    def WAVEPM(self, period: int = 14, lookback_period: int = 100, **kwargs) -> IndSeries:
        """计算WAVEPM（Wave Pattern Momentum）指标，用于分析价格波动模式的动量

        该指标通过结合移动平均线、标准差和双曲正切函数，
        标准化价格波动并生成动量信号，帮助识别价格趋势强度。

        Args:
            period: 计算移动平均线和标准差的窗口大小，默认为14
            lookback_period: 计算方差的回溯窗口大小，默认为100
            **kwargs: 传递给_finta_get_data方法的额外参数，用于数据获取

        NOTE:
            实例包括列：open,high,low,close

        Returns:
            IndSeries
        """
        ...
