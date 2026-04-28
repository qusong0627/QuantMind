"""
绩效分析服务

提供多维度绩效评估功能
"""

import logging
from typing import Dict, List

import numpy as np
import pandas as pd

from backend.services.engine.qlib_app.schemas.analysis import (
    MonthlyReturn,
    PercentileData,
    TimeSeriesData,
)
from backend.services.engine.qlib_app.services.backtest_persistence import (
    BacktestPersistence,
)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "PerformanceService")


class PerformanceService:
    """绩效分析服务"""

    def __init__(self):
        self._persistence = BacktestPersistence()
        task_logger.info("initialized", "PerformanceService initialized")

    async def analyze(self, backtest_id: str, user_id: str, tenant_id: str = "default"):
        """分析回测绩效（占位方法）"""
        task_logger.info("analyze", "分析回测绩效", backtest_id=backtest_id, user_id=user_id, tenant_id=tenant_id)
        return {}

    async def _get_backtest_returns(self, backtest_id: str, user_id: str, tenant_id: str = "default") -> pd.Series:
        """从数据库获取真实的回测收益数据"""
        # 1. 从持久化层获取回测结果
        result = await self._persistence.get_result(backtest_id, tenant_id=tenant_id)
        if not result or not result.equity_curve:
            task_logger.warning("missing_equity_curve", "未找到回测结果或权益曲线为空", backtest_id=backtest_id, tenant_id=tenant_id)
            return pd.Series(dtype=float)

        # 2. 转换数据
        df = pd.DataFrame(result.equity_curve)
        if df.empty:
            return pd.Series(dtype=float)

        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)

        # 3. 计算日收益率
        values = df["value"]
        returns = values.pct_change().fillna(0)

        return returns

    def _calculate_monthly_returns(self, returns: pd.Series) -> list[MonthlyReturn]:
        """计算月度收益"""
        monthly = returns.resample("M")

        monthly_data = []
        for period, group in monthly:
            if len(group) > 0:
                monthly_data.append(
                    MonthlyReturn(
                        year=period.year,
                        month=period.month,
                        return_pct=float(group.sum()),
                        trading_days=len(group),
                    )
                )

        return monthly_data

    def _calculate_quarterly_returns(self, returns: pd.Series) -> dict[str, float]:
        """计算季度收益"""
        quarterly = returns.resample("Q")

        quarterly_data = {}
        for period, group in quarterly:
            if len(group) > 0:
                quarter_name = f"{period.year}Q{period.quarter}"
                quarterly_data[quarter_name] = float(group.sum())

        return quarterly_data

    def _calculate_rolling_metrics(self, returns: pd.Series, window: int) -> dict[str, TimeSeriesData]:
        """计算滚动指标"""
        # 滚动收益
        rolling_return = returns.rolling(window=window).sum()

        # 滚动波动率（年化）
        rolling_vol = returns.rolling(window=window).std() * np.sqrt(252)

        # 滚动夏普比率（年化，扣减无风险利率 2%）
        rolling_mean = returns.rolling(window=window).mean()
        rolling_std = returns.rolling(window=window).std()
        annual_return = rolling_mean * 252
        rolling_vol = rolling_std * np.sqrt(252)
        risk_free_rate = 0.02
        rolling_sharpe = (annual_return - risk_free_rate) / rolling_vol

        # 去除NaN值
        valid_indices = rolling_sharpe.notna()
        dates = returns.index[valid_indices].strftime("%Y-%m-%d").tolist()

        return {
            "return": TimeSeriesData(dates=dates, values=rolling_return[valid_indices].tolist()),
            "volatility": TimeSeriesData(dates=dates, values=rolling_vol[valid_indices].tolist()),
            "sharpe": TimeSeriesData(dates=dates, values=rolling_sharpe[valid_indices].tolist()),
        }

    def _calculate_percentiles(self, returns: pd.Series) -> PercentileData:
        """计算收益率分位数"""
        percentiles = returns.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])

        return PercentileData(
            p01=float(percentiles[0.01]),
            p05=float(percentiles[0.05]),
            p25=float(percentiles[0.25]),
            p50=float(percentiles[0.50]),
            p75=float(percentiles[0.75]),
            p95=float(percentiles[0.95]),
            p99=float(percentiles[0.99]),
        )
