"""
回测引擎核心实现
"""

import logging
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..risk import RiskConfig, RiskManager, StopLossConfig, StopLossManager
from ..strategies.base import BaseStrategy
from .data_feed import DataFeed
from .order import Order, OrderSide, OrderStatus, OrderType
from .portfolio import Portfolio

# 添加路径以便导入共享模块
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/shared")
sys.path.insert(0, "/app/backend/shared")

# 导入共享模块
try:
    from prometheus_client import Counter, Histogram

    from backend.shared.observability.logging import (
        LoggerMixin,
        init_service_logging,
        log_performance,
    )
    from backend.shared.observability.tracing import trace_function
except ImportError:
    # 如果无法导入，使用标准日志
    init_service_logging = None
    LoggerMixin = None
    log_performance = None
    trace_function = None
    Counter = None
    Histogram = None


# 创建fallback装饰器
def fallback_performance_decorator(logger, operation_name):
    """Fallback performance decorator when observability modules are not available"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


# 使用fallback装饰器如果log_performance不可用
if log_performance is None:
    log_performance = fallback_performance_decorator


# 初始化日志
if init_service_logging:
    logger = init_service_logging("backtest-engine")
else:
    logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    统一回测引擎

    支持多种策略类型、手续费计算、风险管理和性能分析。
    """

    def __init__(
        self,
        initial_cash: float = 100000.0,
        commission_rate: float = 0.001,
        slippage_rate: float = 0.001,
        benchmark: str | None = None,
        enable_risk_management: bool = True,
        risk_config: RiskConfig | None = None,
        stop_loss_config: StopLossConfig | None = None,
    ):
        """
        初始化回测引擎

        Args:
            initial_cash: 初始资金
            commission_rate: 手续费率
            slippage_rate: 滑点率
            benchmark: 基准指数代码
            enable_risk_management: 是否启用风险管理
            risk_config: 风险管理配置
            stop_loss_config: 止损配置
        """
        self.initial_cash = initial_cash
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.benchmark = benchmark
        self.enable_risk_management = enable_risk_management
        self.risk_free_rate = 0.02  # 年化无风险利率

        # 核心组件
        self.portfolio = Portfolio(initial_cash)
        self.data_feed = DataFeed()

        # 风险管理组件
        if self.enable_risk_management:
            self.risk_manager = RiskManager(risk_config or RiskConfig())
            self.stop_loss_manager = StopLossManager(stop_loss_config or StopLossConfig())
        else:
            self.risk_manager = None
            self.stop_loss_manager = None

        # 回测状态
        self.current_date = None
        self.current_price = None
        self.orders: list[Order] = []
        self.trades: list[dict] = []
        self.daily_returns: list[float] = []
        self.equity_curve: list[dict] = []
        self.risk_alerts: list[dict] = []

        # 性能指标
        self.performance_metrics = {}
        self.run_started_at: float | None = None

        # Prometheus metrics（可选）
        if Counter and Histogram:
            try:
                if not hasattr(BacktestEngine, "_METRIC_DURATION"):
                    BacktestEngine._METRIC_DURATION = Histogram(
                        "backtest_duration_seconds",
                        "Duration of a backtest run in seconds",
                        ["strategy"],
                    )
                    BacktestEngine._METRIC_TRADES = Counter(
                        "backtest_trades_total",
                        "Total trades executed in a backtest",
                        ["strategy"],
                    )
                    BacktestEngine._METRIC_REJECTED = Counter(
                        "backtest_rejected_orders_total",
                        "Total rejected orders in a backtest",
                        ["strategy"],
                    )
            except Exception:
                # 静默失败，避免影响运行
                BacktestEngine._METRIC_DURATION = None
                BacktestEngine._METRIC_TRADES = None
                BacktestEngine._METRIC_REJECTED = None

        logger.info(
            "Backtest engine initialized",
            extra={
                "initial_cash": initial_cash,
                "commission_rate": commission_rate,
                "slippage_rate": slippage_rate,
                "benchmark": benchmark,
            },
        )

    @log_performance(logger, "set_data")
    def set_data(self, data: pd.DataFrame) -> None:
        """
        设置回测数据

        Args:
            data: 包含OHLCV数据的DataFrame
        """
        required_columns = ["open", "high", "low", "close", "volume"]
        missing_columns = [col for col in required_columns if col not in data.columns]

        if missing_columns:
            logger.error(
                "Missing required columns in data",
                extra={
                    "missing_columns": missing_columns,
                    "available_columns": list(data.columns),
                },
            )
            raise ValueError(f"数据缺少必要列: {missing_columns}")

        self.data_feed.set_data(data)

        start_date, end_date = self.data_feed.get_date_range()
        logger.info(
            "Backtest data set successfully",
            extra={
                "data_rows": len(data),
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "columns": list(data.columns),
            },
        )

    def add_strategy(self, strategy: BaseStrategy) -> None:
        """
        添加策略

        Args:
            strategy: 策略实例
        """
        strategy.set_backtest_engine(self)
        self.strategy = strategy  # 保存策略引用
        logger.info(
            "Strategy added to backtest engine",
            extra={
                "strategy_name": strategy.__class__.__name__,
                "strategy_parameters": strategy.parameters,
            },
        )

    @log_performance(logger, "run_backtest")
    def run(self) -> dict[str, Any]:
        """
        运行回测

        Returns:
            回测结果字典
        """
        logger.info(
            "Starting backtest run",
            extra={
                "initial_cash": self.initial_cash,
                "commission_rate": self.commission_rate,
                "slippage_rate": self.slippage_rate,
                "enable_risk_management": self.enable_risk_management,
            },
        )

        self.run_started_at = time.perf_counter()

        # 重置状态
        self._reset_state()

        # 获取数据
        market_data = self.data_feed.get_data()
        date_list = list(self.data_feed.get_dates())
        total_days = len(date_list)

        start_date, end_date = self.data_feed.get_date_range()
        logger.info(
            "Backtest data loaded",
            extra={
                "total_days": total_days,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "symbols": list(market_data.keys()),
            },
        )

        # 逐日回测
        for i, date in enumerate(date_list):
            daily_market = self.data_feed.get_market_data(date)
            if not daily_market:
                logger.warning("No market data for date", extra={"date": date})
                continue

            self.current_date = date
            # 为兼容单标的场景，当前价格使用首个标的收盘价
            first_symbol = next(iter(daily_market.keys()))
            self.current_price = daily_market[first_symbol]["close"]

            # 更新组合价值（多标的）
            close_prices = {symbol: row["close"] for symbol, row in daily_market.items()}
            self.portfolio.update_market_value(date, close_prices)

            # 记录每日收益
            daily_return = self.portfolio.get_daily_return()
            self.daily_returns.append(daily_return)

            # 记录权益曲线
            equity = {
                "date": date,
                "total_value": self.portfolio.get_total_value(),
                "cash": self.portfolio.cash,
                "positions_value": self.portfolio.get_positions_value(),
                "daily_return": daily_return,
            }
            self.equity_curve.append(equity)

            # 风险监控和止损检查
            if self.enable_risk_management:
                # 止损检查
                if self.stop_loss_manager:
                    price_updates = {
                        position.symbol: daily_market[position.symbol]["close"]
                        for position in self.stop_loss_manager.positions.values()
                        if position.symbol in daily_market
                    }

                    if price_updates:
                        triggered_stops = self.stop_loss_manager.update_prices(price_updates)

                        # 处理触发的止损
                        for position_id, reason in triggered_stops:
                            position = self.stop_loss_manager.positions[position_id]
                            # 创建卖出订单
                            sell_order = Order(
                                symbol=position.symbol,
                                side=OrderSide.SELL,
                                order_type=OrderType.MARKET,
                                quantity=position.quantity,
                            )
                            self.orders.append(sell_order)
                            self.stop_loss_manager.close_position(
                                position_id,
                                daily_market[position.symbol]["close"],
                                reason,
                            )

                # 组合风险检查
                if self.risk_manager and len(self.daily_returns) > 20:
                    portfolio_value = self.portfolio.get_total_value()
                    risk_assessment = self.risk_manager.check_portfolio_risk(
                        portfolio_value,
                        self.daily_returns[-252:],  # 使用近一年数据
                    )

                    # 记录风险警告
                    for alert in risk_assessment.get("alerts", []):
                        risk_alert = {
                            "date": date,
                            "type": alert.alert_type,
                            "severity": alert.severity,
                            "message": alert.message,
                            "recommendations": alert.recommendations,
                        }
                        self.risk_alerts.append(risk_alert)

            # 处理订单
            self._process_orders(daily_market)

            # 执行策略（逐标的，保留旧接口兼容单标数据结构）
            if hasattr(self, "strategy") and self.strategy:
                for symbol, fields in daily_market.items():
                    market_payload = {
                        "symbol": symbol,
                        "date": date,
                        "open": fields["open"],
                        "high": fields["high"],
                        "low": fields["low"],
                        "close": fields["close"],
                        "volume": fields["volume"],
                        "all_markets": daily_market,
                    }
                    try:
                        self.strategy.on_data(market_payload)
                    except Exception as e:
                        logger.error(
                            "Strategy execution error",
                            extra={
                                "date": date.isoformat(),
                                "error": str(e),
                                "strategy": self.strategy.__class__.__name__,
                                "symbol": symbol,
                            },
                            exc_info=True,
                        )

            # 进度日志
            if i % max(1, total_days // 10) == 0:
                progress = (i + 1) / total_days * 100
                logger.info(
                    "Backtest progress",
                    extra={
                        "progress": round(progress, 1),
                        "current_date": date.isoformat(),
                        "total_value": self.portfolio.get_total_value(),
                    },
                )

        # 计算性能指标
        self._calculate_performance_metrics()

        logger.info(
            "Backtest completed successfully",
            extra={
                "total_trades": len(self.trades),
                "final_value": self.portfolio.get_total_value(),
                "total_return": self.performance_metrics.get("total_return", 0),
                "rejected_orders": len([o for o in self.orders if o.status == OrderStatus.REJECTED]),
                "duration_ms": (
                    round((time.perf_counter() - self.run_started_at) * 1000, 2) if self.run_started_at else None
                ),
            },
        )

        # 记录 Prometheus 指标（若可用）
        strategy_label = (
            getattr(self, "strategy", None).__class__.__name__ if getattr(self, "strategy", None) else "unknown"
        )
        if hasattr(BacktestEngine, "_METRIC_DURATION") and BacktestEngine._METRIC_DURATION:
            try:
                duration_sec = time.perf_counter() - self.run_started_at if self.run_started_at else 0
                BacktestEngine._METRIC_DURATION.labels(strategy=strategy_label).observe(duration_sec)
                BacktestEngine._METRIC_TRADES.labels(strategy=strategy_label).inc(len(self.trades))
                BacktestEngine._METRIC_REJECTED.labels(strategy=strategy_label).inc(
                    len([o for o in self.orders if o.status == OrderStatus.REJECTED])
                )
            except Exception:
                # 避免指标失败影响主流程
                logger.debug("Prometheus metrics emission failed", exc_info=True)

        return self.get_results()

    def _reset_state(self) -> None:
        """重置回测状态"""
        self.portfolio.reset()
        self.orders.clear()
        self.trades.clear()
        self.daily_returns.clear()
        self.equity_curve.clear()
        self.risk_alerts.clear()
        self.performance_metrics.clear()

        # 重置风险管理组件
        if self.risk_manager:
            self.risk_manager.reset()
        if self.stop_loss_manager:
            self.stop_loss_manager.reset()

    def _process_orders(self, market_data: dict[str, pd.Series]) -> None:
        """处理待执行订单"""
        pending_orders = [order for order in self.orders if order.status == OrderStatus.PENDING]

        for order in pending_orders:
            symbol_data = market_data.get(order.symbol)
            if symbol_data is None:
                continue
            if self._can_execute_order(order, symbol_data):
                self._execute_order(order, symbol_data)

    def _can_execute_order(self, order: Order, market_data: pd.Series) -> bool:
        """判断订单是否可以执行"""
        # 简单实现：市价单总是可以执行
        if order.order_type == OrderType.MARKET:
            return True

        # 限价单需要检查价格条件
        if order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY and market_data["low"] <= order.price:
                return True
            elif order.side == OrderSide.SELL and market_data["high"] >= order.price:
                return True

        return False

    def _execute_order(self, order: Order, market_data: pd.Series) -> None:
        """执行订单（支持多头买卖和融券做空/平空）"""
        is_short_side = order.side in (OrderSide.SHORT_SELL, OrderSide.BUY_TO_COVER)

        # 计算执行价格（考虑滑点）
        if order.side in (OrderSide.BUY, OrderSide.BUY_TO_COVER):
            execution_price = market_data["close"] * (1 + self.slippage_rate)
        else:
            execution_price = market_data["close"] * (1 - self.slippage_rate)

        # 风险检查
        if self.enable_risk_management and self.risk_manager:
            portfolio_value = self.portfolio.get_total_value()
            position_value = order.quantity * execution_price

            is_valid, suggested_size, reason = self.risk_manager.check_position_size(
                order.symbol, position_value, portfolio_value
            )

            if not is_valid:
                logger.warning(f"订单被风险管理拒绝: {reason}")
                order.status = OrderStatus.REJECTED
                order.reject_reason = reason
                return

        # 计算手续费
        commission = order.quantity * execution_price * self.commission_rate

        # 交易前校验
        if order.side == OrderSide.BUY:
            required_cash = order.quantity * execution_price + commission
            if required_cash > self.portfolio.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "现金不足"
                logger.warning(
                    "订单被拒绝: 现金不足",
                    extra={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "required_cash": required_cash,
                        "available_cash": self.portfolio.cash,
                    },
                )
                return

        elif order.side == OrderSide.SELL:
            current_qty = self.portfolio.get_position(order.symbol)
            if current_qty < order.quantity:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "持仓不足"
                logger.warning(
                    "订单被拒绝: 持仓不足",
                    extra={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "required_qty": order.quantity,
                        "available_qty": current_qty,
                    },
                )
                return

        elif order.side == OrderSide.SHORT_SELL:
            # 融券开空只需确保有足够现金支付手续费
            if commission > self.portfolio.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "现金不足以支付融券手续费"
                logger.warning(
                    "融券订单被拒绝: 现金不足",
                    extra={"order_id": order.order_id, "symbol": order.symbol},
                )
                return

        elif order.side == OrderSide.BUY_TO_COVER:
            current_qty = self.portfolio.get_position(order.symbol)
            if current_qty >= 0:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "无空头持仓可平"
                logger.warning(
                    "平空订单被拒绝: 无空头持仓",
                    extra={"order_id": order.order_id, "symbol": order.symbol},
                )
                return
            if order.quantity > abs(current_qty):
                order.status = OrderStatus.REJECTED
                order.reject_reason = "平空数量超过持仓"
                return
            cover_cost = order.quantity * execution_price + commission
            if cover_cost > self.portfolio.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "现金不足以平空"
                return

        # 记录交易
        trade = {
            "date": self.current_date,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "price": execution_price,
            "commission": commission,
            "total_cost": order.quantity * execution_price + commission,
        }

        # 更新组合
        if order.side == OrderSide.BUY:
            self.portfolio.buy(
                symbol=order.symbol,
                quantity=order.quantity,
                price=execution_price,
                commission=commission,
            )
            if self.enable_risk_management and self.stop_loss_manager:
                self.stop_loss_manager.add_position(
                    symbol=order.symbol,
                    side="long",
                    entry_price=execution_price,
                    quantity=order.quantity,
                    stop_loss_strategy="trailing",
                )
            trade["realized_pnl"] = 0.0

        elif order.side == OrderSide.SELL:
            realized_pnl = self.portfolio.sell(
                symbol=order.symbol,
                quantity=order.quantity,
                price=execution_price,
                commission=commission,
            )
            if self.enable_risk_management and self.stop_loss_manager:
                positions_to_remove = [
                    pos_id
                    for pos_id, position in self.stop_loss_manager.positions.items()
                    if position.symbol == order.symbol and position.side == "long" and not position.is_closed
                ]
                for pos_id in positions_to_remove:
                    self.stop_loss_manager.close_position(pos_id, execution_price, "手动卖出")
            trade["realized_pnl"] = realized_pnl

        elif order.side == OrderSide.SHORT_SELL:
            self.portfolio.short_sell(
                symbol=order.symbol,
                quantity=order.quantity,
                price=execution_price,
                commission=commission,
            )
            if self.enable_risk_management and self.stop_loss_manager:
                self.stop_loss_manager.add_position(
                    symbol=order.symbol,
                    side="short",
                    entry_price=execution_price,
                    quantity=order.quantity,
                    stop_loss_strategy="trailing",
                )
            trade["realized_pnl"] = 0.0

        elif order.side == OrderSide.BUY_TO_COVER:
            realized_pnl = self.portfolio.buy_to_cover(
                symbol=order.symbol,
                quantity=order.quantity,
                price=execution_price,
                commission=commission,
            )
            if self.enable_risk_management and self.stop_loss_manager:
                positions_to_remove = [
                    pos_id
                    for pos_id, position in self.stop_loss_manager.positions.items()
                    if position.symbol == order.symbol and position.side == "short" and not position.is_closed
                ]
                for pos_id in positions_to_remove:
                    self.stop_loss_manager.close_position(pos_id, execution_price, "平空")
            trade["realized_pnl"] = realized_pnl

        # 更新订单状态
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.filled_price = execution_price
        order.filled_at = datetime.now()

        self.trades.append(trade)
        logger.debug(f"执行订单: {trade}")

    def _calculate_performance_metrics(self) -> None:
        """计算性能指标"""
        if not self.daily_returns:
            logger.warning("No daily returns available for performance calculation")
            return

        returns = np.array(self.daily_returns)

        # 基础指标
        total_return = (self.portfolio.get_total_value() - self.initial_cash) / self.initial_cash
        years = len(returns) / 252
        annual_return = (1 + total_return) ** (1 / max(years, 1 / 252)) - 1 if total_return > -1 else total_return

        # 风险指标
        volatility = np.std(returns, ddof=1) * np.sqrt(252)
        sharpe_ratio = (
            (annual_return - self.risk_free_rate) / volatility if volatility > 0 else 0
        )

        # 回撤指标
        equity_values = [eq["total_value"] for eq in self.equity_curve]
        peak = np.maximum.accumulate(equity_values)
        drawdown = (peak - equity_values) / peak
        max_drawdown = np.max(drawdown)

        # 交易统计
        total_trades = len(self.trades)
        winning_trades = len([t for t in self.trades if t.get("realized_pnl", 0) > 0])

        self.performance_metrics = {
            "total_return": total_return,
            "annual_return": annual_return,
            "volatility": volatility,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "win_rate": winning_trades / total_trades if total_trades > 0 else 0,
            "rejected_orders": len([o for o in self.orders if o.status == OrderStatus.REJECTED]),
        }

        logger.info(
            "Performance metrics calculated",
            extra={
                "total_return": round(total_return, 4),
                "annual_return": round(annual_return, 4),
                "sharpe_ratio": round(sharpe_ratio, 4),
                "max_drawdown": round(max_drawdown, 4),
                "total_trades": total_trades,
                "win_rate": round(self.performance_metrics["win_rate"], 4),
            },
        )

    def get_results(self) -> dict[str, Any]:
        """获取回测结果"""
        results = {
            "initial_cash": self.initial_cash,
            "final_value": self.portfolio.get_total_value(),
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "performance_metrics": self.performance_metrics,
            "positions": self.portfolio.get_positions(),
            "risk_management": (self.get_risk_summary() if self.enable_risk_management else None),
        }

        # 添加增强的风险指标
        if self.enable_risk_management and self.daily_returns:
            from ..risk.risk_metrics import RiskMetrics

            risk_metrics = RiskMetrics(self.commission_rate)

            try:
                # 计算详细风险指标
                detailed_metrics = risk_metrics.calculate_all_metrics(
                    returns=self.daily_returns, lookback_period=len(self.daily_returns)
                )

                results["detailed_risk_metrics"] = {
                    "volatility": detailed_metrics.volatility,
                    "downside_volatility": detailed_metrics.downside_volatility,
                    "var_95": detailed_metrics.var_95,
                    "var_99": detailed_metrics.var_99,
                    "cvar_95": detailed_metrics.cvar_95,
                    "cvar_99": detailed_metrics.cvar_99,
                    "max_drawdown": detailed_metrics.max_drawdown,
                    "max_drawdown_duration": detailed_metrics.max_drawdown_duration,
                    "sharpe_ratio": detailed_metrics.sharpe_ratio,
                    "sortino_ratio": detailed_metrics.sortino_ratio,
                    "calmar_ratio": detailed_metrics.calmar_ratio,
                    "omega_ratio": detailed_metrics.omega_ratio,
                    "gain_loss_ratio": detailed_metrics.gain_loss_ratio,
                }
            except Exception as e:
                logger.warning(f"计算详细风险指标失败: {e}")

        # 附加元数据，便于复现
        try:
            import subprocess

            git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=".").decode().strip()
        except Exception:
            git_commit = None

        start_date, end_date = self.data_feed.get_date_range()
        results["metadata"] = {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "symbols": self.data_feed.get_symbol_list(),
            "commission_rate": self.commission_rate,
            "slippage_rate": self.slippage_rate,
            "enable_risk_management": self.enable_risk_management,
            "git_commit": git_commit,
            "duration_ms": (
                round((time.perf_counter() - self.run_started_at) * 1000, 2) if self.run_started_at else None
            ),
        }

        return results

    def place_order(self, order: Order) -> None:
        """下单"""
        self.orders.append(order)
        logger.debug(f"下单: {order}")

    def get_current_price(self, symbol: str) -> float | None:
        """获取当前价格"""
        return self.current_price

    def get_current_date(self) -> datetime:
        """获取当前日期"""
        return self.current_date

    def get_risk_summary(self) -> dict[str, Any]:
        """获取风险管理摘要"""
        if not self.enable_risk_management:
            return {"risk_management_enabled": False}

        summary = {"risk_management_enabled": True}

        # 风险管理器摘要
        if self.risk_manager:
            summary["risk_manager"] = self.risk_manager.get_risk_summary()

        # 止损管理器摘要
        if self.stop_loss_manager:
            summary["stop_loss_manager"] = self.stop_loss_manager.get_risk_summary()

        # 风险警告摘要
        if self.risk_alerts:
            summary["risk_alerts"] = {
                "total_alerts": len(self.risk_alerts),
                "recent_alerts": self.risk_alerts[-10:],  # 最近10个警告
                "alert_types": list(set(alert["type"] for alert in self.risk_alerts)),
            }

        return summary
