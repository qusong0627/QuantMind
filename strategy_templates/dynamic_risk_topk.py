# -*- coding: utf-8 -*-
"""动态风控 TOPK 策略 (dynamic_risk_topk)

在「动态股票池 TOPK」基础上加入快速崩盘过滤器：
    - 均分 >= 0：全市场池 + 100% 仓位
    - 均分 < 0：沪深300池 + 50% 仓位
    - 沪深300 近 10 日跌幅 <= -6% 或近 5 日 <= -4%：次日降到 30% 仓位，
      维持 crash_hold_days 个交易日（快速崩盘过滤，独立于均分）
    - 仓位变化每日检查；选股仍按 rebalance_days 低换手轮动
      （风险档位变化当天会强制做一次目标仓位调整）
"""

import logging

import pandas as pd
from qlib.backtest.decision import Order

from backend.services.engine.qlib_app.utils.recording_strategy import (
    RedisWeightStrategy,
)

logger = logging.getLogger(__name__)


class DynamicRiskTopkStrategy(RedisWeightStrategy):
    def __init__(self, *args, **kwargs):
        self.selection_topk = max(1, int(kwargs.pop("topk", 50)))
        self.n_drop = max(0, int(kwargs.pop("n_drop", 10)))
        self.score_threshold = float(kwargs.pop("score_threshold", 0.0))
        self.pos_up = float(kwargs.pop("pos_up", 1.0))
        self.pos_down = float(kwargs.pop("pos_down", 0.5))
        self.crash_exposure = float(kwargs.pop("crash_exposure", 0.3))
        self.crash_ret10 = float(kwargs.pop("crash_ret10", -0.06))
        self.crash_ret5 = float(kwargs.pop("crash_ret5", -0.04))
        self.crash_hold_days = max(1, int(kwargs.pop("crash_hold_days", 3)))
        self.defensive_universe = str(kwargs.pop("defensive_universe", "csi300"))
        self._score_mean = None
        self._target_exposure = 1.0
        self._use_defensive = False
        self._crash_hold = 0
        self._last_applied_exposure = None
        self._last_applied_defensive = False
        self._force_rebalance = False
        self._intervals = None
        self._member_cache: dict[pd.Timestamp, set[str]] = {}
        self._last_bucket = None
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # 每日风险状态
    # ------------------------------------------------------------------
    def _prev_date(self, trade_step: int):
        try:
            prev, _ = self.trade_calendar.get_step_time(trade_step, shift=1)
            return pd.Timestamp(prev)
        except Exception:
            return None

    def _score_mean_for(self, trade_step: int):
        try:
            pred_start, pred_end = self.trade_calendar.get_step_time(
                trade_step, shift=1
            )
            score = self.signal.get_signal(
                start_time=pred_start, end_time=pred_end
            )
            if isinstance(score, pd.DataFrame):
                score = score.iloc[:, 0]
            if score is None:
                return None
            values = pd.Series(score).dropna()
            return float(values.mean()) if not values.empty else None
        except Exception:  # noqa: BLE001
            logger.exception("dynamic_risk_topk: 计算均分失败")
            return None

    def _index_crash(self, ref_date) -> bool:
        if ref_date is None:
            return False
        try:
            start = pd.Timestamp(ref_date) - pd.Timedelta(days=40)
            prices = self._close_matrix(["SH000300"], start, ref_date)
            if prices is None or prices.empty:
                return False
            col = "SH000300" if "SH000300" in prices.columns else prices.columns[0]
            close = prices[col].dropna()
            if len(close) < 11:
                return False
            ret5 = float(close.iloc[-1] / close.iloc[-6] - 1.0)
            ret10 = float(close.iloc[-1] / close.iloc[-11] - 1.0)
            return ret10 <= self.crash_ret10 or ret5 <= self.crash_ret5
        except Exception:  # noqa: BLE001
            logger.exception("dynamic_risk_topk: 计算指数崩盘信号失败")
            return False

    def _update_regime(self, trade_step: int) -> None:
        ref_date = self._prev_date(trade_step)
        mean = self._score_mean_for(trade_step)
        self._score_mean = mean

        triggered = self._index_crash(ref_date)
        if triggered:
            self._crash_hold = self.crash_hold_days
        crash_active = self._crash_hold > 0
        if self._crash_hold > 0:
            self._crash_hold -= 1

        if mean is None:
            target = (
                self._last_applied_exposure
                if self._last_applied_exposure is not None
                else self.pos_up
            )
            defensive = self._last_applied_defensive
        else:
            defensive = crash_active or mean < self.score_threshold
            if crash_active:
                target = self.crash_exposure
            elif mean >= self.score_threshold:
                target = self.pos_up
            else:
                target = self.pos_down

        self._target_exposure = max(
            0.0, min(1.0, float(target))
        )
        self._use_defensive = defensive

    def _should_rebalance(self, rebalance_days: int) -> bool:
        if self._force_rebalance:
            return True
        return super()._should_rebalance(rebalance_days)

    def get_risk_degree(self, *args, **kwargs):
        if self._target_exposure is not None:
            return max(
                0.0,
                min(
                    float(self._target_exposure),
                    float(getattr(self, "max_leverage", 1.0)),
                ),
            )
        return super().get_risk_degree(*args, **kwargs)

    # ------------------------------------------------------------------
    # 防御池
    # ------------------------------------------------------------------
    def _load_members(self):
        if self._intervals is not None:
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
            self._intervals = {
                str(sym).upper(): [
                    (pd.Timestamp(a), pd.Timestamp(b)) for a, b in intervals
                ]
                for sym, intervals in raw.items()
            }
        except Exception:  # noqa: BLE001
            logger.exception("dynamic_risk_topk: 加载防御池失败，回退全市场")
            self._intervals = {}

    def _members_on(self, day) -> set[str]:
        self._load_members()
        if not self._intervals:
            return set()
        day = pd.Timestamp(day).normalize()
        if day in self._member_cache:
            return self._member_cache[day]
        members = {
            sym
            for sym, intervals in self._intervals.items()
            if any(start <= day <= end for start, end in intervals)
        }
        self._member_cache[day] = members
        return members

    # ------------------------------------------------------------------
    # 选股 + 目标权重
    # ------------------------------------------------------------------
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

        self._score_mean = float(score.mean())

        if self._use_defensive:
            day = kwargs.get("trade_start_time") or kwargs.get("t_start")
            members = self._members_on(day) if day is not None else set()
            if members:
                filtered = score[
                    [str(s).upper() in members for s in score.index]
                ]
                if not filtered.empty:
                    score = filtered

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

    # ------------------------------------------------------------------
    # 每日风险检查 + 调仓闸门
    # ------------------------------------------------------------------
    def generate_trade_decision(self, execute_result=None):
        step = self._get_trade_step_safe() or 0
        scheduled = super()._should_rebalance(self.rebalance_days)
        self._update_regime(step)

        exposure_changed = (
            self._last_applied_exposure is None
            or abs(self._target_exposure - self._last_applied_exposure) >= 0.05
        )
        defensive_changed = self._use_defensive != self._last_applied_defensive
        force = exposure_changed or defensive_changed

        if force and not scheduled:
            logger.info(
                "dynamic_risk_topk: 风险档位变化强制调仓 exposure=%.2f defensive=%s",
                self._target_exposure,
                self._use_defensive,
            )

        self._force_rebalance = force
        try:
            decision = super().generate_trade_decision(execute_result)
        finally:
            self._force_rebalance = False

        if scheduled or force:
            self._last_applied_exposure = self._target_exposure
            self._last_applied_defensive = self._use_defensive
            bucket = (
                "crash"
                if self._target_exposure <= self.crash_exposure + 1e-9
                else "up"
                if self._target_exposure >= self.pos_up - 1e-9
                else "down"
            )
            if bucket != self._last_bucket:
                logger.info(
                    "dynamic_risk_topk: exposure=%.2f defensive=%s mean=%s bucket=%s",
                    self._target_exposure,
                    self._use_defensive,
                    self._score_mean,
                    bucket,
                )
                self._last_bucket = bucket
        return decision


STRATEGY_CONFIG = {
    "class": "DynamicRiskTopkStrategy",
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
        "crash_filter": True,
        "crash_exposure": 0.3,
        "crash_ret10": -0.06,
        "crash_ret5": -0.04,
        "crash_hold_days": 3,
    },
}
