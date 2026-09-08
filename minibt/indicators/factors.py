from __future__ import annotations
from .base import tobtind
from ..utils import pd, TYPE_CHECKING


if TYPE_CHECKING:
    from typing_ import *
    from .core import *



class Factors:
    """
    ## 多因子模型类

    - 该类提供了多种多因子建模和组合方法，用于将多个技术指标或特征因子
    - 融合为综合评分或趋势信号。

    ### 主要功能：
    - 1. 单资产多因子策略：基于IC（信息系数）动态加权多个因子
    - 2. PCA趋势指标：使用主成分分析降维提取核心趋势信号
    - 3. 自适应权重趋势：基于因子历史表现动态调整权重
    - 4. 因子优化器：通过优化算法寻找最优因子组合权重

    ### 核心概念：
    - **因子**：能够预测资产未来收益率的特征或指标
    - **IC（信息系数）**：因子值与未来收益率的Spearman秩相关系数
    - **IR（信息比率）**：IC均值与IC标准差的比值，衡量因子稳定性
    - **主成分分析**：通过正交变换将相关因子转换为不相关的主成分
    - **动态加权**：根据因子近期表现调整权重，适应市场环境变化
    """
    _perfixes: str = "factor_"
    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @tobtind(lines=["combined_score", "signals"], lib="factor")
    def single_asset_multi_factor_strategy(self, *factors: tuple[pd.DataFrame | pd.Series], window: int = 10,
                                           top_pct: float = 0.2, bottom_pct: float = 0.2,
                                           isstand: bool = True, **kwargs)->IndFrame:
        """
        ## 单资产多因子策略

        - 将多个因子动态加权组合成综合得分，基于IC（信息系数）自适应调整因子权重，
        - 然后根据综合得分的分位数生成交易信号。

        底层逻辑：
        ```
        1. 计算未来收益率（下一期涨跌幅）
        2. 对每个因子进行标准化（z-score）
        3. 计算每个因子的滚动IC（Spearman相关系数）
        4. 基于IC的滚动表现计算动态权重
        5. 加权求和得到综合得分
        6. 基于综合得分的分位数生成交易信号
        ```

        Parameters
        ----------

        self (IndSeries): 
            资产价格

        *factors : pd.DataFrame
            一个或多个因子数据框，每个因子应为时间序列

        window : int, default=10
            滚动窗口大小，用于：
            - 计算IC的窗口（用于评估因子有效性）
            - 计算综合得分分位数阈值的窗口

        top_pct : float, default=0.2
            顶部百分比，用于确定做空信号阈值
            例如0.2表示当综合得分超过历史窗口前20%分位数时做空

        bottom_pct : float, default=0.2
            底部百分比，用于确定做多信号阈值
            例如0.2表示当综合得分低于历史窗口后20%分位数时做多

        isstand : bool, default=True
            是否对因子进行标准化（z-score归一化）
            - True：对每个因子减去均值并除以标准差
            - False：使用原始因子值

        **kwargs : dict
            其他关键字参数，传递给底层实现

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - `combined_score` : 综合因子得分，值越大表示看涨信号越强
            - `signals` : 交易信号
              - 1 : 做多（综合得分低于bottom_pct分位数）
              - -1 : 做空（综合得分高于top_pct分位数）
              - 0 : 无信号

        Notes
        -----
        1. IC（信息系数）衡量因子预测未来收益率的能力
        2. 动态权重机制使得近期表现好的因子获得更高权重
        3. 因子标准化确保不同量纲的因子可以公平加权
        4. 分位数阈值机制适应市场波动率的变化
        5. 该方法适用于单资产择时，不涉及横截面比较

        Examples
        --------
        ```python
        # 准备价格数据和因子
        price = self.kline.close
        ma5=price.sma(5)
        ma10=price.sma(10)
        ma20=price.sma(20)

        # 使用多因子策略（传入因子矩阵）
        result = price.single_asset_multi_factor_strategy(ma5,ma10,ma20)

        # 访问综合得分和信号
        combined_score = result.combined_score
        trade_signals = result.signals

        # 交易信号
        long_mask = result.signals == 1
        short_mask = result.signals == -1
        ```
        """
        ...

    @tobtind(lib="factor")
    def pca_trend_indicator(self, *factors: tuple[pd.DataFrame | pd.Series], n_components: int = 2,
                            dynamic_sign: bool = True, filter_low_variance: bool = True)->IndSeries:
        """
        ## PCA趋势指标

        - 使用主成分分析（PCA）将多个相关因子降维为少数几个不相关的主成分，
        - 然后加权组合生成综合趋势指标。

        底层逻辑：
        ```
        1. 过滤低方差因子（可选，避免常数因子干扰）
        2. 对因子进行标准化（z-score）
        3. 应用PCA提取主成分
        4. 使用方差解释比例作为权重组合主成分
        5. 动态调整符号确保与价格正相关（可选）
        ```

        Parameters
        ----------

        self (IndSeries): 
            资产价格

        *factors : pd.DataFrame
            一个或多个因子数据框，每个因子应为时间序列

        n_components : int, default=2
            保留的主成分数量，通常小于等于因子数量
            常用值：1-3，保留大部分方差解释的主成分

        dynamic_sign : bool, default=True
            是否根据主成分与价格的相关性自动调整符号
            - True：确保最终趋势指标与价格正相关
            - False：使用PCA计算的原始方向

        filter_low_variance : bool, default=True
            是否过滤低方差因子
            - True：移除方差小于0.1的因子，避免干扰PCA
            - False：使用所有因子

        Returns
        -------
        IndSeries
            PCA趋势指标序列，值的大小表示趋势强度，符号表示方向

        Notes
        -----
        1. PCA通过正交变换消除因子间的相关性
        2. 方差解释比例表示每个主成分包含的原始信息量
        3. 低方差因子可能是常数或接近常数，对PCA无贡献
        4. 动态符号调整确保指标方向与价格变动一致
        5. PCA趋势指标能捕捉多个因子的共同趋势

        Examples
        --------
        ```python
        # 准备多个技术指标作为因子
        price = self.kline.close

        # 计算多个移动平均作为因子
        ma5 = price.sma(5)
        ma10 = price.sma(10)
        ma20 = price.sma(20)
        ma30 = price.sma(30)
        ma60 = price.sma(60)
        ma100 = price.sma(100)

        # 使用PCA提取趋势指标
        pca_trend = price.pca_trend_indicator(ma5,ma10,ma20,ma30,
                                        ma60,ma100,n_components=2)

        # 使用所有参数
        pca_trend_full = price.pca_trend_indicator(
            ma5, ma10, ma20,
            ma30, ma60, ma100,
            n_components=3,
            dynamic_sign=True,
            filter_low_variance=True
        )

        # 获取PCA诊断信息（需要从底层获取）
        # 通常包括：
        # - 各主成分的方差解释比例
        # - 主成分在原始因子上的载荷（loading）
        ```
        """
        ...

    @tobtind(overlap=True, lib="factor")
    def adaptive_weight_trend(self, windows: list = [5, 20, 50], lookback: int = 10, **kwargs)->IndSeries:
        """
        ## 自适应权重趋势指标

        - 基于因子历史表现（与价格的相关性）动态调整权重，
        - 对多个时间窗口的移动平均进行自适应加权。

        底层逻辑：
        ```
        1. 计算多个时间窗口的移动平均（作为因子）
        2. 在每个时间点，使用最近lookback期的数据计算各因子与价格的相关性
        3. 将相关性归一化为非负权重（和为1）
        4. 使用当前权重对移动平均进行加权求和
        5. 滚动更新权重，适应市场环境变化
        ```

        Parameters
        ----------

        self (IndSeries): 
            资产价格

        windows : list, default=[5, 20, 50]
            移动平均窗口列表，每个窗口生成一个因子
            常用组合：[5, 10, 20, 30, 50, 100, 200]

        lookback : int, default=10
            回溯窗口大小，用于计算因子与价格的相关性
            较小的lookback使权重更敏感，较大的lookback更稳定

        **kwargs : dict
            其他关键字参数，传递给底层实现

        Returns
        -------
        IndSeries
            自适应加权趋势指标序列

        Notes
        -----
        1. 动态权重机制使指标能适应不同的市场环境
        2. 相关性越高的因子获得越大的权重
        3. 负相关性因子权重设为0（只使用正相关因子）
        4. 当所有因子相关性为负时，使用等权重
        5. 该方法特别适用于趋势跟踪策略

        Examples
        --------
        ```python
        # 准备价格数据
        price = self.kline.close

        # 使用自适应权重趋势指标
        adaptive_trend = price.adaptive_weight_trend()

        # 自定义窗口和回溯期
        adaptive_trend_custom = price.adaptive_weight_trend(
            windows=[10, 30, 60, 120],
            lookback=20
        )

        # 分析不同市场环境下的权重变化
        # 可以将权重序列保存下来，分析权重如何随市场状态变化
        ```
        """
        ...

    @tobtind(lines=["merged_factor", "signals"], lib="factor")
    def factor_optimizer(self, *factors: tuple[pd.DataFrame | pd.Series],
                         max_weight: float = 0.8, l2_reg: float = 0.0001,
                         min_ic_abs: float = 0.03, n_init_points: int = 10,
                         optimization_model: str = "scipy", **kwargs)->IndFrame:
        """
        ## 因子优化器

        - 使用优化算法寻找最优因子组合权重，最大化组合因子的信息比率（IR），
        - 同时考虑权重约束和正则化。

        底层逻辑：
        ```
        1. 计算每个因子的IC序列（与未来收益率的相关性）
        2. 定义优化目标：最大化组合因子的信息比率（IR）
        3. 添加约束：权重和为1，单个因子权重不超过max_weight
        4. 添加L2正则化防止过拟合
        5. 使用优化算法（如scipy, hyperopt等）求解最优权重
        6. 应用最优权重生成合并因子和交易信号
        ```

        Parameters
        ----------

        self (IndSeries): 
            资产价格

        *factors : pd.DataFrame
            一个或多个因子数据框，每个因子应为时间序列

        max_weight : float, default=0.8
            单个因子的最大权重，防止过度依赖单一因子
            范围：0.0-1.0，常用值：0.5-0.8

        l2_reg : float, default=0.0001
            L2正则化系数，防止过拟合和权重过分散
            较小的值：弱正则化；较大的值：强正则化

        min_ic_abs : float, default=0.03
            最小绝对IC值，低于此值的因子将被剔除
            筛选掉预测能力太弱的因子

        n_init_points : int, default=10
            优化算法的初始采样点数
            点数越多，找到全局最优解的概率越大，但计算时间越长

        optimization_model : str, default="scipy"
            优化算法类型，可选：
            - "scipy"：使用SciPy的优化器（如SLSQP）
            - "hyperopt"：使用Hyperopt的贝叶斯优化
            - "random"：随机搜索

        **kwargs : dict
            其他关键字参数，传递给优化器

        Returns
        -------
        IndFrame
            包含以下列的数据框：
            - `merged_factor` : 优化权重合并后的因子序列
            - `signals` : 基于合并因子的交易信号
              - 1 : 做多（合并因子值大于零）
              - -1 : 做空（合并因子值小于零）
              - 0 : 无信号（合并因子值接近零）

        Notes
        -----
        1. 优化目标是最大化信息比率，而非单纯最大化IC
        2. 权重约束确保组合的分散化和稳健性
        3. L2正则化防止在小样本下过拟合
        4. 剔除低IC因子提高组合质量
        5. 不同优化算法各有优劣，可根据问题复杂度选择

        Examples
        --------
        ```python
        # 准备价格数据和因子
        price = self.kline.close

        # 计算技术指标作为因子
        rsi = price.rsi(14)
        macd = price.macd()
        bbands = price.bbands()
        volume_ratio = self.kline.volume.tqta.VR()
        momentum = price.mom()

        # 使用因子优化器
        result = price.factor_optimizer(rsi,macd,bbands,volume_ratio,momentum)

        # 使用自定义参数
        result_custom = price.factor_optimizer(
            rsi ,macd,bbands,
            volume_ratio ,momentum,
            max_weight=0.6,
            l2_reg=0.001,
            min_ic_abs=0.05,
            n_init_points=20,
            optimization_model="hyperopt"
        )

        # 访问优化结果
        merged_factor = result.merged_factor
        optimized_signals = result.signals

        # 获取最优权重（需要从底层获取）
        # 通常可以获取每个因子的最终权重，用于分析因子贡献
        # 回测优化前后的表现对比
        # 可以比较原始单个因子、等权重组合和优化权重组合的表现
        ```
        """
        ...
