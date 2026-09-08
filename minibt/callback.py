# -*- coding: utf-8 -*-
"""
止损/止盈回调装饰器
====================
提供 ``@stop_callback`` 装饰器，自动将普通 Python 函数编译为 ``numba.cfunc``，
无需手动指定冗长的类型签名。编译后的函数可直接传入 ``signal_backtest`` 的
``sl_callback=`` / ``tp_callback=`` 参数。

使用示例::

    from minibt import stop_callback, IndSeries
    import numpy as np

    @stop_callback
    def my_trail_sl(bar_idx, direction, entry, price, ref, cur_dist, tick, mult, size, args_data, args_count):
        '''args_data[bar_idx] 取当前 bar 的动态值'''
        atr_val = args_data[bar_idx] if args_count > bar_idx else cur_dist
        return abs(price - ref) if atr_val < cur_dist else 0.0

    ind = IndSeries(kline)
    result = ind.signal_backtest(
        kline,
        sl_callback=my_trail_sl,
        sl_callback_args=(atr_array,),  # 与信号同长的 ndarray
    )

注意:
    被装饰函数建议保持纯 Python 语法（numpy 在 nopython 模式中有限制），
    推荐直接使用 Python 数值运算以实现最快 C 编译速度。
"""

from numba import cfunc, types


# ===== stop_callback 类型签名（与 backtrader_from_signals.pyx 中 stop_callback_t 一致）=====
_STOP_CALLBACK_SIG = types.float64(
    types.int64,        # bar_idx
    types.int64,        # direction
    types.float64,      # entry_price
    types.float64,      # current_price
    types.float64,      # ref_price
    types.float64,      # current_distance
    types.float64,      # price_tick
    types.float64,      # volume_multiple
    types.float64,      # size
    types.CPointer(types.float64),  # args_data
    types.int64,        # args_count
)


def stop_callback(func):
    """装饰器：将函数编译为 stop_callback_t 签名的 numba C 函数指针。

    被装饰函数签名::

        double func(
            int64 bar_idx,           # 当前 bar 索引
            int64 direction,         # 持仓方向: 1=多头, -1=空头
            float64 entry_price,     # 入场价
            float64 current_price,   # 当前 bar 价格
            float64 ref_price,       # 跟踪参考价
            float64 current_distance,# 当前止损/止盈距离
            float64 price_tick,      # 最小波动单位
            float64 volume_multiple, # 合约乘数
            float64 size,            # 持仓手数
            float64[:] args_data,    # 附加参数数组（sl_callback_args 展平）
            int64 args_count,        # 附加参数数组长度
        )

    Returns:
        numba cfunc 对象，可直接传入 ``sl_callback=`` 或 ``tp_callback=``。
    """
    # 使用 functools.wraps 保留原函数名和 docstring
    import functools
    compiled = cfunc(_STOP_CALLBACK_SIG)(func)
    # cfunc 已经包装好了，额外保留原函数引用
    compiled.__wrapped__ = func
    return compiled
