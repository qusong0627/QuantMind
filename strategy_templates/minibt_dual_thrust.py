# -*- coding: utf-8 -*-
"""【minibt·通道突破】Dual Thrust 区间突破

策略思想: 近 N 日最高价差构成动态上下轨:收盘上穿买入轨做多,下穿卖出轨做空;经典日内策略的日线版。
参考: shinnytech Dual Thrust 教程(A股日线化,纯 pandas 实现,不依赖 tqsdk)
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

class DualThrust(BtIndicator):
    params = dict(NDAY=14, K1=0.6, K2=0.6)
    isplot = dict(long_signal=False, short_signal=False)
    overlap = True

    def next(self):
        HH = self.high.rolling(self.params.NDAY).max().shift()
        HC = self.close.rolling(self.params.NDAY).max().shift()
        LC = self.close.rolling(self.params.NDAY).min().shift()
        LL = self.low.rolling(self.params.NDAY).min().shift()
        # range = max(HH-LC, HC-LL)
        rng = (HH - LC).clip(lower=HC - LL)
        buy_line = self.open + rng * self.params.K1   # 上轨
        sell_line = self.open - rng * self.params.K2  # 下轨
        long_signal = self.close.cross_up(buy_line)
        short_signal = self.close.cross_down(sell_line)
        return buy_line, sell_line, long_signal, short_signal


class DualThrustStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.dt = DualThrust(self.kline)

    def next(self):
        if not self.kline.position:
            if self.dt.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.dt.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.dt.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.dt.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(DualThrustStrategy)
    run_and_report(bt, DF)
