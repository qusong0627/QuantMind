import time
from .utils import Literal, np, pd, partial, reduce, get_lennan, MaType, FILED
from .indicators import BtIndicator, IndSeries, IndFrame
import math
from .ta import BtFunc, sm


class HMA_Crossover_1H_with_RSI_Stochastic_RSI_and_Trailing_Stop(BtIndicator):
    """https://cn.tradingview.com/script/rXKcOXbw-HMA-Crossover-1H-with-RSI-Stochastic-RSI-and-Trailing-Stop/"""
    params = dict(hma_len1=5, hma_len2=20, rsi_len=14, ma_len=3)
    overlap = dict(rsi=False, k=False, long_signal=False, short_signal=False)

    def next(self):
        # print(type(self), type(self.close))
        hma1 = self.close.hma(self.params.hma_len1)
        hma2 = self.close.hma(self.params.hma_len2)
        rsi = self.close.rsi(self.params.rsi_len)
        k = self.stoch(k=self.params.rsi_len, d=self.params.ma_len).stoch_k.sma(
            self.params.ma_len)
        long_signal = hma1.cross_up(hma2)
        long_signal &= rsi < 45.
        long_signal &= k < 39.
        short_signal = hma1.cross_down(hma2)
        short_signal &= rsi > 60.
        short_signal &= k > 63.
        return hma1, hma2, rsi, k, long_signal, short_signal


class Price_and_Volume_Breakout_Buy_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/jc2hs2qK-Price-and-Volume-Breakout-Buy-Strategy-TradeDots/"""
    params = dict(price_len=144, volume_len=144, slow=600)
    overlap = dict(vol_highest=False)
    plotinfo = dict(signal=dict(overlap=dict(vol_highest=True)))

    def next(self):
        highest = self.high.rolling(self.params.price_len).max()
        lowest = self.low.rolling(self.params.price_len).min()
        vol_highest = self.volume.rolling(self.params.volume_len).max()
        sma = self.close.sma(self.params.slow)
        long_signal = self.close > highest.shift()
        long_signal &= self.volume > vol_highest.shift()
        long_signal &= self.close > sma

        exitlong_signal = self.close < sma
        exitlong_signal &= self.close.shift() < sma
        exitlong_signal &= self.close.shift(2) < sma
        exitlong_signal &= self.close.shift(3) < sma

        short_signal = self.close < lowest.shift()
        short_signal &= self.volume > vol_highest.shift()
        short_signal &= self.close < sma

        exitshort_signal = self.close > sma
        exitshort_signal &= self.close.shift() > sma
        exitshort_signal &= self.close.shift(2) > sma
        exitshort_signal &= self.close.shift(3) > sma
        return highest, lowest, sma, vol_highest, long_signal, exitlong_signal, short_signal, exitshort_signal


class Khaled_Tamim_Avellaneda_Stoikov_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/uJ64btmF-Khaled-Tamim-s-Avellaneda-Stoikov-Strategy/"""
    params = dict(gamma=2., sigma=8., T=0.0833, K=0.6185, M=0.5)
    overlap = dict(long_signal=False, short_signal=False)

    def next(self):
        def avellanedaStoikov(src: IndSeries, gamma=0., sigma=0., T=0., K=0., **Kwargs):
            midPrice = src.sma(3)
            sqrtTerm = gamma * sigma * sigma * T
            bidQuote = midPrice - K * sqrtTerm
            askQuote = midPrice + K * sqrtTerm
            return bidQuote, askQuote
        bidQuote, askQuote = avellanedaStoikov(self.close, **self.params)
        long_signal = self.low < bidQuote
        short_signal = self.high > askQuote
        return bidQuote, askQuote, long_signal, short_signal


class TTP_Intelligent_Accumulator(BtIndicator):
    """https://cn.tradingview.com/script/lic0Tapd-TTP-Intelligent-Accumulator/"""
    params = dict(length=14, rsi_length=7, mult=1.6185)
    overlap = False

    def next(self):
        rsi = self.close.rsi(self.params.rsi_length)
        rsima = rsi.sma(self.params.length)
        rsistd = rsi.stdev(self.params.length)
        up = rsima+self.params.mult*rsistd
        dn = rsima-self.params.mult*rsistd
        long_signal = rsi.cross_up(dn)
        exitshort_signal = long_signal
        # long_signal &= rsi > 50.
        short_signal = rsi.cross_down(up)
        exitlong_signal = short_signal
        # short_signal &= rsi < 50.

        return rsi, up, dn, long_signal, short_signal


class Aroon_and_ASH_strategy(BtIndicator):
    """https://cn.tradingview.com/script/12aAlkJB-Aroon-and-ASH-strategy-ETHERIUM-IkkeOmar/"""
    params = dict(length_upper_long=56, length_lower_long=20,
                  length_upper_short=17, length_lower_short=55, length=5)
    overlap = dict(ahma=True, upper_long=False, lower_long=False, upper_short=False, lower_short=False,
                   long_signal=False, exitlong_signal=False, short_signal=False, exitshort_signal=False)

    def next(self):
        upper_long: IndSeries = 100. * (self.high.rolling(self.params.length_upper_long+1).apply(lambda x: int(
            np.argmax(x[::-1]))) + self.params.length_upper_long) / self.params.length_upper_long
        lower_long: IndSeries = 100. * (self.low.rolling(self.params.length_lower_long + 1).apply(lambda x: int(
            np.argmin(x[::-1]))) + self.params.length_lower_long) / self.params.length_lower_long

        upper_short: IndSeries = 100. * (self.high.rolling(self.params.length_upper_short + 1).apply(lambda x: int(
            np.argmax(x[::-1]))) + self.params.length_upper_short) / self.params.length_upper_short
        lower_short: IndSeries = 100. * (self.low.rolling(self.params.length_lower_short + 1).apply(lambda x: int(
            np.argmin(x[::-1]))) + self.params.length_lower_short) / self.params.length_lower_short

        size = self.close.size
        # close = self.close.values
        # ahma = self.close.values
        length = self.params.length
        _length = 2*length+1
        hma_ = self.close.hma(length)
        hma = hma_.values
        ahma = hma_.values
        for i in range(size):
            if i > _length:
                ahma[i] = ahma[i-1] + \
                    (hma[i]-(ahma[i-1]+ahma[i-length])/2.)/length
        long_signal = upper_long.cross_up(lower_long)
        long_signal &= lower_long >= 5.
        exitlong_signal = upper_long.cross_down(lower_long)

        short_signal = upper_short.cross_down(lower_short)
        exitshort_signal = upper_short.cross_up(lower_short)
        return ahma, upper_long, lower_long, upper_short, lower_short, long_signal, exitlong_signal, short_signal, exitshort_signal


class Good_Mode_RSI_v2(BtIndicator):
    """https://cn.tradingview.com/script/X1NYTex2/"""
    params = dict(rsi_period=2, sell_level=96, buy_level=4,
                  fit_level_sell=20, fit_level_buy=80)
    overlap = False

    def next(self):
        rsi = self.close.rsi(self.params.rsi_period)
        long_signal = rsi < self.params.buy_level
        exitlong_signal = rsi > self.params.fit_level_buy

        short_signal = rsi > self.params.sell_level
        exitshort_signal = rsi < self.params.fit_level_sell
        return rsi, long_signal, exitlong_signal, short_signal, exitshort_signal


class Buy_Sell_Bullish_Engulfing_The_Quant_Science(BtIndicator):
    """https://cn.tradingview.com/script/0moOi6G5-Buy-Sell-Bullish-Engulfing-The-Quant-Science/"""
    params = dict(rule=False, ma_len1=50, ma_len2=200, length=14,
                  percent=5., factor=2., equals_percent=100., atr_length=30)
    overlap = False

    def next(self):
        sma = self.close.sma(self.params.ma_len1)
        up = self.close > sma
        dn = self.close < sma
        if not self.params.rule:
            sma1 = self.close.sma(self.params.ma_len2)
            up &= sma > sma1
            dn &= sma < sma1
        C_BodyHi = self.close.tqfunc.max(self.open)  # math.max(close, open)
        C_BodyLo = self.close.tqfunc.min(self.open)
        C_Body = C_BodyHi - C_BodyLo
        C_BodyAvg = C_Body.ema(self.params.length)  # ta.ema(C_Body, C_Len)
        C_SmallBody = C_Body < C_BodyAvg
        C_LongBody = C_Body > C_BodyAvg
        C_UpShadow = self.high - C_BodyHi
        C_DnShadow = C_BodyLo - self.low
        C_HasUpShadow = (C_UpShadow > self.params.percent /
                         100 * C_Body).astype(np.float32)
        C_HasDnShadow = (C_DnShadow > self.params.percent /
                         100 * C_Body).astype(np.float32)
        C_WhiteBody = self.open < self.close
        C_BlackBody = self.open > self.close
        C_Range = self.high-self.low
        C_IsInsideBar = (C_BodyHi.shift() > C_BodyHi) & (
            C_BodyLo.shift() < C_BodyLo)
        C_BodyMiddle = C_Body / 2 + C_BodyLo

        C_ShadowEquals = (C_UpShadow == C_DnShadow) | ((((C_UpShadow - C_DnShadow).abs().ZeroDivision(C_DnShadow) * 100) < self.params.equals_percent) &
                                                       (((C_DnShadow - C_UpShadow).abs().ZeroDivision(C_UpShadow) * 100) < self.params.equals_percent))
        C_IsDojiBody = (C_Range > 0.) & (
            C_Body <= C_Range * self.params.percent / 100.)
        C_Doji = C_IsDojiBody & C_ShadowEquals

        patternLabelPosLow = self.low - 0.6 * \
            self.atr(self.params.atr_length)  # (ta.atr(30) * 0.6)
        patternLabelPosHigh = self.high + 0.6 * \
            self.atr(self.params.atr_length)  # (ta.atr(30) * 0.6)

        long_signal = dn & C_WhiteBody & C_LongBody & C_BlackBody.shift() & C_SmallBody.shift() & (self.close >= self.open.shift()) &\
            (self.open <= self.close.shift()) & (
                (self.close > self.open.shift()) | (self.open < self.close.shift()))
        # long_signal = up
        # long_signal &= self.open < self.close
        # long_signal &= C_Body > C_BodyAvg
        # long_signal &= self.open.shift() > self.close.shift()
        # long_signal &= C_Body.shift() < C_BodyAvg.shift()
        # long_signal &= self.close >= self.open.shift()
        # long_signal &= self.open <= self.close.shift()
        # long_signal &= (self.close > self.open.shift()) | (
        #     self.open < self.close.shift())
        # short_signal = dn
        # short_signal &= self.open > self.close
        # short_signal &= C_Body > C_BodyAvg
        # short_signal &= self.open.shift() < self.close.shift()
        # short_signal &= C_Body.shift() < C_BodyAvg.shift()
        # short_signal &= self.close <= self.open.shift()
        # short_signal &= self.open >= self.close.shift()
        # short_signal &= (self.close < self.open.shift()) | (
        #     self.open > self.close.shift())
        return sma, long_signal  # , short_signal


class Crunchster_Normalised_Trend_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/KwYFiCcZ-Crunchster-s-Normalised-Trend-Strategy/"""
    params = dict(length=14, hlength=100)
    overlap = True

    def next(self):
        diff: IndSeries = self.close-self.close.shift()
        nret = diff.ZeroDivision(diff.stdev(self.params.length))
        nret = nret.values
        size = self.close.size
        nprice = np.zeros(size)
        cums = 0.
        for i, value in enumerate(nret):
            if not np.isnan(value):
                cums += value
                nprice[i] = cums
        # nprice = self.to_IndSeries(nprice, self.close.ind_setting(name="nprice"))
        nprice: IndSeries = IndSeries(nprice)
        hma = nprice.hma(self.params.hlength)
        long_signal = nprice.cross_up(hma)
        short_signal = nprice.cross_down(hma)
        nprice = nprice.values
        return nprice, hma, long_signal, short_signal


class The_Flash_Strategy_(BtIndicator):
    """https://cn.tradingview.com/script/XKgLfo15-The-Flash-Strategy-Momentum-RSI-EMA-crossover-ATR/"""
    params = dict(length=10, period=10, mult=3.,
                  pmax_length=12, pmax_mult=3., mom_rsi_val=60.)
    overlap = False

    def next(self):
        mom = self.close-self.close.shift(self.params.length)
        rsi_mom = mom.rsi(self.params.length)

        supertrend, direction = self.supertrend(
            self.params.period, self.params.length).to_lines("trend", "dir")
        pmax_thrend = self.close.btind.pmax(
            self.params.pmax_length, self.params.pmax_mult, mode="ema").thrend
        long_signal = pmax_thrend > 0.
        long_signal &= direction < 0.
        long_signal &= rsi_mom > self.params.mom_rsi_val
        exitlong_signal = self.close.cross_down(supertrend)

        short_signal = pmax_thrend < 0.
        short_signal &= direction > 0.
        short_signal &= rsi_mom > self.params.mom_rsi_val
        exitshort_signal = self.close.cross_up(supertrend)

        return long_signal, exitlong_signal, short_signal, exitshort_signal

    def step(self):
        if not self.kline.position.pos:
            if self.long_signal.new:
                self.kline.buy()
            elif self.short_signal.new:
                self.kline.sell()
        else:
            if self.short_signal.new:
                self.kline.set_target_size(-1)
            elif self.long_signal.new:
                self.kline.set_target_size(1)


class BreakOut_Consecutive(BtIndicator):
    """https://cn.tradingview.com/script/nt3wgHEc-X48-Strategy-BreakOut-Consecutive-11in1-Alert-V-1-2/"""

    def next(self):
        ...


class Risk_Adjusted_Leveraging(BtIndicator):
    """https://cn.tradingview.com/script/4D98MBT9/"""
    params = dict(length=100, atr_length=14)
    overlap = False
    # plotinfo = dict(height=300)

    def next(self):
        t3 = self.close.t3(10)
        ema = self.close.ema(144)
        atr = self.atr(self.params.atr_length)/self.close
        avg_atr = atr.sma(self.params.length)
        ratio = atr / avg_atr
        targetLeverage = 2. / ratio
        targetOpentrades = 5. * targetLeverage
        level1 = targetLeverage.quantile(q=0.25)
        level2 = targetLeverage.quantile(q=0.75)
        long_signal = targetLeverage > level2
        long_signal &= t3 > t3.shift()
        long_signal &= ema > ema.shift()
        exitlong_signal = targetLeverage < level1

        return targetLeverage, targetOpentrades, long_signal, exitlong_signal


class Dual_Supertrend_with_MACD_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/zFKRj4Gi-Dual-Supertrend-with-MACD-Strategy-presentTrading/"""
    params = dict(super_length1=10, super_mult1=3.,
                  super_length2=20, super_mult2=5.)

    def next(self):
        macd = self.close.macd()
        supertrend1 = self.supertrend(
            self.params.super_length1, self.params.super_mult1)
        supertrend2 = self.supertrend(
            self.params.super_length2, self.params.super_mult2)

        long_signal = (supertrend1.dir < 0.) & (
            supertrend2.dir < 0.) & (macd.macds > 0.)
        short_signal = (supertrend1.dir > 0.) & (
            supertrend2.dir > 0.) & (macd.macds < 0.)
        exitlong_signal = (supertrend1.dir > 0.) | (
            supertrend2.dir > 0.) | (macd.macds < 0.)
        exitshort_signal = (supertrend1.dir < 0.) | (
            supertrend2.dir < 0.) | (macd.macds > 0.)

        return long_signal, short_signal, exitlong_signal, exitshort_signal


class POKI_GTREND_ADX(BtIndicator):
    """https://cn.tradingview.com/script/B3J5Ledg-Strategy-Myth-Busting-5-POKI-GTREND-ADX-MYN/"""
    params = dict(atr_length=29)

    def next(self):
        atr = self.atr(self.params.atr_length)
        prevhigh = self.close.shift() + atr
        prevlow = self.close.shift() - atr


class Super8_30M_BTC(BtIndicator):
    """https://cn.tradingview.com/script/zFPZofwt/"""
    params = dict(sEma_Length=500, fEma_Length=100, ADX_len=28, ADX_smo=9, th=20.5, Sst=0.1, Sinc=0.04, Smax=0.4, fastLength=24, slowLength=52, signalLength=11, lengthz=14,
                  lengthStdev=14, A=-0.2, B=0.4, volume_f=0.4, sma_Length=55, BB_Length=40, BB_mult=2.2, bbMinWidth01=5., bbMinWidth02=2., tp=1.8, trailOffset=0.3, DClength=55,
                  MACD_options='MACD')
    overlap = False

    def calc_zvwap(self, length):
        vwma = (self.close*self.volume).sma(length) / self.volume.sma(length)
        vwapsd = (
            self.close-vwma).apply(lambda x: math.pow(x, 2)).sma(length).apply(math.sqrt)
        return (self.close-vwma)/vwapsd

    def next(self):
        # Calculating the slow and fast Exponential Moving Averages (EMAs) using the close price
        # ta.ema(close, sEma_Length)
        sEMA = self.close.ema(self.params.sEma_Length)
        # ta.ema(close, fEma_Length)
        fEMA = self.close.ema(self.params.fEma_Length)

        # Setting the conditions for a long or short signal based on the relationship between the fast and slow EMAs
        EMA_longCond = (fEMA > sEMA) & (sEMA > sEMA.shift())
        EMA_shortCond = (fEMA < sEMA) & (sEMA < sEMA.shift())

        # Calculating the Directional Indicator Plus (+DI), Directional Indicator Minus (-DI), and Average Directional Movement Index (ADX)
        # [DIPlus, DIMinus, ADX] = ta.dmi(ADX_len, ADX_smo)
        adx = self.adx(self.params.ADX_len, self.params.ADX_smo)
        # Setting the conditions for a long or short signal based on the relationship between +DI and -DI and the ADX value
        ADX_longCond = (adx.dmp > adx.dmn) & (adx.adxx > self.params.th)
        ADX_shortCond = (adx.dmp < adx.dmn) & (adx.adxx > self.params.th)

        # Calculating the Parabolic SAR (SAR) based on the parameters set for step, max, and acceleration factor
        # ta.sar(Sst, Sinc, Smax)
        sar = self.talib.SAR(self.params.Sinc, self.params.Smax)
        # sar = self.finta.PSAR(self.params.Sinc, self.params.Smax)
        # Setting the conditions for a long or short signal based on the relationship between the SAR value and the close price
        SAR_longCond = sar < self.close
        SAR_shortCond = sar > self.close

        # Calculating the Moving Average Convergence Divergence (MACD) and its signal line, as well as the MACD-Z value
        # Define three variables lMACD, sMACD, and hist by calling the ta.macd() function using the 'close', 'fastLength', 'slowLength', and 'signalLength' as parameters
        macd = self.close.macd(self.params.fastLength,
                               self.params.slowLength, self.params.signalLength)

        # Call the function calc_zvwap(lengthz) to calculate the z-score of the VWAP for a given period 'lengthz'
        zscore = self.calc_zvwap(self.params.lengthz)
        # Calculate the simple moving averages of the 'close' prices using 'fastLength' and 'slowLength' periods, and assign them to 'fastMA' and 'slowMA' respectively
        fastMA = self.close.sma(self.params.fastLength)
        slowMA = self.close.sma(self.params.slowLength)
        # Assign the 'lMACD' variable to 'macd'
        macdx = macd.macdx
        # Calculate 'macz' by multiplying the z-score by a constant 'A',
        # adding the 'macd' value, and dividing by the product of the standard deviation of the 'close' prices over a period 'lengthStdev' and a constant 'B'
        macz = (zscore * self.params.A) + (macdx /
                                           (self.close.stdev(self.params.lengthStdev) * self.params.B))
        # Calculate the simple moving average of the 'macz' values over a period 'signalLength' and assign it to 'signal'
        signal = macz.sma(self.params.signalLength)
        # Calculate the difference between 'macz' and 'signal' and assign it to 'histmacz'
        histmacz = macz - signal

        # ————— MACD conditions
        # Define two boolean variables 'MACD_longCond' and 'MACD_shortCond'
        # If 'MACD_options' is equal to 'MACD', check if the 'hist' value is greater than 0 and assign the result to 'MACD_longCond';
        # otherwise, check if 'histmacz' is greater than 0 and assign the result to 'MACD_longCond'
        MACD_longCond = macd.macds > 0. if self.params.MACD_options == 'MACD' else histmacz > 0.
        # If 'MACD_options' is equal to 'MACD', check if the
        MACD_shortCond = macd.macds < 0. if self.params.MACD_options == 'MACD' else histmacz < 0.

        # ———————————————————— Bollinger Bands
        # ————— BB calculation
        # Calculates the middle, upper and lower bands using the Bollinger Bands technical analysis indicator

        BB_lower, BB_middle, BB_upper, BB_width, bb_percent = self.close.bbands(
            self.params.BB_Length, self.params.BB_mult).to_lines()

        # ————— Long Bollinger Bands conditions
        # Defines the conditions for entering a long position using Bollinger Bands
        # New Longs
        BB_long01 = (~ADX_shortCond) & self.low.cross_down(
            BB_lower) & EMA_longCond & (BB_width > self.params.bbMinWidth01)

        # Pyramiding Longs
        BB_long02 = (~ADX_shortCond) & self.low.cross_down(
            BB_lower) & EMA_longCond & (BB_width > self.params.bbMinWidth02)

        # ————— Short Bollinger Bands conditions
        # Defines the conditions for entering a short position using Bollinger Bands
        # New Shorts
        BB_short01 = (~ADX_longCond) & self.high.cross_up(
            BB_upper) & EMA_shortCond & (BB_width > self.params.bbMinWidth01)

        # Pyramiding Shorts
        BB_short02 = (~ADX_longCond) & self.high.cross_up(
            BB_upper) & EMA_shortCond & (BB_width > self.params.bbMinWidth02)

        # ———————————————————— Volume
        # Defines conditions for long and short positions based on volume
        VOL_longCond = self.volume > self.volume.sma(
            self.params.sma_Length) * self.params.volume_f
        VOL_shortCond = VOL_longCond

        # ———————————————————— Strategy
        # Defines the long and short conditions for entering a trade based on multiple indicators and volume
        long_signal = EMA_longCond & ADX_longCond & SAR_longCond & MACD_longCond & VOL_longCond
        long_signal |= BB_long01

        short_signal = EMA_shortCond & ADX_shortCond & SAR_shortCond & MACD_shortCond & VOL_shortCond
        short_signal |= BB_short01
        return long_signal, short_signal


class HalfTrend_HullButterfly(BtIndicator):
    """https://cn.tradingview.com/script/lL04fUNr-Strategy-Myth-Busting-20-HalfTrend-HullButterfly-MYN/"""
    params = dict(length=11, mult=2., amplitude=3, channelDeviation=2, m=2)
    overlap = dict(hso=False, up=False, dn=False, os=False,
                   ht=True, atrHigh=True, atrLow=True)

    @staticmethod
    def Hull_squeeze_oscillator(close: pd.Series, hull_coeffs=None):
        size = close.size
        close = close.values
        hma = 0.
        inv_hma = 0.
        for i in range(size):
            hma += close[i]*hull_coeffs[i]
            inv_hma += close[size-1-i] * hull_coeffs[i]
        hso = hma - inv_hma
        return hso

    def next(self):
        length = self.params.length
        short_len = int(length / 2)
        hull_len = int(math.sqrt(length))
        den1 = short_len * (short_len + 1) / 2
        den2 = length * (length + 1) / 2
        den3 = hull_len * (hull_len + 1) / 2
        lcwa_coeffs1 = [0.]*(hull_len-1)
        lcwa_coeffs2 = []
        for i in range(length):
            sum1 = max(short_len - i, 0)
            sum2 = length - i
            lcwa_coeffs2.append(2.*(sum1/den1)-sum2/den2)
        lcwa_coeffs3 = [0.]*hull_len
        lcwa_coeffs = lcwa_coeffs1+lcwa_coeffs2[::-1]+lcwa_coeffs3
        hull_coeffs = []
        for i in range(hull_len, len(lcwa_coeffs)):
            sum3 = 0.
            for j in range(i-hull_len, i):
                sum3 += lcwa_coeffs[j] * (i - j)
            hull_coeffs.append(sum3 / den3)

        hso = self.close.rolling(len(hull_coeffs)).apply(
            partial(self.Hull_squeeze_oscillator, hull_coeffs=hull_coeffs))
        up = hso.apply(np.abs).sma(len(hull_coeffs))*self.params.mult
        dn = -up
        os = pd.Series([0.]*self.close.size)
        condition1 = (hso > hso.shift()) & (hso < dn)
        os = os.mask(condition1, 1.)
        condition2 = (hso < hso.shift()) & (hso > up)
        os = os.mask(condition2, -1.)
        os *= 20.
        return hso, up, dn, os


class yuthavithi_volatility_based_force_trade_scalper_strategy(BtIndicator):
    """https://cn.tradingview.com/script/FN3ZkHZA-yuthavithi-volatility-based-force-trade-scalper-strategy/"""
    params = dict(fast=3, slow=20, atrFast=20, atrSlow=50, length=20, mult=2,)
    overlap = True

    def next(self):
        bbMid = self.close.sma(self.params.length)
        atrFastVal = self.atr(self.params.atrFast)
        atrSlowVal = self.atr(self.params.atrSlow)
        stdOut = self.close.stdev(self.params.length)
        bbUpper = bbMid + stdOut * self.params.mult
        bbLower = bbMid - stdOut * self.params.mult

        force = self.volume * (self.close - self.close.shift())
        xforce = force.rolling(self.params.fast).sum()
        xforceFast = xforce.ema(self.params.fast)
        xforceSlow = xforce.ema(self.params.slow)
        long_signal = ((xforceFast < xforceSlow) & (atrFastVal > atrSlowVal)) & ((xforceFast.shift(
        ) > xforceSlow.shift()) | (atrFastVal.shift() < atrSlowVal.shift())) & (self.close < self.open)
        short_signal = ((xforceFast > xforceSlow) & (atrFastVal > atrSlowVal)) & ((xforceFast.shift(
        ) < xforceSlow.shift()) | (atrFastVal.shift() < atrSlowVal.shift())) & (self.close > self.open)
        return bbUpper, bbLower, long_signal, short_signal


class MicuRobert_EMA_cross_V2(BtIndicator):
    """https://cn.tradingview.com/script/6YthKHw1-STRATEGY-RS-MicuRobert-EMA-cross-V2/"""
    params = dict(length1=5, length=34)

    def next(self):
        ma1 = self.close.zlma(self.params.length1, plotinfo=dict(lineinfo=dict(
            line=dict(line_dash="4 4"))))
        ma2 = self.close.zlma(self.params.length2)

        long_signal = ma1.cross_up(ma2)
        long_signal |= self.close.cross_up(ma1) & ma1 > ma2

        short_signal = ma1.cross_down(ma2)
        short_signal |= self.close.cross_down(ma1) & ma1 < ma2

        return ma1, ma2, long_signal, short_signal


class Follow_the_Janet_Yellen(BtIndicator):
    """https://cn.tradingview.com/script/qEc9DKoP-STRATEGY-Follow-the-Janet-Yellen/"""
    params = dict(length=100)
    overlap = False

    def next(self):
        pre_close = self.close.shift().bfill()
        slope_ = self.close.linreg(self.params.length) - \
            pre_close.linreg(self.params.length)
        return slope_.values


class HullMA_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/qQ0MJCVL-HullMA-Strategy/"""
    params = dict(n=16)
    overlap = True

    def next(self):
        n = self.params.n
        length = int(n/2)
        n2ma = 2.*self.close.wma(length)
        nma = self.close.wma(n)
        diff = n2ma-nma
        sqn = int(math.sqrt(n))

        n2ma1 = 2.*self.close.shift().wma(length)
        nma1 = self.close.shift().wma(n)
        diff1 = n2ma1-nma1

        n1 = diff.wma(sqn)
        n2 = diff1.wma(sqn)
        long_signal = n1.cross_up(n2)
        short_signal = n1.cross_down(n2)
        exitlong_signal = short_signal
        exitshort_signal = long_signal

        return n1, n2, long_signal, short_signal, exitlong_signal, exitshort_signal


class AK_RSI_2_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/klkZ7SdU-AK-RSI-2-Strategy-based-on-Chris-Moody-RSI-2-indicator/"""
    params = dict(rsi_length=2, fast=5, slow=60)
    overlap = dict(rsi=False, ma1=True, ma2=True)

    def next(self):
        rsi = self.close.rsi(self.params.rsi_length)
        ma1 = self.close.ema(self.params.fast)
        ma2 = self.close.ema(self.params.slow)

        long_signal = self.close > ma2
        long_signal &= self.close < ma1
        long_signal &= rsi < 10.

        short_signal = self.close < ma2
        short_signal &= self.close > ma1
        short_signal &= rsi > 90.

        return rsi, ma1, ma2, long_signal, short_signal


class AK_TREND_ID_AS_A_STRATEGY(BtIndicator):
    """https://cn.tradingview.com/script/ztKUkLYL-AK-TREND-ID-AS-A-STRATEGY-FOR-EDUCATIONAL-PURPOSES-ONLY/"""
    params = dict(fast=14, slow=34)
    overlap = True

    def next(self):
        fastmaa = self.close.ema(self.params.fast)
        fastmab = self.close.ema(self.params.slow)

        bspread = (fastmaa-fastmab)
        up = bspread*1.001
        dn = bspread*0.999

        long_signal = up > 0.
        short_signal = dn < 0.

        return fastmaa, fastmab, long_signal, short_signal


class MACD_SMA_200_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/yMCa3XZD-MACD-SMA-200-Strategy-by-ChartArt/"""
    params = dict(fastLength=12, slowLength=26,
                  signalLength=9, veryslowLength=200)

    def next(self):
        fastMA = self.close.sma(self.params.fastLength)
        slowMA = self.close.sma(self.params.slowLength)
        veryslowMA = self.close.sma(self.params.veryslowLength)
        macd = fastMA - slowMA
        signal = macd.sma(self.params.signalLength)
        hist = macd - signal

        long_signal = hist.cross_up(0.)
        long_signal &= macd > 0.
        long_signal &= fastMA > slowMA
        # long_signal &=self.close.shift(self.params.slowLength)>veryslowMA
        long_signal &= slowMA > veryslowMA

        short_signal = hist.cross_down(0.)
        short_signal &= macd < 0.
        short_signal &= fastMA < slowMA
        # short_signal &=self.close.shift(self.params.slowLength)<veryslowMA
        short_signal &= slowMA < veryslowMA

        return fastMA, slowMA, long_signal, short_signal


class GetTrendStrategy(BtIndicator):
    """https://cn.tradingview.com/script/PHnPTxTd-gettrendstrategy/
    https://cn.tradingview.com/script/zeMUHHOD-RichG-Easy-MTF-Strategy/"""
    params = dict(t1=3, t2=5, t3=8, t4=13)
    overlap = True

    def next(self):
        # maxdata = self.resample(int(self.cycle*self.params.t), self.shape[0])
        # close_, open_ = maxdata.close(), maxdata.open()
        # long_signal = close_.cross_up(open_)
        # short_signal = close_.cross_down(open_)
        # return close_, open_, long_signal, short_signal
        open1 = self.open.shift(periods=self.params.t1, fill_value=0.)
        open2 = self.open.shift(periods=self.params.t2, fill_value=0.)
        open3 = self.open.shift(periods=self.params.t3, fill_value=0.)
        open4 = self.open.shift(periods=self.params.t4, fill_value=0.)
        long_signal = (self.close > open1) & (self.close > open2) & (
            self.close > open3) & (self.close > open4)
        short_signal = (self.close < open1) & (self.close < open2) & (
            self.close < open3) & (self.close < open4)
        return open1, open2, open3, open4, long_signal, short_signal


class RSI_versus_SMA(BtIndicator):
    """https://cn.tradingview.com/script/wDLCPh1I-RSI-versus-SMA-no-repaint/"""

    params = dict(rsiLength=8, maLength=34)

    @staticmethod
    def switchDelay(exp: IndSeries, len):
        avg = exp.sma(len)
        stats = exp > avg

    def next(self):
        ha = self.ha()


class Price_Divergence_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/IDoY5LgE-STRATEGY-UL-Price-Divergence-Strategy-v1-0/"""
    ...


class QQE_Cross(BtIndicator):
    """https://cn.tradingview.com/script/nkka4EfU-STRATEGY-UL-QQE-Cross-v1-1/"""
    ...


class PerfectStrategy(BtIndicator):
    """https://cn.tradingview.com/script/9JoYuVLt-PerfectStrategy/"""
    overlap = False

    def next(self):
        x = (self.open.shift(1, fill_value=0.)) / \
            (self.close.shift(1, fill_value=1.))
        x1 = (self.open.shift(2, fill_value=0.)) / \
            (self.close.shift(2, fill_value=1.))
        x2 = (self.open.shift(3, fill_value=0.)) / \
            (self.close.shift(3, fill_value=1.))
        x3 = (self.open.shift(4, fill_value=0.)) / \
            (self.close.shift(4, fill_value=1.))
        x4 = (self.open.shift(5, fill_value=0.)) / \
            (self.close.shift(5, fill_value=1.))
        x5 = (self.open.shift(6, fill_value=0.)) / \
            (self.close.shift(6, fill_value=1.))
        x6 = (self.open.shift(7, fill_value=0.)) / \
            (self.close.shift(7, fill_value=1.))
        x7 = (self.open.shift(8, fill_value=0.)) / \
            (self.close.shift(8, fill_value=1.))
        x8 = (self.open.shift(9, fill_value=0.)) / \
            (self.close.shift(9, fill_value=1.))
        y = (x+x1+x2+x3+x4+x5+x6+x7+x8)/9.
        long_signal = y < 1.
        short_signal = y > 1.
        return y, long_signal, short_signal


class Open_Close_Cross_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/vObmEraY-Open-Close-Cross-Strategy-R5-revised-by-JustUncleL/"""
    params = dict(length=60)
    overlap = True

    @staticmethod
    def _get_caf(len):
        a1 = np.exp(-1.414*np.pi/len)
        b1 = 2.*a1*np.cos(1.414*np.pi/len)
        c2 = b1
        c3 = -a1*a1
        c1 = 1.-c2-c3
        return np.array([c1/2., c2, c3])

    def variant(self, src: IndSeries, c: np.ndarray):
        size = src.size
        v = np.zeros(size)
        src = src.values
        for i in range(size):
            if i > 1:
                v[i] = (np.array([src[i]+src[i-1], v[i-1], v[i-2]])*c).sum()
        return v

    def next(self):
        c = self._get_caf(self.params.length)
        close_ = self.variant(self.close, c)
        open_ = self.variant(self.open, c)
        long_signal = self.cross_up(a=open_, b=close_)
        short_signal = self.cross_down(a=open_, b=close_)

        return open_, close_, long_signal, short_signal


class Ichimoku_Kinko_Hyo(BtIndicator):
    """https://cn.tradingview.com/script/XZcsLIEW-Ichimoku-Kinko-Hyo-Basic-Strategy/"""

    params = dict(ts_bars=9, ks_bars=26, ssb_bars=52,
                  cs_offset=26, ss_offset=26)

    def next(self):
        ichimoku = self.ichimoku(
            self.params.ts_bars, self.params.ks_bars, self.params.ssb_bars)


class Multi_Deviation_Scaled_Moving_Average(BtIndicator):
    """https://www.tradingview.com/script/Yqc5d8Lc-Multi-Deviation-Scaled-Moving-Average-ChartPrime/"""

    params = dict(period=30, step=40, num=8)
    overlap = dict(dsma=True, UP=False, DN=False)

    @staticmethod
    def _get_fac(period):
        pi = np.pi
        g = math.sqrt(2)
        # Smooth with a Super Smoother
        s = 2 * pi / period
        a1 = math.exp(-g * pi / (0.5 * period))
        b1 = 2 * a1 * math.cos(g * s / (0.5 * period))
        c2 = b1
        c3 = -a1 * a1
        c1 = 1 - c2 - c3
        return np.array([c1/2., c2, c3])

    def dsma(self, src: IndSeries, fac: np.ndarray, period: int):

        size = src.size
        zeros = self.close-self.close.shift()
        filt = np.zeros(size)
        for i in range(size):
            if i > 1:
                filt[i] = (fac*[zeros[i]+zeros[i-1],
                           filt[i-1], filt[i-2]]).sum()

        rms = self.ema(close=pd.Series(filt*filt),
                       length=period).apply(math.sqrt)
        scaled_filt = (
            filt/rms).fillna(0.).apply(lambda x: abs(x)*5./period)
        fac_ = 1.-scaled_filt
        dsma = np.zeros(size)
        close = self.close.values
        for i in range(size):
            if i:
                dsma[i] = scaled_filt[i]*close[i]+fac_[i]*dsma[i-1]
        return dsma

    def next(self):
        hlc = self.hlc3()
        period = self.params.period
        step = self.params.step
        num = self.params.num
        val = 1./(num-1)
        fac = self._get_fac(period)
        dsmas = [self.dsma(hlc, fac, period),]
        for i in range(1, num):
            period += step
            dsmas.append(self.dsma(hlc, fac, period))
        dsma = reduce(lambda x, y: x+y, dsmas)/num
        # dsmas_array=[ds.values for ds in dsmas]
        size = self.close.size
        score = np.zeros(size)
        for i in range(size):
            s = 0.
            for j in range(num-1):
                if dsmas[j][i] > dsmas[-1][i]:
                    s += val
            score[i] = s
        UP = score*100.
        DN = 100.-UP
        long_signal = self.cross_up(a=score, b=0.3)
        short_signal = self.cross_down(a=score, b=0.7)

        return dsma, UP, DN, long_signal, short_signal


class Bitcoin_Cycle_High_Low_with_functional_Alert(BtIndicator):
    """https://cn.tradingview.com/script/NcHYOjOH-Bitcoin-Cycle-High-Low-with-functional-Alert-heswaikcrypt/"""
    params = dict(lowma_long=471, lowema_short=150,
                  hima_long=350, hima_short=111)
    overlap = True

    def next(self):
        # // Run the math for the 4 MAs
        lowest = self.close.sma(self.params.lowma_long) * 0.745
        ema_short_low = self.close.ema(self.params.lowema_short)
        higthest = self.close.sma(self.params.hima_long) * 2
        ma_short_hi = self.close.sma(self.params.hima_short)
        # // Define Pi Cycle Low and High conditions
        PiCycleLow = ema_short_low.cross_down(lowest)
        PiCycleHi = higthest.cross_down(ma_short_hi)
        Para = ema_short_low.cross_up(lowest)
        return lowest, ema_short_low, higthest, ma_short_hi


class MadTrend(BtIndicator):
    """https://cn.tradingview.com/script/nhyCYGKn-MadTrend-InvestorUnknown/"""
    params = dict(length=60, mad_mult=1., src="close")
    overlap = True

    def calc_src(self, src: str) -> IndSeries:
        try:
            return getattr(self, src)
        except:
            ls = ["open" if v.startswith("o") else ("high" if v.startswith(
                "h") else ("low" if v.startswith("l") else "close")) for v in src]
            ls = [getattr(self, v) for v in ls]
            return sum(ls)/len(ls)

    def mad(self, length: int) -> IndSeries:
        med = self.close.rolling(window=length).median()  # // Calculate median
        # // Calculate absolute deviations from median
        abs_deviations: IndSeries = (self.close - med).abs()
        # // Return the median of the absolute deviations
        abs_deviations.rolling(window=length).median()
        return abs_deviations

    def next(self):
        # // Calculate MAD (volatility measure)
        src = self.calc_src(self.params.src)
        mad_value = self.mad(self.params.length)

        # // Calculate the MAD-based moving average by scaling the price data with MAD
        median = src.rolling(window=self.params.length).median()
        med_p = median + (mad_value * self.params.mad_mult)
        med_m = median - (mad_value * self.params.mad_mult)

        direction: IndSeries = IndSeries(np.zeros(src.size))
        direction = direction.Where(src > med_p, 1.)
        direction = direction.Where(src < med_m, -1.)
        return direction, median, med_p, med_m


class Follower(BtIndicator):
    """https://cn.tradingview.com/script/p428Jh4H/"""
    params = dict(num=20, uzunluk=14, oran=2.)

    def next(self):
        ort: IndSeries
        for i in range(self.params.num):
            if not i:
                ort = self.high.shift(i)-self.low.shift(i)
            else:
                ort += self.high.shift(i)-self.low.shift(i)
        ort /= self.params.num
        size = self.close.size
        src = self.close.ema(self.params.uzunluk)
        ustT = (self.high+self.params.oran *
                ort.sma(self.params.uzunluk)).ema(self.params.uzunluk)
        altT = (self.low-self.params.oran *
                ort.sma(self.params.uzunluk)).ema(self.params.uzunluk)
        follower = np.zeros(size, dtype=np.float32)
        length = len(ustT[np.isnan(ustT)])
        for j in range(size):
            ...


class Dual_Bayesian_For_Loop(BtIndicator):
    """https://cn.tradingview.com/script/3tpEiLqr-Dual-Bayesian-For-Loop-QuantAlgo/"""
    params = dict(length=14, lookback=70)
    lines = ("final", "signal", "binary_signal")
    overlap = False

    def forloop_analysis(self, source: pd.Series):
        sum = 0.0
        source = source.values
        new = source[-1]
        for i in range(0, self.params.lookback-1):
            sum += 1. if new > source[i] else -1.
        sum = sum / (self.params.lookback-1)
        return sum

    def bayesian_calc(self, loop_val: float):
        evidence = .7 if loop_val > 0. else .3
        prior = 0.5
        return (prior * evidence) / (prior * evidence + (1 - prior) * (1 - evidence))

    def next(self):
        source = self.hlc3()
        self.bayesian_calc1, self.bayesian_calc2 = self.bayesian_calc(
            1.), self.bayesian_calc(-1.)
        loop_score: IndSeries = source.rolling(
            window=self.params.lookback).apply(self.forloop_analysis)
        loop_score_sma = loop_score.sma(self.params.length)

        size = self.close.size
        final = np.zeros(size)  # = (short_prob * 100 + long_prob * 100) / 2
        # signal = np.zeros(size)  # = ta.ema(final, 2)
        # binary_signal = np.zeros(size)  # = signal > 50 ? 1 : -1
        loop_score, loop_score_sma = loop_score.values, loop_score_sma.values
        lennan = get_lennan(loop_score, loop_score_sma)
        for i in range(lennan, size):
            short_prob = self.bayesian_calc1 if loop_score[i] > 0. else self.bayesian_calc2
            long_prob = self.bayesian_calc1 if loop_score_sma[i] > 0. else self.bayesian_calc2
            final[i] = (short_prob + long_prob) * 50.
            # signal[i] = 2.*final[i]/3. + final[i-1]/3.
            # binary_signal[i] = 100. if signal[i] > 50. else 0.
        signal = IndSeries(final).ema(2)
        binary_signal = signal.apply(lambda x: 100 if x > 50. else 0.)

        return pd.Series(final), signal, binary_signal


class RSI_Trail(BtIndicator):
    """https://cn.tradingview.com/script/PUGvtsEu-RSI-Trail-UAlgo/"""
    params = dict(matype="ema", lower=40, upper=60, length=27)
    lines = ("upper_bound", "lower_bound", "long_signal", "short_signal")
    overlap = dict(upper_bound=True, lower_bound=True,
                   long_signal=False, short_signal=False)

    def mcginley(self, src: pd.Series, length: float):
        size = src.size
        src = src.values
        md = np.zeros(size)
        md[i] = src[0]
        for i in range(1, size):
            md[i] = md[i-1] + (src[i] - md[i-1]) / \
                (0.6 * length * math.pow(src[i] / md[i-1], 4))
        return md

    def calculate_bounds(self, ma, range, upper, lower) -> tuple[IndSeries]:
        upper_bound = ma + (upper - 50) / 10 * range
        lower_bound = ma - (50 - lower) / 10 * range
        return upper_bound, lower_bound

    def next(self):
        volatility = self.atr(self.params.length)
        ohlc4 = self.ohlc4()
        ma = ohlc4.ma(self.params.matype, self.params.length)
        upper_bound, lower_bound = self.calculate_bounds(
            ma, volatility, self.params.upper, self.params.lower)
        long_signal = ohlc4.cross_up(upper_bound)
        short_signal = self.close.cross_down(lower_bound)
        return upper_bound, lower_bound, long_signal, short_signal


class Adaptive_Trend_Flow_Strategy_with_Filters(BtIndicator):
    """https://cn.tradingview.com/script/CNJ1hXQw-Adaptive-Trend-Flow-Strategy-with-Filters-for-SPX/"""
    params = dict(atr_length=14, length=2, smooth_length=2, sensitivity=2., sma_length=4,
                  macd_fast_length=2, macd_slow_length=7, macd_signal_length=2, leverage_factor=4.5)
    overlap = dict(level=True, macd_line=False, signal_line=False)

    def calculate_trend_levels(self) -> tuple[IndSeries]:
        typical = self.hlc3()
        fast_ema = typical.ema(self.params.length)
        slow_ema = typical.ema(self.params.length * 2)
        basis = (fast_ema + slow_ema) / 2.
        vol = typical.stdev(self.params.length)
        smooth_vol = vol.ema(self.params.smooth_len)
        upper = basis + (smooth_vol * self.params.sensitivity)
        lower = basis - (smooth_vol * self.params.sensitivity)
        return basis, upper, lower

    def get_trend_state(self, upper: IndSeries, lower: IndSeries, basis: IndSeries) -> tuple[IndSeries]:
        size = self.close.size
        prev_level = np.full(size, fill_value=np.nan)
        trend = np.zeros(size, dtype=np.float32)
        close = self.close.values
        upper, lower, basis = upper.values, lower.values, basis.values
        lennan = get_lennan(upper, lower, basis)
        trend[lennan] = close[lennan] > basis[lennan] and 1. or -1.
        prev_level[lennan] = trend[lennan] == 1 and lower[lennan] or upper[lennan]
        for i in range(lennan+1, size):
            if trend[i-1] == 1.:
                if close[i] < lower[i]:
                    trend[i] = -1.
                    prev_level[i] = max(upper[i], prev_level[i-1])
                else:
                    prev_level[i] = lower[i]
            else:
                if close[i] > upper[i]:
                    trend[i] = 1.
                    prev_level[i] = min(lower[i], prev_level[i-1])
                else:
                    prev_level[i] = upper[i]
        return IndSeries(trend), IndSeries(prev_level)

    def next(self):
        basis, upper, lower = self.calculate_trend_levels()
        trend, level = self.get_trend_state(upper, lower, basis)
        # SMA filter
        sma_value = self.close.sma(self.params.sma_length)
        # sma_condition = self.close > sma_value

        # MACD filter
        macd = self.close.macd(self.params.macd_fast_length,
                               self.params.macd_slow_length, self.params.macd_signal_length)
        # macd_line, signal_line = macd.macdx, macd.macds
        # macd_condition = macd_line > signal_line

        # Signal detection with filters
        long_signal = trend == 1.
        long_signal &= trend.shift() == -1.
        long_signal &= self.close > sma_value
        long_signal &= macd.macdh > 0.
        short_signal = trend == -1.
        short_signal &= trend.shift() == 1.
        short_signal &= self.close < sma_value
        short_signal &= macd.macdh < 0.
        # short_signal = trend == -1.
        # short_signal &= trend.shift() == 1.
        return level, long_signal, short_signal


class Scalper_Pro(BtIndicator):
    """https://cn.tradingview.com/script/iQr6gCaJ-Scalper-Pro/"""
    params = dict(h_left=10, h_right=10)

    def next(self):
        # PTZ Points
        h_left_low = self.low.tqfunc.llv(self.params.h_left)
        h_left_high = self.high.tqfunc.hhv(self.params.h_right)
        newlow = (self.low <= h_left_low).astype(np.float32)
        newhigh = (self.high >= h_left_high).astype(np.float32)

        # Replace Large Arrows with Labels
        size = self.close.size
        length = self.params.h_left+self.params.h_right
        central_bar_is_highest = np.zeros(size)
        central_bar_is_lowest = np.zeros(size)
        low, high = self.low.values, self.high.values
        right = self.params.h_right
        for i in range(length, size):
            central_bar_low = low[i-right]
            central_bar_high = high[i-right]
            full_zone_low = low[i-length:i+1].min()
            full_zone_high = high[i-length:i+1].max()
            if central_bar_high >= full_zone_high:
                central_bar_is_highest[i] = 1.
            if central_bar_low <= full_zone_low:
                central_bar_is_lowest[i] = 1.


class Shendeng_ATR_Trend_EMA(BtIndicator):
    """https://cn.tradingview.com/script/NTsY39nG/"""
    params = dict(typeatr="atr", a=2., c=30)
    overlap = dict(xATRTrailingStop=True, pos=False)

    def next(self):
        # Buy Sell Indicator Calculation
        xATR = getattr(self, self.params.typeatr)(self.params.c)
        nLoss = self.params.a * xATR
        src_bsi = self.close
        size = src_bsi.size
        up = src_bsi+nLoss
        dn = src_bsi-nLoss
        up, dn, src_bsi = up.values, dn.values, src_bsi.values
        lenght = get_lennan(up, dn)
        xATRTrailingStop = np.zeros(size)
        pos = np.zeros(size)
        for i in range(lenght, size):
            pre_src, src = src_bsi[i-1], src_bsi[i]
            iff_1 = src > xATRTrailingStop[i-1] and dn[i] or up[i]
            iff_2 = min(xATRTrailingStop[i-1], up[i]) if (src < xATRTrailingStop[i-1] and pre_src <
                                                          xATRTrailingStop[i-1]) else iff_1
            xATRTrailingStop[i] = max(xATRTrailingStop[i-1], dn[i]) if (src > xATRTrailingStop[i-1] and pre_src
                                                                        > xATRTrailingStop[i-1]) else iff_2
            pos[i] = 1. if (pre_src < xATRTrailingStop[i-1] and src > xATRTrailingStop[i-1]) else \
                (-1. if (pre_src > xATRTrailingStop[i-1]
                 and src < xATRTrailingStop[i-1]) else pos[i-1])

        ema = self.close.ema(1, talib=False)
        above = ema.cross_up(xATRTrailingStop)
        below = ema.cross_down(xATRTrailingStop)
        long_signal = self.close > xATRTrailingStop
        long_signal &= above
        short_signal = self.close < xATRTrailingStop
        short_signal &= below
        return xATRTrailingStop, pos, long_signal, short_signal


class RSI_over_screener_6_TABLO(BtIndicator):
    """https://cn.tradingview.com/script/k71b4U1m/"""

    def next(self):
        _max, _min = self.params.max, self.params.min
        size = self.close.size
        N = _max - _min + 1
        # diff = self.close  # self.close.shift().fillna(0.)
        dif = IndFrame(
            self[["low", "high"]].values, lines=["low", "high"])

        def test(close):
            return close.min(),  close.max()
        ra = self.close.rolling_apply(
            test, 3, lines=["low", "high",])
        # sell = ra.overbuy.shift() > 0. & ra.overbuy == 0.
        # buy = ra.oversell.shift() > 0. & ra.oversell == 0
        # diff.rolling_apply()
        return ra


class Martingale_Short(BtIndicator):
    """https://cn.tradingview.com/script/alsaKnmI/"""
    params = dict(buffer=0.035)

    def next(self):
        b1: IndSeries = self.close.shift(
            1) * (1 + self.params.buffer / 100.)
        b2: IndSeries = self.close.shift(
            1) * (1 - self.params.buffer / 100.)
        long_signal = self.close.cross_up(b1)
        exitlong_signal = self.close.cross_down(b2)
        return long_signal, exitlong_signal


class IU_4Bar_UP_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/fUiBVHl7-IU-4-Bar-UP-Strategy/"""
    params = dict(length=14, factor=1.)

    def next(self):
        bullish_bar = self.close > self.open
        four_bull_bar = bullish_bar & bullish_bar.shift(
            1) & bullish_bar.shift(2) & bullish_bar.shift(3)
        supertrend = self.supertrend(self.params.length, self.params.factor)
        long_signal = four_bull_bar & (self.close > supertrend.long)
        exitlong_signal = self.close < supertrend.long

        bear_bar = self.close < self.open
        four_bear_bar = bear_bar & bear_bar.shift(
            1) & bear_bar.shift(2) & bear_bar.shift(3)
        # supertrend=self.supertrend(self.params.length,self.params.factor)
        short_signal = four_bear_bar & (self.close < supertrend.short)
        exitshort_signal = self.close > supertrend.short
        # long,short=supertrend.long, supertrend.short
        # return long, short, long_signal, exitlong_signal, short_signal, exitshort_signal
        return supertrend.long, supertrend.short, long_signal, exitlong_signal, short_signal, exitshort_signal


class Max_Pain_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/JUJejit7/"""
    params = dict(length=20, volMultiplier=1.,
                  priceMultiplier=5., holdPeriods=20)
    overlap = False

    def next(self):
        # / === Calculations ===
        # // Average volume over the period
        averageVolume = self.volume.sma(self.params.length)
        # // Price change over the 'length' period
        priceChange: IndSeries = (
            self.close - self.close.shift(self.params.length)).abs()

        # // Calculate the pain zone condition (potential delta-hedging areas)
        long_signal = (self.volume > averageVolume * self.params.volMultiplier) & (
            priceChange > self.params.priceMultiplier*self.price_tick)
        short_signal = (self.volume > averageVolume * self.params.volMultiplier) & (
            priceChange < self.params.priceMultiplier*self.price_tick)
        return self.volume, averageVolume, long_signal, short_signal


class SuperATR_7Step_Profit_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/FDYGrZVD-SuperATR-7-Step-Profit-Strategy-presentTrading/"""
    params = dict(short_period=3, long_period=7, momentum_period=7,
                  atr_sma_period=7, trend_strength_threshold=1.6185)
    overlap = True

    def next(self):
        # // Calculate True Range
        true_range = self.true_range()

        # // Calculate Momentum Factor
        momentum = self.close - self.close.shift(self.params.momentum_period)
        stdev_close = self.close.stdev(self.params.momentum_period)
        normalized_momentum = momentum.ZeroDivision(
            stdev_close).apply(lambda x: x if x else 0.)
        momentum_factor: IndSeries = normalized_momentum.abs()
        # self.close.mom()

        # // Calculate Short and Long ATRs
        short_atr = true_range.sma(self.params.short_period)
        long_atr = true_range.sma(self.params.long_period)

        # // Calculate Adaptive ATR
        adaptive_atr = (short_atr * momentum_factor +
                        long_atr) / (1. + momentum_factor)

        # // Calculate Trend Strength
        price_change = self.close - \
            self.close.shift(self.params.momentum_period)
        atr_multiple = price_change / adaptive_atr
        trend_strength = atr_multiple.sma(self.params.momentum_period)

        # // Calculate Moving Averages
        short_ma = self.close.sma(self.params.short_period)
        long_ma = self.close.sma(self.params.long_period)

        # // Determine Trend Signal
        trend_signal: IndSeries = ((short_ma > long_ma) & (trend_strength > self.params.trend_strength_threshold)).apply(lambda x: x and 1. or 0.) +\
            ((short_ma < long_ma) & (trend_strength < -
             self.params.trend_strength_threshold)).apply(lambda x: x and -1. or 0.)

        # // Calculate Adaptive ATR SMA for Confirmation
        adaptive_atr_sma = adaptive_atr.sma(self.params.atr_sma_period)

        # // Determine if Trend is Confirmed with Price Action

        trend_confirmed = adaptive_atr > adaptive_atr_sma
        trend_confirmed1 = trend_signal == 1.
        trend_confirmed1 &= self.close > short_ma

        trend_confirmed2 = trend_signal == -1.
        trend_confirmed2 &= self.close < short_ma

        trend_confirmed &= trend_confirmed1 | trend_confirmed2

        # // Entry Conditions
        long_signal = trend_confirmed & trend_signal == 1
        short_signal = trend_confirmed & trend_signal == -1

        # // Exit Conditions
        # long_exit = strategy.position_size > 0 and short_entry
        # short_exit = strategy.position_size < 0 and long_entry
        return long_ma, short_ma, long_signal, short_signal


class Dynamic_RSI_Mean_Reversion_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/K9FLcueo-Dynamic-RSI-Mean-Reversion-Strategy/"""
    params = dict(rsiPeriod=14, atrPeriod=50, baseOBLevel=70, baseOSLevel=30, atrScaling=.2, dynamicStopDistance=1.,
                  meanLevel=50, ma1Length=21, ma1Type="sma", ma2Length=50, ma2Type="sma", trendFiltering=True)
    overlap = dict(rsiSignal=False, dynamicOB=False, dynamicOS=False)

    def next(self):
        ma1 = self.close.ma(self.params.ma1Type, self.params.ma1Length)
        ma2 = self.close.ma(self.params.ma2Type, self.params.ma2Length)

        # // Calculate RSI
        rsiSignal = self.close.rsi(self.params.rsiPeriod)

        # // Calculate ATR
        atrValue = self.atr(self.params.atrPeriod)

        # // Adjust OB/OS Levels Based on ATR
        maxATR = atrValue.tqfunc.hhv(self.params.atrPeriod)
        minATR = atrValue.tqfunc.llv(self.params.atrPeriod)
        normalizedATR = (atrValue - minATR).ZeroDivision(maxATR - minATR)

        dynamicOB: IndSeries = self.params.baseOBLevel + \
            (normalizedATR * (100. - self.params.baseOBLevel) * self.params.atrScaling)
        dynamicOS: IndSeries = self.params.baseOSLevel - \
            (normalizedATR * self.params.baseOSLevel * self.params.atrScaling)

        # // Ensure OB/OS levels are within bounds
        dynamicOB = dynamicOB.apply(lambda x: min(x, 100.))
        dynamicOS = dynamicOS.apply(lambda x: max(x, .0))

        # // Entry Conditions
        long_signal = (rsiSignal.cross_down(dynamicOS) & (
            ma1 > ma2)) if self.params.trendFiltering else rsiSignal.cross_down(dynamicOS)
        short_signal = (rsiSignal.cross_up(dynamicOB) & (
            ma1 < ma2)) if self.params.trendFiltering else rsiSignal.cross_up(dynamicOB)

        # // Exit Conditions
        # exitlong_signal = rsiSignal.cross_up(self.params.meanLevel)
        # exitshort_signal = rsiSignal.cross_down(self.params.meanLevel)
        # , exitlong_signal, exitshort_signal
        return rsiSignal, dynamicOB, dynamicOS, long_signal, short_signal


class VWAP_Stdev_Bands_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/zr743ECk-VWAP-Stdev-Bands-Strategy-Long-Only/"""
    params = dict(devUp1=1.28, devDn1=1.28,
                  profitTarget=2, gapMinutes=15, length=60)
    overlap = True

    def next(self):
        vwapsum = (self.hl2()*self.volume).rolling(self.params.length).sum()
        volumesum = self.volume.rolling(self.params.length).sum()
        v2sum = (self.hl2()*self.hl2() *
                 self.volume).rolling(self.params.length).sum()
        myvwap = vwapsum / volumesum
        dev = (v2sum / volumesum - myvwap *
               myvwap).apply(lambda x: math.sqrt(max(x, 0.)))

        # // Calculate Upper and Lower Bands
        lowerBand1 = myvwap - self.params.devDn1 * dev
        upperBand1 = myvwap + self.params.devUp1 * dev

        # // Trading Logic (Long Only)
        # // Price crosses below the lower band
        long_signal = self.close.cross_down(lowerBand1)
        short_signal = self.close.cross_up(upperBand1)
        return myvwap, lowerBand1, upperBand1, long_signal, short_signal


class WilliamsR_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/WgrZZgCZ/"""
    params = dict(lookbackPeriod=2)
    overlap = dict(williamsR=False)

    def next(self):
        highestHigh = self.high.tqfunc.hhv(self.params.lookbackPeriod)
        lowestLow = self.low.tqfunc.llv(self.params.lookbackPeriod)
        williamsR = -100. * \
            (highestHigh - self.close).ZeroDivision(highestHigh - lowestLow)

        # // Conditions for entry and exit
        long_signal = williamsR < -90.
        exitlongCondition1 = self.close > self.high.shift()
        exitlongCondition2 = williamsR > -30.
        exitlong_signal = exitlongCondition1 | exitlongCondition2

        short_signal = williamsR > -10.
        exitshortCondition1 = self.close < self.low.shift()
        exitshortCondition2 = williamsR < -70.
        exitshort_signal = exitshortCondition1 | exitshortCondition2
        return williamsR, long_signal, exitlong_signal, short_signal, exitshort_signal


class Reflected_ema_Difference(BtIndicator):
    """https://cn.tradingview.com/script/abYsiELw/"""
    params = dict(wma_length=8, slow=44, fast=36,
                  periodo_suavizado_delta=2, factor_correccion_delta=0.04)
    overlap = dict(media_delta=True, valor_reflejado_delta=True, ema_suavizada_delta=True,
                   limite_tendencia_delta=True, direccion_delta_tendencia=False)

    def next(self):
        media_delta = (2. * self.close.wma(self.params.wma_length / 2) -
                       self.close.wma(self.params.wma_length)).wma(math.floor(math.sqrt(8)))

        # // Calcular EMAs
        # // Calculate EMAs
        ema_corta_delta = self.close.hma(self.params.fast)
        ema_larga_delta = self.close.hma(self.params.slow)

        # // Calcular la diferencia entre las EMAs
        # // Calculate the difference between EMAs
        diferencia_delta_ema: IndSeries = (
            ema_corta_delta - ema_larga_delta).abs()

        # // Calcular el valor reflejado basado en la posición de la EMA corta
        # // Compute the reflected value based on the position of the short EMA
        valor_reflejado_delta: IndSeries = ema_corta_delta + \
            diferencia_delta_ema.Where(
                ema_corta_delta < ema_larga_delta, -diferencia_delta_ema)

        # // Suavizar el valor reflejado
        # // Smooth the reflected value
        # periodo_suavizado_delta = 2, title="Periodo extendido")
        ema_suavizada_delta = valor_reflejado_delta.hma(
            self.params.periodo_suavizado_delta)

        # // Parámetros ajustables para la reversión de tendencia
        # // Adjustable parameters for trend reversal
        # factor_correccion_delta = title='Porcentaje de cambio', minval=0, maxval=100, step=0.1, defval=0.04)
        # tasa_correccion_delta = self.params.factor_correccion_delta * 0.01
        # // Lógica de reversión de tendencia con la EMA suavizada reflejada
        # // Trend reversal logic with the smoothed reflected EMA
        short_signal = media_delta.cross_down(valor_reflejado_delta)
        long_signal = media_delta.cross_up(valor_reflejado_delta)
        short_index: pd.Series = pd.Series(short_signal.index.where(
            short_signal).fillna(0).astype(int).values)
        long_index: pd.Series = pd.Series(long_signal.index.where(
            long_signal).fillna(0).astype(int).values)
        index = long_index+short_index
        index_sum = index.rolling(5).sum()
        long_signal &= media_delta > media_delta.shift(5)
        long_signal &= index_sum != long_index
        short_index &= media_delta < media_delta.shift(5)
        short_index &= index_sum != short_index

        return media_delta, valor_reflejado_delta, ema_suavizada_delta, long_signal, short_signal


class Reversal_Trading_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/jvfW74oo/"""
    params = dict(daysToHold=7, maLength=20)
    overlap = True

    def next(self):
        # // Calculate the 20-day moving average
        ma20 = self.close.sma(self.params.maLength)

        # // Define the conditions for the 123 reversal pattern (bullish reversal)
        # // Condition 1: Today's low is lower than yesterday's low
        condition1 = self.low < self.low.shift()

        # // Condition 2: Yesterday's low is lower than the low three days ago
        condition2 = self.low.shift() < self.low.shift(3)

        # // Condition 3: The low two days ago is lower than the low four days ago
        condition3 = self.low.shift(2) < self.low.shift(4)

        # // Condition 4: The high two days ago is lower than the high three days ago
        condition4 = self.high.shift(2) < self.high.shift(3)

        # // Entry condition: All conditions must be true
        long_signal = condition1 & condition2 & condition3 & condition4
        condition1_ = self.high > self.high.shift()
        condition2_ = self.high.shift() > self.high.shift(3)
        condition3_ = self.high.shift(2) > self.high.shift(4)
        condition4_ = self.low.shift(2) > self.low.shift(3)
        short_signal = condition1_ & condition2_ & condition3_ & condition4_
        return ma20, long_signal, short_signal


class Multi_Factor_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/uqYTGxlB-Multi-Factor-Strategy/"""
    params = dict(fastLength=12, slowLength=26,
                  signalLength=9, rsiLength=14, atrLength=14)

    def next(self):
        # // Calculate indicators
        macd = self.close.macd(self.params.fastLength,
                               self.params.slowLength, self.params.signalLength)
        rsi = self.close.rsi(self.params.rsiLength)
        # atr =self.atr(self.params.atrLength)

        sma50 = self.close.sma(50)
        sma200 = self.close.sma(200)

        # // Strategy logic
        long_signal = (macd.macdh > 0.) & (
            rsi < 70.) & (self.close > sma50) & (sma50 > sma200)
        short_signal = (macd.macdh < 0.) & (
            rsi > 30.) & (self.close < sma50) & (sma50 < sma200)

        return sma50, sma200, long_signal, short_signal


class CE_ZLSMA_5MIN_CANDLECHART(BtIndicator):
    """https://cn.tradingview.com/script/PGzjdK8G-CE-ZLSMA-5MIN-CANDLECHART/"""
    params = dict(length=20, mult=2.)
    overlap = dict(stop=True, dir=False)

    def next(self):
        atr = self.params.mult*self.true_range().rma(self.params.length)
        size = self.close.size
        ha = self.ha()

        longStop_ = (ha.close.tqfunc.hhv(self.params.length) - atr).values
        shortStop_ = (ha.close.tqfunc.llv(self.params.length) + atr).values
        lennan = get_lennan(longStop_, shortStop_)
        longStop = np.zeros(size)
        shortStop = np.zeros(size)
        stop = np.zeros(size)
        dir = np.zeros(size)
        dir[lennan] = 1
        longStop[lennan] = longStop_[lennan]
        shortStop[lennan] = shortStop_[lennan]
        haclose = ha.close.values
        for i in range(lennan+1, size):
            longStopPrev = longStop[i-1]
            longStop[i] = (
                haclose[i-1] > longStopPrev) and max(longStop_[i], longStopPrev) or longStop_[i]

            shortStopPrev = shortStop[i-1]
            shortStop[i] = (
                haclose[i-1] < shortStopPrev) and min(shortStop_[i], shortStopPrev) or shortStop_[i]

            dir[i] = (haclose[i] > shortStopPrev) and 1 or (
                (haclose[i] < longStopPrev) and -1 or dir[i-1])
            if dir[i] > 0:
                stop[i] = longStop[i]
            else:
                stop[i] = shortStop[i]

        # buySignal = dir == 1 and dir[1] == -1 and haClose > zlsma and haClose > haOpen
        return stop, dir


class Scalp_Slayer(BtIndicator):
    """https://cn.tradingview.com/script/ZFlfq3FH-Scalp-Slayer-i/"""
    params = dict(filterNumber=1.5, emaTrendPeriod=50, lookbackPeriod=20,)
    overlap = dict(ema=True)

    def next(self):
        # // Calculations
        tr = self.high - self.low
        ema = self.params.filterNumber * tr.ema(self.params.emaTrendPeriod)
        # // Calculate the EMA for the trend filter
        trendEma = self.close.ema(self.params.emaTrendPeriod)

        # // Highest and lowest high/low within lookback period for swing logic
        # swingHigh = self.high.tqfunc.hhv(self.params.lookbackPeriod)
        # swingLow = self.low.tqfunc.llv(self.params.lookbackPeriod)

        # // Variables to track the entry prices and SL/TP levels
        cond = (self.close-self.open).shift(2).bfill().abs() > (self.close -
                                                                              self.open).shift().bfill().abs()
        cond &= (self.close-self.open).abs() > (self.close -
                                                self.open).shift().bfill().abs()
        cond &= tr > ema
        # // Buy and Sell Conditions with Trend Filter
        long_signal = self.close > trendEma
        long_signal &= self.close.shift(2) > self.open.shift(2)
        long_signal &= self.close.shift() > self.open.shift()
        long_signal &= self.close > self.open
        long_signal &= self.close > self.close.shift()
        long_signal &= self.close.shift() > self.close.shift(2)
        long_signal &= cond

        short_signal = self.close < trendEma
        short_signal &= self.close.shift(2) < self.open.shift(2)
        short_signal &= self.close.shift() < self.open.shift()
        short_signal &= self.close < self.open
        short_signal &= self.close < self.close.shift()
        short_signal &= self.close.shift() < self.close.shift(2)
        short_signal &= cond

        return ema, long_signal, short_signal


class Price_and_Volume_Breakout_Buy_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/jc2hs2qK-Price-and-Volume-Breakout-Buy-Strategy-TradeDots/"""
    params = dict(input_price_breakout_period=60,
                  input_volume_breakout_period=60, input_trendline_legnth=200,)

    def next(self):
        price_highest = self.high.tqfunc.hhv(
            self.params.input_price_breakout_period)
        price_lowest = self.low.tqfunc.llv(
            self.params.input_price_breakout_period)
        volume_highest = self.volume.tqfunc.hhv(
            self.params.input_volume_breakout_period)
        sma = self.close.sma(self.params.input_trendline_legnth)
        long_signal = self.close > price_highest.shift()
        long_signal &= self.volume > volume_highest.shift()
        long_signal &= self.close > sma

        exitlong_signal = self.close < sma
        for i in range(1, 5):
            exitlong_signal &= self.close.shift(i) < sma

        short_signal = self.close < price_lowest.shift()
        short_signal &= self.volume > volume_highest.shift()
        short_signal &= self.close < sma

        exitshort_signal = self.close > sma
        for i in range(1, 5):
            exitshort_signal &= self.close.shift(i) > sma

        return sma, long_signal, exitlong_signal, short_signal, exitshort_signal


class Kaufman_Adaptive_Moving_Average_KAMA_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/rm6K3yV6-Kaufman-Adaptive-Moving-Average-KAMA-Strategy-TradeDots/"""
    params = dict(lookback_period=10, fastma_length=5,
                  slowma_length=50, kama_rising_perid=10, kama_falling_perid=10)

    def rising(self, source: IndSeries, period):
        result = source > source.shift()
        if period <= 1:
            return result
        for i in range(1, period+1):
            result &= source.shift(i) > source.shift(i+1)
        return result

    def falling(self, source: IndSeries, period):
        result = source < source.shift()
        if period <= 1:
            return result
        for i in range(1, period+1):
            result &= source.shift(i) < source.shift(i+1)
        return result

    def next(self):
        # math.abs(source - source[lookback_period])
        price_change: IndSeries = (
            self.close-self.close.shift(self.params.lookback_period)).bfill().abs()
        # math.sum(math.abs(source - source[1]), lookback_period)
        sum_price_change = (self.close-self.close.shift()
                            ).bfill().abs().rolling(self.params.lookback_period).sum()
        fastest = 2./(self.params.fastma_length + 1)
        slowest = 2./(self.params.slowma_length + 1)
        ER = price_change.ZeroDivision(sum_price_change)
        SC = (ER * (fastest-slowest) + slowest).apply(lambda x: x*x).values
        size = self.close.size
        # alpha = SC
        sum = np.zeros(size, dtype=np.float32)
        close = self.close.values
        length = get_lennan(SC)
        sum[length] = close[length]
        for i in range(length+1, size):
            sum[i] = sum[i-1] + SC[i] * (close[i] - sum[i-1])
        sum = pd.Series(sum)
        long_signal = self.rising(sum, self.params.kama_rising_perid)
        short_signal = self.falling(sum, self.params.kama_falling_perid)
        return sum, long_signal, short_signal


class Triple_EMA_QQE_Trend_Following_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/M4y5HT3h-Triple-EMA-QQE-Trend-Following-Strategy-TradeDots/"""
    params = dict(rsi_period=14, rsi_smoothing_period=5,
                  factor=4.238, tema1_length=20, tema2_length=40)
    overlap = False

    def next(self):
        tema1 = self.close.tema(self.params.tema1_length)
        tema2 = self.close.tema(self.params.tema2_length)

        # // calculating qqe
        rsi = self.close.rsi(self.params.rsi_period)
        rsiMa = rsi.ema(self.params.rsi_smoothing_period)
        atrRsi: IndSeries = (
            rsiMa.shift() - rsiMa).bfill().abs()
        rsiMa_values = rsiMa.values

        final_smooth = self.params.rsi_period * 2 - 1
        MaAtrRsi = atrRsi.ema(final_smooth)
        # MaAtrRsi_values = MaAtrRsi.values
        # dar = MaAtrRsi.ema(final_smooth) * self.params.factor
        up = (rsiMa + MaAtrRsi * self.params.factor).values
        dn = (rsiMa - MaAtrRsi * self.params.factor).values
        size = self.close.size
        ts = np.zeros(size)
        length = get_lennan(rsiMa_values)
        ts[length] = rsiMa_values[length]
        for i in range(length+1, size):
            ts[i] = (rsiMa_values[i-1] < 0. < rsiMa_values[i]) and dn[i] or\
                ((rsiMa_values[i-1] > 0. > rsiMa_values[i]) and up[i] or
                    (rsiMa_values[i] > ts[i]) and max(ts[i], dn[i]) or min(up[i], ts[i]))
        # ts := nz(crossover ? rsiMa - MaAtrRsi * factor
        # : crossunder ? rsiMa + MaAtrRsi * factor
        # : rsiMa > ts ? math.max(rsiMa - MaAtrRsi * factor, ts)
        # : math.min(rsiMa + MaAtrRsi * factor, ts), rsiMa)

        # // entry and exit conditions
        long_signal = self.close > tema1
        long_signal &= tema1 > tema2
        long_signal &= tema2 > tema2.shift()
        long_signal &= rsi.cross_down(ts)
        short_signal = self.close < tema1
        short_signal &= tema1 < tema2
        short_signal &= tema2 < tema2.shift()
        short_signal &= rsi.cross_up(ts)

        return rsi, rsiMa, ts, long_signal, short_signal


class RSI_and_ATR_Trend_Reversal_SL_TP(BtIndicator):
    """https://cn.tradingview.com/script/hZLgJ29l-RSI-and-ATR-Trend-Reversal-SL-TP/"""
    params = dict(rsi_length=8, rsi_mult=1.5, lookback=1, sltp=10,)
    overlap = dict(thresh=True, dir=False)

    def next(self):
        sl = (100. - self.params.sltp) / 100.
        tp = (100. + self.params.sltp) / 100.
        size = self.close.size
        dir = np.zeros(size)
        upper = np.zeros(size)
        lower = np.zeros(size)
        thresh = np.zeros(size)
        rsilower: IndSeries = self.close.rsi(self.params.rsi_length)
        rsiupper = (rsilower - 100.).bfill().abs()
        rsilower_, rsiupper_ = rsilower.values, rsiupper.values
        # .ZeroDivision(self.close).values
        atr = self.atr(self.params.rsi_length).values
        close = self.close.values
        length = get_lennan(rsilower, rsiupper)
        lower[length] = close[length]*(1.-(atr[length] + (
            (1. / (rsilower_[length]) * self.params.rsi_mult))))
        upper[length] = close[length]*(1.+(atr[length] + (
            (1. / (rsiupper_[length]) * self.params.rsi_mult))))
        dir[length] = lower[length] > close[length] and -1 or 1

        for i in range(length+1, size):
            # bar += 1
            # src = close[i-bar:i+1]
            lower[i] = max(
                close[i]*(1.-atr[i] / rsilower_[i] * self.params.rsi_mult), lower[i-1])
            upper[i] = min(
                close[i]*(1.+atr[i] / rsiupper_[i] * self.params.rsi_mult), upper[i-1])
            if close[i] > upper[i]:
                dir[i] = 1
            elif close[i] < lower[i]:
                dir[i] = -1
            if dir[i] == 1:
                thresh[i] = lower[i]
            else:
                thresh[i] = upper[i]
            # if dir[i] != dir[i-1]:
            #     bar = 0
        thresh, dir = pd.Series(thresh), pd.Series(dir)
        long_signal = dir.shift() == -1
        long_signal &= dir == 1
        short_signal = dir.shift() == 1
        short_signal &= dir == -1
        return thresh, dir, long_signal, short_signal


class TTP_Intelligent_Accumulator(BtIndicator):
    """https://cn.tradingview.com/script/lic0Tapd-TTP-Intelligent-Accumulator/"""
    params = dict(rsi_length=7, ma_length=14, mult=1.6185)
    overlap = False

    def next(self):
        rsi = self.close.rsi(self.params.rsi_length)
        rsima = rsi.sma(self.params.ma_length)
        bbstd = rsi.stdev(self.params.ma_length)*self.params.mult
        up = rsima+bbstd
        dn = rsima-bbstd

        long_signal = rsi.cross_up(dn)
        short_signal = rsi.cross_down(up)
        return up, dn, rsi, long_signal, short_signal


class Mars_MA_BB_SuperTrend(BtIndicator):
    """https://cn.tradingview.com/script/Y68meyX1-2mars-ma-bb-supertrend/"""
    params = dict(length=14, superTrendFactor=4, superTrendPeriod=20, maRatio=1.08, maMultiplier=89, bbLength=30,
                  bbMultiplier=3, SLAtrPeriod=12, SLAtrMultiplierLong=6, SLAtrMultiplierShort=4.3, character_count=14,
                  maBasisType="sma", maSignalType="sma", bbType="wma", barsConfirm=2)
    # lines = ("maBasis", "maSignal", "supertrend")
    overlap = True

    def ssma(self):
        a1 = math.exp(-1.414*np.pi / self.params.length)
        b1 = 2*a1*math.cos(1.414*np.pi / self.params.length)
        c2 = b1
        c3 = (-a1)*a1
        c1 = 1 - c2 - c3
        size = self.close.size
        src = self.close.values
        sum = np.zeros(size)
        for i in range(2, size):
            sum[i] = c1*(src + src[i-1]) / 2 + c2*sum[i-1] + c3*sum[i-2]
        return sum

    def smma(self):
        length = self.params.length

        ma = self.close.sma(length)
        sma = ma.values
        size = self.close.size
        src = self.close.values
        nanlength = get_lennan(sma)
        smma = np.zeros(size)
        smma[:nanlength] = sma[:nanlength]
        # sum = np.zeros(size)
        for i in range(nanlength+1, size):
            smma[i] = (smma[i-1] * (length - 1) + src[i]) / length
        return smma

    def fj_stdev(self, length, mult, middleType):
        basis = self.close.ma(middleType, length)
        dev = mult * self.close.stdev(length)
        dev2 = (mult * self.close.stdev(length)) * 0.618
        dev3 = (mult * self.close.stdev(length)) * 1.618
        dev4 = (mult * self.close.stdev(length)) * 2.618
        return basis, basis + dev2, basis + dev, basis + dev3, basis + dev4, basis - dev2, basis - dev, basis - dev3, basis - dev4

    def next(self):

        # priceMedianHighLow = (self.high + self.low) / 2.
        # priceMedianOpenClose = (self.open + self.close) / 2.

        supertrend = self.supertrend(
            self.params.superTrendPeriod, self.params.superTrendFactor)
        supertrend = supertrend.trend.rma(self.params.superTrendFactor)
        maBasisLengh = int(self.params.maRatio * self.params.maMultiplier)
        maBasis = self.close.ma(self.params.maBasisType, maBasisLengh)
        maSignal = self.close.ma(
            self.params.maSignalType, self.params.maMultiplier)
        long_signal = maSignal.cross_up(maBasis)
        short_signal = maSignal.cross_down(maBasis)
        return maBasis, maSignal, supertrend, long_signal, short_signal

        # bbMiddle, bbUpper, bbUpper2, bbUpper3, bbUpper4, bbLower, bbLower2, bbLower3, bbLower4=\
        #     self.fj_stdev(self.params.bbLength, self.params.bbMultiplier, self.params.bbType)


# class Contrarian_DC_Strategy(BtIndicator):
#     """https://cn.tradingview.com/script/DApq7rJu-Contrarian-DC-Strategy-w-Entry-SL-Pause-and-TrailingStop/"""
#     params = dict(length=20, riskRewardRatio=1.7,)

#     def next(self):
#         upper = self.high.tqfunc.hhv(self.params.length)
#         lower = self.low.tqfunc.llv(self.params.length)
#         # // Tracking Stop Loss Hits and Pause
#         size = self.close
#         longSLHitBar = 0
#         shortSLHitBar = 0
#         # // 1 for long, -1 for short, 0 for none
#         lastTradeDirection = 0
#         long_signal = np.zeros(size)
#         short_signal = np.zeros(size)
#         length = get_lennan(upper, lower)
#         low, high, close = self.low.values, self.high.values, self.close.values
#         position_avg_price = 0.
#         pauseCandles = 3
#         for i in range(length+1, size):
#             if dir > 0:
#                 if
#             # // Update SL Hit Bars
#             if long_signal[i-1] > 0 and long_signal[i] == 0:
#                 if close[i-1] < position_avg_price:
#                     longSLHitBar = i
#                     lastTradeDirection = 1

#             if short_signal[i-1] > 0 and short_signal[i] == 0:
#                 if close[i-1] > position_avg_price:
#                     shortSLHitBar = i
#                     lastTradeDirection = -1

#             # // Entry Conditions - Trigger on touch
#             long_signal[i] = ((low[i] <= lower[i]) and (
#                 i - longSLHitBar > pauseCandles or lastTradeDirection != 1)) and 1 or 0
#             short_signal[i] = ((high[i] >= upper[i]) and (
#                 i - shortSLHitBar > pauseCandles or lastTradeDirection != -1)) and 1 or 0
#             if long_signal[i-1] == 0 and long_signal[i] > 0:
#                 position_avg_price = close[i]
#                 dir = 1
#             elif short_signal[i-1] == 0 and short_signal[i] > 0:
#                 position_avg_price = close[i]
#                 dir = -1

#         return upper, lower, long_signal, short_signal


class FlexiSuperTrend_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/y9noW915-FlexiSuperTrend-Strategy-presentTrading/"""
    ...


class Powertrend_Volume_Range_Filter_Strategy_(BtIndicator):
    """https://cn.tradingview.com/script/45FlB2qH-Powertrend-Volume-Range-Filter-Strategy-wbburgin/"""
    params = dict(lengthadx=14, lengthhl=14, lengthvwma=233, mult=3.236)
    overlap = True

    def next(self):
        alerts = self.btind.alerts(self.params.lengthvwma, self.params.mult)
        filt, hband, lowband, dir = alerts.to_lines()
        ma = self.vwma(self.params.lengthvwma)

        lowband_trendfollow = lowband.tqfunc.llv(
            self.params.lengthhl)  # ta.lowest(lowband,lengthhl)
        highband_trendfollow = hband.tqfunc.hhv(
            self.params.lengthhl)  # ta.highest(hband,lengthhl)
        # in_general_uptrend = self.close.cross_up(highband_trendfollow.shift()).tqfunc.barlast(
        # ) < self.close.cross_down(lowband_trendfollow.shift()).tqfunc.barlast()
        short_signal = dir < 0.
        short_signal &= self.close < ma
        short_signal &= ma < ma.shift()
        # short_signal &= self.close.cross_up(highband_trendfollow.shift()).tqfunc.barlast(
        # ) > self.close.cross_down(lowband_trendfollow.shift()).tqfunc.barlast()
        short_signal &= self.close.cross_down(filt)
        exitshort_signal = lowband == lowband.shift()
        exitshort_signal &= lowband.shift() != lowband.shift(2)

        long_signal = dir > 0.
        long_signal &= self.close > ma
        long_signal &= ma > ma.shift()
        # long_signal &= self.close.cross_up(highband_trendfollow.shift()).tqfunc.barlast(
        # ) < self.close.cross_down(lowband_trendfollow.shift()).tqfunc.barlast()
        long_signal &= self.close.cross_up(filt)
        exitlong_signal = hband == hband.shift()
        exitlong_signal &= hband.shift() != hband.shift(2)
        #
        return ma, filt, hband, lowband, long_signal, short_signal, exitlong_signal, exitshort_signal


class zonestrength(BtIndicator):
    """https://cn.tradingview.com/script/AAOdDl5n-wbburgin-utils/"""
    params = dict(amplitude=14, wavelength=14)
    overlap = True

    def next(self):
        ohlc_avg = self.ohlc4()
        ocp_avg = (self.open+self.close)/2.
        g = (ohlc_avg + ocp_avg)/2.
        size = self.close.size
        h = np.zeros(size)
        g, ohlc_avg, ocp_avg, ocp_avg = g.values, ohlc_avg.values, ocp_avg.values, ocp_avg.values
        low, high = self.low.values, self.high.values
        for i in range(size):

            h[i] = g[i] * (1. + (ohlc_avg[i] > ocp_avg[i] and (high[i] > ohlc_avg[i] and high[i] - ohlc_avg[i] + high[i] - ocp_avg[i] or high[i] - ocp_avg[i]) or (low[i] <
                                                                                                                                                                   ohlc_avg[i] and low[i] - ocp_avg[i] - ohlc_avg[i] + low[i] or low[i] - ocp_avg[i])) * -1 / g[i])
        h = IndSeries(h)
        # ta.highest(h, amplitude)
        amp_highest = h.tqfunc.hhv(self.params.amplitude)
        # ta.lowest(h, amplitude)
        amp_lowest = h.tqfunc.llv(self.params.amplitude)
        high_deviation = amp_highest - self.high
        low_deviation = self.low - amp_lowest

        # m = amp_lowest.Where(high_deviation > low_deviation, amp_highest)
        # n = amp_lowest.Where(high_deviation < low_deviation, amp_highest)
        # o = (amp_highest + amp_lowest)/2.

        # q4 = (o, m)/2.
        # q2 = (o, n)/2.

        # ta.ema(amp_lowest, wavelength)
        s = amp_lowest.ema(self.params.wavelength)
        # ta.ema(amp_highest, wavelength)
        t = amp_highest.ema(self.params.wavelength)
        zonestrength = (ohlc_avg - s) / (t - s) - .5
        return h, amp_highest, amp_lowest


class fusion(BtIndicator):
    """https://cn.tradingview.com/script/AAOdDl5n-wbburgin-utils/"""
    params = dict(overallLength=3, rsiLength=14, mfiLength=14, macdLength=12,
                  cciLength=12, tsiLength=13, rviLength=10, atrLength=14, adxLength=14, len=14)
    overlap = False

    def next(self):
        rsi = (self.close.rsi(self.params.rsiLength)-50.)/20.
        mfi = (self.mfi(self.params.mfiLength)-50.)/20.
        macdRaw, *_ = self.close.macd(self.params.macdLength*self.params.overallLength,
                                      self.params.macdLength*2*self.params.overallLength, 9).to_lines()
        mHighest = macdRaw.tqfunc.hhv(
            self.params.macdLength*self.params.overallLength)
        mLowest = macdRaw.tqfunc.llv(
            self.params.macdLength*self.params.overallLength)
        macd = macdRaw.Where(macdRaw > 0., macdRaw.ZeroDivision(mHighest))
        macd = macd.Where(macdRaw < 0, macdRaw.ZeroDivision(
            mLowest.apply(lambda x: abs(x))))

        cci = self.cci(self.params.cciLength)/100.
        tsiRaw = self.close.tsi(self.params.tsiLength*self.params.overallLength,
                                self.params.tsiLength*2*self.params.overallLength).tsir
        tHighest = tsiRaw.tqfunc.hhv(
            self.params.tsiLength*self.params.overallLength)
        tLowest = tsiRaw.tqfunc.llv(
            self.params.tsiLength*self.params.overallLength)
        tsi = tsiRaw.Where(tsiRaw > 0., tsiRaw.ZeroDivision(tHighest))
        tsi = tsi.where(tsiRaw < 0., tsiRaw.ZeroDivision(
            tLowest.apply(lambda x: abs(x))))

        src = self.close
        stddev = src.stdev(self.params.rviLength*self.params.overallLength)
        upper = stddev.Where(src.diff() > 0., 0.).ema(self.params.len)
        lower = stddev.Where(src.diff() <= 0., 0.).ema(self.params.len)
        rvi = ((upper / (upper + lower) * 100.)-50.)/20.
        super_dir = (rsi+mfi+macd+cci+tsi+rvi)/6.

       # // Nondirectional Oscillators
        atrRaw = self.atr(self.params.atrLength)
        atrStdev = atrRaw.stdev(self.params.atrLength)
        atrEMA = atrRaw.ema(self.params.atrLength)
        atr = ((atrRaw.ZeroDivision(atrEMA)-1.)*(1+atrStdev))-1.

        adxRaw, *_ = self.adx(17, self.params.adxLength).to_lines()
        adxStdev = adxRaw.stdev(self.params.adxLength)
        adxEMA = adxRaw.ema(self.params.adxLength)
        adx = (adxRaw.ZeroDivision(adxEMA)-1)*(1+adxStdev)

        super_nondirRough = (atr+adx)/2.
        highestNondir = super_nondirRough.tqfunc.hhv(200)
        lowestNondir = super_nondirRough.tqfunc.llv(200)

        super_nondir = super_nondirRough.Where(
            super_nondirRough > 0., super_nondirRough.ZeroDivision(highestNondir))
        super_nondir = super_nondir.Where(super_nondirRough < 0., super_nondirRough.ZeroDivision(
            lowestNondir.apply(lambda x: abs(x))))

        return super_nondir


class Bollinger_Bands_Breakout_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/sTFw2fOj/"""
    params = dict(bbLengthInput=15, bbDevInput=2., trendFilterPeriodInput=223,
                  trendFilterType="ema", volatilityFilterStDevLength=15, volatilityStDevMaLength=15)
    overlap = True

    def next(self):
        bbLower, bbMiddle, bbUpper, *_ = self.close.bbands(
            self.params.bbLengthInput, self.params.bbDevInput).to_lines()
        tradeConditionMa = self.close.ma(
            self.params.trendFilterType, self.params.trendFilterPeriodInput)
        trendConditionLong = self.close > tradeConditionMa
        trendConditionShort = self.close < tradeConditionMa
        stdDevClose = self.close.stdev(self.params.volatilityFilterStDevLength)
        volatilityCondition = stdDevClose > stdDevClose.sma(
            self.params.volatilityStDevMaLength)

        bbLowerCrossUnder = self.close.cross_down(bbLower)
        bbUpperCrossOver = self.close.cross_up(bbUpper)

        long_signal = bbUpperCrossOver & trendConditionLong & volatilityCondition
        short_signal = bbLowerCrossUnder & trendConditionShort & volatilityCondition

        exitlong_signal = self.close.cross_up(bbLower)
        exitshort_signal = self.close.cross_down(bbUpper)

        return bbLower, bbUpper, long_signal, exitlong_signal, short_signal, exitshort_signal


class MTF_Diagonally_Layered_RSI_1minute_Bitcoin_Bot(BtIndicator):
    """https://cn.tradingview.com/script/z1fsrtce-MTF-Diagonally-Layered-RSI-1-minute-Bitcoin-Bot-wbburgin/"""
    params = dict(length=7, ob=80, os=20, tf2=3, tf3=5)
    overlap = False

    def manualRSI(self, time_mult, length):
        length = length*time_mult
        u = (self.close-self.close.shift(time_mult)).apply(lambda x: max(x, 0.))
        d = (self.close.shift(time_mult)-self.close).apply(lambda x: max(x, 0.))
        u = u.ewm(alpha=1.0 / length, min_periods=length).mean()
        d = d.ewm(alpha=1.0 / length, min_periods=length).mean()
        rs = u/d
        res = 100.-100./(1.+rs)
        return res

    def next(self):
        data = self.resample(60)
        data.ismain = True
        rsi = self.close.rsi(self.params.length)
        rsi2 = data.close.rsi(self.params.length)()
        rsi3 = self.manualRSI(self.params.tf2, self.params.length)
        rsi4 = self.manualRSI(self.params.tf3, self.params.length)
        return rsi, rsi2, rsi3, rsi4


class Strategy_for_UT_Bot_Alerts_indicator(BtIndicator):
    """https://cn.tradingview.com/script/R9nsvSZR/"""
    params = dict(length=200, a=3, c=1, h=False)
    overlap = True

    def next(self):
        xATR = self.atr(self.params.c)
        nLoss = self.params.a * xATR

        close = self.ha().ha_close if self.params.h else self.close

        size = self.close.size
        xATRTrailingStop = np.zeros(size)
        up = (close+nLoss).values
        dn = (close-nLoss).values
        src = close.values
        length = get_lennan(up, dn)
        pos = np.zeros(size)

        for i in range(length+1, size):
            iff_1 = src[i] > xATRTrailingStop[i-1] and dn[i] or up[i]
            iff_2 = (src[i] < xATRTrailingStop[i-1] and src[i-1] <
                     xATRTrailingStop[i-1]) and min(xATRTrailingStop[i-1], up[i]) or iff_1
            xATRTrailingStop[i] = (src[i] > xATRTrailingStop[i-1] and src[i-1] >
                                   xATRTrailingStop[i-1]) and max(xATRTrailingStop[i-1], dn[i]) or iff_2
            iff_3 = (src[i-1] > xATRTrailingStop[i-1] and src[i]
                     < xATRTrailingStop[i-1]) and -1 or pos[i-1]
            pos[i] = (src[i-1] < xATRTrailingStop[i-1] and src[i]
                      > xATRTrailingStop[i-1]) and 1 or iff_3

        ema_ut = close
        above = ema_ut.cross_up(xATRTrailingStop)
        below = ema_ut.cross_down(xATRTrailingStop)

        long_signal = close > xATRTrailingStop
        long_signal &= above
        short_signal = src < xATRTrailingStop
        short_signal &= below
        exitlong_signal = short_signal
        exitshort_signal = long_signal

        return xATRTrailingStop, long_signal, short_signal, exitlong_signal, exitshort_signal


class Combined_Strategy_Trading_Bot(BtIndicator):
    """https://cn.tradingview.com/script/7wrshs1q-Combined-Strategy-Trading-Bot-RSI-ADX-20SMA/"""
    params = dict(adxlen=7, dilen=7, atrPeriod=10, factor=3)
    overlap = True

    def next(self):
        adx = self.adx(self.params.adxlen, self.params.dilen)
        sig = adx.adxx
        src = self.hl2()
        atr = self.atr(self.params.atrPeriod)
        src = self.close
        upperBand = src + self.params.factor * atr
        lowerBand = src - self.params.factor * atr
        size = self.V
        close = self.close.values
        length = get_lennan(upperBand, lowerBand)
        lowerBand = lowerBand.values
        upperBand = upperBand.values

        superTrend = np.zeros(size)
        direction = np.zeros(size)
        for i in range(length+1, size):
            lowerBand[i] = (lowerBand[i] > lowerBand[i-1] or close[i -
                                                                   1] < lowerBand[i-1]) and lowerBand[i] or lowerBand[i-1]
            upperBand[i] = (upperBand[i] < upperBand[i-1] or close[i -
                                                                   1] > upperBand[i-1]) and upperBand[i] or upperBand[i-1]
            # if rsi1[i] < 66. and rsi2[i] > 80. and rsi3[i] > 49. and sig[i] > 20.:
            #     direction[i] = 1
            if superTrend[i-1] == upperBand[i-1]:
                direction[i] = close[i] > upperBand[i] and -1 or 1
            else:
                direction[i] = close[i] < lowerBand[i] and 1 or -1
            superTrend[i] = direction[i] == - \
                1 and lowerBand[i] or upperBand[i]

        rsi = self.close.rsi(14)
        rsi1 = self.close.rsi(21)
        rsi2 = self.close.rsi(3)
        rsi3 = self.close.rsi(28)
        sma = self.close.sma(20)
        long_signal = direction > 0
        long_signal &= self.close > superTrend
        long_signal &= self.close > sma
        long_signal &= rsi1 < 80.
        long_signal &= rsi2 > 80.
        long_signal &= rsi3 > 50.
        long_signal &= sig > 20.
        exitlong_signal = self.close.cross_down(sma)
        exitlong_signal |= rsi < 30.
        return superTrend, long_signal, exitlong_signal


class Super8_30M_BTC(BtIndicator):
    """https://cn.tradingview.com/script/zFPZofwt/"""
    params = dict(ADX_len=28, ADX_smo=9, th=20.5, Sst=0.1, Sinc=0.04, Smax=0.4, fastLength=24, slowLength=52, signalLength=11, lengthz=14, lengthStdev=14, A=-0.2, B=0.4, volume_f=0.4, sma_Length=55, BB_Length=40,
                  BB_mult=2.2, bbMinWidth01=5., bbMinWidth02=2., tp=1.8, trailOffset=0.3, DClength=55, sl=8., atrPeriodSl=14, multiplierPeriodSl=15, Risk=5, Pyr=3, StepEntry='Incremental', bbBetterPrice=0.7, MACD_options="MACD")
    overlap = False

    def next(self):

        # // ———————————————————— Exponential Moving Average
        # // Calculating the slow and fast Exponential Moving Averages (EMAs) using the close price
        sEMA = self.close.ema(self.params.sEma_Length)
        fEMA = self.close.ema(self.params.fEma_Length)
        # self.lines.sema = sEMA

        # // Setting the conditions for a long or short signal based on the relationship between the fast and slow EMAs
        EMA_longCond = fEMA > sEMA
        EMA_longCond &= sEMA > sEMA.shift()
        EMA_shortCond = fEMA < sEMA
        EMA_shortCond &= sEMA < sEMA.shift()

        # // ———————————————————— ADX
        # // Calculating the Directional Indicator Plus (+DI), Directional Indicator Minus (-DI), and Average Directional Movement Index (ADX)
        ADX, DIPlus, DIMinus = self.adx(
            self.params.ADX_len, self.params.ADX_smo).to_lines()

        # // Setting the conditions for a long or short signal based on the relationship between +DI and -DI and the ADX value
        ADX_longCond = DIPlus > DIMinus
        ADX_longCond &= ADX > self.params.th
        ADX_shortCond = DIPlus < DIMinus
        ADX_shortCond &= ADX > self.params.th
        # self.lines.adx = ADX
        # self.lines.dip = DIPlus
        # self.lines.dim = DIMinus

        # // ———————————————————— SAR
        # // Calculating the Parabolic SAR (SAR) based on the parameters set for step, max, and acceleration factor
        SAR = self.psar(self.params.Sst, self.params.Sinc,
                        self.params.Smax)
        # // Setting the conditions for a long or short signal based on the relationship between the SAR value and the close price
        SAR_longCond = np.isnan(SAR.psars).astype(int)
        SAR_shortCond = np.isnan(SAR.psarl).astype(int)
        self.lines.longcond = SAR_longCond
        self.lines.shortcond = SAR_shortCond

        # // ———————————————————— MACD
        # // Calculating the Moving Average Convergence Divergence (MACD) and its signal line, as well as the MACD-Z value
        # // Define three variables lMACD, sMACD, and hist by calling the ta.macd() function using the 'close', 'fastLength', 'slowLength', and 'signalLength' as parameters
        lMACD, hist, sMACD = self.close.macd(
            self.params.fastLength, self.params.slowLength, self.params.signalLength).to_lines()

        # // ————— MAC-Z calculation
        # // Define a function calc_zvwap(pds) that calculates the z-score of the volume-weighted average price (VWAP) of 'close' for a given period 'pds'

        mean = (self.volume*self.close).rolling(self.params.lengthz).sum() / \
            self.volume.rolling(self.params.lengthz).sum()
        vwapsd = (self.close-mean).apply(lambda x: np.power(x, 2.)
                                         ).sma(self.params.lengthz).apply(lambda x: np.sqrt(x))
        # mean = math.sum(volume * close, pds) / math.sum(volume, pds)
        # vwapsd = math.sqrt(ta.sma(math.pow(close - mean, 2), pds))
        zscore = (self.close - mean) / vwapsd

        # // Define float variables
        # float zscore = na
        # float fastMA = na
        # float slowMA = na
        # float macd = na
        # float macz = na
        # float signal = na
        # float histmacz = na

        # // Calculate the simple moving averages of the 'close' prices using 'fastLength' and 'slowLength' periods, and assign them to 'fastMA' and 'slowMA' respectively
        fastMA = self.close.sma(self.params.fastLength)
        slowMA = self.close.sma(self.params.slowLength)
        # // Assign the 'lMACD' variable to 'macd'
        macd = lMACD
        # // Calculate 'macz' by multiplying the z-score by a constant 'A',
        # // adding the 'macd' value, and dividing by the product of the standard deviation of the 'close' prices over a period 'lengthStdev' and a constant 'B'
        macz = (zscore * self.params.A) + (macd.ZeroDivision(
                                           self.close.stdev(self.params.lengthStdev)) * self.params.B)
        # // Calculate the simple moving average of the 'macz' values over a period 'signalLength' and assign it to 'signal'
        signal = macz.sma(self.params.signalLength)
        # // Calculate the difference between 'macz' and 'signal' and assign it to 'histmacz'
        histmacz = macz - signal

        # // ————— MACD conditions
        # // Define two boolean variables 'MACD_longCond' and 'MACD_shortCond'
        # bool MACD_longCond = na
        # bool MACD_shortCond = na
        # // If 'MACD_options' is equal to 'MACD', check if the 'hist' value is greater than 0 and assign the result to 'MACD_longCond';
        # // otherwise, check if 'histmacz' is greater than 0 and assign the result to 'MACD_longCond'
        MACD_longCond = hist > 0 if self.params.MACD_options == 'MACD' else histmacz > 0
        # // If 'MACD_options' is equal to 'MACD', check if the
        MACD_shortCond = hist < 0 if self.params.MACD_options == 'MACD' else histmacz < 0

        # // ———————————————————— Bollinger Bands
        # // ————— BB calculation
        # // Calculates the middle, upper and lower bands using the Bollinger Bands technical analysis indicator
        BB_lower, BB_middle, BB_upper, BB_width, _ = self.close.bbands(
            self.params.BB_Length, self.params.BB_mult).to_lines()

        # // ————— Bollinger Bands width
        # // Calculates the width of the Bollinger Bands
        # float BB_width = na
        # BB_width := (BB_upper - BB_lower) / BB_middle

        # // ————— Long Bollinger Bands conditions
        # // Defines the conditions for entering a long position using Bollinger Bands
        # // New Longs
        # bool BB_long01 = na
        # BB_long01 = (~ADX_shortCond) & self.low.cross_down(
        #     BB_lower) & EMA_longCond & (BB_width > (self.params.bbMinWidth01 / 100))

        # # // Pyramiding Longs
        # # bool BB_long02 = na
        # BB_long02 = (~ADX_shortCond) & self.low.cross_down(
        #     BB_lower) & EMA_longCond & (BB_width > (self.params.bbMinWidth02 / 100))

        # # // ————— Short Bollinger Bands conditions
        # # // Defines the conditions for entering a short position using Bollinger Bands
        # # // New Shorts
        # # bool BB_short01 = na
        # BB_short01 = (~ADX_longCond) & self.high.cross_up(
        #     BB_upper) & EMA_shortCond & (BB_width > (self.params.bbMinWidth01 / 100))

        # # // Pyramiding Shorts
        # # bool BB_short02 = na
        # BB_short02 = (~ADX_longCond) & self.high.cross_up(
        #     BB_upper) & EMA_shortCond & (BB_width > (self.params.bbMinWidth02 / 100))

        # // ———————————————————— Volume
        # // Defines conditions for long and short positions based on volume
        # bool VOL_longCond = na
        # bool VOL_shortCond = na
        VOL_longCond = self.volume > self.volume.sma(
            self.params.sma_Length) * self.params.volume_f

        # / ———————————————————— Strategy
        # // Defines the long and short conditions for entering a trade based on multiple indicators and volume
        # bool longCond = na
        long_signal = EMA_longCond & ADX_longCond & SAR_longCond & MACD_longCond & VOL_longCond

        # bool shortCond = na
        short_signal = EMA_shortCond & ADX_shortCond & SAR_shortCond & MACD_shortCond & VOL_longCond

        # return SAR.psarl, SAR.psars, long_signal, short_signal


class Wolfe_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/EGvQ2nLb-Wolfe-Strategy-Trendoscope/"""
    params = dict(factor2=0.0002)
    overlap = True

    def next(self):
        self.lines.sigzag = self.btind.zigzag(
            self.params.factor2, -self.params.factor2)
        # print(self.lines.sigzag.values[~np.isnan(self.lines.sigzag.values)])


class RSI_Divergence_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/ASVRhqFM/"""
    params = dict(rsilen=14, len=14, tpb=25, sb=5,
                  tsb=0.25, tps=25, ss=5, tss=.25,)

    def axb(self, rsi: IndSeries):
        rsi = rsi.values
        model = sm.OLS
        # t = self.close.values
        _rsi = sm.add_constant(np.arange(self.close.size)).astype(np.float32)
        results = model(rsi, _rsi).fit()
        # print(results.params, results.fittedvalues)

        # model = sm.OLS  # sm.WLS if weights else sm.OLS
        # size = high.size
        # rs = np.zeros(size)
        # r2 = np.zeros(size)
        # high = high.values
        # low = low.values
        # volume = volume.values
        # for i in range(length-1, size):
        #     h = high[i-length+1:i+1]
        #     l = low[i-length+1:i+1]
        #     _l = sm.add_constant(l).astype(np.float64)
        #     _h = sm.add_constant(h).astype(np.float64)
        #     if weights:
        #         v = volume[i-length+1:i+1].astype(np.float64)
        #         model1 = model(h, _l, weights=v).fit()
        #         model2 = model(l, _h, weights=v).fit()
        #     else:
        #         model1 = model(h, _l).fit()
        #         model2 = model(l, _h).fit()
        #     rs[i] = model1.params[1]
        #     r2[i] = model2.params[1]  # rsquared

        return results.fittedvalues
        # return t

    def test(self):
        close = self.close.values
        a, b = close[10], close[99]
        diff = b-a
        t = diff/90.
        _a = a-10*t
        _b = b+10*t
        for i in range(0, 200):

            close[i] = _a+i*t

        return close

    def next(self):
        x = self.close.rsi(self.params.rsilen)
        # self.lines.axb = self.axb(self.close)
        # self.lines.test = self.test()
        self.lines.zigzag = self.btind.zigzag_full(0.001)
        # print(self.lines.zigzag.values.tolist())


class Heikin_Ashi_Supertrend(BtIndicator):
    """https://cn.tradingview.com/script/9z16eauD-Heikin-Ashi-Supertrend/"""
    params = dict(supertrendAtrPeriod=10, supertrendAtrMultiplier=2.7)

    def next(self):
        haTrueRange = self.atr(self.params.supertrendAtrPeriod)
        ha = self.ha()
        haHigh = ha.ha_high
        haLow = ha.ha_low
        haSupertrendUp = ((haHigh + haLow) / 2) - \
            (self.params.supertrendAtrMultiplier * haTrueRange)
        haSupertrendDown = ((haHigh + haLow) / 2) + \
            (self.params.supertrendAtrMultiplier * haTrueRange)
        haSupertrendUp, haSupertrendDown = haSupertrendUp.values, haSupertrendDown.values
        size = self.close.V
        trendingUp = np.zeros(size)
        trendingDown = np.zeros(size)
        supertrend = np.zeros(size)
        direction = np.zeros(size)
        haClose = ha.ha_close.values
        length = get_lennan(haSupertrendDown, haSupertrendUp)
        for i in range(length+1, size):
            trendingUp[i] = haClose[i-1] > trendingUp[i -
                                                      1] and max(haSupertrendUp[i], trendingUp[i-1]) or haSupertrendUp[i]
            trendingDown[i] = haClose[i-1] < trendingDown[i-1] and min(
                haSupertrendDown[i], trendingDown[i-1]) or haSupertrendDown[i]
            direction[i] = haClose[i] > trendingDown[i -
                                                     1] and 1 or (haClose[i] < trendingUp[i-1] and -1 or direction[i-1])
            supertrend[i] = direction[i] == 1 and trendingUp[i] or trendingDown[i]
        t = direction-np.append([0], direction[:-1])
        long_signal = t < 0
        short_signal = t > 0
        return supertrend, long_signal, short_signal


class Ichimoku_Cloud_and_ADX_ith_Trailing_Stop_Loss(BtIndicator):
    """https://cn.tradingview.com/script/dB4mrF4Y-Ichimoku-Cloud-and-ADX-with-Trailing-Stop-Loss-by-Coinrule/"""
    params = dict(ts_bars=9, ks_bars=26, ssb_bars=52,
                  cs_offset=26, ss_offset=26)
    overlap = False

    def next(self):
        senkouA, senkouB, tenkan, kijun, chikou_span = self.ichimoku().to_lines()
        avg_dm, pos_dm, neg_dm = self.adx().to_lines()
        ss_high = senkouA.shift(
            self.params.ss_offset - 1).tqfunc.max(senkouB.shift(self.params.ss_offset - 1))
        ss_low = senkouA.shift(
            self.params.ss_offset - 1).tqfunc.min(senkouB.shift(self.params.ss_offset - 1))
        # // Entry/Exit Signals
        tk_cross_bull = tenkan > kijun
        tk_cross_bear = tenkan < kijun
        cs_cross_bull = self.close.mom(self.params.cs_offset - 1) > 0
        cs_cross_bear = self.close.mom(self.params.cs_offset - 1) < 0
        price_above_kumo = self.close > ss_high
        price_below_kumo = self.close < ss_low

        long_signal = tk_cross_bull & cs_cross_bull & price_above_kumo & (
            avg_dm > 20) & (pos_dm > neg_dm) & (self.close.ema(3).cross_down(self.close.ema(8)))
        short_signal = tk_cross_bear & cs_cross_bear & price_below_kumo & (
            avg_dm > 20) & (pos_dm < neg_dm) & (self.close.ema(3).cross_up(self.close.ema(8)))
        return avg_dm, pos_dm, neg_dm, long_signal, short_signal


class Davins_10_200MA_Pullback_on_SPY_Strategy_v2(BtIndicator):
    """https://cn.tradingview.com/script/Gkib6KgL-Davin-s-10-200MA-Pullback-on-SPY-Strategy-v2-0/"""
    params = dict(i_ma1=200, i_ma2=10, i_ma3=50, i_stopPercent=0.15,)

    def next(self):
        # // Get indicator values
        ma1 = self.close.sma(self.params.i_ma1)  # //param 1
        ma2 = self.close.sma(self.params.i_ma2)  # //param 2
        ma3 = self.close.sma(self.params.i_ma3)  # //param 3
        ma_9 = self.close.ema(9)  # //param 2
        ma_20 = self.close.ema(20)  # //param 3
        highest52 = self.high.tqfunc.hhv(52)
        overall_change = (
            (highest52 - self.close.shift(51)) / highest52) * 100.


class GT_5_1_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/NE8Fe86z/"""
    params = dict(dec1=True, harsi_back=6, dec2=True, ssl_back=1, dec3=True, coral_back=4, dec4=False, macd_back=0, dec5=False, wave_back=0, tp=False, tpprice=1,
                  i_lenHARSI=14, i_smoothing=7, len=10, sm=9, cd=.4, n1=10, n2=21, obLevel1=60, obLevel2=53, sma=12, lma=26, tsp=9)

    def f_zrsi(self, _source: IndSeries, _length) -> IndSeries:
        return _source.rsi(_length) - 50.

    def f_zstoch(self, _source: IndSeries, _length, _smooth, _scale) -> IndSeries:
        _zstoch = self.stoch(_length, high=_source,
                             low=_source, close=_source).stochs - 50.
        _smoothed = _zstoch.sma(_smooth)
        _scaled = (_smoothed / 100.) * _scale
        return _scaled

    def f_rsi(self, _source: IndSeries, _length, _mode) -> IndSeries:
        size = _source.size
        _zrsi = self.f_zrsi(_source, _length).values
        _smoothed = np.full(size, np.nan)
        length = get_lennan(_zrsi)
        _smoothed[length] = _zrsi[length]
        for i in range(size):
            _smoothed[i] = (_smoothed[i-1] + _zrsi[i]) / 2.
        return _smoothed if _mode else _zrsi

    def f_rsiHeikinAshi(self, _length) -> tuple[IndSeries]:
        _closeRSI = self.f_zrsi(self.close, _length)
        _openRSI = _closeRSI.bfill()
        _highRSI_raw = self.f_zrsi(self.high, _length)
        _lowRSI_raw = self.f_zrsi(self.low, _length)
        _highRSI = _highRSI_raw.tqfunc.max(_lowRSI_raw).bfill()
        _lowRSI = _highRSI_raw.tqfunc.min(_lowRSI_raw).bfill()
        _close = (_openRSI*2. + _highRSI + _lowRSI) / 4.
        data = IndFrame(np.c_[(_openRSI, _highRSI, _lowRSI, _close)], lines=(
            "open", "high", "low", "close"))
        return data.ha().to_lines()

    def Coral_Trend_Candles(self) -> IndSeries:
        # string GROUP_3 = 'Config » Coral Trend Candles'
        size = self.V
        src = self.close.values
        _sm = self.params.sm
        cd = self.params.cd
        di = (_sm) / 2.0 + 1.0
        c1 = 2. / (di + 1.0)
        c2 = 1. - c1
        c3 = 3.0 * (cd * cd + cd * cd * cd)
        c4 = -3.0 * (2.0 * cd * cd + cd + cd * cd * cd)
        c5 = 3.0 * cd + 1.0 + cd * cd * cd + 3.0 * cd * cd
        i1 = np.zeros(size)
        i2 = np.zeros(size)
        i3 = np.zeros(size)
        i4 = np.zeros(size)
        i5 = np.zeros(size)
        i6 = np.zeros(size)
        bfr = np.zeros(size)
        for i in range(1, size):
            i1[i] = c1 * src[i] + c2 * i1[i-1]
            i2[i] = c1 * i1[i] + c2 * i2[i-1]
            i3[i] = c1 * i2[i] + c2 * i3[i-1]
            i4[i] = c1 * i3[i] + c2 * i4[i-1]
            i5[i] = c1 * i4[i] + c2 * i5[i-1]
            i6[i] = c1 * i5[i] + c2 * i6[i-1]
            bfr[i] = -cd * cd * cd * i6[i] + c3 * \
                i5[i] + c4 * i4[i] + c5 * i3[i]
        return IndSeries(bfr)

    def next(self):
        dec1, dec2, dec3, dec4, dec5 = self.params.dec1, self.params.dec2, self.params.dec3, self.params.dec4, self.params.dec5
        size = self.V
        close = self.close.values
        O, H, L, C = self.f_rsiHeikinAshi(self.params.i_lenHARSI)
        # O, H, L, C = O.values, H.values, L.values, C.values
        # string GROUP_2 = 'Config » SSL Channel'
        smaHigh = self.high.sma(self.params.len).values
        smaLow = self.low.sma(self.params.len).values
        Hlv = np.full(size, np.nan)
        sslDown = np.full(size, np.nan)
        sslUp = np.full(size, np.nan)
        length1 = get_lennan(smaHigh, smaLow)
        for i in range(length1+1, size):
            Hlv[i] = close[i] > smaHigh[i] and 1 or (
                close[i] < smaLow[i] and -1 or Hlv[i-1])
            sslDown[i] = Hlv[i] < 0 and smaHigh[i] or smaLow[i]
            sslUp[i] = Hlv[i] < 0 and smaLow[i] or smaHigh[i]
        sslUp, sslDown = IndSeries(sslUp), IndSeries(sslDown)
        # 'Config » Coral Trend Candles'
        bfr = self.Coral_Trend_Candles()
        bfrC = np.zeros(size)
        length2 = get_lennan(bfr)
        _bfr = bfr.values
        for i in range(length2+1, size):
            bfrC[i] = (_bfr[i] > _bfr[i-1]
                       ) and 1 or ((_bfr[i] < _bfr[i-1]) and -1 or 0)

        # //=======================================================================MACD DEMA=======================================================================//
        # string GROUP_4 = 'Config » MACD DEMA'
        # sma = input(12,title='DEMA Short', group=GROUP_4)
        # lma = input(26,title='DEMA Long', group=GROUP_4)
        # tsp = input(9,title='Signal', group=GROUP_4)
        # //dolignes = input(true,title="Lines", group=GROUP_4)

        MMEslowa = self.close.ema(self.params.lma)
        MMEslowb = MMEslowa.ema(self.params.lma)
        DEMAslow = ((2. * MMEslowa) - MMEslowb)
        MMEfasta = self.close.ema(self.params.sma)
        MMEfastb = MMEfasta.ema(self.params.sma)
        DEMAfast = ((2. * MMEfasta) - MMEfastb)
        LigneMACDZeroLag = (DEMAfast - DEMAslow)
        MMEsignala = LigneMACDZeroLag.ema(self.params.tsp)
        MMEsignalb = MMEsignala.ema(self.params.tsp)
        Lignesignal = ((2. * MMEsignala) - MMEsignalb)
        MACDZeroLag = (LigneMACDZeroLag - Lignesignal)
        swap1 = MACDZeroLag > 0.
        # swap1 = MACDZeroLag>0?color.green:color.red
        # string GROUP_5 = 'Config » WAVE TREND'

        # n1 = input(10, "Channel Length", group=GROUP_5)
        # n2 = input(21, "Average Length", group=GROUP_5)
        # //obLevel1 = input(60, "Over Bought Level 1", group=GROUP_5)
        # //obLevel2 = input(53, "Over Bought Level 2", group=GROUP_5)
        # //osLevel1 = input(-60, "Over Sold Level 1", group=GROUP_5)
        # //osLevel2 = input(-53, "Over Sold Level 2", group=GROUP_5)
        ap = self.hlc3()
        esa = ap.ema(self.params.n1)
        d = (ap - esa).apply(lambda x: abs(x)).ema(self.params.n1)
        ci = (ap - esa) / (0.015 * d)
        tci = ci.ema(self.params.n2)
        wt1 = tci
        wt2 = wt1.sma(4)
        checker_1 = np.zeros(size)  # // HARSI BUY
        checker_11 = np.zeros(size)  # // HARSI lookback BUY
        checker_2 = np.zeros(size)  # // HARSI SELL
        checker_21 = np.zeros(size)  # // HARSI lookback SELL
        checker_3 = np.zeros(size)  # // SSL AL
        checker_31 = np.zeros(size)  # // SSL lookback 0 dan büyükse al
        checker_4 = np.zeros(size)  # // SSL SAT
        checker_41 = np.zeros(size)  # // SSL lookback 0 dan büyükse sat
        checker_5 = np.zeros(size)  # // CORAL AL
        checker_51 = np.zeros(size)  # // CORAL lookback 1 den büyükse al
        checker_6 = np.zeros(size)  # // CORAL SAT
        checker_61 = np.zeros(size)  # // CORAL lookback 1 den büyükse sat
        checker_7 = np.zeros(size)  # // MACD AL
        checker_71 = np.zeros(size)  # // MACD lookback 0 dan büyükse al
        checker_8 = np.zeros(size)  # // MACD SAT
        checker_81 = np.zeros(size)  # // MACD lookback 0 dan büyükse sat
        checker_9 = np.zeros(size)  # // WAVE AL
        checker_91 = np.zeros(size)  # // WAVE lookback 0 dan büyükse al
        checker_10 = np.zeros(size)  # // WAVE SAT
        checker_101 = np.zeros(size)  # // WAVE lookback 0 dan büyükse sat

        # //=======================================================================HARSI=======================================================================//
        # if self.params.harsi_back == 1:
        checker_1 = C > O
        checker_1 &= C.shift() < O.shift()
        checker_1 &= C > C.shift()
        # //HARSI SELL
        checker_2 = C < O
        checker_2 &= C.shift() > O.shift()
        # // HARSI BUY
        # if self.params.harsi_back > 1:
        harsi_back = self.params.harsi_back
        for i in range(size):
            if i > harsi_back:
                for j in range(harsi_back):
                    if C[i-j] > O[i-j] and C[i-j-1] < O[i-j-1] and C[i-j] > C[i-j-1]:
                        checker_11[i] = 1
                        break
                for j in range(harsi_back):
                    if C[i-j] < O[i-j] and C[i-j-1] > O[i-j-1]:
                        checker_21[i] = 1
                        break
        # //=======================================================================SSL=======================================================================//
        # if self.params.ssl_back == 0:
            # if (ta.crossover(sslUp, sslDown))
        checker_3 = sslUp.cross_up(sslDown)
        checker_4 = sslUp.cross_down(sslDown)

        # elif self.params.ssl_back > 0:
        ssl_back = self.params.ssl_back
        sslUp, sslDown = sslUp.values, sslDown.values
        for i in range(size):
            if i > ssl_back:
                for j in range(ssl_back):
                    if sslUp[i-j] > sslDown[i-j] and sslUp[i-j-1] < sslDown[i-j-1]:
                        checker_31[i] = 1
                        break
                for j in range(ssl_back):
                    if sslUp[i-j] < sslDown[i-j] and sslUp[i-j-1] > sslDown[i-j-1]:
                        checker_41[i] = 1
        # //======================================================================CORAL=======================================================================//
        # if self.params.coral_back == 1:
        # if(bfrC == color.green and bfrC[1] == color.red)
        checker_5 = bfr > bfr.shift()
        checker_5 &= bfr.shift() < bfr.shift(2)
        # if(bfrC == color.red and bfrC[1] == color.green)
        checker_6 = bfr < bfr.shift()
        checker_6 &= bfr.shift() > bfr.shift(2)
        # if self.params.coral_back > 1:
        coral_back = self.params.coral_back
        for i in range(size):
            if i > coral_back:
                for j in range(coral_back):
                    if bfrC[i-j] > 0 and bfrC[i-j-1] < 0:
                        checker_51[i] = 1
                        break
                for j in range(coral_back):
                    if bfrC[i-j] < 0 and bfrC[i-j-1] > 0:
                        checker_61[i] = 1
        # //=======================================================================MACD=======================================================================//
        checker_7 = LigneMACDZeroLag.cross_up(Lignesignal)
        checker_8 = LigneMACDZeroLag.cross_down(Lignesignal)
        _checker_7 = checker_7.values
        _checker_8 = checker_8.values
        # if self.params.macd_back > 0:
        macd_back = self.params.macd_back
        for i in range(size):
            if i > macd_back:
                if _checker_7[i-macd_back+1:i+1].any():
                    checker_71[i] = 1
                if _checker_8[i-macd_back+1:i+1].any():
                    checker_81[i] = 1

        # //=======================================================================WAVE TREND=======================================================================//

        checker_9 = wt1.cross_up(wt2)
        checker_10 = wt1.cross_down(wt2)
        _checker_9 = checker_9.values
        _checker_10 = checker_10.values
        # if self.params.wave_back > 0:
        wave_back = self.params.wave_back
        for i in range(size):
            if i > wave_back:
                if _checker_9[i-wave_back+1:i+1].any():
                    checker_91[i] = 1
                if _checker_10[i-wave_back+1:i+1].any():
                    checker_101[i] = 1

        # //=======================================================================TEK SEÇENEK=======================================================================//
        # if multisignal == true
        #     buy := false
        #     sell := true
        # if buy == false and sell==true
        # long_signal=np.zeros(size)
        # short_signal=np.zeros(size)

        # //dec1,"HARSI"
        if dec1 and ~all([dec2, dec3, dec4, dec5]):
            long_signal = checker_1 | checker_11
        # //dec2,"SSL"
        if dec2 and ~all([dec1, dec3, dec4, dec5]):
            long_signal = checker_3 | checker_31
        # //dec3,"CORAL"
        if dec3 and ~all([dec2, dec1, dec4, dec5]):
            long_signal = checker_5 | checker_51
        # //dec4,"MACD"
        if dec4 and ~all([dec2, dec3, dec1, dec5]):
            long_signal = checker_7 | checker_71
        # //dec5,"WAVE"
        if dec5 and ~all([dec2, dec3, dec4, dec1]):
            long_signal = checker_9 | checker_91
        # //=======================================================================2 SEÇENEK=======================================================================//
        # //dec1-dec2,"HARSI\n SSL"
        if dec1 and dec2 and ~all([dec3, dec4, dec5]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal = long_signal1 & long_signal2
        # //dec1 dec3,"HARSI\n CORAL"
        if dec1 and dec3 and ~all([dec2, dec4, dec5]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2
        # //dec1 dec4    "HARSI\n MACD"
        if dec1 and dec4 and ~all([dec3, dec2, dec5]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2
        # //dec1 dec5,"HARSI\n WAVE"
        if dec1 and dec5 and ~all([dec3, dec4, dec2]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2
        # //dec2 dec3,"SSL\n CORAL"
        if dec2 and dec3 and ~all([dec1, dec4, dec5]):
            long_signal1 = checker_3 | checker_31
            long_signal2 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2
        # //dec2 dec4,"SSL\n MACD"
        if dec2 and dec4 and ~all([dec1, dec3, dec5]):
            long_signal1 = checker_3 | checker_31
            long_signal2 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2
        # //dec2 dec5    "SSL\n WAVE"
        if dec2 and dec5 and ~all([dec1, dec4, dec3]):
            long_signal1 = checker_3 | checker_31
            long_signal2 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2
        # //dec3 dec4,"CORAL\n MACD"
        if dec3 and dec4 and ~all([dec1, dec2, dec5]):
            long_signal1 = checker_5 | checker_51
            long_signal2 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2
        # //dec3 dec5,"CORAL\n WAVE"
        if dec3 and dec4 and ~all([dec1, dec2, dec5]):
            long_signal1 = checker_5 | checker_51
            long_signal2 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2
        # //dec4 dec5,"MACD\n WAVE"
        if dec2 and dec3 and ~all([dec1, dec4, dec5]):
            long_signal1 = checker_7 | checker_71
            long_signal2 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2
        # //=======================================================================3 SEÇENEK=======================================================================//
        # // dec 1 dec2 dec3,"HARSI\n SSL\n\n CORAL"
        if dec1 and dec2 and dec3 and ~all([dec4, dec5]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec1 dec2 dec4,"HARSI\n SSL\n\n MACD "
        if dec1 and dec2 and dec4 and ~all([dec3, dec5]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec1 dec2 dec5,"HARSI\n SSL\n\n WAVE "
        if dec1 and dec2 and dec5 and ~all([dec4, dec3]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec1 dec3 dec4,"HARSI\n CORAL\n\n MACD "
        if dec1 and dec4 and dec3 and ~all([dec2, dec5]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_5 | checker_51
            long_signal3 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec1 dec3 dec5,"HARSI\n CORAL\n\n WAVE "
        if dec1 and dec5 and dec3 and ~all([dec4, dec2]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_5 | checker_51
            long_signal3 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec1 dec4 dec5,"HARSI\n MACD\n\n WAVE "
        if dec1 and dec4 and dec5 and ~all([dec2, dec3]):
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_7 | checker_71
            long_signal3 = checker_9 | checker_91
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec2 dec3 dec4,"SSL\n CORAL\n\n MACD"
        if dec2 and dec3 and dec4 and ~all([dec1, dec5]):
            long_signal1 = checker_3 | checker_31
            long_signal2 = checker_7 | checker_71
            long_signal3 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec2 dec3 dec5,"SSL\n CORAL\n\n  WAVE"
        if dec2 and dec3 and dec5 and ~all([dec1, dec4]):
            long_signal1 = checker_3 | checker_31
            long_signal2 = checker_9 | checker_91
            long_signal3 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec2 dec4 dec5,"SSL\n MACD\n\n WAVE"
        if dec2 and dec4 and dec5 and ~all([dec1, dec3]):
            long_signal1 = checker_3 | checker_31
            long_signal2 = checker_9 | checker_91
            long_signal3 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3
        # // dec3 dec4 dec5,"CORAL\n MACD\n\n WAVE"
        if dec3 and dec4 and dec5 and ~all([dec1, dec2]):
            long_signal1 = checker_5 | checker_51
            long_signal2 = checker_9 | checker_91
            long_signal3 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3
        # //= == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == 4 SEÇENEK == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == =//
        # //dec1 dec2 dec3 dec4,"HARSI\n CORAL\n\n CORAL\n\n\n MACD"
        if dec1 and dec2 and dec3 and dec4 and ~dec5:
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_5 | checker_51
            long_signal4 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3 & long_signal4

        # //dec1 dec3 dec4 dec5,"HARSI\n CORAL\n\n MACD\n\n\n WAVE"
        if dec1 and dec5 and dec3 and dec4 and ~dec2:
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_9 | checker_91
            long_signal3 = checker_5 | checker_51
            long_signal4 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3 & long_signal4
        # //dec1 dec2 dec4 dec5,"HARSI\n SSL\n\n MACD\n\n\n WAVE"
        if dec1 and dec2 and dec5 and dec4 and ~dec3:
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_9 | checker_91
            long_signal4 = checker_7 | checker_71
            long_signal = long_signal1 & long_signal2 & long_signal3 & long_signal4

        # //dec1 dec2 dec3 dec5,"HARSI\n SSL\n\n CORAL\n\n\n WAVE"
        if dec1 and dec2 and dec3 and dec5 and ~dec4:
            long_signal1 = checker_1 | checker_11
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_9 | checker_91
            long_signal4 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2 & long_signal3 & long_signal4
        # //dec2 dec3 dec4 dec5,"SSL\n CORAL\n\n MACD\n\n\n WAVE"
        if dec5 and dec2 and dec3 and dec4 and ~dec1:
            long_signal1 = checker_7 | checker_71
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_9 | checker_91
            long_signal4 = checker_5 | checker_51
            long_signal = long_signal1 & long_signal2 & long_signal3 & long_signal4
        # //= == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == 5 SEÇENEK == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == =//
        # //dec1 dec2 dec3 dec4 dec5
        if dec5 and dec2 and dec3 and dec4 and dec1:
            long_signal1 = checker_7 | checker_71
            long_signal2 = checker_3 | checker_31
            long_signal3 = checker_9 | checker_91
            long_signal4 = checker_5 | checker_51
            long_signal5 = checker_1 | checker_11
            long_signal = long_signal1 & long_signal2 & long_signal3 & long_signal4 & long_signal1
        # //= == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == SELL == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == =//
        # //= == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == TEK SEÇENEK == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == =//
        # //dec1,"HARSI"
        if dec1 and ~all([dec2, dec3, dec4, dec5]):
            short_signal = checker_2 | checker_21
        # //dec2,"SSL"
        if dec2 and ~all([dec1, dec3, dec4, dec5]):
            short_signal = checker_4 | checker_41
        # //dec3,"CORAL"
        if dec3 and ~all([dec2, dec1, dec4, dec5]):
            short_signal = checker_6 | checker_61
        # //dec4,"MACD"
        if dec4 and ~all([dec2, dec3, dec1, dec5]):
            short_signal = checker_8 | checker_81
        # //dec5,"WAVE"
        if dec5 and ~all([dec2, dec3, dec4, dec1]):
            short_signal = checker_10 | checker_101
        # //=======================================================================2 SEÇENEK=======================================================================//
        # //dec1-dec2,"HARSI\n SSL"
        if dec1 and dec2 and ~all([dec3, dec4, dec5]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal = short_signal1 & short_signal2
        # //dec1 dec3,"HARSI\n CORAL"
        if dec1 and dec3 and ~all([dec2, dec4, dec5]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2
        # //dec1 dec4    "HARSI\n MACD"
        if dec1 and dec4 and ~all([dec3, dec2, dec5]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2
        # //dec1 dec5,"HARSI\n WAVE"
        if dec1 and dec5 and ~all([dec3, dec4, dec2]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2
        # //dec2 dec3,"SSL\n CORAL"
        if dec2 and dec3 and ~all([dec1, dec4, dec5]):
            short_signal1 = checker_4 | checker_41
            short_signal2 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2
        # //dec2 dec4,"SSL\n MACD"
        if dec2 and dec4 and ~all([dec1, dec3, dec5]):
            short_signal1 = checker_4 | checker_41
            short_signal2 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2
        # //dec2 dec5    "SSL\n WAVE"
        if dec2 and dec5 and ~all([dec1, dec4, dec3]):
            short_signal1 = checker_4 | checker_41
            short_signal2 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2
        # //dec3 dec4,"CORAL\n MACD"
        if dec3 and dec4 and ~all([dec1, dec2, dec5]):
            short_signal1 = checker_6 | checker_61
            short_signal2 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2
        # //dec3 dec5,"CORAL\n WAVE"
        if dec3 and dec4 and ~all([dec1, dec2, dec5]):
            short_signal1 = checker_6 | checker_61
            short_signal2 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2
        # //dec4 dec5,"MACD\n WAVE"
        if dec2 and dec3 and ~all([dec1, dec4, dec5]):
            short_signal1 = checker_8 | checker_81
            short_signal2 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2
        # //=======================================================================3 SEÇENEK=======================================================================//
        # // dec 1 dec2 dec3,"HARSI\n SSL\n\n CORAL"
        if dec1 and dec2 and dec3 and ~all([dec4, dec5]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec1 dec2 dec4,"HARSI\n SSL\n\n MACD "
        if dec1 and dec2 and dec4 and ~all([dec3, dec5]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec1 dec2 dec5,"HARSI\n SSL\n\n WAVE "
        if dec1 and dec2 and dec5 and ~all([dec4, dec3]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec1 dec3 dec4,"HARSI\n CORAL\n\n MACD "
        if dec1 and dec4 and dec3 and ~all([dec2, dec5]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_6 | checker_61
            short_signal3 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec1 dec3 dec5,"HARSI\n CORAL\n\n WAVE "
        if dec1 and dec5 and dec3 and ~all([dec4, dec2]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_6 | checker_61
            short_signal3 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec1 dec4 dec5,"HARSI\n MACD\n\n WAVE "
        if dec1 and dec4 and dec5 and ~all([dec2, dec3]):
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_8 | checker_81
            short_signal3 = checker_10 | checker_101
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec2 dec3 dec4,"SSL\n CORAL\n\n MACD"
        if dec2 and dec3 and dec4 and ~all([dec1, dec5]):
            short_signal1 = checker_4 | checker_41
            short_signal2 = checker_8 | checker_81
            short_signal3 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec2 dec3 dec5,"SSL\n CORAL\n\n  WAVE"
        if dec2 and dec3 and dec5 and ~all([dec1, dec4]):
            short_signal1 = checker_4 | checker_41
            short_signal2 = checker_10 | checker_101
            short_signal3 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec2 dec4 dec5,"SSL\n MACD\n\n WAVE"
        if dec2 and dec4 and dec5 and ~all([dec1, dec3]):
            short_signal1 = checker_4 | checker_41
            short_signal2 = checker_10 | checker_101
            short_signal3 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3
        # // dec3 dec4 dec5,"CORAL\n MACD\n\n WAVE"
        if dec3 and dec4 and dec5 and ~all([dec1, dec2]):
            short_signal1 = checker_6 | checker_61
            short_signal2 = checker_10 | checker_101
            short_signal3 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3
        # //= == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == 4 SEÇENEK == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == =//
        # //dec1 dec2 dec3 dec4,"HARSI\n CORAL\n\n CORAL\n\n\n MACD"
        if dec1 and dec2 and dec3 and dec4 and ~dec5:
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_6 | checker_61
            short_signal4 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3 & short_signal4

        # //dec1 dec3 dec4 dec5,"HARSI\n CORAL\n\n MACD\n\n\n WAVE"
        if dec1 and dec5 and dec3 and dec4 and ~dec2:
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_10 | checker_101
            short_signal3 = checker_6 | checker_61
            short_signal4 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3 & short_signal4
        # //dec1 dec2 dec4 dec5,"HARSI\n SSL\n\n MACD\n\n\n WAVE"
        if dec1 and dec2 and dec5 and dec4 and ~dec3:
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_10 | checker_101
            short_signal4 = checker_8 | checker_81
            short_signal = short_signal1 & short_signal2 & short_signal3 & short_signal4

        # //dec1 dec2 dec3 dec5,"HARSI\n SSL\n\n CORAL\n\n\n WAVE"
        if dec1 and dec2 and dec3 and dec5 and ~dec4:
            short_signal1 = checker_2 | checker_21
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_10 | checker_101
            short_signal4 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2 & short_signal3 & short_signal4
        # //dec2 dec3 dec4 dec5,"SSL\n CORAL\n\n MACD\n\n\n WAVE"
        if dec5 and dec2 and dec3 and dec4 and ~dec1:
            short_signal1 = checker_8 | checker_81
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_10 | checker_101
            short_signal4 = checker_6 | checker_61
            short_signal = short_signal1 & short_signal2 & short_signal3 & short_signal4
        # //= == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == 5 SEÇENEK == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == == =//
        # //dec1 dec2 dec3 dec4 dec5
        if dec5 and dec2 and dec3 and dec4 and dec1:
            short_signal1 = checker_8 | checker_81
            short_signal2 = checker_4 | checker_41
            short_signal3 = checker_10 | checker_101
            short_signal4 = checker_6 | checker_61
            short_signal5 = checker_2 | checker_21
            short_signal = short_signal1 & short_signal2 & short_signal3 & short_signal4 & short_signal1

        return bfr, bfrC, long_signal, short_signal


class Moving_Average_Displaced_Envelope_ATRTS(BtIndicator):
    """https://cn.tradingview.com/script/n45eXKBG-Moving-Average-Displaced-Envelope-ATRTS/"""
    params = dict(Period=9, perAb=0.5, perBl=.5,
                  disp=13, nATRPeriod=15, nATRMultip=2)

    def next(self):
        Price = self.close
        sEMA = Price.ema(self.params.Period)
        top = sEMA.shift(self.params.disp) * \
            ((100. + self.params.perAb) / 100.)
        bott = sEMA.shift(self.params.disp) * \
            ((100. - self.params.perBl) / 100.)

        xATR = self.atr(self.params.nATRPeriod)
        xHHs = top.tqfunc.hhv(self.params.nATRPeriod).sma(
            self.params.nATRPeriod)
        xLLs = bott.tqfunc.llv(self.params.nATRPeriod).sma(
            self.params.nATRPeriod)
        nSpread = (xHHs - xLLs) / 2
        nLoss = self.params.nATRMultip * xATR
        up = (self.close+nLoss).values
        dn = (self.close-nLoss).values
        xATRTrailingStop = np.zeros(self.V)
        length = get_lennan(up, dn)
        close = self.close.values
        for i in range(length+1, self.V):
            xATRTrailingStop[i] = (close[i] > xATRTrailingStop[i-1] and close[i-1] > xATRTrailingStop[i-1]) and max(xATRTrailingStop[i-1], dn[i]) or \
                ((close[i] < xATRTrailingStop[i-1] and close[i-1] < xATRTrailingStop[i-1]) and min(xATRTrailingStop[i-1], up[i]) or
                 ((close[i] > xATRTrailingStop[i-1]) and dn[i] or up[i]))

        long_signal = self.close > xATRTrailingStop
        short_signal = self.close < xATRTrailingStop

        # iff_1 = close > top ? 1 : pos[1]
        # pos := close < bott ? -1 : iff_1
        # iff_2 = reverse and pos == -1 ? 1 : pos
        # possig = reverse and pos == 1 ? -1 : iff_2
        return xATRTrailingStop, long_signal, short_signal


class HHLL_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/x4za4NFZ-Simple-and-Profitable-Scalping-Strategy-ForexSignals-TV/"""
    params = dict(Len=29, std=2.)

    def next(self):
        xLL, mid, xHH, movevalue, _ = self.hlc3().bbands(
            self.params.Len, self.params.std).to_lines()

        movevalue /= 2.
        xHHM = xHH + movevalue
        xLLM = xLL - movevalue

        long_signal = self.high > xHHM.shift(1)
        short_signal = self.low < xLLM.shift(1)
        return xHHM, xLLM, long_signal, short_signal


class Faytterro_Estimator_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/qgtXBmhG/"""
    params = dict(len=10, len2=100, len3=650, len4=650)

    @staticmethod
    def cr(x: pd.Series):
        x = x.values[::-1]
        y = len(x)
        z = 0.0
        for i in range(y):
            z += x[i]*((y-1)/2.+1-abs(i-(y-1)/2.))
        return z/(((y+1)/2.)*(y+1)/2.)

    @staticmethod
    def di(x) -> np.ndarray:
        cr = x[::-1]
        y = len(x)-1
        src = cr[0]
        length = 2*y
        dizi = np.zeros(length)
        for i in range(length):
            dizi[i] = (i*(i-1)*(cr[0]-2*cr[1]+cr[2])/2+i*(cr[1]-cr[2])+cr[2])
        buy = dizi[y+6] > dizi[y+5] and dizi[y+6] < src
        sell = dizi[y+6] < dizi[y+5] and dizi[y+6] > src
        return buy, sell

    def next(self):
        cr: IndSeries = self.close.rolling(
            2*self.params.len-1).apply(self.cr)
        # self.lines.dizi = np.append(
        #     cr.values[:-3], self.di(cr, self.params.len*2))[-self.V:]
        # self.lines.cr = cr
        short_signal, long_signal = cr.rolling_apply(
            self.di, self.params.len+1).astype(np.int8).to_lines()
        return cr, long_signal, short_signal


class SSL_Wave_Trend_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/J0urw1QI-SSL-Wave-Trend-Strategy/"""
    params = dict(slAtrMultiplier=1.7, n1=10, n2=21, obLevel1=60, obLevel2=53, sslChLen=144, maType="HMA", len=60, kidiv=1, jurik_phase=3, jurik_power=1, volatility_lookback=10, beta=.8, z=.5,
                  ssfLength=20, ssfPoles=2, multy=.2, emaLength=200, kcLength=20, kcMult=1.5, alen=10, chPeriod=20, nnfxAtrLength=14, feedback=False, nnfxSmoothing="RMA",
                  useSslHybrid=True, useKeltnerCh=True, keltnerChWicks=True, useEma=True, useCandleHeight=True, candleHeight=1.)

    # //EDSMA
    @staticmethod
    def get2PoleSSF(src: IndSeries, length) -> np.ndarray:
        src = src.values
        PI = 2 * math.asin(1)
        arg = math.sqrt(2) * PI / length
        a1 = math.exp(-arg)
        b1 = 2 * a1 * math.cos(arg)
        c2 = b1
        c3 = -math.pow(a1, 2)
        c1 = 1 - c2 - c3
        size = len(src)
        ssf = np.zeros(size)
        for i in range(2, size):
            ssf[i] = c1 * src[i] + c2 * ssf[i-1] + c3 * ssf[i-2]
        return ssf

    @staticmethod
    def get3PoleSSF(src: IndSeries, length) -> np.ndarray:
        src = src.values
        PI = 2 * math.asin(1)

        arg = PI / length
        a1 = math.exp(-arg)
        b1 = 2 * a1 * math.cos(1.738 * arg)
        c1 = math.pow(a1, 2)

        coef2 = b1 + c1
        coef3 = -(c1 + b1 * c1)
        coef4 = math.pow(c1, 2)
        coef1 = 1 - coef2 - coef3 - coef4

        size = len(src)
        ssf = np.zeros(size)
        for i in range(3, size):
            ssf[i] = coef1 * src[i] + coef2 * ssf[i-1] + \
                coef3 * ssf[i-2] + coef4 * ssf[i-3]
        return ssf

    def getma(self, type: str, src: IndSeries, len):
        size = src.size
        result = np.zeros(size)
        if type == "TMA":
            result = src.sma(math.ceil(len / 2)).sma(math.floor(len / 2) + 1)
            result
        if type == "MF":
            src = src.values
            feedback = self.params.feedback
            z = self.params.z
            beta = self.params.beta
            ts = np.zeros(size)
            ts[0] = src[0]
            b = np.zeros(size)
            b[0] = feedback and (z * src[1] + (1. - z) * ts[0]) or src[1]
            c = np.zeros(size)
            c[0] = b[0]
            os = np.zeros(size)
            alpha = 2 / (len + 1)
            for i in range(1, size):
                a = feedback and (z * src[i] + (1. - z) * ts[i-1]) or src[i]

                b[i] = (a > alpha * a + (1 - alpha) * b[i-1]
                        ) and a or alpha * a + (1 - alpha) * b[i-1]
                c[i] = (a < alpha * a + (1 - alpha) * c[i-1]
                        ) and a or alpha * a + (1 - alpha) * c[i-1]
                os[i] = a == b and 1 or (0 if a == c else os[i-1])

                upper = beta * b[i] + (1 - beta) * c[i]
                lower = beta * c[i] + (1 - beta) * b[i]
                ts[i] = os[i] * upper[i] + (1. - os[i]) * lower[i]
            result = ts

        if type == "LSMA":
            result = src.linreg(self.params.len)
        if type == "SMA":  # // Simple
            result = src.sma(self.params.len)
        if type == "EMA":  # // Exponential
            result = src.ema(self.params.len)
        if type == "DEMA":  # // Double Exponential
            e = src.ema(self.params.len)
            result = 2 * e - e.ema(self.params.len)
        if type == "TEMA":  # // Triple Exponential
            result = src.tema(self.params.len)
        if type == "WMA":  # // Weighted
            result = src.wma(self.params.len)
        if type == "VAMA":  # // Volatility Adjusted
            mid = src.ema(self.params.len)
            dev = src - mid
            vol_up = dev.tqfunc.hhv(self.params.volatility_lookback)
            vol_down = dev.tqfunc.llv(self.params.volatility_lookback)
            result = mid + (vol_up+vol_down)/2.
        if type == "HMA":  # // Hull
            result = src.hma(self.params.len)
        if type == "JMA":  # // Jurik
            result = src.jma(self.params.len)
            # /// Copyright © 2018 Alex Orekhov (everget)
            # /// Copyright © 2017 Jurik Research and Consulting.
            # phaseRatio  = jurik_phase < -100 ? 0.5 : jurik_phase > 100 ? 2.5 : jurik_phase / 100 + 1.5
            # beta        = 0.45 * (len - 1) / (0.45 * (len - 1) + 2)
            # alpha       = math.pow(beta, jurik_power)
            # jma         = 0.0
            # e0          = 0.0
            # e0         := (1 - alpha) * src + alpha * nz(e0[1])
            # e1          = 0.0
            # e1         := (src - e0) * (1 - beta) + beta * nz(e1[1])
            # e2          = 0.0
            # e2         := (e0 + phaseRatio * e1 - nz(jma[1])) * math.pow(1 - alpha, 2) + math.pow(alpha, 2) * nz(e2[1])
            # jma        := e2 + nz(jma[1])
            # result     := jma
            # result
        if type == "McGinley":
            len = self.params.len
            mg = np.full(size, np.nan)
            ema = src.ema(len)
            length = get_lennan(ema)
            mg[length] = ema.iloc[length]
            src = src.values
            for i in range(length+1, size):
                mg[i] = mg[i-1] + (src[i] - mg[i-1]) / \
                    (len * math.pow(src[i] / mg[i-1], 4))
            result = mg
        if type == "EDSMA":
            zeros = src - src.shift(2)
            avgZeros = (zeros + zeros.shift(1)) / 2.
            # // Ehlers Super Smoother Filter
            ssf = self.get2PoleSSF(avgZeros, self.params.ssfLength) if self.params.ssfPoles == 2 else self.get3PoleSSF(
                avgZeros, self.params.ssfLength)
            ssf = IndSeries(ssf)
            # // Rescale filter in terms of Standard Deviations
            stdev = ssf.stdev(self.params.len)
            scaledFilter = ssf.ZeroDivision(stdev)

            alpha = 5. * scaledFilter.abs() / self.params.len
            alpha = alpha.values
            edsma = np.zeros(size)
            src = src.values
            length = get_lennan(alpha)
            for i in range(length+1, size):
                edsma[i] = alpha[i] * src[i] + (1 - alpha[i]) * edsma[i-1]
            result = edsma
        return result

    def function(self, source: IndSeries, length):
        if self.params.nnfxSmoothing == "RMA":
            result = source.rma(length)
        else:
            if self.params.nnfxSmoothing == "SMA":
                result = source.sma(length)
            else:
                if self.params.nnfxSmoothing == "EMA":
                    result = source.ema(length)
                else:
                    result = source.wma(length)
        return result

    @staticmethod
    def formula(number: IndSeries, decimals) -> float:
        factor = math.pow(10, decimals)
        return (number * factor).apply(lambda x: int(x) if not np.isnan(x) else 0.) / factor

    def next(self):
        # // Wave Trend
        # // ----------
        ap = self.hlc3()
        esa = ap.ema(self.params.n1)
        d = (ap - esa).abs().ema(self.params.n1)
        ci = (ap - esa).ZeroDivision(0.015 * d)
        tci = ci.ema(self.params.n2)
        wt1 = tci
        wt2 = wt1.sma(4)

        wtBreakUp = wt1.cross_up(wt2)
        wtBreakDown = wt1.cross_down(wt2)

        # // SSL Channel
        # // -----------
        tr = self.atr(14)
        smaHigh = (self.high+tr).sma(self.params.sslChLen).values
        smaLow = (self.low-tr).sma(self.params.sslChLen).values
        size = self.V
        sslChHlv = np.zeros(size)
        sslChDown = np.zeros(size)
        sslChUp = np.zeros(size)
        length1 = get_lennan(smaHigh, smaLow)
        close = self.close.values
        for i in range(length1+1, self.V):
            sslChHlv[i] = close[i] > smaHigh[i] and 1 or (
                close[i] < smaLow[i] and -1 or sslChHlv[i-1])
            sslChDown[i] = sslChHlv[i] < 0 and smaHigh[i] or smaLow[i]
            sslChUp[i] = sslChHlv[i] < 0 and smaLow[i] or smaHigh[i]
        sslChUp = IndSeries(sslChUp)

        # ///Keltner Baseline Channel
        # BBMC = self.getma(self.params.maType, self.close, self.params.len)
        Keltma = self.getma(self.params.maType, self.close, self.params.len)
        range_1 = self.true_range() if self.params.useTrueRange else self.high - self.low
        rangema = range_1.ema(self.params.len)
        upperk = Keltma + rangema * self.params.multy
        lowerk = Keltma - rangema * self.params.multy

        ema = self.close.ema(self.params.emaLength)

        # // Keltner Channels
        # // ----------------
        kcMa = self.close.ema(self.params.kcLength)

        KTop2 = kcMa + self.params.kcMult * self.atr(self.params.alen)
        KBot2 = kcMa - self.params.kcMult * self.atr(self.params.alen)

        nnfxAtr = self.formula(self.function(
            self.true_range(), self.params.nnfxAtrLength), 5) * self.params.slAtrMultiplier

        # //Sell
        longSlAtr = close - nnfxAtr if self.params.nnfxAtrLength else close + nnfxAtr
        shortSlAtr = close + nnfxAtr if self.params.nnfxAtrLength else close - nnfxAtr

        # // Condition 1: SSL Hybrid blue for long or red for short
        bullSslHybrid = close > upperk if self.params.useSslHybrid else True
        bearSslHybrid = close < lowerk if self.params.useSslHybrid else True
        # // Condition 2: SSL Channel crosses up for long or down for short
        bullSslChannel = sslChUp.cross_up(sslChDown)
        bearSslChannel = sslChUp.cross_down(sslChDown)
        # // Condition 3: Wave Trend crosses up for long or down for short
        bullWaveTrend = wtBreakUp
        bearWaveTrend = wtBreakDown

        # // ---------------------------
        # // Candle Height in Percentage
        # // ---------------------------
        percentHL = (self.high - self.low) / self.low * 100
        percentRed = (self.open - self.close).ZeroDivision(.01 *
                                                           self.close).Where(self.open > self.close, 0.)
        percentGreen = (self.close - self.open).ZeroDivision(.01 *
                                                             self.open).Where(self.open < self.close, 0.)

        # // Condition 4: Entry candle heignt <= 0.6 on Candle Height in Percentage
        candleHeightValid = ((percentGreen <= self.params.candleHeight) & (
            percentRed <= self.params.candleHeight)) if self.params.useCandleHeight else True

        # // Condition 5: Entry candle is inside Keltner Channel
        withinCh = ((self.high < KTop2) & (self.low > KBot2)) if self.params.keltnerChWicks else (
            (self.open < KTop2) & (self.close < KTop2) & (self.open > KBot2) & (self.close > KBot2))
        insideKeltnerCh = withinCh if self.params.useKeltnerCh else True

        # #// Trade entry and exit variables
        # var tradeEntryBar   = bar_index
        # var profitPoints    = 0.
        # var lossPoints      = 0.
        # var slPrice         = 0.
        # var tpPrice         = 0.
        # var inLong          = false
        # var inShort         = false

        # #// Exit calculations
        # slAmount            = nnfxAtr
        # slPercent           = math.abs((1 - (close - slAmount) / close) * 100)
        # tpPercent           = slPercent * riskReward
        # tpPoints            = percentAsPoints(tpPercent)
        # tpTarget            = calcProfitTrgtPrice(tpPoints, wtBreakUp)

        # # // Condition 6: TP target does not touch 200 EMA
        # bullTpValid = ~((close < ema) & (tpTarget > ema)) if self.params.useEma else True
        # bearTpValid = ~((close > ema) & (tpTarget < ema)) if self.params.useEma else True
        # // Combine all entry conditions
        long_signal = bullSslHybrid & bullSslChannel & bullWaveTrend & candleHeightValid & insideKeltnerCh
        short_signal = bearSslHybrid & bearSslChannel & bearWaveTrend & candleHeightValid & insideKeltnerCh
        return sslChUp, sslChDown, long_signal, short_signal


class Bitfinex_Shorts_Strat(BtIndicator):
    """https://cn.tradingview.com/script/Lk7b0jim-Bitfinex-Shorts-Strat/"""
    params = dict(length=7, overSold=75, overBought=30)
    overlap = False

    def next(self):
        vrsi = self.open.rsi(self.params.length)
        long_signal = vrsi.cross_up(self.params.overSold)
        short_signal = vrsi.cross_down(self.params.overBought)
        return vrsi, long_signal, short_signal


class Braid_Filter_ADX_EMA_Trend(BtIndicator):
    """https://cn.tradingview.com/script/ZmsAUj1J-Strategy-Myth-Busting-2-Braid-Filter-ADX-EMA-Trend-MYN/"""
    params = dict(maType="ema", Period1=3, Period2=7, Period3=14,
                  PipsMinSepPercent=40, ema1=100, len=14, th=20, iADXSlope=1.5)
    overlap = False

    def next(self):
        # //-- Braid Filter
        ma01 = self.close.ma(self.params.maType, self.params.Period1)
        ma02 = self.open.ma(self.params.maType, self.params.Period2)
        ma03 = self.close.ma(self.params.maType, self.params.Period3)

        # math.max(math.max(ma01, ma02), ma03)
        max = ma01.tqfunc.max(ma02.tqfunc.max(ma03))
        min = ma01.tqfunc.min(ma02.tqfunc.min(ma03))
        dif = max - min

        filter = self.atr(14) * self.params.PipsMinSepPercent / 100.

        #
        entryBbraidFilterGreenBar = (ma01 > ma02) & (dif > filter)
        entryBraidFilterRedBar = (ma02 > ma01) & (dif > filter)

        usedEma = self.close.ema(self.params.ema1)
        entryPriceActionAboveEMATrend = self.hlc3() >= usedEma
        entryPriceActionBelowEMATrend = self.hlc3() < usedEma

        # DIPlus = SmoothedDirectionalMovementPlus / SmoothedTrueRange * 100
        # DIMinus = SmoothedDirectionalMovementMinus / SmoothedTrueRange * 100
        # DX = math.abs(DIPlus - DIMinus) / (DIPlus + DIMinus) * 100
        # ADX = ta.sma(DX, len)
        ADX, DIPlus, DIMinus = self.adx(self.params.len).to_lines()

        # // 3) ADX must be above the 20 level and be pointed up. If flat or downwards, don't enter trade

        # iADXSlope = 3.5, minval=0, maxval=300, title='ADX Slope', step=.5, group="ADX and DI")
        entryADXAboveThreshold = ADX > self.params.th
        entryADXAboveThreshold &= (ADX.shift(
            2) + self.params.iADXSlope) < ADX.shift()
        entryADXAboveThreshold &= (ADX.shift() + self.params.iADXSlope) < ADX

        long_signal = entryPriceActionAboveEMATrend & entryBbraidFilterGreenBar & entryADXAboveThreshold
        short_signal = entryPriceActionBelowEMATrend & entryBraidFilterRedBar & entryADXAboveThreshold

        return ADX, DIPlus, DIMinus, long_signal, short_signal


class Range_Strat_MACD_RSI(BtIndicator):
    """https://cn.tradingview.com/script/XWgu79vU-Range-Strat-MACD-RSI/"""
    params = dict(length=89, overSold=50, overBought=50,
                  fast_length=60, slow_length=120, signal_length=20,)
    overlap = False

    def next(self):
        rsisrc = self.open
        # // Calculating RSI
        vrsi = rsisrc.rsi(self.params.length)
        RSIunder = vrsi <= self.params.overSold
        RSIover = vrsi >= self.params.overBought
        macd, delta, signal = self.macd(
            self.params.fast_length, self.params.slow_length, self.params.signal_length).to_lines()
        MACDcrossover = delta.cross_up(0.)
        MACDcrossunder = delta.cross_down(0.)
        long_signal = RSIover & MACDcrossover
        short_signal = RSIunder & MACDcrossunder
        return macd, delta, signal, long_signal, short_signal


class Andean_Scalping(BtIndicator):
    """https://cn.tradingview.com/script/Fj0ldM0e-Andean-Scalping/"""
    params = dict()

    def next(self):
        # up, dn, bull, bear, signal = self.btind.AndeanOsc().to_lines()
        up, dn, bull, bear, signal = self.btind.AndeanOsc(34).to_lines()
        adx = self.adx()
        long_signal = bull > bear
        long_signal &= bull > signal
        long_signal &= signal > signal.sma(1000)
        long_signal &= adx.adxx > 20.
        # exitlong_signal = bull < signal

        short_signal = bear > bull
        short_signal &= bear > signal
        short_signal &= signal > signal.sma(1000)
        short_signal &= adx.adxx > 20.
        # exitshort_signal = bear < signal
        return up, dn, long_signal,  short_signal


class PlanB_Quant_Investing(BtIndicator):
    """https://cn.tradingview.com/script/Tz02ikOo-PlanB-Quant-Investing-101-v2/"""
    params = dict(length=14, selllevel=90, drop=65, buylevel=90, len=6)

    def next(self):
        up, dn, bull, bear, signal = self.btind.AndeanOsc().to_lines()

        rsi = self.close.rsi(self.params.length)
        maxrsi = rsi.tqfunc.hhv(self.params.len).shift()

        rsisell = maxrsi > self.params.selllevel
        rsidrop = rsi < self.params.drop
        short_signal = rsisell & rsidrop
        minrsi = rsi.tqfunc.llv(self.params.len).shift()

        rsibuy = minrsi < self.params.buylevel

        # //IF (RSI jumps +2% from the low) THEN buy, ELSE hold.

        rsibounce = rsi > 35

        long_signal = rsibuy & rsibounce
        long_signal &= bull > bear
        long_signal &= bull > signal
        short_signal &= bear > bull
        short_signal &= bear > signal
        return rsi, maxrsi, minrsi, long_signal, short_signal


class AlphaTrend_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/3wdQu7P3-AlphaTrend-Strategy/"""
    params = dict(coeff=1., AP=14, level=50., novolumedata=False)

    def next(self):
        ATR = self.true_range().sma(self.params.AP)
        src = self.close
        upT = self.low - ATR * self.params.coeff
        downT = self.high + ATR * self.params.coeff
        rsi = src.rsi(self.params.AP)
        mfi = self.hlc3().mfi(self.params.AP)
        AlphaTrend = np.zeros(self.V)
        novolumedata = self.params.novolumedata
        for i, rsi_, mfi_, up, dn in self.enumerate(rsi.values, mfi.values, upT.values, downT.values):
            AlphaTrend[i] = (rsi_ >= 50. if novolumedata else mfi_ >= 50.) and min(
                [AlphaTrend[i-1], up]) or max([AlphaTrend[i-1], dn])
        AlphaTrend = IndSeries(AlphaTrend)
        AlphaTrend2 = AlphaTrend.shift(2)
        long_signal = AlphaTrend.cross_up(AlphaTrend2)
        short_signal = AlphaTrend.cross_down(AlphaTrend2)
        self.lines
        return AlphaTrend, AlphaTrend2, long_signal, short_signal


class Bitcoin_Scalping_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/T4rBZCvC-Bitcoin-Scalping-Strategy-Sampled-with-PMARP-MADRID-MA-RIBBON/"""
    params = dict(i_ma_len=20, i_ma_typ='vwma', i_pmarp_lookback=350, i_signal_ma_Len=20, i_signal_ma_typ='sma',
                  i_hi_alert_pmarp=99, i_lo_alert_pmarp=1, i_hi_alert_pmar=2.7, i_lo_alert_pmar=.7,)
    overlap = False

    def f_prior_sum(_P, _X):
        math.sum(_P[1], _X - 1)

    def f_ma_val(self, _price: IndSeries, _typ: str, _len: int) -> IndSeries:
        return _price.ma(_typ, _len)

    @staticmethod
    def pmarpsum(x: np.ndarray) -> float:
        length = len(x)
        new = x[-1]
        sum = 0.
        for v in x[:-1]:
            sum += v <= new and 1. or 0.
        return sum/length*100.

    def f_pmarp(self, _price: IndSeries, _pmarLen, _pmarpLen, _type) -> IndSeries:
        _pmar: IndSeries = _price.ZeroDivision(
            self.f_ma_val(_price, _type, _pmarLen)).abs()
        return _pmar.rolling_apply(self.pmarpsum, _pmarpLen)

    # def f_clrSlct( _percent, _select, _type, _solid, _array1, _array2 ) =>
    #     _select == 'Solid' ? _solid : array.get ( _type == 'Blue Green Red' ? _array1 : _array2, math.round ( _percent ))

    def next(self):
        s_pmarp = 'Price Moving Average Ratio Percentile'
        s_pmar = 'Price Moving Average Ratio'
        s_BGR = 'Blue Green Red'
        s_BR = 'Blue Red'
        i_p_type_line = s_pmarp
        pmarpOn = i_p_type_line == 'Price Moving Average Ratio Percentile'
        ma = self.f_ma_val(self.close, self.params.i_ma_typ,
                           self.params.i_ma_len)
        pmar = self.close / ma
        pmarp = self.f_pmarp(self.close, self.params.i_ma_len,
                             self.params.i_pmarp_lookback, self.params.i_ma_typ)
        pmarHigh = pmar.apply(lambda x: max(0., x))

        # c_pmar = pmar.ZeroDivision(pmarHigh)*100.

        plotline = pmarp if pmarpOn else pmar

        signal_ma = self.f_ma_val(
            plotline,  self.params.i_signal_ma_typ, self.params.i_signal_ma_Len)

        hi_alert = pmarpOn and self.params.i_hi_alert_pmarp or self.params.i_hi_alert_pmar
        lo_alert = pmarpOn and self.params.i_lo_alert_pmarp or self.params.i_lo_alert_pmar

        # hi_alertc   = pmarpOn and self.params.i_hi_alert_pmarp or (i_hi_alert_pmar > pmarHigh ? 100 : ( i_hi_alert_pmar / pmarHigh ) * 100
        hi_alertc = self.params.i_hi_alert_pmarp
        p_hi_alert = plotline > hi_alert
        p_lo_alert = plotline < lo_alert
        PHI = (1 + math.sqrt(5)) / 2
        PI = np.pi

        src = self.close
        matype = "ema"
        ma05 = src.ma(matype, 5)
        ma10 = src.ma(matype, 10)
        ma15 = src.ma(matype, 15)
        ma20 = src.ma(matype, 20)
        ma25 = src.ma(matype, 25)
        ma30 = src.ma(matype, 30)
        ma35 = src.ma(matype, 35)
        ma40 = src.ma(matype, 40)
        ma45 = src.ma(matype, 45)
        ma50 = src.ma(matype, 50)
        ma55 = src.ma(matype, 55)
        ma60 = src.ma(matype, 60)
        ma65 = src.ma(matype, 65)
        ma70 = src.ma(matype, 70)
        ma75 = src.ma(matype, 75)
        ma80 = src.ma(matype, 80)
        ma85 = src.ma(matype, 85)
        ma90 = src.ma(matype, 90)
        ma100 = src.ma(matype, 100)
        # //Lime color conditions when true
        greenMA_1 = ma05 > ma100
        greenMA_2 = ma10 > ma100
        greenMA_3 = ma15 > ma100
        greenMA_4 = ma20 > ma100
        greenMA_5 = ma25 > ma100
        greenMA_6 = ma30 > ma100
        greenMA_7 = ma35 > ma100
        greenMA_8 = ma40 > ma100
        greenMA_9 = ma45 > ma100
        greenMA_10 = ma50 > ma100
        greenMA_11 = ma55 > ma100
        greenMA_12 = ma60 > ma100
        greenMA_13 = ma65 > ma100
        greenMA_14 = ma70 > ma100
        greenMA_15 = ma75 > ma100
        greenMA_16 = ma80 > ma100
        greenMA_17 = ma85 > ma100
        greenMA_18 = ma90 > ma100

        # //Red color
        redMA_1 = ma05 < ma100
        redMA_2 = ma10 < ma100
        redMA_3 = ma15 < ma100
        redMA_4 = ma20 < ma100
        redMA_5 = ma25 < ma100
        redMA_6 = ma30 < ma100
        redMA_7 = ma35 < ma100
        redMA_8 = ma40 < ma100
        redMA_9 = ma45 < ma100
        redMA_10 = ma50 < ma100
        redMA_11 = ma55 < ma100
        redMA_12 = ma60 < ma100
        redMA_13 = ma65 < ma100
        redMA_14 = ma70 < ma100
        redMA_15 = ma75 < ma100
        redMA_16 = ma80 < ma100
        redMA_17 = ma85 < ma100
        redMA_18 = ma90 < ma100

        # // Difference of color
        Diffma1 = ma05.pct_change()
        Diffma2 = ma10.pct_change()
        Diffma3 = ma15.pct_change()
        Diffma4 = ma20.pct_change()
        Diffma5 = ma25.pct_change()
        Diffma6 = ma30.pct_change()
        Diffma7 = ma35.pct_change()
        Diffma8 = ma40.pct_change()
        Diffma9 = ma45.pct_change()
        Diffma10 = ma50.pct_change()
        Diffma11 = ma55.pct_change()
        Diffma12 = ma60.pct_change()
        Diffma13 = ma65.pct_change()
        Diffma14 = ma70.pct_change()
        Diffma15 = ma75.pct_change()
        Diffma16 = ma80.pct_change()
        Diffma17 = ma85.pct_change()
        Diffma18 = ma90.pct_change()

        # //Positive difference values
        Diffma1P = ma05.pct_change() >= 0
        Diffma2P = ma10.pct_change() >= 0
        Diffma3P = ma15.pct_change() >= 0
        Diffma4P = ma20.pct_change() >= 0
        Diffma5P = ma25.pct_change() >= 0
        Diffma6P = ma30.pct_change() >= 0
        Diffma7P = ma35.pct_change() >= 0
        Diffma8P = ma40.pct_change() >= 0
        Diffma9P = ma45.pct_change() >= 0
        Diffma10P = ma50.pct_change() >= 0
        Diffma11P = ma55.pct_change() >= 0
        Diffma12P = ma60.pct_change() >= 0
        Diffma13P = ma65.pct_change() >= 0
        Diffma14P = ma70.pct_change() >= 0
        Diffma15P = ma75.pct_change() >= 0
        Diffma16P = ma80.pct_change() >= 0
        Diffma17P = ma85.pct_change() >= 0
        Diffma18P = ma90.pct_change() >= 0

        # //Negative difference values
        Diffma1N = ma05.pct_change() < 0
        Diffma2N = ma10.pct_change() < 0
        Diffma3N = ma15.pct_change() < 0
        Diffma4N = ma20.pct_change() < 0
        Diffma5N = ma25.pct_change() < 0
        Diffma6N = ma30.pct_change() < 0
        Diffma7N = ma35.pct_change() < 0
        Diffma8N = ma40.pct_change() < 0
        Diffma9N = ma45.pct_change() < 0
        Diffma10N = ma50.pct_change() < 0
        Diffma11N = ma55.pct_change() < 0
        Diffma12N = ma60.pct_change() < 0
        Diffma13N = ma65.pct_change() < 0
        Diffma14N = ma70.pct_change() < 0
        Diffma15N = ma75.pct_change() < 0
        Diffma16N = ma80.pct_change() < 0
        Diffma17N = ma85.pct_change() < 0
        Diffma18N = ma90.pct_change() < 0

        # // Reverse enginnered color boolean's
        Lime1 = Diffma1P & greenMA_1
        Red1 = Diffma1N & redMA_1
        Maroon1 = Diffma1N & greenMA_1
        Green1 = Diffma1P & redMA_1

        Lime2 = Diffma2P & greenMA_2
        Red2 = Diffma2N & redMA_2
        Maroon2 = Diffma2N & greenMA_2
        Green2 = Diffma2P & redMA_2

        Lime3 = Diffma3P & greenMA_3
        Red3 = Diffma3N & redMA_3
        Maroon3 = Diffma3N & greenMA_3
        Green3 = Diffma3P & redMA_3

        Lime4 = Diffma4P & greenMA_4
        Red4 = Diffma4N & redMA_4
        Maroon4 = Diffma4N & greenMA_4
        Green4 = Diffma4P & redMA_4

        Lime5 = Diffma5P & greenMA_5
        Red5 = Diffma5N & redMA_5
        Maroon5 = Diffma5N & greenMA_5
        Green5 = Diffma5P & redMA_5

        Lime6 = Diffma6P & greenMA_6
        Red6 = Diffma6N & redMA_6
        Maroon6 = Diffma6N & greenMA_6
        Green6 = Diffma6P & redMA_6

        Lime7 = Diffma7P & greenMA_7
        Red7 = Diffma7N & redMA_7
        Maroon7 = Diffma7N & greenMA_7
        Green7 = Diffma7P & redMA_7

        Lime8 = Diffma8P & greenMA_8
        Red8 = Diffma8N & redMA_8
        Maroon8 = Diffma8N & greenMA_8
        Green8 = Diffma8P & redMA_8

        Lime9 = Diffma9P & greenMA_9
        Red9 = Diffma9N & redMA_9
        Maroon9 = Diffma9N & greenMA_9
        Green9 = Diffma9P & redMA_9

        Lime10 = Diffma10P & greenMA_10
        Red10 = Diffma10N & redMA_10
        Maroon10 = Diffma10N & greenMA_10
        Green10 = Diffma10P & redMA_10

        Lime11 = Diffma11P & greenMA_11
        Red11 = Diffma11N & redMA_11
        Maroon11 = Diffma11N & greenMA_11
        Green11 = Diffma11P & redMA_11

        Lime12 = Diffma12P & greenMA_12
        Red12 = Diffma12N & redMA_12
        Maroon12 = Diffma12N & greenMA_12
        Green12 = Diffma12P & redMA_12

        Lime13 = Diffma13P & greenMA_13
        Red13 = Diffma13N & redMA_13
        Maroon13 = Diffma13N & greenMA_13
        Green13 = Diffma13P & redMA_13

        Lime14 = Diffma14P & greenMA_14
        Red14 = Diffma14N & redMA_14
        Maroon14 = Diffma14N & greenMA_14
        Green14 = Diffma14P & redMA_14

        Lime15 = Diffma15P & greenMA_15
        Red15 = Diffma15N & redMA_15
        Maroon15 = Diffma15N & greenMA_15
        Green15 = Diffma15P & redMA_15

        Lime16 = Diffma16P & greenMA_16
        Red16 = Diffma16N & redMA_16
        Maroon16 = Diffma16N & greenMA_16
        Green16 = Diffma16P & redMA_16

        Lime17 = Diffma17P & greenMA_17
        Red17 = Diffma17N & redMA_17
        Maroon17 = Diffma17N & greenMA_17
        Green17 = Diffma17P & redMA_17

        Lime18 = Diffma18P & greenMA_18
        Red18 = Diffma18N & redMA_18
        Maroon18 = Diffma18N & greenMA_18
        Green18 = Diffma18P & redMA_18

        # //combination of Lime/Red conditions when true
        lime_Long = Lime1 & Lime2 & Lime3 & Lime4 & Lime5 & Lime6 & Lime7 & Lime8 & Lime9 & Lime10 & Lime11 & Lime12 & Lime13 & Lime14 & Lime15 & Lime16 & Lime17 & Lime18
        red_Short = Red1 & Red2 & Red3 & Red4 & Red5 & Red6 & Red7 & Red8 & Red9 & Red10 & Red12 & Red12 & Red13 & Red14 & Red15 & Red16 & Red17 & Red18
        maroon_Short = Maroon1 & Maroon2 & Maroon3 & Maroon4 & Maroon5 & Maroon6 & Maroon7 & Maroon8 & Maroon9 & Maroon10 & Maroon11 & Maroon12 & Maroon13 & Maroon14 & Maroon15 & Maroon16 & Maroon17 & Maroon18
        green_Long = Green1 & Green2 & Green3 & Green4 & Green5 & Green6 & Green7 & Green8 & Green9 & Green10 & Green11 & Green12 & Green13 & Green14 & Green15 & Green16 & Green17 & Green18
        # //rsistoch values /
        r = self.close.rsi(14)
        # s = self.stoch(14, 1, 3)
        rP = r <= 55
        rM = r >= 75
        noENGL = self.close.shift() > self.open.shift()
        long_signal = lime_Long & pmarp <= 1.  # & (~noENGL) & (~rP)
        short_signal = red_Short & pmarp >= 99.  # & noENGL & (r <= 45)
        exitlong_signal = pmarp >= 99.
        exitshort_signal = pmarp <= 1.
        return pmarp, signal_ma, long_signal, short_signal, exitlong_signal, exitshort_signal


class Heikin_Ashi_Supertrend(BtIndicator):
    """https://cn.tradingview.com/script/9z16eauD-Heikin-Ashi-Supertrend/"""
    params = dict(supertrendAtrPeriod=10, supertrendAtrMultiplier=2.7,)
    overlap = True

    def next(self):
        ha = self.ha()
        haOpen, haHigh, haLow, haClose = ha.open, ha.high, ha.low, ha.close
        haTrueRange = self.atr(self.params.supertrendAtrPeriod)
        haSupertrendUp = ((haHigh + haLow) / 2) - \
            (self.params.supertrendAtrMultiplier * haTrueRange)
        haSupertrendDown = ((haHigh + haLow) / 2) + \
            (self.params.supertrendAtrMultiplier * haTrueRange)
        size = self.V
        trendingUp = np.zeros(size)
        trendingDown = np.zeros(size)
        direction = np.ones(size)
        supertrend = np.zeros(size)
        long_signal = np.zeros(size)
        short_signal = np.zeros(size)
        haOpen, haHigh, haLow, haClose = haOpen.values, haHigh.values, haLow.values, haClose.values
        for i in range(self.params.supertrendAtrPeriod+1, size):
            trendingUp[i] = haClose[i-1] > trendingUp[i -
                                                      1] and max(haSupertrendUp[i], trendingUp[i-1]) or haSupertrendUp[i]
            trendingDown[i] = haClose[i-1] < trendingDown[i-1] and min(
                haSupertrendDown[i], trendingDown[i-1]) or haSupertrendDown[i]
            direction[i] = haClose[i] > trendingDown[i -
                                                     1] and 1. or (haClose[i] < trendingUp[i-1] and -1. or direction[i-1])
            supertrend[i] = direction[i] == 1. and trendingUp[i] or trendingDown[i]
            long_signal[i] = (direction[i-1] < 0) and (direction[i] > 0)
            short_signal[i] = (direction[i-1] > 0) and (direction[i] < 0)

        return supertrend, long_signal, short_signal


class Parabolic_SAR_Heikin_Ashi_MTF_Candle_Scalper(BtIndicator):
    """https://cn.tradingview.com/script/U7uneSDz-Parabolic-SAR-Heikin-Ashi-MTF-Candle-Scalper/"""

    def next(self):
        data = self.ha().psar(acceleration=0.05)
        # print(type(data))
        return data[['psarl', 'psars']]


class Half_Trend_HeikinAshi(BtIndicator):
    """https://cn.tradingview.com/script/oWabuCUL-Half-Trend-HeikinAshi-BigBeluga/"""
    params = dict(amp=5,)
    overlap = dict(hl_t=True, trend_st=False)

    def next(self):
        # // ＣＡＬＣＵＬＡＴＩＯＮＳ
        # // Moving average of close prices
        closeMA = self.close.sma(self.params.amp).values
        # // Highest high over the period
        highestHigh = self.tqfunc.hhv(self.params.amp).values
        # // Lowest low over the period
        lowestLow = self.tqfunc.llv(self.params.amp).values

        # // Initialize half trend on the first bar
        # if barstate.isfirst
        hl_t = self.close.values

        # // Update half trend value based on conditions
        for i in range(self.params.amp+1, self.V):
            if closeMA[i] < hl_t[i-1] and highestHigh[i-1] < hl_t[i-1]:
                hl_t[i] = highestHigh[i-1]
            elif closeMA[i] > hl_t[i-1] and lowestLow[i-1] > hl_t[i-1]:
                hl_t[i] = lowestLow[i-1]
            else:
                hl_t[i] = hl_t[i-1]
        ha = self.ha()
        # // Calculate trend strength
        trend_st = (ha.high - ha.low) / (ha.high - ha.low).stdev(200)
        trend_st = (20.*trend_st).clip(upper=100.)
        return hl_t, trend_st


class Z_Score_Heikin_Ashi_Transformed(BtIndicator):
    """https://cn.tradingview.com/script/MFW8vsmU-Z-Score-Heikin-Ashi-Transformed/"""
    overlap = False
    # lines = ["open", "high", "low", "close"]
    # category = 'candles'
    overlap = False
    params = dict(length=21)

    def next(self):
        def z_score(open: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray):
            return (open[-1]-open.mean())/open.std(), \
                (high[-1]-high.mean())/high.std(), \
                (low[-1]-low.mean())/low.std(), \
                (close[-1]-close.mean())/close.std()

        def z_score(df):
            open, high, low, close = df[:, 0], df[:, 1], df[:, 2], df[:, 3]
            return (open[-1]-open.mean())/open.std(), \
                (high[-1]-high.mean())/high.std(), \
                (low[-1]-low.mean())/low.std(), \
                (close[-1]-close.mean())/close.std()
        data = self.ha()
        # data["open"] = (self.open+self.close)/2.
        data["close"] = self.ohlc4()
        data = data.loc[:, FILED.OHLC].rolling_apply(
            z_score, self.params.length, lines=FILED.OHLC.tolist(), n_jobs=4)
        return data


class Heikin_Ashi(BtIndicator):
    """"""
    lines = ["open", "high", "low", "close", "zlma"]
    category = 'candles'
    overlap = False

    def next(self):
        data = self.ha()
        zlma = self.zlma()
        return data, zlma


class RedK_Slow_Smooth_Average(BtIndicator):
    """https://cn.tradingview.com/script/4nmGHAnL-RedK-Slow-Smooth-Average-RSS-WMA/"""
    params = dict(alpha=15)

    def f_LazyLine(self, _data: IndSeries, _length: int):
        w1 = 0
        w2 = 0
        w3 = 0
        L1 = 0.0
        L2 = 0.0
        L3 = 0.0
        w = _length / 3.
        if _length > 2:
            w2 = int(w)
            w1 = int((_length-w2)/2)
            w3 = int((_length-w2)/2)

            L1 = _data.wma(w1)
            L2 = L1.wma(w2)
            L3 = L2.wma(w3)
        else:
            L3 = _data
        return L3.values

    def next(self):
        LL = self.f_LazyLine(self.close, self.params.alpha)
        return LL


class Delta_RSI_Oscillator_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/oIHW2xG2-Delta-RSI-Oscillator-Strategy/"""
    params = dict(window=21, degree=2, rmse_thrs=10, signalLength=9, rsi_l=21)
    overlap = False
    # // ---Subroutines---

    def matrix_get(self, _A: np.ndarray, _i, _j, _nrows):
        # // Get the value of the element of an implied 2d matrix
        # //input:
        # // _A :: array: pseudo 2d matrix _A = [[column_0],[column_1],...,[column_(n-1)]]
        # // _i :: integer: row number
        # // _j :: integer: column number
        # // _nrows :: integer: number of rows in the implied 2d matrix
        # array.get(_A,_i+_nrows*_j)
        return _A[_i+_nrows*_j]

    def matrix_set(self, _A: np.ndarray, _value, _i, _j, _nrows):
        # // Set a value to the element of an implied 2d matrix
        # //input:
        # // _A :: array, changed on output: pseudo 2d matrix _A = [[column_0],[column_1],...,[column_(n-1)]]
        # // _value :: float: the new value to be set
        # // _i :: integer: row number
        # // _j :: integer: column number
        # // _nrows :: integer: number of rows in the implied 2d matrix
        # array.set(_A,_i+_nrows*_j,_value)
        _A[_i+_nrows*_j] = _value

    def vnorm(self, _X, _n):
        # //Square norm of vector _X with size _n
        _norm = 0.0
        for i in range(_n-1):
            _norm += math.pow(_X[i], 2)
        return math.sqrt(_norm)

    def qr_diag(self, _A, _nrows, _ncolumns):
        # //QR Decomposition with Modified Gram-Schmidt Algorithm (Column-Oriented)
        # // input:
        # // _A :: array: pseudo 2d matrix _A = [[column_0],[column_1],...,[column_(n-1)]]
        # // _nrows :: integer: number of rows in _A
        # // _ncolumns :: integer: number of columns in _A
        # // output:
        # // _Q: unitary matrix, implied dimenstions _nrows x _ncolumns
        # // _R: upper triangular matrix, implied dimansions _ncolumns x _ncolumns
        # var _Q = array.new_float(_nrows*_ncolumns,0)
        # var _R = array.new_float(_ncolumns*_ncolumns,0)
        # var _a = array.new_float(_nrows,0)
        # var _q = array.new_float(_nrows,0)
        # float _r = 0.0
        # float _aux = 0.0
        _Q = np.zeros(_nrows*_ncolumns, dtype=np.float32)
        _R = np.zeros(_ncolumns*_ncolumns, dtype=np.float32)
        _a = np.zeros(_nrows, dtype=np.float32)
        _q = np.zeros(_nrows, dtype=np.float32)
        _r = 0.0
        _aux = 0.0
        # //get first column of _A and its norm:
        # for i = 0 to _nrows-1
        #     array.set(_a,i,matrix_get(_A,i,0,_nrows))
        for i in range(_nrows):
            _a[i] = self.matrix_get(_A, i, 0, _nrows)
        _r = self.vnorm(_a, _nrows)
        # //assign first diagonal element of R and first column of Q
        self.matrix_set(_R, _r, 0, 0, _ncolumns)
        for i in range(_nrows):
            self.matrix_set(_Q, _a[i]/_r, i, 0, _nrows)
        if _ncolumns != 1:
            # //repeat for the rest of the columns
            for k in range(1, _ncolumns):
                for i in range(_nrows):
                    _a[i] = self.matrix_get(_A, i, k, _nrows)
                for j in range(k):
                    # //get R_jk as scalar product of Q_j column and A_k column:
                    _r = 0.
                    for i in range(_nrows):
                        _r += self.matrix_get(_Q, i, j, _nrows)*_a[i]
                    self.matrix_set(_R, _r, j, k, _ncolumns)
                    # //update vector _a
                    for i in range(_nrows):
                        _aux = _a[i] - _r*self.matrix_get(_Q, i, j, _nrows)
                        _a[i] = _aux
                # //get diagonal R_kk and Q_k column
                _r = self.vnorm(_a, _nrows)
                self.matrix_set(_R, _r, k, k, _ncolumns)
                for i in range(_nrows):
                    self.matrix_set(_Q, _a[i]/_r, i, k, _nrows)
        return _Q, _R

    def transpose(self, _A, _nrows, _ncolumns):
        # // Transpose an implied 2d matrix
        # // input:
        # // _A :: array: pseudo 2d matrix _A = [[column_0],[column_1],...,[column_(n-1)]]
        # // _nrows :: integer: number of rows in _A
        # // _ncolumns :: integer: number of columns in _A
        # // output:
        # // _AT :: array: pseudo 2d matrix with implied dimensions: _ncolums x _nrows
        _AT = np.zeros(_nrows*_ncolumns)
        for i in range(_nrows):
            for j in range(_ncolumns):
                self.matrix_set(_AT, self.matrix_get(
                    _A, i, j, _nrows), j, i, _ncolumns)
        return _AT

    def multiply(self, _A, _B, _nrowsA, _ncolumnsA, _ncolumnsB):
        # // Calculate scalar product of two matrices
        # // input:
        # // _A :: array: pseudo 2d matrix
        # // _B :: array: pseudo 2d matrix
        # // _nrowsA :: integer: number of rows in _A
        # // _ncolumnsA :: integer: number of columns in _A
        # // _ncolumnsB :: integer: number of columns in _B
        # // output:
        # // _C:: array: pseudo 2d matrix with implied dimensions _nrowsA x _ncolumnsB
        _C = np.zeros(_nrowsA*_ncolumnsB)
        _nrowsB = _ncolumnsA
        elementC = 0.0
        for i in range(_nrowsA):
            for j in range(_ncolumnsB):
                elementC = 0.
                for k in range(_ncolumnsA):
                    elementC += self.matrix_get(_A, i, k, _nrowsA) *\
                        self.matrix_get(
                        _B, k, j, _nrowsB)
                self.matrix_set(_C, elementC, i, j, _nrowsA)
        return _C

    def pinv(self, _A, _nrows, _ncolumns):
        # //Pseudoinverse of matrix _A calculated using QR decomposition
        # // Input:
        # // _A:: array: implied as a (_nrows x _ncolumns) matrix _A = [[column_0],[column_1],...,[column_(_ncolumns-1)]]
        # // Output:
        # // _Ainv:: array implied as a (_ncolumns x _nrows) matrix _A = [[row_0],[row_1],...,[row_(_nrows-1)]]
        # // ----
        # // First find the QR factorization of A: A = QR,
        # // where R is upper triangular matrix.
        # // Then _Ainv = R^-1*Q^T.
        # // ----
        _Q, _R = self.qr_diag(_A, _nrows, _ncolumns)
        _QT = self.transpose(_Q, _nrows, _ncolumns)
        # // Calculate Rinv:
        _Rinv = np.zeros(_ncolumns*_ncolumns, dtype=np.float32)
        _r = 0.0
        self.matrix_set(_Rinv, 1/self.matrix_get(_R, 0,
                        0, _ncolumns), 0, 0, _ncolumns)
        if _ncolumns != 1:
            for j in range(1, _ncolumns):
                for i in range(j):
                    _r = 0.0
                    for k in range(i, j):
                        _r += self.matrix_get(_Rinv, i, k, _ncolumns) *\
                            self.matrix_get(
                            _R, k, j, _ncolumns)
                    self.matrix_set(_Rinv, _r, i, j, _ncolumns)
                for k in range(j):
                    self.matrix_set(_Rinv, -self.matrix_get(_Rinv, k, j, _ncolumns) /
                                    self.matrix_get(_R, j, j, _ncolumns), k, j, _ncolumns)
                self.matrix_set(_Rinv, 1./self.matrix_get(_R,
                                j, j, _ncolumns), j, j, _ncolumns)

        _Ainv = self.multiply(_Rinv, _QT, _ncolumns, _ncolumns, _nrows)
        return _Ainv

    def norm_rmse(self, _x: np.ndarray, _xhat: np.ndarray) -> float:
        # // Root Mean Square Error normalized to the sample mean
        # // _x.   :: array float, original data
        # // _xhat :: array float, model estimate
        # // output
        # // _nrmse:: float
        _nrmse = 0.0
        if len(_x) != len(_xhat):
            _nrmse = np.nan
        else:
            _N = len(_x)
            _mse = 0.0
            for i in range(_N):
                _mse += pow(_x[i] - _xhat[i], 2)/_N
            _xmean = sum(_x)/_N
            _nrmse = math.sqrt(_mse) / _xmean
        return _nrmse

    def dros_diff(self, _src: np.ndarray, _degree: int = 2):
        # // Polynomial differentiator
        # // input:
        # // _src:: input IndSeries
        # // _window:: integer: wigth of the moving lookback window
        # // _degree:: integer: degree of fitting polynomial
        # // output:
        # // _diff :: IndSeries: time derivative
        # // _nrmse:: float: normalized root mean square error
        # //
        # // Vandermonde matrix with implied dimensions (window x degree+1)
        # // Linear form: J = [ [z]^0, [z]^1, ... [z]^degree], with z = [ (1-window)/2 to (window-1)/2 ]
        # var _J = array.new_float(_window*(_degree+1),0)
        _window = _src.size
        _J = np.zeros(_window*(_degree+1), dtype=np.float32)
        # for i = 0 to _window-1
        #     for j = 0 to _degree
        #         matrix_set(_J,pow(i,j),i,j,_window)
        for i in range(_window):
            for j in range(_degree+1):
                self.matrix_set(_J, pow(i, j), i, j, _window)
        # // Vector of raw datapoints:
        # var _Y_raw = array.new_float(_window,na)
        _Y_raw = np.full(_window, np.nan)
        # for j = 0 to _window-1
        #     array.set(_Y_raw,j,_src[_window-1-j])
        for j in range(_window):
            _Y_raw[j] = _src[_window-1-j]
        # // Calculate polynomial coefficients which minimize the loss function
        _C = self.pinv(_J, _window, _degree+1)
        _a_coef = self.multiply(_C, _Y_raw, _degree+1, _window, 1)
        # // For first derivative, approximate the last point (i.e. z=window-1) by
        _diff = 0.0
        for i in range(1, _degree+1):
            _diff += i*_a_coef[i]*pow(_window-1, i-1)
        # // Calculates data estimate (needed for rmse)
        _Y_hat = self.multiply(_J, _a_coef, _window, _degree+1, 1)
        _nrmse = self.norm_rmse(_Y_raw, _Y_hat)
        return _diff, _nrmse

    def next(self):
        # src = self.close.rsi(self.params.rsi_l)
        src = self.close.ebsw()
        dros = src.rolling_apply(partial(
            self.dros_diff, _degree=self.params.degree), self.params.window, lines=["drsi", "nrmse"])
        drsi, nrmse = dros.drsi, dros.nrmse
        signalline = drsi.ema(self.params.signalLength)
        # long_signal = drsi.cross_up(signalline)
        # short_signal = signalline.cross_down(drsi)
        long_signal = (drsi > drsi.shift()) & (
            drsi.shift() < drsi.shift(2)) & (drsi.shift() < 0.0)
        short_signal = (drsi < drsi.shift()) & (
            drsi.shift() > drsi.shift(2)) & (drsi.shift() > 0.0)
        # crossup = drsi.cross_up(0.0)
        # crossdw = drsi.cross_down(0.0)
        return drsi, signalline, long_signal, short_signal


class VAMA_Volume_Adjusted_Moving_Average_Function(BtIndicator):
    """https://cn.tradingview.com/script/WzvigqK7-VAMA-Volume-Adjusted-Moving-Average-Function/"""

    params = dict(
        nvb=0,  # // N volume bars used as sample to calculate average volume, 0 equals all bars
        scF="close",  # // Richard Arms' default is close
        lnF=13,  # // Richard Arms' default is 55
        fvF=0.67,  # // Richard Arms' default is 0.67
        rlF=True,  # // rule must meet volume requirements even if N bars' v2vi ratios has to exceed VAMA Length to do it
        scS="close",  # // Richard Arms' default is close
        lnS=55,  # // Richard Arms' default is 55
        fvS=0.67,  # // Richard Arms' default is 0.67
        rlS=True,  # // rule must meet volume requirements even if N bars' v2vi ratios has to exceed VAMA Length to do it
    )
    overlap = True

    def vama_func(self, _src: np.ndarray, volume: np.ndarray, _fct=0.67, _rul=True, _nvb=0):
        _len = _src.size
        if _nvb <= 0:
            tvb = np.arange(1, _len+1)
            tvs = np.zeros(_len)
            for i in range(_len):
                tvs[i] = np.sum(volume[:i+1])
        else:
            tvb = max(_nvb, 1)
            tvs = np.zeros(_len)
            for i in range(_len):
                start_idx = max(0, i - _nvb + 1)
                tvs[i] = np.sum(volume[start_idx:i+1])

        v2i = volume / ((tvs / tvb) * _fct)
        wtd = _src*v2i
        nmb = 0
        wtdSumB = 0.0
        v2iSumB = 0.0
        for i in range(1, _len+1):
            wtdSumB += wtd[i-1]
            v2iSumB += v2i[i-1]
            if v2iSumB >= _len:
                break
            nmb += 1
        return (wtdSumB - (v2iSumB - _len) * _src[-1]) / _len

    def next(self):
        vama = self.loc[:, ["close", "volume"]].rolling_apply(partial(
            self.vama_func, _fct=self.params.fvS, _rul=self.params.rlS, _nvb=self.params.nvb), self.params.lnS)
        return vama


class Separated_Moving_Average_evo(BtIndicator):
    """https://cn.tradingview.com/script/66gsofEJ-Separated-Moving-Average-evo/"""
    params = dict(ma="sma", length=20)
    overlap = True

    def next(self):
        up = self.loc[:, ["open", "close"]].rolling_apply(
            lambda x, y: y[0] if x[0] < y[0] else np.nan, 1).ffill()
        dn = self.loc[:, ["open", "close"]].rolling_apply(
            lambda x, y: y[0] if x[0] > y[0] else np.nan, 1).ffill()
        up = up.ma(self.params.ma, self.params.length)
        dn = dn.ma(self.params.ma, self.params.length)
        return up, dn


class Dynamic_Swing_Anchored_VWAP(BtIndicator):
    """https://cn.tradingview.com/script/SxgyrEde-Dynamic-Swing-Anchored-VWAP-Zeiierman/"""
    params = dict(
        prd=50,
        baseAPT=20,
        useAdapt=False,
        volBias=10.0,
        xx=2,
        atrLen=50
    )

    def highestbars(self):
        size = self.V
        prd = self.params.prd
        high = self.high
        phl = np.full(size, np.nan)
        ph = np.full(size, np.nan)
        for i in range(prd-1, size):
            phl[i] = high[i+1-prd:i+1].argmax()
            ph[i] = high[i+1-prd:i+1].max()
        return pd.Series(phl), pd.Series(ph)

    def lowestbars(self):
        size = self.V
        prd = self.params.prd
        low = self.low
        pll = np.full(size, np.nan)
        pl = np.full(size, np.nan)
        for i in range(prd-1, size):
            pll[i] = low[i+1-prd:i+1].argmin()
            pl[i] = low[i+1-prd:i+1].min()
        return pd.Series(pll), pd.Series(pl)

    def alphaFromAPT(self, apt: float) -> float:
        decay = math.exp(-math.log(2.0) / max(1.0, apt))
        return 1.0 - decay

    def vwap_func(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray):
        p = (high+low+close)*volume/3.
        return p.sum()/volume.sum()

    def calculate_dynamic_swing_vwap(self, df, prd=50, baseAPT=20, useAdapt=False, volBias=10.0, atrLen=50):
        """
        计算动态摆动锚定VWAP（分绿线/红线，对应上涨/下跌趋势）

        参数：
        df:        pandas DataFrame，包含 ['open','high','low','close','volume']
        prd:       摆动周期（识别摆动高低点的窗口）
        baseAPT:   基础价格跟踪周期
        useAdapt:  是否启用波动率自适应APT
        volBias:   波动率对APT的影响系数
        atrLen:    ATR计算窗口

        返回：
        df:        新增列 ['ph','pl','phL','plL','dir','apt_IndSeries','vwap_green','vwap_red']
        """
        phL, ph = self.highestbars()
        plL, pl = self.lowestbars()
        dir = self.Where(phL > plL, 1., -1)
        df['ph'] = ph.bfill()  # 保持最近的摆动高点价格
        df['pl'] = pl.bfill()  # 保持最近的摆动低点价格
        df['phL'] = phL.bfill().astype(int)        # 前向填充
        df['plL'] = plL.bfill().astype(int)        # 前向填充
        df['dir'] = dir.bfill()  # 初始值填充（默认上涨）
        # ---------------------- 1. 识别摆动高点（PH）和摆动低点（PL） ---------------------- #
        # # 摆动高点mask：当前K线是最近prd根K线的最高点
        # ph_mask = df['high'].rolling(prd).apply(
        #     lambda x: x.idxmax() == len(x)-1  # 窗口内最大值出现在最后一根K线（当前K线）
        # ).astype(bool)

        # # 摆动低点mask：当前K线是最近prd根K线的最低点
        # pl_mask = df['low'].rolling(prd).apply(
        #     lambda x: x.idxmin() == len(x)-1
        # ).astype(bool)

        # # 填充摆动点价格（ph/pl）和位置（phL/plL）：前向填充最近的摆动点
        # df['ph'] = np.where(ph_mask, df['high'], np.nan)
        # df['ph'] = df['ph'].ffill()  # 保持最近的摆动高点价格

        # df['pl'] = np.where(pl_mask, df['low'], np.nan)
        # df['pl'] = df['pl'].ffill()  # 保持最近的摆动低点价格

        # df['phL'] = np.where(ph_mask, df.index, np.nan)  # 摆动高点的K线索引
        # df['phL'] = df['phL'].ffill().astype(int)        # 前向填充

        # df['plL'] = np.where(pl_mask, df.index, np.nan)  # 摆动低点的K线索引
        # df['plL'] = df['plL'].ffill().astype(int)        # 前向填充

        # # ---------------------- 2. 计算趋势方向（dir） ---------------------- #
        # df['dir'] = np.where(df['phL'] > df['plL'],
        #                      1, -1)  # 1=上涨（绿线），-1=下跌（红线）
        # df['dir'] = df['dir'].fillna(method='ffill').fillna(1)  # 初始值填充（默认上涨）

        # ---------------------- 3. 自适应APT计算（基于ATR波动率） ---------------------- #
        # 计算真实波动TR
        tr = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                np.abs(df['high'] - df['close'].shift()),
                np.abs(df['low'] - df['close'].shift())
            )
        ).fillna(0)  # 首根K线TR为high-low

        # 计算ATR（TR的RMA，窗口atrLen）
        from pandas_ta import rma
        atr = rma(df["close"], atrLen)  # talib.RMA(tr, atrLen)
        atr = atr.replace(0, np.nan).ffill().fillna(0)  # 处理初始NaN

        # 计算ATR的滚动平均（ATR的RMA，窗口atrLen）
        atr_avg = rma(atr, atrLen)  # talib.RMA(atr, atrLen)
        atr_avg = atr_avg.replace(0, 1)  # 避免除零

        # 波动率比例
        ratio = atr / atr_avg

        # 计算APT
        if useAdapt:
            apt_raw = baseAPT / (ratio ** volBias)
        else:
            apt_raw = np.full(len(df), baseAPT)

        apt_clamped = np.clip(apt_raw, 5, 300)  # 限制范围
        df['apt_IndSeries'] = np.round(apt_clamped).astype(int)

        # EWMA衰减因子计算函数：alpha = 1 - exp(-ln(2)/apt)
        def alpha_from_apt(apt):
            apt = np.maximum(1.0, apt)  # 避免除以0
            decay = np.exp(-np.log(2.0) / apt)
            return 1.0 - decay

        # ---------------------- 4. 分趋势计算VWAP（绿线：上涨；红线：下跌） ---------------------- #
        # 初始化状态变量
        p_green = None  # 绿线：累积价量（hlc3*volume，EWMA加权）
        vol_green = None  # 绿线：累积成交量（EWMA加权）
        p_red = None     # 红线：累积价量
        vol_red = None     # 红线：累积成交量
        prev_dir = None    # 上一根K线的趋势
        vwap_green = []    # 绿线VWAP结果
        vwap_red = []      # 红线VWAP结果

        for i in df.index:
            current_dir = df['dir'][i]
            apt_i = df['apt_IndSeries'][i]
            hlc3 = (df['high'][i] + df['low'][i] + df['close'][i]) / 3
            vol_i = df['volume'][i]
            pxv_i = hlc3 * vol_i  # 当前K线的价量

            # 初始状态：首根K线，初始化趋势
            if prev_dir is None:
                prev_dir = current_dir
                if current_dir == 1:
                    p_green, vol_green = pxv_i, vol_i
                    vwap_green.append(
                        p_green / vol_green if vol_green != 0 else np.nan)
                    vwap_red.append(np.nan)
                else:
                    p_red, vol_red = pxv_i, vol_i
                    vwap_red.append(
                        p_red / vol_red if vol_red != 0 else np.nan)
                    vwap_green.append(np.nan)
                continue

            # 趋势变化：重置锚点，回溯计算
            if current_dir != prev_dir:
                # 确定新锚点：上涨趋势锚定摆动低点（plL），下跌趋势锚定摆动高点（phL）
                if current_dir == 1:
                    anchor_idx = df['plL'][i]
                else:
                    anchor_idx = df['phL'][i]

                # 回溯计算：从锚点到当前K线的VWAP（逐根计算EWMA）
                # 初始化锚点的价量和成交量
                hlc3_anchor = (df['high'][anchor_idx] + df['low']
                               [anchor_idx] + df['close'][anchor_idx]) / 3
                p_anchor = hlc3_anchor * df['volume'][anchor_idx]
                vol_anchor = df['volume'][anchor_idx]

                p = p_anchor
                vol = vol_anchor
                # 遍历锚点之后的K线（含当前K线）
                for j in range(anchor_idx + 1, i + 1):
                    apt_j = df['apt_IndSeries'][j]
                    alpha_j = alpha_from_apt(apt_j)
                    hlc3_j = (df['high'][j] + df['low']
                              [j] + df['close'][j]) / 3
                    pxv_j = hlc3_j * df['volume'][j]
                    vol_j = df['volume'][j]

                    p = (1 - alpha_j) * p + alpha_j * pxv_j
                    vol = (1 - alpha_j) * vol + alpha_j * vol_j

                # 更新对应趋势的状态
                if current_dir == 1:
                    p_green, vol_green = p, vol
                    vwap_green.append(
                        p_green / vol_green if vol_green != 0 else np.nan)
                    vwap_red.append(np.nan)
                else:
                    p_red, vol_red = p, vol
                    vwap_red.append(
                        p_red / vol_red if vol_red != 0 else np.nan)
                    vwap_green.append(np.nan)

                prev_dir = current_dir
            else:
                # 趋势不变：用当前APT更新EWMA
                alpha = alpha_from_apt(apt_i)
                if current_dir == 1:
                    if p_green is None:  # 首次进入上涨趋势
                        p_green, vol_green = pxv_i, vol_i
                    else:
                        p_green = (1 - alpha) * p_green + alpha * pxv_i
                        vol_green = (1 - alpha) * vol_green + alpha * vol_i
                    vwap_green.append(
                        p_green / vol_green if vol_green != 0 else np.nan)
                    vwap_red.append(np.nan)
                else:
                    if p_red is None:  # 首次进入下跌趋势
                        p_red, vol_red = pxv_i, vol_i
                    else:
                        p_red = (1 - alpha) * p_red + alpha * pxv_i
                        vol_red = (1 - alpha) * vol_red + alpha * vol_i
                    vwap_red.append(
                        p_red / vol_red if vol_red != 0 else np.nan)
                    vwap_green.append(np.nan)

        # 结果赋值
        df['vwap_green'] = vwap_green
        df['vwap_red'] = vwap_red

        return df.loc[:, ['vwap_green', 'vwap_red']]

    def next(self):
        vwap = self.loc[:, FILED.HLCV].rolling_apply(
            self.vwap_func, self.params.prd)
        df = self.calculate_dynamic_swing_vwap(self.copy()[FILED.OHLCV])
        return df
        # phL,ph=self.highestbars()
        # plL,pl=self.lowestbars()
        # dir = self.Where(phL >plL, 1., -1)
        # atr = self.atr(self.params.atrLen)
        # atrAvg = atr.rma(self.params.atrLen)  # ta.rma(atr, self.params.atrLen)
        # ratio = atr.ZeroDivision(atrAvg)
        # # ratio  = atrAvg > 0 ? atr / atrAvg : 1.0
        # ratio = ratio.where(atrAvg > 0, 1.)
        # size = self.V
        # aptRaw: IndSeries = self.params.baseAPT / ratio.apply(lambda x: math.pow(
        #     x, self.params.volBias)) if self.params.useAdapt else pd.Series(np.full(size,self.params.baseAPT))
        # # math.max(5.0, math.min(300.0, aptRaw))
        # aptClamped = aptRaw.clip(5., 300.)
        # aptSeries = aptClamped.astype(np.int32)

        # vwap = np.full(size, np.nan)
        # p   = self.hlc3() * self.volume
        # p=p.values
        # volume = self.volume.values
        # vol=self.volume.values
        # up = np.full(size, np.nan)
        # dn = np.full(size, np.nan)

        # # from pandas_ta import vwap
        # # // ~~ Main {
        # for i in range(self.params.prd, size):
        #     _dir=dir[i]
        #     if _dir != dir[i-1]:
        #         # x = _dir > 0 and plL[i] or phL[i]
        #         y = _dir > 0 and pl[i] or ph[i]
        #         vwap[i]=y
        #         # prev = _dir > 0 and ph[i-1] or pl[i-1]

        #         # barsback = i - x
        #         # p[i]=y * volume[barsback]
        #         # vol[i]= volume[barsback]
        #         # vap =  p[i]/vol[i]

        #         # pxv=
        #         # for j in range(barsback,-1):
        #         #     apt_i = aptSeries[j]
        #         #     alpha = self.alphaFromAPT(apt_i)

        #         #     pxv = hlc3[j] * volume[j]
        #         #     v_i = volume[j]

        #         #     p := (1.0 - alpha) * p + alpha * pxv
        #         #     vol := (1.0 - alpha) * vol + alpha * v_i
        #         #     vappe = vol > 0 ? p / vol: na

        #         #     vwap.points.push(chart.point.from_index(b - j, vappe))

        #         # vwap.poly := polyline.new(vwap.points, false, false, line_color=dir > 0 ? R: S, line_width=xx)

        #     else
        #         apt_0 = aptSeries[i]
        #         alpha = self.alphaFromAPT(apt_0)

        #         pxv = p[i]#hlc3 * volume
        #         v0 = volume[i]

        #         p := (1.0 - alpha) * p + alpha * pxv
        #         vol := (1.0 - alpha) * vol + alpha * v0
        #         vap = vol > 0 ? p / vol: na

        #         vwap.poly.delete()
        #         vwap.points.push(chart.point.from_index(b, vap))
        #         vwap.poly := polyline.new(vwap.points, false, false, line_color=dir > 0 ? R: S, line_width=xx)
        #     //~~}


class ATR_Rope(BtIndicator):
    """https://cn.tradingview.com/script/YYrxRhi9-ATR-Rope/"""
    params = dict(
        len=60,
        multi=1.5,
    )
    overlap = dict(rope=True, dir=False, up=True, dn=True)

    def rope_smoother(self, _src: IndSeries):
        size = self.V
        _threshold = self.atr(self.params.len)*self.params.multi
        _threshold = _threshold.values
        _src = _src.values
        lennan = len(_threshold[np.isnan(_threshold)])
        _rope = np.full(size, np.nan)
        _rope[lennan] = _src[lennan]
        dir = np.zeros(size)
        for i in range(lennan+1, size):
            _move = _src[i]-_rope[i-1]
            _rope[i] = _rope[i-1] + \
                max(abs(_move) - _threshold[i], 0) * np.sign(_move)
            dir[i] = _rope[i] > _rope[i -
                                      1] and 1 or ((_rope[i] < _rope[i-1]) and -1 or dir[i-1])
        return _rope, dir, _rope+_threshold, _rope-_threshold

    def next(self):
        rope, dir, up, dn = self.rope_smoother(self.close)
        return rope, dir, up, dn


class BERLIN_MAX_1V5(BtIndicator):
    """https://cn.tradingview.com/script/h2Jl4eE8-BERLIN-MAX-1V-5/"""

    params = dict(ut_a=3., ut_c=17)
    overlap = True

    def next(self):
        xATR = self.atr(self.params.ut_c)
        nLoss = self.params.ut_a * xATR
        nLoss = nLoss.values
        src = self.close.values
        size = self.V
        nanlen = len(nLoss[np.isnan(nLoss)])
        xATRTrailingStop = np.zeros(size)
        for i in range(nanlen+1, size):
            if src[i] > xATRTrailingStop[i-1] and src[i-1] > xATRTrailingStop[i-1]:
                xATRTrailingStop[i] = max(
                    xATRTrailingStop[i-1], src[i] - nLoss[i])
            elif src[i] < xATRTrailingStop[i-1] and src[i-1] < xATRTrailingStop[i-1]:
                xATRTrailingStop[i] = min(
                    xATRTrailingStop[i-1], src[i] + nLoss[i])
            else:
                xATRTrailingStop[i] = src[i] > xATRTrailingStop[i -
                                                                1] and src[i] - nLoss[i] or src[i] + nLoss[i]
        ema = IndSeries(self.close.ewm(span=1, adjust=False).mean())
        # from pandas_ta import ema
        long_signal = (src > xATRTrailingStop) & (
            ema.cross_up(xATRTrailingStop))
        short_signal = (src < xATRTrailingStop) & (
            ema.cross_down(xATRTrailingStop))
        return xATRTrailingStop, long_signal, short_signal


class Neural_Network_Buy_and_Sell_Signals(BtIndicator):
    """https://cn.tradingview.com/script/pn25rKHZ-Neural-Network-Buy-and-Sell-Signals/"""

    def next(self):
        # // ============================================================================
        # // BUY SIGNAL
        # // ============================================================================
        signal_trigger_mult = 1.7
        buy_signal_length = 2
        buy_signal_use_close = False
        buy_signal_atr = self.atr(buy_signal_length) * signal_trigger_mult
        signalClose = self.close
        signalHigh = self.high
        signalLow = self.low
        buy_signal_longStop: IndSeries = (signalClose.tqfunc.hhv(
            buy_signal_length) if buy_signal_use_close else signalHigh.tqfunc.llv(buy_signal_length)) - buy_signal_atr
        buy_signal_longStopPrev = buy_signal_longStop.shift()
        buy_signal_longStop_ = buy_signal_longStop.tqfunc.max(
            buy_signal_longStopPrev)
        # print(buy_signal_longStop_, type(buy_signal_longStop_))
        buy_signal_longStop = buy_signal_longStop_.where(
            (signalClose.shift() > buy_signal_longStopPrev), buy_signal_longStop)

        # signalClose[1] > buy_signal_longStopPrev ? math.max(buy_signal_longStop, buy_signal_longStopPrev) : buy_signal_longStop

        buy_signal_shortStop: IndSeries = signalClose.tqfunc.llv(
            buy_signal_length) if buy_signal_use_close else signalLow.tqfunc.llv(buy_signal_length) + buy_signal_atr
        buy_signal_shortStopPrev = buy_signal_shortStop.shift()
        buy_signal_shortStop = buy_signal_shortStop.tqfunc.min(buy_signal_shortStopPrev).where(
            signalClose.shift() < buy_signal_shortStopPrev, other=buy_signal_shortStop)

        # signalClose[1] < buy_signal_shortStopPrev ? math.min(buy_signal_shortStop, buy_signal_shortStopPrev) : buy_signal_shortStop
        size = self.V
        buy_signal_dir = IndSeries(np.ones(size))
        other = IndSeries(-np.ones(size))
        other = other.where(
            signalClose < buy_signal_longStopPrev, buy_signal_dir)
        buy_signal_dir: IndSeries = buy_signal_dir.where(
            signalClose > buy_signal_shortStopPrev, other)
        # signalClose > buy_signal_shortStopPrev ? 1 : signalClose < buy_signal_longStopPrev ? -1 : buy_signal_dir
        long_signal = buy_signal_dir == 1
        long_signal &= buy_signal_dir.shift() == -1
        # // ============================================================================
        # // BUY ATR STOP
        # // ============================================================================

        # buy_atr_atr = ta.atr(buy_atr_length) * stop_atr_mult

        # buy_atr_longStop := (buy_atr_use_close ? ta.highest(signalClose, buy_atr_length) : ta.highest(signalHigh, buy_atr_length)) - buy_atr_atr
        # buy_atr_longStopPrev = nz(buy_atr_longStop[1], buy_atr_longStop)
        # buy_atr_longStop := signalClose[1] > buy_atr_longStopPrev ? math.max(buy_atr_longStop, buy_atr_longStopPrev) : buy_atr_longStop

        # buy_atr_shortStop = (buy_atr_use_close ? ta.lowest(signalClose, buy_atr_length) : ta.lowest(signalLow, buy_atr_length)) + buy_atr_atr
        # buy_atr_shortStopPrev = nz(buy_atr_shortStop[1], buy_atr_shortStop)
        # buy_atr_shortStop := signalClose[1] < buy_atr_shortStopPrev ? math.min(buy_atr_shortStop, buy_atr_shortStopPrev) : buy_atr_shortStop

        # var int buy_atr_dir = 1
        # buy_atr_dir := signalClose > buy_atr_shortStopPrev ? 1 : signalClose < buy_atr_longStopPrev ? -1 : buy_atr_dir

        # buy_atr_sellSignal := buy_atr_dir == -1 and buy_atr_dir[1] == 1

        # // ============================================================================
        # // SELL SIGNAL
        # // ============================================================================
        sell_signal_length = 2
        sell_signal_use_close = False
        sell_signal_atr = self.atr(sell_signal_length) * signal_trigger_mult

        sell_signal_longStop: IndSeries = signalClose.tqfunc.hhv(
            sell_signal_length) if sell_signal_use_close else signalHigh.tqfunc.hhv(sell_signal_length) - sell_signal_atr
        sell_signal_longStopPrev = sell_signal_longStop.shift()
        sell_signal_longStop = sell_signal_longStop.tqfunc.max(sell_signal_longStopPrev).where(
            signalClose.shift() > sell_signal_longStopPrev, other=sell_signal_longStop)
        # signalClose[1] > sell_signal_longStopPrev ? math.max(sell_signal_longStop, sell_signal_longStopPrev) : sell_signal_longStop

        sell_signal_shortStop: IndSeries = signalClose.tqfunc.llv(
            sell_signal_length) if sell_signal_use_close else signalLow.tqfunc.llv(sell_signal_length) + sell_signal_atr
        sell_signal_shortStopPrev = sell_signal_shortStop.shift()
        sell_signal_shortStop = sell_signal_shortStop.tqfunc.min(sell_signal_shortStopPrev).where(
            signalClose.shift() < sell_signal_shortStopPrev, other=sell_signal_shortStop)
        # signalClose[1] < sell_signal_shortStopPrev ? math.min(sell_signal_shortStop, sell_signal_shortStopPrev) : sell_signal_shortStop

        sell_signal_dir = IndSeries(np.ones(size))
        other = IndSeries(-np.ones(size))
        other = other.where(
            signalClose < sell_signal_longStopPrev, sell_signal_dir)
        # ? 1 : signalClose < sell_signal_longStopPrev ? -1 : sell_signal_dir
        sell_signal_dir = sell_signal_dir.where(
            signalClose > sell_signal_shortStopPrev, other)

        short_signal = sell_signal_dir == -1
        short_signal &= sell_signal_dir.shift() == 1

        # // ============================================================================
        # // SELL ATR STOP
        # // ============================================================================

        # sell_atr_atr = ta.atr(sell_atr_length) * stop_atr_mult

        # sell_atr_longStop = (sell_atr_use_close ? ta.highest(signalClose, sell_atr_length) : ta.highest(signalHigh, sell_atr_length)) - sell_atr_atr
        # sell_atr_longStopPrev = nz(sell_atr_longStop[1], sell_atr_longStop)
        # sell_atr_longStop := signalClose[1] > sell_atr_longStopPrev ? math.max(sell_atr_longStop, sell_atr_longStopPrev) : sell_atr_longStop

        # sell_atr_shortStop := (sell_atr_use_close ? ta.lowest(signalClose, sell_atr_length) : ta.lowest(signalLow, sell_atr_length)) + sell_atr_atr
        # sell_atr_shortStopPrev = nz(sell_atr_shortStop[1], sell_atr_shortStop)
        # sell_atr_shortStop := signalClose[1] < sell_atr_shortStopPrev ? math.min(sell_atr_shortStop, sell_atr_shortStopPrev) : sell_atr_shortStop

        # var int sell_atr_dir = 1
        # sell_atr_dir := signalClose > sell_atr_shortStopPrev ? 1 : signalClose < sell_atr_longStopPrev ? -1 : sell_atr_dir

        # sell_atr_buySignal := sell_atr_dir == 1 and sell_atr_dir[1] == -1
        return long_signal, short_signal


class RSI_ADX_Long_Short_Strategy_v6(BtIndicator):
    """https://cn.tradingview.com/script/tJsfSYvm-RSI-ADX-Long-Short-Strategy-v6-Manual-ADX/"""

    params = dict(
        rsiLength=8,
        adxLength=20,
        adxThreshold=14.0,
    )
    overlap = dict(dx=False, adxVal=False)

    def next(self):
        rsiVal = self.close.rsi(self.params.rsiLength)
        upMove = self.high.diff()
        downMove = -self.low.diff()

        plusDM: IndSeries = upMove.where(
            (upMove > downMove) & (upMove > 0), 0.)
        minusDM: IndSeries = downMove.where(
            (downMove > upMove) & (downMove > 0), 0.)

        tr = self.true_range().rma(self.params.adxLength)
        plusDI = 100 * plusDM.rma(self.params.adxLength) / tr
        minusDI = 100 * minusDM.rma(self.params.adxLength) / tr
        dx = 100 * (plusDI - minusDI).abs() / (plusDI + minusDI)
        adxVal = dx.rma(self.params.adxLength)

        long_signal = rsiVal.cross_up(70) & (adxVal > self.params.adxThreshold)
        short_signal = rsiVal.cross_down(
            30) & (adxVal > self.params.adxThreshold)

        exitlong_signal = rsiVal.cross_down(30)
        exitshort_signal = rsiVal.cross_up(70)

        return dx, adxVal, long_signal, short_signal, exitlong_signal, exitshort_signal


class Momentum_EMABand(BtIndicator):
    """https://cn.tradingview.com/script/5C4tnnd3-Momentum-EMABand/"""

    params = dict(emaLength=9, atrLength=10, factor=3,
                  adxLength=14, adxThreshold=20)
    overlap = dict(supertrend=True, dx=False, adx=False)

    def next(self):
        # // EMA Calculations
        emaHigh = self.high.ema(self.params.emaLength)
        emaLow = self.low.ema(self.params.emaLength)
        # // Supertrend Calculation
        atr = self.atr(self.params.atrLength)
        upperBand = (self.high + self.low) / 2 + self.params.factor * atr
        lowerBand = (self.high + self.low) / 2 - self.params.factor * atr
        upperBand = upperBand.values
        lowerBand = lowerBand.values
        nanlen = len(upperBand[pd.isnull(upperBand)])
        size = self.V
        supertrend = np.full(size, np.nan)
        supertrend[nanlen] = lowerBand[nanlen]
        upTrend = np.ones(size)
        close = self.close.values
        for i in range(nanlen+1, size):
            if (close[i] > supertrend[i-i]):
                supertrend[i] = max(lowerBand[i], supertrend[i-1])
            else:
                supertrend[i] = min(upperBand[i], supertrend[i-1])
                upTrend[i] = -1

        # // Manual ADX Calculation
        upMove = self.high.diff()
        downMove = -self.low.diff()
        plusDM = upMove.where((upMove > downMove) & (upMove > 0), 0.)
        minusDM = downMove.where((downMove > upMove) & (downMove > 0), 0.)

        smoothedTR = self.true_range().rma(self.params.adxLength)
        smoothedPlusDM = plusDM.rma(self.params.adxLength)
        smoothedMinusDM = minusDM.rma(self.params.adxLength)

        plusDI = (smoothedPlusDM / smoothedTR) * 100
        minusDI = (smoothedMinusDM / smoothedTR) * 100
        dx = (plusDI - minusDI).abs() / (plusDI + minusDI) * 100
        adx = dx.rma(self.params.adxLength)

        # isWeakTrend = (adx < self.params.adxThreshold)
        # priceMid = (emaHigh + emaLow) / 2
        # bandBuffer = (emaHigh - emaLow) * 0.5

        # isPriceInsideBand = (close <= emaHigh + bandBuffer) and (close >= emaLow - bandBuffer)
        # rangeCondition = isWeakTrend and isPriceInsideBand

        return supertrend, dx, adx


class BBTrend(BtIndicator):
    """"""
    params = dict(
        shortLengthInput=20,
        longLengthInput=50,
        stdDevMultInput=2.0,
    )

    def next(self):
        shortLower, shortMiddle, shortUpper, *_ = self.close.bbands(
            self.params.shortLengthInput, self.params.stdDevMultInput).to_lines()
        longLower, longMiddle,  longUpper, *_ = self.close.bbands(
            self.params.longLengthInput,  self.params.stdDevMultInput).to_lines()
        BBTrend = ((shortLower - longLower).abs() -
                   (shortUpper - longUpper).abs()) / shortMiddle * 100.
        self.kst()
        return BBTrend


class RCI_Ribbon(BtIndicator):
    """RCI（Relative Change Index，相对变化指数）是一种衡量价格变化动量的技术指标，核心通过计算价格变化的排名来反映趋势强度，取值通常在 - 100 到 100 之间（或 0 到 100，取决于公式细节）。结合你的代码中 “Ribbon（多周期带）” 的设计（短、中、长三个周期），它的用法可以从以下几个方面展开：
        1. 判断超买与超卖
        RCI 的数值通常有明确的高低区间参考（不同版本可能略有差异）：
        当 RCI 值高于 70（或 80） 时，通常认为价格进入超买区域，意味着短期上涨动量过强，可能面临回调风险；
        当 RCI 值低于 30（或 20） 时，通常认为价格进入超卖区域，意味着短期下跌动量过强，可能接近反弹节点。
        你的代码中计算结果包含66.66666667，接近超买区间，可能提示短期上涨动能较强。
        2. 识别趋势方向
        RCI 的数值方向和变化可以反映趋势强度：
        若 RCI 在正值区域且持续上升（如从 30 升至 70），说明价格上涨动量在增强，对应上升趋势；
        若 RCI 在负值区域且持续下降（如从 - 30 跌至 - 70），说明价格下跌动量在增强，对应下降趋势；
        若 RCI 在 0 附近波动，说明价格处于横盘整理，动量较弱。
        3. 背离信号（关键用法）
        RCI 与价格走势的背离是重要的反转信号：
        顶背离：价格创新高，但 RCI 未创新高（甚至下降），说明上涨动能不足，可能即将回调；
        底背离：价格创新低，但 RCI 未创新低（甚至上升），说明下跌动能衰竭，可能即将反弹。
        4. 多周期 Ribbon 组合（你的代码设计）
        你的代码用了shortLength（短）、middleLength（中）、longLength（长）三个周期的 RCI，形成 “RCI 带”，这种设计的核心是通过多周期共振确认信号：
        趋势确认：当短、中、长周期 RCI同向排列（如都在正值区域且向上），说明趋势（上涨）一致性强，信号更可靠；
        趋势转折：短周期 RCI 率先转向，中、长周期随后跟进（如短周期从超买区回落，中长周期也开始下降），可能提示趋势即将反转；
        过滤杂波：单周期 RCI 可能频繁发出假信号，多周期组合（如短周期需突破中周期，且中周期方向与长周期一致）可过滤噪音，提高信号质量。
        5. 与其他指标配合使用
        RCI 单独使用时可能存在局限性，通常结合以下指标增强效果：
        均线：当 RCI 发出买入信号（如底背离），且价格站稳均线（如 20 日均线），可确认上涨信号；
        成交量：RCI 上涨时若成交量放大，说明资金跟进，趋势更可持续；
        MACD/RSI：多指标共振（如 RCI 底背离 + MACD 金叉）可降低误判概率。
        注意事项
        RCI 的周期参数（如 10、30、50）需根据品种特性调整（如短线用短周期，长线用长周期）；
        超买超卖区间不是绝对阈值，需结合历史走势动态判断（如波动率高的品种可能超买区间上移至 80）；
        横盘行情中 RCI 可能频繁震荡，此时信号可靠性低，需减少操作。
        总之，RCI 的核心是通过 “价格变化的相对强度” 反映动量，多周期组合（Ribbon）则进一步提升了趋势判断的稳定性，适合趋势跟踪或反转信号确认场景。"""
    params = dict(
        shortLength=10,
        middleLength=30,
        longLength=50,
    )

    def rci_window(self, window_np: np.ndarray) -> float:
        """使用numpy进行内部计算的RCI窗口函数"""
        period = len(window_np) - 1  # 周期 = 窗口大小 - 1
        if period < 2 or pd.isnull(window_np).any():
            return np.nan

        # 提取价格变化序列（窗口内的原始变化值）
        changes = window_np[:period]

        # ===================== 核心修复：修正排名逻辑 =====================
        # RCI排名逻辑：比较每两个时间点的价格变化，统计上升趋势的数量
        # 对时间序列中的每个i<j，判断changes[j] > changes[i]的次数
        rank_sum = 0
        for j in range(1, period):
            for i in range(j):
                if changes[j] > changes[i]:
                    rank_sum += 1  # 上升趋势计数
                elif changes[j] < changes[i]:
                    rank_sum += 0  # 下降趋势不计数
                else:
                    rank_sum += 0.5  # 相等时计0.5（处理平局）
        # =================================================================

        # 计算RCI公式
        denominator = period * (period - 1) / 2  # 总比较次数：n*(n-1)/2
        if denominator == 0:
            return np.nan

        # RCI = (1 - 2 * 上升趋势占比) * 100
        # 等价于原始公式，但更直观体现趋势占比
        rci_value = (1 - 2 * (rank_sum / denominator)) * 100
        return rci_value

    def next(self):
        rci1: IndSeries = self.close.diff(self.params.shortLength).rolling_apply(
            self.rci_window, self.params.shortLength+1)
        rci2 = self.close.diff(self.params.middleLength).rolling_apply(
            self.rci_window, self.params.middleLength+1)
        rci3 = self.close.diff(self.params.longLength).rolling_apply(
            self.rci_window, self.params.longLength+1)

        return rci1, rci2, rci3


class McGinley_Dynamic(BtIndicator):
    params = dict(length=14)
    overlap = True

    def next(self):
        length = self.params.length
        source = self.close.values
        size = self.V
        mg = np.zeros(size)
        ema = self.close.ema(length).values
        mg[length] = ema[length]
        for i in range(length+1, size):
            mg[i] = mg[i-1] + (source[i] - mg[i-1]) / \
                (length * np.power(source[i]/mg[i-1], 4))
        return mg


class Trend_Volume_Confluence_Indicator(BtIndicator):
    """https://cn.tradingview.com/script/kg9htYsF-Trend-Volume-Confluence-Indicator/"""
    params = dict(
        bars_fib=100,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        vol_period=20,
        vol_factor=1.3
    )
    overlap = True

    def next(self):

        # // --- SMA 50/200 ---
        sma50 = self.close.sma(50)
        sma200 = self.close.sma(200)

        # // --- Fibonacci Level (automated line, 50%) ---
        fib_high = self.high.tqfunc.hhv(self.params.bars_fib)
        fib_low = self.low.tqfunc.llv(self.params.bars_fib)
        fib_500 = fib_high - (fib_high - fib_low) * 0.5
        return sma50, sma200, fib_500


class MomentumSync_PSAR_Strategy(BtIndicator):
    """https://cn.tradingview.com/script/FSnDSqnr-MomentumSync-PSAR-RSI-ADX-Filtered-3-Tier-Exit-Strategy/"""

    params = dict(psarStart=0.02, psarMax=0.2, rsiPeriod=14, adxLen=14)

    def next(self):
        psar = self.SAR(self.params.psarStart, self.params.psarMax)
        adx, diplus, diminus = self.adx(
            self.params.adxLen, self.params.adxLen).to_lines()
        rsi = self.close.rsi(self.params.rsiPeriod)

        psarBullishFlip = (psar < self.close) & (
            psar.shift() > self.close.shift()) & (psar.shift(2) > self.close.shift(2))
        rsiAdxOK = (rsi > 40) & (adx > 18)
        long_signal = psarBullishFlip & rsiAdxOK

        psarBullishFlip_ = (psar > self.close) & (
            psar.shift() < self.close.shift()) & (psar.shift(2) < self.close.shift(2))
        rsiAdxOK_ = (rsi < 60) & (adx > 18)
        short_signal = psarBullishFlip_ & rsiAdxOK_
        return psar, long_signal, short_signal


class Twin_Range_Filter(BtIndicator):
    """https://cn.tradingview.com/script/57i9oK2t-Twin-Range-Filter-Buy-Sell-Signals/"""

    params = dict(
        per1=127,
        mult1=1.6,
        per2=155,
        mult2=2.0,
    )
    overlap = True

    def smoothrng(self, x: IndSeries, t: int, m: float):
        wper = t * 2 - 1
        avrng = x.diff().abs().ema(t)
        return avrng.ema(wper) * m

    def rngfilt(self, x: IndSeries, r: IndSeries):
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


class Kairi_Trend_Oscillator(BtIndicator):
    """https://cn.tradingview.com/script/wWwFNH7h-Kairi-Trend-Oscillator-T3-T69/"""
    params = dict(inp_ma_len=29)

    def next(self):
        inp_ma_len = self.params.inp_ma_len
        ha = self.ha()
        src_val = ha.close
        ma_val = (src_val.tqfunc.hhv(inp_ma_len) +
                  src_val.tqfunc.llv(inp_ma_len))/2
        kairi_val = ((src_val - ma_val) / src_val) * 100

        fallingSlope = src_val.linreg(
            inp_ma_len) < src_val.linreg(inp_ma_len, 1)
        risingSlope = src_val.linreg(
            inp_ma_len) > src_val.linreg(inp_ma_len, 1)

        trend_status = self.ifs(
            fallingSlope, -1, risingSlope, 1, other=0)

        return kairi_val
