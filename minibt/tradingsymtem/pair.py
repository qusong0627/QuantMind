from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine


class BollingerBands(BtIndicator):
    """
    ## 布林带配对交易指标类

    使用布林带策略识别价差偏离，生成交易信号。
    当价差突破布林带上轨时做空，跌破下轨时做多。

    参数说明：
    - window: 计算移动平均线和标准差的窗口大小（默认60）
    - std: 标准差倍数，用于确定布林带宽度（默认2.0）
    - lower_level: 做多信号阈值（默认-20）
    - upper_level: 做空信号阈值（默认20）
    - exit_long_level: 多头平仓阈值（默认0）
    - exit_short_level: 空头平仓阈值（默认0）

    生成信号：
    - long_signal: 多头入场信号（价差上穿下轨且低于lower_level）
    - short_signal: 空头入场信号（价差下穿上轨且高于upper_level）
    - exitlong_signal: 多头平仓信号（价差上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（价差下穿exit_short_level）
    """
    params = dict(
        window=60,
        std=2.0,
        lower_level=-20,
        upper_level=20,
        exit_long_level=0,
        exit_short_level=0,
    )

    def __init__(self):
        result = self.pairtrading.bollinger_bands(window=self.params.window, std=self.params.std)
        self.lines.long_signal = result.spread.cross_up(result.lower_band)
        self.lines.long_signal &= result.spread < self.params.lower_level
        self.lines.short_signal = result.spread.cross_down(result.upper_band)
        self.lines.short_signal &= result.spread > self.params.upper_level
        self.lines.exitlong_signal = result.spread.cross_up(self.params.exit_long_level)
        self.lines.exitshort_signal = result.spread.cross_down(self.params.exit_short_level)


class PercentageDeviation(BtIndicator):
    """
    ## 百分比偏差配对交易指标类

    计算价差相对于移动平均的百分比偏离程度，生成交易信号。

    参数说明：
    - window: 计算移动平均的窗口大小（默认60）
    - lower_level: 做多信号阈值（默认-100）
    - upper_level: 做空信号阈值（默认100）
    - exit_long_level: 多头平仓阈值（默认0）
    - exit_short_level: 空头平仓阈值（默认0）

    生成信号：
    - long_signal: 多头入场信号（百分比偏差上穿lower_level）
    - short_signal: 空头入场信号（百分比偏差下穿upper_level）
    - exitlong_signal: 多头平仓信号（百分比偏差上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（百分比偏差下穿exit_short_level）
    """
    params = dict(
        window=60,
        lower_level=-2,
        upper_level=2,
        exit_long_level=0,
        exit_short_level=0,
    )

    def __init__(self):
        result = self.pairtrading.percentage_deviation(window=self.params.window)
        self.lines.long_signal = result.pct_deviation.cross_up(self.params.lower_level)
        self.lines.short_signal = result.pct_deviation.cross_down(self.params.upper_level)
        self.lines.exitlong_signal = result.pct_deviation.cross_up(self.params.exit_long_level)
        self.lines.exitshort_signal = result.pct_deviation.cross_down(self.params.exit_short_level)


class RollingQuantile(BtIndicator):
    """
    ## 移动窗口分位数配对交易指标类

    使用移动窗口的分位数作为动态阈值，识别价差的极端偏离情况。

    参数说明：
    - window: 计算分位数的滚动窗口大小（默认60）
    - upper_quantile: 上分位数阈值（默认0.95）
    - lower_quantile: 下分位数阈值（默认0.05）
    - exit_long_level: 多头平仓阈值（默认0.5）
    - exit_short_level: 空头平仓阈值（默认0.5）

    生成信号：
    - long_signal: 多头入场信号（价差上穿下分位数阈值）
    - short_signal: 空头入场信号（价差下穿上分位数阈值）
    - exitlong_signal: 多头平仓信号（价差上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（价差下穿exit_short_level）
    """
    params = dict(
        window=60,
        upper_quantile=0.95,
        lower_quantile=0.05,
        lower_level=-20,
        upper_level=20,
        exit_long_level=0,
        exit_short_level=0,
    )

    def __init__(self):
        result = self.pairtrading.rolling_quantile(
            window=self.params.window,
            upper_quantile=self.params.upper_quantile,
            lower_quantile=self.params.lower_quantile
        )
        self.lines.long_signal = result.spread.cross_up(result.lower_threshold)
        self.lines.long_signal &= result.spread < self.params.lower_level
        self.lines.short_signal = result.spread.cross_down(result.upper_threshold)
        self.lines.short_signal &= result.spread > self.params.upper_level
        self.lines.exitlong_signal = result.spread.cross_up(self.params.exit_long_level)
        self.lines.exitshort_signal = result.spread.cross_down(self.params.exit_short_level)


class ZScore(BtIndicator):
    """
    ## Z-score配对交易指标类

    计算价差的Z-score（标准分数），基于统计学原理识别价差的极端偏离。

    参数说明：
    - window: 计算均值和标准差的滚动窗口大小（默认60）
    - lower_level: 做多信号阈值（默认-2）
    - upper_level: 做空信号阈值（默认2）
    - exit_long_level: 多头平仓阈值（默认0）
    - exit_short_level: 空头平仓阈值（默认0）

    生成信号：
    - long_signal: 多头入场信号（Z-score上穿lower_level）
    - short_signal: 空头入场信号（Z-score下穿upper_level）
    - exitlong_signal: 多头平仓信号（Z-score上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（Z-score下穿exit_short_level）
    """
    params = dict(
        window=60,
        lower_level=-2,
        upper_level=2,
        exit_long_level=0,
        exit_short_level=0,
    )

    def __init__(self):
        result = self.pairtrading.z_score(window=self.params.window)
        self.lines.long_signal = result.z_score.cross_down(self.params.lower_level)
        self.lines.short_signal = result.z_score.cross_up(self.params.upper_level)
        self.lines.exitlong_signal = result.z_score.cross_up(self.params.exit_long_level)
        self.lines.exitshort_signal = result.z_score.cross_down(self.params.exit_short_level)


class HurstFilter(BtIndicator):
    """
    ## Hurst指数过滤配对交易指标类

    使用Hurst指数判断价差序列的均值回复特性，过滤掉趋势性强的价差。

    参数说明：
    - window: 计算Hurst指数和Z-score的窗口大小（默认20）
    - lower_level: 做多信号阈值（默认-2）
    - upper_level: 做空信号阈值（默认2）
    - exit_long_level: 多头平仓阈值（默认0）
    - exit_short_level: 空头平仓阈值（默认0）

    生成信号：
    - long_signal: 多头入场信号（Z-score上穿lower_level且具有均值回复性）
    - short_signal: 空头入场信号（Z-score下穿upper_level且具有均值回复性）
    - exitlong_signal: 多头平仓信号（Z-score上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（Z-score下穿exit_short_level）
    """
    params = dict(
        window=20,
        lower_level=-2,
        upper_level=2,
        exit_long_level=0,
        exit_short_level=0,
    )

    def __init__(self):
        result = self.pairtrading.hurst_filter(window=self.params.window)
        self.lines.long_signal = result.z_score.cross_up(self.params.lower_level)
        self.lines.short_signal = result.z_score.cross_down(self.params.upper_level)
        self.lines.exitlong_signal = result.z_score.cross_up(self.params.exit_long_level)
        self.lines.exitshort_signal = result.z_score.cross_down(self.params.exit_short_level)


class KalmanFilter(BtIndicator):
    """
    ## 卡尔曼滤波配对交易指标类

    使用一维卡尔曼滤波逐点递推估计动态对冲比率，计算价差并标准化。

    参数说明：
    - window: 计算Z-score的滚动窗口长度（默认20）
    - state_var_init: 过程噪声方差（默认0.0001）
    - obs_var: 观测噪声方差（默认0.01）
    - lower_level: 做多信号阈值（默认-2）
    - upper_level: 做空信号阈值（默认2）
    - exit_long_level: 多头平仓阈值（默认0）
    - exit_short_level: 空头平仓阈值（默认0）

    生成信号：
    - long_signal: 多头入场信号（zscores上穿lower_level）
    - short_signal: 空头入场信号（zscores下穿upper_level）
    - exitlong_signal: 多头平仓信号（zscores上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（zscores下穿exit_short_level）
    """
    params = dict(
        window=20,
        state_var_init=0.0001,
        obs_var=0.01,
        lower_level=-2,
        upper_level=2,
        exit_long_level=.5,
        exit_short_level=-0.5,
    )

    def __init__(self):
        result = self.pairtrading.kalman_filter(
            window=self.params.window,
            state_var_init=self.params.state_var_init,
            obs_var=self.params.obs_var
        )
        self.lines.long_signal = result.zscores.cross_up(self.params.upper_level)
        self.lines.short_signal = result.zscores.cross_down(self.params.lower_level)
        self.lines.exitlong_signal = result.zscores.cross_down(self.params.exit_long_level)
        self.lines.exitshort_signal = result.zscores.cross_up(self.params.exit_short_level)


class GarchVolatilityAdjusted(BtIndicator):
    """
    ## GARCH波动率调整配对交易指标类

    使用GARCH(1,1)模型估计时变波动率，计算波动率调整的Z-score。

    参数说明：
    - lower_level: 做多信号阈值（默认-2）
    - upper_level: 做空信号阈值（默认2）
    - exit_long_level: 多头平仓阈值（默认0）
    - exit_short_level: 空头平仓阈值（默认0）

    生成信号：
    - long_signal: 多头入场信号（garch_z_score上穿lower_level）
    - short_signal: 空头入场信号（garch_z_score下穿upper_level）
    - exitlong_signal: 多头平仓信号（garch_z_score上穿exit_long_level）
    - exitshort_signal: 空头平仓信号（garch_z_score下穿exit_short_level）
    """
    params = dict(
        lower_level=-2,
        upper_level=2,
        exit_long_level=0,
        exit_short_level=0,
    )

    def __init__(self):
        result = self.pairtrading.garch_volatility_adjusted()
        self.lines.long_signal = result.garch_z_score.cross_up(self.params.lower_level)
        self.lines.short_signal = result.garch_z_score.cross_down(self.params.upper_level)
        self.lines.exitlong_signal = result.garch_z_score.cross_up(self.params.exit_long_level)
        self.lines.exitshort_signal = result.garch_z_score.cross_down(self.params.exit_short_level)


class Pair:
    """
    ## 配对交易策略类

    - 配对交易是一种统计套利策略，通过识别具有长期均衡关系的两只或多只资产，
    - 当它们之间的价差偏离历史均值时，分别做多和做空，等待价差回归时平仓获利。

    ## 策略方法分类：

    ### 基础方法：
    - `bollinger_bands`: 布林带策略 - 使用布林带识别价差偏离
    - `percentage_deviation`: 百分比偏差策略 - 基于百分比偏离识别交易机会
    - `rolling_quantile`: 移动窗口分位数策略 - 使用分位数识别极端偏离
    - `z_score`: Z-score策略 - 基于标准分数识别统计套利机会

    ### 高级方法：
    - `hurst_filter`: Hurst指数过滤策略 - 使用Hurst指数过滤趋势性价差
    - `kalman_filter`: 卡尔曼滤波策略 - 动态估计对冲比率和价差
    - `garch_volatility_adjusted`: GARCH模型波动率调整策略 - 考虑时变波动率

    ## 使用方式：

    ```python
    # 导入模块
    from minibt import *

    # 创建配对数据,price_a和price_b是两个资产的K线数据
    data = IndFrame(dict(a=price_a, b=price_b))

    # 使用配对策略
    # 只有kalman_filter不需要基差，其余配对指标都基于基差计算
    result = data.tradingsystem.pair.kalman_filter() 
    result = (data.b-data.a).tradingsystem.pair.bollinger_bands()
    # 访问信号
    long_signals = result.long_signal
    short_signals = result.short_signal
    ```

    ## 筛选方法与交易策略对应关系：

    如需筛选符合条件的配对交易标的，请使用 `minibt.tools` 模块：

    ```python
    from minibt.tools import PairAnalyzer

    # 创建分析器
    pa = PairAnalyzer()

    # 单对分析
    result = pa.coint(leg_a, leg_b)
    if result['is_suitable']:
        # 符合条件，可以交易
        pass

    # 批量筛选
    pairs = pa.find(contracts, method='coint')
    ```
    """

    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        """
        初始化配对交易策略类

        Parameters
        ----------
        data : KLine | IndFrame | IndSeries, optional
            输入数据，可以是K线数据、指标框架或指标序列，默认None
        """
        self.data = data

    def bollinger_bands(self, window=60, std=2.0, lower_level=-20, upper_level=20,
                        exit_long_level=0, exit_short_level=0, **kwargs) -> IndFrame:
        """
        ## 布林带配对交易策略

        使用布林带识别价差偏离，生成交易信号。

        **底层逻辑：**
        ```
        spread_mean = spread.rolling(window).mean()
        spread_std = spread.rolling(window).std()
        upper_band = spread_mean + std * spread_std
        lower_band = spread_mean - std * spread_std
        ```

        **适用条件：**
        - 价差序列需满足均值回复特性（Hurst指数 < 0.5）
        - 价差序列应平稳（ADF检验p值 < 0.05）

        Parameters
        ----------
        window : int, default=60
            计算移动平均线和标准差的窗口大小

        std : float, default=2.0
            标准差倍数，用于确定布林带宽度

        lower_level : int, default=-20
            做多信号阈值（归一化价差低于此值时触发）

        upper_level : int, default=20
            做空信号阈值（归一化价差高于此值时触发）

        exit_long_level : int, default=0
            多头平仓阈值

        exit_short_level : int, default=0
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用布林带策略
        result = spread_indseriestradingsystem.pair.bollinger_bands(window=60, std=2.0)

        # 访问信号
        long_signal = result.long_signal
        short_signal = result.short_signal
        ```
        """
        params = dict(
            window=window,
            std=std,
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return BollingerBands(self.data, **kwargs)

    def percentage_deviation(self, window=60, lower_level=-2, upper_level=2,
                            exit_long_level=0, exit_short_level=0, **kwargs) -> IndFrame:
        """
        ## 百分比偏差配对交易策略

        计算价差相对于移动平均的百分比偏离程度，生成交易信号。

        **底层逻辑：**
        ```
        spread_mean = spread.rolling(window).mean()
        pct_deviation = (spread - spread_mean) / spread_mean * 100
        ```

        **适用条件：**
        - 价差序列需满足均值回复特性（Hurst指数 < 0.5）
        - 价差均值不应接近零

        Parameters
        ----------
        window : int, default=60
            计算移动平均的窗口大小

        lower_level : int, default=-100
            做多信号阈值（百分比偏差低于此值时触发）

        upper_level : int, default=100
            做空信号阈值（百分比偏差高于此值时触发）

        exit_long_level : int, default=0
            多头平仓阈值

        exit_short_level : int, default=0
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用百分比偏差策略
        result = spread_indseriestradingsystem.pair.percentage_deviation(window=30)

        # 访问信号
        signals = result[['long_signal', 'short_signal']]
        ```
        """
        params = dict(
            window=window,
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return PercentageDeviation(self.data, **kwargs)

    def rolling_quantile(self, window=60, upper_quantile=0.95, lower_quantile=0.05,
                         lower_level=-20, upper_level=20,
                         exit_long_level=0., exit_short_level=0., **kwargs) -> IndFrame:
        """
        ## 移动窗口分位数配对交易策略

        使用移动窗口的分位数作为动态阈值，识别价差的极端偏离情况。

        **底层逻辑：**
        ```
        upper_threshold = spread.rolling(window).quantile(upper_quantile)
        lower_threshold = spread.rolling(window).quantile(lower_quantile)
        ```

        **适用条件：**
        - 价差序列需满足均值回复特性（Hurst指数 < 0.5）
        - 价差分布应相对稳定

        Parameters
        ----------
        window : int, default=60
            计算分位数的滚动窗口大小

        upper_quantile : float, default=0.95
            上分位数阈值（0-1之间）

        lower_quantile : float, default=0.05
            下分位数阈值（0-1之间）

        exit_long_level : float, default=0.5
            多头平仓阈值

        exit_short_level : float, default=0.5
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用分位数策略
        result = spread_indseriestradingsystem.pair.rolling_quantile(
            upper_quantile=0.90,
            lower_quantile=0.10
        )

        # 访问信号
        signals = result.signals
        ```
        """
        params = dict(
            window=window,
            upper_quantile=upper_quantile,
            lower_quantile=lower_quantile,
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return RollingQuantile(self.data, **kwargs)

    def z_score(self, window=60, lower_level=-2, upper_level=2,
                exit_long_level=0.5, exit_short_level=-0.5, **kwargs) -> IndFrame:
        """
        ## Z-score配对交易策略

        计算价差的Z-score（标准分数），基于统计学原理识别价差的极端偏离。

        **底层逻辑：**
        ```
        spread_mean = spread.rolling(window).mean()
        spread_std = spread.rolling(window).std()
        z_score = (spread - spread_mean) / spread_std
        ```

        **适用条件：**
        - 价差序列需满足均值回复特性（Hurst指数 < 0.5）
        - 价差序列应平稳（ADF检验p值 < 0.05）
        - 价差应近似正态分布

        Parameters
        ----------
        window : int, default=60
            计算均值和标准差的滚动窗口大小

        lower_level : int, default=-2
            做多信号阈值（Z-score低于此值时触发）

        upper_level : int, default=2
            做空信号阈值（Z-score高于此值时触发）

        exit_long_level : int, default=0
            多头平仓阈值

        exit_short_level : int, default=0
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用Z-score策略
        result = spread_indseriestradingsystem.pair.z_score(window=60, lower_level=-2.5)

        # 访问信号
        signals = result[['long_signal', 'short_signal']]
        ```
        """
        params = dict(
            window=window,
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return ZScore(self.data, **kwargs)

    def hurst_filter(self, window=20, lower_level=-2, upper_level=2,
                     exit_long_level=0, exit_short_level=0, **kwargs) -> IndFrame:
        """
        ## Hurst指数过滤配对交易策略

        使用Hurst指数判断价差序列的均值回复特性，过滤掉趋势性强的价差。

        **底层逻辑：**
        ```
        # 计算Hurst指数
        hurst = calculate_hurst_exponent(spread)

        # 如果Hurst < 0.5，应用Z-score策略
        # 如果Hurst >= 0.5，不生成信号（趋势性太强）
        ```

        **适用条件：**
        - 适用于不确定价差是否具有均值回复性的情况
        - 可作为前置过滤条件

        Parameters
        ----------
        window : int, default=20
            计算Hurst指数和Z-score的窗口大小

        lower_level : int, default=-2
            做多信号阈值

        upper_level : int, default=2
            做空信号阈值

        exit_long_level : int, default=0
            多头平仓阈值

        exit_short_level : int, default=0
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用Hurst过滤策略
        result = spread_indseriestradingsystem.pair.hurst_filter(window=20)

        # 访问信号
        signals = result.signals
        ```
        """
        params = dict(
            window=window,
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return HurstFilter(self.data, **kwargs)

    def kalman_filter(self, window=20, state_var_init=0.0001, obs_var=0.01,
                      lower_level=-2, upper_level=2, exit_long_level=0.5,
                      exit_short_level=-0.5, **kwargs) -> IndFrame:
        """
        ## 卡尔曼滤波配对交易策略

        使用一维卡尔曼滤波逐点递推估计动态对冲比率，计算价差并标准化。

        **底层逻辑：**
        ```
        # 动态估计对冲比率
        for i in range(len(price)):
            # 预测
            pred_var = state_var[i-1] + state_var_init

            # 卡尔曼增益
            k_gain = pred_var / (pred_var * price_x[i]**2 + obs_var)

            # 更新对冲比率
            state_mean[i] = state_mean[i-1] + k_gain * (price_y[i] - state_mean[i-1] * price_x[i])

            # 更新方差
            state_var[i] = (1 - k_gain * price_x[i]) * pred_var

            # 计算价差
            spread[i] = price_y[i] - state_mean[i] * price_x[i]
        ```

        **适用条件：**
        - 两资产需存在协整关系
        - 对冲比率随时间变化

        Parameters
        ----------
        window : int, default=20
            计算Z-score的滚动窗口长度

        state_var_init : float, default=0.0001
            过程噪声方差（控制对冲比率变化幅度）

        obs_var : float, default=0.01
            观测噪声方差（控制滤波器对观测值的信任程度）

        lower_level : int, default=-2
            做多信号阈值

        upper_level : int, default=2
            做空信号阈值

        exit_long_level : int, default=0
            多头平仓阈值

        exit_short_level : int, default=0
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用卡尔曼滤波策略
        data = IndFrame(dict(a=price_a, b=price_b))
        result = data.tradingsystem.pair.kalman_filter(
            window=30,
            state_var_init=0.0001,
            obs_var=0.01
        )

        # 访问信号
        signals = result.signals
        ```
        """
        params = dict(
            window=window,
            state_var_init=state_var_init,
            obs_var=obs_var,
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return KalmanFilter(self.data, **kwargs)

    def garch_volatility_adjusted(self, lower_level=-2, upper_level=2,
                                   exit_long_level=0, exit_short_level=0, **kwargs) -> IndFrame:
        """
        ## GARCH波动率调整配对交易策略

        使用GARCH(1,1)模型估计时变波动率，计算波动率调整的Z-score。

        **底层逻辑：**
        ```
        # 拟合GARCH(1,1)模型
        model = arch_model(spread, vol='GARCH', p=1, q=1)
        garch_results = model.fit()

        # 获取条件波动率
        volatility = garch_results.conditional_volatility

        # 计算波动率调整的Z-score
        garch_z_score = (spread - spread_mean) / volatility
        ```

        **适用条件：**
        - 价差序列存在波动率聚类效应
        - 价差序列具有时变波动率特征

        Parameters
        ----------
        lower_level : int, default=-2
            做多信号阈值

        upper_level : int, default=2
            做空信号阈值

        exit_long_level : int, default=0
            多头平仓阈值

        exit_short_level : int, default=0
            空头平仓阈值

        **kwargs : dict
            其他关键字参数

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - long_signal: 多头入场信号
            - short_signal: 空头入场信号
            - exitlong_signal: 多头平仓信号
            - exitshort_signal: 空头平仓信号

        Examples
        --------
        ```python
        # 使用GARCH波动率调整策略
        result = spread_indseriestradingsystem.pair.garch_volatility_adjusted()

        # 访问信号
        signals = result.signals
        ```
        """
        params = dict(
            lower_level=lower_level,
            upper_level=upper_level,
            exit_long_level=exit_long_level,
            exit_short_level=exit_short_level,
        )
        kwargs['params'] = params
        return GarchVolatilityAdjusted(self.data, **kwargs)
