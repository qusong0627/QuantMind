"""
风险管理器
统一管理各种风险控制功能
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """风险配置"""

    # 基础风险控制
    max_position_size: float = 0.8  # 最大仓位比例
    max_portfolio_risk: float = 0.15  # 最大组合风险
    max_drawdown_limit: float = 0.20  # 最大回撤限制

    # 止损止盈
    stop_loss_pct: float = 0.05  # 止损比例 (5%)
    take_profit_pct: float = 0.15  # 止盈比例 (15%)
    trailing_stop_pct: float = 0.03  # 移动止损比例 (3%)

    # 仓位管理
    min_position_size: float = 0.01  # 最小仓位比例
    position_sizing_method: str = "fixed"  # 仓位计算方法: fixed, percent, kelly, volatility
    max_positions: int = 10  # 最大持仓数量

    # 风险指标
    var_confidence: float = 0.95  # VaR置信度
    cvar_confidence: float = 0.95  # CVaR置信度
    lookback_period: int = 252  # 回看期间(交易日)

    # 其他
    rebalance_threshold: float = 0.05  # 再平衡阈值
    risk_free_rate: float = 0.02  # 无风险利率


@dataclass
class RiskAlert:
    """风险警告"""

    alert_type: str
    severity: str  # low, medium, high, critical
    message: str
    current_value: float
    threshold: float
    timestamp: datetime
    symbol: str | None = None
    recommendations: list[str] = None

    def __post_init__(self):
        if self.recommendations is None:
            self.recommendations = []


class RiskManager:
    """风险管理器"""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self.alerts: list[RiskAlert] = []
        self.risk_metrics_history: list[dict] = []
        self.position_limits: dict[str, float] = {}
        self.is_risk_limit_breached = False

    def check_position_size(
        self, symbol: str, requested_size: float, current_portfolio_value: float
    ) -> tuple[bool, float, str]:
        """
        检查仓位大小是否合规

        Args:
            symbol: 股票代码
            requested_size: 请求的仓位大小
            current_portfolio_value: 当前投资组合价值

        Returns:
            (是否合规, 建议仓位大小, 原因)
        """
        if current_portfolio_value <= 0:
            return False, 0, "投资组合价值无效"

            # 计算请求的仓位比例
        requested_position_value = requested_size
        requested_ratio = requested_position_value / current_portfolio_value

        # 检查最大仓位限制
        if requested_ratio > self.config.max_position_size:
            suggested_size = current_portfolio_value * self.config.max_position_size
            reason = f"仓位比例{requested_ratio:.2%}超过限制{self.config.max_position_size:.2%}"
            return False, suggested_size, reason

            # 检查最小仓位限制
        if requested_ratio < self.config.min_position_size:
            suggested_size = current_portfolio_value * self.config.min_position_size
            reason = f"仓位比例{requested_ratio:.2%}小于最小值{self.config.min_position_size:.2%}"
            return False, suggested_size, reason

            # 检查单个股票的仓位限制
        symbol_limit = self.position_limits.get(symbol, self.config.max_position_size)
        if requested_ratio > symbol_limit:
            suggested_size = current_portfolio_value * symbol_limit
            reason = f"{symbol}仓位比例{requested_ratio:.2%}超过限制{symbol_limit:.2%}"
            return False, suggested_size, reason

        return True, requested_size, "仓位检查通过"

    def check_portfolio_risk(self, portfolio_value: float, portfolio_returns: list[float]) -> dict[str, Any]:
        """
        检查投资组合整体风险

        Args:
            portfolio_value: 投资组合价值
            portfolio_returns: 投资组合收益率序列

        Returns:
            风险评估结果
        """
        if not portfolio_returns:
            return {"risk_level": "unknown", "metrics": {}, "alerts": []}

        import numpy as np

        returns_array = np.array(portfolio_returns)

        # 计算风险指标
        volatility = np.std(returns_array, ddof=1) * np.sqrt(252)  # 年化波动率
        downside_returns = returns_array[returns_array < 0]
        downside_volatility = np.std(downside_returns, ddof=1) * np.sqrt(252) if len(downside_returns) > 0 else 0

        # VaR和CVaR
        var_95 = np.percentile(returns_array, 5)
        cvar_95 = np.mean(returns_array[returns_array <= var_95]) if var_95 is not None else 0

        # 最大回撤
        cumulative = np.cumprod(1 + returns_array)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (cumulative - running_max) / running_max
        max_drawdown = np.min(drawdowns)

        # 夏普比率
        excess_returns = returns_array - self.config.risk_free_rate / 252
        sharpe_ratio = (
            np.mean(excess_returns) / np.std(excess_returns, ddof=1) * np.sqrt(252)
            if np.std(excess_returns, ddof=1) > 0
            else 0
        )

        # 风险等级评估
        risk_level = self._assess_risk_level(volatility, max_drawdown, var_95)

        # 检查风险限制
        alerts = []

        # 检查最大回撤限制
        if abs(max_drawdown) > self.config.max_drawdown_limit:
            alert = RiskAlert(
                alert_type="max_drawdown",
                severity="high",
                message=f"最大回撤{abs(max_drawdown):.2%}超过限制{self.config.max_drawdown_limit:.2%}",
                current_value=abs(max_drawdown),
                threshold=self.config.max_drawdown_limit,
                timestamp=datetime.now(),
                recommendations=["减少仓位", "增加对冲", "降低投资组合风险"],
            )
            alerts.append(alert)
            self.is_risk_limit_breached = True

            # 检查波动率限制
        if volatility > 0.3:  # 30%年化波动率作为警告阈值
            alert = RiskAlert(
                alert_type="high_volatility",
                severity="medium",
                message=f"投资组合波动率{volatility:.2%}过高",
                current_value=volatility,
                threshold=0.3,
                timestamp=datetime.now(),
                recommendations=["降低高风险资产比例", "增加稳定收益资产"],
            )
            alerts.append(alert)

            # 检查VaR
        if var_95 < -0.05:  # 5%日VaR作为警告阈值
            alert = RiskAlert(
                alert_type="var_warning",
                severity="medium",
                message=f"95% VaR为{var_95:.2%}，风险较高",
                current_value=var_95,
                threshold=-0.05,
                timestamp=datetime.now(),
                recommendations=["降低杠杆", "增加分散化投资"],
            )
            alerts.append(alert)

            # 记录风险指标
        risk_metrics = {
            "timestamp": datetime.now(),
            "portfolio_value": portfolio_value,
            "volatility": volatility,
            "max_drawdown": max_drawdown,
            "var_95": var_95,
            "cvar_95": cvar_95,
            "sharpe_ratio": sharpe_ratio,
            "downside_volatility": downside_volatility,
            "risk_level": risk_level,
        }

        self.risk_metrics_history.append(risk_metrics)

        # 添加到警告列表
        self.alerts.extend(alerts)

        return {
            "risk_level": risk_level,
            "metrics": risk_metrics,
            "alerts": alerts,
            "is_limit_breached": self.is_risk_limit_breached,
        }

    def calculate_position_size(
        self,
        symbol: str,
        current_price: float,
        portfolio_value: float,
        available_cash: float,
        method: str | None = None,
    ) -> float:
        """
        计算建议的仓位大小

        Args:
            symbol: 股票代码
            current_price: 当前价格
            portfolio_value: 投资组合价值
            available_cash: 可用现金
            method: 仓位计算方法

        Returns:
            建议的仓位大小(股数)
        """
        method = method or self.config.position_sizing_method

        if method == "fixed":
            # 固定金额
            position_value = portfolio_value * 0.1  # 10%固定比例
        elif method == "percent":
            # 百分比法
            position_value = portfolio_value * 0.2  # 20%比例
        elif method == "kelly":
            # Kelly公式（简化版）
            position_value = portfolio_value * 0.25  # 简化为25%
        elif method == "volatility":
            # 波动率调整法
            position_value = portfolio_value * 0.15  # 15%基础比例
        else:
            position_value = portfolio_value * 0.1

            # 考虑现金限制
        position_value = min(position_value, available_cash * 0.95)  # 保留5%现金

        # 考虑最大仓位限制
        max_position_value = portfolio_value * self.config.max_position_size
        position_value = min(position_value, max_position_value)

        # 计算股数
        shares = int(position_value / current_price)

        return max(0, shares)

    def should_stop_loss(
        self, symbol: str, entry_price: float, current_price: float, position_side: str
    ) -> tuple[bool, str]:
        """
        判断是否应该止损

        Args:
            symbol: 股票代码
            entry_price: 入场价格
            current_price: 当前价格
            position_side: 持仓方向 (long/short)

        Returns:
            (是否止损, 原因)
        """
        if position_side == "long":
            return_pct = (current_price - entry_price) / entry_price

            if return_pct <= -self.config.stop_loss_pct:
                return (
                    True,
                    f"触发止损：收益率{return_pct:.2%} <= -{self.config.stop_loss_pct:.2%}",
                )

            if return_pct >= self.config.take_profit_pct:
                return (
                    True,
                    f"触发止盈：收益率{return_pct:.2%} >= {self.config.take_profit_pct:.2%}",
                )

        elif position_side == "short":
            return_pct = (entry_price - current_price) / entry_price

            if return_pct <= -self.config.stop_loss_pct:
                return (
                    True,
                    f"触发止损：收益率{return_pct:.2%} <= -{self.config.stop_loss_pct:.2%}",
                )

            if return_pct >= self.config.take_profit_pct:
                return (
                    True,
                    f"触发止盈：收益率{return_pct:.2%} >= {self.config.take_profit_pct:.2%}",
                )

        return False, ""

    def update_position_limit(self, symbol: str, max_ratio: float):
        """
        更新单个股票的仓位限制

        Args:
            symbol: 股票代码
            max_ratio: 最大仓位比例
        """
        self.position_limits[symbol] = min(max_ratio, self.config.max_position_size)
        logger.info(f"更新 {symbol} 仓位限制为 {self.position_limits[symbol]:.2%}")

    def get_risk_summary(self) -> dict[str, Any]:
        """获取风险摘要"""
        if not self.risk_metrics_history:
            return {
                "current_risk_level": "unknown",
                "total_alerts": 0,
                "active_alerts": 0,
                "risk_trend": "stable",
            }

        latest_metrics = self.risk_metrics_history[-1]
        active_alerts = [alert for alert in self.alerts if (datetime.now() - alert.timestamp).days <= 1]

        # 分析风险趋势
        risk_trend = "stable"
        if len(self.risk_metrics_history) >= 5:
            recent_volatilities = [m["volatility"] for m in self.risk_metrics_history[-5:]]
            if recent_volatilities[-1] > recent_volatilities[0] * 1.2:
                risk_trend = "increasing"
            elif recent_volatilities[-1] < recent_volatilities[0] * 0.8:
                risk_trend = "decreasing"

        return {
            "current_risk_level": latest_metrics.get("risk_level", "unknown"),
            "total_alerts": len(self.alerts),
            "active_alerts": len(active_alerts),
            "risk_trend": risk_trend,
            "latest_metrics": latest_metrics,
            "position_limits": self.position_limits.copy(),
            "is_limit_breached": self.is_risk_limit_breached,
        }

    def clear_alerts(self, older_than_days: int = 7):
        """清理旧的警告"""
        cutoff_time = datetime.now() - timedelta(days=older_than_days)
        self.alerts = [alert for alert in self.alerts if alert.timestamp > cutoff_time]
        logger.info(f"清理了 {len(self.alerts)} 个风险警告")

    def reset(self):
        """重置风险管理器"""
        self.alerts.clear()
        self.risk_metrics_history.clear()
        self.position_limits.clear()
        self.is_risk_limit_breached = False
        logger.info("风险管理器已重置")

    def _assess_risk_level(self, volatility: float, max_drawdown: float, var_95: float) -> str:
        """评估风险等级"""
        risk_score = 0

        # 波动率评分 (0-40分)
        if volatility > 0.4:
            risk_score += 40
        elif volatility > 0.3:
            risk_score += 30
        elif volatility > 0.2:
            risk_score += 20
        elif volatility > 0.15:
            risk_score += 10

            # 最大回撤评分 (0-40分)
        if abs(max_drawdown) > 0.3:
            risk_score += 40
        elif abs(max_drawdown) > 0.2:
            risk_score += 30
        elif abs(max_drawdown) > 0.15:
            risk_score += 20
        elif abs(max_drawdown) > 0.1:
            risk_score += 10

            # VaR评分 (0-20分)
        if var_95 < -0.08:
            risk_score += 20
        elif var_95 < -0.06:
            risk_score += 15
        elif var_95 < -0.04:
            risk_score += 10
        elif var_95 < -0.02:
            risk_score += 5

            # 根据总分确定风险等级
        if risk_score >= 70:
            return "very_high"
        elif risk_score >= 50:
            return "high"
        elif risk_score >= 30:
            return "medium"
        elif risk_score >= 15:
            return "low"
        else:
            return "very_low"
