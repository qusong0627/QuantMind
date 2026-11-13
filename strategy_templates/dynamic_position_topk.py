# -*- coding: utf-8 -*-
"""动态仓位 TOPK 策略 (dynamic_position_topk)

[Native] 标准 Top-K 选股 + 调仓日全市场预测均分动态仓位：
    - 均分 >= 0  → 目标仓位 100%
    - 均分 < 0   → 目标仓位 50%

仓位执行采用「严格目标仓位」：
    目标权重之和为 1，`get_risk_degree()` 返回目标仓位系数；
    qlib 下单器按 risk_total_value = 目标仓位 × 总权益 生成订单，
    当前持仓高于目标会卖出、低于目标会买入，保证实际仓位贴近目标。

选股：
    保留 Top-K + n_drop 低换手轮动（每期最多替换 n_drop 只），
    并沿用涨跌停/停牌过滤与账户止损。

均分口径：
    当日全部有预测股票的 pred 均值（与「大盘分析 → 全市场均分时序」一致，
    取上一交易日预测截面，无前视）。
"""

import logging

import pandas as pd
from qlib.backtest.decision import Order

from backend.services.engine.qlib_app.utils.recording_strategy import (
    RedisWeightStrategy,
)

logger = logging.getLogger(__name__)


class DynamicPositionTopkStrategy(RedisWeightStrategy):
    """Top-K（低换手）+ 均分动态仓位的严格目标仓位策略。"""

    def __init__(self, *args, **kwargs):
        self.selection_topk = max(1, int(kwargs.pop("topk", 50)))
        self.n_drop = max(0, int(kwargs.pop("n_drop", 10)))
        self.score_threshold = float(kwargs.pop("score_threshold", 0.0))
        self.pos_up = float(kwargs.pop("pos_up", 1.0))
        self.pos_down = float(kwargs.pop("pos_down", 0.5))
        self._score_mean = None
        self._last_bucket = None
        super().__init__(*args, **kwargs)

    def get_risk_degree(self, *args, **kwargs):
        """目标仓位系数：均分 >= 0 满仓，< 0 五成仓。"""
        mean = self._score_mean
        if mean is not None:
            is_up = mean >= self.score_threshold
            target = self.pos_up if is_up else self.pos_down
            bucket = "up" if is_up else "down"
            if bucket != self._last_bucket:
                logger.info(
                    "dynamic_position_topk: mean=%.6f bucket=%s target_exposure=%.2f",
                    mean,
                    bucket,
                    target,
                )
                self._last_bucket = bucket
            return max(
                0.0, min(float(target), float(getattr(self, "max_leverage", 1.0)))
            )
        return super().get_risk_degree(*args, **kwargs)

    def _select_holdings(self, score: pd.Series, current) -> list[str]:
        """复刻 TopkDropout 的低换手选股：保留高分持仓，每期最多换 n_drop 只。"""
        score = pd.Series(score).dropna()
        if score.empty:
            return []

        current_list = list(current.get_stock_list()) if current is not None else []
        held = [s for s in current_list if s in score.index]
        held.sort(key=lambda s: float(score[s]), reverse=True)
        topk = self.selection_topk
        n_drop = min(self.n_drop, len(held), topk)

        candidates = score.drop(index=held, errors="ignore").sort_values(
            ascending=False
        ).index
        today = list(candidates[: max(0, n_drop + topk - len(held))])

        sold = set(held[-n_drop:]) if n_drop > 0 else set()
        keep = [s for s in held if s not in sold]
        buy = today[: max(0, topk - len(keep))]
        return (keep + buy)[:topk]

    def generate_target_weight_position(
        self, score, current=None, trade_exchange=None, *args, **kwargs
    ):
        if self.check_account_stop_loss():
            return {}
        if score is None:
            return {}
        if isinstance(score, pd.DataFrame):
            if score.shape[1] == 0:
                return {}
            score = score.iloc[:, 0]
        score = pd.Series(score).dropna()
        if score.empty:
            return {}

        # 均分基于全部有预测的股票，在任何可交易性过滤之前计算。
        self._score_mean = float(score.mean())

        exchange = trade_exchange or getattr(self, "trade_exchange", None)
        t_start = kwargs.get("trade_start_time") or kwargs.get("t_start")
        t_end = kwargs.get("trade_end_time") or kwargs.get("t_end") or t_start
        if exchange is not None and t_start is not None:
            tradable = []
            for sid in score.index:
                try:
                    if exchange.check_stock_suspended(
                        sid, t_start, t_end
                    ) or exchange.check_stock_limit(
                        sid, t_start, t_end, direction=Order.BUY
                    ):
                        continue
                except Exception:
                    pass
                tradable.append(sid)
            if tradable:
                score = score.loc[tradable]
            if score.empty:
                return {}

        holdings = self._select_holdings(score, current)
        if not holdings:
            return {}
        weight = 1.0 / len(holdings)
        return {sid: weight for sid in holdings}

    def generate_trade_decision(self, execute_result=None):
        is_rebalance = self._should_rebalance(self.rebalance_days)
        if not is_rebalance:
            self._score_mean = None
        return super().generate_trade_decision(execute_result)


STRATEGY_CONFIG = {
    "class": "DynamicPositionTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
        "rebalance_days": 3,
        "dynamic_exposure": True,
        "score_threshold": 0.0,
        "pos_up": 1.0,
        "pos_down": 0.5,
    },
}
