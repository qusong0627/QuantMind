"""
@pyne

RSI 反转：RSI 跌破超卖线买入，升破超买线离场。
"""
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("RSI 反转", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(length=input(14, "RSI 周期"), oversold=input(30, "超卖"), overbought=input(70, "超买")):
    r = ta.rsi(close, length)
    buy: Series[bool] = ta.crossover(r, oversold)
    sell: Series[bool] = ta.crossover(r, overbought)
    if buy:
        strategy.entry('L', strategy.long)
    elif sell:
        strategy.close('L')
