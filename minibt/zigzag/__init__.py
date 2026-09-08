from .core import *
from numpy import ndarray

__title__ = "zigzag"
__description__ = "Package for finding peaks and valleys of time series."
__uri__ = "https://github.com/jbn/ZigZag"
__doc__ = __description__ + " <" + __uri__ + ">"
__license__ = " BSD-3-Clause"
__copyright__ = "Copyright (c) 2017 John Bjorn Nelson"
__version__ = "0.2.2"
__author__ = "John Bjorn Nelson"
__email__ = "jbn@abreka.com"

PEAK = 1
"""整数常量，表示时间序列中的峰（局部最大值）"""

VALLEY = -1
"""整数常量，表示时间序列中的谷（局部最小值）"""


def PeakValleyPivots(array, up_thresh: float = 0.01, down_thresh: float = 0.) -> ndarray:
    """
    识别时间序列中的峰（PEAK）和谷（VALLEY）转折点，过滤微小波动。

    函数通过设定上涨和下跌阈值，仅当价格波动超过阈值时才标记转折点，
    返回与输入序列等长的数组，标记每个位置的类型（峰/谷/非转折点）。

    参数:
        array: 时间序列数据（支持numpy数组或pandas序列）
        up_thresh: 上涨阈值（正浮点数，默认0.01）。
            实际值会被取绝对值，且限制在(0,1)范围内，超出则自动设为0.01
        down_thresh: 下跌阈值（负浮点数，默认-0.01）。
            若未指定或超出(-1,0)范围，自动设为-up_thresh（与上涨阈值对称）

    返回:
        numpy.ndarray: 与输入长度相同的数组，其中：
            - PEAK (1) 表示峰（局部最大值）
            - VALLEY (-1) 表示谷（局部最小值）
            - 0 表示非转折点

    示例:
        >>> data = np.array([1.0, 1.2, 1.05, 0.9, 1.1])
        >>> pivots = PeakValleyPivots(data, up_thresh=0.1, down_thresh=-0.1)
        >>> print(pivots)
        [-1  1 -1  0  1]
    """
    up_thresh = abs(up_thresh)
    up_thresh = up_thresh if 0. < up_thresh < 1. else 0.01
    down_thresh = down_thresh if -1. < down_thresh < 0. else -up_thresh
    return peak_valley_pivots(array, up_thresh, down_thresh)


def ComputeSegmentReturns(array, pivots) -> ndarray:
    """
    计算相邻转折点之间的收益率（回报率）。

    根据峰谷转折点将时间序列分割为连续片段，计算每个片段的收益率，
    反映从一个转折点到下一个转折点的价格变化幅度。

    参数:
        array: 原始时间序列数据（与pivots长度相同）
        pivots: 由PeakValleyPivots生成的转折点数组（含PEAK/VALLEY/0）

    返回:
        numpy.ndarray: 收益率数组，长度为转折点数量-1，
            每个元素为相邻转折点的收益率，计算公式：
            (后点价格 - 前点价格) / 前点价格

    示例:
        >>> data = np.array([1.0, 1.2, 1.05, 0.9, 1.1])
        >>> pivots = PeakValleyPivots(data, 0.1, -0.1)  # [-1,1,-1,0,1]
        >>> returns = ComputeSegmentReturns(data, pivots)
        >>> print(returns)  # 从谷到峰、峰到谷、谷到峰的收益率
        [0.2        -0.125      0.04761905]
    """
    return compute_segment_returns(array, pivots)


def MaxDrawdown(array) -> float:
    """
    计算时间序列的最大回撤率，衡量序列从历史高点到后续低点的最大跌幅。

    最大回撤是风险评估的重要指标，反映最严重的亏损程度，取值范围为[0,1]。

    参数:
        array: 时间序列数据（如价格序列）

    返回:
        float: 最大回撤率，计算公式：
            (历史峰值 - 后续谷值) / 历史峰值

    示例:
        >>> data = np.array([100, 120, 90, 110, 80])
        >>> print(MaxDrawdown(data))  # 从120跌至80，最大回撤为(120-80)/120≈0.333
        0.3333333333333333
    """
    return max_drawdown(array)


def PivotsToModes(pivots: ndarray) -> ndarray:
    """
    将转折点数组转换为趋势模式数组，标记每个位置的趋势方向（上升/下降）。

    根据峰和谷的位置，推断序列在每个时刻处于"上升模式"（谷之后）还是"下降模式"（峰之后）。

    参数:
        pivots: 由PeakValleyPivots生成的转折点数组（含PEAK/VALLEY/0）

    返回:
        numpy.ndarray: 与输入长度相同的趋势模式数组，其中：
            - 1 表示上升模式（当前位置处于谷之后、峰之前）
            - -1 表示下降模式（当前位置处于峰之后、谷之前）

    示例:
        >>> pivots = np.array([-1, 0, 1, 0, 0, -1, 0])  # VALLEY -> PEAK -> VALLEY
        >>> modes = PivotsToModes(pivots)
        >>> print(modes)  # 谷后上升，峰后下降，谷后上升
        [1 1 -1 -1 -1 1 1]
    """
    return pivots_to_modes(pivots)


def IdentifyInitialPivot(array, up_thresh: float = 0.01, down_thresh: float = 0.) -> int:
    """
    判断时间序列的初始转折点类型（首个峰或谷），用于确定序列的起始趋势。

    根据序列开头的波动特征，结合阈值判断初始趋势是上升（起点为谷）还是下降（起点为峰）。

    参数:
        array: 时间序列数据
        up_thresh: 上涨阈值（正浮点数，默认0.01），处理逻辑同PeakValleyPivots
        down_thresh: 下跌阈值（负浮点数，默认-0.01），处理逻辑同PeakValleyPivots

    返回:
        int: 初始转折点类型，PEAK (1) 或 VALLEY (-1)

    示例:
        >>> data = np.array([1.0, 1.1, 1.2, 1.3])  # 持续上涨
        >>> print(IdentifyInitialPivot(data))  # 起点为谷
        -1
        >>> data = np.array([1.3, 1.2, 1.1, 1.0])  # 持续下跌
        >>> print(IdentifyInitialPivot(data))  # 起点为峰
        1
    """
    up_thresh = abs(up_thresh)
    up_thresh = up_thresh if 0. < up_thresh < 1. else 0.01
    down_thresh = down_thresh if -1. < down_thresh < 0. else -up_thresh
    return identify_initial_pivot(array, up_thresh, down_thresh)
