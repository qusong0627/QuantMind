# -*- coding: utf-8 -*-
"""【minibt·趋势跟踪】HullMA 双周期

策略思想: 短周期 Hull 均线上穿/下穿长周期 Hull 均线,且价格在长周期均线同侧才开仓。
参考: shinnytech Hull 策略教程
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

class Hull(BtIndicator):
    params = dict(LONG_HMA_PERIOD=30, SHORT_HMA_PERIOD=5)
    isplot = dict(long_signal=False, short_signal=False)
    overlap = True

    def _hma(self, period):
        half_period = period // 2
        sqrt_period = int(np.sqrt(period))
        wma1 = self.close.wma(half_period)
        wma2 = self.close.wma(period)
        raw_hma = 2 * wma1 - wma2
        return raw_hma.wma(sqrt_period)

    def next(self):
        short_hma = self._hma(self.params.SHORT_HMA_PERIOD)
        long_hma = self._hma(self.params.LONG_HMA_PERIOD)
        long_signal = short_hma.cross_up(long_hma)
        long_signal &= self.close > long_hma
        short_signal = short_hma.cross_down(long_hma)
        short_signal &= self.close < long_hma
        return short_hma, long_hma, long_signal, short_signal


class HullStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.hull = Hull(self.kline)

    def next(self):
        if not self.kline.position:
            if self.hull.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.hull.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.hull.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.hull.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(HullStrategy)
    run_and_report(bt, DF)
