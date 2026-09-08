# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# cython: initializedcheck=False
"""
多合约、多信号回测引擎 (Cython 加速)
======================================

特性:
  - 支持多合约并行回测，共享资金池
  - 支持不等长合约数据（NaN 填充尾部）
  - 配对交易：通过信号驱动（buy A + sell B），无需额外参数
  - 止损/止盈（固定 & 移动 & 回调函数）
  - 持仓时间限制（max_hold_bars）
  - 滑点、手续费、保证金模拟
  - 先遍历时间步，再遍历合约（与 minibt 策略一致）

输入数据格式:
  close:   (max_n, m) float64  — 各合约收盘价，短合约尾部填 NaN
  entries: (max_n, m) float64  — 入场信号，短合约尾部填 0
  exits:   (max_n, m) float64  — 出场信号，短合约尾部填 0
  prices:  (max_n, m) float64  — 成交价（可选，默认=close），短合约尾部填 NaN

输出:
  返回 list[ndarray]，每个合约一个 (max_n, 6) 的数组:
    [:, 0] = total_equity (总权益)
    [:, 1] = position (持仓方向: 1多头/-1空头/0空仓)
    [:, 2] = current_size (持仓手数)
    [:, 3] = pnl (当 bar 结算盈亏)
    [:, 4] = cum_profit (累计收益)
    [:, 5] = total_fee (累计手续费)

  对于合约已结束的 bar（close 为 NaN），若仍在持仓则自动市价平仓，
  position/size/pnl 随后设为 0（共享值正常记录）。

编译:
  python setup.py build_ext --inplace
"""

import numpy as np
cimport numpy as np
from libc.stdint cimport uintptr_t, int64_t
from libc.math cimport isnan


# =====================================================================
# 动态回调函数指针类型定义（与 callback.py 中 _STOP_CALLBACK_SIG 一致）
# =====================================================================
ctypedef double (*stop_callback_t)(
    long long bar_idx,
    long long direction,
    double entry_price,
    double current_price,
    double ref_price,
    double current_distance,
    double price_tick,
    double volume_multiple,
    double size,
    double* args_data,
    long long args_count,
) noexcept nogil


# =====================================================================
# 辅助函数
# =====================================================================

def calculate_commission(double price, double size, double volume_multiple,
                         double commission, int com_type, double price_tick):
    """计算单笔交易手续费。"""
    cdef double com = 0.0
    if com_type == 0:   # Tick
        com = commission * price_tick * volume_multiple * size
    elif com_type == 1: # Fixed
        com = commission * size
    elif com_type == 2: # Percent
        com = price * size * volume_multiple * commission
    return com


def calculate_size(double close_price, double size, int size_type,
                   double init_cash, double margin_rate, double volume_multiple):
    """计算交易手数。"""
    cdef double calculated_size = 0.0
    if size_type == 0:   # Amount（固定手数）
        calculated_size = size
    elif size_type == 1: # Value（固定金额 → 换算手数）
        calculated_size = size / (close_price * volume_multiple * margin_rate)
    elif size_type == 2: # Percent（总资金百分比 → 换算手数）
        calculated_size = (init_cash * size) / (close_price * volume_multiple * margin_rate)
    calculated_size = float(max(int(calculated_size), 1))
    return calculated_size


cdef double _calc_stop_distance(double ref_price, double stop_value, int mode,
                                double price_tick, double size, double vol_mult) noexcept:
    """根据模式计算止损/止盈的价格距离（从参考价出发的偏移量）。

    Returns:
        stop_distance: 价格偏移量（始终 >= 0），0 表示不启用
    """
    cdef double distance = 0.0
    if stop_value <= 0:
        return 0.0

    if mode == 0:   # Tick: 按最小波动单位
        distance = stop_value * price_tick
    elif mode == 1: # Amount: 按金额
        if size > 0 and vol_mult > 0:
            distance = stop_value / (size * vol_mult)
        else:
            distance = 0.0
    elif mode == 2: # Percent: 按合约价值百分比
        distance = ref_price * stop_value
    return distance


# ===== 回调 args 展平 =====
cdef np.ndarray[np.double_t, ndim=1] _build_flat_args(tuple args):
    """将 (标量, np数组, ...) 展平为单个 1D float64 数组。"""
    cdef list parts = []
    cdef np.ndarray part
    for a in args:
        part = np.atleast_1d(np.asarray(a, dtype=np.float64))
        for k in range(part.shape[0]):
            parts.append(float(part[k]))
    cdef np.ndarray[np.double_t, ndim=1] result = np.array(parts, dtype=np.float64)
    return result



# =====================================================================
# 参数展开辅助函数
# =====================================================================

cdef list _expand_param(object param, int m, str name):
    """将标量或列表参数统一展开为长度为 m 的 list[float]。

    - 标量 (int/float) → [val] * m
    - 单元素列表 (m > 1) → [val] * m
    - 多元素列表 → 逐元素转换
    """
    cdef list result
    if isinstance(param, (int, float)):
        result = [float(param) for _ in range(m)]
    elif len(param) == 1 and m > 1:
        result = [float(param[0]) for _ in range(m)]
    else:
        result = [float(p) for p in param]
    return result


# =====================================================================
# 主函数: 多合约多信号回测引擎
# =====================================================================

def multi_bt(
    np.ndarray close,                  # (max_n, m) float64，短合约尾部填 NaN
    np.ndarray entries,                # (max_n, m) float64
    np.ndarray exits,                  # (max_n, m) float64
    object size=1.0,                   # 标量 或 长度为 m 的列表
    int size_type=0,                   # 0=Amount, 1=Value, 2=Percent
    object margin_rate=[0.1],          # 标量 或 长度为 m 的列表
    object price_tick=[1.0],           # 标量 或 长度为 m 的列表
    object volume_multiple=[5.0],      # 标量 或 长度为 m 的列表
    np.ndarray prices=None,            # (max_n, m) float64，短合约尾部填 NaN
    int min_start_length=1,            # 前 N-1 bar 不产生信号
    float init_cash=1000000.0,         # 初始资金（共享）
    object commission=[1.0],           # 标量 或 长度为 m 的列表
    int com_type=1,                    # 0=Tick, 1=Fixed, 2=Percent
    float slip_point=0.0,              # 滑点（绝对值）
    # ===== 止损/止盈参数 =====
    float sl_stop=0.0,                 # 止损值，0=禁用
    float tp_stop=0.0,                 # 止盈值，0=禁用
    int stop_mode=0,                   # 0=Tick, 1=Amount, 2=Percent
    int sl_trail=0,                    # 移动止损: 0=否, 1=是
    # ===== 动态回调 =====
    long long sl_callback_addr=0,      # numba @cfunc .address
    long long tp_callback_addr=0,
    tuple sl_callback_args=(),
    tuple tp_callback_args=(),
    # ===== 持仓限制 =====
    int max_hold_bars=0,               # 最大持仓 K 线数，0=禁用
):
    """
    多合约信号回测引擎（Cython 加速版）。

    Parameters
    ----------
    close : ndarray (max_n, m)
        每个合约的收盘价。m=合约数，max_n=最大长度。
        短合约尾部填 np.nan。
    entries : ndarray (max_n, m)
        入场信号。>0 做多，<0 做空，0 无信号。短合约尾部填 0。
    exits : ndarray (max_n, m)
        出场信号。方向与持仓相反时触发平仓。短合约尾部填 0。
    size : float or list[float]
        手数参数。标量→全局，列表→每合约独立。
        配对交易时通过此参数为两条腿设置不同手数。
    size_type : int
        0=固定手数, 1=固定金额, 2=资金百分比。
    margin_rate : float or list
        保证金比例。标量/单元素→扩展到所有合约。
    price_tick : float or list
        最小变动价位。
    volume_multiple : float or list
        合约乘数。
    prices : ndarray (max_n, m), optional
        成交价，默认=close。用于指定开仓/平仓时实际成交价。
    min_start_length : int
        前 N-1 bar 不产生任何交易。
    init_cash : float
        初始资金（所有合约共享）。
    commission : float or list
        手续费参数。
    com_type : int
        0=Tick, 1=Fixed, 2=Percent。
    slip_point : float
        滑点（绝对值），做多入场+滑点，出场-滑点；做空反之。
    sl_stop : float
        止损值，0=不启用。
    tp_stop : float
        止盈值，0=不启用。
    stop_mode : int
        止损/止盈计算模式: 0=Tick, 1=金额, 2=百分比。
    sl_trail : int
        是否启用移动止损: 0=否, 1=是。
    sl_callback_addr : int
        numba @cfunc 回调地址（止损动态调整）。
    tp_callback_addr : int
        numba @cfunc 回调地址（止盈动态调整）。
    sl_callback_args : tuple
        止损回调的额外参数。
    tp_callback_args : tuple
        止盈回调的额外参数。
    max_hold_bars : int
        最大持仓 K 线数，超过后强制平仓。0=禁用。

    Returns
    -------
    list[ndarray]
        每个合约一个 (max_n, 6) 的 float64 数组:
        [:, 0] = total_equity
        [:, 1] = position
        [:, 2] = current_size
        [:, 3] = pnl
        [:, 4] = cum_profit
        [:, 5] = total_fee
    """
    # ===== 维度获取与校验 =====
    cdef int max_n = close.shape[0]
    cdef int m = close.shape[1]

    if entries.shape[0] != max_n or entries.shape[1] != m:
        raise ValueError(
            "entries shape (%d, %d) 与 close (%d, %d) 不匹配" %
            (entries.shape[0], entries.shape[1], max_n, m))
    if exits.shape[0] != max_n or exits.shape[1] != m:
        raise ValueError(
            "exits shape (%d, %d) 与 close (%d, %d) 不匹配" %
            (exits.shape[0], exits.shape[1], max_n, m))

    # ===== prices 默认值 =====
    if prices is None:
        prices = close
    elif prices.shape[0] != max_n or prices.shape[1] != m:
        raise ValueError(
            "prices shape (%d, %d) 与 close (%d, %d) 不匹配" %
            (prices.shape[0], prices.shape[1], max_n, m))

    # ===== 展开每合约参数 =====
    cdef list size_list        = _expand_param(size,           m, "size")
    cdef list margin_rate_list = _expand_param(margin_rate,    m, "margin_rate")
    cdef list price_tick_list  = _expand_param(price_tick,     m, "price_tick")
    cdef list vol_mult_list    = _expand_param(volume_multiple, m, "volume_multiple")
    cdef list commission_list  = _expand_param(commission,     m, "commission")

    # ===== 回调函数指针 =====
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

    # ===== 回调附加参数 → flat double 数组 =====
    cdef np.ndarray[np.double_t, ndim=1] _sl_args_arr
    cdef np.ndarray[np.double_t, ndim=1] _tp_args_arr
    if sl_callback_args:
        _sl_args_arr = _build_flat_args(sl_callback_args)
    else:
        _sl_args_arr = np.zeros(0, dtype=np.float64)
    if tp_callback_args:
        _tp_args_arr = _build_flat_args(tp_callback_args)
    else:
        _tp_args_arr = np.zeros(0, dtype=np.float64)
    cdef double* _sl_args_ptr = <double*>_sl_args_arr.data if _sl_args_arr.shape[0] > 0 else NULL
    cdef long long _sl_args_len = _sl_args_arr.shape[0]
    cdef double* _tp_args_ptr = <double*>_tp_args_arr.data if _tp_args_arr.shape[0] > 0 else NULL
    cdef long long _tp_args_len = _tp_args_arr.shape[0]

    # ===== 结果数组 =====
    cdef list result = []
    cdef int j
    for j in range(m):
        result.append(np.zeros((max_n, 6), dtype=np.float64))

    # ===== 全局账户变量 =====
    cdef double current_cash = init_cash
    cdef double cum_profit = 0.0
    cdef double total_equity = init_cash
    cdef double total_fee = 0.0

    # ===== 每合约状态变量 =====
    cdef list pnl              = [0.0 for _ in range(m)]
    cdef list position         = [0.0 for _ in range(m)]
    cdef list current_size     = [0.0 for _ in range(m)]
    cdef list entry_price      = [0.0 for _ in range(m)]
    cdef list in_position      = [0   for _ in range(m)]
    cdef list direction        = [0   for _ in range(m)]
    cdef list current_margin   = [0.0 for _ in range(m)]

    # ===== 止损/止盈跟踪 =====
    cdef list sl_price    = [0.0 for _ in range(m)]
    cdef list tp_price    = [0.0 for _ in range(m)]
    cdef list sl_ref_price = [0.0 for _ in range(m)]
    cdef list sl_distance  = [0.0 for _ in range(m)]
    cdef list tp_distance  = [0.0 for _ in range(m)]

    # ===== 持仓 K 线计数器 =====
    cdef list hold_bars = [0 for _ in range(m)]

    # ===== 局部变量（循环中复用） =====
    cdef int i
    cdef double current_close, current_price, trade_price, com, profit, margin
    cdef double new_dist_cb
    cdef int position_closed
    cdef int _dir_cb
    cdef double _entry_cb, _price_cb, _ref_cb, _cur_dist_cb
    cdef double _tick_cb, _mult_cb, _sz_cb

    # =================================================================
    # 主循环: 先遍历时间步，再遍历合约
    # =================================================================
    for i in range(max_n):
        # 重置每个合约的结算盈亏
        for j in range(m):
            pnl[j] = 0.0

        # ===== 逐合约处理 =====
        for j in range(m):
            # ---- 检查合约是否已结束（close 为 NaN）----
            if isnan(close[i, j]):
                # 合约数据已结束，若仍在持仓则强制平仓
                if in_position[j]:
                    if slip_point > 0:
                        if direction[j] == 1:
                            trade_price = prices[i, j] - slip_point
                        else:
                            trade_price = prices[i, j] + slip_point
                    else:
                        trade_price = prices[i, j]
                    com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                               commission_list[j], com_type, price_tick_list[j])
                    if direction[j] == 1:
                        profit = (trade_price - entry_price[j]) * current_size[j] * vol_mult_list[j] - com
                    else:
                        profit = (entry_price[j] - trade_price) * current_size[j] * vol_mult_list[j] - com
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
                    total_equity += profit
                    total_fee += com
                    position[j] = 0.0
                    in_position[j] = 0
                    direction[j] = 0
                    sl_price[j] = 0.0
                    tp_price[j] = 0.0
                    sl_ref_price[j] = 0.0
                    hold_bars[j] = 0
                # 记录当前共享状态
                result[j][i, 0] = total_equity
                result[j][i, 1] = 0.0
                result[j][i, 2] = 0.0
                result[j][i, 3] = pnl[j]
                result[j][i, 4] = cum_profit
                result[j][i, 5] = total_fee
                continue

            # ---- 跳过 min_start_length 之前的 bar ----
            if i < min_start_length - 1:
                result[j][i, 0] = total_equity
                result[j][i, 1] = position[j]
                result[j][i, 2] = current_size[j]
                result[j][i, 3] = pnl[j]
                result[j][i, 4] = cum_profit
                result[j][i, 5] = total_fee
                continue

            current_close = close[i, j]
            current_price = prices[i, j]

            # ========== 入场信号 ==========
            if not in_position[j] and entries[i, j] != 0:
                direction[j] = <int>entries[i, j]

                # 滑点
                if slip_point > 0:
                    if direction[j] == 1:
                        trade_price = current_price + slip_point
                    else:
                        trade_price = current_price - slip_point
                else:
                    trade_price = current_price

                # 计算手数
                current_size[j] = calculate_size(trade_price, size_list[j], size_type,
                                                 current_cash, margin_rate_list[j],
                                                 vol_mult_list[j])

                # 手续费 & 保证金 & 资金检查
                com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                           commission_list[j], com_type, price_tick_list[j])
                margin = trade_price * current_size[j] * vol_mult_list[j] * margin_rate_list[j]

                if current_cash >= margin + com:
                    current_margin[j] = margin
                    total_equity -= com
                    current_cash -= com + margin
                    total_fee += com
                    cum_profit -= com
                    position[j] = direction[j]
                    entry_price[j] = trade_price
                    in_position[j] = 1
                    hold_bars[j] = 0

                    # 初始化止损/止盈
                    if sl_stop > 0 or use_sl_cb:
                        sl_ref_price[j] = trade_price
                        if sl_stop > 0:
                            sl_distance[j] = _calc_stop_distance(
                                trade_price, sl_stop, stop_mode,
                                price_tick_list[j], current_size[j], vol_mult_list[j])
                        else:
                            sl_distance[j] = 0.0
                        if direction[j] == 1:
                            sl_price[j] = trade_price - sl_distance[j]
                        else:
                            sl_price[j] = trade_price + sl_distance[j]
                    else:
                        sl_price[j] = 0.0
                        sl_ref_price[j] = 0.0
                        sl_distance[j] = 0.0

                    if tp_stop > 0 or use_tp_cb:
                        if tp_stop > 0:
                            tp_distance[j] = _calc_stop_distance(
                                trade_price, tp_stop, stop_mode,
                                price_tick_list[j], current_size[j], vol_mult_list[j])
                        else:
                            tp_distance[j] = 0.0
                        if direction[j] == 1:
                            tp_price[j] = trade_price + tp_distance[j]
                        else:
                            tp_price[j] = trade_price - tp_distance[j]
                    else:
                        tp_price[j] = 0.0
                        tp_distance[j] = 0.0

            # ========== 持仓中：出场检查 ==========
            elif in_position[j]:
                position_closed = 0
                hold_bars[j] += 1

                # ---- 移动止损参考价更新 ----
                if not position_closed and sl_trail and (sl_stop > 0 or use_sl_cb):
                    if direction[j] == 1:
                        if current_price > sl_ref_price[j]:
                            sl_ref_price[j] = current_price
                    else:
                        if current_price < sl_ref_price[j]:
                            sl_ref_price[j] = current_price

                # ---- SL 回调 ----
                if not position_closed and use_sl_cb:
                    _dir_cb = direction[j]
                    _entry_cb = entry_price[j]
                    _price_cb = current_price
                    _ref_cb = sl_ref_price[j]
                    _cur_dist_cb = sl_distance[j]
                    _tick_cb = price_tick_list[j]
                    _mult_cb = vol_mult_list[j]
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

                # ---- 固定止损更新（无回调时的 trailing）----
                if not position_closed and not use_sl_cb and sl_trail and sl_stop > 0:
                    sl_distance[j] = _calc_stop_distance(
                        sl_ref_price[j], sl_stop, stop_mode,
                        price_tick_list[j], current_size[j], vol_mult_list[j])
                    if direction[j] == 1:
                        sl_price[j] = sl_ref_price[j] - sl_distance[j]
                    else:
                        sl_price[j] = sl_ref_price[j] + sl_distance[j]

                # ---- TP 回调 ----
                if not position_closed and use_tp_cb and tp_price[j] > 0:
                    _dir_cb = direction[j]
                    _entry_cb = entry_price[j]
                    _price_cb = current_price
                    _ref_cb = sl_ref_price[j]
                    _cur_dist_cb = tp_distance[j]
                    _tick_cb = price_tick_list[j]
                    _mult_cb = vol_mult_list[j]
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

                # ---- 1. 止损检查 ----
                if not position_closed and (sl_stop > 0 or use_sl_cb) and sl_price[j] > 0:
                    if direction[j] == 1:   # 多头止损
                        if current_price <= sl_price[j]:
                            trade_price = sl_price[j]
                            if slip_point > 0:
                                trade_price -= slip_point
                            com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                                       commission_list[j], com_type, price_tick_list[j])
                            profit = (trade_price - entry_price[j]) * current_size[j] * vol_mult_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.0
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
                    else:                   # 空头止损
                        if current_price >= sl_price[j]:
                            trade_price = sl_price[j]
                            if slip_point > 0:
                                trade_price += slip_point
                            com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                                       commission_list[j], com_type, price_tick_list[j])
                            profit = (entry_price[j] - trade_price) * current_size[j] * vol_mult_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.0
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

                # ---- 2. 止盈检查 ----
                if not position_closed and (tp_stop > 0 or use_tp_cb) and tp_price[j] > 0:
                    if direction[j] == 1:   # 多头止盈
                        if current_price >= tp_price[j]:
                            trade_price = tp_price[j]
                            if slip_point > 0:
                                trade_price -= slip_point
                            com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                                       commission_list[j], com_type, price_tick_list[j])
                            profit = (trade_price - entry_price[j]) * current_size[j] * vol_mult_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.0
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
                    else:                   # 空头止盈
                        if current_price <= tp_price[j]:
                            trade_price = tp_price[j]
                            if slip_point > 0:
                                trade_price += slip_point
                            com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                                       commission_list[j], com_type, price_tick_list[j])
                            profit = (entry_price[j] - trade_price) * current_size[j] * vol_mult_list[j] - com
                            pnl[j] = profit
                            cum_profit += profit
                            current_cash += current_margin[j] + profit
                            current_margin[j] = 0.0
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

                # ---- 3. 持仓超时离场 ----
                if not position_closed and max_hold_bars >= 1 and hold_bars[j] >= max_hold_bars:
                    if slip_point > 0:
                        if direction[j] == 1:
                            trade_price = current_price - slip_point
                        else:
                            trade_price = current_price + slip_point
                    else:
                        trade_price = current_price
                    com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                               commission_list[j], com_type, price_tick_list[j])
                    if direction[j] == 1:
                        profit = (trade_price - entry_price[j]) * current_size[j] * vol_mult_list[j] - com
                    else:
                        profit = (entry_price[j] - trade_price) * current_size[j] * vol_mult_list[j] - com
                    pnl[j] = profit
                    cum_profit += profit
                    current_cash += current_margin[j] + profit
                    current_margin[j] = 0.0
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

                # ---- 4. 出场信号 ----
                if not position_closed and exits[i, j] != 0:
                    if (direction[j] == 1 and exits[i, j] == -1) or (direction[j] == -1 and exits[i, j] == 1):
                        if slip_point > 0:
                            if direction[j] == 1:
                                trade_price = current_price - slip_point
                            else:
                                trade_price = current_price + slip_point
                        else:
                            trade_price = current_price
                        com = calculate_commission(trade_price, current_size[j], vol_mult_list[j],
                                                   commission_list[j], com_type, price_tick_list[j])
                        if direction[j] == 1:
                            profit = (trade_price - entry_price[j]) * current_size[j] * vol_mult_list[j] - com
                        else:
                            profit = (entry_price[j] - trade_price) * current_size[j] * vol_mult_list[j] - com
                        pnl[j] = profit
                        cum_profit += profit
                        current_cash += current_margin[j] + profit
                        current_margin[j] = 0.0
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

            # ---- 记录结果 ----
            result[j][i, 0] = total_equity
            result[j][i, 1] = position[j]
            result[j][i, 2] = current_size[j]
            result[j][i, 3] = pnl[j]
            result[j][i, 4] = cum_profit
            result[j][i, 5] = total_fee


    return result
