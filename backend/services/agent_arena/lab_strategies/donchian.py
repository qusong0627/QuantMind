"""
@pyne

唐奇安通道突破（海龟简化版）：突破 N 日新高开仓，跌破 M 日新低离场。
"""
from pynecore.lib import close, high, input, low, script, strategy, ta
from pynecore.types import Series


@script.strategy("唐奇安通道突破", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(entry_len=input(20, "入场周期"), exit_len=input(10, "离场周期")):
    hh: Series[float] = ta.highest(high, entry_len)
    ll: Series[float] = ta.lowest(low, exit_len)
    buy: Series[bool] = high >= hh[1]
    if buy:
        strategy.entry('L', strategy.long)
    elif low <= ll[1]:
        strategy.close('L')
