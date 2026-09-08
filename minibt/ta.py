# -*- coding: utf-8 -*-
import pandas_ta as pta
# get_offset, , verify_series, get_drift,
from pandas_ta.utils import non_zero_range, v_offset, v_drift
from pandas_ta.core import AnalysisIndicators  # , BasePandasObject, pd
# from pandas_ta.core import *
from inspect import signature, _empty
from copy import deepcopy
from collections.abc import Sequence, Generator, Iterable
from .other import *
from . import zigzag as zig
import math
import statsmodels.api as sm
from scipy import stats as scipy_stats
import scipy.signal as scipy_signal
from functools import partial, reduce
from joblib import Parallel, delayed
from numpy.lib.stride_tricks import as_strided
from numpy.version import version as npVersion
from numpy_ext import prepend_na, rolling
from typing import Any
from numpy import array as npArray
from numpy.random import RandomState
from .ta_ import LazyImport, Callable
if npVersion >= "1.20.0":
    from numpy.lib.stride_tricks import sliding_window_view
np.seterr(divide='ignore', invalid='ignore')
npInf = np.inf


def try_to_series(data):
    if isinstance(data, pd.Series):
        data = data.astype(np.float64)
    else:
        if isinstance(data, Iterable):
            data = pd.Series(data)
            data = data.astype(np.float64)
    return data


# def pad_array_to_match(array: Union[np.ndarray, tuple[np.ndarray]], length):
#     if isinstance(array, tuple):
#         array = np.hstack(array)
#     len_diff = length-len(array)
#     if len_diff <= 0:
#         return array
#     if array.ndim == 1:
#         return np.concatenate((np.full(len_diff, np.nan), array))
#     else:
#         return np.hstack((np.full((len_diff, array.shape[1]), np.nan), array))


def pad_array_to_match(array: np.ndarray | tuple[np.ndarray], length: int):
    """
    ## 将数组填充到指定长度，如果长度不够则在前面用NaN填充

    ### 参数:
    >>> array: np.ndarray 或 tuple - 要填充的数组或元组
            如果是元组，假设每个元素都是独立的数组（例如布林带的上中下轨）
            需要将它们堆叠成一个二维数组，每列代表一个数组
        length: int - 目标长度（行数）

    ### 返回:
    >>> np.ndarray: 填充后的数组，形状为 (length, n_columns)
    """
    # 如果是元组，处理多个数组
    if isinstance(array, tuple):
        if len(array) == 0:
            # 空元组，返回全NaN的二维数组（3列）
            return np.full((length, 3), np.nan)

        # 检查每个数组的维度
        arrays_to_stack = []
        for arr in array:
            if arr.ndim == 1:
                # 一维数组转换为列向量
                arr = arr.reshape(-1, 1)
            arrays_to_stack.append(arr)

        # 水平堆叠（按列连接）
        try:
            # 尝试水平堆叠
            array_concat = np.hstack(arrays_to_stack)
        except Exception as e:
            # 如果水平堆叠失败，可能是因为长度不一致
            # 先统一长度
            max_len = max(arr.shape[0] for arr in arrays_to_stack)
            padded_arrays = []
            for arr in arrays_to_stack:
                if arr.shape[0] < max_len:
                    # 在开头填充NaN
                    pad_rows = max_len - arr.shape[0]
                    padded_arr = np.vstack(
                        [np.full((pad_rows, arr.shape[1]), np.nan), arr])
                    padded_arrays.append(padded_arr)
                else:
                    padded_arrays.append(arr)
            array_concat = np.hstack(padded_arrays)

        array = array_concat

    # 现在array是单个numpy数组
    # 确保是二维数组
    if array.ndim == 1:
        array = array.reshape(-1, 1)

    current_len = array.shape[0]
    len_diff = length - current_len

    if len_diff <= 0:
        # 如果当前长度已经 >= 目标长度，直接返回（或截断）
        if len_diff < 0:
            # 截断多余的部分
            return array[-length:]
        return array

    # 填充NaN值（在前面添加NaN行）
    # 注意：要保持列数不变
    nan_rows = np.full((len_diff, array.shape[1]), np.nan)
    return np.vstack([nan_rows, array])  # 使用vstack垂直堆叠


def cum(series: pd.Series, length=10, **kwargs) -> pd.Series:
    return series.rolling(length).sum()


def ZeroDivision(a: np.ndarray, b: np.ndarray = 1., dtype=np.float64, fill_value=0.0, handle_inf=True, **kwargs) -> np.ndarray:
    """
    ## 安全的数组除法，避免除零错误和无穷大问题

    Args:
        a: 被除数数组（支持 pandas Series）
        b: 除数，可以是标量或数组（支持 pandas Series）
        dtype: 输出数据类型
        fill_value: 除零或无效除法时填充的值
        handle_inf: 是否处理无穷大情况
        **kwargs: 其他参数

    Returns:
        a / b 的结果，无效位置用 fill_value 填充
    """
    # 快速路径：b 是标量且安全
    if np.isscalar(b) and b != 0 and (not handle_inf or np.isfinite(b)):
        if hasattr(a, "values"):
            a = a.values
        return np.asarray(a, dtype=dtype) / b

    # 常规路径
    if hasattr(a, "values"):
        a = a.values
    if hasattr(b, "values"):
        b = b.values

    a = np.asarray(a, dtype=dtype)
    b = np.asarray(b, dtype=dtype)

    # 创建结果数组并用 fill_value 初始化
    result = np.full_like(a, fill_value, dtype=dtype)

    # 构建有效掩码
    valid_mask = b != 0
    if handle_inf:
        valid_mask = valid_mask & np.isfinite(b)

    # 执行安全的除法
    result[valid_mask] = a[valid_mask] / b[valid_mask]

    return result


def _linreg(close, length=None, offset=None, **kwargs):
    """Indicator: Linear Regression"""
    # Validate arguments
    length = int(length) if length and length > 0 else 14
    # close = verify_series(close, length)
    offset = v_offset(offset)
    angle = kwargs.pop("angle", False)
    intercept = kwargs.pop("intercept", False)
    degrees = kwargs.pop("degrees", False)
    r = kwargs.pop("r", False)
    slope = kwargs.pop("slope", False)
    tsf = kwargs.pop("tsf", False)

    if close is None:
        return

    # Calculate Result
    x = range(1, length + 1)  # [1, 2, ..., n] from 1 to n keeps Sum(xy) low
    x_sum = 0.5 * length * (length + 1)
    x2_sum = x_sum * (2 * length + 1) / 3
    divisor = length * x2_sum - x_sum * x_sum

    def linear_regression(series):
        if (series == None).any():
            series = pd.Series(series).bfill()
            series = series.values
        y_sum = series.sum()
        xy_sum = (x * series).sum()

        m = (length * xy_sum - x_sum * y_sum) / divisor
        if slope:
            return m
        b = (y_sum * x2_sum - x_sum * xy_sum) / divisor
        if intercept:
            return b

        if angle:
            theta = np.arctan(m)
            if degrees:
                theta *= 180 / np.pi
            return theta

        if r:
            y2_sum = (series * series).sum()
            rn = length * xy_sum - x_sum * y_sum
            rd = (divisor * (length * y2_sum - y_sum * y_sum)) ** 0.5
            return rn / rd

        return m * length + b if tsf else m * (length - 1) + b

    def rolling_window(array, length):
        """https://github.com/twopirllc/pandas-ta/issues/285"""
        strides = array.strides + (array.strides[-1],)
        shape = array.shape[:-1] + (array.shape[-1] - length + 1, length)
        return as_strided(array, shape=shape, strides=strides)

    if npVersion >= "1.20.0":
        linreg_ = [linear_regression(
            _) for _ in sliding_window_view(npArray(close), length)]
    else:
        linreg_ = [linear_regression(_)
                   for _ in rolling_window(npArray(close), length)]

    linreg = pd.Series([npInf] * (length - 1) + linreg_, index=close.index)

    # Offset
    if offset != 0:
        linreg = linreg.shift(offset)

    # Handle fills
    if "fillna" in kwargs:
        linreg.fillna(kwargs["fillna"], inplace=True)
    if "fill_method" in kwargs:
        _method = kwargs["fill_method"]
        if _method in ('ffill', 'pad'):
            linreg.ffill(inplace=True)
        elif _method in ('bfill', 'backfill'):
            linreg.bfill(inplace=True)

    # Name and Categorize it
    linreg.name = f"LR"
    if slope:
        linreg.name += "m"
    if intercept:
        linreg.name += "b"
    if angle:
        linreg.name += "a"
    if r:
        linreg.name += "r"

    linreg.name += f"_{length}"
    linreg.category = "overlap"

    return linreg


# 原函数有BUG
pta.linreg = _linreg


def prepend_na(arr: np.ndarray, n: int) -> np.ndarray:
    """在数组前添加n个NaN值，确保添加后长度合法"""
    if n <= 0:
        return arr
    n = min(n, len(arr) + n)  # 冗余保护，防止n异常
    na_arr = np.full(n, np.nan)
    return np.concatenate([na_arr, arr])


def rolling_apply(
    df: pd.DataFrame | pd.Series,
    func: Callable,
    window: int | np.integer | list[int] | np.ndarray,
    prepend_nans: bool = True,
    n_jobs: int = 1,
    **kwargs
) -> np.ndarray:
    """
    滚动窗口应用函数

    对输入的DataFrame或Series应用滚动窗口计算，支持固定窗口和可变窗口大小，
    可并行处理提高计算效率。

    Args:
        df: 输入数据，支持pandas DataFrame或Series
        func: 应用于每个滚动窗口的函数
        window: 窗口大小，可以是固定整数或与数据长度相同的窗口大小数组
        prepend_nans: 是否在结果前填充NaN以匹配原始数据长度
        n_jobs: 并行工作进程数，1表示串行执行，>1表示并行执行
        **kwargs: 传递给func的额外参数

    Returns:
        np.ndarray: 函数应用于每个窗口的结果数组

    Raises:
        ValueError: 输入数据为空或全为NaN时抛出
        TypeError: 窗口参数类型不正确时抛出
        AssertionError: 函数没有必填参数时抛出

    Example:
        >>> # 计算滚动平均值
        >>> df = pd.DataFrame({'A': [1, 2, 3, 4, 5]})
        >>> result = rolling_apply(df, np.mean, window=3)
        >>> # 使用可变窗口
        >>> window_sizes = [2, 3, 2, 3, 2]
        >>> result = rolling_apply(df, np.mean, window=window_sizes)
    """
    # ==================== 数据预处理 ====================
    # 统一数据格式：处理可能的包装对象
    df = df._df if hasattr(df, "_df") else df
    df = df.pandas_object if hasattr(df, "pandas_object") else df
    input_len = len(df)
    # 统一窗口数据提取方式
    if hasattr(window, "values"):
        window = window.values

    # ==================== 输入验证 ====================
    # 检查输入数据有效性
    if isinstance(df, pd.Series):
        data_vals = df.values
    else:
        data_vals = df.values.ravel()

    if np.all(pd.isnull(data_vals)):
        raise ValueError("输入数据全为NaN，请检查数据源")
    if input_len == 0:
        raise ValueError("输入数据为空，请检查数据源")

    # 检查函数参数（确保func有必填参数）
    try:
        sig = signature(func)
        required_params = [k for k, v in sig.parameters.items()
                           if v.default == v.empty]
        assert required_params, f"函数{func.__name__}请设置必填参数（不可全为默认值）"
    except (ValueError, TypeError) as e:
        # 处理无法获取签名的函数（如C扩展函数）
        required_params = []
        if not kwargs:
            print(f"警告: 无法检查函数{func.__name__}的参数签名")

    # ==================== 窗口参数处理 ====================
    # 处理窗口参数：统一转为数组，确保长度与输入数据一致
    if isinstance(window, (int, np.integer)):
        window = np.full(input_len, int(window))  # 固定窗口→数组（长度=输入长度）
    elif isinstance(window, (list, np.ndarray)):
        # 固定窗口→数组（长度=输入长度）
        window = np.array(window, dtype=int)
        if len(window) != input_len:
            raise ValueError(
                f"窗口序列长度({len(window)})必须与输入数据长度({input_len})一致，"
                f"请调整窗口序列长度"
            )
        if not np.all(window > 0):
            raise ValueError("窗口大小必须为正整数（不可为0或负数）")
        # 限制窗口不超过数据总长度，避免索引错误
        window = np.minimum(window, input_len)  # 窗口不超过数据总长度
    else:
        raise TypeError(
            f"窗口类型错误({type(window)})，"
            f"支持类型：int、List[int]、np.ndarray[int]"
        )
    # 预绑定kwargs参数到函数
    if kwargs:
        func = partial(func, **kwargs)

    # ==================== 滚动计算核心逻辑 ====================

    # --------------------------
    # 1. 处理Series类型输入（核心修改：适配无NaN填充的窗口）
    # --------------------------
    if isinstance(df, pd.Series):
        # 生成无NaN填充的窗口列表（每个元素是当前索引的有效数据窗口）
        rolls = _rolling_window_1D(df.values, window, prepend_nans)
        # 执行函数计算（直接传入有效窗口，无额外NaN）
        if n_jobs == 1:
            arr = list(map(func, rolls))
        else:
            arr = Parallel(n_jobs=n_jobs)(
                delayed(func)(roll) for roll in rolls
            )
        result = np.array(arr)

    # --------------------------
    # 2. 处理DataFrame类型输入（同理适配无NaN填充）
    # --------------------------
    elif isinstance(df, pd.DataFrame) and all([col in df.columns for col in required_params]):
        if df.shape[1] == 1:
            # 单列DataFrame→按Series逻辑处理
            rolls = _rolling_window_1D(df.values[:, 0], window, prepend_nans)
            if n_jobs == 1:
                # arr = [func(roll, **kwargs) for roll in rolls]
                arr = list(map(func, rolls))
            else:
                arr = Parallel(n_jobs=n_jobs)(
                    delayed(func)(roll) for roll in rolls
                )
        else:
            # 多列DataFrame→按列生成有效窗口后合并
            df_filtered = df[required_params]
            # 每列生成无NaN填充的窗口，结果为：[列1窗口列表, 列2窗口列表, ...]
            col_rolls = [_rolling_window_1D(
                df_filtered[col].values, window, prepend_nans) for col in df_filtered.columns]
            # 按索引对齐多列窗口（每个索引对应多列的有效窗口）
            merged_rolls = zip(*col_rolls)
            if n_jobs == 1:
                arr = [func(*cols) for cols in merged_rolls]
            else:
                arr = Parallel(n_jobs=n_jobs)(
                    delayed(func)(*cols) for cols in merged_rolls
                )
        result = np.array(arr)

    # --------------------------
    # 3. 其他数据类型（兜底处理，同样移除NaN填充）
    # --------------------------
    else:
        # 统一的2D数据处理路径
        col_rolls = _rolling_window_2D(df.values, window, prepend_nans)
        if n_jobs == 1:
            arr = list(map(func, col_rolls))
        else:
            arr = Parallel(n_jobs=n_jobs)(delayed(func)(
                cols) for cols in col_rolls)
        result = np.array(arr)

    return result


# --------------------------
# 核心修改：_rolling_window_1D（移除所有无意义的NaN填充）
# 功能：生成"仅包含有效数据"的窗口列表，每个窗口长度=当前索引的窗口大小
# --------------------------
def _rolling_window_1D(v: np.ndarray, window: np.ndarray, prepend_nans=True) -> list[np.ndarray]:
    """
    生成1D数组的滚动窗口（无NaN填充）
    参数：
        v: 输入1D数组
        window: 窗口大小数组（长度=len(v)，每个元素为对应索引的窗口大小）
    返回：
        窗口列表：每个元素是当前索引的有效数据窗口（长度=window[i]）
    """
    input_len = len(v)
    windows = []
    for i in range(input_len):
        # 当前索引的窗口大小（已确保<=input_len）
        w = window[i]
        # 计算窗口起始索引（确保不小于0）
        start_idx = max(0, i - w + 1)
        # 提取有效数据窗口（无任何NaN填充）
        valid_window = v[start_idx:i+1]
        if prepend_nans and valid_window.size < w:
            # 计算需要补充的NaN数量
            pad_length = w - valid_window.size
            # 左侧补NaN（第一个参数为左侧填充数，第二个为右侧填充数）
            valid_window = np.pad(
                valid_window,
                (pad_length, 0),  # 关键修改：左侧补pad_length个NaN，右侧不补
                mode="constant",
                constant_values=np.nan
            )
        windows.append(valid_window)
    return windows


def _rolling_window_2D(v: np.ndarray, window: np.ndarray, prepend_nans=True) -> list[np.ndarray]:
    """生成2D数组的滚动窗口（作为整体二维数组返回）"""
    rows, cols = v.shape
    windows = []
    for i in range(rows):
        w = window[i]
        start_idx = max(0, i - w + 1)
        # 提取窗口内的所有列数据，shape=(window_size, cols)
        valid_window = v[start_idx:i+1, :]
        if prepend_nans and len(valid_window) < w:
            # 计算需要补充的NaN数量
            pad_length = w - len(valid_window)
            # 左侧补NaN（第一个参数为左侧填充数，第二个为右侧填充数）
            valid_window = np.pad(
                valid_window,
                ((pad_length, 0), (0, 0)),  # 关键修改：左侧补pad_length个NaN，右侧不补
                mode="constant",
                constant_values=np.nan
            )
        windows.append(valid_window)
    return windows


def _stoch(high, low, close, k=None, d=None, smooth_k=None, mamode=None, offset=None, **kwargs):
    """Indicator: Stochastic Oscillator (STOCH)"""
    # Validate arguments
    k = k if k and k > 0 else 14
    d = d if d and d > 0 else 3
    smooth_k = smooth_k if smooth_k and smooth_k > 0 else 3
    _length = max(k, d, smooth_k)
    # high = verify_series(high, _length)
    # low = verify_series(low, _length)
    # close = verify_series(close, _length)
    # offset = get_offset(offset)
    offset = v_offset(offset)
    mamode = mamode if isinstance(mamode, str) else "sma"

    if high is None or low is None or close is None:
        return

    # Calculate Result
    lowest_low = low.rolling(k).min()
    highest_high = high.rolling(k).max()

    stoch = 100 * (close - lowest_low)
    stoch /= non_zero_range(highest_high, lowest_low)
    # pandas_ta原指标转入长度与原长度不一致
    # stoch_k = ma(mamode, stoch.loc[stoch.first_valid_index():,], length=smooth_k)
    # stoch_d = ma(mamode, stoch_k.loc[stoch_k.first_valid_index():,], length=d)
    stoch_k = pta.ma(mamode, stoch, length=smooth_k)
    stoch_d = pta.ma(mamode, stoch_k, length=d)
    # Offset
    if offset != 0:
        stoch = stoch.shift(offset)
        stoch_k = stoch_k.shift(offset)
        stoch_d = stoch_d.shift(offset)
    # Handle fills
    if "fillna" in kwargs:
        stoch.fillna(kwargs["fillna"], inplace=True)
        stoch_k.fillna(kwargs["fillna"], inplace=True)
        stoch_d.fillna(kwargs["fillna"], inplace=True)
    if "fill_method" in kwargs:
        _method = kwargs["fill_method"]
        if _method in ('ffill', 'pad'):
            stoch.ffill(inplace=True)
            stoch_k.ffill(inplace=True)
            stoch_d.ffill(inplace=True)
        elif _method in ('bfill', 'backfill'):
            stoch.bfill(inplace=True)
            stoch_k.bfill(inplace=True)
            stoch_d.bfill(inplace=True)

    # Name and Categorize it
    _name = "STOCH"
    _props = f"_{k}_{d}_{smooth_k}"
    stoch_k.name = f"{_name}k{_props}"
    stoch_d.name = f"{_name}d{_props}"
    stoch_k.category = stoch_d.category = "momentum"

    # Prepare DataFrame to return
    data = {stoch.name: stoch, stoch_k.name: stoch_k, stoch_d.name: stoch_d}
    df = pd.DataFrame(data)
    df.name = f"{_name}{_props}"
    df.category = stoch_k.category
    return df


# 原函数有BUG
pta.stoch = _stoch


def _mcgd(close, length=None, offset=None, c=None, **kwargs):
    """Indicator: McGinley Dynamic Indicator"""
    # Validate arguments
    length = int(length) if length and length > 0 else 10
    c = float(c) if c and 0 < c <= 1 else 1
    # close = verify_series(close, length)
    # offset = get_offset(offset)
    offset = v_offset(offset)

    if close is None:
        return

    # Calculate Result
    close = close.copy()

    def mcg_(series):
        denom = (c * length * (series.iloc[1] / series.iloc[0]) ** 4)
        series.iloc[1] = (
            series.iloc[0] + ((series.iloc[1] - series.iloc[0]) / denom))
        return series.iloc[1]

    mcg_cell = close[0:].rolling(2, min_periods=2).apply(mcg_, raw=False)

    # 使用 pd.concat() 替代已弃用的 append()
    mcg_ds = pd.concat([close[:1], mcg_cell[1:]])

    # 确保结果按原始索引排序
    mcg_ds = mcg_ds.sort_index()

    # Offset
    if offset != 0:
        mcg_ds = mcg_ds.shift(offset)

    # Handle fills
    if "fillna" in kwargs:
        mcg_ds.fillna(kwargs["fillna"], inplace=True)
    if "fill_method" in kwargs:
        _method = kwargs["fill_method"]
        if _method in ('ffill', 'pad'):
            mcg_ds.ffill(inplace=True)
        elif _method in ('bfill', 'backfill'):
            mcg_ds.bfill(inplace=True)

    # Name & Category
    mcg_ds.name = f"MCGD_{length}"
    mcg_ds.category = "overlap"

    return mcg_ds


pta.mcgd = _mcgd


def _zigzag_(high: pd.Series, low: pd.Series, close: pd.Series, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1.) -> pd.Series:
    if not up_thresh:
        up_thresh = multiplier * \
            pta.true_range(high, low, close).mean()/close.mean()
    return zig.PeakValleyPivots(close.values.copy(), up_thresh, down_thresh)


class ZigZag:

    @staticmethod
    def zigzag(high: pd.Series, low: pd.Series, close: pd.Series, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> pd.Series:
        # high, low = high.values, low.values
        pvp = _zigzag_(high, low, close, up_thresh, down_thresh, multiplier)
        pvp = np.where(pvp > 0., high, 0.)+np.where(pvp < 0., low, 0.)
        pvp = pd.Series(pvp).apply(lambda x: x if x else np.nan)
        return pvp

    @staticmethod
    def zigzag_full(high: pd.Series, low: pd.Series, close: pd.Series, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> pd.Series:
        return ZigZag.zigzag(high, low, close, up_thresh, down_thresh, multiplier, **kwargs).interpolate(method="linear")

    @staticmethod
    def zigzag_modes(high: pd.Series, low: pd.Series, close: pd.Series, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> pd.Series:
        return pd.Series(zig.PivotsToModes(_zigzag_(high, low, close, up_thresh, down_thresh, multiplier)))

    @staticmethod
    def zigzag_returns(high: pd.Series, low: pd.Series, close: pd.Series, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., limit: bool = True, **kwargs) -> pd.Series:
        pvp = ZigZag.zigzag(high, low, close, up_thresh,
                            down_thresh, multiplier, **kwargs).values
        _close = close.values
        size = _close.size
        result = np.zeros(size)
        last = _close[-1]
        pre = pvp[0]
        for (i,), p in np.ndenumerate(pvp[:-1]):
            if not np.isnan(p):
                result[i] = (p-pre)/pre
                pre = p
            else:
                result[i] = (_close[i]-pre)/pre
        else:
            result[-1] = (last-pre)/pre
        return pd.Series(result)


def abc(df: pd.DataFrame, lim: float = 5., price_tick: float = 0.01, **kwargs) -> pd.DataFrame:
    # df=pd.DataFrame(dict(open=open,high=high,low=low,close=close))
    col = FILED.OHLC.tolist()
    df = df[col]
    frame = pd.DataFrame(columns=col)
    max_line = lim*price_tick
    for rows in df.itertuples():
        index, open, high, low, close = rows
        tick = abs(open-close)/price_tick
        if index:
            diff = open-preclose
            open -= diff
            high -= diff
            low -= diff
            close -= diff

            if tick > lim:
                if close >= open:
                    up = min(high-close, max_line)
                    close = open+lim*price_tick
                    high = close+up
                else:
                    down = min(close-low, max_line)
                    close = open-lim*price_tick
                    low = close-down
        else:
            if tick > lim:
                if close >= open:
                    up = min(high-close, max_line)
                    close = open+lim*price_tick
                    high = close+up
                else:
                    down = min(close-low, max_line)
                    close = open-lim*price_tick
                    low = close-down
        preclose = close
        frame.loc[index, :] = [open, high, low, close]
    frame = frame.astype(np.float64)
    frame.category = 'candles'
    return frame


def insidebar(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 10, **kwargs):
    length = int(length) if length and length > 0 else 10
    high = high.rolling(length).max().values
    low = low.rolling(length).min().values
    size = close.size
    close = close.values
    thrend = np.full((size,), np.nan)
    line = np.full((size,), np.nan)
    thrend[length-1] = 1.
    for i in range(length, size):
        _close, _low, _high = close[i], low[i-1], high[i-1]
        diff = _high-_low
        if close[i] > _high:
            thrend[i] = 1.
        elif close[i] < _low:
            thrend[i] = -1.
        else:
            thrend[i] = thrend[i-1]
        if thrend[i] > 0.:
            line[i] = (_close-_low)/diff
        else:
            line[i] = (_close-_high)/diff
    return pd.DataFrame(dict(thrend=thrend, line=line))


def line_trhend(data: pd.Series, length: int = 1, **kwargs):
    """
    趋势计算

    参数:
    - data: pd.Series 或 pd.DataFrame
    - length: 趋势周期

    返回:
    - 趋势指标：1（上升）、-1（下降）、0（持平）
    """
    if isinstance(data, pd.Series):
        data = data.to_frame()

    result = pd.DataFrame(index=data.index, columns=data.columns)

    for col in data.columns:
        # 计算滚动差值
        diff = data[col].diff(length)

        # 判断趋势
        result[col] = np.sign(diff)  # 使用np.sign更简洁

        # 处理NaN值
        result[col] = result[col].fillna(0)

        # 如果希望持平情况继承前一个趋势，可以使用ffill
        # result[col] = result[col].replace(0, method='ffill')

    if result.shape[1] == 1:
        return result.iloc[:, 0]
    return result


def smoothrng(x: pd.Series, t: int, m: float = 1.):
    """平滑平均范围"""
    wper = t*2 - 1
    avrng = pta.ema((x - x.shift(1)).apply(abs), t)
    smoothrng = pta.ema(avrng, wper)*m
    return smoothrng


def rngfilt(x: pd.Series, r: pd.Series):
    """过滤范围"""
    _add = (x+r).values
    _diff = (x-r).values
    x = x.values
    m = len(_add[pd.isna(_add)])
    rngfilt = np.array([np.nan]*m)
    rngfilt = np.insert(rngfilt, m, x[m])
    dir = np.array([np.nan]*m)
    dir = np.insert(dir, m, 1.)
    # lennan = max(len(x[isnan(x)]), len(r[isnan(r)]))
    for i in range(m+1, x.size):
        pre_rngfilt = rngfilt[i-1]
        add, diff = _add[i], _diff[i]
        # 向上突破时时候保持收盘价与目标线在一个r值距离，如果收盘价上涨小于r值，则没突破，维持原值
        y = pre_rngfilt if diff < pre_rngfilt else diff
        # 向下突破时时候保持收盘价与目标线在一个r值距离，如果收盘价下跌小于r值，则没突破，维持原值
        z = pre_rngfilt if add > pre_rngfilt else add
        # 收盘价在前一值之上，则有可能向上突破，则取y，之下则有可能向下突破，取z
        pre_dir = dir[i-1]
        next_rngfilt = y if x[i] > pre_rngfilt else z
        next_dir = pre_dir if pre_rngfilt == next_rngfilt else (
            1. if next_rngfilt > pre_rngfilt else -1.)
        rngfilt = np.insert(rngfilt, i, next_rngfilt)
        dir = np.insert(dir, i, next_dir)
    return pd.Series(rngfilt), pd.Series(dir)

# 自定义内置指标


class Candles:

    def Heikin_Ashi_Candles(df: pd.DataFrame, length=0) -> pd.DataFrame:
        length = length if length and length >= 0 else 0
        if length:
            df_ls = [df.shift(i).bfill()
                     if i else df for i in range(length+1)]
            _open = df_ls[-1].open
            _close = df.close
            _low = reduce(np.minimum, [data.low for data in df_ls])
            _high = reduce(np.maximum, [data.high for data in df_ls])
            dframe = pta.ha(_open, _high, _low, _close)
        else:
            dframe = pta.ha(df.open, df.high, df.low, df.close)
        dframe.columns = FILED.OHLC.tolist()
        dframe["datetime"] = df.datetime
        dframe["volume"] = df.volume
        dframe = dframe[FILED.ALL]
        dframe.category = 'candles'
        return dframe

    def Linear_Regression_Candles(df: pd.DataFrame, length=11) -> pd.DataFrame:
        lr_open = pta.linreg(df.open, length)
        lr_high = pta.linreg(df.high, length)
        lr_low = pta.linreg(df.low, length)
        lr_close = pta.linreg(df.close, length)
        dframe = pd.DataFrame(
            dict(datetime=df.datetime, open=lr_open, high=lr_high, low=lr_low, close=lr_close, volume=df.volume))
        dframe.loc[:length, FILED.OHLC] = df.loc[:length, FILED.OHLC]
        dframe.category = 'candles'
        return dframe


class BtFunc(LazyImport):

    def open(open, **kwargs):
        return open

    def high(high, **kwargs):
        return high

    def low(low, **kwargs):
        return low

    def close(close, **kwargs):
        return close

    def smoothrng(close: pd.Series, length: int = 14, mult: float = 1., **kwargs) -> pd.Series:
        """平滑平均范围"""
        wper = length*2 - 1
        avrng = pta.ema((close - close.shift(1)).apply(abs), length)
        smoothrng = pta.ema(avrng, wper)*mult
        return smoothrng

    def rngfilt(close: pd.Series, r: pd.Series = None, **kwargs):
        """过滤范围"""

        _add = (close+r).values
        _diff = (close-r).values
        x = close.values
        m = len(_add[pd.isna(_add)])
        rngfilt = np.array([np.nan]*m)
        rngfilt = np.insert(rngfilt, m, x[m])
        dir = np.array([np.nan]*m)
        dir = np.insert(dir, m, 1.)
        # lennan = max(len(x[isnan(x)]), len(r[isnan(r)]))
        for i in range(m+1, x.size):
            pre_rngfilt = rngfilt[i-1]
            add, diff = _add[i], _diff[i]
            # 向上突破时时候保持收盘价与目标线在一个r值距离，如果收盘价上涨小于r值，则没突破，维持原值
            y = pre_rngfilt if diff < pre_rngfilt else diff
            # 向下突破时时候保持收盘价与目标线在一个r值距离，如果收盘价下跌小于r值，则没突破，维持原值
            z = pre_rngfilt if add > pre_rngfilt else add
            # 收盘价在前一值之上，则有可能向上突破，则取y，之下则有可能向下突破，取z
            pre_dir = dir[i-1]
            next_rngfilt = y if x[i] > pre_rngfilt else z
            next_dir = pre_dir if pre_rngfilt == next_rngfilt else (
                1. if next_rngfilt > pre_rngfilt else -1.)
            rngfilt = np.insert(rngfilt, i, next_rngfilt)
            dir = np.insert(dir, i, next_dir)
        df = pd.concat([pd.Series(rngfilt, name="rngfilt"),
                       pd.Series(dir, name="dir")], axis=1)
        df.category = 'overlap'
        return df

    def alerts(close, length=None, mult=None, **kwargs):
        """alerts指标
        https://cn.tradingview.com/script/ETB76oav/"""
        length = int(length) if length and length > 0 else 14
        mult = mult if mult and mult > 0. else 0.6185
        # close=Series(self.close.values)
        smrng = smoothrng(close, length, mult)
        filt, dir = rngfilt(close, smrng)
        hband = filt + smrng
        lband = filt - smrng
        df = pd.concat([filt, hband, lband, dir], axis=1)
        df.category = 'overlap'
        return df

    # price density 价格密度函数

    def noises_density(high: pd.Series, low: pd.Series, length: int = 10, **kwargs) -> pd.Series:
        length = int(length) if length >= 1 else 10
        direction = (high-low).rolling(length).sum()
        volatility = high.rolling(length).max()-low.rolling(length).min()
        return direction/volatility

    # ER效率系数

    def noises_er(close: pd.Series, length: int, **kwargs) -> pd.Series:
        length = int(length) if length >= 1 else 10
        direction = close.diff(length).abs()  # 方向性
        volatility = close.diff().abs().rolling(length).sum()  # 波动性
        return direction/volatility
    # fractal dimension 分型维度

    def noises_fd(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 10, **kwargs) -> pd.Series:
        n = int(length) if length >= 1 else 10
        direction = close.diff().abs()
        volatility = high.rolling(n).max()-low.rolling(n).min()
        l = (pow(1./n, 2)+direction/volatility).apply(lambda x: np.sqrt(x)
                                                      ).rolling(n).sum().apply(lambda x: np.log(x))
        return (l+np.log(2))/np.log(2.*n)

    def kama(close: pd.Series, length=None, fast=None, slow=None, drift=None, offset=None, **kwargs):
        """Indicator: Kaufman's Adaptive Moving Average (KAMA)"""
        # Validate Arguments
        length = int(length) if length and length > 0 else 10
        fast = int(fast) if fast and fast > 0 else 2
        slow = int(slow) if slow and slow > 0 else 30
        # close = verify_series(close, max(fast, slow, length))
        # drift = get_drift(drift)
        # offset = get_offset(offset)
        offset = v_offset(offset)
        drift = v_drift(drift)

        if close is None:
            return

        # Calculate Result
        def weight(length: int) -> float:
            return 2 / (length + 1)

        fr = weight(fast)
        sr = weight(slow)

        abs_diff = non_zero_range(close, close.shift(length)).abs()
        peer_diff = non_zero_range(close, close.shift(drift)).abs()
        peer_diff_sum = peer_diff.rolling(length).sum()
        er = np.divide(abs_diff, peer_diff_sum, out=np.zeros_like(
            abs_diff, dtype=np.float32), where=peer_diff_sum != 0.)
        x = er * (fr - sr) + sr
        sc = x * x

        m = close.size
        result = [np.nan for _ in range(0, length - 1)] + [0]
        for i in range(length, m):
            result.append(sc.iloc[i] * close.iloc[i] +
                          (1 - sc.iloc[i]) * result[i - 1])

        kama = pd.Series(result, index=close.index)

        # Offset
        if offset != 0:
            kama = kama.shift(offset)

        # Handle fills
        if "fillna" in kwargs:
            kama.fillna(kwargs["fillna"], inplace=True)
        if "fill_method" in kwargs:
            _method = kwargs["fill_method"]
            if _method in ('ffill', 'pad'):
                kama.ffill(inplace=True)
            elif _method in ('bfill', 'backfill'):
                kama.bfill(inplace=True)

        # Name & Category
        kama.name = f"KAMA_{length}_{fast}_{slow}"
        kama.category = 'overlap'

        return kama

    @classmethod
    def mama(cls, close: pd.Series, fastlimit=0.6185, slowlimit=0.06185, **kwargs):
        mama, fama = cls.talib().MAMA(close, fastlimit, slowlimit)
        df = pd.concat([mama, fama], axis=1)
        df.category = "overlap"
        return df

    def pmax(close: pd.Series, length=10, mult=3., mode='hma', dev='stdev', **kwargs):
        # Validate Arguments
        length = length > 0 and int(length) or 10
        mult = mult > 0. and float(mult) or 3.
        mode = mode if mode else 'hma'
        # Calculate Results
        mavg = getattr(pta, mode)(close, length)
        std = getattr(pta, dev)(close, length)
        longStop = mavg-mult*std
        shortStop = mavg+mult*std
        size = close.size
        length = get_lennan(longStop, shortStop)
        maxlong = longStop[length]
        minshort = shortStop[length]
        long = np.full(size, np.nan)
        short = np.full(size, np.nan)
        thrend = np.zeros(size)
        dir = 1
        for i in range(length+1, size):
            dir = (dir == -1 and mavg[i] > minshort) and 1 or (
                (dir == 1 and mavg[i] < maxlong) and -1 or dir)
            if dir == 1:
                maxlong = max(maxlong, longStop[i])
                minshort = shortStop[i]
                long[i] = maxlong
            else:
                minshort = min(minshort, shortStop[i])
                maxlong = longStop[i]
                short[i] = minshort
            thrend[i] = dir
        df = pd.DataFrame(dict(long=long, short=short, thrend=thrend))
        df.category = "overlap"
        return df

    def pmax2(close: pd.Series, length=None, mult=None, mode='hma', dev='stdev', **kwargs):
        # Validate Arguments
        length = length > 0 and length or 10
        mult = mult > 0. and mult or 3.
        mode = mode if mode else 'hma'
        # Calculate Results
        mavg = getattr(pta, mode)(close, length)
        std = getattr(pta, dev)(close, length)
        longStop = mavg-mult*std
        shortStop = mavg+mult*std
        length = get_lennan(longStop, shortStop)
        maxlong = longStop[length]
        minshort = shortStop[length]
        size = close.size
        pmax = np.full(size, np.nan)
        thrend = np.zeros(size)
        dir = 1
        for i in range(length+1, size):
            dir = (dir == -1 and mavg[i] > minshort) and 1 or (
                (dir == 1 and mavg[i] < maxlong) and -1 or dir)
            if dir == 1:
                maxlong = max(maxlong, longStop[i])
                minshort = shortStop[i]
                pmax[i] = maxlong
            else:
                minshort = min(minshort, shortStop[i])
                maxlong = longStop[i]
                pmax[i] = minshort
            thrend[i] = dir
        df = pd.DataFrame(dict(pmax=pmax, thrend=thrend))
        df.category = "overlap"
        return df

    def pmax3(close: pd.Series, length=10, mult=3., mode='hma', dev='stdev', **kwargs):
        # Validate Arguments
        length = length > 0 and length or 10
        mult = mult > 0. and mult or 3.
        mode = mode if mode else 'hma'
        # Calculate Results
        mavg = getattr(pta, mode)(close, length)
        std = getattr(pta, dev)(close, length)
        dn = mavg-mult*std
        up = mavg+mult*std
        size = close.size
        length = get_lennan(dn, up)
        pmax = np.full(size, np.nan)
        pmax[length] = dn[length]
        dir = np.ones(size)
        thrend = np.ones(size)
        for i in range(length+1, size):
            dir[i] = (dir[i-1] == -1 and mavg[i] > pmax[i-1]
                      ) and 1 or ((dir[i-1] == 1 and mavg[i] < pmax[i-1]) and -1 or dir[i-1])
            pmax[i] = dir[i] == 1 and max(
                dn[i], pmax[i-1]) or min(up[i], pmax[i-1])
            thrend[i] = pmax[i] > pmax[i -
                                       1] and 1 or (pmax[i] < pmax[i-1] and -1 or thrend[i-1])
        df = pd.DataFrame(dict(pmax=pmax, thrend=thrend))
        df.category = "overlap"
        return df

    def pv(close: pd.Series, length=10, **kwargs):
        result = close.pct_change(length)
        result.name = f"pv_{length}"
        return result

    def AndeanOsc(open: pd.Series, close: pd.Series, length: int = 14, signal_length: int = 9, **kwargs):
        '''
            Inputs
            ------
            close : Closing price (Array)
            open  : Opening price (Array)

            Settings
            --------
            length        : Indicator period (float)
            signal_length : Signal line period (float)

            Returns
            -------
            Bull   : Bullish component (Array)
            Bear   : Bearish component (Array)
            Signal : Signal line (Array)

            Example
            -------
            bull,bear,signal = AndeanOsc(close,open,14,9)
            '''
        N = len(close)

        alpha = 2/(length+1)
        alpha_signal = 2/(signal_length+1)

        up1, up2, dn1, dn2, bull, bear, signal = np.zeros((7, N))

        up1[0] = dn1[0] = signal[0] = close[0]
        up2[0] = dn2[0] = close[0]**2

        for i in range(1, N):
            up1[i] = max(close[i], open[i], up1[i-1] -
                         alpha*(up1[i-1] - close[i]))
            dn1[i] = min(close[i], open[i], dn1[i-1] +
                         alpha*(close[i] - dn1[i-1]))

            up2[i] = max(close[i]**2, open[i]**2, up2[i-1] -
                         alpha*(up2[i-1] - close[i]**2))
            dn2[i] = min(close[i]**2, open[i]**2, dn2[i-1] +
                         alpha*(close[i]**2 - dn2[i-1]))

            bull[i] = np.sqrt(dn2[i] - dn1[i]**2)
            bear[i] = np.sqrt(up2[i] - up1[i]**2)

            signal[i] = signal[i-1] + alpha_signal * \
                (np.maximum(bull[i], bear[i]) - signal[i-1])
        result = pd.DataFrame(
            dict(up=up1, dn=dn1, bull=bull, bear=bear, signal=signal))
        result.overlap = dict(up=True, dn=True, bull=False,
                              bear=False, signal=False)
        return result

    def Coral_Trend_Candles(close: pd.Series, smooth: int = 9., mult: float = .4, **kwargs) -> pd.Series:
        # string GROUP_3 = 'Config » Coral Trend Candles'
        size = len(close)
        src = close.values
        _sm = smooth if smooth and smooth > 0. else 9.
        cd = mult if mult and mult > 0. else .4
        di = (_sm) / 2.0 + 1.0
        c1 = 2. / (di + 1.0)
        c2 = 1. - c1
        c3 = 3.0 * (cd * cd + cd * cd * cd)
        c4 = -3.0 * (2.0 * cd * cd + cd + cd * cd * cd)
        c5 = 3.0 * cd + 1.0 + cd * cd * cd + 3.0 * cd * cd
        i1 = np.zeros(size)
        i2 = np.zeros(size)
        i3 = np.zeros(size)
        i4 = np.zeros(size)
        i5 = np.zeros(size)
        i6 = np.zeros(size)
        bfr = np.zeros(size)
        for i in range(1, size):
            i1[i] = c1 * src[i] + c2 * i1[i-1]
            i2[i] = c1 * i1[i] + c2 * i2[i-1]
            i3[i] = c1 * i2[i] + c2 * i3[i-1]
            i4[i] = c1 * i3[i] + c2 * i4[i-1]
            i5[i] = c1 * i4[i] + c2 * i5[i-1]
            i6[i] = c1 * i5[i] + c2 * i6[i-1]
            bfr[i] = -cd * cd * cd * i6[i] + c3 * \
                i5[i] + c4 * i4[i] + c5 * i3[i]
        return pd.Series(bfr)

    def rsrs(high: pd.Series, low: pd.Series, volume: pd.Series, length: int = 10, method='r1', weights=True, **kwargs):
        # https://zhuanlan.zhihu.com/p/631688107?utm_id=0
        model = sm.WLS if weights else sm.OLS
        size = high.size
        rs = np.zeros(size)
        r2 = np.zeros(size)
        high = high.values
        low = low.values
        volume = volume.values
        for i in range(length-1, size):
            h = high[i-length+1:i+1]
            l = low[i-length+1:i+1]
            _l = sm.add_constant(l).astype(np.float64)
            _h = sm.add_constant(h).astype(np.float64)
            if weights:
                v = volume[i-length+1:i+1].astype(np.float64)
                model1 = model(h, _l, weights=v).fit()
                model2 = model(l, _h, weights=v).fit()
            else:
                model1 = model(h, _l).fit()
                model2 = model(l, _h).fit()
            rs[i] = model1.params[1]
            r2[i] = model2.params[1]  # rsquared

        if method == 'r1':
            return pd.DataFrame(dict(rshl=rs, rslh=r2))
        elif method == 'r2':
            return pta.zscore(pd.Series(rs), 2*length)
        elif method == 'r3':
            return pta.zscore(pd.Series(rs), 2*length)*r2
        return pd.Series(pta.zscore(pd.Series(rs), 2*length).values*r2*rs)

    def realized(close: pd.Series, length: int = 10, **kwargs):
        ret = close.apply(math.log).diff()
        return ret.rolling(window=length).apply(lambda x: np.sqrt(np.sum(x**2)))

    def moving_average(interval: pd.Series, windowsize: int = 10, mode='same'):
        """mode:'same','full','valid'"""
        window = np.ones(int(windowsize)) / float(windowsize)
        re = np.convolve(interval.values, window, mode)
        return re

    def savitzky_golay(close: pd.Series, window_length: Any, polyorder: Any, deriv: int = 0, delta: float = 1, axis: int = -1, mode: str = 'interp', cval: float = 0, **kwargs):
        return pd.Series(scipy_signal.savgol_filter(close.values, window_length, polyorder, deriv, delta, axis, mode, cval))

    def supertrend(high: pd.Series, low: pd.Series, close: pd.Series, length=14, multiplier=2., weights=2., offset=None, **kwargs):
        """Indicator: Supertrend"""
        # Validate Arguments
        length = int(length) if length and length > 0 else 7
        multiplier = float(
            multiplier) if multiplier and multiplier > 0 else 3.0
        # offset = get_offset(offset)
        offset = v_offset(offset)

        # Calculate Results
        m = close.size
        dir_, trend = [1] * m, [0] * m
        long, short = [npInf] * m, [npInf] * m

        hhv = (weights*high+low)/(weights+1.)
        llv = (weights*low+high)/(weights+1.)
        matr = multiplier * pta.atr(high, low, close, length)
        upperband = llv + matr
        lowerband = hhv - matr

        for i in range(1, m):
            if close.iloc[i] > upperband.iloc[i - 1]:
                dir_[i] = 1
            elif close.iloc[i] < lowerband.iloc[i - 1]:
                dir_[i] = -1
            else:
                dir_[i] = dir_[i - 1]
                if dir_[i] > 0 and lowerband.iloc[i] < lowerband.iloc[i - 1]:
                    lowerband.iloc[i] = lowerband.iloc[i - 1]
                if dir_[i] < 0 and upperband.iloc[i] > upperband.iloc[i - 1]:
                    upperband.iloc[i] = upperband.iloc[i - 1]

            if dir_[i] > 0:
                trend[i] = long[i] = lowerband.iloc[i]
            else:
                trend[i] = short[i] = upperband.iloc[i]

        # Prepare DataFrame to return
        _props = f"_{length}_{multiplier}"
        df = pd.DataFrame({
            f"SUPERT{_props}": trend,
            f"SUPERTd{_props}": dir_,
            f"SUPERTl{_props}": long,
            f"SUPERTs{_props}": short,
        }, index=close.index)

        df.name = f"SUPERT{_props}"
        df.category = "overlap"

        # Apply offset if needed
        if offset != 0:
            df = df.shift(offset)

        # Handle fills
        if "fillna" in kwargs:
            df.fillna(kwargs["fillna"], inplace=True)

        if "fill_method" in kwargs:
            _method = kwargs["fill_method"]
            if _method in ('ffill', 'pad'):
                df.ffill(inplace=True)
            elif _method in ('bfill', 'backfill'):
                df.bfill(inplace=True)

        return df

    def argrelextrema(high: pd.Series, low: pd.Series, order: int = 5, mode='clip', **kwargs):
        """mode:[clip,wrap]"""
        maximum, _ = scipy_signal.argrelextrema(
            high.values, np.greater, order=order, mode=mode)
        minimum, _ = scipy_signal.argrelextrema(
            low.values, np.less, order=order, mode=mode)
        zig = np.full(high.size, np.nan)
        zig[maximum] = high[maximum]
        zig[minimum] = low[minimum]
        zig = pd.Series(zig, name='argrelextrema')
        return zig

    def dkx(open, high, low, close, num=None, length=None, offset=None, **kwargs):
        offset = v_offset(offset)
        length = int(length) if length > 0 else 10
        num = int(num) if num >= 2 else 20
        mid = (3*close+open+high+low)/6.0
        dk = num*mid
        for i in range(1, num):
            dk += (num-i)*mid.shift(i)
        dk = dk/sum(list(range(1, num+1)))
        df = pd.concat([dk, pta.sma(dk, length)], axis=1)
        df.columns = [f"{n}_{str(num)}_{str(length)}" for n in [
            "Dkx", "MaDkx"]]
        return df

    def emsa(close: pd.Series, bar: int = 10, length: int = 48, al=True):
        if al:
            alpha = (1 - np.sin(360 / length)) / np.cos(360 / length)
        else:
            alpha = (np.cos(360/length)+np.sin(360/length)-1) / \
                np.cos(360/length)

        deta = 1-alpha/2
        a = np.exp(-np.sqrt(2)*np.pi/bar)
        b = 2*a*np.cos(np.sqrt(2)*180/bar)
        c2, c3 = b, -a**2
        c1 = 1-c2-c3

        diff = close-2*close.shift(1)+close.shift(2)
        filt = np.zeros(close.size)
        hp = np.zeros(close.size)
        em = np.zeros(close.size)
        for i in range(close.size):
            if i > 1:
                hp[i] = deta*deta*diff.iloc[i]+2 * \
                    (1-alpha)*hp[i-1]-(1-alpha)*(1-alpha)*hp[i-2]
                filt[i] = c1*(hp[i]+hp[i-1])/2.+c2*filt[i-1]+c3*filt[i-2]
                wave = (filt[i]+filt[i-1]+filt[i-2])/3.
                pwr = (filt[i]*filt[i]+filt[i-1] *
                       filt[i-1]+filt[i-2]*filt[i-2])/3.
                em[i] = pwr if pwr == 0 else wave/np.sqrt(pwr)

        return pd.Series(em)

    def highpassfilter(close: pd.Series, length: int = 10):
        a = np.cos(.707*360/48)
        alpha = (a+np.sin(.707*360/48)-1)/a
        deta = 1-alpha/2
        src = close-2*close.shift(1)+close.shift(2)
        hp = np.zeros(close.size)
        for i in range(close.size):
            if i > 1:
                hp[i] = deta*deta*src.iloc[i]+2 * \
                    (1-alpha)*hp[i-1]-(1-alpha)*(1-alpha)*hp[i-2]
        hp = pd.Series(hp)

        a = np.exp(-1.414*np.pi/length)
        b = 2*a*np.cos(1.414*180/length)
        c2, c3 = b, -a**2
        c1 = 1-c2-c3
        src = (hp+hp.shift(1))/2.
        filt = np.zeros(close.size)
        for i in range(close.size):
            if i > 1:
                filt[i] = c1*src.iloc[i]+c2*filt[i-1]+c3*filt[i-2]
        return pd.Series(filt)

    def mcd(open, high, low, close, volume, length: int):
        data = np.column_stack(
            (open.values, high.values, low.values, close.values, volume.values))

        size = data.shape[0]
        delta = np.zeros(size)
        _mcd = np.zeros(size)
        for i in range(size):
            sro, srh, srl, src, srv = data[i]
            if src >= sro:
                de = src-sro+2*(srh-src)+2*(sro-srl)
                if de > 0:
                    delta[i] = srv*(srh-srl)/de
            else:
                de = sro-src+2*(srh-sro)+2*(src-srl)
                if de > 0:
                    delta[i] = -srv*(srh-srl)/de
            if i >= length-1:
                _mcd[i] = delta[i-length+1:i+1].sum()
        return pd.Series(_mcd)

    def rsj(close: pd.Series, length: int):
        rt = close/close.shift()-1.
        RSJ = np.zeros(close.size)
        PositiveRt = np.zeros(close.size)
        NegativeRt = np.zeros(close.size)

        for i in range(close.size):
            if i > 0:
                rt_ = rt.iloc[i]
                if rt_ > 0:
                    PositiveRt[i] = rt_
                else:
                    NegativeRt[i] = rt_
            if i > length-1:
                PositiveRV = np.power(PositiveRt[i-length+1:i+1], 2).sum()
                NegativeRV = np.power(NegativeRt[i-length+1:i+1], 2).sum()
                RSJ[i] = (PositiveRV-NegativeRV) / \
                    np.power(rt.iloc[i-length+1:i+1].values, 2).sum()
        return pd.Series(RSJ)

    def superbandpassfilter(close: pd.Series, length: int = 10):
        def func(x: pd.Series):
            return np.sqrt(x.mean())
        a, b = 5/35, 5/65
        pb = np.zeros(close.size)
        for i in range(close.size):
            if i > 1:
                pb[i] = (a-b)*close.iloc[i]+(b*(1-a)-a*(1-b)) * \
                    close.iloc[i-1]+(2-a-b)*pb[i-1]-(1-a)*(1-b)*pb[i-2]
        pb = pd.Series(pb)
        return pb, pb.rolling(length).apply(lambda x: func(x*x))

    def supersmootherfilter(close: pd.Series, length: int):
        a = np.exp(-1.414*np.pi/length)
        b = 2*a*np.cos(1.414*180/length)
        c2, c3 = b, -a**2
        c1 = 1-c2-c3
        src = (close+close.shift(1))/2.
        filt = np.zeros(close.size)
        for i in range(close.size):
            if i > 1:
                filt[i] = c1*src.iloc[i]+c2*filt[i-1]+c3*filt[i-2]
        return pd.Series(filt)

    def vwap(close: pd.Series, volume: pd.Series, length: int, mult: float = 2.):
        cv = (close*volume).rolling(length).apply(lambda x: x.sum())
        sv = volume.rolling(length).apply(lambda x: x.sum())
        vwap = cv/sv
        std = (close-vwap).abs().rolling(length).apply(lambda x: x.std())
        sdup, sddn = vwap+mult*std, vwap-mult*std
        vwapr = 100*(close-sddn)/(2*mult*std)
        return vwap, sdup, sddn, vwapr

    def lowpass(close: pd.Series, length: int):
        lp = np.zeros(close.size)
        a = 2/(1+length)
        powa = a*a
        for i in range(close.size):
            if i >= length-1:
                lp[i] = (a-powa/4)*close.iloc[i]+powa*close.iloc[i-1]/2 - \
                    (a-3*powa/4)*close.iloc[i-2]+2 * \
                    (1-a)*lp[i-1]-(1-a)*(1-a)*lp[i-2]
        return pd.Series(lp)

    def _rsrs(low: pd.Series, high: pd.Series, close: pd.Series, vol: pd.Series, length: int = None, weights: bool = False):
        length = length if length and length > 0 else 18
        size = low.size
        rs = np.full(size, np.nan)
        if weights:
            for i in range(size):
                if i >= length-1:
                    weights = vol.iloc[i-length+1:i+1].values
                    h = high.iloc[i-length+1:i+1].values
                    l = low.iloc[i-length+1:i+1].values
                    l = sm.add_constant(l)
                    model = sm.WLS(h, l, weights=weights).fit()
                    rs[i] = model.params[1]
        else:
            for i in range(size):
                if i >= length-1:
                    h = high.iloc[i-length+1:i+1].values
                    l = low.iloc[i-length+1:i+1].values
                    l = sm.add_constant(l)
                    model = sm.OLS(h, l).fit()
                    rs[i] = model.params[1]

        return pd.Series(rs)

    def z_rsrs(low: pd.Series, high: pd.Series, close: pd.Series, vol: pd.Series, length: int = None):
        length = length if length and length > 0 else 18
        size = low.size
        rs = np.full(size, np.nan)
        zrs = np.full(size, np.nan)
        for i in range(size):
            if i >= length-1:
                weights = vol.iloc[i-length+1:i+1].values
                h = high.iloc[i-length+1:i+1].values
                l = low.iloc[i-length+1:i+1].values
                l = sm.add_constant(l)
                model = sm.WLS(h, l, weights=weights).fit()
                rs[i] = model.params[1]
            if i >= 2*length-1:
                mean = rs[i-length+1:i+1].mean()
                std = rs[i-length+1:i+1].std()
                zrs[i] = (rs[i]-mean)/std

        return pd.Series(zrs)

    def cor_rsrs(low: pd.Series, high: pd.Series, close: pd.Series, vol: pd.Series, length: int = None, right_avertence: bool = False):
        length = length if length and length > 0 else 18
        size = low.size
        rs = np.full(size, np.nan)
        zrs = np.full(size, np.nan)
        for i in range(size):
            if i >= length-1:
                weights = vol.iloc[i-length+1:i+1].values
                h = high.iloc[i-length+1:i+1].values
                l = low.iloc[i-length+1:i+1].values
                l = sm.add_constant(l)
                model = sm.WLS(h, l, weights=weights).fit()
                rs[i] = model.params[1]
            if i >= 2*length-1:
                mean = rs[i-length+1:i+1].mean()
                std = rs[i-length+1:i+1].std()
                zrs[i] = (rs[i]-mean)/std

        return pd.Series(zrs*rs*rs) if right_avertence else pd.Series(zrs*rs)

    def pass_rsrs(low: pd.Series, high: pd.Series, close: pd.Series, vol: pd.Series, length: int = None):
        length = length if length and length > 0 else 18
        size = low.size
        rs = np.full(size, np.nan)
        zrs = np.full(size, np.nan)
        rsquared = np.full(size, np.nan)
        stdpercent = np.full(size, np.nan)
        for i in range(size):
            if i >= length-1:
                weights = vol.iloc[i-length+1:i+1].values
                h = high.iloc[i-length+1:i+1].values
                l = low.iloc[i-length+1:i+1].values
                l = sm.add_constant(l)
                model = sm.WLS(h, l, weights=weights).fit()
                rs[i] = model.params[1]
                rsquared[i] = model.rsquared
                stdpercent[i] = scipy_stats.percentileofscore(
                    weights, weights[-1])/100.0
            if i >= 2*length-1:
                mean = rs[i-length+1:i+1].mean()
                std = rs[i-length+1:i+1].std()
                zrs[i] = (rs[i]-mean)/std

        return pd.Series(zrs*rsquared*rsquared*stdpercent)

    def ols(high: pd.Series, low: pd.Series, n: int = 35):
        '''差价回归买卖信号'''
        size = high.size
        high, low = high.values, low.values
        rs = np.zeros(size)
        rsquared = np.zeros(size)
        for i in range(size):
            if i < n-1:
                rs[i] = np.nan
                rsquared[i] = np.nan
            else:
                x, y = low[i+1-n:i+1], high[i+1-n:i+1]
                X = sm.add_constant(x, prepend=True)
                model = np.dot((np.dot(np.linalg.inv(np.dot(X.T, X)), X.T)), y)
                rsquared[i], rs[i] = model
        return pd.DataFrame(dict(rs=rs, rsquared=rsquared))

    def _func(close: pd.Series, up: pd.Series, dn: pd.Series, count_length: int):
        p, m = np.where(close.iloc[-count_length:] > dn.iloc[-count_length:], 1,
                        0), np.where(close.iloc[-count_length:] < up.iloc[-count_length:], 1, 0)
        return p.sum()-m.sum()

    def tpr(close: pd.Series, length: int, mult: float = 0.6185, mode='dema', count_length: int = None):
        """meoth (str):dema, ema, fwma, hma, linreg, midpoint, pwma, rma,
            sinwma, sma, swma, t3, tema, trima, vidya, wma, zlma"""
        length = length if length and length > 1 else 30
        count_length = min(
            count_length, length) if count_length and count_length > 1 else length
        ma_, std = pta.ma(mode, close, length=length), pta.stdev(close, length)
        up, dn = ma_+mult*std, ma_-mult*std
        return close.rolling(length).apply(
            lambda x: BtFunc._func(x, up[x.index], dn[x.index], count_length))

    def tpr(close: pd.Series, length: int, angle: float = 5):
        _slope = pta.slope(close, length, as_angle=True, to_degrees=True)

        def func(slope: pd.Series, angle: float):
            p = np.where(slope > angle, 1, 0)
            m = np.where(slope < -angle, 1, 0)
            return 100.*abs(p.sum()-m.sum())
        return _slope.rolling(length).apply(lambda x: func(x, angle)/length)

    def _correl(close: pd.Series):
        sx, sy, sxx, sxy, syy = 0, 0, 0, 0, 0
        size = close.size
        for i in range(size):
            x = close.iloc[i]
            y = -i
            sx += x
            sy += y
            sxx += x*x
            sxy += x*y
            syy += y*y
        t1, t2 = size*sxx-sx*sx, size*syy-sy*sy
        if t1 > 0 and t2 > 0:
            return (size*sxy-sx*sy)/np.sqrt(t1*t2)
        else:
            return .0

    def cti(close: pd.Series, length: int):
        return close.rolling(length).apply(lambda x: BtFunc._correl(x))

    def fft_theta(close: pd.Series):
        sp = np.fft.fft(close.values)
        return pd.Series(np.arctan(sp.imag/sp.real))

    def _tma(close: pd.Series):
        size = close.size
        index = np.arange(1, size+1)
        return (close.values*index).sum()/index.sum()

    def tma(high, low, close: pd.Series, length=151, artlength=151, mult=2.5):
        mid = close.rolling(length).apply(lambda x: BtFunc._tma(x))
        range_ = pta.atr(high, low, close, artlength)
        higher = mid+mult*range_
        lower = mid-mult*range_
        df = pd.concat([mid, higher, lower], axis=1)
        df.columns = ['mid', 'higher', 'lower']
        return df

    def variance_calculator(close: pd.Series, _sma: pd.Series, length):
        temp = close.subtract(_sma)  # Difference a-b
        temp2 = temp.apply(lambda x: x**2)  # Square them.. (a-b)^2
        # Summation (a-b)^2 / (length - 1)
        temp3 = temp2.rolling(length - 1).mean()
        sigma = temp3.apply(lambda x: math.sqrt(x))
        return sigma

    def PCR(close: pd.Series, length: int = 20, k: float = 1.5, l: float = 2):
        _sma = pta.sma(close)
        sigma = BtFunc.variance_calculator(close, _sma, length)
        k_sigma = k * sigma
        l_sigma = l * sigma
        UBB = _sma.add(k_sigma)  # Upper Bollinger Band
        LBB = _sma.subtract(k_sigma)  # Lower Bollinger Band
        USL = _sma.add(l_sigma)  # Upper Stoploss Band
        LSL = _sma.subtract(l_sigma)  # Lower Stoploss Band
        long_signal = pta.cross(close, UBB)
        short_signal = pta.cross(close, LBB, above=False)
        up = pta.cross(close, _sma)
        down = pta.cross(close, _sma, False)
        les = pta.cross(close, USL)
        ses = pta.cross(close, LSL, False)
        exitlong_signal = (les | down).astype(np.float32)
        exitshort_signal = (ses | up).astype(np.float32)
        return pd.DataFrame(dict(
            ubb=UBB,
            lbb=LBB,
            usl=USL,
            lsl=LSL,
            long_signal=long_signal,
            exitlong_signal=exitlong_signal,
            short_signal=short_signal,
            exitshort_signal=exitshort_signal,
        ))

    def TRIN(close: pd.Series, volume: pd.Series, length: int = 20, pct_length: int = 1, k: float = 1.5, l: float = 2):
        ratio = close.divide(close.shift(pct_length))
        vol = volume.divide(volume.shift(pct_length))
        trin = ratio.divide(vol)
        _sma = pta.sma(trin)
        sigma = BtFunc.variance_calculator(trin, _sma, length)
        k_sigma = k * sigma
        l_sigma = l * sigma
        UBB = _sma.add(k_sigma)  # Upper Bollinger Band
        LBB = _sma.subtract(k_sigma)  # Lower Bollinger Band
        USL = _sma.add(l_sigma)  # Upper Stoploss Band
        LSL = _sma.subtract(l_sigma)  # Lower Stoploss Band
        long_signal = pta.cross(close, UBB)
        short_signal = pta.cross(close, LBB, above=False)
        up = pta.cross(close, _sma)
        down = pta.cross(close, _sma, False)
        les = pta.cross(close, USL)
        ses = pta.cross(close, LSL, False)
        exitlong_signal = (les | down).astype(np.float32)
        exitshort_signal = (ses | up).astype(np.float32)
        return pd.DataFrame(dict(
            trin=trin,
            ubb=UBB,
            lbb=LBB,
            usl=USL,
            lsl=LSL,
            long_signal=long_signal,
            exitlong_signal=exitlong_signal,
            short_signal=short_signal,
            exitshort_signal=exitshort_signal,
        ))

    def signal_returns_stats(signal: pd.Series, close: pd.Series = None, n: int = 1, **kwargs) -> pd.Series:
        """
        根据信号计算后续n天的收益率，并统计列维度的总收益、最大总收益和平均总收益
        :param signal: 信号序列（仅含0和1），pd.Series，索引需与close对齐
        :param close: 收盘价序列，pd.Series
        :param n: 统计天数（正整数）
        :return: n=1时返回pd.Series（信号触发后的1天收益）；
                n>1时返回pd.DataFrame（含两部分：1~n天收益数据 + 列统计结果）
        """
        assert signal.size == close.size, "signal信号线与close价格线步长须相同"
        n = int(n)
        n = max(1, n)

        # 2. 核心计算：信号触发后的k天收益率（收益率 = (未来k天收盘价 - 信号日收盘价)/信号日收盘价）
        signal_idx = signal[signal == 1].index  # 信号为1的位置索引
        # ret_dict = {}  # 存储1~n天收益的Series
        k_day_returns_fixed = np.zeros(close.size)
        for k in range(1, n + 1):
            # 步骤1：计算全量k天收益（末尾无法计算的收益用0填充，符合"其他设为0"的需求）
            close_future = close.shift(-k).ffill()  # 未来k天收盘价，末尾NaN→0
            k_day_returns = (close_future - close) / close  # 全量收益率

            # 步骤2：非信号位置设为0（信号位置保留收益，非信号位置强制为0）
            # 先创建全0的Series，再用信号位置的收益覆盖

            index = signal_idx+k
            k_day_returns_fixed[index] = k_day_returns[signal_idx]
        # 1. 生成非零掩码（True=非零，False=零）
        non_zero_mask = k_day_returns_fixed != 0

        # 2. 计算非零元素的累积和（零元素位置延续前一个累积和）
        # 原理：用 where 把零值替换为0，再用 cumsum 累积；若全为零，累积和为0
        cumulative_sum = np.cumsum(
            np.where(non_zero_mask, k_day_returns_fixed, 0))

        # 3. 计算非零元素的累积计数（到当前位置为止的非零个数）
        # 原理：非零位置记为1，零位置记为0，再 cumsum
        cumulative_count = np.cumsum(non_zero_mask.astype(int))

        # 4. 计算非零累积均值（计数为0时返回0，避免除以零）
        # 原理：用 where 处理计数为0的情况，否则用累积和 ÷ 累积计数
        non_zero_cumulative_mean = np.where(
            cumulative_count > 0,  # 条件：非零计数>0
            cumulative_sum / cumulative_count,  # 满足条件：计算均值
            0.0  # 不满足条件（全零）：返回0
        )
        return k_day_returns_fixed, non_zero_cumulative_mean

        # 步骤3：存入字典（列名用英文+下划线，更规范）
        # ret_dict[f"returns_{k}"] = k_day_returns_fixed
        # base_df = pd.DataFrame(ret_dict)
        # base_df.fillna(0., inplace=True)

        # 3. 按n的取值返回不同结果
        # if n == 1:
        #     # n=1：直接返回1天收益的Series
        #     return base_df["returns1"]
        # else:
        #     # 计算列维度的统计指标（忽略NaN，因为末尾信号可能无法计算后续收益）
        #     # col_total = base_df.sum(axis=0)  # 各列（k天收益）的总收益
        #     # max_total_col = col_total.idxmax()  # 最大总收益对应的列名
        #     # avg_total = base_df.mean(axis=1)     # 所有列总收益的平均值

        #     # base_df["avg"] = avg_total
        #     # base_df["max"] = base_df[max_total_col]

        #     return base_df
    @staticmethod
    def _calculate_prob_density(evidence, dist_params):
        """
        计算给定证据在特定分布下的概率密度（似然度）

        参数:
            evidence: 观测到的证据（如收益率）
            dist_params: 分布参数 (均值mu, 标准差sigma)

        返回:
            float: 概率密度值（似然度）
        """
        mu, sigma = dist_params
        # 使用正态分布的概率密度函数计算似然度
        return scipy_stats.norm.pdf(evidence, loc=mu, scale=sigma)

    @staticmethod
    def _fit_distributions(returns, up_threshold=0.001, down_threshold=-0.001):
        """
        拟合三种趋势（上涨、横盘、下跌）下的收益率分布参数

        参数:
            returns: 收益率序列
            up_threshold: 上涨趋势的阈值（收益率超过此值视为上涨）
            down_threshold: 下跌趋势的阈值（收益率低于此值视为下跌）

        返回:
            tuple: 三个分布的参数 (上涨, 横盘, 下跌)，每个分布参数为 (mu, sigma)
        """
        # 分离三种趋势的收益率数据
        up_returns = returns[returns > up_threshold]
        down_returns = returns[returns < down_threshold]
        sideways_returns = returns[(returns >= down_threshold) & (
            returns <= up_threshold)]

        # 拟合正态分布参数（处理空数据情况）
        if len(up_returns) > 0:
            mu_up, sigma_up = scipy_stats.norm.fit(up_returns)
        else:
            mu_up, sigma_up = 0.001, 0.01  # 上涨默认参数

        if len(down_returns) > 0:
            mu_down, sigma_down = scipy_stats.norm.fit(down_returns)
        else:
            mu_down, sigma_down = -0.001, 0.01  # 下跌默认参数

        if len(sideways_returns) > 0:
            mu_side, sigma_side = scipy_stats.norm.fit(sideways_returns)
        else:
            mu_side, sigma_side = 0, 0.005  # 横盘默认参数

        return (mu_up, sigma_up), (mu_side, sigma_side), (mu_down, sigma_down)

    @staticmethod
    def calculate_trend_probabilities(
        close: pd.Series,
        window_length=60,
        up_threshold=0.001,
        down_threshold=-0.001,
        **kwargs
    ):
        """
        计算滚动窗口下的三种趋势（上涨、横盘、下跌）的贝叶斯后验概率

        参数:
            close_prices: 收盘价序列
            window_length: 滚动窗口长度，用于动态更新趋势分布
            up_threshold: 上涨趋势的阈值
            down_threshold: 下跌趋势的阈值

        返回:
            pd.DataFrame: 包含以下列的DataFrame:
                - up_prob: 上涨趋势的后验概率
                - sideways_prob: 横盘趋势的后验概率
                - down_prob: 下跌趋势的后验概率
        """
        # 计算对数收益率
        returns = (close / close.shift()).apply(np.log)
        returns.iloc[0] = 0.
        n_obs = len(returns)

        # 初始化概率数组
        up_prob = np.full(n_obs, np.nan)
        sideways_prob = np.full(n_obs, np.nan)
        down_prob = np.full(n_obs, np.nan)

        # 遍历每个时间点计算滚动后验概率
        for i in range(window_length, n_obs):
            # 获取窗口内的历史数据
            window_returns = returns.iloc[i-window_length:i]

            # 计算先验概率（基于窗口内的频率）
            up_count = sum(window_returns > up_threshold)
            down_count = sum(window_returns < down_threshold)
            sideways_count = len(window_returns) - up_count - down_count

            total = len(window_returns)
            p_up_prior = up_count / total if total > 0 else 1/3
            p_side_prior = sideways_count / total if total > 0 else 1/3
            p_down_prior = down_count / total if total > 0 else 1/3

            # 拟合三种趋势的分布参数
            (mu_up, sigma_up), (mu_side, sigma_side), (mu_down, sigma_down) = \
                BtFunc._fit_distributions(
                    window_returns, up_threshold, down_threshold
            )

            # 当前证据（当前收益率）
            current_return = returns.iloc[i]

            # 计算似然度
            lik_up = BtFunc._calculate_prob_density(
                current_return, (mu_up, sigma_up)
            )
            lik_side = BtFunc._calculate_prob_density(
                current_return, (mu_side, sigma_side)
            )
            lik_down = BtFunc._calculate_prob_density(
                current_return, (mu_down, sigma_down)
            )

            # 计算边际概率（全概率公式）
            p_evidence = (lik_up * p_up_prior) + (lik_side *
                                                  p_side_prior) + (lik_down * p_down_prior)

            # 防止除以零
            if p_evidence < 1e-10:
                # 使用前一个有效值填充，而不是设置为0
                if i > 0 and not np.isnan(up_prob[i-1]):
                    up_prob[i] = up_prob[i-1]
                    sideways_prob[i] = sideways_prob[i-1]
                    down_prob[i] = down_prob[i-1]
                else:
                    # 如果是第一个数据点且无效，使用平均概率初始化
                    up_prob[i] = 1/3
                    sideways_prob[i] = 1/3
                    down_prob[i] = 1/3
                continue

            # 计算后验概率（贝叶斯定理）
            up_prob[i] = (lik_up * p_up_prior) / p_evidence
            sideways_prob[i] = (lik_side * p_side_prior) / p_evidence
            down_prob[i] = (lik_down * p_down_prior) / p_evidence

        # 组合结果并添加索引
        result = pd.DataFrame({
            'up_prob': up_prob,
            'sideways_prob': sideways_prob,
            'down_prob': down_prob
        })

        return result

    def gap_ratio(open: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series, length=20, **kwargs):
        """
        计算通道差异系数GapRatio（修正版）
        原文：https://zhuanlan.zhihu.com/p/1973694928241574212

        Returns:
            DataFrame: 包含gap_ratio, upper_hl, lower_hl, upper_v, lower_v
        """
        # 1. HL通道计算（最高-最低的均值）
        hl_range = high - low
        hl_width = hl_range.rolling(window=length).mean().shift()  # Width_HL
        upper_hl = close.shift(1) + hl_width  # 上轨 = 前一根收盘价 + HL带宽
        lower_hl = close.shift(1) - hl_width  # 下轨 = 前一根收盘价 - HL带宽

        # 2. V通道计算（实体|收-开|的均值）
        v_range = (close - open).abs()
        v_width = v_range.rolling(window=length).mean().shift()  # Width_V
        upper_v = close.shift(1) + v_width  # 上轨 = 前一根收盘价 + V带宽
        lower_v = close.shift(1) - v_width  # 下轨 = 前一根收盘价 - V带宽

        # 3. 计算平均带宽 w = (Width_HL + Width_V) / 2
        w = (hl_width + v_width) / 2

        # 处理除零问题
        w = w.replace(0, np.nan).ffill()

        # 4. GapRatio公式：0.5 * (上轨差异 + 下轨差异) / 平均带宽
        # 注意：不需要shift()，因为通道已经是基于前一根收盘价计算的
        upper_diff = (upper_hl - upper_v).abs()
        lower_diff = (lower_hl - lower_v).abs()

        gap_ratio_value: pd.Series = 0.5 * (upper_diff + lower_diff) / w

        # 基于实际分布重新定义阈值
        low_quantile = gap_ratio_value.quantile(0.3)   # 30%分位数作为低质量阈值
        high_quantile = gap_ratio_value.quantile(0.7)  # 70%分位数作为高质量阈值

        signal = np.where(gap_ratio_value <= high_quantile, 0.5, 0)
        signal = pd.Series(signal).mask(gap_ratio_value <= low_quantile, 1.)

        df = pd.DataFrame({
            'gap_ratio': gap_ratio_value,
            'upper_hl': upper_hl,
            'lower_hl': lower_hl,
            'signal': signal,
            # 'upper_v': upper_v,
            # 'lower_v': lower_v,
            # 'hl_width': hl_width,
            # 'v_width': v_width
        })
        return df

    def vwap_window(high, low, close, volume, window=20, offset=None, **kwargs):
        """Indicator: VWAP with rolling window (No anchor)"""
        # Validate Arguments
        # high = verify_series(high)
        # low = verify_series(low)
        # close = verify_series(close)
        # volume = verify_series(volume)
        # offset = get_offset(offset)
        offset = v_offset(offset)

        typical_price = pta.hlc3(high=high, low=low, close=close)

        # Calculate rolling VWAP
        wp = typical_price * volume
        rolling_wp_sum = wp.rolling(window=window).sum()
        rolling_volume_sum = volume.rolling(window=window).sum()
        vwap_series = rolling_wp_sum / rolling_volume_sum

        # Offset
        if offset != 0:
            vwap_series = vwap_series.shift(offset)

        # Handle fills
        if "fillna" in kwargs:
            vwap_series.fillna(kwargs["fillna"], inplace=True)
        if "fill_method" in kwargs:
            _method = kwargs["fill_method"]
            if _method in ('ffill', 'pad'):
                vwap_series.ffill(inplace=True)
            elif _method in ('bfill', 'backfill'):
                vwap_series.bfill(inplace=True)

        # Name & Category
        vwap_series.name = f"VWAP_W{window}"
        vwap_series.category = "overlap"

        return vwap_series

    def vwap_volume_based(high, low, close, volume, volume_quantile=0.25, lookback=100, offset=None, **kwargs):
        """
        Indicator: VWAP that resets based on volume quantile thresholds

        参数:
        ---------
        high (pd.Series): Series of 'high's
        low (pd.Series): Series of 'low's
        close (pd.Series): Series of 'close's
        volume (pd.Series): Series of 'volume's
        volume_quantile (float): 成交量分位数，用于动态设置重置阈值
            - 0.25: 每25%分位数的成交量重置一次
            - 0.5: 每50%分位数的成交量重置一次
            - 0.75: 每75%分位数的成交量重置一次
            Default: 0.25
        offset (int): How many periods to offset the result. Default: 0

        返回:
        ---------
        pd.Series: Volume quantile based VWAP series
        """
        # Validate Arguments
        # high = verify_series(high)
        # low = verify_series(low)
        # close = verify_series(close)
        # volume = verify_series(volume)
        # offset = get_offset(offset)
        offset = v_offset(offset)

        # Validate volume_quantile parameter
        if not 0 < volume_quantile <= 1:
            raise ValueError("volume_quantile must be between 0 and 1")

        typical_price = pta.hlc3(high=high, low=low, close=close)

        # Calculate dynamic volume step based on quantile
        if len(volume) > 0:
            # 方法1: 使用整个序列的成交量分位数
            # volume_threshold = volume.quantile(volume_quantile)

            # 方法2: 使用滚动窗口的分位数（更动态）
            volume_threshold = volume.rolling(window=min(
                lookback, len(volume))).quantile(volume_quantile).iloc[-1]

            # 确保阈值至少为最小成交量的2倍
            min_volume = volume.min()
            volume_threshold = max(volume_threshold, min_volume * 2)

            # print(f"基于{volume_quantile*100}%分位数计算的成交量阈值: {volume_threshold:.2f}")
        else:
            volume_threshold = 1000  # 默认值

        # Calculate VWAP with dynamic volume-based grouping
        wp = typical_price * volume
        cum_volume = volume.cumsum()

        # Create groups based on dynamic volume threshold
        volume_groups = (cum_volume // volume_threshold).astype(int)

        # Calculate VWAP per volume group
        vwap_series = wp.groupby(volume_groups).cumsum()
        vwap_series /= volume.groupby(volume_groups).cumsum()

        # 处理第一组数据不足的情况（设置为NaN）
        # mask = volume_groups.diff().fillna(0) != 0
        # first_group_mask = (volume_groups == 0) & (cum_volume < volume_threshold)
        # vwap_series = vwap_series.where(~first_group_mask)

        # Offset
        if offset != 0:
            vwap_series = vwap_series.shift(offset)

        # Handle fills
        if "fillna" in kwargs:
            vwap_series.fillna(kwargs["fillna"], inplace=True)
        if "fill_method" in kwargs:
            _method = kwargs["fill_method"]
            if _method in ('ffill', 'pad'):
                vwap_series.ffill(inplace=True)
            elif _method in ('bfill', 'backfill'):
                vwap_series.bfill(inplace=True)

        # Name & Category
        vwap_series.name = f"VWAP_VQ{volume_quantile}"
        vwap_series.category = "overlap"

        return vwap_series


    def gaussian_smoothed(close:pd.Series, window:int=11,**kwargs):
        """高斯核卷积平滑"""
        sigma = window / 6.0
        half = window // 2
        x = np.arange(-half, half + 1)
        kernel = np.exp(-x ** 2 / (2 * sigma ** 2))
        kernel = kernel / kernel.sum()
        # 用 'valid' 模式，两端补边缘值
        padded = np.pad(close, (half, half), mode='edge')
        smoothed = np.convolve(padded, kernel, mode='valid')
        # 对齐长度
        if len(smoothed) > len(close):
            smoothed = smoothed[:len(close)]
        elif len(smoothed) < len(close):
            smoothed = np.pad(smoothed, (0, len(close) - len(smoothed)), mode='edge')
        return smoothed
    
    
    def varma(close: pd.Series, length: int=10, var_length: int=10,**kwargs) -> np.ndarray:
        """VAR (Variable Moving Average) — 基于 CMO 的自适应 EMA"""
        src = close.values
        n = len(close)
        out = np.full(n, np.nan, dtype=np.float64)

        # 涨跌分量
        diff = close.diff().fillna(0)
        # vud1 = pd.Series(np.maximum(diff.values, 0.0), index=close.index)
        # vdd1 = pd.Series(np.maximum(-diff.values, 0.0), index=close.index)
        vud1 = TqFunc.max(diff,0.)
        vdd1 = TqFunc.max(-diff,0.)
        # sum(vud1, var_length) / sum(vdd1, var_length) — Chande Momentum Oscillator
        vUD = pta.sma(vud1, var_length).values * var_length   # rolling sum
        vDD = pta.sma(vdd1, var_length).values * var_length

        alpha_base = 2.0 / (length + 1)
        prev = src[0]
        out[0] = src[0]
        for i in range(1, n):
            denom = vUD[i] + vDD[i]
            if np.isfinite(denom) and denom > 1e-12:
                vCMO = (vUD[i] - vDD[i]) / denom
            else:
                vCMO = 0.0
            a = alpha_base * abs(vCMO)
            prev = a * src[i] + (1.0 - a) * prev
            out[i] = prev
        return out
    
    def rsswma(close: pd.Series, length: int = 10, **kwargs):
        """
        RSS_WMA (Lazy Line) — 三重级联 WMA 平滑器。

        将 length 拆为三段 w1, w2, w3，依次级联 WMA：
        L1 = WMA(data, w1)
        L2 = WMA(L1, w2)
        L3 = WMA(L2, w3)
        """
        if length <= 2:
            return close.copy()
        w = length / 3.0
        w2 = int(round(w + 0.5))  # 模拟 Pine math.round (四舍五入半值向上)
        w1 = int(round((length - w2) / 2.0 + 0.5))
        w3 = int((length - w2) / 2)  # 模拟 Pine int() 截断
        if w1 < 1:
            w1 = 1
        if w2 < 1:
            w2 = 1
        if w3 < 1:
            w3 = 1

        L1 = pta.wma(close, w1)
        L2 = pta.wma(L1, w2)
        return pta.wma(L2, w3)
    
    
    def pivot_high(high: pd.Series, left: int=5, right: int=5,**kwargs) -> pd.Series:
        """
        Pine Script pivothigh 等价：当前 bar 的 high 严格大于左右各 left/right 根 bar 的 high。
        返回值放在确认 bar（pivot bar + right）处。
        """
        high = high.values
        n = len(high)
        result = np.full(n, np.nan, dtype=np.float64)
        if left <= 0 or right <= 0 or n <= left + right:
            return result
        for i in range(left, n - right):
            if np.all(high[i - left : i] < high[i]) and \
            np.all(high[i + 1 : i + right + 1] < high[i]):
                result[i + right] = high[i]
        return pd.Series(result, name="pivot_high")


    def pivot_low(low: pd.Series, left: int=5, right: int=5,**kwargs) -> pd.Series:
        """Pine Script pivotlow 等价"""
        low = low.values
        n = len(low)
        result = np.full(n, np.nan, dtype=np.float64)
        if left <= 0 or right <= 0 or n <= left + right:
            return result
        for i in range(left, n - right):
            if np.all(low[i - left : i] > low[i]) and \
            np.all(low[i + 1 : i + right + 1] > low[i]):
                result[i + right] = low[i]
        return pd.Series(result, name="pivot_low")
    
    def linreg_slope(close: pd.Series, length: int=10, **kwargs) -> pd.Series:
        """Pine Script linreg 等价"""
        src = close.values
        n = len(close)
        bar_index = np.arange(1, n + 1, dtype=np.float64)
        result = np.full(n, np.nan, dtype=np.float64)
        for i in range(length - 1, n):
            x = bar_index[i - length + 1 : i + 1]
            y = src[i - length + 1 : i + 1]
            cov = float(np.mean(y * x) - np.mean(y) * np.mean(x))
            var = float(np.var(x))  # Pine ta.variance = 总体方差
            if var > 1e-12:
                result[i] = abs(cov / var)
        return pd.Series(result, name="linreg_slope")
    
    def vwap_high(high: pd.Series, close: pd.Series, volume: pd.Series,
                   length:int=10,**kwargs) -> pd.Series:
        """锚定 VWAP：anchor=True 时重置累计"""
        price = close.values
        volume = volume.values
        hst= TqFunc.tqfunc().hhv(high, length)
        anchor = np.where(np.isnan(hst), False, high == hst)
        n = len(close)
        vwap = np.zeros(n)
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            if anchor[i]:
                cum_pv = 0.0
                cum_v = 0.0
            cum_pv += price[i] * volume[i]
            cum_v += volume[i]
            vwap[i] = cum_pv / max(cum_v, 1e-12)
        return pd.Series(vwap, name="vwap_high")
    
    def vwap_low(low: pd.Series, close: pd.Series, volume: pd.Series,
                   length:int=10,**kwargs) -> pd.Series:
        """锚定 VWAP：anchor=True 时重置累计"""
        price = close.values
        volume = volume.values
        lst= TqFunc.tqfunc().llv(low, length)
        anchor = np.where(np.isnan(lst), False, low == lst)
        n = len(close)
        vwap = np.zeros(n)
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            if anchor[i]:
                cum_pv = 0.0
                cum_v = 0.0
            cum_pv += price[i] * volume[i]
            cum_v += volume[i]
            vwap[i] = cum_pv / max(cum_v, 1e-12)
        return pd.Series(vwap, name="vwap_low")


# 天勤指标


class TqFunc(LazyImport):

    def ref(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().ref(close, length)

    def std(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().std(close, length)

    def ma(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().ma(close, length)

    def sma(close: pd.Series, n: int = 10, m: int = 2, **kwargs):
        return TqFunc.tqfunc().sma(close, n, m)

    def ema(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().ema(close, length)

    def ema2(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().ema2(close, length)

    def crossup(close: pd.Series, b: pd.Series = None, **kwargs):
        return TqFunc.tqfunc().crossup(close, b)

    def crossdown(close: pd.Series, b: pd.Series = None, **kwargs):
        return TqFunc.tqfunc().crossdown(close, b)

    def count(cond: pd.Series = None, length: int = 10, **kwargs):
        return TqFunc.tqfunc().count(cond, length)

    def trma(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().trma(close, length)

    def harmean(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().harmean(close, length)

    def numpow(close: pd.Series, n: int = 10, m: int = 2, **kwargs):
        return TqFunc.tqfunc().numpow(close, n, m)

    def abs(close: pd.Series, **kwargs):
        return TqFunc.tqfunc().abs(close)

    def min(close: pd.Series, b: pd.Series = None, **kwargs):
        return TqFunc.tqfunc().min(close, b)

    def max(close: pd.Series, b: pd.Series = None, **kwargs):
        return TqFunc.tqfunc().max(close, b)

    def median(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().median(close, length)

    def exist(cond: pd.Series = None, length: int = 10, **kwargs):
        return TqFunc.tqfunc().exist(cond, length)

    def every(cond: pd.Series = None, length: int = 10, **kwargs):
        return TqFunc.tqfunc().every(cond, length)

    def hhv(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().hhv(close, length)

    def llv(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().llv(close, length)

    def avedev(close: pd.Series, length: int = 10, **kwargs):
        return TqFunc.tqfunc().avedev(close, length)

    def barlast(cond: pd.Series = None, **kwargs):
        # 处理kwargs中的cond参数
        if cond is None and 'cond' in kwargs:
            cond = kwargs['cond']

        if cond is None:
            raise ValueError("barlast函数需要cond参数")

        # 转换输入数据为pandas Series
        if isinstance(cond, pd.Series):
            cond_series = cond
        elif hasattr(cond, '__array__') or isinstance(cond, (list, tuple)):
            # NumPy数组、列表、元组
            cond_series = pd.Series(cond)
        else:
            # 其他类型，尝试转换
            try:
                cond_series = pd.Series(cond)
            except Exception as e:
                raise TypeError(f"无法将cond参数转换为pandas Series: {e}")

        # 获取数值数组并转换为布尔类型
        try:
            # 首先转换为数值类型
            cond_values = cond_series.values.astype(float)
            # 然后转换为布尔类型：非0为True，0为False
            cond_bool = cond_values != 0
        except Exception as e:
            raise TypeError(f"无法将cond参数转换为布尔数组: {e}")

        # 处理空数据
        if len(cond_bool) == 0:
            return pd.Series([], dtype=int)

        # 核心计算逻辑
        try:
            # 创建一个数组，条件为False的位置为1，True的位置为0
            v = np.where(cond_bool, 0, 1)

            # 计算累计值
            c = np.cumsum(v)

            # 获取条件为True的位置的累计值
            true_indices = np.where(cond_bool)[0]

            if len(true_indices) == 0:
                # 如果没有True值，所有位置都返回0
                result = np.full(len(cond_bool), 0)
            else:
                # 获取True位置上的累计值
                x = c[true_indices]

                # 计算连续的True之间的差值
                d = np.diff(np.concatenate(([0], x)))

                # 创建结果数组
                result = np.zeros(len(cond_bool), dtype=int)

                # 设置True位置的值
                v_temp = v.copy()
                v_temp[true_indices] = -d
                result = np.cumsum(v_temp)

                # 将第一个True之前的位置设为-1
                if true_indices[0] > 0:
                    result[:true_indices[0]] = -1

            # 返回pandas Series
            return pd.Series(result, index=cond_series.index)

        except Exception as e:
            # 如果计算出错，返回错误信息
            # print(f"barlast计算错误: {e}")
            return pd.Series(np.full(len(cond_bool), 0), index=cond_series.index)

    def cum_counts(cond: pd.Series = None, **kwargs):
        return TqFunc.tqfunc()._cum_counts(cond)

    def time_to_ns_timestamp(input_time):
        return TqFunc.tqfunc().time_to_ns_timestamp(input_time)

    def time_to_s_timestamp(input_time):
        return TqFunc.tqfunc().time_to_s_timestamp(input_time)

    def time_to_str(input_time):
        return TqFunc.tqfunc().time_to_str(input_time)

    def time_to_datetime(input_time):
        return TqFunc.tqfunc().time_to_datetime(input_time)


class TqTa(LazyImport):

    def ATR(df, n=14, **kwargs) -> pd.DataFrame:
        # hlc,tr,atr
        return TqTa.tqta().ATR(df, n)

    def BIAS(df, n=6, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().BIAS(df, n).bias

    def BOLL(df, n=26, p=2, **kwargs) -> pd.DataFrame:
        # c,mid,top,bottom
        return TqTa.tqta().BOLL(df, n, p)

    def DMI(df, n=14, m=6, **kwargs) -> pd.DataFrame:
        # hlc,atr,pdi,mdi,adx,adxr
        return TqTa.tqta().DMI(df, n, m)

    def KDJ(df, n=9, m1=3, m2=3, **kwargs) -> pd.DataFrame:
        # hlc,k,d,j
        return TqTa.tqta().KDJ(df, n, m1, m2)

    def MACD(df, short=12, long=26, m=9, **kwargs) -> pd.DataFrame:
        # c,diff,dea,bar
        return TqTa.tqta().MACD(df, short, long, m)

    def SAR(df, n=4, step=0.02, max=0.2, **kwargs) -> pd.Series:
        # ohlc
        return TqTa.tqta().SAR(df, n, step, max).sar

    def WR(df, n=14, **kwargs) -> pd.Series:
        # hlc
        return TqTa.tqta().WR(df, n).wr

    def RSI(df, n=7, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().RSI(df, n).rsi

    def ASI(df, **kwargs) -> pd.Series:
        # ohlc
        return TqTa.tqta().ASI(df).asi

    def VR(df, n=26, **kwargs) -> pd.Series:
        # cv
        return TqTa.tqta().VR(df, n).vr

    def ARBR(df, n=26, **kwargs) -> pd.DataFrame:
        # ohlc ,ar,br
        return TqTa.tqta().ARBR(df, n)

    def DMA(df, short=10, long=50, m=10, **kwargs) -> pd.DataFrame:
        # c,ddd,ama
        return TqTa.tqta().DMA(df, short, long, m)

    def EXPMA(df, p1=5, p2=10, **kwargs) -> pd.DataFrame:
        # c,ma1,ma2
        return TqTa.tqta().EXPMA(df, p1, p2)

    def CR(df, n=26, m=5, **kwargs) -> pd.DataFrame:
        # hlc,cr,crma
        return TqTa.tqta().CR(df, n, m)

    def CCI(df, n=14, **kwargs):
        # hlc
        return TqTa.tqta().CCI(df, n).cci

    def OBV(df, **kwargs) -> pd.Series:
        # cv
        return TqTa.tqta().OBV(df).obv

    def CDP(df, n=3, **kwargs) -> pd.DataFrame:
        # hlc,ah,al,nh,nl
        return TqTa.tqta().CDP(df, n)

    def HCL(df, n=10, **kwargs) -> pd.DataFrame:
        # hlc,mah,mal,mac
        return TqTa.tqta().HCL(df, n)

    def ENV(df, n=14, k=6, **kwargs) -> pd.DataFrame:
        # c,upper,lower
        return TqTa.tqta().ENV(df, n, k)

    def MIKE(df, n, **kwargs) -> pd.DataFrame:
        # hlc,wr,mr,sr,ws,ms,ss
        return TqTa.tqta().MIKE(df, n)

    def PUBU(df, m=4, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().PUBU(df, m).pb

    def BBI(df, n1=3, n2=6, n3=12, n4=24, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().BBI(df, n1, n2, n3, n4).bbi

    def DKX(df, m=10, **kwargs) -> pd.DataFrame:
        # ohlc,b,d
        return TqTa.tqta().DKX(df, m)

    def BBIBOLL(df, n=10, m=3, **kwargs) -> pd.DataFrame:
        # close,bbiboll,upr,dwn
        return TqTa.tqta().BBIBOLL(df, n, m)

    def ADTM(df, n=23, m=8, **kwargs) -> pd.DataFrame:
        # ohl,adtm,adtmma
        return TqTa.tqta().ADTM(df, n, m)

    def B3612(df, **kwargs) -> pd.DataFrame:
        # c,b36,b612
        return TqTa.tqta().B3612(df)

    def DBCD(df, n=5, m=16, t=76, **kwargs) -> pd.DataFrame:
        # c,dbcd,mm
        return TqTa.tqta().DBCD(df, n, m, t)

    def DDI(df, n=13, n1=30, m=10, m1=5, **kwargs) -> pd.DataFrame:
        # hl,ddi,addi,ad
        return TqTa.tqta().DDI(df, n, n1, m, m1)

    def KD(df, n=9, m1=3, m2=3, **kwargs) -> pd.DataFrame:
        # hlc,k,d
        return TqTa.tqta().KD(df, n, m1, m2)

    def LWR(df, n=9, m=3, **kwargs) -> pd.Series:
        # hlc
        return TqTa.tqta().LWR(df, n, m).lwr

    def MASS(df, n1=9, n2=25, **kwargs) -> pd.Series:
        # hl
        return TqTa.tqta().MASS(df, n1, n2).mass

    def MFI(df, n=14, **kwargs) -> pd.Series:
        # hlcv
        return TqTa.tqta().MFI(df, n).mfi

    def MI(df, n=12, **kwargs) -> pd.DataFrame:
        # c,a,mi
        return TqTa.tqta().MI(df, n)

    def MICD(df, n=3, n1=10, n2=20, **kwargs) -> pd.DataFrame:
        # c,dif,micd
        return TqTa.tqta().MICD(df, n, n1, n2)

    def MTM(df, n=6, n1=6, **kwargs) -> pd.DataFrame:
        # c,mtm,mtmma
        return TqTa.tqta().MTM(df, n, n1)

    def PRICEOSC(df, long=26, short=12, **kwargs) -> pd.Series:
        # close
        return TqTa.tqta().PRICEOSC(df, long, short).priceosc

    def PSY(df, n=12, m=6, **kwargs) -> pd.DataFrame:
        # c,psy,psyma
        return TqTa.tqta().PSY(df, n, m)

    def QHLSR(df, **kwargs) -> pd.DataFrame:
        # hlcv,qhl5,qhl10
        return TqTa.tqta().QHLSR(df)

    def RC(df, n=50, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().RC(df, n).arc

    def RCCD(df, n=10, n1=21, n2=28, **kwargs) -> pd.DataFrame:
        # c,dif,rccd
        return TqTa.tqta().RCCD(df, n, n1, n2)

    def ROC(df, n=24, m=20, **kwargs) -> pd.DataFrame:
        # c,roc,rocma
        return TqTa.tqta().ROC(df, n, m)

    def SLOWKD(df, n=9, m1=3, m2=3, m3=3, **kwargs) -> pd.DataFrame:
        # hlc,k,d
        return TqTa.tqta().SLOWKD(df, n, m1, m2, m3)

    def SRDM(df, n=30, **kwargs) -> pd.DataFrame:
        # hlc,srdm,asrdm
        return TqTa.tqta().SRDM(df, n)

    def SRMI(df, n=9, **kwargs) -> pd.DataFrame:
        # c,a,mi
        return TqTa.tqta().SRMI(df, n)

    def ZDZB(df, n1=50, n2=5, n3=20, **kwargs) -> pd.DataFrame:
        # c,b,d
        return TqTa.tqta().ZDZB(df, n1, n2, n3)

    def DPO(df, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().DPO(df).dpo

    def LON(df, **kwargs) -> pd.DataFrame:
        # hlcv,lon,ma1
        return TqTa.tqta().LON(df)

    def SHORT(df, **kwargs) -> pd.DataFrame:
        # hlcv,short,ma1
        return TqTa.tqta().SHORT(df)

    def MV(df, n=10, m=20, **kwargs) -> pd.DataFrame:
        # v,mv1,mv2
        return TqTa.tqta().MV(df, n, m)

    def WAD(df, n=10, m=30, **kwargs) -> pd.DataFrame:
        # hlc,a,b,e
        return TqTa.tqta().WAD(df, n, m)

    def AD(df, **kwargs) -> pd.Series:
        # hlcv
        return TqTa.tqta().AD(df).ad

    def CCL(df, close_oi=None, **kwargs) -> pd.Series:
        # c
        df["close_oi"] = close_oi
        return TqTa.tqta().CCL(df).ccl

    def CJL(df, close_oi=None, **kwargs) -> pd.DataFrame:
        # v,vol,opid
        df["close_oi"] = close_oi
        return TqTa.tqta().CJL(df)

    # def OPI(df, close_oi=None, **kwargs) -> pd.Series:
    #     return pd.Series(list(close_oi), name="opi")

    def PVT(df, **kwargs) -> pd.Series:
        # cv
        return TqTa.tqta().PVT(df).pvt

    def VOSC(df, short=12, long=26, **kwargs) -> pd.Series:
        # v
        return TqTa.tqta().VOSC(df, short, long).vosc

    def VROC(df, n=12, **kwargs) -> pd.Series:
        # v
        return TqTa.tqta().VROC(df, n).vroc

    def VRSI(df, n=6, **kwargs) -> pd.Series:
        # v
        return TqTa.tqta().VRSI(df, n).vrsi

    def WVAD(df, **kwargs) -> pd.Series:
        # ohlcv
        return TqTa.tqta().WVAD(df).wvad

    def MA(df, n=30, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().MA(df, n).ma

    def SMA(df, n=5, m=2, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().SMA(df, n, m).sma

    def EMA(df, n=10, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().EMA(df, n).ema

    def EMA2(df, n=10, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().EMA2(df, n).ema2

    def TRMA(df, n=10, **kwargs) -> pd.Series:
        # c
        return TqTa.tqta().TRMA(df, n).trma
