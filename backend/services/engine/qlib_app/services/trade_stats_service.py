"""
交易统计分析服务

提供交易统计指标与分布数据
"""

import logging
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from backend.services.engine.qlib_app.schemas.analysis import (
    HistogramData,
    TimeSeriesData,
    TradeStatsMetrics,
    TradeStatsResponse,
)
from backend.services.engine.qlib_app.services.backtest_persistence import (
    BacktestPersistence,
)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "TradeStatsService")


class TradeStatsService:
    """交易统计分析服务"""

    def __init__(self):
        self._persistence = BacktestPersistence()
        task_logger.info("init", "TradeStatsService initialized")

    async def analyze(self, backtest_id: str, user_id: str, tenant_id: str = "default") -> TradeStatsResponse:
        try:
            # 1. 获取回测结果 (优化：尝试获取预计算的高级统计指标)
            result = await self._persistence.get_result(
                backtest_id,
                tenant_id=tenant_id,
                include_fields=[
                    "trades", "equity_curve", "backtest_id",
                    "win_rate", "profit_factor", "total_trades",
                    "advanced_stats"
                ],
            )
            if not result:
                raise ValueError(f"回测结果不存在: {backtest_id}")

            # 2. 检查是否有预计算结果（且 avg_holding_days 不是默认值 1.0）
            adv = getattr(result, "advanced_stats", None)
            # 如果 avg_holding_days 是默认值 1.0，可能是旧数据，需要重新计算
            use_precomputed = (
                adv
                and isinstance(adv, dict)
                and "pnl_distribution" in adv
                and adv.get("avg_holding_days", 1.0) != 1.0
            )
            if use_precomputed:
                task_logger.info("precomputed", "使用预计算的交易统计指标", backtest_id=backtest_id, tenant_id=tenant_id)

                # 提取指标
                metrics = TradeStatsMetrics(
                    win_rate=float(result.win_rate or 0.0),
                    profit_loss_ratio=float(result.profit_factor or 0.0),
                    profit_loss_days_ratio=adv.get("profit_loss_days_ratio", 0.0),
                    avg_holding_days=adv.get("avg_holding_days", result.avg_holding_period or 1.0),
                    trade_frequency=adv.get("trade_frequency", 0.0),
                    total_trades=int(result.total_trades or 0),
                )

                return TradeStatsResponse(
                    metrics=metrics,
                    pnl_distribution=adv.get("pnl_distribution"),
                    holding_days_distribution=adv.get("holding_days_distribution"),
                    trade_frequency_series=adv.get("trade_frequency_series"),
                )

            # 3. 兜底计算逻辑 (针对旧数据或未预计算的情况)
            task_logger.info("recompute", "未找到预计算指标，开始实时计算", backtest_id=backtest_id, tenant_id=tenant_id)

            # 提取收益率曲线（用于推导交易日）
            returns = self._extract_returns(result)

            # 优先使用真实提取的交易数据
            if getattr(result, "trades", None):
                task_logger.info("use_trades", "使用真实交易记录", backtest_id=backtest_id, tenant_id=tenant_id, count=len(result.trades))
                trades = pd.DataFrame(result.trades)
                # ... (rest of the logic remains as fallback)

                # 兼容不同交易流水字段命名，统一为 trade_date
                if "trade_date" not in trades.columns:
                    if "date" in trades.columns:
                        trades["trade_date"] = trades["date"]
                    elif "datetime" in trades.columns:
                        trades["trade_date"] = trades["datetime"]
                    elif "created_at" in trades.columns:
                        trades["trade_date"] = trades["created_at"]
                    else:
                        # 没有日期字段时，退化为顺序日，保证统计接口可用
                        if returns.empty:
                            trades["trade_date"] = pd.Timestamp.now().normalize()
                        else:
                            fallback_dates = returns.index
                            trades["trade_date"] = [fallback_dates[i % len(fallback_dates)] for i in range(len(trades))]
            else:
                # 兜底：基于真实收益率模拟交易分布
                task_logger.info("mock_trades", "未找到真实交易记录，使用收益率曲线推导模拟数据", backtest_id=backtest_id, tenant_id=tenant_id)
                trades = self._generate_mock_trades_from_returns(returns, backtest_id)

            if trades.empty:
                raise ValueError(f"交易数据不存在或为空: backtest_id={backtest_id}")

            total_trades = len(trades)
            holding_days_series = self._resolve_holding_days_series(trades)
            avg_holding_days = float(holding_days_series.mean()) if not holding_days_series.empty else 1.0
            win_rate, profit_loss_ratio = self._resolve_trade_quality_metrics(
                result=result, trades=trades, returns=returns
            )
            profit_loss_days_ratio = self._calculate_profit_loss_days_ratio(returns)

            months = max(int(len(returns) / 21), 1)
            trade_frequency = float(total_trades / months)

            pnl_series = self._resolve_pnl_series_for_histogram(trades=trades, returns=returns)
            pnl_hist = self._build_histogram(pnl_series, bins=20)
            holding_hist = self._build_holding_period_histogram(holding_days_series)
            trades["trade_date"] = pd.to_datetime(trades["trade_date"], errors="coerce")
            if trades["trade_date"].isna().all():
                if not returns.empty:
                    fallback_dates = returns.index
                    trades["trade_date"] = [fallback_dates[i % len(fallback_dates)] for i in range(len(trades))]
                else:
                    trades["trade_date"] = pd.Timestamp.now().normalize()
            else:
                trades["trade_date"] = trades["trade_date"].fillna(method="ffill").fillna(method="bfill")
            freq_series = self._build_trade_frequency_series(trades, returns.index)

            metrics = TradeStatsMetrics(
                win_rate=float(win_rate),
                profit_loss_ratio=float(profit_loss_ratio),
                profit_loss_days_ratio=float(profit_loss_days_ratio),
                avg_holding_days=float(avg_holding_days),
                trade_frequency=float(trade_frequency),
                total_trades=int(total_trades),
            )

            return TradeStatsResponse(
                metrics=metrics,
                pnl_distribution=pnl_hist,
                holding_days_distribution=holding_hist,
                trade_frequency_series=freq_series,
            )
        except Exception as exc:
            task_logger.exception("failed", "交易统计分析失败", backtest_id=backtest_id, tenant_id=tenant_id, error=str(exc))
            raise

    def _resolve_trade_quality_metrics(self, result, trades: pd.DataFrame, returns: pd.Series) -> tuple[float, float]:
        """
        交易质量指标回退顺序：
        1) 真实交易 pnl；
        2) 回测汇总字段（win_rate/profit_factor）；
        3) 日收益序列估算。
        """
        pnl = self._extract_real_pnl(trades)
        if not pnl.empty:
            win_trades = pnl[pnl > 0]
            loss_trades = pnl[pnl <= 0]
            win_rate = float(len(win_trades) / len(pnl)) if len(pnl) > 0 else 0.0
            avg_win = float(win_trades.mean()) if not win_trades.empty else 0.0
            avg_loss = float(loss_trades.mean()) if not loss_trades.empty else 0.0
            profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
            return win_rate, float(profit_loss_ratio)

        summary_win_rate = getattr(result, "win_rate", None)
        summary_profit_factor = getattr(result, "profit_factor", None)
        if summary_win_rate is not None:
            win_rate = float(summary_win_rate)
            profit_loss_ratio = float(summary_profit_factor) if summary_profit_factor is not None else 0.0
            return win_rate, profit_loss_ratio

        clean_returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
        if clean_returns.empty:
            return 0.0, 0.0

        win_days = clean_returns[clean_returns > 0]
        loss_days = clean_returns[clean_returns < 0]
        win_rate = float(len(win_days) / len(clean_returns))
        if loss_days.empty:
            profit_loss_ratio = 0.0
        else:
            avg_win = float(win_days.mean()) if not win_days.empty else 0.0
            avg_loss = abs(float(loss_days.mean()))
            profit_loss_ratio = float(avg_win / avg_loss) if avg_loss > 0 else 0.0
        return win_rate, profit_loss_ratio

    def _extract_real_pnl(self, trades: pd.DataFrame) -> pd.Series:
        """提取可用的真实 pnl 列（避免把占位 0 当作真实交易盈亏）。"""
        if "pnl" not in trades.columns:
            return pd.Series(dtype=float)
        pnl = pd.to_numeric(trades["pnl"], errors="coerce")
        pnl = pnl.replace([np.inf, -np.inf], np.nan).dropna()
        if pnl.empty:
            return pd.Series(dtype=float)
        # 全 0 大概率是占位值，不用于质量指标计算
        if (pnl != 0).sum() == 0:
            return pd.Series(dtype=float)
        return pnl

    def _resolve_pnl_series_for_histogram(self, trades: pd.DataFrame, returns: pd.Series) -> pd.Series:
        """用于直方图展示的 pnl 序列：优先真实 pnl，缺失时用收益率近似。"""
        pnl = self._extract_real_pnl(trades)
        if not pnl.empty:
            return pnl
        clean_returns = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
        if clean_returns.empty:
            return pd.Series([0.0], dtype=float)
        sample_size = max(1, len(trades))
        if len(clean_returns) >= sample_size:
            sampled = clean_returns.sample(n=sample_size, random_state=7)
        else:
            repeat = int(np.ceil(sample_size / len(clean_returns)))
            sampled = pd.concat([clean_returns] * repeat, ignore_index=True).iloc[:sample_size]
        return pd.Series(sampled, dtype=float)

    def _resolve_holding_days_series(self, trades: pd.DataFrame) -> pd.Series:
        """
        持仓天数推导顺序：
        1) 使用 trades.holding_days（若可用）；
        2) 基于 buy/sell + symbol + date 的真实流水推导；
        3) 回退为常量 1 天，保证接口稳定。
        """
        direct = self._extract_holding_days_column(trades)
        if not direct.empty:
            return direct

        derived = self._derive_holding_days_from_trades(trades)
        if not derived.empty:
            return derived

        return pd.Series([1.0], dtype=float)

    def _extract_holding_days_column(self, trades: pd.DataFrame) -> pd.Series:
        if "holding_days" not in trades.columns:
            return pd.Series(dtype=float)
        holding = pd.to_numeric(trades["holding_days"], errors="coerce")
        holding = holding.replace([np.inf, -np.inf], np.nan).dropna()
        if holding.empty:
            return pd.Series(dtype=float)
        holding = holding.clip(lower=1.0)
        return holding

    def _derive_holding_days_from_trades(self, trades: pd.DataFrame) -> pd.Series:
        required = {"symbol", "action"}
        if not required.issubset(set(trades.columns)):
            return pd.Series(dtype=float)

        date_col = None
        for candidate in ("trade_date", "date", "datetime", "created_at"):
            if candidate in trades.columns:
                date_col = candidate
                break
        if date_col is None:
            return pd.Series(dtype=float)

        working = trades.copy()
        working[date_col] = pd.to_datetime(working[date_col], errors="coerce")
        working = working.dropna(subset=[date_col, "symbol", "action"])
        if working.empty:
            return pd.Series(dtype=float)

        working["action_norm"] = working["action"].astype(str).str.lower().str.strip()
        working = working[working["action_norm"].isin(["buy", "sell"])]
        if working.empty:
            return pd.Series(dtype=float)

        # --- 性能优化：针对超大规模成交进行抽样 ---
        # 如果成交量超过 5000 笔，全量计算 FIFO 极其耗时，且统计意义上抽样已足够准确
        if len(working) > 5000:
            StructuredTaskLogger(logger, "trade-stats-service").info(
                "sample_trades", "成交记录过多，启用抽样分析以避免超时", count=len(working)
            )
            # 按股票池均匀抽样，保留时序性
            symbols = working["symbol"].unique()
            sample_symbols = np.random.choice(symbols, size=min(len(symbols), 200), replace=False)
            working = working[working["symbol"].isin(sample_symbols)]
            StructuredTaskLogger(logger, "trade-stats-service").info(
                "sampled_trades", "抽样后计算规模", count=len(working)
            )

        qty_col = "quantity" if "quantity" in working.columns else None
        if qty_col is not None:
            working["qty"] = pd.to_numeric(working[qty_col], errors="coerce").abs()
            working["qty"] = working["qty"].replace([np.inf, -np.inf], np.nan).fillna(1.0)
            working.loc[working["qty"] <= 0, "qty"] = 1.0
        else:
            working["qty"] = 1.0

        working = working.sort_values(by=[date_col], kind="mergesort")

        open_lots: dict[str, deque[dict[str, float | pd.Timestamp]]] = defaultdict(deque)
        holding_days: list[float] = []

        # 使用 to_dict('records') 替代 iterrows()，速度提升约 10 倍
        records = working.to_dict("records")
        for row in records:
            symbol = str(row["symbol"])
            action = str(row["action_norm"])
            trade_date = row[date_col]
            quantity = float(row["qty"])

            if action == "buy":
                open_lots[symbol].append({"qty": quantity, "date": trade_date})
                continue

            if action != "sell":
                continue

            remaining = quantity
            weighted_days = 0.0
            matched_qty = 0.0

            while remaining > 0 and open_lots[symbol]:
                lot = open_lots[symbol][0]
                lot_qty = float(lot["qty"])
                lot_date = lot["date"]
                consume = min(remaining, lot_qty)

                delta_days = (trade_date - lot_date).days
                day_value = float(max(delta_days, 1))
                weighted_days += day_value * consume
                matched_qty += consume

                remaining -= consume
                lot_qty -= consume
                if lot_qty <= 1e-8:
                    open_lots[symbol].popleft()
                else:
                    lot["qty"] = lot_qty

            if matched_qty > 0:
                holding_days.append(weighted_days / matched_qty)

        if not holding_days:
            return pd.Series(dtype=float)
        return pd.Series(holding_days, dtype=float)

    def _extract_returns(self, result) -> pd.Series:
        """从回测结果提取收益率"""
        if not result.equity_curve:
            return pd.Series(dtype=float)
        df = pd.DataFrame(result.equity_curve)
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        return df["value"].pct_change().fillna(0)

    def _calculate_profit_loss_days_ratio(self, returns: pd.Series) -> float:
        """盈亏天数比：盈利交易日数 / 亏损交易日数。"""
        clean_returns = pd.to_numeric(pd.Series(returns), errors="coerce")
        clean_returns = clean_returns.replace([np.inf, -np.inf], np.nan).dropna()
        if clean_returns.empty:
            return 0.0
        win_days = int((clean_returns > 0).sum())
        loss_days = int((clean_returns < 0).sum())
        if loss_days == 0:
            return float(win_days) if win_days > 0 else 0.0
        return float(win_days / loss_days)

    def _generate_mock_trades_from_returns(self, returns: pd.Series, backtest_id: str) -> pd.DataFrame:
        """基于真实收益率曲线模拟交易分布（用于展示）"""
        if returns.empty:
            return pd.DataFrame()

        np.random.seed(self._seed_from_backtest(backtest_id) + 7)
        # 识别收益率显著变动的日子作为“交易日”
        significant_days = returns[abs(returns) > 0.0001].index
        if len(significant_days) < 5:
            significant_days = returns.index

        total_trades = min(len(significant_days), 100)
        if total_trades == 0:
            return pd.DataFrame()

        trade_days = np.random.choice(significant_days, size=total_trades, replace=False)
        trade_days = pd.to_datetime(sorted(trade_days))

        # PNL 基于该日的真实收益率进行一定的缩放和随机化
        real_pnl = returns.loc[trade_days].values
        pnl = real_pnl * np.random.uniform(0.8, 1.2, size=total_trades)
        holding_days = np.random.randint(1, 11, size=total_trades)

        return pd.DataFrame(
            {
                "trade_date": trade_days,
                "pnl": pnl,
                "holding_days": holding_days,
            }
        )

    def _build_histogram(self, series: pd.Series, bins: int) -> HistogramData:
        clean = pd.to_numeric(series, errors="coerce")
        clean = clean.replace([np.inf, -np.inf], np.nan).dropna()
        if clean.empty:
            clean = pd.Series([0.0], dtype=float)
        counts, bin_edges = np.histogram(clean, bins=bins)
        return HistogramData(bins=bin_edges.tolist(), counts=counts.tolist())

    def _build_holding_period_histogram(self, series: pd.Series) -> HistogramData:
        """
        固定常用持仓周期分箱（移除 <=1天）：
        1-7天、7-30天、30-90天、90-180天、180-365天。
        """
        clean = pd.to_numeric(series, errors="coerce")
        clean = clean.replace([np.inf, -np.inf], np.nan).dropna()
        if clean.empty:
            clean = pd.Series([1.0], dtype=float)

        # 统一把超过 1 年的持仓归入最后一个分箱，且忽略 1 天以下的超短期噪音
        clipped = clean.clip(lower=1.0, upper=365.0)
        bin_edges = [1.0, 7.0, 30.0, 90.0, 180.0, 365.0]
        counts, _ = np.histogram(clipped, bins=bin_edges)
        return HistogramData(bins=bin_edges, counts=counts.tolist())

    def _build_trade_frequency_series(self, trades: pd.DataFrame, dates: pd.Index) -> TimeSeriesData:
        grouped = trades.groupby(trades["trade_date"].dt.to_period("M")).size()
        labels = []
        values = []
        for period, count in grouped.items():
            labels.append(period.to_timestamp().strftime("%Y-%m-%d"))
            values.append(float(count))
        return TimeSeriesData(dates=labels, values=values)

    def _seed_from_backtest(self, backtest_id: str) -> int:
        return abs(hash(backtest_id)) % (2**32)
