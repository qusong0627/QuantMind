from __future__ import annotations
from ..utils import np, pd, partial, reduce, get_lennan, LineStyle, LineDash
from .core import BtIndicator, IndSeries, IndFrame, KLine,Line
from ..stop import BtStop
import math


class Powertrend_Volume_Range_Filter_Strategy(BtIndicator):
    """✈  Powertrend Volume Range Filter Strategy

    来源: https://cn.tradingview.com/script/45FlB2qH-Powertrend-Volume-Range-Filter-Strategy-wbburgin/

    该策略基于成交量调整的范围过滤器，结合ADX和VWMA指标来生成交易信号

    参数:
    ----------
    l : int, 默认200
        平滑范围的长度
    lengthvwma : int, 默认200
        VWMA（成交量加权移动平均）的长度
    mult : float, 默认3.0
        平滑范围的乘数
    lengthadx : int, 默认200
        ADX（平均趋向指标）的长度
    lengthhl : int, 默认14
        高低带趋势跟随的长度
    useadx : bool, 默认False
        是否使用ADX过滤器
    usehl : bool, 默认False
        是否使用高低带过滤器
    usevwma : bool, 默认False
        是否使用VWMA过滤器
    highlighting : bool, 默认True
        是否高亮显示信号

    返回:
    ----------
    volrng : IndSeries
        成交量调整的范围过滤器
    hband : IndSeries
        上带
    lowband : IndSeries
        下带
    dir : np.ndarray
        方向指标
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(l=200, lengthvwma=200, mult=3., lengthadx=200, lengthhl=14,
                  useadx=False, usehl=False, usevwma=False, highlighting=True)
    overlap = dict(volrng=True, hband=True, lowband=True, dir=False)

    @staticmethod
    def smoothrng(x: IndSeries, t: int, m: float = 1.):
        """计算平滑平均范围

        参数:
        ----------
        x : IndSeries
            输入序列
        t : int
            计算长度
        m : float, 默认1.0
            乘数

        返回:
        ----------
        IndSeries
            平滑后的平均范围
        """
        wper = t*2 - 1
        avrng = (x - x.shift()).abs().ema(t)
        smoothrng = m*avrng.ema(wper)
        return smoothrng

    def _rngfilt_volumeadj(self, source1: IndSeries, tethersource: IndSeries, smoothrng: IndSeries):
        """成交量调整的范围过滤器

        参数:
        ----------
        source1 : IndSeries
            源序列
        tethersource : IndSeries
            绑定源序列（通常是成交量）
        smoothrng : IndSeries
            平滑范围

        返回:
        ----------
        tuple
            (范围过滤器, 方向指标)
        """
        source1 = source1.values
        size = source1.size
        rngfilt = source1.copy()
        dir = np.zeros(size)
        start = self.get_lennan(source1, tethersource, smoothrng)
        for i in range(start+1, size):
            if tethersource[i] > tethersource[i-1]:
                rngfilt[i], dir[i] = ((source1[i] - smoothrng[i]) < rngfilt[i-1]
                                      ) and (rngfilt[i-1], dir[i-1]) or (source1[i] - smoothrng[i], 1)

            else:
                rngfilt[i], dir[i] = ((source1[i] + smoothrng[i]) > rngfilt[i-1]
                                      ) and (rngfilt[i-1], dir[i-1]) or (source1[i] + smoothrng[i], -1)
        return IndSeries(rngfilt, lines=["rngfilt"]), dir

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (volrng, hband, lowband, dir, long_signal, short_signal)
        """
        smoothrng = self.smoothrng(self.close, self.params.l, self.params.mult)
        volrng, dir = self._rngfilt_volumeadj(
            self.close, self.volume, smoothrng)
        hband = volrng + smoothrng
        lowband = volrng - smoothrng

        adx = self.adx(self.params.lengthadx).adxx
        adx_vwma = adx.vwma(self.params.lengthadx)
        adx_filter = adx > adx_vwma

        lowband_trendfollow = lowband.tqfunc.llv(self.params.lengthhl)
        highband_trendfollow = hband.tqfunc.hhv(self.params.lengthhl)
        igu_filter_positive = self.close.cross_up(highband_trendfollow.shift()).tqfunc.barlast(
        ) < self.close.cross_down(lowband_trendfollow.shift()).tqfunc.barlast()
        igu_filter_negative = ~igu_filter_positive

        vwma = volrng.vwma(length=self.params.length)
        vwma_filter_positive = volrng > vwma

        long_signal = dir > 0
        long_signal &= self.close.cross_up(hband)
        long_signal &= igu_filter_positive
        long_signal &= adx_filter
        long_signal &= vwma_filter_positive
        # exitlong_signal = dir < 0
        # exitlong_signal &= self.close.cross_down(lowband)
        # exitlong_signal &= igu_filter_negative
        # exitlong_signal &= adx_filter

        short_signal = dir < 0
        short_signal &= self.close.cross_down(lowband)
        short_signal &= igu_filter_negative
        short_signal &= adx_filter
        short_signal &= vwma_filter_positive

        return volrng, hband, lowband, dir, long_signal, short_signal


class Nadaraya_Watson_Envelope_Strategy(BtIndicator):
    """✈ Nadaraya-Watson 包络线策略

    来源: https://cn.tradingview.com/script/HrZicISx-Nadaraya-Watson-Envelope-Strategy-Non-Repainting-Log-Scale/

    该策略使用 Nadaraya-Watson 核回归方法创建非重绘的包络线，用于识别市场趋势和交易信号

    参数:
    ----------
    customLookbackWindow : float, 默认8.0
        自定义回看窗口
    customRelativeWeighting : float, 默认8.0
        自定义相对权重
    customStartRegressionBar : float, 默认25.0
        自定义回归起始柱
    length : int, 默认60
        周期长度
    customATRLength : int, 默认60
        自定义ATR周期长度
    customNearATRFactor : float, 默认1.5
        近端ATR因子
    customFarATRFactor : float, 默认2.0
        远端ATR因子

    返回:
    ----------
    customEnvelopeClose : IndSeries
        收盘价的包络线
    customEnvelopeHigh : IndSeries
        最高价的包络线
    customEnvelopeLow : IndSeries
        最低价的包络线
    customUpperNear : IndSeries
        近端上轨
    customUpperFar : IndSeries
        远端上轨
    customUpperAvg : IndSeries
        平均上轨
    customLowerNear : IndSeries
        近端下轨
    customLowerFar : IndSeries
        远端下轨
    customLowerAvg : IndSeries
        平均下轨
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(customLookbackWindow=8., customRelativeWeighting=8., customStartRegressionBar=25.,
                  length=60, customATRLength=60, customNearATRFactor=1.5, customFarATRFactor=2.)
    overlap = True

    def get_weight(self, x=0, alpha=0, h=0) -> tuple[np.ndarray]:
        """计算权重数组

        参数:
        ----------
        x : float, 默认0
            参考点
        alpha : float, 默认0
            相对权重参数
        h : int, 默认0
            窗口长度

        返回:
        ----------
        np.ndarray
            权重数组
        """
        weights = np.zeros(h)
        for i in range(h):
            weights[i] = np.power(
                1. + (np.power((x - i), 2.) / (2. * alpha * h * h)), -alpha)
        return weights

    @staticmethod
    def customKernel(close: pd.Series, weights=None) -> float:
        """自定义核函数计算

        参数:
        ----------
        close : pd.Series
            收盘价序列
        weights : np.ndarray, 默认None
            权重数组

        返回:
        ----------
        float
            核回归计算结果
        """
        size = close.size
        close = close.apply(lambda x: np.log(x)).values
        sumXWeights = 0.
        sumWeights = 0.
        for i in range(size):
            weight = weights[i]
            sumWeights += weight
            sumXWeights += weight * close[i]
        return np.exp(sumXWeights / sumWeights)

    @staticmethod
    def customATR(length, _high, _low, _close) -> IndSeries:
        """计算自定义ATR

        参数:
        ----------
        length : int
            ATR周期长度
        _high : IndSeries
            最高价序列
        _low : IndSeries
            最低价序列
        _close : IndSeries
            收盘价序列

        返回:
        ----------
        IndSeries
            ATR序列
        """
        df = IndFrame(dict(high=_high, low=_low, close=_close))
        tr = df.true_range()
        return tr.rma(length)

    @staticmethod
    def getEnvelopeBounds(_atr, _nearFactor, _farFactor, _envelope):
        """计算包络线边界

        参数:
        ----------
        _atr : IndSeries
            ATR序列
        _nearFactor : float
            近端因子
        _farFactor : float
            远端因子
        _envelope : IndSeries
            包络线基础值

        返回:
        ----------
        tuple
            (近端上轨, 远端上轨, 平均上轨, 近端下轨, 远端下轨, 平均下轨)
        """
        _upperFar = _envelope + _farFactor*_atr
        _upperNear = _envelope + _nearFactor*_atr
        _lowerNear = _envelope - _nearFactor*_atr
        _lowerFar = _envelope - _farFactor*_atr
        _upperAvg = (_upperFar + _upperNear) / 2
        _lowerAvg = (_lowerFar + _lowerNear) / 2
        return _upperNear, _upperFar, _upperAvg, _lowerNear, _lowerFar, _lowerAvg

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (customEnvelopeClose, customEnvelopeHigh, customEnvelopeLow, customUpperNear, customUpperFar,
             customUpperAvg, customLowerNear, customLowerFar, customLowerAvg, long_signal, short_signal)
        """
        x = self.params.customStartRegressionBar
        h = int(self.params.customStartRegressionBar)
        alpha = self.params.customRelativeWeighting
        weights = self.get_weight(x=x, alpha=alpha, h=h)
        func = partial(self.customKernel, weights=weights)
        customEnvelopeClose = self.close.rolling(
            h).apply(func)
        customEnvelopeHigh = self.high.rolling(
            h).apply(func)
        customEnvelopeLow = self.low.rolling(
            h).apply(func)

        customATR = self.customATR(
            self.params.customATRLength, customEnvelopeHigh, customEnvelopeLow, customEnvelopeClose)

        customUpperNear, customUpperFar, customUpperAvg, customLowerNear, customLowerFar, customLowerAvg = self.getEnvelopeBounds(
            customATR, self.params.customNearATRFactor, self.params.customFarATRFactor, customEnvelopeClose)

        long_signal = self.close.cross_up(customEnvelopeLow)
        long_signal &= customEnvelopeClose > customEnvelopeClose.shift()
        short_signal = self.close.cross_down(customEnvelopeHigh)
        short_signal &= customEnvelopeClose < customEnvelopeClose.shift()

        return customEnvelopeClose, customEnvelopeHigh, customEnvelopeLow, customUpperNear, customUpperFar, \
            customUpperAvg, customLowerNear, customLowerFar, customLowerAvg, long_signal, short_signal  # test


class G_Channels(BtIndicator):
    """✈ G通道指标 - 高效计算上下极值点

    来源: https://www.tradingview.com/script/fIvlS64B-G-Channels-Efficient-Calculation-Of-Upper-Lower-Extremities/

    该指标使用高效算法计算价格的上下极值点，形成通道，用于识别市场趋势和潜在的反转点

    参数:
    ----------
    length : float, 默认144.0
        通道长度参数，控制通道的宽度和敏感度
    cycle : int, 默认1
        周期参数，控制计算极值点的频率
    thresh : float, 默认0.0
        之字转向阈值，当值在0到1之间时启用之字转向线

    返回:
    ----------
    a : IndSeries
        上轨线
    b : IndSeries
        下轨线
    avg : IndSeries
        中轨线（上下轨的平均值）
    zig : IndSeries, 可选
        之字转向线，当thresh在0到1之间时返回
    """
    params = dict(length=144., cycle=1, thresh=0.)
    overlap = True

    def next(self):
        """计算G通道指标

        返回:
        ----------
        无直接返回值，结果通过self.lines属性设置
            - self.lines.a: 上轨线
            - self.lines.b: 下轨线
            - self.lines.avg: 中轨线
            - self.lines.zig: 之字转向线（当thresh在0到1之间时）
        """
        length = self.params.length
        cycle = max(int(self.params.cycle), 1)
        size = self.close.size
        a = self.full()  # 上轨线
        b = self.full()  # 下轨线
        close = self.close.values
        pre_a = 0.  # 前一个上轨值
        pre_b = 0.  # 前一个下轨值

        # 计算上下轨线
        for i in range(size):
            if i and i % cycle == 0:
                a[i] = max(close[i], pre_a) - (pre_a - pre_b) / length
                b[i] = min(close[i], pre_b) + (pre_a - pre_b) / length
                pre_a = a[i]
                pre_b = b[i]

        # 如果周期大于1，进行插值处理
        if cycle > 1:
            a = a.interpolate()
            b = b.interpolate()

        # 计算中轨线
        avg = (a + b) / 2.

        # 方法1：通过lines属性设置结果
        self.lines.a = a
        self.lines.b = b
        self.lines.avg = avg

        # 如果设置了有效的thresh值，添加之字转向线
        if 0. < self.params.thresh < 1.:
            self.lines.zig = self.btind.zigzag_full(self.params.thresh)

        # # 方法2：直接返回结果
        # if 0. < self.params.thresh < 1.:
        #     zig = self.btind.zigzag_full(self.params.thresh)
        #     return a,b,avg,zig
        # return a,b,avg


class STD_Filtered(BtIndicator):
    """✈ STD过滤N极高斯滤波器

    来源: https://cn.tradingview.com/script/i4xZNAoy-STD-Filtered-N-Pole-Gaussian-Filter-Loxx/

    该指标使用N极高斯滤波器对价格数据进行平滑处理，并结合标准差过滤来减少噪声，用于识别趋势和生成交易信号

    参数:
    ----------
    period : int, 默认25
        主周期参数，用于计算高斯滤波器
    order : int, 默认5
        滤波器阶数，控制滤波器的平滑程度
    filterperiod : int, 默认10
        标准差过滤的周期
    filter : float, 默认1.0
        标准差过滤的阈值

    返回:
    ----------
    filt : IndSeries
        过滤后的信号线
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(period=25, order=5, filterperiod=10, filter=1.)
    overlap = dict(out=True, filt=True)  # , dir=False)

    @staticmethod
    def fact(n: int) -> float:
        """计算阶乘

        参数:
        ----------
        n : int
            要计算阶乘的整数

        返回:
        ----------
        float
            n的阶乘
        """
        if n < 2:
            return 1.
        return float(reduce(lambda x, y: x*y, range(1, n+1)))

    @staticmethod
    def alpha(period, poles):
        """计算alpha值

        参数:
        ----------
        period : int
            周期参数
        poles : int
            极点数量

        返回:
        ----------
        float
            计算得到的alpha值
        """
        w = 2.0 * math.pi / period
        b = (1.0 - math.cos(w)) / (math.pow(1.414, 2.0 / poles) - 1.0)
        a = - b + math.sqrt(b * b + 2.0 * b)
        return a

    def makeCoeffs(self, period, order):
        """生成滤波器系数

        参数:
        ----------
        period : int
            周期参数
        order : int
            滤波器阶数

        返回:
        ----------
        np.ndarray
            滤波器系数矩阵
        """
        coeffs = np.full((order+1, 3), 0.)
        a = self.alpha(period, order)
        for i in range(order+1):
            div = self.fact(order - i) * self.fact(i)
            out = self.fact(order) / div if div else 1.
            coeffs[i, :] = [out, math.pow(a, i), math.pow(1.0 - a, i)]
        return coeffs

    @staticmethod
    def npolegf(src: np.ndarray, order: int = 0, coeffs: np.ndarray = None):
        """计算N极点高斯滤波器

        参数:
        ----------
        src : np.ndarray
            输入数据源
        order : int, 默认0
            滤波器阶数
        coeffs : np.ndarray, 默认None
            滤波器系数矩阵

        返回:
        ----------
        np.ndarray
            滤波后的结果
        """
        size = src.size
        nanlen = len(src[np.isnan(src)])
        filt = np.full(size-nanlen, np.nan)
        value = src[nanlen:]
        for j in range(size-nanlen):
            sign = 1.
            _filt = value[j]*coeffs[order, 1]
            for i in range(1, 1+order):
                if j >= i:
                    _filt += sign * coeffs[i, 0] * coeffs[i, 2] * filt[j-i]
                sign *= -1.
            filt[j] = _filt
        if not nanlen:
            return filt
        return np.append(np.full(nanlen, np.nan), filt)

    @staticmethod
    def std_filter(out: IndSeries, length: int, filter: float):
        """标准差过滤

        参数:
        ----------
        out : IndSeries
            输入序列
        length : int
            计算标准差的周期
        filter : float
            过滤阈值

        返回:
        ----------
        np.ndarray
            过滤后的结果
        """
        std = out.stdev(length).values
        filtdev = filter * std
        nanlen = len(std[np.isnan(std)])
        filt = np.array(out.values)
        for i in range(nanlen+1, out.size):
            if abs(filt[i]-filt[i-1]) < filtdev[i]:
                filt[i] = filt[i-1]
        return filt

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (filt, long_signal, short_signal)
                - filt: 过滤后的信号线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # 生成滤波器系数
        coeffs = self.makeCoeffs(self.params.period, self.params.order)

        # 使用Heikin-Ashi收盘价作为数据源
        src = self.ha().close
        # src = self.ohlc4()

        # 对数据源进行标准差过滤
        src = self.std_filter(
            src, self.params.filterperiod, self.params.filter)

        # 应用N极点高斯滤波器
        out = self.npolegf(src, order=self.params.order, coeffs=coeffs)
        filt = IndSeries(out, name="filt")

        # 再次对过滤结果进行标准差过滤
        filt = self.std_filter(
            filt, self.params.filterperiod, self.params.filter)

        # 生成交易信号
        _filt = pd.Series(filt)
        sig = _filt.shift()
        long_signal = _filt > sig
        long_signal &= (_filt.shift() < sig.shift()) | (
            _filt.shift() == sig.shift())
        short_signal = _filt < sig
        short_signal &= (_filt.shift() > sig.shift()) | (
            _filt.shift() == sig.shift())

        # 计算连续信号
        size = self.V
        contsw = np.zeros(size)
        for i in range(size):
            contsw[i] = long_signal[i] and 1 or (
                short_signal[i] and -1 or contsw[i-1])
        contsw = pd.Series(contsw)

        # 过滤信号，只保留趋势反转时的信号
        long_signal &= contsw.shift() == -1
        short_signal &= contsw.shift() == 1

        return filt, long_signal, short_signal


class Turtles_strategy(BtIndicator):
    """✈ 海龟交易策略 - 历经20年验证的有效策略

    来源: https://cn.tradingview.com/script/Q1O23zJP-20-years-old-turtles-strategy-still-work/

    该策略基于理查德·丹尼斯和威廉·埃克哈特的海龟交易法则，使用突破系统进行交易，包含快速和慢速两个时间周期

    参数:
    ----------
    enter_fast : int, 默认20
        快速周期的入场通道长度
    exit_fast : int, 默认10
        快速周期的出场通道长度
    enter_slow : int, 默认55
        慢速周期的入场通道长度
    exit_slow : int, 默认20
        慢速周期的出场通道长度

    返回:
    ----------
    long_signal : np.ndarray
        多头入场信号
    short_signal : np.ndarray
        空头入场信号
    exitlong_signal : np.ndarray
        多头出场信号
    exitshort_signal : np.ndarray
        空头出场信号
    """
    params = dict(enter_fast=20, exit_fast=10, enter_slow=55, exit_slow=20)

    def next(self):
        """计算海龟交易策略信号

        返回:
        ----------
        tuple
            (long_signal, short_signal, exitlong_signal, exitshort_signal)
                - long_signal: 多头入场信号
                - short_signal: 空头入场信号
                - exitlong_signal: 多头出场信号
                - exitshort_signal: 空头出场信号
        """
        # 计算快速周期的高低点
        fastL = self.high.tqfunc.hhv(self.params.enter_fast)  # 快速周期高点
        fastLC = self.low.tqfunc.llv(self.params.exit_fast)  # 快速周期低点（用于出场）
        fastS = self.low.tqfunc.llv(self.params.enter_fast)  # 快速周期低点
        fastSC = self.high.tqfunc.hhv(self.params.exit_fast)  # 快速周期高点（用于出场）

        # 计算慢速周期的高低点
        slowL = self.high.tqfunc.hhv(self.params.enter_slow)  # 慢速周期高点
        slowLC = self.low.tqfunc.llv(self.params.exit_slow)  # 慢速周期低点（用于出场）
        slowS = self.low.tqfunc.llv(self.params.enter_slow)  # 慢速周期低点
        slowSC = self.high.tqfunc.hhv(self.params.exit_slow)  # 慢速周期高点（用于出场）

        # 生成快速周期信号
        long_signal = self.high > fastL.shift()  # 突破快速周期高点，产生多头信号
        exitlong_signal = self.low <= fastLC.shift()  # 跌破快速周期低点，产生多头出场信号
        short_signal = self.low < fastS.shift()  # 跌破快速周期低点，产生空头信号
        exitshort_signal = self.high >= fastSC.shift()  # 突破快速周期高点，产生空头出场信号

        # 结合慢速周期信号
        long_signal |= self.high > slowL.shift()  # 突破慢速周期高点，产生多头信号
        exitlong_signal |= self.low <= slowLC.shift()  # 跌破慢速周期低点，产生多头出场信号
        short_signal |= self.low < slowS.shift()  # 跌破慢速周期低点，产生空头信号
        exitshort_signal |= self.high >= slowSC.shift()  # 突破慢速周期高点，产生空头出场信号

        return long_signal, short_signal, exitlong_signal, exitshort_signal


class Adaptive_Trend_Filter(BtIndicator):
    """✈ 自适应趋势过滤器

    来源: https://cn.tradingview.com/script/PhSlALob-Adaptive-Trend-Filter-tradingbauhaus/

    该指标使用自适应滤波器对价格数据进行平滑处理，并结合超级趋势指标来识别趋势方向和生成交易信号

    参数:
    ----------
    alphaFilter : float, 默认0.01
        自适应滤波器的alpha参数，控制滤波器的敏感度
    betaFilter : float, 默认0.1
        自适应滤波器的beta参数，控制方差的更新速度
    filterPeriod : int, 默认21
        滤波器的周期参数
    supertrendFactor : int, 默认1
        超级趋势指标的因子参数
    supertrendAtrPeriod : int, 默认7
        超级趋势指标的ATR周期

    返回:
    ----------
    filteredValue : IndSeries
        自适应滤波后的价格值
    supertrendValue : np.ndarray
        超级趋势值
    trendDirection : pd.Series
        趋势方向（1为多头，-1为空头）
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(alphaFilter=0.01, betaFilter=0.1, filterPeriod=21,
                  supertrendFactor=1, supertrendAtrPeriod=7)
    overlap = dict(filteredValue=True, supertrendValue=True,
                   trendDirection=False)

    def adaptiveFilter(self, b, alpha, beta):
        """自适应滤波器函数

        参数:
        ----------
        b : int
            滤波器周期
        alpha : float
            alpha参数
        beta : float
            beta参数

        返回:
        ----------
        IndSeries
            滤波后的价格序列
        """
        size = self.close.size
        close = self.close.values
        estimation = np.zeros(size)
        variance = 1.0
        coefficient = alpha * b

        estimation[0] = close[0]
        for i in range(1, size):
            previous = estimation[i-1]
            gain = variance / (variance + coefficient)
            estimation[i] = previous + gain * (close[i] - previous)
            variance = (1 - gain) * variance + beta / b

        return IndSeries(estimation)

    def supertrendFunc(self, src: IndSeries, factor: float, atrPeriod: int):
        """超级趋势函数

        参数:
        ----------
        src : IndSeries
            输入序列（通常是滤波后的价格）
        factor : float
            超级趋势因子
        atrPeriod : int
            ATR周期

        返回:
        ----------
        tuple
            (superTrend, direction)
                - superTrend: 超级趋势值
                - direction: 趋势方向
        """
        atr = self.atr(atrPeriod)
        upperBand = src + factor * atr
        lowerBand = src - factor * atr
        size = src.size
        src = src.values
        upperBand, lowerBand = upperBand.values.copy(), lowerBand.values.copy()
        direction = np.full(size, 1.)
        superTrend = np.zeros(size)
        length = get_lennan(upperBand, lowerBand)
        for i in range(length+1, size):
            prevLowerBand = lowerBand[i-1]
            prevUpperBand = upperBand[i-1]

            lowerBand[i] = (lowerBand[i] > prevLowerBand or src[i -
                                                                1] < prevLowerBand) and lowerBand[i] or prevLowerBand
            upperBand[i] = (upperBand[i] < prevUpperBand or src[i -
                                                                1] > prevUpperBand) and upperBand[i] or prevUpperBand
            prevSuperTrend = superTrend[i-1]

            if prevSuperTrend == prevUpperBand:
                direction[i] = src[i] > upperBand[i] and 1 or -1
            else:
                direction[i] = src[i] < lowerBand[i] and -1 or 1
            superTrend[i] = direction[i] == \
                1. and lowerBand[i] or upperBand[i]
        return superTrend, direction

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (filteredValue, supertrendValue, trendDirection, long_signal, short_signal)
                - filteredValue: 自适应滤波后的价格值
                - supertrendValue: 超级趋势值
                - trendDirection: 趋势方向
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # 应用自适应滤波器
        filteredValue = self.adaptiveFilter(
            self.params.filterPeriod, self.params.alphaFilter, self.params.betaFilter)

        # 应用超级趋势指标
        supertrendValue, trendDirection = self.supertrendFunc(
            filteredValue, self.params.supertrendFactor, self.params.supertrendAtrPeriod)

        # 转换为Series以便后续处理
        trendDirection = pd.Series(trendDirection)

        # 生成交易信号
        long_signal = trendDirection == 1
        long_signal &= trendDirection.shift() == -1  # 只在趋势反转时产生信号
        short_signal = trendDirection == -1
        short_signal &= trendDirection.shift() == 1  # 只在趋势反转时产生信号

        return filteredValue, supertrendValue, trendDirection, long_signal, short_signal


class DCA_Strategy_with_Mean_Reversion_and_Bollinger_Band(BtIndicator):
    """✈ 均值回归与布林带结合的DCA策略

    来源: https://cn.tradingview.com/script/uVaU9LVC-DCA-Strategy-with-Mean-Reversion-and-Bollinger-Band/

    该策略结合了均值回归和布林带技术，用于DCA（定投）策略中识别买入和卖出时机

    参数:
    ----------
    length : int, 默认14
        布林带周期长度
    mult : float, 默认2.0
        布林带标准差乘数

    返回:
    ----------
    upper : IndSeries
        布林带上轨
    lower : IndSeries
        布林带下轨
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """

    params = dict(length=14, mult=2.)
    overlap = True

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (upper, lower, long_signal, short_signal)
                - upper: 布林带上轨
                - lower: 布林带下轨
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # 计算T3移动平均作为布林带中轨
        basis = self.close.t3(self.params.length)
        # 计算布林带标准差
        bb_dev = self.params.mult * self.close.stdev(self.params.length)
        # 计算布林带上轨和下轨
        upper = basis + bb_dev
        lower = basis - bb_dev
        # 生成多头信号：价格上穿下轨且价格上涨
        long_signal = self.close.cross_up(lower) & (
            self.close > self.close.shift())
        # 生成空头信号：价格下穿上轨且价格下跌
        short_signal = self.close.cross_down(
            upper) & (self.close < self.close.shift())
        return upper, lower, long_signal, short_signal


class Multi_Step_Vegas_SuperTrend_strategy(BtIndicator):
    """✈ 多步维加斯超级趋势策略

    来源: https://cn.tradingview.com/script/SXtas3lS-Multi-Step-Vegas-SuperTrend-strategy-presentTrading/

    该策略结合了维加斯通道和超级趋势指标，根据市场波动性自动调整超级趋势的乘数，提高趋势识别的准确性

    参数:
    ----------
    atrPeriod : int, 默认10
        ATR周期长度
    vegasWindow : int, 默认100
        维加斯通道的窗口长度
    superTrendMultiplier : int, 默认5
        超级趋势的基础乘数
    volatilityAdjustment : int, 默认5
        波动性调整参数
    matype : str, 默认"jma"
        移动平均类型

    返回:
    ----------
    superTrend : np.ndarray
        超级趋势线
    marketTrend : np.ndarray
        市场趋势（1为多头，-1为空头）
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(atrPeriod=10, vegasWindow=100,
                  superTrendMultiplier=5, volatilityAdjustment=5, matype="jma")
    overlap = dict(superTrend=True, marketTrend=False)

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (superTrend, marketTrend, long_signal, short_signal)
                - superTrend: 超级趋势线
                - marketTrend: 市场趋势
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # 计算维加斯移动平均
        vegasMovingAverage: IndSeries = getattr(
            self.close, self.params.matype)(self.params.vegasWindow)
        # 计算维加斯通道的标准差
        vegasChannelStdDev = self.close.stdev(self.params.vegasWindow)

        # 计算维加斯通道的上下轨
        vegasChannelUpper = vegasMovingAverage + vegasChannelStdDev
        vegasChannelLower = vegasMovingAverage - vegasChannelStdDev

        # 根据维加斯通道的宽度调整超级趋势乘数
        channelVolatilityWidth = vegasChannelUpper - vegasChannelLower
        adjustedMultiplier = self.params.superTrendMultiplier + \
            self.params.volatilityAdjustment * \
            (channelVolatilityWidth / vegasMovingAverage)

        # 计算超级趋势指标值
        averageTrueRange = self.atr(self.params.atrPeriod)
        superTrendUpper_ = (
            self.hlc3() - (adjustedMultiplier * averageTrueRange)).values
        superTrendLower_ = (
            self.hlc3() + (adjustedMultiplier * averageTrueRange)).values
        size = self.close.size
        superTrendUpper = np.zeros(size)
        superTrendLower = np.zeros(size)
        marketTrend = np.zeros(size)
        lennan = get_lennan(superTrendUpper_, superTrendLower_)
        superTrendPrevUpper = superTrendUpper_[lennan]
        superTrendPrevLower = superTrendLower_[lennan]
        marketTrend[lennan] = 1
        superTrend = np.zeros(size)

        # 更新超级趋势值并确定当前趋势方向
        close = self.close.values
        for i in range(lennan+1, size):
            marketTrend[i] = (close[i] > superTrendPrevLower) and 1 or (
                (close[i] < superTrendPrevUpper) and -1 or marketTrend[i-1])
            superTrendUpper[i] = (marketTrend[i] == 1) and max(
                superTrendUpper_[i], superTrendPrevUpper) or superTrendUpper_[i]
            superTrendLower[i] = (marketTrend[i] == -1) and min(
                superTrendLower_[i], superTrendPrevLower) or superTrendLower_[i]
            superTrendPrevUpper = superTrendUpper[i]
            superTrendPrevLower = superTrendLower[i]
            if marketTrend[i] == 1:
                superTrend[i] = superTrendUpper[i]
            else:
                superTrend[i] = superTrendLower[i]

        # 生成交易信号
        long_signal = marketTrend == 1
        long_signal &= np.append([0], marketTrend[:-1]) == -1  # 只在趋势反转时产生信号
        short_signal = marketTrend == -1
        short_signal &= np.append([0], marketTrend[:-1]) == 1  # 只在趋势反转时产生信号
        return superTrend, marketTrend, long_signal, short_signal


class The_Flash_Strategy(BtIndicator):
    """✈ Flash策略 - 动量RSI与EMA交叉结合ATR

    来源: https://cn.tradingview.com/script/XKgLfo15-The-Flash-Strategy-Momentum-RSI-EMA-crossover-ATR/

    该策略结合了动量RSI、EMA交叉和ATR指标，用于识别市场趋势和生成交易信号

    参数:
    ----------
    length : int, 默认10
        RSI周期长度
    mom_rsi_val : int, 默认50
        动量RSI阈值
    atrPeriod : int, 默认10
        ATR周期长度
    factor : float, 默认3.0
        超级趋势因子
    AP2 : int, 默认12
        自适应周期参数2
    AF2 : float, 默认0.1618
        自适应因子2

    返回:
    ----------
    supertrend : IndSeries
        超级趋势线
    Trail2 : np.ndarray
        跟踪止损线2
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    overlap = True
    params = dict(length=10, mom_rsi_val=50, atrPeriod=10,
                  factor=3., AP2=12, AF2=.1618)

    def next(self):
        """计算指标和信号

        返回:
        ----------
        tuple
            (supertrend, Trail2, long_signal, short_signal)
                - supertrend: 超级趋势线
                - Trail2: 跟踪止损线2
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # 计算动量
        src2 = self.close
        mom: IndSeries = src2 - src2.shift(self.params.length)
        # 计算动量RSI
        rsi_mom = mom.rsi(self.params.length)
        # 计算超级趋势
        supertrend, direction, * \
            _ = self.supertrend(self.params.atrPeriod,
                                    self.params.factor).to_lines()
        # 计算跟踪止损线
        src = self.close
        Trail1 = src.ema(self.params.AP2).values  # Ema func
        AF2 = self.params.AF2 / 100.
        SL2 = Trail1 * AF2  # Stoploss Ema
        size = self.close.size
        Trail2 = np.zeros(size)
        length = get_lennan(Trail1)
        dir = np.zeros(size)
        for i in range(length+1, size):
            iff_1 = Trail1[i] > Trail2[i-1] and Trail1[i] - \
                SL2[i] or Trail1[i] + SL2[i]
            iff_2 = (Trail1[i] < Trail2[i-1] and Trail1[i-1] < Trail2[i-1]
                     ) and min(Trail2[i-1], Trail1[i] + SL2[i]) or iff_1
            Trail2[i] = (Trail1[i] > Trail2[i-1] and Trail1[i-1] > Trail2[i-1]
                         ) and max(Trail2[i-1], Trail1[i] - SL2[i]) or iff_2
            dir[i] = Trail2[i] > Trail2[i -
                                        1] and 1. or (Trail2[i] < Trail2[i-1] and -1 or dir[i-1])
        # 转换为Series以便后续处理
        dir = pd.Series(dir)
        # 生成交易信号
        long_signal = dir > 0
        long_signal &= dir.shift() < 0  # 只在趋势反转时产生信号
        short_signal = dir < 0
        short_signal &= dir.shift() > 0  # 只在趋势反转时产生信号

        return supertrend, Trail2, long_signal, short_signal


class Quantum_Edge_Pro_Adaptive_AI(BtIndicator):
    """✈ 量子边缘专业自适应AI策略

    来源: https://cn.tradingview.com/script/iGZZmHEo-Quantum-Edge-Pro-Adaptive-AI/

    该策略使用自适应AI技术，结合市场结构分析、动量指标和成交量分析，生成综合加权评分以识别交易机会

    参数:
    ----------
    TICK_SIZE : float, 默认0.25
        最小变动价位
    POINT_VALUE : int, 默认2
        点值
    DOLLAR_PER_POINT : int, 默认2
        每点价格价值
    LEARNING_PERIOD : int, 默认40
        学习周期
    ADAPTATION_SPEED : float, 默认0.3
        适应速度
    PERFORMANCE_MEMORY : int, 默认200
        性能记忆周期
    BASE_MIN_SCORE : int, 默认2
        基础最小分数
    BASE_BARS_BETWEEN : int, 默认9
        基础间隔柱数
    MAX_DAILY_TRADES : int, 默认50
        最大日交易次数

    返回:
    ----------
    weighted_score : np.ndarray
        加权评分信号
    """

    params = dict(TICK_SIZE=0.25, POINT_VALUE=2, DOLLAR_PER_POINT=2, LEARNING_PERIOD=40, ADAPTATION_SPEED=0.3,
                  PERFORMANCE_MEMORY=200, BASE_MIN_SCORE=2, BASE_BARS_BETWEEN=9, MAX_DAILY_TRADES=50)
    overlap = False

    def _vars(self):
        """初始化变量

        定义策略所需的各种参数和权重
        """
        LEARNING_PERIOD = 40
        ADAPTATION_SPEED = 0.3
        PERFORMANCE_MEMORY = 200
        BASE_RISK = 0.005
        BASE_MIN_SCORE = 2
        BASE_BARS_BETWEEN = 9
        MAX_DAILY_TRADES = 50
        session_start = 5
        session_end = 16
        glowIntensity = 4
        adaptive_momentum_weight = 1.0
        adaptive_structure_weight = 1.2
        adaptive_volume_weight = 0.8
        adaptive_reversal_weight = 0.6
        adaptive_min_score = BASE_MIN_SCORE * 0.8
        adaptive_risk_multiplier = 1.0
        adaptive_bars_between = BASE_BARS_BETWEEN

        momentum_win_rate = 0.5
        structure_win_rate = 0.5
        volume_win_rate = 0.5
        reversal_win_rate = 0.5
        long_win_rate = 0.5
        short_win_rate = 0.5

    def MARKET_STRUCTURE_ANALYSIS(self) -> tuple[IndSeries]:
        """市场结构分析

        分析市场的支撑阻力位和突破情况

        返回:
        ----------
        tuple
            (bullish_break, bearish_break)
                - bullish_break: 看涨突破信号
                - bearish_break: 看跌突破信号
        """
        swing_high = self.high.shift().tqfunc.hhv(20)  # 计算20周期高点
        swing_low = self.low.shift().tqfunc.llv(20)  # 计算20周期低点
        bullish_break = (self.close > swing_high) & (
            self.close.shift() <= swing_high)  # 突破高点的看涨信号
        bearish_break = (self.close < swing_low) & (
            self.close.shift() >= swing_low)  # 突破低点的看跌信号
        return bullish_break, bearish_break

    def MOMENTUM_INDICATORS(self) -> tuple[IndSeries]:
        """动量指标分析

        计算各种动量指标，包括RSI、MACD和ADX

        返回:
        ----------
        tuple
            (uptrend, downtrend, macd_bull, macd_bear, rsi_fast, rsi_slow)
                - uptrend: 上升趋势信号
                - downtrend: 下降趋势信号
                - macd_bull: MACD看涨信号
                - macd_bear: MACD看跌信号
                - rsi_fast: 快速RSI
                - rsi_slow: 慢速RSI
        """
        rsi_fast = self.close.rsi(7)  # 快速RSI
        rsi_med = self.close.rsi(14)  # medium RSI
        rsi_slow = self.close.rsi(21)  # 慢速RSI

        macd, hist_macd, signal_macd = self.close.macd(
            12, 26, 9).to_lines()  # MACD指标
        macd_bull = (hist_macd > hist_macd.shift()) & (
            hist_macd > 0.)  # MACD看涨信号
        macd_bear = (hist_macd < hist_macd.shift()) & (
            hist_macd < 0.)  # MACD看跌信号
        adx,_, diplus, diminus = self.adx(14, 14).to_lines()  # ADX指标
        strong_trend = adx > 25.  # 强趋势
        uptrend = (diplus > diminus) & strong_trend  # 上升趋势
        downtrend = (diminus > diplus) & strong_trend  # 下降趋势

        trending_viz = adx > 25.  # 趋势可视化
        consolidating_viz = adx < 20.  # 盘整可视化
        return uptrend, downtrend, macd_bull, macd_bear, rsi_fast, rsi_slow

    def VOLUME_ANALYSIS(self):
        """成交量分析

        分析成交量变化和资金流向

        返回:
        ----------
        tuple
            (volume_bullish, volume_bearish, high_volume, v2_orderFlowScore)
                - volume_bullish: 看涨成交量信号
                - volume_bearish: 看跌成交量信号
                - high_volume: 高成交量信号
                - v2_orderFlowScore: 订单流评分
        """
        vol_ma = self.volume.sma(20)  # 成交量移动平均
        vol_std = self.volume.stdev(20)  # 成交量标准差
        high_volume = self.volume > (vol_ma + vol_std)  # 高成交量信号
        relative_volume_viz = self.volume.ZeroDivision(vol_ma)  # 相对成交量可视化
        vpt: IndSeries = (self.close.diff()/self.close.shift()
                          * self.volume).cum(10)  # 成交量价格趋势
        vpt_signal = vpt.ema(10)  # VPT信号
        volume_bullish = vpt > vpt_signal  # 看涨成交量信号
        volume_bearish = -(vpt < vpt_signal)  # 看跌成交量信号

        v2_orderFlowScore = volume_bullish+volume_bearish  # 订单流评分
        return volume_bullish, volume_bearish, high_volume, v2_orderFlowScore

    def next(self):
        """计算加权评分

        综合市场结构、动量和成交量分析，计算加权评分

        返回:
        ----------
        np.ndarray
            加权评分信号
        """
        # 计算波动率
        atr = self.atr(14)
        current_volatility_pct = atr/self.close
        avg_volatility_calc = current_volatility_pct.sma(100)
        high_volatility_regime = (
            current_volatility_pct > 1.2*avg_volatility_calc).values
        low_volatility_regime = (
            current_volatility_pct < 0.8*avg_volatility_calc).values
        bullish_break, bearish_break = self.MARKET_STRUCTURE_ANALYSIS()
        bullish_break, bearish_break = bullish_break.values, bearish_break.values
        uptrend, downtrend, macd_bull, macd_bear, rsi_fast, rsi_slow = self.MOMENTUM_INDICATORS()
        uptrend, downtrend, macd_bull, macd_bear, rsi_fast, rsi_slow =\
            uptrend.values, downtrend.values, macd_bull.values, macd_bear.values, rsi_fast.values, rsi_slow.values
        volume_bullish, volume_bearish, high_volume, v2_orderFlowScore = self.VOLUME_ANALYSIS()
        volume_bullish, volume_bearish, high_volume = volume_bullish.values, volume_bearish.values, high_volume.values
        support = self.low.shift().tqfunc.llv(20).values
        resistance = self.high.shift().tqfunc.hhv(20).values
        close = self.close.values
        sma20 = self.close.sma(20)
        sma50 = self.close.sma(50)
        ma_up_cond = self.close > sma20
        ma_up_cond &= sma20 > sma50
        ma_up_cond = ma_up_cond.values
        ma_dn_cond = self.close < sma20
        ma_dn_cond &= sma20 < sma50
        ma_dn_cond = ma_dn_cond.values
        size = self.V
        # _momentum_score=np.zeros(size)
        final_momentum_weight = 1.  # ENABLE_ADAPTATION ? adaptive_momentum_weight : 1.0
        final_structure_weight = 1.2  # ENABLE_ADAPTATION ? adaptive_structure_weight : 1.2
        final_volume_weight = 0.8  # ENABLE_ADAPTATION ? adaptive_volume_weight : 0.8
        final_reversal_weight = 0.6  # ENABLE_ADAPTATION ? adaptive_reversal_weight : 0.6
        weighted_score = np.zeros(size)
        for i in range(120, size):
            momentum_score = 0.0
            structure_score = 0.0
            volume_score = 0.0
            reversal_score = 0.0
            momentum_multiplier = high_volatility_regime[i] and 0.8 or (
                low_volatility_regime[i] and 1.2 or 1.0)
            if uptrend[i]:
                momentum_score += 0.8 * momentum_multiplier
            if macd_bull[i]:
                momentum_score += 0.4 * momentum_multiplier
            if rsi_fast[i] > 50. and rsi_fast[i] < 80.:
                momentum_score += 0.4 * momentum_multiplier

            if downtrend[i]:
                momentum_score -= 0.8 * momentum_multiplier
            if macd_bear[i]:
                momentum_score -= 0.4 * momentum_multiplier
            if rsi_fast[i] < 50. and rsi_fast[i] > 20.:
                momentum_score -= 0.4 * momentum_multiplier

            if bullish_break[i]:
                structure_score += 1.0
            if bearish_break[i]:
                structure_score -= 1.0

            if volume_bullish[i] and high_volume[i]:
                volume_score += 1.0
            if volume_bearish[i] and high_volume[i]:
                volume_score -= 1.0

            if rsi_slow[i] < 30. and rsi_fast[i] > rsi_fast[i-1] and close[i] <= support[i]:
                reversal_score += 1.0
            if rsi_slow[i] > 70. and rsi_fast[i] < rsi_fast[i-1] and close[i] >= resistance[i]:
                reversal_score -= 1.0

            if ma_up_cond[i]:
                structure_score += 0.5
            if ma_dn_cond[i]:
                structure_score -= 0.5

            weighted_score[i] = (momentum_score * final_momentum_weight) + \
                (structure_score * final_structure_weight) + \
                (volume_score * final_volume_weight) + \
                (reversal_score * final_reversal_weight)
        return weighted_score


class LOWESS(BtIndicator):
    """✈ LOWESS局部加权散点图平滑

    来源: https://cn.tradingview.com/script/hyeoDyZn-LOWESS-Locally-Weighted-Scatterplot-Smoothing-ChartPrime/

    该指标使用局部加权散点图平滑技术对价格数据进行平滑处理，结合高斯移动平均和LOWESS算法，用于识别趋势

    参数:
    ----------
    length : int, 默认100
        主周期长度
    malen : int, 默认100
        移动平均长度

    返回:
    ----------
    GaussianMA : IndSeries
        高斯移动平均
    smoothed : IndSeries
        平滑后的信号
    """
    params = dict(length=100, malen=100)
    overlap = True

    def next(self):
        """计算LOWESS指标

        实现步骤:
        1. 计算ATR和标准差
        2. 计算sigma值（ATR和标准差的平均值）
        3. 应用高斯移动平均
        4. 应用LOWESS算法进行进一步平滑

        返回:
        ----------
        tuple
            (GaussianMA, smoothed)
                - GaussianMA: 高斯移动平均
                - smoothed: 平滑后的信号
        """
        length = self.params.length
        close = self.close.values
        atr = self.atr(length)  # 计算ATR
        std = self.close.stdev(length)  # 计算标准差
        sigma = (atr+std)/2.  # 计算sigma值
        sigma = sigma.values
        data = IndFrame(dict(close=close, sigma=sigma))

        def func(close: np.ndarray, sigma: np.ndarray):
            """高斯移动平均计算函数

            参数:
            ----------
            close : np.ndarray
                收盘价数组
            sigma : np.ndarray
                sigma值数组

            返回:
            ----------
            float
                高斯移动平均值
            """
            close = close[::-1]  # 反转数组
            sigma = sigma[-1]  # 取最后一个sigma值
            gma = 0.
            sumOfWeights = 0.
            for i in range(length):
                h_l = close[:i+1]  # 取前i+1个值
                highest = h_l.max()  # 计算最大值
                lowest = h_l.min()  # 计算最小值
                # 计算权重
                weight = math.exp(-math.pow(((i - (length - 1)
                                              ) / (2 * sigma)), 2) / 2)
                value = highest+lowest  # 计算高低点和
                gma = gma + (value * weight)  # 加权求和
                sumOfWeights += weight  # 计算权重和

            return (gma / sumOfWeights) / 2.  # 计算加权平均并除以2

        GaussianMA = data.rolling_apply(func, length)  # 应用高斯移动平均

        def lowess(src: pd.Series):
            """LOWESS局部加权散点图平滑函数

            参数:
            ----------
            src : pd.Series
                输入序列

            返回:
            ----------
            float
                平滑后的值
            """
            length = len(src)
            src = src.values[::-1]  # 反转数组
            sum_w = 0.0  # 权重和
            sum_wx = 0.0  # 权重乘以x的和
            sum_wy = 0.0  # 权重乘以y的和
            for i in range(length):
                w = math.pow(1 - math.pow(i / length, 3), 3)  # 计算权重
                sum_w += w
                sum_wx += w * i
                sum_wy += w * src[i]
            a = sum_wy / sum_w  # 计算截距
            b = sum_wx / sum_w  # 计算斜率
            return a + b / (length - 1) / 2000.  # 计算最终值

        smoothed = GaussianMA.rolling(
            self.params.malen).apply(lowess)  # 应用LOWESS算法
        return GaussianMA, smoothed


class The_Price_Radio(BtIndicator):
    """✈ John Ehlers The Price Radio

    来源: https://cn.tradingview.com/script/W5lBL0MV-John-Ehlers-The-Price-Radio/

    该指标由John Ehlers开发，使用价格变化率来分析市场动量和趋势

    参数:
    ----------
    length : int, 默认60
        计算周期长度
    period : int, 默认14
        价格变化率的计算周期

    返回:
    ----------
    deriv : IndSeries
        价格变化率
    amup : IndSeries
        上升动量
    amdn : IndSeries
        下降动量
    fm : IndSeries
        波动因子
    """
    params = dict(length=60, period=14)
    overlap = False

    @staticmethod
    def clamp(_value, _min, _max) -> IndSeries:
        """将值限制在指定范围内

        参数:
        ----------
        _value : IndSeries
            输入值
        _min : IndSeries
            最小值
        _max : IndSeries
            最大值

        返回:
        ----------
        IndSeries
            限制在范围内的值
        """
        df = IndFrame(dict(_value=_value, _min=_min, _max=_max))

        def test(_value, _min, _max):
            _t = _min if _value < _min else _value
            return _max if _t > _max else _t
        return df.rolling_apply(test, 1)

    @staticmethod
    def am(_signal: IndSeries, _period) -> IndSeries:
        """计算平均动量

        参数:
        ----------
        _signal : IndSeries
            输入信号
        _period : int
            计算周期

        返回:
        ----------
        IndSeries
            平均动量
        """
        _envelope = _signal.abs().tqfunc.hhv(4)
        return _envelope.sma(_period)

    @staticmethod
    def fm(_signal: IndSeries, _period) -> IndSeries:
        """计算波动因子

        参数:
        ----------
        _signal : IndSeries
            输入信号
        _period : int
            计算周期

        返回:
        ----------
        IndSeries
            波动因子
        """
        _h = _signal.tqfunc.hhv(_period)
        _l = _signal.tqfunc.llv(_period)
        _hl = The_Price_Radio.clamp(10. * _signal, _l, _h)
        return _hl.sma(_period)

    def next(self):
        """计算价格变化率和动量指标

        返回:
        ----------
        tuple
            (deriv, amup, amdn, fm)
                - deriv: 价格变化率
                - amup: 上升动量
                - amdn: 下降动量
                - fm: 波动因子
        """
        deriv = self.close.pct_change(self.params.period)
        amup = The_Price_Radio.am(deriv, self.params.length)
        amdn = -amup
        fm = The_Price_Radio.fm(deriv, self.params.length)
        return deriv, amup, amdn, fm


class PMax_Explorer(BtIndicator):
    """✈ PMax Explorer - 价格最大化指标

    来源: https://cn.tradingview.com/script/nHGK4Qtp/

    该指标使用多种移动平均类型计算价格最大化通道，结合ATR来确定趋势方向和交易信号

    参数:
    ----------
    Periods : int, 默认10
        ATR计算周期
    Multiplier : int, 默认3
        ATR乘数
    mav : str, 默认"ema"
        移动平均类型
    length : int, 默认10
        移动平均长度
    var_length : int, 默认9
        波动率计算长度

    返回:
    ----------
    pmax : np.ndarray
        价格最大化通道值
    """

    params = dict(Periods=10, Multiplier=3,
                  mav="ema", length=10, var_length=9)
    overlap = True

    def var_Func(self, src: IndSeries, length: int, var_length):
        """计算波动率调整移动平均

        参数:
        ----------
        src : IndSeries
            输入序列
        length : int
            移动平均长度
        var_length : int
            波动率计算长度

        返回:
        ----------
        IndSeries
            波动率调整移动平均
        """
        valpha = 2/(length+1)
        vud1 = src-src.shift()
        vud1 = vud1.apply(lambda x: x > 0. and x or 0.)
        vdd1 = vud1.apply(lambda x: x < 0. and -x or 0.)
        # vud1=src>src[1] ? src-src[1] : 0
        # vdd1=src<src[1] ? src[1]-src : 0
        vUD = vud1.rolling(var_length).sum()
        vDD = vdd1.rolling(var_length).sum()
        vCMO = (vUD-vDD).ZeroDivision(vUD+vDD)
        vCMO = vCMO.values
        nanlen = len(vCMO[np.isnan(vCMO)])
        size = src.size
        value = src.values
        VAR = np.zeros(size)
        for i in range(nanlen+1, size):
            VAR[i] = valpha*abs(vCMO[i])*value[i] + \
                (1-valpha*abs(vCMO[i]))*VAR[i-1]
        return IndSeries(VAR)

    def wwma_Func(self, src: IndSeries, length: int):
        """计算加权移动平均

        参数:
        ----------
        src : IndSeries
            输入序列
        length : int
            移动平均长度

        返回:
        ----------
        IndSeries
            加权移动平均
        """
        wwalpha = 1 / length
        value = src.value
        size = src.size
        WWMA = np.zeros(size)
        for i in range(1, size):
            WWMA[i] = wwalpha*value[i] + (1-wwalpha)*WWMA[i-1]
        return IndSeries(WWMA)

    def zlema_Func(self, src: IndSeries, length: int):
        """计算零滞后指数移动平均

        参数:
        ----------
        src : IndSeries
            输入序列
        length : int
            移动平均长度

        返回:
        ----------
        IndSeries
            零滞后指数移动平均
        """
        zxLag = length/2 == round(length/2) and length/2 or (length - 1) / 2
        zxEMAData = src + src.shift(zxLag)
        ZLEMA = zxEMAData.ema(length)
        return ZLEMA

    def tsf_Func(self, src: IndSeries, length: int):
        """计算时间序列预测

        参数:
        ----------
        src : IndSeries
            输入序列
        length : int
            预测周期

        返回:
        ----------
        IndSeries
            时间序列预测值
        """
        lrc = src.linreg(length)
        lrc1 = src.linreg(length, 1)
        lrs = lrc-lrc1
        TSF = src.linreg(length)+lrs
        return TSF

    def getMA(self, src: IndSeries, mav: str, length: int) -> IndSeries:
        """获取指定类型的移动平均

        支持的移动平均类型:
        - 标准类型: "dema", "ema", "fwma", "hma", "linreg", "midpoint", "pwma", "rma",
          "sinwma", "sma", "swma", "t3", "tema", "trima", "vidya", "wma", "zlma"
        - 自定义类型: "var", "wwma", "zlema", "tsf"

        参数:
        ----------
        src : IndSeries
            输入序列
        mav : str
            移动平均类型
        length : int
            移动平均长度

        返回:
        ----------
        IndSeries
            计算得到的移动平均
        """
        if mav in ["dema", "ema", "fwma", "hma", "linreg", "midpoint", "pwma", "rma",
                   "sinwma", "sma", "swma", "t3", "tema", "trima", "vidya", "wma", "zlma"]:
            return src.ma(mav, length)
        elif mav in ["var", "wwma", "zlema", "tsf"]:
            return getattr(self, f"{mav}_Func")(src, length)
        else:
            return src.ema(length)

    def Pmax_Func(self, src: IndSeries):
        """计算价格最大化通道

        参数:
        ----------
        src : IndSeries
            输入序列

        返回:
        ----------
        np.ndarray
            价格最大化通道值
        """
        atr = self.atr(self.params.Periods)
        ma = self.getMA(src, self.params.mav, self.params.length)
        up = ma + self.params.Multiplier*atr
        dn = ma - self.params.Multiplier*atr
        up = up.values
        dn = dn.values
        upnanlen = len(up[np.isnan(up)])
        dnnanlen = len(dn[np.isnan(dn)])
        nanlen = max(upnanlen, dnnanlen)
        size = self.V
        PMax = np.zeros(size)
        PMax[nanlen] = dn[nanlen]
        dir = np.ones(size)
        MAvg = ma.values
        for i in range(nanlen+1, size):
            dir[i] = (dir[i-1] == -1 and MAvg[i] > PMax[i-1]
                      ) and 1 or ((dir[i-1] == 1 and MAvg[i] < PMax[i-1]) and -1 or dir[i-1])
            # if dir[i] != dir[i-1]:
            #     PMax[i] = dir[i] > dir[i-1] and dn[i] or up[i]
            #     continue
            PMax[i] = dir[i] == 1 and max(
                dn[i], PMax[i-1]) or min(up[i], PMax[i-1])
        return PMax

    def next(self):
        """计算价格最大化通道

        返回:
        ----------
        np.ndarray
            价格最大化通道值
        """
        pmax = self.Pmax_Func(self.hl2())
        return pmax


class VMA_Win(BtIndicator):
    """✈ VMA Win - 波动率调整移动平均

    来源: https://cn.tradingview.com/script/09F2GICn-VMA-Win-Dashboard-for-Different-Lengths/

    该指标使用波动率调整的移动平均，根据市场波动情况动态调整权重

    参数:
    ----------
    length : int, 默认15
        计算周期长度

    返回:
    ----------
    vma : IndSeries
        波动率调整移动平均
    """
    params = dict(length=15)
    overlap = True

    def vma(self, src: IndSeries, length):
        """计算波动率调整移动平均

        参数:
        ----------
        src : IndSeries
            输入序列
        length : int
            计算周期长度

        返回:
        ----------
        IndSeries
            波动率调整移动平均
        """
        vmaLen = length
        k = 1.0 / vmaLen
        size = src.size
        diff = src.diff()
        # math.max(src - src[1], 0)
        pdm = diff.apply(lambda x: x > 0. and x or 0.)
        # math.max(src[1] - src, 0)
        mdm = diff.apply(lambda x: x < 0. and -x or 0.)
        pdmS = np.zeros(size)
        mdmS = np.zeros(size)
        pdiS = np.zeros(size)
        mdiS = np.zeros(size)
        iS = np.zeros(size)
        vma = np.zeros(size)
        src = src.values
        for i in range(vmaLen+1, size):
            pdmS[i] = (1. - k) * pdmS[i-1] + k * pdm[i]
            mdmS[i] = (1. - k) * mdmS[i-1] + k * mdm[i]
            s = pdmS[i] + mdmS[i]
            pdi = pdmS[i] / s
            mdi = mdmS[i] / s
            pdiS[i] = (1. - k) * pdiS[i-1] + k * pdi
            mdiS[i] = (1. - k) * mdiS[i-1] + k * mdi
            d = abs(pdiS[i] - mdiS[i])
            s1 = pdiS[i] + mdiS[i]
            iS[i] = (1. - k) * iS[i-1] + k * d / s1
            hhv = iS[i+1-vmaLen:i+1].max()  # ta.highest(iS, vmaLen)
            llv = iS[i+1-vmaLen:i+1].min()  # ta.lowest(iS, vmaLen)
            vI = (iS[i] - llv) / (hhv - llv) if hhv != llv else 0.
            vma[i] = (1. - k * vI) * vma[i-1] + k * vI * src[i]
        return IndSeries(vma)

    def next(self):
        """计算波动率调整移动平均

        返回:
        ----------
        IndSeries
            波动率调整移动平均
        """
        vma = self.vma(self.close, self.params.length)
        return vma


class RJ_Trend_Engine(BtIndicator):
    """✈ RJ Trend Engine - 综合趋势引擎

    来源: https://cn.tradingview.com/script/xZ9IlWfi-RJ-Trend-Engine-Final-Version/

    该指标综合了SAR、超级趋势和ADX指标，用于识别市场趋势和交易信号

    参数:
    ----------
    psarStart : float, 默认0.02
        SAR指标的起始加速因子
    psarIncrement : float, 默认0.02
        SAR指标的加速因子增量
    psarMax : float, 默认0.2
        SAR指标的最大加速因子
    stAtrPeriod : int, 默认10
        超级趋势指标的ATR周期
    stFactor : float, 默认3.0
        超级趋势指标的因子
    adxLen : int, 默认14
        ADX指标的周期
    adxThreshold : int, 默认20
        ADX趋势强度阈值
    bbLength : int, 默认20
        布林带周期
    bbStdDev : float, 默认3.0
        布林带标准差

    返回:
    ----------
    psar : IndSeries
        SAR指标值
    trend : IndSeries
        超级趋势值
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(
        psarStart=0.02,
        psarIncrement=0.02,
        psarMax=0.2,
        stAtrPeriod=10,
        stFactor=3.0,
        adxLen=14,
        adxThreshold=20,
        bbLength=20,
        bbStdDev=3.0
    )
    overlap = True

    def next(self):
        """计算综合趋势指标和交易信号

        返回:
        ----------
        tuple
            (psar, trend, long_signal, short_signal)
                - psar: SAR指标值
                - trend: 超级趋势值
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        psar = self.SAR(self.params.psarStart, self.params.psarMax)
        trend, st_direction, * \
            _ = self.supertrend(self.params.stAtrPeriod,
                                    self.params.stFactor).to_lines()
        adx,adxr, diplus, diminus = self.adx(
            self.params.adxLen, self.params.adxLen).to_lines()
        # bbLower, bbMiddle, bbUpper, * \
        #     _ = self.close.bbands(self.params.bbLength,
        #                           self.params.bbStdDev).to_lines()
        psarFlipUp = (self.close > psar) & (self.open < psar.shift())
        psarFlipDown = (self.close < psar) & (self.open > psar.shift())
        stIsUptrend = st_direction < 0
        stIsDowntrend = st_direction > 0
        adxIsTrending = adx > self.params.adxThreshold
        standardBuySignal = psarFlipUp & stIsUptrend & adxIsTrending
        standardSellSignal = psarFlipDown & stIsDowntrend & adxIsTrending
        reversalBuySignal = psarFlipUp & adxIsTrending & stIsDowntrend
        reversalSellSignal = psarFlipDown & adxIsTrending & stIsUptrend
        long_signal = standardBuySignal | reversalBuySignal
        short_signal = standardSellSignal | reversalSellSignal
        return psar, trend, long_signal, short_signal


class Twin_Range_Filter(BtIndicator):
    """✈ Twin Range Filter - 双范围过滤器

    来源: https://cn.tradingview.com/script/57i9oK2t-Twin-Range-Filter-Buy-Sell-Signals/

    该指标使用两个不同周期的范围过滤器来生成交易信号，结合趋势检测提高信号质量

    参数:
    ----------
    per1 : int, 默认127
        第一个范围过滤器的周期
    mult1 : float, 默认1.6
        第一个范围过滤器的乘数
    per2 : int, 默认155
        第二个范围过滤器的周期
    mult2 : float, 默认2.0
        第二个范围过滤器的乘数

    返回:
    ----------
    filt : np.ndarray
        过滤后的信号线
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """

    params = dict(
        per1=127,
        mult1=1.6,
        per2=155,
        mult2=2.0,
    )
    overlap = True

    def smoothrng(self, x: IndSeries, t: int, m: float):
        """计算平滑范围

        参数:
        ----------
        x : IndSeries
            输入序列
        t : int
            计算周期
        m : float
            乘数

        返回:
        ----------
        IndSeries
            平滑后的范围
        """
        wper = t * 2 - 1
        avrng = x.diff().abs().ema(t)
        return avrng.ema(wper) * m

    def rngfilt(self, x: IndSeries, r: IndSeries):
        """计算范围过滤器

        参数:
        ----------
        x : IndSeries
            输入序列
        r : IndSeries
            范围序列

        返回:
        ----------
        np.ndarray
            过滤后的序列
        """
        size = x.size
        x = x.values
        r = r.values
        rf = np.zeros(size)
        lennan = max(len(x[pd.isnull(x)]), len(r[pd.isnull(r)]))
        rf[lennan] = x[lennan]
        for i in range(lennan+1, size):
            rf[i] = x[i] > rf[i-1] and (x[i] - r[i] < rf[i-1] and rf[i-1] or x[i] - r[i]) or (
                x[i] + r[i] > rf[i-1] and rf[i-1] or x[i] + r[i])
        return rf

    def next(self):
        """计算双范围过滤器和交易信号

        返回:
        ----------
        tuple
            (filt, long_signal, short_signal)
                - filt: 过滤后的信号线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        source = self.close
        smrng1 = self.smoothrng(
            source, self.params.per1, self.params.mult1)
        smrng2 = self.smoothrng(
            source, self.params.per2, self.params.mult2)
        smrng = (smrng1 + smrng2) / 2
        filt = self.rngfilt(source, smrng)

        # // === Trend Detection ===
        size = self.V
        upward = np.zeros(size)
        downward = np.zeros(size)
        lennan = len(filt[pd.isnull(filt)])
        for i in range(lennan+1, size):
            upward[i] = filt[i] > filt[i-1] and upward[i-1] + \
                1 or (0 if filt[i] < filt[i-1] else upward[i-1])
            downward[i] = filt[i] < filt[i-1] and downward[i-1] + \
                1 or (0 if filt[i] > filt[i-1] else downward[i-1])

        # // === Entry Conditions ===
        longCond = (source > filt) & (upward > 0)
        shortCond = (source < filt) & (downward > 0)
        CondIni = np.zeros(size)
        for i in range(1, size):
            CondIni[i] = longCond[i] and 1 or (
                shortCond[i] and -1 or CondIni[i-1])
        CondIni = pd.Series(CondIni)
        long_signal = longCond & (CondIni.shift() == -1)
        short_signal = shortCond & (CondIni.shift() == 1)
        return filt, long_signal, short_signal


class PMax_Explorer_STRATEGY(BtIndicator):
    """✈ PMax Explorer Strategy - 价格最大化通道策略

    来源: https://cn.tradingview.com/script/nHGK4Qtp/

    该策略使用价格最大化通道来生成交易信号，结合移动平均交叉和价格穿越通道来确定买卖时机

    参数:
    ----------
    Periods : int, 默认10
        ATR计算周期
    Multiplier : float, 默认3.0
        ATR乘数
    mav : str, 默认"ema"
        移动平均类型
    length : int, 默认10
        移动平均长度

    返回:
    ----------
    pmax : IndSeries
        价格最大化通道值
    thrend : IndSeries
        趋势方向
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(Periods=10, Multiplier=3., mav="ema", length=10)
    overlap = dict(pmax=True, thrend=False)

    def next(self):
        """计算价格最大化通道策略信号

        返回:
        ----------
        tuple
            (pmax, thrend, long_signal, short_signal)
                - pmax: 价格最大化通道值
                - thrend: 趋势方向
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        src = self.hl2()
        mult = self.params.Multiplier
        MAvg = self.close.ma(self.params.mav, self.params.length)
        pmax, thrend = self.btind.pmax2(self.params.length, mult).to_lines()
        long_signal = MAvg.cross_up(pmax) & src.cross_up(pmax)
        short_signal = MAvg.cross_down(pmax) & src.cross_down(pmax)
        return pmax, thrend, long_signal, short_signal


class UT_Bot_Alerts(BtIndicator):
    """✈ UT Bot Alerts - ATR跟踪止损策略

    来源: https://cn.tradingview.com/script/n8ss8BID-UT-Bot-Alerts/

    该策略使用ATR跟踪止损来生成交易信号，可选择使用Heikin Ashi蜡烛图

    参数:
    ----------
    a : float, 默认1.0
        ATR乘数，控制止损幅度
    c : int, 默认10
        ATR计算周期
    h : bool, 默认False
        是否使用Heikin Ashi蜡烛图

    返回:
    ----------
    alerts : np.ndarray
        跟踪止损线
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(a=1., c=10, h=False)
    overlap = dict(alerts=True, long_signal=False, short_signal=False)

    def next(self):
        """计算ATR跟踪止损策略信号

        返回:
        ----------
        tuple
            (alerts, long_signal, short_signal)
                - alerts: 跟踪止损线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # // Inputs
        a = self.params.a  # "Key Vaule. 'This changes the sensitivity'"
        c = self.params.c  # "ATR Period"
        h = self.params.h  # "Signals from Heikin Ashi Candles"

        xATR = self.atr(c)
        nLoss = a * xATR
        if h:
            close = self.ha().close
        else:
            close = self.close
        size = close.size
        up = (close+nLoss).values
        dn = (close-nLoss).values
        src = close.values
        xATRTrailingStop = np.zeros(size)
        pos = np.zeros(size)
        index = self.get_first_valid_index(up, dn)
        for i in range(index+1, size):
            xATRTrailingStop[i] = (src[i] > xATRTrailingStop[i-1] and src[i-1] > xATRTrailingStop[i-1]) and max(xATRTrailingStop[i-1], dn[i]) or \
                ((src[i] < xATRTrailingStop[i-1] and src[i-1] < xATRTrailingStop[i-1]) and min(xATRTrailingStop[i-1], up[i]) or
                 ((src[i] > xATRTrailingStop[i-1]) and dn[i] or up[i]))

            pos[i] = (src[i-1] < xATRTrailingStop[i-1] and src[i] > xATRTrailingStop[i-1]) and 1 or \
                ((src[i-1] > xATRTrailingStop[i-1] and src[i]
                 < xATRTrailingStop[i-1]) and -1 or pos[i-1])
        ema = close.ema(1, talib=False)
        above = ema.cross_up(xATRTrailingStop)
        below = ema.cross_down(xATRTrailingStop)

        long_signal = close > xATRTrailingStop
        long_signal &= above
        short_signal = close < xATRTrailingStop
        short_signal &= below
        alerts = xATRTrailingStop

        return alerts, long_signal, short_signal

    def step(self):
        if not self.kline.position:
            if self.long_signal.new:
                self.kline.buy(stop=BtStop.SegmentationTracking)
            elif self.short_signal.new:
                self.kline.sell(stop=BtStop.SegmentationTracking)


class SuperTrend(BtIndicator):
    """✈ SuperTrend - 超级趋势指标

    来源: https://cn.tradingview.com/script/r6dAP7yi/

    该指标使用ATR来计算趋势方向和交易信号，可选择是否对ATR进行平滑处理

    参数:
    ----------
    Periods : int, 默认10
        ATR计算周期
    Multiplier : float, 默认3.0
        ATR乘数
    changeATR : bool, 默认True
        是否对ATR进行平滑处理

    返回:
    ----------
    upper : np.ndarray
        上轨
    lower : np.ndarray
        下轨
    trend : pd.Series
        趋势方向
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(Periods=10, Multiplier=3., changeATR=True,)
    overlap = dict(upper=True, lower=True, trend=False)

    def next(self):
        """计算超级趋势指标和交易信号

        返回:
        ----------
        tuple
            (upper, lower, trend, long_signal, short_signal)
                - upper: 上轨
                - lower: 下轨
                - trend: 趋势方向
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        src = self.hl2()
        size = src.size
        atr = self.atr(self.params.Periods)
        if not self.params.changeATR:
            atr = atr.sma(self.params.Periods)
        close = self.close.values
        up = (src-self.params.Multiplier*atr).values.copy()
        dn = (src+self.params.Multiplier*atr).values.copy()
        index = self.get_first_valid_index(up, dn)
        upper = np.full(size, np.nan)
        lower = np.full(size, np.nan)
        trend = np.ones(size)
        for i in range(index+1, size):
            up[i] = close[i-1] > up[i-1] and max(up[i], up[i-1]) or up[i]
            dn[i] = close[i-1] < dn[i-1] and min(dn[i], dn[i-1]) or dn[i]
            trend[i] = (trend[i-1] == -1 and close[i] > dn[i-1]) and 1 or (
                (trend[i-1] == 1 and close[i] < up[i-1]) and -1 or trend[i-1])
            if trend[i] != trend[i-1]:
                lower[i] = up[i]
                upper[i] = dn[i]
            elif trend[i] == 1:
                lower[i] = up[i]
            else:
                upper[i] = dn[i]
        trend = pd.Series(trend)
        long_signal = trend == 1
        long_signal &= trend.shift() == -1
        short_signal = trend == -1
        short_signal &= trend.shift() == 1
        return upper, lower, trend, long_signal, short_signal


class CM_Williams_Vix_Fix_Finds_Market_Bottoms(BtIndicator):
    """✈ CM Williams Vix Fix - 市场底部寻找指标

    来源: https://cn.tradingview.com/script/og7JPrRA-CM-Williams-Vix-Fix-Finds-Market-Bottoms/

    该指标由Larry Williams开发，用于识别市场底部，结合波动率和通道分析

    参数:
    ----------
    hl : int, 默认22
        计算最高价的周期
    bbl : int, 默认20
        布林带周期
    mult : float, 默认2.0
        布林带标准差乘数
    lb : int, 默认50
        计算高低范围的周期
    ph : float, 默认0.95
        范围高点乘数
    pl : float, 默认1.01
        范围低点乘数

    返回:
    ----------
    wvf : IndSeries
        Williams Vix Fix指标值
    lowerBand : IndSeries
        下轨
    upperBand : IndSeries
        上轨
    rangeHigh : IndSeries
        范围高点
    rangeLow : IndSeries
        范围低点
    """
    params = dict(hl=22, bbl=20, mult=2.0, lb=50, ph=.95, pl=1.01)
    overlap = False
    linestyle = dict(wvf=LineStyle(
        line_dash=LineDash.vbar))

    def next(self):
        """计算Williams Vix Fix指标和相关通道

        返回:
        ----------
        tuple
            (wvf, lowerBand, upperBand, rangeHigh, rangeLow)
                - wvf: Williams Vix Fix指标值
                - lowerBand: 下轨
                - upperBand: 上轨
                - rangeHigh: 范围高点
                - rangeLow: 范围低点
        """
        hl = self.params.hl
        highest = self.close.tqfunc.hhv(hl)
        wvf = 100*(highest-self.low).ZeroDivision(highest)

        sDev = self.params.mult * wvf.stdev(self.params.bbl)
        midLine = wvf.sma(self.params.bbl)
        lowerBand = midLine - sDev
        upperBand = midLine + sDev

        rangeHigh = wvf.tqfunc.hhv(self.params.lb) * self.params.ph
        rangeLow = wvf.tqfunc.llv(self.params.lb) * self.params.pl
        return wvf, lowerBand, upperBand, rangeHigh, rangeLow


class WaveTrend_Oscillator(BtIndicator):
    """✈ WaveTrend Oscillator - 波浪趋势振荡器

    来源: https://cn.tradingview.com/script/2KE8wTuF-Indicator-WaveTrend-Oscillator-WT/

    该指标使用波浪理论和振荡器原理，用于识别市场趋势和潜在的反转点

    参数:
    ----------
    n1 : int, 默认10
        第一周期参数
    n2 : int, 默认21
        第二周期参数
    n3 : int, 默认9
        第三周期参数

    返回:
    ----------
    signal : IndSeries
        信号值
    wt1 : IndSeries
        波浪趋势值1
    wt2 : IndSeries
        波浪趋势值2
    """
    params = dict(n1=10, n2=21, n3=9)
    spanstyle = [60, 53, -60, -53]
    overlap = False
    linestyle = dict(signal=LineStyle(line_dash=LineDash.vbar))

    def next(self):
        """计算波浪趋势振荡器

        返回:
        ----------
        tuple
            (signal, wt1, wt2)
                - signal: 信号值
                - wt1: 波浪趋势值1
                - wt2: 波浪趋势值2
        """
        ap = self.hlc3()
        esa = ap.ema(self.params.n1)
        d = (ap - esa).abs().ema(self.params.n1)
        ci = (ap - esa) / (0.015 * d)
        tci = ci.ema(self.params.n2)

        wt1 = tci
        wt2 = wt1.sma(self.params.n3)
        signal = wt1-wt2
        return signal, wt1, wt2


class ADX_and_DI(BtIndicator):
    """✈ ADX and DI - 平均趋向指标和方向指标

    来源: https://cn.tradingview.com/script/VTPMMOrx-ADX-and-DI/

    该指标用于衡量市场趋势的强度和方向，包括ADX（平均趋向指标）、+DI（正向方向指标）和-DI（负向方向指标）

    参数:
    ----------
    length : int, 默认14
        计算周期
    th : int, 默认20
        ADX阈值，用于判断趋势强度

    返回:
    ----------
    ADX : pd.Series
        平均趋向指标
    DIPlus : IndSeries
        正向方向指标
    DIMinus : IndSeries
        负向方向指标
    """
    params = dict(length=14, th=20)
    overlap = False

    def next(self):
        """计算平均趋向指标和方向指标

        返回:
        ----------
        tuple
            (ADX, DIPlus, DIMinus)
                - ADX: 平均趋向指标
                - DIPlus: 正向方向指标
                - DIMinus: 负向方向指标
        """
        length = self.params.length

        TrueRange = self.true_range()
        DirectionalMovementPlus = self.high.diff().tqfunc.max(
            0.).where(self.high.diff() <= -self.low.diff(), 0.)
        DirectionalMovementMinus = (
            0.-self.low.diff()).tqfunc.max(0.).where(-self.low.diff() <= self.high.diff(), 0.)
        size = self.close.size
        SmoothedTrueRange = np.zeros(size)
        SmoothedDirectionalMovementPlus = np.zeros(size)
        SmoothedDirectionalMovementMinus = np.zeros(size)
        index = self.get_first_valid_index(
            TrueRange, DirectionalMovementPlus, DirectionalMovementMinus)
        for i in range(index+1, size):
            SmoothedTrueRange[i] = SmoothedTrueRange[i-11] - \
                (SmoothedTrueRange[i-1])/length + TrueRange[i]
            SmoothedDirectionalMovementPlus[i] = SmoothedDirectionalMovementPlus[i-11] - (
                SmoothedDirectionalMovementPlus[i-1])/length + DirectionalMovementPlus[i]
            SmoothedDirectionalMovementMinus[i] = SmoothedDirectionalMovementMinus[i-11] - (
                SmoothedDirectionalMovementMinus[i-1])/length + DirectionalMovementMinus[i]

        DIPlus = SmoothedDirectionalMovementPlus / SmoothedTrueRange * 100
        DIMinus = SmoothedDirectionalMovementMinus / SmoothedTrueRange * 100
        DX = np.abs(DIPlus-DIMinus) / (DIPlus+DIMinus)*100
        ADX = pd.Series(DX).rolling(length).mean()
        return ADX, DIPlus, DIMinus


class Bollinger_RSI_Double_Strategy(BtIndicator):
    """✈ Bollinger RSI Double Strategy - 布林带RSI双重策略

    来源: https://cn.tradingview.com/script/uCV8I4xA-Bollinger-RSI-Double-Strategy-by-ChartArt-v1-1/

    该策略结合布林带和RSI指标，当价格穿越布林带边界且RSI同时穿越阈值时生成交易信号

    参数:
    ----------
    RSIlength : int, 默认6
        RSI计算周期
    RSIoverSold : float, 默认50.0
        RSI超卖阈值
    RSIoverBought : float, 默认50.0
        RSI超买阈值
    BBlength : int, 默认200
        布林带周期
    BBmult : float, 默认2.0
        布林带标准差乘数

    返回:
    ----------
    BBupper : IndSeries
        布林带上轨
    BBlower : IndSeries
        布林带下轨
    long_signal : np.ndarray
        多头信号
    short_signal : np.ndarray
        空头信号
    """
    params = dict(RSIlength=6, RSIoverSold=50.,
                  RSIoverBought=50., BBlength=200, BBmult=2.)
    overlap = True

    def next(self):
        """计算布林带RSI双重策略信号

        返回:
        ----------
        tuple
            (BBupper, BBlower, long_signal, short_signal)
                - BBupper: 布林带上轨
                - BBlower: 布林带下轨
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        # //////////// RSI
        RSIlength = self.params.RSIlength  # input(6,title="RSI Period Length")
        RSIoverSold = self.params.RSIoverSold
        RSIoverBought = self.params.RSIoverBought
        price = self.close
        vrsi = price.rsi(RSIlength)

        # ///////////// Bollinger Bands
        # input(200, minval=1,title="Bollinger Period Length")
        BBlength = self.params.BBlength
        # input(2.0, minval=0.001, maxval=50,title="Bollinger Bands Standard Deviation")
        BBmult = self.params.BBmult
        BBbasis = price.sma(BBlength)
        BBdev = BBmult * price.stdev(BBlength)
        BBupper = BBbasis + BBdev
        BBlower = BBbasis - BBdev
        long_signal = price.cross_up(BBlower)
        long_signal &= vrsi.cross_up(RSIoverSold)
        short_signal = price.cross_down(BBupper)
        short_signal &= vrsi.cross_down(RSIoverBought)
        return BBupper, BBlower, long_signal, short_signal


class Pivot_Point_Supertrend(BtIndicator):
    """✈ Pivot Point Supertrend - 枢轴点超级趋势指标

    来源: https://cn.tradingview.com/script/L0AIiLvH-Pivot-Point-Supertrend/

    该指标结合枢轴点和超级趋势指标，用于识别市场趋势和交易信号

    参数:
    ----------
    prd : int, 默认7
        枢轴点计算周期
    Factor : int, 默认4
        超级趋势因子
    Pd : int, 默认14
        ATR计算周期

    返回:
    ----------
    无直接返回值，结果通过self.lines属性设置
    """
    params = dict(prd=7, Factor=4, Pd=14,)
    overlap = True

    def next(self):
        """计算枢轴点超级趋势指标

        实现步骤:
        1. 计算枢轴点
        2. 计算超级趋势指标
        3. 通过self.lines属性设置结果

        返回:
        ----------
        无直接返回值，结果通过self.lines属性设置
        """
        prd = self.params.prd
        Factor = self.params.Factor
        Pd = self.params.Pd
        length = 2*prd+1
        # 原代码有未来函数，以下改为当前K线的前length根K线（包括自身K线）来转换
        size = self.V
        high, low = self.high.values, self.low.values
        center = self.full()
        center[length-1] = high[:length].max()
        for i in range(length, size):
            _high = high[i+1-5:i+1]
            _low = low[i+1-5:i+1]
            if _high.max() == _high[-1]:
                lastpp = _high[-1]
            elif _low.min() == _low[-1]:
                lastpp = _low[-1]
            else:
                lastpp = center[i-1]
            center[i] = (center[i-1]*2+lastpp)/3.

        Up = center - (Factor * self.atr(Pd))
        Dn = center + (Factor * self.atr(Pd))
        index = self.get_first_valid_index(Up, Dn)
        close = self.close.values
        TUp = self.full()
        TDown = self.full()
        TUp[index] = TDown[index] = close[index]
        Trend = self.ones
        Trailingsl = self.full()
        for i in range(index+1, size):
            TUp[i] = close[i-1] > TUp[i-1] and max(Up[i], TUp[i-1]) or Up[i]
            TDown[i] = close[i-1] < TDown[i -
                                          1] and min(Dn[i], TDown[i-1]) or Dn[i]
            Trend[i] = close[i] > TDown[i -
                                        1] and 1 or (close[i] < TUp[i-1] and -1 or Trend[i-1])
            Trailingsl[i] = Trend[i] == 1 and TUp[i] or TDown[i]
        Trend = pd.Series(Trend)
        long_signal = Trend == 1
        long_signal &= Trend.shift() == -1
        short_signal = Trend == -1
        short_signal &= Trend.shift() == 1
        return Trailingsl, long_signal, short_signal


# class AlphaTrend(BtIndicator):
#     """https://cn.tradingview.com/script/o50NYLAZ-AlphaTrend/"""
#     params = dict(coeff=1., AP=14, novolumedata=False)
#     overlap = True

#     def next(self):
#         coeff = self.params.coeff
#         AP = self.params.AP
#         novolumedata = self.params.novolumedata
#         ATR = self.true_range().sma(AP)
#         src = self.close

#         upT = self.low - ATR * coeff
#         downT = self.high + ATR * coeff
#         upT, downT = upT.values, downT.values
#         mfi = self.hlc3().mfi(AP).values
#         rsi = src.rsi(AP).values
#         AlphaTrend = self.full()
#         Trend = self.full()
#         size = src.size
#         index = self.get_first_valid_index(upT, downT, mfi, rsi)
#         AlphaTrend[index] = Trend[index] = 0.
#         for i in range(index+1, size):
#             AlphaTrend[i] = (rsi[i] >= 50. if novolumedata else mfi[i] >= 50.) and (upT[i] < AlphaTrend[i-1] and AlphaTrend[i-1] or upT[i]) or \
#                 (downT[i] > AlphaTrend[i-1] and AlphaTrend[i-1] or downT[i])
#             Trend[i] = AlphaTrend[i] > AlphaTrend[i -
#                                                   2] and 1 or (AlphaTrend[i] < AlphaTrend[i-2] and -1 or Trend[i-1])
#         AlphaTrend = IndSeries(AlphaTrend)
#         AlphaTrend2 = AlphaTrend.shift(2)
#         Trend = IndSeries(Trend)
#         long_signal = (Trend.shift() == -1) & (Trend == 1)
#         short_signal = (Trend.shift() == 1) & (Trend == -1)
#         return AlphaTrend, AlphaTrend2, long_signal, short_signal


class Volume_Flow_Indicator(BtIndicator):
    """https://cn.tradingview.com/script/MhlDpfdS-Volume-Flow-Indicator-LazyBear/"""
    params = dict(length=130, coef=0.2, vcoef=2.5,
                  signalLength=5, smoothVFI=False)
    overlap = False

    def next(self):
        length = self.params.length
        coef = self.params.coef
        vcoef = self.params.vcoef
        signalLength = self.params.signalLength
        smoothVFI = self.params.smoothVFI
        volume = self.volume
        def ma(x: IndSeries, y): return x.sma(y) if smoothVFI else x

        typical = self.hlc3()
        inter = typical.apply(np.log).diff()
        vinter = inter.stdev(30)
        cutoff = coef * vinter * self.close
        vave = volume.sma(length).shift()
        vmax = vave * vcoef
        # vc = self.ifs(volume < vmax, volume,
        #               other=vmax) // volume.tqfunc.min(vmax)
        vc = volume.where(volume < vmax, vmax) // volume.tqfunc.min(vmax)
        mf = typical.shift()
        # vcp = self.ifs(mf > cutoff, vc,  other=self.ifs(
        #     mf < -cutoff, -vc, other=0.))
        vcp = vc.where(mf > cutoff, (-vc).where(mf < -cutoff, 0.))

        vfi = ma(vcp.rolling(length).sum()/vave, 3)
        vfima = vfi.ema(signalLength)

        return vfima, vfi


class Chandelier_Exit(BtIndicator):
    """https://cn.tradingview.com/script/AqXxNS7j-Chandelier-Exit/"""
    params = dict(length=22, mult=3., useClose=True)
    overlap = True

    def next(self):
        length = self.params.length
        mult = self.params.mult
        useClose = self.params.useClose

        atr = mult * self.atr(length)
        if useClose:
            longStop = self.close.tqfunc.hhv(length)-atr
            shortStop = self.close.tqfunc.llv(length)+atr
        else:
            longStop = self.high.tqfunc.hhv(length)-atr
            shortStop = self.high.tqfunc.llv(length)+atr
        dir = self.ones
        size = self.V
        close = self.close.values.copy()
        longStopPrev = longStop.shift().bfill().values.copy()
        longStop = longStop.bfill().values.copy()
        shortStopPrev = shortStop.shift().bfill().values.copy()
        shortStop = shortStop.bfill().values.copy()
        up = self.full()
        dn = self.full()
        for i in range(1, size):
            longStopPrev = longStop[i-1]
            longStop[i] = close[i] > longStopPrev and max(
                longStop[i], longStopPrev) or longStop[i]
            shortStopPrev = shortStop[i-1]
            shortStop[i] = close[i] < shortStopPrev and min(
                shortStop[i], shortStopPrev) or shortStop[i]
            dir[i] = close[i] > shortStopPrev and 1 or (
                close[i] < longStopPrev and -1 or dir[i-1])
            if dir[i] == 1:
                up[i] = longStop[i]
            else:
                dn[i] = shortStop[i]

        # dir = IndSeries(dir)
        long_signal = dir == 1
        long_signal &= dir.shift() == -1
        short_signal = dir == -1
        short_signal &= dir.shift() == 1
        return up, dn, long_signal, short_signal


class SuperTrend_STRATEGY(BtIndicator):
    """https://cn.tradingview.com/script/P5Gu6F8k/"""
    params = dict(Periods=22, Multiplier=3., changeATR=True,)
    overlap = True

    def next(self):
        Periods = self.params.Periods
        src = self.hl2()
        Multiplier = self.params.Multiplier
        changeATR = self.params.changeATR
        if changeATR:
            atr = self.atr(Periods)
        else:
            atr = self.true_range().sma(Periods)
        longStop = src-(Multiplier*atr)
        shortStop = src+(Multiplier*atr)
        dir = self.ones
        size = self.V
        close = self.close.values
        longStop = longStop.bfill().values.copy()
        shortStop = shortStop.bfill().values.copy()
        up = self.full()
        dn = self.full()
        for i in range(1, size):
            longStopPrev = longStop[i-1]
            longStop[i] = close[i] > longStopPrev and max(
                longStop[i], longStopPrev) or longStop[i]
            shortStopPrev = shortStop[i-1]
            shortStop[i] = close[i] < shortStopPrev and min(
                shortStop[i], shortStopPrev) or shortStop[i]
            dir[i] = (dir[i-1] == -1 and close[i] > shortStopPrev) and 1 or (
                (dir[i-1] == 1 and close[i] < longStopPrev) and -1 or dir[i-1])
            if dir[i] == 1:
                up[i] = longStop[i]
            else:
                dn[i] = shortStop[i]
        # dir = IndSeries(dir)
        long_signal = dir == 1
        long_signal &= dir.shift() == -1
        short_signal = dir == -1
        short_signal &= dir.shift() == 1
        return up, dn, long_signal, short_signal


class Optimized_Trend_Tracker(BtIndicator):
    """https://cn.tradingview.com/script/zVhoDQME/"""
    params = dict(length=2, var_length=9, percent=1.4, base=200)
    overlap = True

    def var_Func(self, src: IndSeries, length: int, var_length):
        valpha = 2/(length+1)
        vud1 = src-src.shift()
        vud1 = vud1.apply(lambda x: x > 0. and x or 0.)
        vdd1 = vud1.apply(lambda x: x < 0. and -x or 0.)
        # vud1=src>src[1] ? src-src[1] : 0
        # vdd1=src<src[1] ? src[1]-src : 0
        vUD = vud1.rolling(var_length).sum()
        vDD = vdd1.rolling(var_length).sum()
        vCMO = (vUD-vDD).ZeroDivision(vUD+vDD)
        vCMO = vCMO.values
        nanlen = len(vCMO[np.isnan(vCMO)])
        size = src.size
        value = src.values
        VAR = np.zeros(size)
        for i in range(nanlen+1, size):
            VAR[i] = valpha*abs(vCMO[i])*value[i] + \
                (1-valpha*abs(vCMO[i]))*VAR[i-1]
        return IndSeries(VAR)

    def next(self):
        percent = self.params.percent
        base = self.params.base
        base_up = (base+percent)/base
        base_dn = (base-percent)/base
        MAvg = self.var_Func(self.close, self.params.length,
                             self.params.var_length)
        fark = MAvg*percent*0.01
        longStop = MAvg - fark
        shortStop = MAvg + fark
        MAvg = MAvg.values.copy()
        longStop = longStop.values.copy()
        shortStop = shortStop.values.copy()
        lennan = len(longStop[np.isnan(longStop)])
        dir = np.ones(self.V)
        MT = np.full(self.V, np.nan)
        OTT = np.full(self.V, np.nan)
        for i in range(lennan+1, self.V):
            longStopPrev = longStop[i-1]
            longStop[i] = MAvg[i] > longStopPrev and max(
                longStop[i], longStopPrev) or longStop[i]
            shortStopPrev = shortStop[i-1]
            shortStop[i] = MAvg[i] < shortStopPrev and min(
                shortStop[i], shortStopPrev) or shortStop[i]
            dir[i] = (dir[i-1] == -1 and MAvg[i] > shortStopPrev) and 1 or (
                (dir[i-1] == 1 and MAvg[i] < longStopPrev) and -1 or dir[i-1])
            MT[i] = dir[i] == 1 and longStop[i] or shortStop[i]
            OTT[i] = MAvg[i] > MT[i] and MT[i] * \
                base_up or MT[i]*base_dn
        MT, OTT = IndSeries(MT), IndSeries(OTT)
        long_signal = OTT.cross_up(MT)
        short_signal = MT.cross_up(OTT)
        return MT, OTT, long_signal, short_signal


class TonyUX_EMA_Scalper(BtIndicator):
    """https://cn.tradingview.com/script/egfSfN1y-TonyUX-EMA-Scalper-Buy-Sell/"""
    params = dict(length=20, period=8)
    overlap = True

    def next(self):
        length = self.params.length
        period = self.params.period
        src = self.close
        out = src.ema(length)
        lasth = self.close.tqfunc.hhv(period)
        lastl = self.close.tqfunc.llv(period)

        long_signal = self.close.cross_up(out) & (
            self.close.shift() < self.close)
        short_signal = self.close.cross_down(out) & (
            self.close.shift() > self.close)
        return lasth, lastl, long_signal, short_signal


class Turtle_Trade_Channels_Indicator_TUTCI(BtIndicator):
    """https://cn.tradingview.com/script/pB5nv16J/"""
    params = dict(length=120, len2=100, )
    overlap = True
    linestyle = dict(sup=LineStyle(line_dash=LineDash.dotdash),
                     sdown=LineStyle(line_dash=LineDash.dotdash),
                     K=LineStyle(line_width=3.))

    def next(self):
        length = self.params.length
        len2 = self.params.len2

        up = self.high.tqfunc.hhv(length)
        down = self.low.tqfunc.llv(length)
        sup = self.high.tqfunc.hhv(len2)
        sdown = self.low.tqfunc.llv(len2)
        cond = (self.high >= up.shift()).tqfunc.barlast() <= (self.low <= down.shift(
        )).tqfunc.barlast()
        # cond = (self.close > up.shift()).tqfunc.barlast() <= (self.close < down.shift(
        # )).tqfunc.barlast()
        K1 = down.where(cond, up)
        K2 = sdown.where(cond, sup)
        kmax = K1.tqfunc.max(K2)
        kmin = K1.tqfunc.min(K2)
        K = kmin.where(cond, kmax)
        long_signal = cond & (~cond.shift())
        short_signal = (~cond) & cond.shift()

        exitlong_signal = self.low == sdown.shift()
        exitlong_signal |= sdown.shift().cross_up(self.low)
        exitshort_signal = self.high == sup.shift()
        exitshort_signal |= self.high.cross_up(sup.shift())

        return sup, sdown, K, long_signal, short_signal
    

class AlphaTrend(BtIndicator):
    """
    AlphaTrend — 自适应趋势线
    https://cn.tradingview.com/script/o50NYLAZ-AlphaTrend/

    参数:
    - coeff:        乘数，默认 1.0
    - AP:           通用周期，默认 14
    - novolumedata: True 时使用 RSI 判断方向，False 时使用 MFI
    """
    lines = ('alpha_trend', 'alpha_shift', 'long_signal', 'short_signal')
    isplot = dict(long_signal=False, short_signal=False)
    params = dict(coeff=1.0, AP=14, novolumedata=False)
    overlap = True

    def __init__(self):
        close = self.kline.close.values
        high = self.kline.high.values
        low = self.kline.low.values
        n = len(close)

        coeff = self.params.coeff
        AP = self.params.AP

        # ================================================================
        # 1. ATR = SMA(True Range, AP)
        # ================================================================
        tr = self.atr(AP)

        # ================================================================
        # 2. upT / downT
        # ================================================================
        upT = low - tr * coeff
        downT = high + tr * coeff

        # ================================================================
        # 3. 趋势条件: RSI(>=50) 或 MFI(>=50)
        # ================================================================
        if self.params.novolumedata:
            # RSI (Wilder's RMA smoothing, 同 TradingView ta.rsi)
            rsi_val=self.close.rsi(AP)
            condition = rsi_val >= 50.
        else:
            # MFI = Money Flow Index
            mfi_val=self.mfi(AP)
            condition = mfi_val >= 50.

        # ================================================================
        # 4. AlphaTrend 递归计算
        #    - 条件满足时: upT < prev ? prev : upT
        #    - 条件不满足时: downT > prev ? prev : downT
        # ================================================================
        alpha_trend = np.full(n, np.nan)
        for i in range(n):
            if np.isnan(upT[i]) or np.isnan(downT[i]):
                continue
            prev = alpha_trend[i - 1] if i > 0 else np.nan

            if condition[i]:
                if np.isnan(prev) or upT[i] >= prev:
                    alpha_trend[i] = upT[i]
                else:
                    alpha_trend[i] = prev
            else:
                if np.isnan(prev) or downT[i] <= prev:
                    alpha_trend[i] = downT[i]
                else:
                    alpha_trend[i] = prev

        self.lines.alpha_trend = alpha_trend
        alpha_trend = IndSeries(alpha_trend)
        # ================================================================
        # 5. 交易信号（按原码 barssince 过滤）
        #    buySignalk  = crossover(AlphaTrend, AlphaTrend[2])
        #    sellSignalk = crossunder(AlphaTrend, AlphaTrend[2])
        #    K1 = barssince(buySignalk),   K2 = barssince(sellSignalk)
        #    O1 = barssince(buySignalk[1]), O2 = barssince(sellSignalk[1])
        #    BUY  = buySignalk  and O1 > K2  (避免连续重复买入)
        #    SELL = sellSignalk and O2 > K1  (避免连续重复卖出)
        # ================================================================
        at_shift2 = alpha_trend.shift(2)
        prev_at = alpha_trend.shift(1)
        prev_at_shift2 = at_shift2.shift(1)

        buy_raw = (alpha_trend > at_shift2) & (prev_at <= prev_at_shift2)
        sell_raw = (alpha_trend < at_shift2) & (prev_at >= prev_at_shift2)

        # barssince 计算
        K1 = IndSeries(buy_raw).tqfunc.barlast()
        K2 = IndSeries(sell_raw).tqfunc.barlast()

        buy_shift1 = buy_raw.shift(1)
        sell_shift1 = sell_raw.shift(1)

        O1 = IndSeries(buy_shift1).tqfunc.barlast()
        O2 = IndSeries(sell_shift1).tqfunc.barlast()

        long_signal = buy_raw & (O1 > K2)
        short_signal = sell_raw & (O2 > K1)

        self.lines.alpha_shift = at_shift2
        self.lines.long_signal = long_signal
        self.lines.short_signal = short_signal
        
class ATRRope(BtIndicator):
    """
    ATR Rope — 基于 ATR 的自适应绳索通道
    https://cn.tradingview.com/script/YYrxRhi9-ATR-Rope/

    参数:
    - length:  ATR 周期，默认 14
    - multi:   ATR 乘数，默认 1.5
    - src:     价格源（'close' / 'hlc3' / 'ohlc4'），默认 'close'
    """

    lines = ('rope', 'upper', 'lower', 'range_hi', 'range_lo')
    params = dict(length=14, multi=1.5, src='close')
    overlap = True

    def __init__(self):
        high = self.kline.high.values
        low = self.kline.low.values
        close = self.kline.close.values
        n = len(close)

        length = self.params.length
        multi = self.params.multi
        src_name = self.params.src

        # 选择价格源
        if src_name == 'hlc3':
            src = (high + low + close) / 3.0
        elif src_name == 'ohlc4':
            o = self.kline.open.values
            src = (o + high + low + close) / 4.0
        else:
            src = close

        # ================================================================
        # 1. ATR 阈值
        # ================================================================
        atr = self.atr(length)*multi

        # ================================================================
        # 2. Rope Smoother
        # ================================================================
        rope = np.full(n, np.nan)
        upper = np.full(n, np.nan)
        lower = np.full(n, np.nan)
        dir_ = np.zeros(n, dtype=int)
        for i in range(length,n):
            if i == length:
                rope[i] = src[i]
                upper[i] = rope[i] + atr[i]
                lower[i] = rope[i] - atr[i]
                continue

            _move = src[i] - rope[i - 1]
            threshold = atr[i]
            # 仅当偏离超过阈值时移动
            rope[i] = rope[i - 1] + max(abs(_move) - threshold, 0.0) * np.sign(_move)
            upper[i] = rope[i] + threshold
            lower[i] = rope[i] - threshold

        # ================================================================
        # 3. 方向检测
        # ================================================================
        dir_val = 0
        for i in range(length,n):
            if i == length:
                dir_[i] = 0
                continue

            if rope[i] > rope[i - 1]:
                dir_val = 1
            elif rope[i] < rope[i - 1]:
                dir_val = -1
            # 保持不变

            # 价格穿越 rope → 归零
            if (src[i - 1] <= rope[i - 1] and src[i] > rope[i]) or \
               (src[i - 1] >= rope[i - 1] and src[i] < rope[i]):
                dir_val = 0

            dir_[i] = dir_val

        # ================================================================
        # 4. 盘整区间（方向 = 0 期间累加上/下轨均值）
        # ================================================================
        range_hi = np.full(n, np.nan)
        range_lo = np.full(n, np.nan)

        h_sum = 0.0
        l_sum = 0.0
        c_count = 0

        for i in range(n):
            if dir_[i] == 0:
                if i > 0 and dir_[i - 1] != 0:
                    # 刚进入盘整，重置
                    h_sum = 0.0
                    l_sum = 0.0
                    c_count = 0

                h_sum += upper[i]
                l_sum += lower[i]
                c_count += 1
                range_hi[i] = h_sum / c_count
                range_lo[i] = l_sum / c_count
            # 趋势中保持 NaN

        self.lines.rope = rope
        self.lines.upper = upper
        self.lines.lower = lower
        self.lines.range_hi = range_hi
        self.lines.range_lo = range_lo
        
class DeltaRSISignals(BtIndicator):
    """
    Delta-RSI Oscillator Strategy [tbiktag]
    基于 Pine Script "Delta-RSI Oscillator Strategy" (tbiktag) 转换
    参考 TradingView: https://cn.tradingview.com/script/OXQVFTQD-Delta-RSI-Oscillator/

    三种可选交易信号条件：
      - Zero-Crossing:        D-RSI 上穿/下穿 0 线
      - Signal Line Crossing: D-RSI 上穿/下穿信号线
      - Direction Change:     D-RSI 在负值/正值区域转向

    NRMSE 过滤：启用时仅当多项式拟合误差小于阈值才产生信号。

    输出线：
      drsi          — Delta-RSI 值
      signal        — Signal 线
      nrmse          — NRMSE 值
      long_signal  — 做多信号（1=有信号，0=无）
      short_signal  — 做空信号（1=有信号，0=无）
      endlong_signal  — 多头离场信号
      endshort_signal — 空头离场信号

    参数：
      rsi_length    — RSI 计算周期，默认 21
      window        — 多项式拟合窗口，默认 21
      degree        — 拟合多项式阶数，默认 2
      signal_length — Signal 线 EMA 周期，默认 9
      buycond       — 做多条件: 'Zero-Crossing'/'Signal Line Crossing'/'Direction Change'，默认 'Zero-Crossing'
      sellcond      — 做空条件，默认 'Zero-Crossing'
      endcond       — 离场条件，默认 'Zero-Crossing'
      use_nrmse     — 是否启用 NRMSE 过滤，默认 False
      nrmse_thrs    — NRMSE 阈值（%），默认 10
    """
    lines = ('drsi', 'signal', 'nrmse', 'long_signal', 'short_signal', 'exitlong_signal', 'exitshort_signal')
    params = dict(
        rsi_length=21,
        window=21,
        degree=2,
        signal_length=9,
        buycond='Zero-Crossing',
        sellcond='Zero-Crossing',
        endcond='Zero-Crossing',
        use_nrmse=False,
        nrmse_thrs=10.0,
    )
    isplot = dict(nrmse=False, long_signal=False, short_signal=False, exitlong_signal=False, exitshort_signal=False)
    
    @staticmethod
    def _poly_diff(src: np.ndarray, window: int, degree: int):
        """
        多项式微分器：对滑动窗口做最小二乘多项式拟合，返回最新点的导数值和 NRMSE。

        使用 Vandermonde 矩阵 + 最小二乘拟合（等价于 Pine Script 的 QR 伪逆），
        在每根 bar 对过去 window 个 RSI 值拟合 degree 阶多项式，
        计算多项式在最新点 (x = window-1) 的一阶导数。

        作用说明（相对于价格）:
            diff  — RSI 的变化速率（动量加速度）。
                    diff > 0 表示 RSI 上升 → 价格上涨动量加速或下跌动量减弱；
                    diff < 0 表示 RSI 下降 → 价格下跌动量加速或上涨动量减弱；
                    |diff| 越大动量越强，|diff| 越小动量衰竭。
            nrmse — 多项式拟合优度（价格动量的规则性）。
                    nrmse 低 (<0.05) → RSI 变化规则，价格处于平滑趋势，信号可靠；
                    nrmse 中 (0.05~0.15) → RSI 有噪声但可拟合，价格趋势带噪声；
                    nrmse 高 (≥0.15) → RSI 剧烈震荡，价格处于盘整/转折，信号不可靠。

        Returns:
            diff  — 形状 (n,)，导数序列
            nrmse — 形状 (n,)，归一化均方根误差 (NRMSE = RMSE / |mean|)
        """
        n = len(src)
        diff = np.full(n, np.nan, dtype=np.float64)
        nrmse = np.full(n, np.nan, dtype=np.float64)
        if n < window:
            return diff, nrmse

        # Vandermonde 矩阵: V[i][j] = i^j, j=0..degree, i=0..window-1
        x = np.arange(window, dtype=np.float64)
        V = np.vander(x, N=degree + 1, increasing=True)  # (window, degree+1)

        for i in range(window - 1, n):
            y = np.asarray(src[i - window + 1 : i + 1], dtype=np.float64)

            # 最小二乘拟合: y = V @ coeffs
            coeffs, _, _, _ = np.linalg.lstsq(V, y, rcond=None)

            # 导数: f'(x) = a_1 + 2*a_2*x + ... + degree*a_degree*x^(degree-1)
            d = 0.0
            for j in range(1, degree + 1):
                d += j * coeffs[j] * (window - 1) ** (j - 1)
            diff[i] = d

            # NRMSE = RMSE / |mean(y)|
            y_hat = V @ coeffs
            mse = float(np.mean((y - y_hat) ** 2))
            y_mean = float(np.mean(y))
            if abs(y_mean) > 1e-12:
                nrmse[i] = np.sqrt(mse) / abs(y_mean)

        return diff, nrmse

    def __init__(self):
        close = np.asarray(self.close, dtype=np.float64)
        n = len(close)

        rsi = self.close.rsi(length=self.params.rsi_length)
        drsi, nrmse = self._poly_diff(rsi, self.params.window, self.params.degree)
        drsi = IndSeries(drsi)
        signal = drsi.ema(self.params.signal_length)
        self.lines.drsi = drsi
        self.lines.signal = signal
        self.lines.nrmse = nrmse

        # RMSE 过滤
        nrmse_thrs = self.params.nrmse_thrs / 100.0
        if self.params.use_nrmse:
            rmse_ok = np.isfinite(nrmse) & (nrmse < nrmse_thrs)
        else:
            rmse_ok = np.ones(n, dtype=bool)

        # 方向变化条件
        dir_up = np.zeros(n, dtype=bool)
        dir_dn = np.zeros(n, dtype=bool)
        for i in range(2, n):
            dir_up[i] = (drsi[i] > drsi[i-1]) and (drsi[i-1] < drsi[i-2]) and drsi[i-1] < 0.0
            dir_dn[i] = (drsi[i] < drsi[i-1]) and (drsi[i-1] > drsi[i-2]) and drsi[i-1] > 0.0

        # 0 线交叉
        cross_up = np.zeros(n, dtype=bool)
        cross_dn = np.zeros(n, dtype=bool)
        for i in range(1, n):
            if not (np.isfinite(drsi[i]) and np.isfinite(drsi[i-1])):
                continue
            cross_up[i] = drsi[i] > 0.0 and drsi[i-1] <= 0.0
            cross_dn[i] = drsi[i] < 0.0 and drsi[i-1] >= 0.0

        # 信号线交叉
        cross_sig_up = np.zeros(n, dtype=bool)
        cross_sig_dn = np.zeros(n, dtype=bool)
        for i in range(1, n):
            if not (np.isfinite(drsi[i]) and np.isfinite(drsi[i-1]) and
                    np.isfinite(signal[i]) and np.isfinite(signal[i-1])):
                continue
            cross_sig_up[i] = drsi[i] > signal[i] and drsi[i-1] <= signal[i-1]
            cross_sig_dn[i] = drsi[i] < signal[i] and drsi[i-1] >= signal[i-1]

        # 条件映射
        cond_map = {
            'Direction Change': (dir_up, dir_dn),
            'Zero-Crossing': (cross_up, cross_dn),
            'Signal Line Crossing': (cross_sig_up, cross_sig_dn),
        }
        bc = self.params.buycond
        sc = self.params.sellcond
        ec = self.params.endcond

        _up_buy, _dn_sell = cond_map.get(bc, cond_map['Zero-Crossing'])
        _up_sell, _dn_short = cond_map.get(sc, cond_map['Zero-Crossing'])
        _up_end, _dn_end = cond_map.get(ec, cond_map['Zero-Crossing'])

        self.lines.long_signal = (_up_buy & rmse_ok).astype(np.int32)
        self.lines.short_signal = (_dn_short & rmse_ok).astype(np.int32)
        self.lines.exitlong_signal = (_dn_end & rmse_ok).astype(np.int32)
        self.lines.exitshort_signal = (_up_end & rmse_ok).astype(np.int32)


class DynamicSwingAnchoredVWAP(BtIndicator):
    """
    动态 Swing 锚定 VWAP
    基于 Pine Script "Dynamic Swing Anchored VWAP (Zeiierman)" 转换
    https://cn.tradingview.com/script/SxgyrEde-Dynamic-Swing-Anchored-VWAP-Zeiierman/
    
    核心逻辑：
    1. Swing 高点/低点检测（prd 周期 highestbars/lowestbars == 0）
    2. ATR 波动率自适应调节 APT（Adaptive Price Tracking）周期
    3. 方向变化时（新高/新低），VWAP 从 swing 点重新锚定
    4. EWMA 型 VWAP = EMA(hlc3*vol) / EMA(vol)，alpha = 1 - e^(-ln2/apt)
    
    参数:
    - prd:         Swing 检测周期，默认 50
    - base_apt:    基础自适应价格跟踪周期，默认 20
    - use_adapt:   是否启用 ATR 波动率自适应 APT，默认 False
    - vol_bias:    波动率偏差乘数，默认 10.0
    
    输出:
    - dsav: 动态 Swing 锚定 VWAP 值，与 kline 时间对齐
    """

    lines = ('dsav',)
    params = dict(prd=50, base_apt=20, use_adapt=False, vol_bias=10.0)
    overlap = True
    
    @staticmethod
    def _highestbars(src: np.ndarray, length: int) -> np.ndarray:
        """Pine Script ta.highestbars：返回距最高价的 bar 数（0=当前最高）"""
        n = len(src)
        out = np.full(n, np.nan)
        for i in range(length - 1, n):
            window = src[i - length + 1 : i + 1]
            out[i] = float(length - 1 - np.argmax(window))
        return out

    @staticmethod
    def _lowestbars(src: np.ndarray, length: int) -> np.ndarray:
        """Pine Script ta.lowestbars"""
        n = len(src)
        out = np.full(n, np.nan)
        for i in range(length - 1, n):
            window = src[i - length + 1 : i + 1]
            out[i] = float(length - 1 - np.argmin(window))
        return out

    def __init__(self):
        high = self.kline.high.values
        low = self.kline.low.values
        close = self.kline.close.values
        volume = self.kline.volume.values
        n = len(high)

        prd = self.params.prd
        base_apt = self.params.base_apt
        use_adapt = self.params.use_adapt
        vol_bias = self.params.vol_bias

        # ================================================================
        # 1. Swing 检测
        # ================================================================
        hb = self._highestbars(high, prd)
        lb = self._lowestbars(low, prd)

        is_swing_high = (hb == 0.0)
        is_swing_low = (lb == 0.0)

        # 前向填充 swing 价格和 bar 索引（与 Pine var 行为一致）
        ph_val = np.nan
        pl_val = np.nan
        phL_val = -1.0
        plL_val = -1.0

        ph = np.full(n, np.nan)
        pl = np.full(n, np.nan)
        phL = np.full(n, -1.0)
        plL = np.full(n, -1.0)

        for i in range(n):
            if is_swing_high[i]:
                ph_val = high[i]
                phL_val = float(i)
            if is_swing_low[i]:
                pl_val = low[i]
                plL_val = float(i)
            ph[i] = ph_val
            pl[i] = pl_val
            phL[i] = phL_val
            plL[i] = plL_val

        dir_ = np.where(phL > plL, 1, -1)

        # ================================================================
        # 2. 自适应 APT（ATR 波动率调节）
        # ================================================================
        tr = self.true_range()
        atr = tr.rma(50)
        atr_avg = atr.rma(50)
        ratio = np.where(atr_avg > 0, atr / np.maximum(atr_avg, 1e-12), 1.0)

        if use_adapt:
            apt_raw = base_apt / np.power(ratio, vol_bias)
        else:
            apt_raw = np.full(n, base_apt)

        apt = np.clip(np.round(apt_raw), 5.0, 300.0)

        # ================================================================
        # 3. EWMA 型 Swing 锚定 VWAP
        # ================================================================
        hlc3 = (high + low + close) / 3.0
        dsav = self._compute_vwap(hlc3, volume, dir_, ph, pl, phL, plL, apt, n)

        self.lines.dsav = dsav

    # ------------------------------------------------------------------
    @staticmethod
    def _compute_vwap(hlc3, volume, dir_, ph, pl, phL, plL, apt, n):
        """EWMA VWAP：方向变化时从 swing 点重新锚定"""
        p_acc = 0.0
        vol_acc = 0.0
        vwap = np.full(n, np.nan)
        log2 = np.log(2.0)

        for i in range(n):
            if i > 0 and dir_[i] != dir_[i - 1]:
                # ── 方向变化 ──
                if dir_[i] > 0:
                    x = int(plL[i])
                    y = pl[i]
                else:
                    x = int(phL[i])
                    y = ph[i]

                if x < 0 or np.isnan(y) or x > i:
                    continue

                # 初始化锚点
                p_acc = y * volume[x]
                vol_acc = volume[x]

                # 从 swing 点到当前 bar 正向递推
                for j in range(x, i + 1):
                    ap = max(apt[j], 1.0)
                    alpha = 1.0 - np.exp(-log2 / ap)
                    pxv = hlc3[j] * volume[j]
                    v_j = volume[j]
                    p_acc = (1.0 - alpha) * p_acc + alpha * pxv
                    vol_acc = (1.0 - alpha) * vol_acc + alpha * v_j
                    if vol_acc > 0:
                        vwap[j] = p_acc / vol_acc
            else:
                # ── 方向未变：增量更新 ──
                ap = max(apt[i], 1.0) if not np.isnan(apt[i]) else 1.0
                alpha = 1.0 - np.exp(-log2 / ap)
                pxv = hlc3[i] * volume[i]
                v_i = volume[i]
                p_acc = (1.0 - alpha) * p_acc + alpha * pxv
                vol_acc = (1.0 - alpha) * vol_acc + alpha * v_i
                if vol_acc > 0:
                    vwap[i] = p_acc / vol_acc

        return vwap


class DynamicLinearRegressionChannels(BtIndicator):
    """
    动态线性回归通道 — 自适应分段回归趋势通道
    基于 Pine Script "Dynamic Linear Regression Channels" 转换
    https://cn.tradingview.com/script/IPdDUsgl-Dynamic-Linear-Regression-Channels/

    原理：
      对收盘价做线性回归拟合出基线，以上下轨包住段内行情的高低点。

      通道构建方式（参考趋势线逻辑）：
        - baseline = 对收盘价的线性回归线（段内最佳拟合直线）
        - upper = baseline + upper_mult × upDev
          upDev = 段内最高价相对回归线的最大正偏离 → 上轨贴近高点
        - lower = baseline - lower_mult × dnDev
          dnDev = 段内回归线相对最低价的最大正偏离 → 下轨贴近低点
        - upper_mult / lower_mult = 1.0 时通道刚好包住该段全部价格区间

      核心亮点在于"动态分段"机制：
        - 从起始 bar 开始，随新 bar 不断更新回归通道
        - 一旦收盘价突破通道上轨或下轨，认为趋势结构变化，该段结束
        - 以突破 bar 为起点重新开始新的回归段
        - 每个段都有独立的斜率和通道宽度
        - 同段内三条线共享同一斜率，保证相互平行（直线段）

      计算流程：
        1. 从 start_index 到当前 bar 做线性回归 → slope, intercept
        2. 计算 upDev（最高价 - 回归线 的最大值）
        3. 计算 dnDev（回归线 - 最低价 的最大值）
        4. 上轨 = 回归线 + upper_mult × upDev
        5. 下轨 = 回归线 - lower_mult × dnDev
        6. 若收盘价突破上/下轨，用当前段回归参数填充整段，重置 start_index

    输出线：
      base_line — 线性回归基线
      upper     — 上轨（贴近段内高点）
      lower     — 下轨（贴近段内低点）

    参数：
      upper_mult — 上轨绘制宽度乘数，默认 1.0（=1 时上轨刚好包住最高价）
      lower_mult — 下轨绘制宽度乘数，默认 1.0（=1 时下轨刚好包住最低价）
      break_mult — 突破检测宽度乘数（基于 StdDev），默认 2.0
                   控制分段的敏感度：越小越容易分段，越大分段越少
    """
    lines = ('base_line', 'upper', 'lower')
    params = dict(
        upper_mult=1.0,
        lower_mult=1.0,
        break_mult=2.0,
    )
    overlap = True
    
    @staticmethod
    def _linear_regression(x: np.ndarray) -> tuple:
        """
        对等间距数据做线性回归，拟合 y = slope × per + intercept。

        per 从 1（段最旧 bar）到 n（段最新 bar），与 Pine 方向一致。

        Args:
            x: 输入数组（收盘价序列，index 0 = 段内最旧 bar）

        Returns:
            (slope, average, intercept)
            slope    — 回归斜率（每 bar 价格变化量）
            average  — 收盘价均值
            intercept — 回归线在 per=0 处的截距（非任意 bar 的实际值）
        """
        n = len(x)
        per = np.arange(1, n + 1, dtype=np.float64)
        sum_x = np.sum(per)
        sum_y = np.sum(x)
        sum_xsqr = np.sum(per * per)
        sum_xy = np.sum(x * per)

        denom = n * sum_xsqr - sum_x * sum_x
        if denom == 0:
            return 0.0, x[0] if n > 0 else np.nan, x[0] if n > 0 else np.nan

        slope = (n * sum_xy - sum_x * sum_y) / denom
        average = sum_y / n
        intercept = average - slope * sum_x / n + slope
        return float(slope), float(average), float(intercept)

    @staticmethod
    def _calc_deviation(close_seg: np.ndarray, high_seg: np.ndarray,
                        low_seg: np.ndarray, slope: float, average: float,
                        intercept: float) -> tuple:
        """
        计算回归通道的偏差统计量。

        回归线: y = slope × per + intercept, per = 1..n (1=最旧 bar, n=最新 bar)

        Args:
            close_seg: 段内收盘价（index 0 = 最旧 bar）
            high_seg:  段内最高价
            low_seg:   段内最低价
            slope:     回归斜率
            average:   收盘价均值
            intercept: 回归截距

        Returns:
            (std_dev, pearson_r, up_dev, dn_dev)
            std_dev   — 收盘价与回归线偏差的均方根
            pearson_r — Pearson 相关系数
            up_dev    — 最高价超过回归线的最大距离（通道上半宽度）
            dn_dev    — 回归线超过最低价的最大距离（通道下半宽度）
        """
        n = len(close_seg)
        periods = n - 1
        if periods <= 0:
            return 0.0, 0.0, 0.0, 0.0

        # 回归线在段内每个位置的值（per 从 1 到 n，与 _linear_regression 一致）
        per = np.arange(1, n + 1, dtype=np.float64)
        val_arr = slope * per + intercept

        # 最高/最低价偏离回归线（通道半宽：保证该段价格被上下轨包住）
        up_dev = float(np.max(high_seg - val_arr))
        dn_dev = float(np.max(val_arr - low_seg))

        # 标准差（收盘价 vs 回归线）
        price_diff = close_seg - val_arr
        std_dev = float(np.sqrt(np.sum(price_diff * price_diff) / periods))

        # Pearson R
        da_y = intercept + slope * periods / 2
        dxt = close_seg - average
        dyt = val_arr - da_y
        dsxx = float(np.sum(dxt * dxt))
        dsyy = float(np.sum(dyt * dyt))
        dsxy = float(np.sum(dxt * dyt))
        pearson_r = 0.0 if dsxx == 0 or dsyy == 0 else float(dsxy / np.sqrt(dsxx * dsyy))

        return std_dev, pearson_r, up_dev, dn_dev

    def __init__(self):
        close = np.asarray(self.close, dtype=np.float64)
        high = np.asarray(self.high, dtype=np.float64)
        low = np.asarray(self.low, dtype=np.float64)
        n = len(close)

        upper_mult = self.params.upper_mult
        lower_mult = self.params.lower_mult
        break_mult = self.params.break_mult

        base_line = np.full(n, np.nan, dtype=np.float64)
        upper = np.full(n, np.nan, dtype=np.float64)
        lower = np.full(n, np.nan, dtype=np.float64)

        start_index = 0

        for i in range(1, n):
            seg_len = i - start_index + 1
            if seg_len <= 1:
                continue

            c_seg = close[start_index : i + 1]
            h_seg = high[start_index : i + 1]
            l_seg = low[start_index : i + 1]

            slope, average, intercept = self._linear_regression(c_seg)
            std_dev, pearson_r, up_dev, dn_dev = self._calc_deviation(
                c_seg, h_seg, l_seg, slope, average, intercept
            )

            # 最新 bar 处的回归值（per = seg_len）
            bl_end = slope * seg_len + intercept

            # 突破检测使用 std_dev（Pine 原版逻辑），避免通道过宽无法分段
            upper_end = bl_end + break_mult * std_dev
            lower_end = bl_end - break_mult * std_dev

            # 突破 → 段结束，绘制通道
            # 取上下距离最小值，保证上/下轨到基线等距（Pine 原版对称通道）
            if close[i] > upper_end or close[i] < lower_end:
                half_width = min(upper_mult * up_dev, lower_mult * dn_dev)
                for j in range(start_index, i + 1):
                    seg_pos = j - start_index  # 0-indexed
                    per = seg_pos + 1  # 1=最旧, seg_len=最新
                    bl = slope * per + intercept
                    base_line[j] = bl
                    upper[j] = bl + half_width
                    lower[j] = bl - half_width
                start_index = i

        # 最后一个未完成的段
        if start_index < n - 1:
            c_seg = close[start_index:]
            h_seg = high[start_index:]
            l_seg = low[start_index:]
            slope, average, intercept = self._linear_regression(c_seg)
            std_dev, pearson_r, up_dev, dn_dev = self._calc_deviation(
                c_seg, h_seg, l_seg, slope, average, intercept
            )
            half_width = min(upper_mult * up_dev, lower_mult * dn_dev)
            seg_len = n - start_index
            for j in range(start_index, n):
                seg_pos = j - start_index
                per = seg_pos + 1
                bl = slope * per + intercept
                base_line[j] = bl
                upper[j] = bl + half_width
                lower[j] = bl - half_width

        self.lines.base_line = base_line
        self.lines.upper = upper
        self.lines.lower = lower
        
class IGHSupertrend(BtIndicator):
    """
    Innovation-Gated Hull Supertrend — 创新门控 Hull 超级趋势
    https://cn.tradingview.com/script/tq9f0qwr-Innovation-Gated-Hull-Supertrend-BackQuant/

    参数:
    - hull_length:           Hull MA 周期，默认 55
    - volatility_length:     波动率周期，默认 21
    - volatility_mode:       波动率模型: ATR / StdDev / Blend，默认 Blend
    - measurement_noise:     测量噪声，默认 4.0
    - process_noise:         过程噪声，默认 0.03
    - innovation_threshold:  创新阈值，默认 0.80
    - gate_sharpness:        门控锐度，默认 4.0
    - admission_floor:       准入下限，默认 0.12
    - process_boost:         过程增益，默认 4.0
    - atr_period:            Supertrend ATR 周期，默认 12
    - factor:                Supertrend 因子，默认 1.70
    - adapt_bands:           是否用创新度自适应带，默认 True
    - quiet_band_expansion:  平静期带宽扩展，默认 0.35
    
    Returns:
    - super_trend: 超级趋势值
    - direction: 趋势方向，1 表示上趋势，-1 表示下趋势，0 表示无趋势
    - long_signal: 长信号，1 表示长趋势，0 表示无长趋势
    - short_signal: 短信号，1 表示短趋势，0 表示无短趋势
    """
    lines = ('super_trend', 'direction', 'long_signal', 'short_signal')
    isplot = dict(long_signal=False, short_signal=False, direction=False)
    params = dict(
        hull_length=55,
        volatility_length=21,
        volatility_mode='Blend',
        measurement_noise=4.0,
        process_noise=0.03,
        innovation_threshold=0.80,
        gate_sharpness=4.0,
        admission_floor=0.12,
        process_boost=4.0,
        atr_period=12,
        factor=1.70,
        adapt_bands=True,
        quiet_band_expansion=0.35,
    )
    overlap = True

    def __init__(self):
        close = self.kline.close.values
        n = len(close)
        mintick = 1e-10  # 替代 syminfo.mintick

        p = self.params
        hull_len = max(p.hull_length, 1)
        vol_len = max(p.volatility_length, 1)
        atr_period = max(p.atr_period, 1)

        # ================================================================
        # 1. Hull Projection
        #    half = round(n/2), root = round(sqrt(n))
        #    hull = WMA(2*WMA(src, half) - WMA(src, n), root)
        # ================================================================
        half_len = max(round(hull_len * 0.5), 1)
        root_len = max(round(np.sqrt(hull_len)), 1)
        fast_wma = self.close.wma(half_len)#_wma(close, half_len)
        slow_wma = self.close.wma(hull_len)
        raw_hull = 2.0 * fast_wma - slow_wma
        hull = raw_hull.wma(root_len)#_wma(raw_hull, root_len)

        # ================================================================
        # 2. Volatility Base
        # ================================================================
        atr_val = self.atr(vol_len)#_atr(high, low, close, vol_len)
        stdev_val = hull.stdev(vol_len)
        atr_val = np.nan_to_num(atr_val, nan=0.0)
        stdev_val = np.nan_to_num(stdev_val, nan=0.0)

        mode = p.volatility_mode
        if mode == 'ATR':
            volatility = atr_val
        elif mode == 'StdDev':
            volatility = stdev_val
        else:  # Blend
            volatility = (atr_val + stdev_val) * 0.5
        volatility = np.maximum(volatility, mintick)

        # ================================================================
        # 3. VNIF 创新门控滤波器
        #    类卡尔曼滤波，使用 Sigmoid 门控动态调整过程/测量噪声
        # ================================================================
        meas_noise = max(p.measurement_noise, mintick)
        proc_noise = max(p.process_noise, mintick)
        threshold = max(p.innovation_threshold, mintick)
        sharpness = max(p.gate_sharpness, mintick)
        floor = np.clip(p.admission_floor, mintick, 1.0)
        boost = max(p.process_boost, 0.0)

        estimate = np.full(n, np.nan)
        covariance_arr = np.full(n, np.nan)
        gate_arr = np.full(n, np.nan)
        score_arr = np.full(n, np.nan)

        for i in range(n):
            # 预测
            pred = estimate[i - 1] if i > 0 and not np.isnan(estimate[i - 1]) else hull[i]
            prior_cov = covariance_arr[i - 1] if i > 0 and not np.isnan(covariance_arr[i - 1]) else 1.0

            # 创新
            innov = hull[i] - pred if not np.isnan(hull[i]) and not np.isnan(pred) else 0.0
            score = abs(innov) / max(volatility[i], mintick)

            # Sigmoid 门控
            gate_input = np.clip(sharpness * (score - threshold), -60.0, 60.0)
            gate = 1.0 / (1.0 + np.exp(-gate_input))

            # 准入度
            admission = floor + (1.0 - floor) * gate

            # 自适应噪声
            adaptive_meas = meas_noise / max(admission, mintick)
            adaptive_proc = proc_noise * (1.0 + boost * gate)

            # 卡尔曼更新
            pred_cov = prior_cov + adaptive_proc
            gain = pred_cov / (pred_cov + adaptive_meas)

            if not np.isnan(hull[i]):
                estimate[i] = pred + gain * innov
                covariance_arr[i] = (1.0 - gain) * pred_cov

            gate_arr[i] = gate
            score_arr[i] = score

        filtered_hull = estimate

        # ================================================================
        # 4. 自适应 Supertrend 因子
        #    band_factor = factor * (1 + expansion * (1 - gate))
        # ================================================================
        base_factor = max(p.factor, mintick)
        expansion = max(p.quiet_band_expansion, 0.0)
        gate_filled = np.nan_to_num(gate_arr, nan=0.0)

        if p.adapt_bands:
            st_factor = base_factor * (1.0 + expansion * (1.0 - gate_filled))
        else:
            st_factor = np.full(n, base_factor)

        # ================================================================
        # 5. Supertrend 计算
        #    标准 supertrend 算法，以 filtered_hull 为源
        #    注意：Pine Script 中 upper_band/lower_band 是可变 series，
        #    upper_band[1] 取的是前 bar 修正后的值，而非原始值
        # ================================================================
        st_atr = self.atr(atr_period)#_atr(high, low, close, atr_period)
        upper_band_raw = filtered_hull + st_factor * st_atr
        lower_band_raw = filtered_hull - st_factor * st_atr

        # 存储修正后的最终值
        final_upper = np.full(n, np.nan)
        final_lower = np.full(n, np.nan)
        super_trend = np.full(n, np.nan)
        direction = np.full(n, 0, dtype=int)

        for i in range(n):
            if np.isnan(st_atr[i]) or np.isnan(filtered_hull[i]):
                direction[i] = 1
                continue

            # 取前 bar 修正后的最终值（匹配 Pine Script upper_band[1] / lower_band[1]）
            if i == 0:
                prev_lb = lower_band_raw[i]
                prev_ub = upper_band_raw[i]
            else:
                prev_lb = (final_lower[i - 1] if not np.isnan(final_lower[i - 1])
                           else lower_band_raw[i])
                prev_ub = (final_upper[i - 1] if not np.isnan(final_upper[i - 1])
                           else upper_band_raw[i])

            # 记忆效应：下轨只上移，上轨只下移
            lb = (lower_band_raw[i]
                  if lower_band_raw[i] > prev_lb or close[i - 1] < prev_lb
                  else prev_lb)
            ub = (upper_band_raw[i]
                  if upper_band_raw[i] < prev_ub or close[i - 1] > prev_ub
                  else prev_ub)

            final_lower[i] = lb
            final_upper[i] = ub

            # 方向判断
            prev_st = super_trend[i - 1] if i > 0 else np.nan

            if i == 0 or np.isnan(prev_st):
                direction[i] = -1  # 默认多头
            elif np.isclose(prev_st, prev_ub, atol=1e-10):
                # 前一次在 upper_band → 空头中；close > ub 则翻多
                direction[i] = -1 if close[i] > ub else 1
            else:
                # 前一次在 lower_band → 多头中；close < lb 则翻空
                direction[i] = 1 if close[i] < lb else -1

            # 使用修正后的上下轨
            super_trend[i] = lb if direction[i] == -1 else ub

        # ================================================================
        # 6. 交易信号
        #    long_signal  = crossunder(direction, 0)  → dir 由 1 → -1
        #    short_signal = crossover(direction, 0)   → dir 由 -1 → 1
        #    (direction: -1=多头, 1=空头)
        # ================================================================
        prev_dir = np.roll(direction, 1)
        prev_dir[0] = 0

        long_raw = (direction == -1) & (prev_dir == 1)
        short_raw = (direction == 1) & (prev_dir == -1)

        self.lines.super_trend = super_trend
        self.lines.direction = direction.astype(float)
        self.lines.long_signal = np.where(long_raw, 1.0, np.nan)
        self.lines.short_signal = np.where(short_raw, 1.0, np.nan)
        
class LPTT(BtIndicator):
    """
    LPTT — 低阶多项式拟合趋势交易指标

    原理：
      对价格时间序列做一阶和二阶多项式拟合，提取"趋势方向与强度"（f'）
      和"趋势加速度"（g''），通过两者的正负组合判定当前处于加速/减速
      上涨/下跌四种趋势形态中的哪一种。只在加速阶段（f' × g'' > 0）入场，
      趋势衰减或反转时离场。

      四种趋势形态：
        - 加速上涨：f' > 0 且 g'' > 0 → 顺势做多
        - 加速下跌：f' < 0 且 g'' < 0 → 顺势做空
        - 减速上涨：f' > 0 且 g'' < 0 → 不交易（反转风险高）
        - 减速下跌：f' < 0 且 g'' > 0 → 不交易（反转风险高）

      计算流程：
        1. 取最近 length 根收盘价，一阶 polyfit → f'（趋势斜率）
        2. 取最近 length 根收盘价，二阶 polyfit → g'' = 2a₂（趋势加速度）
        3. f' × g'' > 0 → 顺势信号（加速趋势）
        4. f' 或 g'' 变号 → 离场信号
    
    参数：
      length — 多项式拟合窗口长度，默认 20

    输出线：
      f_prime          — 一阶导数（趋势方向与强度）
      g_double_prime   — 二阶导数（趋势加速度）
      long_signal      — 加速上涨信号（f' > 0 且 g'' > 0）
      short_signal     — 加速下跌信号（f' < 0 且 g'' < 0）

    参数：
      length  — 多项式拟合窗口长度，默认 20
    """
    lines = ('f_prime', 'g_double_prime',
             'long_signal', 'short_signal', 'exitlong_signal', 'exitshort_signal')
    params = dict(length=20)
    overlap = False
    isplot = dict(long_signal=False, short_signal=False, exitlong_signal=False, exitshort_signal=False)
    
    @staticmethod
    def _compute_lptt(close: np.ndarray, length: int):
      """
      计算 LPTT 指标。

      对每根 bar，取其前 length 根收盘价做多项式拟合：
        - 一阶（线性）拟合 → f' = 斜率 a₁
        - 二阶（二次）拟合 → g'' = 2 × 二次项系数 a₂
        
      参数：
        close  — 收盘价时间序列
        length — 多项式拟合窗口长度

      Returns:
          f_prime          — 一阶导数（趋势斜率）
          g_double_prime   — 二阶导数（趋势加速度）
          long_signal      — 加速上涨信号
          short_signal     — 加速下跌信号
          exit_long        — 多头离场信号（f' 或 g'' 变号）
          exit_short       — 空头离场信号
      """
      n = len(close)
      f_prime = np.full(n, np.nan, dtype=np.float64)
      g_double_prime = np.full(n, np.nan, dtype=np.float64)
      long_signal = np.zeros(n, dtype=np.int32)
      short_signal = np.zeros(n, dtype=np.int32)
      exit_long = np.zeros(n, dtype=np.int32)
      exit_short = np.zeros(n, dtype=np.int32)

      if n < length:
          return f_prime, g_double_prime, long_signal, short_signal, exit_long, exit_short

      x_linear = np.arange(length, dtype=np.float64)
      x_quad = np.arange(length, dtype=np.float64)

      for i in range(length - 1, n):
          y = close[i - length + 1 : i + 1]

          # 一阶（线性）拟合：y = a₁·x + b₁
          a1, _ = np.polyfit(x_linear, y, 1)
          f_prime[i] = a1

          # 二阶（二次）拟合：y = a₂·x² + b₂·x + c₂
          a2, _, _ = np.polyfit(x_quad, y, 2)
          g_double_prime[i] = 2.0 * a2

          # 信号
          fp = f_prime[i]
          gpp = g_double_prime[i]
          if not np.isfinite(fp) or not np.isfinite(gpp):
              continue

          # 入场：f' × g'' > 0
          if fp > 0 and gpp > 0:
              long_signal[i] = 1
          elif fp < 0 and gpp < 0:
              short_signal[i] = 1

      # 离场信号：f' 或 g'' 变号
      for i in range(length, n):
          if not np.isfinite(f_prime[i]) or not np.isfinite(g_double_prime[i]):
              continue
          if not np.isfinite(f_prime[i - 1]) or not np.isfinite(g_double_prime[i - 1]):
              continue

          # f' 变号
          fp_sign = np.sign(f_prime[i])
          fp_prev_sign = np.sign(f_prime[i - 1])
          if fp_sign != fp_prev_sign and fp_prev_sign != 0:
              exit_long[i] = 1
              exit_short[i] = 1

          # g'' 变号
          gpp_sign = np.sign(g_double_prime[i])
          gpp_prev_sign = np.sign(g_double_prime[i - 1])
          if gpp_sign != gpp_prev_sign and gpp_prev_sign != 0:
              exit_long[i] = 1
              exit_short[i] = 1

      return f_prime, g_double_prime, long_signal, short_signal, exit_long, exit_short

    def __init__(self):
        close = np.asarray(self.close.values, dtype=np.float64)

        f_prime, g_double_prime, long_signal, short_signal, exit_long, exit_short = \
            self._compute_lptt(close, self.params.length)

        self.lines.f_prime = f_prime
        self.lines.g_double_prime = g_double_prime
        self.lines.long_signal = long_signal
        self.lines.short_signal = short_signal
        self.lines.exitlong_signal = exit_long
        self.lines.exitshort_signal = exit_short
        
        
class MLAdaptiveSuperTrend(BtIndicator):
    """
    机器学习自适应 SuperTrend — 基于 K-Means 聚类动态调整 ATR
    https://cn.tradingview.com/script/CLk71Qgy-Machine-Learning-Adaptive-SuperTrend-AlgoAlpha/

    原理：
      标准 SuperTrend 使用固定周期的 ATR 作为波动率度量，在高/低波动时期
      可能过于迟钝或过于敏感。本指标通过 K-Means 聚类将历史 ATR 分为
      高/中/低三个波动率状态，用当前 bar 所属簇的质心值替换固定 ATR，
      实现 SuperTrend 的自适应调整。

      计算流程：
        1. 用 Wilder's RMA 计算 atr_len 周期的 ATR
        2. 对每根 bar，取过去 training_data_period 根 ATR 做 3-Means 聚类
        3. 将当前 bar 的 ATR 归入最近的簇，取该簇质心作为自适应 ATR
        4. 用自适应 ATR 计算 Pine Script 风格 SuperTrend

    输出线：
      ST      — 自适应 SuperTrend 线，叠加主图，牛市在价格下方、熊市在上方
      dir     — 方向信号：+1 = 多头趋势（ST 在价格下方），-1 = 空头趋势（ST 在价格上方）
      cluster — 波动率聚类标签：0 = 高波动，1 = 中波动，2 = 低波动

    参数：
      atr_len              — ATR 计算周期，默认 10
      fact                 — SuperTrend 乘数因子，默认 3.0
      training_data_period — K-Means 训练窗口长度（bar 数），默认 100
      highvol              — 高波动初始质心百分位，默认 0.75（窗口 ATR 第 75 百分位）
      midvol               — 中波动初始质心百分位，默认 0.50
      lowvol               — 低波动初始质心百分位，默认 0.25
    """
    lines = ('ST', 'dir', 'cluster')
    params = dict(
        atr_len=10,
        fact=3.0,
        training_data_period=100,
        highvol=0.75,
        midvol=0.50,
        lowvol=0.25,
    )
    overlap = dict(ST=True,dir=False,cluster=False)
    

    def __init__(self):
        close = np.asarray(self.close, dtype=np.float64)
        high = np.asarray(self.high, dtype=np.float64)
        low = np.asarray(self.low, dtype=np.float64)
        n = len(close)

        # 1. 计算 ATR（Wilder's RMA）
        atr = self.atr(self.params.atr_len).backfill().values

        # 2. K-Means 聚类 + 自适应 ATR
        adaptive_atr, cluster = self._compute_kmeans(atr, n)

        # 3. 计算自适应 SuperTrend
        hl2 = (high + low) / 2.0
        st, direction = self._pine_supertrend(hl2, close, adaptive_atr, self.params.fact)

        self.lines.ST = st
        self.lines.dir = direction
        self.lines.cluster = cluster

    def _compute_kmeans(self, atr, n):
        """
        逐 bar K-Means 聚类 ATR。

        对每根 bar（从 training_data_period-1 开始），取过去 training_data_period
        根 ATR 做 3-Means 聚类：
          1. 初始质心 = 窗口 ATR 最小/最大值之间的百分位点
          2. 迭代分配-更新，直到三个质心全部收敛
          3. 将当前 bar 的 ATR 归入最近的簇

        Returns:
          adaptive_atr — 形状 (n,)，前 training_data_period-1 根为 NaN
          cluster      — 形状 (n,)，0=高波动, 1=中波动, 2=低波动，前 training_data_period-1 根为 -1
        """
        tp = self.params.training_data_period
        adaptive_atr = np.full(n, np.nan, dtype=np.float64)
        cluster = np.full(n, -1, dtype=np.float64)

        for i in range(tp - 1, n):
            window = atr[i - tp + 1 : i + 1]
            lo = np.min(window)
            hi = np.max(window)
            rng = hi - lo

            # 初始质心（百分位猜测）
            c_high = lo + rng * self.params.highvol
            c_mid = lo + rng * self.params.midvol
            c_low = lo + rng * self.params.lowvol

            # K-Means 迭代至收敛
            while True:
                d_high = np.abs(window - c_high)
                d_mid = np.abs(window - c_mid)
                d_low = np.abs(window - c_low)

                mask_high = (d_high <= d_mid) & (d_high <= d_low)
                mask_mid = (d_mid < d_high) & (d_mid <= d_low)
                mask_low = (d_low < d_high) & (d_low < d_mid)

                nc_high = float(np.mean(window[mask_high])) if mask_high.any() else c_high
                nc_mid = float(np.mean(window[mask_mid])) if mask_mid.any() else c_mid
                nc_low = float(np.mean(window[mask_low])) if mask_low.any() else c_low

                if nc_high == c_high and nc_mid == c_mid and nc_low == c_low:
                    c_high, c_mid, c_low = nc_high, nc_mid, nc_low
                    break
                c_high, c_mid, c_low = nc_high, nc_mid, nc_low

            # 分类当前 bar 的 ATR
            cur = atr[i]
            d_high = abs(cur - c_high)
            d_mid = abs(cur - c_mid)
            d_low = abs(cur - c_low)

            if d_high <= d_mid and d_high <= d_low:
                cluster[i] = 0.0   # 高波动
                adaptive_atr[i] = c_high
            elif d_mid <= d_high and d_mid <= d_low:
                cluster[i] = 1.0   # 中波动
                adaptive_atr[i] = c_mid
            else:
                cluster[i] = 2.0   # 低波动
                adaptive_atr[i] = c_low

        return adaptive_atr, cluster

    @staticmethod
    def _pine_supertrend(hl2, close, atr_vals, factor):
        """
        Pine Script 风格 SuperTrend 计算。

        与标准 SuperTrend 一致：
          - 通道 = hl2 ± factor * atr，通道只扩张不收缩（防止穿越）
          - 方向翻转条件：牛市时 close 突破上轨翻空，熊市时 close 跌破下轨翻多
          - ST 值 = 牛市取上轨，熊市取下轨
          - 早期 atr_vals 为 NaN 时默认方向为多头
        """
        n = len(hl2)
        upper_band = hl2 + factor * atr_vals
        lower_band = hl2 - factor * atr_vals
        direction = np.zeros(n, dtype=np.float64)
        st = np.zeros(n, dtype=np.float64)

        for i in range(n):
            # 调整通道防止穿越（同标准 SuperTrend）
            if i > 0:
                prev_lb = lower_band[i - 1] if np.isfinite(lower_band[i - 1]) else 0.0
                prev_ub = upper_band[i - 1] if np.isfinite(upper_band[i - 1]) else 0.0
                if not (lower_band[i] > prev_lb or close[i - 1] < prev_lb):
                    lower_band[i] = prev_lb
                if not (upper_band[i] < prev_ub or close[i - 1] > prev_ub):
                    upper_band[i] = prev_ub

            # 方向判断
            if i == 0 or np.isnan(atr_vals[i - 1]):
                direction[i] = 1.0
            elif st[i - 1] == upper_band[i - 1]:
                direction[i] = -1.0 if close[i] > upper_band[i] else 1.0
            else:
                direction[i] = 1.0 if close[i] < lower_band[i] else -1.0

            # SuperTrend 值
            st[i] = lower_band[i] if direction[i] == -1.0 else upper_band[i]

        return st, direction
    
    
class NRTR(BtIndicator):
    """
    Nick Rypock 动态反转止损线 (NRTR)
    https://cn.tradingview.com/script/XAscppNW-Nick-Rypock-Trailing-Reverse-NRTR/

    原理：
      NRTR 是一种基于百分比的自适应跟踪止损指标。它根据当前趋势方向，
      在最高价（上升趋势）或最低价（下降趋势）的基础上加减一个百分比
      系数来生成止损线。

      状态机逻辑：
        - 上升趋势中：不断追踪更高最高价，止损线 = 最高价 × (1 - k)
        - 价格跌破止损线 → 趋势反转，进入下降趋势
        - 下降趋势中：不断追踪更低最低价，止损线 = 最低价 × (1 + k)
        - 价格突破止损线 → 趋势反转，进入上升趋势

      该指标同时输出买卖信号：趋势从 -1 翻转到 1 为买入信号，
      从 1 翻转到 -1 为卖出信号。

      计算流程：
        1. trend=1 时：hp = max(hp, close)，nrtr = hp × (1 - k)
        2. 若 close ≤ nrtr：trend = -1，lp = close，nrtr = lp × (1 + k)
        3. trend=-1 时：lp = min(lp, close)，nrtr = lp × (1 + k)
        4. 若 close ≥ nrtr：trend = 1，hp = close，nrtr = hp × (1 - k)

    输出线：
      long_stop  — 多头止损线（上升趋势时有效，下降趋势时为 NaN）
      short_stop — 空头止损线（下降趋势时有效，上升趋势时为 NaN）
      buy_signal — 买入信号（趋势从 -1 翻转到 1，值为 1）
      sell_signal — 卖出信号（趋势从 1 翻转到 -1，值为 -1）

    参数：
      percentage — 修正系数百分比，默认 1（即 1%）
      mult — 修正系数，默认 0（不修正）
      length — 计算窗口，默认 20（20日移动平均）
    NOTE:
      - 当 mult 为非零正数时，止损线会根据移动平均线进行修正。
        
    """
    lines = ('long_stop', 'short_stop', 'buy_signal', 'sell_signal')
    params = dict(
        percentage=1.0,
        mult=0.,
        length=20,
    )
    overlap = True
    isplot = dict( buy_signal=False, sell_signal=False)

    def __init__(self):
        close = self.close.values
        n = len(close)
        k = np.full(n, self.params.percentage / 100.0)
        mult = self.params.mult
        if isinstance(mult, (int, float)) and mult>0.:
            k = self.close.stdev(self.params.length) * mult/self.close.sma(self.params.length)

        long_stop = np.full(n, np.nan, dtype=np.float64)
        short_stop = np.full(n, np.nan, dtype=np.float64)
        buy_signal = np.zeros(n, dtype=np.int32)
        sell_signal = np.zeros(n, dtype=np.int32)

        trend = 0  # 0=初始, 1=上升, -1=下降
        hp = close[self.params.length]  # 上升趋势追踪的最高价
        lp = close[self.params.length]  # 下降趋势追踪的最低价
        nrtr = close[self.params.length]

        for i in range(self.params.length, n):
            c = close[i]

            if trend >= 0:
                # 上升趋势：追踪更高最高价
                if c > hp:
                    hp = c
                nrtr = hp * (1.0 - k[i])
                # 收盘价跌破止损线 → 翻转为下降
                if c <= nrtr:
                    trend = -1
                    lp = c
                    nrtr = lp * (1.0 + k[i])
                    sell_signal[i] = -1
            else:
                # 下降趋势：追踪更低价
                if c < lp:
                    lp = c
                nrtr = lp * (1.0 + k[i])
                # 收盘价突破止损线 → 翻转为上升
                if c >= nrtr:
                    trend = 1
                    hp = c
                    nrtr = hp * (1.0 - k[i])
                    buy_signal[i] = 1

            # 按趋势分别填充止损线
            if trend == 1:
              if long_stop[i-1] > 0:
                long_stop[i] = max(nrtr,long_stop[i-1])
              else:
                long_stop[i] = nrtr
            else:
              if short_stop[i-1] > 0:
                short_stop[i] = min(nrtr,short_stop[i-1])
              else:
                short_stop[i] = nrtr

        self.lines.long_stop = long_stop
        self.lines.short_stop = short_stop
        self.lines.long_signal = buy_signal
        self.lines.short_signal = sell_signal


class NWEEnvelope(BtIndicator):
    """
    Nadaraya-Watson Envelope — 高斯核回归包络带
    https://cn.tradingview.com/script/Iko0E2kL-Nadaraya-Watson-Envelope-LuxAlgo/

    参数:
    - h:          带宽，控制平滑度（越大越平滑），默认 8.0
    - mult:       包络带乘数，默认 3.0
    - lookback:   回看窗口，默认 500
    - src_type:   价格源，可选 close/hl2/hlc3/ohlc4，默认 close
    
    输出线：
      nw_line    — 高斯核回归包络线
      upper      — 上轨
      lower      — 下轨
      long_signal — 多头信号（趋势从 -1 翻转到 1，值为 1）
      short_signal — 空头信号（趋势从 1 翻转到 -1，值为 -1）
    """
    lines = ('nw_line', 'upper', 'lower', 'long_signal', 'short_signal')
    isplot = dict(long_signal=False, short_signal=False)
    params = dict(h=8.0, mult=3.0, lookback=500, src_type='close')
    overlap = True
    
    @staticmethod
    def _gauss(x: float, h: float) -> float:
        """高斯核函数: exp(-x² / (2 * h²))"""
        return np.exp(-(x * x) / (2.0 * h * h))

    def __init__(self):
        src = self._get_source()
        n = len(src)
        h = self.params.h
        mult = self.params.mult
        lookback = min(self.params.lookback, n)

        # ================================================================
        # 1. 预计算高斯核系数
        #    coefs[i] = gauss(i, h)  for i = 0..lookback-1
        #    (i=0 对应当前 bar，权重最高 = 1)
        # ================================================================
        coefs = np.array([self._gauss(i, h) for i in range(lookback)])

        # ================================================================
        # 2. NW 加权均值 (端点法 / 非重绘)
        #    out[i] = sum_{j=0}^{k} src[i-j] * coefs[j] / sum_{j=0}^{k} coefs[j]
        #    k = min(lookback-1, i)
        # ================================================================
        nw_line = np.full(n, np.nan)
        for i in range(n):
            win = min(lookback, i + 1)
            seg = src[i - win + 1 : i + 1][::-1]  # 反转匹配 coefs 顺序
            w = coefs[:win]
            nw_line[i] = np.dot(seg, w) / np.sum(w)

        # ================================================================
        # 3. MAE = SMA(|src - nw_line|, lookback) * mult
        # ================================================================
        mae = np.full(n, np.nan)
        abs_dev = np.abs(src - nw_line)
        for i in range(lookback - 1, n):
            mae[i] = np.mean(abs_dev[i - lookback + 1 : i + 1]) * mult

        # ================================================================
        # 4. 上下轨
        # ================================================================
        upper = nw_line + mae
        lower = nw_line - mae

        # ================================================================
        # 5. 交易信号
        #    long_signal  = crossunder(close, lower)  → 价格跌破下轨
        #    short_signal = crossover(close, upper)   → 价格升破上轨
        # ================================================================
        long_raw = self.close.cross_up(lower)
        short_raw = self.close.cross_down(upper)

        self.lines.nw_line = nw_line
        self.lines.upper = upper
        self.lines.lower = lower
        self.lines.long_signal = long_raw
        self.lines.short_signal = short_raw

    def _get_source(self):
        """根据 src_type 参数获取价格序列"""
        st = self.params.src_type
        if st == 'close':
            return self.close.values
        elif st == 'hl2':
            return self.hl2().values
        elif st == 'hlc3':
            return self.hlc3().values
        elif st == 'ohlc4':
            return self.ohlc4().values
        else:
            return self.close.values
        
        
class OptimizedTrendTracker(BtIndicator):
    """
    ## OTT — 优化趋势跟踪器

    原理：
      OTT 在所选均线上下偏移 percent% 构建动态停损带，利用自适应逻辑使
      longStop 只升不降、shortStop 只降不升，从而减少噪音造成的频繁翻转。
      当价格恢复趋势方向时，OTT 线快速跟上，形成平滑的趋势跟踪曲线。

      支持 8 种均线：
        SMA — 简单移动平均
        EMA — 指数移动平均
        WMA — 加权移动平均
        TMA — 三角移动平均
        VAR — 基于 CMO 的自适应可变均线
        WWMA — Welles Wilder 均线（RMA）
        ZLEMA — 零延迟 EMA
        TSF — 时间序列预测

      计算流程：
        1. 计算所选 MA(close, length)，VAR 类型使用 var_length 作为 CMO 周期
        2. longStop = MA - offset, shortStop = MA + offset（offset = MA × percent%）
        3. 条件调节 longStop/shortStop → 趋势方向 dir
        4. MT = dir==1 ? longStop : shortStop
        5. OTT = MA > MT ? MT×(base+percent)/base : MT×(base-percent)/base
        6. ott_s2 = OTT[2] 滞后 2 bar（Pine 主绘图线）

    输出线：
      ott_s2      — OTT[2]（Pine 主绘图线，滞后 2 bar，与 tradingview OTT 对比用）
      ott         — OTT 原始值（无偏移）
      ma_avg      — 支撑线（所选 MA 的原始值）
      buy_signal  — 支撑线穿越 OTT[2] 买入信号
      sell_signal — 支撑线穿越 OTT[2] 卖出信号

    参数：
      length       — MA 周期，默认 9
      var_length   — VAR 模式的 CMO 周期，默认 9（与 tradingview 版一致）
      percent      — OTT 百分比偏移，默认 0.14
      mult         — OTT 公式基准值，默认 0.
      ma_type      — MA 类型: SMA/EMA/WMA/TMA/VAR/WWMA/ZLEMA/TSF，默认 'VAR'
    NOTE:
        1. mult 为 0 时，percent 为 OTT 百分比偏移
        2. mult 不为 0 时，percent 为 mult * 价格的标准差倍数 / 均值
    """
    lines = ('ott_s2', 'ott', 'ma_avg', 'buy_signal', 'sell_signal')
    params = dict(
        length=9,
        var_length=9,
        percent=0.14,
        mult=0.,
        ma_type='VAR'
    )
    overlap = True
    isplpt = dict(long_signal=False, short_signal=False)

    def __init__(self):
        close = self.close.values

        ma_avg, ott_raw, ott_s2, buy_signal_k, sell_signal_k = self._compute_ott()

        self.lines.ott_s2 = ott_s2
        self.lines.ott = ott_raw
        self.lines.ma_avg = ma_avg
        self.lines.long_signal = buy_signal_k
        self.lines.short_signal = sell_signal_k
                 
    def _compute_ott(self):
        """
        计算 OTT。

        Returns:
            ma_avg       — 支撑线（所选 MA）
            ott_raw      — OTT 原始值（无偏移，等价 tradingview 的 OTT）
            ott_s2       — OTT[2]（Pine 主绘图线，滞后 2 bar）
            buy_signal_k — 支撑线穿越 OTT[2] 买入信号
            sell_signal_k— 支撑线穿越 OTT[2] 卖出信号
        """
        close = self.close.values
        length = self.params.length
        var_length = self.params.var_length
        percent = self.params.percent
        mult = self.params.mult
        ma_type = self.params.ma_type
        n = len(close)

        # 1. MA
        ma_avg = self._compute_ma(length, var_length, ma_type)

        # 2. OTT 迭代逻辑
        if mult > 0:
            atr = close.std()/close.mean()
            pct = atr * mult
        else:
            pct = percent / 100.0
        base_up = 1.0 + pct
        base_dn = 1.0 - pct
        ott_raw = np.full(n, np.nan, dtype=np.float64)

        prev_long_stop = close[0]
        prev_short_stop = close[0]
        prev_dir = 1

        for i in range(n):
            ma = ma_avg[i]
            if not np.isfinite(ma):
                continue

            fark = ma * pct

            # longStop
            cur_long = ma - fark
            if ma > prev_long_stop:
                cur_long = max(cur_long, prev_long_stop)

            # shortStop
            cur_short = ma + fark
            if ma < prev_short_stop:
                cur_short = min(cur_short, prev_short_stop)

            # direction
            if prev_dir == -1 and ma > prev_short_stop:
                cur_dir = 1
            elif prev_dir == 1 and ma < prev_long_stop:
                cur_dir = -1
            else:
                cur_dir = prev_dir

            # MT
            mt = cur_long if cur_dir == 1 else cur_short

            # OTT
            if ma > mt:
                ott_raw[i] = mt * base_up
            else:
                ott_raw[i] = mt * base_dn

            prev_long_stop = cur_long
            prev_short_stop = cur_short
            prev_dir = cur_dir

        # 3. 偏移 [2]（Pine OTT[2]）
        ott_raw = IndSeries(ott_raw)
        ott_s2=ott_raw.shift(2)

        # 4. 支撑线穿越信号（crossover/c vs OTT[ vs OTT[2]）
        # 非原码 
        buy_signal_k = (ott_raw.shift(1)<=ott_s2.shift(1)) & (ott_raw>ott_s2)
        sell_signal_k = (ott_raw.shift(1)>=ott_s2.shift(1)) & (ott_raw<ott_s2)
        

        return ma_avg, ott_raw, ott_s2, buy_signal_k, sell_signal_k
            
    def _compute_ma(self, length: int, var_length: int,
                ma_type: str) -> np.ndarray:
        # 所有指标使用内置指标计算
        if ma_type == 'SMA':
            return self.close.sma(length).values
        elif ma_type == 'EMA':
            return self.close.ema(length).values
        elif ma_type == 'WMA':
            return self.close.wma(length).values
        elif ma_type == 'TMA':
            return self.close.trima(length).values
        elif ma_type == 'VAR':
            return self.close.btind.varma(length, var_length).values
        elif ma_type == 'WWMA':
            return self.close.rma(length).values
        elif ma_type == 'ZLEMA':
            return self.close.zlma(length).values
        elif ma_type == 'TSF':
            return self.close.TSF(length).values
        else:
            return self.close.sma(length).values
        
        
class RangeFilter(BtIndicator):
    """
    ## 范围过滤器 [DW] — 波动率自适应价格过滤器

    参考 TradingView: https://cn.tradingview.com/script/lut7sBgG-Range-Filter-DW/

    原理：
      受 QQE 波动率过滤器启发，将过滤逻辑直接作用于价格而非平滑后的 RSI。
      1. 先计算平均价格波动范围（支持 ATR / Average Change / Std Dev 等多种尺度）
      2. 价格移动超过计算出的阈值时，filter 线才会跟随移动
         - Type 1：简单门控 — 价格穿出通道时 filter 跳至通道边界
         - Type 2：步进移动 — 价格穿出通道时 filter 以 rng 为步长逐步跟随
      3. 方向判断：filter 上升 → +1（多头），下降 → -1（空头），不变 → 维持
      4. 可选：仅在 filter 变化时采样做条件 EMA 平均，输出更平滑的 filter

    输出线：
      filt   — 范围过滤器主线，叠加主图，反映过滤后的趋势价格
      h_band — 上轨 = filt + rng，价格突破此线触发 filter 上移
      l_band — 下轨 = filt - rng，价格跌破此线触发 filter 下移
      fdir   — 方向信号：+1 = 多头趋势，-1 = 空头趋势

    参数：
      filter_type — 过滤类型: 'Type1' 简单门控 / 'Type2' 步进移动
      mov_src     — 移动来源: 'Wicks' 使用最高/最低价 / 'Close' 使用收盘价
      rng_qty     — 范围倍数，默认 2.618
      rng_scale   — 范围计算尺度: 'ATR' / 'Average Change' / 'Standard Deviation'
                    / '% of Price' / 'Pips' / 'Points' / 'Ticks' / 'Absolute'
      rng_per     — 范围计算周期（ATR / Average Change / Std Dev 时生效）
      smooth_range — 是否平滑范围，开启后对 rng 做 EMA 平滑
      smooth_per  — 范围平滑周期
      av_vals     — 是否平均 filter 变化，开启后仅在 filter 值变化时采样做 EMA
      av_samples  — filter 变化平均的采样数
    """
    lines = ('filt', 'h_band', 'l_band', 'fdir')
    params = dict(
        filter_type='Type1',
        mov_src='Wicks',
        rng_qty=2.618,
        rng_scale='Average Change',
        rng_per=14,
        smooth_range=True,
        smooth_per=27,
        av_vals=False,
        av_samples=2,
    )
    overlap = True  # 叠加主图
    isplot = dict(fdir=False)

    def __init__(self):
        close = self.close.values
        high = self.high.values
        low = self.low.values
        n = len(close)

        # 移动来源
        if self.params.mov_src == 'Wicks':
            h_val = high
            l_val = low
        else:
            h_val = close
            l_val = close

        mid = (h_val + l_val) / 2.0

        # 计算范围
        rng = self._calc_range_size(mid, high, low, close, n)

        # 平滑范围
        if self.params.smooth_range:
            ones = np.ones(n, dtype=bool)
            rng = self._cond_ema(rng, ones, self.params.smooth_per)

        # 计算 filter 和通道
        filt, h_band, l_band = self._calc_filter(
            h_val, l_val, rng, n, self.params.filter_type
        )

        # 平均 filter 变化
        if self.params.av_vals:
            cond = np.zeros(n, dtype=bool)
            cond[0] = True
            cond[1:] = (filt[1:] != filt[:-1])
            filt = self._cond_ema(filt, cond, self.params.av_samples)
            h_band = self._cond_ema(h_band, cond, self.params.av_samples)
            l_band = self._cond_ema(l_band, cond, self.params.av_samples)

        # 方向
        fdir = np.zeros(n, dtype=np.float64)
        for i in range(1, n):
            if filt[i] > filt[i - 1]:
                fdir[i] = 1.0
            elif filt[i] < filt[i - 1]:
                fdir[i] = -1.0
            else:
                fdir[i] = fdir[i - 1]

        self.lines.filt = filt
        self.lines.h_band = h_band
        self.lines.l_band = l_band
        self.lines.fdir = fdir
    
    @staticmethod
    def _cond_ema(x: np.ndarray, cond: np.ndarray, period: int) -> np.ndarray:
        """条件 EMA：仅 cond=True 时更新 EMA，否则保持不变"""
        n = len(x)
        result = np.full(n, np.nan, dtype=np.float64)
        alpha = 2.0 / (period + 1.0)
        ema = np.nan
        for i in range(n):
            if cond[i]:
                if np.isnan(ema):
                    ema = x[i]
                else:
                    ema = (x[i] - ema) * alpha + ema
            result[i] = ema
        return result
    
    @staticmethod
    def _stdev(x: np.ndarray, period: int) -> np.ndarray:
        """滚动标准差（总体标准差 ddof=0，与 Pine stdev 一致）"""
        n = len(x)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < period:
            return out
        for i in range(period - 1, n):
            window = x[i - period + 1 : i + 1]
            out[i] = float(np.std(window, ddof=0))
        return out

    def _calc_range_size(self, mid, high, low, close, n):
        """计算价格波动范围，支持多种尺度"""
        scale = self.params.rng_scale
        qty = self.params.rng_qty
        per = self.params.rng_per
        ones = np.ones(n, dtype=bool)

        if scale == 'ATR':
            # Average True Range 的 EMA 平滑
            tr = np.maximum(
                high - low,
                np.maximum(
                    np.abs(high - np.roll(close, 1)),
                    np.abs(low - np.roll(close, 1)),
                ),
            )
            tr[0] = high[0] - low[0]
            atr = self._cond_ema(tr, ones, per)
            return qty * atr

        elif scale == 'Average Change':
            ac_val = np.abs(mid - np.roll(mid, 1))
            ac_val[0] = 0.0
            ac = self._cond_ema(ac_val, ones, per)
            return qty * ac

        elif scale == 'Standard Deviation':
            sd = self._stdev(mid, per)
            return qty * sd

        elif scale == '% of Price':
            return close * qty / 100.0

        elif scale in ('Points', 'Pips', 'Ticks'):
            return np.full(n, qty * 0.01, dtype=np.float64)

        else:  # Absolute
            return np.full(n, qty, dtype=np.float64)

    @staticmethod
    def _calc_filter(h, l, r, n, filter_type):
        """
        计算范围过滤器主线和通道。

        Type1（简单门控）：
          价格向上突破上轨时，filter 跳至高 - 范围
          价格向下跌破下轨时，filter 跳至低 + 范围
          否则 filter 保持不变

        Type2（步进移动）：
          价格向上突破时，filter 以 rng 为步长逐步向上移动
          价格向下跌破时，filter 以 rng 为步长逐步向下移动
          否则 filter 保持不变
        """
        filt = np.zeros(n, dtype=np.float64)
        filt[0] = (h[0] + l[0]) / 2.0

        if filter_type == 'Type1':
            for i in range(1, n):
                prev = filt[i - 1]
                if h[i] - r[i] > prev:
                    filt[i] = h[i] - r[i]
                elif l[i] + r[i] < prev:
                    filt[i] = l[i] + r[i]
                else:
                    filt[i] = prev
        else:  # Type2
            for i in range(1, n):
                prev = filt[i - 1]
                if h[i] >= prev + r[i]:
                    steps = np.floor(np.abs(h[i] - prev) / r[i])
                    filt[i] = prev + steps * r[i]
                elif l[i] <= prev - r[i]:
                    steps = np.floor(np.abs(l[i] - prev) / r[i])
                    filt[i] = prev - steps * r[i]
                else:
                    filt[i] = prev

        h_band = filt + r
        l_band = filt - r
        return filt, h_band, l_band

class RedKMomentumBars(BtIndicator):
    """
    ## RedK 动量 K 线 — 基于双均线动量的蜡烛图指标
    https://cn.tradingview.com/script/O52gURXf-RedK-Momentum-Bars-RedK-Mo-Bars/

    原理：
      受 Elder Impulse 启发，通过对比短周期均线（Fast/Slow）与长周期 Filter 均线的差值，
      生成动量蜡烛图。蜡烛的「开盘价」是延迟后的慢线动量，「收盘价」是快线动量，
      蜡烛实体反映短周期动量的方向和强度，0 线则是多空分界线。

      计算流程：
        1. 计算 Fast MA 和 Slow MA，均支持多种类型 (SMA/EMA/WMA/HMA/RSS_WMA)
        2. 计算 Filter MA（长周期趋势基准线）
        3. Fast_M = Fast_MA - Filter_MA（快线动量）
        4. Slow_M = Slow_MA - Filter_MA（慢线动量）
        5. Rel_M = WMA(Slow_M, delay)（延迟慢线动量，用于蜡烛开盘价）
        6. 动量蜡烛: o=Rel_M, c=Fast_M, h=max(o,c), l=min(o,c)

    输出线：
      open  — 延迟慢线动量（蜡烛开盘价）
      high  — max(open, close)
      low   — min(open, close)
      close — 快线动量（蜡烛收盘价）
      (四线组合构成蜡烛图，0 线以上为多头动量，以下为空头动量)

    参数：
      fast_len   — 快线周期，默认 10
      fast_type  — 快线类型: 'SMA'/'EMA'/'WMA'/'HMA'/'RSS_WMA'，默认 'SMA'
      slow_len   — 慢线周期，默认 20
      slow_type  — 慢线类型，默认 'SMA'
      slow_delay — 慢线延迟周期（1=无延迟），默认 3
      fil_len    — Filter 周期（趋势基准线），默认 50
      fil_type   — Filter 类型，默认 'SMA'
    """
    lines = ('open', 'high', 'low', 'close')
    category = 'candles'
    params = dict(
        fast_len=10,        # 快线MA周期
        fast_type='SMA',    # 快线MA类型
        slow_len=20,        # 慢线MA周期
        slow_type='SMA',    # 慢线MA类型
        slow_delay=3,       # 慢线延迟周期（1=无延迟）
        fil_len=50,         # Filter MA周期（趋势基准线）
        fil_type='SMA',     # Filter MA类型
    )

    def __init__(self):
        # 1. 计算三条均线
        fast_ma = self._get_ma(self.params.fast_len, self.params.fast_type)
        slow_ma = self._get_ma(self.params.slow_len, self.params.slow_type)
        fil_ma = self._get_ma(self.params.fil_len, self.params.fil_type)

        # 2. 相对动量（减去 Filter 基准）
        fast_m = fast_ma - fil_ma
        slow_m = slow_ma - fil_ma

        # 3. 慢线动量延迟（WMA 平滑）
        rel_m = slow_m.wma(self.params.slow_delay)

        # 4. 生成动量蜡烛 OHLC
        o = rel_m
        c = fast_m
        h = np.maximum(o, c)
        l = np.minimum(o, c)

        self.lines.open = o
        self.lines.high = h
        self.lines.low = l
        self.lines.close = c
    
    def _get_ma(self,length: int, ma_type: str) ->IndSeries:
        """MA 类型分发器"""
        if ma_type == 'SMA':
            return self.close.sma(length)
        elif ma_type == 'EMA':
            return self.close.ema(length)
        elif ma_type == 'WMA':
            return self.close.wma(length)
        elif ma_type == 'HMA':
            return self.close.hma(length)
        else:
            return self.close.btind.rsswma(length)  # RSS_WMA (default)


# ====================================================================
# 条件滤波器调度
# ====================================================================

class _CondFilterState:
    """封装所有条件滤波器的内部状态"""

    def __init__(self,ind: ResamplingFilterPack):
        self.ind = ind
        self.samples = []          # 采样值缓冲
        self.vol_buffer = []       # 成交量累积缓冲
        self.ema_init = False
        self.ema_val = 0.0
        self.rma_init = False
        self.rma_val = 0.0
        self.dema_ema1 = 0.0
        self.dema_ema2 = 0.0
        self.dema_init = False
        self.ewhma_ema1 = 0.0
        self.ewhma_ema2 = 0.0
        self.ewhma_init = False
        self.blp_state = (0.0, 0.0, 0.0)  # IIR 状态
        self.blp_init = False
        self.glp_state = (0.0, 0.0, 0.0)
        self.glp_init = False
        self.ssf_state = (0.0, 0.0, 0.0)
        self.ssf_init = False

    def push_sample(self, val, vol_acc=0.0):
        self.samples.append(val)
        self.vol_buffer.append(vol_acc)

    def compute(self, ma_type, ma_per):
        s = self.samples
        n = ma_per
        if not s:
            return 0.0

        if ma_type == 'SMA':
            return _cond_sma(s, n)
        elif ma_type == 'EMA':
            new_val = s[-1]
            self.ema_val, self.ema_init = self.ind._cond_ema(
                new_val, self.ema_val, n, self.ema_init)
            return self.ema_val
        elif ma_type == 'ZLEMA':
            self.ema_val = self.ind._cond_zlema(s, self.ema_val, n)
            self.ema_init = True
            return self.ema_val
        elif ma_type == 'DEMA':
            if not self.dema_init:
                self.dema_ema1 = s[-1]
                self.dema_ema2 = s[-1]
                self.dema_init = True
            self.dema_ema1, self.dema_ema2, _ = self.ind._cond_dema(
                s, self.dema_ema1, self.dema_ema2, n)
            return 2.0 * self.dema_ema1 - self.dema_ema2
        elif ma_type == 'RMA':
            self.rma_val, self.rma_init = self.ind._cond_rma(
                s[-1], self.rma_val, n, self.rma_init)
            return self.rma_val
        elif ma_type == 'WMA':
            return self.ind._cond_wma(s, n)
        elif ma_type == 'VWMA':
            return self.ind._cond_vwma(s, self.vol_buffer, n)
        elif ma_type == 'ALMA':
            return self.ind._cond_alma(s, n)
        elif ma_type == 'HMA':
            return self.ind._cond_hma(s, n)
        elif ma_type == 'EWHMA':
            if not self.ewhma_init:
                self.ewhma_ema1 = s[-1]
                self.ewhma_ema2 = s[-1]
                self.ewhma_init = True
            half_n = int(np.round(n / 2))
            sqrt_n = int(np.round(np.sqrt(n)))
            # 手动做 EWHMA 逻辑
            alpha_half = 2.0 / (half_n + 1)
            alpha_full = 2.0 / (n + 1)
            self.ewhma_ema1 = (s[-1] - self.ewhma_ema1) * alpha_half + self.ewhma_ema1
            self.ewhma_ema2 = (s[-1] - self.ewhma_ema2) * alpha_full + self.ewhma_ema2
            raw = 2.0 * self.ewhma_ema1 - self.ewhma_ema2
            # 对 raw 做 EMA(sqrt_n)
            if not hasattr(self, 'ewhma_final'):
                self.ewhma_final = raw
            alpha_sqrt = 2.0 / (sqrt_n + 1)
            self.ewhma_final = (raw - self.ewhma_final) * alpha_sqrt + self.ewhma_final
            return self.ewhma_final
        elif ma_type == 'BLP':
            if not self.blp_init:
                self.blp_state = (s[-1], s[-1], s[-1])
                self.blp_init = True
            val, self.blp_state = self.ind._cond_blp(s, self.blp_state, n)
            return val
        elif ma_type == 'GLP':
            if not self.glp_init:
                self.glp_state = (s[-1], s[-1], s[-1])
                self.glp_init = True
            val, self.glp_state = self.ind._cond_glp(s, self.glp_state, n)
            return val
        elif ma_type == 'SSF':
            if not self.ssf_init:
                self.ssf_state = (s[-1], s[-1], s[-1])
                self.ssf_init = True
            val, self.ssf_state = self.ind._cond_ssf(s, self.ssf_state, n)
            return val
        else:
            return self.ind._cond_sma(s, n)


# ====================================================================
# 指标
# ====================================================================

class ResamplingFilterPack(BtIndicator):
    """
    ## 重采样滤波器包 — 自定义采样率下的条件平滑滤波
    https://cn.tradingview.com/script/hIXxuhEV-Resampling-Filter-Pack-DW/

    原理：
      先在降低的采样率下（BPS / Interval / PA）对价格重采样，丢弃高频噪声；
      再利用条件滤波器（仅在采样触发时更新）对低频采样点进行平滑，
      产生类似阶梯状的滤波曲线，用于趋势分析。

      计算流程：
        1. 根据采样方法判断当前 bar 是否为采样触发点
        2. 采样触发时将当前 close 加入样本缓冲（舍弃触发间隔内的噪音）
        3. 在样本缓冲上运行选定的滤波器（SMA/EMA/HMA 等）
        4. 非触发 bar 的滤波器输出保持不变

    输出线：
      ds_src  — 重采样后的源值（触发时 = close，否则保持上一次采样值）
      filter  — 条件滤波器输出（主要的趋势跟踪线）
      filter_dir — 滤波方向信号（1=向上, -1=向下, 0=持平）

    参数：
      s_method    — 重采样方法: 'BPS'/'Interval'/'PA'，默认 'BPS'
      s_size      — BPS/Interval 方法每多少 bar 采样一次，默认 5
      s_off       — 采样偏移（延迟），默认 0
      rng_qty     — PA 方法的范围阈值，默认 0
      rng_scale   — PA 范围尺度: 'Points'/'Pips'/'Ticks'/'% of Price'/'ATR'/'Average Change'/'Absolute'
      rng_per     — PA 动态范围计算周期，默认 14
      smooth_range — PA 范围平滑开关，默认 True
      smooth_per  — PA 范围平滑周期，默认 27
      ma_type     — 滤波器类型: SMA/EMA/ZLEMA/DEMA/RMA/WMA/VWMA/ALMA/HMA/EWHMA/BLP/GLP/SSF，默认 'EMA'
      ma_per      — 滤波器周期，默认 9
    """
    lines = ('ds_src', 'filter', 'filter_dir')
    params = dict(
        s_method='BPS',
        s_size=9,
        s_off=0,
        rng_qty=0.0,
        rng_scale='Average Change',
        rng_per=14,
        smooth_range=True,
        smooth_per=27,
        ma_type='EMA',
        ma_per=9,
    )
    overlap = True

    def __init__(self):
        close = np.asarray(self.close, dtype=np.float64)
        high = np.asarray(self.high, dtype=np.float64)
        low = np.asarray(self.low, dtype=np.float64)
        volume = np.asarray(self.volume, dtype=np.float64)

        ds_src, filter_out, filter_dir = self._compute_resampling_filter(
            close, high, low, volume,
            self.params.s_method, self.params.s_size, self.params.s_off,
            self.params.rng_qty, self.params.rng_scale, self.params.rng_per,
            self.params.smooth_range, self.params.smooth_per,
            self.params.ma_type, self.params.ma_per,
        )

        self.lines.ds_src = ds_src
        self.lines.filter = filter_out
        self.lines.filter_dir = filter_dir
    
    @staticmethod
    def _ema_raw(x: np.ndarray, length: int) -> np.ndarray:
        """EMA, alpha = 2/(length+1)"""
        alpha = 2.0 / (length + 1)
        n = len(x)
        out = np.full(n, np.nan, dtype=np.float64)
        first = 0
        while first < n and np.isnan(x[first]):
            first += 1
        if first >= n:
            return out
        s = x[first]
        out[first] = s
        for i in range(first + 1, n):
            s = alpha * x[i] + (1.0 - alpha) * s
            out[i] = s
        return out

    @staticmethod
    def _rma_raw(x: np.ndarray, length: int) -> np.ndarray:
        """Wilder 平滑 RMA (alpha = 1/length)"""
        alpha = 1.0 / length
        n = len(x)
        out = np.zeros(n, dtype=np.float64)
        s = 0.0
        for i in range(n):
            s = alpha * x[i] + (1.0 - alpha) * s
            out[i] = s
        return out

    @staticmethod
    def _rolling_std(x: np.ndarray, period: int) -> np.ndarray:
        """滚动标准差"""
        n = len(x)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < period:
            return out
        for i in range(period - 1, n):
            window = x[i - period + 1 : i + 1]
            out[i] = float(np.std(window, ddof=1))
        return out


    # ====================================================================
    # 条件滤波器核心实现
    # ====================================================================
    
    @staticmethod   
    def _cond_sma(samples, n):
        """条件 SMA：取最近 n 个采样值的均值"""
        buf = samples[-n:] if len(samples) >= n else samples
        return float(np.mean(buf))

    @staticmethod   
    def _cond_ema(new_val, prev_ema, n, initialized):
        """条件 EMA：alpha = 2/(n+1)，仅在采样时更新"""
        if not initialized:
            return new_val, True
        alpha = 2.0 / (n + 1)
        return (new_val - prev_ema) * alpha + prev_ema, True

    @staticmethod   
    def _cond_rma(new_val, prev_rma, n, initialized):
        """条件 RMA (Wilder)：alpha = 1/n"""
        if not initialized:
            return new_val, True
        return (prev_rma * (n - 1) + new_val) / n, True

    @staticmethod   
    def _cond_wma(samples, n):
        """条件 WMA：线性加权，权重 = [1, 2, ..., k]"""
        buf = samples[-n:] if len(samples) >= n else samples
        k = len(buf)
        weights = np.arange(1, k + 1, dtype=np.float64)
        return float(np.sum(np.array(buf) * weights) / np.sum(weights))

    def _cond_zlema(self,samples, prev_state, n):
        """条件 ZLEMA：data = 2*latest - oldest, 然后对 data 做 EMA"""
        lag = int(np.round((n - 1) / 2)) + 1
        buf = samples[-lag:] if len(samples) >= lag else samples
        data = 2.0 * buf[-1] - buf[0]
        new_ema, _ = self._cond_ema(data, prev_state, n, prev_state is not None)
        return new_ema

    def _cond_dema(self,samples, prev_ema1, prev_ema2, n):
        """条件 DEMA：2*EMA - EMA(EMA)"""
        latest = samples[-1]
        ema1, _ = self._cond_ema(latest, prev_ema1, n, prev_ema1 is not None)
        ema2, _ = self._cond_ema(ema1 if prev_ema1 is not None else latest,
                            prev_ema2, n, prev_ema2 is not None)
        return 2.0 * ema1 - ema2, ema1, ema2

    @staticmethod   
    def _cond_alma(samples, n):
        """条件 ALMA：Arnaud Legoux 移动平均"""
        buf = samples[-n:] if len(samples) >= n else samples
        k = len(buf)
        m = 0.85 * (k - 1)
        s_val = k / 6.0
        weights = np.exp(-((np.arange(1, k + 1, dtype=np.float64) - 1 - m) ** 2) /
                        (2.0 * s_val ** 2))
        return float(np.sum(np.array(buf) * weights) / np.sum(weights))

    def _cond_hma(self,samples, n):
        """条件 HMA：WMA(2*WMA(n/2) - WMA(n), sqrt(n))"""
        half_n = int(np.round(n / 2))
        sqrt_n = int(np.round(np.sqrt(n)))
        buf_half = samples[-half_n:] if len(samples) >= half_n else samples
        buf_full = samples[-n:] if len(samples) >= n else samples
        wma_half = self._cond_wma(samples, half_n)
        wma_full = self._cond_wma(samples, n)
        # 需要在全缓冲上算 2*WMA_half - WMA_full 的 WMA
        # 简化：直接用已有 samples 算 HMA
        raw_val = 2.0 * wma_half - wma_full
        return self._cond_wma(list(samples) + [raw_val], sqrt_n)

    def _cond_ewhma(self,samples, prev_ema1, prev_ema2, n):
        """条件 EWHMA：EMA(2*EMA(n/2) - EMA(n), sqrt(n))"""
        half_n = int(np.round(n / 2))
        sqrt_n = int(np.round(np.sqrt(n)))
        latest = samples[-1]

        ema_half, _ = self._cond_ema(latest, prev_ema1, half_n,
                                prev_ema1 is not None)
        ema_full, _ = self._cond_ema(latest, prev_ema2, n,
                                prev_ema2 is not None)

        return ema_half, ema_full

    @staticmethod   
    def _cond_blp(sample_buffer, prev_state, n):
        """条件 2-Pole Butterworth 低通滤波"""
        a = np.exp(-np.sqrt(2) * np.pi / n)
        b_coef = 2.0 * a * np.cos(np.sqrt(2) * np.pi / n)
        c_coef = a ** 2
        d_coef = (1.0 - b_coef + c_coef) / 4.0

        if len(sample_buffer) < 3:
            return sample_buffer[-1], prev_state

        x0, x1, x2 = sample_buffer[-1], sample_buffer[-2], sample_buffer[-3]
        s0, s1, s2 = prev_state  # [current, prev1, prev2]

        new_s0 = b_coef * s1 - c_coef * s2 + d_coef * (x0 + 2.0 * x1 + x2)
        new_state = (new_s0, s0, s1)
        return new_s0, new_state

    @staticmethod   
    def _cond_glp(sample_buffer, prev_state, n):
        """条件 2-Pole Gaussian 低通滤波"""
        beta_val = (1.0 - np.cos(2.0 * np.pi / n)) / (np.sqrt(2.0) - 1.0)
        alpha = -beta_val + np.sqrt(beta_val ** 2 + 2.0 * beta_val)

        if len(sample_buffer) < 3:
            return sample_buffer[-1], prev_state

        x0 = sample_buffer[-1]
        s0, s1, s2 = prev_state  # [current, prev1, prev2]

        new_s0 = alpha ** 2 * x0 + 2.0 * (1.0 - alpha) * s1 - (1.0 - alpha) ** 2 * s2
        new_state = (new_s0, s0, s1)
        return new_s0, new_state

    @staticmethod   
    def _cond_ssf(sample_buffer, prev_state, n):
        """条件 Super Smoother 滤波"""
        omega = 2.0 * np.pi / n
        a = np.exp(-np.sqrt(2) * np.pi / n)
        c2 = 2.0 * a * np.cos(np.sqrt(2) / 2.0 * omega)
        c3 = -(a ** 2)
        c1 = 1.0 - c2 - c3

        if len(sample_buffer) < 3:
            return sample_buffer[-1], prev_state

        x0 = sample_buffer[-1]
        s0, s1, s2 = prev_state  # [current, prev1, prev2]

        new_s0 = c1 * x0 + c2 * s1 + c3 * s2
        new_state = (new_s0, s0, s1)
        return new_s0, new_state

    @staticmethod
    def _cond_vwma(samples, vols_buffer, n):
        """条件 VWMA"""
        buf = samples[-n:] if len(samples) >= n else samples
        vbuf = vols_buffer[-n:] if len(vols_buffer) >= n else vols_buffer
        k = min(len(buf), len(vbuf))
        if k == 0:
            return 0.0
        num = np.sum(np.array(buf[-k:]) * np.array(vbuf[-k:]))
        den = np.sum(vbuf[-k:])
        return float(num / den) if den > 1e-12 else float(np.mean(buf[-k:]))


    # ====================================================================
    # PA 方法：价格行为采样触发
    # ====================================================================
    def _compute_pa_thresholds(self,close, high, low, rng_scale, rng_qty, rng_per,
                            smooth_range, smooth_per):
        """计算 PA 方法的动态阈值（每条 bar 一个阈值）"""
        n = len(close)
        if rng_qty <= 0:
            return np.full(n, 0.0, dtype=np.float64)

        # 计算 ATR
        tr = np.zeros(n, dtype=np.float64)
        tr[0] = high[0] - low[0]
        for i in range(1, n):
            tr[i] = max(high[i] - low[i],
                        abs(high[i] - close[i - 1]),
                        abs(low[i] - close[i - 1]))
        atr_raw = self._ema_raw(tr, rng_per)  # Cond_EMA on new bar triggers → 简化为 EMA

        # 平均变动
        abs_chg = np.zeros(n, dtype=np.float64)
        abs_chg[1:] = np.abs(close[1:] - close[:-1])
        ach_raw = self._ema_raw(abs_chg, rng_per)
        ach_raw[np.isnan(ach_raw)] = atr_raw[np.isnan(ach_raw)]

        # 按 scale 确定 r
        if rng_scale == 'Points':
            r = np.full(n, rng_qty * 1.0, dtype=np.float64)
        elif rng_scale == 'Pips':
            r = np.full(n, rng_qty * 0.0001, dtype=np.float64)
        elif rng_scale == 'Ticks':
            r = np.full(n, rng_qty * 1.0, dtype=np.float64)  # syminfo.mintick ~= 1
        elif rng_scale == '% of Price':
            r = close * rng_qty / 100.0
        elif rng_scale == 'ATR':
            r = rng_qty * atr_raw
        elif rng_scale == 'Average Change':
            r = rng_qty * ach_raw
        else:  # Absolute
            r = np.full(n, rng_qty, dtype=np.float64)

        # 平滑
        if smooth_range:
            r = self._ema_raw(r, smooth_per)

        # 清理 NaN
        r[np.isnan(r)] = 1e-10
        r[r <= 1e-10] = 1e-10
        return r


    # ====================================================================
    # 主计算函数
    # ====================================================================
    def _compute_resampling_filter(self,close, high, low, volume,
                                s_method, s_size, s_off,
                                rng_qty, rng_scale, rng_per,
                                smooth_range, smooth_per,
                                ma_type, ma_per):
        """
        计算重采样滤波器。

        Returns:
            ds_src      — 重采样后的源值
            filter_out  — 条件滤波输出
            filter_dir  — 滤波方向 (1=向上, -1=向下)
        """
        n = len(close)

        ds_src = np.full(n, np.nan, dtype=np.float64)
        filter_out = np.full(n, np.nan, dtype=np.float64)
        filter_dir = np.zeros(n, dtype=np.int32)

        # PA 阈值
        if s_method == 'PA':
            pa_thresh = self._compute_pa_thresholds(
                close, high, low, rng_scale, rng_qty, rng_per,
                smooth_range, smooth_per)
        else:
            pa_thresh = None

        state = _CondFilterState(self)
        last_ds_src = close[0]
        last_filter = close[0]
        prev_filter_dir = 0
        pa_line = close[0]  # PA 方法参考线
        vol_acc = 0.0  # VWMA 成交量累积

        for i in range(n):
            c = close[i]
            vol_acc += volume[i]

            # --- 采样触发判断 ---
            new_sample = False
            if s_method == 'BPS':
                if i >= s_off and (i - s_off) % max(1, s_size) == 0:
                    new_sample = True
            elif s_method == 'Interval':
                # 简化：Interval 退化为 BPS
                if i >= s_off and (i - s_off) % max(1, s_size) == 0:
                    new_sample = True
            elif s_method == 'PA':
                if abs(c - pa_line) >= pa_thresh[i]:
                    pa_line = c
                if pa_line != (pa_line_at_prev_bar if i > 0 else 0):
                    new_sample = True
                else:
                    new_sample = False
                if s_off > 0 and i >= s_off:
                    # 实际 PA 需要 [o] 偏移，简化处理
                    pass
                if rng_qty <= 0:
                    new_sample = True  # 阈值为0 → 每根都采样
                pa_line_at_prev_bar = pa_line
            else:
                new_sample = True

            # --- 更新采样缓冲 ---
            if new_sample:
                last_ds_src = c
                state.push_sample(c, vol_acc)
                vol_acc = 0.0

            ds_src[i] = last_ds_src

            # --- 计算滤波值 ---
            f = state.compute(ma_type, ma_per)
            filter_out[i] = f

            # --- 方向 ---
            if f > last_filter:
                filter_dir[i] = 1
            elif f < last_filter:
                filter_dir[i] = -1
            else:
                filter_dir[i] = prev_filter_dir

            last_filter = f
            prev_filter_dir = filter_dir[i]

        return ds_src, filter_out, filter_dir


class SRChannels(BtIndicator):
    """
    ## SR Channels — 支撑阻力通道 [LonesomeTheBlue]

    参考 TradingView: https://cn.tradingview.com/script/qX1Zv6O7-Support-Resistance-Channels-LonesomeTheBlue/

    原理：
      基于枢轴高点和低点构建支撑阻力通道，贪心纳入通道宽度范围内的其他枢轴，
      计算通道强度（枢轴数 × 20 + K线触及数），保留最强的 top N 通道。
      价格不在通道内时，上穿阻力上轨触发做多，下穿支撑下轨触发做空。

    计算流程：
        1. 检测枢轴高/低点（pivot high/low），左右各 prd 根 bar 的极值
        2. 维护枢轴列表，超出 loopback 的旧枢轴被移除
        3. 每新增枢轴时重算通道：
           - 以每个枢轴为起点，贪心纳入 cwidth 范围内的其他枢轴
           - 通道强度 = 纳入枢轴数 × 20 + loopback 内 K 线触及通道数
           - 选择强度最高的 top N 通道（排除重叠通道）
        4. 生成信号：
           - 价格不在任何通道内时，上穿阻力上轨 → long_signal
           - 价格不在任何通道内时，下穿支撑下轨 → short_signal

    输出线：
        sr_high      — 通道上轨包络线（取所有通道 hi 的最大值），主图叠加
        sr_low       — 通道下轨包络线（取所有通道 lo 的最小值），主图叠加
        long_signal  — 做多信号（价格上穿阻力通道上轨，非通道内），不绘图
        short_signal — 做空信号（价格下穿支撑通道下轨，非通道内），不绘图

    参数：
        prd           — 枢轴检测窗口（左右各 N 根），默认 10
        ppsrc         — 枢轴源: 'High/Low' 或 'Close/Open'，默认 'High/Low'
        channel_w     — 最大通道宽度 %（基于最近 300 根 K 线振幅），默认 5
        min_strength  — 最低通道强度（至少含 2 个枢轴 = 40），默认 1
        max_numsr     — 最多保留通道数，默认 6
        loopback      — 枢轴回看周期，默认 290
    """
    lines = ('long_signal', 'short_signal')
    params = dict(
        prd=10,
        ppsrc='High/Low',
        channel_w=5,
        min_strength=1,
        max_numsr=6,
        loopback=290,
    )
    overlap = True
    isplot = dict(long_signal=False, short_signal=False)

    def __init__(self):
        close = self.close.values
        high = self.high.values
        low = self.low.values
        open_ = self.open.values
        n = len(close)

        prd = self.params.prd
        loopback = self.params.loopback
        cw_pct = self.params.channel_w / 100.0
        max_n = self.params.max_numsr - 1
        min_str = self.params.min_strength

        # ====================================================================
        # 1. 枢轴检测
        # ====================================================================
        if self.params.ppsrc == 'High/Low':
            src_high, src_low = high, low
        else:
            src_high = np.maximum(close, open_)
            src_low = np.minimum(close, open_)

        pivot_high = np.zeros(n, dtype=bool)
        pivot_low = np.zeros(n, dtype=bool)
        for i in range(prd, n - prd):
            if np.all(src_high[i] >= src_high[i - prd:i + prd + 1]):
                pivot_high[i] = True
            if np.all(src_low[i] <= src_low[i - prd:i + prd + 1]):
                pivot_low[i] = True

        # ====================================================================
        # 2. 模拟逐 Bar 推进：更新枢轴列表 + 重算通道 + 检测信号
        # ====================================================================
        pivot_list = []                         # [(value, bar_index), ...]
        sr_channels = []                        # [(hi, lo), ...]
        long_signal = np.zeros(n, dtype=bool)
        short_signal = np.zeros(n, dtype=bool)

        for i in range(prd + 1, n):
            # --- 更新枢轴列表 ---
            if pivot_high[i] or pivot_low[i]:
                val = src_high[i] if pivot_high[i] else src_low[i]
                pivot_list.insert(0, (val, i))
                # 移除超出 loopback 的旧枢轴
                while pivot_list and i - pivot_list[-1][1] > loopback:
                    pivot_list.pop()

                # --- 重算通道 ---
                cwidth = (np.max(high[max(0, i - 299):i + 1]) -
                          np.min(low[max(0, i - 299):i + 1])) * cw_pct

                sr_channels = self._compute_channels(
                    pivot_list, cwidth, min_str, max_n, high, low, i, loopback)

            # --- 检测信号 ---
            if not sr_channels:
                continue

            # 判断当前价格是否在某个通道内
            in_channel = any(lo <= close[i] <= hi for hi, lo in sr_channels)

            if not in_channel and i > 0:
                for hi_ch, lo_ch in sr_channels:
                    # 上穿阻力
                    if close[i - 1] <= hi_ch and close[i] > hi_ch:
                        long_signal[i] = True
                        break
                    # 下穿支撑
                    if close[i - 1] >= lo_ch and close[i] < lo_ch:
                        short_signal[i] = True
                        break

        self.lines.long_signal = long_signal.astype(float)
        self.lines.short_signal = short_signal.astype(float)

    # ------------------------------------------------------------------
    @staticmethod
    def _compute_channels(pivot_list, cwidth, min_strength, max_n,
                          high, low, bar_idx, loopback):
        """
        从枢轴列表计算支撑阻力通道（完全复刻 Pine Script 逻辑）

        1. 每个枢轴为起点，贪心纳入 cwidth 范围内的其他枢轴
        2. 通道强度 = 纳入枢轴数 × 20 + K线触及数
        3. 选择强度最高的 top N 个通道（排除重叠）
        """
        if not pivot_list or cwidth <= 0:
            return []

        n_piv = len(pivot_list)
        supres = []  # [[strength, hi, lo], ...]

        # --- 对每个枢轴构建通道 ---
        for x in range(n_piv):
            pv = pivot_list[x][0]
            hi = lo = pv
            numpp = 0
            for y in range(n_piv):
                cpp = pivot_list[y][0]
                wdth = hi - cpp if cpp <= hi else cpp - lo
                if wdth <= cwidth:
                    if cpp <= hi:
                        lo = min(lo, cpp)
                    else:
                        hi = max(hi, cpp)
                    numpp += 20
            supres.append([float(numpp), float(hi), float(lo)])

        # --- 增加 K线触及 得分 ---
        start = max(0, bar_idx - loopback)
        for x in range(n_piv):
            h, l = supres[x][1], supres[x][2]
            touches = int(np.sum(
                ((low[start:bar_idx + 1] >= l) & (low[start:bar_idx + 1] <= h)) |
                ((high[start:bar_idx + 1] >= l) & (high[start:bar_idx + 1] <= h))
            ))
            supres[x][0] += touches

        # --- 选出强度最高的 top N 通道（排除重叠）---
        selected = []
        banned = set()

        for _ in range(min(max_n + 1, n_piv)):
            best_idx = -1
            best_strength = -1.0
            for x in range(n_piv):
                if x in banned:
                    continue
                if supres[x][0] > best_strength and supres[x][0] >= min_strength * 20:
                    best_strength = supres[x][0]
                    best_idx = x
            if best_idx < 0:
                break

            hh, ll = supres[best_idx][1], supres[best_idx][2]
            selected.append((hh, ll))
            banned.add(best_idx)

            # 将与选中通道重叠的其他通道排除
            for y in range(n_piv):
                if y in banned:
                    continue
                if ll <= supres[y][1] <= hh or ll <= supres[y][2] <= hh:
                    banned.add(y)

        # 按上轨降序排列
        selected.sort(key=lambda x: x[0], reverse=True)
        return selected
    
    
class STDNpoleGaussianFilter(BtIndicator):
    """
    ## STD 过滤的 N-Pole 高斯滤波器 — 高阶平滑趋势线
    https://cn.tradingview.com/script/i4xZNAoy-STD-Filtered-N-Pole-Gaussian-Filter-Loxx/

    原理：
      结合可选的 STD（标准差）噪声过滤和 N-Pole Gaussian Filter（高阶 IIR 高斯平滑），
      生成一条平滑的趋势跟随线。STD 过滤可剔除微小价格波动，Gaussian Filter 则提供
      灵活可调的平滑度（通过 period 和 order 控制）。

      计算流程：
        1. 根据 src 参数选择价格源（Close / HL2 / HLC3 等）
        2. 若 filter_option 含 "Price"，对源数据做 STD 过滤去噪
        3. 对（过滤后的）数据应用 N-Pole Gaussian Filter 平滑
        4. 若 filter_option 含 "Gaussian Filter"，对输出再做一次 STD 过滤
        5. 方向判定：out 上升 → +1，下降 → -1
        6. 信号生成：连续方向切换时产生交易信号

    输出线：
      out          — N-Pole Gaussian Filter 主线，叠加主图
      dir          — 方向信号：+1 = 多头趋势（out 上升），-1 = 空头趋势（out 下降）
      long_signal  — 做多信号：空头转多头时触发（默认隐藏）
      short_signal — 做空信号：多头转空头时触发（默认隐藏）

    参数：
      period        — 高斯滤波周期，决定截止频率，默认 25
      order         — 高斯滤波阶数（极点数量），越大越平滑但滞后越大，默认 5
      src           — 价格源: 'Close'/'Open'/'High'/'Low'/'HL2'/'HLC3'/'OHLC4'，默认 'Close'
      filter_option — STD 过滤选项: 'None' 不过滤 / 'Price' 源端过滤 /
                      'Gaussian Filter' 输出端过滤 / 'Both' 两端过滤，默认 'Both'
      filter_mult   — STD 过滤倍数，默认 1.6185
      filter_period — STD 过滤计算周期，默认 10
    """
    lines = ('out', 'dir', 'long_signal', 'short_signal')
    params = dict(
        period=25,
        order=5,
        src='Close',
        filter_option='Both',
        filter_mult=1.6185,
        filter_period=10,
    )
    overlap = dict(out=True, dir=False)
    isplot = dict(long_signal=False, short_signal=False)
    
    def get_src(self)->IndSeries:
        src=self.params.src
        if src == 'Close':
            return self.close
        elif src == 'Open':
            return self.open
        elif src == 'High':
            return self.high
        elif src == 'Low':
            return self.low
        elif src == 'HL2':
            return self.hl2()
        elif src == 'HLC3':
            return self.hlc3()
        elif src == 'OHLC4':
            return self.ohlc4()
        else:
            return self.close

    def __init__(self):
        n = self.close.size
        src = self.get_src().values

        # 2. 可选：源端 STD 过滤
        fo = self.params.filter_option
        if self.params.filter_mult > 0 and fo in ('Price', 'Both'):
            src = self._std_filter(src, self.params.filter_period, self.params.filter_mult)

        # 3. N-Pole Gaussian Filter
        out = self._npolegf(src, self.params.period, self.params.order)

        # 4. 可选：输出端 STD 过滤
        if self.params.filter_mult > 0 and fo in ('Gaussian Filter', 'Both'):
            out = self._std_filter(out, self.params.filter_period, self.params.filter_mult)

        # 5. 方向判定
        dir_ = np.zeros(n, dtype=np.float64)
        for i in range(1, n):
            if np.isfinite(out[i]) and np.isfinite(out[i - 1]):
                if out[i] > out[i - 1]:
                    dir_[i] = 1.0
                elif out[i] < out[i - 1]:
                    dir_[i] = -1.0
                else:
                    dir_[i] = dir_[i - 1]
            else:
                dir_[i] = dir_[i - 1]

        # 6. 交易信号（趋势反转）
        # goLong  = pregoLong 且上一 bar 为下跌趋势（contsw=-1）
        # goShort = pregoShort 且上一 bar 为上涨趋势（contsw=1）
        long_signal = np.zeros(n, dtype=np.float64)
        short_signal = np.zeros(n, dtype=np.float64)
        contsw = 0  # 连续方向开关：1=多头, -1=空头
        for i in range(2, n):
            if not (np.isfinite(out[i]) and np.isfinite(out[i - 1]) and np.isfinite(out[i - 2])):
                continue

            out_i, out_i1, out_i2 = out[i], out[i - 1], out[i - 2]

            # pregoLong: out 上升 且 前一 bar 非上升（下降或持平）
            prego_long = out_i > out_i1 and (out_i1 < out_i2 or out_i1 == out_i2)
            # pregoShort: out 下降 且 前一 bar 非下降（上升或持平）
            prego_short = out_i < out_i1 and (out_i1 > out_i2 or out_i1 == out_i2)

            if prego_long:
                contsw = 1
            elif prego_short:
                contsw = -1

            # goLong: 由空转多
            if prego_long and dir_[i - 1] == -1.0:
                long_signal[i] = 1.0
            # goShort: 由多转空
            if prego_short and dir_[i - 1] == 1.0:
                short_signal[i] = 1.0

        self.lines.out = out
        self.lines.dir = dir_
        self.lines.long_signal = long_signal
        self.lines.short_signal = short_signal
    
    @staticmethod
    def _factorial(n: int) -> float:
        """阶乘"""
        result = 1.0
        for i in range(1, n + 1):
            result *= i
        return result

    @staticmethod
    def _rolling_std(x: np.ndarray, period: int) -> np.ndarray:
        """滚动标准差（ddof=1，与 Pine ta.stdev 一致）"""
        n = len(x)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < period:
            return out
        for i in range(period - 1, n):
            window = x[i - period + 1 : i + 1]
            out[i] = float(np.std(window, ddof=1))
        return out


    @staticmethod
    def _alpha(period: int, poles: int) -> float:
        """计算高斯滤波 alpha 系数"""
        w = 2.0 * np.pi / period
        b = (1.0 - np.cos(w)) / (pow(1.414, 2.0 / poles) - 1.0)
        a = -b + np.sqrt(b * b + 2.0 * b)
        return a


    def _make_coeffs(self, period: int, order: int):
        """
        生成 N-Pole Gaussian Filter 系数矩阵。

        返回 (coeffs, a)，其中 coeffs 形状为 (order+1, 3)：
        coeffs[r, 0] = C(order, r)     组合数
        coeffs[r, 1] = a^r
        coeffs[r, 2] = (1-a)^r
        """
        a = self._alpha(period, order)
        coeffs = np.zeros((order + 1, 3), dtype=np.float64)
        for r in range(order + 1):
            coeffs[r, 0] = self._factorial(order) / (self._factorial(order - r) * self._factorial(r))
            coeffs[r, 1] = pow(a, r)
            coeffs[r, 2] = pow(1.0 - a, r)
        return coeffs, a


    def _npolegf(self, src: np.ndarray, period: int, order: int) -> np.ndarray:
        """
        N-Pole Gaussian Filter 递归滤波。

        公式：filt = src * a^order + Σ_{r=1}^{order} (-1)^{r+1} * C(order,r) * (1-a)^r * filt[r]

        本质上是一个加权历史滤波器，当前值由当前 src 和历史 filter 值共同决定，
        order 越大则越平滑但滞后越大。
        """
        n = len(src)
        coeffs, _ = self._make_coeffs(period, order)
        filt = np.full(n, np.nan, dtype=np.float64)

        for i in range(n):
            val = src[i] * coeffs[order, 1]  # src * a^order
            sign = 1
            for r in range(1, order + 1):
                if i - r >= 0:
                    # 用 nz() 语义：NaN 按 0 处理
                    prev = filt[i - r] if np.isfinite(filt[i - r]) else 0.0
                    val += sign * coeffs[r, 0] * coeffs[r, 2] * prev
                sign *= -1
            filt[i] = val

        return filt


    def _std_filter(self, src: np.ndarray, length: int, filter_mult: float) -> np.ndarray:
        """
        STD 过滤器：小幅波动保持前值不变，实现去噪。

        对每根 bar，计算 |price - prev_filtered|，若小于 filter_mult * stdev，
        则维持前一过滤值不变；否则接受当前值。
        """
        n = len(src)
        price = src.copy()
        filtdev = filter_mult * self._rolling_std(src, length)
        for i in range(1, n):
            if not np.isnan(filtdev[i]) and abs(price[i] - price[i - 1]) < filtdev[i]:
                price[i] = price[i - 1]
        return price


class SyntheticOscillator(BtIndicator):
    """
    TASC Synthetic Oscillator Strategy
    基于 Pine Script "TASC 2026.04 A Synthetic Oscillator [John F. Ehlers]" 转换
    https://cn.tradingview.com/script/we9AMcvE-TASC-2026-04-A-Synthetic-Oscillator/
    
    参数：
    LB   主周期下界，默认 15
    UB   主周期上界，默认 25
    
    核心逻辑：
    1. Hann 滤波器（12 周期）初始平滑价格
    2. 高通 + SuperSmoother 构成带通滤波 → 实部分量 re
    3. ROC + 归一化 → 虚部分量 im
    4. arctan 变化率 → 估计主周期 dc
    5. 中频带通 + 零交叉检测 → 相位累加 / 重置
    6. sin(累积相位) → Synthetic Oscillator (so)
    7. so 上穿零线 → 做多；下穿零线 → 做空
    """
    params = dict(
        LB=15,
        UB=25,
    )
    
    def __init__(self):
        close = self.close.values
        so = self.synthetic_oscillator(close, self.params.LB, self.params.UB)
        self.lines.so = so
    
    # ====================================================================
    # Ehlers 滤波器实现
    # ====================================================================
    @staticmethod
    def supersmoother(src: np.ndarray, period: int) -> np.ndarray:
        """SuperSmoother: 二阶 IIR 低通滤波器"""
        q = np.exp(-1.414 * np.pi / period)
        c1 = 2.0 * q * np.cos(1.414 * np.pi / period)
        c2 = q * q
        a0 = (1.0 - c1 + c2) / 2.0
        n = len(src)
        ss = np.zeros(n)
        for i in range(n):
            if i < 4:
                ss[i] = src[i]
            else:
                ss[i] = a0 * (src[i] + src[i - 1]) + c1 * ss[i - 1] - c2 * ss[i - 2]
        return ss

    @staticmethod
    def ultimatesmoother(src: np.ndarray, period: int) -> np.ndarray:
        """UltimateSmoother: 全通 - 高通 滤波器"""
        q = np.exp(-1.414 * np.pi / period)
        c1 = 2.0 * q * np.cos(1.414 * np.pi / period)
        c2 = q * q
        a0 = (1.0 + c1 + c2) / 4.0
        n = len(src)
        us = np.zeros(n)
        for i in range(n):
            if i < 4:
                us[i] = src[i]
            else:
                us[i] = ((1.0 - a0) * src[i]
                        + (2.0 * a0 - c1) * src[i - 1]
                        + (c2 - a0) * src[i - 2]
                        + c1 * us[i - 1] - c2 * us[i - 2])
        return us

    @staticmethod
    def hp_filter(src: np.ndarray, period: int) -> np.ndarray:
        """高通滤波器 (二阶 IIR)"""
        Q = np.exp(-1.414 * np.pi / period)
        c1 = 2.0 * Q * np.cos(1.414 * np.pi / period)
        c2 = Q * Q
        a0 = (1.0 + c1 + c2) / 4.0
        n = len(src)
        hp = np.zeros(n)
        for i in range(n):
            if i < 4:
                hp[i] = 0.0
            else:
                hp[i] = (a0 * (src[i] - 2.0 * src[i - 1] + src[i - 2])
                        + c1 * hp[i - 1] - c2 * hp[i - 2])
        return hp

    @staticmethod
    def hann_filter(src: np.ndarray, length: int) -> np.ndarray:
        """Hann 加权滑动平均"""
        n = len(src)
        out = np.zeros(n)
        for i in range(n):
            filt_val = 0.0
            coef = 0.0
            for c in range(1, length + 1):
                p = np.cos(2.0 * np.pi * c / (length + 1.0))
                w = 1.0 - p
                idx = i - c + 1
                if idx >= 0:
                    filt_val += w * src[idx]
                coef += w
            if coef != 0.0:
                out[i] = filt_val / coef
        return out

    @staticmethod
    def rolling_rms(src: np.ndarray, length: int) -> np.ndarray:
        """滚动 RMS（累积和 O(n)）"""
        n = len(src)
        sq = np.cumsum(src ** 2)
        out = np.zeros(n)
        for i in range(length - 1, n):
            s2 = sq[i] - (sq[i - length] if i >= length else 0.0)
            if s2 > 0:
                out[i] = np.sqrt(s2 / length)
        return out


    # ====================================================================
    # Synthetic Oscillator 核心
    # ====================================================================
    def synthetic_oscillator(self, src: np.ndarray, LB: int, UB: int) -> np.ndarray:
        """
        Ehlers Synthetic Oscillator（完全复刻 Pine Script 逻辑）

        Parameters
        ----------
        src : 价格序列
        LB  : 主周期下界
        UB  : 主周期上界
        """
        n = len(src)
        mid = int(np.sqrt(LB * UB))

        # 1. Hann 平滑
        price = self.hann_filter(src, 12)

        # 2. 带通 → 实部分量 re
        hp = self.hp_filter(price, UB)
        lp = self.supersmoother(hp, LB)
        rms_lp = self.rolling_rms(lp, 100)
        re = np.where(rms_lp != 0, lp / rms_lp, 0.0)

        # 3. ROC → 虚部分量 im
        roc = np.zeros(n)
        roc[1:] = re[1:] - re[:-1]
        qrms = self.rolling_rms(roc, 100)
        im = np.where(qrms != 0, roc / qrms, 0.0)

        # 4. arctan 变化率 → 主周期 dc
        im_diff = np.zeros(n)
        im_diff[1:] = im[1:] - im[:-1]
        denom = roc * im - im_diff * re
        dc = np.where(denom != 0, 6.28 * (re ** 2 + im ** 2) / denom, 0.0)
        dc = np.clip(dc, LB, UB)

        # 5. 中频带通 → 零交叉检测
        hp2 = self.hp_filter(src, mid)
        bp = self.ultimatesmoother(hp2, mid)

        # 6. 相位累加 & 合成振荡器
        ph = np.zeros(n)
        so = np.zeros(n)
        ph_acc = 0.0

        for i in range(n):
            # 累加相位
            if dc[i] != 0:
                ph_acc += 2.0 * np.pi / dc[i]

            # 零交叉检测 → 相位重置
            if i >= 1 and dc[i] != 0:
                if bp[i - 1] <= 0.0 and bp[i] > 0.0:         # crossover
                    ph_acc = np.pi / dc[i]
                elif bp[i - 1] >= 0.0 and bp[i] < 0.0:       # crossunder
                    ph_acc = np.pi + np.pi / dc[i]

            ph[i] = ph_acc
            so[i] = np.sin(ph_acc)

            # 消除相位重置毛刺
            if i >= 1:
                if 0.0 < ph_acc < np.pi / 2.0 and so[i] < so[i - 1]:
                    so[i] = so[i - 1]
                elif np.pi < ph_acc < 3.0 * np.pi / 2.0 and so[i] > so[i - 1]:
                    so[i] = so[i - 1]

        return so


class TradingActivityIndex(BtIndicator):
    """
    交易活跃度指数 (Trading Activity Index)
    Trading Activity Index (TAI) Indicator
    基于 Pine Script "Trading Activity Index (Zeiierman)" 转换
    https://cn.tradingview.com/script/rtOfqf13-Trading-Activity-Index-Zeiierman/

    参数:
    - len_form:  价格成交量均线窗口，默认 20
    - len_hist:  滚动百分位历史窗口，默认 252（年化）
    
    核心逻辑：
    1. 价格成交量 = close × volume
    2. SMA(价格成交量, len_form) → log → VOLSCALE 代理值
    3. VOLSCALE 滚动百分位 (P0/P20/P40/P60/P80/P100, 默认 252 根K线)
    """

    lines = ('tai', 'p0', 'p20', 'p40', 'p60', 'p80', 'p100')
    params = dict(len_form=20, len_hist=252)
    overlap = False

    def __init__(self):
        len_form = self.params.len_form
        len_hist = self.params.len_hist

        # SMA → log → VOLSCALE
        dlr_vol_avg = (self.close*self.volume).sma(len_form)
        vscale = np.log(np.maximum(dlr_vol_avg.values, 1e-10))

        # 3. 滚动百分位
        self.lines.tai = vscale
        self.lines.p0 = self._rolling_percentile(vscale, len_hist, 0)
        self.lines.p20 = self._rolling_percentile(vscale, len_hist, 20)
        self.lines.p40 = self._rolling_percentile(vscale, len_hist, 40)
        self.lines.p60 = self._rolling_percentile(vscale, len_hist, 60)
        self.lines.p80 = self._rolling_percentile(vscale, len_hist, 80)
        self.lines.p100 = self._rolling_percentile(vscale, len_hist, 100)
    
    @staticmethod
    def _rolling_percentile(src: np.ndarray, length: int,
                        percentile: float) -> np.ndarray:
        """滚动百分位（线性插值）"""
        n = len(src)
        out = np.full(n, np.nan)
        if n < length:
            return out
        windows = np.lib.stride_tricks.sliding_window_view(src, length)
        percs = np.percentile(windows, percentile, method='linear', axis=1)
        out[length - 1:] = percs
        return out


class TrendStateSignals(BtIndicator):
    """
    Trend State Signals — 自适应步进趋势过滤器
    基于 Pine Script "Trend State Signals [Market Structure Lab]" 转换
    参考 TradingView: https://cn.tradingview.com/script/I19TTam0-Trend-State-Signals/

    参数:
    - length:           灵敏度周期，默认 20
    - multiplier:       范围乘数（越大信号越少），默认 3.5
    - src_type:         价格源，可选 close/hl2/hlc3/ohlc4，默认 close
    
    核心逻辑：
    1. 计算自适应范围：priceMovement = |change(source)|, smoothed = RMA(priceMovement, length)
    adaptiveRange = smoothed * multiplier
    2. Step Filter（步进过滤器）：
    - 价格向上突破 upperTrigger(prev + range) 时，filter = source - range
    - 价格向下突破 lowerTrigger(prev - range) 时，filter = source + range
    - 否则保持不变
    3. 趋势判断：filter 上升 → 多头(1)，下降 → 空头(-1)，不变 → 维持
    4. 信号：趋势方向刚发生变化时触发
    """
    lines = ('filter_line', 'trend', 'long_signal', 'short_signal')
    isplot = dict(long_signal=False, short_signal=False, trend=False)
    params = dict(length=20, multiplier=3.5, src_type='close')
    overlap = True

    def __init__(self):
        src = self._get_source()
        n = len(src)
        length = self.params.length
        multiplier = self.params.multiplier

        # ================================================================
        # 1. 自适应范围
        #    priceMovement = |change(source)|
        #    smoothedMovement = RMA(priceMovement, length)
        #    adaptiveRange = smoothedMovement * multiplier
        # ================================================================
        price_movement = src.diff().abs()  # 价格变化的绝对值
        smoothed_movement = price_movement.rma(length)
        adaptive_range = smoothed_movement * multiplier

        # ================================================================
        # 2. Step Filter 递归计算
        #    upperTrigger = prev + adaptiveRange
        #    lowerTrigger = prev - adaptiveRange
        #    src > upperTrigger → filter = src - adaptiveRange
        #    src < lowerTrigger → filter = src + adaptiveRange
        #    else → filter = prev
        # ================================================================
        # ================================================================
        # 3. 趋势状态
        #    filter 上升 → trend = 1
        #    filter 下降 → trend = -1
        #    不变 → trend = trend[1]
        # ================================================================
        range_filter = np.full(n, np.nan)
        trend = pd.Series(np.zeros(n, dtype=int))
        for i in range(length,n):
            # 2. Step Filter 递归计算
            prev = range_filter[i - 1] if i > 0 else src[i]
            if np.isnan(prev):
                prev = src[i]
            
            upper_trigger = prev + adaptive_range[i]
            lower_trigger = prev - adaptive_range[i]

            if src[i] > upper_trigger:
                range_filter[i] = src[i] - adaptive_range[i]
            elif src[i] < lower_trigger:
                range_filter[i] = src[i] + adaptive_range[i]
            else:
                range_filter[i] = prev
            # 3. 趋势状态
            if range_filter[i] > range_filter[i - 1]:
                trend[i] = 1
            elif range_filter[i] < range_filter[i - 1]:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1]

        # 信号
        long_raw = (trend == 1) & (trend.shift(1) != 1)
        short_raw = (trend == -1) & (trend.shift(1) != -1)

        self.lines.filter_line = range_filter
        self.lines.trend = trend
        self.lines.long_signal = long_raw
        self.lines.short_signal = short_raw

    def _get_source(self)->IndSeries:
        """根据 src_type 参数获取价格序列"""
        st = self.params.src_type
        if st == 'close':
            return self.close
        elif st == 'hl2':
            return self.hl2()
        elif st == 'hlc3':
            return self.hlc3()
        elif st == 'ohlc4':
            return self.ohlc4()
        else:
            return self.close


class TrendlinesWithBreaks(BtIndicator):
    """
    动态趋势线 — 基于 Swing Pivot 的自适应支撑阻力趋势线
    基于 Pine Script "Trendlines with Breaks [LuxAlgo]" 转换
    参考 TradingView: https://cn.tradingview.com/script/IYL88A1N-Trendlines-with-Breaks-LuxAlgo/

    原理：
      在每个 swing pivot high/low 处，根据当时市场波动（ATR/Stdev/Linreg）
      计算斜率，从 pivot 点出发绘制趋势线：
        - upper: pivot high → 向下倾斜（下降趋势线 / 阻力线）
        - lower: pivot low → 向上倾斜（上升趋势线 / 支撑线）
      新 pivot 出现时自动重置，形成动态自适应趋势通道。

      注意：Pine Script 原版使用 offset 将趋势线左偏 length 根 bar，
      以使视觉上对齐 pivot 实际位置。这里输出的是原始确认数据（含延迟确认）。

      计算流程：
        1. pivothigh/pivotlow(left=right=length) 检测 swing 极值
        2. 按 calc_method 计算每根 bar 的斜率
        3. pivot 处捕获新斜率、重置趋势线起点
        4. 趋势线逐 bar 延伸（upper 递减，lower 递增）

    输出线：
      upper — 下降趋势线（阻力），趋势向下
      lower — 上升趋势线（支撑），趋势向上

    参数：
      length        — Swing 检测回溯周期，同时也是 pivot 左右确认 bar 数和斜率计算周期
      mult          — 斜率乘数，默认 1.0，越大趋势线越陡
      calc_method   — 斜率计算方式: 'Atr' / 'Stdev' / 'Linreg'，默认 'Atr'
      unique_signal — True 时每条趋势线仅显示一次突破信号（首次突破），
                      False 时所有突破 bar 都生成信号，默认 False
    """
    lines = ('upper', 'lower', 'long_signal', 'short_signal')
    params = dict(
        length=14,
        mult=1.0,
        calc_method='Atr',
        unique_signal=False,
    )
    overlap = True

    def __init__(self):
        upper, lower, long_signal, short_signal = self._compute_trendlines()
        self.lines.upper = upper
        self.lines.lower = lower
        self.lines.long_signal = long_signal
        self.lines.short_signal = short_signal
    
    def _compute_trendlines(self):
        """
        构建基于 pivot 的趋势线与突破信号。

        1. pivot 检测
        2. 三种斜率计算方法 → slope
        3. 在 pivot 处捕获新斜率 (slope_ph/slope_pl)，并重置 upper/lower
        4. 非 pivot 处 upper 递减 slope_ph、lower 递增 slope_pl

        Pine 中 upper 从 pivot 确认 bar 开始绘制，并左偏 length 根 bar
        以对齐实际 pivot 位置。这里输出原始值（含 rightBars 延迟确认偏移）。

        Args:
            unique_signal: 每条趋势线是否仅保留一个突破信号。
                        True  — 一条趋势线只显示一次突破信号（首次突破），
                                同趋势线的后续重复突破不再标记
                        False — 不限制，所有独立突破 bar 都生成信号

        Returns:
            upper — 下降趋势线（阻力）
            lower — 上升趋势线（支撑）
            long_signal  — 向上突破信号（价格上穿 upper）
            short_signal — 向下突破信号（价格下穿 lower）
        """
        length=self.params.length
        mult=self.params.mult
        calc_method=self.params.calc_method
        unique_signal=self.params.unique_signal
        n = self.close.size

        # 1. Pivot 检测
        ph = self.high.btind.pivot_high(length,length)#_pivot_high(high, length, length)
        pl = self.low.btind.pivot_low(length,length)#_pivot_low(low, length, length)

        # 2. 斜率计算
        if calc_method == 'Atr':
            slope = self.atr(length) * mult/length
        elif calc_method == 'Stdev':
            slope = self.close.stdev(length) * mult/length
        else:  # Linreg
            slope = self.close.btind.linreg(length) * mult / 2.0

        # 3. 构建趋势线 + 突破信号（逐 bar 迭代，模拟 Pine var 赋值语义）
        #    多扩展 length 根 bar：pivot 确认有 length 延迟，
        #    尾部继续按斜率延伸以补偿偏移，最后再截掉前 length 根。
        total = n + length
        close = self.close.values
        high = self.high.values
        low = self.low.values
        _close = np.append([0.,]*length, close)
        _high = np.append([0.,]*length, high)
        _low = np.append([0.,]*length, low)
        upper = np.full(total, np.nan, dtype=np.float64)
        lower = np.full(total, np.nan, dtype=np.float64)
        slope_ph = 0.0
        slope_pl = 0.0
        last_u = 0.0
        last_l = 0.0

        # Pine 突破信号:
        #   upos := ph ? 0 : close > upper - slope_ph * length ? 1 : upos[1]
        #   dnos := pl ? 0 : close < lower + slope_pl * length ? 1 : dnos[1]
        #   突破瞬间: upos > upos[1] / dnos > dnos[1] (0→1 跳变)
        prev_upos = 0
        prev_dnos = 0
        # 信号数组直接按 kline 索引存储，不做偏移切片
        long_signal = np.zeros(total, dtype=np.int32)
        short_signal = np.zeros(total, dtype=np.int32)

        for i in range(total):
            # pivot 处捕获新斜率并重置起点（超出 n 的部分无 ph/pl/slope，仅延续趋势线）
            if i < n:
                if not np.isnan(ph[i]) and np.isfinite(slope[i]):
                    slope_ph = slope[i]
                    last_u = ph[i]
                if not np.isnan(pl[i]) and np.isfinite(slope[i]):
                    slope_pl = slope[i]
                    last_l = pl[i]

            # 赋值当前 upper/lower（Pine: upper := ph ? ph : upper[1] - slope_ph）
            upper[i] = last_u
            lower[i] = last_l
            if not upper[i]:
                upper[i] = _high[i]
            if not lower[i]:
                lower[i] = _low[i]

            # 突破检测：基于刚刚赋值的 upper[i]/lower[i]（等价 Pine 当前 bar 值）
            # 突破检测：基于刚刚赋值的 upper[i]/lower[i]（等价 Pine 当前 bar 值）
            if i > length:
                c = _close[i]
                # upos — 价格向上突破下降趋势线（ph 处复位，趋势线未建立则不计算）
                if upper[i] > upper[i - 1]:
                    cur_upos = 0
                elif c > upper[i] and upper[i] < upper[i - 1]:
                    cur_upos = 1
                else:
                    cur_upos = prev_upos if unique_signal else 0
                # dnos — 价格向下突破上升趋势线（pl 处复位，趋势线未建立则不计算）
                if lower[i] < lower[i - 1]:
                    cur_dnos = 0
                elif c < lower[i] and lower[i] > lower[i - 1]:
                    cur_dnos = 1
                else:
                    cur_dnos = prev_dnos if unique_signal else 0
                if cur_upos > prev_upos :
                    long_signal[i] = 1
                if cur_dnos > prev_dnos :
                    short_signal[i] = 1
            else:
                cur_upos = 0#prev_upos
                cur_dnos = 0#prev_dnos

            prev_upos = cur_upos
            prev_dnos = cur_dnos

            # 为下一 bar：趋势线延伸（upper 递减 / lower 递增）
            last_u -= slope_ph
            last_l += slope_pl

        # 截掉前 length 根（第一 pivot 确认前的无效数据），保留 n 根与 kline 对齐
        upper = upper[length:]
        lower = lower[length:]
        # 信号数组：截取后 n 根（与 upper/lower 长度一致），前 length 根为 0
        long_signal = long_signal[length:]
        short_signal = short_signal[length:]


        return upper, lower, long_signal, short_signal
    
    
class VWAPPriceChannel(BtIndicator):
    """
    VWAP 价格通道 (VWAP Price Channel)
    基于 Pine Script "VWAP Price Channel [SamRecio]" 转换
    https://cn.tradingview.com/script/Psnjpa2Y-VWAP-Price-Channel/
    
    参数:
    - len:  最高/最低检测窗口，默认 20
    
    核心逻辑：
    1. 检测 _len 周期内的最高价/最低价是否刷新
    2. 在最高/最低刷新处锚定 VWAP(high) / VWAP(low)
    3. 上轨 = VWAP 通道中轴上轨，动态跟随 hst 或 VWAP 趋势
    4. 下轨 = VWAP 通道中轴下轨，动态跟随 lst 或 VWAP 趋势
    5. 中轴 = (upper + lower) / 2
    """

    lines = ('upper', 'lower', 'mid', 'hst', 'lst')
    params = dict(len=20)
    overlap = True

    def __init__(self):
        high = self.kline.high.values
        low = self.kline.low.values
        n = len(high)
        _len = self.params.len

        # 1. 滚动最高/最低
        hst = self.high.tqfunc.hhv(_len)
        lst = self.low.tqfunc.llv(_len)

        # 2. 新高/新低标记
        new_high = np.where(np.isnan(hst), False, high == hst)
        new_low = np.where(np.isnan(lst), False, low == lst)

        # 3. 锚定 VWAP
        h_vwap = self.btind.vwap_high(_len)
        l_vwap = self.btind.vwap_low(_len)

        # 4. VWAP 变化量
        h_change = h_vwap.diff()
        l_change = l_vwap.diff()

        # 5. 通道上/下轨（逐 bar 推进，完全复刻 Pine 三元嵌套逻辑）
        upper = np.full(n, np.nan)
        lower = np.full(n, np.nan)

        for i in range(n):
            # --- 上轨 ---
            if new_high[i]:
                upper[i] = hst[i]
            elif i == 0:
                upper[i] = np.nan
            else:
                hst_same = self._pine_eq(hst[i], hst[i - 1])
                trend = (upper[i - 1] + h_change[i]
                        if not np.isnan(upper[i - 1]) else np.nan)
                if hst_same:
                    upper[i] = trend
                else:
                    upper[i] = (np.fmin(hst[i], trend)
                                if not np.isnan(trend) else hst[i])

            # --- 下轨 ---
            if new_low[i]:
                lower[i] = lst[i]
            elif i == 0:
                lower[i] = np.nan
            else:
                lst_same = self._pine_eq(lst[i], lst[i - 1])
                trend = (lower[i - 1] + l_change[i]
                        if not np.isnan(lower[i - 1]) else np.nan)
                if lst_same:
                    lower[i] = trend
                else:
                    lower[i] = (np.fmax(lst[i], trend)
                                if not np.isnan(trend) else lst[i])

        # 6. 中轴
        mid = (upper + lower) / 2.0

        self.lines.upper = upper
        self.lines.lower = lower
        self.lines.mid = mid
        self.lines.hst=hst
        self.lines.lst=lst
    
    @staticmethod
    def _pine_eq(a: float, b: float) -> bool:
        """Pine Script 等值判断：na == na → True"""
        if np.isnan(a) and np.isnan(b):
            return True
        return a == b



class TradingView:
    """
    ## TradingView 社区策略指标集合类

    - **TradingView 策略指标集合类**，用于将 TradingView 平台上广受欢迎的交易策略和指标转换为框架内置的指标数据类型（IndSeries/IndFrame）

    ### 📘 **API文档参考**:
    - https://www.minibt.cn/minibt_api_reference/tradingview/

    ### 核心功能：
    - 封装 TradingView 社区的优质策略指标，提供统一的调用接口
    - 通过 BtIndicator 基类自动处理指标参数校验、计算逻辑调用和返回值转换，确保输出为框架兼容的 IndSeries 或 IndFrame
    - 支持多维度交易策略场景，覆盖趋势跟踪、均值回归、波动率分析、动量交易等量化交易核心需求
    - 内置策略分类体系，便于按交易风格和策略类型快速定位和调用目标指标

    ### 策略分类与包含列表：
    该类支持的策略指标按功能划分为以下 8 大类，具体包含指标如下：

    #### **1. 趋势跟踪策略（Trend Following）**
       - 功能：识别和跟踪市场趋势方向，在趋势启动时入场，趋势结束时出场
       - 包含策略：Powertrend_Volume_Range_Filter_Strategy、Nadaraya_Watson_Envelope_Strategy、Adaptive_Trend_Filter、
       - Multi_Step_Vegas_SuperTrend_strategy、RJ_Trend_Engine、AlphaTrend、SuperTrend、SuperTrend_STRATEGY、Optimized_Trend_Tracker

    #### **2. 均值回归策略（Mean Reversion）**
       - 功能：在价格偏离均值时入场，预期价格回归均值时出场
       - 包含策略：DCA_Strategy_with_Mean_Reversion_and_Bollinger_Band、Bollinger_RSI_Double_Strategy、CM_Williams_Vix_Fix_Finds_Market_Bottoms

    #### **3. 突破策略（Breakout）**
       - 功能：在价格突破关键支撑阻力位时入场，捕捉趋势启动机会
       - 包含策略：Turtles_strategy、Turtle_Trade_Channels_Indicator_TUTCI、G_Channels、Twin_Range_Filter

    #### **4. 动量策略（Momentum）**
       - 功能：基于价格和成交量的动量变化识别交易机会
       - 包含策略：The_Flash_Strategy、WaveTrend_Oscillator、TonyUX_EMA_Scalper、Volume_Flow_Indicator

    #### **5. 波动率策略（Volatility）**
       - 功能：基于市场波动率变化调整交易参数和风险管理
       - 包含策略：STD_Filtered、PMax_Explorer、PMax_Explorer_STRATEGY、Chandelier_Exit、Pivot_Point_Supertrend

    #### **6. 机器学习策略（Machine Learning）**
       - 功能：基于自适应算法和AI技术优化策略参数
       - 包含策略：Quantum_Edge_Pro_Adaptive_AI、LOWESS

    #### **7. 信号处理策略（Signal Processing）**
       - 功能：基于信号处理理论分析价格数据
       - 包含策略：The_Price_Radio、ADX_and_DI

    #### **8. 风险管理策略（Risk Management）**
       - 功能：专注于头寸管理和风险控制的策略工具
       - 包含策略：Chandelier_Exit、Turtles_strategy

    ### 使用说明：
    #### 1. 初始化：
    - 传入框架支持的 KLine、IndFrame 或 IndSeries 数据对象（需包含策略计算所需的基础字段，如 open、high、low、close、volume 等）
    >>> data = IndFrame(...)  # 框架内置数据对象（含OHLCV等基础字段）
    >>> tv = TradingView(data)

    #### 2. 策略调用：
    - 直接调用对应策略方法，传入必要参数（默认参数已适配常见场景，可按需调整）
    >>> # 示例1：调用海龟交易策略
    >>> # 返回框架内置IndFrame，含多空信号和出场信号
    >>> turtle_signals = tv.Turtles_strategy(enter_fast=20, exit_fast=10, enter_slow=55, exit_slow=20)
    >>> # 示例2：调用超级趋势策略
    >>> supertrend_data = tv.SuperTrend_STRATEGY(Periods=10, Multiplier=3.0)
    >>> # 示例3：调用自适应AI策略
    >>> ai_scores = tv.Quantum_Edge_Pro_Adaptive_AI(LEARNING_PERIOD=40, ADAPTATION_SPEED=0.3)

    #### 3. 返回值特性：
    - 所有方法返回框架内置的 IndSeries 或 IndFrame 类型，可直接用于后续策略逻辑（如信号生成、风险控制），无需额外类型转换

    ### 策略集成示例：
    ```python
    class AdvancedStrategy(Strategy):
        def __init__(self):
            self.data = self.get_data(LocalDatas.test)
            self.tv = self.data.tradingview

            # 多重策略信号集成
            self.trend_signals = self.tv.SuperTrend_STRATEGY(Periods=10, Multiplier=3.0)
            self.momentum_signals = self.tv.WaveTrend_Oscillator(n1=10, n2=21, n3=9)
            self.volume_signals = self.tv.Volume_Flow_Indicator(length=130, coef=0.2)

        def next(self):
            if not self.data.position:
                # 趋势确认 + 动量确认 + 成交量确认
                long_condition = (self.trend_signals.long_signal.new & 
                                 (self.momentum_signals.wt1.new > 0) & 
                                 (self.volume_signals.vfi.new > 0))

                short_condition = (self.trend_signals.short_signal.new & 
                                  (self.momentum_signals.wt1.new < 0) & 
                                  (self.volume_signals.vfi.new < 0))

                # 执行交易逻辑
                if long_condition:
                    self.data.buy()
                elif short_condition:
                    self.data.sell()
    ```

    ### 注意事项：
    - 不同策略对基础数据字段要求不同，调用前确保输入数据包含所需字段（如成交量策略需要volume字段）
    - 策略参数对性能影响显著，建议通过回测优化确定最佳参数组合
    - 复杂策略（如AI自适应策略）需要足够的历史数据才能有效工作
    - 建议在模拟环境中充分测试策略表现后再实盘应用
    - 可结合框架的风险管理模块控制单策略和组合风险

    ### 性能优化建议：
    - 1. **参数调优**：使用框架的回测工具对策略参数进行优化
    - 2. **组合使用**：将不同策略信号组合使用，提高系统稳定性
    - 3. **风险分散**：在同一策略类别中选择多个不相关策略分散风险
    - 4. **市场适应**：根据不同市场环境动态调整策略权重
    - 5. **监控评估**：定期评估策略表现，及时调整或替换失效策略
    """
    _kline: KLine | IndFrame | IndSeries | Line

    def __init__(self, kline: KLine | IndFrame | IndSeries | Line ,**kwargs):
        # if kwargs.pop("ischeck", True):
        #     assert isinstance(kline, (KLine, IndFrame)), (
        #         f"❌：tradingview指标初始化数据类型需为KLine或包含OHLCV列的IndFrame，传入的数据格式为{type(kline)}")
        self._kline = kline

    def Powertrend_Volume_Range_Filter_Strategy(self, l=200, lengthvwma=200, mult=3., lengthadx=200, lengthhl=14,
                                                useadx=False, usehl=False, usevwma=False, highlighting=True, **kwargs) -> IndFrame:
        """
        ## 成交量范围过滤策略

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.1_powertrend_volume_range_filter/
        - ✈ https://cn.tradingview.com/script/45FlB2qH-Powertrend-Volume-Range-Filter-Strategy-wbburgin/

        Args:
            l: 平滑范围周期
            lengthvwma: 成交量加权移动平均周期
            mult: 范围乘数
            lengthadx: ADX指标周期
            lengthhl: 高低点周期
            useadx: 是否使用ADX过滤
            usehl: 是否使用高低点过滤
            usevwma: 是否使用VWMA过滤
            highlighting: 是否高亮显示
            **kwargs: 其他参数

        Returns:
            tuple: (volrng, hband, lowband, dir, long_signal, short_signal)
                - volrng: 成交量范围过滤线
                - hband: 上轨
                - lowband: 下轨
                - dir: 方向指标
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return Powertrend_Volume_Range_Filter_Strategy(
            self._kline, l=l, lengthvwma=lengthvwma, mult=mult,
            lengthadx=lengthadx, lengthhl=lengthhl, useadx=useadx,
            usehl=usehl, usevwma=usevwma, highlighting=highlighting, **kwargs
        )

    def Nadaraya_Watson_Envelope_Strategy(self, customLookbackWindow=8., customRelativeWeighting=8., customStartRegressionBar=25.,
                                          length=60, customATRLength=60, customNearATRFactor=1.5, customFarATRFactor=2., **kwargs) -> IndFrame:
        """
        ## Nadaraya-Watson包络线策略

        ### 📘 **文档参考**:
        - ✈ https://cn.tradingview.com/script/HrZicISx-Nadaraya-Watson-Envelope-Strategy-Non-Repainting-Log-Scale/

        Args:
            customLookbackWindow: 自定义回看窗口
            customRelativeWeighting: 自定义相对权重
            customStartRegressionBar: 自定义回归起始柱
            length: 周期长度
            customATRLength: ATR周期长度
            customNearATRFactor: 近端ATR因子
            customFarATRFactor: 远端ATR因子
            **kwargs: 其他参数

        Returns:
            tuple: (customEnvelopeClose, customEnvelopeHigh, customEnvelopeLow, customUpperNear,
                   customUpperFar, customUpperAvg, customLowerNear, customLowerFar, customLowerAvg,
                   long_signal, short_signal)
        """
        return Nadaraya_Watson_Envelope_Strategy(
            self._kline, customLookbackWindow=customLookbackWindow,
            customRelativeWeighting=customRelativeWeighting,
            customStartRegressionBar=customStartRegressionBar, length=length,
            customATRLength=customATRLength, customNearATRFactor=customNearATRFactor,
            customFarATRFactor=customFarATRFactor, **kwargs
        )

    def G_Channels(self, length=144., cycle=1, thresh=0., **kwargs) -> IndFrame:
        """
        ## G通道指标 - 高效计算上下极值点

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.2_g_channels/
        - ✈ https://www.tradingview.com/script/fIvlS64B-G-Channels-Efficient-Calculation-Of-Upper-Lower-Extremities/

        Args:
            length: 通道长度
            cycle: 周期循环
            thresh: zig参数
            **kwargs: 其他参数

        Returns:
            G_Channels: 包含a(上轨), b(下轨), avg(中轨), zig(之字转向)线的指标对象
        """
        return G_Channels(self._kline, length=length, cycle=cycle, thresh=thresh, **kwargs)

    def STD_Filtered(self, period=25, order=5, filterperiod=10, filter=1., **kwargs) -> IndFrame:
        """
        ## STD过滤N极高斯滤波器

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.3_std_filtered_n_pole_gaussian_filter/
        - ✈ https://cn.tradingview.com/script/i4xZNAoy-STD-Filtered-N-Pole-Gaussian-Filter-Loxx/

        Args:
            period: 主周期
            order: 阶数
            filterperiod: 过滤周期
            filter: 过滤因子
            **kwargs: 其他参数

        Returns:
            tuple: (filt, long_signal, short_signal)
                - filt: 过滤后的信号线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return STD_Filtered(
            self._kline, period=period, order=order,
            filterperiod=filterperiod, filter=filter, **kwargs
        )

    def Turtles_strategy(self, enter_fast=20, exit_fast=10, enter_slow=55, exit_slow=20, **kwargs) -> IndFrame:
        """
        ## 海龟交易策略 - 历经20年验证的有效策略

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/Q1O23zJP-20-years-old-turtles-strategy-still-work/

        Args:
            enter_fast: 快速入场周期
            exit_fast: 快速出场周期
            enter_slow: 慢速入场周期
            exit_slow: 慢速出场周期
            **kwargs: 其他参数

        Returns:
            tuple: (long_signal, short_signal,
                    exitlong_signal, exitshort_signal)
                - long_signal: 多头入场信号
                - short_signal: 空头入场信号
                - exitlong_signal: 多头出场信号
                - exitshort_signal: 空头出场信号
        """
        return Turtles_strategy(
            self._kline, enter_fast=enter_fast, exit_fast=exit_fast,
            enter_slow=enter_slow, exit_slow=exit_slow, **kwargs
        )

    def Adaptive_Trend_Filter(self, alphaFilter=0.01, betaFilter=0.1, filterPeriod=21,
                              supertrendFactor=1, supertrendAtrPeriod=7, **kwargs) -> IndFrame:
        """
        ## 自适应趋势过滤器

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.4_adaptive_trend_filter/
        - ✈ https://cn.tradingview.com/script/PhSlALob-Adaptive-Trend-Filter-tradingbauhaus/

        Args:
            alphaFilter: Alpha过滤参数
            betaFilter: Beta过滤参数
            filterPeriod: 过滤周期
            supertrendFactor: 超级趋势因子
            supertrendAtrPeriod: 超级趋势ATR周期
            **kwargs: 其他参数

        Returns:
            IndFrame: (filteredValue, supertrendValue, trendDirection)
                - filteredValue: 过滤后的数值
                - supertrendValue: 超级趋势值
                - trendDirection: 趋势方向
        """
        return Adaptive_Trend_Filter(
            self._kline, alphaFilter=alphaFilter, betaFilter=betaFilter,
            filterPeriod=filterPeriod, supertrendFactor=supertrendFactor,
            supertrendAtrPeriod=supertrendAtrPeriod, **kwargs
        )

    def DCA_Strategy_with_Mean_Reversion_and_Bollinger_Band(self, length=14, mult=2., **kwargs) -> IndFrame:
        """
        ## 均值回归与布林带结合的DCA策略

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/uVaU9LVC-DCA-Strategy-with-Mean-Reversion-and-Bollinger-Band/

        Args:
            length: 布林带周期
            mult: 布林带乘数
            **kwargs: 其他参数

        Returns:
            tuple: (upper, lower, long_signal, short_signal)
                - upper: 布林带上轨
                - lower: 布林带下轨
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return DCA_Strategy_with_Mean_Reversion_and_Bollinger_Band(
            self._kline, length=length, mult=mult, **kwargs
        )

    def Multi_Step_Vegas_SuperTrend_strategy(self, atrPeriod=10, vegasWindow=100,
                                             superTrendMultiplier=5, volatilityAdjustment=5, matype="jma", **kwargs) -> IndFrame:
        """
        ## 多步维加斯超级趋势策略

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.5_multi_step_vegas_supertrend/
        - ✈ https://cn.tradingview.com/script/SXtas3lS-Multi-Step-Vegas-SuperTrend-strategy-presentTrading/

        Args:
            atrPeriod: ATR周期
            vegasWindow: 维加斯窗口
            superTrendMultiplier: 超级趋势乘数
            volatilityAdjustment: 波动率调整
            matype: 移动平均类型
            **kwargs: 其他参数

        Returns:
            tuple: (superTrend, marketTrend, long_signal, short_signal)
                - superTrend: 超级趋势线
                - marketTrend: 市场趋势
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return Multi_Step_Vegas_SuperTrend_strategy(
            self._kline, atrPeriod=atrPeriod, vegasWindow=vegasWindow,
            superTrendMultiplier=superTrendMultiplier,
            volatilityAdjustment=volatilityAdjustment, matype=matype, **kwargs
        )

    def The_Flash_Strategy(self, length=10, mom_rsi_val=50, atrPeriod=10,
                           factor=3., AP2=12, AF2=.1618, **kwargs) -> IndFrame:
        """
        ## Flash策略 - 动量RSI与EMA交叉结合ATR

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.6_the_flash_strategy/
        - ✈ https://cn.tradingview.com/script/XKgLfo15-The-Flash-Strategy-Momentum-RSI-EMA-crossover-ATR/

        Args:
            length: RSI周期
            mom_rsi_val: 动量RSI阈值
            atrPeriod: ATR周期
            factor: 超级趋势因子
            AP2: 自适应周期参数2
            AF2: 自适应因子2
            **kwargs: 其他参数

        Returns:
            tuple: (supertrend, Trail2, long_signal, short_signal)
                - supertrend: 超级趋势线
                - Trail2: 跟踪止损线2
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return The_Flash_Strategy(
            self._kline, length=length, mom_rsi_val=mom_rsi_val,
            atrPeriod=atrPeriod, factor=factor, AP2=AP2, AF2=AF2, **kwargs
        )

    def Quantum_Edge_Pro_Adaptive_AI(self, TICK_SIZE=0.25, POINT_VALUE=2, DOLLAR_PER_POINT=2, LEARNING_PERIOD=40, ADAPTATION_SPEED=0.3,
                                     PERFORMANCE_MEMORY=200, BASE_MIN_SCORE=2, BASE_BARS_BETWEEN=9, MAX_DAILY_TRADES=50, **kwargs) -> IndFrame:
        """
        ## 量子边缘专业自适应AI策略

        ✈ https://cn.tradingview.com/script/iGZZmHEo-Quantum-Edge-Pro-Adaptive-AI/

        Args:
            TICK_SIZE: 最小变动价位
            POINT_VALUE: 点值
            DOLLAR_PER_POINT: 每点价格价值
            LEARNING_PERIOD: 学习周期
            ADAPTATION_SPEED: 适应速度
            PERFORMANCE_MEMORY: 性能记忆周期
            BASE_MIN_SCORE: 基础最小分数
            BASE_BARS_BETWEEN: 基础间隔柱数
            MAX_DAILY_TRADES: 最大日交易次数
            **kwargs: 其他参数

        Returns:
            weighted_score: 加权评分信号
        """
        return Quantum_Edge_Pro_Adaptive_AI(
            self._kline, TICK_SIZE=TICK_SIZE, POINT_VALUE=POINT_VALUE,
            DOLLAR_PER_POINT=DOLLAR_PER_POINT, LEARNING_PERIOD=LEARNING_PERIOD,
            ADAPTATION_SPEED=ADAPTATION_SPEED, PERFORMANCE_MEMORY=PERFORMANCE_MEMORY,
            BASE_MIN_SCORE=BASE_MIN_SCORE, BASE_BARS_BETWEEN=BASE_BARS_BETWEEN,
            MAX_DAILY_TRADES=MAX_DAILY_TRADES, **kwargs
        )

    def SRChannels(self,
                   prd=10,
                   ppsrc='High/Low',
                   channel_w=5,
                   min_strength=1,
                   max_numsr=6,
                   loopback=290,
                   **kwargs) -> IndFrame:
        """
        ## SR Channels — 支撑阻力通道 [LonesomeTheBlue]
        https://cn.tradingview.com/script/qX1Zv6O7-Support-Resistance-Channels-LonesomeTheBlue/

        原理：
        基于枢轴高点和低点构建支撑阻力通道，贪心纳入通道宽度范围内的其他枢轴，
        计算通道强度（枢轴数 × 20 + K线触及数），保留最强的 top N 通道。
        价格不在通道内时，上穿阻力上轨触发做多，下穿支撑下轨触发做空。

        计算流程：
            1. 检测枢轴高/低点（pivot high/low），左右各 prd 根 bar 的极值
            2. 维护枢轴列表，超出 loopback 的旧枢轴被移除
            3. 每新增枢轴时重算通道：
               - 以每个枢轴为起点，贪心纳入 cwidth 范围内的其他枢轴
               - 通道强度 = 纳入枢轴数 × 20 + loopback 内 K 线触及通道数
               - 选择强度最高的 top N 通道（排除重叠通道）
            4. 生成信号：
               - 价格不在任何通道内时，上穿阻力上轨 → long_signal
               - 价格不在任何通道内时，下穿支撑下轨 → short_signal

        输出线：
            sr_high      — 通道上轨包络线（取所有通道 hi 的最大值），主图叠加
            sr_low       — 通道下轨包络线（取所有通道 lo 的最小值），主图叠加
            long_signal  — 做多信号（价格上穿阻力通道上轨，非通道内），不绘图
            short_signal — 做空信号（价格下穿支撑通道下轨，非通道内），不绘图

        参数：
            prd           — 枢轴检测窗口（左右各 N 根），默认 10
            ppsrc         — 枢轴源: 'High/Low' 或 'Close/Open'，默认 'High/Low'
            channel_w     — 最大通道宽度 %（基于最近 300 根 K 线振幅），默认 5
            min_strength  — 最低通道强度（至少含 2 个枢轴 = 40），默认 1
            max_numsr     — 最多保留通道数，默认 6
            loopback      — 枢轴回看周期，默认 290
        """
        return SRChannels(self._kline, prd=prd, ppsrc=ppsrc, channel_w=channel_w,
                          min_strength=min_strength, max_numsr=max_numsr,
                          loopback=loopback, **kwargs)

    def LOWESS(self, length=100, malen=100, **kwargs) -> IndFrame:
        """
        ## LOWESS局部加权散点图平滑

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.8_lowess_locally_weighted_scatterplot_smoothing/
        - ✈ https://cn.tradingview.com/script/hyeoDyZn-LOWESS-Locally-Weighted-Scatterplot-Smoothing-ChartPrime/

        Args:
            length: 主周期长度
            malen: 移动平均长度
            **kwargs: 其他参数

        Returns:
            tuple: (GaussianMA, smoothed)
                - GaussianMA: 高斯移动平均
                - smoothed: 平滑后的信号
        """
        return LOWESS(self._kline, length=length, malen=malen, **kwargs)

    def The_Price_Radio(self, length=60, period=14, **kwargs) -> IndFrame:
        """
        ## John Ehlers价格收音机指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.9_john_ehlers_the_price_radio/
        - ✈ https://cn.tradingview.com/script/W5lBL0MV-John-Ehlers-The-Price-Radio/

        Args:
            length: 周期长度
            period: 变化周期
            **kwargs: 其他参数

        Returns:
            tuple: (deriv, amup, amdn, fm)
                - deriv: 价格导数
                - amup: AM上轨
                - amdn: AM下轨
                - fm: FM信号
        """
        return The_Price_Radio(self._kline, length=length, period=period, **kwargs)

    def PMax_Explorer(self, Periods=10, Multiplier=3, mav="ema", length=10, var_length=9, **kwargs) -> IndFrame:
        """
        ## PMax探索者指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.10_pmax_explorer/
        - ✈ https://cn.tradingview.com/script/nHGK4Qtp/

        Args:
            Periods: ATR周期
            Multiplier: 乘数因子
            mav: 移动平均类型
            length: 移动平均长度
            var_length: 变异长度
            **kwargs: 其他参数

        Returns:
            pmax: PMax指标线
        """
        return PMax_Explorer(
            self._kline, Periods=Periods, Multiplier=Multiplier,
            mav=mav, length=length, var_length=var_length, **kwargs
        )

    def VMA_Win(self, length=15, **kwargs) -> IndFrame:
        """
        ## VMA赢家仪表板 - 不同长度的VMA分析

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/09F2GICn-VMA-Win-Dashboard-for-Different-Lengths/

        Args:
            length: VMA周期长度
            **kwargs: 其他参数

        Returns:
            vma: 变异移动平均线
        """
        return VMA_Win(self._kline, length=length, **kwargs)

    def RJ_Trend_Engine(self, psarStart=0.02, psarIncrement=0.02, psarMax=0.2, stAtrPeriod=10,
                        stFactor=3.0, adxLen=14, adxThreshold=20, bbLength=20, bbStdDev=3.0, **kwargs) -> IndFrame:
        """
        ## RJ趋势引擎 - 最终版本

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.11_rj_trend_engine_final_version/
        - ✈ https://cn.tradingview.com/script/xZ9IlWfi-RJ-Trend-Engine-Final-Version/

        Args:
            psarStart: SAR起始值
            psarIncrement: SAR增量
            psarMax: SAR最大值
            stAtrPeriod: 超级趋势ATR周期
            stFactor: 超级趋势因子
            adxLen: ADX长度
            adxThreshold: ADX阈值
            bbLength: 布林带长度
            bbStdDev: 布林带标准差
            **kwargs: 其他参数

        Returns:
            tuple: (psar, supertrend, long_signal, short_signal)
                - psar: 抛物线转向指标
                - supertrend: 超级趋势线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return RJ_Trend_Engine(
            self._kline, psarStart=psarStart, psarIncrement=psarIncrement,
            psarMax=psarMax, stAtrPeriod=stAtrPeriod, stFactor=stFactor,
            adxLen=adxLen, adxThreshold=adxThreshold, bbLength=bbLength,
            bbStdDev=bbStdDev, **kwargs
        )

    def Twin_Range_Filter(self, per1=127, mult1=1.6, per2=155, mult2=2.0, **kwargs) -> IndFrame:
        """
        ## 双范围过滤器 - 买卖信号生成

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.12_twin_kange_filter/
        - ✈ https://cn.tradingview.com/script/57i9oK2t-Twin-Range-Filter-Buy-Sell-Signals/

        Args:
            per1: 第一个周期
            mult1: 第一个乘数
            per2: 第二个周期
            mult2: 第二个乘数
            **kwargs: 其他参数

        Returns:
            tuple: (filt, long_signal, short_signal)
                - filt: 过滤线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return Twin_Range_Filter(
            self._kline, per1=per1, mult1=mult1, per2=per2, mult2=mult2, **kwargs
        )

    def PMax_Explorer_STRATEGY(self, Periods=10, Multiplier=3., mav="ema", length=10, **kwargs) -> IndFrame:
        """
        ## PMax探索者策略

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/nHGK4Qtp/

        Args:
            Periods: 周期参数
            Multiplier: 乘数因子
            mav: 移动平均类型
            length: 移动平均长度
            **kwargs: 其他参数

        Returns:
            tuple: (pmax, thrend, long_signal, short_signal)
                - pmax: PMax指标线
                - thrend: 趋势线
                - long_signal: 多头信号
                - short_signal: 空头信号
        """
        return PMax_Explorer_STRATEGY(
            self._kline, Periods=Periods, Multiplier=Multiplier,
            mav=mav, length=length, **kwargs
        )

    def UT_Bot_Alerts(self, a=1., c=10, h=False, **kwargs) -> IndFrame:
        """
        ## UT Bot 警报指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.13_ut_bot_alerts/
        - ✈ https://cn.tradingview.com/script/n8ss8BID-UT-Bot-Alerts/

        Args:
            a: 调整因子参数，默认 `1.0`
            c: 周期相关参数，默认 `10`
            h: 布尔值，控制是否启用高亮等额外显示逻辑，默认 `False`
            **kwargs: 其他扩展参数

        Returns:
            IndFrame:alerts, long_signal, short_signal
            UT Bot 警报相关的指标数据（如信号标记、警报触发状态等）
        """
        return UT_Bot_Alerts(self._kline, a=a, c=c, h=h, **kwargs)

    def SuperTrend(self, Periods=10, Multiplier=3., changeATR=True, **kwargs) -> IndFrame:
        """
        ## 超级趋势指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.14_supertrend/
        - ✈ https://cn.tradingview.com/script/r6dAP7yi/

        Args:
            Periods: ATR 计算周期，默认 `10`
            Multiplier: ATR 乘数（用于调整趋势带宽度），默认 `3.0`
            changeATR: 布尔值，控制是否采用特殊逻辑计算 ATR，默认 `True`
            **kwargs: 其他扩展参数

        Returns:
            超级趋势指标相关数据（如趋势线位置、多空趋势信号等）
        """
        return SuperTrend(self._kline, Periods=Periods, Multiplier=Multiplier, changeATR=changeATR, **kwargs)

    def CM_Williams_Vix_Fix_Finds_Market_Bottoms(self, hl=22, bbl=20, mult=2.0, lb=50, ph=.85, pl=1.01, **kwargs) -> IndFrame:
        """
        ## CM Williams Vix Fix 市场底部识别指标

        ### 📘 **文档参考**:
        - ✈ https://cn.tradingview.com/script/og7JPrRA-CM-Williams-Vix-Fix-Finds-Market-Bottoms/

        Args:
            hl: 高低点计算周期，默认 `22`
            bbl: 布林带周期，默认 `20`
            mult: 布林带乘数，默认 `2.0`
            lb: 历史范围统计周期，默认 `50`
            ph: 高点范围系数，默认 `0.85`
            pl: 低点范围系数，默认 `1.01`
            **kwargs: 其他扩展参数

        Returns:
            包含 `vwf`（波动率指标）、`lowerBand`（下轨）、`upperBand`（上轨）、`rangeHigh`（高范围）、`rangeLow`（低范围）的指标数据
        """
        return CM_Williams_Vix_Fix_Finds_Market_Bottoms(self._kline, hl=hl, bbl=bbl, mult=mult, lb=lb, ph=ph, pl=pl, **kwargs)

    def WaveTrend_Oscillator(self, n1=10, n2=21, n3=9, **kwargs) -> IndFrame:
        """
        ## WaveTrend 振荡器指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.15_wavetrend_oscillator/
        - ✈ https://cn.tradingview.com/script/2KE8wTuF-Indicator-WaveTrend-Oscillator-WT/

        Args:
            n1: 第一周期参数，默认 `10`
            n2: 第二周期参数，默认 `21`
            n3: 第三周期参数，默认 `9`
            **kwargs: 其他扩展参数

        Returns:
            包含 `signal`（信号值）、`wt1`（WaveTrend 核心值1）、`wt2`（WaveTrend 核心值2）的指标数据
        """
        return WaveTrend_Oscillator(self._kline, n1=n1, n2=n2, n3=n3, **kwargs)

    def ADX_and_DI(self, length=14, **kwargs) -> IndFrame:
        """
        ## ADX 与 DI 指标（平均方向指数 + 方向指标）

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/VTPMMOrx-ADX-and-DI/

        Args:
            length: ADX 计算周期，默认 `14`
            **kwargs: 其他扩展参数

        Returns:
            包含 `ADX`（趋势强度）、`DIPlus`（正向方向指标）、`DIMinus`（负向方向指标）的指标数据
        """
        return ADX_and_DI(self._kline, length=14, **kwargs)

    def Bollinger_RSI_Double_Strategy(self, RSIlength=6, RSIoverSold=50., RSIoverBought=50., BBlength=200, BBmult=2., **kwargs) -> IndFrame:
        """
        ## 布林带 + RSI 双重策略

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/uCV8I4xA-Bollinger-RSI-Double-Strategy-by-ChartArt-v1-1/

        Args:
            RSIlength: RSI 计算周期，默认 `6`
            RSIoverSold: RSI 超卖阈值，默认 `50.0`
            RSIoverBought: RSI 超买阈值，默认 `50.0`
            BBlength: 布林带计算周期，默认 `200`
            BBmult: 布林带乘数，默认 `2.0`
            **kwargs: 其他扩展参数

        Returns:
            包含 `BBupper`（布林带上轨）、`BBlower`（布林带下轨）、`long_signal`（多头信号）、`short_signal`（空头信号）的策略数据
        """
        return Bollinger_RSI_Double_Strategy(self._kline, RSIlength=RSIlength, RSIoverSold=RSIoverSold, RSIoverBought=RSIoverBought,
                                             BBlength=BBlength, BBmult=BBmult, **kwargs)

    def Pivot_Point_Supertrend(self, prd=2, Factor=3, Pd=10, **kwargs) -> IndFrame:
        """
        ## 枢轴点 + 超级趋势指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.16_pivot_point_supertrend/
        - ✈ https://cn.tradingview.com/script/L0AIiLvH-Pivot-Point-Supertrend/

        Args:
            prd: 枢轴点计算周期相关参数，默认 `2`
            Factor: 超级趋势乘数，默认 `3`
            Pd: ATR 计算周期，默认 `10`
            **kwargs: 其他扩展参数

        Returns:
            包含 `Trailingsl`（跟踪止损线）、`long_signal`（多头信号）、`short_signal`（空头信号）的指标数据
        """
        return Pivot_Point_Supertrend(self._kline, prd=prd, Factor=Factor, Pd=Pd, **kwargs)

    def AlphaTrend(self, coeff=1., AP=14, novolumedata=False, **kwargs) -> IndFrame:
        """
        ## Alpha 趋势指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.17_alphatrend/
        - ✈ https://cn.tradingview.com/script/o50NYLAZ-AlphaTrend/

        Args:
            coeff: 系数参数，默认 `1.0`
            AP: ATR 计算周期，默认 `14`
            novolumedata: 布尔值，控制是否禁用成交量数据，默认 `False`
            **kwargs: 其他扩展参数

        Returns:
            包含 `AlphaTrend`（趋势线）、`AlphaTrend2`（延迟趋势线）、`long_signal`（多头信号）、`short_signal`（空头信号）的指标数据
        """
        return AlphaTrend(self._kline, coeff=coeff, AP=AP, novolumedata=novolumedata, **kwargs)

    def Volume_Flow_Indicator(self, length=130, coef=0.2, vcoef=2.5, signalLength=5, smoothVFI=False, **kwargs) -> IndFrame:
        """
        ## 成交量流量指标

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/MhlDpfdS-Volume-Flow-Indicator-LazyBear/

        Args:
            length: 基础周期，默认 `130`
            coef: 系数参数，默认 `0.2`
            vcoef: 成交量系数，默认 `2.5`
            signalLength: 信号平滑周期，默认 `5`
            smoothVFI: 布尔值，控制是否平滑 VFI，默认 `False`
            **kwargs: 其他扩展参数

        Returns:
            包含 `vfima`（成交量流量指数移动平均）、`vfi`（成交量流量指标）的指标数据
        """
        return Volume_Flow_Indicator(self._kline, length=length, coef=coef, vcoef=vcoef, signalLength=signalLength, smoothVFI=smoothVFI, **kwargs)

    def Chandelier_Exit(self, length=22, mult=3., useClose=True, **kwargs) -> IndFrame:
        """
        ## 吊灯出场指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.18_chandelier_exit/
        - ✈ https://cn.tradingview.com/script/AqXxNS7j-Chandelier-Exit/

        Args:
            length: 周期参数，默认 `22`
            mult: ATR 乘数，默认 `3.0`
            useClose: 布尔值，控制是否用收盘价计算，默认 `True`
            **kwargs: 其他扩展参数

        Returns:
            包含 `up`（上轨止损线）、`dn`（下轨止损线）、`long_signal`（多头信号）、`short_signal`（空头信号）的指标数据
        """
        return Chandelier_Exit(self._kline, length=length, mult=mult, useClose=useClose, **kwargs)

    def SuperTrend_STRATEGY(self, Periods=10, Multiplier=3., changeATR=True, **kwargs) -> IndFrame:
        """
        ## 超级趋势策略

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.19_supertrend_strategy/
        - ✈ https://cn.tradingview.com/script/P5Gu6F8k/

        Args:
            Periods: ATR 计算周期，默认 `10`
            Multiplier: ATR 乘数，默认 `3.0`
            changeATR: 布尔值，控制是否改变 ATR 计算方式，默认 `True`
            **kwargs: 其他扩展参数

        Returns:
            包含 `up`（上轨趋势线）、`dn`（下轨趋势线）、`long_signal`（多头信号）、`short_signal`（空头信号）的策略数据
        """
        return SuperTrend_STRATEGY(self._kline, Periods=Periods, Multiplier=Multiplier, changeATR=changeATR, **kwargs)

    def Optimized_Trend_Tracker(self, length=2, var_length=9, percent=1.4, base=200, **kwargs) -> IndFrame:
        """
        ## 优化的趋势跟踪指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.20_optimized_trend_tracker/
        - ✈ https://cn.tradingview.com/script/zVhoDQME/

        Args:
            length: 基础周期，默认 `2`
            var_length: 变异周期，默认 `9`
            percent: 百分比参数，默认 `1.4`
            base: 基础参考值，默认 `200`
            **kwargs: 其他扩展参数

        Returns:
            包含 `MT`（主趋势线）、`OTT`（优化趋势跟踪线）的指标数据
        """
        return Optimized_Trend_Tracker(self._kline, length=length, var_length=var_length, percent=percent, base=base, **kwargs)

    def TonyUX_EMA_Scalper(self, length=20, period=8, **kwargs) -> IndFrame:
        """
        ## TonyUX EMA 剥头皮策略

        ### 📘 **文档参考**:
        ✈ https://cn.tradingview.com/script/egfSfN1y-TonyUX-EMA-Scalper-Buy-Sell/

        Args:
            length: EMA 计算周期，默认 `20`
            period: 价格高低点统计周期，默认 `8`
            **kwargs: 其他扩展参数

        Returns:
            包含 `lasth`（近期高点）、`lastl`（近期低点）、`long_signal`（多头信号）、`short_signal`（空头信号）的策略数据
        """
        return TonyUX_EMA_Scalper(self._kline, length=length, period=period, **kwargs)

    def Turtle_Trade_Channels_Indicator_TUTCI(self, length=20, len2=10, **kwargs) -> IndFrame:
        """
        ## 海龟交易通道指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.21_turtle_trade_channels_indicator/
        - ✈ https://cn.tradingview.com/script/pB5nv16J/

        Args:
            length: 主通道周期，默认 `20`
            len2: 辅助通道周期，默认 `10`
            **kwargs: 其他扩展参数

        Returns:
            包含 `sup`（上通道）、`sdown`（下通道）、`K`（关键趋势线）、`long_signal`（多头信号）、`short_signal`（空头信号）的指标数据
        """
        return Turtle_Trade_Channels_Indicator_TUTCI(self._kline, length=length, len2=len2, **kwargs)

    def AlphaTrend(self, coeff=1.0, AP=14, novolumedata=False, **kwargs) -> IndFrame:
        """
        ## AlphaTrend 指标

        ### 📘 **文档参考**:
        - ✈ https://www.minibt.cn/minibt_tradingview_indicators/3.22_alpha_trend_indicator/
        - ✈ https://cn.tradingview.com/script/egfSfN1y-AlphaTrend-Indicator/

        Args:
            coeff: 系数参数，默认 `1.0`
            AP: 平均周期参数，默认 `14`
            novolumedata: 布尔值，控制是否用成交量模式，默认 `False`
            **kwargs: 其他扩展参数

        Returns:
            包含 `alpha_trend`（AlphaTrend 指标）、`alpha_shift`（AlphaTrend 指标 2 周期前移）、`long_signal`（多头信号）、`short_signal`（空头信号）的指标数据
        """
        return AlphaTrend(self._kline, coeff=coeff, AP=AP, novolumedata=novolumedata, **kwargs)


    def ATRRope(self, length=14, multi=1.5, src='close', **kwargs) -> IndFrame:
        """
        ## ATR Rope — 基于 ATR 的自适应绳索通道
        https://cn.tradingview.com/script/YYrxRhi9-ATR-Rope/

        参数:
        - length:  ATR 周期，默认 14
        - multi:   ATR 乘数，默认 1.5
        - src:     价格源（'close' / 'hlc3' / 'ohlc4'），默认 'close'
        **kwargs: 其他扩展参数
        Returns:
            包含 `rope`（绳索）、`upper`（上轨）、`lower`（下轨）、`range_hi`（高点）、`range_lo`（低点）的指标数据
        """
        return ATRRope(self._kline, length=length, multi=multi, src=src, **kwargs)


    def DeltaRSISignals(self, 
                        rsi_length=21, window=21, degree=2,
                        buycond='Zero-Crossing',
                        sellcond='Zero-Crossing',
                        endcond='Zero-Crossing',
                        use_nrmse=False, nrmse_thrs=10.0, **kwargs) -> IndFrame:
        """
        ## Delta-RSI Oscillator Strategy [tbiktag]
        基于 Pine Script "Delta-RSI Oscillator Strategy" (tbiktag) 转换
        参考 TradingView: https://cn.tradingview.com/script/OXQVFTQD-Delta-RSI-Oscillator/

        三种可选交易信号条件：
        - Zero-Crossing:        D-RSI 上穿/下穿 0 线
        - Signal Line Crossing: D-RSI 上穿/下穿信号线
        - Direction Change:     D-RSI 在负值/正值区域转向

        NRMSE 过滤：启用时仅当多项式拟合误差小于阈值才产生信号。

        输出线：
        drsi          — Delta-RSI 值
        signal        — Signal 线
        nrmse          — NRMSE 值
        long_signal  — 做多信号（1=有信号，0=无）
        short_signal  — 做空信号（1=有信号，0=无）
        endlong_signal  — 多头离场信号
        endshort_signal — 空头离场信号

        参数：
        rsi_length    — RSI 计算周期，默认 21
        window        — 多项式拟合窗口，默认 21
        degree        — 拟合多项式阶数，默认 2
        signal_length — Signal 线 EMA 周期，默认 9
        buycond       — 做多条件: 'Zero-Crossing'/'Signal Line Crossing'/'Direction Change'，默认 'Zero-Crossing'
        sellcond      — 做空条件，默认 'Zero-Crossing'
        endcond       — 离场条件，默认 'Zero-Crossing'
        use_nrmse     — 是否启用 NRMSE 过滤，默认 False
        nrmse_thrs    — NRMSE 阈值（%），默认 10
        """
        return DeltaRSISignals(self._kline, rsi_length=rsi_length, window=window, degree=degree,
                                buycond=buycond, sellcond=sellcond, endcond=endcond,
                                use_nrmse=use_nrmse, nrmse_thrs=nrmse_thrs, **kwargs)
        
    def DynamicSwingAnchoredVWAP(self, prd=50, base_apt=20, use_adapt=False, vol_bias=10.0, **kwargs) -> IndFrame:
        """
        ## Dynamic Swing Anchored VWAP (DSAV) Indicator
        基于 Pine Script "Dynamic Swing Anchored VWAP (Zeiierman)" 转换
        https://cn.tradingview.com/script/SxgyrEde-Dynamic-Swing-Anchored-VWAP-Zeiierman/

        参数:
        - prd:         Swing 检测周期，默认 50
        - base_apt:    基础自适应价格跟踪周期，默认 20
        - use_adapt:   是否启用 ATR 波动率自适应 APT，默认 False
        - vol_bias:    波动率偏差乘数，默认 10.0
        **kwargs: 其他扩展参数
        Returns:
            包含 `dsav`（Dynamic Swing Anchored VWAP 值）的指标数据
        """
        return DynamicSwingAnchoredVWAP(self._kline, prd=prd, base_apt=base_apt, use_adapt=use_adapt, vol_bias=vol_bias, **kwargs)
 
    def DynamicLinearRegressionChannels(self, upper_mult=1.0, lower_mult=1.0, break_mult=2.0, **kwargs) -> IndFrame:
        """
        动态线性回归通道 — 自适应分段回归趋势通道
        基于 Pine Script "Dynamic Linear Regression Channels" 转换
        https://cn.tradingview.com/script/IPdDUsgl-Dynamic-Linear-Regression-Channels/

        原理：
        对收盘价做线性回归拟合出基线，以上下轨包住段内行情的高低点。

        通道构建方式（参考趋势线逻辑）：
            - baseline = 对收盘价的线性回归线（段内最佳拟合直线）
            - upper = baseline + upper_mult × upDev
            upDev = 段内最高价相对回归线的最大正偏离 → 上轨贴近高点
            - lower = baseline - lower_mult × dnDev
            dnDev = 段内回归线相对最低价的最大正偏离 → 下轨贴近低点
            - upper_mult / lower_mult = 1.0 时通道刚好包住该段全部价格区间

        核心亮点在于"动态分段"机制：
            - 从起始 bar 开始，随新 bar 不断更新回归通道
            - 一旦收盘价突破通道上轨或下轨，认为趋势结构变化，该段结束
            - 以突破 bar 为起点重新开始新的回归段
            - 每个段都有独立的斜率和通道宽度
            - 同段内三条线共享同一斜率，保证相互平行（直线段）

        计算流程：
            1. 从 start_index 到当前 bar 做线性回归 → slope, intercept
            2. 计算 upDev（最高价 - 回归线 的最大值）
            3. 计算 dnDev（回归线 - 最低价 的最大值）
            4. 上轨 = 回归线 + upper_mult × upDev
            5. 下轨 = 回归线 - lower_mult × dnDev
            6. 若收盘价突破上/下轨，用当前段回归参数填充整段，重置 start_index

        输出线：
        base_line — 线性回归基线
        upper     — 上轨（贴近段内高点）
        lower     — 下轨（贴近段内低点）

        参数：
        upper_mult — 上轨绘制宽度乘数，默认 1.0（=1 时上轨刚好包住最高价）
        lower_mult — 下轨绘制宽度乘数，默认 1.0（=1 时下轨刚好包住最低价）
        break_mult — 突破检测宽度乘数（基于 StdDev），默认 2.0
                    控制分段的敏感度：越小越容易分段，越大分段越少
        """
        return DynamicLinearRegressionChannels(self._kline, upper_mult=upper_mult, lower_mult=lower_mult, break_mult=break_mult, **kwargs)

    def IGHSupertrend(self, 
                        hull_length=55,
                        volatility_length=21,
                        volatility_mode='Blend',
                        measurement_noise=4.0,
                        process_noise=0.03,
                        innovation_threshold=0.80,
                        gate_sharpness=4.0,
                        admission_floor=0.12,
                        process_boost=4.0,
                        atr_period=12,
                        factor=1.70,
                        adapt_bands=True,
                        quiet_band_expansion=0.35, 
                        **kwargs) -> IndFrame:
        """
        Innovation-Gated Hull Supertrend — 创新门控 Hull 超级趋势
        https://cn.tradingview.com/script/tq9f0qwr-Innovation-Gated-Hull-Supertrend-BackQuant/

        参数:
            - hull_length:           Hull MA 周期，默认 55
            - volatility_length:     波动率周期，默认 21
            - volatility_mode:       波动率模型: ATR / StdDev / Blend，默认 Blend
            - measurement_noise:     测量噪声，默认 4.0
            - process_noise:         过程噪声，默认 0.03
            - innovation_threshold:  创新阈值，默认 0.80
            - gate_sharpness:        门控锐度，默认 4.0
            - admission_floor:       准入下限，默认 0.12
            - process_boost:         过程增益，默认 4.0
            - atr_period:            Supertrend ATR 周期，默认 12
            - factor:                Supertrend 因子，默认 1.70
            - adapt_bands:           是否用创新度自适应带，默认 True
            - quiet_band_expansion:  平静期带宽扩展，默认 0.35
        
        Returns:
            - super_trend: 超级趋势值
            - direction: 趋势方向，1 表示上趋势，-1 表示下趋势，0 表示无趋势
            - long_signal: 长信号，1 表示长趋势，0 表示无长趋势
            - short_signal: 短信号，1 表示短趋势，0 表示无短趋势
        """
        return IGHSupertrend(self._kline, 
                              hull_length=hull_length,
                              volatility_length=volatility_length,
                              volatility_mode=volatility_mode,
                              measurement_noise=measurement_noise,
                              process_noise=process_noise,
                              innovation_threshold=innovation_threshold,
                              gate_sharpness=gate_sharpness,
                              admission_floor=admission_floor,
                              process_boost=process_boost,
                              atr_period=atr_period,
                              factor=factor,
                              adapt_bands=adapt_bands,
                              quiet_band_expansion=quiet_band_expansion,
                              **kwargs)
    
    def LPTT(self, length=20, **kwargs) -> IndFrame:
        """
        ## LPTT — 低阶多项式拟合趋势交易指标

        原理：
        对价格时间序列做一阶和二阶多项式拟合，提取"趋势方向与强度"（f'）
        和"趋势加速度"（g''），通过两者的正负组合判定当前处于加速/减速
        上涨/下跌四种趋势形态中的哪一种。只在加速阶段（f' × g'' > 0）入场，
        趋势衰减或反转时离场。

        四种趋势形态：
            - 加速上涨：f' > 0 且 g'' > 0 → 顺势做多
            - 加速下跌：f' < 0 且 g'' < 0 → 顺势做空
            - 减速上涨：f' > 0 且 g'' < 0 → 不交易（反转风险高）
            - 减速下跌：f' < 0 且 g'' > 0 → 不交易（反转风险高）

        计算流程：
            1. 取最近 length 根收盘价，一阶 polyfit → f'（趋势斜率）
            2. 取最近 length 根收盘价，二阶 polyfit → g'' = 2a₂（趋势加速度）
            3. f' × g'' > 0 → 顺势信号（加速趋势）
            4. f' 或 g'' 变号 → 离场信号
        
        参数：
        length — 多项式拟合窗口长度，默认 20

        输出线：
        f_prime          — 一阶导数（趋势方向与强度）
        g_double_prime   — 二阶导数（趋势加速度）
        long_signal      — 加速上涨信号（f' > 0 且 g'' > 0）
        short_signal     — 加速下跌信号（f' < 0 且 g'' < 0）

        参数：
        length  — 多项式拟合窗口长度，默认 20
        """
        return LPTT(self._kline, length=length, **kwargs)
    
    def MLAdaptiveSuperTrend(self,
                            atr_len=10,
                            fact=3.0,
                            training_data_period=100,
                            highvol=0.75,
                            midvol=0.50,
                            lowvol=0.25,
                            **kwargs) -> IndFrame:
        """
        机器学习自适应 SuperTrend — 基于 K-Means 聚类动态调整 ATR
        https://cn.tradingview.com/script/CLk71Qgy-Machine-Learning-Adaptive-SuperTrend-AlgoAlpha/

        原理：
        标准 SuperTrend 使用固定周期的 ATR 作为波动率度量，在高/低波动时期
        可能过于迟钝或过于敏感。本指标通过 K-Means 聚类将历史 ATR 分为
        高/中/低三个波动率状态，用当前 bar 所属簇的质心值替换固定 ATR，
        实现 SuperTrend 的自适应调整。

        计算流程：
            1. 用 Wilder's RMA 计算 atr_len 周期的 ATR
            2. 对每根 bar，取过去 training_data_period 根 ATR 做 3-Means 聚类
            3. 将当前 bar 的 ATR 归入最近的簇，取该簇质心作为自适应 ATR
            4. 用自适应 ATR 计算 Pine Script 风格 SuperTrend

        输出线：
        ST      — 自适应 SuperTrend 线，叠加主图，牛市在价格下方、熊市在上方
        dir     — 方向信号：+1 = 多头趋势（ST 在价格下方），-1 = 空头趋势（ST 在价格上方）
        cluster — 波动率聚类标签：0 = 高波动，1 = 中波动，2 = 低波动

        参数：
        atr_len              — ATR 计算周期，默认 10
        fact                 — SuperTrend 乘数因子，默认 3.0
        training_data_period — K-Means 训练窗口长度（bar 数），默认 100
        highvol              — 高波动初始质心百分位，默认 0.75（窗口 ATR 第 75 百分位）
        midvol               — 中波动初始质心百分位，默认 0.50
        lowvol               — 低波动初始质心百分位，默认 0.25
        """
        return MLAdaptiveSuperTrend(self._kline, atr_len=atr_len, fact=fact, training_data_period=training_data_period, highvol=highvol, midvol=midvol, lowvol=lowvol, **kwargs)
    
    
    def NRTR(self, percentage=1.0, mult=0., length=20, **kwargs) -> IndFrame:
        """
        ## Nick Rypock 动态反转止损线 (NRTR)
        https://cn.tradingview.com/script/XAscppNW-Nick-Rypock-Trailing-Reverse-NRTR/

        原理：
        NRTR 是一种基于百分比的自适应跟踪止损指标。它根据当前趋势方向，
        在最高价（上升趋势）或最低价（下降趋势）的基础上加减一个百分比
        系数来生成止损线。

        状态机逻辑：
            - 上升趋势中：不断追踪更高最高价，止损线 = 最高价 × (1 - k)
            - 价格跌破止损线 → 趋势反转，进入下降趋势
            - 下降趋势中：不断追踪更低最低价，止损线 = 最低价 × (1 + k)
            - 价格突破止损线 → 趋势反转，进入上升趋势

        该指标同时输出买卖信号：趋势从 -1 翻转到 1 为买入信号，
        从 1 翻转到 -1 为卖出信号。

        计算流程：
            1. trend=1 时：hp = max(hp, close)，nrtr = hp × (1 - k)
            2. 若 close ≤ nrtr：trend = -1，lp = close，nrtr = lp × (1 + k)
            3. trend=-1 时：lp = min(lp, close)，nrtr = lp × (1 + k)
            4. 若 close ≥ nrtr：trend = 1，hp = close，nrtr = hp × (1 - k)

        输出线：
        long_stop  — 多头止损线（上升趋势时有效，下降趋势时为 NaN）
        short_stop — 空头止损线（下降趋势时有效，上升趋势时为 NaN）
        buy_signal — 买入信号（趋势从 -1 翻转到 1，值为 1）
        sell_signal — 卖出信号（趋势从 1 翻转到 -1，值为 -1）

        参数：
        percentage — 修正系数百分比，默认 1（即 1%）
        mult — 修正系数，默认 0（不修正）
        length — 计算窗口，默认 20（20日移动平均）
        NOTE:
        - 当 mult 为非零正数时，止损线会根据移动平均线进行修正。
        """
        return NRTR(self._kline, percentage=percentage, mult=mult, length=length, **kwargs)
    
    
    def NWEEnvelope(self, h=8.0, mult=3.0, lookback=500, src_type='close', **kwargs) -> IndFrame:
        """
        ## Nadaraya-Watson Envelope — 高斯核回归包络带
        https://cn.tradingview.com/script/Iko0E2kL-Nadaraya-Watson-Envelope-LuxAlgo/

        参数:
        - h:          带宽，控制平滑度（越大越平滑），默认 8.0
        - mult:       包络带乘数，默认 3.0
        - lookback:   回看窗口，默认 500
        - src_type:   价格源，可选 close/hl2/hlc3/ohlc4，默认 close
        
        输出线：
        nw_line    — 高斯核回归包络线
        upper      — 上轨
        lower      — 下轨
        long_signal — 多头信号（趋势从 -1 翻转到 1，值为 1）
        short_signal — 空头信号（趋势从 1 翻转到 -1，值为 -1）
        """
        return NWEEnvelope(self._kline, h=h, mult=mult, lookback=lookback, src_type=src_type, **kwargs)
    
    def OptimizedTrendTracker(self, 
            length=9,
            var_length=9,
            percent=0.14,
            mult=0.,
            ma_type='VAR', 
            **kwargs) -> IndFrame:
        """
        ## OTT — 优化趋势跟踪器

        原理：
        OTT 在所选均线上下偏移 percent% 构建动态停损带，利用自适应逻辑使
        longStop 只升不降、shortStop 只降不升，从而减少噪音造成的频繁翻转。
        当价格恢复趋势方向时，OTT 线快速跟上，形成平滑的趋势跟踪曲线。

        支持 8 种均线：
            SMA — 简单移动平均
            EMA — 指数移动平均
            WMA — 加权移动平均
            TMA — 三角移动平均
            VAR — 基于 CMO 的自适应可变均线
            WWMA — Welles Wilder 均线（RMA）
            ZLEMA — 零延迟 EMA
            TSF — 时间序列预测

        计算流程：
            1. 计算所选 MA(close, length)，VAR 类型使用 var_length 作为 CMO 周期
            2. longStop = MA - offset, shortStop = MA + offset（offset = MA × percent%）
            3. 条件调节 longStop/shortStop → 趋势方向 dir
            4. MT = dir==1 ? longStop : shortStop
            5. OTT = MA > MT ? MT×(base+percent)/base : MT×(base-percent)/base
            6. ott_s2 = OTT[2] 滞后 2 bar（Pine 主绘图线）

        输出线：
        ott_s2      — OTT[2]（Pine 主绘图线，滞后 2 bar，与 tradingview OTT 对比用）
        ott         — OTT 原始值（无偏移）
        ma_avg      — 支撑线（所选 MA 的原始值）
        buy_signal  — 支撑线穿越 OTT[2] 买入信号
        sell_signal — 支撑线穿越 OTT[2] 卖出信号

        参数：
        length       — MA 周期，默认 9
        var_length   — VAR 模式的 CMO 周期，默认 9（与 tradingview 版一致）
        percent      — OTT 百分比偏移，默认 0.14
        mult         — OTT 公式基准值，默认 0.
        ma_type      — MA 类型: SMA/EMA/WMA/TMA/VAR/WWMA/ZLEMA/TSF，默认 'VAR'
        showsupport  — 是否显示支撑线，默认 True
        showsignals_k — 是否显示穿越信号，默认 True
        NOTE:
            1. mult 为 0 时，percent 为 OTT 百分比偏移
            2. mult 不为 0 时，percent 为 mult * 价格的标准差倍数 / 均值
        """
        return OptimizedTrendTracker(self._kline, length=length, var_length=var_length, percent=percent, mult=mult, ma_type=ma_type, **kwargs)
    
    
    def RangeFilter(self, 
            filter_type='Type1',
            mov_src='Wicks',
            rng_qty=2.618,
            rng_scale='Average Change',
            rng_per=14,
            smooth_range=True,
            smooth_per=27,
            av_vals=False,
            av_samples=2, 
            **kwargs) -> IndFrame:
        """
        ## 范围过滤器 [DW] — 波动率自适应价格过滤器

        参考 TradingView: https://cn.tradingview.com/script/lut7sBgG-Range-Filter-DW/

        原理：
        受 QQE 波动率过滤器启发，将过滤逻辑直接作用于价格而非平滑后的 RSI。
        1. 先计算平均价格波动范围（支持 ATR / Average Change / Std Dev 等多种尺度）
        2. 价格移动超过计算出的阈值时，filter 线才会跟随移动
            - Type 1：简单门控 — 价格穿出通道时 filter 跳至通道边界
            - Type 2：步进移动 — 价格穿出通道时 filter 以 rng 为步长逐步跟随
        3. 方向判断：filter 上升 → +1（多头），下降 → -1（空头），不变 → 维持
        4. 可选：仅在 filter 变化时采样做条件 EMA 平均，输出更平滑的 filter

        输出线：
        filt   — 范围过滤器主线，叠加主图，反映过滤后的趋势价格
        h_band — 上轨 = filt + rng，价格突破此线触发 filter 上移
        l_band — 下轨 = filt - rng，价格跌破此线触发 filter 下移
        fdir   — 方向信号：+1 = 多头趋势，-1 = 空头趋势

        参数：
        filter_type — 过滤类型: 'Type1' 简单门控 / 'Type2' 步进移动
        mov_src     — 移动来源: 'Wicks' 使用最高/最低价 / 'Close' 使用收盘价
        rng_qty     — 范围倍数，默认 2.618
        rng_scale   — 范围计算尺度: 'ATR' / 'Average Change' / 'Standard Deviation'
                        / '% of Price' / 'Pips' / 'Points' / 'Ticks' / 'Absolute'
        rng_per     — 范围计算周期（ATR / Average Change / Std Dev 时生效）
        smooth_range — 是否平滑范围，开启后对 rng 做 EMA 平滑
        smooth_per  — 范围平滑周期
        av_vals     — 是否平均 filter 变化，开启后仅在 filter 值变化时采样做 EMA
        av_samples  — filter 变化平均的采样数
        """
        return RangeFilter(self._kline, filter_type=filter_type, mov_src=mov_src, rng_qty=rng_qty, rng_scale=rng_scale, rng_per=rng_per, smooth_range=smooth_range, smooth_per=smooth_per, av_vals=av_vals, av_samples=av_samples, **kwargs)
    
    
    def RedKMomentumBars(self, 
            fast_len=10,
            fast_type='SMA',
            slow_len=20,
            slow_type='SMA',
            slow_delay=3,
            fil_len=50,
            fil_type='SMA',
            **kwargs) -> IndFrame:
        """
        ## RedK 动量 K 线 — 基于双均线动量的蜡烛图指标
        https://cn.tradingview.com/script/O52gURXf-RedK-Momentum-Bars-RedK-Mo-Bars/

        原理：
        受 Elder Impulse 启发，通过对比短周期均线（Fast/Slow）与长周期 Filter 均线的差值，
        生成动量蜡烛图。蜡烛的「开盘价」是延迟后的慢线动量，「收盘价」是快线动量，
        蜡烛实体反映短周期动量的方向和强度，0 线则是多空分界线。

        计算流程：
            1. 计算 Fast MA 和 Slow MA，均支持多种类型 (SMA/EMA/WMA/HMA/RSS_WMA)
            2. 计算 Filter MA（长周期趋势基准线）
            3. Fast_M = Fast_MA - Filter_MA（快线动量）
            4. Slow_M = Slow_MA - Filter_MA（慢线动量）
            5. Rel_M = WMA(Slow_M, delay)（延迟慢线动量，用于蜡烛开盘价）
            6. 动量蜡烛: o=Rel_M, c=Fast_M, h=max(o,c), l=min(o,c)

        输出线：
        open  — 延迟慢线动量（蜡烛开盘价）
        high  — max(open, close)
        low   — min(open, close)
        close — 快线动量（蜡烛收盘价）
        (四线组合构成蜡烛图，0 线以上为多头动量，以下为空头动量)

        参数：
        fast_len   — 快线周期，默认 10
        fast_type  — 快线类型: 'SMA'/'EMA'/'WMA'/'HMA'/'RSS_WMA'，默认 'SMA'
        slow_len   — 慢线周期，默认 20
        slow_type  — 慢线类型，默认 'SMA'
        slow_delay — 慢线延迟周期（1=无延迟），默认 3
        fil_len    — Filter 周期（趋势基准线），默认 50
        fil_type   — Filter 类型，默认 'SMA'
        """
        return RedKMomentumBars(self._kline, fast_len=fast_len, fast_type=fast_type, slow_len=slow_len, slow_type=slow_type, slow_delay=slow_delay, fil_len=fil_len, fil_type=fil_type, **kwargs)
    
    def ResamplingFilterPack(self, 
                            s_method='BPS',
                            s_size=9,
                            s_off=0,
                            rng_qty=0.0,
                            rng_scale='Average Change',
                            rng_per=14,
                            smooth_range=True,
                            smooth_per=27,
                            ma_type='EMA',
                            ma_per=9,
                            **kwargs) -> IndFrame:
        """
        ## 重采样滤波器包 — 自定义采样率下的条件平滑滤波
        https://cn.tradingview.com/script/hIXxuhEV-Resampling-Filter-Pack-DW/

        原理：
        先在降低的采样率下（BPS / Interval / PA）对价格重采样，丢弃高频噪声；
        再利用条件滤波器（仅在采样触发时更新）对低频采样点进行平滑，
        产生类似阶梯状的滤波曲线，用于趋势分析。

        计算流程：
            1. 根据采样方法判断当前 bar 是否为采样触发点
            2. 采样触发时将当前 close 加入样本缓冲（舍弃触发间隔内的噪音）
            3. 在样本缓冲上运行选定的滤波器（SMA/EMA/HMA 等）
            4. 非触发 bar 的滤波器输出保持不变

        输出线：
        ds_src  — 重采样后的源值（触发时 = close，否则保持上一次采样值）
        filter  — 条件滤波器输出（主要的趋势跟踪线）
        filter_dir — 滤波方向信号（1=向上, -1=向下, 0=持平）

        参数：
        s_method    — 重采样方法: 'BPS'/'Interval'/'PA'，默认 'BPS'
        s_size      — BPS/Interval 方法每多少 bar 采样一次，默认 5
        s_off       — 采样偏移（延迟），默认 0
        rng_qty     — PA 方法的范围阈值，默认 0
        rng_scale   — PA 范围尺度: 'Points'/'Pips'/'Ticks'/'% of Price'/'ATR'/'Average Change'/'Absolute'
        rng_per     — PA 动态范围计算周期，默认 14
        smooth_range — PA 范围平滑开关，默认 True
        smooth_per  — PA 范围平滑周期，默认 27
        ma_type     — 滤波器类型: SMA/EMA/ZLEMA/DEMA/RMA/WMA/VWMA/ALMA/HMA/EWHMA/BLP/GLP/SSF，默认 'EMA'
        ma_per      — 滤波器周期，默认 9
        """
        return ResamplingFilterPack(self._kline, s_method=s_method, s_size=s_size, s_off=s_off, rng_qty=rng_qty, rng_scale=rng_scale, rng_per=rng_per, smooth_range=smooth_range, smooth_per=smooth_per, ma_type=ma_type, ma_per=ma_per, **kwargs)
    
    def SRChannels(self, 
                    prd=10,
                    ppsrc='High/Low',
                    channel_w=5,
                    min_strength=1,
                    max_numsr=6,
                    loopback=290,
                    **kwargs) -> IndFrame:
        """
        ## SR Channels — 支撑阻力通道 [LonesomeTheBlue]

        参考 TradingView: https://cn.tradingview.com/script/qX1Zv6O7-Support-Resistance-Channels-LonesomeTheBlue/

        原理：
        基于枢轴高点和低点构建支撑阻力通道，贪心纳入通道宽度范围内的其他枢轴，
        计算通道强度（枢轴数 × 20 + K线触及数），保留最强的 top N 通道。
        价格不在通道内时，上穿阻力上轨触发做多，下穿支撑下轨触发做空。

        计算流程：
            1. 检测枢轴高/低点（pivot high/low），左右各 prd 根 bar 的极值
            2. 维护枢轴列表，超出 loopback 的旧枢轴被移除
            3. 每新增枢轴时重算通道：
            - 以每个枢轴为起点，贪心纳入 cwidth 范围内的其他枢轴
            - 通道强度 = 纳入枢轴数 × 20 + loopback 内 K 线触及通道数
            - 选择强度最高的 top N 通道（排除重叠通道）
            4. 生成信号：
            - 价格不在任何通道内时，上穿阻力上轨 → long_signal
            - 价格不在任何通道内时，下穿支撑下轨 → short_signal

        输出线：
            sr_high      — 通道上轨包络线（取所有通道 hi 的最大值），主图叠加
            sr_low       — 通道下轨包络线（取所有通道 lo 的最小值），主图叠加
            long_signal  — 做多信号（价格上穿阻力通道上轨，非通道内），不绘图
            short_signal — 做空信号（价格下穿支撑通道下轨，非通道内），不绘图

        参数：
            prd           — 枢轴检测窗口（左右各 N 根），默认 10
            ppsrc         — 枢轴源: 'High/Low' 或 'Close/Open'，默认 'High/Low'
            channel_w     — 最大通道宽度 %（基于最近 300 根 K 线振幅），默认 5
            min_strength  — 最低通道强度（至少含 2 个枢轴 = 40），默认 1
            max_numsr     — 最多保留通道数，默认 6
            loopback      — 枢轴回看周期，默认 290
        """
        return SRChannels(self._kline, prd=prd, ppsrc=ppsrc, channel_w=channel_w, min_strength=min_strength, max_numsr=max_numsr, loopback=loopback, **kwargs)
    
    def STDNpoleGaussianFilter(self,
                                period=25,
                                order=5,
                                src='Close',
                                filter_option='Both',
                                filter_mult=1.6185,
                                filter_period=10,
                                **kwargs
                            ) -> IndFrame:
        """
        ## STD 过滤的 N-Pole 高斯滤波器 — 高阶平滑趋势线
        https://cn.tradingview.com/script/i4xZNAoy-STD-Filtered-N-Pole-Gaussian-Filter-Loxx/

        原理：
        结合可选的 STD（标准差）噪声过滤和 N-Pole Gaussian Filter（高阶 IIR 高斯平滑），
        生成一条平滑的趋势跟随线。STD 过滤可剔除微小价格波动，Gaussian Filter 则提供
        灵活可调的平滑度（通过 period 和 order 控制）。

        计算流程：
            1. 根据 src 参数选择价格源（Close / HL2 / HLC3 等）
            2. 若 filter_option 含 "Price"，对源数据做 STD 过滤去噪
            3. 对（过滤后的）数据应用 N-Pole Gaussian Filter 平滑
            4. 若 filter_option 含 "Gaussian Filter"，对输出再做一次 STD 过滤
            5. 方向判定：out 上升 → +1，下降 → -1
            6. 信号生成：连续方向切换时产生交易信号

        输出线：
        out          — N-Pole Gaussian Filter 主线，叠加主图
        dir          — 方向信号：+1 = 多头趋势（out 上升），-1 = 空头趋势（out 下降）
        long_signal  — 做多信号：空头转多头时触发（默认隐藏）
        short_signal — 做空信号：多头转空头时触发（默认隐藏）

        参数：
        period        — 高斯滤波周期，决定截止频率，默认 25
        order         — 高斯滤波阶数（极点数量），越大越平滑但滞后越大，默认 5
        src           — 价格源: 'Close'/'Open'/'High'/'Low'/'HL2'/'HLC3'/'OHLC4'，默认 'Close'
        filter_option — STD 过滤选项: 'None' 不过滤 / 'Price' 源端过滤 /
                        'Gaussian Filter' 输出端过滤 / 'Both' 两端过滤，默认 'Both'
        filter_mult   — STD 过滤倍数，默认 1.6185
        filter_period — STD 过滤计算周期，默认 10
        """
        return STDNpoleGaussianFilter(self._kline, period=period, order=order, src=src, filter_option=filter_option, filter_mult=filter_mult, filter_period=filter_period, **kwargs)
    
    def SyntheticOscillator(self,
                                LB=15,
                                UB=25,
                                **kwargs
                            ) -> IndFrame:
        """
        TASC Synthetic Oscillator Strategy
        基于 Pine Script "TASC 2026.04 A Synthetic Oscillator [John F. Ehlers]" 转换
        https://cn.tradingview.com/script/we9AMcvE-TASC-2026-04-A-Synthetic-Oscillator/
        
        参数：
        LB   主周期下界，默认 15
        UB   主周期上界，默认 25
        
        核心逻辑：
        1. Hann 滤波器（12 周期）初始平滑价格
        2. 高通 + SuperSmoother 构成带通滤波 → 实部分量 re
        3. ROC + 归一化 → 虚部分量 im
        4. arctan 变化率 → 估计主周期 dc
        5. 中频带通 + 零交叉检测 → 相位累加 / 重置
        6. sin(累积相位) → Synthetic Oscillator (so)
        7. so 上穿零线 → 做多；下穿零线 → 做空
        """
        return SyntheticOscillator(self._kline, LB=LB, UB=UB, **kwargs)
    
    def TradingActivityIndex(self,
                                len_form=20,
                                len_hist=252,
                                **kwargs
                            ) -> IndFrame:
        """
        交易活跃度指数 (Trading Activity Index)
        Trading Activity Index (TAI) Indicator
        基于 Pine Script "Trading Activity Index (Zeiierman)" 转换
        https://cn.tradingview.com/script/rtOfqf13-Trading-Activity-Index-Zeiierman/
        
        参数:
        - len_form:  价格成交量均线窗口，默认 20
        - len_hist:  滚动百分位历史窗口，默认 252（年化）
        
        核心逻辑：
        1. 价格成交量 = close × volume
        2. SMA(价格成交量, len_form) → log → VOLSCALE 代理值
        3. VOLSCALE 滚动百分位 (P0/P20/P40/P60/P80/P100, 默认 252 根K线)
        """
        return TradingActivityIndex(self._kline, len_form=len_form, len_hist=len_hist, **kwargs)
    
    def TrendStateSignals(self,
                        length=20,
                        multiplier=1.6185,
                        src_type='close',
                        **kwargs
                            ) -> IndFrame:
        """
        Trend State Signals — 自适应步进趋势过滤器
        基于 Pine Script "Trend State Signals [Market Structure Lab]" 转换
        参考 TradingView: https://cn.tradingview.com/script/I19TTam0-Trend-State-Signals/
        
        参数:
        - length:           灵敏度周期，默认 20
        - multiplier:       范围乘数（越大信号越少），默认 3.5
        - src_type:         价格源，可选 close/hl2/hlc3/ohlc4，默认 close
        
        核心逻辑：
        1. 计算自适应范围：priceMovement = |change(source)|, smoothed = RMA(priceMovement, length)
        adaptiveRange = smoothed * multiplier
        2. Step Filter（步进过滤器）：
        - 价格向上突破 upperTrigger(prev + range) 时，filter = source - range
        - 价格向下突破 lowerTrigger(prev - range) 时，filter = source + range
        - 否则保持不变
        3. 趋势判断：filter 上升 → 多头(1)，下降 → 空头(-1)，不变 → 维持
        4. 信号：趋势方向刚发生变化时触发
        """
        return TrendStateSignals(self._kline, length=length, multiplier=multiplier, src_type=src_type, **kwargs)
    
    def TrendlinesWithBreaks(self,
                            length=14,
                            mult=1.0,
                            calc_method='Atr',
                            unique_signal=False,
                            **kwargs
                            ) -> IndFrame:
        """
        动态趋势线 — 基于 Swing Pivot 的自适应支撑阻力趋势线
        基于 Pine Script "Trendlines with Breaks [LuxAlgo]" 转换
        参考 TradingView: https://cn.tradingview.com/script/IYL88A1N-Trendlines-with-Breaks-LuxAlgo/

        原理：
        在每个 swing pivot high/low 处，根据当时市场波动（ATR/Stdev/Linreg）
        计算斜率，从 pivot 点出发绘制趋势线：
            - upper: pivot high → 向下倾斜（下降趋势线 / 阻力线）
            - lower: pivot low → 向上倾斜（上升趋势线 / 支撑线）
        新 pivot 出现时自动重置，形成动态自适应趋势通道。

        注意：Pine Script 原版使用 offset 将趋势线左偏 length 根 bar，
        以使视觉上对齐 pivot 实际位置。这里输出的是原始确认数据（含延迟确认）。

        计算流程：
            1. pivothigh/pivotlow(left=right=length) 检测 swing 极值
            2. 按 calc_method 计算每根 bar 的斜率
            3. pivot 处捕获新斜率、重置趋势线起点
            4. 趋势线逐 bar 延伸（upper 递减，lower 递增）

        输出线：
        upper — 下降趋势线（阻力），趋势向下
        lower — 上升趋势线（支撑），趋势向上

        参数：
        length        — Swing 检测回溯周期，同时也是 pivot 左右确认 bar 数和斜率计算周期
        mult          — 斜率乘数，默认 1.0，越大趋势线越陡
        calc_method   — 斜率计算方式: 'Atr' / 'Stdev' / 'Linreg'，默认 'Atr'
        unique_signal — True 时每条趋势线仅显示一次突破信号（首次突破），
                        False 时所有突破 bar 都生成信号，默认 False
        """
        return TrendlinesWithBreaks(self._kline, length=length, mult=mult, calc_method=calc_method, unique_signal=unique_signal, **kwargs)
    
    def VWAPPriceChannel(self,
                            len=20,
                            **kwargs
                            ) -> IndFrame:
        """
        VWAP 价格通道 (VWAP Price Channel)
        基于 Pine Script "VWAP Price Channel [SamRecio]" 转换
        https://cn.tradingview.com/script/Psnjpa2Y-VWAP-Price-Channel/
        
        参数:
        - len:  最高/最低检测窗口，默认 20
        
        核心逻辑：
        1. 检测 _len 周期内的最高价/最低价是否刷新
        2. 在最高/最低刷新处锚定 VWAP(high) / VWAP(low)
        3. 上轨 = VWAP 通道中轴上轨，动态跟随 hst 或 VWAP 趋势
        4. 下轨 = VWAP 通道中轴下轨，动态跟随 lst 或 VWAP 趋势
        5. 中轴 = (upper + lower) / 2
        """
        return VWAPPriceChannel(self._kline, len=len, **kwargs)
    
    