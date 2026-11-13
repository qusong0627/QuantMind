"""
@pyne

ATR 通道趋势：收盘上穿 EMA+ATR 上轨开仓，跌破 EMA 离场（波动率自适应）。
"""
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("ATR 通道趋势", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(ma_len=input(20, "均线周期"), atr_len=input(14, "ATR 周期"), mult=input(1.5, "通道倍数")):
    ma = ta.ema(close, ma_len)
    atr = ta.atr(atr_len)
    upper = ma + mult * atr
    buy: Series[bool] = ta.crossover(close, upper)
    if buy:
        strategy.entry('L', strategy.long)
    elif close < ma:
        strategy.close('L')
