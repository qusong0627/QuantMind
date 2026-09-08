# -*- coding: utf-8 -*-
"""【minibt·量价动量】VPT 量价趋势

策略思想: 量价趋势累计线(VPT)在均线上方且放量上涨做多,放量下跌做空。
参考: shinnytech VPT 策略教程
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

class VPT(BtIndicator):
    params = dict(MA_PERIOD=14, VOLUME_MULT=1.5)
    isplot = dict(long_signal=False, short_signal=False)

    def next(self):
        size = len(self.volume)
        vpt = self.volume.copy(True)
        volume = self.volume.values
        close = self.close.values
        diff = self.close.diff().values
        for i in range(1, size):
            vpt[i] = vpt[i - 1] + volume[i] * diff[i] / close[i - 1]
        vpt_ma = vpt.sma(self.params.MA_PERIOD)
        avg_volume = self.volume.sma(self.params.MA_PERIOD)

        long_signal = vpt > vpt_ma
        long_signal &= self.close.diff() > 0.
        long_signal &= self.volume > (self.params.VOLUME_MULT * avg_volume)
        short_signal = vpt < vpt_ma
        short_signal &= self.close.diff() < 0.
        short_signal &= self.volume > (self.params.VOLUME_MULT * avg_volume)
        return vpt, vpt_ma, long_signal, short_signal


class VPTStrategy(Strategy):
    def __init__(self):
        self.kline = self.get_kline(DF, duration_seconds=86400)
        self.percent_commission = COMMISSION  # ⚠️ 必须放在 get_kline 之后
        self.vpt = VPT(self.kline)

    def next(self):
        if not self.kline.position:
            if self.vpt.long_signal.new:
                self.kline.buy(size=SIZE)
            elif self.vpt.short_signal.new:
                self.kline.sell(size=SIZE)
        elif self.kline.position > 0 and self.vpt.short_signal.new:
            self.kline.sell(size=SIZE)
        elif self.kline.position < 0 and self.vpt.long_signal.new:
            self.kline.buy(size=SIZE)


if __name__ == "__main__":
    bt = Bt(auto=False)
    bt.addstrategy(VPTStrategy)
    run_and_report(bt, DF)
