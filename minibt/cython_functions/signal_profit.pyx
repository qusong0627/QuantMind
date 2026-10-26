# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# cython: initializedcheck=False
"""
信号持仓收益计算引擎 (Cython 加速)
====================================

根据交易信号计算持仓 N 根 K 线后的收益曲线。

计算逻辑:
  - 初始 profit = 1.0
  - 多头: profit = pre_profit * (1 + (current_price - open_price) / open_price)
  - 空头: profit = pre_profit * (1 + (open_price - current_price) / open_price)
  - open_price 为信号出现时的价格 (固定)，current_price 为当前 bar 的价格

输入:
  signals:   (n,) float64  — 信号数组 (0=无信号, >0=多头信号, <0=空头信号)
  close:     (n,) float64  — 收盘价数组
  bars:      (m,) int      — 持仓根数列表，如 [3, 5, 10]

输出:
  返回 dict:
    'long_profits':  (n_signals, m) float64 — 每个信号的多头收益
    'short_profits': (n_signals, m) float64 — 每个信号的空头收益
    'signal_indices': list of int — 信号出现的 bar 索引
    'signal_directions': list of int — 信号方向 (1=多头, -1=空头)
    'cum_profit': list of (n,) float64 — 累计收益曲线 (每个 bar 一档，按信号方向合并)
"""

import numpy as np
cimport numpy as np
from libc.math cimport isnan, isfinite


def calc_signal_profit(
    np.ndarray[double, ndim=1] signals,
    np.ndarray[double, ndim=1] close,
    list bars
):
    """
    根据交易信号计算持仓收益

    Args:
        signals: 信号数组 (0=无信号, 1=多头, -1=空头)
        close: 收盘价数组
        bars: 持仓根数列表

    Returns:
        dict: 包含收益数据的字典
    """
    cdef int n = signals.shape[0]
    cdef int m = len(bars)
    cdef int i, j, k
    cdef double open_price, current_price, profit, factor
    cdef int bar_hold

    # 找到所有信号位置
    signal_indices = []
    signal_directions = []
    for i in range(n):
        if signals[i] > 0:
            signal_indices.append(i)
            signal_directions.append(1)
        elif signals[i] < 0:
            signal_indices.append(i)
            signal_directions.append(-1)

    cdef int num_signals = len(signal_indices)

    if num_signals == 0:
        return {
            'long_profits': np.zeros((0, m), dtype=np.float64),
            'short_profits': np.zeros((0, m), dtype=np.float64),
            'signal_indices': [],
            'signal_directions': [],
            'cum_profit': [],
            'bars': bars,
        }

    # 初始化收益数组
    long_profits = np.zeros((num_signals, m), dtype=np.float64)
    short_profits = np.zeros((num_signals, m), dtype=np.float64)

    # 对每个信号，计算持仓 N 根后的收益
    for j in range(num_signals):
        sig_idx = signal_indices[j]
        direction = signal_directions[j]
        open_price = close[sig_idx]

        if open_price <= 0 or not isfinite(open_price):
            long_profits[j, :] = np.nan
            short_profits[j, :] = np.nan
            continue

        for k in range(m):
            bar_hold = bars[k]
            end_idx = sig_idx + bar_hold

            if end_idx >= n:
                end_idx = n - 1

            # 多头收益
            profit = 1.0
            for i in range(sig_idx + 1, end_idx + 1):
                current_price = close[i]
                if not isfinite(current_price):
                    continue
                factor = 1.0 + (current_price - open_price) / open_price
                profit *= factor
            long_profits[j, k] = profit

            # 空头收益
            profit = 1.0
            for i in range(sig_idx + 1, end_idx + 1):
                current_price = close[i]
                if not isfinite(current_price):
                    continue
                factor = 1.0 + (open_price - current_price) / open_price
                profit *= factor
            short_profits[j, k] = profit

    # 计算累计收益曲线 (按 bar 维度聚合，按信号方向合并)
    cum_profit_list = []

    for k in range(m):
        bar_hold = bars[k]
        cum_profit = np.full(n, np.nan, dtype=np.float64)

        cum_value = 1.0
        last_idx = -1

        for j in range(num_signals):
            sig_idx = signal_indices[j]
            direction = signal_directions[j]

            # 填充从上一个信号到当前信号之间的区间
            for idx in range(last_idx + 1, sig_idx):
                if idx < n:
                    cum_profit[idx] = cum_value

            # 当前 bar 的收益在信号出现 bar_hold 根后结算
            settle_idx = sig_idx + bar_hold
            if settle_idx >= n:
                settle_idx = n - 1

            # 根据信号方向选择收益
            if direction == 1:
                cum_value = long_profits[j, k]
            elif direction == -1:
                cum_value = short_profits[j, k]

            # 填充从结算位置到下一个信号或结尾
            next_sig_idx = signal_indices[j + 1] if j + 1 < num_signals else n
            end_fill = min(settle_idx, next_sig_idx - 1)
            for idx in range(settle_idx, end_fill + 1):
                if idx < n:
                    cum_profit[idx] = cum_value

            last_idx = end_fill

        # 填充剩余区间
        for idx in range(last_idx + 1, n):
            cum_profit[idx] = cum_value

        cum_profit_list.append(cum_profit)

    return {
        'long_profits': long_profits,
        'short_profits': short_profits,
        'signal_indices': signal_indices,
        'signal_directions': signal_directions,
        'cum_profit': cum_profit_list,
        'bars': bars,
    }


def calc_single_signal_profit(
    np.ndarray[double, ndim=1] signal,
    np.ndarray[double, ndim=1] close,
    int bar_hold
):
    """
    单个信号的收益计算 (用于 IndSeries 单信号场景)

    Args:
        signal: 单信号数组 (0 或 1)
        close: 收盘价数组
        bar_hold: 持仓根数

    Returns:
        dict: 收益数据
    """
    result = calc_signal_profit(signal, close, [bar_hold])

    return {
        'long_profits': result['long_profits'],
        'short_profits': result['short_profits'],
        'signal_indices': result['signal_indices'],
        'signal_directions': result['signal_directions'],
        'cum_profit': result['cum_profit'] if result['cum_profit'] else [],
        'bars': [bar_hold],
    }
