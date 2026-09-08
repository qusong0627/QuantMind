# -*- coding: utf-8 -*-
"""【minibt·均值回归】RSI 超买超卖反转

策略思想: RSI 从超卖区上穿阈值做多,从超买区下穿做空;震荡市反转思路。
参考: shinnytech RSI 策略教程
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

class RSI(BtIndicator):
    params = dict(RSI_PERIOD=6, OVERBOUGHT=65, OVERSOLD=35)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        rsi = self.close.rsi(self.params.RSI_PERIOD)
        long_signal = rsi.cross_up(self.params.OVERSOLD)
        short_signal = rsi.cross_down(self.params.OVERBOUGHT)
        return rsi, long_signal, short_signal


class RSIStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.rsi = RSI(self.kline)

    def next(self):
        if not self.kline.position:
            if self.rsi.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.rsi.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.rsi.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.rsi.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(RSIStrategy)
    run_and_report(bt, DF)
