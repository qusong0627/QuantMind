# -*- coding: utf-8 -*-
"""示例 5：Strategy Lab 双均线。

在「策略实验室」粘贴运行；setup 必填，on_bar 每根 K 线每只股票调用一次。
"""


def setup(ctx):
    ctx.universe = "csi300"
    ctx.start = "2024-01-01"
    ctx.end = "2025-12-31"
    ctx.cash = 1_000_000
    ctx.benchmark = "SH000300"


def on_bar(ctx, bar):
    closes = ctx.history(bar.symbol, n=20, field="close")
    if len(closes) < 20:
        return
    fast = closes.iloc[-5:].mean()
    slow = closes.mean()
    pos = ctx.position(bar.symbol)
    if fast > slow and pos.qty == 0:
        ctx.buy(bar.symbol, weight=0.05, reason="MA5>MA20")
    elif fast < slow and pos.qty > 0:
        ctx.sell(bar.symbol, all=True, reason="MA5<MA20")
