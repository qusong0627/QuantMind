# -*- coding: utf-8 -*-
"""【minibt·趋势跟踪】双均线趋势跟踪

策略思想: 短期均线上穿长期均线(金叉)做多,下穿(死叉)做空;经典趋势跟随入门策略。
参考: minibt 快速上手示例
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

class DualMAStrategy(Strategy):
    params = dict(fast=10, slow=20)

    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.ma_fast = self.kline.close.sma(self.params.fast)
        self.ma_slow = self.kline.close.sma(self.params.slow)
        self.long_signal = self.ma_fast.cross_up(self.ma_slow)
        self.short_signal = self.ma_fast.cross_down(self.ma_slow)

    def next(self):
        if not self.kline.position:
            if self.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(DualMAStrategy)
    run_and_report(bt, DF)
