# python setup.py build_ext --inplace
import numpy as np
cimport numpy as np


# ===== 计算手续费 =====
cdef double _calc_com(double price, double size, double vol_mult, double commission, int com_type, double price_tick) noexcept:
    """C 级别内联手续费计算（避免 Python 函数调用开销）。"""
    cdef double c = 0.0
    if com_type == 0:  # Tick
        c = commission * price_tick * vol_mult * size
    elif com_type == 1:  # Fixed
        c = commission * size
    elif com_type == 2:  # Percent
        c = price * size * vol_mult * commission
    return c


# 计算交易大小
def calculate_size(double close_price, double size, int size_type, double init_cash, double margin_rate, double volume_multiple):
    cdef double calculated_size = 0.0
    if size_type == 0:  # Amount
        calculated_size = size
    elif size_type == 1:  # Value
        calculated_size = size / (close_price * volume_multiple * margin_rate)
    elif size_type == 2:  # Percent
        calculated_size = (init_cash * size) / (close_price * volume_multiple * margin_rate)
    calculated_size = float(max(int(calculated_size), 1))
    return calculated_size


# ===== 止损/止盈辅助函数 =====
cdef double _calc_stop_dist(double ref_price, double stop_value, int mode,
                             double price_tick, double size, double vol_mult) noexcept:
    cdef double distance = 0.0
    if stop_value <= 0:
        return 0.0
    if mode == 0:  # Tick
        distance = stop_value * price_tick
    elif mode == 1:  # Amount
        if size > 0 and vol_mult > 0:
            distance = stop_value / (size * vol_mult)
        else:
            distance = 0.0
    elif mode == 2:  # Percent
        distance = ref_price * stop_value
    return distance


# ===== 主函数 =====
def pair_from_signals(
    np.ndarray close,              # shape (n, 2)  两列资产收盘价
    np.ndarray entries,            # shape (n,)    >0=做多价差(多A空B), <0=做空价差(空A多B)
    np.ndarray exits,              # shape (n,)    >0=平空价差, <0=平多价差
    object size=1.0,               # 手数参数（价差整体，按 leg_size_ratio 分配）
    int size_type=0,               # Amount
    object margin_rate=[0.1, 0.1],     # [leg_a, leg_b]
    object price_tick=[1.0, 1.0],      # [leg_a, leg_b]
    object volume_multiple=[5.0, 5.0], # [leg_a, leg_b]
    np.ndarray prices=None,        # shape (n, 2) 成交价
    int min_start_length=1,
    float init_cash=1000000.0,
    object commission=[1.0, 1.0],  # [leg_a, leg_b]
    int com_type=1,                # Fixed
    float slip_point=0.0,
    # ===== 止损/止盈参数 =====
    float sl_stop=0.0,
    float tp_stop=0.0,
    int stop_mode=0,
    int sl_trail=0,
    int max_hold_bars=0,
    # ===== 配对参数 =====
    object leg_size_ratio=1.0,     # A:B 手数比例, >1 表示A的手数大于B
):
    """
    配对交易 Cython 回测引擎。

    核心逻辑：
      1. 入场信号检查（联合资金验证，原子入场两条腿）
      2. 每条腿独立检查止损/止盈/超时/出场信号
      3. 任意一条腿离场 → 级联强制平掉另一条腿

    Args:
        close: (n, 2) 两列收盘价
        entries: (n,) >0 做多价差 / <0 做空价差
        exits: (n,) >0 平空价差 / <0 平多价差
        size: 价差交易规模
        size_type: 规模类型
        margin_rate: [leg_a, leg_b] 保证金比例
        price_tick: [leg_a, leg_b] 最小变动单位
        volume_multiple: [leg_a, leg_b] 合约乘数
        prices: (n, 2) 成交价
        leg_size_ratio: A:B 手数比例
    """
    cdef int i
    cdef int n = close.shape[0]  # 时间步数
    cdef int m = 2               # 固定两个合约

    # ---- 处理参数为列表 ----
    cdef list margin_rate_list, price_tick_list, volume_multiple_list, commission_list
    cdef object sz

    if isinstance(margin_rate, (int, float)):
        margin_rate_list = [float(margin_rate) for _ in range(m)]
    elif len(margin_rate) == 1 and m > 1:
        margin_rate_list = [float(margin_rate[0]) for _ in range(m)]
    else:
        margin_rate_list = [float(mr) for mr in margin_rate]

    if isinstance(price_tick, (int, float)):
        price_tick_list = [float(price_tick) for _ in range(m)]
    elif len(price_tick) == 1 and m > 1:
        price_tick_list = [float(price_tick[0]) for _ in range(m)]
    else:
        price_tick_list = [float(pt) for pt in price_tick]

    if isinstance(volume_multiple, (int, float)):
        volume_multiple_list = [float(volume_multiple) for _ in range(m)]
    elif len(volume_multiple) == 1 and m > 1:
        volume_multiple_list = [float(volume_multiple[0]) for _ in range(m)]
    else:
        volume_multiple_list = [float(vm) for vm in volume_multiple]

    if isinstance(commission, (int, float)):
        commission_list = [float(commission) for _ in range(m)]
    elif len(commission) == 1 and m > 1:
        commission_list = [float(commission[0]) for _ in range(m)]
    else:
        commission_list = [float(c) for c in commission]

    # ---- 处理 prices ----
    if prices is None:
        prices = close

    # ---- 解析手数比 ----
    cdef double ratio_a = 1.0
    cdef double ratio_b = 1.0
    if isinstance(leg_size_ratio, (int, float)):
        ratio_a = float(leg_size_ratio)
        ratio_b = 1.0
    elif isinstance(leg_size_ratio, (list, tuple)):
        ratio_a = float(leg_size_ratio[0])
        ratio_b = float(leg_size_ratio[1]) if len(leg_size_ratio) > 1 else 1.0

    # ---- 初始化结果数组 ----
    cdef list result = []
    cdef np.ndarray[np.double_t, ndim=2] r0 = np.zeros((n, 6), dtype=np.float64)
    cdef np.ndarray[np.double_t, ndim=2] r1 = np.zeros((n, 6), dtype=np.float64)
    result = [r0, r1]

    # ---- 账户变量 ----
    cdef double current_cash = init_cash
    cdef double cum_profit = 0.0
    cdef double total_equity = init_cash
    cdef double total_fee = 0.0

    # ---- 每条腿的状态变量 ----
    cdef list pnl            = [0.0, 0.0]
    cdef list position        = [0.0, 0.0]   # 1=多, -1=空, 0=空仓
    cdef list current_size    = [0.0, 0.0]
    cdef list entry_price     = [0.0, 0.0]
    cdef list in_position     = [0, 0]
    cdef list direction       = [0, 0]       # 1=多, -1=空
    cdef list current_margin  = [0.0, 0.0]

    # ---- 止损/止盈跟踪 ----
    cdef list sl_price     = [0.0, 0.0]
    cdef list tp_price     = [0.0, 0.0]
    cdef list sl_ref_price = [0.0, 0.0]
    cdef list sl_distance  = [0.0, 0.0]
    cdef list tp_distance  = [0.0, 0.0]

    # ---- 持仓K线计数 ----
    cdef list hold_bars = [0, 0]

    # ---- 局部临时变量 ----
    cdef int j, a, b
    cdef double trade_price
    cdef double tp_a, tp_b
    cdef double sz_a, sz_b, sz_j
    cdef double com_a, com_b, com_j
    cdef double margin_a, margin_b
    cdef double total_required
    cdef double profit
    cdef int position_closed
    cdef int leg_closed_this_bar
    cdef double dist

    # ---- 主循环 ----
    for i in range(n):
        pnl[0] = 0.0
        pnl[1] = 0.0

        # 跳过最小启动长度
        if i < min_start_length - 1:
            result[0][i, 0] = total_equity
            result[0][i, 1] = position[0]
            result[0][i, 2] = current_size[0]
            result[0][i, 3] = 0.0
            result[0][i, 4] = cum_profit
            result[0][i, 5] = total_fee
            result[1][i, 0] = total_equity
            result[1][i, 1] = position[1]
            result[1][i, 2] = current_size[1]
            result[1][i, 3] = 0.0
            result[1][i, 4] = cum_profit
            result[1][i, 5] = total_fee
            continue

        # ==================================================================
        # Phase 1: 配对入场（联合资金校验，原子入场两条腿）
        # ==================================================================
        if not in_position[0] and not in_position[1] and entries[i] != 0:
            # 确定两条腿的方向
            if entries[i] > 0:
                # 做多价差（long_signal）：买leg[0] / 卖leg[1]
                #   spread = leg[1] - hedge * leg[0]，spread偏高 → leg[1]贵 → 卖leg[1]买leg[0]
                direction[0] = 1
                direction[1] = -1
            else:
                # 做空价差（short_signal）：卖leg[0] / 买leg[1]
                #   spread = leg[1] - hedge * leg[0]，spread偏低 → leg[1]便宜 → 买leg[1]卖leg[0]
                direction[0] = -1
                direction[1] = 1

            # 计算两条腿的滑点调整价
            if slip_point > 0:
                if direction[0] == 1:
                    tp_a = prices[i, 0] + slip_point
                else:
                    tp_a = prices[i, 0] - slip_point
            else:
                tp_a = prices[i, 0]

            if slip_point > 0:
                if direction[1] == 1:
                    tp_b = prices[i, 1] + slip_point
                else:
                    tp_b = prices[i, 1] - slip_point
            else:
                tp_b = prices[i, 1]

            # 计算手数（基于共享资金池，每条腿独立计算后按比例缩放）
            sz_a = calculate_size(tp_a, float(size), size_type, current_cash,
                                  margin_rate_list[0], volume_multiple_list[0])
            sz_b = calculate_size(tp_b, float(size), size_type, current_cash,
                                  margin_rate_list[1], volume_multiple_list[1])

            # 应用手数比
            sz_a *= ratio_a
            sz_b *= ratio_b
            sz_a = float(max(<int>sz_a, 1))
            sz_b = float(max(<int>sz_b, 1))

            # 计算手续费和保证金
            com_a = _calc_com(tp_a, sz_a, volume_multiple_list[0],
                              commission_list[0], com_type, price_tick_list[0])
            com_b = _calc_com(tp_b, sz_b, volume_multiple_list[1],
                              commission_list[1], com_type, price_tick_list[1])
            margin_a = tp_a * sz_a * volume_multiple_list[0] * margin_rate_list[0]
            margin_b = tp_b * sz_b * volume_multiple_list[1] * margin_rate_list[1]
            total_required = margin_a + com_a + margin_b + com_b

            # 联合资金检查
            if current_cash >= total_required:
                # ---- 原子入场：leg A ----
                current_size[0] = sz_a
                total_equity -= com_a
                current_cash -= com_a + margin_a
                current_margin[0] = margin_a
                total_fee += com_a
                cum_profit -= com_a
                position[0] = <double>direction[0]
                entry_price[0] = tp_a
                in_position[0] = 1
                hold_bars[0] = 0

                # 初始化 leg A 的止损/止盈
                if sl_stop > 0:
                    sl_ref_price[0] = tp_a
                    sl_distance[0] = _calc_stop_dist(tp_a, sl_stop, stop_mode,
                                                     price_tick_list[0], sz_a, volume_multiple_list[0])
                    if direction[0] == 1:
                        sl_price[0] = tp_a - sl_distance[0]
                    else:
                        sl_price[0] = tp_a + sl_distance[0]
                else:
                    sl_price[0] = 0.0
                    sl_ref_price[0] = 0.0
                    sl_distance[0] = 0.0
                if tp_stop > 0:
                    tp_distance[0] = _calc_stop_dist(tp_a, tp_stop, stop_mode,
                                                     price_tick_list[0], sz_a, volume_multiple_list[0])
                    if direction[0] == 1:
                        tp_price[0] = tp_a + tp_distance[0]
                    else:
                        tp_price[0] = tp_a - tp_distance[0]
                else:
                    tp_price[0] = 0.0
                    tp_distance[0] = 0.0

                # ---- 原子入场：leg B ----
                current_size[1] = sz_b
                total_equity -= com_b
                current_cash -= com_b + margin_b
                current_margin[1] = margin_b
                total_fee += com_b
                cum_profit -= com_b
                position[1] = <double>direction[1]
                entry_price[1] = tp_b
                in_position[1] = 1
                hold_bars[1] = 0

                # 初始化 leg B 的止损/止盈
                if sl_stop > 0:
                    sl_ref_price[1] = tp_b
                    sl_distance[1] = _calc_stop_dist(tp_b, sl_stop, stop_mode,
                                                     price_tick_list[1], sz_b, volume_multiple_list[1])
                    if direction[1] == 1:
                        sl_price[1] = tp_b - sl_distance[1]
                    else:
                        sl_price[1] = tp_b + sl_distance[1]
                else:
                    sl_price[1] = 0.0
                    sl_ref_price[1] = 0.0
                    sl_distance[1] = 0.0
                if tp_stop > 0:
                    tp_distance[1] = _calc_stop_dist(tp_b, tp_stop, stop_mode,
                                                     price_tick_list[1], sz_b, volume_multiple_list[1])
                    if direction[1] == 1:
                        tp_price[1] = tp_b + tp_distance[1]
                    else:
                        tp_price[1] = tp_b - tp_distance[1]
                else:
                    tp_price[1] = 0.0
                    tp_distance[1] = 0.0

                # 入场后跳过逐腿检查（本 bar 刚入场）
                result[0][i, 0] = total_equity
                result[0][i, 1] = position[0]
                result[0][i, 2] = current_size[0]
                result[0][i, 3] = 0.0
                result[0][i, 4] = cum_profit
                result[0][i, 5] = total_fee
                result[1][i, 0] = total_equity
                result[1][i, 1] = position[1]
                result[1][i, 2] = current_size[1]
                result[1][i, 3] = 0.0
                result[1][i, 4] = cum_profit
                result[1][i, 5] = total_fee
                continue

        # ==================================================================
        # Phase 2: 逐腿检查（止损 / 止盈 / 超时 / 出场信号）
        # ==================================================================
        leg_closed_this_bar = 0
        for j in range(m):
            if not in_position[j]:
                result[j][i, 0] = total_equity
                result[j][i, 1] = position[j]
                result[j][i, 2] = current_size[j]
                result[j][i, 3] = pnl[j]
                result[j][i, 4] = cum_profit
                result[j][i, 5] = total_fee
                continue

            # 增加持仓K线计数
            hold_bars[j] += 1
            position_closed = 0

            # ---- 更新移动止损参考价 ----
            if not position_closed and sl_trail and sl_stop > 0:
                if direction[j] == 1:
                    if prices[i, j] > sl_ref_price[j]:
                        sl_ref_price[j] = prices[i, j]
                else:
                    if prices[i, j] < sl_ref_price[j]:
                        sl_ref_price[j] = prices[i, j]

            # ---- 重算移动止损价 ----
            if not position_closed and sl_trail and sl_stop > 0:
                sl_distance[j] = _calc_stop_dist(sl_ref_price[j], sl_stop, stop_mode,
                                                 price_tick_list[j], current_size[j],
                                                 volume_multiple_list[j])
                if direction[j] == 1:
                    sl_price[j] = sl_ref_price[j] - sl_distance[j]
                else:
                    sl_price[j] = sl_ref_price[j] + sl_distance[j]

            # ---- 1. 检查止损 ----
            if not position_closed and sl_stop > 0:
                if direction[j] == 1 and prices[i, j] <= sl_price[j]:
                    trade_price = sl_price[j]
                    if slip_point > 0:
                        trade_price -= slip_point
                    com_j = _calc_com(trade_price, current_size[j], volume_multiple_list[j],
                                      commission_list[j], com_type, price_tick_list[j])
                    profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com_j
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
                    total_equity += profit
                    total_fee += com_j
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                    position_closed = 1
                    leg_closed_this_bar = 1

                elif direction[j] == -1 and prices[i, j] >= sl_price[j]:
                    trade_price = sl_price[j]
                    if slip_point > 0:
                        trade_price += slip_point
                    com_j = _calc_com(trade_price, current_size[j], volume_multiple_list[j],
                                      commission_list[j], com_type, price_tick_list[j])
                    profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com_j
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
                    total_equity += profit
                    total_fee += com_j
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                    position_closed = 1
                    leg_closed_this_bar = 1

            # ---- 2. 检查止盈 ----
            if not position_closed and tp_stop > 0:
                if direction[j] == 1 and prices[i, j] >= tp_price[j]:
                    trade_price = tp_price[j]
                    if slip_point > 0:
                        trade_price -= slip_point
                    com_j = _calc_com(trade_price, current_size[j], volume_multiple_list[j],
                                      commission_list[j], com_type, price_tick_list[j])
                    profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com_j
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
                    total_equity += profit
                    total_fee += com_j
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                    position_closed = 1
                    leg_closed_this_bar = 1

                elif direction[j] == -1 and prices[i, j] <= tp_price[j]:
                    trade_price = tp_price[j]
                    if slip_point > 0:
                        trade_price += slip_point
                    com_j = _calc_com(trade_price, current_size[j], volume_multiple_list[j],
                                      commission_list[j], com_type, price_tick_list[j])
                    profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com_j
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
                    total_equity += profit
                    total_fee += com_j
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                    position_closed = 1
                    leg_closed_this_bar = 1

            # ---- 3. 持仓超时离场 ----
            if not position_closed and max_hold_bars >= 1 and hold_bars[j] >= max_hold_bars:
                if slip_point > 0:
                    if direction[j] == 1:
                        trade_price = prices[i, j] - slip_point
                    else:
                        trade_price = prices[i, j] + slip_point
                else:
                    trade_price = prices[i, j]
                com_j = _calc_com(trade_price, current_size[j], volume_multiple_list[j],
                                  commission_list[j], com_type, price_tick_list[j])
                if direction[j] == 1:
                    profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com_j
                else:
                    profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com_j
                pnl[j] = profit
                cum_profit += profit
                current_cash += current_margin[j] + profit
                current_margin[j] = 0.0
                total_equity += profit
                total_fee += com_j
                position[j] = 0.0
                in_position[j] = 0
                direction[j] = 0
                sl_price[j] = 0.0
                tp_price[j] = 0.0
                sl_ref_price[j] = 0.0
                hold_bars[j] = 0
                position_closed = 1
                leg_closed_this_bar = 1

            # ---- 4. 检查出场信号 ----
            if not position_closed and exits[i] != 0:
                # exits > 0: 平空价差 → 平空腿(即当前 direction==-1 的腿)
                # exits < 0: 平多价差 → 平多腿(即当前 direction==1 的腿)
                if (direction[j] == 1 and exits[i] < 0) or (direction[j] == -1 and exits[i] > 0):
                    if slip_point > 0:
                        if direction[j] == 1:
                            trade_price = prices[i, j] - slip_point
                        else:
                            trade_price = prices[i, j] + slip_point
                    else:
                        trade_price = prices[i, j]
                    com_j = _calc_com(trade_price, current_size[j], volume_multiple_list[j],
                                      commission_list[j], com_type, price_tick_list[j])
                    if direction[j] == 1:
                        profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com_j
                    else:
                        profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com_j
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
                    total_equity += profit
                    total_fee += com_j
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                    position_closed = 1
                    leg_closed_this_bar = 1

            # ---- 写结果 ----
            result[j][i, 0] = total_equity
            result[j][i, 1] = position[j]
            result[j][i, 2] = current_size[j]
            result[j][i, 3] = pnl[j]
            result[j][i, 4] = cum_profit
            result[j][i, 5] = total_fee

        # ==================================================================
        # Phase 3: 配对出场级联（一条腿离场 → 强制关另一条腿）
        # ==================================================================
        if in_position[0] and not in_position[1]:
            # leg B 已平，级联平 leg A
            if slip_point > 0:
                if direction[0] == 1:
                    trade_price = prices[i, 0] - slip_point
                else:
                    trade_price = prices[i, 0] + slip_point
            else:
                trade_price = prices[i, 0]
            com_j = _calc_com(trade_price, current_size[0], volume_multiple_list[0],
                              commission_list[0], com_type, price_tick_list[0])
            if direction[0] == 1:
                profit = (trade_price - entry_price[0]) * current_size[0] * volume_multiple_list[0] - com_j
            else:
                profit = (entry_price[0] - trade_price) * current_size[0] * volume_multiple_list[0] - com_j
            pnl[0] = profit
            cum_profit += profit
            current_cash += current_margin[0] + profit
            current_margin[0] = 0.0
            total_equity += profit
            total_fee += com_j
            position[0] = 0.0
            in_position[0] = 0
            direction[0] = 0
            sl_price[0] = 0.0
            tp_price[0] = 0.0
            sl_ref_price[0] = 0.0
            hold_bars[0] = 0
            result[0][i, 0] = total_equity
            result[0][i, 1] = 0.0
            result[0][i, 2] = 0.0
            result[0][i, 3] = pnl[0]
            result[0][i, 4] = cum_profit
            result[0][i, 5] = total_fee

        elif not in_position[0] and in_position[1]:
            # leg A 已平，级联平 leg B
            if slip_point > 0:
                if direction[1] == 1:
                    trade_price = prices[i, 1] - slip_point
                else:
                    trade_price = prices[i, 1] + slip_point
            else:
                trade_price = prices[i, 1]
            com_j = _calc_com(trade_price, current_size[1], volume_multiple_list[1],
                              commission_list[1], com_type, price_tick_list[1])
            if direction[1] == 1:
                profit = (trade_price - entry_price[1]) * current_size[1] * volume_multiple_list[1] - com_j
            else:
                profit = (entry_price[1] - trade_price) * current_size[1] * volume_multiple_list[1] - com_j
            pnl[1] = profit
            cum_profit += profit
            current_cash += current_margin[1] + profit
            current_margin[1] = 0.0
            total_equity += profit
            total_fee += com_j
            position[1] = 0.0
            in_position[1] = 0
            direction[1] = 0
            sl_price[1] = 0.0
            tp_price[1] = 0.0
            sl_ref_price[1] = 0.0
            hold_bars[1] = 0
            result[1][i, 0] = total_equity
            result[1][i, 1] = 0.0
            result[1][i, 2] = 0.0
            result[1][i, 3] = pnl[1]
            result[1][i, 4] = cum_profit
            result[1][i, 5] = total_fee

        # ---- 最终统一校正：确保本bar两腿权益记录一致 ----
        # 当某条腿在Phase 2中先平仓，Phase 3级联平另一条腿时，
        # 先平仓的腿result可能只记录了部分盈亏，此处统一覆盖为最终状态
        result[0][i, 0] = total_equity
        result[0][i, 1] = position[0]
        result[0][i, 2] = current_size[0]
        result[0][i, 3] = pnl[0]
        result[0][i, 4] = cum_profit
        result[0][i, 5] = total_fee
        result[1][i, 0] = total_equity
        result[1][i, 1] = position[1]
        result[1][i, 2] = current_size[1]
        result[1][i, 3] = pnl[1]
        result[1][i, 4] = cum_profit
        result[1][i, 5] = total_fee

    return result
