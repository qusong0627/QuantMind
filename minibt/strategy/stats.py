"""策略统计指标模块

基于 pandas.Series 的统计类，封装了策略收益序列的各类量化分析指标，
涵盖收益分析、风险度量、比率指标、回撤分析、回撤详情、基准对比等。
所有 @get_stats 装饰的方法均通过 minibt 的工具函数链进行统一计算。
"""

from ..utils import get_stats, pd


class Stats(pd.Series):
    """策略统计指标类，继承自 pd.Series。

    用于对策略的累计收益/收益率序列进行全面的量化统计分析。
    通过 @get_stats 装饰器将底层计算委托给 minibt 工具函数链。

    Attributes:
        profit_array: 原始收益/权益数据序列（pd.Series 或类似索引序列）
        available:   初始可用资金 / 本金，用于计算盈亏与盈亏率
    """

    def __init__(self, data:pd.Series=None, index=None, dtype=None, name=None,
                 copy=False, available=None) -> None:
        """初始化 Stats 对象。

        Args:
            data:      收益/权益序列数据（通常为累计收益数组）
            index:     索引（时间戳等）
            dtype:     数据类型
            name:      Series 名称
            copy:      是否复制数据
            available: 初始本金 / 可用资金，用于 profit/profit_rate 计算
        """
        super().__init__(data, index, dtype, name, copy)
        self.profit_array = data
        self.available = available

    @property
    def last_result(self):
        """返回最新的累计收益值（profit_array 最后一个元素）。

        Returns:
            float: 最新累计收益；若序列为空返回 0.0
        """
        if len(self.profit_array) == 0:
            return 0.0
        return self.profit_array.iloc[-1]

    def result(self) -> float:
        """返回最终结果，等同于 last_result。

        Returns:
            float: 最新累计收益值
        """
        return self.last_result

    def profit(self) -> float:
        """计算绝对盈亏金额 = 最终权益 - 初始本金。

        Returns:
            float: 盈亏金额（正数为盈利，负数为亏损）
        """
        return self.last_result - self.available

    def profit_rate(self) -> float:
        """计算盈亏率 = 盈亏金额 / 初始本金。

        Returns:
            float: 盈亏比例（小数形式，如 0.2 表示 20% 收益率）
        """
        return self.profit() / self.available

    # ---- 基础收益率计算 ----

    @get_stats
    def pct_rank(self, window=60):
        """按窗口计算收益率的百分位排名。

        Args:
            window: 滚动窗口大小，默认 60

        Returns:
            收益率在滚动窗口内的百分位排名序列
        """
        ...

    @get_stats
    def compsum(self):
        """计算滚动累计复利收益率序列。

        对收益率序列逐日计算：cumsum = ∏(1 + r_t) - 1

        Returns:
            滚动累计复利收益序列
        """
        ...

    @get_stats
    def comp(self):
        """计算从起始到当前的总复利收益率（标量）。

        等价于 compsum() 的最新值，即总累积收益率。

        Returns:
            float: 总复利收益率
        """
        ...

    @get_stats
    def distribution(self, compounded=True, prepare_returns=True):
        """返回收益率分布统计信息。

        Args:
            compounded:    是否使用复利收益率，默认 True
            prepare_returns: 是否预处理收益率数据，默认 True

        Returns:
            收益率分布数据（含均值、标准差、偏度、峰度等）
        """
        ...

    @get_stats
    def expected_return(self, aggregate=None, compounded=True,
                        prepare_returns=True):
        """计算给定周期的预期收益率（几何持有期收益率）。

        即对所有日收益率的几何平均，反映"典型"的单周期收益水平。

        Args:
            aggregate:       聚合周期，如 'M'=月、'W'=周、'Q'=季、'Y'=年
            compounded:      是否使用复利收益率，默认 True
            prepare_returns: 是否预处理收益率

        Returns:
            float: 预期（几何平均）收益率
        """
        ...

    @get_stats
    def geometric_mean(self, aggregate=None, compounded=True):
        """expected_return() 的别名 —— 几何平均收益率。

        Args:
            aggregate:  聚合周期
            compounded: 是否使用复利

        Returns:
            float: 几何平均收益率
        """
        ...

    @get_stats
    def ghpr(self, aggregate=None, compounded=True):
        """expected_return() 的别名 —— 几何持有期收益率 (GHPR)。

        Args:
            aggregate:  聚合周期
            compounded: 是否使用复利

        Returns:
            float: 几何持有期收益率
        """
        ...

    # ---- 异常值 & 极值分析 ----

    @get_stats
    def outliers(self, quantile=.95):
        """提取收益率序列中的异常值（超出指定分位数的值）。

        Args:
            quantile: 分位数阈值，默认 0.95（即大于 95% 分位数的值）

        Returns:
            异常值序列
        """
        ...

    @get_stats
    def remove_outliers(self, quantile=.95):
        """返回剔除异常值后的收益率序列。

        Args:
            quantile: 分位数阈值，默认 0.95

        Returns:
            剔除异常值后的收益率序列
        """
        ...

    @get_stats
    def best(self, aggregate=None, compounded=True, prepare_returns=True):
        """返回按日/周/月/季/年聚合后的最佳单期收益率。

        Args:
            aggregate:       聚合周期（'D', 'W', 'M', 'Q', 'Y'）
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            float: 最佳单期收益率
        """
        ...

    @get_stats
    def worst(self, aggregate=None, compounded=True, prepare_returns=True):
        """返回按日/周/月/季/年聚合后的最差单期收益率。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            float: 最差单期收益率
        """
        ...

    @get_stats
    def consecutive_wins(self, aggregate=None, compounded=True,
                         prepare_returns=True):
        """计算按日/周/月/季/年聚合后的最大连续盈利次数。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            int: 最大连续盈利期数
        """
        ...

    @get_stats
    def consecutive_losses(self, aggregate=None, compounded=True,
                           prepare_returns=True):
        """计算按日/周/月/季/年聚合后的最大连续亏损次数。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            int: 最大连续亏损期数
        """
        ...

    @get_stats
    def exposure(self, prepare_returns=True):
        """计算市场暴露时间（持仓时间占比），即收益率非零的天数比例。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 暴露时间比例（0~1）
        """
        ...

    # ---- 胜率 & 收益率分析 ----

    @get_stats
    def win_rate(self, aggregate=None, compounded=True, prepare_returns=True):
        """计算给定周期内的胜率（盈利期数 / 总期数）。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            float: 胜率（0~1）
        """
        ...

    @get_stats
    def avg_return(self, aggregate=None, compounded=True, prepare_returns=True):
        """计算给定周期内的平均收益率（含盈亏全部期数）。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            float: 平均收益率
        """
        ...

    @get_stats
    def avg_win(self, aggregate=None, compounded=True, prepare_returns=True):
        """计算给定周期内的平均盈利（仅盈利期数的均值）。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            float: 平均盈利金额/比率
        """
        ...

    @get_stats
    def avg_loss(self, aggregate=None, compounded=True, prepare_returns=True):
        """计算给定周期内的平均亏损（仅亏损期数的均值）。

        Args:
            aggregate:       聚合周期
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            float: 平均亏损金额/比率
        """
        ...

    # ---- 波动率分析 ----

    @get_stats
    def volatility(self, periods=252, annualize=True, prepare_returns=True):
        """计算收益率的波动率（标准差）。

        Args:
            periods:         年化周期数（日线=252，月线=12）
            annualize:       是否年化，默认 True
            prepare_returns: 是否预处理收益率

        Returns:
            float: 波动率（年化则返回年化波动率）
        """
        ...

    @get_stats
    def rolling_volatility(self, rolling_period=126, periods_per_year=252,
                           prepare_returns=True):
        """计算滚动波动率序列。

        Args:
            rolling_period:   滚动窗口大小，默认 126（约半年）
            periods_per_year: 年化周期数，默认 252
            prepare_returns:  是否预处理收益率

        Returns:
            滚动波动率序列
        """
        ...

    @get_stats
    def implied_volatility(self, periods=252, annualize=True):
        """计算隐含波动率（基于实际收益率分布推算）。

        Args:
            periods:   年化周期数
            annualize: 是否年化

        Returns:
            float: 隐含波动率
        """
        ...

    @get_stats
    def autocorr_penalty(self, prepare_returns=False):
        """计算自相关惩罚因子。

        用于调整收益率序列中自相关带来的偏差影响。

        Args:
            prepare_returns: 是否预处理收益率，默认 False

        Returns:
            float: 自相关惩罚因子
        """
        ...

    # ======= 风险调整收益指标 (METRICS) =======

    @get_stats
    def sharpe(self, rf=0., periods=252, annualize=True, smart=False):
        """计算夏普比率（Sharpe Ratio）。

        衡量每单位总风险所获得的超额收益。

        Args:
            rf:        无风险利率（年化），默认 0
            periods:   年化周期数（日线=252）
            annualize: 是否年化，默认 True
            smart:     是否使用智能夏普比率（考虑偏度/峰度），默认 False

        Returns:
            float: 夏普比率
        """
        ...

    @get_stats
    def smart_sharpe(self, rf=0., periods=252, annualize=True):
        """智能夏普比率（smart sharpe）的便捷方法。

        与 sharpe(smart=True) 等价，对偏度和峰度进行修正。

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化

        Returns:
            float: 智能夏普比率
        """
        ...

    @get_stats
    def rolling_sharpe(self, rf=0., rolling_period=126,
                       annualize=True, periods_per_year=252,
                       prepare_returns=True):
        """计算滚动夏普比率序列。

        Args:
            rf:               无风险利率
            rolling_period:   滚动窗口大小，默认 126
            annualize:        是否年化
            periods_per_year: 年化周期数
            prepare_returns:  是否预处理收益率

        Returns:
            滚动夏普比率序列
        """
        ...

    @get_stats
    def sortino(self, rf=0, periods=252, annualize=True, smart=False):
        """计算索提诺比率（Sortino Ratio）。

        与夏普比率类似，但仅考虑下行波动率（亏损侧的波动），
        更精准地衡量下行风险调整后的收益。
        参考论文：Red Rock Capital 的 Sortino 计算方式。

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化
            smart:     是否智能修正

        Returns:
            float: 索提诺比率
        """
        ...

    @get_stats
    def smart_sortino(self, rf=0, periods=252, annualize=True):
        """智能索提诺比率的便捷方法，等同于 sortino(smart=True)。

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化

        Returns:
            float: 智能索提诺比率
        """
        ...

    @get_stats
    def rolling_sortino(self, rf=0, rolling_period=126, annualize=True,
                        periods_per_year=252, **kwargs):
        """计算滚动索提诺比率序列。

        Args:
            rf:               无风险利率
            rolling_period:   滚动窗口大小
            annualize:        是否年化
            periods_per_year: 年化周期数

        Returns:
            滚动索提诺比率序列
        """
        ...

    @get_stats
    def adjusted_sortino(self, rf=0, periods=252, annualize=True, smart=False):
        """Jack Schwager 版调整索提诺比率。

        允许直接与夏普比率进行对比，消除了传统索提诺比率与夏普比率不可比的问题。
        详见：https://archive.is/wip/2rwFW

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化
            smart:     是否智能修正

        Returns:
            float: 调整后的索提诺比率
        """
        ...

    @get_stats
    def probabilistic_ratio(self, rf=0., base="sharpe", periods=252,
                            annualize=False, smart=False):
        """计算概率比率（Probabilistic Ratio）—— 通用概率度量框架。

        Args:
            rf:        无风险利率
            base:      基础比率类型，'sharpe' 或 'sortino'
            periods:   年化周期数
            annualize: 是否年化
            smart:     是否智能修正

        Returns:
            float: 概率比率值
        """
        ...

    @get_stats
    def probabilistic_sharpe_ratio(self, rf=0., periods=252, annualize=False,
                                   smart=False):
        """概率夏普比率（PSR）—— 夏普比的概率显著性度量。

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化
            smart:     是否智能修正

        Returns:
            float: 概率夏普比率
        """
        ...

    @get_stats
    def probabilistic_sortino_ratio(self, rf=0., periods=252, annualize=False,
                                    smart=False):
        """概率索提诺比率 —— 索提诺比的概率显著性度量。

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化
            smart:     是否智能修正

        Returns:
            float: 概率索提诺比率
        """
        ...

    @get_stats
    def probabilistic_adjusted_sortino_ratio(self, rf=0., periods=252,
                                             annualize=False, smart=False):
        """概率调整索提诺比率 —— 调整索提诺比的概率显著性度量。

        Args:
            rf:        无风险利率
            periods:   年化周期数
            annualize: 是否年化
            smart:     是否智能修正

        Returns:
            float: 概率调整索提诺比率
        """
        ...

    @get_stats
    def treynor_ratio(self, benchmark, periods=252., rf=0.):
        """计算特雷诺比率（Treynor Ratio）。

        衡量每单位系统性风险（Beta）的超额收益。

        Args:
            benchmark: 基准收益序列（用于计算 Beta）
            periods:   年化周期数，默认 252
            rf:        无风险利率

        Returns:
            float: 特雷诺比率
        """
        ...

    @get_stats
    def omega(self, rf=0.0, required_return=0.0, periods=252):
        """计算欧米伽比率（Omega Ratio）。

        衡量收益超过损失的概率加权比，综合考虑了收益分布的全部阶矩。
        详见：https://en.wikipedia.org/wiki/Omega_ratio

        Args:
            rf:              无风险利率
            required_return: 目标收益率阈值，默认 0
            periods:         年化周期数

        Returns:
            float: 欧米伽比率
        """
        ...

    @get_stats
    def gain_to_pain_ratio(self, rf=0, resolution="D"):
        """Jack Schwager 的收益痛苦比（GPR）。

        计算总收益与总亏损的绝对值比值。详见：https://archive.is/wip/2rwFW

        Args:
            rf:         无风险利率
            resolution: 时间分辨率，默认 'D'（日）

        Returns:
            float: 收益痛苦比
        """
        ...

    @get_stats
    def cagr(self, rf=0., compounded=True):
        """计算复合年化增长率（CAGR）。

        反映策略在每一年度的几何平均增长率。

        Args:
            rf:         无风险利率
            compounded: 是否复利计算

        Returns:
            float: 年化复合增长率（小数形式）
        """
        ...

    @get_stats
    def rar(self, rf=0.):
        """计算风险调整收益（RAR, Risk-Adjusted Return）。

        RAR = CAGR / 暴露时间，考虑了实际持仓时间的年化收益。

        Args:
            rf: 无风险利率

        Returns:
            float: 风险调整收益率
        """
        ...

    @get_stats
    def skew(self, prepare_returns=True):
        """计算收益率分布的偏度（Skewness）。

        衡量分布相对于均值的非对称程度：
        - 正偏：右尾更长，出现极端正收益概率更高
        - 负偏：左尾更长，出现极端负收益概率更高

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 偏度值
        """
        ...

    @get_stats
    def kurtosis(self, prepare_returns=True):
        """计算收益率分布的峰度（Kurtosis）。

        衡量分布峰值相对于正态分布的高低程度：
        - 高峰度：肥尾，出现极端值的概率更高
        - 低峰度：瘦尾，分布更集中

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 峰度值（超额峰度，正态分布=0）
        """
        ...

    @get_stats
    def calmar(self, prepare_returns=True):
        """计算卡尔玛比率（Calmar Ratio）。

        Calmar = CAGR / 最大回撤，衡量单位最大回撤的年化收益。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 卡尔玛比率
        """
        ...

    @get_stats
    def ulcer_index(self):
        """计算溃疡指数（Ulcer Index）。

        衡量回撤的深度和持续时间，反映下行风险的实际"痛苦"程度。
        与最大回撤不同，溃疡指数考虑了回撤持续的时间。

        Returns:
            float: 溃疡指数
        """
        ...

    @get_stats
    def ulcer_performance_index(self, rf=0):
        """计算溃疡表现指数（UPI）。

        UPI = (收益率 - 无风险利率) / 溃疡指数，类似于夏普比但用溃疡指数替代标准差。

        Args:
            rf: 无风险利率

        Returns:
            float: 溃疡表现指数
        """
        ...

    @get_stats
    def upi(self, rf=0):
        """ulcer_performance_index() 的便捷别名。

        Args:
            rf: 无风险利率

        Returns:
            float: 溃疡表现指数
        """
        ...

    @get_stats
    def serenity_index(self, rf=0):
        """计算安宁指数（Serenity Index）。

        综合考量多种风险因素的稳健性指标。
        详见：https://www.keyquant.com/Download/GetFile?Filename=%5CPublications%5CKeyQuant_WhitePaper_APT_Part1.pdf

        Args:
            rf: 无风险利率

        Returns:
            float: 安宁指数
        """
        ...

    @get_stats
    def risk_of_ruin(self, prepare_returns=True):
        """计算破产风险（Risk of Ruin）。

        估算策略在未来损失全部本金的理论概率，
        基于亏损概率和平均亏损幅度计算。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 破产风险概率（0~1）
        """
        ...

    @get_stats
    def ror(self):
        """risk_of_ruin() 的便捷别名。

        Returns:
            float: 破产风险概率
        """
        ...

    @get_stats
    def value_at_risk(self, sigma=1, confidence=0.95, prepare_returns=True):
        """计算在险价值（VaR, Value at Risk）。

        在给定置信水平下，单日最大预期亏损金额（方差-协方差法）。

        Args:
            sigma:      标准差倍数，默认 1
            confidence: 置信水平，默认 0.95
            prepare_returns: 是否预处理收益率

        Returns:
            float: 在险价值（正数表示亏损金额）
        """
        ...

    @get_stats
    def var(self, sigma=1, confidence=0.95, prepare_returns=True):
        """value_at_risk() 的便捷别名。

        Args:
            sigma:      标准差倍数
            confidence: 置信水平
            prepare_returns: 是否预处理收益率

        Returns:
            float: 在险价值
        """
        ...

    @get_stats
    def conditional_value_at_risk(self, sigma=1, confidence=0.95,
                                  prepare_returns=True):
        """计算条件在险价值（CVaR，即预期亏损 Expected Shortfall）。

        衡量在超出 VaR 阈值时的平均亏损，反映尾部风险的严重程度。

        Args:
            sigma:      标准差倍数
            confidence: 置信水平
            prepare_returns: 是否预处理收益率

        Returns:
            float: 条件在险价值
        """
        ...

    @get_stats
    def cvar(self, sigma=1, confidence=0.95, prepare_returns=True):
        """conditional_value_at_risk() 的便捷别名。

        Args:
            sigma:      标准差倍数
            confidence: 置信水平
            prepare_returns: 是否预处理收益率

        Returns:
            float: 条件在险价值
        """
        ...

    @get_stats
    def expected_shortfall(self, sigma=1, confidence=0.95):
        """conditional_value_at_risk() 的便捷别名。

        Args:
            sigma:      标准差倍数
            confidence: 置信水平

        Returns:
            float: 预期亏损
        """
        ...

    @get_stats
    def tail_ratio(self, cutoff=0.95, prepare_returns=True):
        """计算尾部比率（Tail Ratio）。

        衡量右尾（最好 5%）与左尾（最差 5%）收益率的比值，
        反映极端收益的不对称程度。

        Args:
            cutoff:          分位数分割点，默认 0.95（右尾 95% vs 左尾 5%）
            prepare_returns: 是否预处理收益率

        Returns:
            float: 尾部比率
        """
        ...

    @get_stats
    def payoff_ratio(self, prepare_returns=True):
        """计算盈亏比（Payoff Ratio）= 平均盈利 / 平均亏损。

        反映每单位亏损能够带来多少盈利。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 盈亏比
        """
        ...

    @get_stats
    def win_loss_ratio(self, prepare_returns=True):
        """payoff_ratio() 的便捷别名。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 盈亏比
        """
        ...

    @get_stats
    def profit_ratio(self, prepare_returns=True):
        """计算利润比（Profit Ratio）= 胜率 / 亏损率。

        从概率角度反映盈亏结构。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 利润比
        """
        ...

    @get_stats
    def profit_factor(self, prepare_returns=True):
        """计算盈利因子（Profit Factor）= 总盈利 / 总亏损。

        量化交易中最常用的收益质量指标之一，>1 表示策略整体盈利。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 盈利因子
        """
        ...

    @get_stats
    def cpc_index(self, prepare_returns=True):
        """计算 CPC 综合指标。

        CPC = 盈利因子 × 胜率 × 盈亏比，综合衡量策略质量。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: CPC 指数
        """
        ...

    @get_stats
    def common_sense_ratio(self, prepare_returns=True):
        """计算常识比率（Common Sense Ratio）。

        = 盈利因子 × 尾部比率，结合了收益质量和尾部风险。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 常识比率
        """
        ...

    @get_stats
    def outlier_win_ratio(self, quantile=.99, prepare_returns=True):
        """计算异常盈利比率。

        = 99%分位数盈利 / 平均正收益，衡量极端盈利的贡献。

        Args:
            quantile:        分位数，默认 0.99
            prepare_returns: 是否预处理收益率

        Returns:
            float: 异常盈利比率
        """
        ...

    @get_stats
    def outlier_loss_ratio(self, quantile=.01, prepare_returns=True):
        """计算异常亏损比率。

        = 1%分位数亏损 / 平均负收益，衡量极端亏损的严重程度。

        Args:
            quantile:        分位数，默认 0.01
            prepare_returns: 是否预处理收益率

        Returns:
            float: 异常亏损比率
        """
        ...

    @get_stats
    def recovery_factor(self, prepare_returns=True):
        """计算恢复因子（Recovery Factor）。

        衡量策略从回撤中恢复的速度和质量，
        值越大表示恢复能力越强。

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 恢复因子
        """
        ...

    @get_stats
    def risk_return_ratio(self, prepare_returns=True):
        """计算风险收益比（Risk Return Ratio）。

        即不考虑无风险利率的夏普比率：
        = 平均收益 / 标准差

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 风险收益比
        """
        ...

    @get_stats
    def max_drawdown(self):
        """计算最大回撤（Maximum Drawdown）。

        从历史最高点到其后最低点的最大跌幅，
        是衡量策略下行风险最重要的指标之一。

        Returns:
            float: 最大回撤（正值表示回撤幅度）
        """
        ...

    @get_stats
    def to_drawdown_series(self):
        """将收益率序列转换为回撤序列。

        每个时间点的值为从历史最高点的回撤百分比。

        Returns:
            回撤时间序列（负值表示回撤中）
        """
        ...

    @staticmethod
    @get_stats
    def drawdown_details(drawdown):
        """详细回撤分析 —— 列出每一次回撤周期的详细信息。

        包含每次回撤的起始日期、结束日期、谷底日期、
        持续时间、最大回撤幅度、99%回撤期最大回撤等。

        Args:
            drawdown: 回撤序列（由 to_drawdown_series() 生成）

        Returns:
            DataFrame: 每次回撤的详细信息表
        """
        ...

    @get_stats
    def kelly_criterion(self, prepare_returns=True):
        """计算凯利准则（Kelly Criterion）—— 推荐的最优仓位比例。

        基于策略的胜率和盈亏比，计算理论上能使长期增长率最大化的
        最优资金投入比例。
        详见：http://en.wikipedia.org/wiki/Kelly_criterion

        Args:
            prepare_returns: 是否预处理收益率

        Returns:
            float: 推荐仓位比例（0~1），如 0.2 表示投入 20% 资金
        """
        ...

    # ==== 基准对比 (VS. BENCHMARK) ====

    @get_stats
    def r_squared(self, benchmark, prepare_returns=True):
        """计算 R 平方（拟合优度），衡量策略收益曲线与基准的线性拟合度。

        R² 越接近 1，说明策略与基准走势越同步。

        Args:
            benchmark:       基准收益序列
            prepare_returns: 是否预处理收益率

        Returns:
            float: R² 值（0~1）
        """
        ...

    @get_stats
    def r2(self, benchmark):
        """r_squared() 的便捷别名。

        Args:
            benchmark: 基准收益序列

        Returns:
            float: R² 值
        """
        ...

    @get_stats
    def information_ratio(self, benchmark, prepare_returns=True):
        """计算信息比率（Information Ratio）。

        衡量超额收益（策略收益 - 基准收益）相对于跟踪误差的比值，
        反映策略主动管理能力的稳定性。

        Args:
            benchmark:       基准收益序列
            prepare_returns: 是否预处理收益率

        Returns:
            float: 信息比率
        """
        ...

    @get_stats
    def greeks(self, benchmark, periods=252., prepare_returns=True):
        """计算 Alpha 和 Beta（"希腊字母"）。

        - Alpha：策略相对于基准的超额收益（截距）
        - Beta： 策略对基准的敏感度/系统性风险（斜率）

        Args:
            benchmark:       基准收益序列
            periods:         年化周期数
            prepare_returns: 是否预处理收益率

        Returns:
            tuple: (alpha, beta) 或包含更多回归结果的序列
        """
        ...

    @get_stats
    def rolling_greeks(self, benchmark, periods=252, prepare_returns=True):
        """计算滚动 Alpha 和 Beta 序列。

        动态衡量策略在不同时间段的 Alpha/Beta 变化。

        Args:
            benchmark:       基准收益序列
            periods:         滚动窗口大小
            prepare_returns: 是否预处理收益率

        Returns:
            DataFrame: 滚动 Alpha 和 Beta 序列
        """
        ...

    @get_stats
    def compare(self, benchmark, aggregate=None, compounded=True,
                round_vals=None, prepare_returns=True):
        """按日/周/月/季/年基准对比策略与基准的收益率。

        Args:
            benchmark:       基准收益序列
            aggregate:       聚合周期（'D', 'W', 'M', 'Q', 'Y'）
            compounded:      是否复利
            round_vals:      小数保留位数
            prepare_returns: 是否预处理收益率

        Returns:
            DataFrame: 策略与基准各周期收益率对比表
        """
        ...

    @get_stats
    def monthly_returns(self, eoy=True, compounded=True, prepare_returns=True):
        """计算月度收益率矩阵。

        以月份为行、年份为列的矩阵表格，便于快速查看每月的表现。

        Args:
            eoy:             是否包含年末汇总（YTD）行
            compounded:      是否复利
            prepare_returns: 是否预处理收益率

        Returns:
            DataFrame: 月度收益率矩阵
        """
        ...
