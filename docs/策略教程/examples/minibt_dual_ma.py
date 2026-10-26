# -*- coding: utf-8 -*-
"""示例 3：minibt 双均线（AI-IDE / 回测中心 / 模拟盘）。

A 股回测口径提示：
- 信号当根 K 线收盘价成交；无 T+1、涨跌停、整手、ST 约束；
- 手续费必须显式设置；
- 反手需要显式两步（持多时 sell 只平不开空）。
"""
from minibt import Bt, Strategy

from backend.shared.minibt_qdb import load_daily
from backend.shared.minibt_result import run_and_report

SYMBOL = "600036"
START = "20240101"
END = "20261026"
SIZE = 1000
COMMISSION = 0.00025

DF = load_daily(SYMBOL, START, END)


class DualMA(Strategy):
    params = dict(fast=10, slow=20)

    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION
        self.ma_fast = self.kline.close.sma(self.params.fast)
        self.ma_slow = self.kline.close.sma(self.params.slow)
        self.golden = self.ma_fast.cross_up(self.ma_slow)
        self.death = self.ma_fast.cross_down(self.ma_slow)

    def next(self):
        if not self.kline.position:
            if self.golden.new:
                self.kline.buy(size=SIZE)
            elif self.death.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.death.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.golden.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(DualMA)
    run_and_report(bt, DF)
