# python setup.py build_ext --inplace
import numpy as np
cimport numpy as np
from libc.stdint cimport uintptr_t, int64_t


# ===== 动态回调函数指针类型定义 =====
ctypedef double (*stop_callback_t)(
    long long bar_idx,          # 当前bar索引
    long long direction,        # 持仓方向: 1=多头, -1=空头
    double entry_price,         # 入场价
    double current_price,       # 当前bar价格
    double ref_price,           # 跟踪参考价（入场价或trailing新高/新低）
    double current_distance,    # 当前的止损/止盈价格距离
    double price_tick,          # 最小波动单位
    double volume_multiple,     # 合约乘数
    double size,                # 持仓手数
    double* args_data,          # 自定义参数数组（指针），由 adjust_sl_args / adjust_tp_args 传入
    long long args_count,       # 自定义参数数组长度，0 表示无附加参数
) noexcept nogil


# 计算手续费
def calculate_commission(double price, double size, double volume_multiple, double commission, int com_type, double price_tick):
    cdef double com = 0.0
    if com_type == 0:  # Tick
        com = commission * price_tick * volume_multiple * size
    elif com_type == 1:  # Fixed
        com = commission * size
    elif com_type == 2:  # Percent
        com = price * size * volume_multiple * commission
    return com

# 计算交易大小
def calculate_size(double close_price, double size, int size_type, double init_cash, double margin_rate, double volume_multiple):
    cdef double calculated_size = 0.0
    if size_type == 0:  # Amount
        calculated_size = size
    elif size_type == 1:  # Value
        calculated_size = size / (close_price * volume_multiple * margin_rate)
    elif size_type == 2:  # Percent，int ((init_cash * size) / (close_price * volume_multiple * margin_rate))
        calculated_size = (init_cash * size) / (close_price * volume_multiple * margin_rate)
    calculated_size = float(max(int(calculated_size), 1))
    return calculated_size


# ===== 止损/止盈辅助函数 =====
cdef double calc_stop_distance(double ref_price, double stop_value, int mode,
                                double price_tick, double size, double vol_mult):
    """根据模式计算止损/止盈的价格距离（从参考价出发的偏移量）。
    
    Args:
        ref_price: 参考价格（入场价或移动止损参考价）
        stop_value: 止损/止盈参数值
        mode: 模式 (0=tick, 1=金额, 2=百分比)
        price_tick: 最小变动单位
        size: 持仓手数
        vol_mult: 合约乘数
    
    Returns:
        stop_distance: 价格偏移量（始终 >= 0）
    """
    cdef double distance = 0.0
    if stop_value <= 0:
        return 0.0
    
    if mode == 0:  # Tick: 按最小波动单位
        distance = stop_value * price_tick
    elif mode == 1:  # Amount: 按金额
        if size > 0 and vol_mult > 0:
            distance = stop_value / (size * vol_mult)
        else:
            distance = 0.0
    elif mode == 2:  # Percent: 按合约价值百分比
        distance = ref_price * stop_value
    return distance


# ===== 回调 args 展平辅助函数 =====
# 将 (标量, np数组, 标量, ...) tuple 展平为单个 1D float64 数组
# 标量 → 单元素，np数组 → 展平(ravel)所有元素
# 这样 callback 可通过 bar_idx 索引数组元素实现动态取值
cdef np.ndarray[np.double_t, ndim=1] _build_flat_args(tuple args):
    cdef list parts = []
    cdef np.ndarray part
    for a in args:
        # np.atleast_1d: 确保标量→1D数组[val], 高维→先展平
        part = np.atleast_1d(np.asarray(a, dtype=np.float64))
        for k in range(part.shape[0]):
            parts.append(float(part[k]))
    cdef np.ndarray[np.double_t, ndim=1] result = np.array(parts, dtype=np.float64)
    return result


# 主函数
def from_signals(
    np.ndarray close,
    np.ndarray entries,
    np.ndarray exits,
    object size=1.0,  # 可以是单个数值或与合约长度相同的列表/数组
    int size_type=0,  # Amount
    object margin_rate=[0.1],  # 与合约长度相同的列表
    object price_tick=[1.0],  # 与合约长度相同的列表
    object volume_multiple=[5.0],  # 与合约长度相同的列表
    np.ndarray prices=None,
    int min_start_length=1,
    float init_cash=1000000.0,
    object commission=[1],  # 默认1，与合约长度相同的列表
    int com_type=1,  # Fixed
    float slip_point=0.0,
    # ===== 止损/止盈参数（模式一：固定值）=====
    float sl_stop=0.0,      # 止损值，0表示不使用止损
    float tp_stop=0.0,      # 止盈值，0表示不使用止盈
    int stop_mode=0,        # 止损/止盈模式: 0=最小波动单位, 1=金额, 2=百分比
    int sl_trail=0,         # 是否启用移动止损: 0=否, 1=是
    # ===== 止损/止盈参数（模式二：动态回调函数）=====
    long long sl_callback_addr=0,   # numba @cfunc 的 .address，0=不使用回调
    long long tp_callback_addr=0,   # numba @cfunc 的 .address，0=不使用回调
    tuple sl_callback_args=(),      # sl回调附加参数（对标 vectorbt 的 adjust_sl_args）
    tuple tp_callback_args=(),      # tp回调附加参数（对标 vectorbt 的 adjust_tp_args）
    int max_hold_bars=0,            # 持仓最大K线数，>=1 时超过该值强制离场，0=禁用
):
    cdef int i, j
    cdef int n = close.shape[0]  # 时间步数
    cdef int m = close.shape[1]  # 合约数量
    cdef list result = []
    cdef np.ndarray[np.double_t, ndim=2] contract_result
    cdef double current_cash, cum_profit, total_equity, total_fee
    cdef double trade_price, com, profit
    cdef str datetime_str, symbol
    cdef int direction_old
    cdef double current_size_old
    
    # 声明数组类型
    cdef list pnl = []  # 结算的盈亏
    cdef list position = []  # 1表示多头，-1表示空头，0表示空仓
    cdef list current_size = [] # 手数
    cdef list entry_price = []
    cdef list in_position = []
    cdef list direction = []
    cdef list size_list = []
    cdef list margin_rate_list = []
    cdef list price_tick_list = []
    cdef list volume_multiple_list = []
    cdef list commission_list = []
    
    # ===== 止损/止盈跟踪变量 =====
    cdef list sl_price = []       # 当前止损触发价格
    cdef list tp_price = []       # 当前止盈触发价格
    cdef list sl_ref_price = []   # 移动止损参考价（入场价或新的最高/最低价）
    cdef list sl_distance = []    # 止损价格距离
    cdef list tp_distance = []    # 止盈价格距离
    
    # 局部标志
    cdef int position_closed
    cdef double current_sl_price, current_tp_price
    cdef double dist
    cdef double new_dist_cb  # 回调函数返回的新距离
    # 回调参数预提取变量（用于 nogil 块）
    cdef int _dir_cb
    cdef double _entry_cb, _price_cb, _ref_cb, _cur_dist_cb
    cdef double _tick_cb, _mult_cb, _sz_cb
    
    # 处理prices参数
    if prices is None:
        prices = close
    
    # 处理参数，转换为与合约长度相同的列表
    # 处理size参数
    if isinstance(size, (int, float)):
        size_list = [float(size) for _ in range(m)]
    else:
        size_list = [float(s) for s in size]
    
    # 处理margin_rate参数
    if isinstance(margin_rate, (int, float)):
        margin_rate_list = [float(margin_rate) for _ in range(m)]
    else:
        margin_rate_list = [float(mr) for mr in margin_rate]
    
    # 处理price_tick参数
    if isinstance(price_tick, (int, float)):
        price_tick_list = [float(price_tick) for _ in range(m)]
    else:
        price_tick_list = [float(pt) for pt in price_tick]
    
    # 处理volume_multiple参数
    if isinstance(volume_multiple, (int, float)):
        volume_multiple_list = [float(volume_multiple) for _ in range(m)]
    else:
        volume_multiple_list = [float(vm) for vm in volume_multiple]
    
    # 处理commission参数
    if isinstance(commission, (int, float)):
        commission_list = [float(commission) for _ in range(m)]
    else:
        commission_list = [float(c) for c in commission]
    
    # ===== 从地址提取回调函数指针 =====
    cdef stop_callback_t sl_cb = NULL
    cdef stop_callback_t tp_cb = NULL
    cdef int use_sl_cb = 0
    cdef int use_tp_cb = 0
    
    if sl_callback_addr != 0:
        sl_cb = <stop_callback_t><uintptr_t>sl_callback_addr
        use_sl_cb = 1
    if tp_callback_addr != 0:
        tp_cb = <stop_callback_t><uintptr_t>tp_callback_addr
        use_tp_cb = 1
    
    # ===== 将回调附加参数 tuple 转换为 flat double 数组 =====
    # 支持每元素为标量或 np.ndarray：标量→单元素，数组→展平所有元素
    # 这样 callback 可通过 bar_idx 索引数组元素实现动态取值
    cdef np.ndarray[np.double_t, ndim=1] _sl_args_arr
    cdef np.ndarray[np.double_t, ndim=1] _tp_args_arr
    cdef int _did_sl_args = 0
    cdef int _did_tp_args = 0
    if sl_callback_args:
        _did_sl_args = 1
        _sl_args_arr = _build_flat_args(sl_callback_args)
    else:
        _sl_args_arr = np.zeros(0, dtype=np.float64)
    if tp_callback_args:
        _did_tp_args = 1
        _tp_args_arr = _build_flat_args(tp_callback_args)
    else:
        _tp_args_arr = np.zeros(0, dtype=np.float64)
    cdef double* _sl_args_ptr = <double*>_sl_args_arr.data if _sl_args_arr.shape[0] > 0 else NULL
    cdef long long _sl_args_len = _sl_args_arr.shape[0]
    cdef double* _tp_args_ptr = <double*>_tp_args_arr.data if _tp_args_arr.shape[0] > 0 else NULL
    cdef long long _tp_args_len = _tp_args_arr.shape[0]
    
    # 初始化结果数组列表
    result = []
    for j in range(m):
        result.append(np.zeros((n, 6), dtype=np.float64))
    
    # 初始化变量 - 共用账户变量
    current_cash = init_cash
    cum_profit = 0.0 # 这个是指累计收益
    total_equity = init_cash # 这个参数是指每个时刻的权益
    total_fee = 0.0  # 累计手续费
    
    # 初始化每个合约的状态变量 - 使用数组
    pnl = [0.0 for _ in range(m)]  # 结算的盈亏
    position = [0.0 for _ in range(m)]  # 1表示多头，-1表示空头，0表示空仓
    current_size = [0.0 for _ in range(m)] # 手数
    entry_price = [0.0 for _ in range(m)]
    in_position = [0 for _ in range(m)]
    direction = [0 for _ in range(m)]
    current_margin=[0.0 for _ in range(m)]

    # ===== 初始化止损/止盈跟踪数组 =====
    sl_price = [0.0 for _ in range(m)]
    tp_price = [0.0 for _ in range(m)]
    sl_ref_price = [0.0 for _ in range(m)]
    sl_distance = [0.0 for _ in range(m)]
    tp_distance = [0.0 for _ in range(m)]
    
    # ===== 持仓K线数计数器 =====
    hold_bars = [0 for _ in range(m)]  # 每个合约已持仓的K线根数

    
    # 先遍历每个时间步，再遍历每个合约（与minibt策略一致）
    for i in range(n):
        # 重置每个合约的结算盈亏
        for j in range(m):
            pnl[j] = 0.0
        
        # 遍历每个合约
        for j in range(m):
            # 跳过最小启动长度之前的时间步
            if i < min_start_length - 1:
                result[j][i, 0] = total_equity
                result[j][i, 1] = position[j]
                result[j][i, 2] = current_size[j]
                result[j][i, 3] = pnl[j]
                result[j][i, 4] = cum_profit
                result[j][i, 5] = total_fee
                continue
            
            # 获取当前价格
            current_close = close[i, j]
            current_price = prices[i, j]
            
            # ===== 检查入场信号 =====
            if not in_position[j] and entries[i, j] != 0:
                direction[j] = entries[i, j]
                # 处理滑点
                if slip_point > 0:
                    if direction[j] == 1:  # 多头开仓
                        trade_price = current_price + slip_point
                    else:  # 空头开仓
                        trade_price = current_price - slip_point
                else:
                    trade_price = current_price
                
                # 计算交易大小
                current_size[j] = calculate_size(trade_price, size_list[j], size_type, current_cash, margin_rate_list[j], volume_multiple_list[j])
                # 计算手续费
                com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                # 检验账户现金是否足够开仓，不够的话直接跳过
                margin = trade_price * current_size[j] * volume_multiple_list[j] * margin_rate_list[j]
                required_cash = margin + com
                if current_cash >= required_cash:
                    current_margin[j]=margin
                    # 执行交易
                    total_equity -= com
                    current_cash -= com + margin

                    total_fee += com
                    cum_profit -= com
                    position[j] = direction[j]
                    entry_price[j] = trade_price
                    in_position[j] = 1
                    hold_bars[j] = 0  # 重置持仓K线计数器
                    
                    # ===== 初始化止损/止盈 =====
                    if sl_stop > 0 or use_sl_cb:
                        sl_ref_price[j] = trade_price
                        if sl_stop > 0:
                            sl_distance[j] = calc_stop_distance(
                                trade_price, sl_stop, stop_mode,
                                price_tick_list[j], current_size[j], volume_multiple_list[j]
                            )
                        else:
                            sl_distance[j] = 0.0  # 回调函数在下个bar设置
                        if direction[j] == 1:  # 多头：止损价 = 入场价 - 距离
                            sl_price[j] = trade_price - sl_distance[j]
                        else:  # 空头：止损价 = 入场价 + 距离
                            sl_price[j] = trade_price + sl_distance[j]
                    else:
                        sl_price[j] = 0.0
                        sl_ref_price[j] = 0.0
                        sl_distance[j] = 0.0
                    
                    if tp_stop > 0 or use_tp_cb:
                        if tp_stop > 0:
                            tp_distance[j] = calc_stop_distance(
                                trade_price, tp_stop, stop_mode,
                                price_tick_list[j], current_size[j], volume_multiple_list[j]
                            )
                        else:
                            tp_distance[j] = 0.0  # 回调函数在下个bar设置
                        if direction[j] == 1:  # 多头：止盈价 = 入场价 + 距离
                            tp_price[j] = trade_price + tp_distance[j]
                        else:  # 空头：止盈价 = 入场价 - 距离
                            tp_price[j] = trade_price - tp_distance[j]
                    else:
                        tp_price[j] = 0.0
                        tp_distance[j] = 0.0
                 
            # ===== 检查出场（动态回调更新 -> 止损 -> 止盈 -> 持仓超时 -> 出场信号）=====
            elif in_position[j]:
                position_closed = 0
                hold_bars[j] += 1  # 每过一个K线递增持仓计数器
                
                # ---- 0a. 更新移动止损参考价（trailing ref_price）----
                # 无论是否使用回调，只要 sl_trail=1 且止损已启用，就持续跟踪参考价
                if not position_closed and sl_trail and (sl_stop > 0 or use_sl_cb):
                    if direction[j] == 1:  # 多头：参考价取最高价
                        if current_price > sl_ref_price[j]:
                            sl_ref_price[j] = current_price
                    else:  # 空头：参考价取最低价
                        if current_price < sl_ref_price[j]:
                            sl_ref_price[j] = current_price
                
                # ---- 0b. 动态SL回调：回调函数决定新的止损距离 ----
                if not position_closed and use_sl_cb:
                    _dir_cb = direction[j]
                    _entry_cb = entry_price[j]
                    _price_cb = current_price
                    _ref_cb = sl_ref_price[j]
                    _cur_dist_cb = sl_distance[j]
                    _tick_cb = price_tick_list[j]
                    _mult_cb = volume_multiple_list[j]
                    _sz_cb = current_size[j]
                    with nogil:
                        new_dist_cb = sl_cb(i, _dir_cb, _entry_cb, _price_cb,
                                           _ref_cb, _cur_dist_cb,
                                           _tick_cb, _mult_cb, _sz_cb,
                                           _sl_args_ptr, _sl_args_len)
                    if new_dist_cb > 0:
                        sl_distance[j] = new_dist_cb
                        if direction[j] == 1:
                            sl_price[j] = sl_ref_price[j] - new_dist_cb
                        else:
                            sl_price[j] = sl_ref_price[j] + new_dist_cb
                
                # ---- 0c. 固定止损更新（fallback：无回调时从 sl_ref_price 重新计算）----
                if not position_closed and not use_sl_cb and sl_trail and sl_stop > 0:
                    sl_distance[j] = calc_stop_distance(
                        sl_ref_price[j], sl_stop, stop_mode,
                        price_tick_list[j], current_size[j], volume_multiple_list[j]
                    )
                    if direction[j] == 1:
                        sl_price[j] = sl_ref_price[j] - sl_distance[j]
                    else:
                        sl_price[j] = sl_ref_price[j] + sl_distance[j]
                
                # ---- 0d. 动态TP回调：回调函数决定新的止盈距离 ----
                if not position_closed and use_tp_cb and tp_price[j] > 0:
                    _dir_cb = direction[j]
                    _entry_cb = entry_price[j]
                    _price_cb = current_price
                    _ref_cb = sl_ref_price[j]
                    _cur_dist_cb = tp_distance[j]
                    _tick_cb = price_tick_list[j]
                    _mult_cb = volume_multiple_list[j]
                    _sz_cb = current_size[j]
                    with nogil:
                        new_dist_cb = tp_cb(i, _dir_cb, _entry_cb, _price_cb,
                                           _ref_cb, _cur_dist_cb,
                                           _tick_cb, _mult_cb, _sz_cb,
                                           _tp_args_ptr, _tp_args_len)
                    if new_dist_cb > 0:
                        tp_distance[j] = new_dist_cb
                        if direction[j] == 1:
                            tp_price[j] = sl_ref_price[j] + new_dist_cb
                        else:
                            tp_price[j] = sl_ref_price[j] - new_dist_cb
                
                # ---- 1. 检查止损 ----
                if not position_closed and (sl_stop > 0 or use_sl_cb) and sl_price[j] > 0:
                    if direction[j] == 1:  # 多头止损：当前价 <= 止损价
                        if current_price <= sl_price[j]:
                            trade_price = sl_price[j]
                            if slip_point > 0:
                                trade_price -= slip_point
                            com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                            profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.
                            total_equity += profit
                            total_fee += com
                            position[j] = 0.0
                            in_position[j] = 0
                            direction[j] = 0
                            sl_price[j] = 0.0
                            tp_price[j] = 0.0
                            sl_ref_price[j] = 0.0
                            hold_bars[j] = 0
                            position_closed = 1
                    else:  # 空头止损：当前价 >= 止损价
                        if current_price >= sl_price[j]:
                            trade_price = sl_price[j]
                            if slip_point > 0:
                                trade_price += slip_point
                            com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                            profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.
                            total_equity += profit
                            total_fee += com
                            position[j] = 0.0
                            in_position[j] = 0
                            direction[j] = 0
                            sl_price[j] = 0.0
                            tp_price[j] = 0.0
                            sl_ref_price[j] = 0.0
                            hold_bars[j] = 0
                            position_closed = 1
                
                # ---- 2. 检查止盈 ----
                if not position_closed and (tp_stop > 0 or use_tp_cb) and tp_price[j] > 0:
                    if direction[j] == 1:  # 多头止盈：当前价 >= 止盈价
                        if current_price >= tp_price[j]:
                            trade_price = tp_price[j]
                            if slip_point > 0:
                                trade_price -= slip_point
                            com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                            profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.
                            total_equity += profit
                            total_fee += com
                            position[j] = 0.0
                            in_position[j] = 0
                            direction[j] = 0
                            sl_price[j] = 0.0
                            tp_price[j] = 0.0
                            sl_ref_price[j] = 0.0
                            hold_bars[j] = 0
                            position_closed = 1
                    else:  # 空头止盈：当前价 <= 止盈价
                        if current_price <= tp_price[j]:
                            trade_price = tp_price[j]
                            if slip_point > 0:
                                trade_price += slip_point
                            com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                            profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.
                            total_equity += profit
                            total_fee += com
                            position[j] = 0.0
                            in_position[j] = 0
                            direction[j] = 0
                            sl_price[j] = 0.0
                            tp_price[j] = 0.0
                            sl_ref_price[j] = 0.0
                            hold_bars[j] = 0
                            position_closed = 1
                
                # ---- 3. 持仓超时离场（max_hold_bars）----
                if not position_closed and max_hold_bars >= 1 and hold_bars[j] >= max_hold_bars:
                    # 以当前市价平仓（与出场信号一致的处理方式）
                    if slip_point > 0:
                        if direction[j] == 1:  # 多头平仓
                            trade_price = current_price - slip_point
                        else:  # 空头平仓
                            trade_price = current_price + slip_point
                    else:
                        trade_price = current_price
                    com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                    if direction[j] == 1:
                        profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com
                    else:
                        profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.
                    total_equity += profit
                    total_fee += com
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                    position_closed = 1
                
                # ---- 4. 检查出场信号 ----
                if not position_closed and exits[i, j] != 0:
                    # 确保出场信号与当前持仓方向一致
                    if (direction[j] == 1 and exits[i, j] == -1) or (direction[j] == -1 and exits[i, j] == 1):
                        # 处理滑点
                        if slip_point > 0:
                            if direction[j] == 1:  # 多头平仓
                                trade_price = current_price - slip_point
                            else:  # 空头平仓
                                trade_price = current_price + slip_point
                        else:
                            trade_price = current_price
                        
                        # 计算手续费
                        com = calculate_commission(trade_price, current_size[j], volume_multiple_list[j], commission_list[j], com_type, price_tick_list[j])
                        # 计算利润
                        if direction[j] == 1:
                            profit = (trade_price - entry_price[j]) * current_size[j] * volume_multiple_list[j] - com
                        else:
                            profit = (entry_price[j] - trade_price) * current_size[j] * volume_multiple_list[j] - com
                        pnl[j] = profit
                        cum_profit += profit
                        # 执行交易
                        current_cash += current_margin[j] + profit
                        current_margin[j]=0.
                        total_equity += profit
    
                        total_fee += com
                        position[j] = 0.0
                        in_position[j] = 0
                        direction[j] = 0
                        sl_price[j] = 0.0
                        tp_price[j] = 0.0
                        sl_ref_price[j] = 0.0
                        hold_bars[j] = 0
                        position_closed = 1
            
            # 保存结果 - 与minibt策略保持一致
            result[j][i, 0] = total_equity # total_profit
            result[j][i, 1] = position[j] # positions
            result[j][i, 2] = current_size[j] # sizes
            result[j][i, 3] = pnl[j] # float_profits
            result[j][i, 4] = cum_profit # cum_profits
            result[j][i, 5] = total_fee # total_fee
    
    return result
