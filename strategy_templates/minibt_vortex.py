# -*- coding: utf-8 -*-
"""【minibt·趋势跟踪】涡旋指标 Vortex

策略思想: 正向/负向涡旋运动比较:+VI 上穿 -VI 且大于阈值做多,反之做空。
参考: shinnytech Vortex 策略教程
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

class Vortex(BtIndicator):
    params = dict(VI_PERIOD=14, VI_THRESHOLD=1.0)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        tr = self.true_range()
        plus_vm = (self.high - self.low.shift()).abs()
        minus_vm = (self.low - self.high.shift()).abs()
        tr_sum = tr.rolling(self.params.VI_PERIOD).sum()
        plus_vi = plus_vm.rolling(self.params.VI_PERIOD).sum() / tr_sum
        minus_vi = minus_vm.rolling(self.params.VI_PERIOD).sum() / tr_sum
        long_signal = plus_vi.cross_up(minus_vi)
        long_signal &= plus_vi > self.params.VI_THRESHOLD
        short_signal = minus_vi.cross_up(plus_vi)
        short_signal &= minus_vi > self.params.VI_THRESHOLD
        return plus_vi, minus_vi, long_signal, short_signal


class VortexStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.vortex = Vortex(self.kline)

    def next(self):
        if not self.kline.position:
            if self.vortex.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.vortex.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.vortex.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.vortex.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(VortexStrategy)
    run_and_report(bt, DF)
