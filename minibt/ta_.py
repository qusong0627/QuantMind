# -*- coding: utf-8 -*-
from warnings import simplefilter
import pandas_ta as pta
import numpy as np
import pandas as pd
from typing import Callable, Any,  Literal,TYPE_CHECKING
from collections.abc import Iterable,Sequence, Mapping, Generator,Iterator
# from numpy.random import RandomState
import threading
from pandas_ta.utils import signed_series, v_bool, v_pos_default, v_series
if TYPE_CHECKING:
    from finta import TA as FinTa
    import talib
    import tulipy
    from .SingleAssetFactorOptimizer import SingleAssetFactorOptimizer
    from pykalman import KalmanFilter
    from arch import arch_model
    import sklearn.decomposition as sd
    import sklearn.preprocessing as sp
    from tqsdk import tafunc
    from tqsdk import ta
    import statsmodels.api as sm
    from scipy import stats
model_lock = threading.Lock()
_sklearn_preprocessing = None
_sklearn_decomposition = None
_arch_model = None
_KalmanFilter = None
_ti = None
_talib = None
_SingleAssetFactorOptimizer = None
_PairTrading = None
_FinTa = None
_TqFunc = None
_TqTa = None
_StatsmodelsApi = None
_ScipyStats = None

__all__ = ["LazyImport","PairTrading","Factors"]


def vp(
    close: pd.Series, volume: pd.Series,
    width: int = None, sort: bool = None,
    **kwargs
) -> pd.DataFrame:
    """Volume Profile

    This indicator attempts to quantify volume across binned price ranges of
    certain width.

    Sources:
        * [ranchodinero](http://www.ranchodinero.com/volume-tpo-essentials/)
        * [stockcharts](https://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:volume_by_price)
        * [tradingtechnologies](https://www.tradingtechnologies.com/blog/2013/05/15/volume-at-price/)
        * [tradingview](https://www.tradingview.com/wiki/Volume_Profile)

    Parameters:
        close (Series): ```close``` Series
        volume (Series): ```volume``` Series
        width (int): Source distrubution count. Default: ```10```
        sort (value): Sort ```close``` before splitting into ranges.
            Default: ```False```

    Other Parameters:
        fillna (value): ```pd.DataFrame.fillna(value)```

    Returns:
        (DataFrame): 5 columns

    Note:
        * By default, sorts by date index or chronological.
        * Value Area is not calculated.

    Warning:
        **Volume Profile** not a Time ```Series```. It is a volume distribution
        snapshot for an arbitrary ```DateTime``` Index and thus can not be
        concatenated onto the existing ```DataFrame```.

    """
    # Validate
    width = v_pos_default(width, 10)
    close = v_series(close, width)
    volume = v_series(volume, width)

    if close is None or volume is None:
        return

    # Calculate
    signed_price = signed_series(close, 1)
    pos_volume = volume * signed_price[signed_price > 0]
    pos_volume.name = volume.name
    neg_volume = -volume * signed_price[signed_price < 0]
    neg_volume.name = volume.name
    neut_volume = volume + signed_price[signed_price == 0]
    neut_volume.name = volume.name
    vp = pd.concat([close, pos_volume, neg_volume, neut_volume], axis=1)

    close_col = f"{vp.columns[0]}"
    high_price_col = f"high_{close_col}"
    low_price_col = f"low_{close_col}"
    mean_price_col = f"mean_{close_col}"

    volume_col = f"{vp.columns[1]}"
    pos_volume_col = f"pos_{volume_col}"
    neg_volume_col = f"neg_{volume_col}"
    neut_volume_col = f"neut_{volume_col}"
    total_volume_col = f"total_{volume_col}"
    vp.columns = [close_col, pos_volume_col, neg_volume_col, neut_volume_col]

    simplefilter(action="ignore", category=FutureWarning)
    # sort: Sort by close before splitting into ranges. Default: False
    # If False, it sorts by date index or chronological versus by price
    if sort:
        vp[mean_price_col] = vp[close_col]

        vpdf = vp.groupby(
            cut(vp[close_col], width, include_lowest=True, precision=2),
            observed=False
        ).agg({
            mean_price_col: np.mean,
            pos_volume_col: np.sum,
            neg_volume_col: np.sum,
            neut_volume_col: np.sum
        })

        vpdf[low_price_col] = [x.left for x in vpdf.index]
        vpdf[high_price_col] = [x.right for x in vpdf.index]
        vpdf = vpdf.reset_index(drop=True)

        vpdf = vpdf[[
            low_price_col, mean_price_col, high_price_col,
            pos_volume_col, neg_volume_col, neut_volume_col
        ]]
    else:
        # 使用 DataFrame.iloc 替代 np.array_split（修复数组索引问题）
        n = len(vp)
        chunk_size = max(1, n // width)
        result = []
        for i in range(width):
            start = i * chunk_size
            end = n if i == width - 1 else (i + 1) * chunk_size
            r = vp.iloc[start:end]
            if len(r) == 0:
                continue
            result.append({
                low_price_col: r[close_col].min(),
                mean_price_col: r[close_col].mean(),
                high_price_col: r[close_col].max(),
                pos_volume_col: r[pos_volume_col].sum(),
                neg_volume_col: r[neg_volume_col].sum(),
                neut_volume_col: r[neut_volume_col].sum(),
            })

        vpdf = pd.DataFrame(result)

    vpdf[total_volume_col] = vpdf[pos_volume_col] + vpdf[neg_volume_col]

    # Fill
    if "fillna" in kwargs:
        vpdf.fillna(kwargs["fillna"], inplace=True)

    # Name and Category
    vpdf.name = f"VP_{width}"
    vpdf.category = "volume"

    return vpdf

pta.vp=vp

def ZLEMA(
    ohlc: pd.DataFrame,
    period: int = 26,
    adjust: bool = True,
    column: str = "close",
) -> pd.Series:
    """ZLEMA is an abbreviation of Zero Lag Exponential Moving Average. It was developed by John Ehlers and Rick Way.
    ZLEMA is a kind of Exponential moving average but its main idea is to eliminate the lag arising from the very nature of the moving averages
    and other trend following indicators. As it follows price closer, it also provides better price averaging and responds better to price swings."""

    lag = int((period - 1) / 2)

    ema = pd.Series(
        (ohlc[column] + (ohlc[column].diff(lag))),
        name="{0} period ZLEMA.".format(period),
    )

    zlema = pd.Series(
        ema.ewm(span=period, adjust=adjust).mean(),
        name="{0} period ZLEMA".format(period),
    )

    return zlema

def FRAMA(ohlc: pd.DataFrame, period: int = 16, batch: int=10) -> pd.Series:
    """Fractal Adaptive Moving Average
    Source: http://www.stockspotter.com/Files/frama.pdf
    Adopted from: https://www.quantopian.com/posts/frama-fractal-adaptive-moving-average-in-python

    :period: Specifies the number of periods used for FRANA calculation
    :batch: Specifies the size of batches used for FRAMA calculation
    """

    assert period % 2 == 0, print("FRAMA period must be even")

    c = ohlc.close.copy()
    window = batch * 2

    hh = c.rolling(batch).max()
    ll = c.rolling(batch).min()

    n1 = (hh - ll) / batch
    n2 = n1.shift(batch)

    hh2 = c.rolling(window).max()
    ll2 = c.rolling(window).min()
    n3 = (hh2 - ll2) / window

    # calculate fractal dimension
    D = (np.log(n1 + n2) - np.log(n3)) / np.log(2)
    alp = np.exp(-4.6 * (D - 1))
    alp = np.clip(alp, .01, 1).values

    filt = c.values.copy()
    for i, x in enumerate(alp):
        cl = c.values[i]
        if i < window:
            continue
        filt[i] = cl * x + (1 - x) * filt[i - 1]

    return pd.Series(filt, index=ohlc.index, name="{0} period FRAMA.".format(period))


def EVWMA(ohlcv: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    The eVWMA can be looked at as an approximation to the
    average price paid per share in the last n periods.

    :period: Specifies the number of Periods used for eVWMA calculation
    """

    vol_sum = (
        ohlcv["volume"].rolling(window=period).sum()
    )  # floating shares in last N periods

    x = (vol_sum - ohlcv["volume"]) / vol_sum
    y = (ohlcv["volume"] * ohlcv["close"]) / vol_sum

    evwma = [0]

    #  evwma = (evma[-1] * (vol_sum - volume)/vol_sum) + (volume * price / vol_sum)
    for x, y in zip(x.fillna(0).items(), y.items()):
        if x[1] == 0 or y[1] == 0:
            evwma.append(0)
        else:
            evwma.append(evwma[-1] * x[1] + y[1])

    return pd.Series(
        evwma[1:], index=ohlcv.index, name="{0} period EVWMA.".format(period),
    )


def KAMA(
    ohlc: pd.DataFrame,
    er: int = 10,
    ema_fast: int = 2,
    ema_slow: int = 30,
    period: int = 20,
    column: str = "close",
) -> pd.Series:
    """Developed by Perry Kaufman, Kaufman's Adaptive Moving Average (KAMA) is a moving average designed to account for market noise or volatility.
    Its main advantage is that it takes into consideration not just the direction, but the market volatility as well."""

    change = ohlc[column].diff(period).abs()
    volatility = ohlc[column].diff().abs().rolling(window=period).sum()

    er = pd.Series(change / volatility, name="{0} period ER".format(period))
    fast_alpha = 2 / (ema_fast + 1)
    slow_alpha = 2 / (ema_slow + 1)
    sc = pd.Series(
        (er * (fast_alpha - slow_alpha) + slow_alpha) ** 2,
        name="smoothing_constant",
    )  # smoothing constant

    sma = pd.Series(
        ohlc[column].rolling(period).mean(), name="SMA"
    )  # first KAMA is SMA
    kama = []
    # Current KAMA = Prior KAMA + smoothing_constant * (Price - Prior KAMA)
    for s, ma, price in zip(
        sc.items(), sma.shift().items(), ohlc[column].items()
    ):
        try:
            kama.append(kama[-1] + s[1] * (price[1] - kama[-1]))
        except (IndexError, TypeError):
            if pd.notnull(ma[1]):
                kama.append(ma[1] + s[1] * (price[1] - ma[1]))
            else:
                kama.append(None)

    sma["KAMA"] = pd.Series(
        kama, index=sma.index, name="{0} period KAMA.".format(period)
    )  # apply the kama list to existing index
    return sma["KAMA"]


class LazyImport(object):

    @classmethod
    def scipy_stats(cls)->"stats":
        global _ScipyStats
        if _ScipyStats is None:
            with model_lock:  # 确保多线程安全
                try:
                    from scipy import stats
                    _ScipyStats = stats
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装scipy才能使用此功能: pip install scipy"
                    ) from e
        return _ScipyStats

    @classmethod
    def statsmodels_api(cls)->"sm":
        global _StatsmodelsApi
        if _StatsmodelsApi is None:
            with model_lock:  # 确保多线程安全
                try:
                    import statsmodels.api as sm
                    _StatsmodelsApi = sm
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装statsmodels才能使用此功能: pip install statsmodels"
                    ) from e
        return _StatsmodelsApi

    @classmethod
    def tqfunc(cls)->"tafunc":
        global _TqFunc
        if _TqFunc is None:
            with model_lock:  # 确保多线程安全
                try:
                    from .other import FilteredOutputRedirector
                    with FilteredOutputRedirector():
                        from tqsdk import tafunc
                        _TqFunc = tafunc
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装tqsdk才能使用此功能: pip install tqsdk"
                    ) from e
        return _TqFunc

    @classmethod
    def tqta(cls)->"ta":
        global _TqTa
        if _TqTa is None:
            with model_lock:  # 确保多线程安全
                try:
                    from .other import FilteredOutputRedirector
                    with FilteredOutputRedirector():
                        from tqsdk import ta
                        _TqTa = ta
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装tqsdk才能使用此功能: pip install tqsdk"
                    ) from e
        return _TqTa

    @classmethod
    def sp(cls)->"sp":
        global _sklearn_preprocessing
        if _sklearn_preprocessing is None:
            with model_lock:
                try:
                    import sklearn.preprocessing as sp
                    _sklearn_preprocessing = sp
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装sklearn才能使用此功能: pip install sklearn"
                    ) from e
        return _sklearn_preprocessing

    @classmethod
    def sklearn_decomposition(cls)->"sd":
        global _sklearn_decomposition
        if _sklearn_decomposition is None:
            with model_lock:
                try:
                    import sklearn.decomposition as sd
                    _sklearn_decomposition = sd
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装sklearn才能使用此功能: pip install sklearn"
                    ) from e
        return _sklearn_decomposition

    @classmethod
    def arch_model(cls)->"arch_model":
        global _arch_model
        if _arch_model is None:
            with model_lock:
                try:
                    from arch import arch_model
                    _arch_model = arch_model
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装arch才能使用此功能: pip install arch"
                    ) from e
        return _arch_model

    @classmethod
    def KalmanFilter(cls) -> "KalmanFilter":
        global _KalmanFilter
        if _KalmanFilter is None:
            with model_lock:
                try:
                    from pykalman import KalmanFilter
                    _KalmanFilter = KalmanFilter
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装pykalman才能使用此功能: pip install pykalman"
                    ) from e
        return _KalmanFilter

    @classmethod
    def SingleAssetFactorOptimizer(cls) -> "SingleAssetFactorOptimizer":
        global _SingleAssetFactorOptimizer
        if _SingleAssetFactorOptimizer is None:
            with model_lock:
                if _SingleAssetFactorOptimizer is None:
                    from .SingleAssetFactorOptimizer import SingleAssetFactorOptimizer
                    _SingleAssetFactorOptimizer = SingleAssetFactorOptimizer
        return _SingleAssetFactorOptimizer

    @classmethod
    def tulipy(cls) -> "tulipy":
        global _ti
        if _ti is None:
            with model_lock:
                try:
                    import tulipy
                    _ti = tulipy
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装tulipy才能使用此功能: pip install tulipy"
                    ) from e
        return _ti

    @classmethod
    def talib(cls) -> "talib":
        global _talib
        if _talib is None:
            with model_lock:
                try:
                    import talib
                    _talib = talib
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装talib才能使用此功能: pip install TA-Lib"
                    ) from e
        return _talib

    @classmethod
    def FinTa(cls) -> "FinTa":
        global _FinTa
        if _FinTa is None:
            with model_lock:
                try:
                    from finta import TA as FinTa
                    _FinTa = FinTa
                    _FinTa.EVWMA = EVWMA
                    _FinTa.KAMA = KAMA
                    _FinTa.ZLEMA = ZLEMA
                    _FinTa.FRAMA = FRAMA
                except ImportError as e:
                    raise RuntimeError(
                        "需要安装talib才能使用此功能: pip install FinTa"
                    ) from e
        return _FinTa

    @classmethod
    def PairTrading(cls) -> "PairTrading":
        return PairTrading

    @classmethod
    def Factors(cls) -> "Factors":
        return Factors


def transform_array(X: Any) -> np.ndarray:
    if hasattr(X, "_df"):
        X = X._df
    assert isinstance(X, Iterable)
    if hasattr(X, "values"):
        X = X.values
    if not isinstance(X, np.ndarray):
        X = np.array(X)
    if len(X.shape) < 2:
        X = X.reshape(-1, 1)
    return X


class PairTrading(LazyImport):
    """## 配对交易

    ### method:
    #### 基础方法：
    >>> bollinger_bands_strategy:布林带
        percentage_deviation_strategy:百分比偏差
        rolling_quantile_strategy:移动窗口分位数
        z_score_strategy:Z-score

    #### 高级方法:
    >>> hurst_filter_strategy:Hurst指数过滤
        kalman_filter_strategy:卡尔曼滤波
        garch_volatility_adjusted_signals:GARCH模型
        vecm_based_signals:VECM模型"""

    @staticmethod
    def bollinger_bands_strategy(data: pd.DataFrame | pd.Series, window=60, num_std=2., **kwargs) -> pd.DataFrame:
        """使用布林带生成交易信号"""
        # 计算移动均值和标准差
        if data.ndim == 1:
            spread_series = data
        else:
            a,b=list(data.columns)
            spread_series = data[a] - data[b]
        spread_mean = spread_series.rolling(window=window).mean()
        spread_std = spread_series.rolling(window=window).std()

        # 计算上下轨
        upper_band = num_std * spread_std
        lower_band = -upper_band
        series = spread_series-spread_mean
        # 生成信号：突破上轨做空，突破下轨做多
        signals = np.where(series > upper_band, -1.,
                           np.where(series < lower_band, 1., 0.))
        return pd.DataFrame(dict(
            spread=series,
            upper_band=upper_band,
            lower_band=lower_band,
            signals=signals
        ))

    # ------------------------------
    # 基础方法：百分比偏差
    # ------------------------------

    @staticmethod
    def percentage_deviation_strategy(data: pd.DataFrame | pd.Series, window=60, threshold=0.1, **kwargs) -> pd.Series | pd.DataFrame:
        """使用百分比偏差生成交易信号"""
        if data.ndim == 1:
            spread_series = data
        else:
            a,b=list(data.columns)
            spread_series = data[a] - data[b]
        spread_mean = spread_series.rolling(window=window).mean()

        # 计算百分比偏差 (spread - mean) / mean * 100
        # 避免除以零
        spread_mean = spread_mean.replace(0, 1e-10)
        pct_deviation = (spread_series - spread_mean) / spread_mean * 100.

        # 生成信号
        signals = np.where(pct_deviation > threshold, -1.,
                           np.where(pct_deviation < -threshold, 1., 0))
        return pd.DataFrame(dict(
            pct_deviation=pct_deviation,
            signals=signals
        ))

    # ------------------------------
    # 基础方法：移动窗口分位数
    # ------------------------------

    @staticmethod
    def rolling_quantile_strategy(data: pd.DataFrame | pd.Series, window=60, upper_quantile=0.95, lower_quantile=0.05, **kwargs) -> pd.DataFrame:
        """使用移动窗口分位数生成交易信号"""
        if data.ndim == 1:
            spread_series = data
        else:
            a,b=list(data.columns)
            spread_series = data[a] - data[b]
        spread_mean = spread_series.rolling(window=window).mean()
        # 计算滚动分位数
        upper_threshold = spread_series.rolling(
            window=window).quantile(upper_quantile)-spread_mean
        lower_threshold = spread_series.rolling(
            window=window).quantile(lower_quantile)-spread_mean
        series = spread_series-spread_mean

        # 生成信号
        signals = np.where(series > upper_threshold, -1.,
                           np.where(series < lower_threshold, 1., 0.))

        return pd.DataFrame(dict(
            spread=series,
            upper_threshold=upper_threshold,
            lower_threshold=lower_threshold,
            signals=signals
        ))

    # ------------------------------
    # 基础方法：Z-score
    # ------------------------------

    @staticmethod
    def z_score_strategy(data: pd.DataFrame | pd.Series, window=60, z_threshold=2.0, **kwargs) -> pd.DataFrame | pd.Series:
        """使用Z-score生成交易信号"""
        if data.ndim == 1:
            spread_series = data
        else:
            a,b=list(data.columns)
            spread_series = data[a] - data[b]
        spread_mean = spread_series.rolling(window=window).mean()
        spread_std = spread_series.rolling(window=window).std()

        # 避免除以零
        spread_std = spread_std.replace(0, 1e-10)
        z_score = (spread_series - spread_mean) / spread_std
        # 生成信号
        signals = np.where(z_score > z_threshold, -1,
                           np.where(z_score < -z_threshold, 1, 0))
        return pd.DataFrame(dict(
            z_score=z_score,
            signals=signals
        ))

    # ------------------------------
    # 高级方法：Hurst指数过滤
    # ------------------------------

    @staticmethod
    def calculate_hurst_exponent(series, max_lag=20):
        """计算Hurst指数"""
        lags = range(2, max_lag + 1)

        tau = []
        for lag in lags:
            diff = np.subtract(series[lag:], series[:-lag])
            std = np.std(diff)
            tau.append(std if std != 0 else 1e-10)

        try:
            poly = np.polyfit(np.log(lags), np.log(tau), 1)
            return poly[0]
        except:
            return 0.5

    @staticmethod
    def hurst_filter_strategy(data: pd.DataFrame | pd.Series,window=20, hurst_threshold=0.5, z_threshold=2.0, **kwargs) -> pd.DataFrame:
        """使用Hurst指数过滤交易信号"""
        if data.ndim == 1:
            spread_series = data
        else:
            a,b=list(data.columns)
            spread_series = data[a] - data[b]
        hurst = PairTrading.calculate_hurst_exponent(spread_series,window)
        # print(f"Hurst指数: {hurst:.4f}")

        # 先计算Z-score
        zscore = PairTrading.z_score_strategy(
            spread_series, z_threshold=z_threshold, signal=True)
        z_signals, z_score = zscore.signals, zscore.z_score
        # 应用Hurst过滤
        if hurst >= hurst_threshold:
            signals = np.zeros_like(z_signals)
        else:
            signals = z_signals

        return pd.DataFrame(dict(
            z_score=z_score,
            signals=signals))

    # ------------------------------
    # 高级方法：卡尔曼滤波
    # ------------------------------
    
    @staticmethod
    def kalman_filter(data: pd.DataFrame , window=60, 
                      state_var_init=1e-4, obs_var=1e-2, **kwargs) -> pd.DataFrame:
        """使用卡尔曼滤波生成动态价差和交易信号"""
        assert data.ndim == 2, "data must be 2-dimensional"
        price_x = data.values[:, 0]      # leg[0] 价格（v）
        price_y = data.values[:, 1]     # leg[1] 价格（pp）
        size = len(price_x)

        state_mean = np.ones(size)
        state_var_array = np.ones(size)
        spread = np.full(size, np.nan)

        for i in range(10, size):
            pred_var = state_var_array[i - 1] + state_var_init
            k_gain = pred_var / (pred_var * price_x[i]**2 + obs_var)
            state_mean[i] = state_mean[i - 1] + k_gain * (
                price_y[i] - state_mean[i - 1] * price_x[i]
            )
            state_var_array[i] = (1 - k_gain * price_x[i]) * pred_var
            spread[i] = price_y[i] - state_mean[i] * price_x[i]

        zscores = pta.zscore(pd.Series(spread),window)
        return pd.DataFrame(dict(
            spread=spread,
            zscores=zscores,
            state_mean=state_mean
        ))
    

    # @classmethod
    # def kalman_filter_strategy(cls, x_series, y_series, z_threshold=2.,state_var_init=0.0001,obs_var=0.01,z_window=60, **kwargs) -> pd.DataFrame:
    #     """使用卡尔曼滤波生成动态价差和交易信号"""
        # 初始化卡尔曼滤波器
        # kf = cls.KalmanFilter()(
        #     transition_matrices=[[1, 0], [0, 1]],
        #     observation_matrices=[[x_series.values[0], 1]],
        #     initial_state_mean=[0, 0],
        #     initial_state_covariance=np.eye(2),
        #     observation_covariance=1.0,
        #     transition_covariance=np.eye(2) * 0.01
        # )

        # # 应用卡尔曼滤波
        # state_means, _ = kf.filter(y_series.values)
        # hedge_ratios = state_means[:, 0]
        # intercepts = state_means[:, 1]

        # # 计算动态价差
        # dynamic_spread = y_series.values - hedge_ratios * x_series.values - intercepts
        # dynamic_spread = pd.Series(dynamic_spread, index=x_series.index)
        # data = pd.DataFrame(dict(
        #     hedge_ratios=hedge_ratios,
        # ))

        # # 对动态价差应用Z-score生成信号
        # zscore = PairTrading.z_score_strategy(
        #     dynamic_spread, z_threshold=z_threshold)

        # return pd.concat([data, zscore], axis=1)

    # ------------------------------
    # 高级方法：GARCH模型
    # ------------------------------

    @staticmethod
    def garch_volatility_adjusted_signals(data: pd.DataFrame | pd.Series, z_threshold=2.0, **kwargs) -> pd.DataFrame:
        """使用GARCH模型调整波动率"""
        if data.ndim == 1:
            spread_series = data
        else:
            a,b=list(data.columns)
            spread_series = data[a] - data[b]
        # 关键修复：强制转换为数值类型，非数值转为NaN
        spread_series = pd.to_numeric(spread_series, errors='coerce')
        # 移除NaN和无穷值
        spread_series = spread_series.replace(
            [np.inf, -np.inf], np.nan).dropna()

        # 若清理后数据为空，直接返回空结果
        if spread_series.empty:
            return pd.DataFrame(columns=['volatility', 'garch_z_score', 'signals'])
        # 解决数据缩放问题
        spread_scaled = spread_series * 100

        # 拟合GARCH(1,1)模型
        model = cls.arch_model()(spread_scaled.values, vol='GARCH', p=1, q=1)
        garch_results = model.fit(disp='off')

        # 获取条件波动率并还原缩放
        volatility = pd.Series(garch_results.conditional_volatility) / 100
        spread_mean = spread_series.rolling(window=60).mean()

        # 避免除以零
        volatility = volatility.replace(0, 1e-10)
        garch_z_score = (spread_series - spread_mean) / volatility
        # 生成信号
        signals = np.where(garch_z_score > z_threshold, -1,
                           np.where(garch_z_score < -z_threshold, 1, 0))
        return pd.DataFrame(dict(
            garch_z_score=garch_z_score,
            signals=signals
        ))

    # ------------------------------
    # 高级方法：手动实现VECM模型
    # ------------------------------

    @classmethod
    def johansen_test_manual(cls,series, lags=2):
        """手动实现简化版Johansen协整检验，确保所有滞后项长度一致"""
        # 确保输入是数值型且无缺失值
        if not np.issubdtype(series.dtype, np.number):
            # 尝试转换为数值类型，非数值转为NaN
            series = np.array([pd.to_numeric(col, errors='coerce')
                              for col in series.T]).T

        # 移除包含NaN的行
        series = series[~np.isnan(series).any(axis=1)]

        # 检查数据量是否足够
        n = series.shape[0]
        k = series.shape[1]  # 变量数量

        # 确保有足够数据进行滞后计算（至少需要lags+1个样本）
        required_length = lags + 10  # 增加安全边际
        if n < required_length:
            raise ValueError(f"数据量不足，需要至少{required_length}个样本，实际只有{n}个")
        if k < 2:
            raise ValueError("至少需要2个变量进行协整检验")

        # 计算一阶差分（长度为n-1）
        diff_series = np.diff(series, axis=0)
        diff_length = len(diff_series)  # 应为n-1

        # 构建滞后项（确保所有滞后项长度与差分序列一致）
        lagged_terms = []
        max_possible_length = diff_length  # 最大可能长度为差分序列长度

        for i in range(1, lags + 1):
            # 计算当前滞后项可获取的最大长度
            current_possible_length = n - i
            # 取与差分序列长度的较小值，确保不越界
            take_length = min(max_possible_length, current_possible_length)

            # 截取滞后项，确保长度一致
            lagged = series[i:i + take_length, :]

            # 如果长度仍不足，用最后一个值填充（处理极端情况）
            if len(lagged) < max_possible_length:
                fill_length = max_possible_length - len(lagged)
                last_val = lagged[-1:] if len(lagged) > 0 else np.zeros((1, k))
                lagged = np.vstack(
                    [lagged, np.repeat(last_val, fill_length, axis=0)])

            lagged_terms.append(lagged)

        # 检查所有滞后项长度是否一致
        lengths = [len(lt) for lt in lagged_terms]
        if len(set(lengths)) != 1:
            # 最后的安全措施：统一截取到最短长度
            min_length = min(lengths)
            lagged_terms = [lt[:min_length] for lt in lagged_terms]
            diff_series = diff_series[:min_length]  # 同时调整差分序列长度
            # print(f"警告：滞后项长度不一致，已统一调整为{min_length}")

        # 合并所有滞后项为一个矩阵
        lagged_series = np.hstack(lagged_terms)

        # 构建回归模型的X矩阵 (添加常数项)
        X = cls.statsmodels_api().add_constant(lagged_series)

        # 确保X中没有NaN或无穷值
        if not np.isfinite(X).all():
            raise ValueError("回归模型输入包含非有限值，请检查原始数据")

        # 拟合OLS模型
        model = cls.statsmodels_api().OLS(diff_series, X).fit()
        u = model.resid  # 残差

        # 计算协方差矩阵
        S_uu = np.cov(u.T)
        S_ut = np.cov(u.T, series[1:1+len(diff_series), :].T)[0:k, k:]
        S_tt = np.cov(series[1:1+len(diff_series), :].T)

        # 计算特征值和特征向量（增加数值稳定性处理）
        try:
            S_tt_inv = np.linalg.pinv(S_tt)  # 使用伪逆提高稳定性
            M = np.dot(np.dot(S_ut.T, np.linalg.pinv(S_uu)), S_ut)
            eigvals, eigvecs = np.linalg.eig(np.dot(S_tt_inv, M))
        except np.linalg.LinAlgError:
            # 处理矩阵奇异的情况
            return np.array([1.0, -1.0])  # 退回到简单的价差比例

        # 返回最大特征值对应的协整向量（归一化处理）
        max_eig_idx = np.argmax(eigvals)
        coint_vector = eigvecs[:, max_eig_idx]

        # 归一化协整向量（确保第一个元素为1或-1，便于解释）
        if coint_vector[0] != 0:
            coint_vector = coint_vector / coint_vector[0]

        return coint_vector

    @staticmethod
    def vecm_based_signals(data: pd.DataFrame, window=60, lag=2, **kwargs) -> pd.DataFrame | pd.Series:
        """完全手动实现的VECM模型，确保输入数据长度一致"""
        assert data.ndim == 2, "data must be 2-dimensional"
        data.columns = ['x', 'y']
        # 如果对齐后数据不足，直接报错
        min_required = 100  # 最小数据量要求
        if len(data) < min_required:
            raise ValueError(
                f"对齐后的数据量不足，需要至少{min_required}个样本，实际只有{len(data)}个")

        # 转换为numpy数组
        series = data[['x', 'y']].values

        # 手动进行协整检验
        coint_vector = PairTrading.johansen_test_manual(series, lags=lag)

        # 计算误差修正项(ECT)
        ect = np.dot(series, coint_vector)
        ect = pd.Series(ect, index=data.index)
        ect_mean = ect.mean()
        ect -= ect_mean  # 中心化处理

        # 生成交易信号（使用滚动分位数更稳健）
        # window = max(60, len(ect) // 5)  # 动态窗口大小
        upper_threshold = ect.rolling(window=window).quantile(0.90)
        lower_threshold = ect.rolling(window=window).quantile(0.10)

        signals = np.where(ect > upper_threshold, -1.,
                           np.where(ect < lower_threshold, 1., 0.))
        return pd.DataFrame(dict(
            ect=ect,
            upper_threshold=upper_threshold,
            lower_threshold=lower_threshold,
            signals=signals
        ))


class Factors(LazyImport):

    @classmethod
    def single_asset_multi_factor_strategy(cls,price: pd.DataFrame, factors: pd.DataFrame, window=10, top_pct=0.2, bottom_pct=0.2, isstand=True, **kwargs):
        """
        单资产多因子策略实现，根据因子重要性自动设置权重
        """
        # 计算未来收益率（使用下一期收盘价的涨跌幅）
        returns_series = price.pct_change(
        ).shift(-1).fillna(0.)  # 预测下一期收益
        # 因子标准化函数 (z-score)

        def standardize(factor_series: pd.Series):  # , window: int):
            # mean = factor_series.rolling(window).mean()
            # std = factor_series.rolling(window).std()
            mean = factor_series.mean()
            std = factor_series.std()
            return (factor_series - mean) / std

        names = list(factors.columns)
        # 标准化因子
        factors = [factors[name] for name in names]  # 简化因子提取方式
        if isstand:
            factors = [standardize(factor) for factor in factors]

        # 计算因子IC (信息系数) - 单资产版本
        def calculate_single_asset_ic(factor_series: pd.Series, returns_series: pd.Series, window=20):
            rolling_ic = pd.Series(index=factor_series.index, dtype='float64')
            for i in range(window, len(factor_series)):
                start_idx = i - window
                end_idx = i
                factor_window = factor_series.iloc[start_idx:end_idx]
                returns_window = returns_series.iloc[start_idx:end_idx]
                valid_mask = ~(factor_window.isna() | returns_window.isna())
                if valid_mask.sum() < 3:
                    rolling_ic.iloc[i] = np.nan
                    continue
                ic, _ = cls.scipy_stats().spearmanr(
                    factor_window[valid_mask], returns_window[valid_mask])
                rolling_ic.iloc[i] = ic
            return rolling_ic

        # 计算各因子的IC序列
        factors_ic = [calculate_single_asset_ic(
            factor, returns_series, window) for factor in factors]

        # 计算因子权重 (基于IC的滚动表现)
        def calculate_factor_weights(ic_series: pd.Series, window=20):
            rolling_ic_mean = ic_series.abs().rolling(window).mean()
            smoothed_weights = rolling_ic_mean.ewm(span=window).mean()
            total_weight = pd.Series(0.0, index=ic_series.index)
            for ic in factors_ic:
                total_weight += ic.abs().rolling(window).mean()
            normalized_weights = smoothed_weights / total_weight
            normalized_weights = normalized_weights.fillna(1.0 / len(factors))
            return normalized_weights

        # 计算各因子的动态权重
        factors_weight = [calculate_factor_weights(
            ic, window) for ic in factors_ic]

        # 修正权重总和为1
        weight_sum = sum(factors_weight)
        # factors_weight = [weight/weight_sum for weight in factors_weight]
        valid_mask = ~(weight_sum.isna() | (weight_sum == 0))
        n_factors = len(factors_weight)
        factors_weight_corrected = []
        for weight in factors_weight:
            corrected = weight.where(valid_mask, np.nan)
            corrected = corrected / weight_sum.where(valid_mask, np.nan)
            corrected = corrected.fillna(1.0 / n_factors)
            factors_weight_corrected.append(corrected)
        # 强制修正浮点数精度
        for i in price.index:
            current_sum = sum(weight.loc[i]
                              for weight in factors_weight_corrected)
            if not np.isclose(current_sum, 1.0, atol=1e-6):
                error = 1.0 - current_sum
                factors_weight_corrected[0].loc[i] += error
        factors_weight = factors_weight_corrected

        # 生成综合得分
        combined_score = pd.Series(index=price.index, dtype='float64')
        for i in price.index:
            if i in factors[0].index:
                score = 0.0
                for j in range(len(factors)):
                    if i in factors[j].index and i in factors_weight[j].index:
                        score += factors[j].loc[i] * factors_weight[j].loc[i]
                combined_score.loc[i] = score

        # 生成交易信号
        signals = pd.Series(0, index=price.index)
        for i in price.index[window:]:
            if i in combined_score.index:
                current_score = combined_score.loc[i]
                start_idx = max(0, combined_score.index.get_loc(i) - window)
                end_idx = combined_score.index.get_loc(i)
                score_history = combined_score.iloc[start_idx:end_idx]
                if len(score_history) >= 3:
                    top_threshold = score_history.quantile(1 - top_pct)
                    bottom_threshold = score_history.quantile(bottom_pct)
                    if current_score > top_threshold:
                        signals.loc[i] = 1
                    elif current_score < bottom_threshold:
                        signals.loc[i] = -1

        # # 构建返回的指标字典
        # metrics = {f'factor_{names[i]}_weight': factors_weight[i]
        #         for i in range(len(factors))}
        # metrics.update({f'factor_{names[i]}_ic': factors_ic[i]
        #             for i in range(len(factors))})
        return pd.DataFrame(dict(
            combined_score=combined_score,
            signals=signals
        ))
        # return signals, combined_score#, metrics, factors, names
    @classmethod
    def evaluate_factors(cls,price: pd.Series, factors: pd.DataFrame, window=20, **kwargs):
        """IC均值、标准差和IR"""
        results = {}
        # 计算未来收益率（使用下一期收盘价的涨跌幅）
        returns = price.pct_change(
        ).shift(-1).fillna(0.)  # 预测下一期收益
        returns = returns.values
        for factor_name in factors.columns:
            factor = factors[factor_name].values
            # 计算滚动IC
            rolling_ic = []
            factor = factor[~np.isnan(factor)]
            for i in range(window, len(factor)):
                ic, _ = cls.scipy_stats().spearmanr(
                    factor[i-window:i], returns[i-window:i])
                rolling_ic.append(ic)
            # 计算IC均值、标准差和IR
            ic_mean = np.mean(rolling_ic)
            ic_std = np.std(rolling_ic)
            ir = ic_mean / ic_std if ic_std != 0. else 0.
            results[factor_name] = {"IC_mean": ic_mean, "IR": ir}

        # 按IC均值排序
        factor_stats = pd.DataFrame(results).T.sort_values(
            "IC_mean", ascending=False)
        valid_factors = factor_stats[(factor_stats["IC_mean"] > 0.) & (
            factor_stats["IR"] > 0.)].index
        # valid_factors = factor_stats[(factor_stats["IC_mean"] > 0.05) & (
        #     factor_stats["IR"] > 0.5)].index
        # 计算相关系数矩阵
        corr_matrix = factors[valid_factors].corr().abs()
        # 找出高度相关的因子对
        redundant_pairs = []
        for i in range(len(corr_matrix.columns)):
            for j in range(i+1, len(corr_matrix.columns)):
                if corr_matrix.iloc[i, j] > 0.8:
                    redundant_pairs.append(
                        (corr_matrix.columns[i], corr_matrix.columns[j]))

        # 保留IC更高的因子
        factors_to_remove = set()
        for pair in redundant_pairs:
            factor1, factor2 = pair
            if factor_stats.loc[factor1, "IC_mean"] > factor_stats.loc[factor2, "IC_mean"]:
                factors_to_remove.add(factor2)
            else:
                factors_to_remove.add(factor1)

        final_factors = [
            f for f in valid_factors if f not in factors_to_remove]
        return final_factors, factor_stats

    @classmethod
    def pca_trend_indicator(cls, price: pd.Series, factors: pd.DataFrame, n_components=2,
                            dynamic_sign=True, filter_low_variance=True):
        """
        使用PCA融合多个均线，生成趋势指标

        参数:
        price: 价格序列
        windows: 均线窗口列表
        n_components: 保留的主成分数量
        dynamic_sign: 是否根据主成分与价格的相关性自动调整符号
        filter_low_variance: 是否过滤低方差因子

        返回:
        优化后的PCA趋势指标
        """
        # 计算各均线
        # factors = pd.DataFrame()
        # for w in windows:
        #     factors[f'MA_{w}'] = price.rolling(w).mean()

        # 过滤低方差因子（避免PCA被常数因子干扰）
        if filter_low_variance:
            original_columns = factors.columns
            factors = factors.loc[:, factors.var() > 0.1]  # 保留方差>0.1的因子
            if len(factors.columns) < len(original_columns):
                print(f"过滤了{len(original_columns)-len(factors.columns)}个低方差因子")

        # 去除NaN
        factors = factors.dropna()
        if factors.empty:
            raise ValueError("所有因子在去除NaN后均为空")
        # 标准化
        scaler = cls.sp().StandardScaler()
        scaled_data = scaler.fit_transform(factors)

        # PCA降维
        pca = cls.sklearn_decomposition().PCA(
            n_components=min(n_components, len(factors.columns)))
        principal_components = pca.fit_transform(scaled_data)

        # 计算主成分加权组合（使用方差解释比例作为权重）
        weights = pca.explained_variance_ratio_
        combined_trend = np.average(
            principal_components, weights=weights, axis=1)

        # 转回Series
        pca_trend = pd.Series(combined_trend, index=factors.index)

        # 动态调整符号（确保与价格正相关）
        if dynamic_sign and len(pca_trend) > 10:  # 确保有足够数据计算相关性
            corr = pca_trend.corr(price.loc[pca_trend.index])
            if corr < 0:
                pca_trend = -pca_trend
                # print(f"已调整PCA趋势指标符号（原相关性：{corr:.4f}）")

        # 缩放至与价格相近的范围以便可视化
        # try:
        #     pca_trend = pca_trend * (price.std() / pca_trend.std()) + price.mean()
        # except ZeroDivisionError:
        #     print("PCA趋势指标标准差为0，使用替代缩放方法")
        #     pca_trend = pca_trend * price.std() + price.mean()

        # 返回结果和诊断信息
        # diagnostics = {
        #     'explained_variance_ratio': pca.explained_variance_ratio_,
        #     'loadings': pd.DataFrame(pca.components_, columns=factors.columns,
        #                              index=[f'PC{i+1}' for i in range(pca.n_components_)])
        # }

        return pca_trend  # , diagnostics

    def adaptive_weight_trend(price: pd.Series, windows=[5, 20, 50], lookback=60, **kwargs):
        """
        基于因子历史表现的自适应权重趋势指标

        参数:
        price: 价格序列
        windows: 均线窗口列表
        lookback: 计算权重的回溯窗口

        返回:
        自适应权重的趋势指标
        """
        # 计算各均线
        ma_list = [price.rolling(w).mean() for w in windows]

        # 初始化结果序列
        adaptive_trend = pd.Series(index=price.index, dtype=np.float64)

        for i in range(max(windows) + lookback, len(price)):
            # 回溯窗口
            start = i - lookback
            end = i

            # 计算各因子在回溯窗口内的表现（与价格的相关性）
            correlations = []
            for ma in ma_list:
                corr = ma.iloc[start:end].corr(price.iloc[start:end])
                correlations.append(corr if not np.isnan(corr) else 0)

            # 归一化权重（确保非负且和为1）
            weights = np.array(correlations)
            weights = np.maximum(weights, 0)  # 去除负权重
            weights = weights / \
                weights.sum() if weights.sum() > 0 else np.ones_like(weights) / len(weights)

            # 计算当前位置的加权趋势
            current_trend = 0
            for ma, w in zip(ma_list, weights):
                current_trend += ma.iloc[i] * w

            adaptive_trend.iloc[i] = current_trend

        return adaptive_trend

    @classmethod
    def FactorOptimizer(cls, price: pd.Series, factors: pd.DataFrame,
                        max_weight: float = 0.8, l2_reg: float = 0.0001,
                        min_ic_abs: float = 0.03, n_init_points: int = 10,
                        optimization_model: str = "scipy", **kwargs):
        returns = price.pct_change().shift(-1).fillna(0.)  # 预测下一期收益
        optimizer = cls.SingleAssetFactorOptimizer()(
            factors=factors,
            returns=returns,
            price=price.iloc[:-1],
            max_weight=max_weight,
            l2_reg=l2_reg,
            min_ic_abs=min_ic_abs,
            n_init_points=n_init_points,
            optimization_model=optimization_model  # 切换优化方法
        )
        return optimizer.build_merged_factor()
