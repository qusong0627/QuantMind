"""Qlib Risk Analyzer Service"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibBacktestResult,
    QlibPortfolioMetrics,
)
from backend.services.engine.qlib_app.utils.qlib_utils import D
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger
from backend.shared.redis_sentinel_client import get_redis_sentinel_client

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "RiskAnalyzer")


class RiskAnalyzer:
    """Risk Analyzer for Qlib Backtest Results"""

    @staticmethod
    def _clean_nan(value):
        """Clean NaN and Inf values for JSON serialization"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            if np.isnan(value) or np.isinf(value):
                return None
        return value

    @staticmethod
    def _to_finite_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            val = float(value)
            if np.isnan(val) or np.isinf(val):
                return None
            return val
        except Exception:
            return None

    @staticmethod
    def _normalize_display_quantity(symbol: str, quantity: float) -> float:
        """
        统一交易展示数量：
        1) 默认按整数股展示；
        2) A 股（SH/SZ/BJ）若仅偏离整手极小值（如 2701/2702），自动回吸到 100 股整手，
           避免复权因子日间微漂移导致的“买 2700 卖 2702”。
        """
        if not np.isfinite(quantity):
            return 0.0

        qty_int = int(round(float(quantity)))
        symbol_upper = str(symbol or "").upper()
        if symbol_upper.startswith(("SH", "SZ", "BJ")) and qty_int >= 100:
            lot_rounded = int(round(qty_int / 100.0) * 100)
            if abs(qty_int - lot_rounded) <= 2:
                return float(lot_rounded)
        return float(qty_int)

    @classmethod
    def _clamp_unreasonable_metric(cls, value: Any, *, abs_limit: float, metric_name: str) -> float | None:
        val = cls._to_finite_float(value)
        if val is None:
            return None
        if abs(val) > abs_limit:
            task_logger.warning(
                "metric_outlier_discarded",
                "Discarding unreasonable metric value",
                metric_name=metric_name,
                value=val,
                abs_limit=abs_limit,
            )
            return None
        return val

    @classmethod
    def _load_factor_map(cls, symbol_date_pairs: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
        factor_map: dict[tuple[str, str], float] = {}
        if not symbol_date_pairs:
            return factor_map
        try:
            symbols = sorted({s for s, _ in symbol_date_pairs if s})
            dates = sorted({d for _, d in symbol_date_pairs if d})
            if not symbols or not dates:
                return factor_map
            factor_df = D.features(
                symbols,
                ["$factor"],
                start_time=dates[0],
                end_time=dates[-1],
            )
            if factor_df is None or factor_df.empty:
                return factor_map
            for (instrument, dt), row_val in factor_df.iterrows():
                factor = cls._to_finite_float(row_val.get("$factor"))
                if factor is None or factor <= 0:
                    continue
                date_key = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
                factor_map[(str(instrument), date_key)] = factor
        except Exception as e:
            task_logger.warning("load_factor_map_failed", "Failed to load factor map from qlib", error=str(e))
        return factor_map

    @classmethod
    def _trade_dedupe_key(cls, trade: dict[str, Any]) -> tuple[Any, ...] | None:
        """构建交易去重键，兼容 exchange 与 strategy 两种写入格式。"""
        if not isinstance(trade, dict):
            return None
        date_key = str(trade.get("date", ""))[:10]
        symbol_key = str(trade.get("symbol", "")).upper()
        action_key = str(trade.get("action", "")).strip().lower()
        if not date_key or not symbol_key or not action_key:
            return None

        amount_val = cls._to_finite_float(
            trade.get("totalAmount", trade.get("total_amount", trade.get("amount")))
        )
        commission_val = cls._to_finite_float(trade.get("commission")) or 0.0

        if amount_val is not None:
            # amount + commission 在双写场景下稳定一致，优先作为主键。
            return (
                "amount",
                date_key,
                symbol_key,
                action_key,
                round(float(amount_val), 2),
                round(float(commission_val), 2),
            )

        price_val = cls._to_finite_float(trade.get("price", trade.get("adj_price")))
        quantity_val = cls._to_finite_float(trade.get("quantity", trade.get("adj_quantity")))
        if price_val is None and quantity_val is None:
            return None

        qty_key = cls._normalize_display_quantity(symbol_key, quantity_val) if quantity_val is not None else None
        return (
            "price_qty",
            date_key,
            symbol_key,
            action_key,
            round(float(price_val), 4) if price_val is not None else None,
            int(round(float(qty_key))) if qty_key is not None else None,
            round(float(commission_val), 2),
        )

    @classmethod
    def _trade_record_score(cls, trade: dict[str, Any]) -> int:
        """同一交易键冲突时，保留信息更完整的一条记录。"""
        if not isinstance(trade, dict):
            return 0
        score = 0
        for field in (
            "totalAmount",
            "amount",
            "adj_price",
            "adj_quantity",
            "factor",
            "cash_after",
            "position_value_after",
            "equity_after",
            "balance",
        ):
            if cls._to_finite_float(trade.get(field)) is not None:
                score += 1
        return score

    @classmethod
    def _deduplicate_trades(cls, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(trades, list) or not trades:
            return trades
        deduped: list[dict[str, Any]] = []
        keyed_index: dict[tuple[Any, ...], int] = {}

        for item in trades:
            if not isinstance(item, dict):
                continue
            key = cls._trade_dedupe_key(item)
            if key is None:
                deduped.append(item)
                continue
            existing_idx = keyed_index.get(key)
            if existing_idx is None:
                keyed_index[key] = len(deduped)
                deduped.append(item)
                continue

            prev_item = deduped[existing_idx]
            if cls._trade_record_score(item) > cls._trade_record_score(prev_item):
                deduped[existing_idx] = item

        if len(deduped) != len(trades):
            task_logger.info(
                "deduplicate_trades",
                "Deduplicated duplicated trades from mixed Redis writers",
                before=len(trades),
                after=len(deduped),
            )
        return deduped

    @classmethod
    def normalize_trades_for_display(cls, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(trades, list) or not trades:
            return trades

        trades = cls._deduplicate_trades(trades)
        need_factor: list[tuple[str, str]] = []
        for t in trades:
            symbol = str(t.get("symbol", ""))
            date_str = str(t.get("date", ""))[:10]
            factor_val = cls._to_finite_float(t.get("factor"))
            if symbol and date_str and factor_val is None:
                need_factor.append((symbol, date_str))

        factor_map = cls._load_factor_map(need_factor)
        normalized: list[dict[str, Any]] = []

        for t in trades:
            row = dict(t or {})
            symbol = str(row.get("symbol", ""))
            date_str = str(row.get("date", ""))[:10]

            factor_val = cls._to_finite_float(row.get("factor"))
            if factor_val is None:
                factor_val = factor_map.get((symbol, date_str))

            price = cls._to_finite_float(row.get("price"))
            quantity = cls._to_finite_float(row.get("quantity"))
            adj_price = cls._to_finite_float(row.get("adj_price"))
            adj_quantity = cls._to_finite_float(row.get("adj_quantity"))

            if adj_price is None:
                adj_price = price
            if adj_quantity is None:
                adj_quantity = quantity

            display_price = price if price is not None else 0.0
            display_quantity = quantity if quantity is not None else 0.0
            quantity_is_integer_like = (
                quantity is not None and np.isfinite(quantity) and abs(quantity - round(quantity)) <= 1e-6
            )
            should_restore = (
                factor_val is not None
                and factor_val > 0
                and adj_price is not None
                and adj_quantity is not None
                and (quantity is None or not quantity_is_integer_like)
            )
            if should_restore:
                display_price = adj_price / factor_val
                display_quantity = adj_quantity * factor_val

            total_amount = cls._to_finite_float(row.get("totalAmount", row.get("total_amount")))
            if total_amount is None:
                total_amount = display_price * display_quantity

            row["price"] = float(display_price)
            row["quantity"] = cls._normalize_display_quantity(symbol, display_quantity)
            row["totalAmount"] = float(total_amount)
            row["adj_price"] = float(adj_price) if adj_price is not None else None
            row["adj_quantity"] = float(adj_quantity) if adj_quantity is not None else None
            row["factor"] = float(factor_val) if factor_val is not None else None
            normalized.append(row)

        return normalized

    @staticmethod
    def _compute_benchmark_return(benchmark: str, start_date: str, end_date: str) -> float | None:
        try:
            df = D.features(
                [benchmark],
                ["$close"],
                start_time=start_date,
                end_time=end_date,
            )
            if df is None or df.empty:
                return None
            df = df.droplevel(level="instrument")
            series = df["$close"].dropna()
            if series.empty:
                return None
            first = float(series.iloc[0])
            last = float(series.iloc[-1])
            if first == 0:
                return None
            benchmark_return = (last / first) - 1
            return RiskAnalyzer._clamp_unreasonable_metric(
                benchmark_return,
                abs_limit=10.0,
                metric_name="benchmark_return",
            )
        except Exception as exc:
            task_logger.warning("compute_benchmark_return_failed", "Failed to compute benchmark return", error=str(exc))
            return None

    @classmethod
    def _compute_risk_metrics(
        cls,
        daily_returns: pd.Series,
        benchmark: str,
        start_date: str,
        end_date: str,
        annual_return: float | None,
        risk_free_rate: float = 0.02,
    ) -> dict[str, float | None]:
        try:
            bm_df = D.features([benchmark], ["$close"], start_time=start_date, end_time=end_date)
            if bm_df is None or bm_df.empty:
                return {"alpha": None, "beta": None, "information_ratio": None}

            bm_df = bm_df.droplevel(level="instrument")
            bm_prices = bm_df["$close"].dropna()
            bm_daily_returns = bm_prices.pct_change().dropna()

            aligned = pd.concat([daily_returns, bm_daily_returns], axis=1, join="inner")
            aligned.columns = ["portfolio", "benchmark"]
            if len(aligned) < 5:
                return {"alpha": None, "beta": None, "information_ratio": None}

            port_r = aligned["portfolio"]
            bm_r = aligned["benchmark"]

            # Beta
            bm_var = float(bm_r.var())
            beta = float(port_r.cov(bm_r) / bm_var) if bm_var > 0 else None

            # Alpha (CAPM): alpha = Rp - [Rf + beta * (Rm - Rf)]
            periods = len(aligned)
            port_compounded = float((1.0 + port_r).prod())
            bm_compounded = float((1.0 + bm_r).prod())
            port_annual = (
                float(port_compounded ** (252 / periods) - 1)
                if periods > 0 and port_compounded > 0
                else annual_return
            )
            bm_annual = float(bm_compounded ** (252 / periods) - 1) if periods > 0 and bm_compounded > 0 else None
            alpha = (
                (port_annual - (risk_free_rate + beta * (bm_annual - risk_free_rate)))
                if port_annual is not None and bm_annual is not None and beta is not None
                else None
            )

            # Information Ratio
            excess = port_r - bm_r
            excess_std = float(excess.std())
            ir = float(excess.mean() / excess_std * np.sqrt(252)) if excess_std > 0 else None

            beta = cls._clamp_unreasonable_metric(beta, abs_limit=10.0, metric_name="beta")
            alpha = cls._clamp_unreasonable_metric(alpha, abs_limit=10.0, metric_name="alpha")
            ir = cls._clamp_unreasonable_metric(ir, abs_limit=20.0, metric_name="information_ratio")

            return {
                "alpha": cls._clean_nan(alpha),
                "beta": cls._clean_nan(beta),
                "information_ratio": cls._clean_nan(ir),
            }
        except Exception as exc:
            task_logger.warning("compute_risk_metrics_failed", "Risk metrics calculation failed", error=str(exc))
            return {"alpha": None, "beta": None, "information_ratio": None}

    @staticmethod
    def _extract_report_from_portfolio(portfolio_dict: dict[str, Any]) -> pd.DataFrame | None:
        if not portfolio_dict:
            return None
        report = None
        if "1day" in portfolio_dict:
            freq_data = portfolio_dict["1day"]
            if isinstance(freq_data, tuple) and len(freq_data) >= 1:
                report = freq_data[0]
            elif isinstance(freq_data, dict):
                report = freq_data.get("report")
        if report is None and isinstance(portfolio_dict, dict):
            report = portfolio_dict.get("report")
        if isinstance(report, pd.DataFrame):
            return report
        return None

    @staticmethod
    def _calculate_trade_stats(
        trades: list[dict[str, Any]], daily_returns: pd.Series | None = None
    ) -> dict[str, Any]:
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
            }
        wins = []
        losses = []
        for t in trades:
            pnl = t.get("pnl")
            if pnl is None:
                pnl = t.get("profit")
            if pnl is not None:
                if pnl > 0:
                    wins.append(pnl)
                elif pnl < 0:
                    losses.append(abs(pnl))

        total_trades = len(trades)
        win_count = len(wins)
        win_rate = win_count / total_trades if total_trades > 0 else 0.0
        sum_wins = sum(wins)
        sum_losses = sum(losses)
        profit_factor = sum_wins / sum_losses if sum_losses > 0 else (float("inf") if sum_wins > 0 else 0.0)
        avg_win = sum_wins / len(wins) if wins else 0.0
        avg_loss = sum_losses / len(losses) if losses else 0.0

        # 部分交易流水只有成交信息无 pnl 时，回退到日收益口径，避免指标长期为 0
        if win_count == 0 and len(losses) == 0 and daily_returns is not None:
            try:
                clean_returns = daily_returns.dropna()
                if len(clean_returns) > 0:
                    win_days = clean_returns[clean_returns > 0]
                    loss_days = clean_returns[clean_returns < 0]
                    win_rate = float(len(win_days) / len(clean_returns))
                    if len(loss_days) > 0:
                        avg_day_win = float(win_days.mean()) if len(win_days) > 0 else 0.0
                        avg_day_loss = abs(float(loss_days.mean()))
                        profit_factor = (
                            float(avg_day_win / avg_day_loss)
                            if avg_day_loss > 0
                            else (float("inf") if avg_day_win > 0 else 0.0)
                        )
                    else:
                        profit_factor = float("inf") if len(win_days) > 0 else 0.0
            except Exception as e:
                task_logger.warning(
                    "fallback_trade_stats_failed",
                    "Failed to fallback trade stats from daily returns",
                    error=str(e),
                )

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    @classmethod
    def _calculate_advanced_trade_stats(
        cls, trades: list[dict[str, Any]], daily_returns: pd.Series | None = None
    ) -> dict[str, Any]:
        """计算高级交易统计指标（分布、频率等），这些将被持久化以避免前端重复计算瓶颈。"""
        if not trades:
            return {}

        try:
            df_trades = pd.DataFrame(trades)
            # 确保日期列存在
            date_col = next((c for c in ["date", "datetime", "trade_date"] if c in df_trades.columns), None)
            if date_col:
                df_trades["_dt"] = pd.to_datetime(df_trades[date_col])

            # 1. PNL 分布
            if "pnl" in df_trades.columns:
                raw_pnl = df_trades["pnl"]
            elif "profit" in df_trades.columns:
                raw_pnl = df_trades["profit"]
            else:
                raw_pnl = pd.Series(0.0, index=df_trades.index)
            pnl_series = pd.to_numeric(raw_pnl, errors="coerce").fillna(0.0)
            if (pnl_series == 0).all() and daily_returns is not None:
                # 兜底：使用收益率抽样模拟分布
                pnl_series = daily_returns.sample(n=min(len(daily_returns), len(trades)), replace=True)

            pnl_counts, pnl_bins = np.histogram(pnl_series.dropna(), bins=20)
            pnl_distribution = {"bins": pnl_bins.tolist(), "counts": pnl_counts.tolist()}

            # 2. 持仓天数分布
            raw_holding_days = (
                df_trades["holding_days"]
                if "holding_days" in df_trades.columns
                else pd.Series(dtype=float, index=df_trades.index)
            )
            holding_days = pd.to_numeric(raw_holding_days, errors="coerce").dropna()
            if holding_days.empty:
                holding_days = pd.Series([1.0]) # 默认 1 天

            # 固定分箱：1-7天, 7-30天, 30-90天, 90-180天, 180-365天
            holding_bins = [1.0, 7.0, 30.0, 90.0, 180.0, 365.0]
            holding_counts, _ = np.histogram(holding_days.clip(1, 365), bins=holding_bins)
            holding_distribution = {"bins": holding_bins, "counts": holding_counts.tolist()}

            # 3. 交易频率 (按月)
            freq_series = {"dates": [], "values": []}
            if date_col:
                grouped = df_trades.groupby(df_trades["_dt"].dt.to_period("M")).size()
                for period, count in grouped.items():
                    freq_series["dates"].append(period.to_timestamp().strftime("%Y-%m-%d"))
                    freq_series["values"].append(float(count))

            return {
                "pnl_distribution": pnl_distribution,
                "holding_days_distribution": holding_distribution,
                "trade_frequency_series": freq_series
            }
        except Exception as e:
            task_logger.warning("calculate_advanced_trade_stats_failed", "Failed to calculate advanced trade stats", error=str(e))
            return {}

    @classmethod
    def _build_equity_curve(
        cls,
        portfolio_dict: dict[str, Any],
        request: QlibBacktestRequest,
        total_return: float,
    ) -> list[dict[str, Any]]:
        report = cls._extract_report_from_portfolio(portfolio_dict)
        if isinstance(report, pd.DataFrame) and not report.empty:
            candidates = [
                "account",
                "total_value",
                "account_value",
                "portfolio_value",
                "asset",
                "value",
            ]
            value_col = next((col for col in candidates if col in report.columns), None)
            if value_col:
                return [
                    {
                        "date": idx.strftime("%Y-%m-%d"),
                        "value": (float(val) if not pd.isna(val) and not np.isnan(val) else 0.0),
                    }
                    for idx, val in report[value_col].items()
                ]
        return cls._build_minimal_equity_curve(request, total_return)

    @staticmethod
    def _build_minimal_equity_curve(request: QlibBacktestRequest, total_return: float) -> list[dict[str, Any]]:
        initial = float(request.initial_capital)
        final_value = initial * (1 + float(total_return)) if total_return else initial
        return [
            {"date": request.start_date, "value": initial},
            {"date": request.end_date, "value": final_value},
        ]

    @staticmethod
    def _build_drawdown_curve(equity_curve: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not equity_curve:
            return []
        values = [point["value"] for point in equity_curve]
        dates = [point["date"] for point in equity_curve]
        running_max = []
        current_max = values[0]
        for value in values:
            current_max = max(current_max, value)
            running_max.append(current_max)
        drawdowns = [(value - peak) / peak if peak else 0 for value, peak in zip(values, running_max)]
        return [
            {
                "date": date,
                "drawdown": float(dd) if not (np.isnan(dd) or np.isinf(dd)) else 0.0,
            }
            for date, dd in zip(dates, drawdowns)
        ]

    @classmethod
    def _build_trades_list(
        cls, portfolio_dict: dict[str, Any], backtest_id: str | None = None
    ) -> list[dict[str, Any]]:
        trades = []
        if backtest_id:
            try:
                r = get_redis_sentinel_client()
                key = f"qlib:backtest:trades:{backtest_id}"
                raw_trades = r.lrange(key, 0, -1)
                if raw_trades:
                    task_logger.info("read_trades_from_redis", "Read trades from Redis", count=len(raw_trades))
                    for item in raw_trades:
                        if isinstance(item, bytes):
                            item = item.decode("utf-8")
                        trades.append(json.loads(item))
                    return cls.normalize_trades_for_display(trades)
            except Exception as e:
                task_logger.warning("read_trades_from_redis_failed", "Failed to read trades from Redis", error=str(e))

        sys_analyser = portfolio_dict.get("sys_analyser")
        if isinstance(sys_analyser, dict):
            trades_df = sys_analyser.get("trades")
            if isinstance(trades_df, pd.DataFrame) and not trades_df.empty:
                return cls._parse_trades_df(trades_df)

        if "report" in portfolio_dict:
            report = cls._extract_report_from_portfolio(portfolio_dict)
            if report is not None and not report.empty:
                return cls._infer_trades_from_report(report)

        if "trades" in portfolio_dict:
            trades_obj = portfolio_dict["trades"]
            if isinstance(trades_obj, pd.DataFrame):
                return cls._parse_trades_df(trades_obj)
            if isinstance(trades_obj, list):
                return trades_obj

        return trades

    @staticmethod
    def _infer_trades_from_report(report: pd.DataFrame) -> list[dict[str, Any]]:
        trades = []
        if "turnover" not in report.columns and "total_turnover" not in report.columns:
            return []
        try:
            for date, row in report.iterrows():
                if not isinstance(date, (datetime, pd.Timestamp)):
                    continue
                turnover_val = 0.0
                if "total_turnover" in row:
                    turnover_val = float(row["total_turnover"])
                elif "turnover" in row and "account" in row:
                    turnover_val = float(row["account"]) * float(row["turnover"])
                if turnover_val > 1.0:
                    trades.append(
                        {
                            "date": date.strftime("%Y-%m-%d"),
                            "symbol": "Portfolio Rebalance",
                            "action": "buy/sell",
                            "price": 1.0,
                            "quantity": int(turnover_val),
                            "amount": turnover_val,
                            "commission": float(row.get("cost", 0.0)),
                        }
                    )
        except Exception as e:
            task_logger.warning("infer_trades_from_report_failed", "Failed to infer trades from report", error=str(e))
        return trades

    @classmethod
    def _parse_trades_df(cls, df: pd.DataFrame) -> list[dict[str, Any]]:
        trades = []
        try:
            raw_rows: list[dict[str, Any]] = []
            need_factor: list[tuple[str, str]] = []
            # 按日期顺序维护每只股票的累计持仓，用于区分四种操作
            cumulative_positions: dict[str, float] = {}

            for index, row in df.iterrows():
                date, symbol = index if isinstance(index, tuple) else (index, "UNKNOWN")
                date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
                symbol_str = str(symbol)
                amount_raw = cls._to_finite_float(row.get("amount", 0)) or 0.0
                adj_price = cls._to_finite_float(row.get("price", 0)) or 0.0
                factor_val = cls._to_finite_float(row.get("factor"))
                if factor_val is None:
                    need_factor.append((symbol_str, date_str))

                adj_quantity_raw = cls._to_finite_float(row.get("deal_amount"))
                if adj_quantity_raw is None:
                    adj_quantity_raw = abs(amount_raw)
                adj_quantity = abs(adj_quantity_raw)

                commission = cls._to_finite_float(row.get("commission"))
                if commission is None:
                    commission = cls._to_finite_float(row.get("cost"))

                # 根据交易前持仓推断操作类型
                prev_qty = cumulative_positions.get(symbol_str, 0.0)
                if amount_raw > 0:
                    action = "buy_to_cover" if prev_qty < 0 else "buy_to_open"
                else:
                    action = "sell_to_close" if prev_qty > 0 else "sell_to_open"
                cumulative_positions[symbol_str] = prev_qty + amount_raw

                raw_rows.append(
                    {
                        "date": date_str,
                        "symbol": symbol_str,
                        "action": action,
                        "adj_price": float(adj_price),
                        "adj_quantity": float(adj_quantity),
                        "factor": float(factor_val) if factor_val is not None else None,
                        "trade_val": cls._to_finite_float(row.get("trade_val")),
                        "commission": float(commission or 0.0),
                    }
                )

            factor_map = cls._load_factor_map(need_factor)

            for row in raw_rows:
                factor_val = row["factor"]
                if factor_val is None:
                    factor_val = factor_map.get((row["symbol"], row["date"]))

                adj_price = float(row["adj_price"])
                adj_quantity = float(row["adj_quantity"])
                display_price = adj_price
                display_quantity = adj_quantity
                if factor_val is not None and factor_val > 0:
                    display_price = adj_price / factor_val
                    display_quantity = adj_quantity * factor_val

                total_amount = row["trade_val"]
                if total_amount is None:
                    total_amount = display_price * display_quantity

                trades.append(
                    {
                        "date": row["date"],
                        "symbol": row["symbol"],
                        "action": row["action"],
                        "price": float(display_price),
                        "quantity": int(cls._normalize_display_quantity(row["symbol"], display_quantity)),
                        "totalAmount": float(total_amount),
                        "commission": float(row["commission"]),
                        "adj_price": float(adj_price),
                        "adj_quantity": float(adj_quantity),
                        "factor": float(factor_val) if factor_val is not None else None,
                    }
                )
        except Exception as e:
            task_logger.warning("parse_trades_dataframe_failed", "Failed to parse trades dataframe", error=str(e))
        return trades

    @classmethod
    def _build_positions_list(cls, portfolio_dict: dict[str, Any]) -> list[dict[str, Any]]:
        positions = []
        try:
            if "1day" in portfolio_dict:
                freq_data = portfolio_dict["1day"]
                if isinstance(freq_data, tuple) and len(freq_data) > 1:
                    pos_history = freq_data[1]
                    if isinstance(pos_history, dict):
                        for date, pos_obj in pos_history.items():
                            date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)
                            if hasattr(pos_obj, "get_stock_list"):
                                # 计算组合总净值用于权重归一化
                                total_val = 0.0
                                try:
                                    total_val = float(pos_obj.calculate_value()) if hasattr(pos_obj, "calculate_value") else 0.0
                                except Exception:
                                    pass

                                for symbol in pos_obj.get_stock_list():
                                    amount = pos_obj.get_stock_amount(symbol)
                                    if abs(amount) < 1e-8:
                                        continue
                                    # 计算持仓权重（多头为正，空头为负）
                                    weight = 0.0
                                    if total_val > 0:
                                        try:
                                            price = pos_obj.get_stock_price(symbol)
                                            weight = float(amount * price) / total_val
                                        except Exception:
                                            pass
                                    positions.append(
                                        {
                                            "date": date_str,
                                            "symbol": symbol,
                                            "weight": weight,
                                            "amount": float(amount),
                                            "side": "short" if amount < 0 else "long",
                                        }
                                    )
                            elif isinstance(pos_obj, dict):
                                for symbol, weight in pos_obj.items():
                                    positions.append(
                                        {
                                            "date": date_str,
                                            "symbol": symbol,
                                            "weight": float(weight),
                                            "side": "short" if float(weight) < 0 else "long",
                                        }
                                    )

            if not positions and "positions" in portfolio_dict:
                raw = portfolio_dict["positions"]
                if isinstance(raw, list):
                    positions = raw
                elif isinstance(raw, dict):
                    for symbol, weight in raw.items():
                        w = float(weight)
                        positions.append({
                            "symbol": symbol,
                            "weight": w,
                            "side": "short" if w < 0 else "long",
                        })
        except Exception as e:
            task_logger.warning("extract_positions_failed", "Failed to extract positions", error=str(e))
        return positions

    @classmethod
    def _build_config_payload(
        cls, request: QlibBacktestRequest, signal_meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = {
            "symbol": request.universe,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "initial_capital": request.initial_capital,
            "user_id": request.user_id,
            "tenant_id": request.tenant_id,
            "history_source": request.history_source,
            "qlib_strategy_type": request.strategy_type,
            "qlib_strategy_params": request.strategy_params.model_dump(),
            "benchmark_symbol": request.benchmark,
            "deal_price": request.deal_price,
            "seed": request.seed,
            "dynamic_position": request.dynamic_position,
            "style": request.style,
            "market_state_symbol": request.market_state_symbol,
            "market_state_window": request.market_state_window,
            "strategy_total_position": request.strategy_total_position,
        }

        # 补全模型标识：优先从 signal_meta 提取推理引擎实际解析出的模型，否则回退到请求中的 model_id
        if signal_meta:
            effective_model_id = signal_meta.get("effective_model_id")
            if effective_model_id:
                payload["model_id"] = effective_model_id
            
            payload["signal_meta"] = signal_meta
            
        if not payload.get("model_id") and hasattr(request, "model_id") and request.model_id:
            payload["model_id"] = request.model_id

        if hasattr(request, "strategy_content") and request.strategy_content:
            payload["strategy_content"] = request.strategy_content
        if hasattr(request, "strategy_id") and request.strategy_id:
            payload["strategy_id"] = request.strategy_id
        return payload

    @classmethod
    async def analyze(
        cls,
        portfolio_dict: dict[str, Any],
        request: QlibBacktestRequest,
        backtest_id: str,
        created_at: datetime,
        execution_time: float,
        signal_data: Any = None,
        signal_meta: dict[str, Any] | None = None,
        on_progress: Any | None = None,
    ) -> QlibBacktestResult:
        """Core analysis logic extracted from QlibBacktestService"""

        async def report_progress(val: float, msg: str | None = None):
            if on_progress:
                try:
                    await on_progress(val, msg)
                except Exception:
                    pass

        await report_progress(0.85, "正在提取回测原始报告...")
        report = cls._extract_report_from_portfolio(portfolio_dict)

        annual_return = None
        total_return = None
        sharpe_ratio = None
        max_drawdown = None
        volatility = None
        daily_returns = None

        if report is not None and hasattr(report, "__len__") and len(report) > 0:
            try:
                await report_progress(0.87, "正在计算核心绩效指标...")
                if "return" in report.columns:
                    daily_returns = report["return"]
                    value_col = next(
                        (col for col in ["account", "value", "total_value"] if col in report.columns),
                        None,
                    )
                    if value_col:
                        end_val = float(report[value_col].iloc[-1])
                        start_val = float(request.initial_capital)
                        total_return = (end_val - start_val) / start_val
                    else:
                        total_return = (1 + daily_returns).prod() - 1

                    total_return = cls._clean_nan(float(total_return))

                    if total_return is not None:
                        trading_days = len(daily_returns)
                        if trading_days > 0:
                            annual_return = cls._clean_nan((1 + total_return) ** (252 / trading_days) - 1)

                    # Build equity curve for net-of-fees metrics
                    if value_col:
                        equity = report[value_col]
                    else:
                        equity = (1 + daily_returns).cumprod() * request.initial_capital

                    # Volatility & Sharpe: use net daily returns from equity curve
                    if len(equity) > 1:
                        net_daily_returns = equity.pct_change().dropna()
                        if len(net_daily_returns) > 1:
                            vol_value = net_daily_returns.std(ddof=1) * np.sqrt(252)
                            volatility = cls._clean_nan(float(vol_value))

                    if volatility and volatility > 0:
                        risk_free_rate = float(getattr(request, "risk_free_rate", 0.02))
                        sharpe_ratio = cls._clean_nan((annual_return - risk_free_rate) / volatility)

                    cummax = equity.cummax()
                    drawdown = (equity - cummax) / cummax
                    max_drawdown = cls._clean_nan(float(drawdown.min()))
                else:
                    task_logger.warning(
                        "report_missing_return_column",
                        "Report missing 'return' column",
                        available_columns=list(report.columns),
                    )
            except Exception as e:
                task_logger.exception("metric_extraction_failed", "Metric extraction failed", error=str(e))
        else:
            task_logger.warning("empty_or_invalid_report", "Empty or invalid report")

        await report_progress(0.89, "正在对比基准指数收益...")
        benchmark_return = cls._compute_benchmark_return(request.benchmark, request.start_date, request.end_date)
        benchmark_return = cls._clean_nan(benchmark_return)

        risk_metrics = {"alpha": None, "beta": None, "information_ratio": None}
        if daily_returns is not None and annual_return is not None:
            risk_metrics = cls._compute_risk_metrics(
                daily_returns=daily_returns,
                benchmark=request.benchmark,
                start_date=request.start_date,
                end_date=request.end_date,
                annual_return=annual_return,
                risk_free_rate=float(getattr(request, "risk_free_rate", 0.02)),
            )

        portfolio_metrics = None
        final_total_assets = request.initial_capital
        if portfolio_dict:
            final_total_assets = portfolio_dict.get(
                "final_value", portfolio_dict.get("account", request.initial_capital)
            )
            portfolio_metrics = QlibPortfolioMetrics(
                final_value=portfolio_dict.get("final_value"),
                account=portfolio_dict.get("account"),
                position_value=portfolio_dict.get("position_value"),
            )

        await report_progress(0.91, "正在生成净值与回撤曲线...")
        equity_curve = cls._build_equity_curve(
            portfolio_dict, request, float(total_return) if total_return is not None else 0.0
        )
        drawdown_curve = cls._build_drawdown_curve(equity_curve)

        await report_progress(0.93, "正在深度解析交易明细与高级统计指标...")
        trades = cls._build_trades_list(portfolio_dict, backtest_id=backtest_id)
        trades = cls.normalize_trades_for_display(trades)
        positions = cls._build_positions_list(portfolio_dict)
        trade_stats = cls._calculate_trade_stats(trades, daily_returns=daily_returns)
        advanced_stats = cls._calculate_advanced_trade_stats(trades, daily_returns=daily_returns)

        factor_metrics = None
        stratified_returns = None
        style_attribution = None
        rebalance_suggestions = None

        try:
            from backend.services.engine.qlib_app.services.factor_analysis_service import (
                FactorAnalysisService,
            )
            from backend.services.engine.qlib_app.services.order_generation_service import (
                OrderGenerationService,
            )
            from backend.services.engine.qlib_app.services.style_attribution_service import (
                StyleAttributionService,
            )

            # 1. Factor Analysis
            pred_df = None
            if isinstance(signal_data, pd.DataFrame):
                pred_df = signal_data

            if pred_df is not None and not pred_df.empty:
                await report_progress(0.95, "正在执行因子有效性分析...")
                instruments = pred_df.index.get_level_values("instrument").unique().tolist()
                label_df = D.features(
                    instruments,
                    ["Ref($close, -1)/$close - 1"],
                    start_time=request.start_date,
                    end_time=request.end_date,
                )
                if label_df is not None and not label_df.empty:
                    factor_metrics = FactorAnalysisService.calculate_ic_metrics(pred_df, label_df)
                    stratified_returns = FactorAnalysisService.calculate_stratified_returns(pred_df, label_df)

            # 2. Style Attribution
            if positions:
                await report_progress(0.97, "正在分析持仓风格归因...")
                style_attribution = await StyleAttributionService.analyze_portfolio_exposure(
                    positions=positions,
                    benchmark=request.benchmark,
                    start_date=request.start_date,
                    end_date=request.end_date,
                )

            # 3. Rebalance Instructions
            target_pos = []
            if positions:
                all_dates = sorted(list(set([p["date"] for p in positions if "date" in p])))
                if all_dates:
                    last_pos_date = all_dates[-1]
                    target_pos = [p for p in positions if p.get("date") == last_pos_date]

            if not target_pos and pred_df is not None:
                try:
                    last_pred_date = pred_df.index.get_level_values("datetime").max()
                    latest_scores = pred_df.xs(last_pred_date, level="datetime")
                    top_stocks = latest_scores.nlargest(request.strategy_params.topk, latest_scores.columns[0])
                    target_pos = [{"symbol": s, "weight": 1.0 / len(top_stocks)} for s in top_stocks.index]
                    task_logger.info(
                        "generated_fallback_target_positions",
                        "Generated fallback target positions",
                        count=len(target_pos),
                    )
                except Exception:
                    pass

            if target_pos:
                rebalance_suggestions = OrderGenerationService.generate_rebalance_instructions(
                    target_positions=target_pos,
                    total_assets=float(final_total_assets),
                )

        except Exception as fe:
            task_logger.exception("advanced_analysis_skipped", "Advanced analysis skipped", error=str(fe))

        await report_progress(0.99, "正在汇总回测报告结果...")
        return QlibBacktestResult(
            backtest_id=backtest_id,
            tenant_id=request.tenant_id,
            status="completed",
            created_at=created_at,
            completed_at=datetime.now(),
            config=cls._build_config_payload(request, signal_meta=signal_meta),
            annual_return=float(annual_return) if annual_return is not None else 0.0,
            sharpe_ratio=float(sharpe_ratio) if sharpe_ratio is not None else 0.0,
            max_drawdown=float(max_drawdown) if max_drawdown is not None else 0.0,
            alpha=float(risk_metrics["alpha"]) if risk_metrics["alpha"] is not None else None,
            total_return=float(total_return) if total_return is not None else None,
            volatility=float(volatility) if volatility is not None else None,
            information_ratio=(
                float(risk_metrics["information_ratio"]) if risk_metrics["information_ratio"] is not None else None
            ),
            beta=float(risk_metrics["beta"]) if risk_metrics["beta"] is not None else None,
            benchmark_return=(float(benchmark_return) if benchmark_return is not None else None),
            benchmark_symbol=request.benchmark,
            portfolio_metrics=portfolio_metrics,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            trades=trades,
            positions=positions,
            total_trades=trade_stats["total_trades"],
            win_rate=trade_stats["win_rate"],
            profit_factor=trade_stats["profit_factor"],
            avg_win=trade_stats["avg_win"],
            avg_loss=trade_stats["avg_loss"],
            factor_metrics=factor_metrics,
            stratified_returns=stratified_returns,
            style_attribution=style_attribution,
            rebalance_suggestions=rebalance_suggestions,
            advanced_stats=advanced_stats,
            execution_time=execution_time,
        )
