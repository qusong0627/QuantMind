"""
@pyne

EMA 趋势跟踪：快线上穿慢线开仓，跌破快线离场。
"""
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("EMA 趋势跟踪", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(fast=input(20, "快线"), slow=input(60, "慢线")):
    f = ta.ema(close, fast)
    s = ta.ema(close, slow)
    up: Series[bool] = f > s
    if up and not up[1]:
        strategy.entry('L', strategy.long)
    elif not up and up[1]:
        strategy.close('L')
