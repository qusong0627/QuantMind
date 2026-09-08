import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import spearmanr
from scipy.optimize import minimize


# 设置matplotlib支持中文
plt.rcParams["font.family"] = ["SimHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False  # 解决负号显示问题

__all__ = ["SingleAssetFactorOptimizer",]


class SingleAssetFactorOptimizer:
    """### 单资产多因子权重优化器（支持交易信号生成）"""

    def __init__(self, factors: pd.DataFrame, returns: pd.Series, price: pd.Series,
                 max_weight: float = 0.8, l2_reg: float = 0.0001,
                 min_ic_abs: float = 0.03, n_init_points: int = 10,
                 optimization_model: str = "scipy",
                 signal_params: dict = None):  # 新增：信号参数
        # 保存原始数据的索引
        self.original_index = factors.index.copy()

        # 数据预处理
        self.factors = factors.copy()
        self.returns = returns.copy()
        self.price = price.copy()
        self.returns.name = "returns"
        self.price.name = "price"

        # 强制对齐索引并处理缺失值
        common_index = self.factors.index.intersection(self.returns.index)
        common_index = common_index.intersection(self.price.index)

        self.factors = self.factors.loc[common_index]
        self.returns = self.returns.loc[common_index]
        self.price = self.price.loc[common_index]

        # 统一删除包含缺失值的行
        combined = pd.concat([self.factors, self.returns, self.price], axis=1)
        combined = combined.dropna(how='any')

        self.factors = combined[self.factors.columns]
        self.returns = combined[self.returns.name]
        self.price = combined[self.price.name]

        # 因子筛选
        self.min_ic_abs = min_ic_abs
        self._filter_factors_by_ic()

        self.factor_names = self.factors.columns.tolist()
        self.n_factors = len(self.factor_names)
        if self.n_factors == 0:
            raise ValueError("无有效因子，无法进行优化")

        # 优化参数
        self.max_weight = max_weight
        self.l2_reg = l2_reg
        self.n_init_points = n_init_points
        self.optimization_model = optimization_model.lower()
        self._check_model_support()

        # 信号参数（默认值）
        self.signal_params = signal_params or {
            'threshold_pos': 0.5,    # 多头信号阈值（因子标准化后的标准差倍数）
            'threshold_neg': -0.5,   # 空头信号阈值
            'trend_window': 3,       # 趋势确认窗口
            'filter_std': 1.0,       # 过滤极端值的标准差倍数
            'holding_period': None   # 持有期（None表示直到反转信号）
        }

        # 存储结果
        self.optimal_weights = None
        self.merged_factor = None
        self.original_ics = None
        self.signals = None  # 交易信号
        self.standardized_factor = None  # 标准化后的融合因子

    def _check_model_support(self):
        """检查优化方法是否支持"""
        supported_models = ["scipy", "cvxpy", "ortools"]
        if self.optimization_model not in supported_models:
            raise ValueError(
                f"不支持的优化方法：{self.optimization_model}，可选：{supported_models}")

        if self.optimization_model == "cvxpy":
            try:
                import cvxpy  # noqa: F401
            except ImportError:
                raise ImportError("请安装cvxpy：pip install cvxpy")

        if self.optimization_model == "ortools":
            try:
                from ortools.linear_solver import pywraplp  # noqa: F401
            except ImportError:
                raise ImportError("请安装ortools：pip install ortools")

    def _filter_factors_by_ic(self):
        """根据IC绝对值筛选因子"""
        ics = {}
        for factor in self.factors.columns:
            if len(self.factors[factor]) != len(self.returns):
                raise ValueError(
                    f"因子{factor}与收益率长度不匹配：{len(self.factors[factor])} vs {len(self.returns)}")
            ic, _ = spearmanr(self.factors[factor], self.returns)
            ics[factor] = abs(ic)

        valid_factors = [factor for factor,
                         ic in ics.items() if ic >= self.min_ic_abs]
        if not valid_factors:
            raise ValueError(f"所有因子的IC绝对值均低于阈值{self.min_ic_abs}，请检查因子质量")

        self.factors = self.factors[valid_factors]

    def _objective_scipy(self, weights):
        """scipy优化目标：最大化ICIR"""
        weighted_factor = (weights * self.factors).sum(axis=1)
        ic, _ = spearmanr(weighted_factor, self.returns)
        n = len(weighted_factor)
        icir = ic * np.sqrt(n-2) / np.sqrt(1 - ic **
                                           2) if abs(ic) < 1 else np.inf
        reg_term = self.l2_reg * np.sum(np.square(weights))
        return -icir + reg_term

    def _optimize_scipy(self):
        """scipy优化（多初始点策略）"""
        best_result = None
        best_icir = -np.inf

        for _ in range(self.n_init_points):
            initial_weights = np.random.rand(self.n_factors)
            initial_weights /= initial_weights.sum()

            constraints = [
                {'type': 'eq', 'fun': lambda w: np.sum(w) - 1},
                {'type': 'ineq', 'fun': lambda w: w},
                {'type': 'ineq', 'fun': lambda w: self.max_weight - w}
            ]

            result = minimize(
                fun=self._objective_scipy,
                x0=initial_weights,
                constraints=constraints,
                method='SLSQP',
                options={'maxiter': 10000, 'disp': False}
            )

            if not result.success:
                continue

            # 计算当前解的ICIR
            weighted_factor = (result.x * self.factors).sum(axis=1)
            ic, _ = spearmanr(weighted_factor, self.returns)
            n = len(weighted_factor)
            icir = ic * np.sqrt(n-2) / np.sqrt(1 - ic **
                                               2) if abs(ic) < 1 else np.inf

            if icir > best_icir:
                best_result = result
                best_icir = icir

        if best_result is None:
            raise ValueError("scipy优化失败，请调整参数")

        return best_result.x

    def _optimize_cvxpy(self):
        """cvxpy优化（凸优化）"""
        import cvxpy as cp

        # 定义变量
        weights = cp.Variable(self.n_factors)

        # 目标函数：最大化IC（简化为线性目标，适合凸优化）
        weighted_factor = self.factors.values @ weights
        ic = cp.sum(cp.multiply(weighted_factor, self.returns.values))  # 近似IC
        reg_term = self.l2_reg * cp.sum_squares(weights)  # L2正则化
        objective = cp.Maximize(ic - reg_term)  # 最大化目标

        # 约束
        constraints = [
            cp.sum(weights) == 1,  # 权重和为1
            weights >= 0,          # 非负
            weights <= self.max_weight  # 单个权重上限
        ]

        # 求解
        prob = cp.Problem(objective, constraints)
        prob.solve(solver=cp.ECOS)  # 高效凸优化求解器

        if prob.status != cp.OPTIMAL:
            raise ValueError(f"cvxpy优化失败，状态：{prob.status}")

        return weights.value

    def _optimize_ortools(self):
        """ortools优化（线性规划，适合线性目标）"""
        from ortools.linear_solver import pywraplp

        # 初始化求解器
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if not solver:
            raise ValueError("无法创建ortools求解器")

        # 定义变量（权重）
        weights = [solver.NumVar(
            0, self.max_weight, f"w_{i}") for i in range(self.n_factors)]

        # 目标函数：最大化因子与收益的相关性（线性近似）
        cov = np.corrcoef(self.factors.T, self.returns)[-1, :-1]  # 相关性系数
        objective = solver.Objective()
        for i in range(self.n_factors):
            objective.SetCoefficient(weights[i], cov[i])
        objective.SetMaximization()

        # 约束：权重和为1
        solver.Add(solver.Sum(weights) == 1)

        # 求解
        status = solver.Solve()
        if status != pywraplp.Solver.OPTIMAL:
            raise ValueError(f"ortools优化失败，状态：{status}")

        return [w.solution_value() for w in weights]

    def optimize(self):
        """根据选择的方法执行优化"""

        if self.optimization_model == "scipy":
            weights = self._optimize_scipy()
        elif self.optimization_model == "cvxpy":
            weights = self._optimize_cvxpy()
        elif self.optimization_model == "ortools":
            weights = self._optimize_ortools()

        # 格式化权重
        self.optimal_weights = pd.Series(
            np.round(weights, 4),
            index=self.factor_names
        )
        return self.optimal_weights

    def build_merged_factor(self, standardize=True):
        """构建融合因子，确保与原始数据长度一致（前面补NaN）"""
        if self.optimal_weights is None:
            self.optimize()

        # 计算优化后的融合因子（基于清洗后的数据）
        optimized_factor = (self.factors * self.optimal_weights).sum(axis=1)

        # 创建与原始数据长度一致的Series，初始值为NaN
        self.merged_factor = pd.Series(
            np.nan,
            index=self.original_index  # 使用原始数据的索引
        )

        # 将计算出的融合因子值填充到对应位置
        self.merged_factor.loc[optimized_factor.index] = optimized_factor.values

        # 标准化融合因子（便于阈值设置）
        if standardize:
            valid_data = self.merged_factor.dropna()
            mean = valid_data.mean()
            std = valid_data.std()
            self.standardized_factor = (self.merged_factor - mean) / std
        else:
            self.standardized_factor = self.merged_factor.copy()

        return pd.DataFrame(dict(
            merged_factor=self.merged_factor,
            signals=self.generate_signals()
        ))
        # return self.merged_factor, self.generate_signals()

    def generate_signals(self):
        """
        生成交易信号：
        1: 买入信号, -1: 卖出信号, 0: 无信号
        基于融合因子的阈值突破 + 趋势确认 + 极端值过滤
        """
        if self.standardized_factor is None:
            self.build_merged_factor()

        # 复制参数便于引用
        params = self.signal_params
        signals = pd.Series(0, index=self.merged_factor.index, name='signals')

        # 1. 极端值过滤（避免异常值导致错误信号）
        filter_upper = self.standardized_factor.mean(
        ) + params['filter_std'] * self.standardized_factor.std()
        filter_lower = self.standardized_factor.mean(
        ) - params['filter_std'] * self.standardized_factor.std()
        is_valid = (self.standardized_factor <= filter_upper) & (
            self.standardized_factor >= filter_lower)

        # 2. 趋势确认（连续多期符合条件才确认信号）
        # 多头趋势：连续trend_window期因子>正阈值
        pos_trend = (self.standardized_factor > params['threshold_pos']).rolling(
            window=params['trend_window']).sum() == params['trend_window']

        # 空头趋势：连续trend_window期因子<负阈值
        neg_trend = (self.standardized_factor < params['threshold_neg']).rolling(
            window=params['trend_window']).sum() == params['trend_window']

        # 3. 信号生成（只在有效区域生成信号）
        signals.loc[is_valid & pos_trend] = 1
        signals.loc[is_valid & neg_trend] = -1

        # 4. 去重（连续相同信号只保留第一个）
        signals = signals.where(signals != signals.shift(1), 0)

        # 5. 持有期处理（如果设置了持有期，到期强制平仓）
        if params['holding_period']:
            hold_period = params['holding_period']
            for i in range(1, hold_period):
                # 前i期有买入信号，本期未平仓则继续持有
                signals.loc[signals.shift(i) == 1] = 0
                signals.loc[signals.shift(i) == -1] = 0

        self.signals = signals
        return self.signals

    def evaluate_signals(self):
        """评估交易信号效果"""
        if self.signals is None:
            self.generate_signals()

        # 对齐信号和收益率
        signal_returns = self.returns.reindex(self.signals.index).copy()
        signal_returns.name = 'strategy_returns'

        # 计算策略收益率
        signal_returns = signal_returns * self.signals.shift(1)  # 信号滞后一期生效
        signal_returns = signal_returns.dropna()

        # 计算基准收益率（持有）
        bench_returns = self.returns.reindex(signal_returns.index)

        # 绩效指标
        total_signal = len(self.signals[self.signals != 0])
        total_positive = len(signal_returns[signal_returns > 0])
        total_negative = len(signal_returns[signal_returns < 0])

        win_rate = total_positive / total_signal if total_signal > 0 else 0
        total_return = (1 + signal_returns).prod() - 1
        bench_return = (1 + bench_returns).prod() - 1

        # 风险指标
        sharpe = np.sqrt(252) * signal_returns.mean() / \
            signal_returns.std() if signal_returns.std() != 0 else 0

        print("\n===== 交易信号评估 =====")
        print(f"总信号数: {total_signal}")
        print(f"胜率: {win_rate:.2%}")
        print(f"策略总收益: {total_return:.2%}")
        print(f"基准总收益: {bench_return:.2%}")
        print(f"夏普比率: {sharpe:.2f}")

        return {
            'win_rate': win_rate,
            'total_return': total_return,
            'bench_return': bench_return,
            'sharpe': sharpe,
            'signal_returns': signal_returns
        }

    def plot_signals(self):
        """可视化价格、融合因子与交易信号"""
        if self.merged_factor is None:
            self.build_merged_factor()
        if self.signals is None:
            self.generate_signals()

        # 准备数据
        price = self.price.reindex(self.original_index)
        merged_factor = self.standardized_factor  # 使用标准化因子便于观察
        signals = self.signals

        # 创建图表
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 12), sharex=True)

        # 1. 价格与交易信号
        ax1.plot(price, label='资产价格', color='blue', alpha=0.7)

        # 买入信号（向上箭头）
        buy_signals = price[signals == 1]
        ax1.scatter(buy_signals.index, buy_signals.values,
                    marker='^', color='g', label='买入信号', zorder=3)

        # 卖出信号（向下箭头）
        sell_signals = price[signals == -1]
        ax1.scatter(sell_signals.index, sell_signals.values,
                    marker='v', color='r', label='卖出信号', zorder=3)

        ax1.set_title('资产价格与交易信号')
        ax1.legend()
        ax1.grid(alpha=0.3)

        # 2. 标准化融合因子与阈值线
        ax2.plot(merged_factor, label='标准化融合因子', color='orange')
        ax2.axhline(y=self.signal_params['threshold_pos'], color='g', linestyle='--',
                    label=f'多头阈值 ({self.signal_params["threshold_pos"]})')
        ax2.axhline(y=self.signal_params['threshold_neg'], color='r', linestyle='--',
                    label=f'空头阈值 ({self.signal_params["threshold_neg"]})')
        ax2.axhline(y=0, color='gray', linestyle='-', alpha=0.3)

        ax2.set_title('标准化融合因子与信号阈值')
        ax2.legend()
        ax2.grid(alpha=0.3)

        plt.tight_layout()
        plt.show()

    def evaluate(self):
        """评估优化效果"""
        if self.merged_factor is None:
            self.build_merged_factor()

        # 计算原始因子IC
        self.original_ics = {}
        for factor in self.factor_names:
            ic, _ = spearmanr(self.factors[factor], self.returns)
            self.original_ics[factor] = round(ic, 4)

        # 计算融合因子IC（只使用有值的部分）
        valid_merged = self.merged_factor.dropna()
        valid_returns = self.returns.reindex(valid_merged.index)
        merged_ic, _ = spearmanr(valid_merged, valid_returns)
        merged_ic = round(merged_ic, 4)

        print("===== 因子评估结果 =====")
        print(f"原始因子IC: {self.original_ics}")
        print(f"融合因子IC: {merged_ic}")
        print(f"最优权重: \n{self.optimal_weights.sort_values(ascending=False)}")

        # 计算IC提升百分比
        avg_original_ic = np.mean([abs(ic)
                                  for ic in self.original_ics.values()])
        if avg_original_ic == 0:
            print("原始因子平均IC为0，无法计算提升比例")
        else:
            ic_improvement = (abs(merged_ic) - avg_original_ic) / \
                avg_original_ic * 100
            print(f"IC提升: {ic_improvement:.2f}%")

        return self.original_ics, merged_ic

    def plot_weights(self):
        """可视化权重分布"""
        if self.optimal_weights is None:
            self.optimize()

        plt.figure(figsize=(10, 6))
        ax = self.optimal_weights.sort_values().plot(kind='barh', color='skyblue')
        plt.title(f"单资产多因子最优权重分布（{self.optimization_model}优化）")
        plt.xlabel("权重值")
        plt.axvline(x=0, color='black', linestyle='--')

        # 添加权重值标签
        for i, v in enumerate(self.optimal_weights.sort_values()):
            ax.text(v + 0.01, i, f'{v:.4f}', va='center')

        plt.tight_layout()
        plt.show()


# 示例运行
if __name__ == "__main__":
    import pandas_ta as pta

    # 生成示例数据
    n_days = 500
    dates = pd.date_range(start='2020-01-01', periods=n_days, freq='D')
    np.random.seed(42)  # 固定随机种子便于复现
    price = pd.Series(np.cumsum(np.random.randn(n_days)) +
                      100, index=dates, name='close')

    # 生成因子（加入一些与价格相关的因子）
    factors = pd.DataFrame(index=dates)
    factors["rsi5"] = pta.rsi(price, 5)/100.
    factors["sma5"] = pta.zscore(price, 5)
    factors["rsi10"] = pta.rsi(price, 10)/100.
    factors["sma10"] = pta.zscore(price, 10)
    factors["momentum"] = price.pct_change(5)
    factors.iloc[:10] = np.nan  # 前10行添加NaN

    # 生成收益率
    returns = price.pct_change().shift(-1).fillna(0.)

    # 自定义信号参数
    signal_params = {
        'threshold_pos': 0.6,    # 多头信号阈值（标准化因子的标准差倍数）
        'threshold_neg': -0.6,   # 空头信号阈值
        'trend_window': 2,       # 连续2期符合条件确认信号
        'filter_std': 2.0,       # 过滤2倍标准差以外的极端值
        'holding_period': None   # 持有到反转信号
    }

    # 执行优化与信号生成
    try:
        optimizer = SingleAssetFactorOptimizer(
            factors=factors,
            returns=returns,
            price=price,
            max_weight=0.8,
            l2_reg=0.0001,
            min_ic_abs=0.01,
            n_init_points=10,
            optimization_model="scipy",
            signal_params=signal_params
        )

        print("最优因子权重：")
        print(optimizer.optimize())
        optimizer.build_merged_factor()
        optimizer.evaluate()

        # 生成并评估信号
        signals = optimizer.generate_signals()
        print(f"\n交易信号示例（前20行）：\n{signals.head(20)}")
        optimizer.evaluate_signals()
        optimizer.plot_weights()
        optimizer.plot_signals()

    except ValueError as e:
        print(f"优化过程出错: {e}")
