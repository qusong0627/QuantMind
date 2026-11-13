# -*- coding: utf-8 -*-
"""动态股票池 TOPK 策略 (dynamic_universe_topk)

标准 Top-K 选股 + 调仓日全市场预测均分动态切换股票池与仓位：
    - 均分 >= 0  → 全市场股票池，目标仓位 100%
    - 均分 < 0   → 沪深300股票池，目标仓位 50%

仓位执行采用严格目标仓位；选股保留 Top-K + n_drop 低换手轮动。
"""

import logging

import pandas as pd
from qlib.backtest.decision import Order

from backend.services.engine.qlib_app.utils.recording_strategy import (
    RedisWeightStrategy,
)

logger = logging.getLogger(__name__)


class DynamicUniverseTopkStrategy(RedisWeightStrategy):
    def __init__(self, *args, **kwargs):
        self.selection_topk = max(1, int(kwargs.pop("topk", 50)))
        self.n_drop = max(0, int(kwargs.pop("n_drop", 10)))
        self.score_threshold = float(kwargs.pop("score_threshold", 0.0))
        self.pos_up = float(kwargs.pop("pos_up", 1.0))
        self.pos_down = float(kwargs.pop("pos_down", 0.5))
        self.defensive_universe = str(
            kwargs.pop(
                "defensive_universe",
                kwargs.pop("defensive_market", "csi300"),
            )
        )
        self._score_mean = None
        self._last_bucket = None
        self._csi300_intervals = None
        self._csi300_cache: dict[pd.Timestamp, set[str]] = {}
        super().__init__(*args, **kwargs)

    def get_risk_degree(self, *args, **kwargs):
        mean = self._score_mean
        if mean is not None:
            is_up = mean >= self.score_threshold
            target = self.pos_up if is_up else self.pos_down
            bucket = "up" if is_up else "down"
            if bucket != self._last_bucket:
                logger.info(
                    "dynamic_universe_topk: mean=%.6f bucket=%s target_exposure=%.2f",
                    mean,
                    bucket,
                    target,
                )
                self._last_bucket = bucket
            return max(
                0.0, min(float(target), float(getattr(self, "max_leverage", 1.0)))
            )
        return super().get_risk_degree(*args, **kwargs)

    def _load_defensive_members(self):
        if self._csi300_intervals is not None:
            return
        try:
            from qlib.data import D

            cal = self.trade_calendar
            start = pd.Timestamp(cal.get_step_time(0)[0])
            end = pd.Timestamp(cal.get_step_time(cal.get_trade_len() - 1)[0])
            raw = D.list_instruments(
                D.instruments(self.defensive_universe),
                start_time=start,
                end_time=end,
                as_list=False,
            )
            self._csi300_intervals = {
                str(sym).upper(): [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in intervals]
                for sym, intervals in raw.items()
            }
        except Exception:  # noqa: BLE001
            logger.exception("dynamic_universe_topk: 加载防御股票池失败，回退全市场")
            self._csi300_intervals = {}

    def _defensive_members(self, trade_date) -> set[str]:
        self._load_defensive_members()
        if not self._csi300_intervals:
            return set()
        day = pd.Timestamp(trade_date).normalize()
        if day in self._csi300_cache:
            return self._csi300_cache[day]
        members = {
            sym
            for sym, intervals in self._csi300_intervals.items()
            if any(start <= day <= end for start, end in intervals)
        }
        self._csi300_cache[day] = members
        return members

    def _select_holdings(self, score: pd.Series, current) -> list[str]:
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

        # 均分始终基于全市场预测，不受股票池切换影响。
        self._score_mean = float(score.mean())

        t_start = kwargs.get("trade_start_time") or kwargs.get("t_start")
        t_end = kwargs.get("trade_end_time") or kwargs.get("t_end") or t_start

        if self._score_mean < self.score_threshold:
            members = self._defensive_members(t_start) if t_start is not None else set()
            if members:
                filtered = score[[str(s).upper() in members for s in score.index]]
                if not filtered.empty:
                    score = filtered
                else:
                    logger.warning(
                        "dynamic_universe_topk: 防御池过滤后为空，回退全市场"
                    )

        exchange = trade_exchange or getattr(self, "trade_exchange", None)
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
    "class": "DynamicUniverseTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
        "rebalance_days": 3,
        "dynamic_exposure": True,
        "score_threshold": 0.0,
        "pos_up": 1.0,
        "pos_down": 0.5,
        "defensive_universe": "csi300",
    },
}
