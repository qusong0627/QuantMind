"""
实时风控监控服务

功能:
1. 监控回测中的风险指标
2. 实时推送风险告警
3. 提供风控阈值配置
4. 自动触发风控措施
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from backend.services.engine.qlib_app.services.basic_risk_service import (
    BasicRiskService,
)
from backend.services.engine.qlib_app.websocket.connection_manager import ws_manager
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)


class RiskControlConfig:
    """风控配置"""

    def __init__(
        self,
        max_drawdown_threshold: float = 0.20,  # 最大回撤阈值20%
        sharpe_threshold: float = 0.5,  # 夏普比率阈值
        volatility_threshold: float = 0.30,  # 波动率阈值30%
        var_threshold: float = -0.05,  # VaR阈值-5%
        position_concentration_threshold: float = 0.30,  # 单股持仓阈值30%
        enable_auto_stop: bool = False,  # 是否自动停止
    ):
        self.max_drawdown_threshold = max_drawdown_threshold
        self.sharpe_threshold = sharpe_threshold
        self.volatility_threshold = volatility_threshold
        self.var_threshold = var_threshold
        self.position_concentration_threshold = position_concentration_threshold
        self.enable_auto_stop = enable_auto_stop


class RiskAlert:
    """风险告警"""

    def __init__(
        self,
        alert_type: str,  # 'warning' | 'critical'
        indicator: str,  # 指标名称
        current_value: float,  # 当前值
        threshold: float,  # 阈值
        message: str,  # 告警消息
        timestamp: datetime | None = None,
    ):
        self.alert_type = alert_type
        self.indicator = indicator
        self.current_value = current_value
        self.threshold = threshold
        self.message = message
        self.timestamp = timestamp or datetime.now()

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "alert_type": self.alert_type,
            "indicator": self.indicator,
            "current_value": self.current_value,
            "threshold": self.threshold,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
        }


class RealTimeRiskMonitor:
    """实时风控监控器"""

    def __init__(
        self,
        backtest_id: str,
        config: RiskControlConfig | None = None,
    ):
        self.backtest_id = backtest_id
        self.config = config or RiskControlConfig()
        self.risk_service = BasicRiskService()
        self.alerts: list[RiskAlert] = []
        self.is_monitoring = False
        self._stop_signal = False

    async def start_monitoring(
        self,
        equity_curve: pd.Series,
        positions: pd.DataFrame | None = None,
        update_interval: int = 5,  # 更新间隔（秒）
    ):
        """
        开始监控

        Args:
            equity_curve: 权益曲线
            positions: 持仓数据（可选）
            update_interval: 更新间隔（秒）
        """
        self.is_monitoring = True
        StructuredTaskLogger(
            logger,
            "risk-monitor-service",
            {"backtest_id": self.backtest_id},
        ).info("start", "开始风控监控")

        try:
            while self.is_monitoring and not self._stop_signal:
                # 1. 计算当前风险指标
                risk_metrics = await self._calculate_current_risk(equity_curve, positions)

                # 2. 检查风险阈值
                alerts = self._check_risk_thresholds(risk_metrics)

                # 3. 发送风险更新
                await self._broadcast_risk_update(risk_metrics, alerts)

                # 4. 处理告警
                if alerts:
                    await self._handle_alerts(alerts)

                # 5. 等待下一次更新
                await asyncio.sleep(update_interval)

        except Exception as e:
            StructuredTaskLogger(
                logger,
                "risk-monitor-service",
                {"backtest_id": self.backtest_id},
            ).exception("failed", "风控监控异常", error=e)
        finally:
            self.is_monitoring = False
            StructuredTaskLogger(
                logger,
                "risk-monitor-service",
                {"backtest_id": self.backtest_id},
            ).info("stop", "风控监控已停止")

    async def run_once(
        self,
        equity_curve: pd.Series,
        positions: pd.DataFrame | None = None,
    ) -> dict:
        """执行一次风险评估（无后台循环）。"""
        self.is_monitoring = True
        try:
            risk_metrics = await self._calculate_current_risk(equity_curve, positions)
            alerts = self._check_risk_thresholds(risk_metrics)
            await self._broadcast_risk_update(risk_metrics, alerts)
            if alerts:
                await self._handle_alerts(alerts)
            return {"metrics": risk_metrics, "alerts": [a.to_dict() for a in alerts]}
        finally:
            self.is_monitoring = False

    async def stop_monitoring(self):
        """停止监控"""
        self._stop_signal = True
        self.is_monitoring = False

    async def _calculate_current_risk(
        self,
        equity_curve: pd.Series,
        positions: pd.DataFrame | None = None,
    ) -> dict:
        """
        计算当前风险指标

        Returns:
            风险指标字典
        """
        try:
            # 计算收益率
            returns = equity_curve.pct_change().dropna()

            if len(returns) == 0:
                return {}

            # 计算基础风险指标
            daily_std = float(returns.std(ddof=1))
            annualized_return = float(returns.mean() * 252)
            risk_free_rate = 0.02  # 年化无风险利率 2%
            metrics = {
                "current_value": float(equity_curve.iloc[-1]),
                "total_return": float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1),
                "volatility": float(daily_std * (252**0.5)),
                "sharpe_ratio": float(
                    (annualized_return - risk_free_rate) / (daily_std * (252**0.5))
                    if daily_std > 0
                    else 0
                ),
                "max_drawdown": float((equity_curve / equity_curve.cummax() - 1).min()),
                "var_95": float(returns.quantile(0.05)),
                "cvar_95": float(returns[returns <= returns.quantile(0.05)].mean()),
            }

            # 如果有持仓数据，计算持仓集中度
            if positions is not None and len(positions) > 0:
                latest_positions = positions.iloc[-1]
                if isinstance(latest_positions, pd.Series):
                    # 计算最大单股持仓比例
                    max_position = float(latest_positions.max())
                    metrics["max_position_ratio"] = max_position

            return metrics

        except Exception as e:
            StructuredTaskLogger(
                logger,
                "risk-monitor-service",
                {"backtest_id": self.backtest_id},
            ).exception("metrics_failed", "计算风险指标失败", error=e)
            return {}

    def _check_risk_thresholds(self, risk_metrics: dict) -> list[RiskAlert]:
        """
        检查风险阈值

        Args:
            risk_metrics: 风险指标

        Returns:
            告警列表
        """
        alerts = []

        # 检查最大回撤
        max_drawdown = risk_metrics.get("max_drawdown", 0)
        if max_drawdown < -self.config.max_drawdown_threshold:
            alerts.append(
                RiskAlert(
                    alert_type="critical",
                    indicator="max_drawdown",
                    current_value=max_drawdown,
                    threshold=-self.config.max_drawdown_threshold,
                    message=f"最大回撤 {max_drawdown:.2%} 超过阈值 " f"{self.config.max_drawdown_threshold:.2%}",
                )
            )

        # 检查夏普比率
        sharpe = risk_metrics.get("sharpe_ratio", 0)
        if sharpe < self.config.sharpe_threshold:
            alerts.append(
                RiskAlert(
                    alert_type="warning",
                    indicator="sharpe_ratio",
                    current_value=sharpe,
                    threshold=self.config.sharpe_threshold,
                    message=f"夏普比率 {sharpe:.2f} 低于阈值 " f"{self.config.sharpe_threshold:.2f}",
                )
            )

        # 检查波动率
        volatility = risk_metrics.get("volatility", 0)
        if volatility > self.config.volatility_threshold:
            alerts.append(
                RiskAlert(
                    alert_type="warning",
                    indicator="volatility",
                    current_value=volatility,
                    threshold=self.config.volatility_threshold,
                    message=f"波动率 {volatility:.2%} 超过阈值 " f"{self.config.volatility_threshold:.2%}",
                )
            )

        # 检查VaR
        var_95 = risk_metrics.get("var_95", 0)
        if var_95 < self.config.var_threshold:
            alerts.append(
                RiskAlert(
                    alert_type="critical",
                    indicator="var_95",
                    current_value=var_95,
                    threshold=self.config.var_threshold,
                    message=f"VaR(95%) {var_95:.2%} 超过阈值 " f"{self.config.var_threshold:.2%}",
                )
            )

        # 检查持仓集中度
        max_position = risk_metrics.get("max_position_ratio")
        if max_position is not None and max_position > self.config.position_concentration_threshold:
            alerts.append(
                RiskAlert(
                    alert_type="warning",
                    indicator="position_concentration",
                    current_value=max_position,
                    threshold=self.config.position_concentration_threshold,
                    message=f"单股持仓比例 {max_position:.2%} 超过阈值 "
                    f"{self.config.position_concentration_threshold:.2%}",
                )
            )

        return alerts

    async def _broadcast_risk_update(self, risk_metrics: dict, alerts: list[RiskAlert]):
        """
        广播风险更新

        Args:
            risk_metrics: 风险指标
            alerts: 告警列表
        """
        message = {
            "type": "risk_update",
            "backtest_id": self.backtest_id,
            "metrics": risk_metrics,
            "alerts": [alert.to_dict() for alert in alerts],
            "timestamp": datetime.now().isoformat(),
        }

        await ws_manager.broadcast_to_room(message, self.backtest_id)

    async def _handle_alerts(self, alerts: list[RiskAlert]):
        """
        处理告警

        Args:
            alerts: 告警列表
        """
        # 记录告警
        self.alerts.extend(alerts)

        # 输出日志
        for alert in alerts:
            if alert.alert_type == "critical":
                StructuredTaskLogger(
                    logger,
                    "risk-monitor-service",
                    {"backtest_id": self.backtest_id, "alert_type": "critical"},
                ).error("alert", alert.message, indicator=alert.indicator, threshold=alert.threshold)
            else:
                StructuredTaskLogger(
                    logger,
                    "risk-monitor-service",
                    {"backtest_id": self.backtest_id, "alert_type": "warning"},
                ).warning("alert", alert.message, indicator=alert.indicator, threshold=alert.threshold)

        # 检查是否需要自动停止
        if self.config.enable_auto_stop:
            critical_alerts = [a for a in alerts if a.alert_type == "critical"]
            if critical_alerts:
                StructuredTaskLogger(
                    logger,
                    "risk-monitor-service",
                    {"backtest_id": self.backtest_id},
                ).error("auto_stop", "触发自动停止", critical_alert_count=len(critical_alerts))
                await self.stop_monitoring()

                # 发送停止通知
                await ws_manager.broadcast_to_room(
                    {
                        "type": "auto_stop",
                        "backtest_id": self.backtest_id,
                        "reason": "触发风控阈值",
                        "alerts": [alert.to_dict() for alert in critical_alerts],
                        "timestamp": datetime.now().isoformat(),
                    },
                    self.backtest_id,
                )

    def get_alerts_summary(self) -> dict:
        """
        获取告警摘要

        Returns:
            告警摘要
        """
        return {
            "total_alerts": len(self.alerts),
            "critical_alerts": len([a for a in self.alerts if a.alert_type == "critical"]),
            "warning_alerts": len([a for a in self.alerts if a.alert_type == "warning"]),
            # 最近5条
            "latest_alerts": [alert.to_dict() for alert in self.alerts[-5:]],
        }


# 全局监控器管理
_active_monitors: dict[str, RealTimeRiskMonitor] = {}


async def start_risk_monitoring(
    backtest_id: str,
    equity_curve: pd.Series,
    positions: pd.DataFrame | None = None,
    config: RiskControlConfig | None = None,
    update_interval: int = 5,
) -> RealTimeRiskMonitor:
    """
    启动风控监控

    Args:
        backtest_id: 回测ID
        equity_curve: 权益曲线
        positions: 持仓数据
        config: 风控配置
        update_interval: 更新间隔（秒）

    Returns:
        监控器实例
    """
    # 停止已存在的监控器
    if backtest_id in _active_monitors:
        await _active_monitors[backtest_id].stop_monitoring()

    # 创建新监控器
    monitor = RealTimeRiskMonitor(backtest_id, config)
    _active_monitors[backtest_id] = monitor

    # Celery-only 约束下不再在服务进程内创建后台协程，改为单次评估。
    await monitor.run_once(equity_curve, positions)

    return monitor


async def stop_risk_monitoring(backtest_id: str):
    """
    停止风控监控

    Args:
        backtest_id: 回测ID
    """
    if backtest_id in _active_monitors:
        await _active_monitors[backtest_id].stop_monitoring()
        del _active_monitors[backtest_id]


def get_active_monitor(backtest_id: str) -> RealTimeRiskMonitor | None:
    """
    获取活跃监控器

    Args:
        backtest_id: 回测ID

    Returns:
        监控器实例或None
    """
    return _active_monitors.get(backtest_id)


def get_all_active_monitors() -> dict[str, RealTimeRiskMonitor]:
    """
    获取所有活跃监控器

    Returns:
        监控器字典
    """
    return _active_monitors.copy()
