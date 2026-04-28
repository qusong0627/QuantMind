"""
风险指标计算
提供各种量化风险指标的计算功能
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RiskMetricsResult:
    """风险指标计算结果"""

    # 基础统计指标
    total_return: float
    annual_return: float
    volatility: float
    downside_volatility: float

    # 风险价值指标
    var_95: float
    var_99: float
    cvar_95: float
    cvar_99: float

    # 回撤指标
    max_drawdown: float
    max_drawdown_duration: int
    recovery_factor: float
    calmar_ratio: float

    # 风险调整收益指标
    sharpe_ratio: float
    sortino_ratio: float
    information_ratio: float
    treynor_ratio: float

    # 其他指标
    beta: float
    alpha: float
    tracking_error: float
    omega_ratio: float
    gain_loss_ratio: float

    # 计算时间
    calculated_at: datetime

    # 附加信息
    lookback_period: int
    data_points: int


class RiskMetrics:
    """风险指标计算器"""

    def __init__(self, risk_free_rate: float = 0.02):
        self.risk_free_rate = risk_free_rate  # 年化无风险利率

    def calculate_all_metrics(
        self,
        returns: list[float],
        benchmark_returns: list[float] | None = None,
        lookback_period: int = 252,
    ) -> RiskMetricsResult:
        """
        计算所有风险指标

        Args:
            returns: 收益率序列
            benchmark_returns: 基准收益率序列 (可选)
            lookback_period: 回看期间

        Returns:
            风险指标结果
        """
        if not returns:
            raise ValueError("收益率序列不能为空")

        returns_array = np.array(returns)
        n = len(returns_array)

        # 基础统计指标
        total_return = self._calculate_total_return(returns_array)
        annual_return = self._calculate_annual_return(returns_array, lookback_period)
        volatility = self._calculate_volatility(returns_array, lookback_period)
        downside_volatility = self._calculate_downside_volatility(returns_array, lookback_period)

        # 风险价值指标
        var_95, var_99 = self._calculate_var(returns_array)
        cvar_95, cvar_99 = self._calculate_cvar(returns_array, var_95, var_99)

        # 回撤指标
        max_drawdown, max_dd_duration = self._calculate_max_drawdown(returns_array)
        recovery_factor = self._calculate_recovery_factor(total_return, max_drawdown)
        calmar_ratio = self._calculate_calmar_ratio(annual_return, max_drawdown)

        # 风险调整收益指标
        sharpe_ratio = self._calculate_sharpe_ratio(annual_return, volatility)
        sortino_ratio = self._calculate_sortino_ratio(annual_return, returns_array, lookback_period)

        # 基准相关指标
        beta = 0.0
        alpha = 0.0
        information_ratio = 0.0
        tracking_error = 0.0

        if benchmark_returns:
            benchmark_array = np.array(benchmark_returns)
            if len(benchmark_array) == len(returns_array):
                beta, alpha = self._calculate_beta_alpha(returns_array, benchmark_array)
                tracking_error = self._calculate_tracking_error(returns_array, benchmark_array, lookback_period)
                information_ratio = self._calculate_information_ratio(annual_return, tracking_error)

        # 其他指标
        treynor_ratio = self._calculate_treynor_ratio(annual_return, beta)
        omega_ratio = self._calculate_omega_ratio(returns_array)
        gain_loss_ratio = self._calculate_gain_loss_ratio(returns_array)

        return RiskMetricsResult(
            total_return=total_return,
            annual_return=annual_return,
            volatility=volatility,
            downside_volatility=downside_volatility,
            var_95=var_95,
            var_99=var_99,
            cvar_95=cvar_95,
            cvar_99=cvar_99,
            max_drawdown=max_drawdown,
            max_drawdown_duration=max_dd_duration,
            recovery_factor=recovery_factor,
            calmar_ratio=calmar_ratio,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            information_ratio=information_ratio,
            treynor_ratio=treynor_ratio,
            beta=beta,
            alpha=alpha,
            tracking_error=tracking_error,
            omega_ratio=omega_ratio,
            gain_loss_ratio=gain_loss_ratio,
            calculated_at=datetime.now(),
            lookback_period=lookback_period,
            data_points=n,
        )

    def _calculate_total_return(self, returns: np.ndarray) -> float:
        """计算总收益率"""
        return np.prod(1 + returns) - 1

    def _calculate_annual_return(self, returns: np.ndarray, lookback_period: int) -> float:
        """计算年化收益率"""
        total_return = self._calculate_total_return(returns)
        years = len(returns) / lookback_period
        if years > 0:
            return (1 + total_return) ** (1 / years) - 1
        return 0.0

    def _calculate_volatility(self, returns: np.ndarray, lookback_period: int) -> float:
        """计算年化波动率"""
        return np.std(returns, ddof=1) * np.sqrt(lookback_period)

    def _calculate_downside_volatility(self, returns: np.ndarray, lookback_period: int) -> float:
        """计算下行波动率"""
        downside_returns = returns[returns < 0]
        if len(downside_returns) > 0:
            return np.std(downside_returns, ddof=1) * np.sqrt(lookback_period)
        return 0.0

    def _calculate_var(self, returns: np.ndarray) -> tuple[float, float]:
        """计算VaR (Value at Risk)"""
        var_95 = np.percentile(returns, 5)
        var_99 = np.percentile(returns, 1)
        return var_95, var_99

    def _calculate_cvar(self, returns: np.ndarray, var_95: float, var_99: float) -> tuple[float, float]:
        """计算CVaR (Conditional Value at Risk)"""
        # 95% CVaR
        cvar_95 = np.mean(returns[returns <= var_95]) if var_95 is not None else 0.0

        # 99% CVaR
        cvar_99 = np.mean(returns[returns <= var_99]) if var_99 is not None else 0.0

        return cvar_95, cvar_99

    def _calculate_max_drawdown(self, returns: np.ndarray) -> tuple[float, int]:
        """计算最大回撤和持续时间"""
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (cumulative - running_max) / running_max

        max_drawdown = np.min(drawdowns)

        # 计算最大回撤持续时间
        drawdown_periods = []
        in_drawdown = False
        start_idx = 0

        for i, dd in enumerate(drawdowns):
            if dd < 0 and not in_drawdown:
                in_drawdown = True
                start_idx = i
            elif dd >= 0 and in_drawdown:
                in_drawdown = False
                drawdown_periods.append(i - start_idx)

        # 如果结束时仍在回撤中
        if in_drawdown:
            drawdown_periods.append(len(drawdowns) - start_idx)

        max_duration = max(drawdown_periods) if drawdown_periods else 0

        return max_drawdown, max_duration

    def _calculate_recovery_factor(self, total_return: float, max_drawdown: float) -> float:
        """计算恢复因子"""
        if max_drawdown != 0:
            return total_return / abs(max_drawdown)
        return float("in") if total_return > 0 else 0.0

    def _calculate_calmar_ratio(self, annual_return: float, max_drawdown: float) -> float:
        """计算卡尔玛比率"""
        if max_drawdown != 0:
            return annual_return / abs(max_drawdown)
        return 0.0

    def _calculate_sharpe_ratio(self, annual_return: float, volatility: float) -> float:
        """计算夏普比率"""
        if volatility != 0:
            excess_return = annual_return - self.risk_free_rate
            return excess_return / volatility
        return 0.0

    def _calculate_sortino_ratio(self, annual_return: float, returns: np.ndarray, lookback_period: int) -> float:
        """计算索提诺比率（下行基于 MAR=无风险利率）"""
        daily_rf = self.risk_free_rate / lookback_period
        downside_returns = returns[returns < daily_rf]
        if len(downside_returns) == 0:
            return 0.0
        downside_deviation = np.std(downside_returns, ddof=1) * np.sqrt(lookback_period)
        if downside_deviation != 0:
            excess_return = annual_return - self.risk_free_rate
            return excess_return / downside_deviation
        return 0.0

    def _calculate_beta_alpha(self, returns: np.ndarray, benchmark_returns: np.ndarray) -> tuple[float, float]:
        """计算Beta和Alpha"""
        if len(returns) != len(benchmark_returns) or len(returns) < 2:
            return 0.0, 0.0

        # 计算协方差和方差
        covariance = np.cov(returns, benchmark_returns)[0, 1]
        benchmark_variance = np.var(benchmark_returns)

        if benchmark_variance != 0:
            beta = covariance / benchmark_variance
        else:
            beta = 0.0

        # 计算Alpha
        benchmark_mean = np.mean(benchmark_returns) * 252  # 年化
        strategy_mean = np.mean(returns) * 252  # 年化
        alpha = strategy_mean - (self.risk_free_rate + beta * (benchmark_mean - self.risk_free_rate))

        return beta, alpha

    def _calculate_tracking_error(
        self, returns: np.ndarray, benchmark_returns: np.ndarray, lookback_period: int
    ) -> float:
        """计算跟踪误差"""
        if len(returns) != len(benchmark_returns):
            return 0.0

        excess_returns = returns - benchmark_returns
        return np.std(excess_returns, ddof=1) * np.sqrt(lookback_period)

    def _calculate_information_ratio(self, annual_return: float, tracking_error: float) -> float:
        """计算信息比率"""
        if tracking_error != 0:
            excess_return = annual_return - self.risk_free_rate
            return excess_return / tracking_error
        return 0.0

    def _calculate_treynor_ratio(self, annual_return: float, beta: float) -> float:
        """计算特雷纳比率"""
        if beta != 0:
            excess_return = annual_return - self.risk_free_rate
            return excess_return / beta
        return 0.0

    def _calculate_omega_ratio(self, returns: np.ndarray, threshold: float = 0.0) -> float:
        """计算Omega比率"""
        gains = returns[returns > threshold] - threshold
        losses = threshold - returns[returns <= threshold]

        if len(losses) > 0:
            sum_losses = np.sum(losses)
            if sum_losses == 0:
                return float("in") if len(gains) > 0 else 0.0
            return np.sum(gains) / sum_losses
        return float("in") if len(gains) > 0 else 0.0

    def _calculate_gain_loss_ratio(self, returns: np.ndarray) -> float:
        """计算盈亏比"""
        gains = returns[returns > 0]
        losses = returns[returns < 0]

        if len(gains) > 0 and len(losses) > 0:
            avg_gain = np.mean(gains)
            avg_loss = np.mean(np.abs(losses))
            return avg_gain / avg_loss if avg_loss != 0 else float("in")
        return 0.0

    def calculate_rolling_metrics(self, returns: list[float], window: int = 30) -> dict[str, list[float]]:
        """
        计算滚动风险指标

        Args:
            returns: 收益率序列
            window: 滚动窗口大小

        Returns:
            滚动指标字典
        """
        if len(returns) < window:
            return {}

        returns_array = np.array(returns)
        n = len(returns_array)

        rolling_volatility = []
        rolling_sharpe = []
        rolling_max_drawdown = []

        for i in range(window, n + 1):
            window_returns = returns_array[i - window : i]

            # 滚动波动率
            vol = np.std(window_returns, ddof=1) * np.sqrt(252)
            rolling_volatility.append(vol)

            # 滚动夏普比率
            annual_return = np.mean(window_returns) * 252
            if vol > 0:
                sharpe = (annual_return - self.risk_free_rate) / vol
            else:
                sharpe = 0.0
            rolling_sharpe.append(sharpe)

            # 滚动最大回撤
            cumulative = np.cumprod(1 + window_returns)
            running_max = np.maximum.accumulate(cumulative)
            drawdowns = (cumulative - running_max) / running_max
            max_dd = np.min(drawdowns)
            rolling_max_drawdown.append(max_dd)

        return {
            "rolling_volatility": rolling_volatility,
            "rolling_sharpe": rolling_sharpe,
            "rolling_max_drawdown": rolling_max_drawdown,
        }

    def calculate_stress_test(self, returns: list[float], stress_scenarios: dict[str, list[float]]) -> dict[str, float]:
        """
        压力测试

        Args:
            returns: 历史收益率
            stress_scenarios: 压力场景

        Returns:
            压力测试结果
        """
        if not returns:
            return {}

        current_value = 100000  # 假设当前组合价值10万

        results = {}

        for scenario_name, scenario_returns in stress_scenarios.items():
            # 应用压力场景到当前组合
            stressed_value = current_value
            for ret in scenario_returns:
                stressed_value *= 1 + ret

            # 计算损失
            loss = (stressed_value - current_value) / current_value
            results[scenario_name] = loss

        return results

    def calculate_correlation_matrix(self, returns_dict: dict[str, list[float]]) -> pd.DataFrame:
        """
        计算收益率相关性矩阵

        Args:
            returns_dict: 多个资产的收益率字典

        Returns:
            相关性矩阵
        """
        if not returns_dict:
            return pd.DataFrame()

        # 找到最短的数据长度
        min_length = min(len(returns) for returns in returns_dict.values())

        # 构建DataFrame
        data = {}
        for name, returns in returns_dict.items():
            if len(returns) >= min_length:
                data[name] = returns[:min_length]

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)
        return df.corr()

    def calculate_risk_contribution(self, returns: list[float], weights: list[float]) -> dict[str, float]:
        """
        计算风险贡献度

        Args:
            returns: 收益率矩阵 (n_assets x n_periods)
            weights: 权重向量

        Returns:
            各资产的风险贡献
        """
        # 这里是一个简化实现
        # 实际应用中可能需要更复杂的计算

        returns_array = np.array(returns)
        if len(returns_array.shape) == 1:
            # 单资产情况
            return {"asset_1": 1.0}

        # 计算协方差矩阵
        cov_matrix = np.cov(returns_array)

        # 计算组合方差
        weights_array = np.array(weights)
        portfolio_variance = np.dot(weights_array, np.dot(cov_matrix, weights_array))

        # 计算边际风险贡献
        marginal_contrib = np.dot(cov_matrix, weights_array)

        # 计算风险贡献
        risk_contrib = weights_array * marginal_contrib / portfolio_variance

        return {f"asset_{i + 1}": contrib for i, contrib in enumerate(risk_contrib)}
