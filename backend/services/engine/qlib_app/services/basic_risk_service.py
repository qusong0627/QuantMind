"""
基础风险指标分析服务

基于Qlib risk_analysis()提供核心风险指标计算
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from backend.services.engine.qlib_app.schemas.analysis import (
    BasicRiskMetrics,
    BasicRiskResponse,
    HistogramData,
    TimeSeriesData,
)
from backend.services.engine.qlib_app.services.backtest_persistence import (
    BacktestPersistence,
)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)


class BasicRiskService:
    """基础风险指标分析服务"""

    def __init__(self):
        self._persistence = BacktestPersistence()
        StructuredTaskLogger(logger, "basic-risk-service").info("init", "BasicRiskService initialized")

    @staticmethod
    def _finite_or_zero(value: float) -> float:
        """将 NaN/Inf 归一化为 0，保证响应稳定可序列化。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        if np.isnan(v) or np.isinf(v):
            return 0.0
        return v

    async def analyze(self, backtest_id: str, user_id: str, tenant_id: str = "default") -> BasicRiskResponse:
        """
        执行基础风险分析

        Args:
            backtest_id: 回测ID
            user_id: 用户ID

        Returns:
            BasicRiskResponse: 风险分析结果
        """
        try:
            # 1. 获取回测数据
            returns = await self._get_backtest_returns(backtest_id, user_id, tenant_id)

            if returns is None or len(returns) == 0:
                raise ValueError(f"回测数据不存在或为空: backtest_id={backtest_id}")

            StructuredTaskLogger(
                logger,
                "basic-risk-service",
                {"backtest_id": backtest_id, "tenant_id": tenant_id},
            ).info("returns_loaded", "获取收益数据", count=len(returns))

            # 2. 计算Qlib原生指标
            qlib_metrics = self._calculate_qlib_metrics(returns)

            # 3. 补充计算指标
            supplementary_metrics = self._calculate_supplementary_metrics(returns, qlib_metrics)

            # 4. 统计指标
            statistics = self._calculate_statistics(returns)

            # 5. 生成时间序列数据
            series_data = self._generate_series_data(returns)

            # 6. 生成分布数据
            histogram = self._generate_histogram(returns)

            # 7. 组装响应
            metrics = BasicRiskMetrics(**qlib_metrics, **supplementary_metrics, **statistics)

            response = BasicRiskResponse(
                metrics=metrics,
                daily_returns=series_data["daily_returns"],
                cumulative_returns=series_data["cumulative_returns"],
                drawdown=series_data["drawdown"],
                returns_distribution=histogram,
                data_points=len(returns),
            )

            StructuredTaskLogger(
                logger,
                "basic-risk-service",
                {"backtest_id": backtest_id, "tenant_id": tenant_id},
            ).info("complete", "风险分析完成", sharpe=f"{metrics.sharpe_ratio:.2f}")
            return response

        except Exception as e:
            StructuredTaskLogger(
                logger,
                "basic-risk-service",
                {"backtest_id": backtest_id, "tenant_id": tenant_id},
            ).exception("failed", "风险分析失败", error=e)
            raise

    async def _get_backtest_returns(
        self, backtest_id: str, user_id: str, tenant_id: str = "default"
    ) -> pd.Series | None:
        """从数据库获取真实的回测收益数据"""
        # 优化：仅加载权益曲线和配置
        result = await self._persistence.get_result(
            backtest_id, tenant_id=tenant_id, include_fields=["equity_curve", "config", "total_return"]
        )
        if not result or not result.equity_curve:
            StructuredTaskLogger(
                logger,
                "basic-risk-service",
                {"backtest_id": backtest_id, "tenant_id": tenant_id},
            ).warning("equity_missing", "未找到回测结果或权益曲线为空")
            return None

        # 2. 将 equity_curve (资产价值) 转换为 Pandas Series
        # 假设格式: [{"date": "2024-01-01", "value": 1000000}, ...]
        df = pd.DataFrame(result.equity_curve)
        if df.empty:
            return None

        if "date" not in df.columns or "value" not in df.columns:
            StructuredTaskLogger(
                logger,
                "basic-risk-service",
                {"backtest_id": backtest_id, "tenant_id": tenant_id},
            ).warning("equity_invalid", "equity_curve 字段不完整", columns=list(df.columns))
            return None

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        df.set_index("date", inplace=True)

        # 3. 清洗价值序列并计算日收益率
        values = pd.to_numeric(df["value"], errors="coerce")
        values = values.replace([np.inf, -np.inf], np.nan).ffill().bfill()
        if values.isna().all():
            StructuredTaskLogger(
                logger,
                "basic-risk-service",
                {"backtest_id": backtest_id, "tenant_id": tenant_id},
            ).warning("equity_invalid", "equity_curve 全部为无效 value")
            return None

        # 首个 pct_change 为 NaN，不应被当作真实 0% 收益写入分布与统计。
        returns = values.pct_change()
        returns = returns.replace([np.inf, -np.inf], np.nan).dropna()

        StructuredTaskLogger(
            logger,
            "basic-risk-service",
            {"backtest_id": backtest_id, "tenant_id": tenant_id},
        ).info(
            "returns_prepared",
            "成功获取收益数据",
            count=len(returns),
            first=returns.iloc[0] if len(returns) > 0 else "N/A",
        )
        return returns

    def _calculate_qlib_metrics(self, returns: pd.Series) -> dict:
        """
        使用Qlib risk_analysis()计算指标

        优先尝试使用Qlib原生方法，失败则回退到手动计算
        """
        returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
        if returns.empty:
            return {
                "annualized_return": 0.0,
                "total_return": 0.0,
                "volatility": 0.0,
                "max_drawdown": 0.0,
            }

        # 改进：基于实际日期跨度计算年化因子
        # 使用日历天数计算，以解决数据密度（如334点/年）不一致的问题
        days_delta = (returns.index[-1] - returns.index[0]).days + 1
        years_delta = max(days_delta / 365.25, 1 / 365.25)

        total_ret = (1 + returns).prod() - 1
        annualized_return = (
            (1 + total_ret) ** (1 / years_delta) - 1
            if years_delta > 0 and (1 + total_ret) > 0
            else returns.mean() * (len(returns) / years_delta)
        )

        # 波动率也需要根据实际密度进行年化处理
        # 实际年频率 = 数据点数 / 年数
        annual_freq = len(returns) / years_delta
        volatility = returns.std(ddof=1) * np.sqrt(annual_freq) if len(returns) > 1 else 0.0
        equity = (1 + returns).cumprod()
        drawdown = (equity / equity.cummax()) - 1
        max_drawdown = drawdown.min() if not drawdown.empty else 0.0

        try:
            # 尝试导入Qlib risk_analysis
            from qlib.contrib.evaluate import risk_analysis

            # risk_analysis 正确输入为日收益序列
            qlib_result = risk_analysis(returns, freq="day")

            StructuredTaskLogger(logger, "basic-risk-service").info("qlib_risk", "使用Qlib原生risk_analysis计算风险指标")

            res_dict = qlib_result["risk"].to_dict()
            # 改进：不再盲目覆盖 annualized_return，因为 Qlib 的原生计算在数据密度异常时会有偏差
            # 我们保留上面计算的、基于实际日历天数的 annualized_return

            max_drawdown = res_dict.get("max_drawdown", max_drawdown)
            # qlib 返回 std 通常是日波动率，这里统一年化
            # 注意：如果使用 Qlib 原生结果，我们需要由于数据密度导致的偏差
            # 这里的 volatility 已经在上方基于 annual_freq 计算好了，
            # 只有当 Qlib 提供了更精确的 annualized_return 时才覆盖
            std = res_dict.get("std")
            if std is not None:
                # 依然使用我们计算的 annual_freq 来修正 Qlib 的日波动率
                volatility = float(std) * np.sqrt(annual_freq)

            return {
                "annualized_return": self._finite_or_zero(annualized_return),
                "total_return": self._finite_or_zero(total_ret),
                "volatility": self._finite_or_zero(volatility),
                "max_drawdown": self._finite_or_zero(max_drawdown),
            }

        except (ImportError, Exception) as e:
            StructuredTaskLogger(logger, "basic-risk-service").warning("qlib_risk_unavailable", "Qlib risk_analysis不可用，使用手动计算", error=e)

            return {
                "annualized_return": self._finite_or_zero(annualized_return),
                "total_return": self._finite_or_zero(total_ret),
                "volatility": self._finite_or_zero(volatility),
                "max_drawdown": self._finite_or_zero(max_drawdown),
            }

    def _calculate_supplementary_metrics(self, returns: pd.Series, qlib_metrics: dict) -> dict:
        """计算补充指标（高级风险指标）"""
        clean_returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
        if clean_returns.empty:
            clean_returns = pd.Series([0.0])

        # 夏普比率（基于实际计算的年化频率缩放）
        # 这里为了保持一致性，使用上面计算出的 annual_freq 进行年化
        days_delta = (clean_returns.index[-1] - clean_returns.index[0]).days + 1
        years_delta = max(days_delta / 365.25, 1 / 365.25)
        annual_freq = len(clean_returns) / years_delta

        std = clean_returns.std(ddof=1)
        if std and std > 0:
            sharpe_ratio = clean_returns.mean() / std * np.sqrt(annual_freq)
        else:
            sharpe_ratio = 0.0

        # Calmar比率
        calmar_ratio = (
            qlib_metrics["annualized_return"] / abs(qlib_metrics["max_drawdown"])
            if qlib_metrics["max_drawdown"] != 0
            else 0
        )

        # Sortino比率（下行风险调整收益）
        downside_returns = clean_returns[clean_returns < 0]
        if len(downside_returns) > 0:
            downside_deviation = downside_returns.std(ddof=1)
            if downside_deviation and downside_deviation > 0:
                sortino_ratio = clean_returns.mean() / downside_deviation * np.sqrt(annual_freq)
            else:
                sortino_ratio = 0.0
        else:
            sortino_ratio = 0.0

        # VaR (Value at Risk) - 95%置信度
        var_95 = float(clean_returns.quantile(0.05))

        # CVaR (Conditional VaR) - 95%置信度的平均损失
        cvar_95 = float(clean_returns[clean_returns <= var_95].mean())
        cvar_95 = self._finite_or_zero(cvar_95)

        # 偏度 (Skewness) - 衡量收益分布的对称性
        skewness = float(clean_returns.skew())

        # 峰度 (Kurtosis) - 衡量极端值出现的频率
        kurtosis = float(clean_returns.kurtosis())

        # Omega比率 - 收益/损失比
        gains = clean_returns[clean_returns > 0].sum()
        losses = abs(clean_returns[clean_returns < 0].sum())
        omega_ratio = gains / losses if losses > 0 else 0.0

        return {
            "sharpe_ratio": self._finite_or_zero(sharpe_ratio),
            "calmar_ratio": self._finite_or_zero(calmar_ratio),
            "sortino_ratio": self._finite_or_zero(sortino_ratio),
            "var_95": self._finite_or_zero(var_95),
            "cvar_95": cvar_95,
            "skewness": self._finite_or_zero(skewness),
            "kurtosis": self._finite_or_zero(kurtosis),
            "omega_ratio": self._finite_or_zero(omega_ratio),
        }

    def _calculate_statistics(self, returns: pd.Series) -> dict:
        """计算统计指标"""
        clean_returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
        total_days = len(clean_returns)
        if total_days == 0:
            return {
                "positive_days_pct": 0.0,
                "best_day_return": 0.0,
                "worst_day_return": 0.0,
            }

        positive_days = (clean_returns > 0).sum()

        return {
            "positive_days_pct": self._finite_or_zero(positive_days / total_days),
            "best_day_return": self._finite_or_zero(clean_returns.max()),
            "worst_day_return": self._finite_or_zero(clean_returns.min()),
        }

    def _generate_series_data(self, returns: pd.Series) -> dict:
        """生成时间序列数据"""
        clean_returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).fillna(0.0)

        # 累计收益（复利口径）
        equity = (1 + clean_returns).cumprod()
        cumulative = equity - 1

        # 回撤（相对峰值比例）
        running_max = equity.cummax()
        drawdown = (equity / running_max) - 1

        dates = clean_returns.index.strftime("%Y-%m-%d").tolist()

        return {
            "daily_returns": TimeSeriesData(dates=dates, values=clean_returns.tolist()),
            "cumulative_returns": TimeSeriesData(
                dates=dates, values=[self._finite_or_zero(v) for v in cumulative.tolist()]
            ),
            "drawdown": TimeSeriesData(dates=dates, values=[self._finite_or_zero(v) for v in drawdown.tolist()]),
        }

    def _generate_histogram(self, returns: pd.Series, bins: int = 50) -> HistogramData:
        """生成收益率分布直方图数据"""
        clean_returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
        if clean_returns.empty:
            return HistogramData(bins=[], counts=[])
        counts, bin_edges = np.histogram(clean_returns, bins=bins)

        return HistogramData(bins=bin_edges.tolist(), counts=counts.tolist())
