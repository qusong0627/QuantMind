# -*- coding: utf-8 -*-
"""【minibt·均值回归】Z-Score 统计套利式回归

策略思想: 价格 Z-Score 低于 -1.8 做多、高于 +1.8 做空,回到 ±0.4 区间或击穿 2.5 止损离场。
参考: shinnytech Z-Score 策略教程
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

class Zscore(BtIndicator):
    params = dict(WINDOW=14, ENTRY=1.8, EXIT=0.4, STOP=2.5)
    isplot = dict(long_signal=False, short_signal=False,
                  exitlong_signal=False, exitshort_signal=False)

    def next(self):
        z = self.close.zscore(self.params.WINDOW)
        long_signal = z.cross_down(-self.params.ENTRY)
        short_signal = z.cross_up(self.params.ENTRY)
        back_in_band = (-self.params.EXIT <= z) & (z <= self.params.EXIT)
        stop_hit = (z >= self.params.STOP) | (z <= -self.params.STOP)
        exitlong_signal = back_in_band | (z >= self.params.STOP)
        exitshort_signal = back_in_band | (z <= -self.params.STOP)
        return z, long_signal, short_signal, exitlong_signal, exitshort_signal


class ZscoreStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.zscore = Zscore(self.kline)

    def next(self):
        if not self.kline.position:
            if self.zscore.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.zscore.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.zscore.exitlong_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.zscore.exitshort_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(ZscoreStrategy)
    run_and_report(bt, DF)
