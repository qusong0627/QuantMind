from __future__ import annotations
import re
from ast import arg
from typing import TYPE_CHECKING
from .ta import *
from pandas.api.types import is_datetime64_any_dtype
from .ta_ import PairTrading, Factors
if TYPE_CHECKING:
    import tulipy
    import talib as Talib
    
    from finta import TA as FinTa
    class BtObject(object):
        _df: pd.DataFrame | pd.Series | None  # pyright: ignore[reportUninitializedInstanceVariable]
        pandas_object: pd.DataFrame | pd.Series | None  # pyright: ignore[reportUninitializedInstanceVariable]
        

def is_bt_ind_type(obj):
    """判断对象是否为BtIndType包含的类型"""
    type_name = type(obj).__name__
    return type_name in ['Line', 'IndFrame', 'IndSeries']


def get_factors_df(*factors):
    factors_df = pd.DataFrame()
    for factor in factors:
        if is_bt_ind_type(factor):
            lines = factor.lines
            if len(lines) == 1:
                factors_df[lines[0]] = factor.values
            else:
                factors_df = pd.concat([factors_df, factor], axis=1)
    return factors_df


def get_data(obj:BtObject | None =None) -> pd.Series | pd.DataFrame:
    # IndicatorClass->IndSeries,IndFrame
    if hasattr(obj, '_df'):
        obj = obj._df  # pyright: ignore[reportAssignmentType, reportOptionalMemberAccess]
    # BtIndType,KLineType->pd.Series,pd.DataFrame
    if hasattr(obj, '_df'):
        return obj._df  # pyright: ignore[reportReturnType, reportOptionalMemberAccess]
    elif hasattr(obj, 'pandas_object'):
        return obj.pandas_object  # pyright: ignore[reportReturnType, reportOptionalMemberAccess]
    else:
        return obj  # pyright: ignore[reportReturnType]


def get_tqta_dataframe(obj: pd.Series | pd.DataFrame, columns: list[Any], **kwargs) -> pd.DataFrame:
    data = get_data(obj)  # pyright: ignore[reportArgumentType]
    if len(data.shape) > 1:
        data_cols = data.columns
        for col in columns:
            if col in data_cols:
                continue
            # 覆盖
            if col in kwargs:
                data[col] = list(kwargs.pop(col))
        assert set(data_cols).issuperset(
            columns), f"传入数据必须包含以下数据字段：{columns},或通过**kwargs进行传输。"
        return data  # pyright: ignore[reportReturnType]
    else:
        return pd.DataFrame(list(kwargs.pop(columns[0], data)), columns=columns)


def _get_column_(self, *args) -> tuple[list[pd.Series], dict] | dict:  # pyright: ignore[reportMissingTypeArgument]
    """应用于pandas_ta"""
    local, *default_filed = args
    kwargs: dict = local.pop('kwargs', {})  # pyright: ignore[reportMissingTypeArgument]
    if 'open' in local:  # pandas_ta所有open参数都用open_
        local['open_'] = local.pop('open')
    if not default_filed:  # 没指定默认字段则返回
        return {**local, **kwargs}
    data = kwargs.get('data', get_data(self))
    isdataframe = len(data.shape) > 1
    if not isdataframe and 'filed' not in kwargs:
        filed = default_filed
    else:
        filed = kwargs.pop('filed', default_filed)
        if isinstance(filed, str):
            filed = [filed,]
        assert len(filed) == len(
            default_filed), f"filed字段{filed}长度与默认字段{default_filed}长度不一致"

    # 优化：直接遍历字典，避免使用map和lambda函数
    for key, value in local.items():
        local[key] = get_data(value)
    for key, value in kwargs.items():
        kwargs[key] = get_data(value)

    args = {}
    for f in filed:
        args[f] = kwargs.pop(f, data[f] if isdataframe else data)
    
    # 优化：直接构建返回列表，避免使用列表推导式
    result = []
    for k, v in args.items():
        if k == "datetime":
            result.append(v)
        else:
            result.append(v.astype(np.float64))
    return result, {**local, **kwargs}


def _ti_get_data(*args) -> tuple[list, dict]:  # pyright: ignore[reportMissingTypeArgument]
    values, kwargs = _get_column_(*args)
    values = [value.values.astype(np.float64) for value in values]
    return values, kwargs


def _get_data_(self, a) -> pd.Series:
    if isinstance(a, str):
        _a_frame = get_data(self)
        if len(_a_frame.shape) > 1:
            assert a in list(
                _a_frame.columns), f"{a}不在列表{list(_a_frame.columns)}中"
            a = _a_frame[a]
        else:
            a = _a_frame
    return a  # pyright: ignore[reportReturnType]


def _finta_get_data(self, **kwargs):
    if "filed" in kwargs:
        filed = kwargs.pop("filed")
        assert filed in FILED.OHLCV, f"{filed}字段不存在"
        data = self._df[:, filed].copy()
    else:
        data = kwargs.pop('close', self._df[FILED.OHLCV])
        data = data.copy()
    return data


@pd.api.extensions.register_series_accessor("ta")
# class SeriesIndicators(BasePandasObject, LazyImport):
class SeriesIndicators(LazyImport):
    _indicator_cache = {}
    _df = pd.Series()
    
    def __init__(self, pandas_obj):
        self._validate(pandas_obj)
        self._df = pandas_obj

    @staticmethod
    def _validate(obj: pd.Series):
        if not isinstance(obj, pd.Series):
            raise AttributeError(
                "[X] Must be either a Pandas Series.")
    
    def __getattr__(self, name):
        """优化：拦截指标方法调用，添加缓存机制"""
        if name in self._indicator_cache:
            return self._indicator_cache[name]
        else:# 检查CoreFunc是否有该方法
            if hasattr(CoreFunc, name):
                method = getattr(CoreFunc, name)
                # 缓存方法
                self._indicator_cache[name] = method
                # 返回缓存的方法
                return self._indicator_cache[name]
        
        # 其他属性和方法正常处理
        return super().__getattr__(name)  # pyright: ignore[reportUnknownMemberType]


def length(self) -> int:
    return len(self._df)


# 设置AnalysisIndicators类的__getattr__方法
setattr(AnalysisIndicators, "_indicator_cache",{})
setattr(AnalysisIndicators, '__getattr__', SeriesIndicators.__getattr__)

AnalysisIndicators.length = property(length)  # pyright: ignore[reportAttributeAccessIssue]
SeriesIndicators.length = property(length)  # pyright: ignore[reportAttributeAccessIssue]
setattr(AnalysisIndicators, '_get_column_', _get_column_)
setattr(SeriesIndicators, '_get_column_', _get_column_)
setattr(AnalysisIndicators, '_get_data_', _get_data_)
setattr(SeriesIndicators, '_get_data_', _get_data_)
setattr(AnalysisIndicators, '_finta_get_data', _finta_get_data)
setattr(SeriesIndicators, '_finta_get_data', _finta_get_data)
setattr(AnalysisIndicators, '_ti_get_data', _ti_get_data)
setattr(SeriesIndicators, '_ti_get_data', _ti_get_data)


class CoreFunc:
    """## 核心指标计算器

    - 该类重载并统一了来自多个技术分析库的指标计算方法，
    - 提供一致的用户体验和数据返回格式。

    ### 主要功能：
    1. **统一调用接口**：不同库的指标通过统一的前缀调用
    2. **自动数据适配**：自动处理输入数据的格式转换
    3. **批量计算支持**：支持对DataFrame的多列同时计算
    4. **错误处理**：内置错误处理和异常捕获机制

    ### 属性说明：
    - `length` : 返回数据的长度（样本数）
    - `_df` : 底层数据对象（pandas DataFrame或Series）

    ### 内部方法（用于数据准备）：
    - `_finta_get_data()` : 为FinTA库准备数据
    - `_get_column_()` : 获取指定列的数据
    - `_get_data_()` : 获取单列数据
    - `_ti_get_data()` : 为TuLip库准备数据

    ### 支持的指标库前缀：
    - `pta_` : PandasTA 指标 - 基于pandas的高性能技术分析库
    - `btind_` : BtInd 指标 - 回测专用指标库
    - `ti_` : TuLip 指标 - 传统技术分析指标库
    - `talib_` : TA-Lib 指标 - 行业标准技术分析库（C语言实现，速度快）
    - `finta_` : FinTA 指标 - 金融技术分析库
    - `tqfunc_` : TqFunc 指标 - 天勤量化函数库
    - `tqta_` : TqTa 指标 - 天勤技术分析库
    - `pair_` : Pair 指标 - 配对交易专用指标
    - `factor_` : Factors 指标 - 多因子模型指标
    """
    _df: pd.DataFrame | pd.Series  # pyright: ignore[reportUninitializedInstanceVariable]

    @property
    def length(self) -> int:
        ...

    def _finta_get_data(self, **kwargs):
        ...

    def _get_column_(self, *args) -> tuple[list[pd.Series] | dict] | dict:
        ...

    def _get_data_(self, a) -> pd.Series:
        ...

    def _ti_get_data(self) -> tuple[list, dict]:  # pyright: ignore[reportMissingTypeArgument]
        ...

    # 无标签指标
    def pta_cum(self, length=10, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return cum(*arg, **kwargs)  # pyright: ignore[reportUnknownArgumentType]

    def pta_ZeroDivision(self, b=1., dtype=np.float64, fill_value=0.0, handle_inf=True, **kwargs) -> ndarray:  # pyright: ignore[reportMissingTypeArgument]
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return ZeroDivision(*arg, **kwarg)  # pyright: ignore[reportUnknownVariableType]

    def pta_rolling_apply(self, func: Callable, window: int, prepend_nans: bool = True, n_jobs: int = 1, **kwargs) -> np.ndarray:  # pyright: ignore[reportMissingTypeArgument]
        return rolling_apply(self, func, window, prepend_nans, n_jobs, **kwargs)  # pyright: ignore[reportUnknownVariableType]

    def Linear_Regression_Candles(self, length=11, **kwargs):
        """# Linear_Regression_Candles
            Utilizing Linear Regression to Enhance Chart Interpretation in Trading
            In the world of trading, accurate interpretation of charts is paramount to making informed decisions. Among the plethora of tools and techniques available, linear regression stands out for its simplicity and efficacy.
            Linear regression, a fundamental statistical method, helps traders identify the underlying trend of a security’s price by fitting a straight line through the data points. This line, known as the regression line, represents the best estimate of the future price movement, providing a clearer picture of the trend’s direction, strength, and volatility.
            By reducing the noise in price data, linear regression makes it easier to spot trends and reversals, offering a solid foundation for both technical analysis and trading strategy development.
            Linear Regression Candles Indicator
            At its core, the indicator recalibrates standard candlestick data using linear regression, creating candles that better reflect the underlying trend’s direction.
            Its ability to filter out market noise and present a cleaner trend analysis can significantly enhance decision-making in various trading strategies, including:
            Trend Following: Traders can use the indicator to confirm the presence of a strong trend and enter trades in the direction of that trend.
            Reversal Detection: The crossover of candles with the signal line can indicate potential trend reversals, providing early entries for counter-trend strategies.
            Risk Management: By understanding the trend’s strength and direction, traders can set more informed stop-loss and take-profit levels, improving their risk-to-reward ratios.
            The Linear Regression Candles indicator exemplifies how statistical techniques like linear regression can be innovatively applied to enhance traditional trading tools.
            By offering a clearer view of the market trend and smoothing out price volatility, this indicator aids traders in navigating the complexities of the financial markets with greater confidence.
            Whether for trend identifica

            # Args:
                df (pd.DataFrame): K线数据
                length (int, optional): 长度. Defaults to 11.

            # Returns:
                >>> pd.DataFrame"""
        kwargs.update(dict(length=length))
        return Candles.Linear_Regression_Candles(get_data(self), **kwargs)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def Heikin_Ashi_Candles(self, length=0, **kwargs):
        """# Heikin_Ashi_Candles
            Normal candlestick charts are composed of a series of open-high-low-close (OHLC) candles
            set apart by a time series. The Heikin-Ashi technique shares some characteristics with
            standard candlestick charts but uses a modified formula of close-open-high-low (COHL):

            >>> Close=1/4 * (Open+High+Low+Close)(The average price of the current bar)
                Open=1/2 * (Open of Prev. Bar+Close of Prev. Bar)(The midpoint of the previous bar)
                High=Max[High, Open, Close]
                Low=Min[Low, Open, Close]

            # Args:
                >>> df (pd.DataFrame): K线数据
                    length (int, optional): 长度. Defaults to 0.

            # Returns:
                >>> pd.DataFrame
            """
        kwargs.update(dict(length=length))
        return Candles.Heikin_Ashi_Candles(get_data(self), **kwargs)  # pyright: ignore[reportUnknownMemberType]

    def pta_line_trhend(self, length=1, **kwargs):
        return line_trhend(self._df, length, **kwargs)

    def pta_abc(self, lim: float = 5., **kwargs):
        kwargs.update(dict(lim=lim))
        return abc(get_data(self), **kwargs)

    def pta_insidebar(self, length: int = 10, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return insidebar(*arg, **kwarg)

    def time_to_datetime(self):
        data = self._df
        if not isinstance(data, pd.DataFrame):
            return data
        try:
            cols = list(data.columns)
            datetime_list = [
                col for col in cols if is_datetime64_any_dtype(data[col])]
            if datetime_list:
                for dt in datetime_list:
                    data[dt] = data[dt].apply(time_to_datetime)
        except:
            ...
        return data
    # Public DataFrame Methods: Indicators and Utilities
    # pandas_ta指标
    # Candles

    def pta_cdl_pattern(self, name="all", scalar=None, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, _ = self._get_column_(locals(), *FILED.OHLC)
        result = pta.cdl_pattern(*arg, name=name, scalar=scalar, offset=offset)
        # 处理列名：去掉末尾参数数字后缀并转为小写，保留形态名称中的数字
        if isinstance(result, pd.DataFrame):
            result.columns = [re.sub(r'(?:_\d+(?:[._]\d+)*)+$', '', col).lower() for col in result.columns]
        return result

    def pta_cdl_z(self, length=30, full=False, ddof=1, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.cdl_z(*arg, **kwarg)

    def pta_ha(self, length=0, offset=None, **kwargs):
        """args : open, high, low, close"""
        # arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        # return pta.ha(*arg, **kwarg)
        df = get_data(self)
        length = length if length and length >= 0 else 0
        if length:
            df_ls = [df.shift(i).bfill()
                     if i else df for i in range(length+1)]
            _open = df_ls[-1].open
            _close = df.close
            _low = reduce(np.minimum, [data.low for data in df_ls])
            _high = reduce(np.maximum, [data.high for data in df_ls])
            dframe = pta.ha(_open, _high, _low, _close, offset, **kwargs)
        else:
            dframe = pta.ha(df.open, df.high, df.low,
                            df.close, offset, **kwargs)
        return dframe

    def pta_lrc(self, length=10, **kwargs):
        df = get_data(self)
        lr_open = pta.linreg(df.open, length)
        lr_high = pta.linreg(df.high, length)
        lr_low = pta.linreg(df.low, length)
        lr_close = pta.linreg(df.close, length)
        dframe = pd.DataFrame(
            dict(open=lr_open, high=lr_high, low=lr_low, close=lr_close))
        dframe.loc[:length, FILED.OHLC] = df.loc[:length, FILED.OHLC]
        dframe.category = 'candles'
        return dframe

    def pta_cdl_doji(self,
        length: int = 10, factor: int | float = 10,
        scalar: int | float = 100, asint: bool = True,
        offset: int = 0, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.cdl_doji(*arg, **kwarg)

    def pta_cdl_inside(
        self,
        asbool: bool = False, scalar: int | float = 100,
        offset: int = 0, **kwargs
    ):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.cdl_inside(*arg, **kwarg)
    
    def pta_cdl(self, name="all", scalar=None, offset=None, **kwargs) -> pd.DataFrame:
        """args : open, high, low, close"""
        arg, _ = self._get_column_(locals(), *FILED.OHLC)
        result = pta.cdl(*arg, name=name, scalar=scalar, offset=offset)
        # 处理列名：去掉末尾参数数字后缀并转为小写，保留形态名称中的数字
        if isinstance(result, pd.DataFrame):
            result.columns = [re.sub(r'(?:_\d+(?:[._]\d+)*)+$', '', col).lower() for col in result.columns]
        return result

    # Cycles
    def pta_ebsw(self, length=None, bars=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ebsw(*arg, **kwarg)

    def pta_reflex(
        self, length: int = 20,
        smooth: int = 20, alpha: int | float = 0.04,
        pi: int | float = 3.14159, sqrt2: int | float = 1.414,
        offset: int = 0, **kwargs
    ) ->pd.Series:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.reflex(*arg, **kwarg)

    # Momentum
    def pta_ao(self, fast=None, slow=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.ao(*arg, **kwarg)

    def pta_apo(self, fast=None, slow=None, mamode=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.apo(*arg, **kwarg)

    def pta_bias(self, length=None, mamode=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.bias(*arg, **kwarg)

    def pta_bop(self,  scalar=None, talib=None, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.bop(*arg, **kwarg)

    def pta_brar(self, length=None, scalar=None, drift=None, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.brar(*arg, **kwarg)

    def pta_cci(self, length=None, c=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.cci(*arg, **kwarg)

    def pta_cfo(self, length=None, scalar=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.cfo(*arg, **kwarg)

    def pta_cg(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.cg(*arg, **kwarg)

    def pta_cmo(self, length=None, scalar=None, talib=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.cmo(*arg, **kwarg)

    def pta_coppock(self, length=None, fast=None, slow=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.coppock(*arg, **kwarg)

    def pta_cti(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.cti(*arg, **kwarg)

    def pta_dm(self, length=None, mamode=None, talib=None, drift=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.dm(*arg, **kwarg)

    def pta_er(self, length=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.er(*arg, **kwarg)

    def pta_eri(self, length=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.eri(*arg, **kwarg)

    def pta_fisher(self, length=None, signal=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.fisher(*arg, **kwarg)

    def pta_inertia(self, length=None, rvi_length=None, scalar=None, refined=None, thirds=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : close, high, low"""
        arg, kwarg = self._get_column_(locals(), 'close', 'high', 'low')
        return pta.inertia(*arg, **kwarg)

    def pta_kdj(self, length=None, signal=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.kdj(*arg, **kwarg)

    def pta_kst(self, roc1=None, roc2=None, roc3=None, roc4=None, sma1=None, sma2=None, sma3=None, sma4=None, signal=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.kst(*arg, **kwarg)

    def pta_macd(self, fast=None, slow=None, signal=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.macd(*arg, **kwarg)

    def pta_mom(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.mom(*arg, **kwarg)

    def pta_pgo(self, length=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.pgo(*arg, **kwarg)

    def pta_ppo(self, fast=None, slow=None, signal=None, scalar=None, mamode=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ppo(*arg, **kwarg)

    def pta_psl(self, open_=None, length=None, scalar=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.psl(*arg, **kwarg)

    def pta_pvo(self, fast=None, slow=None, signal=None, scalar=None, offset=None, **kwargs):
        """args : volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.V)
        return pta.pvo(*arg, **kwarg)

    def pta_qqe(self, length=None, smooth=None, factor=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.qqe(*arg, **kwarg)

    def pta_roc(self, length=None, scalar=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.roc(*arg, **kwarg)

    def pta_rsi(self, length=None, scalar=None, talib=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.rsi(*arg, **kwarg)

    def pta_rsx(self, length=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.rsx(*arg, **kwarg)

    def pta_rvgi(self, length=None, swma_length=None, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.rvgi(*arg, **kwarg)

    def pta_slope(self, length=None, as_angle=None, to_degrees=None, vertical=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.slope(*arg, **kwarg)

    def pta_smi(self, fast=None, slow=None, signal=None, scalar=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.smi(*arg, **kwarg)

    def pta_squeeze(self, bb_length=None, bb_std=None, kc_length=None, kc_scalar=None, mom_length=None, mom_smooth=None, use_tr=None, mamode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.squeeze(*arg, **kwarg)

    def pta_squeeze_pro(self, bb_length=None, bb_std=None, kc_length=None, kc_scalar_wide=None, kc_scalar_normal=None, kc_scalar_narrow=None, mom_length=None, mom_smooth=None, use_tr=None, mamode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.squeeze_pro(*arg, **kwarg)

    def pta_stc(self, tclength=None, fast=None, slow=None, factor=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.stc(*arg, **kwarg)

    def pta_stoch(self, k=None, d=None, smooth_k=None, mamode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.stoch(*arg, **kwarg)

    def pta_stochrsi(self, length=None, rsi_length=None, k=None, d=None, mamode=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.stochrsi(*arg, **kwarg)

    # def pta_td_seq(self, asint=None, offset=None, show_all=None, **kwargs):
    #     """args : close"""
    #     arg, kwarg = self._get_column_(locals(), *FILED.C)
    #     return pta.td_seq(*arg, **kwarg)

    def pta_trix(self, length=None, signal=None, scalar=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.trix(*arg, **kwarg)

    def pta_tsi(self, fast=None, slow=None, signal=None, scalar=None, mamode=None, drift=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.tsi(*arg, **kwarg)

    def pta_uo(self, fast=None, medium=None, slow=None, fast_w=None, medium_w=None, slow_w=None, talib=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.uo(*arg, **kwarg)

    def pta_willr(self, length=None, talib=True, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.willr(*arg, **kwarg)

    def pta_crsi(
        self, rsi_length: int = 3, streak_length: int = 2,
        rank_length: int = 100, scalar: int | float = 100,
        talib: bool = True, drift: int = 1,
        offset: int = 0, **kwargs,
    ) -> pd.Series:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.crsi(*arg, **kwarg)

    def pta_exhc(
        self, length: int = 4, cap: int = 13,
        asint: bool = False, show_all: bool = False, nozeros: bool = False,
        offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.exhc(*arg, **kwarg)

    def pta_smc(
        self,
        abr_length: int = 14, close_length: int = 50, vol_length: int = 20,
        percent: int = 5, vol_ratio: int | float = 1.5, asint: bool =True,
        mamode: str = "sma", talib: bool = True,
        offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args : open,high,low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.smc(*arg, **kwarg)

    def pta_stochf(
        self,
        k: int = 14, d: int = 3,
        mamode: str = "sma", talib: bool = True,
        offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args : high,low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.stochf(*arg, **kwarg)
    def pta_tmo(
        self, tmo_length: int = 14,
        calc_length: int =5, smooth_length: int = 3,
        momentum: bool = False, normalize: bool =False, exclusive: bool = False,
        mamode: str = "ema", offset: int = 0, **kwargs,
    ) -> pd.DataFrame:
        """args : open,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OC)
        return pta.tmo(*arg, **kwarg)

    # Overlap
    def pta_alma(self, length=None, sigma=None, distribution_offset=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.alma(*arg, **kwarg)

    def pta_dema(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.dema(*arg, **kwarg)

    def pta_ema(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ema(*arg, **kwarg)

    def pta_fwma(self, length=None, asc=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.fwma(*arg, **kwarg)

    def pta_hilo(self, high_length=None, low_length=None, mamode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.hilo(*arg, **kwarg)

    def pta_hl2(self, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.hl2(*arg, **kwarg)

    def pta_hlc3(self, talib=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.hlc3(*arg, **kwarg)

    def pta_hma(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.hma(*arg, **kwarg)

    def pta_hwma(self, na=None, nb=None, nc=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.hwma(*arg, **kwarg)

    def pta_jma(self, length=None, phase=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.jma(*arg, **kwarg)

    def pta_kama(self, length=None, fast=None, slow=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.kama(*arg, **kwarg)

    def pta_ichimoku(self, tenkan=None, kijun=None, senkou=None, include_chikou=True, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        result, _ = pta.ichimoku(*arg, **kwarg)
        return result

    def pta_linreg(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.linreg(*arg, **kwarg)

    def pta_mcgd(self, length=None, offset=None, c=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.mcgd(*arg, **kwarg)

    def pta_midpoint(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.midpoint(*arg, **kwarg)

    def pta_midprice(self, length=None, talib=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.midprice(*arg, **kwarg)

    def pta_ohlc4(self, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.ohlc4(*arg, **kwarg)

    def pta_pwma(self, length=None, asc=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.pwma(*arg, **kwarg)

    def pta_ma(self, name: str = None, length: int = 10, **kwargs):
        """args : close"""
        kwargs.update(dict(length=length))
        locals().pop('length')
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        source = dict(source=arg[0])
        return pta.ma(**{**source, **kwarg})

    def pta_rma(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.rma(*arg, **kwarg)

    def pta_sinwma(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.sinwma(*arg, **kwarg)

    def pta_sma(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.sma(*arg, **kwarg)

    def pta_ssf(self, length=None, poles=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ssf(*arg, **kwarg)

    def pta_supertrend(self, length=None, multiplier=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.supertrend(*arg, **kwarg)

    def pta_swma(self, length=None, asc=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.swma(*arg, **kwarg)

    def pta_t3(self, length=None, a=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.t3(*arg, **kwarg)

    def pta_tema(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.tema(*arg, **kwarg)

    def pta_trima(self, length=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.trima(*arg, **kwarg)

    def pta_vidya(self, length=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.vidya(*arg, **kwarg)

    def pta_vwma(self, length=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.vwma(*arg, **kwarg)

    def pta_wcp(self, talib=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.wcp(*arg, **kwarg)

    def pta_wma(self, length=None, asc=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.wma(*arg, **kwarg)

    def pta_zlma(self, length=None, mamode=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.zlma(*arg, **kwarg)

    def pta_alligator(
        self, jaw: int = 13, teeth: int = 8, lips: int = 5,
        talib: bool = True, offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.alligator(*arg, **kwarg)
    
    def pta_mama(
        self, fastlimit: int | float = 0.5, slowlimit: int | float = 0.05,
        prenan: int = 3, talib: bool = True,
        offset: int = 0, **kwargs
    ) -> pd.Series:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.mama(*arg, **kwarg)
    
    def pta_pivots(
        self,
        method: str = 'traditional', anchor: str = 'D',
        **kwargs
    ) -> pd.DataFrame:
        """args : open,high,low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        # 设置datetime索引以满足pandas_ta.pivots的要求
        datetime = self._df.datetime
        for i in range(len(arg)):
            arg[i].index = datetime
        # 从kwarg中移除可能的重复参数
        kwarg.pop('method', None)
        kwarg.pop('anchor', None)
        return pta.pivots(*arg, method=method, anchor=anchor, **kwarg)

    def pta_smma(
        self, length: int = 10,
        mamode: str = "sma", talib: bool = True,
        offset: int = 0, **kwargs
    ) -> pd.Series:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.smma(*arg, **kwarg)

    def pta_ssf3(
        self, length: int = 20,
        pi: int | float = 3.14159, sqrt3: int | float = 1.732,
        offset: int = 0, **kwargs
    ) -> pd.Series:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ssf3(*arg, **kwarg)
    
    # Performance
    def pta_log_return(self, length=None, cumulative=False, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.log_return(*arg, **kwarg)

    def pta_percent_return(self, length=None, cumulative=False, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.percent_return(*arg, **kwarg)

    def pta_drawdown(
        self, offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.drawdown(*arg, **kwarg)

    # Statistics
    def pta_entropy(self, length=None, base=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.entropy(*arg, **kwarg)

    def pta_kurtosis_(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.kurtosis(*arg, **kwarg)

    def pta_mad(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.mad(*arg, **kwarg)

    def pta_median_(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.median(*arg, **kwarg)

    def pta_quantile_(self, length=None, q=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.quantile(*arg, **kwarg)

    def pta_skew_(self, length=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.skew(*arg, **kwarg)

    def pta_stdev(self, length=None, ddof=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.stdev(*arg, **kwarg)

    def pta_tos_stdevall(self, length=None, stds=None, ddof=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.tos_stdevall(*arg, **kwarg)

    def pta_variance(self, length=None, ddof=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.variance(*arg, **kwarg)

    def pta_zscore(self, length=None, std=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.zscore(*arg, **kwarg)

    # Trend
    def pta_adx(self, length=None, lensig=None, scalar=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.adx(*arg, **kwarg)

    def pta_amat(self, fast=None, slow=None, lookback=None, mamode=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.amat(*arg, **kwarg)

    def pta_aroon(self, length=None, scalar=None, talib=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.aroon(*arg, **kwarg)

    def pta_chop(self, length=None, atr_length=None, ln=None, scalar=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.chop(*arg, **kwarg)

    def pta_cksp(self, p=None, x=None, q=None, tvmode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.cksp(*arg, **kwarg)

    def pta_decay(self, kind=None, length=None, mode=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.decay(*arg, **kwarg)

    def pta_decreasing(self, length=None, strict=None, asint=None, percent=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.decreasing(*arg, **kwarg)

    def pta_dpo(self, length=None, centered=True, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.dpo(*arg, **kwarg)

    def pta_increasing(self, length=None, strict=None, asint=None, percent=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.increasing(*arg, **kwarg)

    def pta_long_run(self, fast=None, slow=None, length=None, offset=None, **kwargs):
        """args : self"""
        if fast is None and slow is None:
            return self._df
        else:
            return pta.long_run(fast, slow, length, offset, **kwargs)

    def pta_psar(self, af0=None, af=None, max_af=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.psar(*arg, **kwarg)

    def pta_qstick(self, length=None, ma="sma", offset=None, **kwargs):
        """args : open, close"""
        kwargs.update(dict(ma=ma))
        locals().pop("ma")
        arg, kwarg = self._get_column_(locals(), *FILED.OC)
        return pta.qstick(*arg, **kwarg)

    def pta_short_run(self, slow=None, length=None, offset=None, **kwargs):
        """args : self"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        # if fast is None and slow is None:
        #     return self._df
        # else:
        kwarg.pop("slow")
        return pta.short_run(*arg, slow, **kwarg)

    def pta_tsignals(self, trend=None, asbool=None, trend_reset=None, trade_offset=None, drift=None, offset=None, **kwargs):
        """args : self"""
        # if trend is None:
        #     return self._df
        # else:
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.tsignals(*arg,  **kwarg)

    def pta_ttm_trend(self, length=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pd.Series(pta.ttm_trend(*arg, **kwarg).values,name="ttm_trend")

    def pta_vhf(self, length=None, drift=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.vhf(*arg, **kwarg)

    def pta_vortex(self, length=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.vortex(*arg, **kwarg)

    def pta_xsignals(self, xa=None, xb=None, above=None, long=None, asbool=None, trend_reset=None, trade_offset=None, offset=None, **kwargs):
        """args : self"""
        # if signal is None:
        #     return self._df
        # else:
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.xsignals(*arg, **kwarg)
    
    def pta_alphatrend(
        self,
        src: str = "close",
        length: int = 14, multiplier: int | float = 1,
        threshold: int | float = 50, lag: int = 2,
        mamode: str = "sma", talib: bool = True,
        offset: int = 0, **kwargs)  -> pd.DataFrame:
        """args :open, high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLCV)
        return pta.alphatrend(*arg, **kwarg)
    
    def pta_ht_trendline(
        self, talib: bool = True,
        prenan: int = 63, offset: int = 0,
        **kwargs
    ) -> pd.Series:
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ht_trendline(*arg, **kwarg)

    def pta_rwi(
        self,
        length: int = 14, mamode: str = "rma", talib: bool = True,
        drift: int = 1, offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args :high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.rwi(*arg, **kwarg)

    def pta_trendflex(
        self, length: int = 20,
        smooth: int = 20, alpha: int | float = 0.04,
        pi: int | float = 3.14159, sqrt2: int | float = 1.414,
        offset: int = 0, **kwargs
    ) -> pd.Series:
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.trendflex(*arg, **kwarg)

    def pta_zigzag(
        self,
        legs: int = 10, deviation: int | float = 5, backtest: bool = False,
        offset: int = 0, **kwargs
    )-> pd.DataFrame:
        """args :high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.zigzag(*arg, **kwarg)


    # Utility
    def pta_above(self, b=None, asint=True, offset=None, **kwargs):
        """args : a(self),b"""
        # arg, kwarg = self._get_column_(locals(), *FILED.C)
        kwargs.update(dict(asint=asint, offset=offset))
        a = self._get_data_(get_data(kwargs.pop('a', self)))
        b = get_data(b)
        a = try_to_series(a)
        b = try_to_series(b)
        if isinstance(b, (int, float)):
            return pta.above_value(a, b, **kwargs)
        else:
            return pta.above(a, b, **kwargs)

    def pta_below(self,  b=None, asint=True, offset=None, **kwargs):
        """args : a(self),b"""
        # arg, kwarg = self._get_column_(locals(), *FILED.C)
        kwargs.update(dict(asint=asint, offset=offset))
        a = self._get_data_(get_data(kwargs.pop('a', self)))
        b = get_data(b)
        a = try_to_series(a)
        b = try_to_series(b)
        if isinstance(b, (int, float)):
            return pta.below_value(a, b, **kwargs)
        else:
            return pta.below(a, b, **kwargs)

    def pta_cross(self, b=None, above=True, asint=True, offset=None, **kwargs):
        """args : a(self),b"""
        # arg, kwarg = self._get_column_(locals(), *FILED.C)
        kwargs.update(dict(above=above, asint=asint, offset=offset))
        a = self._get_data_(get_data(kwargs.pop('a', self)))
        if isinstance(a, str):
            _a_frame = get_data(self)
            if len(_a_frame.shape) > 1:
                assert a in list(
                    _a_frame.columns), f"{a}不在列表{list(_a_frame.columns)}中"
                a = _a_frame[a]
            else:
                a = _a_frame
        b = get_data(b)
        a = try_to_series(a)
        b = try_to_series(b)
        if isinstance(b, (int, float)):
            return pta.cross_value(a, float(b), **kwargs)
        else:
            return pta.cross(a, b, **kwargs)

    def pta_cross_up(self, b=None, asint=True, offset=None, **kwargs):
        """args : a(self),b"""
        # arg, kwarg = self._get_column_(locals(), *FILED.C)
        kwargs.update(dict(above=True, asint=asint, offset=offset))
        a = self._get_data_(get_data(kwargs.pop('a', self)))
        if isinstance(a, str):
            _a_frame = get_data(self)
            if len(_a_frame.shape) > 1:
                assert a in list(
                    _a_frame.columns), f"{a}不在列表{list(_a_frame.columns)}中"
                a = _a_frame[a]
            else:
                a = _a_frame
        b = get_data(b)
        a = try_to_series(a)
        b = try_to_series(b)
        if isinstance(b, (int, float)):
            return pta.cross_value(a, b, **kwargs)
        else:
            return pta.cross(a, b, **kwargs)

    def pta_cross_down(self, b=None, asint=True, offset=None, **kwargs):
        """args : a(self),b"""
        # arg, kwarg = self._get_column_(locals(), *FILED.C)
        kwargs.update(dict(above=False, asint=asint, offset=offset))
        a = self._get_data_(get_data(kwargs.pop('a', self)))
        if isinstance(a, str):
            _a_frame = get_data(self)
            if len(_a_frame.shape) > 1:
                assert a in list(
                    _a_frame.columns), f"{a}不在列表{list(_a_frame.columns)}中"
                a = _a_frame[a]
            else:
                a = _a_frame
        b = get_data(b)
        a = try_to_series(a)
        b = try_to_series(b)
        if isinstance(b, (int, float)):
            return pta.cross_value(a, b, **kwargs)
        else:
            return pta.cross(a, b, **kwargs)

    def pta_signals(
        self, xa: int | float = None, xb: int | float = None,
        cross_values: bool = None, xseries: pd.Series = None,
        xseries_a: pd.Series = None, xseries_b: pd.Series = None,
        cross_series: bool = None, offset: int = 0
    ) -> pd.DataFrame:
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.signals(*arg, **kwarg)


    # Volatility
    def pta_aberration(self, length=None, atr_length=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.aberration(*arg, **kwarg)

    def pta_accbands(self, length=None, c=None, drift=None, mamode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.accbands(*arg, **kwarg)

    def pta_atr(self, length=None, mamode=None, talib=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.atr(*arg, **kwarg)

    def pta_bbands(self, length=None, std=None, ddof=0, mamode=None, talib=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.bbands(*arg, **kwarg)

    def pta_donchian(self, lower_length=None, upper_length=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.donchian(*arg, **kwarg)

    def pta_hwc(self, na=None, nb=None, nc=None, nd=None, scalar=None, channel_eval=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.hwc(*arg, **kwarg)

    def pta_kc(self, length=None, scalar=None, mamode=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.kc(*arg, **kwarg)

    def pta_massi(self, fast=None, slow=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.massi(*arg, **kwarg)

    def pta_natr(self, length=None, scalar=None, mamode=None, talib=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.natr(*arg, **kwarg)

    def pta_pdist(self, drift=None, offset=None, **kwargs):
        """args : open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return pta.pdist(*arg, **kwarg)

    def pta_rvi(self, length=None, scalar=None, refined=None, thirds=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), 'close', 'high', 'low')
        return pta.rvi(*arg, **kwarg)

    def pta_thermo(self, length=None, long=None, short=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pta.thermo(*arg, **kwarg)

    def pta_true_range(self, talib=None, drift=None, offset=None, **kwargs):
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.true_range(*arg, **kwarg)

    def pta_ui(self, length=None, scalar=None, offset=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pta.ui(*arg, **kwarg)

    def pta_atrts(
        self, length: int = 14,
        ma_length: int = 20, k: int | float = 3,
        mamode: str = "ema", talib: bool = True, drift: int = 1,
        offset: int = 0, **kwargs
    ) -> pd.Series:
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.atrts(*arg, **kwarg)
    
    def pta_chandelier_exit(
        self,
        high_length: int = 22, low_length: int = 22,
        atr_length: int = 14, multiplier: int | float = 2.,
        mamode: str = "rma", talib: bool = True, use_close: bool = False,
        drift: int = 1, offset: int = 0, **kwargs
    )-> pd.DataFrame:
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pta.chandelier_exit(*arg, **kwarg)

    # Volume
    def pta_ad(self, open_=None, talib=None, offset=None, **kwargs):
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return pta.ad(*arg, **kwarg)

    def pta_adosc(self, open_=None, fast=None, slow=None, talib=None, offset=None, **kwargs):
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(
            locals(), *FILED.HLCV)
        return pta.adosc(*arg, **kwarg)

    def pta_aobv(self, fast=None, slow=None, max_lookback=None, min_lookback=None, mamode=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.aobv(*arg, **kwarg)

    def pta_cmf(self, open_=None, length=None, offset=None, **kwargs):
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(
            locals(), *FILED.HLCV)
        return pta.cmf(*arg, **kwarg)

    def pta_efi(self,  length=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.efi(*arg, **kwarg)

    def pta_eom(self, length=None, divisor=None, drift=None, offset=None, **kwargs):
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return pta.eom(*arg, **kwarg)

    def pta_kvo(self, fast=None, slow=None, length_sig=None, signal=None, mamode=None, drift=None, offset=None, **kwargs):
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return pta.kvo(*arg, **kwarg)

    def pta_mfi(self, length=None, talib=None, drift=None, offset=None, **kwargs):
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return pta.mfi(*arg, **kwarg)

    def pta_nvi(self, length=None, initial=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.nvi(*arg, **kwarg)

    def pta_obv(self, talib=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.obv(*arg, **kwarg)

    def pta_pvi(self, length=None, initial=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.pvi(*arg, **kwarg)

    def pta_pvol(self, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.pvol(*arg, **kwarg)

    def pta_pvr(self, **kwargs):
        """args : close, volume"""
        arg, _ = self._get_column_(locals(), *FILED.CV)
        return pta.pvr(*arg)

    def pta_pvt(self, drift=None, offset=None, **kwargs):
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.pvt(*arg, **kwarg)

    def pta_vp(self, width=None, **kwargs):
        """Volume Profile - 自定义实现修复pandas_ta的array_split问题
        args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.vp(*arg, **kwarg)

    def pta_tsv(
        self,
        length: int = 18, signal: int = 10,
        mamode: str = "sma", drift: int = 1,
        offset: int = 0, **kwargs
    ) -> pd.DataFrame:
        """args : close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return pta.tsv(*arg, **kwarg)

    def pta_vhm(
        self, length: int = 610, std_length = 610,
        mamode: str = "sma", offset: int = 0, **kwargs
    ) -> pd.Series:
        """args : volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.V)
        return pta.vhm(*arg, **kwarg)

    def pta_vwap(
        self,
        anchor: str = "D", bands: list = [],
        offset: int = 0, **kwargs
    ) -> pd.Series:
        """args : high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        # 设置datetime索引以满足pandas_ta.vwap的要求
        datetime = self._df.datetime
        for i in range(len(arg)):
            arg[i].index = datetime
        # 从kwarg中移除可能的重复参数
        kwarg.pop('anchor', None)
        kwarg.pop('bands', None)
        kwarg.pop('offset', None)
        return pta.vwap(*arg, anchor=anchor, bands=bands, offset=offset, **kwarg)

    # TALIB
    # Cycle Indicator Functions
    def talib(cls) -> Talib:
        ...

    def talib_HT_DCPERIOD(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().HT_DCPERIOD(*arg)

    def talib_HT_DCPHASE(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().HT_DCPHASE(*arg)

    def talib_HT_PHASOR(self, **kwargs):
        """args:close
        lines=inphase, quadrature"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().HT_PHASOR(*arg), axis=1)

    def talib_HT_SINE(self, **kwargs):
        """args:close
        lines=sine, leadsine"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().HT_SINE(*arg), axis=1)

    def talib_HT_TRENDMODE(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().HT_TRENDMODE(*arg)

    # TALIB
    # Math Operator Functions
    def talib_ADD(self,b=None, **kwargs):
        """args:high,low"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ADD(*arg,b)

    def talib_DIV(self,b=None, **kwargs):
        """args:high,low"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().DIV(*arg,b) 

    def talib_MAX(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MAX(*arg, timeperiod=timeperiod)

    def talib_MAXINDEX(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MAXINDEX(*arg, timeperiod=timeperiod)

    def talib_MIN(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MIN(*arg, timeperiod=timeperiod)

    def talib_MININDEX(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MININDEX(*arg, timeperiod=timeperiod)

    def talib_MINMAX(self, timeperiod=30, **kwargs):
        """args:close
        lines:min , max"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().MINMAX(*arg, timeperiod=timeperiod), axis=1)

    def talib_MINMAXINDEX(self, timeperiod=30, **kwargs):
        """args:close
        lines:minidx , maxidx"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().MINMAXINDEX(*arg, timeperiod=timeperiod), axis=1)

    def talib_MULT(self,b=None, **kwargs):
        """args:high,low"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MULT(*arg,b)

    def talib_SUB(self,b=None, **kwargs):
        """args:high,low"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().SUB(*arg,b)

    def talib_SUM(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().SUM(*arg, timeperiod=timeperiod)

    # TALIB
    # Math Transform Functions
    def talib_ACOS(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ACOS(*arg)

    def talib_ASIN(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ASIN(*arg)

    def talib_ATAN(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ATAN(*arg)

    def talib_CEIL(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().CEIL(*arg)

    def talib_COS(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().COS(*arg)

    def talib_COSH(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().COSH(*arg)

    def talib_EXP(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().EXP(*arg)

    def talib_FLOOR(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().FLOOR(*arg)

    def talib_LN(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LN(*arg)

    def talib_LOG10(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LOG10(*arg)

    def talib_SIN(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LOG10(*arg)

    def talib_SINH(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().SINH(*arg)

    def talib_SQRT(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().SQRT(*arg)

    def talib_TAN(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().TAN(*arg)

    def talib_TANH(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().TANH(*arg)

    # TALIB
    # Momentum Indicator Functions
    def talib_ADX(self, timeperiod=14, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().ADX(*arg, timeperiod=timeperiod)

    def talib_ADXR(self, timeperiod=14, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().ADXR(*arg, timeperiod=timeperiod)

    def talib_APO(self, fastperiod=12, slowperiod=26, matype=0, **kwargs):
        """args: close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().APO(*arg, fastperiod=fastperiod, slowperiod=slowperiod, matype=matype)

    def talib_AROON(self, timeperiod=14, **kwargs):
        """args:high, low
        lines:aroondown, aroonup"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return pd.concat(self.talib().AROON(*arg, timeperiod=timeperiod), axis=1)

    def talib_AROONOSC(self, timeperiod=14, **kwargs):
        """args:high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().AROONOSC(*arg, timeperiod=timeperiod)

    def talib_BOP(self, **kwargs):
        """args:open,high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return self.talib().BOP(*arg)

    def talib_CCI(self, timeperiod=14, **kwargs):
        """args:high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().CCI(*arg, timeperiod=timeperiod)

    def talib_CMO(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().CMO(*arg, timeperiod=timeperiod)

    def talib_DX(self, timeperiod=14, **kwargs):
        """args:high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().DX(*arg, timeperiod=timeperiod)

    def talib_MACD(self, fastperiod=12, slowperiod=26, signalperiod=9, **kwargs):
        """args:close
        lines:dif, dem, histogram"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().MACD(*arg, fastperiod=fastperiod, slowperiod=slowperiod, signalperiod=signalperiod), axis=1)

    def talib_MACDEXT(self, fastperiod=12, fastmatype=0, slowperiod=26, slowmatype=0, signalperiod=9, signalmatype=0, **kwargs):
        """args:close
        lines:dif, dem, histogram"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().MACDEXT(*arg, fastperiod=fastperiod, fastmatype=fastmatype, slowperiod=slowperiod, slowmatype=slowmatype, signalperiod=signalperiod, signalmatype=signalmatype), axis=1)

    def talib_MACDFIX(self, signalperiod=9, **kwargs):
        """args:close
        lines:dif, dem, histogram"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().MACDFIX(*arg, signalperiod), axis=1)

    def talib_MFI(self, timeperiod=14, **kwargs):
        """args:high, low,close,volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return self.talib().MFI(*arg, timeperiod=timeperiod)

    def talib_MINUS_DI(self, timeperiod=14, **kwargs):
        """args:high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().MINUS_DI(*arg, timeperiod=timeperiod)

    def talib_MINUS_DM(self, timeperiod=14, **kwargs):
        """args:high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().MINUS_DM(*arg, timeperiod=timeperiod)

    def talib_MOM(self, timeperiod=10, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MOM(*arg, timeperiod=timeperiod)

    def talib_PLUS_DI(self, timeperiod=14, **kwargs):
        """args:high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().PLUS_DI(*arg, timeperiod=timeperiod)

    def talib_PLUS_DM(self, timeperiod=14, **kwargs):
        """args:high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().PLUS_DM(*arg, timeperiod=timeperiod)

    def talib_PPO(self, fastperiod=12, slowperiod=26, matype=0, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().PPO(*arg, fastperiod=fastperiod, slowperiod=slowperiod, matype=matype)

    def talib_ROC(self, timeperiod=10, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ROC(*arg, timeperiod=timeperiod)

    def talib_ROCP(self, timeperiod=10, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ROCP(*arg, timeperiod=timeperiod)

    def talib_ROCR(self, timeperiod=10, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ROCR(*arg, timeperiod=timeperiod)

    def talib_ROCR100(self, timeperiod=10, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().ROCR100(*arg, timeperiod=timeperiod)

    def talib_RSI(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().RSI(*arg, timeperiod=timeperiod)

    def talib_STOCH(self, fastk_period=5, slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0, **kwargs):
        """args:high, low,close
        lines:slowk, slowd """
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pd.concat(self.talib().STOCH(*arg, fastk_period=fastk_period, slowk_period=slowk_period, slowk_matype=slowk_matype, slowd_period=slowd_period, slowd_matype=slowd_matype), axis=1)

    def talib_STOCHF(self, fastk_period=5, fastd_period=3, fastd_matype=0, **kwargs):
        """args:high, low,close
        lines:fastk, fastd """
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return pd.concat(self.talib().STOCHF(*arg, fastk_period=fastk_period, fastd_period=fastd_period, fastd_matype=fastd_matype), axis=1)

    def talib_STOCHRSI(self, timeperiod=14, fastk_period=5, fastd_period=3, fastd_matype=0, **kwargs):
        """args:close
        lines:fastk, fastd """
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().STOCHRSI(*arg, timeperiod=timeperiod, fastk_period=fastk_period, fastd_period=fastd_period, fastd_matype=fastd_matype), axis=1)

    def talib_TRIX(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().TRIX(*arg, timeperiod=timeperiod)

    def talib_ULTOSC(self, timeperiod1=7, timeperiod2=14, timeperiod3=28, **kwargs):
        """args:high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().ULTOSC(*arg, timeperiod1=timeperiod1, timeperiod2=timeperiod2, timeperiod3=timeperiod3)

    def talib_WILLR(self, timeperiod=14, **kwargs):
        """args:high, low,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().WILLR(*arg, timeperiod=timeperiod)

    # Overlap Studies Functions
    def talib_BBANDS(self, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0, **kwargs):
        """args:close
        lines:upperband, middleband, lowerband"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return pd.concat(self.talib().BBANDS(*arg, timeperiod=timeperiod, nbdevup=nbdevup, nbdevdn=nbdevdn, matype=matype), axis=1)

    def talib_DEMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().DEMA(*arg, timeperiod=timeperiod)

    def talib_EMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().EMA(*arg, timeperiod=timeperiod)

    def talib_HT_TRENDLINE(self, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().HT_TRENDLINE(*arg)

    def talib_KAMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().KAMA(*arg, timeperiod=timeperiod)

    def talib_MA(self, timeperiod=30, matype=0, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MA(*arg, timeperiod=timeperiod, matype=matype)

    def talib_MAMA(self, fastlimit=0.06185, slowlimit=0.6185, **kwargs):
        """args:close
        lines:mama, fama"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MAMA(*arg, fastlimit=fastlimit, slowlimit=slowlimit)

    def talib_MAVP(self, periods=14., minperiod=2, maxperiod=30, matype=0, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        # 处理periods参数
        size = arg[0].size
        if periods is None:
            # 默认使用周期14
            periods_array = np.full(size, 14.0)
        elif isinstance(periods, (int, float)):
            # 标量值转换为数组
            periods_array = np.full(size, float(periods))
        elif hasattr(periods, 'values'):
            # Pandas Series或IndSeries
            periods_array = periods.values if len(
                periods) == size else np.full(size, float(periods.mean()))
        elif isinstance(periods, (list, np.ndarray)):
            # 列表或numpy数组
            periods_array = np.asarray(periods, dtype=np.float64)
            if len(periods_array) != size:
                # 长度不匹配，使用平均值填充
                avg_period = float(np.nanmean(periods_array))
                periods_array = np.full(size, avg_period)
        else:
            raise TypeError(f"不支持的periods类型: {type(periods)}")

        # 确保周期在[minperiod, maxperiod]范围内
        periods_array = np.clip(periods_array, minperiod, maxperiod)
        arg.append(periods_array)
        return self.talib().MAVP(*arg, minperiod=minperiod, maxperiod=maxperiod, matype=matype)

    def talib_MIDPOINT(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().MIDPOINT(*arg, timeperiod=timeperiod)

    def talib_MIDPRICE(self, timeperiod=14, **kwargs):
        """args:high, low,"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().MIDPRICE(*arg, timeperiod=timeperiod)

    def talib_SAR(self, acceleration=0, maximum=0, **kwargs):
        """args:high, low,"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().SAR(*arg, acceleration=acceleration, maximum=maximum)

    def talib_SAREXT(self, startvalue=0, offsetonreverse=0, accelerationinitlong=0, accelerationlong=0, accelerationmaxlong=0, accelerationinitshort=0, accelerationshort=0, accelerationmaxshort=0, **kwargs):
        """args:high, low,"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().SAREXT(*arg, startvalue=startvalue, offsetonreverse=offsetonreverse, accelerationinitlong=accelerationinitlong, accelerationlong=accelerationlong, accelerationmaxlong=accelerationmaxlong,
                                 accelerationinitshort=accelerationinitshort, accelerationshort=accelerationshort, accelerationmaxshort=accelerationmaxshort)

    def talib_SMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().SMA(*arg, timeperiod=timeperiod)

    def talib_T3(self, timeperiod=5, vfactor=0, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().T3(*arg, timeperiod=timeperiod, vfactor=vfactor)

    def talib_TEMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().TEMA(*arg, timeperiod=timeperiod)

    def talib_TRIMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().TRIMA(*arg, timeperiod=timeperiod)

    def talib_WMA(self, timeperiod=30, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().WMA(*arg, timeperiod=timeperiod)

    # Pattern Recognition Functions 形态识别
    def talib_PatternRecognition(self, name="CDL2CROWS", penetration=0, **kwargs):
        """args:open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        ls = ["CDLDARKCLOUDCOVER", "CDLABANDONEDBABY", "CDLEVENINGDOJISTAR",
              "CDLEVENINGSTAR", "CDLMATHOLD", "CDLMORNINGDOJISTAR", "CDLMORNINGSTAR"]
        indfunc: Callable = getattr(self.talib(), name)
        return indfunc(*arg) if name not in ls else indfunc(*arg, penetration=penetration)

    # Price Transform Functions
    def talib_AVGPRICE(self, **kwargs):
        """args:open, high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return self.talib().AVGPRICE(*arg)

    def talib_MEDPRICE(self, **kwargs):
        """args:high, low,"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().MEDPRICE(*arg)

    def talib_TYPPRICE(self, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().TYPPRICE(*arg)

    def talib_WCLPRICE(self, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().WCLPRICE(*arg)

    # Statistic Functions 统计学指标
    def talib_BETA(self, timeperiod=5, **kwargs):
        """args:high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().BETA(*arg, timeperiod=timeperiod)

    def talib_CORREL(self, timeperiod=30, **kwargs):
        """args:high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return self.talib().CORREL(*arg, timeperiod=timeperiod)

    def talib_LINEARREG(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LINEARREG(*arg, timeperiod=timeperiod)

    def talib_LINEARREG_ANGLE(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LINEARREG_ANGLE(*arg, timeperiod=timeperiod)

    def talib_LINEARREG_INTERCEPT(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LINEARREG_INTERCEPT(*arg, timeperiod=timeperiod)

    def talib_LINEARREG_SLOPE(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().LINEARREG_SLOPE(*arg, timeperiod=timeperiod)

    def talib_STDDEV(self, timeperiod=5, nbdev=1, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().STDDEV(*arg, timeperiod=timeperiod, nbdev=nbdev)

    def talib_TSF(self, timeperiod=14, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().TSF(*arg, timeperiod=timeperiod)

    def talib_VAR(self, timeperiod=5, nbdev=1, **kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return self.talib().VAR(*arg, timeperiod=timeperiod, nbdev=nbdev)

    # Volatility Indicator Functions 波动率指标函数
    def talib_ATR(self, timeperiod=14, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().ATR(*arg, timeperiod=timeperiod)

    def talib_NATR(self, timeperiod=14, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().NATR(*arg, timeperiod=timeperiod)

    def talib_TRANGE(self, **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return self.talib().TRANGE(*arg)

    # Volume Indicators 成交量指标
    def talib_AD(self, **kwargs):
        """args:high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return self.talib().AD(*arg)

    def talib_ADOSC(self, fastperiod=3, slowperiod=10, **kwargs):
        """args:high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return self.talib().ADOSC(*arg, fastperiod=fastperiod, slowperiod=slowperiod)

    def talib_OBV(self, **kwargs):
        """args:close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.CV)
        return self.talib().OBV(*arg)

    # tulipy指标
    # https://tulipindicators.org/list
    # https://github.com/TulipCharts/tulipy
    # def tulipy(self) -> tulipy:
    #     ...
    def tulipy(cls) -> tulipy:
        ...

    def ti_abs(self, **kwargs):
        """Vector Absolute Value
        args:close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().abs(*arg), self.length)

    def ti_acos(self, **kwargs):
        """Vector Arccosine
        args:close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().acos(*arg), self.length)

    def ti_ad(self, **kwargs):
        """Accumulation/Distribution Line
        args:high, low, close, volume"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLCV)
        return pad_array_to_match(self.tulipy().ad(*arg), self.length)

    def ti_add(self, series=None, **kwargs):
        """Vector Addition
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        if hasattr(series, "values"):
            series = series.values.astype(np.float64)
        if not ((isinstance(series, np.ndarray) and self.length == len(series)) or
                isinstance(series, (float, int))):
            return self
        arg.append(series)
        return pad_array_to_match(self.tulipy().add(*arg), self.length)

    def ti_adosc(self, short_period=10, long_period=10, **kwargs):
        """
        Accumulation/Distribution Oscillator
        args:high, low, close, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLCV)
        arg.extend([short_period, long_period])
        return pad_array_to_match(self.tulipy().adosc(*arg), self.length)

    def ti_adx(self, period=10, **kwargs):
        """
        Average Directional Movement Index
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().adx(*arg), self.length)

    def ti_adxr(self, period=10, **kwargs):
        """
        Average Directional Movement Rating
        args:high, low, close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().adxr(*arg), self.length)

    def ti_ao(self, **kwargs):
        """
        Awesome Oscillator
        args:high, low"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        return pad_array_to_match(self.tulipy().ao(*arg), self.length)

    def ti_apo(self, short_period=12, long_period=26, **kwargs):
        """
        Absolute Price Oscillator
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.extend([short_period, long_period])
        return pad_array_to_match(self.tulipy().apo(*arg), self.length)

    def ti_aroon(self, period=10, **kwargs):
        """
        Aroon
        args:high, low
        return:2 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.append(period)
        return pad_array_to_match(self.tulipy().aroon(*arg), self.length)

    def ti_aroonosc(self, period=10, **kwargs):
        """
        Aroon Oscillator
        args:high, low
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.append(period)
        return pad_array_to_match(self.tulipy().aroonosc(*arg), self.length)

    def ti_asin(self, **kwargs):
        """
        Vector Arcsine
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().asin(*arg), self.length)

    def ti_atan(self, **kwargs):
        """
        Vector Arctangent
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().atan(*arg), self.length)

    def ti_atr(self, period=10, **kwargs):
        """
        Average True Range
        args:high, low, close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().atr(*arg), self.length)

    def ti_avgprice(self, **kwargs):
        """
        Average Price
        args:open, high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.OHLC)
        return pad_array_to_match(self.tulipy().avgprice(*arg), self.length)

    def ti_bbands(self, period=10, stddev=1., **kwargs):
        """
        Bollinger Bands
        args:close
        return:3 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.extend([period, stddev])
        return pad_array_to_match(self.tulipy().bbands(*arg), self.length)

    def ti_bop(self, **kwargs):
        """
        Balance of Power
        args:open, high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.OHLC)
        return pad_array_to_match(self.tulipy().bop(*arg), self.length)

    def ti_cci(self, period=10, **kwargs):
        """
        Commodity Channel Index
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().cci(*arg), self.length)

    def ti_ceil(self, **kwargs):
        """
        Vector Ceiling
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().ceil(*arg), self.length)

    def ti_cmo(self, period=10, **kwargs):
        """
        Chande Momentum Oscillator
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().cmo(*arg), self.length)

    def ti_cos(self, **kwargs):
        """
        Vector Cosine
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().cos(*arg), self.length)

    def ti_cosh(self, **kwargs):
        """
        Vector Hyperbolic Cosine
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().cosh(*arg), self.length)

    def ti_crossany(self, series=None, **kwargs):
        """
        Crossany
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        if hasattr(series, "values"):
            series = series.values.astype(np.float64)
        if not ((isinstance(series, np.ndarray) and self.length == len(series)) or
                isinstance(series, (float, int))):
            return self
        arg.append(series)
        return pad_array_to_match(self.tulipy().crossany(*arg), self.length)

    def ti_crossover(self, series=None, **kwargs):
        """
        Crossover
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        if hasattr(series, "values"):
            series = series.values.astype(np.float64)
        if not ((isinstance(series, np.ndarray) and self.length == len(series)) or
                isinstance(series, (float, int))):
            return self
        arg.append(series)
        return pad_array_to_match(self.tulipy().crossover(*arg), self.length)

    def ti_cvi(self, period=10, **kwargs):
        """
        Chaikins Volatility
        args:high, low
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.append(period)
        return pad_array_to_match(self.tulipy().cvi(*arg), self.length)

    def ti_decay(self, period=10, **kwargs):
        """
        Linear Decay
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().decay(*arg), self.length)

    def ti_dema(self, period=10, **kwargs):
        """
        Double Exponential Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().dema(*arg), self.length)

    def ti_di(self, period=10, **kwargs):
        """
        Directional Indicator
        args:high, low, close
        return:2 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().di(*arg), self.length)

    def ti_div(self, series=None, **kwargs):
        """
        Vector Division
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        if hasattr(series, "values"):
            series = series.values.astype(np.float64)
        if not ((isinstance(series, np.ndarray) and self.length == len(series)) or
                isinstance(series, (float, int))):
            return self
        arg.append(series)
        return pad_array_to_match(self.tulipy().div(*arg), self.length)

    def ti_dm(self, period=10, **kwargs):
        """
        Directional Movement
        args:high, low
        return:2 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.append(period)
        return pad_array_to_match(self.tulipy().dm(*arg), self.length)

    def ti_dpo(self, period=10, **kwargs):
        """
        Detrended Price Oscillator
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().dpo(*arg), self.length)

    def ti_dx(self, period=10, **kwargs):
        """
        Directional Movement Index
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().dx(*arg), self.length)

    def ti_edecay(self, period=10, **kwargs):
        """
        Exponential Decay
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().edecay(*arg), self.length)

    def ti_ema(self, period=10, **kwargs):
        """
        Exponential Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().ema(*arg), self.length)

    def ti_emv(self, **kwargs):
        """
        Ease of Movement
        args:high, low, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLV)
        return pad_array_to_match(self.tulipy().emv(*arg), self.length)

    def ti_exp(self, **kwargs):
        """
        Vector Exponential
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().exp(*arg), self.length)

    def ti_fisher(self, period=10, **kwargs):
        """
        Fisher Transform
        args:high, low
        return:2 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.append(period)
        return pad_array_to_match(self.tulipy().fisher(*arg), self.length)

    def ti_floor(self, **kwargs):
        """
        Vector Floor
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().floor(*arg), self.length)

    def ti_fosc(self, period=10, **kwargs):
        """
        Forecast Oscillator
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().fosc(*arg), self.length)

    def ti_hma(self, period=10, **kwargs):
        """
        Hull Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().hma(*arg), self.length)

    def ti_kama(self, period=10, **kwargs):
        """
        Kaufman Adaptive Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().kama(*arg), self.length)

    def ti_kvo(self, short_period=10, long_period=10, **kwargs):
        """
        Klinger Volume Oscillator
        args:high, low, close, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLCV)
        arg.extend([short_period, long_period])
        return pad_array_to_match(self.tulipy().kvo(*arg), self.length)

    def ti_lag(self, period=10, **kwargs):
        """
        Lag
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().lag(*arg), self.length)

    def ti_linreg(self, period=10, **kwargs):
        """
        Linear Regression
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().linreg(*arg), self.length)

    def ti_linregintercept(self, period=10, **kwargs):
        """
        Linear Regression Intercept
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().linregintercept(*arg), self.length)

    def ti_linregslope(self, period=10, **kwargs):
        """
        Linear Regression Slope
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().linregslope(*arg), self.length)

    def ti_ln(self, **kwargs):
        """
        Vector Natural Log
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().ln(*arg), self.length)

    def ti_log10(self, **kwargs):
        """
        Vector Base-10 Log
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().log10(*arg), self.length)

    def ti_macd(self, short_period=12, long_period=26, signal_period=9, **kwargs):
        """
        Moving Average Convergence/Divergence
        args:close
        return:3 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.extend([short_period, long_period, signal_period])
        return pad_array_to_match(self.tulipy().macd(*arg), self.length)

    def ti_marketfi(self, **kwargs):
        """
        Market Facilitation Index
        args:high, low, volume"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        return pad_array_to_match(self.tulipy().marketfi(*arg), self.length)

    def ti_mass(self, period=10, **kwargs):
        """
        Mass Index
        args:high, low
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.append(period)
        return pad_array_to_match(self.tulipy().mass(*arg), self.length)

    def ti_max(self, period=10, **kwargs):
        """
        Maximum In Period
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().max(*arg), self.length)

    def ti_md(self, period=10, **kwargs):
        """
        Mean Deviation Over Period
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().md(*arg), self.length)

    def ti_medprice(self, **kwargs):
        """
        Median Price
        args:high, low
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        return pad_array_to_match(self.tulipy().medprice(*arg), self.length)

    def ti_mfi(self, period=10, **kwargs):
        """
        Money Flow Index
        args:high, low, close, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLCV)
        arg.append(period)
        return pad_array_to_match(self.tulipy().mfi(*arg), self.length)

    def ti_min(self, period=10, **kwargs):
        """
        Minimum In Period
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().min(*arg), self.length)

    def ti_mom(self, period=10, **kwargs):
        """
        Momentum
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().mom(*arg), self.length)

    def ti_msw(self, period=10, **kwargs):
        """
        Mesa Sine Wave
        args:close
        return:2 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().msw(*arg), self.length)

    def ti_mul(self, series=None, **kwargs):
        """
        Vector Multiplication
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        if hasattr(series, "values"):
            series = series.values.astype(np.float64)
        if not ((isinstance(series, np.ndarray) and self.length == len(series)) or
                isinstance(series, (float, int))):
            return self
        arg.append(series)
        return pad_array_to_match(self.tulipy().mul(*arg), self.length)

    def ti_natr(self, period=10, **kwargs):
        """
        Normalized Average True Range
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().natr(*arg), self.length)

    def ti_nvi(self, **kwargs):
        """
        Negative Volume Index
        args:close, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.CV)
        return pad_array_to_match(self.tulipy().nvi(*arg), self.length)

    def ti_obv(self, **kwargs):
        """
        On Balance Volume
        args:close, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.CV)
        return pad_array_to_match(self.tulipy().obv(*arg), self.length)

    def ti_ppo(self, short_period=10, long_period=10, **kwargs):
        """
        Percentage Price Oscillator
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.extend([short_period, long_period])
        return pad_array_to_match(self.tulipy().ppo(*arg), self.length)

    def ti_psar(self, acceleration_factor_step=0.06185, acceleration_factor_maximum=0.6185, **kwargs):
        """
        Parabolic SAR
        args:high, low
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HL)
        arg.extend([acceleration_factor_step, acceleration_factor_maximum])
        return pad_array_to_match(self.tulipy().psar(*arg), self.length)

    def ti_qstick(self, period=10, **kwargs):
        """
        Qstick
        args:open, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.OC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().qstick(*arg), self.length)

    def ti_roc(self, period=10, **kwargs):
        """
        Rate of Change
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().roc(*arg), self.length)

    def ti_rocr(self, period=10, **kwargs):
        """
        Rate of Change Ratio
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().rocr(*arg), self.length)

    def ti_round(self, **kwargs):
        """
        Vector Round
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().round(*arg), self.length)

    def ti_rsi(self, period=10, **kwargs):
        """
        Relative Strength Index
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().rsi(*arg), self.length)

    def ti_sin(self, **kwargs):
        """
        Vector Sine
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().sin(*arg), self.length)

    def ti_sinh(self, **kwargs):
        """
        Vector Hyperbolic Sine
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().sinh(*arg), self.length)

    def ti_sma(self, period=10, **kwargs):
        """
        Simple Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().sma(*arg), self.length)

    def ti_sqrt(self, **kwargs):
        """
        Vector Square Root
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().sqrt(*arg), self.length)

    def ti_stddev(self, period=10, **kwargs):
        """
        Standard Deviation Over Period
        args:close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().stddev(*arg), self.length)

    def ti_stderr(self, period=10, **kwargs):
        """
        Standard Error Over Period
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().stderr(*arg), self.length)

    def ti_stoch(self, pct_k_period=3, pct_k_slowing_period=6, pct_d_period=3, **kwargs):
        """
        Stochastic Oscillator
        args:high, low, close
        retrun:2 lines
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.extend([pct_k_period, pct_k_slowing_period, pct_d_period])
        return pad_array_to_match(self.tulipy().stoch(*arg), self.length)

    def ti_stochrsi(self, period=10, **kwargs):
        """
        Stochastic RSI
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().stochrsi(*arg), self.length)

    def ti_sub(self, series=None, **kwargs):
        """
        Vector Subtraction
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        if hasattr(series, "values"):
            series = series.values.astype(np.float64)
        if not ((isinstance(series, np.ndarray) and self.length == len(series)) or
                isinstance(series, (float, int))):
            return self
        arg.append(series)
        return pad_array_to_match(self.tulipy().sub(*arg), self.length)

    def ti_sum(self, period=10, **kwargs):
        """
        Sum Over Period
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().sum(*arg), self.length)

    def ti_tan(self, **kwargs):
        """
        Vector Tangent
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().tan(*arg), self.length)

    def ti_tanh(self, **kwargs):
        """
        Vector Hyperbolic Tangent
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().tanh(*arg), self.length)

    def ti_tema(self, period=10, **kwargs):
        """
        Triple Exponential Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().tema(*arg), self.length)

    def ti_todeg(self, **kwargs):
        """
        Vector Degree Conversion
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().todeg(*arg), self.length)

    def ti_torad(self, **kwargs):
        """
        Vector Radian Conversion
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().torad(*arg), self.length)

    def ti_tr(self, **kwargs):
        """
        True Range
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        return pad_array_to_match(self.tulipy().tr(*arg), self.length)

    def ti_trima(self, period=10, **kwargs):
        """
        Triangular Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().trima(*arg), self.length)

    def ti_trix(self, period=10, **kwargs):
        """
        Trix
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().trix(*arg), self.length)

    def ti_trunc(self, **kwargs):
        """
        Vector Truncate
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().trunc(*arg), self.length)

    def ti_tsf(self, period=10, **kwargs):
        """
        Time Series Forecast
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().tsf(*arg), self.length)

    def ti_typprice(self, **kwargs):
        """
        Typical Price
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        return pad_array_to_match(self.tulipy().typprice(*arg), self.length)

    def ti_ultosc(self, short_period=10, medium_period=10, long_period=10, **kwargs):
        """
        Ultimate Oscillator
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.extend([short_period, medium_period, long_period])
        return pad_array_to_match(self.tulipy().ultosc(*arg), self.length)

    def ti_var(self, period=10, **kwargs):
        """
        Variance Over Period
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().var(*arg), self.length)

    def ti_vhf(self, period=10, **kwargs):
        """
        Vertical Horizontal Filter
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().vhf(*arg), self.length)

    def ti_vidya(self, short_period=5, long_period=10, alpha=0.1, **kwargs):
        """
        Variable Index Dynamic Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        return pad_array_to_match(self.tulipy().vidya(arg[0], short_period, long_period, alpha), self.length)

    def ti_volatility(self, period=10, **kwargs):
        """
        Annualized Historical Volatility
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().volatility(*arg), self.length)

    def ti_vosc(self, short_period=10, long_period=10, **kwargs):
        """
        Volume Oscillator
        args:volume"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.V)
        arg.extend([short_period, long_period])
        return pad_array_to_match(self.tulipy().vosc(*arg), self.length)

    def ti_vwma(self, period=10, **kwargs):
        """
        Volume Weighted Moving Average
        args:close, volume
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.CV)
        arg.append(period)
        return pad_array_to_match(self.tulipy().vwma(*arg), self.length)

    def ti_wad(self, **kwargs):
        """
        Williams Accumulation/Distribution
        args:high, low, close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        return pad_array_to_match(self.tulipy().wad(*arg), self.length)

    def ti_wcprice(self, **kwargs):
        """
        Weighted Close Price
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        return pad_array_to_match(self.tulipy().wcprice(*arg), self.length)

    def ti_wilders(self, period=10, **kwargs):
        """
        Wilders Smoothing
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().wilders(*arg), self.length)

    def ti_willr(self, period=10, **kwargs):
        """
        Williams %R
        args:high, low, close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.HLC)
        arg.append(period)
        return pad_array_to_match(self.tulipy().willr(*arg), self.length)

    def ti_wma(self, period=10, **kwargs):
        """
        Weighted Moving Average
        args:close
        """
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().wma(*arg), self.length)

    def ti_zlema(self, period=10, **kwargs):
        """
        Zero-Lag Exponential Moving Average
        args:close"""
        arg, kwarg = self._ti_get_data(locals(), *FILED.C)
        arg.append(period)
        return pad_array_to_match(self.tulipy().zlema(*arg), self.length)

    # finta指标
    def FinTa(cls) -> FinTa:
        ...

    def finta_SMA(self, period: int = 41, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SMA(data, period)

    def finta_SMM(self, period: int = 9, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SMM(data, period)

    def finta_SSMA(self, period: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SSMA(data, period, adjust=adjust)

    def finta_EMA(self, period: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EMA(data, period, adjust=adjust)

    def finta_DEMA(self, period: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().DEMA(data, period, adjust=adjust)

    def finta_TEMA(self, period: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().DEMA(data, period, adjust=adjust)

    def finta_TRIMA(self, period: int = 18, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().TRIMA(data, period)

    def finta_TRIX(self, period: int = 20, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().TRIX(data, period, adjust=adjust)

    def finta_LWMA(self, period: int = 10, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().LWMA(data, period)

    def finta_VAMA(self, period: int = 8, **kwargs):
        """args:ohlcv,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VAMA(data, period)

    def finta_VIDYA(self, period: int = 9, smoothing_period: int = 12, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VIDYA(data, period, smoothing_period)

    def finta_ER(self, period: int = 10, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ER(data, period)

    def finta_KAMA(self, er: int = 10, ema_fast: int = 2, ema_slow: int = 30, period: int = 20, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().KAMA(data, er, ema_fast, ema_slow, period)

    def finta_ZLEMA(self, period: int = 26, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ZLEMA(data, period, adjust)

    def finta_WMA(self, period: int = 9, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().WMA(data, period)

    def finta_HMA(self, period: int = 16, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().HMA(data, period)

    def finta_EVWMA(self, period: int = 20, **kwargs):
        """args:ohlcv,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EVWMA(data, period)

    def finta_VWAP(self, **kwargs):
        """args:ohlcv,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VWAP(data)

    def finta_SMMA(self, period: int = 42, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SMMA(data, period, adjust=adjust)

    def finta_ALMA(self, period: int = 9, sigma: int = 6, offset: int = 0.85, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ALMA(data, period, sigma, offset)

    def finta_MAMA(self, period: int = 16, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().MAMA(data, period)

    def finta_FRAMA(self, period: int = 16, batch: int = 10, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().FRAMA(data, period, batch)

    def finta_MACD(self, period_fast: int = 12, period_slow: int = 26, signal: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().MACD(data, period_fast, period_slow, signal, adjust=adjust)

    def finta_PPO(self, period_fast: int = 12, period_slow: int = 26, signal: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().PPO(data, period_fast, period_slow, signal, adjust=adjust)

    def finta_VW_MACD(self, period_fast: int = 12, period_slow: int = 26, signal: int = 9, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VW_MACD(data, period_fast, period_slow, signal, adjust=adjust)

    def finta_EV_MACD(self, period_fast: int = 20, period_slow: int = 40, signal: int = 9, adjust: bool = True, **kwargs):
        """args:ohlcv,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EV_MACD(data, period_fast, period_slow, signal, adjust=adjust)

    def finta_MOM(self, period: int = 10, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().MOM(data, period)

    def finta_ROC(self, period: int = 12, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ROC(data, period)

    def finta_VBM(self, roc_period: int = 12, atr_period: int = 26, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VBM(data, roc_period, atr_period)

    def finta_RSI(self, period: int = 14, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().RSI(data, period, adjust=adjust)

    def finta_IFT_RSI(self, rsi_period: int = 5, wma_period: int = 9, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().IFT_RSI(data, "close", rsi_period, wma_period)

    def finta_SWI(self, period: int = 16, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SWI(data, period)

    def finta_DYMI(self, adjust: bool = True, **kwargs):
        """args:close,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().DYMI(data, adjust=adjust)

    def finta_TR(self, **kwargs):
        """args:ohlcv,kwargs:filed"""
        data = self._finta_get_data(**kwargs)
        return self.FinTa().TR(data)

    def finta_ATR(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ATR(data, period)

    def finta_SAR(self, af: int = 0.02, amax: int = 0.2, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SAR(data, af, amax)

    def finta_PSAR(self, iaf: int = 0.02, maxaf: int = 0.2, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().PSAR(data, iaf, maxaf)

    def finta_BBANDS(self, period: int = 20, MA: pd.Series = None, std_multiplier: float = 2, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().BBANDS(data, period, MA, std_multiplier=std_multiplier)

    def finta_MOBO(self, period: int = 10, std_multiplier: float = 0.8, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().MOBO(data, period, std_multiplier)

    def finta_BBWIDTH(self, period: int = 20, MA: pd.Series = None, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().BBWIDTH(data, period, MA)

    def finta_PERCENT_B(self, period: int = 20, MA: pd.Series = None, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().PERCENT_B(data, period, MA)

    def finta_KC(self, period: int = 20, atr_period: int = 10, MA: pd.Series = None, kc_mult: float = 2, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().KC(data, period, atr_period, MA, kc_mult)

    def finta_DO(self, upper_period: int = 20, lower_period: int = 5, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().DO(data, upper_period, lower_period)

    def finta_DMI(self, period: int = 14, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().DMI(data, period, adjust)

    def finta_ADX(self, period: int = 14, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ADX(data, period, adjust)

    def finta_PIVOT(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().PIVOT(data)

    def finta_PIVOT_FIB(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().PIVOT_FIB(data)

    def finta_STOCH(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().STOCH(data, period)

    def finta_STOCHD(self, period: int = 3, stoch_period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().STOCHD(data, period, stoch_period)

    def finta_STOCHRSI(self, rsi_period: int = 14, stoch_period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().STOCHRSI(data, rsi_period, stoch_period)

    def finta_WILLIAMS(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().WILLIAMS(data, period)

    def finta_UO(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().UO(data)

    def finta_AO(self, slow_period: int = 34, fast_period: int = 5, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().AO(data, slow_period, fast_period)

    def finta_MI(self, period: int = 9, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().MI(data, period, adjust)

    def finta_BOP(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().BOP(data)

    def finta_VORTEX(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VORTEX(data, period)

    def finta_KST(self, r1: int = 10, r2: int = 15, r3: int = 20, r4: int = 30, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().KST(data, r1, r2, r3, r4)

    def finta_TSI(self, long: int = 25, short: int = 13, signal: int = 13, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().TSI(data, long, short, signal, adjust=adjust)

    def finta_TP(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().TP(data)

    def finta_ADL(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ADL(data)

    def finta_CHAIKIN(self, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().CHAIKIN(data, adjust)

    def finta_MFI(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().CHAIKIN(data, period)

    def finta_OBV(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().OBV(data)

    def finta_WOBV(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().WOBV(data)

    def finta_VZO(self, period: int = 14, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VZO(data, period, adjust=adjust)

    def finta_PZO(self, period: int = 14, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().PZO(data, period, adjust=adjust)

    def finta_EFI(self, period: int = 13, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EFI(data, period, adjust=adjust)

    def finta_CFI(self, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().CFI(data, adjust=adjust)

    def finta_EBBP(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EBBP(data)

    def finta_EMV(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EMV(data, period)

    def finta_CCI(self, period: int = 20, constant: float = 0.015, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().CCI(data, period, constant)

    def finta_COPP(self, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().COPP(data, adjust)

    def finta_BASP(self, period: int = 40, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().BASP(data, period, adjust)

    def finta_BASPN(self, period: int = 40, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().BASPN(data, period, adjust)

    def finta_CMO(self, period: int = 9, factor: int = 100, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().CMO(data, period, factor, adjust=adjust)

    def finta_CHANDELIER(self, short_period: int = 22, long_period: int = 22, k: int = 3, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().CHANDELIER(data, short_period, long_period, k)

    def finta_QSTICK(self, period: int = 14, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().QSTICK(data, period)

    def finta_TMF(self, period: int = 21, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().TMF(data, period)

    def finta_WTO(self, channel_length: int = 10, average_length: int = 21, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().WTO(data, channel_length, average_length, adjust)

    def finta_FISH(self, period: int = 10, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().FISH(data, period, adjust)

    def finta_ICHIMOKU(self, tenkan_period: int = 9, kijun_period: int = 26, senkou_period: int = 52, chikou_period: int = 26, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().ICHIMOKU(data, tenkan_period, kijun_period, senkou_period, chikou_period)

    def finta_APZ(self, period: int = 21, dev_factor: int = 2, MA: pd.Series = None, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().APZ(data, period, dev_factor, MA, adjust)

    def finta_SQZMI(self, period: int = 20, MA: pd.Series = None, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SQZMI(data, period, MA)

    def finta_VPT(self, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().SQZMI(data)

    def finta_FVE(self, period: int = 22, factor: int = 0.3, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().FVE(data, period, factor)

    def finta_VFI(self, period: int = 130, smoothing_factor: int = 3, factor: int = 0.2, vfactor: int = 2.5, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().VFI(data, period, smoothing_factor, factor, vfactor, adjust)

    def finta_MSD(self, period: int = 21, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().MSD(data, period)

    def finta_STC(self, period_fast: int = 23, period_slow: int = 50, k_period: int = 10, d_period: int = 3, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().STC(data, period_fast, period_slow, k_period, d_period, adjust=adjust)

    def finta_EVSTC(self, period_fast: int = 12, period_slow: int = 30, k_period: int = 10, d_period: int = 3, adjust: bool = True, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().EVSTC(data, period_fast, period_slow, k_period, d_period, adjust=adjust)

    def finta_WILLIAMS_FRACTAL(self, period: int = 2, **kwargs):
        data = self._finta_get_data(**kwargs)
        return self.FinTa().WILLIAMS_FRACTAL(data, period)

    def finta_VC(self, period: int = 5, **kwargs):
        ohlc = self._finta_get_data(**kwargs)
        float_axis = ((ohlc.high + ohlc.low) / 2).rolling(window=period).mean()
        vol_unit = (ohlc.high - ohlc.low).rolling(window=period).mean() * 0.2

        value_chart_high = pd.Series(
            (ohlc.high - float_axis) / vol_unit, name="Value Chart High")
        value_chart_low = pd.Series(
            (ohlc.low - float_axis) / vol_unit, name="Value Chart Low")
        value_chart_close = pd.Series(
            (ohlc.close - float_axis) / vol_unit, name="Value Chart Close")
        value_chart_open = pd.Series(
            (ohlc.open - float_axis) / vol_unit, name="Value Chart Open")
        return pd.concat([value_chart_open, value_chart_high, value_chart_low, value_chart_close], axis=1)

    def finta_WAVEPM(self, period: int = 14, lookback_period: int = 100, **kwargs):
        ohlc = self._finta_get_data(**kwargs)
        ma = ohlc['close'].rolling(window=period).mean()
        std = ohlc['close'].rolling(window=period).std(ddof=0)

        def tanh(x):
            two = np.where(x > 0, -2, 2)
            what = two * x
            ex = np.exp(what)
            j = 1 - ex
            k = ex - 1
            l = np.where(x > 0, j, k)
            output = l / (1 + ex)
            return output

        def osc(input_dev, mean, power):
            variance = pd.Series(power).rolling(
                window=lookback_period).sum() / lookback_period
            calc_dev = np.sqrt(variance) * mean
            y = (input_dev / calc_dev)
            oscLine = tanh(y)
            return oscLine

        dev = 3.2 * std
        power = np.power(dev / ma, 2)
        wavepm = osc(dev, ma, power)

        return pd.Series(wavepm, name="{0} period WAVEPM".format(period))

    # 天勤函数
    def tqfunc_ref(self, length=10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.ref(*arg, **kwarg)

    def tqfunc_std(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.std(*arg, **kwarg)

    def tqfunc_ma(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.ma(*arg, **kwarg)

    def tqfunc_sma(self, n: int = 10, m: int = 2, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.sma(*arg, **kwarg)

    def tqfunc_ema(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.ema(*arg, **kwarg)

    def tqfunc_ema2(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.ema2(*arg, **kwarg)

    def tqfunc_crossup(self, b=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        b = kwarg.pop('b')
        return TqFunc.crossup(*arg, b=b, **kwarg)

    def tqfunc_crossdown(self, b=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        b = kwarg.pop('b')
        return TqFunc.crossdown(*arg, b=b, **kwarg)

    def tqfunc_count(self, length: int = 10, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.count(*arg, **kwarg)

    def tqfunc_trma(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.trma(*arg, **kwarg)

    def tqfunc_harmean(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.harmean(*arg, **kwarg)

    def tqfunc_numpow(self, n: int = 10, m: int = 2, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.numpow(*arg, **kwarg)

    def tqfunc_abs(self, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.abs(*arg, **kwarg)

    def tqfunc_min(self, b=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        a = kwarg.pop('a', arg[0])
        b = kwarg.pop('b')
        return TqFunc.min(a, b, **kwarg)

    def tqfunc_max(self, b=None, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        a = kwarg.pop('a', arg[0])
        b = kwarg.pop('b')
        return TqFunc.max(a, b, **kwarg)

    def tqfunc_median(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.median(*arg, **kwarg)

    def tqfunc_exist(self, length: int = 10, **kwargs):
        arg, kwarg = self._get_column_(locals(), "close")
        return TqFunc.exist(*arg, **kwarg)

    def tqfunc_every(self, length: int = 10, **kwargs):
        arg, kwarg = self._get_column_(locals(), "close")
        arg.append(length)
        return TqFunc.every(*arg)

    def tqfunc_hhv(self, length: int = 10, **kwargs):
        """args : high"""
        arg, kwarg = self._get_column_(locals(), 'high')
        return TqFunc.hhv(*arg, **kwarg)

    def tqfunc_llv(self, length: int = 10, **kwargs):
        """args : low"""
        arg, kwarg = self._get_column_(locals(), 'low')
        return TqFunc.llv(*arg, **kwarg)

    def tqfunc_avedev(self, length: int = 10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.avedev(*arg, **kwarg)

    def tqfunc_barlast(self, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.barlast(*arg, **kwarg)

    def tqfunc_cum_counts(self, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return TqFunc.cum_counts(*arg)

    def tqfunc_time_to_ns_timestamp(self, **kwargs):
        """args : datetime"""
        arg, kwarg = self._get_column_(locals(), *FILED.D)
        input_time = arg[0]
        return input_time.apply(TqFunc.tqfunc().time_to_ns_timestamp)

    def tqfunc_time_to_s_timestamp(self, **kwargs):
        """args : datetime"""
        arg, kwarg = self._get_column_(locals(), *FILED.D)
        input_time = arg[0]
        return input_time.apply(TqFunc.tqfunc().time_to_s_timestamp)

    def tqfunc_time_to_str(self, **kwargs):
        """args : datetime"""
        arg, kwarg = self._get_column_(locals(), *FILED.D)
        input_time = arg[0]
        return input_time.apply(TqFunc.tqfunc().time_to_str)

    def tqfunc_time_to_datetime(self, **kwargs):
        """args : datetime"""
        arg, kwarg = self._get_column_(locals(), *FILED.D)
        input_time = arg[0]
        return input_time.apply(TqFunc.tqfunc().time_to_datetime)

    # tqta:天勤指标
    def tqta_ATR(self, n=14, **kwargs) -> pd.DataFrame:
        # hlc,tr,atr
        return TqTa.ATR(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_BIAS(self, n=6, **kwargs) -> pd.Series:
        # c
        return TqTa.BIAS(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_BOLL(self, n=26, p=2, **kwargs) -> pd.DataFrame:
        # c,mid,top,bottom
        return TqTa.BOLL(get_tqta_dataframe(self, FILED.C, **kwargs), n, p)

    def tqta_DMI(self, n=14, m=6, **kwargs) -> pd.DataFrame:
        # hlc,atr,pdi,mdi,adx,adxr
        return TqTa.DMI(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m)

    def tqta_KDJ(self, n=9, m1=3, m2=3, **kwargs) -> pd.DataFrame:
        # hlc,k,d,j
        return TqTa.KDJ(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m1, m2)

    def tqta_MACD(self, short=12, long=26, m=9, **kwargs) -> pd.DataFrame:
        # c,diff,dea,bar
        return TqTa.MACD(get_tqta_dataframe(self, FILED.C, **kwargs), short, long, m)

    def tqta_SAR(self, n=4, step=0.02, max=0.2, **kwargs) -> pd.Series:
        # ohlc
        return TqTa.SAR(get_tqta_dataframe(self, FILED.OHLC, **kwargs), n, step, max)

    def tqta_WR(self, n=14, **kwargs) -> pd.Series:
        # hlc
        return TqTa.WR(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_RSI(self, n=7, **kwargs) -> pd.Series:
        # c
        return TqTa.RSI(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_ASI(self, **kwargs) -> pd.Series:
        # ohlc
        return TqTa.ASI(get_tqta_dataframe(self, FILED.OHLC, **kwargs))

    def tqta_VR(self, n=26, **kwargs) -> pd.Series:
        # cv
        return TqTa.VR(get_tqta_dataframe(self, FILED.CV, **kwargs), n)

    def tqta_ARBR(self, n=26, **kwargs) -> pd.DataFrame:
        # ohlc ,ar,br
        return TqTa.ARBR(get_tqta_dataframe(self, FILED.OHLC, **kwargs), n)

    def tqta_DMA(self, short=10, long=50, m=10, **kwargs) -> pd.DataFrame:
        # c,ddd,ama
        return TqTa.DMA(get_tqta_dataframe(self, FILED.C, **kwargs), short, long, m)

    def tqta_EXPMA(self, p1=5, p2=10, **kwargs) -> pd.DataFrame:
        # c,ma1,ma2
        return TqTa.EXPMA(get_tqta_dataframe(self, FILED.C, **kwargs), p1, p2)

    def tqta_CR(self, n=26, m=5, **kwargs) -> pd.DataFrame:
        # hlc,cr,crma
        return TqTa.CR(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m)

    def tqta_CCI(self, n=14, **kwargs):
        # hlc
        return TqTa.CCI(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_OBV(self, **kwargs) -> pd.Series:
        # cv
        return TqTa.OBV(get_tqta_dataframe(self, FILED.CV, **kwargs))

    def tqta_CDP(self, n=3, **kwargs) -> pd.DataFrame:
        # hlc,ah,al,nh,nl
        return TqTa.CDP(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_HCL(self, n=10, **kwargs) -> pd.DataFrame:
        # hlc,mah,mal,mac
        return TqTa.HCL(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_ENV(self, n=14, k=6, **kwargs) -> pd.DataFrame:
        # c,upper,lower
        return TqTa.ENV(get_tqta_dataframe(self, FILED.C, **kwargs), n, k)

    def tqta_MIKE(self, n=10, **kwargs) -> pd.DataFrame:
        # hlc,wr,mr,sr,ws,ms,ss
        return TqTa.MIKE(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_PUBU(self, m=4, **kwargs) -> pd.Series:
        # c
        return TqTa.PUBU(get_tqta_dataframe(self, FILED.C, **kwargs), m)

    def tqta_BBI(self, n1=3, n2=6, n3=12, n4=24, **kwargs) -> pd.Series:
        # c
        return TqTa.BBI(get_tqta_dataframe(self, FILED.C, **kwargs), n1, n2, n3, n4)

    def tqta_DKX(self, m=10, **kwargs) -> pd.DataFrame:
        # ohlc,b,d
        return TqTa.DKX(get_tqta_dataframe(self, FILED.OHLC, **kwargs), m)

    def tqta_BBIBOLL(self, n=10, m=3, **kwargs) -> pd.DataFrame:
        # close,bbiboll,upr,dwn
        return TqTa.BBIBOLL(get_tqta_dataframe(self, FILED.C, **kwargs), n, m)

    def tqta_ADTM(self, n=23, m=8, **kwargs) -> pd.DataFrame:
        # ohl,adtm,adtmma
        return TqTa.ADTM(get_tqta_dataframe(self, FILED.OHL, **kwargs), n, m)

    def tqta_B3612(self, **kwargs) -> pd.DataFrame:
        # c,b36,b612
        return TqTa.B3612(get_tqta_dataframe(self, FILED.C, **kwargs))

    def tqta_DBCD(self, n=5, m=16, t=76, **kwargs) -> pd.DataFrame:
        # c,dbcd,mm
        return TqTa.DBCD(get_tqta_dataframe(self, FILED.C, **kwargs), n, m, t)

    def tqta_DDI(self, n=13, n1=30, m=10, m1=5, **kwargs) -> pd.DataFrame:
        # hl,ddi,addi,ad
        return TqTa.DDI(get_tqta_dataframe(self, FILED.HL, **kwargs), n, n1, m, m1)

    def tqta_KD(self, n=9, m1=3, m2=3, **kwargs) -> pd.DataFrame:
        # hlc,k,d
        return TqTa.KD(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m1, m2)

    def tqta_LWR(self, n=9, m=3, **kwargs) -> pd.Series:
        # hlc
        return TqTa.LWR(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m)

    def tqta_MASS(self, n1=9, n2=25, **kwargs) -> pd.Series:
        # hl
        return TqTa.MASS(get_tqta_dataframe(self, FILED.HL, **kwargs), n1, n2)

    def tqta_MFI(self, n=14, **kwargs) -> pd.Series:
        # hlcv
        return TqTa.MFI(get_tqta_dataframe(self, FILED.HLCV, **kwargs), n)

    def tqta_MI(self, n=12, **kwargs) -> pd.DataFrame:
        # c,a,mi
        return TqTa.MI(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_MICD(self, n=3, n1=10, n2=20, **kwargs) -> pd.DataFrame:
        # c,dif,micd
        return TqTa.MICD(get_tqta_dataframe(self, FILED.C, **kwargs), n, n1, n2)

    def tqta_MTM(self, n=6, n1=6, **kwargs) -> pd.DataFrame:
        # c,mtm,mtmma
        return TqTa.MTM(get_tqta_dataframe(self, FILED.C, **kwargs), n, n1)

    def tqta_PRICEOSC(self, long=26, short=12, **kwargs) -> pd.Series:
        # close
        return TqTa.PRICEOSC(get_tqta_dataframe(self, FILED.C, **kwargs), long, short)

    def tqta_PSY(self, n=12, m=6, **kwargs) -> pd.DataFrame:
        # c,psy,psyma
        return TqTa.PSY(get_tqta_dataframe(self, FILED.C, **kwargs), n, m)

    def tqta_QHLSR(self, **kwargs) -> pd.DataFrame:
        # hlcv,qhl5,qhl10
        return TqTa.QHLSR(get_tqta_dataframe(self, FILED.HLCV, **kwargs))

    def tqta_RC(self, n=50, **kwargs) -> pd.Series:
        # c
        return TqTa.RC(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_RCCD(self, n=10, n1=21, n2=28, **kwargs) -> pd.DataFrame:
        # c,dif,rccd
        return TqTa.RCCD(get_tqta_dataframe(self, FILED.C, **kwargs), n, n1, n2)

    def tqta_ROC(self, n=24, m=20, **kwargs) -> pd.DataFrame:
        # c,roc,rocma
        return TqTa.ROC(get_tqta_dataframe(self, FILED.C, **kwargs), n, m)

    def tqta_SLOWKD(self, n=9, m1=3, m2=3, m3=3, **kwargs) -> pd.DataFrame:
        # hlc,k,d
        return TqTa.SLOWKD(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m1, m2, m3)

    def tqta_SRDM(self, n=30, **kwargs) -> pd.DataFrame:
        # hlc,srdm,asrdm
        return TqTa.SRDM(get_tqta_dataframe(self, FILED.HLC, **kwargs), n)

    def tqta_SRMI(self, n=9, **kwargs) -> pd.DataFrame:
        # c,a,mi
        return TqTa.SRMI(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_ZDZB(self, n1=50, n2=5, n3=20, **kwargs) -> pd.DataFrame:
        # c,b,d
        return TqTa.ZDZB(get_tqta_dataframe(self, FILED.C, **kwargs), n1, n2, n3)

    def tqta_DPO(self, **kwargs) -> pd.Series:
        # c
        return TqTa.DPO(get_tqta_dataframe(self, FILED.C, **kwargs))

    def tqta_LON(self, **kwargs) -> pd.DataFrame:
        # hlcv,lon,ma1
        return TqTa.LON(get_tqta_dataframe(self, FILED.HLCV, **kwargs))

    def tqta_SHORT(self, **kwargs) -> pd.DataFrame:
        # hlcv,short,ma1
        return TqTa.SHORT(get_tqta_dataframe(self, FILED.HLCV, **kwargs))

    def tqta_MV(self, n=10, m=20, **kwargs) -> pd.DataFrame:
        # v,mv1,mv2
        return TqTa.MV(get_tqta_dataframe(self, FILED.V, **kwargs), n, m)

    def tqta_WAD(self, n=10, m=30, **kwargs) -> pd.DataFrame:
        # hlc,a,b,e
        return TqTa.WAD(get_tqta_dataframe(self, FILED.HLC, **kwargs), n, m)

    def tqta_AD(self, **kwargs) -> pd.Series:
        # hlcv
        return TqTa.AD(get_tqta_dataframe(self, FILED.HLCV, **kwargs))

    def tqta_CCL(self, close_oi=None, **kwargs) -> pd.Series:
        # c
        return TqTa.CCL(get_tqta_dataframe(self, FILED.C, **kwargs))

    def tqta_CJL(self, close_oi=None, **kwargs) -> pd.DataFrame:
        # v,vol,opid
        return TqTa.CJL(get_tqta_dataframe(self, FILED.V, **kwargs))

    # def tqta_OPI(self, close_oi=None, **kwargs) -> pd.Series:
    #     return TqTa.OPI(get_tqta_dataframe(self, ["close_oi"], **kwargs))

    def tqta_PVT(self, **kwargs) -> pd.Series:
        # cv
        return TqTa.PVT(get_tqta_dataframe(self, FILED.CV, **kwargs))

    def tqta_VOSC(self, short=12, long=26, **kwargs) -> pd.Series:
        # v
        return TqTa.VOSC(get_tqta_dataframe(self, FILED.V, **kwargs), short, long)

    def tqta_VROC(self, n=12, **kwargs) -> pd.Series:
        # v
        return TqTa.VROC(get_tqta_dataframe(self, FILED.V, **kwargs), n)

    def tqta_VRSI(self, n=6, **kwargs) -> pd.Series:
        # v
        return TqTa.VRSI(get_tqta_dataframe(self, FILED.V, **kwargs), n)

    def tqta_WVAD(self, **kwargs) -> pd.Series:
        # ohlcv
        return TqTa.WVAD(get_tqta_dataframe(self, FILED.OHLCV, **kwargs))

    def tqta_MA(self, n=30, **kwargs) -> pd.Series:
        # c
        return TqTa.MA(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_SMA(self, n=5, m=2, **kwargs) -> pd.Series:
        # c
        return TqTa.SMA(get_tqta_dataframe(self, FILED.C, **kwargs), n, m)

    def tqta_EMA(self, n=10, **kwargs) -> pd.Series:
        # c
        return TqTa.EMA(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_EMA2(self, n=10, **kwargs) -> pd.Series:
        # c
        return TqTa.EMA2(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    def tqta_TRMA(self, n=10, **kwargs) -> pd.Series:
        # c
        return TqTa.TRMA(get_tqta_dataframe(self, FILED.C, **kwargs), n)

    # 配对交易指标
    # def PairTrading(self):
    #     from .ta_ import PairTrading
    #     return PairTrading()

    def pair_bollinger_bands(self, window=60, num_std=2., **kwargs) -> pd.DataFrame:
        data=get_data(self)
        return PairTrading.bollinger_bands_strategy(data.astype(np.float64), window, num_std)

    def pair_percentage_deviation(self, window=60, threshold=0.1, **kwargs) -> pd.Series | pd.DataFrame:
        data=get_data(self)
        return PairTrading.percentage_deviation_strategy(data.astype(np.float64), window, threshold)

    def pair_rolling_quantile(self, window=60, upper_quantile=0.95, lower_quantile=0.05, **kwargs) -> pd.DataFrame:
        data=get_data(self)
        return PairTrading.rolling_quantile_strategy(data.astype(np.float64), window, upper_quantile, lower_quantile)

    def pair_z_score(self, window=60, z_threshold=2.0, **kwargs)-> pd.DataFrame | pd.Series:
        data=get_data(self)
        return PairTrading.z_score_strategy(data.astype(np.float64), window, z_threshold)

    def pair_hurst_filter(self,window=20, hurst_threshold=0.5, z_threshold=2.0, **kwargs) -> pd.DataFrame:
        data=get_data(self)
        return PairTrading.hurst_filter_strategy(data.astype(np.float64), window, hurst_threshold, z_threshold)

    def pair_kalman_filter(self, window=60, state_var_init=1e-4, obs_var=1e-2, **kwargs) -> pd.DataFrame:
        data=get_data(self)
        return PairTrading.kalman_filter(data.astype(np.float64), window, state_var_init, obs_var)

    def pair_garch_volatility_adjusted(self, z_threshold=2.0, **kwargs) -> pd.DataFrame:
        data=get_data(self)
        return PairTrading.garch_volatility_adjusted_signals(data.astype(np.float64), z_threshold)

    def pair_vecm_based(self, y_series=None, window=60, lag=2, **kwargs) -> pd.DataFrame | pd.Series:
        data=get_data(self)
        return PairTrading.vecm_based_signals(data.astype(np.float64), y_series, window, lag)

    def factor_single_asset_multi_factor_strategy(self, *factors: pd.DataFrame, window=10, top_pct=0.2, bottom_pct=0.2, isstand=True, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        price = arg[0]
        factors_df = get_factors_df(*factors)
        return Factors.single_asset_multi_factor_strategy(price, factors_df, window=window, top_pct=top_pct, bottom_pct=bottom_pct, isstand=isstand)

    def factor_evaluate_factors(self, *factors: pd.DataFrame, window=20, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        price = arg[0]
        factors_df = get_factors_df(*factors)
        return Factors.evaluate_factors(price, factors_df, window=window)

    def factor_pca_trend_indicator(self, *factors: pd.DataFrame, n_components=2,
                                   dynamic_sign=True, filter_low_variance=True):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        price = arg[0]
        factors_df = get_factors_df(*factors)
        return Factors.pca_trend_indicator(price, factors_df, n_components, dynamic_sign, filter_low_variance)

    def factor_adaptive_weight_trend(self, windows=[5, 20, 50], lookback=10, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return Factors.adaptive_weight_trend(arg[0], windows=windows, lookback=lookback)

    def factor_factor_optimizer(self, *factors: pd.DataFrame,
                                max_weight: float = 0.8, l2_reg: float = 0.0001,
                                min_ic_abs: float = 0.03, n_init_points: int = 10,
                                optimization_model: str = "scipy", **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        price = arg[0]
        factors_df = get_factors_df(*factors)
        return Factors.FactorOptimizer(price,
                                            factors_df,
                                            max_weight=max_weight,
                                            l2_reg=l2_reg,
                                            min_ic_abs=min_ic_abs,
                                            n_init_points=n_init_points,
                                            optimization_model=optimization_model)
    # 自定义指标
    # btind

    def btind_smoothrng(self, length: int = 14, mult: float = 1., **kwargs) -> pd.Series:
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.smoothrng(*arg, **kwarg)

    def btind_rngfilt(self, r: pd.Series = None, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.rngfilt(*arg, **kwarg)

    def btind_alerts(self, length=None, mult=None, **kwargs):
        """alerts指标,args : close
        https://cn.tradingview.com/script/ETB76oav/"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.alerts(*arg, **kwarg)

    # price density 价格密度函数

    def btind_noises_density(self, length: int = 10, **kwargs) -> pd.Series:
        """args : high, low"""
        arg, kwarg = self._get_column_(locals(), *FILED.HL)
        return BtFunc.noises_density(*arg, **kwarg)

    # ER效率系数

    def btind_noises_er(self, length: int = 10, **kwargs) -> pd.Series:
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.noises_er(*arg, **kwarg)

    # fractal dimension 分型维度

    def btind_noises_fd(self, length: int = 10, **kwargs) -> pd.Series:
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return BtFunc.noises_fd(*arg, **kwarg)

    def btind_kama(self, length=None, fast=None, slow=None, drift=None, offset=None, **kwargs):
        """Indicator: Kaufman's Adaptive Moving Average (KAMA)
        args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.kama(*arg, **kwarg)

    def btind_mama(self, fastlimit=0.6185, slowlimit=0.06185, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.mama(*arg, **kwarg)

    def btind_pmax(self, length=14, mult=1.6185, mode='hma', dev='stdev', **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.pmax(*arg, **kwarg)

    def btind_pmax2(self, length=14, mult=1.6185, mode='hma', dev='stdev', **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.pmax2(*arg, **kwarg)

    def btind_pmax3(self, length=14, mult=1.6185, mode='hma', dev='stdev', **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.pmax3(*arg, **kwarg)

    def btind_pv(self, length=10, **kwargs):
        """args : close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.pv(*arg, **kwarg)

    def btind_realized(self, length: int = 10, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.realized(*arg, **kwarg)

    def btind_rsrs(self, length: int = 10, method='r1', weights=True, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.HLV)
        return BtFunc.rsrs(*arg, **kwarg)

    def btind_savitzky_golay(self, window_length: int = 10, polyorder: int = 2, deriv: int = 0, delta: float = 1, axis: int = -1, mode: str = 'interp', cval: float = 0, **kwargs):
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.savitzky_golay(*arg, **kwarg)

    def btind_supertrend(self, length=14, multiplier=2., weights=2., **kwargs):
        """args:high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return BtFunc.supertrend(*arg, **kwarg)

    def btind_zigzag(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> pd.Series:
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return ZigZag.zigzag(*arg, **kwarg)

    def btind_zigzag_full(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> pd.Series:
        """args : high, low, close"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return ZigZag.zigzag_full(*arg, **kwarg)

    def btind_zigzag_modes(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., **kwargs) -> pd.Series:
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return ZigZag.zigzag_modes(*arg, **kwarg)

    def btind_zigzag_returns(self, up_thresh: float = 0., down_thresh: float = 0., multiplier: float = 1., limit: bool = True, **kwargs) -> pd.Series:
        arg, kwarg = self._get_column_(locals(), *FILED.HLC)
        return ZigZag.zigzag_returns(*arg, **kwarg)

    def btind_AndeanOsc(self, length: int = 14, signal_length: int = 9, **kwargs) -> pd.DataFrame:
        """args :open,close"""
        arg, kwarg = self._get_column_(locals(), *FILED.OC)
        return BtFunc.AndeanOsc(*arg, **kwarg)

    def btind_Coral_Trend_Candles(self, smooth: int = 9., mult: float = .4, **kwargs) -> pd.Series:
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.Coral_Trend_Candles(*arg, **kwarg)

    def btind_signal_returns_stats(self, close=None, n: int = 1, **kwargs):
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.signal_returns_stats(*arg, **kwarg)

    def btind_calculate_trend_probabilities(self, window_length=60,
                                            up_threshold=0.001,
                                            down_threshold=-0.001, **kwargs):
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.calculate_trend_probabilities(*arg, **kwarg)

    def btind_gap_ratio(self, length=20, **kwargs):
        "args:open,high,low,close"
        arg, kwarg = self._get_column_(locals(), *FILED.OHLC)
        return BtFunc.gap_ratio(*arg, **kwarg)

    def btind_vwap_window(self, window=20, offset=None, **kwargs):
        """args:high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return BtFunc.vwap_window(*arg, **kwarg)

    def btind_vwap_volume_based(self, volume_quantile=0.25, lookback=100, offset=None, **kwargs):
        """args:high, low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HLCV)
        return BtFunc.vwap_volume_based(*arg, **kwarg)

    def btind_gaussian_smoothed(self, window:int=11,**kwargs):
        """args:close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.gaussian_smoothed(*arg, **kwarg)
    
    def btind_varma(self, length: int = 10, var_length: int = 10, **kwargs):
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.varma(*arg, **kwarg)
    
    def btind_rsswma(self, length: int = 10, **kwargs):
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.rsswma(*arg, **kwarg)
    
    def btind_pivot_high(self, left: int=5, right: int=5,**kwargs) :
        """args :high"""
        arg, kwarg = self._get_column_(locals(), *FILED.H)
        return BtFunc.pivot_high(*arg, **kwarg)
    
    def btind_pivot_low(self, left: int=5, right: int=5,**kwargs) :
        """args :low"""
        arg, kwarg = self._get_column_(locals(), *FILED.L)
        return BtFunc.pivot_low(*arg, **kwarg)
    
    def btind_linreg_slope(self, length: int=10, **kwargs) :
        """args :close"""
        arg, kwarg = self._get_column_(locals(), *FILED.C)
        return BtFunc.linreg_slope(*arg,**kwarg)
    
    def btind_vwap_high(self,length:int=10,**kwargs) :
        """args :high, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.HCV)
        return BtFunc.vwap_high(*arg, **kwarg)
    
    def btind_vwap_low(self,length:int=10,**kwargs) :
        """args :low, close, volume"""
        arg, kwarg = self._get_column_(locals(), *FILED.LCV)
        return BtFunc.vwap_low(*arg, **kwarg)



# setattr(AnalysisIndicators, 'btta', property(lambda self: self._df.ta))
# setattr(SeriesIndicators, 'btta', property(lambda self: self._df.ta))
for atrk, atrv in CoreFunc.__dict__.items():
    if not atrk.startswith('_') and isinstance(atrv, Callable):
        setattr(AnalysisIndicators, atrk, atrv)
        setattr(SeriesIndicators, atrk, atrv)

AnalysisIndicators.__name__ = 'DataFrameIndicators'
#AnalysisIndicators.__bases__ = (LazyImport,) + AnalysisIndicators.__bases__
for name, attr in LazyImport.__dict__.items():
    if isinstance(attr, classmethod):
        setattr(AnalysisIndicators, name, attr)
        setattr(SeriesIndicators, name, attr)
