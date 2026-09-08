from __future__ import annotations
import joblib
from typing import Any, Callable, TYPE_CHECKING
from collections.abc import Iterable,Sequence, Mapping, Generator,Iterator
from functools import wraps, cache, reduce, partial
from collections import Counter
from cachetools import cachedmethod, Cache
from operator import attrgetter
import os
import pickle
from pandas.core.generic import NDFrame
import psutil
from retrying import retry
from addict import Addict, Dict
from pandas._libs.internals import BlockPlacement
from pandas.core import common
import quantstats.stats as qs_stats
import quantstats.plots as qs_plot
from typing_extensions import Literal
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, ALL_COMPLETED
from copy import deepcopy
from iteration_utilities import flatten
from queue import Queue, LifoQueue
from inspect import signature, getsourcelines  # , stack, getsource
from pandas.core.window import Rolling, ExponentialMovingWindow, Expanding,ExpandingGroupby,ExponentialMovingWindowGroupby,RollingGroupby
from pandas.io.formats import format as fmt
from pandas._libs import lib
from .other import *
from .constant import *
from .order import *
from dataclasses import dataclass, field, fields
from operator import neg, pos
import warnings
from collections import OrderedDict
from math import isfinite
import time as _time
import pandas as pd
import itertools
from io import StringIO

try:
    with FilteredOutputRedirector():
        from tqsdk.objs import Position, Quote
        from tqsdk.objs import Account as TqAccount
        from tqsdk import TqApi, TargetPosTask
except ImportError:  # [pilot-prune] tqsdk 可选:仅实盘天勤链路使用
    Position = Quote = TqAccount = TqApi = TargetPosTask = None

pd.options.mode.chained_assignment = None
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
if TYPE_CHECKING:
    from typing_ import *
    from .indicators import (KLine, IndFrame, IndSeries, Line,
                             IndicatorsBase)
    from .strategy.strategy import Strategy
    from .indicators.tradingview import TradingView
    from .core import CoreFunc
    from .logger import Logger
    from tqsdk.objs import TradingTime

    class Params:
        """参数字典"""

        def __getattr__(self, name: str) -> float: ...

    class corefunc:
        """核心函数"""

        def __getattr__(self, name: str) -> CoreFunc: ...

# 基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPECIAL_FUNC = {"assign", }


class Gui(str,Enum):
    Bokeh="bokeh"
    LightChart="light_chart"


class MinibtOptions:
    """## 全局配置类

    Attributes:
        set_conversion_mode: 设置转换模式
            - 'strict': 仅同长度转换（默认）
            - 'free': 全部转换
        set_data_patching: 是否启用数据二次替换/修补模式（默认：False）
            - True: 开启高频数据替换（适合next中二次处理，性能低）
            - False: 关闭自动替换（适合纯回测，性能高）

    Examples:
        >>> # 全局设置
        >>> minibt.options.set_conversion_mode = 'strict'
        >>> minibt.options.set_data_patching = False

        >>> # 使用上下文管理器临时设置
        >>> with minibt.options.context(set_conversion_mode='free', set_data_patching=True):
        ...     # 在此代码块内使用自由转换+数据修补模式
        ...     result = some_computation()
    """

    def __init__(self):
        self._conversion_mode = 'strict'
        # 新增：数据替换模式开关，默认开启以保持向后兼容
        self._data_patching = False

    @property
    def set_conversion_mode(self) -> str:
        """## 内置指标转换模式
        Attributes:
            set_conversion_mode: 设置转换模式
                - 'strict': 仅同长度转换（默认）
                - 'free': 全部转换

        Examples:
            >>> # 全局设置
            >>> minibt.options.set_conversion_mode = 'strict'
            >>> minibt.options.set_conversion_mode = 'free'

            >>> # 使用上下文管理器临时设置
            >>> with minibt.options.context(set_conversion_mode='free'):
            ...     # 在此代码块内使用自由转换模式
            ...     indicator = df.rolling(10).apply(custom_function)"""
        return self._conversion_mode

    @set_conversion_mode.setter
    def set_conversion_mode(self, value: str):
        if value in ['strict', 'free']:
            self._conversion_mode = value

    # -------------------- 调整后的核心代码 --------------------
    @property
    def set_data_patching(self) -> bool:
        """## 数据二次替换模式开关
        控制是否执行高频的 `update_values` / `update_replace` 操作。
        关闭此模式可显著提升回测性能，适合不需要在next中修改数据的场景。

        Attributes:
            set_data_patching: True/False（默认：False）

        Examples:
            >>> # 全局关闭数据替换，最大化回测速度
            >>> minibt.options.set_data_patching = False

            >>> # 临时开启以处理数据
            >>> with minibt.options.context(set_data_patching=True):
            ...     self.data.ma5 = self.data.close.sma(5)
        """
        return self._data_patching

    @set_data_patching.setter
    def set_data_patching(self, value: bool):
        self._data_patching = bool(value)

    def check_conversion_mode(self, data: pd.DataFrame | pd.Series, indicator: IndicatorsBase) -> bool:
        """## 判断是否应该将pandas对象转换为minibt指标

        Args:
            data: 计算结果对象
            indicator: 内置指标

        Returns:
            bool: 是否应该转换
        """
        if not ispandasojb(data):
            return False

        if self._conversion_mode == 'free':
            return True

        if not indicator.isindicator:
            return True

        return len(data) == indicator.V

    @contextmanager
    def context(self, **kwargs):
        """## 临时修改配置的上下文管理器（统一入口）
        兼容 set_conversion_mode 和 set_data_patching。

        Args:
            **kwargs: 要临时修改的配置项（如 set_conversion_mode='free'、set_data_patching=True）

        Examples:
            >>> with minibt.options.context(set_conversion_mode='free', set_data_patching=True):
            ...     # 临时同时开启自由转换和数据修补
            ...     result = some_computation()
        """
        original_settings: dict[str, Any] = {}
        # 映射外部set开头的属性名到内部私有变量名
        attr_mapping = {
            'set_conversion_mode': '_conversion_mode',
            'set_data_patching': '_data_patching'
        }

        for key, value in kwargs.items():
            if key in attr_mapping:
                internal_key = attr_mapping[key]
                original_settings[internal_key] = getattr(self, internal_key)
                setattr(self, internal_key, value)
            else:
                raise AttributeError(f"MinibtOptions 不支持配置项: '{key}'")

        try:
            yield
        finally:
            # 恢复原始配置
            for internal_key, value in original_settings.items():
                setattr(self, internal_key, value)

    # 保留原有别名以确保兼容性
    context_conversion_mode = context

    def reset(self):
        """## 重置所有配置为默认值"""
        self._conversion_mode = 'strict'
        self._data_patching = False

    def get_settings(self) -> dict:
        """## 获取当前所有配置

        Returns:
            dict: 当前配置字典
        """
        return {
            'set_conversion_mode': self._conversion_mode,
            'set_data_patching': self._data_patching
        }

    def __repr__(self) -> str:
        return (f"MinibtOptions(set_conversion_mode='{self._conversion_mode}', "
                f"set_data_patching={self._data_patching})")


# 全局选项实例
options = MinibtOptions()


def _cagr(returns, rf=0.0, compounded=True, periods=252):
    """适合tick数据
    计算超额收益的年化增长率(CAGR%)

    如果rf非零，必须指定periods，此时rf被假定为年度化数据

    参数:
        returns: 收益数据，带时间索引
        rf: 无风险收益率
        compounded: 是否使用复利计算
        periods: 每年的交易周期数（默认252，适用于股票交易日）
    """

    total = qs_stats._utils._prepare_returns(returns, rf)

    # 计算总收益
    if compounded:
        total = qs_stats.comp(total)
    else:
        total = np.sum(total)

    # 计算时间跨度（年）
    try:
        # 获取时间差（天）
        time_diff_days = (returns.index[-1] - returns.index[0]).days

        # 处理时间跨度为0或负的情况
        if time_diff_days <= 0:
            # 对于单周期数据，直接返回对应周期的收益率（非年化）
            return total if not isinstance(returns, pd.DataFrame) else pd.Series(total, index=returns.columns)

        # 计算年数（避免除以零）
        years = time_diff_days / periods

        # 计算CAGR
        res = (abs(total + 1.0) ** (1.0 / years)) - 1

        # 保持返回格式与输入一致
        if isinstance(returns, pd.DataFrame):
            res = pd.Series(res)
            res.index = returns.columns

        return res

    except ZeroDivisionError:
        # 理论上已通过time_diff_days检查避免，但保留作为最后防护
        return total if not isinstance(returns, pd.DataFrame) else pd.Series(total, index=returns.columns)
    except Exception as e:
        # 处理其他可能的索引错误
        raise ValueError(f"计算CAGR时出错: {str(e)}") from e


# 原函数有BUG
qs_stats.cagr = _cagr


def _omega(returns, rf=0.0, required_return=0.0, periods=252):
    """
    修复后的Omega比率计算函数：
    1. 确保numer和denom为标量（单个数值）
    2. 处理空序列和异常情况
    """
    # 输入校验：确保returns有效
    if len(returns) < 2:
        return np.nan
    if required_return <= -1:
        return np.nan

    # 预处理收益率（去空值、计算超额收益）
    returns = qs_stats._utils._prepare_returns(returns, rf, periods)
    if returns.empty:  # 新增：处理空序列
        return np.nan

    # 计算目标收益率阈值
    if periods == 1:
        return_threshold = required_return
    else:
        return_threshold = (1 + required_return) ** (1.0 / periods) - 1

    # 计算超额收益（减去阈值）
    returns_less_thresh = returns - return_threshold

    # 核心修复：确保sum()返回标量，并处理可能的空序列
    # 1. 计算正超额收益总和（numerator）
    positive = returns_less_thresh[returns_less_thresh > 0.0]
    numer = positive.sum().item() if not positive.empty else 0.0  # .item()强制转为标量

    # 2. 计算负超额收益总和的绝对值（denominator）
    negative = returns_less_thresh[returns_less_thresh < 0.0]
    denom = -negative.sum().item() if not negative.empty else 0.0  # .item()强制转为标量

    # 避免除以零，同时确保denom是标量判断
    if isinstance(denom, (int, float)) and denom > 0.0:
        return numer / denom
    else:
        return np.nan


# 原函数有BUG
qs_stats.omega = _omega


def _add_symbol_info(self: pd.DataFrame, **kwargs):
    """pd.DataFrame快速增加数据列"""
    if kwargs:
        ls = []
        cols = self.columns
        for k, v in kwargs.items():
            if k not in cols:
                try:
                    self[k] = v
                except:
                    ls.append(k)
        else:
            if ls:
                print(f"以下列：{ls}添加失败")
    return self


pd.DataFrame.add_info = _add_symbol_info


def __sharpe(returns, rf=0.0, periods=252, annualize=True, smart=False):
    """
    Calculates the sharpe ratio of access returns

    If rf is non-zero, you must specify periods.
    In this case, rf is assumed to be expressed in yearly (annualized) terms

    Args:
        * returns (Series, DataFrame): Input return series
        * rf (float): Risk-free rate expressed as a yearly (annualized) return
        * periods (int): Freq. of returns (252/365 for daily, 12 for monthly)
        * annualize: return annualize sharpe?
        * smart: return smart sharpe ratio
    """
    if rf != 0 and periods is None:
        raise Exception("Must provide periods if rf != 0")

    returns = qs_stats._utils._prepare_returns(returns, rf, periods)
    divisor = returns.std(ddof=1)
    if smart:
        # penalize sharpe with auto correlation
        divisor = divisor * qs_stats.autocorr_penalty(returns)
    # 原函数有BUG
    if isinstance(divisor, pd.Series):
        divisor = divisor.iloc[0]
    if divisor:
        res = returns.mean() / divisor
    else:
        return .0

    if annualize:
        return res * qs_stats._np.sqrt(1 if periods is None else periods)

    return res


# 原函数有BUG
qs_stats.sharpe = __sharpe

# 开盘时间
# OPEN_TIME: list[time] = [time(9, 0), time(13, 0), time(21, 0)]
# CPU核心个数
MAX_WORKERS = psutil.cpu_count(logical=True)-1

#
SIGNAL_Str = np.array(['long_signal', 'exitlong_signal',
                      'short_signal', 'exitshort_signal'])


class TPE:
    """Initializes a new ThreadPoolExecutor instance.
    ---
    attr:
    --
    >>> self.executor = ThreadPoolExecutor()
    method:
    ---
    >>> multi_run  策略初始化时多指标计算
        replay_run 实时播放数据多线程计算
    """
    executor: ThreadPoolExecutor | None

    def __init__(self) -> None:
        self.executor = None

    def reinit(self, **kwargs):
        max_workers = kwargs.pop("max_workers", None)
        thread_name_prefix = kwargs.pop("thread_name_prefix", "")
        initializer = kwargs.pop("initializer", None)
        initargs = kwargs.pop("initargs", ())
        if max_workers is None:
            # ThreadPoolExecutor is often used to:
            # * CPU bound task which releases GIL
            # * I/O bound task (which releases GIL, of course)
            #
            # We use cpu_count + 4 for both types of tasks.
            # But we limit it to 32 to avoid consuming surprisingly large resource
            # on many core machine.
            max_workers = min(32, (os.cpu_count() or 1) + 4)
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")

        if initializer is not None and not callable(initializer):
            raise TypeError("initializer must be a callable")

        self.executor._max_workers = max_workers
        self.executor._thread_name_prefix = (thread_name_prefix or
                                             ("ThreadPoolExecutor-%d" % self.executor._counter()))
        self.executor._initializer = initializer
        self.executor._initargs = initargs

    def multi_run(self, *args, **kwargs):
        """策略初始化时多指标计算"""
        df = kwargs.pop("data")
        if self.executor is None:
            self.executor = ThreadPoolExecutor()
        if kwargs:
            self.reinit(**kwargs)
        assert len(args) >= 2, "传参长度大于2"
        all_task = []
        for i, arg in enumerate(args):
            if isinstance(arg, Multiply):
                func, params, data = arg.values
                if data is not None:
                    data = data,
                else:
                    data = df,
                params = {**params, "_multi_index": i}
                assert isinstance(func, Callable), "请传入指标函数"
                all_task.append(self.executor.submit(func, *data, **params))
            elif isinstance(arg, Iterable) and len(arg) >= 2:
                func, params, *data = arg
                data = (data[0],) if data else None
                params = {**params, "_multi_index": i}
                assert isinstance(func, Callable), "请传入指标函数"
                all_task.append(self.executor.submit(func, **params))
            else:
                raise KeyError("参数有误")
        wait(all_task, return_when=FIRST_COMPLETED)
        results: list = []
        for f in as_completed(all_task):
            result = f.result()
            results.append(result)
        results = sorted(results, key=lambda x: x[0])
        return [value for _, value in results]

    def run(self, func, klines) -> np.ndarray:
        if self.executor is None:
            self.executor = ThreadPoolExecutor()
        values = []
        results = [self.executor.submit(func, i, k)
                   for i, k in enumerate(klines)]
        wait(results, return_when=ALL_COMPLETED)
        for f in as_completed(results):
            result = f.result()
            values.append(result)
        values = sorted(values, key=lambda x: x[0])
        return np.array(list(map(lambda x: x[1], values)))

    def replay_run(self, func, klines, **kwargs) -> np.ndarray:
        """实时播放数据多线程计算"""
        from joblib import Parallel, delayed, parallel_backend
        with parallel_backend('loky'):  # 正确配置后端
            results = Parallel(
                n_jobs=-1,
                prefer='processes',
                max_nbytes='16M'
            )(delayed(func)(index=i, data_=k) for i, k in enumerate(klines))

        results = sorted(results, key=lambda x: x[0])
        return np.array(list(map(lambda x: x[1], results)))


TPE = TPE()


def cyclestring(cycle) -> str:
    """周期转字符串"""
    assert cycle > 0
    return cycle < 60 and f"{cycle}S" or (
        cycle < 3600 and f"{int(cycle/60)}M" or (
            cycle < 86400 and f"{int(cycle/3600)}H" or f"{int(cycle/86400)}D"
        )
    )


class CategoryString(str):
    """## 指标类别字符串"""
    @property
    def iscandles(self) -> bool:
        """## 是否为蜡烛图"""
        return "candles" in self.lower()

    @property
    def isoverlap(self) -> bool:
        """## 是否为主图叠加类别"""
        return "overlap" in self.lower()


class CandlesCategory(metaclass=Meta):
    """## 蜡烛图类型

    >>> Candles: CategoryString = CategoryString("candles")
        Heikin_Ashi_Candles: CategoryString = CategoryString(
            "heikin_ashi_candles")
        Linear_Regression_Candles: CategoryString = CategoryString(
            "linear_regression_candles")"""
    Candles: CategoryString = CategoryString("candles")
    Heikin_Ashi_Candles: CategoryString = CategoryString("heikin_ashi_candles")
    Linear_Regression_Candles: CategoryString = CategoryString(
        "linear_regression_candles")


class Category(metaclass=Meta):
    """## 指标类别

    >>> Any: CategoryString = CategoryString("any")
        Candles: CategoryString = CategoryString("candles")
        Heikin_Ashi_Candles: CategoryString = CategoryString(
            "heikin_ashi_candles")
        Linear_Regression_Candles: CategoryString = CategoryString(
            "linear_regression_candles")
        Momentum: CategoryString = CategoryString("momentum")
        Overlap: CategoryString = CategoryString("overlap")
        Performance: CategoryString = CategoryString("performance")
        Statistics: CategoryString = CategoryString("statistics")
        Trend: CategoryString = CategoryString("trend")
        Volatility: CategoryString = CategoryString("volatility")
        Volume: CategoryString = CategoryString("volume")"""
    Any: CategoryString = CategoryString("any")
    Candles: CategoryString = CategoryString("candles")
    Heikin_Ashi_Candles: CategoryString = CategoryString("heikin_ashi_candles")
    Linear_Regression_Candles: CategoryString = CategoryString(
        "linear_regression_candles")
    Momentum: CategoryString = CategoryString("momentum")
    Overlap: CategoryString = CategoryString("overlap")
    Performance: CategoryString = CategoryString("performance")
    Statistics: CategoryString = CategoryString("statistics")
    Trend: CategoryString = CategoryString("trend")
    Volatility: CategoryString = CategoryString("volatility")
    Volume: CategoryString = CategoryString("volume")


@dataclass
class Config:
    """## 策略设置

    >>> value: float = 1000_000.
        margin_rate: float = 0.05
        tick_commission: float = 0.
        percent_commission: float = 0.
        fixed_commission: float = 1.
        min_start_length: int = 0
        islog: bool = False
        isplot: bool = True
        clear_gap: bool = False
        data_segments: Union[float, int] = 1.
        slip_point = 0.
        print_account: bool = True
        key: str = 'datetime'
        start_time = None
        end_time = None
        time_segments: Union[time] = None
        profit_plot: bool = True
        click_policy: Literal["hide", "mute"] = "hide"
        take_time: bool = True
        on_close: bool = True"""
    value: float = 1000_000.
    margin_rate: float = 0.05
    tick_commission: float = 0.
    percent_commission: float = 0.
    fixed_commission: float = 0.
    min_start_length: int = 0
    islog: bool = False
    islogorder: bool = False
    isplot: bool = True
    clear_gap: bool = False
    data_segments: float | int = 1.
    slip_point = 0.
    print_account: bool = True
    pprint: bool = False  # 旧版打印账户信息
    key: str = 'datetime'
    start_time = None
    end_time = None
    time_segments: time | None= None
    profit_plot: bool = True
    click_policy: Literal["hide", "mute"] = "hide"
    take_time: bool = False
    on_close: bool = True
    replay: bool = False
    performance: bool = False  # 是否显示性能评估面板
    log_to_file: bool = True  # 是否保存日志
    auto_clean_days: int = 15  # 清理日志的天数

    def _get_commission(self):
        comm = dict()
        if isinstance(self.percent_commission, float) and self.percent_commission >= 0.:
            return comm.update(dict(percent_commission=self.percent_commission))
        else:
            if isinstance(self.tick_commission, (float, int)) and self.tick_commission >= 0.:
                comm.update(dict(tick_commission=self.tick_commission))
            elif isinstance(self.fixed_commission, (float, int)) and self.fixed_commission >= 0.:
                comm.update(dict(fixed_commission=self.fixed_commission))
        if not comm:
            return dict(fixed_commission=0.)
        return comm

    @property
    def logger_params_is_change(self) -> bool:
        return any([not self.log_to_file, self.auto_clean_days != 15])

    @property
    def logger_params(self) -> tuple:
        return self.log_to_file, self.auto_clean_days


def _addict__setattr__(self, name, value):
    super(Addict, self).__setattr__(name, value)
    # 使用 object.__getattribute__ 避免触发 Addict 的 __getattr__ 导致递归
    try:
        _bt_lines = object.__getattribute__(self, '_bt_lines')
        if isinstance(_bt_lines, Lines) and name not in _bt_lines:
            _bt_lines.append(name)
    except AttributeError:
        pass


def _addict__setitem__(self, name, value):
    super(Addict, self).__setitem__(name, value)
    # 使用 object.__getattribute__ 避免触发 Addict 的 __getattr__ 导致递归
    try:
        _bt_lines = object.__getattribute__(self, '_bt_lines')
        if isinstance(_bt_lines, Lines) and name not in _bt_lines:
            _bt_lines.append(name)
    except AttributeError:
        pass


def _addict__delattr__(self, name):
    super(Addict, self).__delattr__(name)
    # 使用 object.__getattribute__ 避免触发 Addict 的 __getattr__ 导致递归
    try:
        _bt_lines = object.__getattribute__(self, '_bt_lines')
        if isinstance(_bt_lines, Lines) and name in _bt_lines:
            _bt_lines.pop(_bt_lines.index(name))
    except AttributeError:
        pass


# 内置指标属性lines实现指标列设置
Addict.__setattr__ = _addict__setattr__
Addict.__setitem__ = _addict__setitem__
Addict.__delattr__ = _addict__delattr__


class Lines(list):
    """## 指标线列表"""
    _ind_obj: KLine | IndFrame | IndSeries | Line | None
    _ind_source: KLine | IndFrame | IndSeries | Line | None
    _lines: Addict

    def __init__(self, *args) -> None:
        _args = []
        for arg in args:
            if isinstance(arg, (list, tuple)):
                _args.extend(*arg)
            else:
                _args.append(arg)
        super(Lines, self).__init__(_args)
        object.__setattr__(self, "_ind_obj", None)
        object.__setattr__(self, "_ind_source", None)
        object.__setattr__(self, "_lines", Addict())
        self._lines.__dict__['_bt_lines'] = self

    def __setattr__(self, __name: str, __value: Any) -> None:
        if isinstance(__value, (pd.Series, np.ndarray)):
            # 始终同步 _lines 字典，确保 base.py 中 tobtind 能正确提取
            self._lines[__name] = __value
            if self._ind_obj is None:
                pass  # 仅 _lines 存储
            else:
                assert len(__value) == self._ind_source.length, "传入数据长度与指标长度不符"
                if self._ind_obj.isMDim:
                    self._ind_obj[__name] = __value
                else:
                    self._ind_obj[:] = __value
            return
        return super().__setattr__(__name, __value)

    def __getattribute__(self, name) -> IndSeries | Line | Any:
        # 避免无限递归：使用 object.__getattribute__ 直接访问 _ind_obj
        try:
            _ind_source = object.__getattribute__(self, '_ind_source')
            lines = _ind_source.lines
        except AttributeError:
            _ind_source = None
            lines = []
        if _ind_source is None:
            return super().__getattribute__(name)
        if name in lines:
            if _ind_source.ndim == 1:
                return _ind_source
            return object.__getattribute__(_ind_source, name)
        return super().__getattribute__(name)

    @property
    def values(self) -> list[str]:
        """## 指标线列表"""
        return list(self)

    @property
    def items(self) -> dict[str, pd.Series | np.ndarray]:
        """## 指标线数据"""
        return self._lines

    def __call__(self, ind) -> Lines:
        object.__setattr__(self, "_ind_source", ind)
        return self


class OrderedAddict(Addict):
    """## 有序Addict字典"""

    def _converted_key(self, key: str | int) -> str:
        if isinstance(key, int):
            key = list(self.keys())[key]
        return key

    def __getitem__(self, key: str | int) -> Line | IndSeries | IndFrame | KLine | Strategy:
        return super().__getitem__(self._converted_key(key))

    def __setitem__(self, name: str | int, value: Line | IndSeries | IndFrame | KLine | Strategy):
        name = self._converted_key(name)
        isFrozen = (hasattr(self, '__frozen') and
                    object.__getattribute__(self, '__frozen'))
        if isFrozen and name not in super(Dict, self).keys():
            raise KeyError(name)
        super(Dict, self).__setitem__(name, value)
        try:
            p = object.__getattribute__(self, '__parent')
            key = object.__getattribute__(self, '__key')
        except AttributeError:
            p = None
            key = None
        if p is not None:
            p[key] = self
            object.__delattr__(self, '__parent')
            object.__delattr__(self, '__key')

    def add_data(self, key: str | int, value: Iterable | Strategy) -> None:
        """## 向数据集中添加数据,当键相同时直接代替原数据"""
        self._validate_key(key)
        self._validate_value(
            value, ["Line", "IndSeries", "IndFrame"])
        self[key] = value

    @property
    def num(self) -> int:
        """## 数据集中数据的个数"""
        return len(self)

    def _validate_key(self, key: str | Any) -> None:
        """校验键类型（必须是 str）"""
        if not isinstance(key, str):
            raise TypeError(f"键必须是 str 类型，当前键「{key}」的类型为 {type(key).__name__}")

    def _validate_value(self, value: Any, class_name: str | list[str]) -> None:
        """校验值类型（类名必须是 class_name）"""
        # 方式1：严格校验类名（仅匹配当前类名，不匹配子类）
        if type(value).__name__ not in class_name:
            raise TypeError(
                f"值必须是「{class_name}」类型，当前值「{value}」的类型为 {type(value).__name__}"
            )


class StrategyInstances(OrderedDict, OrderedAddict):
    """## 策略实例有序字典"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, value in self.items():
            self._validate_key(key)
            self._validate_value(value)

    def _validate_value(self, value: Any, class_name: str | list[str] = "Strategy") -> None:
        """校验值类型（类名必须是 class_name）"""
        if not any(cls.__name__ == class_name for cls in type(value).__mro__):
            raise TypeError(
                f"值必须是「{class_name}」或其子类类型，当前值类型为 {type(value).__name__}"
            )

    def add_data(self, key: str | int, value: Iterable | Strategy) -> None:
        """## 向数据集中添加数据,当键相同时直接代替原数据"""
        self._validate_key(key)
        self._validate_value(value)
        self[key] = value


class BtIndicatorDataSet(OrderedDict, OrderedAddict):
    """## 策略指标数据集有序字典"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, value in self.items():
            self._validate_key(key)
            self._validate_value(
                value, ["Line", "IndSeries", "IndFrame"])

    @property
    def isplot(self) -> dict[str, bool]:
        """
        ## 获取指标绘图开关（属性接口，预留）
        - 用于控制所有指标是否显示在图表中，实际逻辑在setter中实现
        - 可集中设置是否画图

        Returns:
            dict: 指标名称和绘图开关状态的字典
        """
        return {k: v.isplot for k, v in self.items()}

    @isplot.setter
    def isplot(self, value: bool):
        for _, v in self.items():
            v.isplot = bool(value)

    @property
    def height(self) -> dict:
        """## 获取指标绘图高度（属性接口，预留）
        - 用于控制所有画图指标的高度
        - 可集中设置所有指标高度

        Returns:
            dict: 指标名称与绘图高度字典
        """
        return {k: v.height for k, v in self.items}

    @height.setter
    def height(self, value):
        if isinstance(value, (float, int)) and value >= 10:
            for _, v in self.items():
                v.height = int(value)

    def update_values(self):
        """## 更新指标数据"""
        for value in self.values():
            value._update_replace()


class KLinesSet(OrderedDict, OrderedAddict):
    """## 策略KLine数据集有序字典"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 优化：添加缓存机制，避免重复计算
        self._max_length_cache = None
        self._lengths_cache = None
        self._max_length: int | None = None
        for key, value in self.items():
            self._validate_key(key)
            self._validate_value(value, "KLine")

    @property
    def max_length(self) -> int:
        """## KLine数据最大长度"""
        # 优化：使用缓存，避免重复计算
        if self._max_length_cache is None:
            self._max_length_cache = max(self.lengths)
        return self._max_length_cache

    @property
    def lengths(self) -> list[int]:
        """## 各个数据长度"""
        # 优化：使用缓存，避免重复计算
        if self._lengths_cache is None:
            self._lengths_cache = [len(value) for _, value in self.items()]
        return self._lengths_cache

    @property
    def cycles(self) -> list[int]:
        """## 各个数据周期"""
        return [value.cycle for _, value in self.items()]

    @property
    def date_index(self) -> pd.Index:
        """## KLine数据时间日期索引,用于回测分析"""
        max_length = self.max_length
        return pd.Index(list(filter(lambda x: len(x) == max_length, self.values()))[0].datetime.values)

    @property
    def default_kline(self) -> KLine:
        """## KLine数据默认数据,索引为0的KLine数据"""
        return self[0]

    @property
    def last_kline(self) -> KLine:
        """## KLine数据最后添加的数据"""
        return self[-1]

    def add_data(self, key: str | int, value: KLine) -> None:
        """## 向数据集中添加数据,当键相同时直接代替原数据"""
        self._validate_key(key)
        self._validate_value(value, "KLine")
        self[key] = value
        # 优化：添加数据时清除缓存，确保下次访问时重新计算
        self._max_length_cache = None
        self._lengths_cache = None
        if not hasattr(self, "_isha"):
            self._isha = []
        self._isha.append(value._indsetting.isha)
        if not hasattr(self, "_islr"):
            self._islr = []
        self._islr.append(value._indsetting.islr)
        if self._max_length is None:
            self._max_length = len(value)
        else:
            self._max_length = max(self._max_length, len(value))

    def update_values(self):
        """## 更新K线数据"""
        for value in self.values():
            value._update_replace()
        # 优化：更新数据时清除缓存，确保下次访问时重新计算
        self._max_length_cache = None
        self._lengths_cache = None

    @property
    def tq_klines(self) -> list[pd.DataFrame]:
        """## 天勤K线数据集"""
        return [kline._dataset.tq_object for _, kline in self.items()]

    def get_replay_data(self, index) -> dict[str, KLine | pd.DataFrame]:
        return {k: v.pandas_object[:index+1] for k, v in self.items()}

    @property
    def isha(self) -> list[bool]:
        return self._isha

    @property
    def islr(self) -> list[bool]:
        return self._islr

    def update_values(self):
        """## 更新K线数据"""
        for value in self.values():
            value._update_replace()

    @property
    def height(self) -> dict:
        """## 获取指标绘图高度（属性接口，预留）
        - 用于控制所有K线图指标的高度
        - 可集中设置所有指标高度

        Returns:
            dict: 指标名称与绘图高度字典
        """
        return {k: v.height for k, v in self.items}

    @height.setter
    def height(self, value):
        if isinstance(value, (float, int)) and value >= 100:
            for _, v in self.items():
                v.height = int(value)


class DataSetBase:
    """## 数据基类"""

    def __getitem__(self, key: str):
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(f"Key '{key}' not found")

    def __setitem__(self, key: str, value: Any) -> None:
        if not hasattr(self, key):
            raise KeyError(f"Cannot set unknown key '{key}'")
        setattr(self, key, value)

    def __delitem__(self, key: str) -> None:
        if hasattr(self, key):
            raise KeyError(f"Deleting key '{key}' is not allowed")
        raise KeyError(f"Key '{key}' not found")

    def __iter__(self):
        return (field.name for field in fields(self))

    def __len__(self) -> int:
        return len(fields(self))

    def __contains__(self, key: object) -> bool:
        return hasattr(self, key) if isinstance(key, str) else False

    def keys(self) -> list[str]:
        """## 键"""
        return [field.name for field in fields(self)]

    def values(self) -> list:
        """## 值"""
        return [getattr(self, field.name) for field in fields(self)]

    def items(self) -> tuple[str]:
        """## 键值"""
        return [(field.name, getattr(self, field.name)) for field in fields(self)]

    def get(self, key: str, default: Any = None):
        """## 获取数据"""
        return getattr(self, key, default)

    def update(self, other: dict) -> None:
        """## 更新数据"""
        for key, value in other.items():
            if hasattr(self, key) and value != getattr(self, key):
                setattr(self, key, value)
            else:
                raise KeyError(f"Key '{key}' not found")

    @property
    def copy_values(self) -> dict:
        """## 返回深复制的属性字典"""
        return {k: deepcopy(v) for k, v in self.items()}

    def filt_values(self, *args, **kwargs) -> dict:
        """## 返回已过滤的属性

        args :list[str]. 要过滤（删除）属性名称.

        kwargs :dic[str,Any]. 要替换属性的名称."""
        values = self.copy_values
        if args:
            for arg in args:
                if arg in values:
                    values.pop(arg)
        if kwargs:
            values = {**values, **kwargs}
        return values

    def copy(self, **kwargs) -> BtID | IndSetting | Quotes | Broker | SymbolInfo | DataFrameSet:
        """## 复制"""
        values = self.copy_values
        if kwargs:
            values = {**values, **kwargs}
        return self.__class__(**values)

    # @property
    # def vars(self) -> dict:
    #     """## 递归转换为字典，处理嵌套的DataSetBase对象"""
    #     result = {}
    #     for field in fields(self):
    #         key = field.name
    #         value = getattr(self, key)

    #         # 递归处理嵌套的DataSetBase对象
    #         if isinstance(value, DataSetBase):
    #             result[key] = value.vars
    #         # 处理DataSetBase对象列表
    #         elif isinstance(value, (list, SpanList)):
    #             result[key] = [item.vars if isinstance(
    #                 item, DataSetBase) else item for item in value]
    #         # 处理Addict字典中的DataSetBase对象
    #         elif isinstance(value, dict):
    #             result[key] = {
    #                 k: v.vars if isinstance(v, DataSetBase) else v
    #                 for k, v in value.items()
    #             }
    #         elif isinstance(value, str):  # CategoryString
    #             result[key] = str(value)
    #         else:
    #             result[key] = value
    #     return result
    @property
    def vars(self) -> dict:
        """## 递归转换为字典，处理嵌套的DataSetBase对象
        新增：将SignalLabel实例转换为其vars字典（而非保留原对象）
        """
        result = {}
        for field in fields(self):
            key = field.name
            value = getattr(self, key)

            # ------------- 核心修改开始 -------------
            # 1. 优先处理SignalLabel实例：直接提取其vars属性（转为字典）
            if isinstance(value, SignalLabel):
                result[key] = value.vars
                continue
            # ------------- 核心修改结束 -------------

            # 2. 递归处理嵌套的DataSetBase对象（保留原有逻辑）
            if isinstance(value, DataSetBase):
                result[key] = value.vars
            # 3. 处理DataSetBase对象列表（含SpanList）
            elif isinstance(value, (list, SpanList)):
                processed_list = []
                for item in value:
                    if isinstance(item, SignalLabel):
                        # 列表中的SignalLabel同样转为字典
                        processed_list.append(item.vars)
                    elif isinstance(item, DataSetBase):
                        processed_list.append(item.vars)
                    else:
                        processed_list.append(item)
                result[key] = processed_list
            # 4. 处理Addict/dict字典中的嵌套对象（重点处理signalstyle）
            elif isinstance(value, dict):
                processed_dict = {}
                for k, v in value.items():
                    if isinstance(v, SignalLabel):
                        # 字典（如signalstyle）中的SignalLabel转为字典
                        processed_dict[k] = v.vars
                    elif isinstance(v, DataSetBase):
                        processed_dict[k] = v.vars
                    else:
                        processed_dict[k] = v
                result[key] = processed_dict
            # 5. 处理CategoryString等字符串子类
            elif isinstance(value, str):
                result[key] = str(value)
            # 6. 其他类型直接保留
            else:
                result[key] = value
        return result

    def to_dict(self) -> Addict:
        return Addict({k: v for k, v in self.items()})


@dataclass
class BtID(DataSetBase):
    """## BtID
    >>> strategy_id:int 策略实例 ID.
        plot_id:int 画图 ID,改变ID可在跨周期显示指标.
        data_id:int 数据 ID,导入数据的顺序.
        resample_id:int resample ID,原始数据的序号,即resample的数据从哪个数据转换而来的序号.
        replay_id:int replay ID,原始数据的序号,即replay的数据从哪个数据转换而来的序号.
    """
    strategy_id: int = 0
    plot_id: int = 0
    data_id: int = 0
    resample_id: int | None = None
    replay_id: int | None = None


@dataclass
class DefaultIndicatorConfig(DataSetBase):
    """## 默认指标配置"""
    id: BtID = field(default_factory=BtID)
    sname: str = "name"
    ind_name: str = "ind_name"
    lines: list[str] = field(default_factory=lambda: ["line",])
    category: str | None = None
    isplot: bool = True
    ismain: bool = False
    isreplay: bool = False
    isresample: bool = False
    overlap: bool = False
    isindicator: bool = True
    iscustom: bool = False
    dim_math: bool = True
    heigth: int = 150


@dataclass
class CandleStyle(DataSetBase):
    """## 蜡烛图风格"""
    bear: str | Colors = Colors.tomato
    bull: str | Colors = Colors.lime


@dataclass
class LineStyle(DataSetBase):
    """## 指标线风格"""
    line_dash: str | LineDash = LineDash.solid
    line_width: int | float = 1.3
    line_color: str | Colors | None = None
    price_line: bool = False
    price_label: bool = False


class LineStyleType:
    """## 设置指标线风格代理类型"""
    long_signal: LineStyle
    exitlong_signal: LineStyle
    short_signal: LineStyle
    exitshort_signal: LineStyle

    def __init__(self, dataframe):
        # 使用 object.__setattr__ 避免触发自定义的 __setattr__
        object.__setattr__(self, '_dataframe', dataframe)

    def __getattr__(self, name) -> LineStyle:
        # 代理属性获取到 dataframe 的 linstyle 字典
        return getattr(object.__getattribute__(self, '_dataframe')._plotinfo.linestyle, name)

    def __setattr__(self, name, value):
        # 代理属性设置到 dataframe 的 linstyle 字典
        if name in object.__getattribute__(self, '_dataframe')._plotinfo.linestyle:
            setattr(object.__getattribute__(
                self, '_dataframe')._plotinfo.linestyle, name, value)


class LineAttrType:
    """## 设置指标线属性风格代理类型"""
    long_signal: str | float | LineDash | Colors
    exitlong_signal: str | float | LineDash | Colors
    short_signal: str | float | LineDash | Colors
    exitshort_signal: str | float | LineDash | Colors

    def __init__(self, dataframe, attr):
        object.__setattr__(self, '_dataframe', dataframe)
        object.__setattr__(self, '_attr', attr)

    def __getattr__(self, name):
        # 返回代理对象，允许链式操作
        return LineAttrProxy(object.__getattribute__(self, '_dataframe'), name)

    def __setattr__(self, name, value):
        if name == '_dataframe':
            object.__setattr__(self, name, value)
        else:
            # 直接设置指定线条的 line_dash
            df: IndFrame = object.__getattribute__(self, '_dataframe')
            attr = object.__getattribute__(self, '_attr')
            if name not in df._plotinfo.linestyle:
                df._plotinfo.linestyle[name] = LineStyle()
            setattr(df._plotinfo.linestyle[name], attr, value)


class LineAttrProxy:
    """## 代理类，支持链式赋值 d.line_dash.line = value"""

    def __init__(self, dataframe, key):
        object.__setattr__(self, '_dataframe', dataframe)
        object.__setattr__(self, '_key', key)

    def __setattr__(self, name, value):
        if name in ['_dataframe', '_key']:
            object.__setattr__(self, name, value)
        else:
            # 设置指定属性的 line_dash
            df: IndFrame = object.__getattribute__(self, '_dataframe')
            key = object.__getattribute__(self, '_key')
            if key not in df._plotinfo.linestyle:
                df._plotinfo.linestyle[key] = LineStyle()
            setattr(df._plotinfo.linestyle[key], name, value)


class SignalStyleType:
    """## 设置信号指标线风格代理类型"""
    long_signal: SignalStyle
    exitlong_signal: SignalStyle
    short_signal: SignalStyle
    exitshort_signal: SignalStyle

    def __init__(self, dataframe):
        # 使用 object.__setattr__ 避免触发自定义的 __setattr__
        object.__setattr__(self, '_dataframe', dataframe)

    def __getattr__(self, name) -> SignalStyle:
        # 代理属性获取到 dataframe 的 signalstyle 字典
        return getattr(object.__getattribute__(self, '_dataframe')._plotinfo.signalstyle, name)

    def __setattr__(self, name, value):
        # 代理属性设置到 dataframe 的 signalstyle 字典
        if name in object.__getattribute__(self, '_dataframe')._plotinfo.signalstyle:
            setattr(object.__getattribute__(
                self, '_dataframe')._plotinfo.signalstyle, name, value)


class SignalAttrType:
    """## 设置信号指标线属性风格代理类型"""
    long_signal: str | Markers | bool | float | SignalLabel
    exitlong_signal: str | Markers | bool | float | SignalLabel
    short_signal: str | Markers | bool | float | SignalLabel
    exitshort_signal: str | Markers | bool | float | SignalLabel

    def __init__(self, dataframe, attr):
        object.__setattr__(self, '_dataframe', dataframe)
        object.__setattr__(self, '_attr', attr)

    def __getattr__(self, name) -> SignalStyle:
        # 返回代理对象，允许链式操作
        return SignalAttrProxy(object.__getattribute__(self, '_dataframe'), name)

    def __setattr__(self, name, value):
        if name == '_dataframe':
            object.__setattr__(self, name, value)
        else:
            # 直接设置指定线条的属性
            df: IndFrame = object.__getattribute__(self, '_dataframe')
            if name not in df._plotinfo.signalstyle:
                return
            attr = object.__getattribute__(self, '_attr')
            df._plotinfo.set_signal_attr(attr, name, value)


class SignalAttrProxy:
    """## 代理类，支持链式赋值 d.signal_key.long_signal = value"""

    def __init__(self, dataframe, key):
        object.__setattr__(self, '_dataframe', dataframe)
        object.__setattr__(self, '_key', key)

    def __setattr__(self, name, value):
        if name in ['_dataframe', '_key']:
            object.__setattr__(self, name, value)
        else:
            # 设置指定属性的属性
            key = object.__getattribute__(self, '_key')
            df: IndFrame = object.__getattribute__(self, '_dataframe')
            if key not in df._plotinfo.signallines:
                return
            df._plotinfo.set_signal_attr(key, name, value)


@dataclass
class SignalStyle(DataSetBase):
    """## 信号指标线风格"""
    key: str
    color: str
    marker: str
    overlap: bool = True
    show: bool = True
    size: int | float = 12.
    label: bool | SignalLabel = False

    def set_default_label(self, name: str):
        self.name = name
        self.label = default_signal_label.get(
            name).copy().set_position(name, self.key)
        return self

    def set_label(self,
                  text: str = "",
                  size: int = 10,
                  style: Literal["normal", "bold"] = "bold",
                  color: str = "red", **kwargs) -> SignalStyle:
        if not hasattr(self, "name"):
            self.name = kwargs.pop("name", "")
        text = text if isinstance(
            text, str) and text else signal_text_map.get(self.name)
        self.label = SignalLabel(
            text, size, style, color).set_position(self.name, self.key)
        return self


@dataclass
class SignalLabel(DataSetBase):
    """## 信号标签"""
    text: str = ""
    size: int = 10
    style: Literal["normal", "bold"] = "bold"
    color: str = "red"

    def set_position(self, signal_name: str = "", signal_key: str = ""):
        """设置标签生成的位置与偏移量"""
        is_buy_signal = any([name in signal_name for name in [
            "long", "exitshort"]]) or "low" in signal_key.lower()
        self.set_default_offset(is_buy_signal)
        return self

    @property
    def vars(self) -> dict:
        return dict(text=self.text, x_offset=self.x_offset, y_offset=self.y_offset, text_font_size=f"{self.size}pt", text_font_style=self.style, text_color=self.color)

    def copy(self) -> SignalLabel:
        return SignalLabel(**self.to_dict())

    def set_default_offset(self, islong):
        """
        ## 计算单个标签文字的偏移量

        ### 参数:
        - label: 要显示的文本
        - font_size: 字体大小（像素）
        - char_width_ratio_cn: 中文字符宽度比例
        - char_width_ratio_en: 英文字符宽度比例

        ### 返回:
        - (x_offset, y_offset): 水平和垂直偏移量
        """

        total_width = 0
        font_size = self.size
        label = self.text
        char_width_ratio_en = 0.78 if islong else 0.57
        char_width_ratio_cn = 1.32 if islong else 1.32

        # 遍历每个字符计算总宽度
        for char in label:
            if self.is_chinese_char(char):
                # 中文字符使用中文宽度比例
                char_width = font_size * char_width_ratio_cn
            else:
                # 非中文字符使用英文宽度比例
                char_width = font_size * char_width_ratio_en

            total_width += char_width

        # 计算偏移量（居中显示）
        x_offset = -total_width / 2  # 水平居中
        y_offset = -2.5*font_size if islong else 1.4*font_size    # 垂直居中

        self.x_offset, self.y_offset = x_offset, y_offset

    def is_chinese_char(self, char):
        """
        ## 判断字符是否为中文字符

        ### 参数:
        - char: 单个字符

        ### 返回:
        - bool: 是否为中文字符
        """
        if re.search(r'[\u4e00-\u9fff]', char):
            return True


signal_text_map = {
    "long_signal": "Long Entry",    # 多头入场标签
    "short_signal": "Short Entry",  # 空头入场标签
    "exitlong_signal": "Exit Long",  # 多头离场标签
    "exitshort_signal": "Exit Short"  # 空头离场标签
}

long_label = dict(size=10, style="bold", color="red")
short_label = dict(size=10, style="bold", color="green")

default_signal_label = {
    "long_signal": SignalLabel("Long Entry", **long_label),    # 多头入场标签
    "short_signal": SignalLabel("Short Entry", **short_label),  # 空头入场标签
    "exitlong_signal": SignalLabel("Exit Long", **short_label),  # 多头离场标签
    "exitshort_signal": SignalLabel("Exit Short", **long_label)  # 空头离场标签
}


def default_signal_style(name: str, overlap: bool = True, show: bool = True, size=12.) -> SignalStyle:
    """## 默认信号指标风格

    Args:
        name (str): 信号线名称
        overlap (bool, optional): 是否显示. Defaults to True.
        show (bool, optional): 是否显示. Defaults to True.
        size (_type_, optional): 图标大小. Defaults to 12..

    Returns:
        SignalStyle

    信号样式规则（四种信号各具特色，便于区分）：
        - long_signal: 多头买入信号 → 绿色三角形（lime + triangle）显示在低点
        - short_signal: 空头卖出信号 → 红色倒三角形（tomato + inverted_triangle）显示在高点
        - exitlong_signal: 多头平仓信号 → 橙色圆形（orange + circle）显示在高点
        - exitshort_signal: 空头平仓信号 → 蓝色圆形（blue + circle）显示在低点
    """
    if name == "long_signal":
        # 多头买入信号：绿色三角形显示在低点
        return long_signal_style(overlap, show, size)
    elif name == "short_signal":
        # 空头卖出信号：红色倒三角形显示在高点
        return short_signal_style(overlap, show, size)
    elif name == "exitlong_signal":
        # 多头平仓信号：橙色圆形显示在高点（与long_signal区分）
        return exit_long_signal_style(overlap, show, size)
    elif name == "exitshort_signal":
        # 空头平仓信号：蓝色圆形显示在低点（与short_signal区分）
        return exit_short_signal_style(overlap, show, size)
    else:
        # 默认使用空头信号样式
        return short_signal_style(overlap, show, size)


def long_signal_style(overlap: bool = True, show: bool = True, size=12.) -> SignalStyle:
    """## 多头买入或空头平仓信号风格

    Args:
        overlap (bool, optional): 是否显示. Defaults to True.
        show (bool, optional): 是否显示. Defaults to True.
        size (_type_, optional): 图标大小. Defaults to 12..

    Returns:
        SignalStyle
    """
    return SignalStyle("low", "lime", "triangle", overlap, show, size)


def short_signal_style(overlap: bool = True, show: bool = True, size=12.) -> SignalStyle:
    """## 多头平仓或空头卖出信号风格

    Args:
        overlap (bool, optional): 是否显示. Defaults to True.
        show (bool, optional): 是否显示. Defaults to True.
        size (_type_, optional): 图标大小. Defaults to 12..

    Returns:
        SignalStyle
    """
    return SignalStyle("high", "tomato", "inverted_triangle", overlap, show, size)


def exit_long_signal_style(overlap: bool = True, show: bool = True, size=12.) -> SignalStyle:
    """## 多头平仓信号风格（与long_signal区分）

    Args:
        overlap (bool, optional): 是否显示. Defaults to True.
        show (bool, optional): 是否显示. Defaults to True.
        size (_type_, optional): 图标大小. Defaults to 12..

    Returns:
        SignalStyle
    """
    return SignalStyle("high", "orange", "circle_x", overlap, show, size)


def exit_short_signal_style(overlap: bool = True, show: bool = True, size=12.) -> SignalStyle:
    """## 空头平仓信号风格（与short_signal区分）

    Args:
        overlap (bool, optional): 是否显示. Defaults to True.
        show (bool, optional): 是否显示. Defaults to True.
        size (_type_, optional): 图标大小. Defaults to 12..

    Returns:
        SignalStyle
    """
    return SignalStyle("low", "#908EEB", "circle_x", overlap, show, size)


@dataclass
class SpanStyle(DataSetBase):
    """## 水平线风格"""
    location: float = np.nan
    dimension: str = 'width'
    line_color: str | Colors = Colors.RGB666666  # '#666666'
    line_dash: str | LineDash = LineDash.dashed  # 'dashed'
    line_width: float = .8


def span_add(self: SpanList, other):
    if isinstance(other, (float, int)) and isfinite(other):
        other = SpanStyle(float(other))
    if isinstance(other, SpanStyle):
        if other.location not in self.locations:
            self.append(other)
    return self


def span_sub(self: SpanList, other):
    if isinstance(other, SpanStyle):
        other = other.location
    if isinstance(other, (float, int)) and isfinite(other):
        other = float(other)
        if other in self.locations:
            self.pop(self.locations.index(other))
    return self


class SpanList(list):
    __add__ = span_add
    __sub__ = span_sub
    __radd__ = span_add
    __rsub__ = span_sub
    __iadd__ = span_add
    __isub__ = span_sub

    @property
    def locations(self) -> list[float]:
        return [sapn.location for sapn in self]


@dataclass
class WaterMark(DataSetBase):
    text: str = ""
    font_size: int = 44
    # color: str = 'rgba(180, 180, 200, 0.5)'


@dataclass
class PlotInfo(DataSetBase, metaclass=KeyMeta):
    """## 画图信息"""
    height: int = 150
    sname: str = "name"
    ind_name: str = "ind_name"
    lines: Lines[str] = field(
        default_factory=lambda: Lines("line"))
    line_filed: list[str] = field(default_factory=list)
    signallines: list[str] = field(default_factory=list)
    category: CategoryString | str = Category.Any
    isplot: bool | dict[str, bool] = True
    overlap: bool | dict[str, bool] = False
    candlestyle: CandleStyle | None = None
    linestyle: Addict[str, LineStyle] | dict[str,
                                             LineStyle] = field(default_factory=Addict)
    signalstyle: Addict[str, SignalStyle] | dict[str,
                                                 SignalStyle] = field(default_factory=Addict)
    spanstyle: Spanlist[SpanStyle] | list[SpanStyle] = field(
        default_factory=SpanList)
    watermark: WaterMark | None = None
    source: str = ""

    def __post_init__(self):
        assert isinstance(self.lines, Iterable), "lines为可迭代字符串类型"
        self.set_lines_plot()
        self.set_lines_overlap()
        self.set_default_linestyle()
        self.set_spanstyle()
        self.set_default_signalstyle()
        self.category = CategoryString(self.category)

    def split_by_lines(self, selected_lines: list[str], split: bool = False) -> tuple[PlotInfo, PlotInfo] | PlotInfo:
        """## 根据指定的lines元素分割PlotInfo对象

        ### 参数:
        - selected_lines: 要提取的lines元素列表
        - split: 是否返回两个PlotInfo（True=返回(selected, remaining), False=只返回selected）

        ### 返回:
        - 如果split=True: 返回元组(包含selected_lines的PlotInfo, 包含剩余lines的PlotInfo)
        - 如果split=False: 返回包含selected_lines的PlotInfo
        """
        # 计算剩余lines
        selected_lines = [
            line for line in self.lines if line in selected_lines]
        remaining_lines = [
            line for line in self.lines if line not in selected_lines]

        # 创建包含selected_lines的PlotInfo
        selected_plotinfo = self.extract_by_lines(selected_lines)

        # 根据split参数决定返回值
        if split and remaining_lines:
            remaining_plotinfo = self.extract_by_lines(remaining_lines)
            return selected_plotinfo, remaining_plotinfo
        else:
            return selected_plotinfo

    def extract_by_lines(self, selected_lines: list[str]) -> PlotInfo:
        """## 根据指定的lines元素创建新的PlotInfo对象

        ### 参数:
        - selected_lines: 要提取的lines元素列表

        ### 返回:
        - 一个新的PlotInfo对象，只包含指定的lines及其相关属性
        """
        # 准备新对象的参数
        new_kwargs = {}

        # 处理基础属性
        for field_name in ['height', 'sname', 'ind_name', 'category', 'source']:
            new_kwargs[field_name] = deepcopy(getattr(self, field_name))

        # 设置lines
        new_kwargs['lines'] = Lines(*selected_lines)
        new_kwargs['line_filed'] = [f"_{string}" for string in selected_lines]

        # 处理signallines：只保留在selected_lines中的信号线
        new_signallines = [
            sig for sig in self.signallines if sig in selected_lines]
        new_kwargs['signallines'] = deepcopy(new_signallines)

        # 处理isplot
        if isinstance(self.isplot, dict):
            new_isplot = {line: self.isplot[line]
                          for line in selected_lines if line in self.isplot}
            new_kwargs['isplot'] = deepcopy(new_isplot)
        else:
            new_kwargs['isplot'] = deepcopy(self.isplot)

        # 处理overlap
        if isinstance(self.overlap, dict):
            new_overlap = {line: self.overlap[line]
                           for line in selected_lines if line in self.overlap}
            new_kwargs['overlap'] = deepcopy(new_overlap)
        else:
            new_kwargs['overlap'] = deepcopy(self.overlap)

        # 处理candlestyle
        new_kwargs['candlestyle'] = deepcopy(self.candlestyle)

        # 处理linestyle：只保留selected_lines对应的样式
        if isinstance(self.linestyle, Mapping):  # 兼容dict/Addict
            new_linestyle = {}
            for line in selected_lines:
                if line in self.linestyle:
                    new_linestyle[line] = deepcopy(self.linestyle[line])
            # 保持原类型（Addict/dict）
            if isinstance(self.linestyle, Addict):
                new_kwargs['linestyle'] = Addict(new_linestyle)
            else:
                new_kwargs['linestyle'] = dict(new_linestyle)
        else:
            new_kwargs['linestyle'] = deepcopy(self.linestyle)

        # 处理signalstyle：严格筛选！仅保留new_signallines中存在且在self.signalstyle中存在的键
        new_kwargs['signalstyle'] = Addict() if isinstance(
            self.signalstyle, Addict) else {}
        if isinstance(self.signalstyle, Mapping):  # 兼容dict/Addict
            new_signalstyle = {}
            # 仅遍历新的信号线列表，杜绝普通指标线混入
            for sig in new_signallines:
                if sig in self.signalstyle:
                    new_signalstyle[sig] = deepcopy(self.signalstyle[sig])
            # 保持原类型（Addict/dict）
            if isinstance(self.signalstyle, Addict):
                new_kwargs['signalstyle'] = Addict(new_signalstyle)
            else:
                new_kwargs['signalstyle'] = dict(new_signalstyle)
        else:
            new_kwargs['signalstyle'] = deepcopy(self.signalstyle)

        # 处理spanstyle
        new_kwargs['spanstyle'] = deepcopy(self.spanstyle)

        # 创建新对象
        new_plotinfo = PlotInfo(**new_kwargs)

        return new_plotinfo

    def set_default_signalstyle(self):
        """## 设置默认信号指标线样式"""
        if not self.signallines:
            return  # 无信号线时直接返回，避免空操作

        # 初始化空字典（保持原类型）
        if not isinstance(self.signalstyle, (dict, Addict)):
            self.signalstyle = Addict() if hasattr(
                self, 'signalstyle') and isinstance(self.signalstyle, Addict) else {}

        # 仅遍历signallines，为每个信号线设置默认样式
        for sig_line in self.signallines:
            if sig_line not in self.signalstyle:  # 避免覆盖已有的自定义样式
                self.signalstyle[sig_line] = default_signal_style(sig_line)

        # 后续处理信号叠加逻辑
        self._set_signal_overlap()

    def _set_signal_overlap(self):
        """## 当指标图没有画线时，禁止副图上显示，否则会有冲突"""
        if not self.signallines or not isinstance(self.isplot, dict):
            return

        # 仅遍历合法的信号线（与signallines对应）
        for sig_line in self.signallines:
            if sig_line in self.isplot and sig_line in self.signalstyle:
                if not self.isplot[sig_line]:
                    self.signalstyle[sig_line].overlap = True

    def set_default_candles(self, value, height=300, category=Category.Candles, candlestyle=CandleStyle()) -> PlotInfo:
        """## 设置默认K线图样式"""
        self.height = height
        self.category = category
        self.candlestyle = candlestyle
        self.spanstyle = SpanList([SpanStyle(value)])
        return self

    def copy(self) -> PlotInfo:
        """## 复制"""
        values = {}
        for k, v in self.items():
            values.update({k: deepcopy(v)})
        return PlotInfo(**values)

    def set_spanstyle(self):
        """## 设置水平线样式"""
        span = self.spanstyle
        if isinstance(span, Iterable) and span and all([isinstance(s, SpanStyle)] for s in span):
            self.spanstyle = SpanList(list(span))
            return
        if isinstance(span, float):
            span = SpanList([SpanStyle(span),])
        elif isinstance(span, SpanStyle):
            span = SpanList([span,])
        else:
            span = SpanList([SpanStyle(np.nan),])
        self.spanstyle = span

    def set_default_linestyle(self):
        """## 设置默认指标线风格"""
        for line in self.lines:
            if line not in self.linestyle:
                self.linestyle.update({line: LineStyle()})

    def set_lines_plot(self, isplot=None):
        """## 设置指标线是否显示"""
        if isplot is None:
            isplot = self.isplot
        if len(self.lines) > 1:
            if isinstance(isplot, dict):
                isplot = [bool(isplot.get(
                    line, True)) for line in self.lines]
            elif isinstance(isplot, Iterable):
                isplot = list(isplot)
                isplot = len(self.lines) > len(isplot) and isplot + \
                    [True] * \
                    (len(self.lines) - len(isplot)
                     ) or isplot[:len(self.lines)]
                isplot = [bool(value) for value in isplot]
            else:
                isplot = [bool(isplot),]*len(self.lines)
            self.isplot = Addict(zip(self.lines, isplot))
        else:
            self.isplot = bool(isplot)

    def set_lines_overlap(self, overlap=None):
        """## 设置指标线是否为主图叠加"""
        if overlap is None:
            overlap = self.overlap
        if len(self.lines) > 1:
            default_overlap = self.category == "overlap" or overlap
            if isinstance(overlap, dict):
                overlap = [bool(overlap.get(
                    line, default_overlap)) for line in self.lines]
            elif isinstance(overlap, Iterable):
                overlap = list(overlap)
                overlap = len(self.lines) > len(overlap) and overlap + \
                    [default_overlap] * \
                    (len(self.lines) - len(overlap)
                     ) or overlap[:len(self.lines)]
                overlap = [bool(value) for value in overlap]
            else:
                overlap = [bool(overlap),]*len(self.lines)
            self.overlap = Addict(zip(self.lines, overlap))
        else:
            self.overlap = bool(overlap)

    def rename_related_keys_using_mapping(self, values: dict):
        """## 使用传入的映射关系，对多个相关字典中的键进行重命名操作"""
        for old, new in values.items():
            if isinstance(self.isplot, dict):
                self.isplot[new] = self.isplot[old]
                del self.isplot[old]
            if isinstance(self.overlap, dict):
                self.overlap[new] = self.overlap[old]
                del self.overlap[old]
            if old in self.linestyle:
                self.linestyle[new] = self.linestyle[old]
                del self.linestyle[old]

    @property
    def signal_style(self) -> Addict[str, SignalStyle]:
        return self.signalstyle

    @signal_style.setter
    def signal_style(self, value: SignalStyle):
        if isinstance(value, SignalStyle):
            if not self.signalstyle:
                self.set_default_signalstyle()
            key = value.key
            assert key in self.lines or key in FILED.OHLC, f"{key}需要设置在指标线{self.lines.values}或在K线图{FILED.OHLC.tolist()}上."
            if key in self.signallines:
                value.overlap = False
            else:
                value.overlap = True
            # 只设置指定key的信号样式，而不是覆盖所有信号样式
            if key in self.signalstyle:
                self.signalstyle[key] = value.copy()
            else:
                # 如果key不在signalstyle中，添加它
                self.signalstyle[key] = value.copy()

    @property
    def signal_key(self) -> dict[str, str]:
        return {k: v.key for k, v in self.signalstyle.items()}

    @signal_key.setter
    def signal_key(self, value):
        if value in self.lines or value in FILED.OHLC:
            for k in self.signalstyle.keys():
                setattr(self.signalstyle[k], "key", value)
                setattr(self.signalstyle[k], "overlap",
                        value not in self.signallines)

    @property
    def signal_show(self) -> dict[str, bool]:
        return {k: v.show for k, v in self.signalstyle.items()}

    @signal_show.setter
    def signal_show(self, value):
        if self.signalstyle:
            [setattr(self.signalstyle[k], "show", bool(value))
             for k in self.signalstyle.keys()]

    @property
    def signal_color(self) -> dict[str, str]:
        return {k: v.color for k, v in self.signalstyle.items()}

    @signal_color.setter
    def signal_color(self, value):
        if self.signalstyle and value in Colors:
            [setattr(self.signalstyle[k], "color", value)
                for k in self.signalstyle.keys()]

    @property
    def signal_overlap(self) -> dict[str, bool]:
        return {k: v.overlap for k, v in self.signalstyle.items()}

    @signal_overlap.setter
    def signal_overlap(self, value):
        if self.signalstyle:
            [setattr(self.signalstyle[k], "overlap", bool(value))
             for k in self.signalstyle.keys() if self.isplot[k]]

    @property
    def signal_size(self) -> dict[str, int]:
        return {k: v.size for k, v in self.signalstyle.items()}

    @signal_size.setter
    def signal_size(self, value):
        if self.signalstyle and isinstance(value, (int, float)):
            [setattr(self.signalstyle[k], "size", float(max(1., value)))
             for k in self.signalstyle.keys()]

    @property
    def signal_label(self) -> dict[str, SignalLabel | bool]:
        return {k: v.label for k, v in self.signalstyle.items()}

    @signal_label.setter
    def signal_label(self, value):
        if self.signalstyle:
            if isinstance(value, bool) and value:
                for k, v in self.signalstyle.items():
                    v.set_default_label(k)
            elif isinstance(value, SignalLabel):
                [setattr(self.signalstyle[k], "label", value.copy().set_position(k, v.key))
                 for k, v in self.signalstyle.items()]

    def set_signal_attr(self, attr: str, name: str, value: Any):
        if attr == "key" and isinstance(value, str) and (value in self.lines or value in FILED.OHLC):
            setattr(self.signalstyle[name], attr, value)
            setattr(self.signalstyle[name], "overlap",
                    value in FILED.OHLC)
        elif attr == "show":
            setattr(self.signalstyle[name], attr, bool(value))
        elif attr == "color" and value in Colors:
            setattr(self.signalstyle[name], attr, value)
        elif attr == "overlap":
            setattr(self.signalstyle[name], attr, bool(value))
        elif attr == "size" and isinstance(value, (int, float)):
            setattr(self.signalstyle[name], attr, float(max(1., value)))
        elif attr == "label":
            if isinstance(value, str) and value:
                if not isinstance(self.signalstyle[name].label, SignalLabel):
                    self.signalstyle[name].set_default_label(name)
                self.signalstyle[name].label.text = value
            elif isinstance(value, SignalLabel) and value:
                self.signalstyle[name].label = value.set_position(
                    name, self.signalstyle[name].key)
            elif isinstance(value, bool):
                if value:
                    if not isinstance(self.signalstyle[name].label, SignalLabel):
                        self.signalstyle[name].set_default_label(name)
                else:
                    self.signalstyle[name].label = False

    @property
    def line_style(self) -> Addict[str, LineStyle]:
        return self.linestyle

    @line_style.setter
    def line_style(self, value: LineStyle):
        if isinstance(value, LineStyle):
            for k in self.linestyle.keys():
                self.linestyle[k] = value.copy()
            # [setattr(self.linestyle, k, value.copy())
            #  for k in self.linestyle.keys()]

    @property
    def line_dash(self) -> dict[str, str]:
        return {k: v.line_dash for k, v in self.linestyle.items()}

    @line_dash.setter
    def line_dash(self, value):
        if value in LineDash:
            [setattr(self.linestyle[k].line_dash, value)
             for k in self.linestyle.keys()]

    @property
    def line_width(self) -> dict[str, float]:
        return {k: v.line_width for k, v in self.linestyle.items()}

    @line_width.setter
    def line_width(self, value):
        if isinstance(value, (int, float)) and value > .0:
            [setattr(self.linestyle[k].line_width, float(value))
             for k in self.linestyle.keys()]

    @property
    def line_color(self) -> dict[str, str | Colors]:
        return {k: v.line_color for k, v in self.linestyle.items()}

    @line_color.setter
    def line_color(self, value):
        if value in Colors or (value and isinstance(value, str)):
            [setattr(self.linestyle[k].line_color, value)
             for k in self.linestyle.keys()]

    @property
    def price_line(self) -> dict[str, str | Colors]:
        return {k: v.price_line for k, v in self.linestyle.items()}

    @price_line.setter
    def price_line(self, value):
        [setattr(self.linestyle[k].price_line, bool(value))
            for k in self.linestyle.keys()]

    @property
    def price_label(self) -> dict[str, str | Colors]:
        return {k: v.price_label for k, v in self.linestyle.items()}

    @price_label.setter
    def price_label(self, value):
        [setattr(self.linestyle[k].price_label, bool(value))
            for k in self.linestyle.keys()]

    @property
    def span_style(self) -> Spanlist[SpanStyle]:
        return self.spanstyle

    @span_style.setter
    def span_style(self, value):
        self.spanstyle += value

    @property
    def span_location(self) -> list[float]:
        return [span.location for span in self.spanstyle]

    @property
    def span_color(self) -> list[str]:
        return [span.line_color for span in self.spanstyle]

    @span_color.setter
    def span_color(self, value):
        if value in Colors or (value and isinstance(value, str)):
            [setattr(span, "line_color", value) for span in self.spanstyle]

    @property
    def span_dash(self) -> list[str]:
        return [span.line_dash for span in self.spanstyle]

    @span_dash.setter
    def span_dash(self, value):
        if value in LineDash:
            [setattr(span, "line_dash", value) for span in self.spanstyle]

    @property
    def span_width(self) -> list[str]:
        return [span.line_width for span in self.spanstyle]

    @span_width.setter
    def span_width(self, value):
        if isinstance(value, (float, int)) and value > 0.:
            [setattr(span, "line_width", float(value))
             for span in self.spanstyle]


@dataclass
class Multiply:
    """## 指标信息
    - 引用于多线程计算

    Args:
        func: Callable     :指标函数
        params: dict       :指标参数
        data: Any | KLine :指标数据

    Examples:
    >>> self.ebsw, self.ma1, self.ma2, self.buy_signal, self.sell_signal, self.ema40 = self.multi_apply(
            Multiply(Ebsw, data=self.data),
            [self.data.sma, dict(length=20),],
            [self.data.sma, dict(length=30),],
            [self.test1.t1.cross_up, dict(b=self.test1.t2)],
            [self.test1.t1.cross_down, dict(b=self.test1.t2),],
            Multiply(PandasTa.ema, dict(length=40), data=self.data))
            Multiply(PandasTa.ema, dict(length=20), self.data)
    """
    func: Callable
    params: dict = field(default_factory=dict)
    data = None

    @property
    def values(self) -> tuple[Callable, dict, KLine]:
        """## 返回计算指标信息"""
        return self.func, self.params, self.data


class _tq:
    @property
    def values(self) -> dict:
        return {k: v for k, v in vars(self).items()}


@dataclass
class tq_account(_tq):
    broker_id: str
    account_id: str
    password: str


@dataclass
class tq_auth(_tq):
    user_name: str
    password: str


# 缓存字典，用于存储get_cycle函数的结果
_get_cycle_cache = {}


def get_cycle(datetime: pd.Series) -> int:
    """## 获取时间序列周期（总秒数）

    Args:
        datetime (pd.Series): 时间序列

    Returns:
        int: 时间序列周期（秒），异常时返回0
    """
    # 优化：添加缓存机制，避免重复计算
    # 使用时间序列的哈希值作为缓存键
    try:
        # 计算缓存键：使用时间序列的前几个值和长度作为哈希基础
        cache_key = hash((len(datetime), tuple(
            datetime[:5].values), tuple(datetime[-5:].values)))
        if cache_key in _get_cycle_cache:
            return _get_cycle_cache[cache_key]
    except:
        # 如果哈希计算失败，继续执行计算
        pass

    # 1. 基础类型检查
    if not pd.api.types.is_datetime64_any_dtype(datetime):
        print("警告：时间序列不是datetime类型，返回周期0")
        return 0

    # 2. 计算时间差（处理空数据/单条数据）
    if len(datetime) < 2:
        print("警告：时间序列长度不足2，无法计算周期，返回0")
        return 0

    # 3. 计算相邻时间差（填充最后一个值避免空值）
    time_diff = datetime.diff().bfill()

    # 4. 过滤掉0值/NaT值的时间差
    valid_diff = time_diff[(time_diff > pd.Timedelta(0)) & (time_diff.notna())]
    if valid_diff.empty:
        print("警告：无有效时间差数据，返回周期0")
        return 0

    # 5. 统计最频繁的时间差（避免Counter取多值问题）
    # 转换为总秒数后统计，避免Timedelta精度问题
    diff_seconds = valid_diff.dt.total_seconds()
    most_common_diff = Counter(diff_seconds).most_common(1)
    if not most_common_diff:
        return 0
    cycle = int(most_common_diff[0][0])  # 取最频繁的时间差作为周期

    # 缓存结果
    try:
        if cache_key not in _get_cycle_cache:
            _get_cycle_cache[cache_key] = cycle
    except:
        pass

    return cycle


def ffillnan(arr: np.ndarray) -> np.ndarray:
    """## 过滤NAN值"""
    if len(arr.shape) > 1:
        arr = pd.DataFrame(arr)
    else:
        arr = pd.Series(arr)
    arr.ffill(inplace=True)
    return arr.values


def abc(df: pd.DataFrame, lim: float = 5., **kwargs) -> pd.DataFrame:
    """## 将K线范围压缩至lim个price_tick以内"""
    col = list(df.columns)
    if "price_tick" in kwargs:
        price_tick = kwargs.pop("price_tick")
    else:
        price_tick = df.price_tick.iloc[0] if 'price_tick' in col else 0.01
    col1 = FILED.OHLC.tolist()
    col2 = list(set(col)-set(col1))
    assert set(col).issuperset(col1)
    frame = pd.DataFrame(columns=col1)
    df1, df2 = df[col1], df[col2]
    for rows in df1.itertuples():
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
                    up = high-close
                    close = open+lim*price_tick
                    high = close+up
                else:
                    down = close-low
                    close = open-lim*price_tick
                    low = close-down
        else:
            if tick > lim:
                if close >= open:
                    up = high-close
                    close = open+lim*price_tick
                    high = close+up
                else:
                    down = close-low
                    close = open-lim*price_tick
                    low = close-down
        preclose = close
        frame.loc[index, :] = [open, high, low, close]
    frame = frame.astype(np.float64)
    return pd.concat([frame, df2], axis=1)[col]


@dataclass
class IndSetting(DataSetBase, metaclass=KeyMeta):
    """## 框架指标元数据配置类（继承 DataSetBase，使用元类 Meta 管理）
        - 核心定位：统一存储指标的基础属性、数据维度、状态标识等元信息，为指标的创建、计算、绘图、数据联动提供标准化配置支撑，是框架内指标生命周期管理的核心数据载体
        ### 核心作用：
        1. 指标身份标识：通过 `id` 实现指标唯一区分，避免多指标数据冲突
        2. 数据维度管理：记录指标的行/列数量（`v`/`h`）、列名映射（`line_filed`），确保数据结构一致性
        3. 状态标识控制：通过布尔属性标记指标的特殊类型（如是否为自定义数据 `iscustom`、是否为多维度 `isMDim`）
        4. 计算与联动配置：定义数据维度匹配规则（`dim_match`）、最新数据索引（`last_index`），支撑指标迭代计算
        ### 字段说明（按功能分类）：
        #### 一、指标唯一标识与基础状态
        1. id (BtID): 指标唯一标识对象（默认通过 `BtID` 工厂函数自动生成）
            - 作用：区分不同指标实例，尤其在多指标并行计算、数据缓存时避免混淆
            - 示例：两个相同参数的MA指标，通过不同 `id` 标记为独立实例
        2. is_mir (bool): MIR（多指标复用）标识（默认 False）
            - 作用：标记指标是否支持多场景复用（如同一指标同时用于信号生成与风险控制）
            - 说明：设为 True 时，指标会启用特殊的缓存与更新逻辑
        3. isha (bool): Heikin-Ashi（布林带K线）标识（默认 False）
            - 作用：标记指标是否为布林带K线衍生数据（如 `ha` 方法生成的K线）
            - 关联：与 `KLine` 的 `Heikin_Ashi_Candles` 属性联动，用于绘图样式适配
        4. islr (bool): Linear Regression（线性回归）标识（默认 False）
            - 作用：标记指标是否为线性回归衍生数据（如 `lrc` 方法生成的K线）
            - 关联：与 `KLine` 的 `Linear_Regression_Candles` 属性联动，用于计算逻辑适配
        #### 二、指标数据类型与场景标识
        5. ismain (bool): 主指标/主K线标识（默认 False）
            - 作用：
                - 对K线类数据：标记是否为主图K线（即显示在绘图区域的第一个图表）
                - 对技术指标：标记是否为核心主指标（如策略依赖的关键均线）
            - 影响：主指标会优先加载，且绘图时默认置于顶层
        6. isreplay (bool): 实时回放数据标识（默认 False）
            - 作用：标记指标数据是否来自周期回放（如 `replay` 方法转换的低频数据）
            - 影响：设为 True 时，指标计算会启用回放模式的时间戳同步逻辑
        7. isresample (bool): 重采样数据标识（默认 False）
            - 作用：标记指标数据是否来自周期重采样（如 `resample` 方法转换的高频转低频数据）
            - 影响：设为 True 时，指标会自动适配重采样后的时间粒度，避免计算偏差
        8. isindicator (bool): 技术指标标识（默认 True）
            - 作用：区分指标数据与基础数据（如K线原始数据 `KLine` 设为 False）
            - 影响：设为 False 时，会跳过指标专属的计算逻辑（如 `step` 方法调用）
        9. iscustom (bool): 自定义数据标识（默认 False）
            - 作用：标记指标是否为用户手动创建的自定义数据（如通过整数长度初始化的全NaN序列）
            - 影响：设为 True 时，数据会被存入 `_dataset.custom_object`，支持特殊的更新策略
        #### 三、数据维度与结构配置
        10. isMDim (bool): 多维度标识（默认 True）
            - 作用：标记指标是否为多维度数据（如 `IndFrame` 多列指标设为 True，`IndSeries` 单列指标设为 False）
            - 影响：多维度指标会启用列级独立配置（如每列单独设置线型）
        11. dim_match (bool): 维度匹配标识（默认 True）
            - 作用：控制指标计算时是否强制与源数据维度一致（如K线数据行数）
            - 场景：
                - True：指标长度必须与源数据相同，避免数据错位
                - False：允许指标长度小于源数据（如仅计算最新N期数据）
        12. line_filed (list[str]): 指标列名映射列表（默认空列表）
            - 作用：存储指标列名的下划线前缀形式（如列名 "ma5" 对应 "_ma5"）
            - 用途：用于动态绑定 `Line` 实例到 `IndFrame`/`IndSeries`，实现列级属性访问（如 `df._ma5`）
        ### 使用说明：
        - 1. 自动初始化：通常无需用户手动创建 `IndSetting` 实例，指标类（如 `IndFrame`/`IndSeries`/`KLine`）初始化时会自动生成
        - 2. 配置修改：可通过指标实例的 `_indsetting` 属性访问并修改配置（如 `df._indsetting.ismain = True` 设为主图指标）
        - 3. 关联逻辑：`IndSetting` 与 `PlotInfo`（绘图配置）联动，指标的 `isplot`/`overlap` 等可视化相关配置会同步到 `PlotInfo`
        ### 示例：
        >>> #1. 访问指标的 IndSetting 配置
        # 自动生成 IndSetting 实例
        >>> df = IndFrame(raw_data, lines=["ma5", "ma10"])
        >>> print(df._indsetting.v)  # 输出指标行数（如 100）
        >>> print(df._indsetting.line_filed)  # 输出列名映射（如 ["_ma5", "_ma10"]）
        >>>
        >>> #2. 修改配置
        >>> df._indsetting.ismain = True  # 将 IndFrame 设为主图指标
        >>> df._indsetting.dim_match = False  # 允许指标长度与源数据不匹配
    """
    id: BtID = field(default_factory=BtID)  # 使用 default_factory
    is_mir: bool = False
    isha: bool = False
    islr: bool = False
    ismain: bool = False
    isreplay: bool = False
    isresample: bool = False
    isindicator: bool = True
    iscustom: bool = False
    isMDim: bool = True
    dim_match: bool = True


def get_category(category: Any) -> str | None:
    """## 类别转换"""
    if isinstance(category, bool) and category:
        return 'overlap'
    elif isinstance(category, str):
        return category


def retry_with_different_params(params_list, times=3):
    """
    ## 装饰器:使用retrying库,在每次重试时更换参数组合。

    ### 参数:
        params_list (list): 参数列表

    ### 返回:
        装饰后的函数，调用时会按顺序尝试参数列表中的参数
    """
    def decorator(func):
        # 将参数列表转换为迭代器
        params_iter = iter(params_list)

        @retry(
            stop_max_attempt_number=min(
                len(params_list), times),  # 最大尝试次数=参数个数
            retry_on_exception=lambda _: True,        # 对所有异常重试
            wrap_exception=True                       # 保留原始异常信息
        )
        def wrapped_function():
            try:
                current_params = next(params_iter)     # 获取下一个参数
            except StopIteration:
                raise ValueError("所有参数尝试均失败")

            try:
                # 根据参数类型调用函数
                return func(current_params)
            except Exception as e:
                print(f"参数 {current_params} 失败: {e}")
                raise  # 抛出异常以触发重试

        return wrapped_function
    return decorator


def check_type(datas):
    """## 检查数据是否为np.ndarray, pd.Series, pd.DataFrame类型"""
    if isinstance(datas, (list, tuple)):
        return all([isinstance(data, (np.ndarray, pd.Series, pd.DataFrame)) for data in datas])
    return isinstance(datas, (np.ndarray, pd.Series, pd.DataFrame))


def get_stats(func):
    """## quantstats统计"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        func_name = func.__name__
        return getattr(qs_stats, func_name)(*args, **kwargs)
    return wrapper


def qs_plots(func):
    """## quantstats图表"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        func_name = func.__name__
        return getattr(qs_plot, func_name)(*args, **kwargs)
    return wrapper


# def storeData(data, filename='examplePickle'):
#     """## 读取pickle数据"""
#     with open(filename, 'wb') as f:
#         pickle.dump(data, f)


# def loadData(filename='examplePickle'):
#     """## 保存为pickle数据"""
#     try:
#         with open(filename, 'rb') as f:
#             db = pickle.load(f)
#         return db
#     except:
#         return None


def storeData(data, filename='exampleJoblib'):
    try:
        # joblib.dump 直接写入文件，无需手动打开文件句柄（更简洁）
        joblib.dump(data, filename, compress=3)
    except Exception as e:  # 建议捕获具体异常，而非空except
        # 可添加异常提示，方便调试
        # print(f"保存数据失败：{str(e)}")
        return None


def loadData(filename='exampleJoblib'):
    try:
        # joblib.load 直接读取文件
        db = joblib.load(filename)
        return db
    except Exception as e:
        # print(f"加载数据失败：{str(e)}")
        return None


def trytokline(data: pd.DataFrame) -> pd.DataFrame:
    """## 将原始dataframe数据转为一定格式的内部数据
    - 时间包含date,其它的包括open,high,low,close,volume"""
    from pandas.api.types import is_datetime64_any_dtype, is_string_dtype, is_float_dtype
    data.columns = [col.lower() for col in data.columns]
    cols = data.columns
    datetime_list = [col for col in cols if is_datetime64_any_dtype(data[col])]
    if datetime_list:  # 按时间类型直接寻找时间序列
        datetime = data[datetime_list[0]]
    else:  # 在关键字符date中查找
        datetime_list = [col for col in cols if 'date' in col]
        if_fall = False
        if datetime_list:
            try:  # 将包含date字符的列进行时间转换
                datetime = data[datetime_list[0]]
                datetime = datetime.apply(time_to_datetime)
            except:
                if_fall = True
        if if_fall:  # 在列数据为字符串中查找
            datetime_list = [col for col in cols if is_string_dtype(data[col])]
            assert datetime_list, '找不到时间序列'
            datetime, if_break = None, False
            for date in datetime_list:
                try:  # 深度将字符串转为时间类型
                    datetime_ = data[date]
                    datetime = datetime_.apply(time_to_datetime)
                    if_break = True
                except:
                    ...
                if if_break:
                    break
            assert datetime is not None, '找不到时间序列'
    datas = [datetime,]
    try:
        datas.extend([data[[col for col in cols if (
            'vol' if 'vol' in filed else filed) in col][0]] for filed in FILED.OHLCV])
    except:  # 以open,high,low,close,volume首个字母来确定数据列
        datas_ = [data[[col for col in cols if col.startswith(filed)][0]] for filed in [
            'o', 'h', 'l', 'c', 'v']]
        assert all([is_float_dtype(data_) for data_ in datas_]
                   ), 'open,high,low,close,volume列中有非浮点类型数据'
        datas.extend(datas_)
        assert len(datas) < 6, '找不到时间序列'
    return pd.DataFrame(dict(zip(FILED.ALL, datas)))


class Actions(int, Enum):
    """## 动作

    >>> HOLD :持仓
        BUY :买入
        SELL :卖出
        Long_exit :多头平仓
        Short_exit :空头平仓
        Long_reversing :多头反手(卖出)
        Short_reversing :空头反手(买入)
    """
    HOLD = 0
    BUY = 1
    SELL = 2
    Long_exit = 3
    Short_exit = 4
    Long_reversing = 5
    Short_reversing = 6


class BtPosition(int):
    """## 策略内部持仓对象

    - 多头: BtPosition(1)
    - 无仓位: BtPosition(0)
    - 空头: BtPosition(-1)"""
    broker: Broker

    @property
    def value(self) -> int:
        """### 值"""
        return int(self)

    @property
    def pos(self) -> int:
        """
        ### 仓位:正数为多头,负数为空头,0为无持仓
        """
        return self.broker._size*self.value

    @property
    def poses(self) -> list[int]:
        """### 逐笔合约成交手数"""
        return [size*self.value for size in self.broker._sizes]

    @property
    def pos_long(self) -> int:
        """
        ### 多头持仓:正数为多头,0为无持仓
        """
        return self > 0 and self.pos or 0

    @property
    def pos_short(self) -> int:
        """
        ### 空头持仓:正数为空头,0为无持仓
        """
        return self < 0 and self.pos or 0

    @property
    def open_price(self) -> int:
        """### 开仓价格"""
        return self.broker._open_price

    @property
    def open_price_long(self) -> float:
        """
        ### 多头开仓价格:无持仓返回0.
        """
        return 0. if self <= 0 else self.broker._open_price

    @property
    def open_cost_long(self) -> float:
        """
        ### 多头开仓成本价(包括开仓成本):无持仓返回0.
        """
        return 0. if self <= 0 else self.broker._cost_price

    @property
    def open_price_short(self) -> float:
        """
        ### 空头开仓价格:无持仓返回0.
        """
        return 0. if self >= 0 else self.broker._open_price

    @property
    def open_cost(self) -> float:
        """
        ### 开仓成本价
        """
        return self.broker._cost_price

    @property
    def open_cost_short(self) -> float:
        """
        ### 空头开仓成本价(包括开仓成本):无持仓返回0.
        """
        return 0. if self >= 0 else self.broker._cost_price

    @property
    def float_profit(self) -> float:
        """
        ### 持仓浮动盈亏:无持仓返回0.
        """
        return self.broker._float_profit

    @property
    def float_profit_long(self) -> float:
        """
        ### 多头持仓浮动盈亏:无持仓返回0.
        """
        return self.float_profit if self > 0 else 0.

    @property
    def float_profit_short(self) -> float:
        """
        ### 空头持仓浮动盈亏:无持仓返回0.
        """
        return self.float_profit if self < 0 else 0.

    @property
    def margin_long(self) -> float:
        """
        ### 多头持仓保证金:无持仓返回0.
        """
        return self > 0 and self.broker._margin or 0.

    @property
    def margin_short(self) -> float:
        """
        ### 空头持仓保证金:无持仓返回0.
        """
        return self < 0 and self.broker._margin or 0.

    @property
    def margin(self) -> float:
        """
        ### 持仓保证金:无持仓返回0.
        """
        return self.broker._margin

    @property
    def step_margin(self) -> list[float]:
        """### 合约逐笔保证金"""
        return self.broker._step_margin

    @property
    def current_commission(self) -> float:
        """### 当前交易手续费"""
        return self.broker._comm

    def __call__(self, broker: Broker):
        """### 绑定代理"""
        self.broker = broker
        return self


class TrainPosition(int, Enum):
    """## 强化学习动作"""
    SHORT = -1
    FLAT = 0
    LONG = 1


class PositionCreator:
    """## 账户内置仓位对象创建器"""
    @property
    def LONG(self):
        return BtPosition(1)

    @property
    def FLAT(self):
        return BtPosition(0)

    @property
    def SHORT(self):
        return BtPosition(-1)


@dataclass
class Quotes(DataSetBase):
    datetime: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    price_tick: float
    volume_multiple: float
    tick_value: float

    @property
    def last_price(self):
        return self.close


class Base:
    """## 全局变量"""
    _isstart: bool = False
    _strategy_instances = StrategyInstances()  # 所有策略实例
    _is_live_trading: bool = False  # 是否为真正交易
    _api: TqApi | None = None  # 天勤API
    tq: bool = False
    _base_dir: str = BASE_DIR  # 基础路径
    _tqobjs: dict[str, TqObjs] = {}  # 天勤数据字典
    _tq_contracts_dict: dict = {}  # 期货合约字典
    _datas: list[pd.DataFrame] = []  # 外部传入数据列表
    # 策略回放
    _strategy_replay: bool = False
    _trading_view: TradingView | None = None
    _logger: Logger | None = None
    _btplot: Callable | None = None
    _light_chart:bool = False

    @classmethod
    def Logger(cls) -> Logger:
        """## 日志"""
        if cls._logger is None:
            from .logger import get_logger
            cls._logger = get_logger()
        return cls._logger

    @classmethod
    def BtPlot(cls) -> Callable:
        """## 画图"""
        if cls._btplot is None:
            from .btplot import btplot
            cls._btplot = btplot
        return cls._btplot


class Orders(list):
    broker: Broker

    def __init__(self, broker: Broker):
        super().__init__()
        self.broker = broker

    @property
    def is_active(self: list[Order]) -> bool:
        """## 是否有订单"""
        return any([order.is_active for order in self])

    @property
    def cancel_orders(self: list[Order]) -> None:
        """## 取消所有订单"""
        for order in self:
            self.broker.cancel_order(order)


class Broker(Base):
    """## 框架交易代理核心类（继承 Base 类）
        - 核心定位：作为 `KLine` 与 `BtAccount` 之间的中间层，统一处理交易执行、仓位管理、手续费计算、保证金核算、盈亏统计等量化交易核心逻辑，是回测与模拟交易的核心控制单元

        ### 核心职责：
            - 1. 交易生命周期管理：处理开仓（加仓）、平仓（减仓）、反手等完整交易流程，包含资金充足性校验
            - 2. 仓位与订单记录：通过队列维护逐笔交易详情（保证金、价格、手数、手续费），支持仓位均价、浮动盈亏计算
            - 3. 成本与风险控制：实现三种手续费计算模式，动态核算保证金（逐笔/总保证金），确保交易符合风险规则
            - 4. 账户联动更新：实时同步交易结果到关联的 `BtAccount`（如可用资金、总盈亏、累计手续费）
            - 5. 交易日志生成：支持开启交易日志（`islog=True`），自动生成中文格式的开仓/平仓记录


        ### 核心特性：
        1. 灵活的手续费体系：
            - 支持三种手续费类型：按 tick 价值（`tick_commission`）、固定金额（`fixed_commission`）、按成交金额百分比（`percent_commission`）
            - 通过 `_setcommission` 自动绑定对应计算函数，交易时实时计算手续费
        2. 精细化仓位管理：
            - 依赖 `PositionCreator` 生成仓位状态（`LONG` 多头/`SHORT` 空头/`FLAT` 平仓）
            - 用 `LifoQueue`（后进先出队列）记录逐笔交易信息，支持部分平仓、逐笔保证金核算
        3. 实时盈亏与成本计算：
            - 动态计算开仓均价（`_open_price`）、持仓成本价（`_cost_price`）
            - 实时更新浮动盈亏（`_float_profit`）、累计盈亏（`cum_profits`）
        4. 资金风险校验：
            - 开仓/加仓前校验可用资金是否覆盖「保证金+手续费」，不足时触发交易失败（调用 `account._fail_to_trade`）
            - 支持保证金率（`margin_rate`）配置，动态计算所需保证金
        5. 多场景适配：
            - 与 `KLine` 强关联，从 `KLine` 获取当前价格（`current_close`）、时间（`current_datetime`）、合约信息（最小变动单位、乘数）
            - 与 `BtAccount` 实时联动，更新账户资金状态（可用资金、总手续费、总盈亏）


        ### 初始化参数说明：
        Args:
            kline (KLine): 关联的K线数据实例（必须包含合约信息、账户对象 `account`）
            **kwargs: 额外配置参数（优先级：kwargs > KLine策略配置 > 框架默认）：
                - config (Config): 策略配置对象（默认从 `kline.strategy_instance.config` 获取，无则使用框架默认 `btconfig`）
                - margin_rate (float): 保证金率（默认从 config 获取，框架默认通常为 0.08，即8%）
                - commission (dict): 手续费配置（键为 `commission_keys` 中的类型，值为费率/金额，默认从 config 获取）
                - slip_point (float): 滑点（每笔交易额外成本，默认从 config 获取，单位：最小变动单位）
                - islog (bool): 是否开启交易日志（默认 False，开启后生成中文交易记录）
                - index: 账户中 broker 的索引标识（默认 None，用于多 broker 场景区分）


        ### 核心属性说明（按功能分类）：
        一、基础关联与标识
            - 1. kline (KLine): 关联的K线数据实例，提供价格、时间、合约信息（`price_tick`/`volume_multiple`）
            - 2. account (BtAccount): 关联的账户实例，用于更新资金状态（可用资金、总盈亏等）
            - 3. symbol (str): 合约名称（从 `kline.symbol_info` 获取）
            - 4. __name__ (str): Broker 唯一名称（格式：`合约名_周期_broker`，如 "SHFE.rb2410_60_broker"）
            - 5. islog (bool): 交易日志开关（True 时生成开仓/平仓中文日志）

        二、手续费与滑点配置
            - 1. commission_keys (list[str]): 支持的手续费类型列表（["tick_commission", "fixed_commission", "percent_commission"]）
            - 2. commission (dict): 手续费配置字典（键为类型，值为对应参数，如 {"fixed_commission": 1.5} 表示每手固定1.5元）
            - 3. commission_value (float): 当前手续费类型的参数值（如固定手续费1.5元则为1.5）
            - 4. commission_func (Callable): 手续费计算函数（自动绑定，如 `_get_comm_fixed` 对应固定手续费）
            - 5. slip_point (float): 滑点（每笔交易额外增加的成本，单位：最小变动单位）

        三、保证金与成本
            - 1. margin_rate (float): 保证金率（如 0.08 表示需缴纳成交金额8%的保证金）
            - 2. _step_margin (list[float]): 逐笔交易保证金列表（从 `mpsc` 队列提取，每笔对应一笔开仓的保证金）
            - 3. _margin (float): 总保证金（所有未平仓交易的保证金总和）
            - 4. cost_price (float): 持仓成本价（逐笔开仓成本加权平均，含手续费）
            - 5. tick_value (float): 每 tick 价值（= `volume_multiple`，即1个最小变动单位对应的资金价值）

        四、仓位与交易数据
            - 1. poscreator (PositionCreator): 仓位创建器，生成 `LONG`/`SHORT`/`FLAT` 三种仓位状态
            - 2. position (BtPosition): 当前仓位状态（多头/空头/平仓）
            - 3. mpsc (LifoQueue): 逐笔交易队列（后进先出，存储每笔开仓的 [保证金, 开仓价, 手数, 手续费]）
            - 4. history_queue (Queue): 交易历史队列（存储所有已完成的交易记录）
            - 5. _size (int): 当前总持仓手数（所有未平仓交易的手数总和）
            - 6. _open_price (float): 平均开仓价（逐笔开仓价按手数加权平均）
            - 7. _float_profit (float): 当前浮动盈亏（按最新收盘价计算所有未平仓仓位的盈亏）

        五、盈亏与统计
            - 1. profit (float): 单笔交易盈亏（每次开仓/平仓后更新）
            - 2. cum_profits (float): 累计盈亏（所有交易的盈亏总和）
            - 3. length (int): 关联K线数据的总周期数（从 `kline.length` 获取）


        ### 核心方法说明：
        1. __init__(self, kline: KLine, **kwargs):
            - 初始化 Broker 实例，关联 KLine 与 BtAccount，加载手续费、保证金、滑点等配置
            - 初始化仓位状态（默认平仓 `FLAT`）、交易队列（`mpsc`/`history_queue`）

        2. _setcommission(self, commission: dict):
            - 配置手续费类型与计算函数
            - 校验手续费类型是否在 `commission_keys` 中，无效时默认设为固定手续费（0元）
            - 绑定对应计算函数（如 `tick_commission` 绑定 `_get_comm_tick`）

        3. update(self, size: int, long: bool = True) -> None:
            - 核心交易执行方法，处理开仓、加仓、平仓、反手逻辑：
                - 1. 开仓（无持仓时）：校验可用资金，计算保证金与手续费，更新仓位与账户资金
                - 2. 平仓（持仓方向相反时）：支持部分/全部平仓，计算盈亏，同步账户资金，处理反手（若平仓后仍有剩余手数）
                - 3. 加仓（持仓方向相同时）：校验可用资金，追加保证金与手续费，更新逐笔交易队列
            - 开启日志时（`islog=True`），调用账户 `_optional_msg` 生成中文交易记录

        4. reset(self):
            - 重置 Broker 状态：恢复仓位为平仓（`FLAT`），清空逐笔交易队列（`mpsc`）与交易历史队列（`history_queue`）
            - 用于策略重新运行或多轮回测场景

        5. factor_analyzer(self, num: int):
            - 因子分析专用初始化方法：创建多组仓位记录、历史队列，预计算价格变动对应的资金价值（`diff_value`）
            - 用于多因子策略中多组仓位的并行回测与分析

        6. factor_update(self, index: Optional[int] = None, enter: bool = False, exit: bool = False, long=True):
            - 因子分析场景下的仓位与盈亏更新方法：根据开仓/平仓信号更新指定组的仓位，计算实时盈亏
            - 同步更新历史队列中的盈亏记录


        ### 关键计算逻辑说明：
        1. 手续费计算：
            - 按 tick 价值：`_get_comm_tick` → 手续费 = 合约乘数 × (手续费参数 + 滑点)
            - 固定金额：`_get_comm_fixed` → 手续费 = 固定金额 + 滑点 × 合约乘数
            - 按百分比：`_get_comm_percent` → 手续费 = (成交价格 × 百分比参数 + 滑点) × 合约乘数
        2. 保证金计算：
            - 单笔保证金 = 成交价格 × 保证金率 × 合约乘数 × 手数
            - 总保证金 = 所有未平仓单笔保证金之和
        3. 浮动盈亏计算：
            - 多头仓位：(当前收盘价 - 开仓价) × 手数 × 合约乘数 - 手续费
            - 空头仓位：(开仓价 - 当前收盘价) × 手数 × 合约乘数 - 手续费


        ### 使用示例：
        >>> #1. 关联 KLine 初始化 Broker
        >>> kline = KLine(raw_kline_data)  # 已初始化的 KLine 实例
        >>> #配置固定手续费1.5元/手，保证金率8%，滑点1个tick
        >>> broker = Broker(
        ...     kline,
        ...     commission={"fixed_commission": 1.5},
        ...     margin_rate=0.08,
        ...     slip_point=1,
        ...     islog=True  # 开启交易日志
        ... )
        >>>
        >>> #2. 执行多头开仓（2手）
        >>> broker.update(size=2, long=True)  # 校验资金后开仓，生成"创建多头开仓委托"日志
        >>>
        >>> #3. 执行多头加仓（1手）
        >>> broker.update(size=1, long=True)  # 同方向加仓，生成"多头加仓"日志
        >>>
        >>> #4. 执行多头平仓（3手，全部平仓）
        >>> broker.update(size=3, long=False)  # 反向交易平仓，计算盈亏，生成"多头平仓成交"日志
        >>>
        >>> #5. 查看当前状态
        >>> print(broker.position)  # 输出 FLAT（已平仓）
        >>> print(broker.cum_profits)  # 输出累计盈亏
        >>> print(broker._margin)  # 输出总保证金（平仓后为0）"""
    poscreator = PositionCreator()
    cols = ["total_profit", "positions",
            "sizes", "float_profits", "cum_profits","total_fee"]
    #cols["total_equity","positions","sizes","pnl","cum_profits"]
    commission_keys: list[str] = ["tick_commission",
                                  "fixed_commission", "percent_commission"]
    commission_value: float
    commission: dict[str, float]
    cost_fixed: float
    # 成本的最小波动单位形式
    cost_tick: float
    cost_percent: float
    # 成本的价值形式
    cost_value: float
    # 成本的价格形式
    cost_price: float

    def __init__(self, kline: KLine, **kwargs):
        self.kline = kline
        config: Config = kwargs.pop(
            "config", kline.strategy_instance.config)
        self.account: BtAccount = kline.account
        self.account.add_broker(self)
        self.margin_rate: float = kwargs.pop(
            "margin_rate", config.margin_rate)
        commission: dict = kwargs.pop(
            "commission", config._get_commission())
        self.slip_point: float = kwargs.pop(
            "slip_point", config.slip_point)
        self.islog: bool = config.islog
        self.islogorder: bool = config.islogorder
        symbol_info = kline.symbol_info
        self.symbol = symbol_info.symbol
        self.__name__ = f"{self.symbol}_{symbol_info.cycle}_broker"
        self.strategy_name = kline.strategy_instance.__class__.__name__
        self.price_tick = symbol_info.price_tick
        self.volume_multiple = symbol_info.volume_multiple
        self.tick_value = symbol_info.volume_multiple
        self._setcommission(commission)
        self.reset()

    def factor_analyzer(self, num: int):
        self.positions: list[BtPosition] = [
            self.poscreator.FLAT(self) for _ in range(num)]
        self.last_trade_prices: list[float] = [0. for _ in range(num)]
        self.history_queues: LifoQueue = LifoQueue()
        self.diff_value = self.kline.pandas_object.close.diff().values * \
            self.volume_multiple
        self.diff_value[0] = 0.

    @property
    def btindex(self) -> int:
        return self.kline.btindex

    def reset(self):
        """## 重置"""
        self.profit = 0.
        self.cum_profits = 0.
        self.total_commission = 0.
        self.position: BtPosition = self.poscreator.FLAT(self)
        # 每笔交易的保证金margin，成交价price，手数size和手续费用commission的存放
        self.mpsc = LifoQueue()
        self.history_queue = Queue()
        self.length = self.kline.length

        # 新增订单相关属性
        self._orders: dict[int, Order] = {}  # 订单字典 {ref: Order}
        self._pending_orders: list[Order] = Orders(self)  # 待处理订单列表
        self._active_orders: list[Order] = []  # 活跃订单列表
        self._completed_orders: list[Order] = []  # 已完成订单列表
        self._cancelled_orders: list[Order] = []  # 已取消订单列表
        self._rejected_orders: list[Order] = []  # 已拒绝订单列表
        self._expired_orders: list[Order] = []  # 已过期订单列表
        self._order_ref_counter = itertools.count(1)  # 订单编号生成器

        # 订单执行配置
        self._default_exectype = OrderType.Close
        self._order_expiry_days = 30  # 订单默认有效期
        self._stop = None

        # 价格序列缓存，用于检查价格跳空
        self._current_bar_data = None

    def _setcommission(self, commission: dict):
        """设置手续费用"""
        if not commission:
            commission = {"fixed_commission": 0.}
        for key, value in commission.items():
            break
        if key not in self.commission_keys:
            commission = {"fixed_commission": 0.}
        for k in self.commission_keys:
            com_key = k.split("_")[0]
            if k == key:
                self.commission = commission
                self.commission_value = value
                self.commission_func = getattr(self, f"_get_comm_{com_key}")
                break

    @property
    def _sizes(self) -> list[int]:
        """## 逐笔合约成交手数"""
        return [0,] if self.mpsc.empty() else list(map(lambda x: x[2], self.mpsc.queue))

    @property
    def _size(self) -> int:
        """## 合约成交手数"""
        return sum(self._sizes)

    def commission_func(self, close) -> float:
        ...

    @cache
    def _get_comm_tick(self, close):
        return self.volume_multiple*self.commission_value

    @cache
    def _get_comm_fixed(self, close):
        return self.commission_value

    def _get_comm_percent(self, close):
        return self.commission_value*close*self.volume_multiple

    @property
    def LONG(self) -> BtPosition:
        return self.poscreator.LONG

    @property
    def SHORT(self) -> BtPosition:
        return self.poscreator.SHORT

    @property
    def FLAT(self) -> BtPosition:
        return self.poscreator.FLAT

    @property
    def _diff_price(self) -> Callable:
        return pos if self.position > 0 else neg

    @property
    def _open_price(self) -> float:
        return 0. if self.mpsc.empty() else sum(list(map(lambda x: x[1]*x[2], self.mpsc.queue)))/sum(list(map(lambda x: x[2], self.mpsc.queue)))

    @property
    def _cost_price(self) -> float:
        return 0. if self.mpsc.empty() else sum(list(map(lambda x: x[1]*x[2]+(x[3] if self.position.pos>0 else -x[3]), self.mpsc.queue)))/sum(list(map(lambda x: x[2], self.mpsc.queue)))

    @property
    def _float_profit(self) -> float:
        return sum(list(map(lambda x: (self._diff_price(self.current_close-x[1]))*x[2]*self.volume_multiple, self.mpsc.queue)))

    def _getmargin(self, price) -> float:
        """## 获取保证金"""
        return price*self.margin_rate*self.volume_multiple

    @property
    def _step_margin(self) -> list[float]:
        """## 合约逐笔保证金"""
        return [0,] if self.mpsc.empty() else list(map(lambda x: x[0], self.mpsc.queue))

    @property
    def _margin(self) -> float:
        """## 合约保证金"""
        return sum(self._step_margin)

    @property
    def _comm(self) -> float:
        return 0. if self.mpsc.empty() else sum(list(map(lambda x: x[3], self.mpsc.queue)))

    @property
    def current_open(self) -> float:
        return self.kline.current_open

    @property
    def current_close(self) -> float:
        return self.kline.current_close

    @property
    def current_datetime(self) -> str:
        return self.kline.current_datetime

    @property
    def current_time(self) -> str:
        return self.kline.current_time

    @property
    def current_diff_value(self) -> float:
        return self.diff_value[self.btindex]

    def factor_update(self, index: int | None = None, enter: bool = False, exit: bool = False, long=True):
        close = self.current_close
        comm = 0.
        if index is not None:
            if enter or exit:
                comm = -self.commission_func(close)
            if enter:
                self.positions[index] = long and 1 or -1
            elif exit:
                self.positions[index] = 0
        history = []
        diff = 0.
        queue = self.history_queues.queue[-1]
        for i, (pos, value) in enumerate(zip(self.positions, queue)):
            if pos > 0:
                diff = self.current_diff_value
            elif pos < 0:
                diff = -self.current_diff_value
            value += diff
            if index == i:
                value += comm
            history.append(value)
        self.history_queues.put(history)

    def update(self, size: int, long: bool, exec_price: float = None) -> None:
        """## 更新账户交易
        - exec_price: 执行价格，如果为None，则使用当前收盘价作为执行价格
        """
        if self.btindex < self.length:
            # 如果未提供执行价格，则使用当前收盘价
            if exec_price is None:
                exec_price = self.kline.current_close
            current_position = self.position
            price = exec_price
            datetime = self.current_datetime  # 总是定义 datetime 变量

            (pos_stats1, pos_stats2) = (self.LONG, self.SHORT) if long else (
                self.SHORT, self.LONG)

            available = self.account._available

            # 开仓
            if not current_position:
                self._set_stop()
                exec_price = self._handle_slip_point(price, long)
                margin = size * self._getmargin(exec_price)
                comm = size * self.commission_func(exec_price)
                if available < margin + comm:
                    return self.Logger().log_insufficient_cash(datetime)
                self.position = pos_stats1(self)
                self.profit = -comm
                # 逐笔记录
                self.mpsc.put([margin, exec_price, size, comm])
                self.account._available -= margin + comm
                self.account._total_commission += comm
                self.total_commission += comm
                self.account._total_profit -= comm
                if self.islog:
                    args = (self.strategy_name, self.symbol, datetime,
                            exec_price, size, comm, self.account.balance)
                    self.Logger().operation_msg('开多' if long else '开空', None, *args)
            # 平仓
            elif current_position == pos_stats2:
                exec_price = self._handle_slip_point(price, long)
                # 逐笔平仓
                value, comm, margin, total_close_size = 0., 0., 0., 0
                for _ in range(self.mpsc.qsize()):
                    if size > 0:
                        m, p, s, _ = self.mpsc.get()  # s:1
                        # 例:本次平仓手数,可能减仓 position long(s) 3, size 2, close_size 2
                        close_size = min(size, s)  # 2
                        # 累计平仓手数
                        total_close_size += close_size  # 2
                        # 剩余手数 3-2 = 1
                        size -= close_size  # 1
                        diff_price = exec_price - p if not long else p - exec_price
                        value += close_size * diff_price * \
                            self.volume_multiple
                        comm += close_size * self.commission_func(exec_price)
                        if close_size == s:  # 本次全部平仓
                            margin += m
                        elif s > close_size:  # 部分平仓
                            out_margin = m * close_size / s
                            margin += out_margin
                            self.mpsc.put(
                                [m - out_margin, p, s - close_size, comm])
                            break
                # size>0:反手 size<0:减仓 size=0:清仓
                if size == 0:
                    self.position = self.FLAT(self)
                value -= comm
                self.account._available += value + margin
                self.profit = value
                self.account._total_commission += comm
                self.total_commission += comm
                self.account._total_profit += value
                if self.islog:
                    args = (self.strategy_name, self.symbol, datetime, exec_price, total_close_size,
                            comm, self.account.balance)
                    if size == 0:
                        self.Logger().operation_msg('平空' if long else '平多', value, *args)
                    elif size < 0:
                        self.Logger().operation_msg('减空' if long else '减多', value, *args)
                # 反手
                if size > 0:
                    exec_price = self._handle_slip_point(price, long)
                    self._set_stop()
                    margin = size * self._getmargin(exec_price)
                    comm = size * self.commission_func(exec_price)
                    if self.account._available < margin + comm:
                        return self.Logger().log_insufficient_cash(datetime)
                    else:
                        self.profit = value - comm
                        self.position = pos_stats1(self)
                        self.mpsc.put([margin, exec_price, size, comm])
                        self.account._available -= margin + comm
                        value -= comm
                        self.account._total_commission += comm
                        self.total_commission += comm
                        self.account._total_profit += value
                        if self.islog:
                            args = (self.strategy_name, self.symbol, datetime, exec_price, size,
                                    comm, self.account.balance)
                            self.Logger().operation_msg('开多' if long else '开空', None, *args)
            # 加仓
            else:
                # 加仓
                exec_price = self._handle_slip_point(price, long)
                self._set_stop()
                margin = size * self._getmargin(exec_price)
                comm = size * self.commission_func(exec_price)
                if available < margin + comm:
                    return self.Logger().log_insufficient_cash(datetime)
                self.profit = -comm
                self.mpsc.put([margin, exec_price, size, comm])
                self.account._available -= margin + comm
                self.account._total_commission += comm
                self.total_commission += comm
                self.account._total_profit -= comm
                if self.islog:
                    args = (self.strategy_name, self.symbol, datetime, exec_price, size, comm,
                            self.account.balance)
                    self.Logger().operation_msg('加多' if long else '加空', None, *args)
            if self.profit:
                self.cum_profits += self.profit
                
    # 处理滑点后的执行价格
    def _handle_slip_point(self, exec_price: float,long:bool) -> float:
        """## 处理滑点后的执行价格"""
        if self.slip_point > 0:
            if long:
                # 多头滑点
                exec_price += self.slip_point
            else:
                # 空头滑点
                exec_price -= self.slip_point
        return exec_price

    def _set_stop(self):
        if self._stop and not self.kline._klinesetting.isstop:
            self.kline._set_stop(self._stop)
            self._stop = None

    @classmethod
    def is_immediate(self, exectype) -> bool:
        """## 是否为即时订单（Market或Close）"""
        return OrderType.is_immediate(exectype)

    @classmethod
    def is_conditional(self, exectype) -> bool:
        """## 是否为条件订单（Limit, Stop, StopLimit）"""
        return OrderType.is_conditional(exectype)

    @classmethod
    def requires_price(self, exectype) -> bool:
        """## 该订单是否需要价格参数"""
        return OrderType.requires_price(exectype)

    @classmethod
    def requires_bar_specification(self, exectype) -> bool:
        """## 该订单是否需要bar参数"""
        return OrderType.requires_bar_specification(exectype)

    def create_order(self, side: OrderSide, size: int,
                     exectype: OrderType | None = None,
                     price: float | None = None,
                     valid: datetime.datetime | datetime.timedelta | int | None = None,
                     oco: Order | None = None, **kwargs) -> Order:
        """## 创建订单"""
        if exectype is None:
            exectype = self._default_exectype
        if exectype == OrderType.Market:
            bar = 1
        elif exectype == OrderType.Close:
            bar = 0
        # 即时订单中，如果已有订单则不再创建订单
        if self.is_immediate(exectype) and self._pending_orders:
            return
        # 手数校验
        size = int(size)
        if size <= 0:
            raise ValueError(f"手数必须为正整数，当前为: {size}")

        # 生成订单编号
        ref = next(self._order_ref_counter)

        # 获取当前K线时间（回测模式使用K线时间）
        create_time = self.kline.current_time if hasattr(
            self.kline, 'current_time') else datetime.datetime.now()

        # 创建订单
        order = Order(
            create_time=create_time,
            side=side,
            size=size,
            exectype=exectype,
            price=price,
            pricelimit=price if exectype == OrderType.StopLimit else None,
            valid=valid,
            oco=oco,
            ref=ref,
            bar=bar
        )

        # 设置止损参数
        self._stop = kwargs.pop("stop", None)

        # 设置初始状态
        order.update_status(OrderStatus.Submitted)

        # 存储订单
        self._orders[ref] = order
        
        # 如果是OCO订单，处理关联关系
        if oco:
            if 'oco_linked' not in oco.info:
                oco.info['oco_linked'] = []
            oco.info['oco_linked'].append(order.ref)

            if 'oco_linked' not in order.info:
                order.info['oco_linked'] = []
            order.info['oco_linked'].append(oco.ref)
            
        if self.islogorder:
            self.log_order_created(order)

        # 当 exectype == OrderType.Close 时，即时执行交易
        if exectype == OrderType.Close:
            # 获取执行价格
            execute_price = self._get_execute_price(order)
            # 执行交易
            self._execute_trade(order.side, order.size, execute_price)
            # 更新订单状态为已完成
            order.execute(
                price=execute_price,
                size=order.size,
                datetime=self.current_datetime,
                value=execute_price * order.size * self.volume_multiple,
                commission=self.commission_func(execute_price) * order.size
            )
            order.update_status(OrderStatus.Completed)
            # 添加到已完成订单列表
            self._completed_orders.append(order)
            # 处理OCO订单
            if 'oco_linked' in order.info:
                for linked_ref in order.info['oco_linked']:
                    if linked_ref in self._orders:
                        self.cancel_order(self._orders[linked_ref])
            if self.islogorder:
                self.log_order_executed(order)
        else:
            # 非 Close 订单，加入待处理队列
            self._pending_orders.append(order)
        return order

    def _get_execute_price(self, order: Order) -> float:
        """## 获取订单执行价格，考虑订单类型和价格跳空"""

        if order.exectype == OrderType.Market:
            # 市价单使用开盘价
            return self.kline.current_open

        elif order.exectype == OrderType.Close:
            # 收盘价单使用收盘价
            return self.kline.current_close

        elif order.exectype == OrderType.Limit:
            if order.is_buy:
                # 买入限价单：执行价格为限价与开盘价的较小值（如果开盘跳空低于限价）
                return min(order.price, self.kline.current_open)
            else:
                # 卖出限价单：执行价格为限价与开盘价的较大值（如果开盘跳空高于限价）
                return max(order.price, self.kline.current_open)

        elif order.exectype == OrderType.Stop:
            if order.is_buy:
                # 买入止损单：执行价格为止损价与开盘价的较大值
                return max(order.price, self.kline.current_open)
            else:
                # 卖出止损单：执行价格为止损价与开盘价的较小值
                return min(order.price, self.kline.current_open)

        elif order.exectype == OrderType.StopLimit:
            # 止损限价单：使用限价执行
            return order.pricelimit

        return self.kline.current_close

    def buy(self, size: int, **kwargs) -> Order:
        """## 创建买入订单"""
        return self.create_order(OrderSide.Buy, size, **kwargs)

    def sell(self, size: int, **kwargs) -> Order:
        """## 创建卖出订单"""
        return self.create_order(OrderSide.Sell, size, **kwargs)

    def log_order_created(self, order: Order):
        """## 记录订单创建日志"""
        order_type = OrderType.get_name(order.exectype)
        side = OrderSide.get_name(order.side)

        price_info = ""
        if order.exectype in [OrderType.Limit, OrderType.Stop]:
            price_info = f" 价格: {order.price}"
        elif order.exectype == OrderType.StopLimit:
            price_info = f" 触发: {order.price} 限价: {order.pricelimit}"

        valid_info = ""
        if order.valid:
            if isinstance(order.valid, int):
                valid_info = f" 有效期: {order.valid}周期"
            elif isinstance(order.valid, datetime.timedelta):
                valid_info = f" 有效期: {order.valid}"
            elif isinstance(order.valid, datetime.datetime):
                valid_info = f" 有效期至: {order.valid}"

        msg = (f"订单创建 [{order.ref}]: {side} {order.size}手 {order_type}"
               f"{price_info}{valid_info}")
        self.account.Logger().info(msg)

    def cancel_order(self, order: Order):
        """## 取消订单"""
        if order.is_active:
            order.cancel()
            self._active_orders.remove(order)

            # 如果是OCO订单，取消关联订单
            if 'oco_linked' in order.info:
                linked_ref = order.info['oco_linked']
                if linked_ref in self._orders:
                    self.cancel_order(self._orders[linked_ref])

    def process_orders(self):
        """## 处理所有待处理的订单"""

        # 处理待处理订单
        for order in list(self._pending_orders):
            # 检查订单是否过期
            if self._is_order_expired(order):
                order.update_status(OrderStatus.Expired)
                self._pending_orders.remove(order)
                self._expired_orders.append(order)

                if self.islogorder:
                    self.log_order_status_change(order, "过期")
                continue

            # 检查订单是否可以执行
            if self._can_order_execute(order):
                # 执行订单
                self._execute_order(order)

                # 从待处理列表移除
                self._pending_orders.remove(order)

                # 根据状态添加到相应列表
                if order.is_completed:
                    self._completed_orders.append(order)
                elif order.status == OrderStatus.Partial:
                    self._active_orders.append(order)
                elif order.status == OrderStatus.Rejected:
                    self._rejected_orders.append(order)

            # 更新订单的bar计数和有效期
            if order.bar > 0:
                order.bar -= 1

            if order._isnumvalid and order.valid >= 0:
                order.valid -= 1

    def _is_order_expired(self, order: Order) -> bool:
        """## 检查订单是否过期"""
        if order.valid is None:
            return False

        if isinstance(order.valid, int):
            # 相对时间（天数）
            return order.valid < 0
        else:
            current_time = self.current_time
            if isinstance(order.valid, datetime.datetime):
                return current_time > order.valid
            elif isinstance(order.valid, datetime.timedelta):
                # 这里需要订单创建时间，简化处理
                return current_time > order.create_time+order.valid

        return False

    def _can_order_execute(self, order: Order) -> bool:
        """## 检查订单是否可以执行"""
        if order.exectype == OrderType.Market:
            return order.bar == 0

        elif order.exectype == OrderType.Close:
            return order.bar == 0

        elif order.exectype == OrderType.Limit:
            if order.is_buy:
                # 买入限价单：当前最低价 <= 限价
                return self.kline.current_low <= order.price
            else:
                # 卖出限价单：当前最高价 >= 限价
                return self.kline.current_high >= order.price

        elif order.exectype == OrderType.Stop:
            if order.is_buy:
                # 买入止损单：当前最高价 >= 止损价
                return self.kline.current_high >= order.price
            else:
                # 卖出止损单：当前最低价 <= 止损价
                return self.kline.current_low <= order.price

        elif order.exectype == OrderType.StopLimit:
            # 首先检查是否触发止损
            if 'triggered' not in order.info:
                if order.is_buy:
                    triggered = self.kline.current_high >= order.price
                else:
                    triggered = self.kline.current_low <= order.price

                if triggered:
                    order.info['triggered'] = True
                    # 触发后转为限价单
                    order.exectype = OrderType.Limit
                    order.price = order.pricelimit

            # 如果已触发，按限价单逻辑检查
            if 'triggered' in order.info:
                if order.is_buy:
                    return self.kline.current_low <= order.price
                else:
                    return self.kline.current_high >= order.price

            return False

        return False

    def _execute_order(self, order: Order):
        """## 执行订单，支持部分成交"""
        # 确定执行价格
        execute_price = self._get_execute_price(order)

        # 检查是否可以执行全部手数
        available_size = order.size
        executed_size = 0

        # 模拟部分成交：这里可以添加更复杂的部分成交逻辑
        # 例如：根据市场深度、成交量等因素决定成交数量
        # 简化处理：全部成交
        executed_size = available_size

        # 执行交易
        self._execute_trade(order.side, executed_size, execute_price)

        # 更新订单状态
        if executed_size == order.size:
            # 全部成交
            order.execute(
                price=execute_price,
                size=executed_size,
                datetime=self.current_datetime,
                value=execute_price * executed_size * self.volume_multiple,
                commission=self.commission_func(execute_price) * executed_size
            )
            order.update_status(OrderStatus.Completed)

            # 处理OCO订单
            if 'oco_linked' in order.info:
                for linked_ref in order.info['oco_linked']:
                    if linked_ref in self._orders:
                        self.cancel_order(self._orders[linked_ref])

        elif executed_size > 0:
            # 部分成交
            order.execute(
                price=execute_price,
                size=executed_size,
                datetime=self.current_datetime,
                value=execute_price * executed_size * self.volume_multiple,
                commission=self.commission_func(execute_price) * executed_size
            )
            order.update_status(OrderStatus.Partial)

            # 创建剩余手数的新订单
            remaining_size = order.size - executed_size
            if remaining_size > 0:
                # 创建新的订单用于剩余手数
                new_order = self.create_order(
                    side=order.side,
                    size=remaining_size,
                    exectype=order.exectype,
                    price=order.price,
                    valid=order.valid,
                    bar=0,  # 剩余部分立即执行
                    oco=None  # 不继承OCO关系
                )
                # 复制重要信息
                new_order.info['parent_order'] = order.ref
                order.info['child_order'] = new_order.ref
        else:
            # 没有成交
            pass

        if self.islogorder:
            self.log_order_executed(order)

    def _execute_trade(self, side: OrderSide, size: int, price: float):
        """## 执行交易（调用原有的update逻辑）"""
        long = side == OrderSide.Buy
        self.update(size, long, price)

    def cancel_order(self, order: Order, reason: str = "用户取消"):
        """## 取消订单"""
        if order.is_active:
            order.cancel()

            # 从相应列表中移除
            if order in self._pending_orders:
                self._pending_orders.remove(order)
            if order in self._active_orders:
                self._active_orders.remove(order)

            # 添加到取消列表
            self._cancelled_orders.append(order)

            # 记录取消原因
            order.info['cancel_reason'] = reason

            # 处理OCO订单
            if 'oco_linked' in order.info:
                for linked_ref in order.info['oco_linked']:
                    if linked_ref in self._orders:
                        linked_order = self._orders[linked_ref]
                        if linked_order.is_active:
                            self.cancel_order(linked_order, "关联订单取消")

            if self.islogorder:
                self.log_order_status_change(order, f"取消: {reason}")

            return True
        return False

    def reject_order(self, order: Order, reason: str = "资金不足"):
        """## 拒绝订单"""
        if order.is_active:
            order.reject()

            # 从相应列表中移除
            if order in self._pending_orders:
                self._pending_orders.remove(order)
            if order in self._active_orders:
                self._active_orders.remove(order)

            # 添加到拒绝列表
            self._rejected_orders.append(order)

            # 记录拒绝原因
            order.info['reject_reason'] = reason

            # 处理OCO订单
            if 'oco_linked' in order.info:
                for linked_ref in order.info['oco_linked']:
                    if linked_ref in self._orders:
                        linked_order = self._orders[linked_ref]
                        if linked_order.is_active:
                            self.reject_order(linked_order, "关联订单拒绝")

            if self.islogorder:
                self.log_order_status_change(order, f"拒绝: {reason}")

            return True
        return False

    def log_order_executed(self, order: Order):
        """## 记录订单执行日志"""
        status = OrderStatus.get_name(order.status)
        side = OrderSide.get_name(order.side)

        if order.is_completed:
            msg = (f"订单完成 [{order.ref}]: {side} {order.size}手 "
                   f"成交价: {order.executed_price:.2f} "
                   f"手续费: {order.executed_commission:.2f}")
        elif order.status == OrderStatus.Partial:
            msg = (f"订单部分成交 [{order.ref}]: {side} {order.executed_size}/{order.size}手 "
                   f"成交价: {order.executed_price:.2f}")
        else:
            msg = f"订单状态更新 [{order.ref}]: {status}"

        self.account.Logger().info(msg)

    def get_orders(self, status: OrderStatus | None = None) -> list[Order]:
        """## 获取订单列表"""
        if status is None:
            return list(self._orders.values())
        return [order for order in self._orders.values() if order.status == status]

    def get_order(self, ref: int) -> Order | None:
        """## 获取指定订单"""
        return self._orders.get(ref)

    def log_order_status_change(self, order: Order, status_change: str):
        """## 记录订单状态变更日志"""
        side = OrderSide.get_name(order.side)
        msg = f"订单状态变更 [{order.ref}]: {side} {order.size}手 {status_change}"
        self.account.Logger().info(msg)

    def get_orders_by_status(self, status: OrderStatus) -> list[Order]:
        """## 按状态获取订单列表"""
        return [order for order in self._orders.values() if order.status == status]

    def get_orders_by_side(self, side: OrderSide) -> list[Order]:
        """## 按方向获取订单列表"""
        return [order for order in self._orders.values() if order.side == side]

    def get_orders_by_type(self, exectype: OrderType) -> list[Order]:
        """## 按类型获取订单列表"""
        return [order for order in self._orders.values() if order.exectype == exectype]

    def get_active_orders(self) -> list[Order]:
        """## 获取活跃订单列表"""
        return self._active_orders.copy()

    def get_pending_orders(self) -> list[Order]:
        """## 获取待处理订单列表"""
        return self._pending_orders.copy()

    def get_completed_orders(self) -> list[Order]:
        """## 获取已完成订单列表"""
        return self._completed_orders.copy()

    def get_order_statistics(self) -> Dict | dict:
        """## 获取订单统计信息"""
        total_orders = len(self._orders)
        active_orders = len(self._active_orders)
        pending_orders = len(self._pending_orders)
        completed_orders = len(self._completed_orders)
        cancelled_orders = len(self._cancelled_orders)
        rejected_orders = len(self._rejected_orders)
        expired_orders = len(self._expired_orders)

        # 计算成交统计
        total_executed_value = 0.0
        total_commission = 0.0
        buy_orders = 0
        sell_orders = 0

        for order in self._completed_orders + self._cancelled_orders:
            if order.executed_size > 0:
                total_executed_value += order.executed_value
                total_commission += order.executed_commission

            if order.side == OrderSide.Buy:
                buy_orders += 1
            else:
                sell_orders += 1

        return {
            'total_orders': total_orders,
            'active_orders': active_orders,
            'pending_orders': pending_orders,
            'completed_orders': completed_orders,
            'cancelled_orders': cancelled_orders,
            'rejected_orders': rejected_orders,
            'expired_orders': expired_orders,
            'buy_orders': buy_orders,
            'sell_orders': sell_orders,
            'total_executed_value': total_executed_value,
            'total_commission': total_commission
        }

    def clear_expired_orders(self):
        """## 清理过期订单，释放内存"""
        # 找出所有过期订单的ref
        expired_refs = [order.ref for order in self._expired_orders]

        # 从主订单字典中移除
        for ref in expired_refs:
            if ref in self._orders:
                del self._orders[ref]

        # 清空过期订单列表
        self._expired_orders.clear()

        if self.islogorder:
            self.account.Logger().info(f"已清理 {len(expired_refs)} 个过期订单")

    def reset_orders(self):
        """## 重置订单系统"""
        self._orders.clear()
        self._pending_orders.clear()
        self._active_orders.clear()
        self._completed_orders.clear()
        self._cancelled_orders.clear()
        self._rejected_orders.clear()
        self._expired_orders.clear()
        self._order_ref_counter = itertools.count(1)

        if self.islogorder:
            self.account.Logger().info("订单系统已重置")


@dataclass(eq=False)
class BtAccount(Base):
    """## 框架内置账户管理类（继承 Base 类，基于 dataclass 实现）
    - 核心定位：统一管理量化交易中的资金、持仓、盈亏、手续费等账户核心数据，关联多个交易代理（Broker），是回测与模拟交易的资金中枢

    ### 核心职责：
    - 1. 资金统筹管理：实时维护账户权益、可用现金、总保证金，动态计算盈利率、风险度等关键财务指标
    - 2. 交易代理联动：支持添加、替换多个 Broker（对应不同合约/策略），同步所有 Broker 的保证金、盈亏数据
    - 3. 历史记录追踪：初始化并更新账户历史数据（权益、仓位、盈亏等），支持事后回测结果分析与导出
    - 4. 交易风险控制：校验交易资金充足性，触发交易失败提醒，避免账户透支
    - 5. 日志与信息输出：根据交易结果生成中文日志（盈利/亏损/失败），支持账户状态打印，便于调试与复盘


    ### 核心特性：
    1. 多代理兼容：
        - 支持关联多个 Broker（通过 `add_broker` 方法），可同时管理多合约/多策略的交易，自动汇总所有 Broker 的保证金、盈亏
        - 支持 Broker 替换（指定 index 参数），适配策略调整或合约切换场景
    2. 资金动态核算：
        - 实时计算账户权益（`balance` = 可用现金 + 总保证金），反映账户实时净值
        - 自动汇总所有 Broker 的总保证金（`_margin`）、总盈亏（`total_profit`）、总手续费（`total_commission`）
    3. 风险指标实时计算：
        - 盈利率（`net`）：反映账户整体收益水平（(当前权益 - 初始现金) / 初始现金）
        - 风险度（`risk_ratio`）：衡量账户风险暴露（总保证金 / 账户权益），用于风险控制
    4. 历史数据完整追踪：
        - 初始化历史记录（`_init_history`），按周期存储账户关键数据（权益、仓位、盈亏等）
        - 支持导出历史结果（`get_history_results`）为 DataFrame，便于回测报告生成与可视化分析
    5. 交易日志分级：
        - 根据交易结果（盈利/亏损/失败）生成不同级别日志（info/error/warning），日志内容为中文，清晰易懂
        - 支持账户状态打印（`print` 属性），格式化输出当前资金、持仓、手续费等核心信息


    ### 初始化参数说明（dataclass 字段）：
    Args:
        cash (float): 账户初始现金（必填），作为账户的初始资金池，将自动赋值给 `_available`（可用现金）和 `_balance`（初始权益）
        islog (bool): 是否开启账户日志（默认 False，开启后将打印交易结果、失败提醒等信息）
        train (bool): 是否为强化学习训练模式（默认 False，训练模式下可能跳过部分日志与打印逻辑，优化性能）


    ### 核心属性说明（按功能分类）：
    一、基础资金数据
    - 1. cash (float): 账户初始现金（dataclass 初始化字段，不可修改）
    - 2. _available (float): 可用现金（初始等于 cash，随交易扣减/增加，用于开仓/加仓时的资金校验）
    - 3. _balance (float): 账户初始权益（固定为初始 cash，用于计算盈利率）
    - 4. balance (float, property): 当前账户权益（动态计算 = 可用现金 + 总保证金），反映账户实时净值
    - 5. available (float, property): 当前可用现金（对外暴露的只读属性，避免直接修改）

    二、盈亏与手续费统计
    - 1. _total_profit (float): 账户总盈亏（汇总所有 Broker 的盈亏，初始为 0）
    - 2. total_profit (float, property): 账户总盈亏（对外暴露的只读属性）
    - 3. _total_commission (float): 账户总手续费（汇总所有 Broker 的手续费，初始为 0）
    - 4. total_commission (float, property): 账户总手续费（对外暴露的只读属性）
    - 5. profit (float, property): 当前周期盈亏（汇总所有 Broker 当期的单笔盈亏，周期结束后重置）
    - 6. net (float, property): 账户盈利率（动态计算 = (当前权益 - 初始权益) / 初始权益，正数为盈利，负数为亏损）

    三、风险与保证金
    - 1. _margin (float, property): 账户总保证金（汇总所有 Broker 的未平仓仓位保证金，动态更新）
    - 2. risk_ratio (float, property): 账户风险度（动态计算 = 总保证金 / 当前权益，值越大风险越高）

    四、交易代理与历史
    - 1. brokers (list[Broker]): 关联的交易代理列表（初始为空，通过 `add_broker` 方法添加）
    - 2. num (int, property): 关联的 Broker 数量（动态计算 = 列表长度）
    - 3. history (list[pd.DataFrame]): 账户历史数据列表（每个元素对应一个 Broker 的历史记录，初始为 None，通过 `_init_history` 初始化）

    五、日志与提示
    - 1. islog (bool): 日志开关（dataclass 字段，控制是否打印交易日志）
    - 2. _fail (str): 交易失败提示文案（默认 "账户现金不足,交易失败!"，用于资金不足时的提醒）
    - 3. print (property): 账户状态打印属性（调用时格式化输出当前权益、现金、手续费等核心信息）


    ### 核心方法说明：
    1. __post_init__(self):
    - dataclass 后置初始化方法，自动初始化账户核心数据：
    - 可用现金（`_available`）、初始权益（`_balance`）设为初始现金（`cash`）
    - 总盈亏（`_total_profit`）、总手续费（`_total_commission`）初始化为 0
    - 初始化 Broker 列表（`brokers`）、历史记录（`history`）为空

    2. add_broker(self, broker: Broker, index=None):
    - 添加或替换交易代理（Broker）：
    - 未指定 index：将 Broker 追加到 `brokers` 列表（支持多 Broker 管理）
    - 指定 index：替换列表中对应索引的 Broker（用于策略调整或合约切换）

    3. reset(self, length):
    - 重置账户状态（用于策略重新运行或多轮回测）：
    - 恢复可用现金（`_available`）、初始权益（`_balance`）为初始现金（`cash`）
    - 重置总盈亏（`_total_profit`）、总手续费（`_total_commission`）为 0
    - 调用所有关联 Broker 的 `reset` 方法，重置其仓位与交易队列
    - 初始化历史记录（调用 `_init_history`，长度为 `length`）

    4. _init_history(self, length: int):
    - 初始化账户历史记录（按周期预填充空数据）：
    - 普通模式：为每个周期预存空的账户历史（权益、仓位、盈亏等）
    - 因子分析模式（`_is_factor_analyzer=True`）：为每个 Broker 预存多组因子分析所需的空值
    - 作用：确保策略从非0索引开始时，历史数据长度与策略周期匹配

    5. update_history(self):
    - 按周期更新账户历史记录：
    - 为每个 Broker 记录当期的账户权益、仓位状态、单笔盈亏、累计盈亏
    - 重置所有 Broker 的当期单笔盈亏（`broker.profit`），避免跨周期重复统计

    6. get_history_results(self) -> list[pd.DataFrame]:
    - 导出账户历史记录为 DataFrame 列表：
    - 每个元素对应一个 Broker 的历史数据，列名由 Broker 的 `cols` 定义（如 total_profit/positions/sizes 等）
    - 历史数据未初始化时自动创建，支持事后回测结果分析与可视化

    7. get_profits(self) -> Series:
    - 提取第一个 Broker 的历史总盈亏序列（`total_profit` 列），用于快速获取核心回测结果（如收益曲线绘制）


    ### 使用示例：
    >>> # 1. 初始化账户（初始现金100000元，开启日志）
    >>> account = BtAccount(cash=100000.0, islog=True)
    >>>
    >>> # 2. 关联 Broker（假设已初始化 kline1、kline2 两个 K线数据实例）
    >>> broker1 = Broker(kline1, commission={"fixed_commission": 1.5})
    >>> broker2 = Broker(kline2, commission={"percent_commission": 0.0001})
    >>> account.add_broker(broker1)  # 添加第一个 Broker
    >>> account.add_broker(broker2)  # 添加第二个 Broker
    >>> print(account.num)  # 输出 Broker 数量：2
    >>>
    >>> # 3. 执行交易（通过 Broker 间接触发账户资金更新）
    >>> broker1.update(size=2, long=True)  # 多头开仓2手，账户可用现金扣减保证金+手续费
    >>> broker2.update(size=1, long=False)  # 空头开仓1手，账户可用现金扣减保证金+手续费
    >>>
    >>> # 4. 查看账户当前状态
    >>> account.print  # 格式化输出：账户权益、现金、手续费、总盈亏、持仓保证金等
    >>> print(f"当前权益：{account.balance:.2f}")  # 输出当前账户权益
    >>> print(f"总盈亏：{account.total_profit:.2f}")  # 输出账户总盈亏
    >>> print(f"风险度：{account.risk_ratio:.4f}")  # 输出账户风险度
    >>>
    >>> # 5. 重置账户（用于重新回测，假设策略周期长度为 200）
    >>> account.reset(length=200)
    >>> print(f"重置后可用现金：{account.available:.2f}")  # 输出 100000.0（恢复初始现金）
    >>>
    >>> # 6. 导出历史记录（回测结束后）
    >>> history_dfs = account.get_history_results()
    >>> print(history_dfs[0].head())  # 查看第一个 Broker 的前5条历史数据"""
    strategy: Strategy
    cash: float
    # 是否打印
    islog: bool = False

    def __post_init__(self):
        self._available = self.cash
        self._balance = self.cash
        self._total_profit = 0.
        self._total_commission = 0.
        self.history = None
        self.account_info: list[str] = []
        self.brokers: list[Broker] = []
        self._fail: str = '账户现金不足,交易失败!'
        self._is_factor_analyzer: bool = False
        self._isreplay = self.strategy._isreplay

    def add_broker(self, broker: Broker = None):
        if broker is None:
            # 替换broker，即合约相同时同一个broker，例如一个合约多周期数据共用一个broker
            # self.brokers[index] = broker
            # self.brokers.append(self.brokers[index])
            return
        self.brokers.append(broker)

    def reset(self, length):
        """## broker重置"""
        self._available = self.cash
        self._balance = self.cash
        self._total_profit = 0.
        self._total_commission = 0.
        self.history = None
        self.account_info: list[str] = []
        assert self.brokers, "无法重置"
        [broker.reset() for broker in self.brokers]
        self._init_history(length)

    @property
    def num(self) -> int:
        """## broker数量"""
        return len(self.brokers)

    @property
    def balance(self) -> float:
        """## 权益"""
        return self._available+self.margin

    @property
    def available(self) -> float:
        """## 现金"""
        return self._available

    @property
    def total_profit(self) -> float:
        """## 总盈亏"""
        return self._total_profit

    @property
    def close_profit(self) -> float:
        return self._total_profit

    @property
    def total_commission(self) -> float:
        """## 总手续费用"""
        return self._total_commission

    @property
    def commission(self) -> float:
        """## 总手续费用"""
        return self._total_commission

    @property
    def margin(self) -> float:
        """## 总保证金"""
        return sum([broker._margin for broker in self.brokers])

    @property
    def profit(self) -> float:
        """## 当前盈亏"""
        return sum([broker._float_profit for broker in self.brokers])

    @property
    def position_profit(self) -> float:
        return self.profit

    @property
    def float_profit(self) -> float:
        return self.profit

    def update_history(self):
        """## 更新账户历史（现在包含订单处理）"""
        # 1. 处理所有broker的订单
        for broker in self.brokers:
            broker.process_orders()

        # 2. 更新历史记录（原有逻辑）
        for broker in self.brokers:
            broker.history_queue.put([
                self.balance,
                broker.position.value,
                broker.position.pos,
                broker.profit,
                broker.cum_profits,
                broker.total_commission
            ])
            broker.profit = 0.

        # 3. 清理过期订单
        self._cleanup_expired_orders()
        if self._isreplay:
            self.account_info.append(self.strategy._get_account_info())

    def _cleanup_expired_orders(self):
        """## 清理过期订单"""
        for broker in self.brokers:
            # 获取过期订单
            expired_orders = [
                order for order in broker.get_orders()
                if order.status == OrderStatus.Expired
            ]

            # 执行清理操作
            for order in expired_orders:
                # 从活跃列表中移除
                if order in broker._active_orders:
                    broker._active_orders.remove(order)

                if order in broker._pending_orders:
                    broker._pending_orders.remove(order)

                # 记录日志
                if broker.islog:
                    self.Logger().log("INFO",
                        f"订单过期: {OrderSide.get_name(order.side)} {order.size}手")

    def _init_history(self, length: int):
        """## 策略索引从非0开始时初始化历史信息"""
        if length > 0:
            if self._is_factor_analyzer:
                for _ in range(length):
                    for broker in self.brokers:
                        broker.history_queues.put([0.,]*len(broker.positions))
                return
            for _ in range(length):
                self.update_history()

    def get_history_results(self) -> list[pd.DataFrame]:
        if not self.history:
            self.history = self._get_history_results()
        return self.history

    def _get_history_results(self) -> list[pd.DataFrame]:
        if self._is_factor_analyzer:
            return [pd.DataFrame(
                broker.history_queues.queue, columns=[f"values{i}" for i in len(broker.positions)]) for broker in self.brokers]
        else:
            return [pd.DataFrame(
                broker.history_queue.queue, columns=broker.cols) for broker in self.brokers]

    def _get_history_result(self, i, j) -> np.ndarray:
        """### cols="total_profit", "positions","sizes", "float_profits", "cum_profits" """
        return self._get_history_results()[i].iloc[j].values

    def get_profits(self) -> pd.Series:
        results = self._get_history_results()
        if not results:
            # 防护：brokers 为空时返回初始资金（避免 IndexError）
            # 常见原因：KLine 未通过 __setattr__ 注册到 _btklinedataset，
            # 或策略初始化阶段 _account 尚未创建 brokers
            import warnings
            warnings.warn(
                f"Account.get_profits(): brokers 为空，返回默认值。"
                f"可能原因: 策略 {getattr(self, '_owner_strategy', 'unknown')} "
                f"的 KLine 未正确注册到 _btklinedataset")
            return pd.Series([self._balance], index=[0])
        return results[0]["total_profit"]

    @property
    def net(self) -> float:
        """## 盈利率"""
        return (self.balance-self._balance)/self._balance

    @property
    def risk_ratio(self) -> float:
        """## 风险度(风险度 = 保证金 / 账户权益)"""
        return self.margin/self.balance

    @property
    def print(self):
        """## 账户打印"""
        self.Logger().print_account(self)


def ispandasojb(data, check_iterable=False) -> bool:
    """## 是否为pandas对象

    ### 参数:
        data: 待检查的数据对象
        check_iterable: 是否检查可迭代对象（list/tuple）中的所有元素是否都是pandas对象，默认False

    ### 返回:
        bool: 检查结果
    """
    if check_iterable and isinstance(data, (list, tuple)):
        return all([isinstance(d, (pd.DataFrame, pd.Series)) for d in data])
    return isinstance(data, (pd.DataFrame, pd.Series))


@dataclass
class SymbolInfo(DataSetBase):
    """## 合约信息"""
    symbol: str
    duration: int
    price_tick: float
    volume_multiple: float

    @property
    def cycle(self) -> int:
        return self.duration


@dataclass
class DataFrameSet(DataSetBase):
    """## 数据集
    >>> pandas_object: Union[pd.DataFrame, pd.Series, corefunc]
        kline_object: Optional[Union[pd.DataFrame, corefunc]] = None
        source_object: Optional[Union[IndFrame,
                                    IndSeries, corefunc]] = None
        conversion_object: Optional[Union[pd.DataFrame,
                                        pd.Series, corefunc]] = None
        custom_object: Optional[Union[pd.DataFrame, pd.Series, corefunc]] = None
        tq_object: Optional[Union[pd.DataFrame, pd.Series, corefunc]] = None
        upsample_object: Optional[Union[Line,
                                        IndSeries, IndFrame]] = None
        copy_object: Optional[Union[pd.DataFrame, pd.Series, corefunc]] = None"""
    pandas_object: pd.DataFrame | pd.Series | corefunc
    kline_object: KLine | pd.DataFrame | corefunc | None = None
    source_object: IndFrame | IndSeries | corefunc | None = None
    conversion_object: pd.DataFrame | pd.Series | corefunc | None = None
    custom_object: pd.DataFrame | pd.Series | corefunc | None = None
    tq_object: pd.DataFrame | pd.Series | corefunc | None = None
    tq_tick: pd.DataFrame | pd.Series | corefunc | None = None
    upsample_object: Line | IndSeries | IndFrame | None = None
    copy_object: pd.DataFrame | pd.Series | corefunc | None  = None

    def set_copy_obj(self, obj: pd.DataFrame | pd.Series | corefunc):
        if self.copy_object is None or (isinstance(self.copy_object, (pd.DataFrame,pd.Series)) and self.copy_object.empty):
            self.copy_object = obj.copy()
        if self.pandas_object is None or (isinstance(self.pandas_object, (pd.DataFrame,pd.Series)) and self.copy_object.empty):
            self.pandas_object = obj.copy()
        return self

    def set_source_obj(self, obj: pd.DataFrame | pd.Series | corefunc):
        if self.source_object is None:
            self.source_object = obj
        return self

def default_symbol_info(data: pd.DataFrame) -> dict:
    try:
        # 过滤掉小于等于0的值，获取大于0的最小值
        positive_cycles = data.datetime.diff().apply(lambda x: x.seconds).values
        positive_cycles = positive_cycles[positive_cycles > 0]
        cycle = positive_cycles.min() if len(positive_cycles) > 0 else 60
    except:
        cycle = 60
    return SymbolInfo("symbol", cycle, 0.01, 1.).vars


def set_property(cls, attr: str):
    """## IndFrame快速创建Line的property接口
    - 在类上创建 property，但避免覆盖已有的 callable 方法（如 PandasTa/TaLib 指标）。"""
    for klass in cls.__mro__:
        if attr in klass.__dict__:
            existing = klass.__dict__[attr]
            # 若类或其父类中已存在同名的 callable（且不是 property），
            # 则跳过创建 property，防止污染/覆盖原有方法调用能力。
            if callable(existing) and not isinstance(existing, property):
                return
    def _getter(self): return getattr(self, f'_{attr}')
    def _setter(self, value): self[attr] = value
    setattr(cls, f"{attr}", property(_getter, _setter))


@dataclass
class TqObjs(Base):
    """## 天勤对象管理类（用于连接天勤API获取行情、持仓和交易任务）
    - 核心定位：封装天勤API的Quote、Position和TargetPosTask对象，实现与期货/股票市场的实时数据交互和仓位管理

    ### 核心职责：
    - 1. 行情数据获取：通过Quote对象获取实时行情数据（最新价、买卖盘、成交量等）
    - 2. 持仓信息同步：通过Position对象获取当前合约持仓情况（多空方向、持仓量、持仓成本等）
    - 3. 目标仓位管理：通过TargetPosTask实现目标仓位追踪和自动调仓（支持多策略同时运行）

    ### 核心特性：
    1. API连接验证：
        - 初始化时自动验证天勤API连接状态，确保API未关闭
        - 提供清晰的错误提示，帮助排查连接问题
    2. 对象延迟初始化：
        - Quote、Position、TargetPosTask在`__post_init__`中初始化，避免dataclass字段默认值问题
        - 支持动态获取最新行情和持仓状态
    3. 目标仓位任务支持：
        - TargetPosTask支持直接传入或使用工厂函数创建
        - 自动绑定API和合约代码，简化使用流程

    ### 初始化参数说明：
    Args:
        _api (TqApi): 天勤API实例（必填），用于获取行情、持仓和创建交易任务
        symbol (str): 合约代码（必填），如"SHFE.cu2401"表示上期所铜2401合约

    ### 核心属性说明：
    - 1. Quote (Quote | None): 行情对象，提供实时行情数据访问
    - 2. Position (Position | None): 持仓对象，提供当前合约持仓信息
    - 3. TargetPosTask (TargetPosTask | Callable | None): 目标仓位任务对象，用于自动仓位管理

    ### 使用示例：
    >>> # 1. 初始化天勤API（假设已安装tqsdk）
    >>> from tqsdk import TqApi
    >>> api = TqApi()
    >>>
    >>> # 2. 创建TqObjs对象（获取铜2401合约的相关对象）
    >>> tq_objs = TqObjs(_api=api, symbol="SHFE.cu2401")
    >>>
    >>> # 3. 获取实时行情
    >>> print(f"最新价: {tq_objs.Quote.last_price}")
    >>> print(f"买一价: {tq_objs.Quote.bid_price1}")
    >>> print(f"卖一价: {tq_objs.Quote.ask_price1}")
    >>>
    >>> # 4. 获取持仓信息
    >>> print(f"多头持仓: {tq_objs.Position.pos_long}")
    >>> print(f"空头持仓: {tq_objs.Position.pos_short}")
    >>> print(f"持仓成本: {tq_objs.Position.open_price_long}")
    >>>
    >>> # 5. 设置目标仓位（自动调仓到目标手数）
    >>> tq_objs.TargetPosTask.set_target_volume(5)  # 目标多头5手
    >>> # 或目标空头
    >>> # tq_objs.TargetPosTask.set_target_volume(-3)  # 目标空头3手
    >>>
    >>> # 6. 关闭API连接
    >>> api.close()
    """
    symbol: str
    Quote: Quote | None = None
    Position: Position | None = None
    TargetPosTask: TargetPosTask | Callable | None = None

    def __post_init__(self):
        assert self._api
        assert not self._api._loop.is_closed(), "请连接天勤API"
        self.Quote = self._api.get_quote(self.symbol)
        self.Position = self._api.get_position(self.symbol)
        self.TargetPosTask = TargetPosTask(self._api, self.symbol)
    
    @property
    def tradingtime(self) -> TradingTime:
        """ TradingTime 是一个交易时间对象

        (每个连续的交易时间段是一个列表，包含两个字符串元素，分别为这个时间段的起止点)
        #: 白盘
        self.day: list = []
        #: 夜盘（注意：本字段中过了 24：00 的时间则在其基础往上加，如凌晨1点为 '25:00:00' ）
        self.night: list = []
        """
        return self.Quote.trading_time


class TradeTimeFilter:
    """## 交易时间段过滤器

    用于设置策略在指定时间段内可以/不可以交易。
    结合合约的交易时段，支持排除特定时间段、开盘后N分钟禁止交易、
    收盘前N分钟禁止交易等功能。

    ### 使用示例：
    >>> # 1. 通过策略的 tq_objs 创建过滤器
    >>> trade_filter = TradeTimeFilter(self.tq_obj)
    >>>
    >>> # 2. 通用排除：9:00-9:30 不可交易
    >>> trade_filter.exclude_time("09:00:00", "09:30:00")
    >>>
    >>> # 3. 快捷设置：开盘后15分钟不可交易
    >>> trade_filter.exclude_open_minutes(15)
    >>>
    >>> # 4. 快捷设置：收盘前15分钟不可交易
    >>> trade_filter.exclude_close_minutes(15)
    >>>
    >>> # 5. 在 next() 中检查
    >>> def next(self):
    >>>     if not self.trade_filter.can_trade():
    >>>         return
    >>>     # ... 交易逻辑

    ### 会话识别规则：
    - 相邻时段间隔 > 30 分钟视为不同会话
    - 早盘 (9:00-11:30)：含 10:15-10:30 中场休息，视为同一会话
    - 午盘 (13:30-15:00)
    - 夜盘 (21:00-xxx)：根据合约不同，收盘时间各异
    """
    def __init__(self, tq_obj: TqObjs | None = None,day_segments=None,night_segments=None):
        if tq_obj is None:
            self._day_segments = day_segments or []
            self._night_segments = night_segments or []
        else:
            trading_time = tq_obj.tradingtime
            # 解析日盘和夜盘时段（秒为单位，夜盘保留 25:00 等跨日格式）
            self._day_segments = self._parse_segments(trading_time.day or [])
            self._night_segments = self._parse_segments(trading_time.night or [])
        self._all_segments: list[tuple[int, int]] = sorted(
            self._night_segments + self._day_segments, key=lambda s: s[0])
        # 排除的时段
        self._exclusions: list[tuple[int, int]] = []
        # 识别会话（开盘/收盘时间）
        self._sessions = self._group_sessions(self._all_segments)

    # ── 内部方法 ──

    @staticmethod
    def _parse_segments(segments: list) -> list[tuple[int, int]]:
        result = []
        for seg in segments:
            result.append((
                TradeTimeFilter._time_to_seconds(seg[0]),
                TradeTimeFilter._time_to_seconds(seg[1]),
            ))
        return result

    @staticmethod
    def _time_to_seconds(time_str: str) -> int:
        """时间字符串 → 秒（兼容 '25:00:00' 等跨日格式）"""
        parts = time_str.split(':')
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return h * 3600 + m * 60 + s

    @staticmethod
    def _seconds_to_str(seconds: int) -> str:
        """秒数 → 24小时制时间字符串"""
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f'{h:02d}:{m:02d}:{s:02d}'

    @staticmethod
    def _to_seconds_from_time(current_time) -> int:
        """将各种时间格式转为当日秒数（0-86400）"""
        if current_time is None:
            from datetime import datetime
            now = datetime.now()
            return now.hour * 3600 + now.minute * 60 + now.second
        if isinstance(current_time, str):
            return TradeTimeFilter._time_to_seconds(current_time)
        if hasattr(current_time, 'hour'):
            return current_time.hour * 3600 + current_time.minute * 60 + current_time.second
        if isinstance(current_time, (int, float)):
            return int(current_time)
        return 0

    @staticmethod
    def _group_sessions(segments: list) -> list[list[tuple[int, int]]]:
        """将时段分组为会话（用于识别开盘/收盘时间）

        相邻时段间隔 > 30 分钟视为不同会话。
        例如 [21:00-23:00, 9:00-10:15, 10:30-11:30, 13:30-15:00]
        分组 → [夜盘], [早盘(含中场休息)], [午盘]
        """
        if not segments:
            return []
        sessions = [[segments[0]]]
        for i in range(1, len(segments)):
            gap = segments[i][0] - segments[i - 1][1]
            if gap > 1800:  # 间隔 > 30 分钟 → 新会话
                sessions.append([segments[i]])
            else:
                sessions[-1].append(segments[i])
        return sessions

    def _get_session_name(self, session: list) -> str:
        """根据开盘时段自动识别会话名称"""
        start_hour = session[0][0] // 3600
        if 21 <= start_hour or start_hour <= 3:
            return '夜盘'
        elif 9 <= start_hour < 12:
            return '早盘'
        elif 13 <= start_hour < 18:
            return '午盘'
        return '未知'

    # ── 通用排除（底层机制） ──

    def exclude_time(self, start: str, end: str) -> 'TradeTimeFilter':
        """排除指定时间段（不可交易）

        Args:
            start: 开始时间，如 "09:00:00"
            end: 结束时间，如 "09:30:00"

        Returns:
            self，支持链式调用

        >>> trade_filter.exclude_time("08:55:00", "09:00:00")
        """
        self._exclusions.append((
            self._time_to_seconds(start),
            self._time_to_seconds(end),
        ))
        return self

    # ── 快捷排除（基于会话开盘/收盘） ──

    def exclude_open_minutes(self, minutes: int) -> 'TradeTimeFilter':
        """开盘后 N 分钟内不可交易

        自动识别各会话开盘时间（早盘 9:00、午盘 13:30、夜盘 21:00），
        对每个开盘时间后 N 分钟设为不可交易。

        **注意**：10:30 不是开盘时间（早盘中场休息后的恢复），不会被排除。

        Args:
            minutes: 开盘后不可交易的分钟数

        >>> trade_filter.exclude_open_minutes(15)
        """
        for session in self._sessions:
            open_time = session[0][0]  # 会话第一个时段开始 = 开盘
            self._exclusions.append((open_time, open_time + minutes * 60))
        return self

    def exclude_close_minutes(self, minutes: int) -> 'TradeTimeFilter':
        """收盘前 N 分钟内不可交易

        自动识别各会话收盘时间（早盘 11:30、午盘 15:00、夜盘收盘按合约），
        对每个收盘时间前 N 分钟设为不可交易。

        Args:
            minutes: 收盘前不可交易的分钟数

        >>> trade_filter.exclude_close_minutes(15)
        """
        for session in self._sessions:
            close_time = session[-1][1]  # 会话最后一个时段结束 = 收盘
            self._exclusions.append((close_time - minutes * 60, close_time))
        return self

    # ── 结果查询 ──

    def get_tradable_segments(self) -> list[tuple[int, int]]:
        """获取当前可交易时段（已扣除所有排除段）

        Returns:
            [(start_seconds, end_seconds), ...]
        """
        result = list(self._all_segments)
        for exc_start, exc_end in self._exclusions:
            new_result: list[tuple[int, int]] = []
            for seg_start, seg_end in result:
                if exc_end <= seg_start or exc_start >= seg_end:
                    new_result.append((seg_start, seg_end))
                else:
                    if seg_start < exc_start:
                        new_result.append((seg_start, exc_start))
                    if seg_end > exc_end:
                        new_result.append((exc_end, seg_end))
            result = new_result
        return sorted(result, key=lambda s: s[0])

    def get_sessions_info(self) -> list[dict]:
        """获取各会话信息（名称、开盘、收盘、时段）

        Returns:
            [{'name': '夜盘', 'open': '21:00:00', 'close': '23:00:00',
              'segments': [('21:00:00','23:00:00')]}, ...]
        """
        return [
            dict(
                name=self._get_session_name(s),
                open=self._seconds_to_str(s[0][0]),
                close=self._seconds_to_str(s[-1][1]),
                segments=[
                    (self._seconds_to_str(seg[0]), self._seconds_to_str(seg[1]))
                    for seg in s
                ],
            )
            for s in self._sessions
        ]

    def can_trade(self, current_time=None) -> bool:
        """检查指定时间是否可交易

        Args:
            current_time: 要检查的时间，支持：
                - None（默认）：使用系统当前时间
                - str："HH:MM:SS" 格式
                - datetime.time：时间对象
                - datetime.datetime：日期时间对象
                - int：当日秒数（0-86400）

        Returns:
            bool: 是否可交易

        >>> if not self.trade_filter.can_trade():
        >>>     return
        >>> trade_filter.can_trade("09:30:00")
        >>> trade_filter.can_trade(self.data.datetime[-1])
        """
        from datetime import datetime as _datetime
        seconds = self._to_seconds_from_time(current_time)
        if isinstance(current_time, _datetime):
            seconds = current_time.hour * 3600 + current_time.minute * 60 + current_time.second

        segments = self.get_tradable_segments()
        for seg_start, seg_end in segments:
            if seg_start <= seconds < seg_end:
                return True
            # 跨日检查（夜盘跨越 24:00，如 21:00-25:00）
            # 当前时间在 0:00-2:00 范围，秒数 + 86400 后匹配
            if seg_end > 86400 and seg_start <= seconds + 86400 < seg_end:
                return True
        return False

    @property
    def tradable_str(self) -> str:
        """可交易时段的字符串表示"""
        segments = self.get_tradable_segments()
        seg_strs = [
            f'{self._seconds_to_str(s)}-{self._seconds_to_str(e)}'
            for s, e in segments
        ]
        return ', '.join(seg_strs)

    def __repr__(self):
        return f'TradeTimeFilter({self.tradable_str})'


class PandasObject(metaclass=Meta):
    """## pandas对象"""
    DataFrame = pd.DataFrame
    Series = pd.Series

def np_random(seed: int | None = None) -> tuple[np.random.Generator]:
    """## 根据种子生成随机数生成器并返回生成器和种子

    Args:
        seed: 用于创建生成器的种子

    Returns:
        生成器和结果种子

    Raises:
        Error: 种子必须是非负整数或省略
    """
    if seed is not None and not (isinstance(seed, int) and 0 <= seed):
        raise Exception(
            f"Seed must be a non-negative integer or omitted, not {seed}")

    seed_seq = np.random.SeedSequence(seed)
    np_seed = seed_seq.entropy
    rng = np.random.Generator(np.random.PCG64(seed_seq))
    return rng, np_seed


class GAOpConfig:
    def __new__(cls,
                worker_num: int = None,
                MU: int = 80,
                population_size: int = 100,
                ngen_size: int = 20,
                cx_prb: float = 0.9,
                show_bar: bool = True,
                ) -> dict:
        return dict(
            worker_num=worker_num,
            MU=MU,
            population_size=population_size,
            ngen_size=ngen_size,
            cx_prb=cx_prb,
            show_bar=show_bar
        )


class OptunaConfig:
    def __new__(cls,
                n_trials: int | None = 100,
                timeout: float | None = None,
                n_jobs: int | str = 1,
                catch=(),
                callbacks=None,
                gc_after_trial: bool = False,
                show_progress_bar: bool = True,

                storage=None,
                sampler: Literal['BaseSampler', 'GridSampler', 'RandomSampler', 'TPESampler', 'CmaEsSampler',
                                 'PartialFixedSampler', 'NSGAIISampler', 'NSGAIIISampler', 'MOTPESampler',
                                 'QMCSampler', 'BruteForceSampler', 'IntersectionSearchSpace',
                                 'intersection_search_space'] = 'NSGAIISampler',
                pruner: Literal['BasePruner', 'MedianPruner', 'NopPruner', 'PatientPruner',
                                'PercentilePruner', 'SuccessiveHalvingPruner', 'HyperbandPruner',
                                'ThresholdPruner'] = 'HyperbandPruner',
                study_name='test_optuna',
                direction=None,
                load_if_exists=False,
                directions=None,
                logging: bool = False,
                optunaplot: Literal['plot_rank', 'plot_pareto_front',
                                    'plot_param_importances'] = 'plot_pareto_front',
                ) -> tuple[dict]:
        return dict(
            n_trials=n_trials,
            timeout=timeout,
            n_jobs=n_jobs,
            catch=catch,
            callbacks=callbacks,
            gc_after_trial=gc_after_trial,
            show_progress_bar=show_progress_bar,), dict(
            storage=storage,
            sampler=sampler,
            pruner=pruner,
            study_name=study_name,
            direction=direction,
            load_if_exists=load_if_exists,
            directions=directions,
            logging=logging,
            optunaplot=optunaplot,
        )


class OpConfig:
    def __new__(cls,
                target: OPTTargetType | list[OPTTargetType]= 'profit_ratio',
                weights: float | tuple[float] = 1., 
                opconfig: GAOpConfig | OptunaConfig | dict | list[dict] = {}, 
                op_method: OPTMethodType = 'optuna',
                params: dict = {}) -> dict:
        return dict(
            target=target,
            weights=weights,
            opconfig=opconfig,
            op_method=op_method,
            params=params,
        )


# def execute_once(func):
#     """## 确保函数只执行一次
#     - 用于在指标类中确保每个指标只执行一次
#     - 用于在交易类中确保每个交易只执行一次
#     """
#     def wrapper(self, *args, **kwargs):
#         if not hasattr(self, '_executed'):
#             self._executed = False
#
#         result = func(self, *args, **kwargs)
#         setattr(self, '_executed', True)
#         return result
#     return wrapper


def _create_operator_func(op: str, reverse: bool = False, isbool: bool = False) -> Callable:
    def func(self: IndicatorsBase, other):
        return self._apply_operator(other, op, reverse, isbool)
    return func


def _create_unary_func(expr: str) -> Callable:
    def func(self: IndicatorsBase):
        return self._apply_operate_string(expr)
    return func


class BtNDFrame:
    # ------------------------------
    # 运算符重载（支持指标间直接运算）
    # ------------------------------
    # 比较运算符（<, <=, ==, !=, >, >=）

    def __lt__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('<')(self, other)

    def __le__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('<=')(self, other)

    def __eq__(self, other) -> IndFrame | IndSeries:
        try:
            return _create_operator_func('==')(self, other)
        except:
            return object().__eq__(other)

    def __ne__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('!=')(self, other)

    def __gt__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('>')(self, other)

    def __ge__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('>=')(self, other)

    # 反向比较运算符（如a < b 等效于 b > a）
    def __rlt__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('<', True)(self, other)

    def __rle__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('<=', True)(self, other)

    def __req__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('==', True)(self, other)

    def __rne__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('!=', True)(self, other)

    def __rgt__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('>', True)(self, other)

    def __rge__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('>=', True)(self, other)

    # 一元运算符（将布尔值转换为float，支持数值运算）
    def __pos__(self) -> IndFrame | IndSeries:
        return _create_unary_func('value=+(self.pandas_object.astype(np.float64))')(self)

    def __neg__(self) -> IndFrame | IndSeries:
        return _create_unary_func('value=-(self.pandas_object.astype(np.float64))')(self)

    def __abs__(self) -> IndFrame | IndSeries:
        return _create_unary_func('value=self.pandas_object.astype(np.float64).abs()')(self)

    def __invert__(self) -> IndFrame | IndSeries:
        return _create_unary_func('value=~self.pandas_object.astype(np.bool_)')(self)

    # 二元算术运算符（+, -, *, /, //, %, **）
    def __add__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('+')(self, other)

    def __sub__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('-')(self, other)

    def __mul__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('*')(self, other)

    def __truediv__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('/')(self, other)

    def __floordiv__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('//')(self, other)

    def __mod__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('%')(self, other)

    def __pow__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('**')(self, other)

    # 二元逻辑运算符（&, |，仅布尔值）
    def __and__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('&')(self, other)

    def __or__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('|')(self, other)

    # 反向二元运算符（如a + b 等效于 b + a）
    def __radd__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('+', True)(self, other)

    def __rsub__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('-', True)(self, other)

    def __rmul__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('*', True)(self, other)

    def __rtruediv__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('/', True)(self, other)

    def __rfloordiv__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('//', True)(self, other)

    def __rmod__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('%', True)(self, other)

    def __rpow__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('**', True)(self, other)

    # 反向二元逻辑运算符（&, |，仅布尔值）
    def __rand__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('&', True, True)(self, other)

    def __ror__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('|', True, True)(self, other)

    # 原地运算符（如a += b，直接修改a的值）
    def __iadd__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('+')(self, other)

    def __isub__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('-')(self, other)

    def __imul__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('*')(self, other)

    def __itruediv__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('/')(self, other)

    def __ifloordiv__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('//')(self, other)

    def __imod__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('%')(self, other)

    def __ipow__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('**')(self, other)

    def __iand__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('&', isbool=True)(self, other)

    def __ior__(self, other) -> IndFrame | IndSeries:
        return _create_operator_func('|', isbool=True)(self, other)

    def __iter__(self: IndFrame | IndSeries | KLine | Line) -> Iterator[Line] | Iterator[float | bool]:
        if self.ndim == 1:
            return iter(self.values.tolist())
        return iter([getattr(self, col) for col in self._plotinfo.line_filed])

    def __repr__(self: IndFrame | IndSeries | KLine | Line | NDFrame) -> str:
        """## 返回DataFrame或Series的字符串表示

        返回特定DataFrame的字符串表示。
        返回特定Series的字符串表示。
        """
        if self.isMDim:
            if self._info_repr():
                buf = StringIO()
                self.info(buf=buf)
                return buf.getvalue()

            repr_params = fmt.get_dataframe_repr_params()
        else:
            repr_params = fmt.get_series_repr_params()
        return self.pandas_object.to_string(**repr_params)

    # ----------------------------------------------------------------------
    # Arithmetic Methods
    def eq(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 等于比较（显式方法，对应 == 运算符）

        Args:
            other: 比较的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 元素为布尔值（True表示相等，False表示不相等）
        """
        ...

    def ne(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 不等于比较（显式方法，对应 != 运算符）

        Args:
            other: 比较的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 元素为布尔值（True表示不相等，False表示相等）
        """
        ...

    def le(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 小于等于比较（显式方法，对应 <= 运算符）

        Args:
            other: 比较的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 元素为布尔值（True表示小于等于，False表示大于）
        """
        ...

    def lt(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 小于比较（显式方法，对应 < 运算符）

        Args:
            other: 比较的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 元素为布尔值（True表示小于，False表示不小于）
        """
        ...

    def ge(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 大于等于比较（显式方法，对应 >= 运算符）

        Args:
            other: 比较的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 元素为布尔值（True表示大于等于，False表示小于）
        """
        ...

    def gt(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 大于比较（显式方法，对应 > 运算符）

        Args:
            other: 比较的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 元素为布尔值（True表示大于，False表示不大于）
        """
        ...

    def add(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 算术加法（显式方法，对应 + 运算符）

        Args:
            other: 相加的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储加法运算结果
        """
        ...

    def radd(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向加法（显式方法，对应 other + df）

        Args:
            other: 相加的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向加法运算结果
        """
        ...

    def sub(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 算术减法（显式方法，对应 - 运算符）

        Args:
            other: 相减的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储减法运算结果
        """
        ...

    subtract = sub

    def rsub(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向减法（显式方法，对应 other - df）

        Args:
            other: 相减的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向减法运算结果
        """
        ...

    def mul(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 算术乘法（显式方法，对应 * 运算符）

        Args:
            other: 相乘的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储乘法运算结果
        """
        ...

    multiply = mul

    def rmul(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向乘法（显式方法，对应 other * df）

        Args:
            other: 相乘的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向乘法运算结果
        """
        ...

    def truediv(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 真除法（显式方法，强制返回浮点数结果）

        Args:
            other: 相除的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储真除法运算结果（浮点数）
        """
        ...
    div = truediv
    divide = truediv

    def rtruediv(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向真除法（显式方法，强制返回浮点数结果）

        Args:
            other: 相除的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向真除法运算结果（浮点数）
        """
        ...

    rdiv = rtruediv

    def floordiv(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 向下取整除法（显式方法，对应 // 运算符）

        Args:
            other: 相除的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储整除运算结果（整数）
        """
        ...

    def rfloordiv(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向向下取整除法（显式方法，对应 other // df）

        Args:
            other: 相除的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向整除运算结果（整数）
        """
        ...

    def mod(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 取模运算（显式方法，对应 % 运算符）

        Args:
            other: 取模的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储取余运算结果
        """
        ...

    def rmod(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向取模（显式方法，对应 other % df）

        Args:
            other: 取模的对象（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向取余运算结果
        """
        ...

    def pow(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 幂运算（显式方法，对应 ** 运算符）

        Args:
            other: 幂运算的指数（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储幂次运算结果
        """
        ...

    def rpow(self, other, axis: Axis = "columns", level=None, fill_value=None, **kwargs) -> IndFrame | IndSeries:
        """## 反向幂运算（显式方法，对应 other ** df）

        Args:
            other: 幂运算的底数（如数值、Series、DataFrame）
            axis: 对齐轴（默认"columns"，按列对齐；"index"按行对齐）
            level: 多层索引时的对齐层级（默认None）
            fill_value: 缺失值填充值（默认None，缺失值参与运算仍为缺失值）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 存储反向幂次运算结果
        """
        ...

    ########################## NDFrame ##########################
    # attrs
    # flags
    # set_flags
    # shape
    # axes
    # ndim
    # size
    # set_axis
    # swapaxes
    # droplevel
    # squeeze
    # equals
    # bool

    def abs(self, **kwargs) -> IndFrame | IndSeries:
        """## 计算DataFrame中每个元素的绝对值

        对DataFrame或Series中的所有数值元素取绝对值，非数值元素保持不变。

        Args:
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）

        Returns:
            IndFrame | IndSeries: 元素为原数据绝对值的新对象

        Examples:
            >>> df = IndFrame({'A': [1, -2, 3], 'B': [-4.5, 5.5, -6.5]})
            >>> result = df.abs()
            >>> print(result)
                 A    B
            0  1.0  4.5
            1  2.0  5.5
            2  3.0  6.5
        """
        ...

    # keys
    # items
    # empty
    # ----------------------------------------------------------------------
    # I/O Methods
    # to_excel
    # to_json
    # to_hdf
    # to_sql
    # to_pickle
    # to_clipboard
    # to_xarray
    # to_latex
    # to_csv
    # ----------------------------------------------------------------------
    # Indexing Methods

    def take(self, indices, axis: Axis = 0, **kwargs) -> IndFrame | IndSeries:
        """## 沿指定轴按位置索引获取元素

        根据整数位置索引获取DataFrame的子集，类似于NumPy的take操作。

        Args:
            indices: 位置索引列表或数组
            axis: 操作轴（0表示行，1表示列，默认0）
            **kwargs: 其他关键字参数

        Returns:
            IndFrame | IndSeries : 按位置索引选取的数据子集

        Note:
            当选取结果与原始数据长度相同时返回内置指标格式，否则返回原函数格式

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3, 4], 'B': [5, 6, 7, 8], 'C': [5, 6, 7, 8]})
            >>> result = df.take([0, 1], axis=1)  # 取前两列
            >>> print(result)
               A  B
            0  1  5
            1  2  6
            2  3  7
            3  4  8
        """
        ...

    # xs
    # get
    # reindex_like
    # ...
    # add_prefix
    # add_suffix

    def filter(
        self,
        items=None,
        like: str | None = None,
        regex: str | None = None,
        axis: Axis | None = None,
    ) -> IndFrame | IndSeries:
        """## 根据标签名称过滤数据

        通过列名或索引名过滤数据，支持精确匹配、包含匹配和正则表达式匹配。

        Args:
            items: 精确匹配的标签列表
            like: 包含指定字符串的标签（模糊匹配）
            regex: 匹配正则表达式的标签
            axis: 过滤轴（0表示行索引，1表示列索引，默认None自动判断）

        Returns:
            IndFrame | IndSeries: 过滤后的数据子集

        Examples:
            >>> df = IndFrame({'open_price': [1, 2], 'close_price': [3, 4], 'volume': [5, 6]})
            >>> # 过滤包含'price'的列
            >>> result = df.filter(like='price')
            >>> print(result)
               open_price  close_price
            0           1            3
            1           2            4
        """
        ...

    # head
    # tail
    # sample

    def pipe(
        self,
        func: Callable | tuple[Callable, str],
        *args,
        **kwargs,
    ) -> IndFrame | IndSeries:
        """## 链式方法调用管道

        将DataFrame作为参数传递给函数，支持链式方法调用，提高代码可读性。

        Args:
            func: 要应用的函数或(函数, 方法名)元组
            *args: 函数的其他位置参数
            **kwargs: 函数的关键字参数

        Returns:
            IndFrame | IndSeries : 函数处理后的结果

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>>
            >>> # 定义处理函数
            >>> def normalize_data(df):
            ...     return (df - df.mean()) / df.std()
            >>>
            >>> result = df.pipe(normalize_data)
            >>> print(result)
                      A         B
            0 -1.224745 -1.224745
            1  0.000000  0.000000
            2  1.224745  1.224745
        """
        ...

    # ----------------------------------------------------------------------
    # Internal Interface Methods
    # values
    # dtypes
    def astype(self, dtype, copy: bool | None = None, errors: IgnoreRaise = "raise", **kwargs) -> IndFrame | IndSeries:
        """## 转换DataFrame的数据类型

        将DataFrame或Series的数据转换为指定类型，支持数值类型、字符串类型等转换。

        Args:
            dtype: 目标数据类型（如np.float64、'str'、'int32'等）
            copy: 是否复制数据（True创建新对象，False尝试修改原对象，默认None自动判断）
            errors: 类型转换错误处理（"raise"抛出错误，"ignore"忽略错误并保留原类型，默认"raise"）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 类型转换后的新对象

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [1.1, 2.2, 3.3]})
            >>> # 将A列转换为浮点型
            >>> result = df.astype({'A': 'float64'})
            >>> print(result.dtypes)
            A    float64
            B    float64
            dtype: object
        """
        ...

    def copy(self, as_internal=False, deep=True, ** kwargs) -> IndFrame | IndSeries:
        """## 复制当前指标对象

        创建DataFrame或Series的副本，可控制复制深度和内部属性。

        Args:
            as_internal: 是否转为框架内部指标（True转为内部指标，False保持原属性，默认False）
            deep: 复制深度（True深复制完全独立，False浅复制共享数据，默认True）
            **kwargs: 其他关键字参数（如特定属性过滤等）

        Returns:
            IndFrame | IndSeries: 复制后的新对象

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> df_copy = df.copy(deep=True)
            >>>
            >>> # 修改副本不会影响原对象
            >>> df_copy['A'] = [10, 20, 30]
            >>> print(df['A'])  # 原对象不变
            0    1
            1    2
            2    3
            Name: A, dtype: int64
        """
        ...

    # infer_objects
    # convert_dtypes

    def fillna(
            self,
            value: Hashable | Mapping | pd.Series | pd.DataFrame | None = None,
            *,
            axis: Axis | None = None,
            inplace: bool = False,
            limit: int | None = None,
            **kwargs) -> IndFrame | IndSeries | None:
        """## 填充缺失值

        使用指定值或方法填充DataFrame中的NaN或None值。

        Args:
            value: 填充值（标量、字典、Series或DataFrame）
            axis: 填充轴（0按列填充，1按行填充，默认None自动判断）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            limit: 最大填充次数（None无限制，默认None）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries | None: inplace=False时返回填充后的新对象，否则返回None

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [np.nan, 5, 6]})
            >>> # 用0填充所有缺失值
            >>> result = df.fillna(0)
            >>> print(result)
                 A    B
            0  1.0  0.0
            1  0.0  5.0
            2  3.0  6.0
        """
        ...

    def ffill(
            self,
            *,
            axis: None | Axis = None,
            inplace: bool = False,
            limit: None | int = None,
            limit_area: Literal["inside", "outside"] | None = None,
            **kwargs) -> IndFrame | IndSeries:
        """## 向前填充缺失值

        使用前一个非缺失值填充当前缺失值，适用于时间序列数据的缺失值处理。

        Args:
            axis: 填充轴（0按列向下填充，1按行向右填充，默认None自动判断）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            limit: 最大填充次数（None无限制，默认None）
            limit_area: 填充范围（'inside'仅填充连续缺失值内部，'outside'仅填充边缘缺失值，默认None无限制）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: inplace=False时返回填充后的新对象，否则返回原对象

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'price': [1, np.nan, np.nan, 4, np.nan]})
            >>> # 向前填充缺失值
            >>> result = df.ffill()
            >>> print(result)
               price
            0    1.0
            1    1.0
            2    1.0
            3    4.0
            4    4.0
        """
        ...

    def pad(
        self,
        *,
        axis: None | Axis = None,
        inplace: bool = False,
        limit: None | int = None,
        limit_area: Literal["inside", "outside"] | None = None,
    ) -> IndFrame | IndSeries | None:
        """## 向前填充缺失值（ffill的别名）

        参数和功能与ffill方法完全相同。

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3]})
            >>> result = df.pad()
            >>> print(result)
                 A
            0  1.0
            1  1.0
            2  3.0
        """
        ...

    def bfill(
            self,
            *,
            axis: None | Axis = None,
            inplace: bool = False,
            limit: None | int = None,
            limit_area: Literal["inside", "outside"] | None = None,
            **kwargs) -> IndFrame | IndSeries | None:
        """## 向后填充缺失值

        使用后一个非缺失值填充当前缺失值，与ffill方向相反。

        Args:
            axis: 填充轴（0按列向上填充，1按行向左填充，默认None自动判断）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            limit: 最大填充次数（None无限制，默认None）
            limit_area: 填充范围（'inside'仅填充连续缺失值内部，'outside'仅填充边缘缺失值，默认None无限制）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries | None: inplace=False时返回填充后的新对象，否则返回None

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'price': [np.nan, 2, np.nan, 4, np.nan]})
            >>> # 向后填充缺失值
            >>> result = df.bfill()
            >>> print(result)
               price
            0    2.0
            1    2.0
            2    4.0
            3    4.0
            4    NaN
        """
        ...

    def replace(
            self,
            to_replace=None,
            value=None,
            *,
            inplace: bool = False,
            regex: bool = False,
            **kwargs) -> IndFrame | IndSeries:
        """## 替换DataFrame中的指定值

        将DataFrame中的特定值替换为新值，支持单值、列表、字典和正则表达式匹配。

        Args:
            to_replace: 待替换的值（标量、列表、字典或正则表达式）
            value: 替换后的值（需与to_replace匹配的类型）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            regex: 是否使用正则表达式匹配（True使用正则，False精确匹配，默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: inplace=False时返回替换后的新对象，否则返回原对象

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3, 1], 'B': ['x', 'y', 'x', 'z']})
            >>> # 将所有的1替换为100
            >>> result = df.replace(1, 100)
            >>> print(result)
                 A  B
            0  100  x
            1    2  y
            2    3  x
            3  100  z
        """
        ...

    def interpolate(
            self,
            method: InterpolateOptions = "linear",
            *,
            axis: Axis = 0,
            limit: int | None = None,
            inplace: bool = False,
            limit_direction: Literal["forward",
                                     "backward", "both"] | None = None,
            limit_area: Literal["inside", "outside"] | None = None,
            **kwargs) -> IndFrame | IndSeries:
        """## 插值法填充缺失值

        使用各种插值方法填充DataFrame中的缺失值，适用于数值型数据的连续填充。

        Args:
            method: 插值方法（'linear'线性插值，'polynomial'多项式插值，'spline'样条插值等，默认'linear'）
            axis: 插值轴（0按列插值，1按行插值，默认0）
            limit: 最大插值次数（None无限制，默认None）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            limit_direction: 插值方向（'forward'向前插值，'backward'向后插值，'both'双向插值，默认None）
            limit_area: 插值范围（'inside'仅插值连续缺失值内部，'outside'仅插值边缘缺失值，默认None）
            **kwargs: 框架扩展参数（如多项式插值的order参数）

        Returns:
            IndFrame | IndSeries: inplace=False时返回插值后的新对象，否则返回原对象

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'price': [1, np.nan, np.nan, 4, 5]})
            >>> # 线性插值填充缺失值
            >>> result = df.interpolate(method='linear')
            >>> print(result)
               price
            0    1.0
            1    2.0
            2    3.0
            3    4.0
            4    5.0
        """
        ...
    # asof

    def clip(
            self,
            lower=None,
            upper=None,
            *,
            axis: Axis | None = None,
            inplace: bool = False,
            **kwargs) -> IndFrame | IndSeries | None:
        """## 将值裁剪到指定范围

        将DataFrame中的值限制在[lower, upper]范围内，超出范围的值被替换为边界值。

        Args:
            lower: 裁剪下限（None表示不限制下限，默认None）
            upper: 裁剪上限（None表示不限制上限，默认None）
            axis: 裁剪轴（0按列裁剪，1按行裁剪，默认None自动判断）
            inplace: 是否原地修改原对象（True修改原对象，False返回新对象，默认False）
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）

        Returns:
            IndFrame | IndSeries | None: inplace=False时返回裁剪后的新对象，否则返回None

        Examples:
            >>> df = IndFrame({'A': [1, 5, 10], 'B': [-5, 0, 5]})
            >>> # 将值裁剪到[0, 8]范围内
            >>> result = df.clip(lower=0, upper=8)
            >>> print(result)
                 A  B
            0  1.0  0
            1  5.0  0
            2  8.0  5
        """
        ...

    # asfreq
    # at_time
    # between_time
    # resample
    # first
    # last

    def rank(
            self,
            axis: Axis = 0,
            method: Literal["average", "min", "max",
                            "first", "dense"] = "average",
            numeric_only: bool = False,
            na_option: Literal["keep", "top", "bottom"] = "keep",
            ascending: bool = True,
            pct: bool = False,
            **kwargs) -> IndFrame | IndSeries:
        """## 计算数据的排名

        为DataFrame中的元素计算排名，支持多种排名方法和缺失值处理方式。

        Args:
            axis: 排名轴（0按列排名，1按行排名，默认0）
            method: 排名方法：
                    - 'average'：相同值取平均排名（默认）
                    - 'min'：相同值取最小排名
                    - 'max'：相同值取最大排名
                    - 'first'：按出现顺序排名
                    - 'dense'：相同值同排名，但排名序列连续无间隔
            numeric_only: 是否仅对数值列排名（True仅数值列，False所有列，默认False）
            na_option: NaN值处理方式：
                       - 'keep'：保留NaN，不参与排名（默认）
                       - 'top'：NaN排在最前
                       - 'bottom'：NaN排在最后
            ascending: 是否升序排名（True小值排名靠前，False大值排名靠前，默认True）
            pct: 是否返回百分比排名（True返回[0,1]区间百分比，False返回绝对排名，默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 与输入数据相同形状的排名结果

        Examples:
            >>> df = IndFrame({'A': [3, 1, 2], 'B': [1, 2, 1]})
            >>> # 计算百分比排名
            >>> result = df.rank(pct=True)
            >>> print(result)
                 A         B
            0   1.0       0.5
            1  0.333333   1.0
            2  0.666667   0.5
        """
        ...

    # align
    def where(self,
              cond,
              other=np.nan,
              *,
              inplace: bool = False,
              axis: Axis | None = None,
              level: Hashable | None = None,
              **kwargs) -> IndFrame | IndSeries | None:
        """## 条件替换（满足条件保留原值，不满足替换）

        根据条件表达式保留或替换值，满足条件的保留原值，不满足条件的用other替换。

        Args:
            cond: 条件表达式（布尔型DataFrame/Series或可产生布尔值的函数）
            other: 替换值（不满足cond时使用，默认np.nan）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            axis: 对齐轴（0按行对齐，1按列对齐，默认None自动判断）
            level: 多层索引时的对齐层级（默认None）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries | None: inplace=False时返回条件替换后的新对象，否则返回None

        Examples:
            >>> df = IndFrame({'price': [10, 20, 30], 'volume': [100, 50, 200]})
            >>> # 价格大于15的保留原值，否则设为NaN
            >>> result = df.where(df['price'] > 15)
            >>> print(result)
               price  volume
            0    NaN     NaN
            1   20.0    50.0
            2   30.0   200.0
        """
        ...

    def mask(
            self,
            cond,
            other=None,
            *,
            inplace: bool = False,
            axis: Axis | None = None,
            level: Hashable | None = None,
            **kwargs) -> IndFrame | IndSeries:
        """## 条件掩码（满足条件替换，不满足保留原值）

        与where方法逻辑相反，满足条件的用other替换，不满足条件的保留原值。

        Args:
            cond: 条件表达式（布尔型DataFrame/Series或可产生布尔值的函数）
            other: 替换值（满足cond时使用，默认None）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            axis: 对齐轴（0按行对齐，1按列对齐，默认None自动判断）
            level: 多层索引时的对齐层级（默认None）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: inplace=False时返回条件掩码后的新对象，否则返回原对象

        Examples:
            >>> df = IndFrame({'price': [10, 20, 30], 'volume': [100, 50, 200]})
            >>> # 价格大于15的设为0，否则保留原值
            >>> result = df.mask(df['price'] > 15, 0)
            >>> print(result)
               price  volume
            0     10     100
            1      0       0
            2      0       0
        """
        ...
    # tz_convert
    # tz_localize
    # describe

    def pct_change(
            self,
            periods: int = 1,
            fill_method: None = None,
            freq=None,
            **kwargs) -> IndFrame | IndSeries:
        """## 计算百分比变化

        计算相邻期或指定期间隔的百分比变化，常用于计算收益率、增长率等。

        Args:
            periods: 变化步长（1表示与前1期比较，2表示与前2期比较，默认1）
            fill_method: 已废弃，保持为None以兼容pandas 3.0
            freq: 时间序列频率（仅对时间索引有效，默认None）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | IndSeries: 与输入数据相同长度的百分比变化结果

        Examples:
            >>> df = IndFrame({'price': [100, 110, 99, 105]})
            >>> # 计算每日收益率（百分比变化）
            >>> result = df.pct_change()
            >>> print(result)
                   price
            0       NaN
            1  0.100000
            2 -0.100000
            3  0.060606
        """
        ...

    def shift(
            self,
            periods: int | Sequence[int] = 1,
            freq: Frequency | None = None,
            axis: Axis = 0,
            fill_value: Hashable = None,
            suffix: str | None = None,
            **kwargs) -> IndFrame | IndSeries:
        """## 将IndFrame/IndSeries数据按指定步长移动

        - 常用于计算时序数据的滞后/超前值，支持时间频率移动。

        Args:
            periods: 移动步长（正数向下/向右移，负数向上/向左移，默认1）
            freq: 时间序列的频率（仅index为DatetimeIndex时有效，默认None）
            axis: 移动轴（0按行移动，1按列移动，默认0）
            fill_value: 移动后空值的填充值（默认None表示用NaN填充）
            suffix: 移动后列名后缀（默认None）
            **kwargs: 框架扩展参数

        Returns:
        >>> IndFrame | IndSeries: 存储移动后的数据

        Examples:
            >>> df = IndFrame({'price': [10, 20, 30, 40]})
            >>> result = df.shift(periods=1)
            >>> print(result)
               price
            0    NaN
            1   10.0
            2   20.0
            3   30.0
            >>> df = IndSeries([10, 20, 30, 40])
            >>> result = df.shift(periods=1)
            >>> print(result)
            0    NaN
            1   10.0
            2   20.0
            3   30.0
        """
        ...


class PandasDataFrame(BtNDFrame, pd.DataFrame):
    """
    量化框架自定义的DataFrame增强类，继承自pandas原生pd.DataFrame
    核心功能：
    1. 重载 pandas 所有常用运算符（比较、算术、反向算术、原地算术），确保运算结果自动转为框架自定义的IndFrame类型
    2. 重写 pandas 核心数据处理方法（如数值计算、缺失值填充、数据转换等），保持原生功能逻辑的同时，
       通过 self._pandas_object_method 或 inplace_values 适配框架内数据类型，兼容后续指标计算、可视化等扩展能力
    3. 支持 inplace 参数控制是否修改原对象，统一返回框架自定义的IndFrame类型（或Optional[IndFrame]），适配量化回测数据流程
    """

    def _repr_html_(self) -> str | None:
        """## 返回特定DataFrame的HTML表示
        主要用于IPython笔记本中显示DataFrame。
        """
        return pd.DataFrame._repr_html_(self.pandas_object)

    ######################## DataFrame ############################

    # ----------------------------------------------------------------------
    # axes
    # shape
    # to_string
    # style
    # items
    # iterrows
    # itertuples
    # dot
    # ----------------------------------------------------------------------
    # IO methods (to / from other formats)
    # from_dict
    # to_numpy
    # to_dict
    # to_gbq
    # from_records
    # to_records
    # to_stata
    # to_feather
    # to_markdown
    # to_parquet
    # to_orc
    # to_html
    # to_xml

    # ----------------------------------------------------------------------
    # info
    # memory_usage
    # transpose
    # T
    # ----------------------------------------------------------------------
    # Indexing Methods
    # def isetitem(self, loc, value) -> None:
    #     ...
    # ----------------------------------------------------------------------
    # Unsorted
    # query
    # def query(self, expr: str, *, inplace: bool = False, **kwargs) -> IndFrame | None:
    #     """## 使用布尔表达式查询DataFrame数据

    #     通过字符串表达式筛选DataFrame的行，支持复杂的条件组合。

    #     Args:
    #         expr: 查询表达式字符串（如"price > 100 and volume > 1000"）
    #         inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
    #         **kwargs: 其他关键字参数

    #     Returns:
    #         IndFrame | None: inplace=False时返回查询结果的新对象，否则返回None

    #     Examples:
    #         >>> df = IndFrame({'price': [95, 105, 110], 'volume': [800, 1200, 1500]})
    #         >>> result = df.query("price > 100 and volume > 1000")
    #         >>> print(result)
    #            price  volume
    #         1    105    1200
    #         2    110    1500
    #     """
    #     ...

    def eval(self, expr: str, *, inplace: bool = False, **kwargs) -> IndFrame:
        """## 执行字符串表达式计算

        - 在DataFrame的上下文中执行Python表达式，支持列名直接参与运算。

        Args:
            expr: 计算表达式字符串（如"price * volume"）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            **kwargs: 其他关键字参数

        Returns:
            IndFrame  : 表达式计算结果

        Examples:
            >>> df = IndFrame({'price': [10, 20], 'volume': [100, 200]})
            >>> result = df.eval("total = price * volume")
            >>> print(result)
               price  volume  total
            0     10     100   1000
            1     20     200   4000
        """
        ...

    def insert(
        self,
        loc: int,
        column: Hashable,
        value: Scalar | AnyArrayLike,
        allow_duplicates: bool | None = None,
        inplace: bool = True,
        **kwargs
    ) -> IndFrame:
        """## 在指定位置插入列

        - 在DataFrame的指定位置插入新列。

        Args:
            loc: 插入位置索引（0表示第一列前）
            column: 新列名称
            value: 列数据（标量或数组）
            allow_duplicates: 是否允许列名重复（默认None，遵循全局设置）

        Returns:
            None: 原地修改，无返回值

        Examples:
            >>> df = IndFrame({'A': [1, 2], 'B': [3, 4]})
            >>> df.insert(1, 'C', [5, 6])
            >>> print(df)
               A  C  B
            0  1  5  3
            1  2  6  4
        """
        ...

    def assign(self, **kwargs) -> IndFrame:
        """## 分配新列（链式操作）

        - 为DataFrame分配新列，返回包含新列的新对象，支持链式操作。

        Args:
            **kwargs: 列名和列数据的键值对

        Returns:
            IndFrame: 包含新列的新对象

        Examples:
            >>> df = IndFrame({'A': [1, 2], 'B': [3, 4]})
            >>> result = df.assign(C=lambda x: x.A + x.B, D=[5, 6])
            >>> print(result)
               A  B  C  D
            0  1  3  4  5
            1  2  4  6  6
        """
        ...
    # set_axis
    # reindex

    def drop(
            self,
            labels: IndexLabel | None = None,
            *,
            axis: Axis = 0,
            index: IndexLabel | None = None,
            columns: IndexLabel | None = None,
            level: Hashable | None = None,
            inplace: bool = False,
            errors: IgnoreRaise = "raise",
            **kwargs) -> IndFrame | pd.DataFrame:
        """## 删除DataFrame中的指定行或列

        - 删除指定的行或列，支持多种删除方式。

        Args:
            labels: 待删除的行/列标签（默认None）
            axis: 删除轴（0删除行，1删除列，默认0）
            index: 直接指定待删除的行标签（优先级高于labels+axis=0）
            columns: 直接指定待删除的列标签（优先级高于labels+axis=1）
            level: 多层索引时的删除层级（默认None）
            inplace: 是否修改原对象（默认False）
            errors: 标签不存在时的处理（'raise'抛出错误，'ignore'忽略，默认'raise'）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | pd.DataFrame | None: inplace=False时返回删除后的新对象，否则返回None

        ## None:
            删除行返回的是pd.DataFrame

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6], 'C': [7, 8, 9]})
            >>> result = df.drop(columns=['B'])
            >>> print(result)
               A  C
            0  1  7
            1  2  8
            2  3  9
        """
        ...
    # rename

    # def shift(
    #         self,
    #         periods: int | Sequence[int] = 1,
    #         freq: Frequency | None = None,
    #         axis: Axis = 0,
    #         fill_value: Hashable = None,
    #         suffix: str | None = None,
    #         **kwargs) -> IndFrame:
    #     """## 将IndFrame数据按指定步长移动

    #     - 常用于计算时序数据的滞后/超前值，支持时间频率移动。

    #     Args:
    #         periods: 移动步长（正数向下/向右移，负数向上/向左移，默认1）
    #         freq: 时间序列的频率（仅index为DatetimeIndex时有效，默认None）
    #         axis: 移动轴（0按行移动，1按列移动，默认0）
    #         fill_value: 移动后空值的填充值（默认None表示用NaN填充）
    #         suffix: 移动后列名后缀（默认None）
    #         **kwargs: 框架扩展参数

    #     Returns:
    #         IndFrame: 存储移动后的数据

    #     Examples:
    #         >>> df = IndFrame({'price': [10, 20, 30, 40]})
    #         >>> result = df.shift(periods=1)
    #         >>> print(result)
    #            price
    #         0    NaN
    #         1   10.0
    #         2   20.0
    #         3   30.0
    #     """
    #     ...
    # set_index

    def reset_index(
        self,
        level: IndexLabel | None = None,
        *,
        drop: bool = False,
        inplace: bool = False,
        col_level: Hashable = 0,
        col_fill: Hashable = "",
        allow_duplicates: bool | lib.NoDefault = lib.no_default,
        names: Hashable | Sequence[Hashable] | None = None,
    ) -> IndFrame:
        """## 重置索引

        - 将索引重置为默认整数索引，原索引转为列。

        Args:
            level: 要重置的索引层级（多层索引时使用，默认None重置所有）
            drop: 是否丢弃原索引（True丢弃，False保留为列，默认False）
            inplace: 是否修改原对象（默认False）
            col_level: 多层列索引时的插入层级（默认0）
            col_fill: 列名填充值（默认""）
            allow_duplicates: 是否允许列名重复（默认None）
            names: 新索引的名称（默认None）

        Returns:
            IndFrame | None: inplace=False时返回重置后的新对象，否则返回None

        Examples:
            >>> df = IndFrame({'value': [1, 2, 3]}, index=['a', 'b', 'c'])
            >>> result = df.reset_index()
            >>> print(result)
              index  value
            0     a      1
            1     b      2
            2     c      3
        """
        ...
    # ----------------------------------------------------------------------
    # Reindex-based selection methods

    def isna(self, **kwargs) -> IndFrame:
        """## 检测DataFrame中的空元素

        返回与原始DataFrame相同形状的布尔值DataFrame，其中：
        - True 表示对应位置的元素为空值（NaN、None等）
        - False 表示对应位置的元素为非空值

        Returns:
            IndFrame: 与输入数据相同长度，存储布尔值结果

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [None, 5, 6]})
            >>> result = df.isna()
            >>> print(result)
                   A      B
            0  False   True
            1   True  False
            2  False  False
        """
        ...

    def isnull(self, **kwargs) -> IndFrame:
        """## 检测DataFrame中的空元素（isnull是isna的别名）

        返回与原始DataFrame相同形状的布尔值DataFrame，其中：
        - True 表示对应位置的元素为空值（NaN、None等）
        - False 表示对应位置的元素为非空值

        Returns:
            IndFrame: 与输入数据相同长度，存储布尔值结果

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [None, 5, 6]})
            >>> result = df.isnull()
            >>> print(result)
                   A      B
            0  False   True
            1   True  False
            2  False  False
        """
        ...

    def notna(self, **kwargs) -> IndFrame:
        """## 检测DataFrame中的非空元素

        返回与原始DataFrame相同形状的布尔值DataFrame，其中：
        - True 表示对应位置的元素为非空值
        - False 表示对应位置的元素为空值（NaN、None等）

        Returns:
            IndFrame: 与输入数据相同长度，存储布尔值结果

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [None, 5, 6]})
            >>> result = df.notna()
            >>> print(result)
                   A      B
            0   True  False
            1  False   True
            2   True   True
        """
        ...

    def notnull(self, **kwargs) -> IndFrame:
        """## 检测DataFrame中的非空元素（notnull是notna的别名）

        返回与原始DataFrame相同形状的布尔值DataFrame，其中：
        - True 表示对应位置的元素为非空值
        - False 表示对应位置的元素为空值（NaN、None等）

        Returns:
            IndFrame: 与输入数据相同长度，存储布尔值结果

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [None, 5, 6]})
            >>> result = df.notnull()
            >>> print(result)
                   A      B
            0   True  False
            1  False   True
            2   True   True
        """
        ...
    # dropna?
    # def dropna(
    #     self,
    #     *,
    #     axis: Axis = 0,
    #     how: AnyAll | None = None,
    #     thresh: int | None = None,
    #     subset: IndexLabel | None = None,
    #     inplace: bool = False,
    #     ignore_index: bool = False,
    # ) -> IndFrame | None:
    #     """## 删除包含缺失值的行或列

    #     根据缺失值情况删除DataFrame的行或列。

    #     Args:
    #         axis: 删除轴（0删除行，1删除列，默认0）
    #         how: 删除条件（'any'任意缺失即删除，'all'全部缺失才删除，默认'any'）
    #         thresh: 非缺失值的最小数量阈值（默认None）
    #         subset: 检查缺失值的列子集（默认None检查所有列）
    #         inplace: 是否修改原对象（默认False）
    #         ignore_index: 是否重置索引（默认False）

    #     Returns:
    #         IndFrame | None: inplace=False时返回删除后的新对象，否则返回None

    #     Examples:
    #         >>> import numpy as np
    #         >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [4, 5, np.nan]})
    #         >>> result = df.dropna()
    #         >>> print(result)
    #              A    B
    #         0  1.0  4.0
    #     """
    #     ...

    # drop_duplicates

    def duplicated(
        self,
        subset: Hashable | Sequence[Hashable] | None = None,
        keep: Literal["first", "last", False] = "first",
        **kwargs
    ) -> IndSeries:
        """## 标记重复行

        - 返回布尔值Series，标识DataFrame中的重复行。

        Args:
            subset: 用于判断重复的列子集（默认使用所有列）
            keep: 重复标记策略：
                  - 'first'：除第一次出现外，标记所有重复为True
                  - 'last'：除最后一次出现外，标记所有重复为True
                  - False：标记所有重复为True
            **kwargs: 框架扩展参数

        Returns:
            IndSeries: 与输入数据相同长度，存储布尔值结果

        Examples:
            >>> df = IndFrame({'A': [1, 2, 1, 3], 'B': [4, 5, 4, 6]})
            >>> result = df.duplicated()
            >>> print(result)
            0    False
            1    False
            2     True
            3    False
            dtype: bool
        """
        ...

    # ----------------------------------------------------------------------
    # Sorting

    def sort_values(
        self,
        by: IndexLabel,
        *,
        axis: Axis = 0,
        ascending: bool | list[bool] | tuple[bool, ...] = True,
        inplace: bool = False,
        kind: SortKind = "quicksort",
        na_position: str = "last",
        ignore_index: bool = False,
        key: ValueKeyFunc | None = None,
        **kwargs
    ) -> IndFrame | None:
        """## 按值排序

        - 根据指定列或行的值对DataFrame进行排序。

        Args:
            by: 排序依据的列名或行索引
            axis: 排序轴（0按列排序，1按行排序，默认0）
            ascending: 是否升序排序（默认True）
            inplace: 是否修改原对象（默认False）
            kind: 排序算法（'quicksort', 'mergesort', 'heapsort'，默认'quicksort'）
            na_position: 缺失值位置（'first'排在最前，'last'排在最后，默认'last'）
            ignore_index: 是否重置索引（默认False）
            key: 排序前应用于数据的函数（默认None）

        Returns:
            IndFrame | None: inplace=False时返回排序后的新对象，否则返回None

        Examples:
            >>> df = IndFrame({'A': [3, 1, 2], 'B': [6, 4, 5]})
            >>> result = df.sort_values(by='A')
            >>> print(result)
               A  B
            1  1  4
            2  2  5
            0  3  6
        """
        ...

    def sort_index(
        self,
        *,
        axis: Axis = 0,
        level: IndexLabel | None = None,
        ascending: bool | Sequence[bool] = True,
        inplace: bool = False,
        kind: SortKind = "quicksort",
        na_position: NaPosition = "last",
        sort_remaining: bool = True,
        ignore_index: bool = False,
        key: IndexKeyFunc | None = None,
    ) -> IndFrame:
        """## 按索引排序

        - 根据行索引或列名对DataFrame进行排序。

        Args:
            axis: 排序轴（0按行索引排序，1按列名排序，默认0）
            level: 多层索引时的排序层级（默认None）
            ascending: 是否升序排序（默认True）
            inplace: 是否修改原对象（默认False）
            kind: 排序算法（默认'quicksort'）
            na_position: 缺失值位置（默认'last'）
            sort_remaining: 是否排序剩余层级（默认True）
            ignore_index: 是否重置索引（默认False）
            key: 排序前应用于索引的函数（默认None）

        Returns:
            IndFrame | None: inplace=False时返回排序后的新对象，否则返回None

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3]}, index=[2, 0, 1])
            >>> result = df.sort_index()
            >>> print(result)
               A
            0  2
            1  3
            2  1
        """
        ...
    # value_counts
    # nlargest
    # nsmallest
    # swaplevel
    # reorder_levels
    # ----------------------------------------------------------------------
    # Combination-Related

    # def compare(
    #     self,
    #     other: pd.DataFrame,
    #     align_axis: Axis = 1,
    #     keep_shape: bool = False,
    #     keep_equal: bool = False,
    #     result_names: Suffixes = ("self", "other"),
    # ) -> IndFrame:
    #     """## 比较两个DataFrame的差异

    #     比较当前DataFrame与另一个DataFrame的差异，返回差异结果。

    #     Args:
    #         other: 要比较的另一个DataFrame
    #         align_axis: 结果对齐轴（1列对齐，0行对齐，默认1）
    #         keep_shape: 是否保持原始形状（默认False）
    #         keep_equal: 是否保留相等值（默认False）
    #         result_names: 结果列名后缀（默认('self', 'other')）

    #     Returns:
    #         IndFrame: 包含差异比较结果

    #     Examples:
    #         >>> df1 = IndFrame({'A': [1, 2], 'B': [3, 4]})
    #         >>> df2 = IndFrame({'A': [1, 3], 'B': [3, 5]})
    #         >>> result = df1.compare(df2)
    #         >>> print(result)
    #              A         B
    #            self other self other
    #         1   2.0   3.0  4.0   5.0
    #     """
    #     ...

    def combine(
        self,
        other: pd.DataFrame,
        func: Callable[[pd.Series, pd.Series], pd.Series | Hashable],
        fill_value=None,
        overwrite: bool = True,
        **kwargs
    ) -> IndFrame:
        """## 按元素组合两个DataFrame

        - 使用指定函数按元素组合两个DataFrame。

        Args:
            other: 要组合的另一个DataFrame
            func: 组合函数，接受两个Series返回一个Series或标量
            fill_value: 缺失值填充值（默认None）
            overwrite: 是否覆盖原值（默认True）

        Returns:
            IndFrame: 组合后的结果

        Examples:
            >>> df1 = IndFrame({'A': [1, 2], 'B': [3, 4]})
            >>> df2 = IndFrame({'A': [5, 6], 'B': [7, 8]})
            >>> result = df1.combine(df2, lambda x, y: x + y)
            >>> print(result)
               A   B
            0  6  10
            1  8  12
        """
        ...

    def combine_first(self, other: pd.DataFrame, **kwargs) -> IndFrame:
        """## 用另一个DataFrame填充缺失值

        - 使用另一个DataFrame的非空值填充当前DataFrame的缺失值。

        Args:
            other: 用于填充的DataFrame

        Returns:
            IndFrame: 填充后的结果

        Examples:
            >>> import numpy as np
            >>> df1 = IndFrame({'A': [1, np.nan], 'B': [np.nan, 4]})
            >>> df2 = IndFrame({'A': [5, 6], 'B': [7, 8]})
            >>> result = df1.combine_first(df2)
            >>> print(result)
                 A    B
            0  1.0  7.0
            1  6.0  4.0
        """
        ...

    def update(
        self,
        other,
        join: UpdateJoin = "left",
        overwrite: bool = True,
        filter_func=None,
        errors: IgnoreRaise = "ignore",
        inplace: bool = True,
        **kwargs
    ) -> IndFrame:
        """## 使用另一个DataFrame更新当前对象

        - 使用另一个DataFrame的值更新当前DataFrame，默认原地修改。

        Args:
            other: 用于更新的DataFrame
            join: 连接方式（'left'左连接，默认'left'）
            overwrite: 是否覆盖非空值（默认True）
            filter_func: 过滤函数（默认None）
            errors: 错误处理（'ignore'忽略错误，'raise'抛出错误，默认'ignore'）
            inplace:新增参数,是否原地修改,默认为True

        Returns:
            IndFrame: 原值

        Examples:
            >>> df1 = IndFrame({'A': [1, 2], 'B': [3, 4]})
            >>> df2 = IndFrame({'A': [5, 6]}, index=[0, 1])
            >>> df1.update(df2)
            >>> print(df1)
               A  B
            0  5  3
            1  6  4
        """
        ...

    # Data reshaping
    # groupby
    # pivot
    # pivot_table
    # def stack(
    #     self,
    #     level: IndexLabel = -1,
    #     dropna: bool | None = None,
    #     sort: bool | None = None,
    #     future_stack: bool = False,
    # ) -> IndSeries | IndFrame:
    #     """## 堆叠列到行（宽表转长表）

    #     将列层级堆叠到行索引，实现宽表向长表的转换。

    #     Args:
    #         level: 要堆叠的列层级（默认-1）
    #         dropna: 是否删除缺失值（默认None）
    #         sort: 是否排序结果（默认None）
    #         future_stack: 是否使用未来堆叠行为（默认False）

    #     Returns:
    #         IndSeries | IndFrame: 堆叠后的结果

    #     Note:
    #         返回的数据长度可能发生变化，可能返回pandas原生的Series或DataFrame

    #     Examples:
    #         >>> df = IndFrame({'A': [1, 2], 'B': [3, 4]}, index=['x', 'y'])
    #         >>> result = df.stack()
    #         >>> print(result)
    #         x  A    1
    #            B    3
    #         y  A    2
    #            B    4
    #         dtype: int64
    #     """
    #     ...

    # def explode(
    #     self,
    #     column: IndexLabel,
    #     ignore_index: bool = False,
    # ) -> IndFrame:
    #     """## 爆炸列表列

    #     将包含列表-like数据的列拆分为多行。

    #     Args:
    #         column: 要爆炸的列名
    #         ignore_index: 是否重置索引（默认False）

    #     Returns:
    #         IndFrame: 爆炸后的结果

    #     Examples:
    #         >>> df = IndFrame({'A': [[1, 2], [3]], 'B': ['x', 'y']})
    #         >>> result = df.explode('A')
    #         >>> print(result)
    #              A  B
    #         0    1  x
    #         0    2  x
    #         1    3  y
    #     """
        ...

    # def unstack(self, level: IndexLabel = -1, fill_value=None, sort: bool = True) -> IndFrame | IndSeries:
    #     """## 展开行到列（长表转宽表）

    #     将行索引层级展开为列，实现长表向宽表的转换。

    #     Args:
    #         level: 要展开的行层级（默认-1）
    #         fill_value: 缺失值填充值（默认None）
    #         sort: 是否排序结果（默认True）

    #     Returns:
    #         IndFrame | IndSeries: 展开后的结果

    #     Note:
    #         返回的数据长度可能发生变化，可能返回pandas原生的Series或DataFrame

    #     Examples:
    #         >>> s = pd.Series([1, 2, 3, 4], index=pd.MultiIndex.from_tuples([('x', 'a'), ('x', 'b'), ('y', 'a'), ('y', 'b')]))
    #         >>> df = IndFrame(s)
    #         >>> result = df.unstack()
    #         >>> print(result)
    #              a  b
    #         x  1.0  2.0
    #         y  3.0  4.0
    #     """
    #     ...

    # def melt(
    #     self,
    #     id_vars=None,
    #     value_vars=None,
    #     var_name=None,
    #     value_name: Hashable = "value",
    #     col_level: Level | None = None,
    #     ignore_index: bool = True,
    # ) -> IndFrame:
    #     """## 熔化DataFrame（宽表转长表）

    #     将多列转换为键值对形式，实现宽表向长表的转换。

    #     Args:
    #         id_vars: 作为标识符的列
    #         value_vars: 要熔化的列
    #         var_name: 变量列名称
    #         value_name: 值列名称（默认"value"）
    #         col_level: 多层列索引时的层级（默认None）
    #         ignore_index: 是否重置索引（默认True）

    #     Returns:
    #         IndFrame: 熔化后的结果

    #     Examples:
    #         >>> df = IndFrame({'A': [1, 2], 'B': [3, 4], 'C': [5, 6]})
    #         >>> result = df.melt(id_vars=['A'], value_vars=['B', 'C'])
    #         >>> print(result)
    #            A variable  value
    #         0  1        B      3
    #         1  2        B      4
    #         2  1        C      5
    #         3  2        C      6
    #     """
    #     ...

    # Time IndSeries-related
    def diff(self, periods: int = 1, axis: Axis = 0, **kwargs) -> IndFrame:
        """## 计算DataFrame数据的差分

        - 计算相邻元素的差值，常用于时序数据平稳性检验。

        Args:
            periods: 差分步长（默认1，即当前值减前1期值）
            axis: 差分轴（0按行差分，1按列差分，默认0）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame: 存储差分结果（首periods行/列为NaN）

        Examples:
            >>> df = IndFrame({'price': [10, 12, 15, 14]})
            >>> result = df.diff()
            >>> print(result)
               price
            0    NaN
            1    2.0
            2    3.0
            3   -1.0
        """
        ...
    # Function application

    # def aggregate(self, func: AggFuncType, axis: Axis = 0, *args, **kwargs) -> IndFrame | IndSeries:
    #     """## 聚合计算

    #     对DataFrame进行聚合计算，支持多种聚合函数。

    #     Args:
    #         func: 聚合函数或函数列表
    #         axis: 聚合轴（0按列聚合，1按行聚合，默认0）
    #         *args: 传递给聚合函数的参数
    #         **kwargs: 关键字参数

    #     Returns:
    #         IndFrame | IndSeries: 聚合结果

    #     Note:
    #         当axis=0时返回IndSeries，axis=1时返回IndFrame，具体取决于聚合结果

    #     Examples:
    #         >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
    #         >>> result = df.aggregate(['sum', 'mean'])
    #         >>> print(result)
    #              A    B
    #         sum   6.0  15.0
    #         mean  2.0   5.0
    #     """
    #     ...

    # agg = aggregate

    def transform(
        self, func: AggFuncType, axis: Axis = 0, *args, **kwargs
    ) -> IndFrame:
        """## 转换数据

        - 对DataFrame应用函数并返回相同形状的结果。

        Args:
            func: 转换函数
            axis: 转换轴（0按列转换，1按行转换，默认0）
            *args: 传递给函数的参数
            **kwargs: 关键字参数

        Returns:
            IndFrame: 转换后的结果，与输入形状相同

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.transform(lambda x: x - x.mean())
            >>> print(result)
                 A    B
            0 -1.0 -1.0
            1  0.0  0.0
            2  1.0  1.0
        """
        ...

    def apply(
        self,
        func: AggFuncType,
        axis: Axis = 0,
        raw: bool = False,
        result_type: Literal["expand", "reduce", "broadcast"] | None = None,
        args=(),
        by_row: Literal[False, "compat"] = "compat",
        engine: Literal["python", "numba"] | None = None,
        engine_kwargs: dict[str, bool] | None = None,
        **kwargs,
    ) -> IndSeries | IndFrame:
        """## 对DataFrame按指定轴应用自定义函数

        - 支持元素级、行/列级计算，灵活处理DataFrame数据。

        Args:
            func: 自定义函数（如lambda x: x.sum()、np.mean）
            axis: 应用轴（0按列应用函数，1按行应用函数，默认0）
            raw: 是否传入原始数组（True传入ndarray，False传入Series，默认False）
            result_type: 返回结果类型（'expand'展开为DataFrame，'reduce'压缩为Series，默认None自动判断）
            args: 传递给func的额外位置参数（默认()）
            by_row: 按行应用方式（默认"compat"）
            engine: 执行引擎（'python'或'numba'，默认'python'）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 传递给func的额外关键字参数，及框架扩展参数

        Returns:
            IndSeries | IndFrame: 框架自定义IndSeries（结果为1维时）或IndFrame（结果为多维时）

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.apply(lambda x: x.max() - x.min(), axis=0)
            >>> print(result)
            A    2
            B    2
            dtype: int64
        """
        ...

    def map(self, func: PythonFuncType, na_action: Literal["ignore"] | None = None, **kwargs) -> IndFrame:
        """## 对DataFrame每个元素应用自定义函数

        - 元素级运算，类似元素遍历，支持字典映射。

        Args:
            func: 元素级自定义函数（如lambda x: x*2，或字典映射）
            na_action: 缺失值处理（'ignore'跳过缺失值，默认None表示不跳过）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame: 存储元素级运算结果

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.map(lambda x: x * 2)
            >>> print(result)
               A   B
            0  2   8
            1  4  10
            2  6  12
        """
        ...

    # ----------------------------------------------------------------------
    # Merging / joining methods

    # def _append(
    #     self,
    #     other,
    #     ignore_index: bool = False,
    #     verify_integrity: bool = False,
    #     sort: bool = False,
    # ) -> IndFrame:
    #     """## 追加数据（已弃用，建议使用concat）

    #     将其他DataFrame追加到当前DataFrame末尾。

    #     Args:
    #         other: 要追加的DataFrame
    #         ignore_index: 是否重置索引（默认False）
    #         verify_integrity: 是否验证索引完整性（默认False）
    #         sort: 是否排序列（默认False）

    #     Returns:
    #         IndFrame: 追加后的结果

    #     Examples:
    #         >>> df1 = IndFrame({'A': [1, 2], 'B': [3, 4]})
    #         >>> df2 = IndFrame({'A': [5, 6], 'B': [7, 8]})
    #         >>> result = df1._append(df2, ignore_index=True)
    #         >>> print(result)
    #            A  B
    #         0  1  3
    #         1  2  4
    #         2  5  7
    #         3  6  8
    #     """
    #     ...

    def join(
        self,
        other: pd.DataFrame | pd.Series | Iterable[pd.DataFrame | pd.Series],
        on: IndexLabel | None = None,
        how: MergeHow = "left",
        lsuffix: str = "",
        rsuffix: str = "",
        sort: bool = False,
        validate: JoinValidate | None = None,
        **kwargs
    ) -> IndFrame | pd.DataFrame:
        """## 连接操作

        - 基于索引连接DataFrame，类似SQL的JOIN操作。

        Args:
            other: 要连接的DataFrame、Series或它们的可迭代对象
            on: 连接的列名（默认None使用索引）
            how: 连接方式（'left', 'right', 'outer', 'inner'，默认'left'）
            lsuffix: 左重复列后缀（默认""）
            rsuffix: 右重复列后缀（默认""）
            sort: 是否排序结果（默认False）
            validate: 连接验证（默认None）

        Returns:
            IndFrame: 连接后的结果

        Note:
            返回的数据长度取决于连接方式和索引匹配情况：
            - 'left': 保持左表长度，右表无匹配则为NaN
            - 'right': 保持右表长度，左表无匹配则为NaN  
            - 'inner': 仅保留两表都有匹配的行
            - 'outer': 保留所有行，无匹配则为NaN

        Examples:
            >>> # 创建索引不完全匹配的DataFrame
            >>> df1 = IndFrame({'A': [1, 2, 3]}, index=['x', 'y', 'z'])
            >>> df2 = IndFrame({'B': [4, 5]}, index=['x', 'y'])
            >>> # 左连接：保持左表所有行，右表无匹配的显示为NaN
            >>> result = df1.join(df2, how='left')
            >>> print(result)
               A    B
            x  1  4.0
            y  2  5.0
            z  3  NaN
            >>> print(f"原始左表长度: {len(df1)}, 连接后长度: {len(result)}")
            原始左表长度: 3, 连接后长度: 3
        """
        ...

    def merge(
        self,
        right: pd.DataFrame | pd.Series,
        how: MergeHow = "inner",
        on: IndexLabel | AnyArrayLike | None = None,
        left_on: IndexLabel | AnyArrayLike | None = None,
        right_on: IndexLabel | AnyArrayLike | None = None,
        left_index: bool = False,
        right_index: bool = False,
        sort: bool = False,
        suffixes: Suffixes = ("_x", "_y"),
        copy: bool | None = None,
        indicator: str | bool = False,
        validate: MergeValidate | None = None,
        **kwargs
    ) -> IndFrame | pd.DataFrame:
        """## 合并操作

        - 基于列值合并DataFrame，类似SQL的JOIN操作。

        Args:
            right: 要合并的右DataFrame
            how: 合并方式（'left', 'right', 'outer', 'inner'，默认'inner'）
            on: 合并依据的列名
            left_on: 左表合并列
            right_on: 右表合并列
            left_index: 是否使用左表索引（默认False）
            right_index: 是否使用右表索引（默认False）
            sort: 是否排序结果（默认False）
            suffixes: 重复列后缀（默认('_x', '_y')）
            copy: 是否复制数据（默认None）
            indicator: 是否添加合并指示列（默认False）
            validate: 合并验证（默认None）

        Returns:
            IndFrame: 合并后的结果

        Note:
            返回的数据长度取决于合并方式和键匹配情况：
            - 'inner': 仅保留两个表中都存在的键，长度可能减少
            - 'left': 保留左表所有行，右表无匹配则为NaN，长度等于左表
            - 'right': 保留右表所有行，左表无匹配则为NaN，长度等于右表
            - 'outer': 保留所有行，无匹配则为NaN，长度可能增加

        Examples:
            >>> # 创建完全匹配的DataFrame，确保合并后长度相同
            >>> df1 = IndFrame({'key': ['a', 'b', 'c'], 'A': [1, 2, 3]})
            >>> df2 = IndFrame({'key': ['a', 'b', 'c'], 'B': [4, 5, 6]})
            >>> # 内连接：只保留两个表都有的键，这里完全匹配所以长度不变
            >>> result = df1.merge(df2, on='key', how='inner')
            >>> print(result)
              key  A  B
            0   a  1  4
            1   b  2  5
            2   c  3  6
            >>> print(f"左表长度: {len(df1)}, 右表长度: {len(df2)}, 合并后长度: {len(result)}")
            左表长度: 3, 右表长度: 3, 合并后长度: 3
        """
        ...

    def round(self, decimals: int | dict[IndexLabel, int] | pd.Series = 0, *args, **kwargs) -> IndFrame:
        """## 对DataFrame元素按指定小数位数四舍五入

        - 对数值型数据进行四舍五入处理。

        Args:
            decimals: 保留的小数位数（默认0，即取整）
            *args: 其他位置参数
            **kwargs: 框架扩展参数

        Returns:
            IndFrame: 元素为四舍五入后的值

        Examples:
            >>> df = IndFrame({'A': [1.234, 2.567], 'B': [3.891, 4.123]})
            >>> result = df.round(1)
            >>> print(result)
                 A    B
            0  1.2  3.9
            1  2.6  4.1
        """
        ...

    # ----------------------------------------------------------------------
    # Statistical methods, etc.
    # corr
    # cov
    # corrwith

    # ----------------------------------------------------------------------
    # ndarray-like stats methods
    # 以下操作axis !=0返回的是内置数据
    def count(self, axis: Axis = 0, numeric_only: bool = False, **kwargs) -> IndSeries:
        """## 计算非空值数量

        - 统计每行或每列的非空值数量。

        Args:
            axis: 统计轴（0按列统计，1按行统计，默认0）
            numeric_only: 是否仅统计数值列（默认False）

        Returns:
            IndSeries: 非空值数量统计结果

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> import numpy as np
            >>> df = IndFrame({'A': [1, np.nan, 3], 'B': [4, 5, np.nan]})
            >>> result = df.count()
            >>> print(result)
            A    2
            B    2
            dtype: int64
        """
        ...

    def any(  # type: ignore[override]
        self,
        *,
        axis: Axis | None = 0,
        bool_only: bool = False,
        skipna: bool = True,
        **kwargs,
    ) -> IndSeries | bool:
        """## 判断是否存在True值

        - 检查每行或每列是否存在至少一个True值。

        Args:
            axis: 检查轴（0按列检查，1按行检查，默认0）
            bool_only: 是否仅检查布尔列（默认False）
            skipna: 是否跳过缺失值（默认True）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries | bool: 检查结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [True, False], 'B': [False, True]})
            >>> result = df.any()
            >>> print(result)
            A     True
            B     True
            dtype: bool
        """
        ...

    def all(
        self,
        axis: Axis | None = 0,
        bool_only: bool = False,
        skipna: bool = True,
        **kwargs,
    ) -> IndSeries | bool:
        """## 判断是否全部为True值

        - 检查每行或每列是否全部为True值。

        Args:
            axis: 检查轴（0按列检查，1按行检查，默认0）
            bool_only: 是否仅检查布尔列（默认False）
            skipna: 是否跳过缺失值（默认True）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries | bool: 检查结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [True, True], 'B': [True, False]})
            >>> result = df.all()
            >>> print(result)
            A     True
            B    False
            dtype: bool
        """
        ...

    def min(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算最小值

        - 计算每行或每列的最小值。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 最小值结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 5], 'B': [3, 2]})
            >>> result = df.min()
            >>> print(result)
            A    1
            B    2
            dtype: int64
        """
        ...

    def max(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算最大值

        - 计算每行或每列的最大值。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 最大值结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 5], 'B': [3, 2]})
            >>> result = df.max()
            >>> print(result)
            A    5
            B    3
            dtype: int64
        """
        ...

    def sum(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        min_count: int = 0,
        **kwargs,
    ) -> IndSeries:
        """## 计算和

        - 计算每行或每列的和。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            min_count: 非空值最小数量（默认0）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 和的结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.sum()
            >>> print(result)
            A     6
            B    15
            dtype: int64
        """
        ...

    def prod(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        min_count: int = 0,
        **kwargs,
    ) -> IndSeries:
        """## 计算乘积

        - 计算每行或每列的乘积。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            min_count: 非空值最小数量（默认0）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 乘积结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.prod()
            >>> print(result)
            A      6
            B    120
            dtype: int64
        """
        ...

    product = prod  # NDFrame

    def mean(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算平均值

        - 计算每行或每列的平均值。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 平均值结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.mean()
            >>> print(result)
            A    2.0
            B    5.0
            dtype: float64
        """
        ...

    def median(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算中位数

        - 计算每行或每列的中位数。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 中位数结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.median()
            >>> print(result)
            A    2.0
            B    5.0
            dtype: float64
        """
        ...

    def sem(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        ddof: int = 1,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算标准误差

        - 计算每行或每列的标准误差。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            ddof: 自由度 Delta（默认1）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 标准误差结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.sem()
            >>> print(result)
            A    0.57735
            B    0.57735
            dtype: float64
        """
        ...

    def var(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        ddof: int = 1,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算方差

        - 计算每行或每列的方差。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            ddof: 自由度 Delta（默认1）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 方差结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.var()
            >>> print(result)
            A    1.0
            B    1.0
            dtype: float64
        """
        ...

    def std(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        ddof: int = 1,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算标准差

        - 计算每行或每列的标准差。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            ddof: 自由度 Delta（默认1）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 标准差结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.std()
            >>> print(result)
            A    1.0
            B    1.0
            dtype: float64
        """
        ...

    def skew(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算偏度

        - 计算每行或每列的偏度（数据分布不对称性的度量）。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries : 偏度结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3, 4, 5], 'B': [1, 1, 2, 2, 5]})
            >>> result = df.skew()
            >>> print(result)
            A    0.0
            B    1.735582
            dtype: float64
        """
        ...

    def kurt(
        self,
        axis: Axis | None = 0,
        skipna: bool = True,
        numeric_only: bool = False,
        **kwargs,
    ) -> IndSeries:
        """## 计算峰度

        - 计算每行或每列的峰度（数据分布尖锐程度的度量）。

        Args:
            axis: 计算轴（0按列计算，1按行计算，默认0）
            skipna: 是否跳过缺失值（默认True）
            numeric_only: 是否仅计算数值列（默认False）
            **kwargs: 框架扩展参数

        Returns:
            IndSeries Any: 峰度结果，当axis=None时返回标量

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3, 4, 5], 'B': [1, 1, 2, 2, 5]})
            >>> result = df.kurt()
            >>> print(result)
            A   -1.2
            B    3.251029
            dtype: float64
        """
        ...

    kurtosis = kurt
    product = prod

    def cummin(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndFrame | pd.DataFrame:
        """## 计算DataFrame数据的累计最小值

        - 常用于计算时序数据的谷值。

        Args:
            axis: 累计轴（0按列累计，1按行累计，None按所有元素累计，默认None）
            skipna: 是否跳过缺失值（True跳过，False缺失值参与累计仍为NaN，默认True）
            *args: 其他位置参数
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | pd.DataFrame: 存储累计最小值结果

        ## NOTE
            按列统计时返回的是pd.DataFrame

        Examples:
            >>> df = IndFrame({'price': [10, 8, 12, 9]})
            >>> result = df.cummin()
            >>> print(result)
               price
            0     10
            1      8
            2      8
            3      8
        """
        ...

    def cummax(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndFrame | pd.DataFrame:
        """## 计算DataFrame数据的累计最大值

        - 常用于计算时序数据的峰值。

        Args:
            axis: 累计轴（0按列累计，1按行累计，None按所有元素累计，默认None）
            skipna: 是否跳过缺失值（True跳过，False缺失值参与累计仍为NaN，默认True）
            *args: 其他位置参数
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | pd.DataFrame: 存储累计最大值结果

        ## NOTE
            按列统计时返回的是pd.DataFrame

        Examples:
            >>> df = IndFrame({'price': [10, 12, 8, 15]})
            >>> result = df.cummax()
            >>> print(result)
               price
            0     10
            1     12
            2     12
            3     15
        """
        ...

    def cumsum(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndFrame | pd.DataFrame:
        """## 计算DataFrame数据的累计和

        - 常用于计算累计收益、累计成交量等。

        Args:
            axis: 累计轴（0按列累计，1按行累计，None按所有元素累计，默认None）
            skipna: 是否跳过缺失值（True跳过，False缺失值参与累计仍为NaN，默认True）
            *args: 其他位置参数
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | pd.DataFrame: 存储累计和结果

        ## NOTE
            按列统计时返回的是pd.DataFrame

        Examples:
            >>> df = IndFrame({'returns': [0.01, 0.02, -0.01, 0.03]})
            >>> result = df.cumsum()
            >>> print(result)
               returns
            0     0.01
            1     0.03
            2     0.02
            3     0.05
        """
        ...

    def cumprod(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndFrame | pd.DataFrame:
        """## 计算DataFrame数据的累计积

        - 常用于计算复利收益。

        Args:
            axis: 累计轴（0按列累计，1按行累计，None按所有元素累计，默认None）
            skipna: 是否跳过缺失值（True跳过，False缺失值参与累计仍为NaN，默认True）
            *args: 其他位置参数
            **kwargs: 框架扩展参数

        Returns:
            IndFrame | pd.DataFrame: 存储累计积结果

        ## NOTE
            按列统计时返回的是pd.DataFrame

        Examples:
            >>> df = IndFrame({'returns': [1.01, 1.02, 0.99, 1.03]})
            >>> result = df.cumprod()
            >>> print(result)
               returns
            0    1.0100
            1    1.0302
            2    1.0199
            3    1.0505
        """
        ...

    def nunique(self, axis: Axis = 0, dropna: bool = True) -> IndSeries:
        """## 计算唯一值数量

        - 统计每行或每列的唯一值数量。

        Args:
            axis: 统计轴（0按列统计，1按行统计，默认0）
            dropna: 是否排除缺失值（默认True）

        Returns:
            IndSeries: 唯一值数量统计结果

        ## NOTE
            按列统计时返回的是pd.Series

        Examples:
            >>> df = IndFrame({'A': [1, 2, 1], 'B': [3, 3, 4]})
            >>> result = df.nunique()
            >>> print(result)
            A    2
            B    2
            dtype: int64
        """
        ...

    # def idxmin(
    #     self, axis: Axis = 0, skipna: bool = True, numeric_only: bool = False
    # ) -> IndSeries:
    #     """## 查找最小值所在位置

    #     返回每行或每列最小值所在的索引位置。

    #     Args:
    #         axis: 查找轴（0按列查找，1按行查找，默认0）
    #         skipna: 是否跳过缺失值（默认True）
    #         numeric_only: 是否仅查找数值列（默认False）

    #     Returns:
    #         IndSeries: 最小值索引位置

    #     Examples:
    #         >>> df = IndFrame({'A': [1, 5, 3], 'B': [4, 2, 6]})
    #         >>> result = df.idxmin()
    #         >>> print(result)
    #         A    0
    #         B    1
    #         dtype: int64
    #     """
    #     ...

    # def idxmax(
    #     self, axis: Axis = 0, skipna: bool = True, numeric_only: bool = False
    # ) -> IndSeries:
    #     """## 查找最大值所在位置

    #     返回每行或每列最大值所在的索引位置。

    #     Args:
    #         axis: 查找轴（0按列查找，1按行查找，默认0）
    #         skipna: 是否跳过缺失值（默认True）
    #         numeric_only: 是否仅查找数值列（默认False）

    #     Returns:
    #         IndSeries: 最大值索引位置

    #     Examples:
    #         >>> df = IndFrame({'A': [1, 5, 3], 'B': [4, 2, 6]})
    #         >>> result = df.idxmax()
    #         >>> print(result)
    #         A    1
    #         B    2
    #         dtype: int64
    #     """
    #     ...

    # def mode(
    #     self, axis: Axis = 0, numeric_only: bool = False, dropna: bool = True
    # ) -> IndFrame:
    #     """## 计算众数

    #     计算每行或每列的众数（出现频率最高的值）。

    #     Args:
    #         axis: 计算轴（0按列计算，1按行计算，默认0）
    #         numeric_only: 是否仅计算数值列（默认False）
    #         dropna: 是否排除缺失值（默认True）

    #     Returns:
    #         IndFrame: 众数结果

    #     Example:
    #         >>> df = IndFrame({'A': [1, 2, 1], 'B': [3, 3, 4]})
    #         >>> result = df.mode()
    #         >>> print(result)
    #            A    B
    #         0  1  3.0
    #     """
    #     ...
    # quantile
    # to_timestamp
    # to_period

    def isin(self, values: pd.Series | pd.DataFrame | Sequence | Mapping, **kwargs) -> IndFrame:
        """## 判断DataFrame中的每个元素是否包含在指定值集合中

        - 返回与原始DataFrame相同形状的布尔值DataFrame。

        Args:
            values: 要检查的值集合，可以是：
                   - 可迭代对象（列表、元组等）
                   - Series（按索引匹配）
                   - DataFrame（按索引和列名匹配）
                   - 字典（键为列名，值为该列要检查的值列表）
            **kwargs: 框架扩展参数

        Returns:
            IndFrame: 与输入数据相同长度，存储布尔值结果

        Examples:
            >>> df = IndFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
            >>> result = df.isin([1, 4, 5])
            >>> print(result)
                   A      B
            0   True   True
            1  False   True
            2  False  False
        """
        ...


class PandasSeries(BtNDFrame, pd.Series):
    """
    量化框架自定义的Series增强类，继承自pandas原生pd.Series
    核心功能：
    1. 重载 pandas 所有常用运算符（比较、算术、反向算术、原地算术），确保运算结果自动转为框架自定义的IndSeries类型
    2. 重写 pandas 核心数据处理方法（数值计算、缺失值填充、数据转换等），保持原生功能逻辑的同时，
       通过 self._pandas_object_method 或 inplace_values 适配框架内数据类型，兼容后续指标计算、可视化等扩展能力
    3. 支持 inplace 参数控制是否修改原对象，统一返回框架自定义的IndSeries类型（或None），适配量化回测的时序数据处理流程
    """
    ######################## Series ############################
    # ----------------------------------------------------------------------
    # dtype
    # dtypes
    # name
    # values
    # array
    # ops----------------------------------------------------------------------
    # ravel
    # view
    # indexers----------------------------------------------------------------------
    # axes
    # Unsorted----------------------------------------------------------------------
    # repeat

    def reset_index(
        self,
        level: IndexLabel | None = None,
        *,
        drop: bool = False,
        name: Hashable = lib.no_default,
        inplace: bool = False,
        allow_duplicates: bool = False,
        **kwargs
    ) -> IndFrame | IndSeries:
        """## 重置Series索引

        - 将索引重置为默认整数索引，原索引转为列。

        Args:
            level: 指定重置的索引层级（多层索引时使用，默认None重置所有）
            drop: 是否丢弃原索引（True丢弃，False保留为列，默认False）
            name: 重置后Series的名称（默认None）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            allow_duplicates: 是否允许重复列名（默认False）
            **kwargs: 其他关键字参数

        Returns:
            IndFrame | IndSeries : drop=False时返回DataFrame，drop=True时返回Series

        Examples:
            >>> s = IndSeries([1, 2, 3], index=['a', 'b', 'c'])
            >>> result = s.reset_index()
            >>> print(result)
               index  0
            0     a  1
            1     b  2
            2     c  3
        """
        ...
    # Rendering Methods----------------------------------------------------------------------
    # to_string
    # to_markdown
    # items
    # keys
    # to_dict

    def to_frame(self, name: Hashable = None, **kwargs) -> IndFrame:
        """## 将Series转换为DataFrame

        - 将Series转换为单列的DataFrame。

        Args:
            name: 新DataFrame的列名（默认None使用原Series名称）
            **kwargs: 其他关键字参数

        Returns:
            IndFrame: 转换后的DataFrame

        Examples:
            >>> s = IndSeries([1, 2, 3], name='values')
            >>> df = s.to_frame()
            >>> print(df)
               values
            0       1
            1       2
            2       3
        """
        ...
    # groupby
    # Statistics, overridden ndarray methods-------------------------------------------------------
    # count
    # mode
    # unique
    # drop_duplicates

    def duplicated(self, keep: DropKeep = "first", **kwargs) -> IndSeries:
        """## 检测重复值

        - 标记Series中的重复值。

        Args:
            keep: 重复值标记策略（'first'标记第一个后的重复，'last'标记最后一个前的重复，False标记所有重复，默认'first'）
            **kwargs: 其他关键字参数

        Returns:
            IndSeries: 布尔Series，重复值为True

        Examples:
            >>> s = IndSeries([1, 2, 2, 3, 1])
            >>> result = s.duplicated()
            >>> print(result)
            0    False
            1    False
            2     True
            3    False
            4     True
            dtype: bool
        """
        ...
    # idxmin
    # idxmax

    def round(self, decimals: int = 0, *args, **kwargs) -> IndSeries:
        """## 对Series值进行四舍五入

        - 将Series中的数值四舍五入到指定小数位数。

        Args:
            decimals: 小数位数（默认0，即取整）
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            IndSeries: 四舍五入后的Series

        Examples:
            >>> s = IndSeries([1.234, 2.567, 3.891])
            >>> result = s.round(2)
            >>> print(result)
            0    1.23
            1    2.57
            2    3.89
            dtype: float64
        """
        ...
    # quantile
    # corr
    # cov

    def diff(self, periods: int = 1) -> IndSeries:
        """## 计算元素之间的差值

        - 计算Series中元素与前一个元素的差值，常用于计算时序数据的变化量。

        Args:
            periods: 差值计算的间隔周期（默认1，即相邻元素差值）

        Returns:
            IndSeries: 差值计算结果

        Examples:
            >>> s = IndSeries([10, 15, 13, 18])
            >>> result = s.diff()
            >>> print(result)
            0    NaN
            1    5.0
            2   -2.0
            3    5.0
            dtype: float64
        """
        ...
    # autocorr
    # dot
    # searchsorted
    # compare
    # combine
    # combine_first

    def update(self, other: pd.Series | Sequence | Mapping) -> None:
        """## 使用另一个Series的值更新当前Series

        - 使用other中的非空值替换当前Series的对应值，原地修改。

        Args:
            other: 用于更新的数据（Series、序列或映射）

        Returns:
            None: 原地修改，无返回值

        Examples:
            >>> s1 = IndSeries([1, 2, 3, 4])
            >>> s2 = pd.Series([10, 20], index=[1, 2])
            >>> s1.update(s2)
            >>> print(s1)
            0     1
            1    10
            2    20
            3     4
            dtype: int64
        """
        ...

    def sort_values(
        self,
        *,
        axis: Axis = 0,
        ascending: bool | Sequence[bool] = True,
        inplace: bool = False,
        kind: SortKind = "quicksort",
        na_position: NaPosition = "last",
        ignore_index: bool = False,
        key: ValueKeyFunc | None = None,
    ) -> IndSeries | None:
        """## 按值排序Series

        - 对Series的值进行排序，支持升序降序和缺失值位置控制。

        Args:
            axis: 排序轴（Series固定为0）
            ascending: 是否升序排序（True升序，False降序，默认True）
            inplace: 是否修改原对象（True修改原对象，False返回新对象，默认False）
            kind: 排序算法（'quicksort'快速排序，'mergesort'归并排序，'heapsort'堆排序，默认'quicksort'）
            na_position: 缺失值位置（'first'放在开头，'last'放在末尾，默认'last'）
            ignore_index: 是否重置索引（True重置为0-based索引，默认False）
            key: 排序前应用于值的函数（默认None）

        Returns:
            IndSeries | None: inplace=False时返回排序后的新对象，否则返回None

        Examples:
            >>> s = IndSeries([3, 1, 4, 2])
            >>> result = s.sort_values()
            >>> print(result)
            1    1
            3    2
            0    3
            2    4
            dtype: int64
        """
        ...

    def sort_index(
        self,
        *,
        axis: Axis = 0,
        level: IndexLabel | None = None,
        ascending: bool | Sequence[bool] = True,
        inplace: bool = False,
        kind: SortKind = "quicksort",
        na_position: NaPosition = "last",
        sort_remaining: bool = True,
        ignore_index: bool = False,
        key: IndexKeyFunc | None = None,
    ) -> IndSeries | None:
        """## 按索引排序Series

        - 对Series的索引进行排序，支持多层索引排序。

        Args:
            axis: 排序轴（Series固定为0）
            level: 多层索引时指定排序层级（默认None）
            ascending: 是否升序排序（默认True）
            inplace: 是否修改原对象（默认False）
            kind: 排序算法（默认'quicksort'）
            na_position: 缺失值位置（默认'last'）
            sort_remaining: 多层索引时是否排序剩余层级（默认True）
            ignore_index: 是否重置索引（默认False）
            key: 排序前应用于索引的函数（默认None）

        Returns:
            IndSeries | None: inplace=False时返回排序后的新对象，否则返回None

        Examples:
            >>> s = IndSeries([1, 2, 3], index=['c', 'a', 'b'])
            >>> result = s.sort_index()
            >>> print(result)
            a    2
            b    3
            c    1
            dtype: int64
        """
        ...

    def argsort(
        self,
        axis: Axis = 0,
        kind: SortKind = "quicksort",
        order: None = None,
        stable: None = None,
    ) -> IndSeries:
        """## 返回排序后的索引位置

        - 返回将Series值排序后的索引位置数组。

        Args:
            axis: 排序轴（Series固定为0）
            kind: 排序算法（默认'quicksort'）
            order: 保留参数（默认None）
            stable: 是否使用稳定排序（默认None）

        Returns:
            IndSeries: 排序后的索引位置

        Examples:
            >>> s = IndSeries([30, 10, 20])
            >>> result = s.argsort()
            >>> print(result)
            1    1
            2    2
            0    0
            dtype: int64
        """
        ...
    # nlargest
    # nsmallest
    # swaplevel
    # reorder_levels
    # explode
    # unstack
    # function application----------------------------------------------------------------

    def map(
        self,
        arg: Callable | Mapping | pd.Series | None = None,
        na_action: Literal["ignore"] | None = None,
        engine: Callable | None = None,
        **kwargs,
    ) -> IndSeries:
        """## 映射Series值

        - 根据映射函数、字典或Series替换Series中的值。

        Args:
            arg: 映射关系（函数、字典或Series）
            na_action: 对缺失值的处理（'ignore'忽略缺失值，默认None）
            engine: 执行引擎（如numba.jit，默认None）

        Returns:
            IndSeries: 映射后的Series

        Examples:
            >>> s = IndSeries(['a', 'b', 'c'])
            >>> result = s.map({'a': 1, 'b': 2, 'c': 3})
            >>> print(result)
            0    1
            1    2
            2    3
            dtype: int64
        """
        ...
    # aggregate
    # agg
    # transform

    def apply(
        self,
        func: AggFuncType,
        args: tuple[Any, ...] = (),
        *,
        by_row: Literal[False, "compat"] = "compat",
        **kwargs,
    ) -> IndFrame | IndSeries:
        """## 对Series应用函数

        - 将函数应用于Series的每个元素，支持复杂的数据转换。

        Args:
            func: 应用的函数
            args: 传递给函数的额外位置参数
            by_row: 逐行应用模式（默认'compat'）
            **kwargs: 传递给函数的额外关键字参数

        Returns:
            IndFrame | IndSeries: 函数应用结果

        Examples:
            >>> s = IndSeries([1, 2, 3])
            >>> result = s.apply(lambda x: x * 2)
            >>> print(result)
            0    2
            1    4
            2    6
            dtype: int64
        """
        ...

    # rename
    # set_axis
    # reindex
    # rename_axis
    # drop
    # pop
    # info
    # memory_usage
    def isin(self, values) -> IndSeries:
        """## 检查Series值是否在给定集合中

        - 返回布尔Series，表示每个元素是否在给定的值集合中。

        Args:
            values: 要检查的值集合（列表、元组、Series等）

        Returns:
            IndSeries: 布尔Series，存在为True

        Examples:
            >>> s = IndSeries([1, 2, 3, 4, 5])
            >>> result = s.isin([2, 4])
            >>> print(result)
            0    False
            1     True
            2    False
            3     True
            4    False
            dtype: bool
        """
        ...

    def between(
        self,
        left,
        right,
        inclusive: Literal["both", "neither", "left", "right"] = "both",
    ) -> IndSeries:
        """## 检查值是否在给定范围内

        - 返回布尔Series，表示每个元素是否在指定的左右边界范围内。

        Args:
            left: 左边界值
            right: 右边界值
            inclusive: 边界包含方式（'both'包含两边，'neither'不包含，'left'只包含左，'right'只包含右，默认'both'）

        Returns:
            IndSeries: 布尔Series，在范围内为True

        Examples:
            >>> s = IndSeries([1, 2, 3, 4, 5])
            >>> result = s.between(2, 4)
            >>> print(result)
            0    False
            1     True
            2     True
            3     True
            4    False
            dtype: bool
        """
        ...

    def case_when(
        self,
        caselist: list[
            tuple[
                ArrayLike | Callable[[pd.Series],
                                     pd.Series | np.ndarray | Sequence[bool]],
                ArrayLike | Scalar | Callable[[
                    pd.Series], pd.Series | np.ndarray],
            ],
        ],
    ) -> IndSeries:
        """## 多条件值替换

        - 根据条件列表进行多分支的值替换，类似于SQL的CASE WHEN语句。

        Args:
            caselist: 条件-值对列表，每个元组包含（条件，替换值）

        Returns:
            IndSeries: 替换后的Series

        Examples:
            >>> s = IndSeries([1, 2, 3, 4, 5])
            >>> result = s.case_when([
            ...     (s < 2, 'low'),
            ...     (s < 4, 'medium'), 
            ...     (s >= 4, 'high')
            ... ])
            >>> print(result)
            0      low
            1   medium
            2   medium
            3     high
            4     high
            dtype: object
        """
        ...

    def isna(self) -> IndSeries:
        """## 检测缺失值

        - 返回布尔Series，标记所有缺失值（NaN、NaT、None等）。

        Returns:
            IndSeries: 布尔Series，缺失值为True

        Examples:
            >>> s = IndSeries([1, np.nan, 3, None])
            >>> result = s.isna()
            >>> print(result)
            0    False
            1     True
            2    False
            3     True
            dtype: bool
        """
        ...

    def isnull(self) -> IndSeries:
        """## 检测空值（isna的别名）

        - 与isna功能相同，检测Series中的缺失值。

        Returns:
            IndSeries: 布尔Series，空值为True

        Examples:
            >>> s = IndSeries([1, np.nan, 3, None])
            >>> result = s.isnull()
            >>> print(result)
            0    False
            1     True
            2    False
            3     True
            dtype: bool
        """
        ...

    def notna(self) -> IndSeries:
        """## 检测非缺失值

        - 返回布尔Series，标记所有非缺失值。

        Returns:
            IndSeries: 布尔Series，非缺失值为True

        Examples:
            >>> s = IndSeries([1, np.nan, 3, None])
            >>> result = s.notna()
            >>> print(result)
            0     True
            1    False
            2     True
            3    False
            dtype: bool
        """
        ...

    def notnull(self) -> IndSeries:
        """## 检测非空值（notna的别名）

        - 与notna功能相同，检测Series中的非缺失值。

        Returns:
            IndSeries: 布尔Series，非空值为True

        Examples:
            >>> s = IndSeries([1, np.nan, 3, None])
            >>> result = s.notnull()
            >>> print(result)
            0     True
            1    False
            2     True
            3    False
            dtype: bool
        """
        ...
    # dropna
    # Time IndSeries-oriented methods---------------------------------------------------------------
    # to_timestamp
    # to_period
    # index
    # str = CachedAccessor("str", StringMethods)
    # dt = CachedAccessor("dt", CombinedDatetimelikeProperties)
    # cat = CachedAccessor("cat", CategoricalAccessor)
    # plot = CachedAccessor("plot", pandas.plotting.PlotAccessor)
    # sparse = CachedAccessor("sparse", SparseAccessor)
    # struct = CachedAccessor("struct", StructAccessor)
    # list = CachedAccessor("list", ListAccessor)
    # ----------------------------------------------------------------------
    # Add plotting methods to Series
    # hist = pandas.plotting.hist_IndSeries
    # Reductions-----------------------------------------------------------------
    # any
    # all
    # min
    # max
    # sum
    # prod
    # mean
    # median
    # sem
    # var
    # std
    # skew
    # kurt
    # kurtosis = kurt
    # product = prod

    def cummin(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndSeries:
        """## 计算累积最小值

        - 返回Series的累积最小值。

        Args:
            axis: 计算轴（Series固定为0）
            skipna: 是否跳过缺失值（默认True）
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            IndSeries: 累积最小值序列

        Examples:
            >>> s = IndSeries([3, 1, 4, 2])
            >>> result = s.cummin()
            >>> print(result)
            0    3
            1    1
            2    1
            3    1
            dtype: int64
        """
        ...

    def cummax(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndSeries:
        """## 计算累积最大值

        - 返回Series的累积最大值。

        Args:
            axis: 计算轴（Series固定为0）
            skipna: 是否跳过缺失值（默认True）
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            IndSeries: 累积最大值序列

        Examples:
            >>> s = IndSeries([3, 1, 4, 2])
            >>> result = s.cummax()
            >>> print(result)
            0    3
            1    3
            2    4
            3    4
            dtype: int64
        """
        ...

    def cumsum(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndSeries:
        """## 计算累积和

        - 返回Series的累积求和。

        Args:
            axis: 计算轴（Series固定为0）
            skipna: 是否跳过缺失值（默认True）
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            IndSeries: 累积和序列

        Examples:
            >>> s = IndSeries([1, 2, 3, 4])
            >>> result = s.cumsum()
            >>> print(result)
            0     1
            1     3
            2     6
            3    10
            dtype: int64
        """
        ...

    def cumprod(self, axis: Axis | None = None, skipna: bool = True, *args, **kwargs) -> IndSeries:
        """## 计算累积乘积

        - 返回Series的累积乘积。

        Args:
            axis: 计算轴（Series固定为0）
            skipna: 是否跳过缺失值（默认True）
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            IndSeries: 累积乘积序列

        Examples:
            >>> s = IndSeries([1, 2, 3, 4])
            >>> result = s.cumprod()
            >>> print(result)
            0     1
            1     2
            2     6
            3    24
            dtype: int64
        """
        ...

    # def shift(
    #         self,
    #         periods: int | Sequence[int] = 1,
    #         freq: Frequency | None = None,
    #         axis: Axis = 0,
    #         fill_value: Hashable = None,
    #         suffix: str | None = None,
    #         **kwargs) -> IndSeries:
    #     """## 将IndSeries数据按指定步长移动

    #     - 常用于计算时序数据的滞后/超前值，支持时间频率移动。

    #     Args:
    #         periods: 移动步长（正数向下/向右移，负数向上/向左移，默认1）
    #         freq: 时间序列的频率（仅index为DatetimeIndex时有效，默认None）
    #         axis: 移动轴（0按行移动，1按列移动，默认0）
    #         fill_value: 移动后空值的填充值（默认None表示用NaN填充）
    #         suffix: 移动后列名后缀（默认None）
    #         **kwargs: 框架扩展参数

    #     Returns:
    #         IndSeries: 存储移动后的数据

    #     Examples:
    #         >>> df = IndSeries([10, 20, 30, 40])
    #         >>> result = df.shift(periods=1)
    #         >>> print(result)
    #         0    NaN
    #         1   10.0
    #         2   20.0
    #         3   30.0
    #     """
    #     ...


def get_pandas_explicit_methods(cls) -> set[str]:
    """
    ## 动态提取类显式定义的公共方法（非继承、非下划线开头、可调用、非属性）
    - 自动适配类新增的方法，无需手动维护列表
    """
    explicit_methods = set()

    # 遍历类自身定义的成员（__dict__ 仅包含类自己定义的，不包含继承的）
    for name in cls.__dict__:
        # 1. 排除以下划线开头的方法（私有/保护方法）
        if name.startswith('_'):
            continue

        # 2. 获取成员对象（可能是方法、属性、常量等）
        member = cls.__dict__[name]

        # 3. 排除属性（仅保留可调用的方法）
        if isinstance(member, property):
            continue

        # 4. 确保是可调用的方法（排除类变量、常量等）
        if callable(member):
            explicit_methods.add(name)

    return explicit_methods


# 用于转换为框架内指标
pandas_method = get_pandas_explicit_methods(PandasSeries) | get_pandas_explicit_methods(
    PandasDataFrame) | get_pandas_explicit_methods(BtNDFrame)
rolling_method = get_pandas_explicit_methods(Rolling)
ewm_method = get_pandas_explicit_methods(ExponentialMovingWindow)
expanding_method = get_pandas_explicit_methods(Expanding)

