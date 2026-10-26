from __future__ import annotations
import warnings
from .base import tobtind
from ..utils import pd, np, TYPE_CHECKING

# scipy.stats 惰性加载：仅在真正需要计算 Spearman IC / scipy 优化时才导入
# （scipy.stats 本身约 0.6s，之前的模块顶层导入会拖慢 import minibt）
_scipy_stats = None
_HAS_SCIPY = None


def _get_scipy_stats():
    """惰性导入 scipy.stats；scipy 不可用时返回 None。"""
    global _scipy_stats, _HAS_SCIPY
    if _HAS_SCIPY is None:
        try:
            from scipy import stats as _stats
            _scipy_stats = _stats
            _HAS_SCIPY = True
        except ImportError:
            _HAS_SCIPY = False
    return _scipy_stats

if TYPE_CHECKING:
    from typing_ import *
    from .core import *


def _to_array(obj) -> np.ndarray:
    """将 IndSeries / pd.Series / np.ndarray 统一转为 numpy 数组"""
    if hasattr(obj, 'values'):
        return np.asarray(obj.values, dtype=np.float64)
    if isinstance(obj, np.ndarray):
        return obj.astype(np.float64)
    return np.asarray(obj, dtype=np.float64)


def _spearman_ic(factor: np.ndarray, returns: np.ndarray) -> float:
    """计算 Spearman 秩相关系数 (IC)"""
    stats_mod = _get_scipy_stats()
    if stats_mod is not None:
        corr, _ = stats_mod.spearmanr(factor, returns)
        return float(corr) if not np.isnan(corr) else 0.0
    # 纯 Python fallback
    from numpy.lib.function_base import corrcoef
    rf = pd.Series(factor).rank().values
    rr = pd.Series(returns).rank().values
    if np.std(rf) < 1e-12 or np.std(rr) < 1e-12:
        return 0.0
    return float(np.corrcoef(rf, rr)[0, 1])


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
        """
        price = _to_array(self)
        n = len(price)

        # 1. 构建因子矩阵 (n, k)
        factor_arrays = []
        for f in factors:
            arr = _to_array(f)
            if len(arr) == n:
                factor_arrays.append(arr)

        if len(factor_arrays) == 0:
            return pd.DataFrame({"combined_score": np.zeros(n), "signals": np.zeros(n)})

        factor_matrix = np.column_stack(factor_arrays)

        # 2. 计算未来收益率 (shift -1)
        returns = np.zeros(n)
        if n > 1:
            returns[:-1] = price[1:] / price[:-1] - 1.0

        # 3. 标准化因子
        if isstand:
            for i in range(factor_matrix.shape[1]):
                col = factor_matrix[:, i]
                mu, sigma = np.nanmean(col), np.nanstd(col)
                if sigma > 1e-12:
                    factor_matrix[:, i] = (col - mu) / sigma
                else:
                    factor_matrix[:, i] = 0.0

        # 4. 滚动 IC → 动态权重
        k = factor_matrix.shape[1]
        ic_series = np.zeros((n, k))
        for t in range(window, n):
            for j in range(k):
                ic_series[t, j] = _spearman_ic(
                    factor_matrix[t - window:t, j],
                    returns[t - window:t]
                )

        # 权重 = 滚动IC均值的正值部分 (负IC因子权重为0)
        weights = np.zeros((n, k))
        for t in range(n):
            w = np.maximum(ic_series[t], 0)
            s = w.sum()
            if s > 0:
                weights[t] = w / s
            else:
                weights[t] = 1.0 / k

        # 5. 加权合成综合得分
        combined = np.zeros(n)
        for t in range(n):
            combined[t] = np.dot(factor_matrix[t], weights[t])

        # 6. 基于分位数阈值生成信号
        signals = np.zeros(n)
        for t in range(window, n):
            hist = combined[max(0, t - window):t]
            if len(hist) > 0:
                q_top = np.percentile(hist, (1 - top_pct) * 100)
                q_bot = np.percentile(hist, bottom_pct * 100)
                if combined[t] > q_top:
                    signals[t] = -1.0  # 高分做空
                elif combined[t] < q_bot:
                    signals[t] = 1.0   # 低分做多

        return pd.DataFrame({"combined_score": combined, "signals": signals})

    @tobtind(lib="factor")
    def pca_trend_indicator(self, *factors: tuple[pd.DataFrame | pd.Series], n_components: int = 2,
                            dynamic_sign: bool = True, filter_low_variance: bool = True)->IndSeries:
        """
        ## PCA趋势指标

        - 使用主成分分析（PCA）将多个相关因子降维为少数几个不相关的主成分，
        - 然后加权组合生成综合趋势指标。
        """
        price = _to_array(self)
        n = len(price)

        factor_arrays = []
        for f in factors:
            arr = _to_array(f)
            if len(arr) == n:
                factor_arrays.append(arr)

        if len(factor_arrays) < 2:
            warnings.warn("PCA至少需要2个因子，返回原始价格序列")
            return pd.Series(price)

        factor_matrix = np.column_stack(factor_arrays)

        # 1. 过滤低方差因子
        if filter_low_variance:
            variances = np.nanvar(factor_matrix, axis=0)
            mask = variances > 1e-6
            if mask.sum() < 2:
                mask = np.ones(factor_matrix.shape[1], dtype=bool)
            factor_matrix = factor_matrix[:, mask]

        # 2. 标准化
        means = np.nanmean(factor_matrix, axis=0)
        stds = np.nanstd(factor_matrix, axis=0)
        stds[stds < 1e-12] = 1.0
        standardized = (factor_matrix - means) / stds
        standardized = np.nan_to_num(standardized, nan=0.0)

        # 3. PCA (使用 SVD)
        n_comp = min(n_components, standardized.shape[1], n - 1)
        if n_comp <= 0:
            return pd.Series(np.zeros(n))

        U, S, Vt = np.linalg.svd(standardized, full_matrices=False)
        components = U[:, :n_comp] * S[:n_comp]
        var_ratios = (S[:n_comp] ** 2) / (S ** 2).sum()

        # 4. 方差加权组合
        result = components @ var_ratios[:n_comp]

        # 5. 动态符号调整 (确保与价格正相关)
        if dynamic_sign and len(price) > 10:
            corr = np.corrcoef(result, price)[0, 1]
            if np.isnan(corr):
                corr = 0.0
            if corr < 0:
                result = -result

        return pd.Series(result)

    @tobtind(overlap=True, lib="factor")
    def adaptive_weight_trend(self, windows: list = [5, 20, 50], lookback: int = 10, **kwargs)->IndSeries:
        """
        ## 自适应权重趋势指标

        - 基于因子历史表现（与价格的相关性）动态调整权重，
        - 对多个时间窗口的移动平均进行自适应加权。
        """
        price = _to_array(self)
        n = len(price)
        windows = [w for w in windows if w < n]
        if not windows:
            return pd.Series(price)

        # 1. 计算各窗口均线
        ma_matrix = np.zeros((n, len(windows)))
        for i, w in enumerate(windows):
            ma = pd.Series(price).rolling(w, min_periods=1).mean().values
            ma_matrix[:, i] = ma

        # 2. 滚动相关性 → 动态权重
        weights = np.zeros((n, len(windows)))
        for t in range(n):
            w_start = max(0, t - lookback)
            if t - w_start < 2:
                weights[t] = 1.0 / len(windows)
                continue

            corrs = np.zeros(len(windows))
            for j in range(len(windows)):
                seg_p = price[w_start:t]
                seg_m = ma_matrix[w_start:t, j]
                if np.std(seg_p) > 1e-12 and np.std(seg_m) > 1e-12:
                    corrs[j] = np.corrcoef(seg_p, seg_m)[0, 1]

            # 负相关置零，归一化
            corrs = np.maximum(corrs, 0)
            s = corrs.sum()
            if s > 0:
                weights[t] = corrs / s
            else:
                weights[t] = 1.0 / len(windows)

        # 3. 加权合成
        result = np.array([np.dot(ma_matrix[t], weights[t]) for t in range(n)])
        return pd.Series(result)

    @tobtind(lines=["merged_factor", "signals"], lib="factor")
    def factor_optimizer(self, *factors: tuple[pd.DataFrame | pd.Series],
                         max_weight: float = 0.8, l2_reg: float = 0.0001,
                         min_ic_abs: float = 0.03, n_init_points: int = 10,
                         optimization_model: str = "scipy", **kwargs)->IndFrame:
        """
        ## 因子优化器

        - 使用优化算法寻找最优因子组合权重，最大化组合因子的信息比率（IR），
        - 同时考虑权重约束和正则化。
        """
        price = _to_array(self)
        n = len(price)

        factor_arrays = []
        for f in factors:
            arr = _to_array(f)
            if len(arr) == n:
                factor_arrays.append(arr)

        if len(factor_arrays) == 0:
            return pd.DataFrame({"merged_factor": np.zeros(n), "signals": np.zeros(n)})

        k = len(factor_arrays)
        factor_matrix = np.column_stack(factor_arrays)

        # 1. 未来收益率
        returns = np.zeros(n)
        if n > 1:
            returns[:-1] = price[1:] / price[:-1] - 1.0

        # 2. 计算每个因子的 IC 序列
        ic_series = np.zeros((n, k))
        for j in range(k):
            for t in range(1, n):
                ic_series[t, j] = _spearman_ic(factor_matrix[:t, j], returns[:t])

        ic_means = np.nanmean(ic_series, axis=0)
        ic_stds = np.nanstd(ic_series, axis=0)
        ic_stds[ic_stds < 1e-12] = 1.0
        irs = ic_means / ic_stds

        # 3. 筛选有效因子
        valid_mask = np.abs(ic_means) >= min_ic_abs
        if valid_mask.sum() == 0:
            valid_mask = np.abs(ic_means) >= np.sort(np.abs(ic_means))[max(0, k - 3)]
        valid_indices = np.where(valid_mask)[0]
        valid_k = len(valid_indices)

        if valid_k == 0:
            return pd.DataFrame({"merged_factor": np.zeros(n), "signals": np.zeros(n)})

        valid_factors = factor_matrix[:, valid_indices]
        valid_irs = irs[valid_indices]

        # 4. 优化权重
        def _objective(w):
            port_ic = ic_series[:, valid_indices] @ w
            mean_ic = np.nanmean(port_ic)
            std_ic = np.nanstd(port_ic)
            if std_ic < 1e-12:
                return -abs(mean_ic)
            ir_val = mean_ic / std_ic
            l2_penalty = l2_reg * np.sum(w ** 2)
            return -(abs(ir_val) - l2_penalty)

        best_w = None
        best_obj = np.inf

        if optimization_model == "scipy" and _get_scipy_stats() is not None:
            from scipy.optimize import minimize
            bounds = [(0, max_weight)] * valid_k
            constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
            for _ in range(n_init_points):
                w0 = np.random.dirichlet(np.ones(valid_k))
                res = minimize(_objective, w0, method="SLSQP",
                               bounds=bounds, constraints=constraints)
                if res.fun < best_obj:
                    best_obj = res.fun
                    best_w = res.x
        else:
            # 随机搜索 + IR加权回退
            for _ in range(n_init_points * 5):
                w = np.random.dirichlet(np.ones(valid_k))
                obj = _objective(w)
                if obj < best_obj:
                    best_obj = obj
                    best_w = w.copy()

            # IR 加权作为 fallback
            if best_w is None:
                pos_irs = np.maximum(valid_irs, 0.01)
                best_w = pos_irs / pos_irs.sum()

        if best_w is None:
            best_w = np.ones(valid_k) / valid_k

        # 5. 合成因子与信号
        merged = valid_factors @ best_w
        signals = np.zeros(n)
        signals[merged > 0] = 1.0
        signals[merged < 0] = -1.0

        return pd.DataFrame({"merged_factor": merged, "signals": signals})
