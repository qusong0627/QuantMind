"""
@pyne

双均线交叉（SMA 金叉买入 / 死叉离场）——A股多头口径。

A股参数口径（所有模板统一，便于横向比较）：
  initial_capital=100000、95% 仓位、双边费率 0.05%（含印花税摊薄）、滑点 1 tick。
"""
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("双均线交叉", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(fast=input(5, "快线周期"), slow=input(20, "慢线周期")):
    f = ta.sma(close, fast)
    s = ta.sma(close, slow)
    up: Series[bool] = f > s
    if up and not up[1]:
        strategy.entry('L', strategy.long)
    elif not up and up[1]:
        strategy.close('L')
