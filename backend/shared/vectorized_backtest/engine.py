import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

@dataclass
class VectorizedBacktestConfig:
    initial_capital: float = 100000.0
    # 默认费率与主引擎 CnExchange 对齐：佣金万2.5(双向)，
    # sell_cost 为仅卖出侧费率(印花税万5+过户费万0.1)
    commission: float = 0.00025
    slippage: float = 0.0001
    topk: int = 50
    sell_cost: float = 0.00051  # stamp duty + transfer fee (sell-only cost)

@dataclass
class VectorizedBacktestResult:
    success: bool
    annual_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    total_return: float = 0.0
    win_rate: float = 0.0
    portfolio_dict: dict | None = None
    indicator_dict: dict | None = None
    error_message: str = ""

class VectorizedBacktestEngine:
    def __init__(self, config: VectorizedBacktestConfig):
        self.config = config
        self.logger = logger

    @staticmethod
    def _get_limit_threshold_vec(
        stock_ids: pd.Index,
        trade_date=None,
        st_set: set | frozenset | None = None,
    ) -> pd.Series:
        """Return per-stock limit thresholds based on stock code.

        严谨口径：委托 ``local_market_data.limit_pct``，支持 ST 主板 5%→10%
        切换、创业板/科创板 20%、北交所 30% 及 302/689/92 前缀。
        为保持向量化速度，阈值按“当日”统一计算；跨日历差异由调用方按日重算。
        """
        try:
            from datetime import date as _date

            from backend.services.simulation.services.local_market_data import limit_pct
            from backend.shared.stock_utils import StockCodeUtil

            d = trade_date
            if d is None:
                d = _date.today()
            elif hasattr(d, "date") and not isinstance(d, _date):
                try:
                    d = d.date()
                except Exception:
                    d = _date.today()
            # ST 集合：suffix 形态
            if st_set is None:
                try:
                    from backend.services.simulation.services.local_market_data import (
                        get_local_market_data,
                    )

                    st_set = get_local_market_data()._st_symbol_set()
                except Exception:
                    st_set = set()
            thresholds = pd.Series(0.10, index=stock_ids, dtype=float)
            for sid in stock_ids:
                # 统一转为 suffix 以查 ST
                try:
                    sym_sfx = StockCodeUtil.to_suffix(str(sid))
                except Exception:
                    sym_sfx = str(sid)
                is_st = sym_sfx in st_set if st_set else False
                try:
                    thresholds[sid] = float(limit_pct(str(sid), is_st=is_st, trade_date=d))
                except Exception:
                    # 回退前缀判定
                    code = str(sid).split(".")[0]
                    pure = code.upper()
                    for pfx in ("SH", "SZ", "BJ"):
                        if pure.startswith(pfx):
                            pure = pure[len(pfx):]
                            break
                    if pure.startswith(("688", "689")) or pure.startswith(("300", "301", "302")):
                        thresholds[sid] = 0.20
                    elif pure.startswith(("43", "83", "87", "88", "92")):
                        thresholds[sid] = 0.30
                    else:
                        thresholds[sid] = 0.10
            return thresholds
        except Exception:
            # 极端回退
            thresholds = pd.Series(0.10, index=stock_ids, dtype=float)
            for sid in stock_ids:
                code = str(sid).split(".")[0] if "." in str(sid) else str(sid)
                pure = code.upper()
                for pfx in ("SH", "SZ", "BJ"):
                    if pure.startswith(pfx):
                        pure = pure[len(pfx):]
                        break
                if pure.startswith("68") or pure.startswith("30"):
                    thresholds[sid] = 0.20
                elif pure.startswith("8") or pure.startswith("4"):
                    thresholds[sid] = 0.30
            return thresholds

    def run_backtest(
        self,
        signals: pd.DataFrame,
        prices: pd.DataFrame,
        changes: pd.DataFrame | None = None,
    ) -> VectorizedBacktestResult:
        """
        Pure pandas/numpy vectorized backtest.
        signals: MultiIndex (datetime, instrument) [score]
        prices: MultiIndex (datetime, instrument) [$close]
        changes: MultiIndex (datetime, instrument) [$change] — daily return for limit detection
        """
        try:
            self.logger.info("Starting true vectorized backtest")
            # 1. Unstack to wide format: (datetime x instrument)
            if isinstance(signals, pd.Series):
                signals = signals.to_frame("score")

            sig_wide = signals["score"].unstack(level="instrument")
            price_wide = prices["$close"].unstack(level="instrument").reindex_like(sig_wide).ffill()
            valid_dates = sig_wide.index.intersection(price_wide.dropna(how="all").index)
            sig_wide = sig_wide.loc[valid_dates]
            price_wide = price_wide.loc[valid_dates]
            if len(sig_wide) < 2:
                raise ValueError("vectorized backtest requires at least two aligned signal/price dates after lagging")

            # 2. Build tradability mask: filter limit-up (can't buy) and suspended stocks
            if changes is not None and not changes.empty:
                change_wide = changes["$change"].unstack(level="instrument").reindex_like(sig_wide)
            else:
                change_wide = pd.DataFrame(np.nan, index=sig_wide.index, columns=sig_wide.columns)

            # 严谨：按日按 ST 动态阈值（容差 0.5pp 对齐分位舍入），并补充跌停无法卖出
            try:
                from backend.services.simulation.services.local_market_data import (
                    get_local_market_data,
                )

                st_set = get_local_market_data()._st_symbol_set()
            except Exception:
                st_set = set()
            # 逐日计算阈值矩阵（小规模按日循环，约 2500*500 场景可向量化）
            thresh_mat = pd.DataFrame(
                np.nan, index=sig_wide.index, columns=sig_wide.columns, dtype=float
            )
            for dt in sig_wide.index:
                thresh_mat.loc[dt] = self._get_limit_threshold_vec(
                    sig_wide.columns, trade_date=dt, st_set=st_set
                )
            # 分位舍入容差：沪深 0.5%、北交所 1%（market_breadth.TOL）；change 与阈值差在容差内即视为封板
            # 为兼容历史 change 口径，这里按 change >= thresh - 0.005 判涨停，change <= -thresh + 0.005 判跌停
            limit_up = (change_wide + 0.005).ge(thresh_mat).fillna(False)
            limit_down = (change_wide - 0.005).le(-thresh_mat).fillna(False)
            # Suspended mask: True = stock has no close price (suspended)
            suspended = price_wide.isna()

            # Tradable mask：涨停不可买、跌停不可卖（向量化等权 TopK 仅做买入侧过滤，跌停作后续权重钳制）
            # 买入不可：涨停或停牌
            buy_tradable = ~limit_up & ~suspended
            # 保留原 tradable 语义供 TopK 排名使用（仅过滤涨停买入）
            tradable = buy_tradable

            # Apply tradability: zero out scores for untradable stocks
            sig_wide = sig_wide.where(buy_tradable, other=-np.inf)

            # 3. Daily returns
            # pct_change()[t] = P[t]/P[t-1] - 1 (T-1到T的日收益)
            # 价格已有 forward-fill，用 fill_method=None 避免隐式 pad 的弃用告警
            asset_returns = price_wide.pct_change(fill_method=None)

            # 4. Target Weights (TopK equal weight)
            # Rank scores cross-sectionally (untradable stocks ranked last)
            ranks = sig_wide.rank(axis=1, ascending=False, method="first")
            weights = (ranks <= self.config.topk).astype(float)

            # Zero out weights for untradable stocks (safety double-check)
            weights = weights.where(tradable, other=0.0)

            # Normalize weights
            weight_sums = weights.sum(axis=1)
            weights = weights.div(weight_sums.where(weight_sums > 0, 1), axis=0)

            # A股 T+1 settlement: T-1信号 → T日成交 → 收益从T+1开始
            # weights[t] 基于 T-1 信号, 应配对 T+1 的收益 (asset_returns[t+1])
            # 等价于将收益前移1天: portfolio[t] = weights[t] * returns[t+1]
            asset_returns = asset_returns.shift(-1)

            # 5. Calculate Portfolio Returns
            # weights[t] = T-1日信号权重
            # asset_returns[t] = T到T+1的收益 (已shift(-1))
            # portfolio[t] = T-1信号 × (T到T+1收益) = 正确的T+1 settlement
            portfolio_daily_returns = (weights * asset_returns).sum(axis=1).fillna(0)

            # 6. Transaction costs (buy-side + sell-side asymmetry)
            weight_diff = weights.diff().abs().sum(axis=1)
            # Buying costs: commission + slippage; Selling costs: commission + slippage + stamp duty
            avg_cost = self.config.commission + self.config.slippage + self.config.sell_cost * 0.5
            turnover_cost = weight_diff * avg_cost
            portfolio_daily_returns = portfolio_daily_returns - turnover_cost.fillna(0)

            # 5. Equity Curve
            equity_curve = (1 + portfolio_daily_returns).cumprod() * self.config.initial_capital

            # 6. Basic Metrics
            total_return = (equity_curve.iloc[-1] / self.config.initial_capital) - 1 if len(equity_curve) > 0 else 0

            years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25 if len(equity_curve) > 1 else 1
            annual_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1

            daily_std = portfolio_daily_returns.std(ddof=1)
            sharpe_ratio = (annual_return - 0.02) / (daily_std * np.sqrt(252)) if daily_std > 0 else 0.0

            rolling_max = equity_curve.cummax()
            drawdowns = (equity_curve - rolling_max) / rolling_max
            max_drawdown = drawdowns.min() if len(drawdowns) > 0 else 0.0

            win_rate = (portfolio_daily_returns > 0).mean()

            # Construct a portfolio_dict compatible with Qlib RiskAnalyzer
            # Qlib expects a report DataFrame with account/cost/return columns.
            # RiskAnalyzer 会基于该 report 重算 annual_return/sharpe/max_drawdown，
            # 因此无需在此重复计算（保留计算值仅为兼容调用方直接读取）。
            report_df = pd.DataFrame({
                "account": equity_curve.values,
                "cost": turnover_cost.values * self.config.initial_capital,
                "return": portfolio_daily_returns.values,
            }, index=equity_curve.index)

            portfolio_dict = {
                "report": report_df,
                "final_value": float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else float(self.config.initial_capital),
                "account": float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else float(self.config.initial_capital),
                "position_value": float((weights.iloc[-1] * price_wide.iloc[-1]).sum()) if len(weights) > 0 else 0.0,
            }

            return VectorizedBacktestResult(
                success=True,
                annual_return=float(annual_return),
                sharpe_ratio=float(sharpe_ratio),
                max_drawdown=float(max_drawdown),
                total_return=float(total_return),
                win_rate=float(win_rate),
                portfolio_dict=portfolio_dict,
                indicator_dict={"report": report_df},
            )

        except Exception as e:
            self.logger.error(f"Vectorized backtest failed: {e}", exc_info=True)
            return VectorizedBacktestResult(
                success=False,
                error_message=str(e)
            )
