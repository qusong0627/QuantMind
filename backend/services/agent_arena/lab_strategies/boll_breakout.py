"""
@pyne

布林带突破：收盘上穿上轨开仓，回落中轨离场。
"""
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("布林带突破", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(length=input(20, "周期"), mult=input(2.0, "带宽倍数")):
    mid = ta.sma(close, length)
    dev = mult * ta.stdev(close, length)
    upper = mid + dev
    buy: Series[bool] = ta.crossover(close, upper)
    if buy:
        strategy.entry('L', strategy.long)
    elif close < mid:
        strategy.close('L')
