# -*- coding: utf-8 -*-
"""示例 4：模拟盘沙箱 on_tick。

系统每 10 秒调用一次；本示例只在空仓时把目标仓位设为 20%。
实际使用请加入卖出条件、止损、最大持仓等。
"""


def on_tick(context):
    symbol = "SH600036"
    pos = context.get_position(symbol)
    if pos["volume"] == 0:
        context.order_target_percent(symbol, 0.20)
        context.log(f"建仓 20%：{symbol}")
