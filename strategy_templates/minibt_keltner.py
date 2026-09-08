# -*- coding: utf-8 -*-
"""【minibt·通道突破】肯特纳通道突破

策略思想: EMA±动态 ATR 通道:价格突破上轨且短期 EMA 在上做多,跌破下轨做空;通道宽度随趋势强度自适应。
参考: shinnytech Keltner Channel 策略教程
数据: QuantDB 前复权日线(代码可传 '600036' / 'SH600036' / '600036.SH')

A 股口径提示:
- 信号当根 K 线收盘价成交, 引擎无 T+1/涨跌停/整手约束, 回测与实盘存在口径差
- 反手逻辑含做空(A 股现货无裸卖空, 实盘请忽略 short_signal 只做多)
- 手续费为双边佣金比例, 未含印花税与滑点
- 想加自动止损可把 buy/sell 换成: self.kline.buy(size=SIZE, stop=BtStop.SegmentationTracking)
"""
import numpy as np

from minibt import Bt, BtIndicator, BtStop, Strategy
from backend.shared.minibt_qdb import load_daily
from backend.shared.minibt_result import run_and_report

SYMBOL = '600036'      # 标的代码
START = '20220101'     # 起始日(前几个月为指标预热, 不产生信号)
END = '20250630'       # 结束日
SIZE = 1000            # 每次交易股数(100 的整数倍)
COMMISSION = 0.00025   # 双边佣金比例(万 2.5)

DF = load_daily(SYMBOL, START, END)

class KeltnerChannel(BtIndicator):
    params = dict(EMA_PERIOD=8, ATR_PERIOD=7, ATR_MULT=1.5,
                  SHORT_EMA_PERIOD=5, TREND=0.5)
    overlap = True

    def next(self):
        ema = self.close.ema(self.params.EMA_PERIOD)
        ema_short = self.close.ema(self.params.SHORT_EMA_PERIOD)
        trend_direction = self.zeros().ifs(ema_short > ema, 1., ema_short < ema, -1.)
        trend_strength = (ema_short - ema).abs() / self.close * 100.
        tr = self.true_range()
        atr = tr.sma(self.params.ATR_PERIOD)
        # 强趋势时收窄通道(乘数 ×0.8),弱趋势时用标准乘数
        dynamic_mult = self.full(self.params.ATR_MULT).mask(
            trend_strength > self.params.TREND, self.params.ATR_MULT * 0.8)
        upper_band = ema + dynamic_mult * atr
        lower_band = ema - dynamic_mult * atr
        long_signal = self.close > upper_band
        long_signal &= trend_direction > 0.
        short_signal = self.close < lower_band
        short_signal &= trend_direction < 0.
        return upper_band, lower_band, long_signal, short_signal


class KeltnerChannelStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.kc = KeltnerChannel(self.kline)

    def next(self):
        if not self.kline.position:
            if self.kc.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.kc.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.kc.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.kc.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(KeltnerChannelStrategy)
    run_and_report(bt, DF)
